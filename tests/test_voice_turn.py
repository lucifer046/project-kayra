# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_voice_turn.py                              │
# │   Utterance Endpointing · Dangerous-Control Confirmation · DMM Retries  │
# └────────────────────────────────────────────────────────────────────────┘
r"""
test_voice_turn.py — the suite for the voice-reliability milestone.

    .venv\Scripts\python tests\test_voice_turn.py

SAFE. No microphone, no camera, no browser, no network, no provider. The endpoint decision is
a pure function driven by a table; the confirmation manager runs on an injected clock; the DMM
retry contract is exercised against a FAKE local model. `EnvironmentGuard` wraps the run and
fails it if anything the developer owns changed.

WHAT IT EXISTS TO PREVENT
-------------------------
A user said, in Hindi and English together —

    "यार मेरी girlfriend मुझसे नाराज़ है, बताओ मैं क्या करूँ?"

— and Kayra shut down mid-sentence. The cause was a SECOND COMMIT POINT: the recognition page
classified lifecycle commands from the INTERIM transcript and published a flag that the control
watcher dispatched without ever consulting the endpointer. A transient interim reading of
"exit" therefore reached `request_shutdown()` while the person was still talking.

So this suite asserts three properties, in the order they defend:

  1. **§1–3 — one commit point, and it will not commit while somebody is speaking.** Nothing
     upstream of `core.endpointing.decide()` may end a turn, and `decide()` refuses while the
     acoustic detector still hears the user.
  2. **§4–6 — a dangerous control asks first.** SHUTDOWN and SLEEP are requests until answered.
  3. **§7 — the DMM retries an empty completion five times and never a sixth.**

Section 8 is the one that would catch a regression by the back door: it walks the parsed source
for any path that could reach a dangerous action without a confirmation.
"""

import os
import io
import ast
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, FakeClock, describe_host, run, PROJECT_ROOT

from kayra.utils import print_banner, print_info
from kayra.core import endpointing as ep
from kayra.core import voice_control as vc

check = Checker("voice turn")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          A TURN, SIMULATED                             │
# └────────────────────────────────────────────────────────────────────────┘
# The page's tick loop, in Python: advance the clock, feed events, ask `decide()` every 60ms
# exactly as the page does, and record where the turn was committed. This is what makes the
# scenarios in §2 statements about the real predicate rather than about a paraphrase of it.

TICK_MS = 60


class Turn:
    """One simulated speech turn, driven event by event."""

    def __init__(self, config=None):
        self.config = config or ep.tuning()
        self.now = 1_000_000              # an arbitrary epoch; only differences matter
        self.start = self.now
        self.last_result = self.now
        self.last_voice = self.now
        self.committed = ""
        self.interim = ""
        self.voice = False
        self.commits = []                 # (text, reason, ms_since_start)

    # ── Events the recognizer and the VAD produce ──

    def speak(self, ms):
        """The person is making speech sounds for `ms`. Ticks the endpointer throughout."""
        self.voice = True
        self._advance(ms)
        return self

    def silence(self, ms):
        """Nobody is speaking for `ms`."""
        self.voice = False
        self._advance(ms)
        return self

    def interim_result(self, text):
        """The recognizer emitted an INTERIM segment. Never ends a turn."""
        self.interim = text
        self.last_result = self.now
        return self

    def final_result(self, text):
        """
        The recognizer emitted a FINAL segment.

        This means "this SEGMENT is final", NOT "the user has finished". Several arrive per
        sentence and they arrive while the speaker keeps going — which is exactly why this
        method appends rather than commits.
        """
        self.committed = (self.committed + " " + text).strip()
        self.interim = ""
        self.last_result = self.now
        return self

    # ── The loop ──

    def _snapshot(self):
        return ep.TurnSnapshot(
            now_ms=self.now, utterance_start_ms=self.start,
            last_result_ms=self.last_result, last_voice_ms=self.last_voice,
            committed_text=self.committed, interim_text=self.interim,
            voice_active=self.voice, vad_ready=True)

    def _advance(self, ms):
        elapsed = 0
        while elapsed < ms:
            step = min(TICK_MS, ms - elapsed)
            self.now += step
            elapsed += step
            if self.voice:
                self.last_voice = self.now
            decision = ep.decide(self._snapshot(), self.config)
            if decision.commit:
                self.commits.append((self.committed or self.interim, decision.reason,
                                     self.now - self.start))
                # The page resets and starts a new utterance; so do we.
                self.committed = ""
                self.interim = ""
                self.start = self.now
                self.last_result = self.now

    # ── Questions the assertions ask ──

    @property
    def committed_count(self):
        return len(self.commits)

    @property
    def first_text(self):
        return self.commits[0][0] if self.commits else ""

    @property
    def texts(self):
        return [c[0] for c in self.commits]


# ┌────────────────────────────────────────────────────────────────────────┐
# │        1. NOTHING BUT THE ENDPOINT DECISION MAY COMMIT A TURN          │
# └────────────────────────────────────────────────────────────────────────┘

def section_one_commit_point():
    check.section("[1] One authoritative commit point")

    from kayra.input import speech_to_text as stt
    js = stt.html_code

    # ── The bypass, and its absence ──
    check("the page no longer classifies lifecycle commands",
          "function looksLikeControl" not in js,
          "it ran on the INTERIM transcript and published a flag the watcher dispatched")
    check("the page's control-phrase table is hard-coded empty",
          "window.kayraControlPhrases = [];" in js)
    assignments = [line.strip() for line in js.splitlines()
                   if "window.kayraControl " in line + " "
                   and "kayraControlPhrases" not in line
                   and "=" in line and "null" not in line
                   and not line.strip().startswith("//")]
    check("nothing in the page can publish a control", not assignments, str(assignments[:2]))

    # ── Exactly one commit site ──
    flush_lines = [line.strip() for line in js.splitlines()
                   if "flushUtterance(" in line and not line.strip().startswith("//")]
    check("there is one definition and one call of the commit function",
          len(flush_lines) == 2, str(flush_lines))
    check("and the call is the endpoint decision's",
          any("decision.commit" in line for line in flush_lines), str(flush_lines))

    # ── Barge-in survives, and is the ONLY thing interim text may do ──
    check("barge-in still runs off interim results", "looksLikeInterrupt(probe)" in js)
    check("barge-in is the documented exception",
          "THE ONE THING INTERIM TEXT MAY STILL DO" in js)

    # ── Python end: the committed utterance is the only thing classified ──
    app_source = io.open(os.path.join(PROJECT_ROOT, "src", "kayra", "app.py"),
                         encoding="utf-8").read()
    tree = ast.parse(app_source)
    listen = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "Listen")
    calls = [n.func.id for n in ast.walk(listen)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("Listen() classifies control only after repair", "classify_control" in calls)
    check("Listen() resolves a pending confirmation first",
          "resolve_lifecycle_confirmation" in calls)
    order = app_source.index("resolve_lifecycle_confirmation(user_input")
    check("and it does so BEFORE classification",
          order < app_source.index("control = classify_control(user_input)"),
          "a bare 'yes' sent to the classifier comes back as conversation")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  2. THE ENDPOINT SCENARIO TABLE                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_endpoint_scenarios():
    check.section("[2] Endpoint scenarios")
    config = ep.tuning()

    # ── THE REPORTED FAILURE ─────────────────────────────────────────────
    # A long Hindi/English sentence. The recognizer emits a transient short final segment
    # while the person keeps speaking. Nothing may commit during the speech.
    turn = Turn(config)
    turn.speak(600).interim_result("यार मेरी")
    turn.speak(600).final_result("exit")            # <- the transient reading
    turn.speak(2000).interim_result("girlfriend मुझसे नाराज़")
    turn.speak(1500).final_result("मेरी girlfriend मुझसे नाराज़ है बताओ मैं क्या करूँ")
    check("[hinglish] nothing commits while the user is still speaking",
          turn.committed_count == 0, str(turn.commits))
    turn.silence(1600)
    check("[hinglish] the turn commits exactly once when they stop",
          turn.committed_count == 1, str(turn.commits))
    check("[hinglish] and it carries the WHOLE sentence, not the fragment",
          "क्या करूँ" in turn.first_text and turn.first_text != "exit",
          turn.first_text)

    # ── A FINAL RESULT IS NOT THE END OF A TURN ──────────────────────────
    turn = Turn(config)
    turn.speak(500).final_result("Okay Kayra")
    turn.speak(400)
    check("[final-mid-speech] a final segment does not end the turn",
          turn.committed_count == 0)
    turn.final_result("I wanted to ask you something")
    turn.speak(400).final_result("because my girlfriend is upset with me")
    turn.speak(400).final_result("and I don't know what to do")
    check("[aggregation] still one open turn", turn.committed_count == 0)
    turn.silence(1600)
    check("[aggregation] four final segments become ONE utterance",
          turn.committed_count == 1, str(turn.texts))
    check("[aggregation] and it contains every segment",
          all(fragment in turn.first_text
              for fragment in ("Okay Kayra", "ask you something",
                               "upset with me", "don't know what to do")),
          turn.first_text)

    # ── A SHORT PAUSE INSIDE A SENTENCE ──────────────────────────────────
    turn = Turn(config)
    turn.speak(1200).final_result("I've been trying to fix this")
    turn.silence(300)                       # a breath between clauses
    turn.speak(1000).final_result("for three hours")
    turn.silence(1600)
    check("[breath] a 300ms pause does not split the turn",
          turn.committed_count == 1, str(turn.texts))
    check("[breath] the whole sentence arrives together",
          "trying to fix this" in turn.first_text and "three hours" in turn.first_text,
          turn.first_text)

    # ── SPEECH RESUMING DURING THE GRACE PERIOD CANCELS THE ENDPOINT ─────
    turn = Turn(config)
    turn.speak(1200).final_result("tell me about the")
    turn.silence(700)                       # most of the way to an endpoint
    check("[resume] not yet committed", turn.committed_count == 0)
    turn.speak(900).final_result("deployment we discussed")
    turn.silence(1600)
    check("[resume] resuming speech cancelled the endpoint",
          turn.committed_count == 1, str(turn.texts))
    check("[resume] and both halves are in one utterance",
          "tell me about" in turn.first_text and "deployment" in turn.first_text,
          turn.first_text)

    # ── A GENUINE END OF SENTENCE STILL COMMITS ──────────────────────────
    turn = Turn(config)
    turn.speak(1500).final_result("what is the weather today")
    turn.silence(2000)
    check("[genuine end] a finished sentence does commit",
          turn.committed_count == 1, str(turn.commits))
    check("[genuine end] with the endpoint reason",
          turn.commits[0][1] == ep.Decision.ENDPOINT, str(turn.commits))

    # ── SHORT CONTROL COMMANDS STAY FAST ─────────────────────────────────
    turn = Turn(config)
    turn.speak(300).final_result("stop listening")
    turn.silence(1200)
    check("[short command] commits", turn.committed_count == 1, str(turn.commits))
    latency = turn.commits[0][2] - 300
    check("[short command] within ~600ms of the speaker stopping", latency <= 620,
          f"{latency}ms")
    print_info(f"      short-command endpoint latency: {latency} ms after speech ends")

    # ── A LONE WORD AFTER LONG SPEECH WAITS LONGER, THEN COMMITS ─────────
    # The safety property AND its bound: this delays a commit, it never blocks one.
    turn = Turn(config)
    turn.speak(2500).final_result("exit")
    turn.silence(700)
    check("[truncated] a lone word after 2.5s of speech is not committed at 700ms",
          turn.committed_count == 0)
    turn.silence(1500)
    check("[truncated] but it IS committed once the room stays quiet",
          turn.committed_count == 1, str(turn.commits))
    check("[truncated] nothing was invented — the text is what was heard",
          turn.first_text == "exit", turn.first_text)

    # ── AN ORDINARY SHORT UTTERANCE IS NOT PENALISED ─────────────────────
    turn = Turn(config)
    turn.speak(400).final_result("exit")
    turn.silence(1200)
    check("[short exit] a genuinely short utterance still commits promptly",
          turn.committed_count == 1, str(turn.commits))

    # ── INTERIM ONLY, NEVER FINALIZED ────────────────────────────────────
    turn = Turn(config)
    turn.speak(700).interim_result("open the browser")
    turn.silence(2200)
    check("[uncommitted] an interim that never finalized is still delivered",
          turn.committed_count == 1 and turn.first_text == "open the browser",
          str(turn.commits))

    # ── RECOGNIZER QUIET, ROOM NOT ──────────────────────────────────────
    turn = Turn(config)
    turn.speak(1000).final_result("hold on")
    turn.speak(2000)                        # still audibly speaking, no new results
    check("[recognizer quiet, room busy] does not commit",
          turn.committed_count == 0, str(turn.commits))

    # ── ROOM QUIET, RECOGNIZER STILL PRODUCING ──────────────────────────
    turn = Turn(config)
    turn.speak(800).final_result("first part")
    for _ in range(6):
        turn.silence(200).interim_result("second part still arriving")
    check("[room quiet, recognizer busy] does not commit",
          turn.committed_count == 0, str(turn.commits))

    # ── EMPTY: NOTHING HEARD AT ALL ─────────────────────────────────────
    turn = Turn(config)
    turn.silence(5000)
    check("[silence] nothing is committed when nothing was said",
          turn.committed_count == 0)

    # ── DELAYED RECOGNIZER RESULT ───────────────────────────────────────
    turn = Turn(config)
    turn.speak(1500)                        # speech, no results yet at all
    turn.silence(400)
    turn.final_result("the backend was slow")
    turn.silence(1600)
    check("[lagging backend] a late result still forms one turn",
          turn.committed_count == 1 and "backend was slow" in turn.first_text,
          str(turn.commits))


# ┌────────────────────────────────────────────────────────────────────────┐
# │          3. THE DECISION ITSELF, RULE BY RULE                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_decision_rules():
    check.section("[3] Endpoint decision rules")
    config = ep.tuning()

    def snap(**kwargs):
        base = dict(now_ms=10_000, utterance_start_ms=8_000, last_result_ms=9_000,
                    last_voice_ms=9_000, committed_text="hello there how are you",
                    interim_text="",
                    voice_active=False, vad_ready=True)
        base.update(kwargs)
        return ep.TurnSnapshot(**base)

    check("no text is not an endpoint",
          ep.decide(snap(committed_text="", interim_text=""), config).reason
          == ep.Decision.NO_SPEECH)
    check("an audible speaker blocks the commit",
          not ep.decide(snap(voice_active=True), config).commit)
    check("and says why", ep.decide(snap(voice_active=True), config).reason
          == ep.Decision.VOICE_ACTIVE)
    check("a turn younger than the minimum cannot end",
          ep.decide(snap(utterance_start_ms=9_900), config).reason == ep.Decision.TOO_YOUNG)
    check("pending interim text extends the wait",
          not ep.decide(snap(interim_text="and also", last_result_ms=9_500),
                        config).commit)
    check("the room must be quiet too",
          ep.decide(snap(last_voice_ms=9_900), config).reason == ep.Decision.ROOM_NOISY)
    check("both quiet commits",
          ep.decide(snap(last_result_ms=8_900, last_voice_ms=8_900), config).commit)

    # ── The truncation rule ──
    long_short = snap(utterance_start_ms=6_000, committed_text="exit")
    check("a lone word after long speech looks truncated",
          ep.looks_truncated(long_short, config))
    check("a lone word after brief speech does not",
          not ep.looks_truncated(snap(utterance_start_ms=9_800, committed_text="exit"),
                                 config))
    check("a full sentence never looks truncated",
          not ep.looks_truncated(snap(utterance_start_ms=1_000,
                                      committed_text="one two three four"), config))
    check("pending interim is never 'truncated' — the recognizer is visibly working",
          not ep.looks_truncated(snap(utterance_start_ms=1_000, committed_text="exit",
                                      interim_text="ing the app"), config))
    check("a truncated turn does NOT get the fast path",
          ep.required_quiet_ms(long_short, config) == config["silence_ms"])
    check("and waits out a longer room hangover",
          ep.required_hangover_ms(long_short, config) > config["vad_hangover_ms"])

    # ── THE BOUND ON THAT RULE. It delays, it never blocks. ──
    committed = snap(utterance_start_ms=0, committed_text="exit",
                     last_result_ms=6_000, last_voice_ms=6_000)
    check("a truncated turn still commits once the room stays quiet",
          ep.decide(committed, config).commit, repr(ep.decide(committed, config)))

    # ── Fast path, and what closes it ──
    fast = snap(utterance_start_ms=9_500, committed_text="stop", last_result_ms=9_500)
    check("a short committed command takes the fast path",
          ep.required_quiet_ms(fast, config) == config["fast_endpoint_ms"])
    check("a long utterance does not",
          ep.required_quiet_ms(snap(committed_text="a b c d e"), config)
          == config["silence_ms"])

    # ── The timeout can no longer flush a sentence out from under a speaker ──
    stuck = snap(now_ms=100_000, utterance_start_ms=90_000, last_result_ms=90_000,
                 last_voice_ms=99_990, voice_active=True)
    check("the hard timeout does not fire while the user is audible",
          not ep.decide(stuck, config).commit, repr(ep.decide(stuck, config)))
    quiet_stuck = snap(now_ms=100_000, utterance_start_ms=93_000, last_result_ms=93_000,
                       last_voice_ms=93_000)
    check("but it does fire when they have stopped",
          ep.decide(quiet_stuck, config).reason == ep.Decision.TIMEOUT)

    # ── The absolute bound, so a wedged VAD cannot make Kayra deaf ──
    wedged = snap(now_ms=200_000, utterance_start_ms=100_000, last_result_ms=199_000,
                  last_voice_ms=199_990, voice_active=True)
    check("an absolute bound exists above everything",
          ep.decide(wedged, config).reason == ep.Decision.ABSOLUTE)
    check("and it is far outside any real sentence",
          config["absolute_max_ms"] >= 30_000, str(config["absolute_max_ms"]))

    # ── No VAD: degrade to the historical behaviour, do not fail ──
    no_vad = ep.TurnSnapshot(now_ms=10_000, utterance_start_ms=8_000, last_result_ms=9_000,
                             last_voice_ms=0, committed_text="hello there how are you",
                             vad_ready=False)
    check("with no VAD the room test degrades to the recognizer test",
          no_vad.since_voice_ms == no_vad.since_result_ms)
    check("and a turn can still end", ep.decide(
        ep.TurnSnapshot(now_ms=10_000, utterance_start_ms=8_000, last_result_ms=8_900,
                        last_voice_ms=0, committed_text="hello there how are you",
                        vad_ready=False), config).commit)

    # ── Configuration invariants are enforced, not trusted ──
    saved = {k: os.environ.get(k) for k in ("STT_FAST_ENDPOINT_MS", "STT_SILENCE_MS")}
    try:
        os.environ["STT_FAST_ENDPOINT_MS"] = "9999"      # longer than the baseline
        os.environ["STT_SILENCE_MS"] = "800"
        corrected = ep.tuning()
        check("a fast path longer than the baseline is corrected",
              corrected["fast_endpoint_ms"] <= corrected["silence_ms"],
              str(corrected["fast_endpoint_ms"]))
        os.environ["STT_FAST_ENDPOINT_MS"] = "not a number"
        check("a malformed value falls back to the default",
              ep.tuning()["fast_endpoint_ms"] == ep.DEFAULTS["fast_endpoint_ms"])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── Cost. This runs at 60ms resolution; it must be free. ──
    probe = snap()
    started = time.perf_counter()
    for _ in range(20_000):
        ep.decide(probe, config)
    per_call_us = (time.perf_counter() - started) / 20_000 * 1_000_000
    print_info(f"      endpoint decision: {per_call_us:.2f} us/call")
    check("the decision is effectively free", per_call_us < 25.0, f"{per_call_us:.2f} us")


# ┌────────────────────────────────────────────────────────────────────────┐
# │        4. THE PAGE MIRRORS THE PYTHON DECISION                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_page_agreement():
    check.section("[4] Page / Python agreement")
    from kayra.input import speech_to_text as stt
    js = stt.html_code

    payload = ep.tuning_payload()
    for key in payload:
        # `silenceMs` is the one value the page also holds under its historical name
        # (`silenceLimit`), because it arrives as a positional argument too.
        present = (f"tuning.{key}" in js or f"{key}:" in js
                   or (key == "silenceMs" and "tuningOverride.silenceMs" in js))
        check(f"the page reads tuning.{key}", present)

    tuning = stt._capture_tuning()
    check("the page is configured FROM core.endpointing",
          all(tuning[k] == payload[k] for k in payload),
          "one set of thresholds, not two that can drift")

    # Every reason string the Python decision can produce must exist in the page, and vice
    # versa — that is what makes "they implement the same rule" checkable rather than claimed.
    reasons = {ep.Decision.ENDPOINT, ep.Decision.TIMEOUT, ep.Decision.ABSOLUTE,
               ep.Decision.NO_SPEECH, ep.Decision.VOICE_ACTIVE, ep.Decision.TOO_YOUNG,
               ep.Decision.RECOGNIZER_BUSY, ep.Decision.ROOM_NOISY, ep.Decision.TRUNCATED}
    for reason in reasons:
        check(f"the page can report '{reason}'", f'"{reason}"' in js)

    # Ordering: the voice check must precede the timeout, or the timeout can flush a sentence
    # out from under a speaker — which is the bug it had before.
    check("the page checks voice before the hard timeout",
          js.index('"voice-active"') < js.index('reason: "timeout"'))
    check("the page checks the absolute bound before everything",
          js.index('"absolute-timeout"') < js.index('"no-speech"'))


# ┌────────────────────────────────────────────────────────────────────────┐
# │        5. DANGEROUS CONTROLS ASK BEFORE THEY ACT                       │
# └────────────────────────────────────────────────────────────────────────┘

def section_control_vocabulary():
    check.section("[5] Control classification")

    # ── The requests this milestone names ──
    for phrase, kind in (
            ("Okay Kayra, go to sleep.", vc.ControlKind.SLEEP),
            ("Okay Kayra, shut down the engine.", vc.ControlKind.SHUTDOWN),
            ("Okay Kayra, shut down.", vc.ControlKind.SHUTDOWN),
            ("Okay Kayra, turn the engine off.", vc.ControlKind.SHUTDOWN),
            ("shut down Kayra", vc.ControlKind.SHUTDOWN),
            ("turn off Kayra", vc.ControlKind.SHUTDOWN),
            ("close Kayra", vc.ControlKind.SHUTDOWN),
            ("exit Kayra", vc.ControlKind.SHUTDOWN),
            ("quit Kayra", vc.ControlKind.SHUTDOWN),
            ("end Kayra", vc.ControlKind.SHUTDOWN),
            ("exit", vc.ControlKind.SHUTDOWN),
            ("put Kayra to sleep", vc.ControlKind.SLEEP),
            ("enter sleep mode", vc.ControlKind.SLEEP),
            ("sleep mode", vc.ControlKind.SLEEP),
            ("Stop listening.", vc.ControlKind.PAUSE_LISTENING),
            ("pause listening", vc.ControlKind.PAUSE_LISTENING),
            ("mute listening", vc.ControlKind.PAUSE_LISTENING),
            ("don't listen", vc.ControlKind.PAUSE_LISTENING),
            ("stop hearing me", vc.ControlKind.PAUSE_LISTENING),
            ("wake up Kayra", vc.ControlKind.WAKE),
            ("Kayra wake up", vc.ControlKind.WAKE),
            ("resume listening", vc.ControlKind.RESUME_LISTENING),
            ("start listening", vc.ControlKind.RESUME_LISTENING),
    ):
        command = vc.classify_control(phrase)
        check(f"'{phrase}' -> {kind}",
              command is not None and command.kind == kind,
              command.kind if command else "None")

    # ── FALSE POSITIVES. Ordinary sentences must reach the DMM untouched. ──
    for phrase in ("Why did the process exit?",
                   "What is an exit code?",
                   "The application exists.",
                   "She exists in the same file.",
                   "She is quite upset.",
                   "That is great.",
                   "Please write this down.",
                   "Why do people sleep so much?",
                   "Tell me what sleep mode does.",
                   "I want to exit the application after saving.",
                   "The process will exit after this.",
                   "Why did the program exit?",
                   "yaar meri girlfriend mujhse naraz hai bata mai kya karu",
                   "I've been trying to fix this for three hours.",
                   "turn off the computer",
                   "shut down my pc"):
        check(f"'{phrase}' is conversation, not a control",
              vc.classify_control(phrase) is None,
              str(vc.classify_control(phrase)))

    # ── The dangerous set, and what is deliberately NOT in it ──
    check("shutdown and sleep are the dangerous kinds",
          vc.DANGEROUS_KINDS == frozenset({vc.ControlKind.SHUTDOWN, vc.ControlKind.SLEEP}))
    check("pausing the microphone is NOT dangerous — it is one button to undo",
          vc.ControlKind.PAUSE_LISTENING not in vc.DANGEROUS_KINDS)
    check("barge-in is NOT dangerous — gating it would defeat it",
          vc.ControlKind.INTERRUPT not in vc.DANGEROUS_KINDS)

    # ── Evidence strength ──
    check("'shut down Kayra' names its target",
          vc.classify_control("shut down Kayra").explicit)
    check("'exit' does not", not vc.classify_control("exit").explicit)
    check("but BOTH are dangerous — naming the target is not a shortcut",
          vc.classify_control("exit").dangerous
          and vc.classify_control("shut down Kayra").dangerous)

    # ── 'stop Kayra' must stay a barge-in ──
    check("'stop Kayra' is silencing, not shutdown",
          vc.classify_control("stop Kayra").kind == vc.ControlKind.INTERRUPT)


def section_confirmation():
    check.section("[6] Confirmation")

    # ── The vocabulary ──
    for text, expected in (
            ("yes", vc.ConfirmationReply.YES),
            ("yeah", vc.ConfirmationReply.YES),
            ("yep", vc.ConfirmationReply.YES),
            ("confirm", vc.ConfirmationReply.YES),
            ("do it", vc.ConfirmationReply.YES),
            ("go ahead", vc.ConfirmationReply.YES),
            ("please do", vc.ConfirmationReply.YES),
            ("yes please", vc.ConfirmationReply.YES),
            ("yes Kayra", vc.ConfirmationReply.YES),
            ("yeah, go ahead", vc.ConfirmationReply.YES),
            ("no", vc.ConfirmationReply.NO),
            ("cancel", vc.ConfirmationReply.NO),
            ("don't", vc.ConfirmationReply.NO),
            ("never mind", vc.ConfirmationReply.NO),
            ("not now", vc.ConfirmationReply.NO),
            ("stop", vc.ConfirmationReply.NO),
            # Answer-shaped and not an answer.
            ("yes, but first tell me what my options are", vc.ConfirmationReply.UNCLEAR),
            ("no wait what does that even do", vc.ConfirmationReply.UNCLEAR),
            # Not about the question at all.
            ("what is the weather", None),
            ("open chrome", None),
            ("tell me a joke", None),
    ):
        got = vc.read_confirmation(text)
        check(f"'{text}' -> {expected}", got == expected, str(got))

    # ── The manager, on an injected clock ──
    clock = FakeClock(0)
    manager = vc.ControlConfirmations(ttl=20.0, clock=lambda: clock.now / 1000.0)
    command = vc.classify_control("shut down Kayra")

    check("nothing is pending to begin with", manager.pending is None)
    manager.request(command)
    check("a request becomes pending", manager.pending is not None)
    check("bound to the kind that raised it",
          manager.pending.kind == vc.ControlKind.SHUTDOWN)

    outcome, request = manager.answer("yes")
    check("'yes' executes", outcome == "execute" and request.kind == vc.ControlKind.SHUTDOWN)
    check("and clears the request", manager.pending is None)
    check("a late 'yes' after that does nothing",
          manager.answer("yes") == ("none", None))

    manager.request(command)
    check("'no' cancels", manager.answer("no")[0] == "cancel")
    check("and clears", manager.pending is None)

    manager.request(command)
    outcome, _ = manager.answer("yes but first tell me my options")
    check("an ambiguous answer re-asks rather than executing", outcome == "reask")
    check("and the request is still pending", manager.pending is not None)
    outcome, _ = manager.answer("yeah well maybe not right now actually")
    check("a second ambiguous answer cancels rather than looping",
          outcome == "cancel" and manager.pending is None)

    manager.request(command)
    outcome, _ = manager.answer("what time is it")
    check("an unrelated utterance is not an answer", outcome == "none")
    check("and it clears the request rather than leaving it armed",
          manager.pending is None,
          "a later 'yes' to some other exchange must not execute it")

    # ── ECHO: Kayra must not answer, cancel, or clear her own question ──
    # Part 7 of the milestone in one paragraph. She asks out loud, the microphone hears her,
    # and the capture-timestamp gate flags it. The reply that matters most — the user's "Yes."
    # — arrives within a second or two of that, often overlapping it, so the confirmation is
    # resolved BEFORE the echo gate. That would be unsafe if echo-flagged audio were trusted
    # equally, so it is not: only a clean whole-utterance YES or NO is honoured from it.
    manager = vc.ControlConfirmations(ttl=20.0, clock=lambda: clock.now / 1000.0)
    manager.request(command)
    for own_words in (vc.confirmation_question(vc.ControlKind.SHUTDOWN, True),
                      vc.confirmation_question(vc.ControlKind.SHUTDOWN, False),
                      vc.confirmation_question(vc.ControlKind.SLEEP),
                      vc.confirmation_ack(vc.ControlKind.SHUTDOWN, True),
                      vc.confirmation_ack(vc.ControlKind.SHUTDOWN, False),
                      vc.confirmation_ack(vc.ControlKind.SLEEP, True)):
        outcome, _ = manager.answer(own_words, echo=True)
        check(f"Kayra's own '{own_words[:34]}...' does nothing",
              outcome == "none-echo", outcome)
        check("and leaves the question open", manager.pending is not None,
              "clearing on her own words would cancel the confirmation she just asked")

    check("a clean 'yes' still gets through over echo",
          manager.answer("yes", echo=True)[0] == "execute")
    manager.request(command)
    check("and a clean 'no' does too",
          manager.answer("no", echo=True)[0] == "cancel")
    manager.request(command)
    check("but an ambiguous reply on echo-flagged audio is ignored, not re-asked",
          manager.answer("yes but hold on", echo=True)[0] == "none-echo")
    check("without consuming an ask", manager.pending.asks == 1)

    # The resolver is wired to the gate, and BEFORE it.
    app_source = io.open(os.path.join(PROJECT_ROOT, "src", "kayra", "app.py"),
                         encoding="utf-8").read()
    check("Listen() hands the echo verdict to the resolver",
          "echo=spoken_over_tts" in app_source)
    check("and the recognizer's own alternatives with it",
          "alternatives=confirmation_alternatives" in app_source,
          "a one-word reply is where the recognizer is least certain")
    check("and resolves the confirmation BEFORE the echo gate discards anything",
          app_source.index("resolve_lifecycle_confirmation(user_input")
          < app_source.index("if spoken_over_tts:"),
          "the answer to a spoken question arrives while she is still audible")

    # ── Both bounds are configurable, and clamped ──
    saved = {k: os.environ.get(k) for k in ("KAYRA_CONFIRMATION_TTL_SECONDS",
                                            "KAYRA_DMM_RETRY_BUDGET_SECONDS")}
    try:
        os.environ["KAYRA_CONFIRMATION_TTL_SECONDS"] = "7"
        check("the confirmation window is configurable",
              vc.ControlConfirmations()._ttl == 7.0)
        os.environ["KAYRA_CONFIRMATION_TTL_SECONDS"] = "99999"
        check("and clamped — a shutdown must not stay armed for an hour",
              vc.ControlConfirmations()._ttl <= 300.0)
        os.environ["KAYRA_CONFIRMATION_TTL_SECONDS"] = "0"
        check("a zero window is corrected, not obeyed",
              vc.ControlConfirmations()._ttl >= 5.0,
              "an unanswerable confirmation is worse than a long one")
        os.environ["KAYRA_CONFIRMATION_TTL_SECONDS"] = "nonsense"
        check("a malformed value falls back to the default",
              vc.ControlConfirmations()._ttl == 20.0)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── Expiry ──
    clock.now = 0
    manager = vc.ControlConfirmations(ttl=20.0, clock=lambda: clock.now / 1000.0)
    manager.request(command)
    clock.advance(19_000)
    check("still pending just before the deadline", manager.pending is not None)
    clock.advance(2_000)
    check("expired just after it", manager.pending is None)
    check("expiry is reported once", manager.expire_if_due() is None)
    manager.request(command)
    clock.advance(30_000)
    check("a late 'yes' after expiry does not execute",
          manager.answer("yes") == ("none", None))

    # ── One at a time ──
    manager = vc.ControlConfirmations(ttl=20.0, clock=lambda: clock.now / 1000.0)
    manager.request(vc.classify_control("shut down Kayra"))
    manager.request(vc.classify_control("go to sleep"))
    check("a new request replaces the old one",
          manager.pending.kind == vc.ControlKind.SLEEP,
          "two outstanding questions is a state nobody can answer unambiguously")

    # ── The wording ──
    shutdown_q = vc.confirmation_question(vc.ControlKind.SHUTDOWN, explicit=True)
    check("the shutdown question names Kayra", "kayra" in shutdown_q.lower(), shutdown_q)
    check("and never the computer",
          "computer" not in shutdown_q.lower() and "pc" not in shutdown_q.lower(),
          shutdown_q)
    bare_q = vc.confirmation_question(vc.ControlKind.SHUTDOWN, explicit=False)
    check("a bare request says what it thinks it heard",
          "heard" in bare_q.lower(), bare_q)
    sleep_q = vc.confirmation_question(vc.ControlKind.SLEEP)
    check("the sleep question describes sleep mode", "sleep mode" in sleep_q.lower(), sleep_q)
    check("and promises to keep listening", "listen" in sleep_q.lower(), sleep_q)
    check("acknowledgements are short and spoken",
          all(len(vc.confirmation_ack(k, c)) < 60
              for k in (vc.ControlKind.SHUTDOWN, vc.ControlKind.SLEEP)
              for c in (True, False)))

    # ── Cost ──
    started = time.perf_counter()
    for _ in range(20_000):
        vc.read_confirmation("yes")
    per_call_us = (time.perf_counter() - started) / 20_000 * 1_000_000
    print_info(f"      read_confirmation: {per_call_us:.2f} us/call")
    check("reading a confirmation is local and free", per_call_us < 50.0,
          f"{per_call_us:.2f} us")


# ┌────────────────────────────────────────────────────────────────────────┐
# │        7. THE DMM RETRY CONTRACT, AGAINST A LOCAL MODEL                │
# └────────────────────────────────────────────────────────────────────────┘

def section_dmm_retries():
    check.section("[7] DMM empty-response retries (local model)")
    from kayra.intelligence import llm_engine

    check("the bound is five", llm_engine.MAX_DMM_EMPTY_RETRIES == 5)

    class FakeLocalEngine:
        """
        `classify_intent` with everything except the retry contract stubbed out.

        NO PROVIDER IS CONTACTED — not Cohere, not Groq, not Gemini, and not a local server.
        `is_online` is False, so the real method takes the `_dmm_local` branch, and that is
        the one method substituted. This is what makes the suite runnable while every cloud
        key is rate-limited, which is the state the user is actually in.
        """

        is_online = False
        funcs = ["general ", "open ", "close ", "exit"]
        # THE REAL METHOD, bound to a stand-in. Everything it depends on is provided below,
        # so what runs is the production retry contract rather than a paraphrase of it.
        classify_intent = llm_engine.CentralizedLLMEngine.classify_intent
        _local_unhealthy = llm_engine.CentralizedLLMEngine._local_unhealthy
        _note_local_result = llm_engine.CentralizedLLMEngine._note_local_result

        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            if self.responses:
                return self.responses.pop(0)
            return ""

    # ── Persistent empty: exactly six calls (the first plus five retries), never seven ──
    engine = FakeLocalEngine([""] * 20)
    result = engine.classify_intent("open chrome")
    check("a persistently empty local model is called 1 + 5 times",
          engine.calls == 6, f"{engine.calls} calls")
    check("there is never a seventh attempt", engine.calls <= 6)
    check("and the request degrades to conversation",
          result == ["general open chrome"], str(result))

    # ── Success on each attempt stops immediately ──
    for attempt in range(1, 7):
        responses = [""] * (attempt - 1) + ["open chrome"]
        engine = FakeLocalEngine(responses)
        result = engine.classify_intent("open chrome")
        check(f"a valid response on attempt {attempt} stops there",
              engine.calls == attempt, f"{engine.calls} calls")
        check(f"and returns the classification (attempt {attempt})",
              result == ["open chrome"], str(result))

    # ── Whitespace and junk count as empty; a real token does not ──
    engine = FakeLocalEngine(["   ", ",,,", "not a known header", "open chrome"])
    result = engine.classify_intent("open chrome")
    check("unparseable output is retried like an empty one",
          engine.calls == 4 and result == ["open chrome"], f"{engine.calls}")

    # ── THE WALL-CLOCK BUDGET, on top of the count ──
    # A count alone is not a bound on the user's wait. Measured against the real local model
    # on this host an empty completion costs ~2.0s, so five retries is ~10s of silence for a
    # request that ends in "treat this as conversation". The budget stops a SLOWER backend
    # turning that into half a minute, and does not fire on a normal one.
    check("a retry budget exists", llm_engine.DMM_RETRY_BUDGET_SECONDS > 0)
    saved_budget = os.environ.get("KAYRA_DMM_RETRY_BUDGET_SECONDS")
    try:
        os.environ["KAYRA_DMM_RETRY_BUDGET_SECONDS"] = "9"
        check("the budget is configurable", llm_engine._retry_budget_seconds() == 9.0)
        os.environ["KAYRA_DMM_RETRY_BUDGET_SECONDS"] = "0"
        check("and clamped — a zero budget would disable the retry entirely",
              llm_engine._retry_budget_seconds() >= 2.0)
    finally:
        if saved_budget is None:
            os.environ.pop("KAYRA_DMM_RETRY_BUDGET_SECONDS", None)
        else:
            os.environ["KAYRA_DMM_RETRY_BUDGET_SECONDS"] = saved_budget
    check("and it is generous enough for five fast attempts",
          llm_engine.DMM_RETRY_BUDGET_SECONDS >= 10.0,
          f"{llm_engine.DMM_RETRY_BUDGET_SECONDS}s")

    class SlowLocalEngine(FakeLocalEngine):
        """A local model that answers nothing, slowly — the case the budget is for."""

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            clock.advance(6_000)            # 6s per attempt, on the injected clock
            return ""

    clock = FakeClock(0)
    original_monotonic = llm_engine.time.monotonic
    try:
        llm_engine.time.monotonic = lambda: clock.now / 1000.0
        engine = SlowLocalEngine([])
        engine.classify_intent("open chrome")
        check("a slow backend stops on the budget rather than on the count",
              engine.calls < 6, f"{engine.calls} calls at 6s each")
        check("and it still made more than one attempt", engine.calls >= 2,
              f"{engine.calls} calls")

        # A FAST backend must still get all five. The budget is a ceiling, not a schedule.
        class FastEmptyEngine(FakeLocalEngine):
            def _dmm_local(self, prompt, timeout=None):
                self.calls += 1
                clock.advance(10)
                return ""

        engine = FastEmptyEngine([])
        engine.classify_intent("open chrome")
        check("a fast backend still gets the full five retries",
              engine.calls == 6, f"{engine.calls} calls")
    finally:
        llm_engine.time.monotonic = original_monotonic

    # ── BOUNDED BACKOFF, and the first retry is free ──
    # The common case is a retry that SUCCEEDS, so delaying the first one is pure added
    # latency on the path that works. Later attempts get a small linear pause — not
    # exponential: this is not congestion control, it is a sampler that produced a stop token,
    # and a growing wait would only make the worst case worse.
    engine = FakeLocalEngine([""] * 20)
    started = time.perf_counter()
    engine.classify_intent("open chrome")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print_info(f"      five exhausted retries: {elapsed_ms:.2f} ms of backoff")
    expected_ms = sum(min(llm_engine.DMM_RETRY_DELAY_MS * i,
                          llm_engine.DMM_MAX_RETRY_DELAY_MS)
                      for i in range(llm_engine.MAX_DMM_EMPTY_RETRIES))
    check("the total backoff is bounded and small", elapsed_ms < expected_ms + 400,
          f"{elapsed_ms:.0f} ms against a {expected_ms} ms budget")
    check("and it is far below the retry budget",
          elapsed_ms < llm_engine._retry_budget_seconds() * 1000.0)

    engine = FakeLocalEngine(["", "open chrome"])
    started = time.perf_counter()
    engine.classify_intent("open chrome")
    first_retry_ms = (time.perf_counter() - started) * 1000.0
    check("the FIRST retry is immediate", first_retry_ms < 60.0,
          f"{first_retry_ms:.1f} ms — the common case is a retry that succeeds")

    # ── The retry is over OUTPUT only. A transport failure must not multiply. ──
    source = io.open(llm_engine.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    classify = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "classify_intent")
    # The ONE sleep is the bounded backoff, and it is capped by the remaining budget so it
    # can never extend a request past the deadline.
    sleeps = [n for n in ast.walk(classify)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "sleep"]
    check("there is at most one sleep in the retry loop", len(sleeps) <= 1, str(len(sleeps)))
    check("and it is bounded by a maximum delay",
          llm_engine.DMM_MAX_RETRY_DELAY_MS <= 1000,
          f"{llm_engine.DMM_MAX_RETRY_DELAY_MS} ms")
    check("the backoff is linear, not exponential",
          "DMM_RETRY_DELAY_MS * retries" in source,
          "a growing wait makes the worst case worse for no benefit")
    recursions = [n for n in ast.walk(classify)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "classify_intent"]
    check("there is exactly one retry call site", len(recursions) == 1)

    # ── The log line the user will read ──
    from kayra.core import logbus
    lines = []
    original = logbus.log
    try:
        logbus.log = lambda level, sub, msg, **kw: lines.append(msg) or original(
            level, sub, msg, **kw)
        engine = FakeLocalEngine([""] * 20)
        engine.classify_intent("open chrome")
    finally:
        logbus.log = original
    retries = [line for line in lines if "retry" in line.lower() and "/" in line]
    check("retries are numbered out of five", len(retries) == 5, str(retries))
    check("the numbering runs 1/5 to 5/5",
          any("1/5" in line for line in retries) and any("5/5" in line for line in retries),
          str(retries))
    check("exhaustion is stated once", sum("exhausted" in line for line in lines) == 1,
          str([line for line in lines if "exhausted" in line]))
    # THE TURN IS STAMPED BY THE CORRELATOR, NOT BY THE MESSAGE. Putting it in both produced
    # "[DMM] Turn #1 · Turn #1 retry 1/5", so the message says what happened and `logbus`
    # says whose turn it was. The correlation is asserted on the RENDERED line below.
    check("a retry message does not repeat the turn number",
          not any("Turn #" in line for line in retries), str(retries[:2]))
    rendered = llm_engine.logbus.format_line(
        llm_engine.logbus.INFO, llm_engine.Subsystem.DMM,
        "empty response — retry 1/5", turn=42)
    check("but the rendered line carries it", "Turn #42" in rendered, rendered)
    check("no traceback is printed for an expected empty response",
          not any("Traceback" in line for line in lines))


# ┌────────────────────────────────────────────────────────────────────────┐
# │      9. SHORT CONFIRMATION ANSWERS, AND WHAT THEY MAY NOT DO           │
# └────────────────────────────────────────────────────────────────────────┘
# OBSERVED LIVE: the user says "yes" and the recognizer commits "S". That matters more than it
# looks, because "yes" is the word that authorises a shutdown.

def section_short_answers():
    check.section("[9] Degraded short confirmation answers")

    # ── Route 1: the recognizer's OWN alternatives. Nothing is invented. ──
    reply, evidence = vc.resolve_short_answer(
        "s", alternatives=[{"text": "s", "confidence": 0.3},
                           {"text": "yes", "confidence": 0.28}])
    check("a 'yes' among the recognizer's alternatives is preferred",
          reply == vc.ConfirmationReply.YES, str(reply))
    check("and the evidence says it came from the N-best list",
          evidence.startswith("n-best"), evidence)

    reply, _ = vc.resolve_short_answer(
        "s", alternatives=[{"text": "s"}, {"text": "no"}])
    check("an alternative reading of 'no' is honoured too",
          reply == vc.ConfirmationReply.NO, str(reply))

    # ── Route 2: a strict affix, under every guard ──
    for token, expected in (("s", vc.ConfirmationReply.YES),
                            ("S", vc.ConfirmationReply.YES),
                            ("ye", vc.ConfirmationReply.YES),
                            ("yea", vc.ConfirmationReply.YES),
                            ("sur", vc.ConfirmationReply.YES),
                            ("n", vc.ConfirmationReply.NO),
                            ("na", vc.ConfirmationReply.NO),
                            ("nop", vc.ConfirmationReply.NO)):
        got, evidence = vc.resolve_short_answer(token)
        check(f"'{token}' recovers to {expected}", got == expected, f"{got} {evidence}")

    # ── THE FALSE POSITIVES. These are the whole reason it is guarded. ──
    for token in ("school", "system", "its", "stop", "yesterday", "sunday", "essay",
                  "session", "sorry", "north", "nothing"):
        got, _ = vc.resolve_short_answer(token)
        check(f"'{token}' is NOT recovered as an answer", got is None, str(got))

    # A token that could be either polarity is a refusal, never a coin toss — the two
    # outcomes are "execute the shutdown" and "cancel it".
    got, _ = vc.resolve_short_answer("o")
    check("'o' is ambiguous across polarities and refused", got is None, str(got))

    # A sentence is not an answer, however short its words.
    for phrase in ("tell me about s", "my grade is s", "the letter s", "s is a letter"):
        got, _ = vc.resolve_short_answer(phrase)
        check(f"'{phrase}' is a sentence, not an answer", got is None, str(got))

    # A confident recognizer keeps its reading.
    got, _ = vc.resolve_short_answer("s", confidence=0.95)
    check("a confidently recognised 's' is left alone", got is None, str(got))
    got, _ = vc.resolve_short_answer("s", confidence=0.2)
    check("a low-confidence 's' is eligible", got == vc.ConfirmationReply.YES, str(got))

    # ── THE GUARD THAT MATTERS MOST: none of this is reachable without a pending action ──
    manager = vc.ControlConfirmations()
    check("with nothing pending, 's' answers nothing",
          manager.answer("s") == ("none", None))
    check("and the manager has no pending request to execute", manager.pending is None)

    manager.request(vc.classify_control("shut down kayra"))
    outcome, request = manager.answer("s")
    check("with a shutdown pending, 's' confirms it", outcome == "execute", outcome)
    check("and it executes the PENDING action, not something else",
          request.kind == vc.ControlKind.SHUTDOWN)
    check("the evidence is recorded for the log",
          bool(manager.last_evidence), manager.last_evidence)

    manager.request(vc.classify_control("go to sleep"))
    outcome, request = manager.answer("s")
    check("with a sleep pending, 's' confirms THAT", outcome == "execute"
          and request.kind == vc.ControlKind.SLEEP, f"{outcome} {request.kind}")

    for noise in ("school", "system", "its"):
        manager.request(vc.classify_control("shut down kayra"))
        outcome, _ = manager.answer(noise)
        check(f"'{noise}' does not confirm a pending shutdown", outcome != "execute", outcome)

    # ── ECHO-FLAGGED AUDIO IS NEVER RECOVERED ──
    # A degraded token attributed to Kayra's own voice must not become an authorisation.
    manager.request(vc.classify_control("shut down kayra"))
    outcome, _ = manager.answer("s", echo=True)
    check("a degraded token on echo-flagged audio confirms nothing",
          outcome != "execute", outcome)
    check("and the question stays open", manager.pending is not None)

    # ── NO GLOBAL REPLACEMENT MAP, asserted structurally ──
    source = io.open(vc.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    word_maps = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not node.keys:
            continue
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)
                 and isinstance(v, ast.Constant) and isinstance(v.value, str)]
        # A word -> word map: every key and value a single lower-case token.
        if pairs and len(pairs) == len(node.keys) and all(
                " " not in k.value and " " not in v.value and k.value.islower()
                for k, v in pairs):
            word_maps.append(node.lineno)
    check("there is no word-to-word replacement map anywhere in voice_control",
          not word_maps, str(word_maps))
    check("and no literal 's' -> 'yes' substitution",
          '"s": "yes"' not in source and "'s': 'yes'" not in source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │   10. THE POWER-TARGET BOUNDARY — KAYRA IS NOT THE COMPUTER            │
# └────────────────────────────────────────────────────────────────────────┘

def section_power_boundary():
    check.section("[10] Kayra shutdown vs computer shutdown")
    from kayra.automation import windows as auto
    from kayra.automation.policy import (classify_action, Risk, POWER_VERBS,
                                         resolve_power_target, TARGET_COMPUTER,
                                         TARGET_KAYRA, TARGET_AMBIGUOUS)

    # THE REPORTED FAILURE. "Shutdown the engine car." — a malformed transcript of a request
    # about KAYRA — reached `system.shutdown` through a substring test and produced
    # "This will shut down your computer. Should I go ahead?".
    for token in ("system shutdown the engine car",
                  "system shut down the engine",
                  "system shutdown",
                  "system shut down",
                  "system restart",
                  "system sleep",
                  "system shutdown kayra",
                  "system turn off the engine"):
        action = auto.normalize_command(token)
        verdict, _reason = classify_action(action)
        check(f"'{token}' never becomes a machine power action",
              action.action not in POWER_VERBS, action.key)
        check(f"'{token}' is denied", verdict == Risk.DENY, str(verdict))
        outcome = auto.execute_action(action)
        check(f"'{token}' executes nothing", outcome.status != auto.Status.OK, outcome.status)
        check(f"'{token}' answers with words", bool(outcome.message))

    # An EXPLICIT computer target is the only thing that reaches the machine, and it still
    # has to be confirmed.
    for token in ("system shutdown the computer", "system turn off my pc",
                  "system restart the computer", "system power off the laptop"):
        action = auto.normalize_command(token)
        verdict, _ = classify_action(action)
        check(f"'{token}' resolves to a machine power action",
              action.action in POWER_VERBS, action.key)
        check(f"'{token}' still requires confirmation", verdict == Risk.CONFIRM, str(verdict))

    # The refusal for an ambiguous request is a QUESTION naming both, not a flat no.
    ambiguous = auto.execute_action(auto.normalize_command("system shutdown"))
    check("an ambiguous power request asks which target",
          "kayra" in ambiguous.message.lower() and "computer" in ambiguous.message.lower(),
          ambiguous.message)

    # And the Kayra-targeted one points at the phrase that works.
    kayra_side = auto.execute_action(auto.normalize_command("system shutdown the engine car"))
    check("a Kayra-targeted power request explains how to phrase it",
          "shut down kayra" in kayra_side.message.lower(), kayra_side.message)

    check("resolve_power_target defaults to AMBIGUOUS, never to the computer",
          resolve_power_target("") == TARGET_AMBIGUOUS
          and resolve_power_target("do the thing") == TARGET_AMBIGUOUS)
    check("naming both targets is ambiguous",
          resolve_power_target("shut down kayra and the computer") == TARGET_AMBIGUOUS)
    check("'system' is NOT a computer-target word",
          "system" not in __import__("kayra.automation.policy", fromlist=["x"])
          .COMPUTER_TARGET_WORDS,
          "it is the DMM's own token prefix and would match every payload")

    # Turning the display off is not suspending the machine.
    screen = auto.normalize_command("system turn off screen")
    check("'turn off screen' blanks the display instead of suspending",
          screen.key == "system.screen_off", screen.key)
    check("and needs no power target", classify_action(screen)[0] == Risk.ALLOW)


# ┌────────────────────────────────────────────────────────────────────────┐
# │      11. TURN OWNERSHIP AND STALE WORK                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_turn_ownership():
    check.section("[11] Turn ownership")
    from kayra.core import logbus
    from kayra.intelligence import llm_engine

    logbus.end_turn()
    check("no turn is open to begin with", logbus.current_turn() == 0)
    first = logbus.begin_turn()
    check("a turn has a number", first > 0, str(first))
    second_source = first
    logbus.end_turn()
    second = logbus.begin_turn()
    check("the next turn is a later number", second > second_source, f"{second_source}->{second}")

    check("an uncorrelated call is never superseded",
          llm_engine._turn_superseded(0) is False,
          "boot-time and diagnostic calls carry no turn")
    check("the CURRENT turn is not superseded by itself",
          llm_engine._turn_superseded(second) is False)
    check("an OLDER turn is superseded",
          llm_engine._turn_superseded(second_source) is True,
          "a retry chain outliving its turn is working on a question the user moved on from")
    logbus.end_turn()

    # A retry chain abandons rather than running to five when its turn is replaced.
    class SupersededEngine:
        is_online = False
        funcs = ["general ", "open "]
        classify_intent = llm_engine.CentralizedLLMEngine.classify_intent
        _local_unhealthy = llm_engine.CentralizedLLMEngine._local_unhealthy
        _note_local_result = llm_engine.CentralizedLLMEngine._note_local_result

        def __init__(self):
            self.calls = 0
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            if self.calls == 2:
                # The user says something else while this chain is mid-flight.
                logbus.end_turn()
                logbus.begin_turn()
            return ""

    logbus.end_turn()
    owner = logbus.begin_turn()
    engine = SupersededEngine()
    result = engine.classify_intent("open chrome", turn=owner)
    check("a superseded retry chain stops early",
          engine.calls < 6, f"{engine.calls} calls")
    check("and returns nothing rather than a stale classification",
          result == [], str(result))
    logbus.end_turn()

    # Shutdown cancels in-flight work before anything is disposed.
    app_source = io.open(os.path.join(PROJECT_ROOT, "src", "kayra", "app.py"),
                         encoding="utf-8").read()
    check("shutdown cancels active work", "_cancel_active_work()" in app_source)
    check("and does so before the browser session is reaped",
          app_source.index("    _cancel_active_work()")
          < app_source.rindex("stt_engine.shutdown("),
          "a retry printing after the farewell shows the assistant working after goodbye")

    # The turn is opened and closed on the same path.
    check("the loop opens a numbered turn", "logbus.begin_turn()" in app_source)
    check("and closes it when the turn ends",
          app_source.count("logbus.end_turn()") >= 2,
          "an open turn makes every later line claim to belong to it")


# ┌────────────────────────────────────────────────────────────────────────┐
# │    12. LOCAL MODEL HEALTH, AND WHAT IS WORTH RETRYING                  │
# └────────────────────────────────────────────────────────────────────────┘

def section_local_health():
    check.section("[12] Local model health and retry classification")
    from kayra.intelligence import llm_engine

    check("an empty completion is retryable",
          "EMPTY_RESPONSE" in llm_engine.RETRYABLE_DMM_FAILURES)
    for kind in ("INVALID_REQUEST", "AUTH_FAILURE", "MODEL_UNAVAILABLE"):
        check(f"{kind} is terminal", kind in llm_engine.TERMINAL_DMM_FAILURES)
        check(f"{kind} is not retryable", kind not in llm_engine.RETRYABLE_DMM_FAILURES)
    check("the two sets are disjoint",
          not (llm_engine.RETRYABLE_DMM_FAILURES & llm_engine.TERMINAL_DMM_FAILURES))

    class FailingEngine:
        is_online = False
        funcs = ["general ", "open "]
        classify_intent = llm_engine.CentralizedLLMEngine.classify_intent
        _local_unhealthy = llm_engine.CentralizedLLMEngine._local_unhealthy
        _note_local_result = llm_engine.CentralizedLLMEngine._note_local_result

        def __init__(self, exc):
            self.exc = exc
            self.calls = 0
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            raise self.exc

    class _BadRequest(Exception):
        pass
    _BadRequest.__name__ = "BadRequestError"

    engine = FailingEngine(_BadRequest("invalid request: bad schema"))
    result = engine.classify_intent("open chrome")
    check("a terminal failure is not retried five times", engine.calls == 1,
          f"{engine.calls} calls")
    check("and degrades to conversation", result == ["general open chrome"], str(result))

    # ── The health cooldown ──
    class EmptyEngine:
        is_online = False
        funcs = ["general ", "open "]
        classify_intent = llm_engine.CentralizedLLMEngine.classify_intent
        _local_unhealthy = llm_engine.CentralizedLLMEngine._local_unhealthy
        _note_local_result = llm_engine.CentralizedLLMEngine._note_local_result

        def __init__(self):
            self.calls = 0
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            return ""

    engine = EmptyEngine()
    for _ in range(llm_engine.DMM_UNHEALTHY_AFTER):
        engine.classify_intent("open chrome")
    calls_before = engine.calls
    check("three empty REQUESTS stand the model down",
          engine._local_cooldown_until > 0, str(engine._local_cooldown_until))
    engine.classify_intent("open chrome")
    check("and the next request makes no attempt at all",
          engine.calls == calls_before, f"{engine.calls} vs {calls_before}")
    check("the streak counts requests, not attempts",
          engine._local_empty_streak == llm_engine.DMM_UNHEALTHY_AFTER,
          str(engine._local_empty_streak))

    # One success clears it. The request after the cooldown IS the probe.
    engine._local_cooldown_until = 0.0
    engine._note_local_result(True)
    check("a success clears the latch", engine._local_empty_streak == 0
          and engine._local_cooldown_until == 0.0)

    check("the cooldown is short — this is a stand-down, not an outage",
          5.0 <= llm_engine.DMM_HEALTH_COOLDOWN_SECONDS <= 60.0,
          f"{llm_engine.DMM_HEALTH_COOLDOWN_SECONDS}s")


# ┌────────────────────────────────────────────────────────────────────────┐
# │     8. NO PATH REACHES A DANGEROUS ACTION WITHOUT A CONFIRMATION       │
# └────────────────────────────────────────────────────────────────────────┘

def section_no_bypass():
    check.section("[8] No unconfirmed path to a dangerous action")

    source = io.open(os.path.join(PROJECT_ROOT, "src", "kayra", "app.py"),
                     encoding="utf-8").read()
    tree = ast.parse(source)

    def function_named(name):
        return next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)

    dispatch = function_named("_dispatch_control")
    check("_dispatch_control exists", dispatch is not None)
    called = {n.func.id for n in ast.walk(dispatch)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("_dispatch_control never calls request_shutdown", "request_shutdown" not in called)
    # WAKE calls `set_sleeping(False)` and must — waking is not a dangerous action, it is
    # the undo for one. What may not exist here is a call that puts Kayra TO sleep.
    sleeping_true = [n for n in ast.walk(dispatch)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "set_sleeping"
                     and n.args and isinstance(n.args[0], ast.Constant)
                     and n.args[0].value is True]
    check("_dispatch_control never puts Kayra to sleep", not sleeping_true)
    check("it may still wake it",
          any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "set_sleeping" for n in ast.walk(dispatch)))
    check("it asks instead", "_ask_confirmation" in called)

    executor = function_named("_execute_confirmed")
    check("_execute_confirmed exists", executor is not None)
    exec_called = {n.func.id for n in ast.walk(executor)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("it is the one that shuts down", "request_shutdown" in exec_called)
    check("and the one that sleeps", "set_sleeping" in exec_called)

    # Who may call the executor? Only the resolver, which only reaches it on an affirmative.
    callers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_execute_confirmed"):
                    callers.append(node.name)
    check("only the confirmation resolver executes a confirmed control",
          callers == ["resolve_lifecycle_confirmation"], str(callers))

    # The UI, the tray and the signal handler still reach shutdown DIRECTLY, and should: a
    # button press is already an unambiguous confirmed intent. What must not exist is a path
    # from a TRANSCRIPT that skips the question.
    listen = function_named("Listen")
    listen_calls = {n.func.id for n in ast.walk(listen)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("Listen() never shuts down directly", "request_shutdown" not in listen_calls)
    check("Listen() never sleeps directly", "set_sleeping" not in listen_calls)

    watcher = function_named("_local_control_watcher")
    watcher_calls = {n.func.id for n in ast.walk(watcher)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("the control watcher never shuts down directly",
          "request_shutdown" not in watcher_calls)

    # And the repair stage may not INVENT a dangerous target.
    from kayra.input import transcript_repair
    check("the repair stage refuses to invent any dangerous control",
          transcript_repair._is_irreversible("exit")
          and transcript_repair._is_irreversible("go to sleep"),
          "widened from SHUTDOWN to every dangerous kind")
    check("but ordinary words are still repairable",
          not transcript_repair._is_irreversible("chrome"))

    repair_source = io.open(transcript_repair.__file__, encoding="utf-8").read()
    check("the repair stage still has no word-replacement dictionary",
          "DANGEROUS_KINDS" in repair_source
          and "_REPLACEMENTS" not in repair_source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │   13. LIFECYCLE ANNOUNCEMENTS AND THE TURN'S VISIBLE FLOW               │
# └────────────────────────────────────────────────────────────────────────┘
# Three things the user reads or hears, and one they wait on:
#
#   * ONE spoken line at boot, after every subsystem is verified and before listening.
#   * ONE spoken countdown on a CONFIRMED shutdown, before the process exits.
#   * ONE terminal line per lifecycle stage that actually CHANGED.
#   * A retry chain bounded per attempt as well as in total.
#
# Deliberately small. The retry contract, the confirmation state machine and the shutdown
# ORDER are asserted in full in sections 7, 6 and in `test_voice_control.py`; what is added
# here is only what those suites did not already cover.

def section_lifecycle_announcements():
    check.section("[13] Boot / shutdown announcements and the turn flow")

    from kayra import app
    from kayra.core import logbus

    class FakeTTS:
        def __init__(self):
            self.spoken = []

        def begin_turn(self):
            pass

        def speak(self, text, blocking=False):
            self.spoken.append((text, blocking))

    class FakeEngine:
        def __init__(self, online):
            self.is_online = online

    # ── The boot announcement plays exactly once, and names the LIVE tier ──
    saved = (app.tts_engine, app.TTS_ENABLED, app.engine, app._boot_announced.is_set())
    try:
        tts = FakeTTS()
        app.tts_engine, app.TTS_ENABLED = tts, True
        app.engine = FakeEngine(False)
        app._boot_announced.clear()

        first = app.speak_boot_announcement()
        second = app.speak_boot_announcement()
        check("the boot announcement is spoken", len(tts.spoken) == 1, str(tts.spoken))
        check("and never a second time in one process", second == "", repr(second))
        check("it is not blocking, so the microphone opens while it plays",
              tts.spoken[0][1] is False)
        check("a local model is announced as local",
              "local intelligence" in first.lower(), first)
        check("the line says it is ready", "ready" in first.lower(), first)

        app.engine = FakeEngine(True)
        check("a cloud model is announced as cloud",
              "cloud intelligence" in app.boot_announcement().lower(),
              app.boot_announcement())
    finally:
        app.tts_engine, app.TTS_ENABLED, app.engine = saved[0], saved[1], saved[2]
        if saved[3]:
            app._boot_announced.set()
        else:
            app._boot_announced.clear()

    app_source = io.open(app.__file__, encoding="utf-8").read()
    app_tree = ast.parse(app_source)

    check("the tier is read from the live engine, never from .env",
          "is_online" in app_source.split("def boot_announcement")[1].split("\ndef ")[0])

    # ── ...after initialization, and before the first Listen() ──
    main_fn = next(n for n in ast.walk(app_tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "main")
    main_calls = [n.func.id for n in ast.walk(main_fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("main() boots every subsystem before it runs the loop",
          "bootstrap" in main_calls, str(main_calls))
    loop_src = app_source.split("async def Main_Loop")[1].split("\ndef ")[0]
    check("the announcement is made inside Main_Loop",
          "speak_boot_announcement()" in loop_src)
    check("and BEFORE the listening loop it precedes",
          loop_src.index("speak_boot_announcement()") < loop_src.index("while True:"),
          "an announcement after the first capture is not a startup announcement")
    session_source = io.open(
        os.path.join(PROJECT_ROOT, "src", "kayra", "ui", "session.py"),
        encoding="utf-8").read()
    check("both front ends announce through the same latched function",
          "speak_boot_announcement" in session_source)

    # ── The confirmed shutdown speaks the countdown, and only Kayra dies ──
    check("the announcement counts down",
          "3..." in app.SHUTDOWN_ANNOUNCEMENT and "1..." in app.SHUTDOWN_ANNOUNCEMENT,
          app.SHUTDOWN_ANNOUNCEMENT)
    check("and ends on a goodbye",
          app.SHUTDOWN_ANNOUNCEMENT.strip().endswith("Goodbye."),
          app.SHUTDOWN_ANNOUNCEMENT)
    check("it never claims to power off the machine",
          not any(word in app.SHUTDOWN_ANNOUNCEMENT.lower()
                  for word in ("computer", "windows", " pc")),
          app.SHUTDOWN_ANNOUNCEMENT)

    confirmed = next(n for n in ast.walk(app_tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_execute_confirmed")
    farewells = [kw.value for n in ast.walk(confirmed)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "request_shutdown"
                 for kw in n.keywords if kw.arg == "farewell"]
    check("a CONFIRMED shutdown asks for the spoken farewell",
          len(farewells) == 1 and getattr(farewells[0], "value", None) is True,
          str(farewells))

    shutdown_src = app_source.split("def request_shutdown")[1].split("\ndef ")[0]
    check("the countdown is spoken blocking, so it finishes before the exit",
          "SHUTDOWN_ANNOUNCEMENT, True" in shutdown_src)
    check("and it is spoken before any resource is disposed",
          shutdown_src.index("SHUTDOWN_ANNOUNCEMENT")
          < shutdown_src.index("proactive_agent.stop"))
    check("the announcement is a fixed sentence, never generated",
          "SHUTDOWN_ANNOUNCEMENT = (" in app_source
          and "classify_intent" not in shutdown_src,
          "nothing that runs during teardown may wait on a model")
    check("the shutdown path never reaches a Windows power action",
          "system shutdown" not in shutdown_src and "shutdown /s" not in shutdown_src,
          "a Windows shutdown is a separate, separately confirmed automation action")

    # ── The turn flow: one line per CHANGE, never one per callback ──
    lines = []
    original = logbus.log
    saved_flow = app._flow_last
    try:
        logbus.log = lambda level, sub, msg, **kw: lines.append(msg)
        app._flow_last = None
        for _ in range(50):
            app._voice_flow("Listening")
        check("fifty listening callbacks produce ONE line", len(lines) == 1, str(lines))

        app._voice_flow("Processing")
        app._voice_flow("Processing")
        check("and a real change produces exactly one more",
              len(lines) == 2 and lines[1] == "Processing", str(lines))

        app._voice_flow("Listening")
        check("returning to listening is a change, so it is logged",
              len(lines) == 3, str(lines))

        lines.clear()
        app._flow_last = None
        app._announce_reply("  Chrome   is open.  ")
        check("the reply is announced once, normalized and quoted",
              len(lines) == 1 and '"Chrome is open."' in lines[0], str(lines))
        app._announce_reply("Chrome is open.")
        check("and an identical reply in the same turn is not repeated",
              len(lines) == 1, str(lines))

        lines.clear()
        app._flow_last = None
        app._announce_reply("x" * 500)
        check("a very long answer is truncated rather than flooding the terminal",
              len(lines[0]) < 200, str(len(lines[0])))
        check("an empty reply says nothing", app._announce_reply("") is False)
    finally:
        logbus.log = original
        app._flow_last = saved_flow

    check("the committed transcript is the ONE transcript line, at INFO",
          'Transcribed: "{user_input}"' in app_source)
    check("the endpoint's own reasoning stays at DEBUG",
          "logbus.debug(Subsystem.VOICE" in app_source)
    check("the DMM token list is a DEBUG diagnostic, not a lifecycle line",
          "logbus.debug(Subsystem.DMM, f\"tokens=" in app_source)
    check("stale work cannot print itself as the current turn",
          "logbus.current_turn()" in app_source.split("def _voice_flow")[1]
          .split("\ndef ")[0],
          "the flow key carries the OPEN turn, which is 0 between turns")


# ┌────────────────────────────────────────────────────────────────────────┐
# │   14. THE DMM PER-ATTEMPT RETRY CEILING                                 │
# └────────────────────────────────────────────────────────────────────────┘
# The budget in section 7 bounds the whole CHAIN. It does not bound one ATTEMPT, so a local
# server that accepts a request and then takes twelve seconds to answer nothing spent almost
# the entire chain on one useless call and the five retries never happened.

def section_attempt_budget():
    check.section("[14] The DMM per-attempt ceiling")
    from kayra.intelligence import llm_engine

    engine_source = io.open(llm_engine.__file__, encoding="utf-8").read()

    check("the count is unchanged at five", llm_engine.MAX_DMM_EMPTY_RETRIES == 5)
    check("each attempt gets about three seconds",
          2.0 <= llm_engine.DMM_ATTEMPT_TIMEOUT_SECONDS <= 3.5,
          f"{llm_engine.DMM_ATTEMPT_TIMEOUT_SECONDS}s")
    check("the whole chain is still bounded at about fifteen",
          llm_engine.DMM_RETRY_BUDGET_SECONDS <= 15.0,
          f"{llm_engine.DMM_RETRY_BUDGET_SECONDS}s")
    check("the per-attempt ceiling is capped by what is left of the budget",
          llm_engine._attempt_timeout_seconds(0.4) == 0.4
          and llm_engine._attempt_timeout_seconds(60.0)
          == llm_engine.DMM_ATTEMPT_TIMEOUT_SECONDS)
    check("an exhausted budget still yields a positive timeout, never a hang",
          llm_engine._attempt_timeout_seconds(-5.0) > 0)
    check("it is a request deadline, not a sleep",
          "with_options(timeout=" in engine_source)

    class Attempt:
        """A local model that answers nothing and takes its full deadline doing it."""

        is_online = False
        funcs = ["general ", "open "]
        classify_intent = llm_engine.CentralizedLLMEngine.classify_intent
        _local_unhealthy = llm_engine.CentralizedLLMEngine._local_unhealthy
        _note_local_result = llm_engine.CentralizedLLMEngine._note_local_result

        def __init__(self, responses, clock):
            self.responses = list(responses)
            self.clock = clock
            self.calls = 0
            self.timeouts = []
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0

        def _dmm_local(self, prompt, timeout=None):
            self.calls += 1
            self.timeouts.append(timeout)
            # A hung backend consumes exactly its deadline and no more.
            self.clock.advance(int((timeout or 0) * 1000))
            return self.responses.pop(0) if self.responses else ""

    clock = FakeClock(0)
    original_monotonic = llm_engine.time.monotonic
    try:
        llm_engine.time.monotonic = lambda: clock.now / 1000.0

        # ── Success on retry 1 stops immediately ──
        clock.now = 0
        engine = Attempt(["", "open chrome"], clock)
        result = engine.classify_intent("open chrome")
        check("a valid response on retry 1 stops further retries",
              engine.calls == 2 and result == ["open chrome"],
              f"{engine.calls} calls -> {result}")
        check("and it costs two attempts, not five",
              clock.now / 1000.0
              <= 2 * llm_engine.DMM_ATTEMPT_TIMEOUT_SECONDS + 1.0,
              f"{clock.now / 1000.0:.1f}s")

        # ── Success on retry 2 stops immediately ──
        clock.now = 0
        engine = Attempt(["", "", "open chrome"], clock)
        result = engine.classify_intent("open chrome")
        check("a valid response on retry 2 stops there too",
              engine.calls == 3 and result == ["open chrome"],
              f"{engine.calls} calls -> {result}")

        # ── Total failure cannot exceed the ~15s window ──
        clock.now = 0
        engine = Attempt([], clock)
        result = engine.classify_intent("open chrome")
        spent = clock.now / 1000.0
        print_info(f"      worst case: {engine.calls} attempts in {spent:.1f}s")
        check("a total failure stays inside the retry window",
              spent <= llm_engine.DMM_RETRY_BUDGET_SECONDS + 1.0, f"{spent:.1f}s")
        check("and never exceeds about fifteen seconds", spent <= 16.0, f"{spent:.1f}s")
        check("it still degrades to conversation rather than failing the turn",
              result == ["general open chrome"], str(result))
        check("every attempt was given a bounded deadline",
              all(t and t <= llm_engine.DMM_ATTEMPT_TIMEOUT_SECONDS
                  for t in engine.timeouts), str(engine.timeouts))

        # ── A terminal failure is still not retried ──
        class Terminal(Attempt):
            def _dmm_local(self, prompt, timeout=None):
                self.calls += 1
                raise RuntimeError("Error code: 401 - invalid api key")

        clock.now = 0
        engine = Terminal([], clock)
        engine.classify_intent("open chrome")
        check("an auth failure is not retried five times", engine.calls == 1,
              f"{engine.calls} calls")
    finally:
        llm_engine.time.monotonic = original_monotonic


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               MAIN                                     │
# └────────────────────────────────────────────────────────────────────────┘

def main():
    print_banner("VOICE TURN")
    print_info(f"Host: {describe_host()}")
    print_info("No microphone, no browser, no provider is contacted.")

    with EnvironmentGuard() as guard:
        section_one_commit_point()
        section_endpoint_scenarios()
        section_decision_rules()
        section_page_agreement()
        section_control_vocabulary()
        section_confirmation()
        section_dmm_retries()
        section_no_bypass()
        section_short_answers()
        section_power_boundary()
        section_turn_ownership()
        section_local_health()
        section_lifecycle_announcements()
        section_attempt_budget()

    check.section("[9] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())

    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
