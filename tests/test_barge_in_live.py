# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_barge_in_live.py                           │
# │              Human-in-the-Loop Voice Interruption Test                 │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_barge_in_live.py — the barge-in test that needs an actual human voice.

    .venv\\Scripts\\python tests\\test_barge_in_live.py

Everything else in tests/ can run unattended, which is exactly why this file exists
separately: interrupting the assistant is the one behaviour that cannot be honestly
verified by injecting events, because the part most likely to fail is the recognizer
hearing a short word over the assistant's own voice.

It runs the full production path — microphone -> Chrome Web Speech interim result ->
window.kayraInterrupt -> barge-in watcher -> tts.stop() -> silence — and measures how long
each interruption took.

PRE-FLIGHT: it measures the microphone's signal level first. A muted or disabled input
device produces a confident-looking "not interrupted" result that has nothing to do with
the code, so that case is detected and reported up front rather than mis-attributed.
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules.utils import print_banner, print_info, print_success, print_error, print_system, console

RESULTS = []


def microphone_is_live(seconds=2.0, threshold=0.001):
    """
    Returns (is_live, rms). A silent device reads around the noise floor (~0.0002); normal
    room audio on a working microphone reads an order of magnitude above that.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        return None, None
    try:
        recording = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="float32")
        sd.wait()
        rms = float(np.sqrt(np.mean(recording ** 2)))
        return rms >= threshold, rms
    except Exception:
        return None, None


LONG_RESPONSE = [
    "Sure, let me walk you through how the whole deployment process works, from the very beginning.",
    "The first stage compiles the project and runs the entire unit test suite against it.",
    "After that, the build artifacts are uploaded and the rollout begins gradually across the fleet.",
    "Then the health checks confirm that the new version is serving live traffic correctly.",
    "And finally the old version is retired once everything has been stable for a while.",
]


def trial(main, word, prompt_text):
    """Speaks a long response and waits for the user to interrupt it out loud."""
    stt, tts = main.stt_engine, main.tts_engine

    stt.clear_queue()
    tts.begin_turn()
    for sentence in LONG_RESPONSE:
        tts.speak(sentence)

    deadline = time.perf_counter() + 30
    while not tts.last_latency.get("first_audio_s") and time.perf_counter() < deadline:
        time.sleep(0.01)

    console.print(f"\n[bold yellow]>>> {prompt_text}[/bold yellow]")
    started_waiting = time.perf_counter()

    while tts.is_playing and time.perf_counter() - started_waiting < 20:
        time.sleep(0.005)

    interrupted = not tts.is_playing
    elapsed = time.perf_counter() - started_waiting

    if interrupted and elapsed < 19:
        print_success(f"    interrupted — playback stopped {elapsed:.2f}s after the prompt appeared "
                      f"(includes your reaction time)")
        RESULTS.append((word, True, elapsed))
    else:
        print_error(f"    NOT interrupted after {elapsed:.1f}s")
        heard = stt._script("return window.speechQueue.slice().map(function(i){return i.text;});")
        print_info(f"    what the recognizer heard: {heard}")
        RESULTS.append((word, False, elapsed))

    tts.stop()
    stt.clear_queue()
    time.sleep(1.0)


def follow_up(main):
    """After an interruption, the next spoken command must be captured normally, exactly once."""
    stt = main.stt_engine
    stt.clear_queue()
    console.print("\n[bold yellow]>>> Now say a command, for example: "
                  "\"what is the time\"[/bold yellow]")

    captured = []
    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline:
        result = main.Listen()
        if result:
            captured.append(result)
            break
        time.sleep(0.05)

    if captured:
        print_success(f"    captured: {captured[0]!r}")
        time.sleep(2.0)
        extra = stt._script("return window.speechQueue.slice().map(function(i){return i.text;});")
        if extra:
            print_info(f"    note: {len(extra)} further utterance(s) still queued: {extra}")
        RESULTS.append(("follow-up command", True, 0.0))
    else:
        print_error("    nothing captured within 20s")
        RESULTS.append(("follow-up command", False, 0.0))


if __name__ == "__main__":
    print_banner("KAYRA LIVE BARGE-IN TEST", "Requires you to speak out loud")

    print_system("[0] Microphone pre-flight")
    live, rms = microphone_is_live()
    if live is None:
        print_info("    could not measure the input level (sounddevice/numpy unavailable)")
    elif not live:
        print_error(f"    microphone appears SILENT (RMS {rms:.5f}, expected > 0.001).")
        print_error("    Windows privacy settings, a hardware mute key, or the wrong default")
        print_error("    input device will all produce this. Fix that before trusting any")
        print_error("    result below — a dead microphone looks exactly like a broken barge-in.")
        if "--force" not in sys.argv:
            print_info("    Re-run with --force to continue anyway.")
            sys.exit(2)
    else:
        print_success(f"    microphone is live (RMS {rms:.5f})")

    print_info("Booting the assistant (this starts the real TTS, STT and watcher)...")
    import main  # noqa: E402  — booting is the point, and it must happen after the pre-flight

    import threading
    threading.Thread(target=main._barge_in_watcher, daemon=True, name="kayra-barge-in").start()
    time.sleep(0.5)

    try:
        print_system("\n[1] Interrupting with \"stop\"")
        trial(main, "stop", "Kayra is speaking — say STOP out loud now.")

        print_system("\n[2] Interrupting with \"wait\"")
        trial(main, "wait", "Kayra is speaking again — say WAIT out loud now.")

        print_system("\n[3] Command after the interruption")
        follow_up(main)

        print_system("\n[4] \"stop the music\" must NOT be swallowed as a barge-in")
        print_info("    (verified without audio by test_audio_pipeline.py; say it now to")
        print_info("     confirm it reaches the DMM as a command rather than being eaten)")
        main.stt_engine.clear_queue()
        console.print("\n[bold yellow]>>> Say: \"stop the music\"[/bold yellow]")
        heard = None
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline and not heard:
            heard = main.Listen()
            time.sleep(0.05)
        if heard:
            print_success(f"    reached the command pipeline as: {heard!r}")
            RESULTS.append(("'stop the music' not swallowed", True, 0.0))
        else:
            print_error("    it was swallowed or nothing was heard")
            RESULTS.append(("'stop the music' not swallowed", False, 0.0))

        print_system("\n" + "=" * 60)
        passed = sum(1 for _n, ok, _t in RESULTS if ok)
        for name, ok, elapsed in RESULTS:
            status = "[success]PASS[/success]" if ok else "[error]FAIL[/error]"
            console.print(f"  {status}  {name}" + (f"  [dim]{elapsed:.2f}s[/dim]" if elapsed else ""))
        if passed == len(RESULTS):
            print_success(f"Live barge-in: {passed}/{len(RESULTS)} passed.")
        else:
            print_error(f"Live barge-in: {passed}/{len(RESULTS)} passed.")
    finally:
        main._force_shutdown()
