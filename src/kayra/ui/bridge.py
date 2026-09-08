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
