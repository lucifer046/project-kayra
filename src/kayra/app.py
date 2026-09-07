# ┌────────────────────────────────────────────────────────────────────────┐
# │                              app.py                                    │
# │                  Kayra Master Orchestrator Node                        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The application: boot sequence, listen/route loop, lifecycle, shutdown.

ORCHESTRATION
-------------
This module is the orchestrator, not the place domain logic lives. It initializes the
services, starts the background workers, moves events between them and owns the lifecycle.
The shared assistant state and the event bus live in `kayra.core.runtime_state` so workers on
other threads can read them without importing this module.

NO IMPORT-TIME SIDE EFFECTS
---------------------------
Importing `kayra.app` starts nothing: no ONNX session, no headless Chrome, no microphone, no
API clients. All of that happens inside `bootstrap()`, which is idempotent. That property is
what lets the test suites import this module to inspect it, and it is why the boot lives in a
function rather than at module scope.

    from kayra.app import main
    main()          # bootstrap + run

CONCURRENCY MODEL
-----------------
Four long-lived flows run at once:

  * the **main loop** (this file)               — LISTENING -> PROCESSING -> SPEAKING -> ...
  * the **TTS pipeline** (output.text_to_speech)— synthesis + playback workers, epoch-cancellable
  * the **barge-in watcher** (below)            — polls the STT page for interrupt words at
                                                  60ms intervals and cancels playback from
                                                  OUTSIDE the main loop
  * the **proactive service** (services.proactive_agent) — one thread sleeping on an Event

The barge-in watcher is essential: while a response is being generated and spoken, the main
loop is blocked inside `Execute_Task` and cannot poll the microphone. Before this watcher
existed, barge-in could not physically work — the "stop" was only read after the response had
already finished playing.

The proactive service is deliberately a READER of runtime state, never a writer. It cannot
touch the TTS cancellation epoch, and its speech is cancelled by the same barge-in path as any
other response.

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

# Reconfigure stdout/stderr to support UTF-8 characters on Windows legacy consoles.
if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from kayra.core.config import (load_environment, env_values,
                               assistant_name as configured_assistant_name)
from kayra.core.runtime_state import AssistantState, get_runtime_state
from kayra.utils import (
    print_banner, print_system, print_info, print_error, print_success, print_warning,
    console, StageTimer, now_ms,
)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        MODULE-LEVEL STATE                              │
# └────────────────────────────────────────────────────────────────────────┘
# Populated by `bootstrap()`. Declared here so the functions below can reference them, and so
# that importing this module is free.

BOOT = None
env_vars = {}
assistant_name = "Kayra"

tts_engine = None
stt_engine = None
TTS_ENABLED = False
AUDIO_ENABLED = False
_boot_errors = []

engine = None
emotion_engine = None
EMOTION_ENABLED = False

proactive_agent = None
PROACTIVE_AVAILABLE = False

Chatbot = None
RealTimeSearchEngine = None
DeepResearchEngine = None
Automation = None
pending_confirmation = None
resolve_confirmation = None
shutdown_automation = None
is_interrupt_phrase = None
create_default_agent = None

_BOOTSTRAPPED = False
_barge_in_metrics = {}


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       ASSISTANT STATE MACHINE                          │
# └────────────────────────────────────────────────────────────────────────┘
# Deterministic, single-writer transitions, owned by the shared runtime service rather than by
# a module global here. The main loop owns LISTENING/PROCESSING/AUTOMATING; the response
# dispatcher owns SPEAKING; the barge-in watcher owns INTERRUPTING and always hands back to
# LISTENING.
#
# It lives out there because it has a SECOND reader: the proactive agent runs on its own thread
# and has to know, truthfully and at any instant, whether the user is mid-turn.

RUNTIME = get_runtime_state()

STATE_IDLE = AssistantState.IDLE
STATE_LISTENING = AssistantState.LISTENING
STATE_PROCESSING = AssistantState.PROCESSING
STATE_SPEAKING = AssistantState.SPEAKING
STATE_INTERRUPTING = AssistantState.INTERRUPTING
STATE_AUTOMATING = AssistantState.AUTOMATING

set_state = RUNTIME.set_state


def get_state():
    return RUNTIME.state


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             BOOTSTRAP                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _boot_tts():
    global tts_engine, TTS_ENABLED
    try:
        from kayra.output.text_to_speech import TextToSpeechEngine
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
        # browser sessions.
        from kayra.input.speech_to_text import get_shared_engine
        stt_engine = get_shared_engine()
        AUDIO_ENABLED = True
        # Named after the browser actually chosen, which is not always Chrome: the engine
        # picks whichever installed browser can genuinely transcribe.
        browser = getattr(getattr(stt_engine, "browser", None), "label", None) or "browser"
        BOOT.mark(f"STT ready (headless {browser} Web Speech + VAD)")
    except Exception as e:
        _boot_errors.append(("Speech-to-Text", e))


def _report_boot_errors():
    """Surfaces any subsystem that failed to start. Never silently degrades."""
    for name, err in _boot_errors:
        print_error(f"{name} failed to initialize: {err}")
    if not TTS_ENABLED:
        print_warning("Text-to-Speech unavailable. Assistant will run muted.")
    if not AUDIO_ENABLED:
        print_warning("Speech-to-Text unavailable. Falling back to Keyboard Input Mode.")


def bootstrap():
    """
    Brings every subsystem up, overlapping the slow ones. Idempotent.

    ORDERING IS LOAD-BEARING. Headless Chrome (~2-4s) and the Kokoro ONNX session (~1-3s) are
    both dominated by native work that releases the GIL, so booting them on threads genuinely
    overlaps them with each other AND with the LLM client construction that happens during the
    heavy imports below. Moving the action-module imports back above the thread launches
    re-serializes the entire cold start.
    """
    global _BOOTSTRAPPED, BOOT, env_vars, assistant_name
    global engine, emotion_engine, EMOTION_ENABLED
    global Chatbot, RealTimeSearchEngine, DeepResearchEngine, Automation
    global pending_confirmation, resolve_confirmation, shutdown_automation
    global is_interrupt_phrase, create_default_agent, PROACTIVE_AVAILABLE, proactive_agent

    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    BOOT = StageTimer("boot")
    load_environment()
    env_vars = env_values()
    assistant_name = configured_assistant_name()

    # ── STAGE 1 — launch the slow engines in parallel ──
    tts_thread = threading.Thread(target=_boot_tts, daemon=True, name="kayra-boot-tts")
    stt_thread = threading.Thread(target=_boot_stt, daemon=True, name="kayra-boot-stt")
    tts_thread.start()
    stt_thread.start()

    # ── STAGE 2 — core imports (run concurrently with stage 1) ──
    # Importing the action modules constructs the shared CentralizedLLMEngine singleton exactly
    # once (local-server probe + API clients).
    from kayra.intelligence.llm_engine import CentralizedLLMEngine

    # Fallback stubs accept (*args, **kwargs) rather than a fixed single argument — the real
    # functions are called with several positional args, and a stub with a narrower signature
    # would crash with a TypeError the moment it is invoked, masking the real "module offline"
    # message behind an unrelated stack trace.
    try:
        from kayra.services.chatbot import Chatbot as _Chatbot
        Chatbot = _Chatbot
    except ImportError:
        def Chatbot(*args, **kwargs):
            print_error("Chatbot module is offline.")

    try:
        from kayra.services.real_time_search import RealTimeSearchEngine as _Search
        RealTimeSearchEngine = _Search
    except ImportError:
        def RealTimeSearchEngine(*args, **kwargs):
            print_error("Real-Time Search is offline.")

    try:
        from kayra.services.deep_research import DeepResearchEngine as _Research
        DeepResearchEngine = _Research
    except ImportError:
        def DeepResearchEngine(*args, **kwargs):
            print_error("Deep Research Engine is offline.")

    try:
        from kayra.automation.windows import (Automation as _Automation,
                                              pending_confirmation as _pending,
                                              resolve_confirmation as _resolve,
                                              shutdown_automation as _shutdown_auto)
        Automation, pending_confirmation = _Automation, _pending
        resolve_confirmation, shutdown_automation = _resolve, _shutdown_auto
    except ImportError as e:
        print_error(f"Automation Engine is offline: {e}")

        async def Automation(cmds):
            print_error("Automation Engine is offline.")
            return ""

        def pending_confirmation():
            return None

        def resolve_confirmation(reply):
            return False, ""

        def shutdown_automation():
            return 0

    try:
        from kayra.intelligence.emotion_engine import EmotionEngine
        emotion_engine = EmotionEngine()
        EMOTION_ENABLED = True
    except ImportError:
        emotion_engine = None
        EMOTION_ENABLED = False

    # Proactive service (optional). `create_default_agent` performs the wiring, so this module
    # never has to know how proactive speech reaches the speaker or which LLM client it uses.
    try:
        from kayra.services.proactive_agent import create_default_agent as _create_agent
        create_default_agent = _create_agent
        PROACTIVE_AVAILABLE = True
    except ImportError:
        create_default_agent = None
        PROACTIVE_AVAILABLE = False

    # The interrupt-phrase helper lives with the STT vocabulary so both sides stay in sync.
    try:
        from kayra.input.speech_to_text import is_interrupt_phrase as _is_interrupt
        is_interrupt_phrase = _is_interrupt
    except ImportError:
        def is_interrupt_phrase(text):
            return False

    engine = CentralizedLLMEngine()
    BOOT.mark("LLM engine ready (routing + DMM)")

    # ── STAGE 3 — join & verify readiness ──
    # Nothing below may run before the engines it depends on actually exist, so no command can
    # ever be dispatched against a half-initialized subsystem.
    tts_thread.join()
    stt_thread.join()
    _report_boot_errors()

    # Model-routing diagnostics: printed, not spoken. Narrating them cost several seconds of
    # blocking speech synthesis before the assistant was usable.
    engine.run_boot_sequence()
    BOOT.mark("Assistant ready")

    # ── STAGE 4 — background services ──
    # Started AFTER the core is ready and returns immediately: it must not be able to lengthen
    # the cold start. Its first evaluation happens one tick interval later, and it reads
    # assistant state from RUNTIME rather than being told when to be quiet.
    if PROACTIVE_AVAILABLE:
        try:
            proactive_agent = create_default_agent(
                tts_engine=tts_engine if TTS_ENABLED else None,
                llm_engine=engine,
                runtime=RUNTIME,
            )
            proactive_agent.start()
        except Exception as e:
            print_warning(f"Proactive agent failed to start (non-fatal): {e}")
            proactive_agent = None

    _install_signal_handlers()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        BARGE-IN WATCHER THREAD                         │
# └────────────────────────────────────────────────────────────────────────┘

def _barge_in_watcher():
    """
    Watches for interruption words while the assistant is audible and cancels playback
    immediately, independently of whatever the main loop is doing.

    The STT page flags interrupts from INTERIM recognition results, so this fires roughly a
    VAD window (~800ms) earlier than a finalized transcript would, and without the translation
    round-trip.
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

                # The user has the floor. Everything that decides whether an unprompted line
                # may be spoken keys off this timestamp, and a proactive suggestion that was
                # mid-flight is cancelled by exactly this same path — there is no separate
                # interruption mechanism for proactive speech.
                RUNTIME.note_interrupt()
                RUNTIME.emit("barge_in", text=hit.get("text", ""))

                # Drop everything the recognizer buffered up to this point: the interrupt word
                # itself plus any echo captured while she was still talking. Without this the
                # swallowed "stop" would resurface as the next command.
                stt_engine.clear_queue()

                _barge_in_metrics.update({
                    "detected_at_ms": now_ms(),
                    "spoken_at_ms": float(hit.get("start") or now_ms()),
                    "stop_call_ms": stop_latency,
                })
                print_info(
                    f"[BARGE-IN] speech->detection "
                    f"{(_barge_in_metrics['detected_at_ms'] - _barge_in_metrics['spoken_at_ms']):.0f}ms, "
                    f"detection->silence {stop_latency:.0f}ms"
                )
                set_state(STATE_LISTENING)

            # `hit` while NOT speaking is discarded here on purpose: there is nothing to
            # interrupt, and leaving the flag latched would fire a phantom barge-in the instant
            # the next response starts. The utterance still reaches the normal queue, where
            # Listen() decides what to do with it.

            time.sleep(0.06 if speaking else 0.2)
        except Exception:
            # A watcher crash must never take the assistant down or wedge playback.
            time.sleep(0.5)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            INPUT CAPTURE                               │
# └────────────────────────────────────────────────────────────────────────┘

def _is_self_echo(result):
    """
    True when a captured utterance overlaps a window in which the assistant's own voice was
    leaving the speakers.

    This replaces a text-similarity heuristic that compared the transcript against
    `tts.last_spoken_text`. That could not work with streamed speech: playback lags generation
    by several sentences, so the echo arriving at the microphone was of a sentence spoken much
    earlier than the one the comparison string held — similarity came out near zero and the
    echo was promoted to a user command.
    """
    if not (TTS_ENABLED and tts_engine is not None):
        return False
    return tts_engine.was_audible_between(result["start_ms"], result["end_ms"])


def Listen():
    """
    Captures one usable user utterance.

    Returns "" when nothing actionable was heard (echo rejected, or a bare interruption word
    that the audio layer has already handled and must NOT be routed to the DMM).
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
                    # The watcher normally beats us to this by ~800ms; this is the backstop for
                    # the case where only the finalized transcript matched.
                    print_system(f"[BARGE-IN] '{user_input}' — cancelling speech (finalized path).")
                    tts_engine.stop()
                    stt_engine.clear_queue()
                return ""

            # ── Echo gate ──
            # While the assistant is audible the microphone is dominated by her own voice, so
            # the only speech we trust is the interrupt vocabulary handled above.
            if spoken_over_tts:
                print_warning(f"[ECHO REJECTED] Ignoring own voice picked up by mic: '{user_input}'")
                continue

            return user_input

    return console.input("\n[bold cyan]User >[/bold cyan] ").strip()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SHUTDOWN SIGNAL HANDLER                         │
# └────────────────────────────────────────────────────────────────────────┘

def _force_shutdown(signum=None, frame=None):
    """
    Instant hard-shutdown handler registered for SIGINT (Ctrl+C), SIGTERM and SIGBREAK.

    Terminates the ChromeDriver/Chrome processes the STT engine created — identified by the
    PIDs it recorded at startup, never by process name. Name matching was actively dangerous
    here: `automation.windows.OpenApp` opens applications through AppOpener, which uses
    `subprocess.Popen`, so a Chrome window Kayra opened *for the user* is a child of this
    process and a name-based sweep would close the user's browsing session on exit.
    """
    # 0. Announce the shutdown before touching anything. Every background worker polls this
    #    flag, so the proactive agent stops producing candidates here rather than racing the
    #    teardown of the engine it would have spoken through.
    RUNTIME.shutdown_event.set()
    RUNTIME.set_state(AssistantState.SHUTTING_DOWN)

    # 0b. Cancel outstanding timers and any pending confirmation. Timers are the only
    #     long-lived resource the automation layer owns; an uncancelled one used to keep a
    #     sleeping thread alive and fire a message box after the assistant had exited.
    if shutdown_automation is not None:
        try:
            cancelled = shutdown_automation()
            if cancelled:
                print_info(f"Cancelled {cancelled} pending timer(s).")
        except BaseException:
            pass

    # 0c. Stop the proactive service and flush its habit counters. Done BEFORE the audio and
    #     browser teardown so it can never hand text to an engine that is being disposed.
    if proactive_agent is not None:
        try:
            proactive_agent.stop(timeout=2.0)
        except BaseException:
            pass

    # 1. Silence the speakers so nothing keeps talking through the teardown.
    if TTS_ENABLED and tts_engine is not None:
        try:
            tts_engine.shutdown()
        except BaseException:
            pass

    # 2. Clean Selenium driver session.
    if AUDIO_ENABLED and stt_engine:
        try:
            stt_engine.shutdown()
        except BaseException:
            pass

    # 3. Verify the browser processes Kayra owns are actually gone, force-killing by PID only.
    #    `stt_engine.shutdown()` already does this; this is the belt-and-braces pass for the
    #    case where shutdown() itself was interrupted.
    if AUDIO_ENABLED and stt_engine:
        try:
            survivors = stt_engine.terminate_owned_processes(timeout=3.0)
            if survivors:
                print_warning(f"Kayra-owned browser processes survived teardown: {sorted(survivors)}")
        except BaseException:
            pass

    print_system("System shutdown complete.")
    os._exit(0)


def _install_signal_handlers():
    """
    Registers the shutdown handler.

    SIGBREAK is Windows-only and is the only signal a parent process can actually deliver to a
    child console app there, so registering it is what makes an externally-triggered shutdown
    run this handler instead of being a hard kill that would leave the STT engine's Chrome
    processes orphaned.

    Signal handlers can only be installed from the main thread, so this is called from
    `bootstrap()` and guarded — importing the module from a worker must not raise.
    """
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        handler = getattr(signal, name, None)
        if handler is None:
            continue
        try:
            signal.signal(handler, _force_shutdown)
        except (ValueError, OSError):
            pass


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             TASK ROUTER                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Execute_Task(intent_array, original_query, mood=None):
    """
    Takes the parsed intent array from the DMM and routes it to the correct modules, then
    speaks whatever the handler returned.
    """
    automation_commands = []

    for task in intent_array:
        task_lower = task.strip().lower()

        # A barge-in mid-response cancels the rest of the turn: the user has moved on.
        if TTS_ENABLED and tts_engine is not None and tts_engine.interrupted:
            print_system("Turn cancelled by user interruption.")
            return

        # 1. Exit protocol
        if task_lower == "exit":
            print_system(f"Initiating shutdown sequence for {assistant_name}. Goodbye!")
            if TTS_ENABLED:
                # blocking=True: the process exits on the next line, so the farewell has to
                # finish playing before we tear the audio device down.
                await asyncio.to_thread(tts_engine.speak, "Shutting down. Goodbye.", True)
            _force_shutdown()

        # 2. Assistant self-control. This is NOT the audio interrupt: "stop" silences playback
        #    via the barge-in path and never reaches the DMM, whereas "stop proactive
        #    suggestions" arrives here and switches the subsystem off for the session without
        #    touching anything that is currently speaking.
        elif task_lower.startswith("proactive "):
            wants_on = task_lower.strip().endswith("on")
            if proactive_agent is None:
                reply = "The proactive service isn't running."
            else:
                proactive_agent.set_enabled(wants_on)
                reply = ("Proactive suggestions are back on."
                         if wants_on else "Alright, I'll keep quiet unless you ask.")
            print_system(reply)
            if TTS_ENABLED:
                tts_engine.speak(reply)

        # 3. General conversation (knowledge, math, logic)
        elif task_lower.startswith("general "):
            set_state(STATE_SPEAKING)
            await asyncio.to_thread(Chatbot, original_query,
                                    tts_engine if TTS_ENABLED else None, mood)

        # 4. Real-time web search (live RAG)
        elif task_lower.startswith("realtime "):
            set_state(STATE_SPEAKING)
            # The TTS engine is handed in so sentences are spoken as they stream out of the
            # model, instead of buffering the whole answer and speaking it afterwards.
            await asyncio.to_thread(RealTimeSearchEngine, original_query, mood,
                                    tts_engine if TTS_ENABLED else None)

        # 5. Autonomous deep research
        elif task_lower.startswith("deep research "):
            topic = task.replace("deep research", "", 1).strip()
            if TTS_ENABLED:
                tts_engine.speak("Initiating deep research protocol. This may take a few minutes.")

            await asyncio.to_thread(DeepResearchEngine, topic)

            if TTS_ENABLED and not tts_engine.interrupted:
                tts_engine.begin_turn()
                tts_engine.speak("Deep research complete. The report has been saved to your system.")

        # 6. Hardware & system automation (grouped)
        else:
            automation_commands.append(task.strip())

    # 7. Execute the grouped automation commands.
    #
    # Ordering, concurrency and safety all live inside the automation layer (`plan_actions` +
    # the policy), so this is a single dispatch rather than a gather of racing tasks.
    if automation_commands:
        print_info(f"Dispatching hardware automation tasks: {automation_commands}")
        # AUTOMATING is a busy state: nothing unprompted may be spoken while the assistant is
        # driving the user's desktop.
        set_state(STATE_AUTOMATING)
        try:
            spoken = await Automation(automation_commands)
        finally:
            set_state(STATE_PROCESSING)

        # Automation used to be silent to a voice user: results were printed and nothing was
        # ever said, so a failed or ambiguous action was indistinguishable from a successful
        # one. The layer now returns the sentence to say — including the question when it needs
        # to disambiguate or confirm.
        if spoken and isinstance(spoken, str) and TTS_ENABLED and not tts_engine.interrupted:
            tts_engine.speak(spoken)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             MASTER LOOP                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Main_Loop():
    """The infinite listening and routing loop."""
    print_banner(f"{assistant_name.upper()} SYSTEM ONLINE", "Master Orchestrator Node Active")

    if AUDIO_ENABLED:
        print_success("Microphone arrays hot. Continuous Web-Speech recognition ONLINE.")
        threading.Thread(target=_barge_in_watcher, daemon=True, name="kayra-barge-in").start()
        print_info("Barge-in watcher active — say \"stop\" or \"wait\" to interrupt at any time.")
    else:
        print_warning("SpeechToText module not detected. Defaulting to Keyboard Input Mode.")

    if BOOT is not None:
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
                os._exit(1)          # terminal died

            # 1. Capture input
            user_input = await asyncio.to_thread(Listen)
            if not user_input or not user_input.strip():
                continue

            if AUDIO_ENABLED:
                print_info(f"Transcribed Input: '{user_input}'")

            # A fresh turn: clears any latched interrupt from the previous response and starts
            # the command -> first-audible-word stopwatch.
            turn_t0 = time.perf_counter()
            if TTS_ENABLED:
                tts_engine.begin_turn()

            # ── Pending confirmation ──
            # Answered here, BEFORE the classifier, for two reasons. A bare "yes" sent to the
            # DMM comes back as 'general yes' and gets answered by the chatbot, so the
            # confirmation would never resolve; and the answer to "should I restart your
            # computer?" must not depend on a cloud round-trip. `resolve_confirmation` returns
            # handled=False for anything that is not actually an answer, so an unrelated
            # command spoken while a confirmation is pending still runs normally.
            if pending_confirmation and pending_confirmation():
                handled, reply = resolve_confirmation(user_input)
                if handled:
                    print_system(reply)
                    if TTS_ENABLED:
                        tts_engine.speak(reply)
                    RUNTIME.note_user_utterance()
                    set_state(STATE_LISTENING)
                    continue

            # Runtime bookkeeping. `note_user_utterance` is what keeps the proactive agent
            # quiet around live conversation, and the emitted event is how it learns habits and
            # reads the user's reaction to its last suggestion — this module does not need to
            # know that the agent exists.
            RUNTIME.note_user_utterance()
            RUNTIME.begin_turn()
            RUNTIME.emit("user_utterance", text=user_input)

            set_state(STATE_PROCESSING)

            # Emotion is estimated BEFORE routing but is handed only to the response
            # generators, never to the DMM: how the user sounds must not be able to change what
            # they asked for. The runtime supplies the one contextual signal the engine cannot
            # see for itself — whether they just cut the assistant off.
            detected_mood = None
            if EMOTION_ENABLED:
                try:
                    detected_mood = emotion_engine.analyze(
                        user_input,
                        seconds_since_interrupt=RUNTIME.seconds_since_interrupt(),
                    )
                except Exception as e:
                    # Mood is a nicety. A failure here must never cost the user their answer.
                    print_warning(f"Emotion analysis failed (non-fatal): {e}")
                    detected_mood = None

            # 2. Feed text into the Decision-Making Model
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

            RUNTIME.emit("intent_classified", text=user_input, tokens=list(dmm_commands))

            # 3. Dispatch to the execution router
            try:
                await Execute_Task(dmm_commands, user_input, detected_mood)
            finally:
                RUNTIME.end_turn()

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
            # Close the turn explicitly: a turn left latched open by a crash would read as
            # "user is mid-command" forever and mute the proactive agent for the session.
            RUNTIME.end_turn()
            set_state(STATE_LISTENING)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             ENTRY POINT                                │
# └────────────────────────────────────────────────────────────────────────┘

def main(argv=None):
    """
    Boots the assistant and runs it. Returns a process exit code.

    This is the single application entry point. `run.py` re-executes the interpreter and
    lands here; `python -m kayra` lands here; `python main.py` lands here through the
    compatibility shim at the repository root.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        bootstrap()
        asyncio.run(Main_Loop())
    except KeyboardInterrupt:
        print_system("System shutdown complete.")
        _force_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
