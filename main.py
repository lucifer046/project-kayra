# ┌────────────────────────────────────────────────────────────────────────┐
# │                            main.py                                     │
# │                 Kayra Master Orchestrator Node                         │
# └────────────────────────────────────────────────────────────────────────┘
"""
The Central Nervous System of the AI.
This module loops infinitely, capturing voice/text input, routing it through
the Decision-Making Model (DMM), and concurrently dispatching tasks to the
Chatbot, Real-Time Search, Deep Research, Hardware Automation, and TTS matrices.

CONCURRENCY MODEL
-----------------
Three long-lived flows run at once:

  * the **main loop** (this file)          — LISTENING -> PROCESSING -> SPEAKING -> LISTENING
  * the **TTS pipeline** (text_to_speech)  — synthesis + playback workers, epoch-cancellable
  * the **barge-in watcher** (below)       — polls the STT page for interrupt words at 60ms
                                             intervals and cancels playback from OUTSIDE the
                                             main loop

That last one is essential: while a response is being generated and spoken, the main loop is
blocked inside Execute_Task and cannot poll the microphone. Before this watcher existed,
barge-in could not physically work — the "stop" was only read after the response had already
finished playing.

SELF-LISTENING
--------------
The microphone stays open the whole time (continuous listening is intentional). Utterances
captured while the assistant was audible are rejected as acoustic echo using the capture
timestamps from the STT page and the audible-window ledger from the TTS engine — see
`_is_self_echo`. The only speech accepted during playback is the interrupt vocabulary.
"""

import os
import sys
import time
import signal
import asyncio
import threading
from dotenv import dotenv_values, load_dotenv

# Reconfigure stdout/stderr to support UTF-8 characters on Windows legacy consoles
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in path for absolute importing across directories
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     STAGE 0 — LIGHTWEIGHT BOOTSTRAP                    │
# └────────────────────────────────────────────────────────────────────────┘
# Only the console/logging helpers are imported here. Everything expensive
# (ONNX voice model, headless Chrome, LLM clients) is started below, in
# parallel — importing it at the top of the file would serialize the cold start.

try:
    from modules.utils import (
        print_banner, print_system, print_info, print_error, print_success, print_warning,
        console, StageTimer, now_ms,
    )
except ImportError:
    print("Warning: utils.py not found. Running with standard print statements.")
    print_banner = print_system = print_info = print_error = print_success = print_warning = print
    now_ms = lambda: time.time() * 1000.0
    class StageTimer:  # minimal stand-in so boot still works without utils
        def __init__(self, label="startup"): self.t0 = time.perf_counter()
        def mark(self, name, quiet=False):
            e = time.perf_counter() - self.t0
            print(f"[TIMING] {name} +{e:.2f}s")
            return e
        def elapsed(self): return time.perf_counter() - self.t0
        def report(self, title=None): pass
    class ConsoleMock:
        def print(self, *args, **kwargs): print(*args)
        def input(self, prompt): return input(prompt)
    console = ConsoleMock()

BOOT = StageTimer("boot")

load_dotenv(os.path.join(project_root, ".env"))
env_vars = dotenv_values(os.path.join(project_root, ".env")) or {}
assistant_name = env_vars.get("ASSISTANT_NAME", "Kayra").strip()

# ┌────────────────────────────────────────────────────────────────────────┐
# │             STAGE 1 — LAUNCH THE SLOW ENGINES IN PARALLEL              │
# └────────────────────────────────────────────────────────────────────────┘
# Headless Chrome (~2-4s) and the Kokoro ONNX session (~1-3s) are both dominated by
# native work that releases the GIL, so booting them on threads genuinely overlaps
# them with each other AND with the LLM client construction that happens during the
# heavy imports below. Failures are collected, never swallowed — see `_report_boot_errors`.

tts_engine = None
stt_engine = None
TTS_ENABLED = False
AUDIO_ENABLED = False
_boot_errors = []


def _boot_tts():
    global tts_engine, TTS_ENABLED
    try:
        from modules.text_to_speech import TextToSpeechEngine
        tts_engine = TextToSpeechEngine()
        TTS_ENABLED = True
        BOOT.mark("TTS ready (Kokoro-ONNX vocal matrix)")
    except Exception as e:
        _boot_errors.append(("Text-to-Speech", e))


def _boot_stt():
    global stt_engine, AUDIO_ENABLED
    try:
        # get_shared_engine(), not SpeechToTextEngine(): the process-wide accessor returns the
        # existing healthy session if one is already up, so no code path can end up with two
        # Chrome sessions.
        from modules.speech_to_text import get_shared_engine
        stt_engine = get_shared_engine()
        AUDIO_ENABLED = True
        BOOT.mark("STT ready (headless Chrome Web Speech + VAD)")
    except Exception as e:
        _boot_errors.append(("Speech-to-Text", e))


_tts_boot_thread = threading.Thread(target=_boot_tts, daemon=True, name="kayra-boot-tts")
_stt_boot_thread = threading.Thread(target=_boot_stt, daemon=True, name="kayra-boot-stt")
_tts_boot_thread.start()
_stt_boot_thread.start()

# ┌────────────────────────────────────────────────────────────────────────┐
# │        STAGE 2 — CORE IMPORTS (runs concurrently with stage 1)         │
# └────────────────────────────────────────────────────────────────────────┘

# 1. Brain (LLM Engine). Importing the action modules below constructs the shared
#    CentralizedLLMEngine singleton exactly once (local-server probe + API clients).
try:
    from modules.llm_engine import CentralizedLLMEngine
except ImportError as e:
    print(f"Fatal Error: Could not locate llm_engine.py. {e}")
    sys.exit(1)

# 2. Action Modules
# NOTE: Fallback stubs accept (*args, **kwargs) rather than a fixed single argument — the
# real functions are called with multiple positional args (query, tts_engine, mood), and a
# stub with a narrower signature would crash with a TypeError the moment it's actually invoked,
# masking the real "module offline" message behind an unrelated stack trace.
try:
    from modules.chatbot import Chatbot
except ImportError:
    def Chatbot(*args, **kwargs): print_error("Chatbot module is offline.")

try:
    from modules.real_time_search import RealTimeSearchEngine
except ImportError:
    def RealTimeSearchEngine(*args, **kwargs): print_error("Real-Time Search is offline.")

try:
    from modules.deep_research import DeepResearchEngine
except ImportError:
    def DeepResearchEngine(*args, **kwargs): print_error("Deep Research Engine is offline.")

try:
    from modules.automation_windows import Automation
except ImportError:
    try:
        from modules.automation_windows import translate_and_execute as Automation
    except ImportError:
        async def Automation(cmds): print_error("Automation Engine is offline.")

try:
    from modules.emotion_engine import EmotionEngine
    emotion_engine = EmotionEngine()
    EMOTION_ENABLED = True
except ImportError:
    emotion_engine = None
    EMOTION_ENABLED = False

# 2b. Proactive Context & Habit Engine (optional — degrades silently if pygetwindow/schedule missing)
try:
    from modules.proactive_agent import ProactiveAgent
    PROACTIVE_ENABLED = True
except ImportError:
    ProactiveAgent = None
    PROACTIVE_ENABLED = False

# Interrupt-phrase helper lives with the STT vocabulary so both sides stay in sync.
try:
    from modules.speech_to_text import is_interrupt_phrase
except ImportError:
    def is_interrupt_phrase(text): return False

engine = CentralizedLLMEngine()
BOOT.mark("LLM engine ready (routing + DMM)")

# ┌────────────────────────────────────────────────────────────────────────┐
# │                  STAGE 3 — JOIN & VERIFY READINESS                     │
# └────────────────────────────────────────────────────────────────────────┘
# Nothing below this point may run before the engines it depends on actually exist —
# the threads are joined here, so no command can ever be dispatched against a
# half-initialized subsystem.

_tts_boot_thread.join()
_stt_boot_thread.join()


def _report_boot_errors():
    """Surfaces any subsystem that failed to start. Never silently degrades."""
    for name, err in _boot_errors:
        print_error(f"{name} failed to initialize: {err}")
    if not TTS_ENABLED:
        print_warning("Text-to-Speech unavailable. Assistant will run muted.")
    if not AUDIO_ENABLED:
        print_warning("Speech-to-Text unavailable. Falling back to Keyboard Input Mode.")


_report_boot_errors()

# Model-routing diagnostics: printed, not spoken. `run_boot_sequence` stays the purely
# cosmetic narration hook it was designed to be — called with no TTS engine it emits the
# status lines to the console only, so the two diagnostic sentences no longer cost ~6
# seconds of blocking speech synthesis before the assistant is usable.
engine.run_boot_sequence()

BOOT.mark("Assistant ready")

# ┌────────────────────────────────────────────────────────────────────────┐
# │                       ASSISTANT STATE MACHINE                          │
# └────────────────────────────────────────────────────────────────────────┘
# Deterministic, single-writer transitions. The main loop owns LISTENING/PROCESSING;
# the TTS pipeline owns SPEAKING; the barge-in watcher owns INTERRUPTING and always
# hands back to LISTENING.

STATE_IDLE = "IDLE"
STATE_LISTENING = "LISTENING"
STATE_PROCESSING = "PROCESSING"
STATE_SPEAKING = "SPEAKING"
STATE_INTERRUPTING = "INTERRUPTING"

_state = STATE_IDLE
_state_lock = threading.Lock()


def set_state(new_state):
    global _state
    with _state_lock:
        _state = new_state


def get_state():
    with _state_lock:
        return _state


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        BARGE-IN WATCHER THREAD                         │
# └────────────────────────────────────────────────────────────────────────┘

_barge_in_metrics = {}


def _barge_in_watcher():
    """
    Watches for interruption words while the assistant is audible and cancels playback
    immediately, independently of whatever the main loop is doing.

    The STT page flags interrupts from INTERIM recognition results, so this fires roughly
    a VAD window (~800ms) earlier than a finalized transcript would, and without the
    translation round-trip.
    """
    while True:
        try:
            if stt_engine is None:
                time.sleep(0.5)
                continue

            speaking = TTS_ENABLED and tts_engine is not None and tts_engine.is_playing

            hit = stt_engine.poll_interrupt()

            if hit and speaking:
                t_detect = time.perf_counter()
                set_state(STATE_INTERRUPTING)
                print_system(f"[BARGE-IN] '{hit.get('text', '').strip()}' — cancelling speech.")

                tts_engine.stop()
                stop_latency = (time.perf_counter() - t_detect) * 1000.0

                # Drop everything the recognizer buffered up to this point: the interrupt
                # word itself plus any echo captured while she was still talking. Without
                # this the swallowed "stop" would resurface as the next command.
                stt_engine.clear_queue()

                _barge_in_metrics.update({
                    "detected_at_ms": now_ms(),
                    "spoken_at_ms": float(hit.get("start") or now_ms()),
                    "stop_call_ms": stop_latency,
                })
                print_info(
                    f"[BARGE-IN] speech->detection {(_barge_in_metrics['detected_at_ms'] - _barge_in_metrics['spoken_at_ms']):.0f}ms, "
                    f"detection->silence {stop_latency:.0f}ms"
                )
                set_state(STATE_LISTENING)

            # `hit` while NOT speaking is discarded here on purpose: there is nothing to
            # interrupt, and leaving the flag latched would fire a phantom barge-in the
            # instant the next response starts. The utterance still reaches the normal
            # queue, where Listen() decides what to do with it.

            time.sleep(0.06 if speaking else 0.2)
        except Exception:
            # A watcher crash must never take the assistant down or wedge playback.
            time.sleep(0.5)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            INPUT CAPTURE                               │
# └────────────────────────────────────────────────────────────────────────┘

def _is_self_echo(result):
    """
    True when a captured utterance overlaps a window in which the assistant's own voice
    was leaving the speakers.

    This replaces the previous text-similarity heuristic, which compared the transcript
    against `tts.last_spoken_text`. That could not work with streamed speech: playback lags
    generation by several sentences, so the echo arriving at the microphone was of a
    sentence spoken much earlier than the one the comparison string held — similarity came
    out near zero and the echo was promoted to a user command.
    """
    if not (TTS_ENABLED and tts_engine is not None):
        return False
    return tts_engine.was_audible_between(result["start_ms"], result["end_ms"])


def Listen():
    """
    Captures one usable user utterance.

    Returns "" when nothing actionable was heard (echo rejected, or a bare interruption
    word that the audio layer has already handled and must NOT be routed to the DMM).
    """
    if AUDIO_ENABLED and stt_engine is not None:
        set_state(STATE_LISTENING)

        while True:
            result = stt_engine.capture()

            if result is None:
                return ""

            user_input = (result.get("text") or "").strip()
            if not user_input:
                return ""

            spoken_over_tts = _is_self_echo(result)

            # ── A bare interruption command is handled here, not by the DMM ──
            if is_interrupt_phrase(user_input):
                if TTS_ENABLED and tts_engine is not None and tts_engine.is_playing:
                    # The watcher normally beats us to this by ~800ms; this is the
                    # backstop for the case where only the finalized transcript matched.
                    print_system(f"[BARGE-IN] '{user_input}' — cancelling speech (finalized path).")
                    tts_engine.stop()
                    stt_engine.clear_queue()
                return ""

            # ── Echo gate ──
            # While the assistant is audible the microphone is dominated by her own voice,
            # so the only speech we trust is the interrupt vocabulary handled above.
            if spoken_over_tts:
                print_warning(f"[ECHO REJECTED] Ignoring own voice picked up by mic: '{user_input}'")
                continue

            return user_input

    return console.input("\n[bold cyan]User >[/bold cyan] ").strip()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     PROACTIVE AGENT (BACKGROUND)                       │
# └────────────────────────────────────────────────────────────────────────┘

proactive_agent = None
if PROACTIVE_ENABLED and env_vars.get("PROACTIVE_AGENT_ENABLED", "True").strip().lower() != "false":
    try:
        proactive_agent = ProactiveAgent()

        def _speak_suggestion(text):
            # Never talk over an in-flight response or over the user's turn.
            if TTS_ENABLED and tts_engine and not tts_engine.is_playing and get_state() in (STATE_IDLE, STATE_LISTENING):
                # begin_background_utterance(), not begin_turn(): a nudge must not reset the
                # user turn's latency instrumentation, and response streams key their
                # cancellation off a turn token so this cannot un-cancel an interrupted answer.
                tts_engine.begin_background_utterance()
                tts_engine.speak(text)

        proactive_agent.start(on_suggestion=_speak_suggestion)
    except Exception as e:
        print_warning(f"Proactive agent failed to start (non-fatal): {e}")
        proactive_agent = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SHUTDOWN SIGNAL HANDLER                         │
# └────────────────────────────────────────────────────────────────────────┘

def _force_shutdown(signum=None, frame=None):
    """
    Instant hard-shutdown handler registered for SIGINT (Ctrl+C) and SIGTERM.

    Terminates the ChromeDriver/Chrome processes the STT engine created — identified by the
    PIDs it recorded at startup, never by process name. Name matching was actively dangerous
    here: `automation_windows.OpenApp` opens applications through AppOpener, which uses
    `subprocess.Popen`, so a Chrome window Kayra opened *for the user* is a child of this
    process and a name-based sweep would close the user's browsing session on exit.
    """
    # 0. Silence the speakers first so nothing keeps talking through the teardown
    if TTS_ENABLED and tts_engine is not None:
        try:
            tts_engine.shutdown()
        except BaseException:
            pass

    # 1. Clean Selenium driver session
    if AUDIO_ENABLED and stt_engine:
        try:
            stt_engine.shutdown()
        except BaseException:
            pass

    # 1b. Flush and stop the proactive habit tracker, if running
    if proactive_agent is not None:
        try:
            proactive_agent.stop()
        except BaseException:
            pass

    # 2. Verify the browser processes Kayra owns are actually gone, force-killing by PID
    #    only. stt_engine.shutdown() already does this; this is the belt-and-braces pass for
    #    the case where shutdown() itself was interrupted.
    if AUDIO_ENABLED and stt_engine:
        try:
            survivors = stt_engine.terminate_owned_processes(timeout=3.0)
            if survivors:
                print_warning(f"Kayra-owned browser processes survived teardown: {sorted(survivors)}")
        except BaseException:
            pass

    print_system("System shutdown complete.")
    os._exit(0)


# Register for both Ctrl+C (SIGINT) and kill (SIGTERM)
signal.signal(signal.SIGINT,  _force_shutdown)
signal.signal(signal.SIGTERM, _force_shutdown)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                             TASK ROUTER                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Execute_Task(intent_array, original_query, mood=None):
    """
    Takes the parsed intent array from the DMM and routes it to the correct modules.
    Groups hardware automation tasks together to execute them concurrently,
    and passes AI responses to the TTS engine.
    """
    automation_commands = []

    for task in intent_array:
        task_lower = task.strip().lower()

        # A barge-in mid-response cancels the rest of the turn: the user has moved on.
        if TTS_ENABLED and tts_engine is not None and tts_engine.interrupted:
            print_system("Turn cancelled by user interruption.")
            return

        # 1. Exit Protocol
        if task_lower == "exit":
            shutdown_msg = f"Initiating shutdown sequence for {assistant_name}. Goodbye!"
            print_system(shutdown_msg)
            if TTS_ENABLED:
                # blocking=True: the process exits on the next line, so the farewell has to
                # finish playing before we tear the audio device down.
                await asyncio.to_thread(tts_engine.speak, "Shutting down. Goodbye.", True)

            # Use the robust force-shutdown handler to kill all processes instantly
            _force_shutdown()

        # 2. General Conversation (Knowledge, Math, Logic)
        elif task_lower.startswith("general "):
            set_state(STATE_SPEAKING)
            await asyncio.to_thread(Chatbot, original_query, tts_engine if TTS_ENABLED else None, mood)

        # 3. Real-Time Web Search (Live RAG)
        elif task_lower.startswith("realtime "):
            set_state(STATE_SPEAKING)
            # The TTS engine is handed in so sentences are spoken as they stream out of the
            # model, instead of buffering the whole answer and speaking it afterwards.
            await asyncio.to_thread(RealTimeSearchEngine, original_query, mood,
                                    tts_engine if TTS_ENABLED else None)

        # 4. Autonomous Deep Research
        elif task_lower.startswith("deep research "):
            topic = task.replace("deep research", "", 1).strip()
            if TTS_ENABLED:
                tts_engine.speak("Initiating deep research protocol. This may take a few minutes.")

            await asyncio.to_thread(DeepResearchEngine, topic)

            if TTS_ENABLED and not tts_engine.interrupted:
                tts_engine.begin_turn()
                tts_engine.speak("Deep research complete. The report has been saved to your system.")

        # 5. Hardware & System Automation (Grouped)
        else:
            automation_commands.append(task.strip())

    # 6. Execute all grouped automation commands concurrently
    if automation_commands:
        print_info(f"Dispatching hardware automation tasks: {automation_commands}")
        await Automation(automation_commands)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                             MASTER LOOP                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Main_Loop():
    """
    The infinite listening and routing loop.
    """
    print_banner(f"{assistant_name.upper()} SYSTEM ONLINE", "Master Orchestrator Node Active")

    if AUDIO_ENABLED:
        print_success("Microphone arrays hot. Continuous Web-Speech recognition ONLINE.")
        threading.Thread(target=_barge_in_watcher, daemon=True, name="kayra-barge-in").start()
        print_info("Barge-in watcher active — say \"stop\" or \"wait\" to interrupt at any time.")
    else:
        print_warning("SpeechToText module not detected. Defaulting to Keyboard Input Mode.")

    BOOT.report("BOOT")

    # ONE short spoken line. The model-routing diagnostics stay on the console where they
    # belong; narrating them cost several seconds of speech before the first user turn.
    if TTS_ENABLED:
        tts_engine.begin_turn()
        tts_engine.speak(f"{assistant_name} online.")

    while True:
        try:
            try:
                if AUDIO_ENABLED:
                    console.print("\n[bold cyan]Listening...[/bold cyan]")
            except ValueError:
                os._exit(1) # Terminal died

            # 1. Capture Input
            user_input = await asyncio.to_thread(Listen)

            if not user_input or not user_input.strip():
                continue

            if AUDIO_ENABLED:
                print_info(f"Transcribed Input: '{user_input}'")

            # A fresh turn: clears any latched interrupt from the previous response and
            # starts the command -> first-audible-word stopwatch.
            turn_t0 = time.perf_counter()
            if TTS_ENABLED:
                tts_engine.begin_turn()

            set_state(STATE_PROCESSING)

            detected_mood = emotion_engine.analyze_text(user_input) if EMOTION_ENABLED else None

            # 2. Feed text into the Decision Making Model (DMM)
            try:
                console.print("[dim yellow]Analyzing semantic intent...[/dim yellow]")
            except ValueError:
                os._exit(1)

            dmm_commands = await asyncio.to_thread(engine.classify_intent, user_input)
            dmm_seconds = time.perf_counter() - turn_t0

            try:
                console.print(f"[bold magenta]System Trace ->[/bold magenta] {dmm_commands} "
                              f"[dim](DMM {dmm_seconds:.2f}s)[/dim]")
            except ValueError:
                os._exit(1)

            # 3. Dispatch to Execution Router
            await Execute_Task(dmm_commands, user_input, detected_mood)

            if TTS_ENABLED:
                first_audio = tts_engine.last_latency.get("first_audio_s")
                if first_audio:
                    print_info(f"[LATENCY] command -> first spoken word: {first_audio:.2f}s "
                               f"(DMM {dmm_seconds:.2f}s of it)")

            set_state(STATE_LISTENING)

        except KeyboardInterrupt:
            try:
                console.print()
            except Exception:
                pass
            print_system("Manual interrupt detected. Halting main execution loop.")
            _force_shutdown()
        except Exception as e:
            print_error(f"Critical failure in Master Loop: {e}")
            set_state(STATE_LISTENING)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                               BOOT LOGIC                               │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(Main_Loop())
    except KeyboardInterrupt:
        print_system("System shutdown complete.")
        _force_shutdown()
