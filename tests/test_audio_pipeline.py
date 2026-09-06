# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_audio_pipeline.py                          │
# │            Barge-In / Echo-Rejection / Latency Diagnostics             │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_audio_pipeline.py — standalone diagnostic for the voice loop.

Like the other scripts in tests/, this is a manual entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_audio_pipeline.py

It exercises, with real audio hardware but without needing anyone to speak:

  1. TTS cold start and time-to-first-spoken-word.
  2. Barge-in: stop latency, queue flush, refusal of late sentences from an
     already-cancelled response, and re-arming on the next turn.
  3. The echo gate — that an utterance captured while the assistant was audible is
     classified as her own voice, while one captured after she stopped is not.
  4. Interrupt-phrase classification ("stop" is a barge-in, "stop the music" is a command).

Audio plays out loud during section 1-2; that is intentional, it is measuring the
real device path.
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules.utils import (print_banner, print_info, print_success, print_error,
                          print_system, now_ms, SentenceStreamer, speech_safe_text)
from modules.text_to_speech import TextToSpeechEngine
from modules.speech_to_text import is_interrupt_phrase

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def section_tts_latency(tts):
    print_system("\n[1] TTS latency")

    response = ("Absolutely, I can walk you through the entire deployment process step by step, "
                "starting with the build stage and then the rollout. It usually takes ten minutes.")

    tts.begin_turn()
    t0 = time.perf_counter()
    streamer = SentenceStreamer(tts.speak, stop_check=lambda: tts.interrupted)
    for word in response.split(" "):
        time.sleep(0.012)  # simulate an ~80 token/s LLM stream
        streamer.feed(word + " ")
        if tts.last_latency.get("first_audio_s"):
            break
    streamer.flush()

    while not tts.last_latency.get("first_audio_s") and time.perf_counter() - t0 < 30:
        time.sleep(0.005)

    first_audio = tts.last_latency.get("first_audio_s", 99)
    print_info(f"time to first spoken word: {first_audio:.2f}s")
    check("speech starts while the response is still generating", first_audio < 3.0,
          f"({first_audio:.2f}s)")
    return first_audio


def section_barge_in(tts):
    print_system("\n[2] Barge-in")

    # Queue up a multi-sentence response, then interrupt it mid-flight.
    tts.begin_turn()
    for sentence in ["Here is the first part of a long explanation.",
                     "Here is the second part that should be cut off.",
                     "And a third part nobody will ever hear.",
                     "Plus a fourth for good measure."]:
        tts.speak(sentence)

    deadline = time.perf_counter() + 10
    while not tts.is_playing and time.perf_counter() < deadline:
        time.sleep(0.01)
    check("playback started", tts.is_playing)

    time.sleep(1.0)
    t0 = time.perf_counter()
    tts.stop()
    stop_latency = (time.perf_counter() - t0) * 1000
    print_info(f"stop() -> silence: {stop_latency:.0f}ms")
    check("stop() returns fast enough to feel instant", stop_latency < 250,
          f"({stop_latency:.0f}ms)")

    time.sleep(0.2)
    check("nothing is playing or queued after stop", not tts.is_playing)
    check("interrupt flag latched", tts.interrupted)

    # A generator that hasn't noticed the interruption yet must not be able to
    # resurrect the cancelled response one sentence at a time.
    tts.speak("This late sentence belongs to the cancelled response.")
    time.sleep(0.5)
    check("late sentences from the cancelled turn are refused", not tts.is_playing)

    tts.begin_turn()
    check("begin_turn re-arms the engine", not tts.interrupted)
    tts.speak("Barge in test complete.", blocking=True)
    check("speech works again after an interruption", not tts.interrupted)
    return stop_latency


def section_echo_gate(tts):
    print_system("\n[3] Echo gate (self-listening)")

    # Speak, and capture the wall-clock window in which audio was really audible.
    tts.begin_turn()
    tts.speak("This sentence is playing through the speakers right now.")
    deadline = time.perf_counter() + 10
    while not tts.is_playing and time.perf_counter() < deadline:
        time.sleep(0.01)
    time.sleep(0.4)

    # An utterance the microphone captured DURING playback: this is the assistant's
    # own voice coming back through the room.
    echo_start = now_ms() - 300
    echo_end = now_ms()
    check("utterance captured during playback is classified as echo",
          tts.was_audible_between(echo_start, echo_end))

    tts.wait_until_idle(timeout=20)
    time.sleep(0.5)

    # An utterance that starts well after she went quiet is the user.
    later_start = now_ms()
    check("utterance captured after playback ended is NOT echo",
          not tts.was_audible_between(later_start, later_start + 400))

    # After an explicit barge-in the user owns the floor immediately — their next
    # command must not be swallowed by the echo tail margin.
    tts.begin_turn()
    tts.speak("A long sentence that the user is about to interrupt mid-way through.")
    while not tts.is_playing and time.perf_counter() < deadline + 10:
        time.sleep(0.01)
    time.sleep(0.5)
    tts.stop()
    post_stop = now_ms() + 50
    check("command spoken right after a barge-in is NOT treated as echo",
          not tts.was_audible_between(post_stop, post_stop + 500))


def section_interrupt_vocabulary():
    print_system("\n[4] Interrupt phrase classification")

    for phrase in ["Stop.", "stop", "Wait.", "hold on", "Shut up.", "Kayra stop"]:
        check(f"'{phrase}' is an interruption", is_interrupt_phrase(phrase))

    for phrase in ["Stop the music.", "Open chrome.", "What is the weather today?",
                   "Wait for the build to finish and then tell me."]:
        check(f"'{phrase}' is a normal command", not is_interrupt_phrase(phrase))


def section_speech_normalization():
    """
    The display/speech split: the console keeps the model's formatting, the TTS engine gets a
    version that sounds right. These cases are the ones that previously broke — the old
    inline cleaner deleted every unrecognised symbol, so units and non-Latin text were lost.
    """
    print_system("\n[5] Speech-safe normalization")

    cases = [
        # (raw model output, expected spoken form)
        ("The answer is **42**.", "The answer is 42."),
        ("\U0001F680 Great news! The update is live.", "Great news! The update is live."),
        ("## Summary\n- First point\n- Second point", "Summary\nFirst point\nSecond point"),
        ("Prices rose 15% to $1,200.", "Prices rose 15 percent to 1,200 dollars."),
        ("It was 30\u00b0C outside.", "It was 30 degrees Celsius outside."),
        ("Rust\u2019s compiler can\u2019t be fooled.", "Rust's compiler can't be fooled."),
        ("See [the docs](https://example.com) for more.", "See the docs for more."),
        ("Wow!!! Really??? Yes...", "Wow! Really? Yes."),
        ("Kayra ne kaha: \u092f\u0939 \u0920\u0940\u0915 \u0939\u0948\u0964", "Kayra ne kaha: \u092f\u0939 \u0920\u0940\u0915 \u0939\u0948."),
        ("", ""),
    ]
    for raw, expected in cases:
        got = speech_safe_text(raw)
        check(f"speech form of {raw[:34]!r}", got == expected,
              f"got {got!r}" if got != expected else "")

    # Meaning must survive: units, numbers and non-Latin scripts are pronounced, not deleted.
    check("percent sign is spoken, not dropped", "percent" in speech_safe_text("up 20%"))
    check("currency is spoken, not dropped", "dollars" in speech_safe_text("costs $30"))
    check("code fences do not reach the speaker",
          "```" not in speech_safe_text("run\n```py\nx=1\n```\nnow"))
    check("devanagari survives normalization",
          "\u0920\u0940\u0915" in speech_safe_text("\u0920\u0940\u0915 \u0939\u0948"))


if __name__ == "__main__":
    print_banner("KAYRA AUDIO PIPELINE DIAGNOSTIC", "Barge-in, echo rejection & speech latency")

    t_boot = time.perf_counter()
    tts = TextToSpeechEngine()
    print_info(f"TTS engine constructed in {time.perf_counter() - t_boot:.2f}s")
    time.sleep(2.5)  # let the background ONNX warm-up finish

    try:
        section_tts_latency(tts)
        section_barge_in(tts)
        section_echo_gate(tts)
        section_interrupt_vocabulary()
        section_speech_normalization()
    finally:
        tts.shutdown()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All audio pipeline checks passed.")
