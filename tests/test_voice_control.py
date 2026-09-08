# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_voice_control.py                           │
# │       Local Control Vocabulary, Lifecycle and Shutdown Diagnostics     │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_voice_control.py — standalone diagnostic for the local control layer.

Like the other scripts in tests/, this is a manual entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_voice_control.py

Hardware-free: no microphone, no browser, no model, no network. It exercises

  1. Interrupt classification — "stop"/"wait"/"hold" ARE barge-ins, and the commands that
     merely begin with those words are NOT.
  2. The lifecycle vocabulary — pause/resume listening, sleep, wake, shutdown.
  3. The boundary that matters most: shutting KAYRA down versus shutting the COMPUTER down.
  4. Tail matching — the fix for the actual stop/wait/hold failure — including proof that it
     is scoped to the speaking case and cannot leak into normal commands.
  5. The JS/Python agreement: the page's `looksLikeInterrupt` and this module's classifier are
     two implementations of one vocabulary and must not drift.
  6. Shutdown idempotency and ordering, against a fully stubbed backend — no process is
     harmed, and `os._exit` is replaced for the duration.
  7. Cost: classification has to be cheap enough to run on every utterance.
"""

import os
import re
import sys
import time
import types

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core import voice_control as vc
from kayra.core.voice_control import ControlKind, classify_control, is_interrupt_phrase

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def kind_of(text):
    command = classify_control(text)
    return command.kind if command is not None else None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      1. INTERRUPT CLASSIFICATION                       │
# └────────────────────────────────────────────────────────────────────────┘

def section_interrupts():
    print_system("\n[1] Interrupt vocabulary")

    # The three the product requires by name, plus the variations a person actually says.
    for phrase in ["stop", "Stop.", "wait", "Wait!", "hold", "Hold.",
                   "hold on", "hold up", "please stop", "please wait", "stop talking",
                   "stop speaking", "Kayra stop", "Kayra, please stop", "shut up",
                   "be quiet", "enough", "stop stop stop", "wait a second"]:
        check(f"'{phrase}' is an interruption", is_interrupt_phrase(phrase) is True)

    # THE PREFIX BOUNDARY. Every one of these begins with an interrupt word and is a real
    # command; a `startswith` matcher would swallow all of them.
    for phrase in ["stop the music", "stop the timer", "wait for me",
                   "wait for the build to finish and then tell me",
                   "hold the window", "hold the door open", "stop proactive suggestions",
                   "close this tab", "open chrome", "what is the weather today?",
                   "pause the music", "cancel the timer"]:
        check(f"'{phrase}' is NOT an interruption", is_interrupt_phrase(phrase) is False)

    check("is_interrupt_phrase returns a real bool, not a truthy object",
          is_interrupt_phrase("stop") is True and is_interrupt_phrase("open chrome") is False)
    check("non-string input is survived", is_interrupt_phrase(None) is False
          and is_interrupt_phrase(12) is False and is_interrupt_phrase("") is False)

    # Devanagari must survive normalization. A `\w` allow-list drops combining marks
    # (category Mn) and turns "रुको" into "र क", which is how the Hindi vocabulary silently
    # stopped matching once before.
    for phrase in ["रुको", "चुप", "स्टॉप"]:
        check(f"Hindi '{phrase}' is an interruption", is_interrupt_phrase(phrase) is True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       2. LIFECYCLE VOCABULARY                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_lifecycle():
    print_system("\n[2] Lifecycle commands")

    expectations = [
        # ── listening ──
        ("stop listening", ControlKind.PAUSE_LISTENING),
        ("stop listening to me", ControlKind.PAUSE_LISTENING),
        ("Kayra, stop listening", ControlKind.PAUSE_LISTENING),
        ("pause listening", ControlKind.PAUSE_LISTENING),
        ("pause the microphone", ControlKind.PAUSE_LISTENING),
        ("mute the mic", ControlKind.PAUSE_LISTENING),
        ("start listening", ControlKind.RESUME_LISTENING),
        ("resume listening", ControlKind.RESUME_LISTENING),
        ("listen again", ControlKind.RESUME_LISTENING),
        # ── standby ──
        ("go to sleep", ControlKind.SLEEP),
        ("Kayra sleep", ControlKind.SLEEP),
        ("sleep Kayra", ControlKind.SLEEP),
        ("Kayra, go to sleep", ControlKind.SLEEP),
        ("stand by", ControlKind.SLEEP),
        ("wake up", ControlKind.WAKE),
        ("Wake up, Kayra!", ControlKind.WAKE),
        ("wake Kayra", ControlKind.WAKE),
        ("come back", ControlKind.WAKE),
        # ── shutdown ──
        ("exit", ControlKind.SHUTDOWN),
        ("Exit.", ControlKind.SHUTDOWN),
        ("quit", ControlKind.SHUTDOWN),
        ("shutdown Kayra", ControlKind.SHUTDOWN),
        ("shut down Kayra", ControlKind.SHUTDOWN),
        ("turn off Kayra", ControlKind.SHUTDOWN),
        ("close Kayra", ControlKind.SHUTDOWN),
        ("turn off your engine", ControlKind.SHUTDOWN),
        ("turn off the engine", ControlKind.SHUTDOWN),
        ("shut yourself down", ControlKind.SHUTDOWN),
        ("power down Kayra", ControlKind.SHUTDOWN),
    ]
    for phrase, expected in expectations:
        actual = kind_of(phrase)
        check(f"'{phrase}' -> {expected}", actual == expected, f"(got {actual})")

    # Every kind is reachable from at least one phrase — a kind with no way to trigger it is a
    # feature that does not exist.
    for kind in vc.control_kinds():
        check(f"{kind} has at least one phrase", len(vc.phrases_for(kind)) > 0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │            3. KAYRA SHUTDOWN vs WINDOWS SHUTDOWN (the boundary)        │
# └────────────────────────────────────────────────────────────────────────┘

def section_shutdown_boundary():
    print_system("\n[3] Shutting Kayra down vs shutting the computer down")

    # Anything naming the MACHINE must fall through to the DMM, where 'system shutdown' is a
    # CONFIRM-gated automation action. Not one of these may be a local control command.
    machine = [
        "shut down my computer", "shutdown my computer", "turn off my pc",
        "turn off my computer", "restart my computer", "reboot the machine",
        "shut down the pc", "put my computer to sleep", "sleep my computer",
        "power off the laptop", "log out of windows", "lock my pc",
    ]
    for phrase in machine:
        check(f"'{phrase}' is NOT a Kayra control command", kind_of(phrase) is None)

    # And the assistant-directed ones must not be mistaken for the machine.
    for phrase in ["turn off Kayra", "shut down Kayra", "exit", "quit"]:
        check(f"'{phrase}' IS a Kayra shutdown", kind_of(phrase) == ControlKind.SHUTDOWN)

    # The two sets are disjoint by construction, not by luck.
    shutdown_phrases = set(vc.phrases_for(ControlKind.SHUTDOWN))
    for phrase in shutdown_phrases:
        check(f"shutdown phrase '{phrase}' does not name the machine",
              not re.search(r"\b(computer|pc|laptop|machine|windows|desktop|system)\b", phrase))

    # A bare "sleep" is ambiguous between standby and the machine's sleep, so it is
    # deliberately NOT in the local vocabulary — it belongs to the DMM's 'system sleep'.
    check("a bare 'sleep' is left to the DMM", kind_of("sleep") is None)

    # Documented and deliberate: "stop kayra" reduces to "stop" (the name is a filler) and is
    # read as the recoverable action. This pins that decision so it cannot change silently.
    check("'stop Kayra' is an interrupt, not a shutdown",
          kind_of("stop Kayra") == ControlKind.INTERRUPT)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        4. TAIL MATCHING                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_tail_matching():
    print_system("\n[4] Tail matching — the actual stop/wait/hold fix")

    # THE FAILURE THIS FIXES. During playback the recognizer's buffer already holds echo of
    # Kayra's own voice, so when the user says "stop" the interim probe is the whole sentence
    # with "stop" glued to the end. No whole-utterance test can match that, which is exactly
    # why "stop" worked in a quiet room and not over a long answer.
    polluted = [
        "and then the rollout usually takes about ten minutes stop",
        "i can walk you through the deployment process step by step wait",
        "starting with the build stage and then the rollout hold",
        "here is the first part of a long explanation stop stop",
    ]
    for probe in polluted:
        check(f"tail match finds the interrupt in '...{probe[-24:]}'",
              vc.interrupt_in_tail(probe) is True)
        check(f"whole-utterance match correctly does NOT: '...{probe[-24:]}'",
              is_interrupt_phrase(probe) is False)

    # And it must not fire on a command that merely ENDS with an interrupt-shaped word in a
    # different sense. This is why the tail branch is gated on `kayraSpeaking` in the page and
    # never consulted for a finalized transcript.
    check("tail matching is a separate function from the classifier",
          vc.interrupt_in_tail is not is_interrupt_phrase)
    check("an empty probe is not an interrupt", vc.interrupt_in_tail("") is False)
    check("a probe of pure filler is not an interrupt",
          vc.interrupt_in_tail("hey kayra please") is False)
    check("the suffix scan is bounded by the longest phrase",
          vc.MAX_INTERRUPT_WORDS <= 5, f"({vc.MAX_INTERRUPT_WORDS} words)")


# ┌────────────────────────────────────────────────────────────────────────┐
# │              5. THE JS PAGE AND PYTHON MUST NOT DRIFT                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_page_agreement():
    print_system("\n[5] STT page / Python vocabulary agreement")

    # Importing speech_to_text does NOT start a browser — no import-time side effects — so the
    # page source can be inspected as text here, with no hardware.
    from kayra.input import speech_to_text as stt

    check("the page injects the shared interrupt list",
          "window.kayraInterruptWords = interruptWords" in stt.html_code)
    check("the page injects the shared control table",
          "window.kayraControlPhrases = controlPhrases" in stt.html_code)
    check("the page has a speaking flag", "window.kayraSpeaking" in stt.html_code)
    check("the page tail-matches ONLY while speaking",
          "if (!window.kayraSpeaking) return false;" in stt.html_code)
    check("lifecycle commands are never tail-matched",
          "function looksLikeControl" in stt.html_code
          and "kayraSpeaking" not in stt.html_code.split("function looksLikeControl")[1]
              .split("function ")[0])

    # The clear-queue bug: clearing the queue without clearing the accumulated buffer only
    # delayed the polluted transcript by one VAD window.
    check("the page can reset the accumulated utterance",
          "function resetUtteranceBuffer" in stt.html_code)
    def js_of(method):
        """The JavaScript a method sends, gathered from its constants."""
        return " ".join(c for c in method.__code__.co_consts if isinstance(c, str))

    clear_js = js_of(stt.SpeechToTextEngine.clear_queue)
    check("clear_queue resets the accumulated buffer", "resetUtteranceBuffer" in clear_js)
    check("clear_queue drops the queue", "speechQueue" in clear_js)
    check("clear_queue drops both latches",
          "kayraInterrupt" in clear_js and "kayraControl" in clear_js)

    # One vocabulary, re-exported rather than re-implemented.
    check("speech_to_text re-exports the shared vocabulary",
          stt.is_interrupt_phrase is vc.is_interrupt_phrase)
    check("INTERRUPT_PHRASES is the same object", stt.INTERRUPT_PHRASES is vc.INTERRUPT_PHRASES)
    check("the control table covers every lifecycle kind",
          {kind for _, kind in stt._CONTROL_PHRASE_TABLE} ==
          {ControlKind.PAUSE_LISTENING, ControlKind.RESUME_LISTENING,
           ControlKind.SLEEP, ControlKind.WAKE, ControlKind.SHUTDOWN})
    check("no interrupt phrase leaked into the control table",
          not ({p for p, _ in stt._CONTROL_PHRASE_TABLE} & set(vc.INTERRUPT_PHRASES)))

    # The engine has to be able to publish the flag and read both results in one round-trip.
    check("the engine exposes poll_controls", hasattr(stt.SpeechToTextEngine, "poll_controls"))
    poll_js = js_of(stt.SpeechToTextEngine.poll_controls)
    check("poll_controls sets the speaking flag and clears both latches",
          all(token in poll_js
              for token in ("kayraSpeaking", "kayraInterrupt", "kayraControl")))
    check("pause/resume listening still exist and are unchanged in name",
          hasattr(stt.SpeechToTextEngine, "pause_listening")
          and hasattr(stt.SpeechToTextEngine, "resume_listening"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                6. SHUTDOWN: ONE PATH, IDEMPOTENT, ORDERED              │
# └────────────────────────────────────────────────────────────────────────┘

class _StubTTS:
    def __init__(self, log):
        self.log = log
        self.is_playing = False
        self.interrupted = False

    def stop(self):
        self.log.append("tts.stop")
        self.interrupted = True

    def speak(self, text, blocking=False):
        self.log.append(f"tts.speak({text!r},{blocking})")

    def begin_background_utterance(self):
        pass

    def begin_turn(self):
        pass

    def shutdown(self):
        self.log.append("tts.shutdown")


class _StubSTT:
    def __init__(self, log):
        self.log = log
        self.owned_pids = {4242}
        self.paused = False

    def shutdown(self):
        self.log.append("stt.shutdown")

    def terminate_owned_processes(self, timeout=3.0):
        self.log.append("stt.terminate_owned")
        return set()

    def clear_queue(self):
        self.log.append("stt.clear_queue")

    def pause_listening(self):
        self.paused = True
        self.log.append("stt.pause")
        return True

    def resume_listening(self):
        self.paused = False
        self.log.append("stt.resume")
        return True


class _StubProactive:
    def __init__(self, log):
        self.log = log
        self.enabled = True

    def set_enabled(self, value):
        self.enabled = bool(value)
        self.log.append(f"proactive.set_enabled({self.enabled})")

    def stop(self, timeout=2.0):
        self.log.append("proactive.stop")


def _stub_app(log):
    """
    Wires `kayra.app`'s module globals to stubs so shutdown can be exercised for real.

    Nothing here starts a subsystem: importing `kayra.app` has no side effects by design, so
    the module can be inspected and driven with its globals replaced. `os._exit` is swapped
    for a sentinel raise so the sequence runs to completion without ending this process.
    """
    from kayra import app

    app.tts_engine = _StubTTS(log)
    app.stt_engine = _StubSTT(log)
    app.proactive_agent = _StubProactive(log)
    app.TTS_ENABLED = True
    app.AUDIO_ENABLED = True
    app.shutdown_automation = lambda: log.append("automation.shutdown") or 0
    app._shutdown_started.clear()
    app._pre_exit_hooks.clear()
    app.RUNTIME.shutdown_event.clear()
    return app


class _Exited(Exception):
    pass


def section_shutdown():
    print_system("\n[6] Shutdown — one path, idempotent, ordered")

    log = []
    app = _stub_app(log)

    real_exit = os._exit
    os._exit = lambda code=0: (_ for _ in ()).throw(_Exited(code))
    try:
        check("request_shutdown is the authoritative entry point",
              callable(getattr(app, "request_shutdown", None)))
        check("_force_shutdown still exists for signal handlers and old callers",
              callable(getattr(app, "_force_shutdown", None)))

        app.on_before_exit(lambda: log.append("ui.hook"))

        try:
            app.request_shutdown(reason="test", farewell=True)
            check("shutdown reaches os._exit", False, "(it returned instead)")
        except _Exited:
            check("shutdown reaches os._exit", True)

        # ORDER. Each of these is here for a documented reason; a reordering is a regression.
        def before(a, b):
            return a in log and b in log and log.index(a) < log.index(b)

        check("the farewell is spoken BEFORE the audio device is disposed",
              before("tts.speak('Shutting down. Goodbye.',True)", "tts.shutdown"))
        check("the proactive agent stops BEFORE the engine it speaks through",
              before("proactive.stop", "tts.shutdown"))
        check("timers are cancelled before teardown",
              before("automation.shutdown", "tts.shutdown"))
        check("speech is silenced before the browser is torn down",
              before("tts.shutdown", "stt.shutdown"))
        check("owned processes are verified after the session is closed",
              before("stt.shutdown", "stt.terminate_owned"))
        check("the UI hook runs LAST, after every resource is released",
              before("stt.terminate_owned", "ui.hook"))

        check("the runtime is told first", app.RUNTIME.shutdown_event.is_set())
        check("the state becomes SHUTTING_DOWN", app.RUNTIME.state == "SHUTTING_DOWN")
        check("shutdown_requested() reports it", app.shutdown_requested() is True)

        # IDEMPOTENCY. A second caller must not run the sequence again.
        before_count = len(log)
        app.request_shutdown(reason="second call")
        check("a second request_shutdown does not re-run the teardown",
              len(log) == before_count, f"({len(log) - before_count} extra step(s))")

        # And the signal handler is the same path, not a second one.
        source = app._force_shutdown.__code__.co_names
        check("_force_shutdown delegates rather than duplicating",
              "request_shutdown" in source)
    finally:
        os._exit = real_exit
        app._shutdown_started.clear()
        app.RUNTIME.shutdown_event.clear()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    7. DISPATCH, SLEEP AND LISTENING                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_dispatch():
    print_system("\n[7] Control dispatch — listening, standby, wake")

    log = []
    app = _stub_app(log)
    app.RUNTIME.set_listening(True)
    app.RUNTIME.set_sleeping(False)

    # ── stop listening ──
    app._dispatch_control(ControlKind.PAUSE_LISTENING, "stop listening")
    check("'stop listening' closes the microphone", app.RUNTIME.listening is False)
    check("'stop listening' also silences current speech", "tts.stop" in log)
    check("'stop listening' does NOT shut Kayra down",
          not app.RUNTIME.shutdown_event.is_set())
    check("'stop listening' does NOT put Kayra to sleep", app.RUNTIME.sleeping is False)
    check("the STT session is PAUSED, not torn down",
          "stt.pause" in log and "stt.shutdown" not in log)

    app._dispatch_control(ControlKind.RESUME_LISTENING, "start listening")
    check("'start listening' reopens the microphone", app.RUNTIME.listening is True)
    check("resuming reuses the same session", "stt.resume" in log and
          log.count("stt.shutdown") == 0)

    # ── sleep / wake ──
    app.proactive_agent.set_enabled(True)
    app._dispatch_control(ControlKind.SLEEP, "go to sleep")
    check("'go to sleep' enters standby", app.RUNTIME.sleeping is True)
    check("standby switches the proactive service off",
          app.proactive_agent.enabled is False)
    check("standby does NOT close the microphone — a closed mic cannot hear 'wake up'",
          app.RUNTIME.listening is True)
    check("standby does NOT end the process", not app.RUNTIME.shutdown_event.is_set())

    app._dispatch_control(ControlKind.WAKE, "wake up")
    check("'wake up' leaves standby", app.RUNTIME.sleeping is False)
    check("waking RESTORES the proactive setting rather than forcing it on",
          app.proactive_agent.enabled is True)

    # Waking from a state where proactive was off must not switch it on.
    app.proactive_agent.set_enabled(False)
    app._dispatch_control(ControlKind.SLEEP, "go to sleep")
    app._dispatch_control(ControlKind.WAKE, "wake up")
    check("waking does not enable a proactive service the user had switched off",
          app.proactive_agent.enabled is False)

    # ── the three "stop"-shaped concepts stay separate ──
    log.clear()
    app.RUNTIME.set_listening(True)
    app._dispatch_control(ControlKind.INTERRUPT, "stop")
    check("a barge-in silences speech", "tts.stop" in log)
    check("a barge-in does NOT close the microphone", app.RUNTIME.listening is True)
    check("a barge-in does NOT sleep", app.RUNTIME.sleeping is False)
    check("a barge-in does NOT shut down", not app.RUNTIME.shutdown_event.is_set())
    check("a barge-in drops the recognizer buffer", "stt.clear_queue" in log)

    # Reset for anything that runs afterwards.
    app.RUNTIME.set_sleeping(False)
    app.RUNTIME.set_listening(True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          8. RUNTIME STATE                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_runtime():
    print_system("\n[8] Runtime state — three independent axes")

    from kayra.core.runtime_state import RuntimeState, AssistantState

    runtime = RuntimeState()
    check("listening and sleeping start in the expected state",
          runtime.listening is True and runtime.sleeping is False)

    events = []
    runtime.subscribe(lambda name, payload: events.append((name, payload)))

    check("set_sleeping reports a change", runtime.set_sleeping(True) is True)
    check("set_sleeping is a no-op when unchanged", runtime.set_sleeping(True) is False)
    check("sleeping_changed is announced",
          any(name == "sleeping_changed" and payload.get("sleeping") is True
              for name, payload in events))

    # Independence is the property that matters: the three concepts must never share a flag.
    runtime.set_state(AssistantState.SPEAKING)
    check("Kayra can be asleep in any state", runtime.sleeping is True
          and runtime.state == AssistantState.SPEAKING)
    runtime.set_listening(False)
    check("listening and sleeping are independent",
          runtime.listening is False and runtime.sleeping is True)
    runtime.set_sleeping(False)
    check("waking does not reopen a microphone the user closed",
          runtime.listening is False and runtime.sleeping is False)

    snapshot = runtime.snapshot()
    check("snapshot carries all three axes",
          {"state", "listening", "sleeping"} <= set(snapshot))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             9. COST                                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_performance():
    print_system("\n[9] Cost — this runs on every utterance")

    samples = ["stop", "stop the music", "turn off kayra", "go to sleep",
               "what is the weather in tokyo today and will it rain tomorrow"]
    iterations = 20000
    t0 = time.perf_counter()
    for _ in range(iterations // len(samples)):
        for sample in samples:
            classify_control(sample)
    per_call = (time.perf_counter() - t0) / iterations * 1e6
    print_info(f"classify_control: {per_call:.1f}us per call")
    check("classification is microseconds, not milliseconds", per_call < 100.0,
          f"({per_call:.1f}us)")

    # A dict probe, not a scan: the cost must not grow with the length of the utterance.
    long_text = "please " * 400 + "stop the music"
    t0 = time.perf_counter()
    for _ in range(2000):
        classify_control(long_text)
    long_per_call = (time.perf_counter() - t0) / 2000 * 1e6
    print_info(f"classify_control on a 400-word utterance: {long_per_call:.1f}us")
    check("a long utterance short-circuits rather than scanning the table",
          long_per_call < 2000.0, f"({long_per_call:.1f}us)")

    check("no LLM client is reachable from the control layer",
          not any(name in dir(vc) for name in ("cohere", "requests", "CentralizedLLMEngine")))
    check("the control module imports nothing heavy",
          set(n for n in dir(vc) if n in ("torch", "numpy", "selenium", "onnxruntime")) == set())


def main():
    print_banner("LOCAL VOICE CONTROL DIAGNOSTIC", "Interrupt / listening / standby / shutdown")
    section_interrupts()
    section_lifecycle()
    section_shutdown_boundary()
    section_tail_matching()
    section_page_agreement()
    section_shutdown()
    section_dispatch()
    section_runtime()
    section_performance()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All voice control checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
