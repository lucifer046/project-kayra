# ┌────────────────────────────────────────────────────────────────────────┐
# │                           text_to_speech.py                            │
# │                 Offline TTS Speech Synthesis Engine                    │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements a lightweight, 100% offline Text-to-Speech (TTS) system.
It utilizes the Kokoro-82M ONNX voice model running optimized on the CPU,
with dynamic voice style mapping and real-time chunked audio streaming.

PIPELINE (three decoupled stages, one thread each):

    speak(text) ──> text queue ──> [synth worker] ──> audio queue ──> [playback worker] ──> speakers
                                    Kokoro ONNX                        persistent sd.OutputStream

Every queued item is tagged with the *utterance epoch* it was produced under.
`stop()` bumps the epoch, drains both queues, and aborts the output stream, so a
barge-in kills in-flight synthesis, queued sentences, and buffered audio in one
atomic step — nothing produced before the interrupt can ever reach the speakers
afterwards. See `stop()` / `begin_turn()`.

The engine also records exactly WHEN it was audible (`speech_intervals`), which
main.py uses to tell the user's voice apart from its own acoustic echo returning
through the microphone.
"""

import os
import re
import sys
import time
import queue
import asyncio
import threading
import numpy as np
import sounddevice as sd
from collections import deque
from kokoro_onnx import Kokoro
from dotenv import dotenv_values
try:
    from .utils import print_info, print_warning, print_error, print_system, print_success, console, now_ms, speech_safe_text
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_system, print_success, console, now_ms, speech_safe_text
    except ImportError:
        from utils import print_info, print_warning, print_error, print_system, print_success, console, now_ms, speech_safe_text


# Sample rate the Kokoro v1.0 checkpoints synthesize at.
SAMPLE_RATE = 24000

# Audio is handed to the sound card in slices of this many milliseconds. Small
# slices are what make barge-in feel instant: the playback worker re-checks the
# utterance epoch between slices, so the worst-case delay between `stop()` and
# actual silence is one slice plus the device latency, not one whole sentence.
WRITE_SLICE_MS = 40


class KokoroOnnx(Kokoro):
    """
    Quantized Kokoro TTS ONNX wrapper with robust pathing and real-time streaming capability.
    Extends standard Kokoro to support a synchronous streaming generator compatible with sounddevice.
    """
    def __init__(self, model_path: str, voices_path: str):
        super().__init__(model_path, voices_path)

    def stream(self, text: str, voice: str, speed: float = 1.1, lang: str = "en-us"):
        """
        Synchronous generator wrapper for the asynchronous create_stream method.
        Yields audio chunks in real-time.
        """
        q = queue.Queue()

        def run_async_loop():
            # Create a separate dedicated event loop for background voice chunk processing
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def consume_stream():
                    async for audio_samples, sample_rate in self.create_stream(
                        text, voice=voice, speed=speed, lang=lang, trim=True
                    ):
                        q.put((audio_samples, sample_rate))
                    q.put(None)  # Signal completion of stream

                loop.run_until_complete(consume_stream())
            except Exception as e:
                print_error(f"Error in background voice stream: {e}")
                q.put(None)
            finally:
                loop.close()

        # Execute the audio synthesis async generator on a daemon background thread
        threading.Thread(target=run_async_loop, daemon=True).start()

        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk


class DynamicVoiceEngine:
    """
    Dynamic voice allocation engine configured via environment variables and supporting real-time streaming.

    Concurrency model
    -----------------
    Two long-lived daemon workers (synthesis + playback) and one shared epoch counter.
    All mutable cross-thread state (`_epoch`, `_speaking`, `_intervals`) is guarded by
    `self._lock`; the queues provide the hand-off between stages. Callers only ever touch
    `speak()`, `stop()`, `begin_turn()`, `wait_until_idle()` and the read-only properties.
    """

    # Words that, on their own, mean "be quiet and listen to me". Exposed here so the
    # STT page (which does the first-pass interim match) and main.py's gate agree on
    # one vocabulary instead of each carrying its own private copy.
    INTERRUPT_WORDS = (
        "stop", "wait", "shut up", "pause", "hold on", "quiet", "silence",
        "enough", "cancel", "nevermind", "never mind",
    )

    def __init__(self, model_filename="kokoro-v1.0.int8.onnx", voices_filename="voices-v1.0.bin", warm_up=True):
        # Robust path resolution
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Load environment specifications
        env_vars = dotenv_values(os.path.join(project_root, ".env")) or {}

        # Dynamic name and fallback gender mapping
        self.assistant_name = env_vars.get("ASSISTANT_NAME", "").strip()
        if not self.assistant_name:
            self.assistant_name = "Kayra"
            gender = "female"
        else:
            gender = env_vars.get("ASSISTANT_GENDER", "Female").strip().lower()

        model_path = os.path.join(project_root, "models", model_filename)
        voices_path = os.path.join(project_root, "models", voices_filename)

        # Automatic fallback to standard filenames if default filenames are not present
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
            alt_model_path = os.path.join(project_root, "models", "kokoro.onnx")
            alt_voices_path = os.path.join(project_root, "models", "voices.bin")
            if os.path.exists(alt_model_path) and os.path.exists(alt_voices_path):
                model_path = alt_model_path
                voices_path = alt_voices_path
                # The full-precision graph synthesizes at roughly 1.0-1.4x real time on a
                # typical CPU, which is the single largest remaining contributor to
                # speaking latency. The int8 checkpoint this class asks for by default is
                # several times faster at effectively identical audio quality.
                print_warning(
                    "Using full-precision kokoro.onnx. Dropping the quantized "
                    f"'{model_filename}' + '{voices_filename}' into models/ cuts speech "
                    "synthesis latency substantially."
                )
            else:
                # Direct CWD check
                if os.path.exists(model_filename) and os.path.exists(voices_filename):
                    model_path = os.path.abspath(model_filename)
                    voices_path = os.path.abspath(voices_filename)

        # Verify physical model presence
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
            print_error(f"Core voice matrix components missing: Check {model_path}")
            sys.exit(1)

        self.onnx = KokoroOnnx(model_path, voices_path)
        self.sample_rate = SAMPLE_RATE
        self.last_spoken_text = ""

        # ── Cancellation / turn state ──────────────────────────────────────
        # `_epoch` is the generation counter. Anything queued under an older epoch is
        # stale and gets dropped instead of played. `_interrupted` latches until the
        # next `begin_turn()` so that a response whose LLM stream is still running
        # cannot resurrect itself one sentence at a time after the user said "stop".
        self._lock = threading.RLock()
        self._epoch = 0
        self._interrupted = False

        self._text_queue = queue.Queue()    # (epoch, text) pending synthesis
        self._audio_queue = queue.Queue()   # (epoch, np.ndarray) pending playback
        # Epoch of the sentence currently being synthesized, or None when idle. Compared
        # against `_epoch` so that a cancelled-but-still-running synthesis (Kokoro cannot be
        # aborted mid-inference) does not keep reporting the engine as "speaking" — its
        # output is discarded on the way out.
        self._synth_epoch = None
        self._playing = False
        self._idle = threading.Event()
        self._idle.set()

        # ── Echo bookkeeping ───────────────────────────────────────────────
        # Wall-clock windows (ms) during which audio was actually leaving the sound
        # card. main.py intersects these with the microphone utterance timestamps to
        # decide "was this the user, or was this me?".
        self._intervals = deque(maxlen=64)   # list of [start_ms, end_ms]
        self._burst_start_ms = None
        # Wall-clock of the last barge-in. Anything the microphone starts capturing after
        # this instant belongs to the user, by definition — they just took the floor.
        # Cleared the moment audio starts again (see `_begin_burst`).
        self._last_stop_ms = None

        # Latency instrumentation: filled per utterance, read by main.py / diagnostics.
        self.last_latency = {}
        self._turn_t0 = None

        # ── Persistent output stream ───────────────────────────────────────
        # Opened ONCE and left running for the process lifetime. The previous
        # implementation called sd.play()/sd.wait() per chunk, paying a full device
        # open+close (tens of ms, and an audible gap) between every audio chunk.
        self._stream = None
        self._open_stream()

        # ── Workers ────────────────────────────────────────────────────────
        self._synth_thread = threading.Thread(target=self._synth_worker, daemon=True,
                                              name="kayra-tts-synth")
        self._playback_thread = threading.Thread(target=self._playback_worker, daemon=True,
                                                 name="kayra-tts-playback")
        self._synth_thread.start()
        self._playback_thread.start()

        # => Dynamic vocal allocation style mapping
        if gender == "male":
            self.voice = "am_adam"  # High-quality North American Male style vector
        else:
            self.voice = "af_bella"  # Ultra-realistic North American Female style vector

        # First Kokoro inference pays the ONNX graph-optimization + weight-warm cost
        # (~0.6-1.5s on CPU). Doing it here, silently, on a background thread means the
        # user's FIRST real sentence starts at steady-state latency instead of paying it.
        if warm_up:
            threading.Thread(target=self._warm_up, daemon=True, name="kayra-tts-warmup").start()

    # ──────────────────────────────────────────────────────────────────────
    #                        AUDIO DEVICE PLUMBING
    # ──────────────────────────────────────────────────────────────────────

    def _open_stream(self):
        """Opens the persistent output stream, falling back to blocking sd.play() if unavailable."""
        try:
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=0,          # let PortAudio pick its optimal block size
                latency="low",
            )
            self._stream.start()
        except Exception as e:
            # No usable device / exclusive-mode conflict: degrade to the legacy path
            # rather than taking the whole assistant down over audio hardware.
            print_warning(f"Persistent audio stream unavailable ({e}). Falling back to buffered playback.")
            self._stream = None

    def _warm_up(self):
        """Runs one throwaway synthesis so the first user-visible utterance is not the cold one."""
        try:
            t0 = time.perf_counter()
            for _ in self.onnx.stream("Ready.", voice=self.voice, speed=1.1):
                break  # first chunk is enough to force the full graph warm-up
            self.warm_up_seconds = time.perf_counter() - t0
        except Exception:
            self.warm_up_seconds = None

    # ──────────────────────────────────────────────────────────────────────
    #                              WORKERS
    # ──────────────────────────────────────────────────────────────────────

    def _synth_worker(self):
        """Consumes queued sentences, synthesizes them, and forwards audio to the playback queue."""
        while True:
            epoch, text = self._text_queue.get()
            try:
                if epoch != self._epoch:
                    continue  # Interrupted before we even started: drop silently.

                with self._lock:
                    self._synth_epoch = epoch
                    self._idle.clear()

                try:
                    for audio_samples, _rate in self.onnx.stream(text, voice=self.voice, speed=1.1):
                        if epoch != self._epoch:
                            break  # Barge-in mid-sentence: abandon the rest of this sentence.
                        self._audio_queue.put((epoch, np.asarray(audio_samples, dtype=np.float32)))
                except Exception as e:
                    print_error(f"Failed to stream voice output: {e}")
            finally:
                with self._lock:
                    self._synth_epoch = None
                self._text_queue.task_done()
                self._refresh_idle()

    def _playback_worker(self):
        """Writes queued audio to the sound card in small slices so cancellation is immediate."""
        slice_len = int(SAMPLE_RATE * WRITE_SLICE_MS / 1000)
        while True:
            epoch, audio = self._audio_queue.get()
            try:
                if epoch != self._epoch:
                    continue  # Stale audio from a cancelled utterance: never plays.

                self._begin_burst()
                if self._stream is not None:
                    for start in range(0, len(audio), slice_len):
                        if epoch != self._epoch:
                            break
                        try:
                            self._stream.write(audio[start:start + slice_len])
                        except Exception as e:
                            if epoch != self._epoch:
                                # `stop()` aborted the device out from under this write.
                                # That is the intended path for a barge-in — it has already
                                # restarted the stream, so do not touch it or warn.
                                break
                            print_warning(f"Audio write failed, reopening stream: {e}")
                            self._reopen_stream()
                            break
                        self._touch_burst()
                else:
                    # Legacy fallback path (no persistent stream available)
                    sd.play(audio, SAMPLE_RATE)
                    sd.wait()
                    self._touch_burst()
            finally:
                self._audio_queue.task_done()
                if self._audio_queue.empty():
                    self._end_burst()
                self._refresh_idle()

    def _reopen_stream(self):
        """Recovers from a device error (e.g. Bluetooth headset dropping out)."""
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._open_stream()

    # ──────────────────────────────────────────────────────────────────────
    #                    AUDIBLE-WINDOW (ECHO) BOOKKEEPING
    # ──────────────────────────────────────────────────────────────────────

    def _begin_burst(self):
        with self._lock:
            self._playing = True
            self._idle.clear()
            self._last_stop_ms = None  # A new utterance re-arms the echo gate.
            if self._burst_start_ms is None:
                self._burst_start_ms = now_ms()
                self._intervals.append([self._burst_start_ms, self._burst_start_ms])
            if self._turn_t0 is not None:
                # First audible sample of this turn — the number that actually matters
                # for "how long before she started talking".
                self.last_latency["first_audio_s"] = time.perf_counter() - self._turn_t0
                self._turn_t0 = None

    def _touch_burst(self):
        with self._lock:
            if self._intervals:
                self._intervals[-1][1] = now_ms()

    def _end_burst(self):
        with self._lock:
            if self._burst_start_ms is not None:
                if self._intervals:
                    self._intervals[-1][1] = now_ms()
                self._burst_start_ms = None
            self._playing = False

    def _refresh_idle(self):
        """Sets the idle event once nothing is queued, synthesizing, or playing."""
        with self._lock:
            if (self._text_queue.empty() and self._audio_queue.empty()
                    and not self._is_synthesizing_live() and not self._playing):
                self._idle.set()

    def _is_synthesizing_live(self) -> bool:
        """True only while synthesizing work that is still wanted (current epoch)."""
        return self._synth_epoch is not None and self._synth_epoch == self._epoch

    def was_audible_between(self, start_ms: float, end_ms: float,
                            lead_margin_ms: float = 300.0,
                            tail_margin_ms: float = 250.0) -> bool:
        """
        Returns True if audio was leaving the speakers at any point overlapping
        [start_ms, end_ms], padded to absorb recognizer timestamp jitter.

        This is the primitive that makes echo rejection reliable: a microphone
        utterance is only ever finalized ~800ms AFTER it was spoken, so comparing
        against "am I speaking right now" is wrong — we have to compare against
        "was I speaking when that audio was actually captured".

        The margins are asymmetric on purpose. `lead_margin_ms` covers Web Speech
        timestamps arriving slightly late relative to the physical audio; `tail_margin_ms`
        is kept tight so a user who speaks immediately after the assistant finishes is
        not mistaken for an echo.
        """
        with self._lock:
            intervals = [list(i) for i in self._intervals]
            live = self._playing
            burst_start = self._burst_start_ms
            last_stop = self._last_stop_ms

        # The user barged in and nothing has played since: they own the floor, so this
        # utterance is theirs even if it began while the tail margin was still open.
        if last_stop is not None and start_ms >= (last_stop - 150.0):
            return False

        if live:
            intervals.append([burst_start or start_ms, now_ms()])
        for s, e in intervals:
            if start_ms <= (e + tail_margin_ms) and (s - lead_margin_ms) <= end_ms:
                return True
        return False

    # ──────────────────────────────────────────────────────────────────────
    #                             PUBLIC API
    # ──────────────────────────────────────────────────────────────────────

    @property
    def is_playing(self) -> bool:
        """True while anything is queued, being synthesized, or audibly playing."""
        with self._lock:
            return (self._playing or self._is_synthesizing_live()
                    or not self._text_queue.empty() or not self._audio_queue.empty())

    @property
    def interrupted(self) -> bool:
        """True from the moment `stop()` runs until the next `begin_turn()`."""
        with self._lock:
            return self._interrupted

    def turn_token(self):
        """
        Returns an opaque token identifying the current utterance generation.

        A response generator captures this once and then asks `is_cancelled(token)` instead
        of reading the global `interrupted` flag. The difference matters: `interrupted` is
        process-wide and any caller can clear it with `begin_turn()` — including the
        proactive agent, which fires on its own schedule. If a nudge landed in the window
        between a barge-in and the chatbot noticing it, the cleared flag would let the
        cancelled response carry on speaking. A token cannot be un-cancelled, because
        `stop()` only ever moves the epoch forward.
        """
        with self._lock:
            return self._epoch

    def is_cancelled(self, token) -> bool:
        """True once `stop()` has superseded the generation `token` was taken from."""
        with self._lock:
            return token != self._epoch

    def begin_turn(self):
        """
        Opens a new speaking turn: clears the latched interrupt flag and starts the
        first-audio latency stopwatch. main.py calls this once per user command,
        BEFORE dispatching it — so a response can never inherit the previous turn's
        interrupt state, and a stale one can never un-cancel itself.
        """
        with self._lock:
            self._interrupted = False
            self._turn_t0 = time.perf_counter()
            self.last_latency = {}

    def begin_background_utterance(self):
        """
        Prepares the engine for an unprompted utterance (e.g. a proactive suggestion)
        without disturbing the user turn's instrumentation.

        Deliberately does NOT reset `last_latency`: that belongs to the command the user
        actually issued, and a background nudge landing mid-turn would otherwise erase the
        command-to-first-word measurement.
        """
        with self._lock:
            self._interrupted = False

    def speak(self, text, blocking: bool = False):
        """
        Queues text for synthesis and playback.

        Non-blocking by default: the caller (typically the chatbot's token stream)
        hands over one sentence at a time and immediately goes back to consuming
        LLM tokens, which is what makes the speech overlap generation.

        Args:
            text (str): The sentence/paragraph to speak.
            blocking (bool): Wait until this utterance has finished playing. Used for
                             the shutdown line, where the process exits right after.
        """
        if not text or not text.strip():
            return

        with self._lock:
            if self._interrupted:
                # The user cut this response off. Anything the generator is still
                # producing for it is dead on arrival.
                return
            epoch = self._epoch

        # Normalize for SPEECH only. The caller has already streamed the model's response to
        # the console in full, so this conversion never destroys what the user can read — it
        # only decides what they hear (see utils.speech_safe_text).
        #
        # The previous inline cleaner was `re.sub(r'[^\w\s\.,!\?\-\'"]', '', text)`, which
        # deleted every character it did not recognise: "50%" was spoken as "50", "$20" as
        # "20", and Devanagari vowel signs (category Mn, which \w excludes) were stripped out
        # of Hindi replies entirely.
        clean_text = speech_safe_text(text)
        if not clean_text.strip():
            return

        console.print(f"\n[bold magenta][{self.assistant_name} Speaking]:[/] [italic text]{clean_text}[/]")

        self.last_spoken_text = clean_text.lower()

        self._idle.clear()
        self._text_queue.put((epoch, clean_text))

        if blocking:
            self.wait_until_idle(timeout=60)

    # Explicit alias for streaming callers, so the intent reads clearly at the call site.
    say = speak

    def stop(self):
        """
        Barge-in protocol: silence everything, now.

        1. Bump the epoch  -> in-flight synthesis and every queued item become stale.
        2. Drain both queues -> nothing pending survives.
        3. Abort the output stream -> audio already handed to the sound card is dropped.
        4. Latch `_interrupted` -> late `speak()` calls from the still-running LLM
           stream are refused until the next `begin_turn()`.
        """
        with self._lock:
            self._epoch += 1
            self._interrupted = True
            self._last_stop_ms = now_ms()

        for q in (self._text_queue, self._audio_queue):
            while True:
                try:
                    q.get_nowait()
                    q.task_done()
                except queue.Empty:
                    break

        if self._stream is not None:
            try:
                # abort() discards the device buffer instead of draining it (stop() would
                # politely play out the ~100ms already queued — audible as a trailing word).
                self._stream.abort()
                self._stream.start()
            except Exception:
                self._reopen_stream()
        else:
            try:
                sd.stop()
            except Exception:
                pass

        self._end_burst()
        self._idle.set()

    def wait_until_idle(self, timeout: float = None) -> bool:
        """Blocks until the speech pipeline has fully drained (or the timeout elapses)."""
        return self._idle.wait(timeout=timeout)

    def shutdown(self):
        """Closes the audio device cleanly."""
        self.stop()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class TextToSpeechEngine(DynamicVoiceEngine):
    """
    Standard backward-compatible Text-to-Speech engine wrapper.
    Inherits all real-time streaming and voice mapping capabilities from DynamicVoiceEngine.

    Prefers the quantized checkpoint (materially lower synthesis latency) and falls back to
    the classic `kokoro.onnx` / `voices.bin` pair automatically when it isn't present — this
    used to request the full-precision files by name, so a quantized model sitting in
    models/ was never picked up.
    """
    def __init__(self, model_filename="kokoro-v1.0.int8.onnx", voices_filename="voices-v1.0.bin", warm_up=True):
        super().__init__(model_filename=model_filename, voices_filename=voices_filename, warm_up=warm_up)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 BACKWARD COMPATIBILITY CLASS ALIASES                   │
# └────────────────────────────────────────────────────────────────────────┘
LiveOfflineTTS = TextToSpeechEngine
LiveOffileTTS = TextToSpeechEngine
OfflineTTS = TextToSpeechEngine


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     MAIN SCRIPT TEST ENTRYPOINT                        │
# └────────────────────────────────────────────────────────────────────────┘
if __name__ == "__main__":
    # Run a test speech synthesis sequence using the modern dynamic engine
    tts = DynamicVoiceEngine()
    tts.begin_turn()
    tts.speak("Voice matrix active. Real-time audio streaming is fully initialized.", blocking=True)
    print_info(f"First-audio latency: {tts.last_latency.get('first_audio_s', float('nan')):.3f}s")
