# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_proactive_agent.py                          │
# │        Proactive Service — State, Safety, Scoring, Habits, Teardown    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_proactive_agent.py — assertion suite for the proactive subsystem.

Like the other scripts in tests/, this is a standalone entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_proactive_agent.py

It exits non-zero if anything fails.

It needs NO audio hardware, NO microphone, NO network and NO LLM: the agent takes its
speech and phrasing collaborators as callables, so every decision it makes is driven here
with fakes and a fake clock. That is the point of the injection seams — the policy that
decides when the assistant talks unprompted has to be testable deterministically.

Habit state is redirected to a temporary directory so a test run can never touch the real
`data/habits.json`.
"""

import os
import sys
import time
import json
import shutil
import tempfile
import threading

# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core.runtime_state import RuntimeState, AssistantState


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    HOST ISOLATION — read this first                    │
# └────────────────────────────────────────────────────────────────────────┘
# THIS SUITE IS TIER 1: hardware-free, and its results must not depend on the machine it runs
# on. `system_profile.pressure_sample()` reads the REAL battery, CPU and memory, and the
# presence layer turns a low battery into a CRITICAL candidate — which by design outranks
# every candidate the tests below are trying to exercise.
#
# That is the presence layer working correctly and the SUITE being wrong. Caught the honest
# way: the run was green all afternoon and then failed 9 checks, on unchanged code, because
# the laptop had dropped to 12% and unplugged. A suite whose verdict depends on the charge
# level is not a suite anybody can trust.
#
# Pinned to a healthy, plugged-in machine under no load. The battery, CPU and memory
# THRESHOLDS are still exercised — the tests that care drive `pressure_sample` directly with
# the values they need — but nothing is decided by the host's own state.
def _pin_host_environment():
    from kayra.core import system_profile

    def stable_sample(max_age=20.0):
        return {"cpu_percent": 8.0, "ram_percent": 42.0,
                "battery_percent": 88.0, "battery_plugged": True}

    system_profile.pressure_sample = stable_sample
    try:
        import kayra.intelligence.proactive_presence as presence
        presence.pressure_sample = stable_sample
    except Exception:
        pass                    # the presence layer is optional; the agent runs without it


_pin_host_environment()
import kayra.services.proactive_agent as pa
from kayra.services.proactive_agent import (ProactiveAgent, ProactiveConfig, ProactiveState,
                                     HabitStore, Candidate, normalize_app_name,
                                     _habit_key, _validate_phrasing)

FAILURES = []
_TMP = tempfile.mkdtemp(prefix="kayra-proactive-test-")


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               FIXTURES                                 │
# └────────────────────────────────────────────────────────────────────────┘

class FakeClock:
    """Monotonic clock the test drives by hand, so cooldowns are exact rather than slept."""

    def __init__(self, start=None):
        # Anchored to a real epoch so datetime.fromtimestamp() gives a sensible hour.
        self.t = start if start is not None else time.time()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class FakeSpeaker:
    """Stands in for the TTS engine: records what would have been spoken."""

    def __init__(self):
        self.spoken = []
        self.playing = False
        self.begin_turn_calls = 0
        self.background_calls = 0

    def speak(self, text):
        if self.playing:
            return False
        self.background_calls += 1
        self.spoken.append(text)
        return True

    def is_playing(self):
        return self.playing


def at_hour(hour, minute=30):
    """Epoch seconds for today at a specific local hour — the time signals are hour-based."""
    import datetime as _dt
    now = _dt.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now.timestamp()


def make_agent(clock=None, speaker=None, runtime=None, habits_name="habits.json", **overrides):
    """Builds an agent with an isolated habit file and fully controllable collaborators."""
    config = ProactiveConfig()
    # Deterministic, test-friendly defaults. Individual tests override what they exercise.
    config.enabled = True
    config.tick_seconds = 0.05
    config.defer_poll_seconds = 0.05
    config.global_cooldown_s = 3600.0
    config.repeat_cooldown_s = 21600.0
    config.break_cooldown_s = 5400.0
    config.fatigue_seconds = 5400.0
    config.score_threshold = 0.6
    config.quiet_after_interaction_s = 90.0
    config.max_defer_s = 600.0
    config.llm_phrasing = False
    config.late_night_enabled = True
    config.save_interval_s = 100000.0     # never auto-flush during a test
    for key, value in overrides.items():
        setattr(config, key, value)

    speaker = speaker or FakeSpeaker()
    clock = clock or FakeClock()
    # The runtime shares the SAME fake clock, otherwise the agent would measure focus time on
    # the fake clock while the safety gate measured "seconds since the user spoke" on the
    # real one, and no offset in the test would mean anything.
    runtime = runtime or RuntimeState(clock_ms=lambda: clock() * 1000.0)
    runtime.set_state(AssistantState.LISTENING)

    agent = ProactiveAgent(runtime=runtime, speak_fn=speaker.speak,
                           is_speaking_fn=speaker.is_playing,
                           config=config, clock=clock)
    # Redirect persistence away from the real project data directory.
    agent.habits = HabitStore(os.path.join(_TMP, habits_name))
    # Do NOT sample the real desktop: on a developer machine `getActiveWindow()` returns the
    # terminal this test is running in, which would overwrite every foreground-window fixture
    # on the first tick. Tests that exercise the context signal set the fields directly.
    agent.context_available = False
    return agent, speaker, runtime


def put_in_deep_focus(agent, app="Visual Studio Code", seconds=None, familiar=True):
    """Puts the agent in the state a long uninterrupted coding session would produce."""
    seconds = seconds if seconds is not None else agent.config.fatigue_seconds + 600
    agent.current_app = app
    agent._app_since = agent._clock() - seconds
    agent._app_committed_at = agent._clock()
    if familiar:
        # Enough recorded history for the app to read as one the user genuinely works in.
        agent.habits.record_app_time(app, agent.config.fatigue_seconds * 4)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        1. STATE & LIFECYCLE                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_state():
    print_system("\n[1] State model and lifecycle")

    agent, _, _ = make_agent(enabled=False)
    check("disabled config -> DISABLED state", agent.state == ProactiveState.DISABLED)
    check("disabled agent refuses to start", agent.start() is False)
    check("disabled agent has no thread", agent._thread is None)

    agent, _, _ = make_agent()
    check("enabled agent constructs IDLE", agent.state == ProactiveState.IDLE)
    check("start() succeeds", agent.start() is True)
    check("started agent is OBSERVING", agent.state == ProactiveState.OBSERVING)
    check("exactly one proactive thread", _proactive_thread_count() == 1,
          f"(found {_proactive_thread_count()})")
    check("start() is idempotent", agent.start() is True and _proactive_thread_count() == 1)

    agent.stop()
    check("stop() reaches STOPPED", agent.state == ProactiveState.STOPPED)
    check("no proactive thread survives stop", _proactive_thread_count() == 0)

    # Restart after a full stop must work — the session-level enable/disable relies on it.
    check("restart after stop", agent.start() is True and agent.state == ProactiveState.OBSERVING)
    agent.stop()
    check("restart then stop leaves no thread", _proactive_thread_count() == 0)


def _proactive_thread_count():
    return sum(1 for t in threading.enumerate()
               if t.name == "kayra-proactive" and t.is_alive())


def section_voice_control():
    print_system("\n[2] Session enable/disable (voice control)")

    agent, speaker, runtime = make_agent()
    agent.start()
    agent.set_enabled(False)
    check("set_enabled(False) -> DISABLED", agent.state == ProactiveState.DISABLED)
    check("disabled agent reports not enabled", agent.enabled is False)

    ok, reason = agent.is_safe_window()
    check("disabled agent never has a safe window", ok is False and reason == "disabled")

    put_in_deep_focus(agent)
    agent.tick()
    check("disabled agent produces no candidate", agent._pending is None)
    check("disabled agent speaks nothing", speaker.spoken == [])

    agent.set_enabled(True)
    check("set_enabled(True) resumes observing", agent.state == ProactiveState.OBSERVING)
    check("re-enable does not duplicate the thread", _proactive_thread_count() == 1,
          f"(found {_proactive_thread_count()})")
    agent.stop()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       3. NEVER INTERRUPT THE USER                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_safety_gate():
    print_system("\n[3] Safety gate — never interrupt the user")

    clock = FakeClock(at_hour(15))
    agent, speaker, runtime = make_agent(clock=clock)
    put_in_deep_focus(agent)

    # Baseline: idle, quiet for a long time -> safe.
    runtime.set_state(AssistantState.LISTENING)
    ok, reason = agent.is_safe_window()
    check("idle + quiet is a safe window", ok is True, reason)

    busy_cases = [
        (AssistantState.PROCESSING, "Kayra processing a command"),
        (AssistantState.SPEAKING, "Kayra speaking"),
        (AssistantState.INTERRUPTING, "Kayra was interrupted"),
        (AssistantState.AUTOMATING, "automation executing"),
        (AssistantState.SHUTTING_DOWN, "shutting down"),
    ]
    for state, label in busy_cases:
        runtime.set_state(state)
        ok, reason = agent.is_safe_window()
        check(f"no proactive speech while {label}", ok is False, f"({reason})")
    runtime.set_state(AssistantState.LISTENING)

    # A turn in flight blocks even if the state has drifted back to LISTENING.
    runtime.begin_turn()
    ok, _ = agent.is_safe_window()
    check("no proactive speech while a turn is in flight", ok is False)
    runtime.end_turn()

    # A just-ended turn is still too close to speak into.
    ok, reason = agent.is_safe_window()
    check("no proactive speech immediately after a turn ends", ok is False, f"({reason})")
    clock.advance(agent.config.quiet_after_interaction_s + 1)
    ok, _ = agent.is_safe_window()
    check("safe again once the quiet period elapses", ok is True)

    # Audio still draining counts as speaking, even in LISTENING state.
    speaker.playing = True
    ok, reason = agent.is_safe_window()
    check("no proactive speech while audio is still playing", ok is False, f"({reason})")
    speaker.playing = False

    # A barge-in gives the user the floor.
    runtime.note_interrupt()
    ok, reason = agent.is_safe_window()
    check("no proactive speech right after a barge-in", ok is False, f"({reason})")
    clock.advance(agent.config.quiet_after_interaction_s + 1)
    check("safe again well after the barge-in", agent.is_safe_window()[0] is True)

    # The user speaking blocks it too.
    runtime.note_user_utterance()
    check("no proactive speech right after the user spoke",
          agent.is_safe_window()[0] is False)


def section_deferral():
    print_system("\n[4] Deferral instead of interruption")

    clock = FakeClock(at_hour(15))
    agent, speaker, runtime = make_agent(clock=clock)
    put_in_deep_focus(agent)

    runtime.set_state(AssistantState.SPEAKING)
    agent.tick()
    check("candidate is held, not spoken, while busy",
          agent._pending is not None and speaker.spoken == [])
    check("state is WAITING_FOR_SAFE_WINDOW",
          agent.state == ProactiveState.WAITING_FOR_SAFE_WINDOW)

    # Once the assistant goes quiet the held candidate is delivered.
    runtime.set_state(AssistantState.LISTENING)
    clock.advance(agent.config.quiet_after_interaction_s + 1)
    agent.tick()
    check("deferred candidate speaks once it is safe", len(speaker.spoken) == 1,
          str(speaker.spoken))
    check("state moves to COOLDOWN after speaking", agent.state == ProactiveState.COOLDOWN)

    # A candidate that never finds a safe window is dropped, not queued forever.
    clock2 = FakeClock(at_hour(15))
    agent2, speaker2, runtime2 = make_agent(clock=clock2, max_defer_s=120.0)
    put_in_deep_focus(agent2)
    runtime2.set_state(AssistantState.SPEAKING)
    agent2.tick()
    check("second agent holds a candidate", agent2._pending is not None)
    clock2.advance(200)
    agent2.tick()
    check("stale candidate is dropped, never spoken",
          agent2._pending is None and speaker2.spoken == [])
    check("drop is counted", agent2.stats["dropped"] >= 1)

    # A barge-in cancels a waiting candidate outright.
    clock3 = FakeClock(at_hour(15))
    agent3, speaker3, runtime3 = make_agent(clock=clock3)
    put_in_deep_focus(agent3)
    runtime3.set_state(AssistantState.SPEAKING)
    agent3.tick()
    check("third agent holds a candidate", agent3._pending is not None)
    agent3.on_event("barge_in", {"text": "stop"})
    check("barge-in discards the waiting candidate", agent3._pending is None)
    check("nothing was spoken over the interruption", speaker3.spoken == [])


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     5. SCORING & CONTEXT SUFFICIENCY                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_scoring():
    print_system("\n[5] Relevance scoring")

    clock = FakeClock(at_hour(15))
    agent, _, _ = make_agent(clock=clock)

    # HARD REQUIREMENT: active-window information alone must never reach the threshold.
    context_only = Candidate("break", "x", {"habit": 0.0, "temporal": 0.0,
                                            "context": 1.0, "recency": 0.0})
    score = agent.score(context_only)
    check("context alone cannot clear the threshold",
          score < agent.config.score_threshold, f"(score {score:.2f})")

    full = Candidate("break", "x", {"habit": 1.0, "temporal": 1.0,
                                    "context": 1.0, "recency": 1.0})
    check("all signals aligned scores 1.0", agent.score(full) == 1.0,
          f"(score {agent.score(full):.2f})")

    check("scoring is deterministic", agent.score(full) == agent.score(full))

    # Real candidate paths.
    put_in_deep_focus(agent)
    best = agent.evaluate()
    check("long focus + familiar app + working hours -> candidate",
          best is not None and best.kind == "break",
          str(best))

    # Same window, but freshly focused: no candidate at all.
    agent2, _, _ = make_agent(clock=FakeClock(at_hour(15)))
    agent2.current_app = "Visual Studio Code"
    agent2._app_since = agent2._clock() - 60
    check("active window alone produces no candidate", agent2.evaluate() is None)

    # Late night is its own trigger.
    agent3, _, _ = make_agent(clock=FakeClock(at_hour(2)))
    agent3.current_app = "Visual Studio Code"
    agent3._app_since = agent3._clock() - 60
    best3 = agent3.evaluate()
    check("late night with the user active -> late_night candidate",
          best3 is not None and best3.kind == "late_night", str(best3))

    agent4, _, _ = make_agent(clock=FakeClock(at_hour(2)), late_night_enabled=False)
    agent4.current_app = "Visual Studio Code"
    agent4._app_since = agent4._clock() - 60
    check("late night can be switched off", agent4.evaluate() is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            6. COOLDOWNS                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_cooldowns():
    print_system("\n[6] Cooldowns and anti-spam")

    clock = FakeClock(at_hour(15))
    agent, speaker, runtime = make_agent(clock=clock)
    put_in_deep_focus(agent)
    clock.advance(agent.config.quiet_after_interaction_s + 1)

    agent.tick()
    check("first eligible suggestion is spoken", len(speaker.spoken) == 1, str(speaker.spoken))

    # Immediately re-eligible by context — must be blocked.
    put_in_deep_focus(agent)
    agent.tick()
    check("the same event twice in a row is blocked", len(speaker.spoken) == 1)
    check("agent is in COOLDOWN", agent.state == ProactiveState.COOLDOWN)

    # Still inside the global cooldown an hour later minus a minute.
    clock.advance(agent.config.global_cooldown_s - 60)
    put_in_deep_focus(agent)
    agent.tick()
    check("still blocked just inside the global cooldown", len(speaker.spoken) == 1)

    # Past the global cooldown but still inside the longer per-kind break cooldown.
    clock.advance(120)
    put_in_deep_focus(agent)
    agent.tick()
    check("per-kind cooldown outlasts the global one", len(speaker.spoken) == 1,
          str(speaker.spoken))

    # Past the per-kind cooldown: the exact same wording is still blocked by the repeat gate.
    clock.advance(agent.config.break_cooldown_s)
    put_in_deep_focus(agent)
    spoken_text = speaker.spoken[0]
    agent._last_spoken_text[spoken_text] = clock() - 10  # pin the wording as just-used
    candidate = Candidate("break", spoken_text, {"habit": 1, "temporal": 1,
                                                 "context": 1, "recency": 1})
    check("identical wording is blocked by the repeat cooldown",
          agent._cooldown_ok(candidate, clock()) is False)

    # The per-wording ledger is pruned, so it cannot grow without bound.
    agent._last_spoken_text["ancient"] = clock() - agent.config.repeat_cooldown_s - 1
    agent._prune_text_cooldowns(clock())
    check("expired wording entries are pruned", "ancient" not in agent._last_spoken_text)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        7. HABITS & LEARNING                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_habits():
    print_system("\n[7] Habit model and user-response learning")

    store = HabitStore(os.path.join(_TMP, "habits_unit.json"), max_actions=5, max_apps=3)

    for _ in range(4):
        store.record_action("open:chrome", hour=9)
    check("repeated event increases frequency", store.action("open:chrome")["count"] == 4)
    check("hour histogram records the hour", store.action("open:chrome")["hours"][9] == 4)
    check("hour histogram stays 24 buckets", len(store.action("open:chrome")["hours"]) == 24)

    # Bounded storage: the least-observed keys are evicted, not accumulated forever.
    for i in range(20):
        store.record_action(f"filler{i}", hour=3)
    check("actions bucket is bounded", len(store.data["actions"]) <= 5,
          f"({len(store.data['actions'])} keys)")
    check("the strongest habit survives eviction", "open:chrome" in store.data["actions"])

    for i in range(20):
        store.record_app_time(f"App{i}", 10 + i)
    check("apps bucket is bounded", len(store.data["apps"]) <= 3,
          f"({len(store.data['apps'])} keys)")

    # Atomic persistence round-trip.
    check("habit store saves", store.save() is True)
    reloaded = HabitStore(store.path, max_actions=5, max_apps=3)
    check("habit store reloads what it wrote",
          reloaded.action("open:chrome")["count"] == 4)
    check("no .tmp file is left behind", not os.path.exists(store.path + ".tmp"))

    # Annoyance adaptation.
    store2 = HabitStore(os.path.join(_TMP, "habits_annoy.json"))
    for _ in range(3):
        store2.record_suggestion("break")
        store2.record_suggestion("break", outcome="ignored")
    check("repeated ignores raise annoyance", store2.annoyance("break") > 0.5,
          f"({store2.annoyance('break'):.2f})")

    store3 = HabitStore(os.path.join(_TMP, "habits_accept.json"))
    for _ in range(3):
        store3.record_suggestion("break")
        store3.record_suggestion("break", outcome="accepted")
    check("acceptance keeps annoyance at zero", store3.annoyance("break") == 0.0)

    # Annoyance actually lowers the score of a real candidate.
    agent, _, _ = make_agent(clock=FakeClock(at_hour(15)), habits_name="habits_score.json")
    candidate = Candidate("break", "x", {"habit": 1.0, "temporal": 1.0,
                                         "context": 1.0, "recency": 1.0})
    clean = agent.score(candidate)
    for _ in range(4):
        agent.habits.record_suggestion("break")
        agent.habits.record_suggestion("break", outcome="ignored")
    annoyed = agent.score(candidate)
    check("ignored suggestions reduce relevance", annoyed < clean,
          f"({clean:.2f} -> {annoyed:.2f})")

    # Habit key extraction: actions are recorded, conversation is not.
    check("open tokens keep their target", _habit_key("open chrome") == "open:chrome")
    check("parameterless tokens are their own key", _habit_key("take screenshot") == "take screenshot")
    check("conversation is not stored as a habit", _habit_key("general what is the capital of japan") is None)
    check("search queries are not stored as a habit", _habit_key("realtime bitcoin price") is None)
    check("authored content is not stored as a habit", _habit_key("content email to my boss") is None)

    agent2, _, _ = make_agent(habits_name="habits_intents.json")
    agent2.note_intents(["open chrome", "general how are you", "take screenshot"])
    check("note_intents records only actions",
          set(agent2.habits.actions()) == {"open:chrome", "take screenshot"},
          str(set(agent2.habits.actions())))


def section_feedback():
    print_system("\n[8] Reaction tracking")

    clock = FakeClock(at_hour(15))
    agent, speaker, runtime = make_agent(clock=clock, habits_name="habits_feedback.json")
    put_in_deep_focus(agent)
    clock.advance(agent.config.quiet_after_interaction_s + 1)
    agent.tick()
    check("a suggestion was made", len(speaker.spoken) == 1)
    check("agent is awaiting a reaction", agent._awaiting is not None)

    agent.on_event("user_utterance", {"text": "yes please"})
    check("an affirmative reply is recorded as accepted",
          agent.habits.suggestion("break").get("accepted") == 1)
    check("awaiting state is cleared", agent._awaiting is None)

    # Rejection.
    agent._awaiting = {"kind": "break", "at": clock(), "deadline": clock() + 300}
    agent.on_event("user_utterance", {"text": "no, not now"})
    check("a refusal is recorded as dismissed",
          agent.habits.suggestion("break").get("dismissed") == 1)

    # Silence.
    agent._awaiting = {"kind": "break", "at": clock(), "deadline": clock() + 300}
    clock.advance(400)
    agent._resolve_pending_outcome(clock())
    check("no reaction is recorded as ignored",
          agent.habits.suggestion("break").get("ignored") == 1)

    # A barge-in during proactive speech.
    agent._awaiting = {"kind": "break", "at": clock(), "deadline": clock() + 300}
    agent.on_event("barge_in", {"text": "stop"})
    check("a barge-in is recorded as interrupted",
          agent.habits.suggestion("break").get("interrupted") == 1)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    9. LLM INDEPENDENCE & TTS ROUTING                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_llm_fallback():
    print_system("\n[9] LLM independence and phrasing validation")

    clock = FakeClock(at_hour(15))

    def exploding_phraser(prompt, fallback):
        raise RuntimeError("no network")

    agent, speaker, runtime = make_agent(clock=clock, llm_phrasing=True,
                                         habits_name="habits_llm.json")
    agent.phrase_fn = exploding_phraser
    put_in_deep_focus(agent)
    clock.advance(agent.config.quiet_after_interaction_s + 1)
    agent.tick()
    check("a suggestion is still produced with the LLM unavailable",
          len(speaker.spoken) == 1, str(speaker.spoken))
    check("the fallback wording is the template",
          "break" in speaker.spoken[0].lower() or "minute" in speaker.spoken[0].lower(),
          speaker.spoken[0] if speaker.spoken else "")

    # A working phraser is used.
    agent2, speaker2, _ = make_agent(clock=FakeClock(at_hour(15)), llm_phrasing=True,
                                     habits_name="habits_llm2.json")
    agent2.phrase_fn = lambda prompt, fallback: "Long stretch there. Want to stand up?"
    put_in_deep_focus(agent2)
    agent2._clock.advance(agent2.config.quiet_after_interaction_s + 1)
    agent2.tick()
    check("a good rewording is used", speaker2.spoken == ["Long stretch there. Want to stand up?"],
          str(speaker2.spoken))

    # Bad output is rejected in favour of the template.
    fallback = "You've been in VS Code for a while. Want a break?"
    bad = [
        ("markdown list", "- take a break\n- stretch"),
        ("code fence", "```py\nprint('break')\n```"),
        ("a URL", "See https://example.com for stretching tips."),
        ("a paragraph", "Well. " * 60),
        ("an empty reply", "   "),
        ("too many sentences", "One. Two. Three. Four."),
    ]
    for label, text in bad:
        check(f"rejects {label}", _validate_phrasing(text, fallback) == fallback,
              repr(_validate_phrasing(text, fallback))[:60])

    check("emoji are stripped from an accepted rewording",
          "\U0001F600" not in _validate_phrasing("Take a break \U0001F600", fallback))


def section_tts_routing():
    print_system("\n[10] TTS integration")

    # The default wiring must use the background-utterance path, never begin_turn(), and
    # must re-check playback immediately before queueing.
    class SpyTTS:
        def __init__(self):
            self.is_playing = False
            self.calls = []

        def begin_turn(self):
            self.calls.append("begin_turn")

        def begin_background_utterance(self):
            self.calls.append("begin_background_utterance")

        def speak(self, text, blocking=False):
            self.calls.append(("speak", text))

    runtime = RuntimeState()
    runtime.set_state(AssistantState.LISTENING)
    tts = SpyTTS()
    agent = pa.create_default_agent(tts_engine=tts, llm_engine=None, runtime=runtime)
    agent.config.llm_phrasing = False

    spoken = agent.speak_fn("Take a quick break.")
    check("default wiring speaks", spoken is True)
    check("uses begin_background_utterance", "begin_background_utterance" in tts.calls)
    check("never calls begin_turn (would clear the interrupt latch)",
          "begin_turn" not in tts.calls)
    check("routes through the one existing TTS pipeline",
          ("speak", "Take a quick break.") in tts.calls)

    # Playback already in flight: refuse.
    tts.calls.clear()
    tts.is_playing = True
    check("refuses to queue while audio is playing", agent.speak_fn("later") is False)
    check("nothing was queued", tts.calls == [])

    # Busy runtime: refuse even if audio is idle (closes the gate/queue race).
    tts.is_playing = False
    runtime.set_state(AssistantState.PROCESSING)
    check("refuses to queue while the assistant is busy", agent.speak_fn("later") is False)

    runtime.set_state(AssistantState.LISTENING)
    runtime.shutdown_event.set()
    check("refuses to queue during shutdown", agent.speak_fn("later") is False)

    check("no LLM client is constructed when none is supplied", agent.phrase_fn is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   11. RUNTIME STATE & EVENT BUS                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_runtime_state():
    print_system("\n[11] Runtime state service")

    runtime = RuntimeState()
    check("starts IDLE", runtime.state == AssistantState.IDLE)
    check("IDLE is not busy", runtime.is_busy() is False)
    for state in (AssistantState.PROCESSING, AssistantState.SPEAKING,
                  AssistantState.INTERRUPTING, AssistantState.AUTOMATING,
                  AssistantState.SHUTTING_DOWN):
        runtime.set_state(state)
        check(f"{state} is busy", runtime.is_busy() is True)
    runtime.set_state(AssistantState.LISTENING)
    check("LISTENING is not busy", runtime.is_busy() is False)

    turn_a = runtime.begin_turn()
    check("turn ids increase", runtime.begin_turn() > turn_a)
    check("turn_active while open", runtime.turn_active is True)
    runtime.end_turn()
    check("turn_active clears", runtime.turn_active is False)

    seen = []
    runtime.subscribe(lambda event, payload: seen.append((event, payload)))

    def exploder(event, payload):
        raise RuntimeError("bad subscriber")

    runtime.subscribe(exploder)
    runtime.emit("user_utterance", text="hello")
    check("subscribers receive events", seen and seen[0][0] == "user_utterance")
    check("a broken subscriber cannot break emit", len(seen) == 1)

    check("event log is bounded",
          all(True for _ in range(1)) and _event_log_bounded(runtime))

    snap = runtime.snapshot()
    check("snapshot carries the policy inputs",
          {"state", "busy", "turn_active", "shutting_down",
           "seconds_since_interrupt"} <= set(snap))


def _event_log_bounded(runtime):
    for i in range(RuntimeState.MAX_EVENT_LOG * 3):
        runtime.emit("noise", i=i)
    return len(runtime.recent_events()) <= RuntimeState.MAX_EVENT_LOG


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   12. SHUTDOWN & IDLE OVERHEAD                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_shutdown_and_overhead():
    print_system("\n[12] Shutdown and idle overhead")

    before = threading.active_count()
    agent, speaker, runtime = make_agent(tick_seconds=0.05, habits_name="habits_shutdown.json")
    agent.habits.record_action("open:chrome", hour=9)
    agent.start()
    time.sleep(0.4)   # let several real ticks run
    check("the agent ticks on its own thread", agent.stats["ticks"] >= 1,
          f"({agent.stats['ticks']} ticks)")

    t0 = time.perf_counter()
    agent.stop()
    shutdown_s = time.perf_counter() - t0
    check("stop() returns promptly", shutdown_s < 1.0, f"({shutdown_s * 1000:.0f}ms)")
    check("thread count returns to baseline", threading.active_count() <= before,
          f"({threading.active_count()} vs {before})")
    check("habits are flushed on shutdown", os.path.exists(agent.habits.path))
    with open(agent.habits.path, encoding="utf-8") as f:
        check("flushed file is valid JSON with the recorded action",
              json.load(f)["actions"].get("open:chrome", {}).get("count") == 1)

    # The runtime subscription must be released, or a stopped agent keeps reacting.
    runtime.emit("barge_in", text="stop")
    check("a stopped agent is unsubscribed from the event bus",
          agent.on_event not in getattr(runtime, "_subscribers", []))

    # Idle cost: a tick with nothing to do must be trivially cheap and allocate no state.
    agent2, _, _ = make_agent(habits_name="habits_idle.json")
    agent2.current_app = None
    t0 = time.perf_counter()
    for _ in range(200):
        agent2.tick()
    per_tick_ms = (time.perf_counter() - t0) * 1000.0 / 200
    print_info(f"idle tick cost: {per_tick_ms:.3f}ms")
    check("an idle tick is cheap", per_tick_ms < 5.0, f"({per_tick_ms:.3f}ms)")
    check("idle ticking produces no candidates", agent2.stats["candidates"] == 0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       13. HELPER CORRECTNESS                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_helpers():
    print_system("\n[13] Helpers")

    check("window titles collapse to an app identity",
          normalize_app_name("index.py - Visual Studio Code") == "Visual Studio Code")
    check("plain titles survive", normalize_app_name("Spotify") == "Spotify")
    check("empty titles are safe", normalize_app_name("") == "Unknown")
    check("app identity is length-bounded", len(normalize_app_name("x" * 500)) <= 60)

    # A v1 habits.json must not crash the store.
    legacy = os.path.join(_TMP, "legacy.json")
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump({"app_totals": {"Brave": 161.8}, "last_updated": "2026-09-07T11:20:56"}, f)
    store = HabitStore(legacy)
    check("a v1 habits file is migrated, not crashed on",
          store.app("Brave").get("seconds") == 161.8)

    corrupt = os.path.join(_TMP, "corrupt.json")
    with open(corrupt, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("a corrupt habits file falls back to empty",
          HabitStore(corrupt).data["actions"] == {})


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    14. REGRESSION GUARDS (cross-module)                │
# └────────────────────────────────────────────────────────────────────────┘

def section_regressions():
    print_system("\n[14] Regression guards")

    from kayra.input.speech_to_text import is_interrupt_phrase

    check("'stop' is still a barge-in", is_interrupt_phrase("stop") is True)
    check("'stop the music' is still a command, not a barge-in",
          is_interrupt_phrase("stop the music") is False)
    check("'stop proactive suggestions' is not swallowed as a barge-in",
          is_interrupt_phrase("stop proactive suggestions") is False)
    check("\"don't interrupt me\" is not swallowed as a barge-in",
          is_interrupt_phrase("don't interrupt me") is False)
    check("'disable proactive mode' is not swallowed as a barge-in",
          is_interrupt_phrase("disable proactive mode") is False)

    # The DMM vocabulary must be able to express the proactive switch, and every token in
    # the acceptance gate must still be dispatchable by something.
    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    funcs = CentralizedLLMEngine().funcs
    check("'proactive on' is in the DMM vocabulary", "proactive on" in funcs)
    check("'proactive off' is in the DMM vocabulary", "proactive off" in funcs)
    check("no removed token lingers in the vocabulary",
          "generate image" not in funcs)

    # The proactive module must not import the engines it speaks through — that is what
    # keeps it unable to touch the TTS cancellation epoch.
    source = open(os.path.join(project_root, "src", "kayra", "services", "proactive_agent.py"),
                  encoding="utf-8").read()
    check("proactive_agent never calls begin_turn() on any engine",
          ".begin_turn(" not in source)
    check("proactive_agent does not import text_to_speech",
          "import text_to_speech" not in source and "from .text_to_speech" not in source)
    check("proactive_agent does not construct an LLM engine",
          "CentralizedLLMEngine(" not in source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                RUNNER                                  │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_banner("KAYRA PROACTIVE AGENT DIAGNOSTIC",
                 "State, safety, scoring, habits, cooldowns & teardown")
    try:
        section_state()
        section_voice_control()
        section_safety_gate()
        section_deferral()
        section_scoring()
        section_cooldowns()
        section_habits()
        section_feedback()
        section_llm_fallback()
        section_tts_routing()
        section_runtime_state()
        section_shutdown_and_overhead()
        section_helpers()
        section_regressions()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All proactive agent checks passed.")
