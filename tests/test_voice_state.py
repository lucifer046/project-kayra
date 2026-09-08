# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_voice_state.py                             │
# │      The Voice State Machine — precedence, revisions, and the orb      │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_voice_state.py — standalone diagnostic for the authoritative voice state.

    .venv\\Scripts\\python tests\\test_voice_state.py

HARDWARE-FREE. No microphone, no browser, no Qt, no model. The machine takes facts and
returns a state, so every situation in this file — a barge-in, an STT recovery, standby,
shutdown, a stale callback — is expressed as a set of facts rather than staged with real
hardware, and can therefore be asserted exactly.

THE BUG THIS FILE EXISTS FOR. The assistant visual said "Listening paused" while the user was
talking to it. Not because any label was wrong, but because four surfaces each held part of
the truth and the screen showed whichever wrote last. So the checks below are mostly about
things that must NOT happen:

  * silence must never become PAUSED
  * a late transcript must never become STOPPED
  * an STT recovery must never become PAUSED
  * a stale callback must never repaint an older state
  * nothing must return to LISTENING once shutdown has begun

It exercises

  1. Every state, and the precedence between them.
  2. Silence, VAD and the LISTENING / USER_SPEAKING distinction.
  3. Barge-in.
  4. Recovery, standby, pause and shutdown, and the ways they must not be confused.
  5. Revisions and staleness.
  6. Debounce: enough to absorb one noisy sample, never enough to feel sluggish.
  7. The full sequences the brief names.
  8. The presentation tables, and the orb's amplitude contract.
  9. Cost, and the leaf-module rule.
 10. Integration with the real `app` wiring.
"""

import os
import re
import sys
import ast
import time
import inspect

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core import voice_state as vs
from kayra.core.voice_state import (
    VoiceState, VoiceStateMachine, STATES, STATE_TEXT, STATE_DETAIL,
    ORB_STATE, ORB_AMPLITUDE, CAPTURING_STATES, TERMINAL_STATES,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


class Clock:
    """A hand-advanced clock, so dwell times are tested at exact offsets instead of by sleeping."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


def machine(clock=None, **facts):
    """A machine primed with a working speech session, unless the caller says otherwise."""
    m = VoiceStateMachine(clock_ms=clock or Clock())
    base = {"assistant_state": "IDLE", "listening": True, "sleeping": False,
            "shutting_down": False, "voice_available": True,
            "backend_status": "LISTENING", "voice_active": False}
    base.update(facts)
    m.update(**base)
    return m


def resolve(**facts):
    """The pure precedence function, with sensible defaults for anything unspecified."""
    base = {"assistant_state": "IDLE", "listening": True, "sleeping": False,
            "shutting_down": False, "voice_available": True,
            "backend_status": "LISTENING", "voice_active": False}
    base.update(facts)
    return VoiceStateMachine._compute(base)[0]


# ──────────────────────────────────────────────────────────────────────────
#                         1. STATES AND PRECEDENCE
# ──────────────────────────────────────────────────────────────────────────

def section_states():
    print_system("\n[1] The states, and the order they outrank each other in")

    check("eleven states are declared", len(STATES) == 11, str(len(STATES)))
    for state in STATES:
        check(f"{state} has user-facing text", bool(STATE_TEXT.get(state)))
        check(f"{state} has a detail line", bool(STATE_DETAIL.get(state)))
        check(f"{state} maps to an orb state", bool(ORB_STATE.get(state)))
        check(f"{state} has an orb amplitude", state in ORB_AMPLITUDE)

    # The resting state.
    check("an open, quiet microphone is LISTENING", resolve() == VoiceState.LISTENING)

    # Precedence, top down. Each of these is a decision documented in `_compute`.
    check("shutdown outranks everything",
          resolve(shutting_down=True, sleeping=True, voice_active=True,
                  assistant_state="SPEAKING") == VoiceState.STOPPING)
    check("no speech input at all is OFFLINE",
          resolve(voice_available=False) == VoiceState.OFFLINE)
    check("standby outranks the turn machine",
          resolve(sleeping=True, assistant_state="SPEAKING") == VoiceState.STANDBY)
    check("standby outranks a paused microphone",
          resolve(sleeping=True, listening=False) == VoiceState.STANDBY)
    check("the TURN outranks the SESSION",
          resolve(assistant_state="PROCESSING", backend_status="RECOVERING")
          == VoiceState.PROCESSING,
          "a reply being generated is what the user is waiting on")
    check("automating reads as working, not as a separate thing to learn",
          resolve(assistant_state="AUTOMATING") == VoiceState.PROCESSING)
    check("a recovering session outranks a paused microphone",
          resolve(backend_status="RECOVERING", listening=False) == VoiceState.RECOVERING)
    check("an unusable session is ERROR",
          resolve(backend_status="ERROR") == VoiceState.ERROR)
    check("a starting session is STARTING",
          resolve(backend_status="STARTING") == VoiceState.STARTING)
    check("a deliberately closed microphone is PAUSED",
          resolve(listening=False) == VoiceState.PAUSED)
    check("a backend reporting PAUSED is PAUSED too",
          resolve(backend_status="PAUSED") == VoiceState.PAUSED)

    # Total: every combination resolves to a declared state, none of them to None.
    combos = 0
    for assistant in ("IDLE", "LISTENING", "PROCESSING", "SPEAKING", "INTERRUPTING",
                      "AUTOMATING", "SHUTTING_DOWN"):
        for backend in ("OFF", "STARTING", "LISTENING", "PAUSED", "RECOVERING",
                        "STOPPING", "ERROR"):
            for listening in (True, False):
                for sleeping in (True, False):
                    for voice in (True, False):
                        state = resolve(assistant_state=assistant, backend_status=backend,
                                        listening=listening, sleeping=sleeping,
                                        voice_active=voice)
                        combos += 1
                        if state not in STATES:
                            check(f"unresolved combination {assistant}/{backend}", False)
                            return
    check(f"all {combos} fact combinations resolve to a declared state", True)


# ──────────────────────────────────────────────────────────────────────────
#                    2. SILENCE IS STILL LISTENING
# ──────────────────────────────────────────────────────────────────────────

def section_silence():
    print_system("\n[2] Silence is still listening — the rule that was broken")

    check("a quiet open microphone is LISTENING, not PAUSED",
          resolve(voice_active=False) == VoiceState.LISTENING)
    check("VAD hearing a voice is USER_SPEAKING",
          resolve(voice_active=True) == VoiceState.USER_SPEAKING)
    check("LISTENING and USER_SPEAKING are different states",
          VoiceState.LISTENING != VoiceState.USER_SPEAKING)

    # Nothing but a deliberately closed microphone may produce PAUSED. Sweep every fact
    # combination that leaves `listening` True and the backend healthy.
    offenders = []
    for assistant in ("IDLE", "LISTENING"):
        for voice in (True, False):
            for backend in ("LISTENING",):
                state = resolve(assistant_state=assistant, voice_active=voice,
                                backend_status=backend, listening=True)
                if state == VoiceState.PAUSED:
                    offenders.append((assistant, voice, backend))
    check("no combination with an open microphone can produce PAUSED",
          not offenders, str(offenders))

    # A late transcript is not a fact this machine takes at all — there is no input for it,
    # which is the structural reason a delayed result cannot become STOPPED.
    facts = set(VoiceStateMachine().snapshot()["facts"])
    check("the machine takes no 'transcript' input at all",
          not any("transcript" in key or "result" in key for key in facts), str(sorted(facts)))
    check("and no 'silence' or 'timeout' input either",
          not any(key in ("silence", "timeout", "idle_seconds") for key in facts))

    # A long silence changes nothing, however long it lasts.
    clock = Clock()
    m = machine(clock)
    check("the resting state is LISTENING", m.state == VoiceState.LISTENING)
    start_revision = m.revision
    for _ in range(200):
        clock.advance(500)
        m.update(voice_active=False)
    check("100 seconds of silence produces no transition at all",
          m.revision == start_revision and m.state == VoiceState.LISTENING,
          f"rev {m.revision} state {m.state}")


# ──────────────────────────────────────────────────────────────────────────
#                            3. BARGE-IN
# ──────────────────────────────────────────────────────────────────────────

def section_barge_in():
    print_system("\n[3] Barge-in — the user takes the floor")

    check("a voice arriving over playback is USER_SPEAKING, not 'interrupted'",
          resolve(assistant_state="SPEAKING", voice_active=True) == VoiceState.USER_SPEAKING)
    check("the turn machine's INTERRUPTING is USER_SPEAKING too",
          resolve(assistant_state="INTERRUPTING") == VoiceState.USER_SPEAKING)
    check("speaking with nobody talking over it is ASSISTANT_SPEAKING",
          resolve(assistant_state="SPEAKING", voice_active=False)
          == VoiceState.ASSISTANT_SPEAKING)
    check("a barge-in never renders as a pause",
          resolve(assistant_state="INTERRUPTING", listening=True) != VoiceState.PAUSED)

    # The full sequence from the brief.
    clock = Clock()
    m = machine(clock)
    seen = []
    m.subscribe(lambda t: seen.append(t.state))

    m.update(assistant_state="SPEAKING")
    clock.advance(1200)
    m.update(voice_active=True)                    # the user speaks over the reply
    clock.advance(400)
    m.update(assistant_state="INTERRUPTING")
    clock.advance(400)
    m.update(assistant_state="PROCESSING", voice_active=False)

    check("ASSISTANT_SPEAKING -> USER_SPEAKING -> PROCESSING",
          seen == [VoiceState.ASSISTANT_SPEAKING, VoiceState.USER_SPEAKING,
                   VoiceState.PROCESSING], str(seen))
    check("PAUSED never appears in a barge-in", VoiceState.PAUSED not in seen)


# ──────────────────────────────────────────────────────────────────────────
#              4. RECOVERY, STANDBY, PAUSE AND SHUTDOWN
# ──────────────────────────────────────────────────────────────────────────

def section_lifecycle():
    print_system("\n[4] Recovery, standby, pause and shutdown are four different things")

    # RECOVERY must never read as a pause. This is the specific misreport in the brief.
    clock = Clock()
    m = machine(clock)
    seen = []
    m.subscribe(lambda t: seen.append(t.state))

    m.update(backend_status="RECOVERING")
    clock.advance(2500)
    m.update(backend_status="LISTENING")

    check("LISTENING -> RECOVERING -> LISTENING",
          seen == [VoiceState.RECOVERING, VoiceState.LISTENING], str(seen))
    check("a recovery never passes through PAUSED", VoiceState.PAUSED not in seen)
    check("RECOVERING says 'Reconnecting', not 'paused'",
          "econnect" in STATE_TEXT[VoiceState.RECOVERING]
          and "paused" not in STATE_TEXT[VoiceState.RECOVERING].lower(),
          STATE_TEXT[VoiceState.RECOVERING])

    # A recovery WHILE the microphone happens to be paused is still a recovery: reconnecting
    # is the more urgent and more informative fact.
    check("recovery outranks a paused microphone",
          resolve(backend_status="RECOVERING", listening=False) == VoiceState.RECOVERING)

    # STANDBY: the microphone stays open, which is why it is a separate state from PAUSED.
    clock2 = Clock()
    m2 = machine(clock2)
    seen2 = []
    m2.subscribe(lambda t: seen2.append(t.state))
    m2.update(sleeping=True)
    clock2.advance(3000)
    m2.update(sleeping=False, backend_status="STARTING")
    clock2.advance(3000)
    m2.update(backend_status="LISTENING")
    check("LISTENING -> STANDBY -> STARTING -> LISTENING",
          seen2 == [VoiceState.STANDBY, VoiceState.STARTING, VoiceState.LISTENING],
          str(seen2))
    check("standby is a capturing state — the microphone stays open for 'wake up'",
          VoiceState.STANDBY in CAPTURING_STATES)
    check("a paused microphone is NOT a capturing state",
          VoiceState.PAUSED not in CAPTURING_STATES)
    check("standby and pause share their words but not their state",
          STATE_TEXT[VoiceState.STANDBY] == STATE_TEXT[VoiceState.PAUSED]
          and VoiceState.STANDBY != VoiceState.PAUSED)
    check("but their detail lines differ, because what to do about them differs",
          STATE_DETAIL[VoiceState.STANDBY] != STATE_DETAIL[VoiceState.PAUSED])

    # SHUTDOWN is absorbing. Nothing gets back to LISTENING.
    clock3 = Clock()
    m3 = machine(clock3)
    m3.update(assistant_state="SPEAKING")
    m3.update(shutting_down=True)
    check("shutdown reaches STOPPING from any state", m3.state == VoiceState.STOPPING)

    for facts in ({"voice_active": True}, {"assistant_state": "LISTENING"},
                  {"listening": True, "backend_status": "LISTENING"},
                  {"sleeping": False}, {"backend_status": "RECOVERING"}):
        clock3.advance(2000)
        m3.update(**facts)
        if m3.state != VoiceState.STOPPING:
            break
    check("no fact can take it back out of STOPPING",
          m3.state == VoiceState.STOPPING, m3.state)
    check("a late VAD sample after shutdown cannot repaint a live microphone",
          m3.state != VoiceState.LISTENING and m3.state != VoiceState.USER_SPEAKING)

    # OFFLINE is the one permitted exit, and it is where teardown ends.
    m3.update(shutting_down=False, voice_available=False, backend_status="OFF")
    check("STOPPING -> OFFLINE is permitted, and is where it ends",
          m3.state == VoiceState.OFFLINE, m3.state)
    m3.update(voice_available=True, backend_status="LISTENING")
    check("and OFFLINE does not go back to LISTENING either",
          m3.state == VoiceState.OFFLINE, m3.state)
    check("both terminal states are declared as such",
          TERMINAL_STATES == {VoiceState.STOPPING, VoiceState.OFFLINE})

    # PAUSE: explicit, immediate, and not debounced.
    clock4 = Clock()
    m4 = machine(clock4)
    m4.update(voice_active=True)
    check("the user is speaking", m4.state == VoiceState.USER_SPEAKING)
    transition = m4.update(listening=False, voice_active=False)
    check("closing the microphone is immediate, never held back by a dwell timer",
          transition is not None and transition.state == VoiceState.PAUSED,
          str(transition))
    check("PAUSED is never delayed by a dwell",
          VoiceState.PAUSED in VoiceStateMachine.IMMEDIATE_STATES)
    check("nor are STANDBY, STOPPING, OFFLINE or ERROR",
          {VoiceState.STANDBY, VoiceState.STOPPING, VoiceState.OFFLINE, VoiceState.ERROR}
          <= VoiceStateMachine.IMMEDIATE_STATES)
    check("and neither is anything the assistant is doing on the user's behalf",
          {VoiceState.PROCESSING, VoiceState.ASSISTANT_SPEAKING, VoiceState.USER_SPEAKING}
          <= VoiceStateMachine.IMMEDIATE_STATES)
    check("only resting states can ever be delayed",
          not (VoiceStateMachine.IMMEDIATE_STATES
               & {VoiceState.LISTENING, VoiceState.STARTING, VoiceState.RECOVERING}))


# ──────────────────────────────────────────────────────────────────────────
#                     5. REVISIONS AND STALENESS
# ──────────────────────────────────────────────────────────────────────────

def section_revisions():
    print_system("\n[5] Revisions — a stale callback cannot repaint an older state")

    clock = Clock()
    m = machine(clock)
    start = m.revision

    revisions = []
    m.subscribe(lambda t: revisions.append(t.revision))

    for facts in ({"voice_active": True}, {"assistant_state": "PROCESSING",
                                           "voice_active": False},
                  {"assistant_state": "SPEAKING"}, {"assistant_state": "IDLE"}):
        clock.advance(1000)
        m.update(**facts)

    check("every committed transition increments the revision",
          revisions == list(range(start + 1, start + 1 + len(revisions))), str(revisions))
    check("revisions are strictly monotonic",
          all(b > a for a, b in zip(revisions, revisions[1:])))
    check("a no-op update does NOT burn a revision",
          m.update(voice_active=False) is None and m.revision == revisions[-1])

    current = m.revision
    check("the current revision is not stale", m.is_stale(current) is False)
    check("an older revision IS stale", m.is_stale(current - 1) is True)
    check("a much older revision is stale", m.is_stale(1) is True)
    check("None is treated as stale", m.is_stale(None) is True)

    # The rule a consumer implements, simulated: render only what is newer.
    rendered = {"revision": -1, "state": None}

    def render(state, revision):
        if revision <= rendered["revision"]:
            return False
        rendered.update(revision=revision, state=state)
        return True

    check("a newer transition renders", render(VoiceState.PROCESSING, 100) is True)
    check("a stale one is dropped", render(VoiceState.PAUSED, 42) is False)
    check("and the newer state survives", rendered["state"] == VoiceState.PROCESSING)
    check("a repeat of the current revision is also dropped",
          render(VoiceState.PAUSED, 100) is False)

    # The transition object carries everything a consumer needs.
    clock2 = Clock()
    m2 = machine(clock2)
    clock2.advance(1000)
    transition = m2.update(voice_active=True)
    check("a transition carries its previous state",
          transition.previous == VoiceState.LISTENING)
    check("a transition carries its own text and detail",
          transition.text == STATE_TEXT[VoiceState.USER_SPEAKING]
          and transition.detail == STATE_DETAIL[VoiceState.USER_SPEAKING])
    check("a transition carries a reason, for diagnostics", bool(transition.reason))
    check("to_dict has everything a bridge forwards",
          set(transition.to_dict()) >= {"state", "previous", "revision", "text", "detail"})


# ──────────────────────────────────────────────────────────────────────────
#                            6. DEBOUNCE
# ──────────────────────────────────────────────────────────────────────────

def section_debounce():
    print_system("\n[6] Debounce — absorbs one noisy sample, never feels sluggish")

    clock = Clock()
    m = machine(clock)

    # A VAD dip between two words must not flicker back to LISTENING.
    clock.advance(1000)
    m.update(voice_active=True)
    check("speech shows immediately", m.state == VoiceState.USER_SPEAKING)
    clock.advance(120)
    m.update(voice_active=False)
    check("a 120ms gap between words does not flicker back to LISTENING",
          m.state == VoiceState.USER_SPEAKING, m.state)
    clock.advance(80)
    m.update(voice_active=True)
    check("and the next word is already covered", m.state == VoiceState.USER_SPEAKING)
    clock.advance(500)
    m.update(voice_active=False)
    check("a genuine end of speech does return to LISTENING",
          m.state == VoiceState.LISTENING, m.state)

    # RISING activity is never debounced: the recording indicator has to be instant.
    clock2 = Clock()
    m2 = machine(clock2)
    clock2.advance(10)
    transition = m2.update(voice_active=True)
    check("rising activity is instant, with no dwell at all",
          transition is not None and transition.state == VoiceState.USER_SPEAKING)

    # A short recovery must not flash.
    clock3 = Clock()
    m3 = machine(clock3)
    m3.update(backend_status="RECOVERING")
    clock3.advance(150)
    m3.update(backend_status="LISTENING")
    check("a 150ms recovery does not flash on screen",
          m3.state == VoiceState.RECOVERING, m3.state)
    clock3.advance(400)
    m3.update(backend_status="LISTENING")
    check("but a completed recovery does return to LISTENING",
          m3.state == VoiceState.LISTENING, m3.state)

    # No dwell may be long enough to feel like lag.
    for state, dwell in VoiceStateMachine.DWELL_MS.items():
        check(f"{state} dwell is under half a second", dwell < 500, f"{dwell}ms")

    # The oscillation from the brief: five alternations inside a few hundred ms must not
    # produce five renders.
    clock4 = Clock()
    m4 = machine(clock4)
    committed = []
    m4.subscribe(lambda t: committed.append(t.state))
    for _ in range(5):
        clock4.advance(40)
        m4.update(voice_active=True)
        clock4.advance(40)
        m4.update(voice_active=False)
    # 400ms of 40ms-alternating VAD. The guarantee is roughly one change per dwell window,
    # not zero: the user genuinely IS starting and stopping, and pinning the display for
    # longer would be the sluggishness the short dwell exists to avoid.
    check("rapid VAD alternation is capped at about one change per dwell window",
          len(committed) <= 3, str(committed))
    check("and none of them is PAUSED", VoiceState.PAUSED not in committed)


# ──────────────────────────────────────────────────────────────────────────
#                       7. THE FULL SEQUENCES
# ──────────────────────────────────────────────────────────────────────────

def section_sequences():
    print_system("\n[7] The sequences named in the brief, end to end")

    clock = Clock()
    m = VoiceStateMachine(clock_ms=clock)
    seen = []
    m.subscribe(lambda t: seen.append(t.state))

    def step(ms=600, **facts):
        clock.advance(ms)
        m.update(**facts)

    # OFFLINE -> STARTING -> LISTENING -> USER_SPEAKING -> LISTENING -> PROCESSING
    #         -> ASSISTANT_SPEAKING -> USER_SPEAKING -> PROCESSING -> LISTENING
    #
    # The machine STARTS in OFFLINE, so that is the initial condition rather than a
    # transition — `seen` records changes, and a state does not transition into itself.
    check("a fresh machine starts OFFLINE", m.state == VoiceState.OFFLINE)
    step(voice_available=False, backend_status="OFF")
    check("and declaring no speech input changes nothing", seen == [], str(seen))
    step(voice_available=True, backend_status="STARTING")
    step(backend_status="LISTENING")
    step(voice_active=True)
    step(voice_active=False)
    step(assistant_state="PROCESSING")
    step(assistant_state="SPEAKING")
    step(voice_active=True)                       # barge-in
    step(assistant_state="PROCESSING", voice_active=False)
    step(assistant_state="IDLE")

    expected = [VoiceState.STARTING, VoiceState.LISTENING,
                VoiceState.USER_SPEAKING, VoiceState.LISTENING, VoiceState.PROCESSING,
                VoiceState.ASSISTANT_SPEAKING, VoiceState.USER_SPEAKING,
                VoiceState.PROCESSING, VoiceState.LISTENING]
    check("the full interaction sequence is exactly as specified",
          seen == expected, f"\n  got      {seen}\n  expected {expected}")

    # LISTENING -> RECOVERING -> LISTENING
    clock2 = Clock()
    m2 = machine(clock2)
    seen2 = []
    m2.subscribe(lambda t: seen2.append(t.state))
    clock2.advance(600); m2.update(backend_status="RECOVERING")
    clock2.advance(600); m2.update(backend_status="LISTENING")
    check("LISTENING -> RECOVERING -> LISTENING",
          seen2 == [VoiceState.RECOVERING, VoiceState.LISTENING], str(seen2))

    # LISTENING -> STANDBY -> STARTING -> LISTENING
    clock3 = Clock()
    m3 = machine(clock3)
    seen3 = []
    m3.subscribe(lambda t: seen3.append(t.state))
    clock3.advance(600); m3.update(sleeping=True)
    clock3.advance(600); m3.update(sleeping=False, backend_status="STARTING")
    clock3.advance(600); m3.update(backend_status="LISTENING")
    check("LISTENING -> STANDBY -> STARTING -> LISTENING",
          seen3 == [VoiceState.STANDBY, VoiceState.STARTING, VoiceState.LISTENING],
          str(seen3))

    # ANY -> STOPPING -> OFFLINE, from every state there is.
    for origin in STATES:
        if origin in TERMINAL_STATES:
            continue
        c = Clock()
        mm = VoiceStateMachine(clock_ms=c)
        mm.update(**_facts_for(origin))
        c.advance(1000)
        mm.update(shutting_down=True)
        if mm.state != VoiceState.STOPPING:
            check(f"{origin} -> STOPPING", False, mm.state)
            break
    else:
        check("every non-terminal state reaches STOPPING", True)

    c = Clock()
    mm = machine(c)
    c.advance(600); mm.update(shutting_down=True)
    c.advance(600); mm.update(shutting_down=False, voice_available=False,
                              backend_status="OFF")
    check("and STOPPING reaches OFFLINE", mm.state == VoiceState.OFFLINE)


def _facts_for(state):
    """The facts that produce a given state — used to start a sequence from each one."""
    base = {"assistant_state": "IDLE", "listening": True, "sleeping": False,
            "shutting_down": False, "voice_available": True,
            "backend_status": "LISTENING", "voice_active": False}
    overrides = {
        VoiceState.STARTING: {"backend_status": "STARTING"},
        VoiceState.LISTENING: {},
        VoiceState.USER_SPEAKING: {"voice_active": True},
        VoiceState.PROCESSING: {"assistant_state": "PROCESSING"},
        VoiceState.ASSISTANT_SPEAKING: {"assistant_state": "SPEAKING"},
        VoiceState.PAUSED: {"listening": False},
        VoiceState.STANDBY: {"sleeping": True},
        VoiceState.RECOVERING: {"backend_status": "RECOVERING"},
        VoiceState.ERROR: {"backend_status": "ERROR"},
    }
    base.update(overrides.get(state, {}))
    return base


# ──────────────────────────────────────────────────────────────────────────
#                    8. PRESENTATION AND THE ORB
# ──────────────────────────────────────────────────────────────────────────

def section_presentation():
    print_system("\n[8] What the user is told, and what the orb shows")

    expected_text = {
        VoiceState.STARTING: "Starting",
        VoiceState.LISTENING: "Listening",
        VoiceState.PROCESSING: "Thinking",
        VoiceState.ASSISTANT_SPEAKING: "Speaking",
        VoiceState.PAUSED: "Listening paused",
        VoiceState.STANDBY: "Listening paused",
        VoiceState.STOPPING: "Shutting down",
        VoiceState.OFFLINE: "Offline",
        VoiceState.ERROR: "Speech input unavailable",
    }
    for state, text in expected_text.items():
        check(f"{state} reads as {text!r}", STATE_TEXT[state] == text, STATE_TEXT[state])
    check("USER_SPEAKING is a listening variant, not a different word",
          STATE_TEXT[VoiceState.USER_SPEAKING].startswith("Listening"),
          STATE_TEXT[VoiceState.USER_SPEAKING])
    check("RECOVERING reads as reconnecting",
          "econnect" in STATE_TEXT[VoiceState.RECOVERING])

    # Only two states may ever say "paused".
    paused_words = {s for s, t in STATE_TEXT.items() if "paused" in t.lower()}
    check("exactly two states say 'paused', and both mean it",
          paused_words == {VoiceState.PAUSED, VoiceState.STANDBY}, str(paused_words))
    check("no state says 'stopped' except the terminal ones",
          not any("stopped" in STATE_TEXT[s].lower()
                  for s in STATES if s not in TERMINAL_STATES))

    # The orb: USER_SPEAKING is the loudest listening value, which IS the recording signal.
    check("USER_SPEAKING is louder than LISTENING",
          ORB_AMPLITUDE[VoiceState.USER_SPEAKING] > ORB_AMPLITUDE[VoiceState.LISTENING],
          f"{ORB_AMPLITUDE[VoiceState.USER_SPEAKING]} vs {ORB_AMPLITUDE[VoiceState.LISTENING]}")
    check("both use the SAME animation, distinguished by energy",
          ORB_STATE[VoiceState.USER_SPEAKING] == ORB_STATE[VoiceState.LISTENING] == "LISTENING")
    check("LISTENING is much louder than PAUSED",
          ORB_AMPLITUDE[VoiceState.LISTENING] > ORB_AMPLITUDE[VoiceState.PAUSED] * 3)
    check("every amplitude is within 0..1",
          all(0.0 <= v <= 1.0 for v in ORB_AMPLITUDE.values()))
    check("STOPPING and OFFLINE are the quietest",
          max(ORB_AMPLITUDE[VoiceState.STOPPING], ORB_AMPLITUDE[VoiceState.OFFLINE])
          <= min(ORB_AMPLITUDE[s] for s in STATES
                 if s not in (VoiceState.STOPPING, VoiceState.OFFLINE)))

    # Every orb state the machine can emit must be one the theme actually has a colour for.
    from kayra.ui.theme.tokens import STATE_COLORS
    for state in STATES:
        check(f"the theme has a colour for {ORB_STATE[state]}",
              ORB_STATE[state] in STATE_COLORS, ORB_STATE[state])

    # The orb renders amplitude and decides nothing.
    from kayra.ui.components.orb import AssistantOrb
    source = inspect.getsource(AssistantOrb)
    check("the orb accepts an amplitude", "amplitude" in source)
    # The orb must not REASON about the microphone. It is handed a visual state and an
    # amplitude; it has no access to whether listening is paused and no way to infer one.
    check("the orb never reads the microphone state",
          "listening_paused" not in source and "listening_enabled" not in source)
    check("the orb never mentions being paused", "paused" not in source.lower())
    check("the orb never reads the runtime state",
          "get_runtime_state" not in source and "RuntimeState" not in source)
    # The CODE, not the prose. The class docstring explains where its state comes from, which
    # is exactly the documentation this rule deserves — what must not exist is a reference.
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))
    check("the orb holds no bridge and no engine",
          "self.bridge" not in code and "stt_engine" not in code
          and "tts_engine" not in code)


# ──────────────────────────────────────────────────────────────────────────
#                     9. COST AND THE LEAF RULE
# ──────────────────────────────────────────────────────────────────────────

def section_cost():
    print_system("\n[9] Cost, and the leaf-module rule")

    source = inspect.getsource(vs)
    tree = ast.parse(source)
    imports = {n.names[0].name.split(".")[0]
               for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imports |= {(n.module or "").split(".")[0]
                for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    check("the module imports only the stdlib", imports <= {"time", "threading"},
          str(sorted(imports)))
    check("it imports nothing from kayra", not any(i == "kayra" for i in imports))
    check("it starts no thread", "threading.Thread" not in source)
    check("it owns no timer", "Timer" not in source and "QTimer" not in source)
    check("it persists nothing",
          not re.search(r"\bopen\(|json\.dump|\.write\(", source))
    check("it holds no engine, driver or client",
          not any(word in source for word in ("driver", "tts_engine", "stt_engine",
                                              "cohere", "onnxruntime")))

    clock = Clock()
    m = machine(clock)
    iterations = 50000
    started = time.perf_counter()
    for index in range(iterations):
        clock.advance(1)
        m.update(voice_active=bool(index % 2))
    per_call = (time.perf_counter() - started) / iterations * 1e6
    check("update() costs well under 30us", per_call < 30.0, f"{per_call:.2f}us")

    started = time.perf_counter()
    for _ in range(20000):
        m.snapshot()
    per_call = (time.perf_counter() - started) / 20000 * 1e6
    check("snapshot() costs well under 40us", per_call < 40.0, f"{per_call:.2f}us")

    # A subscriber that raises must not be able to wedge a transition.
    clock2 = Clock()
    m2 = machine(clock2)
    delivered = []

    def bad(_t):
        raise RuntimeError("listener bug")

    m2.subscribe(bad)
    m2.subscribe(lambda t: delivered.append(t.state))
    clock2.advance(1000)
    m2.update(voice_active=True)
    check("a subscriber that raises does not stop the others",
          delivered == [VoiceState.USER_SPEAKING], str(delivered))
    check("and the state still committed", m2.state == VoiceState.USER_SPEAKING)

    check("the process accessor returns one machine",
          vs.get_voice_state() is vs.get_voice_state())


# ──────────────────────────────────────────────────────────────────────────
#                     10. INTEGRATION WITH `app`
# ──────────────────────────────────────────────────────────────────────────

def section_integration():
    print_system("\n[10] The wiring in `kayra.app`")

    import kayra.app as app

    for name in ("_voice_facts", "_refresh_voice_state", "_on_runtime_voice_event",
                 "_on_backend_changed", "voice_runtime_state"):
        check(f"app exposes {name}", hasattr(app, name))

    source = inspect.getsource(app)
    check("the runtime bus feeds the voice state",
          "RUNTIME.subscribe(_on_runtime_voice_event)" in source)
    check("the control watcher publishes VAD activity",
          "voice_active=bool(getattr(stt_engine" in source)
    check("shutdown drives the voice state to STOPPING",
          "_refresh_voice_state(shutting_down=True" in source)

    # ONE transition log, in one place, at INFO. Not per frame and not in the UI.
    check("transitions are logged where they are committed",
          "State: {transition.previous} -> {transition.state}" in source)
    log_sites = source.count("State: {transition.previous}")
    check("and exactly once", log_sites == 1, str(log_sites))

    # The UI must render, never resolve.
    from kayra.ui.views import home
    from kayra.ui import application as ui_app
    for module, label in ((home, "home"), (ui_app, "application")):
        text = inspect.getsource(module)
        check(f"{label} guards against a stale revision",
              "revision <= self._voice_revision" in text)
        check(f"{label} does not compose its own paused caption",
              'setText("Listening paused")' not in text, label)

    check("home no longer keeps a caption-composing method",
          not hasattr(home.HomeView, "_refresh_caption"))

    # `app.voice_runtime_state` returns the whole picture the brief asks for.
    snapshot = app.voice_runtime_state()
    for key in ("capture_active", "vad_active", "stt_backend", "stt_status",
                "runtime_state", "revision", "last_transition", "last_error", "state"):
        check(f"voice_runtime_state carries {key}", key in snapshot, str(sorted(snapshot)))


def main():
    print_banner("VOICE STATE DIAGNOSTIC", "Precedence · revisions · debounce · the orb")
    section_states()
    section_silence()
    section_barge_in()
    section_lifecycle()
    section_revisions()
    section_debounce()
    section_sequences()
    section_presentation()
    section_cost()
    section_integration()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All voice state checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
