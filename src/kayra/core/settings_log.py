# ┌────────────────────────────────────────────────────────────────────────┐
# │                           settings_log.py                              │
# │        The ONE Place A Setting Change Is Announced And Committed       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Every setting change goes through here, and it is announced exactly once.

    [21:48:03] [INFO   ] [SETTINGS] Speech input backend: Automatic -> Google Chrome
    [21:48:06] [SUCCESS] [SETTINGS] Speech input backend committed: Google Chrome

WHY A MODULE RATHER THAN A `print` AT EACH CALL SITE
----------------------------------------------------
Three reasons, and the first two are correctness rather than presentation.

**Ownership.** A setting change is visible to the view that raised it, the bridge that
forwarded it, the session that applied it and the service that acted on it. Four layers each
logging "the user changed the browser" is four lines for one event, and they drift: they
disagree about the old value, about whether the change actually took, and about what the
setting is called. This module is the single owner; nothing above or below it prints the same
event.

**Transactions.** A setting that triggers runtime work has three outcomes, not two: requested,
committed, and requested-but-not-committed. `apply()` makes that structure explicit — it logs
the request, runs the runtime operation, and commits ONLY if the operation reports success. A
screen that shows the new value because the dropdown changed is the specific lie this exists
to prevent: the dropdown moving is not evidence that anything happened.

**Secrets.** `record()` refuses to print a value whose key is a credential. It says the key was
SET or CLEARED and nothing else. A settings log that helpfully includes the new API key is a
credential leak with a timestamp on it.

WHAT THIS MODULE IS NOT
-----------------------
It is not a settings STORE. `core.config.write_env_values` owns persistence and stays the only
writer of `.env`; the live services own their own runtime state. This records and sequences
changes, and it holds only the last announced value per key so a repeat can be recognised.
"""

import threading

from kayra.core.logbus import Subsystem, info, warning, error, success


# Human labels for the settings that have them. A log line that says `STT_BROWSER` makes the
# reader translate; one that says `Speech input backend` does not. Unknown keys fall back to
# the key itself rather than being dropped — a new setting must still be logged.
LABELS = {
    "STT_BROWSER": "Speech input backend",
    "TTS_DEVICE_MODE": "TTS device",
    "INPUT_LANGUAGE": "Recognition language",
    "ASSISTANT_VOICE": "Voice",
    "ASSISTANT_NAME": "Assistant name",
    "USERNAME": "Your name",
    "FORCE_ONLINE": "Always use cloud models",
    "LOCAL_BASE_URL": "Local model endpoint",
    "KAYRA_LOG_LEVEL": "Log level",
    "PROACTIVE_AGENT_ENABLED": "Proactive agent (at startup)",
    "PROACTIVE_ENABLED": "Proactive suggestions",
    "PROACTIVE_PRESENCE_ENABLED": "Proactive presence",
    "PROACTIVE_GREETINGS_ENABLED": "Greetings",
    "PROACTIVE_CONTEXT_ENABLED": "Contextual observations",
    "PROACTIVE_LATE_NIGHT_ENABLED": "Late-night awareness",
    "PROACTIVE_WORK_SESSION_ENABLED": "Work-session awareness",
    "PROACTIVE_SYSTEM_ENABLED": "System observations",
    "PROACTIVE_HUMOR_ENABLED": "Humour",
    "LISTENING": "Microphone",
    "GESTURE_ENABLED": "Hand gesture control",
    "CAMERA": "Camera",
    "GESTURE_SENSITIVITY": "Gesture sensitivity",
    "GESTURE_CURSOR_SMOOTHING": "Cursor smoothing",
    "GESTURE_CLICK_SENSITIVITY": "Click sensitivity",
    "GESTURE_DIAGNOSTICS": "Gesture diagnostics",
    "SLEEPING": "Standby",
    "CohereAPIKey": "Cohere API key",
    "GROQ_API_KEY": "Groq API key",
    "GEMINI_API_KEY": "Gemini API key",
}


def label_for(key):
    return LABELS.get(key, str(key))


def _is_secret(key):
    """
    Whether a key holds a credential.

    Delegates to `core.config.is_secret`, which is already the authority the settings screen
    uses to decide what it may render — one definition of "secret", not two that could drift
    apart and leave a key printable in the log but not on screen.
    """
    try:
        from kayra.core.config import is_secret
        return bool(is_secret(key))
    except Exception:
        # The safe direction. A key we cannot classify is treated as a secret, because the
        # cost of over-redacting one log line is nothing and the cost of the other mistake is
        # a credential in the scrollback.
        lowered = str(key).lower()
        return any(token in lowered for token in ("key", "secret", "token", "password"))


def _display(key, value, label_value=None):
    """
    The value as it may appear in a log line.

    `label_value` lets the CALLER supply the human name for a machine value — `chrome` ->
    `Google Chrome`. It is a callable rather than a table here because this module lives in
    `core`, which is a leaf: importing `kayra.input.stt_backend` to learn what `chrome` is
    called would make every importer of the settings recorder able to reach the STT package.
    The caller already has that name, so it hands it over.
    """
    if _is_secret(key):
        return "set" if str(value or "").strip() else "cleared"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if label_value is not None and value is not None:
        try:
            labelled = str(label_value(value)).strip()
            if labelled:
                return labelled if len(labelled) <= 60 else labelled[:57] + "…"
        except Exception:
            pass                        # a labelling helper must never break a log line
    text = str(value if value is not None else "")
    text = text.strip()
    if not text:
        return "(empty)"
    # Bounded. A pasted endpoint URL or a long voice name must not push a log line off screen.
    return text if len(text) <= 60 else text[:57] + "…"


class SettingsRecorder:
    """
    Records and announces setting changes. Thread-safe; holds only the last value per key.

    Bounded by construction: one entry per setting key that has ever changed, which is a
    couple of dozen strings for the life of the process.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._values = {}

    # ── The simple case: a setting that just changed ──────────────────

    def record(self, key, new_value, old_value=None, subsystem=Subsystem.SETTINGS,
               label_value=None):
        """
        Announces `label: old -> new` and returns the line, or "" when nothing changed.

        A no-op change is silent on purpose. A settings screen re-syncing its controls from
        the live services on every navigation would otherwise print a page of transitions
        from a value to itself, which is noise that makes the real changes harder to find.
        """
        with self._lock:
            if old_value is None and key in self._values:
                old_value = self._values[key]
            before = _display(key, old_value, label_value)
            after = _display(key, new_value, label_value)
            if before == after:
                self._values[key] = new_value
                return ""
            self._values[key] = new_value
        return info(subsystem, f"{label_for(key)}: {before} -> {after}", correlate=False)

    def note(self, key, value):
        """Seeds the last-known value WITHOUT logging. Used to prime the recorder at boot."""
        with self._lock:
            self._values[key] = value

    def known(self, key, default=None):
        with self._lock:
            return self._values.get(key, default)

    # ── The transactional case: a setting that does runtime work ──────

    def apply(self, key, new_value, runtime, old_value=None, subsystem=Subsystem.SETTINGS,
              label_value=None):
        """
        Requests a change, performs the runtime work, and commits only if it succeeded.

        `runtime` is a zero-argument callable returning either a truthy value (success) or
        `(ok, detail)`. It raises freely — a runtime operation that throws is a failed change,
        not a crashed settings screen.

        Returns `(committed, detail)`.

        THE POINT OF THE THREE-STEP SHAPE. A change that fails must not leave the recorder,
        the log or the screen claiming the new value. `requested` is announced first because
        the operation can take seconds (an STT backend switch reaps nine browser processes and
        starts a new session) and a silent gap is indistinguishable from a hang; the commit
        line is what makes it true, and the failure line explicitly says the change was NOT
        committed rather than simply not mentioning it again.
        """
        with self._lock:
            if old_value is None and key in self._values:
                old_value = self._values[key]
        before = _display(key, old_value, label_value)
        after = _display(key, new_value, label_value)
        name = label_for(key)

        if before == after:
            # Not a no-op we can skip: the caller asked for runtime work. Say so quietly and
            # still do it — re-selecting the current backend is a legitimate way to ask for a
            # restart of it.
            info(subsystem, f"{name}: reapplying {after}", correlate=False)
        else:
            info(subsystem, f"{name}: {before} -> {after}", correlate=False)

        try:
            outcome = runtime()
        except Exception as exc:
            error(subsystem, f"{name} change failed: {type(exc).__name__}: {exc}")
            warning(subsystem, f"{name} not committed — still {before}")
            return False, f"{type(exc).__name__}: {exc}"

        if isinstance(outcome, tuple) and len(outcome) == 2:
            ok, detail = outcome
        else:
            ok, detail = bool(outcome), ""

        if ok:
            with self._lock:
                self._values[key] = new_value
            success(subsystem, f"{name} committed: {after}"
                               + (f" ({detail})" if detail else ""))
            return True, detail

        error(subsystem, f"{name} change failed" + (f": {detail}" if detail else ""))
        warning(subsystem, f"{name} not committed — still {before}")
        return False, detail

    # ── Bulk persistence (the Save button) ────────────────────────────

    def record_many(self, updates, previous=None, subsystem=Subsystem.SETTINGS):
        """
        Announces a batch of persisted values, one line each, skipping the unchanged.

        Returns the number of lines emitted. The settings screen writes every control on
        Save whether or not it was touched, so without the unchanged filter one click would
        print thirty lines of which two mattered.
        """
        previous = previous or {}
        emitted = 0
        for key in sorted(updates):
            if self.record(key, updates[key], previous.get(key), subsystem=subsystem):
                emitted += 1
        return emitted


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One recorder per process. Two would each hold half the last-known values, so the same change
# could be announced twice — by the console front end and by the UI — which is precisely the
# duplication this module exists to remove.

_RECORDER = None
_LOCK = threading.Lock()


def get_settings_recorder() -> SettingsRecorder:
    global _RECORDER
    if _RECORDER is None:
        with _LOCK:
            if _RECORDER is None:
                _RECORDER = SettingsRecorder()
    return _RECORDER


def record(key, new_value, old_value=None, **kwargs):
    """Module-level shorthand for the common case."""
    return get_settings_recorder().record(key, new_value, old_value, **kwargs)


def apply(key, new_value, runtime, old_value=None, **kwargs):
    """Module-level shorthand for the transactional case."""
    return get_settings_recorder().apply(key, new_value, runtime, old_value, **kwargs)
