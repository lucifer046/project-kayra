# ┌────────────────────────────────────────────────────────────────────────┐
# │                              logbus.py                                 │
# │            Structured Terminal Logging — one format, one owner         │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single structured log surface for Kayra.

    [21:48:03] [INFO   ] [STT] Backend: Google Chrome

WHY THIS EXISTS ALONGSIDE `utils/console.py`
--------------------------------------------
`utils.console` owns the THEME and the raw `print_*` helpers, and every historical call site
uses it. This module owns the FORMAT: a timestamp, a level, a canonical subsystem tag and a
message, in that order, for every line the newer subsystems emit. It renders THROUGH
`utils.console.safe_print`, so there is still exactly one Console object and one place that
copes with a terminal that has been closed underneath the process.

Nothing here replaces `print_info`/`print_warning`/… — those keep working unchanged. New code,
and the subsystems retrofitted in this milestone (the provider router, the STT backend
manager, the settings recorder, the memory store, the voice state machine), logs here instead,
so those areas have one consistent presentation.

THE RULES THAT ARE LOAD-BEARING
-------------------------------
* **One canonical name per subsystem.** `[STT]`, never `[Speech input]` / `[Recognizer]` /
  `[STT Engine]` in three places. The names live in `Subsystem` and nowhere else.
* **One owner per event.** The layer that PERFORMS an action logs it; the layers above it do
  not log it again. The provider router owns provider/fallback lines, the STT backend manager
  owns backend transitions, the settings recorder owns setting changes, the voice state
  machine owns state transitions. A bridge or a view that reprints one of those is a bug, and
  `tests/test_logging.py` walks the AST for the common cases.
* **DEBUG is not printed at INFO.** Interim transcripts, VAD levels, presence scores and
  stale-revision drops are real diagnostics and they belong at DEBUG, where they do not drown
  the nine lines a human actually reads.
* **Never a secret.** `redact()` is applied to every message. An API key that reaches a log
  line is a leak that survives in scrollback, in screenshots and in pasted bug reports long
  after the process is gone.
* **The level is configuration, not a code change.** `KAYRA_LOG_LEVEL`, read from the process
  environment or `.env`; DEBUG / INFO / WARNING / ERROR, default INFO.

FILE LOGGING
------------
Optional and OFF by default (`KAYRA_LOG_FILE`). When on it writes the DEBUG-level stream to
`logs/kayra-debug.log` through a SIZE-ROTATING handler — a process designed to run all day
must not be able to fill a disk with its own diagnostics.
"""

import os
import re
import time
import logging
import threading

# `core` is a leaf package: this module imports the stdlib and `core.paths` only. It must
# never reach into intelligence/, input/, output/ or ui/ — every one of those wants to log.
from kayra.core.paths import logs_dir


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                LEVELS                                  │
# └────────────────────────────────────────────────────────────────────────┘
# SUCCESS is its own printable word but deliberately the SAME threshold as INFO: "it worked"
# is normal-path information, and someone who has quietened the log to warnings does not want
# a stream of successes either.

DEBUG = "DEBUG"
INFO = "INFO"
SUCCESS = "SUCCESS"
WARNING = "WARNING"
ERROR = "ERROR"

LEVELS = (DEBUG, INFO, SUCCESS, WARNING, ERROR)

# Only these four are selectable as a threshold. SUCCESS is a rendering level, not a filter.
CONFIGURABLE_LEVELS = (DEBUG, INFO, WARNING, ERROR)

_THRESHOLD = {
    DEBUG: 10,
    INFO: 20,
    SUCCESS: 20,        # same rank as INFO on purpose — see above
    WARNING: 30,
    ERROR: 40,
}

# The Rich theme style each level renders in. Colour is an ENHANCEMENT: the level word is
# always present in plain text, so a terminal with no ANSI support loses nothing but colour.
_STYLE = {
    DEBUG: "dim",
    INFO: "info",
    SUCCESS: "success",
    WARNING: "warning",
    ERROR: "error",
}


class Subsystem:
    """
    The canonical subsystem tags. One name per subsystem, chosen once.

    These are the only values that may appear between the second pair of brackets. Adding a
    subsystem means adding it here, not inventing a string at a call site.
    """

    BOOT = "BOOT"
    STT = "STT"
    VOICE = "VOICE"
    DMM = "DMM"
    LLM = "LLM"
    CHAT = "CHAT"
    SEARCH = "SEARCH"
    RESEARCH = "RESEARCH"
    AUTO = "AUTO"
    TTS = "TTS"
    PROACTIVE = "PROACTIVE"
    PRESENCE = "PRESENCE"
    MEMORY = "MEMORY"
    SETTINGS = "SETTINGS"
    UI = "UI"
    GPU = "GPU"
    # Hand gesture control and the camera behind it are SEPARATE subsystems, for the same
    # reason the speech backend and the microphone are: the camera can be on with gesture
    # control off, and a single tag would make a camera failure read as a gesture failure.
    GESTURE = "GESTURE"
    CAMERA = "CAMERA"
    SYSTEM = "SYSTEM"
    SHUTDOWN = "SHUTDOWN"


SUBSYSTEMS = frozenset(
    value for name, value in vars(Subsystem).items()
    if not name.startswith("_") and isinstance(value, str)
)

# Widest level word, so the message column lines up down the page. Computed rather than
# hardcoded, so a new level cannot silently break the alignment.
_LEVEL_WIDTH = max(len(name) for name in LEVELS)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              REDACTION                                 │
# └────────────────────────────────────────────────────────────────────────┘
# Applied to EVERY message, including ones this module did not compose — an SDK error string
# that echoes the key it was handed is exactly the case a call site would not think to guard.

_SECRET_PATTERNS = (
    # key=value / key: value, for anything whose NAME says it is a credential.
    # The prefix is OPTIONAL. It was `[A-Za-z_][A-Za-z0-9_]*`, which requires at least one
    # character BEFORE the keyword — so `GROQ_API_KEY=…` was caught and the bare `password=…`
    # was not, which is the single most obvious case there is.
    re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|apikey|secret|token|password|passwd|"
        r"bearer|credential)[A-Za-z0-9_]*)\s*[=:]\s*(\S+)"),
    # Bare "Bearer <token>", as it appears in an HTTP client's own error text.
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]{12,})"),
)

# Provider key SHAPES, for the case where no name is attached at all. Deliberately narrow: a
# pattern loose enough to catch every conceivable key would redact ordinary words, and an
# over-redacted error message is undebuggable, which is its own kind of harm.
_BARE_KEY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),          # OpenAI-style
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{16,}"),         # Groq
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"),         # Google / Gemini
)

REDACTED = "***"


def redact(message):
    """Removes anything that looks like a credential from a log message."""
    text = str(message)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    for pattern in _BARE_KEY_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

_lock = threading.RLock()
_level = INFO
_file_logger = None
_file_configured = False

# Correlation. A short monotonic turn number, not a UUID: it exists so a human can follow one
# interaction down the page, and a 36-character identifier on every line defeats that purpose.
_turn = 0
# Monotonic. See `begin_turn` for why this is separate from `_turn`.
_turn_seq = 0


def _resolve_initial_level():
    """
    Reads `KAYRA_LOG_LEVEL` from the process environment first, then `.env`.

    The process environment WINS here, unlike `core.config.env` where `.env` has precedence,
    because a log level is a debugging switch: someone who exports it for one run means it for
    that run. `core.config` is imported lazily — it imports `core.paths`, which this module
    also imports, and a module-level import would be a cycle for no benefit.
    """
    raw = (os.environ.get("KAYRA_LOG_LEVEL") or "").strip().upper()
    if not raw:
        try:
            from kayra.core.config import env_values
            raw = str(env_values().get("KAYRA_LOG_LEVEL", "") or "").strip().upper()
        except Exception:
            raw = ""
    return raw if raw in _THRESHOLD else INFO


def set_level(level):
    """Sets the console threshold. An unknown value is ignored rather than crashing a boot."""
    global _level
    level = str(level or "").strip().upper()
    if level not in _THRESHOLD:
        return _level
    with _lock:
        _level = level
    return _level


def get_level():
    return _level


def is_enabled(level):
    return _THRESHOLD.get(str(level).upper(), 20) >= _THRESHOLD[_level]


def debug_enabled():
    return _level == DEBUG


# ── Correlation ───────────────────────────────────────────────────────────

def begin_turn(turn_id=None):
    """
    Stamps subsequent lines with a turn number, and returns it.

    Called once per user turn by the front ends; everything logged until `end_turn()` is
    attributed to it, which is what lets `[VOICE] Turn #184 …` and `[AUTO] Turn #184 …` be
    read as one interaction.

    TWO COUNTERS, AND THE DISTINCTION IS LOAD-BEARING. `_turn_seq` only ever increases;
    `_turn` is the turn currently OPEN, and is 0 between turns.

    They used to be one variable, and `end_turn()` set it to 0 — so the next `begin_turn()`
    computed `0 + 1` and EVERY turn was "Turn #1". The correlation the number exists for was
    therefore absent, and worse, nothing could tell whether one turn was newer than another:
    a background retry chain comparing its own turn against the current one always saw the
    same value and could never notice it had been superseded.
    """
    global _turn, _turn_seq
    with _lock:
        if turn_id is not None:
            _turn_seq = int(turn_id)
        else:
            _turn_seq += 1
        _turn = _turn_seq
        return _turn


def current_turn():
    """The turn currently OPEN, or 0 between turns."""
    return _turn


def latest_turn():
    """
    The highest turn number ever started, whether or not it is still open.

    This is the one to compare against when asking "has my work been superseded?" —
    `current_turn()` is 0 between turns, so a check against it would report every finished
    turn as still current the moment its successor had not started yet.
    """
    return _turn_seq


def end_turn():
    global _turn
    with _lock:
        _turn = 0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            FILE LOGGING                                │
# └────────────────────────────────────────────────────────────────────────┘

def _ensure_file_logger():
    """
    Lazily configures the rotating debug log, when `KAYRA_LOG_FILE` asks for one.

    Bounded on purpose: 2 MB per file, 3 files. Kayra is meant to run all day, and an
    unbounded diagnostic log on a process like that is a slow disk-filling bug.
    """
    global _file_logger, _file_configured
    if _file_configured:
        return _file_logger
    _file_configured = True

    wanted = (os.environ.get("KAYRA_LOG_FILE") or "").strip().lower()
    if not wanted:
        try:
            from kayra.core.config import env_values
            wanted = str(env_values().get("KAYRA_LOG_FILE", "") or "").strip().lower()
        except Exception:
            wanted = ""
    if wanted not in ("1", "true", "yes", "on"):
        return None

    try:
        from logging.handlers import RotatingFileHandler
        path = os.path.join(logs_dir(), "kayra-debug.log")
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger = logging.getLogger("kayra.logbus")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(handler)
        _file_logger = logger
    except Exception:
        _file_logger = None
    return _file_logger


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              EMISSION                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _stamp():
    return time.strftime("%H:%M:%S")


def _escape(text):
    """Rich treats `[...]` as markup; a message containing brackets must not be swallowed."""
    return str(text).replace("[", r"\[")


def format_line(level, subsystem, message, turn=None, timestamp=None):
    """
    The one format. Pure — it renders a string and prints nothing, so tests assert on it.

        [21:48:03] [INFO   ] [STT] Backend: Google Chrome
        [21:48:14] [INFO   ] [AUTO] Turn #184 · Target: YouTube
    """
    level = str(level).upper()
    subsystem = str(subsystem).upper()
    stamp = timestamp if timestamp is not None else _stamp()
    body = redact(message)
    if turn:
        body = f"Turn #{turn} · {body}"
    return f"[{stamp}] [{level:<{_LEVEL_WIDTH}}] [{subsystem}] {body}"


def log(level, subsystem, message, turn=None, correlate=True):
    """
    Emits one structured line and returns it.

    `correlate=True` (the default) stamps the current turn number when one is open. Boot,
    shutdown and settings lines pass False: they do not belong to a user turn, and attributing
    them to whichever turn happened to be open would be a lie in the one field whose entire
    job is to say what belongs together.
    """
    level = str(level).upper()
    if level not in _THRESHOLD:
        level = INFO
    subsystem = str(subsystem).upper()

    turn_id = turn if turn is not None else (_turn if correlate else 0)
    stamp = _stamp()
    line = format_line(level, subsystem, message, turn=turn_id, timestamp=stamp)

    logger = _ensure_file_logger()
    if logger is not None:
        # The file always gets DEBUG, whatever the console threshold is. That is the point of
        # having a file: the console stays readable and the detail is still recoverable.
        try:
            logger.debug(line)
        except Exception:
            pass

    if not is_enabled(level):
        return line

    # Imported lazily: `utils.console` pulls in Rich and reconfigures the streams, and a
    # module-level import here would tie every `core` importer to that.
    from kayra.utils.console import safe_print

    style = _STYLE.get(level, "text")
    body = redact(message)
    if turn_id:
        body = f"Turn #{turn_id} · {body}"
    safe_print(
        f"[dim]\\[{stamp}][/dim] "
        f"[{style}]\\[{level:<{_LEVEL_WIDTH}}][/{style}] "
        f"[highlight]\\[{subsystem}][/highlight] "
        f"[text]{_escape(body)}[/text]",
        soft_wrap=True,
    )
    return line


def debug(subsystem, message, **kwargs):
    return log(DEBUG, subsystem, message, **kwargs)


def info(subsystem, message, **kwargs):
    return log(INFO, subsystem, message, **kwargs)


def success(subsystem, message, **kwargs):
    return log(SUCCESS, subsystem, message, **kwargs)


def warning(subsystem, message, **kwargs):
    return log(WARNING, subsystem, message, **kwargs)


def error(subsystem, message, **kwargs):
    return log(ERROR, subsystem, message, **kwargs)


def exception(subsystem, message, exc, detail=True):
    """
    An error line plus, at DEBUG only, its traceback.

    Expected recoverable failures (a rate limit, a lost STT session) must not dump a traceback
    onto a terminal a human is reading — the structured line already says what happened and
    what is being done about it. The traceback is genuine diagnostic value, so it is KEPT and
    moved to DEBUG rather than discarded.
    """
    error(subsystem, f"{message}: {type(exc).__name__}: {exc}")
    if detail and debug_enabled():
        import traceback
        for chunk in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for part in chunk.rstrip().splitlines():
                debug(subsystem, part)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          GROUPING HELPERS                              │
# └────────────────────────────────────────────────────────────────────────┘
# Section markers are for multi-step operations a human needs to follow as a unit — a backend
# switch, a boot phase. Kept short and used sparingly: a rule around every three lines is
# noise, not structure.

_RULE = "-" * 58


def section(subsystem, title):
    if not is_enabled(INFO):
        return
    from kayra.utils.console import safe_print
    safe_print(f"[dim]{_RULE}[/dim]")
    safe_print(f"[highlight]\\[{str(subsystem).upper()}][/highlight] "
               f"[text]{_escape(title)}[/text]")
    safe_print(f"[dim]{_RULE}[/dim]")


def section_end():
    if not is_enabled(INFO):
        return
    from kayra.utils.console import safe_print
    safe_print(f"[dim]{_RULE}[/dim]")


def field(subsystem, name, value, level=INFO):
    """
    An aligned `name  value` line inside a phase block.

    Used for the startup summary, where a column of aligned labels is the difference between
    a scannable report and a wall of prose.
    """
    return log(level, subsystem, f"{str(name):<14}{value}", correlate=False)


def transition(subsystem, label, before, after, level=INFO):
    """`label: before -> after`. The one shape every change in this codebase is announced in."""
    return log(level, subsystem, f"{label}: {before} -> {after}", correlate=False)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       THIRD-PARTY NOISE CONTROL                        │
# └────────────────────────────────────────────────────────────────────────┘

def quiet_third_party():
    """
    Turns down libraries that log at INFO on their normal path, WITHOUT hiding their failures.

    Every logger here is raised to WARNING — never to ERROR or CRITICAL, and never disabled:
    a real failure in Selenium, urllib3 or a provider SDK still reaches the terminal. What is
    suppressed is the routine chatter: `urllib3` announcing every connection to the local
    WebDriver at 17Hz while the barge-in watcher polls, `httpx` printing a line per streamed
    request while a reply is generated token by token.

    ONNX Runtime is deliberately NOT touched here. Its provider warnings are the EVIDENCE for
    the GPU account `tts_device` gives, and that module owns the decision about them.
    """
    for name in ("urllib3", "urllib3.connectionpool", "selenium",
                 "selenium.webdriver.remote.remote_connection",
                 "httpx", "httpcore", "openai", "cohere", "requests",
                 "PIL", "matplotlib", "comtypes", "asyncio"):
        try:
            logging.getLogger(name).setLevel(logging.WARNING)
        except Exception:
            pass


_level = _resolve_initial_level()
