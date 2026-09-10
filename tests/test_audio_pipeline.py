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

# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import (print_banner, print_info, print_success, print_error,
                          print_system, now_ms, SentenceStreamer, speech_safe_text)
from kayra.output.text_to_speech import (TextToSpeechEngine, SAMPLE_RATE,
                                        WRITE_SLICE_MS, MAX_PRIME_MS)
from kayra.input.speech_to_text import is_interrupt_phrase, interrupt_in_tail

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


def section_first_word_not_clipped(tts):
    """
    The first word must not be clipped, and nothing may be added or lost to achieve it.

    THE BUG. Kokoro is asked for `trim=True`, which strips the leading silence — the very
    first sample handed to the device is already a phoneme (measured peak 0.09-0.28 in the
    first 50ms, against 0.0000 untrimmed). The persistent output stream has meanwhile been
    running and idle since boot, so its ring is empty when speech arrives: 2560 frames of
    room against a 93ms reported latency. Writing 40ms at a time took THREE writes to fill
    it while the callback was already consuming, so the callback assembled its first block
    from a partially-filled ring — and with no lead-in silence, what got mangled was the
    first word.
    """
    print_system("\n[1b] First word is not clipped")

    if tts._stream is None:
        print_info("no persistent output stream on this host — priming does not apply")
        return

    # Start from silence, BEFORE the stream is captured. `section_tts_latency` may still be
    # draining, and a spy installed mid-burst would record writes for an utterance that was
    # primed before it. `stop()` can also REPLACE the stream object, so capturing first would
    # leave the spy on one stream and the original `write` on another.
    tts.stop()
    tts.wait_until_idle(timeout=10)
    time.sleep(0.3)

    writes = []
    real_write = tts._stream.write

    def spy(data):
        room = tts._stream.write_available
        underflowed = real_write(data)
        writes.append((len(data), room, bool(underflowed)))

    tts._stream.write = spy
    try:
        tts.begin_turn()
        tts.speak("Certainly sir, the deployment finished about ten minutes ago.")
        tts.wait_until_idle(timeout=30)
        first_run = list(writes)

        writes.clear()
        tts.begin_turn()
        tts.speak("Yes, the file has been saved.")
        tts.wait_until_idle(timeout=30)
        second_run = list(writes)
    finally:
        tts._stream.write = real_write

    check("audio reached the device", bool(first_run) and bool(second_run))
    if not (first_run and second_run):
        return

    slice_frames = int(SAMPLE_RATE * WRITE_SLICE_MS / 1000)

    # ── 1. THE FIRST WRITE FILLS THE RING ──
    # The check that actually pins the fix: the device must be full after ONE write, not
    # after three. `write_available` before the second write is what says so.
    first_len, first_room, _ = first_run[0]
    check("the first write covers at least one device period",
          first_len >= min(first_room, tts._prime_frames),
          f"wrote {first_len} frames into {first_room} of room")
    if len(first_run) > 1:
        _len2, room_after_first, _u = first_run[1]
        check("the ring is full after the FIRST write, not after three",
              room_after_first < slice_frames,
              f"{room_after_first} frames still free — the callback can assemble a "
              f"partial block, and with trim=True that eats the first word")
    check("no write underflowed", not any(u for _l, _r, u in first_run))

    # ── 2. PRIMING ONLY EVER HAPPENS INTO A DRAINED RING ──
    # A long sentence arrives as several bursts — the audio queue empties between them and
    # the ring genuinely drains — so more than one write can be oversized. What must never
    # happen is an oversized write into a ring that is already full: that would block for
    # longer than a slice and coarsen barge-in for no benefit.
    cap = int(SAMPLE_RATE * MAX_PRIME_MS / 1000)
    oversized = [(length, room) for length, room, _u in first_run + second_run
                 if length > slice_frames]
    check("every oversized write went into a ring with room for it",
          all(room >= length for length, room in oversized),
          f"{[(l, r) for l, r in oversized if r < l][:3]}")
    check("no priming write exceeds the cap",
          all(length <= cap for length, _room in oversized),
          f"max {max((l for l, _r in oversized), default=0)} against a {cap}-frame cap")
    check("most writes are still ordinary slices",
          len(oversized) * 4 <= len(first_run) + len(second_run),
          f"{len(oversized)} oversized of {len(first_run) + len(second_run)}")

    # ── 3. PRIMING IS PER UTTERANCE, NOT PER PROCESS ──
    # The latch resets when the burst ends, or the second sentence of a session would be
    # the one that gets clipped.
    second_len, second_room, _ = second_run[0]
    check("a later utterance primes the device again",
          second_len >= min(second_room, tts._prime_frames),
          f"wrote {second_len} frames into {second_room} of room")


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
    """
    The vocabulary and the tail rule. `tests/test_voice_control.py` covers the classifier
    exhaustively and hardware-free; what is kept here is the part that belongs beside the live
    audio pipeline, because these are the phrases the barge-in path is measured with.
    """
    print_system("\n[4] Interrupt phrase classification")

    for phrase in ["Stop.", "stop", "Wait.", "wait", "Hold.", "hold", "hold on",
                   "Shut up.", "Kayra stop", "please stop", "stop talking"]:
        check(f"'{phrase}' is an interruption", is_interrupt_phrase(phrase))

    for phrase in ["Stop the music.", "Open chrome.", "What is the weather today?",
                   "Wait for the build to finish and then tell me.",
                   "Hold the window there."]:
        check(f"'{phrase}' is a normal command", not is_interrupt_phrase(phrase))

    # THE CASE THAT ACTUALLY BROKE BARGE-IN. The microphone stays open during playback, so when
    # the user says "stop" the recognizer's buffer already holds echo of Kayra's own voice --
    # the probe is the whole sentence with "stop" glued to the end, which no whole-utterance
    # test can match. `interrupt_in_tail` is what matches it, and it is consulted only while
    # she is audible.
    polluted = "and then the rollout usually takes about ten minutes stop"
    check("an echo-polluted probe is NOT a whole-utterance interrupt",
          not is_interrupt_phrase(polluted))
    check("but the tail rule finds the interrupt in it", interrupt_in_tail(polluted))
    check("the tail rule is a separate, speaking-only path",
          interrupt_in_tail is not is_interrupt_phrase)


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
        section_first_word_not_clipped(tts)
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
