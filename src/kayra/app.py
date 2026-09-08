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
from kayra.core.conversation_context import get_conversation_context
from kayra.core.voice_control import ControlKind, classify_control
from kayra.core.voice_state import get_voice_state
from kayra.core import logbus
from kayra.core.logbus import Subsystem
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

# The transcript repair stage, built on first use. `False` means "tried and unavailable",
# which is distinct from `None` ("not built yet") so a failure is not retried per utterance.
_REPAIR_STAGE = None

# How long the audio-pipeline report waits for the microphone to be granted before saying it
# was not. Generous, because it runs on its own thread and delays nothing.
AUDIO_REPORT_TIMEOUT_S = 8.0

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

# Shutdown is idempotent and single-entry. Ctrl+C during teardown, a tray Quit that races the
# spoken "exit", and the UI button pressed twice all arrive here; the first caller runs the
# sequence and every later one returns immediately instead of tearing down a half-torn-down
# process. An Event rather than a bool because the losing callers have to be able to WAIT.
_shutdown_started = threading.Event()
_shutdown_lock = threading.Lock()
_pre_exit_hooks = []

# Proactive state to restore on wake. Standby switches the service off; waking must put it back
# the way the user had it, not switch it unconditionally on.
_proactive_before_sleep = None


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

# What the conversation is currently ABOUT, as opposed to what the assistant is DOING.
# A separate object from RUNTIME for the same reason listening is a separate axis from state:
# they answer different questions, and one of them is read on the recognition path by a stage
# that must never import the intelligence layer to get an answer.
CONTEXT = get_conversation_context()

set_state = RUNTIME.set_state

# The authoritative voice state. `RUNTIME` says what the assistant is DOING; this says what
# the user should be told about the microphone, resolved from every relevant fact at once.
VOICE = get_voice_state()


def get_state():
    return RUNTIME.state


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       VOICE STATE PUBLICATION                          │
# └────────────────────────────────────────────────────────────────────────┘
# THIS ORCHESTRATOR IS THE ONLY THING THAT FEEDS THE VOICE STATE MACHINE. Four producers each
# know one fact and none knows them all — the runtime bus knows the turn, the backend manager
# knows the session, the control watcher knows what the VAD hears, and `set_listening` /
# `set_sleeping` know what the user asked for. They report facts HERE; the machine resolves
# them; everything downstream renders the answer.
#
# It lives in `app.py` rather than in `core` because it is wiring: `core.voice_state` is a leaf
# that imports only the stdlib, and it must stay one so a UI can import it without dragging in
# the STT engine.

def _voice_facts(**facts):
    """
    Hands facts to the voice state machine and logs any transition it commits.

    THE TRANSITION LOG LIVES HERE AND NOWHERE ELSE. One line per committed change, at INFO —
    not per animation frame, not per VAD sample, and not again in the UI. `update()` returns
    None when nothing changed, which is what keeps a 5Hz VAD poll from producing 5 lines a
    second.
    """
    try:
        transition = VOICE.update(**facts)
    except Exception:
        return None
    if transition is not None:
        logbus.info(Subsystem.VOICE,
                    f"State: {transition.previous} -> {transition.state}",
                    correlate=False)
    return transition


def _refresh_voice_state(**extra):
    """Re-resolves the voice state from everything currently observable."""
    backend_status = "OFF"
    try:
        from kayra.input.stt_backend import get_stt_backend_manager
        backend_status = get_stt_backend_manager().snapshot().status
    except Exception:
        pass
    facts = {
        "assistant_state": RUNTIME.state,
        "listening": RUNTIME.listening,
        "sleeping": RUNTIME.sleeping,
        "shutting_down": RUNTIME.shutdown_event.is_set(),
        "voice_available": bool(AUDIO_ENABLED and stt_engine is not None),
        "backend_status": backend_status,
    }
    facts.update(extra)
    return _voice_facts(**facts)


def _on_runtime_voice_event(event, payload):
    """
    Runtime-bus subscriber that keeps the voice state in step with the turn machine.

    Deliberately narrow: it reads the payload it was given and re-resolves. It must return
    fast and must never raise — `emit()` fans out synchronously from the main loop and from
    the control watcher, and a slow subscriber would stall both.
    """
    try:
        if event == "state_changed":
            _refresh_voice_state(assistant_state=payload.get("state", "IDLE"))
        elif event == "listening_changed":
            _refresh_voice_state(listening=bool(payload.get("listening", True)),
                                 voice_active=False)
        elif event == "sleeping_changed":
            _refresh_voice_state(sleeping=bool(payload.get("sleeping", False)))
        elif event == "barge_in":
            # RE-RESOLVE, but do NOT assert that a voice is present.
            #
            # `barge_in` is emitted by every path that silences speech, including ones with no
            # user in them at all: entering standby and starting a shutdown both cancel
            # playback and both emit it. Setting `voice_active=True` here latched that fact
            # with nothing to clear it, and the assistant visual then read "Listening…" — the
            # user is speaking — for the rest of the session with the room silent. Caught in
            # the live boot test.
            #
            # A REAL spoken barge-in is already covered twice over without this: the control
            # watcher publishes the VAD sample that produced it, and the turn machine's
            # INTERRUPTING state resolves to USER_SPEAKING on its own.
            _refresh_voice_state()
    except Exception:
        pass


def _on_backend_changed(_state):
    """Backend manager subscriber: a session transition is a voice-state fact like any other."""
    try:
        _refresh_voice_state()
    except Exception:
        pass


def voice_runtime_state():
    """
    Everything a presentation layer needs to render the assistant's voice presence, in one
    read: the state, its revision, the facts behind it and the live backend.

    THE UI CONSUMES THIS AND INFERS NOTHING. It is deliberately a single call rather than a
    set of getters, because the bug this replaced was four surfaces each reading a different
    subset and disagreeing about the rest.
    """
    snapshot = VOICE.snapshot()
    try:
        from kayra.input.stt_backend import get_stt_backend_manager
        backend = get_stt_backend_manager().snapshot().to_dict()
    except Exception:
        backend = {}
    snapshot["stt_backend"] = backend.get("active_backend")
    snapshot["stt_backend_label"] = backend.get("active_label", "None")
    snapshot["stt_requested"] = backend.get("requested_backend")
    snapshot["stt_status"] = backend.get("status", snapshot.get("stt_status"))
    snapshot["last_error"] = backend.get("last_error", "")
    snapshot["last_transition"] = snapshot.get("seconds_in_state")
    # The microphone fact travels with the rest of the voice picture, and is accompanied by
    # whether it is KNOWN. A presentation layer that reads them separately can be handed
    # "not listening" when the honest answer is "not booted yet" — see `listening_known`.
    snapshot["listening"] = bool(snapshot.get("facts", {}).get("listening", True))
    snapshot["listening_known"] = True
    return snapshot


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
        # Hand the live session to the backend manager, which from here on is the ONE place
        # that answers "which backend was requested, which is active, and how did that
        # happen?". Adoption seeds `requested` from configuration so the very first snapshot
        # is truthful rather than reporting `auto` while `.env` says `chrome`.
        try:
            from kayra.input.stt_backend import get_stt_backend_manager
            manager = get_stt_backend_manager()
            manager.subscribe(_on_backend_changed)
            manager.adopt(requested=env_values().get("STT_BROWSER", "auto"), source="env")
        except Exception as exc:
            print_warning(f"Speech backend state unavailable: {exc}")
        # Reported from a short-lived daemon thread rather than inline. `getUserMedia` is
        # ASYNCHRONOUS: the session is up before the device is granted, so reporting here
        # printed "microphone settings unavailable" on every single boot — a false alarm
        # about the one subsystem whose warnings need to be trustworthy. Waiting inline
        # instead would put the delay straight into cold start, which is the other thing
        # this boot path is not allowed to do.
        threading.Thread(target=_report_audio_pipeline, daemon=True,
                         name="kayra-audio-report").start()
    except Exception as e:
        _boot_errors.append(("Speech-to-Text", e))


def _report_audio_pipeline():
    """
    Prints what the capture pipeline ACTUALLY got, read back from the live microphone track.

    Constraints are a request, not a promise. Echo cancellation, noise suppression and gain
    control are asked for; whether the browser and the device grant them depends on the driver
    and the device, and an assistant that assumes it got them will misdescribe the one thing
    that explains its mistakes. This is the same rule the speech-device card follows for the
    ONNX provider: report the session that exists, never the one that was requested.

    Diagnostics only — nothing here is load-bearing, and a failure to read it is not an error.
    """
    # The device is granted asynchronously, so poll briefly rather than reading once. A
    # refusal is reported the moment it is known; a grant usually lands well inside a second.
    report, settings = {}, {}
    deadline = time.time() + AUDIO_REPORT_TIMEOUT_S
    while time.time() < deadline:
        try:
            report = stt_engine.audio_pipeline_report()
        except Exception:
            return
        settings = report.get("settings") or {}
        if settings or report.get("error"):
            break
        time.sleep(0.25)

    if not settings:
        detail = report.get("error") or "microphone settings unavailable"
        logbus.warning(Subsystem.STT,
                       f"Capture: {detail}. Echo rejection falls back to the capture "
                       f"timestamp gate alone.")
        return

    granted = [name for name, key in (("echo cancellation", "echoCancellation"),
                                      ("noise suppression", "noiseSuppression"),
                                      ("gain control", "autoGainControl"))
               if settings.get(key)]
    missing = [name for name, key in (("echo cancellation", "echoCancellation"),
                                      ("noise suppression", "noiseSuppression"),
                                      ("gain control", "autoGainControl"))
               if not settings.get(key)]
    rate = settings.get("sampleRate")
    channels = settings.get("channelCount")
    # WHAT WAS GRANTED is stated once, by the startup report, which reads the same values
    # from the same place. This thread only owns what was REFUSED — the part the startup
    # report cannot know it should mention, and the part that actually changes what the user
    # should expect from the assistant.
    logbus.debug(Subsystem.STT,
                 "capture: " + (", ".join(granted) or "no processing") +
                 (f" @ {rate} Hz" if rate else "") +
                 (f", {channels}ch" if channels else ""))
    if missing:
        logbus.warning(Subsystem.STT,
                       "Capture did NOT get: " + ", ".join(missing) +
                       ". Recognition accuracy while Kayra is speaking will be lower.")


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

    # Third-party loggers down to WARNING before anything can start chattering. Never
    # disabled and never raised past WARNING — a real Selenium or SDK failure still reaches
    # the terminal; what is suppressed is urllib3 announcing every WebDriver connection at
    # 17Hz while the control watcher polls.
    logbus.quiet_third_party()

    # Subscribed BEFORE the engines come up, so no transition is missed while they boot.
    RUNTIME.subscribe(_on_runtime_voice_event)

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
    # NOT `engine.run_boot_sequence()`. That prints the same provider block the startup
    # report prints at the end of `bootstrap`, and two copies of one fact is the duplication
    # section 13.21 exists to remove. The method is kept for standalone diagnostics that boot
    # the engine alone and want the routing narrated.
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

    _report_startup()
    _install_signal_handlers()


def _report_startup():
    """
    The startup summary: one short block per subsystem, in a fixed order.

    WHY A REPORT RATHER THAN THE LINES EACH SUBSYSTEM ALREADY PRINTS. Boot is where the
    terminal is least readable, because half a dozen subsystems come up on overlapping threads
    and interleave their output. This runs AFTER all of them, on one thread, and states the
    facts a person actually needs: which speech backend, which speech device, which model
    routing, how much memory, and whether presence is on. It answers "what is Kayra running
    with?" without requiring anyone to reconstruct it from the order things happened to print.

    Every value is READ FROM THE LIVE SUBSYSTEM, never from configuration. That is the same
    rule the speech-device card follows and it is the whole point: a boot report that recites
    `.env` back would say "GPU" on a machine where synthesis is running on the processor.
    """
    from kayra.core.logbus import field
    from kayra.intelligence.provider_router import ROUTE_CHAT, ROUTE_DECISION

    logbus.section(Subsystem.BOOT, "Kayra is ready")

    # ── Models ──
    try:
        logbus.info(Subsystem.LLM, "Providers", correlate=False)
        if getattr(engine, "is_online", False):
            field(Subsystem.LLM, "DMM", engine.router.describe(ROUTE_DECISION))
            field(Subsystem.LLM, "Chat", engine.router.describe(ROUTE_CHAT))
        else:
            field(Subsystem.LLM, "DMM", f"Local ({engine.local_decision_model})")
            field(Subsystem.LLM, "Chat", f"Local ({engine.local_chat_model})")
    except Exception:
        logbus.warning(Subsystem.LLM, "Model routing unavailable")

    # ── Speech output ──
    if TTS_ENABLED and tts_engine is not None:
        try:
            report = tts_engine.device_status.to_dict()
            field(Subsystem.TTS, "Provider", report.get("provider", "unknown"))
            field(Subsystem.TTS, "Device", report.get("device", "unknown"))
            if report.get("fallback"):
                # "Asked for a GPU and got the CPU" is the one thing about this subsystem a
                # user must not have to discover later.
                logbus.warning(Subsystem.TTS,
                               f"Fell back to {report.get('device')}: {report.get('reason', '')}")
        except Exception:
            field(Subsystem.TTS, "Provider", "unknown")
    else:
        logbus.warning(Subsystem.TTS, "Speech output unavailable — Kayra will run muted")

    # ── Speech input ──
    if AUDIO_ENABLED and stt_engine is not None:
        try:
            backend = stt_backend_state()
            field(Subsystem.STT, "Backend", backend.get("active_label", "unknown"))
            if not backend.get("matches"):
                logbus.warning(Subsystem.STT,
                               f"Requested {backend.get('requested_label')} but "
                               f"{backend.get('active_label')} is active")
        except Exception:
            pass
        try:
            pipeline = stt_engine.audio_pipeline_report() or {}
            settings = pipeline.get("settings") or {}
            if settings.get("label"):
                field(Subsystem.STT, "Microphone", settings["label"])
            # Constraints are a REQUEST, not a promise. Reporting what the browser actually
            # granted is the same rule the speech-device card follows for the ONNX provider:
            # an assistant that claims echo cancellation it never got is misdescribing the one
            # thing that explains its mistakes.
            for label, key in (("AEC", "echoCancellation"),
                               ("Noise supp.", "noiseSuppression")):
                if key in settings:
                    field(Subsystem.STT, label, "ON" if settings[key] else "OFF")
            vad = pipeline.get("vad") or {}
            field(Subsystem.STT, "VAD", "READY" if vad.get("ready") else "unavailable")
        except Exception:
            pass
    else:
        logbus.warning(Subsystem.STT, "Speech input unavailable — keyboard input only")

    # ── Memory ──
    try:
        from kayra.memory.store import report_loaded
        report_loaded()
    except Exception:
        pass

    # ── Presence ──
    try:
        presence = presence_engine()
        if presence is not None:
            field(Subsystem.PRESENCE, "Contextual",
                  "enabled" if getattr(presence, "enabled", False) else "disabled")
    except Exception:
        pass

    logbus.section_end()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     LOCAL CONTROL WATCHER THREAD                       │
# └────────────────────────────────────────────────────────────────────────┘
# The fast path for everything the user says ABOUT Kayra rather than TO it. This thread is the
# reason those commands work at all while a response is in flight: `Main_Loop` is blocked
# inside `Execute_Task` for the whole of a generated answer and cannot poll the microphone.
#
#   interim STT result -> local control check -> act
#                                     |
#                                     +-- nothing matched -> the utterance stays in the queue
#                                         and reaches the DMM through Listen() as normal
#
# No LLM call, no network, no classification. See `core.voice_control` for the vocabulary and
# for why the matching is exact.


def _dispatch_control(kind, text="", source="voice"):
    """
    Executes one local control command. Returns True if it was handled.

    Every caller -- the watcher below, `Listen()`, the UI -- routes through this one function,
    so a spoken "stop listening" and a clicked pause button cannot drift into two behaviours.
    """
    if kind == ControlKind.INTERRUPT:
        return _interrupt_speech(text)

    if kind == ControlKind.SHUTDOWN:
        print_system(f"[CONTROL] '{(text or '').strip()}' -- shutting {assistant_name} down.")
        request_shutdown(reason=f"{source}: {(text or '').strip()}", farewell=True)
        return True

    if kind == ControlKind.PAUSE_LISTENING:
        # Silence first, then close the microphone. A user who says "stop listening" over a
        # running answer wants both, and doing it the other way round would leave a sentence
        # playing to a room where Kayra can no longer be told to stop it.
        _interrupt_speech(text, announce=False)
        _confirm(set_listening(False))
        return True

    if kind == ControlKind.RESUME_LISTENING:
        _confirm(set_listening(True))
        return True

    if kind == ControlKind.SLEEP:
        _confirm(set_sleeping(True))
        return True

    if kind == ControlKind.WAKE:
        _confirm(set_sleeping(False))
        return True

    return False


def _confirm(reply):
    """
    Speaks a one-line confirmation for a control command. NEVER blocking.

    THE BLOCKING VERSION WAS A REAL BUG, caught by measurement rather than by reading. This runs
    on the local control watcher, and `speak(..., blocking=True)` parks that thread until the
    sentence has finished PLAYING — measured at ~5s for "Okay, listening is paused...". For
    those five seconds the watcher polls nothing, so a "stop" or an "exit" spoken straight after
    "stop listening" was not noticed at all. The thread whose entire purpose is to stay
    responsive must never wait on audio.

    Nothing is lost by not blocking: closing the microphone does not silence the speaker, so the
    confirmation plays out in full either way. The one place a blocking farewell is genuinely
    required is `request_shutdown`, where the audio device is disposed moments later — and it
    blocks there, on the caller's thread, by design.

    `begin_background_utterance()` clears the `_interrupted` latch that `_interrupt_speech` just
    set, which is what lets this sentence through. It is safe here and only here: the epoch has
    already moved, so a response stream cancelled a moment ago stays cancelled (it tests
    `is_cancelled(token)`, not the latch), and there is no generator still feeding the queue.
    """
    if not (reply and TTS_ENABLED and tts_engine is not None):
        return
    tts_engine.begin_background_utterance()
    tts_engine.speak(reply)


def _interrupt_speech(text="", announce=True):
    """
    The barge-in itself: cancel the epoch, drop the recognizer's buffer, hand the floor back.

    Kept separate from `_dispatch_control` because three other things call it -- the finalized
    backstop in `Listen()`, the UI's stop button, and the pause-listening branch above -- and
    each needs exactly this and nothing else.
    """
    if not (TTS_ENABLED and tts_engine is not None):
        return False

    t_detect = time.perf_counter()
    set_state(STATE_INTERRUPTING)
    if announce:
        print_system(f"[BARGE-IN] '{(text or '').strip()}' -- cancelling speech.")

    tts_engine.stop()
    stop_latency = (time.perf_counter() - t_detect) * 1000.0

    # The user has the floor. Everything that decides whether an unprompted line may be spoken
    # keys off this timestamp, and a proactive suggestion that was mid-flight is cancelled by
    # exactly this same path -- there is no separate interruption mechanism for proactive
    # speech.
    RUNTIME.note_interrupt()
    RUNTIME.emit("barge_in", text=text or "")

    # Drop everything the recognizer buffered up to this point: the interrupt word itself, plus
    # the echo it is glued to. Without this the swallowed "stop" resurfaces as the user's next
    # command one VAD window later.
    if stt_engine is not None:
        try:
            stt_engine.clear_queue()
        except Exception:
            pass

    _barge_in_metrics["stop_call_ms"] = stop_latency
    set_state(STATE_LISTENING)
    return True


def _local_control_watcher():
    """
    Polls the STT page for control commands and acts on them, independently of the main loop.

    The page flags both kinds from INTERIM recognition results, so this fires roughly a VAD
    window (~800ms) earlier than a finalized transcript would, and without the translation
    round-trip.

    THE SPEAKING FLAG IS PUBLISHED FROM HERE, in the same round-trip that reads the flags back.
    That is not an optimisation detail, it is what makes "stop" reliable: the page only
    tail-matches the interrupt vocabulary while Kayra is audible, and this loop is the only
    thing that knows, at 17Hz, whether she is.
    """
    while True:
        try:
            if stt_engine is None:
                time.sleep(0.5)
                continue

            speaking = bool(TTS_ENABLED and tts_engine is not None and tts_engine.is_playing)

            poll = getattr(stt_engine, "poll_controls", None)
            if poll is not None:
                hit, control = poll(speaking=speaking)
            else:                     # pragma: no cover - an engine older than this change
                hit, control = stt_engine.poll_interrupt(), None

            # Lifecycle commands are acted on whatever Kayra is doing. They are the two
            # categories -- silence yourself, end yourself -- that never need protecting from.
            if control:
                _dispatch_control(str(control.get("kind") or ""),
                                  str(control.get("text") or ""))

            if hit and speaking:
                spoken_at = float(hit.get("start") or now_ms())
                _interrupt_speech(hit.get("text", ""))
                _barge_in_metrics["detected_at_ms"] = now_ms()
                _barge_in_metrics["spoken_at_ms"] = spoken_at
                print_info(
                    f"[BARGE-IN] speech->detection "
                    f"{(_barge_in_metrics['detected_at_ms'] - spoken_at):.0f}ms, "
                    f"detection->silence {_barge_in_metrics.get('stop_call_ms', 0.0):.0f}ms"
                )

            # `hit` while NOT speaking is discarded here on purpose: there is nothing to
            # interrupt, and leaving the flag latched would fire a phantom barge-in the instant
            # the next response starts. The utterance still reaches the normal queue, where
            # Listen() decides what to do with it.

            # VOICE ACTIVITY, FROM THE POLL THAT ALREADY HAPPENED. `poll_controls` reads
            # `window.kayraVad.voice` in the same round-trip it uses for the interrupt flags,
            # so this costs nothing beyond a dict read — and it is what lets the assistant
            # visual show "I am hearing you right now" without a second observation, a second
            # thread or a second Selenium command over the driver lock.
            _refresh_voice_state(voice_active=bool(getattr(stt_engine, "voice_active", False)))

            time.sleep(0.06 if speaking else 0.15)
        except Exception:
            # A watcher crash must never take the assistant down or wedge playback.
            time.sleep(0.5)


# The watcher answered to this name for its whole life and `ui.session` starts it by it. Kept
# as an alias rather than renamed at the call sites, because a rename here would be a silent
# behavioural change in a front end that would still import cleanly.
_barge_in_watcher = _local_control_watcher


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


def set_listening(enabled, announce=True):
    """
    Opens or closes the microphone WITHOUT touching anything else Kayra is running.

    This is not a shutdown and it is not a barge-in. Nothing here stops the TTS engine, the
    proactive agent, the automation layer, the model clients or the STT browser session — the
    session stays up so resuming costs a script call instead of a 2.5s rebuild.

    Returns the sentence to say, or "" when nothing changed. The caller decides whether to
    speak it, because this is called from the DMM dispatch (which speaks) and from the UI
    (which does not, and shows the state instead).

    ASYMMETRY IS DELIBERATE. "Stop listening" can be spoken; "start listening" cannot, because
    a paused microphone by definition cannot hear the command to un-pause. Resuming is a
    manual action from the UI or the tray, and the vocabulary reflects that — there is no
    `start listening` DMM token.
    """
    enabled = bool(enabled)
    runtime = get_runtime_state()

    if AUDIO_ENABLED and stt_engine is not None:
        try:
            if enabled:
                stt_engine.resume_listening()
            else:
                stt_engine.pause_listening()
        except Exception as exc:
            print_warning(f"Could not change the microphone state: {exc}")

    changed = runtime.set_listening(enabled)
    if not changed:
        return ""

    if enabled:
        reply = "Listening again."
        print_success("[LISTENING] Microphone open.")
    else:
        reply = "Okay, listening is paused. Use the window to start it again."
        print_system("[LISTENING] Microphone paused — Kayra is still running.")
    return reply if announce else ""


def set_stt_backend(backend, source="settings"):
    """
    Changes which browser runs speech recognition, on the LIVE session. Returns (ok, detail).

    THE ONE ENTRY POINT, for the same reason `set_listening` is: the Settings dropdown, a
    future spoken command and any test all have to produce identical state, and two
    implementations of a browser swap would mean two answers to "which backend is active".

    It is a THIN pass-through on purpose. The backend manager owns the transaction (request,
    stop, start, verify, publish) and the STT engine owns the safety of the swap (teardown
    before rebuild, PID-scoped reaping, restore-on-failure). Sequencing any of that here would
    be a second implementation of the one operation that must not have two — exactly the rule
    already applied to `set_tts_device`.

    THE CURRENT STATE IS PRESERVED ACROSS THE SWITCH. A backend change is not a pause, not a
    barge-in, not a wake and not a shutdown: if listening was paused it stays paused, if Kayra
    was asleep it stays asleep. What it does interrupt is playback, and only when it must —
    the browser session being torn down is the one holding the microphone that barge-in
    depends on, so a half-swapped session must not be left able to hear "stop".
    """
    from kayra.core.settings_log import get_settings_recorder
    from kayra.input.stt_backend import get_stt_backend_manager, normalize, label_for

    manager = get_stt_backend_manager()
    target = normalize(backend)
    before = manager.snapshot()

    # RECOVERING while the swap runs, never PAUSED. A backend switch is a session transition,
    # and showing "Listening paused" during one would be the same false pause an STT recovery
    # used to produce.
    _refresh_voice_state(backend_status="STARTING", voice_active=False)

    recorder = get_settings_recorder()
    committed, detail = recorder.apply(
        "STT_BROWSER", target,
        runtime=lambda: manager.request(target, source=source),
        old_value=before.requested_backend,
        subsystem=Subsystem.SETTINGS,
        # So the line reads `Automatic -> Google Chrome` rather than `auto -> chrome`. The
        # names live in `stt_backend`; the recorder is a leaf in `core` and must not import
        # them, so the caller — which already has them — hands the resolver over.
        label_value=label_for,
    )

    _refresh_voice_state()
    return committed, (detail or label_for(target))


def stt_backend_state():
    """The requested/active speech backend, as a plain dict. Never boots an engine."""
    from kayra.input.stt_backend import get_stt_backend_manager
    return get_stt_backend_manager().snapshot().to_dict()


def listening_enabled():
    """Whether the microphone is currently open. One source of truth, read by every surface."""
    try:
        return bool(get_runtime_state().listening)
    except Exception:
        return True


def set_sleeping(enabled, announce=True):
    """
    Puts Kayra into standby, or brings it back. Returns the sentence to say, or "".

    WHAT STANDBY ACTUALLY IS, AND THE ONE THING IT DELIBERATELY IS NOT
    ------------------------------------------------------------------
    Sleeping stops Kayra DOING things. It silences whatever is being spoken, switches the
    proactive service off, and makes the listen loop discard every utterance that is not a
    control command -- so no emotion analysis, no DMM call, no cloud round-trip, no automation.
    That is the whole cost of the assistant, gone, for the price of one dictionary probe per
    utterance.

    It does NOT close the microphone, and that is a decision rather than an oversight. "Wake
    up" is a spoken command; a closed microphone cannot hear it. The two requirements --
    "standby releases the microphone" and "you can wake Kayra by speaking to it" -- are
    mutually exclusive, and between them the one that makes standby useful is the second.
    A user who genuinely wants the microphone released has a separate command for exactly
    that: "stop listening", which does close it and which is undone from the window, the tray
    or Ctrl+M (see `set_listening` for why THAT asymmetry is unavoidable).

    So the two are orthogonal and composable:

        sleeping   -> Kayra hears you, and ignores everything but "wake up"
        listening  -> whether Kayra hears you at all

    Standby is also not a value of `RuntimeState.state`. Overloading it there would make
    "asleep" mutually exclusive with SPEAKING, which is wrong in both directions -- exactly the
    argument that already keeps `listening` on its own axis.
    """
    global _proactive_before_sleep

    enabled = bool(enabled)
    runtime = get_runtime_state()

    changed = runtime.set_sleeping(enabled)

    if enabled:
        # Silence, AFTER the standby flag is set rather than before it.
        #
        # The order matters only to the assistant visual, and only for a few microseconds —
        # but that is the flicker requirement 29 forbids. `_interrupt_speech` moves the turn
        # machine to INTERRUPTING, which the voice state machine resolves to USER_SPEAKING
        # ("the user has the floor"). Silencing first therefore rendered a phantom
        # LISTENING -> USER_SPEAKING -> STANDBY on the way into standby, with nobody talking.
        # With the flag already set, standby outranks the turn machine and the visual goes
        # straight to STANDBY. Nothing about the SILENCING changes: it still happens before
        # this function returns, so Kayra cannot fall asleep mid-sentence and keep talking.
        _interrupt_speech("sleep", announce=False)

    if not changed:
        return ""

    # The proactive service is the only thing that speaks unprompted, so standby has to switch
    # it off -- and waking has to restore what the user had, not switch it unconditionally on.
    if proactive_agent is not None:
        try:
            if enabled:
                _proactive_before_sleep = bool(proactive_agent.enabled)
                proactive_agent.set_enabled(False)
            else:
                if _proactive_before_sleep is not None:
                    proactive_agent.set_enabled(_proactive_before_sleep)
                _proactive_before_sleep = None
        except Exception as exc:
            print_warning(f"Could not change the proactive service while sleeping: {exc}")

    if enabled:
        reply = "Going to sleep. Say wake up when you need me."
        print_system("[STANDBY] Asleep - listening only for a wake word.")
    else:
        reply = "I'm awake."
        print_success("[STANDBY] Awake.")
    return reply if announce else ""


def sleeping():
    """Whether the assistant is in standby. One source of truth, read by every surface."""
    try:
        return bool(get_runtime_state().sleeping)
    except Exception:
        return False


def _repair_stage():
    """
    The process's transcript repair stage, built lazily with this installation's vocabulary.

    The classifier's task headers are INJECTED rather than imported by the input layer: that
    keeps `kayra.input` from reaching into `kayra.intelligence` on the recognition path, and
    it means the words the repair stage considers plausible are exactly the words this
    assistant can actually act on.
    """
    global _REPAIR_STAGE
    if _REPAIR_STAGE is None:
        try:
            from kayra.input.transcript_repair import get_transcript_repair
            vocabulary = []
            try:
                vocabulary = list(getattr(engine, "funcs", ()) or ())
            except Exception:
                vocabulary = []
            _REPAIR_STAGE = get_transcript_repair(vocabulary=vocabulary)
        except Exception as exc:
            print_warning(f"Transcript repair unavailable (non-fatal): {exc}")
            _REPAIR_STAGE = False          # False, not None: do not retry every utterance
    return _REPAIR_STAGE or None


def _repair_transcript(result):
    """
    Runs the LAST stage of the capture pipeline and returns the text to act on.

    Everything before this — the microphone constraints, echo cancellation, the voice-activity
    endpointer, the recognizer itself — has already happened inside the browser. This stage
    only chooses between readings the recognizer offered, using what the conversation is
    currently about. It changes nothing for the overwhelming majority of utterances, and when
    it does change something it says so on the console, because an invisible correction layer
    is worse than none.
    """
    text = (result.get("text") or "").strip()
    stage = _repair_stage()
    if stage is None or not text:
        return text
    try:
        alternatives = result.get("alternatives") or []
        confidence = None
        if alternatives:
            confidence = alternatives[0].get("confidence")
        outcome = stage.repair(text, alternatives=alternatives, confidence=confidence,
                               uncommitted=bool(result.get("uncommitted")))
    except Exception as exc:
        # A repair failure must never cost the user their command.
        print_warning(f"Transcript repair failed (non-fatal): {exc}")
        return text
    if outcome.changed:
        print_info(f"[TRANSCRIPT] '{outcome.original}' -> '{outcome.text}' "
                   f"({outcome.reason})")
    return outcome.text


def Listen():
    """
    Captures one usable user utterance.

    Returns "" when nothing actionable was heard (echo rejected, or a bare interruption word
    that the audio layer has already handled and must NOT be routed to the DMM).
    """
    if AUDIO_ENABLED and stt_engine is not None:
        # Paused: report nothing heard rather than blocking. The console loop and the UI
        # listener both wait between calls, so a paused microphone costs no polling.
        if not listening_enabled():
            time.sleep(0.2)
            return ""

        set_state(STATE_LISTENING)

        while True:
            result = stt_engine.capture()
            if result is None:
                return ""

            # ── STAGE 5: conservative, context-aware repair ──
            # Ordered here deliberately: after the recognizer, before anything acts on the
            # words. The four stages before it (capture, echo cancellation, voice-activity
            # endpointing, recognition) all live in the browser page; this one lives here
            # because it is the only one that needs to know what the conversation is about.
            user_input = _repair_transcript(result)
            if not user_input:
                return ""

            spoken_over_tts = _is_self_echo(result)

            # ── LOCAL CONTROL INTERPRETER ──
            # Runs BEFORE the echo gate and before the DMM. Before the DMM because none of
            # these commands should cost a cloud round-trip and several of them have to work
            # with the network down; before the echo gate because "exit" and "stop" spoken over
            # a running answer are exactly the cases that matter most, and the echo gate would
            # discard them.
            #
            # The watcher normally beats this path by ~800ms (it reads INTERIM results). This
            # is the backstop for the case where only the finalized transcript matched -- a
            # short utterance the recognizer never emitted an interim result for, or a moment
            # when the driver lock was busy.
            control = classify_control(user_input)
            if control is not None:
                if control.kind == ControlKind.INTERRUPT:
                    if TTS_ENABLED and tts_engine is not None and tts_engine.is_playing:
                        print_system(f"[BARGE-IN] '{user_input}' — cancelling speech "
                                     f"(finalized path).")
                        _interrupt_speech(user_input, announce=False)
                    return ""
                _dispatch_control(control.kind, user_input)
                return ""

            # ── Standby ──
            # Asleep, nothing but a control command is acted on -- and every control command
            # was already handled above. Discarding here rather than in the caller is what
            # makes standby genuinely cheap: no emotion analysis, no DMM call, no network.
            if sleeping():
                print_info(f"[STANDBY] Ignoring '{user_input}' — say \"wake up\" first.")
                continue

            # ── Echo gate ──
            # While the assistant is audible the microphone is dominated by her own voice, so
            # the only speech we trust is the control vocabulary handled above.
            if spoken_over_tts:
                print_warning(f"[ECHO REJECTED] Ignoring own voice picked up by mic: '{user_input}'")
                continue

            return user_input

    return console.input("\n[bold cyan]User >[/bold cyan] ").strip()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SHUTDOWN SIGNAL HANDLER                         │
# └────────────────────────────────────────────────────────────────────────┘

def on_before_exit(hook):
    """
    Registers a callable to run just before the process ends, after every subsystem is down.

    This exists for exactly one caller: the desktop UI, which has a window and a tray icon to
    take off the screen. It is NOT an extension point for cleanup — every resource Kayra owns
    is released by the sequence below, in an order that matters. A hook that raises, or that
    takes longer than `HOOK_TIMEOUT`, is abandoned rather than allowed to hold the exit open.
    """
    if callable(hook) and hook not in _pre_exit_hooks:
        _pre_exit_hooks.append(hook)
    return hook


def shutdown_requested():
    """True once shutdown has begun. Read by anything that must stop producing work."""
    return _shutdown_started.is_set()


def request_shutdown(reason="", farewell=False, exit_code=0):
    """
    THE authoritative shutdown. Every source ends up here: the spoken "exit", the UI button,
    the tray's Quit, Ctrl+C, SIGTERM/SIGBREAK, `Execute_Task`'s exit token, and a fatal loop
    failure. There is deliberately no second teardown anywhere in the codebase — the ordering
    below is load-bearing and a duplicate would drift out of step with it.

    IDEMPOTENT. Ctrl+C pressed during teardown, a tray Quit racing a spoken "exit", or the UI
    button clicked twice all arrive here concurrently. The first caller runs the sequence; every
    later one returns immediately rather than tearing down an already half-torn-down process
    (which is how a second pass used to reach a disposed audio device and hang).

    THE ORDER, AND WHY IT IS THIS ORDER
    -----------------------------------
      1. announce      — every background worker polls `shutdown_event`, so the proactive agent
                         stops PRODUCING candidates here, before the engine it would speak
                         through is disposed.
      2. reject work   — SHUTTING_DOWN is a busy state; nothing unprompted may be spoken in it.
      3. proactive     — stopped first among the services, for the reason in (1).
      4. timers        — the only long-lived resource the automation layer owns. An uncancelled
                         one used to fire a toast after the assistant had already exited.
      5. speech        — silence the speakers so nothing keeps talking through the teardown.
      6. microphone    — the browser session, torn down by PID.
      7. verify        — belt and braces for the case where (6) was itself interrupted.
      8. UI hooks      — the window and tray come off the screen last, so the user sees Kayra
                         disappear only once it genuinely has.

    THE FINAL `os._exit`. It is the last statement, reached only after every step above has
    run — not a shortcut past cleanup. It is here because the process is full of daemon threads
    parked in native code (PortAudio's callback, urllib3 sockets inside Selenium, ONNX Runtime's
    intra-op pool) that a normal interpreter exit has to join or unwind, and several of them do
    not come back promptly. Returning from `main()` instead reliably added seconds to a quit
    the user had already asked for, and occasionally hung outright.
    """
    HOOK_TIMEOUT = 2.0

    with _shutdown_lock:
        first = not _shutdown_started.is_set()
        if first:
            _shutdown_started.set()

    if not first:
        # A second caller must not run the sequence again. Park briefly so it does not return
        # into code that assumes a live engine, then get out of the way.
        time.sleep(HOOK_TIMEOUT)
        return

    if reason:
        print_system(f"Shutdown requested ({reason}).")

    # 1-2. Announce, and stop accepting work.
    RUNTIME.shutdown_event.set()
    RUNTIME.set_state(AssistantState.SHUTTING_DOWN)
    # STOPPING is an ABSORBING state in the voice machine: once it is entered, no fact — not a
    # late VAD sample from a watcher thread that has not noticed yet, not a backend
    # notification from a session being reaped — can put the assistant visual back to
    # LISTENING. That is the guarantee, and this is where it starts.
    _refresh_voice_state(shutting_down=True, voice_active=False)

    # A farewell is spoken BEFORE anything is disposed, and blocking, because the audio device
    # is torn down four steps below. It is skipped for a signal-driven shutdown: someone
    # pressing Ctrl+C wants the process gone, not a sentence first.
    #
    # THE `stop()` FIRST IS NOT OPTIONAL, and leaving it out was measured rather than guessed.
    # "Exit" is most often said OVER a running answer, and `speak(..., blocking=True)` waits for
    # the whole pipeline to drain — so without cancelling first the farewell queued BEHIND the
    # rest of the response and the process took **26.6 seconds** to die instead of ~3. Bumping
    # the epoch discards every queued sentence and every buffered audio chunk; the farewell is
    # then the only thing in the pipeline.
    if TTS_ENABLED and tts_engine is not None:
        try:
            tts_engine.stop()
            if farewell:
                # Clears the `_interrupted` latch `stop()` just set, so this one sentence is
                # allowed through. Safe here because the epoch has already moved: a response
                # stream cancelled a moment ago tests `is_cancelled(token)` and stays cancelled.
                tts_engine.begin_background_utterance()
                tts_engine.speak("Shutting down. Goodbye.", True)
        except BaseException:
            pass

    # 3. Proactive service: stopped BEFORE the audio and browser teardown so it can never hand
    #    text to an engine that is being disposed.
    if proactive_agent is not None:
        try:
            proactive_agent.stop(timeout=2.0)
        except BaseException:
            pass

    # 4. Outstanding timers and any pending confirmation.
    if shutdown_automation is not None:
        try:
            cancelled = shutdown_automation()
            if cancelled:
                print_info(f"Cancelled {cancelled} pending timer(s).")
        except BaseException:
            pass

    # 5. Silence the speakers and release the audio device (and, with it, the ONNX session).
    if TTS_ENABLED and tts_engine is not None:
        try:
            tts_engine.shutdown()
        except BaseException:
            pass

    # 6. The STT browser session.
    #
    #    Its processes are identified by the PIDs the engine recorded at startup, NEVER by
    #    process name. Name matching is actively dangerous here: `automation.windows` opens
    #    applications through AppOpener, which uses `subprocess.Popen`, so a Chrome window Kayra
    #    opened FOR THE USER is a child of this process and a name-based sweep would close the
    #    user's browsing session on exit.
    if AUDIO_ENABLED and stt_engine:
        try:
            stt_engine.shutdown()
        except BaseException:
            pass

        # 7. Verify the browser processes Kayra owns are actually gone, force-killing by PID
        #    only. `stt_engine.shutdown()` already does this; this is the belt-and-braces pass
        #    for the case where shutdown() itself was interrupted.
        try:
            survivors = stt_engine.terminate_owned_processes(timeout=3.0)
            if survivors:
                print_warning(f"Kayra-owned browser processes survived teardown: "
                              f"{sorted(survivors)}")
        except BaseException:
            pass

    # 8. Presentation last: the window and the tray icon come off the screen only once the
    #    assistant behind them is genuinely gone.
    for hook in list(_pre_exit_hooks):
        done = threading.Event()

        def run(target=hook):
            try:
                target()
            except BaseException:
                pass
            finally:
                done.set()

        threading.Thread(target=run, daemon=True, name="kayra-exit-hook").start()
        done.wait(timeout=HOOK_TIMEOUT)

    # The last thing said about the microphone. OFFLINE is the only transition the machine
    # permits out of STOPPING, so this is the terminal state by construction rather than by
    # being the last line that happens to run.
    _voice_facts(voice_available=False, backend_status="OFF", shutting_down=False)
    logbus.success(Subsystem.SHUTDOWN, "Kayra has stopped.", correlate=False)
    os._exit(exit_code)


def _force_shutdown(signum=None, frame=None):
    """
    Signal-handler entry point, and the historical name every other module calls.

    Kept as a thin adapter rather than renamed: `ui.session`, `ui.application` and the test
    suite all reach shutdown by this name, and a signal handler must accept (signum, frame).
    No farewell — a Ctrl+C is a request for the process to be gone, not for a sentence first.
    """
    request_shutdown(reason=f"signal {signum}" if signum else "")


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

def presence_engine():
    """The running contextual presence layer, or None. One accessor, no second copy."""
    agent = proactive_agent
    return getattr(agent, "presence", None) if agent is not None else None


def _presence_greeting(text):
    """
    The contextual reply to a bare greeting, or None when this is not one.

    Returns None for every failure mode — presence absent, greetings switched off, the
    utterance not actually a greeting — and the caller then routes to the chatbot exactly as
    it always has. A greeting must never be able to cost the user their answer.
    """
    presence = presence_engine()
    if presence is None:
        return None
    try:
        return presence.greeting(text)
    except Exception:
        return None


async def Execute_Task(intent_array, original_query, mood=None):
    """
    Takes the parsed intent array from the DMM and routes it to the correct modules, then
    speaks whatever the handler returned.
    """
    automation_commands = []

    for task in intent_array:
        task_lower = task.strip().lower()

        # Resolved ONCE per task. Calling the greeting builder inside the `elif` condition
        # and again in its body would render two different wordings and record both as
        # "recently said", which is precisely the repetition the presence ledger exists to
        # prevent. It is None for anything that is not a bare greeting.
        greeting_reply = (_presence_greeting(original_query)
                          if task_lower.startswith("general ") else None)

        # A barge-in mid-response cancels the rest of the turn: the user has moved on.
        if TTS_ENABLED and tts_engine is not None and tts_engine.interrupted:
            print_system("Turn cancelled by user interruption.")
            return

        # 1. Exit protocol.
        #
        #    The local control interpreter normally catches "exit" / "turn off Kayra" before
        #    the classifier ever sees them, so this branch is the fallback for the phrasings
        #    only the DMM recognises ("that's all", "bye jarvis"). Both routes converge on the
        #    same `request_shutdown`, which speaks the farewell itself.
        if task_lower == "exit":
            print_system(f"Initiating shutdown sequence for {assistant_name}. Goodbye!")
            await asyncio.to_thread(request_shutdown, "dmm: exit", True)

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

        # 2b. Listening control. A THIRD kind of "stop", and the one most easily confused
        #     with the other two: "stop" alone is a barge-in handled by the audio layer and
        #     never reaches here, "exit" ends the process, and this closes the microphone and
        #     nothing else.
        #
        #     The local control interpreter normally catches this before the classifier ever
        #     sees it; this branch is the fallback for a phrasing only the DMM recognised. It
        #     dispatches through the SAME function, so the two routes cannot drift into two
        #     behaviours.
        elif task_lower.startswith("stop listening"):
            await asyncio.to_thread(_dispatch_control, ControlKind.PAUSE_LISTENING,
                                    original_query, "dmm")

        # 2c. A bare greeting.
        #
        #     Answered from local context — the clock, how long the user has actually been
        #     away, and whether this is the first exchange of the session — instead of being
        #     sent to the chatbot for the same "Hello! How can I help you today?" every time.
        #
        #     It is deliberately narrow. `is_greeting` matches the WHOLE utterance once the
        #     assistant's name and filler are stripped, so "hello" is a greeting and "hello,
        #     open Chrome" is an instruction; anything that is not purely a greeting falls
        #     through to the branch below exactly as before. No model is called on this path,
        #     which is the point: a greeting that costs a cloud round-trip arrives after the
        #     moment for it has passed.
        elif greeting_reply is not None:
            reply = greeting_reply
            CONTEXT.note_assistant_turn(reply)
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
        # The targets automation just acted on are the things most likely to be referred
        # to again in the next utterance, which is legitimate evidence for the repair stage.
        for command in automation_commands:
            parts = command.split()
            CONTEXT.note_automation(action=parts[0] if parts else "",
                                    target=" ".join(parts[1:])[:60])
        if spoken and isinstance(spoken, str):
            CONTEXT.note_assistant_turn(spoken)
            # A question from automation ("Which browser did you mean?") makes the answer
            # vocabulary plausible on the very next utterance.
            CONTEXT.set_pending_confirmation(
                (pending_confirmation() or "") if pending_confirmation else "")
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
        # Same one-sentence budget as before, now aware of the hour. A contextual opening
        # line costs nothing at boot — it is a dictionary lookup and a `random.choice` — and
        # it is the first thing that makes the assistant feel present rather than started.
        opening = None
        presence = presence_engine()
        if presence is not None:
            try:
                opening = presence.boot_line()
            except Exception:
                opening = None
        tts_engine.speak(opening or f"{assistant_name} online.")

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
                    CONTEXT.note_user_turn(user_input)
                    CONTEXT.note_assistant_turn(reply)
                    # Answered: the question is no longer outstanding, so the answer
                    # vocabulary stops being the plausible one on the next utterance.
                    CONTEXT.set_pending_confirmation("")
                    set_state(STATE_LISTENING)
                    continue

            # Runtime bookkeeping. `note_user_utterance` is what keeps the proactive agent
            # quiet around live conversation, and the emitted event is how it learns habits and
            # reads the user's reaction to its last suggestion — this module does not need to
            # know that the agent exists.
            RUNTIME.note_user_utterance()
            RUNTIME.begin_turn()
            RUNTIME.emit("user_utterance", text=user_input)
            # The conversation context is updated on the SAME path as the runtime state, so
            # the repair stage reading it on the next utterance can never be a turn behind.
            CONTEXT.note_user_turn(user_input)
            CONTEXT.set_pending_confirmation(
                (pending_confirmation() or "") if pending_confirmation else "")

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
            CONTEXT.note_intent(list(dmm_commands))

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
