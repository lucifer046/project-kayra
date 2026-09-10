# ┌────────────────────────────────────────────────────────────────────────┐
# │                              bridge.py                                 │
# │           The UI ↔ Backend Boundary (Qt adapter over a session)        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single seam between Qt and Kayra. Views talk to this; nothing else.

THREAD SAFETY IS THE WHOLE POINT
--------------------------------
Kayra's runtime bus calls subscribers SYNCHRONOUSLY on whichever thread emitted — the turn
runner, the barge-in watcher, the proactive agent. Qt widgets may only be touched from the GUI
thread. Calling `label.setText(...)` from the barge-in watcher is undefined behaviour that
usually looks like it works and occasionally corrupts the widget tree.

This class is the marshalling point. It lives in the GUI thread, so every one of its signals is
delivered through Qt's queued connection mechanism when emitted from a worker: the payload is
copied, posted to the GUI thread's event queue and delivered on the next event-loop turn.

    backend thread  ──emit──▶  KayraBridge signal  ──queued──▶  widget slot (GUI thread)

The rule that follows, and it is not optional:
**no view may subscribe to the runtime bus or call a backend engine directly.**
Everything arrives as a signal; everything outbound goes through a method here.

NO POLLING FOR STATE
--------------------
`stateChanged` is driven by `RuntimeState.set_state()`, which announces transitions. The UI
therefore never polls for "what is the assistant doing" — it is told. The only timers in the
application are for values that genuinely have no event source: system metrics and the audit
log, both on multi-second intervals.
"""

from PySide6.QtCore import QObject, Signal, Qt

from kayra.ui.session import KayraSession, SessionEvents


class KayraBridge(QObject):
    """Qt-facing facade over a `KayraSession`."""

    # ── Boot ──
    bootStage = Signal(str)                 # human-readable stage name
    bootFinished = Signal(bool, str)        # ok, detail

    # ── Assistant state ──
    stateChanged = Signal(str, str)         # state, previous
    busyChanged = Signal(bool)
    listeningChanged = Signal(bool)         # the microphone opened or closed
    sleepingChanged = Signal(bool)          # standby entered or left
    moodDetected = Signal(str, float)       # emotion label, confidence

    # ── Voice presence ──
    # THE signal every surface that renders the microphone listens to. `revision` is not
    # decoration: Qt delivers queued signals in order but a widget can also be repainted from
    # a timer, a `showEvent` or a synchronous read, and any of those can land after a newer
    # transition. A consumer keeps the last revision it rendered and drops anything not newer.
    #
    # `listeningChanged` and `stateChanged` are KEPT, unchanged, because they are separate
    # facts that other parts of the UI legitimately need (a button label, a window icon).
    # What no longer happens is a screen deriving the voice CAPTION from them.
    voiceStateChanged = Signal(str, str, str, int)   # state, text, detail, revision

    # ── Speech backend ──
    sttBackendChanged = Signal(dict)        # the full requested/active snapshot

    # ── Hand gesture control ──
    # ONE signal carrying the whole resolved status, for the same reason `voiceStateChanged`
    # carries the whole voice picture: a card that took its camera state from one signal and
    # its gesture state from another would eventually render a combination that never existed.
    #
    # There is deliberately NO signal carrying camera frames. The preview PULLS from
    # `camera_frame()` on its own timer — a per-frame queued signal is a queue, and a queue
    # that the GUI thread drains more slowly than the camera fills it is unbounded latency.
    gestureStateChanged = Signal(dict)

    # ── Conversation ──
    userMessage = Signal(str, str)          # text, source ("text" | "voice")
    assistantMessage = Signal(str)          # one spoken sentence, as it is produced
    systemMessage = Signal(str, str)        # text, tone ("info" | "warning" | "danger")
    errorOccurred = Signal(str)

    # ── Intent and automation ──
    intentClassified = Signal(str, list)    # utterance, tokens
    automationStarted = Signal(list)        # command tokens
    automationFinished = Signal(str)        # spoken result

    def __init__(self, enable_voice=True, parent=None):
        super().__init__(parent)

        events = SessionEvents()
        # Bound signal emission is thread-safe in Qt; the connection type resolves to Queued
        # because this object's affinity is the GUI thread and the emitter is not.
        events.boot_stage = lambda name, seconds: self.bootStage.emit(name)
        events.boot_finished = lambda ok, detail: self.bootFinished.emit(bool(ok), str(detail))
        events.state_changed = lambda state, previous: self.stateChanged.emit(str(state), str(previous))
        events.busy_changed = lambda busy: self.busyChanged.emit(bool(busy))
        events.listening_changed = lambda listening: self.listeningChanged.emit(bool(listening))
        events.sleeping_changed = lambda sleeping: self.sleepingChanged.emit(bool(sleeping))
        events.voice_state_changed = lambda state, text, detail, revision: (
            self.voiceStateChanged.emit(str(state), str(text), str(detail), int(revision)))
        events.stt_backend_changed = lambda state: self.sttBackendChanged.emit(dict(state or {}))
        events.gesture_state_changed = lambda status: self.gestureStateChanged.emit(
            dict(status or {}))
        events.mood_detected = lambda emotion, confidence: self.moodDetected.emit(str(emotion),
                                                                                  float(confidence))
        events.user_message = lambda text, source: self.userMessage.emit(str(text), str(source))
        events.assistant_message = lambda text: self.assistantMessage.emit(str(text))
        events.system_message = lambda text, tone: self.systemMessage.emit(str(text), str(tone))
        events.intent_classified = lambda text, tokens: self.intentClassified.emit(str(text),
                                                                                   list(tokens))
        events.automation_started = lambda commands: self.automationStarted.emit(list(commands))
        events.automation_finished = lambda spoken: self.automationFinished.emit(str(spoken or ""))
        events.error = lambda message: self.errorOccurred.emit(str(message))

        self._session = KayraSession(events=events, enable_voice=enable_voice)

    # ──────────────────────────────────────────────────────────────────
    #                            LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def start(self):
        self._session.start()

    def shutdown(self, hard=True):
        """
        Ends the session. `hard=True` hands over to Kayra's own shutdown, which is authoritative.

        That path deliberately ends in `os._exit(0)`, so nothing after this call runs. The UI
        does not attempt to tidy up first: the backend teardown already stops the proactive
        agent, silences audio and reaps the browser processes it owns by PID, in an order that
        matters and that a UI must not second-guess.
        """
        self._session.shutdown(hard=hard)

    # ──────────────────────────────────────────────────────────────────
    #                             COMMANDS
    # ──────────────────────────────────────────────────────────────────

    def submit_text(self, text):
        return self._session.submit_text(text)

    def interrupt(self):
        return self._session.interrupt()

    def set_proactive(self, enabled):
        return self._session.set_proactive(enabled)

    def set_sleeping(self, enabled):
        """Standby. A third thing again: not a barge-in, not a pause, not a shutdown."""
        return self._session.set_sleeping(enabled)

    def set_presence(self, enabled):
        """
        The contextual presence layer, on or off, for this session.

        Distinct from `set_proactive`, which is the master switch for the whole subsystem:
        turning presence off leaves the habit-based suggestions running, turning the
        subsystem off silences both. The master switch stays authoritative.
        """
        return self._session.set_presence(enabled)

    def set_presence_category(self, name, enabled):
        """One presence category (greetings, context, late_night, work_session, system, humor)."""
        return self._session.set_presence_category(name, enabled)

    def set_tts_device(self, mode):
        """AUTO / GPU / CPU, applied to the running Kokoro session."""
        return self._session.set_tts_device(mode)

    def set_listening(self, enabled):
        """
        Opens or closes the microphone.

        Deliberately NOT routed through `interrupt()` or `shutdown()`. Those are the other two
        "stop"-shaped actions in this application and conflating any two of them is the failure
        this method exists to prevent: interrupt cancels a sentence, this closes the
        microphone, shutdown ends the process.
        """
        return self._session.set_listening(enabled)

    # ──────────────────────────────────────────────────────────────────
    #                          READ-ONLY STATUS
    # ──────────────────────────────────────────────────────────────────
    # Cheap synchronous reads for painting. Anything expensive belongs on a signal instead.

    @property
    def ready(self):
        return self._session.ready

    @property
    def boot_error(self):
        return self._session.boot_error

    def state(self):
        return self._session.state()

    def voice_available(self):
        return self._session.voice_available()

    def tts_available(self):
        return self._session.tts_available()

    def proactive_enabled(self):
        return self._session.proactive_enabled()

    def listening_enabled(self):
        return self._session.listening_enabled()

    def presence_available(self):
        return self._session.presence_available()

    def presence_enabled(self):
        return self._session.presence_enabled()

    def presence_categories(self):
        return self._session.presence_categories()

    def presence_status(self):
        """
        What the presence layer is doing right now — read live, never cached here.

        Returns `{}` when the layer is not running, which is what lets the Home card say so
        rather than showing a plausible-looking empty state for a service that is off.
        """
        return self._session.presence_status()

    def sleeping(self):
        return self._session.sleeping()

    def tts_device_report(self):
        """
        What the live speech session is ACTUALLY running on. `{}` when there is no engine.

        Read straight from the engine every time rather than cached here: a device switch,
        including one made from another surface, must never be able to leave this screen
        displaying a provider that is no longer in use.
        """
        return self._session.tts_device_report()

    def intelligence_status(self):
        """Which model tier is live, and what each route resolved to. `{}` before boot."""
        return self._session.intelligence_status()

    def gpu_metrics(self):
        """
        Live GPU telemetry, or `{}`. Same source as the Settings device card, by construction.

        Never cached here. A second copy in the UI would be a second source of truth that could
        disagree with the first, which is the failure this whole layer is shaped to avoid.
        """
        return self._session.gpu_metrics()

    def tts_provider(self):
        """The provider the live speech session is ACTUALLY using, or "" when none is running."""
        return self._session.tts_provider()

    def gpu_telemetry_pending(self):
        """True while GPU telemetry is still being fetched, as opposed to genuinely absent."""
        return self._session.gpu_telemetry_pending()

    def graphics_profile(self):
        """The physical graphics adapter on ANY machine, or `{}`. No telemetry, no vendor bias."""
        return self._session.graphics_profile()

    # ──────────────────────────────────────────────────────────────────
    #                     VOICE PRESENCE AND BACKEND
    # ──────────────────────────────────────────────────────────────────

    def voice_runtime_state(self):
        """
        The whole voice picture in one synchronous read: state, revision, the facts behind it,
        and the live speech backend.

        Used for the INITIAL paint and for a screen becoming visible again. Everything after
        that arrives on `voiceStateChanged` — this is not something to poll, and the absence
        of a timer anywhere near it is deliberate.
        """
        return self._session.voice_runtime_state()

    def listening_known(self):
        """
        Whether `listening_enabled()` is a measurement rather than the pre-boot default.

        A view built before the session boots must not paint a microphone control as though
        it had asked — see `KayraSession.listening_enabled`.
        """
        return self._session.listening_known()

    def stt_backend_state(self):
        """Requested vs active speech backend. The two are separate fields and stay separate."""
        return self._session.stt_backend_state()

    def set_stt_backend(self, backend):
        """
        Switches the live speech backend. Returns (committed, detail).

        A pass-through, for the same reason `set_tts_device` is one: the backend manager owns
        the transaction and the engine owns the swap. A UI that sequenced it would be a second
        implementation of the one operation that must not have two.
        """
        return self._session.set_stt_backend(backend)

    # ──────────────────────────────────────────────────────────────────
    #                       HAND GESTURE CONTROL
    # ──────────────────────────────────────────────────────────────────

    def set_gesture(self, enabled):
        """
        Turns hand gesture control on or off. Returns (committed, detail).

        Enabling starts the camera first when it is off — the controller owns that ordering,
        which is why no caller here has to check. A pass-through, like `set_tts_device`.
        """
        return self._session.set_gesture(enabled)

    def set_camera(self, enabled):
        """
        Turns the camera on or off. Returns (committed, detail).

        A SEPARATE axis from gesture control and it stays separate: seeing the preview and
        having something move your pointer are different requests.
        """
        return self._session.set_camera(enabled)

    def gesture_status(self):
        """
        Camera, gesture switch and runtime state. `{}` when nothing has ever been started.

        Read live every time, never cached here — a cached copy in the UI would be a second
        source of truth that could disagree with the controller, which is the failure this
        whole layer is shaped to avoid.
        """
        return self._session.gesture_status()

    def gesture_telemetry(self):
        """Full diagnostics. Behind the advanced switch; never painted on the normal path."""
        return self._session.gesture_telemetry()

    def camera_frame(self):
        """
        The newest preview frame as `(rgb_bytes, width, height)`, or None. Cheap and
        non-blocking: the conversion already happened on the gesture thread.
        """
        return self._session.camera_frame()

    # ──────────────────────────────────────────────────────────────────
    #                             MEMORY
    # ──────────────────────────────────────────────────────────────────

    def list_memories(self, limit=None):
        """Saved memories, newest first, each with a stable id. Deletion is BY that id."""
        return self._session.list_memories(limit=limit)

    def delete_memory(self, memory_id):
        """Removes one memory by id. Returns (deleted, detail) — False means it is still there."""
        return self._session.delete_memory(memory_id)

    def clear_memories(self):
        """Empties the store. Returns (count_cleared, ok)."""
        return self._session.clear_memories()

    def memory_store(self):
        """Where memory lives, how big it is and how many entries it holds."""
        return self._session.memory_store()

    def open_memory_location(self):
        """Reveals the memory file in File Explorer. Returns (ok, path_or_error)."""
        return self._session.open_memory_location()

    def recent_automation(self, limit=20):
        """
        The automation audit ring, newest last.

        Read straight from `automation.policy.recent_audit()` — the backend already keeps a
        bounded, structured record of every automation decision, and a second history in the UI
        would be a second source of truth that could disagree with the first.
        """
        try:
            from kayra.automation.policy import recent_audit
            return list(recent_audit(limit))
        except Exception:
            return []

    def conversation_memory(self):
        """
        The raw saved exchanges.

        Kept for compatibility with anything reading the plain list, but the Memory screen
        uses `list_memories()` instead: this shape has no stable identity, and deleting from
        it meant deleting by position — which is wrong the moment a chat turn appends to the
        store between the render and the click.
        """
        try:
            from kayra.memory.conversation import load_conversation_memory
            return list(load_conversation_memory() or [])
        except Exception:
            return []

    def habits(self):
        """Learned routines, read from the proactive agent's habit store."""
        try:
            import json
            from kayra.core.paths import data_path
            with open(data_path("habits.json"), "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}
