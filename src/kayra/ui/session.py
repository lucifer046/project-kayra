# ┌────────────────────────────────────────────────────────────────────────┐
# │                             session.py                                 │
# │        Backend Driver for the UI — no Qt, no widgets, no imports       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Drives the Kayra backend on behalf of a presentation layer, and knows nothing about Qt.

WHY THIS IS SEPARATE FROM `bridge.py`
-------------------------------------
This module contains all the awkward parts — thread ownership, the turn pipeline, boot
sequencing, shutdown — and expresses its output as plain callbacks. `bridge.py` is a thin Qt
adapter that turns those callbacks into signals.

Splitting them buys the thing that matters most for a UI of this size: the backend driver can
be tested headless, with no QApplication, no display and no widgets, while the Qt layer stays
small enough to be obviously correct.

RELATIONSHIP TO `kayra.app`
---------------------------
`kayra.app.Main_Loop()` is the console front end: it owns a listen/route loop that blocks on
`Listen()`. A GUI cannot host that loop — its main thread belongs to Qt — so this module
replaces the LOOP and reuses everything else.

It deliberately does NOT reimplement a turn. `bootstrap()`, `Execute_Task()`, the emotion
engine, the DMM, the confirmation gate and the automation dispatcher are all called exactly as
the console loop calls them, in the same order, so voice behaviour is identical whichever front
end is running. If the turn pipeline changes in `app.py`, this follows automatically.

THREADS
-------
Two, and the second one is not free — it is the honest cost of accepting typed input while the
microphone is open:

  * `kayra-ui-runner`   consumes a queue of turns and executes them.
  * `kayra-ui-listener` blocks in `Listen()` and pushes utterances onto that queue.

`Listen()` blocks until an utterance is finalized and has no timeout, so a single thread could
not also service the text box while listening. Everything else — TTS synthesis and playback,
the barge-in watcher, the proactive agent — is a thread the backend already owned; this module
adds no others and starts no timers.
"""

import queue
import asyncio
import threading
import traceback


# What the UI can ask for. A small closed vocabulary rather than arbitrary callables, so the
# runner can never be handed work that blocks it forever.
TURN_TEXT = "text"          # a typed message
TURN_VOICE = "voice"        # a transcribed utterance
CMD_STOP = "stop"           # barge-in
CMD_QUIT = "quit"


class SessionEvents:
    """
    The callback surface. Every attribute is replaced by the adapter that owns the session.

    Defaults are no-ops so a session can run unobserved — which is exactly what the headless
    tests do, and it means a missing handler can never raise inside a worker thread.
    """

    def __init__(self):
        self.boot_stage = lambda name, seconds: None
        self.boot_finished = lambda ok, detail: None
        self.state_changed = lambda state, previous: None
        self.user_message = lambda text, source: None
        self.assistant_message = lambda text: None
        self.system_message = lambda text, tone: None
        self.intent_classified = lambda text, tokens: None
        self.automation_started = lambda commands: None
        self.automation_finished = lambda spoken: None
        self.mood_detected = lambda emotion, confidence: None
        self.error = lambda message: None
        self.busy_changed = lambda busy: None
        self.listening_changed = lambda listening: None
        self.sleeping_changed = lambda sleeping: None


class KayraSession:
    """
    Owns the backend for the lifetime of the UI process.

    Lifecycle:  start() -> [running] -> shutdown()

    `start()` returns immediately. Booting happens on the runner thread, so the window can
    paint and report progress instead of freezing for the four seconds the engines take.
    """

    def __init__(self, events=None, enable_voice=True):
        self.events = events or SessionEvents()
        self.enable_voice = enable_voice

        self._queue = queue.Queue(maxsize=32)
        self._runner = None
        self._listener = None
        self._stop = threading.Event()

        self._app = None            # the kayra.app module, imported during boot
        self._runtime = None
        self._ready = threading.Event()
        self._boot_error = None
        self._loop = None           # asyncio loop owned by the runner thread
        # The listener waits on this instead of polling while the microphone is paused.
        # An Event costs nothing to wait on and wakes the instant listening resumes.
        self._listening_gate = threading.Event()
        self._listening_gate.set()

    # ──────────────────────────────────────────────────────────────────
    #                              STATUS
    # ──────────────────────────────────────────────────────────────────

    @property
    def ready(self):
        return self._ready.is_set() and self._boot_error is None

    @property
    def boot_error(self):
        return self._boot_error

    @property
    def runtime(self):
        return self._runtime

    @property
    def app(self):
        """The live `kayra.app` module, or None before boot completes."""
        return self._app

    def state(self):
        if self._runtime is None:
            return "STARTING"
        try:
            return self._runtime.state
        except Exception:
            return "OFFLINE"

    def voice_available(self):
        return bool(self._app is not None and getattr(self._app, "AUDIO_ENABLED", False))

    def tts_available(self):
        return bool(self._app is not None and getattr(self._app, "TTS_ENABLED", False))

    def proactive_enabled(self):
        agent = getattr(self._app, "proactive_agent", None) if self._app else None
        try:
            return bool(agent.enabled) if agent is not None else False
        except Exception:
            return False

    def listening_enabled(self):
        """Whether the microphone is open. Read from the runtime, never cached here."""
        if self._runtime is None:
            return False
        try:
            return bool(self._runtime.listening)
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    #                             LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def start(self):
        """Starts the runner thread. Returns immediately; boot happens there."""
        if self._runner is not None:
            return
        self._runner = threading.Thread(target=self._run, name="kayra-ui-runner", daemon=True)
        self._runner.start()

    def shutdown(self, hard=True):
        """
        Stops the session.

        `hard=True` delegates to `app.request_shutdown`, which is THE authoritative teardown:
        it announces the shutdown, stops the proactive agent, cancels timers, silences audio,
        reaps the browser processes Kayra owns BY PID and runs the UI's pre-exit hooks, in that
        order. The UI does not reimplement any of it and does not reorder it — a second
        teardown living here would drift out of step with the one the backend depends on.

        It is also idempotent, so the Home button, the tray's Quit and a spoken "turn off
        Kayra" arriving together cannot re-enter a shutdown that is already running.
        """
        self._stop.set()
        try:
            self._queue.put_nowait((CMD_QUIT, None))
        except queue.Full:
            pass

        if hard and self._app is not None:
            try:
                request = getattr(self._app, "request_shutdown", None)
                if request is not None:
                    request(reason="ui")          # never returns: ends in os._exit(0)
                else:                             # pragma: no cover - pre-rename backend
                    self._app._force_shutdown()
            except SystemExit:
                raise
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    #                           UI COMMANDS
    # ──────────────────────────────────────────────────────────────────

    def submit_text(self, text):
        """Queues a typed message. Safe to call from the GUI thread; never blocks."""
        text = (text or "").strip()
        if not text or not self.ready:
            return False
        try:
            self._queue.put_nowait((TURN_TEXT, text))
            return True
        except queue.Full:
            self.events.error("Kayra is still working through the previous requests.")
            return False

    def interrupt(self):
        """Barge-in from a button instead of a spoken word. Same path, same cancellation."""
        if not self._app:
            return False
        engine = getattr(self._app, "tts_engine", None)
        if engine is None:
            return False
        try:
            engine.stop()
            if self._runtime is not None:
                self._runtime.note_interrupt()
                self._runtime.emit("barge_in", source="ui")
            return True
        except Exception:
            return False

    def set_listening(self, enabled):
        """
        Opens or closes the microphone. NOT a shutdown, and NOT a barge-in.

        Delegates to `app.set_listening`, which is the same function the spoken "stop
        listening" reaches through the DMM — so both routes produce identical state and
        neither is a second implementation of the other.
        """
        if not self._app:
            return False
        control = getattr(self._app, "set_listening", None)
        if control is None:
            return False
        try:
            control(bool(enabled), announce=False)
            # Wake the listener thread if it is parked on the gate.
            if enabled:
                self._listening_gate.set()
            else:
                self._listening_gate.clear()
            return True
        except Exception:
            return False

    def set_sleeping(self, enabled):
        """
        Puts Kayra into standby, or wakes it. NOT a pause and NOT a shutdown.

        Delegates to `app.set_sleeping`, which is the same function the spoken "go to sleep"
        reaches — so both routes produce identical state and neither is a second implementation
        of the other.
        """
        if not self._app:
            return False
        control = getattr(self._app, "set_sleeping", None)
        if control is None:
            return False
        try:
            control(bool(enabled), announce=False)
            return True
        except Exception:
            return False

    def sleeping(self):
        if self._runtime is None:
            return False
        try:
            return bool(self._runtime.sleeping)
        except Exception:
            return False

    def tts_device_report(self):
        """
        What the LIVE Kokoro session is actually running on, or {} when there is no engine.

        Deliberately empty rather than a plausible default: the settings screen distinguishes
        "this is what is running" from "this is what would run", and it can only do that if the
        absence of a live engine is reported as an absence.
        """
        engine = getattr(self._app, "tts_engine", None) if self._app else None
        if engine is None:
            return {}
        try:
            return dict(engine.device_report() or {})
        except Exception:
            return {}

    def gpu_metrics(self):
        """
        Live GPU telemetry, or `{}` when this machine has none.

        Read from `tts_device`, which is the single authority — the Home dashboard and the
        Settings device card therefore cannot disagree about the GPU, because there is only one
        place the numbers come from. Returns immediately from a cache; the sampler behind it is
        one self-parking thread, never a persistent monitor process.

        Deliberately NOT gated on the backend being booted: the physical GPU exists whether or
        not Kayra's speech engine is up, and hiding it until boot finishes would make the card
        flicker in on startup.
        """
        try:
            from kayra.output import tts_device
            return dict(tts_device.gpu_metrics() or {})
        except Exception:
            return {}

    def gpu_telemetry_pending(self):
        """
        True while GPU telemetry is expected but has not arrived yet.

        This is what lets Home say "Reading GPU..." on the first tick and "No GPU detected"
        afterwards, instead of announcing an absence that is really just a cache that has not
        been filled. The distinction matters because the first call to `gpu_metrics()` always
        returns None: it starts the sampler rather than blocking the GUI thread on nvidia-smi.
        """
        try:
            from kayra.output import tts_device
            return bool(tts_device.gpu_telemetry_available())
        except Exception:
            return False

    def tts_provider(self):
        """
        The provider the LIVE speech session is using, or "" when there is no engine.

        This is the value Home shows next to the GPU statistics. It is read from the engine
        rather than inferred from the GPU's presence, because "there is a GPU" and "speech is
        running on it" are different facts — and showing the second when only the first is true
        is exactly the lie this whole subsystem exists to prevent.
        """
        report = self.tts_device_report()
        return report.get("provider", "") if report else ""

    def set_tts_device(self, mode):
        """
        Switches the running speech engine to AUTO / GPU / CPU. Returns the resulting report,
        or None when there is no engine to switch.

        The engine owns the safety of the switch — it cancels playback, drains the pipeline and
        builds the replacement session before releasing the old one. This is a pass-through on
        purpose: a UI that sequenced the swap itself would be a second implementation of the
        one operation that must not have two.
        """
        engine = getattr(self._app, "tts_engine", None) if self._app else None
        if engine is None or not hasattr(engine, "set_device_mode"):
            return None
        try:
            status = engine.set_device_mode(mode)
            return status.to_dict() if status is not None else None
        except Exception as exc:
            self.events.error(f"Could not switch the speech device: {exc}")
            return None

    def set_proactive(self, enabled):
        agent = getattr(self._app, "proactive_agent", None) if self._app else None
        if agent is None:
            return False
        try:
            agent.set_enabled(bool(enabled))
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    #                          RUNNER THREAD
    # ──────────────────────────────────────────────────────────────────

    def _run(self):
        if not self._boot():
            return

        # One asyncio loop for the life of the session. `Execute_Task` is a coroutine and the
        # console front end runs it under `asyncio.run` per process; creating a fresh loop per
        # turn here would churn selectors and thread-pool executors on every single message.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        if self.enable_voice and self.voice_available():
            self._start_listener()

        while not self._stop.is_set():
            try:
                kind, payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == CMD_QUIT:
                break
            try:
                self._run_turn(payload, source=kind)
            except Exception as exc:
                self.events.error(f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
                try:
                    self._runtime.end_turn()
                    self._set_state("IDLE")
                except Exception:
                    pass

    def _boot(self):
        """Imports and boots the backend, reporting each stage as it completes."""
        try:
            self.events.boot_stage("Loading Kayra", 0.0)
            from kayra import app as kayra_app
            from kayra.core.runtime_state import get_runtime_state

            self._app = kayra_app
            self._runtime = get_runtime_state()

            # Subscribed BEFORE bootstrap so no transition is missed while the engines come up.
            self._runtime.subscribe(self._on_runtime_event)

            self.events.boot_stage("Starting speech, voice and models", 0.0)
            kayra_app.bootstrap()

            # The barge-in watcher normally starts inside `Main_Loop`, which the UI does not
            # run. Without it, "stop" is only noticed after a response has finished playing —
            # the watcher exists precisely because the turn pipeline cannot poll while it is
            # busy. Started here so voice interruption behaves identically in both front ends.
            if self.voice_available():
                threading.Thread(target=kayra_app._barge_in_watcher,
                                 name="kayra-barge-in", daemon=True).start()

            self._install_output_tap()

            self._ready.set()
            detail = self._boot_summary()
            self.events.boot_stage("Ready", 0.0)
            self.events.boot_finished(True, detail)
            self._set_state("IDLE")
            return True

        except Exception as exc:
            self._boot_error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._ready.set()
            self.events.boot_finished(False, self._boot_error)
            return False

    def _install_output_tap(self):
        """
        Mirrors everything Kayra says into the UI, without touching any service.

        WHY TAP THE SPEAKER. The conversational services stream their answer straight into
        `tts_engine.speak()` sentence by sentence and return nothing; there is no existing
        return path a transcript could come from. The alternatives were to change the signature
        of `Chatbot`, `RealTimeSearchEngine` and the automation dispatcher so each also returns
        text, or to wrap the one public method they all already funnel through.

        Wrapping is strictly better here. It is a presentation concern living in the
        presentation layer, it touches no stable backend code, and it captures EVERY utterance
        by construction — chat, live search, automation sentences, confirmations and proactive
        suggestions — including any future service, which a signature change would not.

        A pleasant side effect: because sentences arrive as they are synthesized, the chat view
        streams the reply in as it is spoken rather than pasting it in complete afterwards.

        Known limitation, surfaced honestly in the UI: with speech output unavailable there is
        no stream to tap, so the chat transcript is not captured. The Chat view says so rather
        than appearing silently broken.
        """
        engine = getattr(self._app, "tts_engine", None)
        if engine is None or not self.tts_available():
            return
        if getattr(engine, "_kayra_ui_tapped", False):
            return                      # a session restart must not stack wrappers

        original = engine.speak

        def speak(text, blocking=False):
            try:
                cleaned = (text or "").strip()
                if cleaned:
                    self.events.assistant_message(cleaned)
            except Exception:
                pass                    # the tap must never be able to break speech
            return original(text, blocking)

        engine.speak = speak
        engine._kayra_ui_tapped = True

    def _boot_summary(self):
        parts = []
        parts.append("voice input ready" if self.voice_available() else "voice input unavailable")
        parts.append("speech output ready" if self.tts_available() else "speech output unavailable")
        return ", ".join(parts)

    def _start_listener(self):
        def listen_loop():
            listen = getattr(self._app, "Listen", None)
            if listen is None:
                return
            while not self._stop.is_set():
                # Paused: park until listening resumes. `Listen()` returns immediately while
                # the microphone is closed, so without this the loop would spin on it. Waiting
                # on the gate with a timeout keeps the shutdown check responsive.
                if not self._listening_gate.wait(timeout=0.25):
                    continue
                try:
                    text = listen()
                except Exception:
                    if self._stop.is_set():
                        return
                    threading.Event().wait(1.0)   # a transient STT fault must not hot-spin
                    continue
                if self._stop.is_set():
                    return
                if not text or not str(text).strip():
                    continue
                try:
                    self._queue.put_nowait((TURN_VOICE, str(text).strip()))
                except queue.Full:
                    pass

        self._listener = threading.Thread(target=listen_loop, name="kayra-ui-listener",
                                          daemon=True)
        self._listener.start()

    # ──────────────────────────────────────────────────────────────────
    #                           THE TURN
    # ──────────────────────────────────────────────────────────────────

    def _run_turn(self, text, source):
        """
        One complete turn, in the same order as `app.Main_Loop`.

        confirmation gate -> runtime bookkeeping -> emotion -> DMM -> Execute_Task

        The ordering is not incidental. The confirmation gate runs BEFORE the classifier
        because a bare "yes" sent to the DMM comes back as `general yes` and is answered by the
        chatbot, so a pending "should I restart your computer?" would never resolve.
        """
        app = self._app
        self.events.user_message(text, source)

        # ── Pending confirmation ──
        pending = getattr(app, "pending_confirmation", None)
        resolve = getattr(app, "resolve_confirmation", None)
        if pending and resolve and pending():
            handled, reply = resolve(text)
            if handled:
                self.events.assistant_message(reply)
                if app.TTS_ENABLED and app.tts_engine is not None:
                    app.tts_engine.speak(reply)
                self._runtime.note_user_utterance()
                self._set_state("IDLE")
                return

        self._runtime.note_user_utterance()
        self._runtime.begin_turn()
        self._runtime.emit("user_utterance", text=text)

        if app.TTS_ENABLED and app.tts_engine is not None:
            app.tts_engine.begin_turn()

        self._set_state("PROCESSING")

        # ── Emotion: tone only, never intent ──
        mood = None
        if getattr(app, "EMOTION_ENABLED", False) and app.emotion_engine is not None:
            try:
                mood = app.emotion_engine.analyze(
                    text, seconds_since_interrupt=self._runtime.seconds_since_interrupt())
                if mood is not None:
                    self.events.mood_detected(str(mood), float(getattr(mood, "confidence", 0.0)))
            except Exception:
                mood = None          # a mood failure must never cost the user their answer

        # ── Intent ──
        try:
            tokens = app.engine.classify_intent(text)
        except Exception as exc:
            self._runtime.end_turn()
            self._set_state("IDLE")
            self.events.error(f"Intent classification failed: {exc}")
            return

        tokens = list(tokens or [])
        self.events.intent_classified(text, tokens)
        self._runtime.emit("intent_classified", text=text, tokens=tokens)

        automation = [t for t in tokens
                      if not t.strip().lower().startswith(
                          ("general ", "realtime ", "deep research ", "proactive ", "exit"))]
        if automation:
            self.events.automation_started(automation)

        # ── Execute ──
        try:
            self._loop.run_until_complete(app.Execute_Task(tokens, text, mood))
        finally:
            self._runtime.end_turn()

        if automation:
            self.events.automation_finished("")

        self._set_state("IDLE")

    # ──────────────────────────────────────────────────────────────────
    #                          RUNTIME EVENTS
    # ──────────────────────────────────────────────────────────────────

    def _on_runtime_event(self, event, payload):
        """
        Subscriber on the runtime bus. Runs on whichever thread emitted.

        It must return quickly and must never raise: `emit()` fans out synchronously from the
        main loop and the barge-in watcher, and a slow subscriber would stall both. Everything
        here is a dict lookup and a callback.
        """
        try:
            if event == "state_changed":
                self.events.state_changed(payload.get("state", "IDLE"),
                                          payload.get("previous", ""))
                self.events.busy_changed(bool(self._runtime.is_busy()))
            elif event == "listening_changed":
                listening = bool(payload.get("listening", True))
                # Keep the listener's gate in step with the runtime however the change was
                # made — the UI button, the spoken "stop listening", or anything else that
                # calls `app.set_listening`. This is what makes one source of truth real.
                if listening:
                    self._listening_gate.set()
                else:
                    self._listening_gate.clear()
                self.events.listening_changed(listening)
            elif event == "sleeping_changed":
                sleeping = bool(payload.get("sleeping", False))
                self.events.sleeping_changed(sleeping)
                self.events.system_message(
                    "Kayra is asleep. Say \"wake up\" when you need it." if sleeping
                    else "Kayra is awake.", "info")
            elif event == "barge_in":
                self.events.system_message("Interrupted.", "warning")
        except Exception:
            pass

    def _set_state(self, state):
        if self._runtime is not None:
            try:
                self._runtime.set_state(state)
            except Exception:
                pass
