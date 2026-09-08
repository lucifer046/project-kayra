# ┌────────────────────────────────────────────────────────────────────────┐
# │                           stt_backend.py                               │
# │      The Authoritative Speech-Input Backend State, And Its Switch      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Which browser is running speech recognition, what was ASKED for, and how to change it live.

THE DEFECT THIS FIXES
---------------------
`STT_BROWSER` was a `.env` value read exactly once, inside `SpeechToTextEngine.__init__`.
Choosing "Google Chrome" in Settings therefore wrote a string to a file and did nothing else:
the live session kept running on whatever it had started with, the screen showed the new
value, and the two disagreed until the next restart. A setting that changes only a screen is
not a setting.

REQUESTED IS NOT ACTIVE
-----------------------
The single most important thing in this module is that they are separate fields.

    requested_backend   what the user asked for      ("chrome")
    active_backend      what is running right now    ("edge", or None)
    status              how that came to be          (LISTENING / ERROR / …)

They are equal on the happy path and DIFFERENT whenever a switch failed, which is the case
the UI must be able to show. A screen that renders "Google Chrome" because a dropdown says
Chrome, while Edge holds the microphone, is the exact lie this file exists to prevent.

ONE STATE, ONE OWNER
--------------------
No view, bridge or session keeps its own copy. `snapshot()` is the only reader, `request()`
is the only writer, and both are lock-guarded. `request()` is also SERIALISED against itself
with a non-blocking guard: a user clicking through the dropdown three times must not start
three overlapping teardowns of the same browser session, and the LAST request has to be the
one that wins.

WHAT IT REFUSES TO DO
---------------------
* It never boots an STT engine. It reads the LIVE one out of `sys.modules` — the same
  string-keyed lookup, and the same reasoning, as `automation.targets.kayra_owned_pids`: the
  Settings screen asking "which backend is active?" must not have the side effect of starting
  a headless browser. With no engine it reports OFF and says a restart is needed.
* It never terminates a process by name. The engine's PID-scoped ownership does the reaping,
  unchanged, so a switch cannot touch the user's own Chrome windows.
* It never falls back silently from a named backend. `auto` uses the existing capability
  logic in `kayra.input.browsers`; an explicit choice is strict, and a failure is reported.
"""

import sys
import time
import threading

from kayra.core.logbus import Subsystem, info, warning, error, success, debug, section, section_end


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               STATUSES                                 │
# └────────────────────────────────────────────────────────────────────────┘

class BackendStatus:
    """
    How the speech-input backend currently stands.

    Distinct from `SttState` in `speech_to_text.py`, which is the ENGINE's internal lifecycle
    (NOT_STARTED / STARTING / READY / LISTENING / RECOVERING / STOPPING / STOPPED / FAILED).
    This is the view a settings screen and a state machine need, and PAUSED — which the engine
    tracks on a separate axis entirely — is one of its values.
    """

    OFF = "OFF"                 # no engine in this process
    STARTING = "STARTING"       # a session is being brought up
    LISTENING = "LISTENING"     # a verified session, microphone open
    PAUSED = "PAUSED"           # a verified session, microphone deliberately closed
    RECOVERING = "RECOVERING"   # the session died and is being rebuilt
    STOPPING = "STOPPING"
    ERROR = "ERROR"             # no usable session


STATUSES = (BackendStatus.OFF, BackendStatus.STARTING, BackendStatus.LISTENING,
            BackendStatus.PAUSED, BackendStatus.RECOVERING, BackendStatus.STOPPING,
            BackendStatus.ERROR)

AUTO = "auto"

# Labels for the values the settings dropdown offers. Keys match `browsers._FAMILIES`, plus
# `auto`. The label is what the log and the UI say; the key is what everything else passes.
BACKEND_LABELS = {
    AUTO: "Automatic",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "brave": "Brave",
    "chromium": "Chromium",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
}


def label_for(key):
    if not key:
        return "None"
    return BACKEND_LABELS.get(str(key).strip().lower(), str(key))


def normalize(key):
    """`auto` for anything empty or explicitly automatic; a lowercase browser key otherwise."""
    text = str(key or "").strip().lower()
    return AUTO if text in ("", AUTO, "default", "none") else text


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                STATE                                   │
# └────────────────────────────────────────────────────────────────────────┘

class STTBackendState:
    """
    An immutable snapshot. Every field a UI, a log line or a state machine could want, taken
    in one locked read so no consumer can see a half-updated switch.
    """

    __slots__ = ("requested_backend", "active_backend", "status", "browser_process_id",
                 "session_id", "last_error", "started_at", "settings_source", "revision")

    def __init__(self, requested_backend=AUTO, active_backend=None, status=BackendStatus.OFF,
                 browser_process_id=None, session_id=None, last_error="", started_at=None,
                 settings_source="env", revision=0):
        self.requested_backend = requested_backend
        self.active_backend = active_backend
        self.status = status
        self.browser_process_id = browser_process_id
        self.session_id = session_id
        self.last_error = last_error
        self.started_at = started_at
        self.settings_source = settings_source
        self.revision = revision

    @property
    def requested_label(self):
        return label_for(self.requested_backend)

    @property
    def active_label(self):
        return label_for(self.active_backend) if self.active_backend else "None"

    @property
    def matches(self):
        """
        Whether what is running is what was asked for.

        `auto` matches ANY active backend by definition — automatic means "whichever one
        works", so a session on Edge under `auto` is a satisfied request, not a mismatch.
        """
        if self.active_backend is None:
            return False
        return self.requested_backend == AUTO or self.requested_backend == self.active_backend

    def to_dict(self):
        return {
            "requested_backend": self.requested_backend,
            "requested_label": self.requested_label,
            "active_backend": self.active_backend,
            "active_label": self.active_label,
            "status": self.status,
            "browser_process_id": self.browser_process_id,
            "session_id": self.session_id,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "settings_source": self.settings_source,
            "revision": self.revision,
            "matches": self.matches,
        }

    def __repr__(self):
        return (f"<STTBackendState requested={self.requested_backend} "
                f"active={self.active_backend} status={self.status}>")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE MANAGER                               │
# └────────────────────────────────────────────────────────────────────────┘

class STTBackendManager:
    """
    Owns the requested/active distinction and performs the live switch.

    It holds no engine reference of its own. `_engine()` reads the live one out of
    `sys.modules` on each call, which is what keeps this module importable from a settings
    screen without booting a browser as a side effect.
    """

    # The module the live engine lives in. A string, because importing it starts Chrome.
    #
    # A RENAME OF THAT MODULE TURNS THIS OFF SILENTLY rather than raising — the same trap
    # `automation.targets.kayra_owned_pids` fell into after the package reorganisation, where
    # a stale name tuple quietly stopped excluding Kayra's own browser. `tests/test_stt_backend.py`
    # pins the module path for exactly that reason. If the STT module moves, update this.
    ENGINE_MODULES = ("kayra.input.speech_to_text", "speech_to_text")

    def __init__(self):
        self._lock = threading.RLock()
        # Serialises switches against each other. Non-blocking: a request arriving while one
        # is in flight is REJECTED as stale rather than queued, because queueing would mean a
        # burst of dropdown clicks each tearing the browser down in turn.
        self._switch_lock = threading.Lock()
        self._requested = AUTO
        self._source = "env"
        self._last_error = ""
        self._revision = 0
        self._started_at = None
        self._listeners = []
        self._switching = False

    # ── Engine access ─────────────────────────────────────────────────

    def _engine(self):
        """
        The LIVE STT engine, or None. Never constructs one.

        Reads `_active_instance` off the class in an already-imported module. `get_shared_engine`
        would CREATE one, which is what makes this a `sys.modules` lookup rather than an import.
        """
        for name in self.ENGINE_MODULES:
            module = sys.modules.get(name)
            if module is None:
                continue
            engine_class = getattr(module, "SpeechToTextEngine", None)
            live = getattr(engine_class, "_active_instance", None) if engine_class else None
            if live is not None and getattr(live, "driver", None) is not None:
                return live
        return None

    def engine_present(self):
        return self._engine() is not None

    # ── Reading ───────────────────────────────────────────────────────

    def snapshot(self) -> STTBackendState:
        """One locked read of everything, with the ACTIVE half taken from the live engine."""
        engine = self._engine()
        with self._lock:
            requested, source = self._requested, self._source
            last_error, revision = self._last_error, self._revision
            started_at, switching = self._started_at, self._switching

        if engine is None:
            return STTBackendState(requested_backend=requested, active_backend=None,
                                   status=BackendStatus.OFF, last_error=last_error,
                                   started_at=started_at, settings_source=source,
                                   revision=revision)

        active = None
        try:
            active = engine.backend_key()
        except Exception:
            active = None

        status = self._status_for(engine, switching)
        pid = None
        try:
            pid = getattr(engine, "_service_pid", None)
        except Exception:
            pid = None
        session_id = None
        try:
            driver = getattr(engine, "driver", None)
            session_id = getattr(driver, "session_id", None) if driver is not None else None
        except Exception:
            session_id = None

        return STTBackendState(
            requested_backend=requested, active_backend=active, status=status,
            browser_process_id=pid, session_id=session_id, last_error=last_error,
            started_at=started_at, settings_source=source, revision=revision)

    def _status_for(self, engine, switching):
        """
        Maps the engine's own lifecycle onto the backend status a UI needs.

        PAUSED IS CHECKED BEFORE RECOVERING BUT AFTER THE LIFECYCLE FAULTS, and the order is
        the point: a paused microphone with a healthy session is genuinely PAUSED, while a
        session being rebuilt is RECOVERING whether or not listening happens to be paused —
        "reconnecting" is the more urgent and more informative of the two.
        """
        from kayra.input.speech_to_text import SttState

        state = getattr(engine, "state", None)
        if switching:
            return BackendStatus.STARTING
        if state in (SttState.STOPPING,):
            return BackendStatus.STOPPING
        if state in (SttState.STOPPED, SttState.FAILED):
            return BackendStatus.ERROR
        if state == SttState.RECOVERING:
            return BackendStatus.RECOVERING
        if state in (SttState.NOT_STARTED, SttState.STARTING):
            return BackendStatus.STARTING
        try:
            if engine.listening_paused:
                return BackendStatus.PAUSED
        except Exception:
            pass
        return BackendStatus.LISTENING

    # ── Change notification ───────────────────────────────────────────

    def subscribe(self, callback):
        """
        `callback(STTBackendState)`, called after every committed change.

        Synchronous and exception-swallowing, exactly like `RuntimeState.emit`: the caller is
        the switch itself, and a listener bug must not be able to wedge a backend transition
        halfway through.
        """
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self):
        state = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(state)
            except Exception:
                pass

    # ── Writing ───────────────────────────────────────────────────────

    def adopt(self, requested=None, source="env"):
        """
        Records the backend the engine started with, without switching anything.

        Called once at boot. It seeds `requested` from configuration so the first snapshot is
        truthful rather than defaulting to `auto` while `.env` says `chrome`.
        """
        with self._lock:
            if requested is not None:
                self._requested = normalize(requested)
            self._source = source
            self._started_at = time.time()
            self._revision += 1
        state = self.snapshot()
        info(Subsystem.STT, f"Backend: {state.active_label}", correlate=False)
        if state.requested_backend != AUTO and not state.matches and state.active_backend:
            # The honest case: configuration asked for one browser and another is running.
            warning(Subsystem.STT,
                    f"Requested {state.requested_label} but {state.active_label} is active")
        self._notify()
        return state

    def request(self, backend, source="settings"):
        """
        Asks for a backend and performs the live switch. Returns (committed, detail).

        The full transaction, in order:

            record the request  ->  stop the old session  ->  start the requested one  ->
            verify it can transcribe  ->  publish  ->  log

        `committed` is False whenever the ACTIVE backend did not end up as requested, and the
        snapshot then shows requested != active with a `last_error`. Nothing here returns True
        because a value was stored.

        RAPID CHANGES (11.6/11.7). A second request arriving while one is in flight is
        rejected as stale rather than queued. The requested value is still updated first, so
        the user's LATEST choice is what the screen and a subsequent retry see — what is
        refused is a second concurrent teardown of the same browser session, which is how a
        duplicate session or a leaked process would happen.
        """
        target = normalize(backend)

        with self._lock:
            previous = self._requested
            self._requested = target
            self._source = source
            self._revision += 1
            if self._switching:
                debug(Subsystem.STT,
                      f"Ignoring {label_for(target)} — a backend switch is already in flight")
                return False, "a backend switch is already in progress"

        if not self._switch_lock.acquire(blocking=False):
            debug(Subsystem.STT, "Ignoring a concurrent backend request")
            return False, "a backend switch is already in progress"

        try:
            with self._lock:
                self._switching = True
            return self._perform_switch(target, previous)
        finally:
            with self._lock:
                self._switching = False
            self._switch_lock.release()
            self._notify()

    def _perform_switch(self, target, previous):
        engine = self._engine()
        before = self.snapshot()

        if engine is None:
            # No live engine: the value is recorded and applies at the next start. Said out
            # loud, because a silent no-op here is indistinguishable from a working switch.
            with self._lock:
                self._last_error = "speech input is not running"
            warning(Subsystem.STT,
                    f"{label_for(target)} recorded, but speech input is not running — "
                    f"it will apply at the next start")
            return False, "speech input is not running"

        section(Subsystem.STT, "Backend switch")
        info(Subsystem.STT, f"From: {before.active_label}", correlate=False)
        info(Subsystem.STT, f"To: {label_for(target)}", correlate=False)
        info(Subsystem.STT, "Stopping the current session", correlate=False)

        started = time.perf_counter()
        try:
            ok, detail = engine.switch_backend(None if target == AUTO else target)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"

        elapsed = (time.perf_counter() - started) * 1000.0
        after = self.snapshot()

        with self._lock:
            self._last_error = "" if ok else str(detail)
            self._started_at = time.time() if ok else self._started_at

        if ok and after.matches:
            success(Subsystem.STT,
                    f"Active: {after.active_label} ({elapsed:.0f}ms)")
            section_end()
            return True, after.active_label

        # Not committed. Both halves are stated, because "it failed" without "and this is what
        # you have instead" leaves the user unable to tell whether Kayra can hear them at all.
        error(Subsystem.STT, f"{label_for(target)} could not be started: {detail}")
        info(Subsystem.STT, f"Active: {after.active_label}", correlate=False)
        warning(Subsystem.STT, "Change not committed", correlate=False)
        section_end()
        return False, str(detail)

    def note_recovery(self, phase, reason=""):
        """
        Records an STT session recovery. `phase` is "started" or "finished".

        The MANAGER owns these lines, not the engine and not the UI, so a recovery is
        announced once. It also drives the assistant visual's RECOVERING state through the
        same notification every other change uses — a lost session is a backend transition
        like any other, and treating it as one is what stops it rendering as a false pause.
        """
        if phase == "started":
            warning(Subsystem.STT, f"Session lost{f': {reason}' if reason else ''}")
            info(Subsystem.STT, "Recovery started", correlate=False)
        else:
            state = self.snapshot()
            if state.active_backend:
                success(Subsystem.STT, f"Session restored on {state.active_label}")
            else:
                error(Subsystem.STT, "Session could not be restored")
        self._notify()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One manager per process, for the same reason `RuntimeState` and `CentralizedLLMEngine` are
# singletons: a second copy would answer "which backend was requested?" differently from the
# one the switch actually wrote to, and the UI would be reading the one nobody updates.

_MANAGER = None
_LOCK = threading.Lock()


def get_stt_backend_manager() -> STTBackendManager:
    global _MANAGER
    if _MANAGER is None:
        with _LOCK:
            if _MANAGER is None:
                _MANAGER = STTBackendManager()
    return _MANAGER


def reset_stt_backend_manager():
    """Drops the process manager. For tests only — nothing in the application calls this."""
    global _MANAGER
    with _LOCK:
        _MANAGER = None
