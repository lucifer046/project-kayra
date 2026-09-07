# ┌────────────────────────────────────────────────────────────────────────┐
# │                          proactive_agent.py                            │
# │        Context-Aware, Cooldown-Gated Proactive Suggestion Service      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Kayra's proactive subsystem: the part that occasionally says something without being asked.

It is built around one rule — **observe cheaply, speak rarely, never interrupt**:

    cheap local tick (clock, active window, counters)
        -> candidate detected
        -> deterministic local score
        -> cooldown + safety gate
        -> wait for a safe window
        -> phrase it (template; LLM only to make an accepted candidate sound natural)
        -> speak through the ONE existing TTS pipeline
        -> learn from what the user did next

What it deliberately is NOT
---------------------------
It is not a timer that calls an LLM every few minutes to ask itself whether it should say
something. That is expensive (a cloud round-trip per tick, forever), unpredictable, and
impossible to test. Every decision below — whether a candidate exists, how relevant it is,
whether a cooldown blocks it, whether this moment is safe — is made by local arithmetic on
integers already in memory. The LLM is consulted at most once per *spoken* suggestion, only
to reword an already-approved candidate, and the agent works fully without it.

Concurrency contract
--------------------
* ONE daemon thread for the whole subsystem, sleeping on an Event (so shutdown is immediate
  and an idle agent costs no CPU between ticks).
* It reads `RuntimeState` and never writes assistant state.
* It NEVER touches the TTS cancellation epoch. Proactive speech goes out through
  `begin_background_utterance()` + `speak()`, wired in by `create_default_agent`, so a nudge
  landing between a barge-in and the chatbot noticing it cannot un-cancel the interrupted
  response. `begin_turn()` must never be called from this module — see
  `text_to_speech.turn_token` for why that specific race matters.
* Proactive speech is cancelled by exactly the same "stop" path as any other speech; there
  is no second interruption mechanism.

Persistence
-----------
`data/habits.json`, bounded in every dimension (see `HabitStore`). It stores counters and
hour histograms — never conversation transcripts.
"""

import os
import json
import time
import random
import datetime
import threading

# PyGetWindow handles Windows active-window introspection. Absent elsewhere; the agent then
# runs with the context signal disabled rather than failing.
try:
    import pygetwindow as gw
except Exception:
    gw = None

from kayra.utils import (print_info, print_warning, print_system, print_success,
                    speech_safe_text)
from kayra.core.paths import data_path
from kayra.core.runtime_state import AssistantState, get_runtime_state


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘
# Read from the process environment (main.py loads .env before importing this module).
# Every value has a defensive default so a malformed .env degrades to sane behaviour
# instead of crashing a background thread at boot.

def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_num(name, default, cast=float, minimum=None, maximum=None):
    try:
        value = cast(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


class ProactiveConfig:
    """Snapshot of the proactive tuning knobs, resolved once at construction."""

    def __init__(self):
        self.enabled = _env_bool("PROACTIVE_AGENT_ENABLED", True)

        # How often the cheap observation tick runs. This is the ONLY periodic work the
        # subsystem does; at the default it is 180 window reads an hour.
        self.tick_seconds = _env_num("PROACTIVE_TICK_SECONDS", 20.0, float, 5.0, 300.0)
        # While a candidate is waiting for a safe moment we look more often, so the nudge
        # lands promptly once the user stops talking rather than up to a tick later.
        self.defer_poll_seconds = _env_num("PROACTIVE_DEFER_POLL_SECONDS", 3.0, float, 1.0, 30.0)

        self.global_cooldown_s = _env_num("PROACTIVE_GLOBAL_COOLDOWN_MINUTES", 60.0) * 60.0
        self.repeat_cooldown_s = _env_num("PROACTIVE_REPEAT_COOLDOWN_MINUTES", 360.0) * 60.0
        self.break_cooldown_s = _env_num("PROACTIVE_BREAK_COOLDOWN_MINUTES", 90.0) * 60.0

        self.fatigue_seconds = _env_num("PROACTIVE_FATIGUE_MINUTES", 90.0) * 60.0
        self.score_threshold = _env_num("PROACTIVE_SCORE_THRESHOLD", 0.6, float, 0.0, 1.0)

        self.late_night_enabled = _env_bool("PROACTIVE_LATE_NIGHT_ENABLED", True)
        self.late_night_start = int(_env_num("PROACTIVE_LATE_NIGHT_START_HOUR", 1, int, 0, 23))
        self.late_night_end = int(_env_num("PROACTIVE_LATE_NIGHT_END_HOUR", 5, int, 0, 23))

        self.habit_learning = _env_bool("PROACTIVE_HABIT_LEARNING_ENABLED", True)
        self.max_habit_actions = int(_env_num("PROACTIVE_MAX_HABIT_ACTIONS", 60, int, 8, 500))
        self.max_habit_apps = int(_env_num("PROACTIVE_MAX_HABIT_APPS", 40, int, 8, 500))
        # Minimum observations before a routine is considered a habit at all.
        self.habit_min_count = int(_env_num("PROACTIVE_HABIT_MIN_COUNT", 4, int, 2, 100))

        # Optional: let the shared LLM reword an already-approved suggestion.
        self.llm_phrasing = _env_bool("PROACTIVE_LLM_PHRASING", True)

        # How long after the user last spoke (or was spoken to) before an unprompted line is
        # acceptable. Prevents a nudge landing in the natural pause of a conversation.
        self.quiet_after_interaction_s = _env_num("PROACTIVE_QUIET_SECONDS", 90.0, float, 5.0, 3600.0)
        # A deferred candidate that never found a safe window is dropped, not queued forever —
        # a break suggestion that arrives forty minutes late is noise.
        self.max_defer_s = _env_num("PROACTIVE_MAX_DEFER_SECONDS", 600.0, float, 30.0, 7200.0)

        self.save_interval_s = _env_num("PROACTIVE_SAVE_INTERVAL_SECONDS", 300.0, float, 30.0, 3600.0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             STATE MODEL                                │
# └────────────────────────────────────────────────────────────────────────┘

class ProactiveState:
    """
    Explicit lifecycle for the proactive subsystem.

    The states are not decoration: OBSERVING vs COOLDOWN decides whether candidate
    generation runs at all on a tick, and WAITING_FOR_SAFE_WINDOW is what makes "never
    interrupt the user" a deferral rather than a drop.
    """
    DISABLED = "DISABLED"                            # switched off by config or by voice
    IDLE = "IDLE"                                    # constructed, thread not started
    OBSERVING = "OBSERVING"                          # cheap ticks, looking for candidates
    CANDIDATE = "CANDIDATE"                          # something scored above threshold
    WAITING_FOR_SAFE_WINDOW = "WAITING_FOR_SAFE_WINDOW"  # approved, waiting for silence
    SPEAKING = "SPEAKING"                            # handing text to the TTS pipeline
    COOLDOWN = "COOLDOWN"                            # recently spoke; candidate gen skipped
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          HABIT PERSISTENCE                             │
# └────────────────────────────────────────────────────────────────────────┘

_TITLE_SEPARATORS = (" - ", " — ", " | ")

# Window titles that are not really an application the user is "in".
_IGNORED_APP_TITLES = {"", "unknown", "program manager", "windows input experience",
                       "task switching", "search"}


def normalize_app_name(raw_title: str) -> str:
    """
    Collapses a raw window title to a stable application identity.

    Without this, every file open in an editor and every browser tab registers as a distinct
    "application" and the habit model never accumulates enough observations of anything to
    clear `habit_min_count`.
    """
    if not raw_title:
        return "Unknown"
    title = raw_title.strip()
    for sep in _TITLE_SEPARATORS:
        if sep in title:
            # The tail segment is usually the application name:
            # "index.py - Visual Studio Code" -> "Visual Studio Code".
            title = title.split(sep)[-1].strip()
    return (title[:60] or "Unknown")


class HabitStore:
    """
    Bounded, atomically-written habit model.

    RETENTION POLICY (every collection here has one — an assistant that runs all day cannot
    own an unbounded JSON file):
      * `actions`  — at most `max_actions` keys; the least-observed are evicted first.
      * `apps`     — at most `max_apps` keys; the least-used by total seconds are evicted.
      * `suggestions` — one small record per suggestion KIND, and there are a fixed handful.
      * hour histograms are 24 integer buckets, so they cannot grow at all.

    It stores counters and hour buckets. It never stores what the user said.
    """

    VERSION = 2

    def __init__(self, path, max_actions=60, max_apps=40):
        self.path = path
        self.max_actions = max_actions
        self.max_apps = max_apps
        self._lock = threading.RLock()
        self.data = self._load()

    # ── disk ──────────────────────────────────────────────────────────────

    def _empty(self):
        return {"version": self.VERSION, "updated": None,
                "actions": {}, "apps": {}, "suggestions": {}}

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._empty()
            if data.get("version") != self.VERSION:
                # A v1 file held only `app_totals` (raw seconds, no hour buckets). There is
                # nothing in it the new scorer can use, so start clean rather than carrying
                # a shape the rest of this class would have to special-case forever.
                migrated = self._empty()
                for app, seconds in (data.get("app_totals") or {}).items():
                    try:
                        migrated["apps"][str(app)[:60]] = {
                            "seconds": float(seconds), "hours": [0] * 24, "last_ms": 0.0}
                    except (TypeError, ValueError):
                        continue
                return migrated
            for key in ("actions", "apps", "suggestions"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self._empty()

    def save(self):
        """Atomic write: a crash mid-save can only ever lose the newest counters."""
        with self._lock:
            try:
                self.data["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
                tmp = self.path + ".tmp"
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
                return True
            except Exception as e:
                print_warning(f"Habit database write failed (non-fatal): {e}")
                return False

    # ── bounded mutation ──────────────────────────────────────────────────

    @staticmethod
    def _hours():
        return [0] * 24

    def _evict(self, bucket_name, limit, weight_key):
        bucket = self.data.setdefault(bucket_name, {})
        if len(bucket) <= limit:
            return
        ranked = sorted(bucket.items(), key=lambda kv: kv[1].get(weight_key, 0))
        for key, _ in ranked[: len(bucket) - limit]:
            bucket.pop(key, None)

    def record_action(self, key: str, hour: int = None, now_ms: float = None):
        """Increments the counter and hour bucket for one user-initiated action."""
        if not key:
            return
        hour = datetime.datetime.now().hour if hour is None else int(hour) % 24
        now_ms = time.time() * 1000.0 if now_ms is None else now_ms
        with self._lock:
            entry = self.data.setdefault("actions", {}).setdefault(
                key[:80], {"count": 0, "hours": self._hours(), "last_ms": 0.0})
            if len(entry.get("hours") or []) != 24:
                entry["hours"] = self._hours()
            entry["count"] += 1
            entry["hours"][hour] += 1
            entry["last_ms"] = now_ms
            self._evict("actions", self.max_actions, "count")

    def record_app_time(self, app: str, seconds: float, hour: int = None, now_ms: float = None):
        """Rolls elapsed foreground time into the per-application totals."""
        if not app or seconds <= 0:
            return
        hour = datetime.datetime.now().hour if hour is None else int(hour) % 24
        now_ms = time.time() * 1000.0 if now_ms is None else now_ms
        with self._lock:
            entry = self.data.setdefault("apps", {}).setdefault(
                app[:60], {"seconds": 0.0, "hours": self._hours(), "last_ms": 0.0})
            if len(entry.get("hours") or []) != 24:
                entry["hours"] = self._hours()
            entry["seconds"] = round(entry["seconds"] + float(seconds), 1)
            entry["hours"][hour] += 1
            entry["last_ms"] = now_ms
            self._evict("apps", self.max_apps, "seconds")

    def record_suggestion(self, kind: str, outcome: str = None, now_ms: float = None):
        """
        Logs that a suggestion of `kind` was offered, and later how the user reacted.

        `outcome` is one of accepted / ignored / dismissed / interrupted. Called with no
        outcome it only records the offer and its timestamp.
        """
        now_ms = time.time() * 1000.0 if now_ms is None else now_ms
        with self._lock:
            entry = self.data.setdefault("suggestions", {}).setdefault(
                kind[:40], {"offered": 0, "accepted": 0, "ignored": 0,
                            "dismissed": 0, "interrupted": 0, "last_ms": 0.0})
            if outcome is None:
                entry["offered"] += 1
                entry["last_ms"] = now_ms
            elif outcome in ("accepted", "ignored", "dismissed", "interrupted"):
                entry[outcome] += 1

    # ── read helpers used by the scorer ───────────────────────────────────

    def action(self, key):
        with self._lock:
            return dict(self.data.get("actions", {}).get(key, {}))

    def actions(self):
        with self._lock:
            return dict(self.data.get("actions", {}))

    def app(self, name):
        with self._lock:
            return dict(self.data.get("apps", {}).get(name, {}))

    def suggestion(self, kind):
        with self._lock:
            return dict(self.data.get("suggestions", {}).get(kind, {}))

    def annoyance(self, kind) -> float:
        """
        How irritating this kind of suggestion has proven, in [0, 1].

        Deliberately gentle: this is adaptation, not surveillance. Three ignores roughly
        halve a candidate's headroom above the threshold; it takes sustained rejection to
        silence a trigger entirely.
        """
        record = self.suggestion(kind)
        offered = record.get("offered", 0)
        if offered < 2:
            return 0.0
        negative = record.get("ignored", 0) + record.get("dismissed", 0) + record.get("interrupted", 0)
        positive = record.get("accepted", 0)
        ratio = (negative - positive) / float(offered)
        return max(0.0, min(1.0, ratio))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              CANDIDATES                                │
# └────────────────────────────────────────────────────────────────────────┘

class Candidate:
    """
    One thing the agent could say, with the local evidence behind it.

    `components` is kept around after scoring purely so a decision is explainable in the
    console — "why did she say that" should never require adding print statements.
    """

    __slots__ = ("kind", "text", "components", "score", "created_s", "context")

    def __init__(self, kind, text, components, context=None):
        self.kind = kind
        self.text = text
        self.components = components
        self.context = context or {}
        self.score = 0.0
        # Stamped by the agent from ITS clock when the candidate is accepted, never here
        # from `time.time()`: the deferral age is compared against the agent's own clock, and
        # mixing the two time sources made every candidate look instantly stale.
        self.created_s = None

    def __repr__(self):
        return f"<Candidate {self.kind} score={self.score:.2f} {self.components}>"


# Weights for the deterministic scorer. They sum to 1.0, so a raw score is directly
# comparable to `score_threshold`.
#
# NOTE the deliberate consequence of these numbers: context alone maxes out at 0.35, which
# is below the 0.6 default threshold. That is the requirement that active-window
# information on its own must never be able to trigger speech — it is enforced by the
# arithmetic, not by a special case, and `tests/test_proactive_agent.py` asserts it.
W_HABIT = 0.30
W_TEMPORAL = 0.25
W_CONTEXT = 0.35
W_RECENCY = 0.10

# Per-suggestion-kind minimum spacing, in seconds. The generic global cooldown applies on
# top of these; whichever is longer wins.
KIND_COOLDOWNS = {
    "break": None,          # taken from config (PROACTIVE_BREAK_COOLDOWN_MINUTES)
    "late_night": 4 * 3600,
    "habit_routine": 2 * 3600,
}

# Template wording. These exist so the subsystem works with no LLM, no network and no cloud
# quota — the LLM is only ever asked to make an already-approved line sound less canned.
# Short on purpose: this is spoken aloud, so one or two sentences is the whole budget.
_TEMPLATES = {
    "break": [
        "You've been in {app} for about {minutes} minutes straight. Want to take a quick break?",
        "That's a long stretch in {app}. Might be worth standing up for a minute.",
    ],
    "late_night": [
        "It's getting late. Worth wrapping up soon if you can.",
        "Pretty late to still be at it. Don't push it too much longer.",
    ],
    "habit_routine": [
        "You usually {action} around this time. Want me to?",
        "This is about when you'd normally {action}. Shall I?",
    ],
}

# Words that mean the user took the suggestion, and words that mean they refused it. Only
# an explicit reaction counts; anything else is recorded as "ignored".
_ACCEPT_WORDS = ("yes", "yeah", "yep", "sure", "okay", "ok", "please do", "go ahead",
                 "good idea", "sounds good", "do it", "thanks", "thank you")
_REJECT_WORDS = ("no", "nope", "not now", "later", "leave it", "don't", "do not",
                 "stop suggesting", "not interested")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE PROACTIVE AGENT                           │
# └────────────────────────────────────────────────────────────────────────┘

class ProactiveAgent:
    """
    Background proactive-suggestion service.

    It is decoupled from the audio and LLM stacks on purpose: it receives `speak_fn`,
    `is_speaking_fn` and `phrase_fn` callables instead of importing the engines. That is
    what makes every decision in here testable without a sound card, and it makes it
    structurally impossible for this module to reach into the TTS engine's cancellation
    state. `create_default_agent()` does the real wiring for main.py.

    Args:
        runtime: `RuntimeState` — read-only source of truth for what the assistant is doing.
        speak_fn: `callable(text) -> bool`. Must route through the existing TTS pipeline as a
            BACKGROUND utterance. Returns True if the text was actually handed over.
        is_speaking_fn: `callable() -> bool`. True while audio is queued/playing.
        phrase_fn: optional `callable(prompt, fallback) -> str|None` used to reword an
            approved candidate. Any failure falls back to the template.
        config: optional `ProactiveConfig` (mostly for tests).
        clock: optional `callable() -> float` epoch seconds, for deterministic tests.
    """

    def __init__(self, runtime=None, speak_fn=None, is_speaking_fn=None,
                 phrase_fn=None, config=None, clock=None):
        self.config = config or ProactiveConfig()
        self.runtime = runtime if runtime is not None else get_runtime_state()
        self.speak_fn = speak_fn
        self.is_speaking_fn = is_speaking_fn or (lambda: False)
        self.phrase_fn = phrase_fn
        self._clock = clock or time.time

        habit_path = data_path("habits.json")
        self.habits = HabitStore(habit_path,
                                 max_actions=self.config.max_habit_actions,
                                 max_apps=self.config.max_habit_apps)

        self.state = ProactiveState.DISABLED if not self.config.enabled else ProactiveState.IDLE
        self._state_lock = threading.RLock()

        # Foreground-window telemetry
        self.current_app = None
        self._app_since = self._clock()
        self._app_committed_at = self._clock()

        # Cooldown ledgers. Keys are cheap strings; both dicts are bounded by the fixed
        # number of suggestion kinds and by `repeat_cooldown_s` pruning.
        self._last_spoken_any = 0.0
        self._last_spoken_kind = {}
        self._last_spoken_text = {}

        # The single approved-but-not-yet-spoken candidate. There is at most one, ever:
        # a queue of unprompted remarks waiting to fire is exactly the failure mode this
        # design exists to prevent.
        self._pending = None

        # Outcome tracking for the suggestion most recently spoken.
        self._awaiting = None          # {"kind", "at", "deadline"}

        self._last_save = self._clock()
        self._stop_event = threading.Event()
        self._thread = None

        # Diagnostics — cheap counters, read by tests and by the shutdown report.
        self.stats = {"ticks": 0, "candidates": 0, "spoken": 0,
                      "deferred": 0, "dropped": 0, "llm_calls": 0}

        self.context_available = gw is not None
        if not self.context_available:
            print_warning("pygetwindow unavailable — proactive context signal disabled "
                          "(time and habit signals still work).")

    # ──────────────────────────────────────────────────────────────────────
    #                              LIFECYCLE
    # ──────────────────────────────────────────────────────────────────────

    def _set_state(self, new_state):
        with self._state_lock:
            self.state = new_state

    def start(self):
        """
        Starts the single background thread.

        Returns immediately — nothing here is allowed to extend Kayra's startup, and the
        first tick does not run until one tick interval has elapsed.
        """
        if not self.config.enabled:
            self._set_state(ProactiveState.DISABLED)
            print_info("Proactive agent disabled by configuration.")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        self._stop_event.clear()
        self._set_state(ProactiveState.OBSERVING)
        self.runtime.subscribe(self.on_event)
        self._thread = threading.Thread(target=self._run, daemon=True, name="kayra-proactive")
        self._thread.start()
        print_success(f"Proactive agent observing (tick {self.config.tick_seconds:.0f}s, "
                      f"threshold {self.config.score_threshold:.2f}).")
        return True

    def stop(self, timeout=3.0):
        """
        Deterministic shutdown: signal, join, flush.

        The thread sleeps on an Event rather than in `time.sleep`, so it wakes on the very
        first line of this method instead of up to a full tick later. Nothing here can keep
        the interpreter alive — the thread is a daemon and the join is bounded.
        """
        self._set_state(ProactiveState.STOPPING)
        self._stop_event.set()
        try:
            self.runtime.unsubscribe(self.on_event)
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._commit_current_app()
        self.habits.save()
        self._thread = None
        self._set_state(ProactiveState.STOPPED)

    def set_enabled(self, enabled: bool):
        """
        Voice/API switch for the current session.

        This changes the AGENT's state only. It is unrelated to the TTS interrupt mechanism:
        "stop" silences whatever is playing, "stop proactive suggestions" turns this
        subsystem off and leaves playback alone.
        """
        self.config.enabled = bool(enabled)
        if enabled:
            self._stop_event.clear()
            if self._thread is None or not self._thread.is_alive():
                self.start()
            else:
                self._set_state(ProactiveState.OBSERVING)
            print_info("Proactive suggestions enabled.")
        else:
            self._pending = None
            self._awaiting = None
            self._set_state(ProactiveState.DISABLED)
            print_info("Proactive suggestions disabled for this session.")

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    # ──────────────────────────────────────────────────────────────────────
    #                          THREAD BODY / TICK
    # ──────────────────────────────────────────────────────────────────────

    def _run(self):
        """
        The one background thread.

        `Event.wait(timeout)` is the whole scheduler: it blocks (zero CPU) for the interval
        and returns True the instant `stop()` fires. The interval shortens only while a
        candidate is waiting for a safe window.
        """
        while True:
            interval = (self.config.defer_poll_seconds if self._pending is not None
                        else self.config.tick_seconds)
            if self._stop_event.wait(interval):
                return
            try:
                self.tick()
            except Exception as e:
                # A bad tick must never take the thread — or the assistant — down.
                print_warning(f"Proactive tick failed (non-fatal): {e}")

    def tick(self):
        """
        One full cheap evaluation cycle. No network, no LLM, no disk (except the periodic
        flush), and no work at all beyond a state read when the agent is off or cooling down.
        """
        self.stats["ticks"] += 1
        now = self._clock()

        if not self.config.enabled or self.runtime.is_shutting_down():
            self._set_state(ProactiveState.DISABLED if not self.config.enabled
                            else ProactiveState.STOPPING)
            return

        # 1. Cheap observation — this is the only thing that runs on most ticks.
        self._observe(now)
        self._resolve_pending_outcome(now)

        # 2. An approved candidate is waiting for the user to be quiet.
        if self._pending is not None:
            self._try_speak_pending(now)
            self._maybe_flush(now)
            return

        # 3. Global cooldown: skip candidate generation entirely. Cheapest possible path,
        #    and it is the one taken for most of the hour after any suggestion.
        if now - self._last_spoken_any < self.config.global_cooldown_s:
            self._set_state(ProactiveState.COOLDOWN)
            self._maybe_flush(now)
            return

        self._set_state(ProactiveState.OBSERVING)

        # 4. Local candidate generation + scoring.
        best = self.evaluate(now)
        if best is not None:
            self.stats["candidates"] += 1
            best.created_s = now
            self._pending = best
            self._set_state(ProactiveState.CANDIDATE)
            print_info(f"[PROACTIVE] candidate '{best.kind}' score={best.score:.2f} "
                       f"{best.components}")
            self._try_speak_pending(now)

        self._maybe_flush(now)

    def _maybe_flush(self, now):
        if now - self._last_save >= self.config.save_interval_s:
            self._commit_current_app(now)
            self.habits.save()
            self._last_save = now

    # ──────────────────────────────────────────────────────────────────────
    #                        SIGNAL 1: ACTIVE WINDOW
    # ──────────────────────────────────────────────────────────────────────

    def _active_app(self):
        if not self.context_available:
            return None
        try:
            window = gw.getActiveWindow()
            if window is None or not getattr(window, "title", None):
                return None
            name = normalize_app_name(window.title)
            if name.lower() in _IGNORED_APP_TITLES:
                return None
            return name
        except Exception:
            return None

    def _commit_current_app(self, now=None):
        """Rolls the time accrued in the current app into the habit store."""
        now = self._clock() if now is None else now
        if self.current_app and self.config.habit_learning:
            elapsed = now - self._app_committed_at
            if elapsed > 0:
                self.habits.record_app_time(self.current_app, elapsed)
        self._app_committed_at = now

    def _observe(self, now):
        """Samples the foreground window and maintains the continuous-focus stopwatch."""
        app = self._active_app()
        if app is None:
            return
        if app != self.current_app:
            self._commit_current_app(now)
            self.current_app = app
            self._app_since = now

    def focus_seconds(self, now=None) -> float:
        """Uninterrupted seconds in the current foreground application."""
        if not self.current_app:
            return 0.0
        return max(0.0, (self._clock() if now is None else now) - self._app_since)

    # ──────────────────────────────────────────────────────────────────────
    #                       CANDIDATE ENGINE + SCORING
    # ──────────────────────────────────────────────────────────────────────

    def evaluate(self, now=None):
        """
        Builds candidates from the three signals, scores them locally, and returns the best
        one that clears both the threshold and its cooldowns — or None.

        Pure and side-effect free apart from reading the habit store, which is what lets the
        test suite drive it directly with a fake clock.
        """
        now = self._clock() if now is None else now
        stamp = datetime.datetime.fromtimestamp(now)

        candidates = []
        for builder in (self._candidate_break, self._candidate_late_night,
                        self._candidate_habit_routine):
            try:
                candidate = builder(now, stamp)
            except Exception:
                candidate = None
            if candidate is not None:
                candidates.append(candidate)

        scored = []
        for candidate in candidates:
            candidate.score = self.score(candidate)
            if candidate.score < self.config.score_threshold:
                continue
            if not self._cooldown_ok(candidate, now):
                continue
            scored.append(candidate)

        if not scored:
            return None
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[0]

    def score(self, candidate) -> float:
        """
        Deterministic relevance score in [0, 1].

            score = w·habit + w·temporal + w·context + w·recency − annoyance

        No model, no training, no state that a human cannot read off `data/habits.json`.
        That is a feature: this decides when the assistant talks unprompted, so it has to be
        predictable and debuggable before it is clever.
        """
        c = candidate.components
        raw = (W_HABIT * _clamp(c.get("habit", 0.0))
               + W_TEMPORAL * _clamp(c.get("temporal", 0.0))
               + W_CONTEXT * _clamp(c.get("context", 0.0))
               + W_RECENCY * _clamp(c.get("recency", 0.0)))
        annoyance = self.habits.annoyance(candidate.kind)
        c["annoyance"] = round(annoyance, 3)
        return round(_clamp(raw - annoyance * 0.5), 3)

    # ── individual signal builders ────────────────────────────────────────

    def _candidate_break(self, now, stamp):
        """
        CONTEXT + TIME + HABIT: a long unbroken stretch in one application.

        Note what is required beyond the window itself — the focus duration has to approach
        the fatigue threshold, the hour has to be a plausible one to be interrupted in, and
        the application has to be one the habit model has actually seen the user work in.
        A freshly-focused unknown app at 4am cannot reach the threshold.
        """
        if not self.current_app:
            return None
        focus = self.focus_seconds(now)
        if focus < self.config.fatigue_seconds * 0.75:
            return None

        app_record = self.habits.app(self.current_app)
        familiarity = _clamp(app_record.get("seconds", 0.0) / (4 * self.config.fatigue_seconds))

        hour = stamp.hour
        # Interrupting someone at 3am to suggest a break is worse than useless; the
        # late-night candidate covers that case with the right message.
        temporal = 1.0 if 8 <= hour <= 22 else 0.2

        components = {
            "habit": familiarity,
            "temporal": temporal,
            "context": _clamp(focus / self.config.fatigue_seconds),
            "recency": 1.0,
        }
        minutes = int(focus // 60)
        text = _pick(_TEMPLATES["break"]).format(app=self.current_app, minutes=minutes)
        return Candidate("break", text, components,
                         context={"app": self.current_app, "minutes": minutes})

    def _candidate_late_night(self, now, stamp):
        """TIME: the user is still at the machine inside the configured late-night window."""
        if not self.config.late_night_enabled:
            return None
        start, end = self.config.late_night_start, self.config.late_night_end
        hour = stamp.hour
        in_window = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if not in_window:
            return None

        # Only worth saying if they are demonstrably still using the machine.
        active = self.current_app is not None or self.runtime.seconds_since_user_utterance() < 1800
        components = {
            "habit": 0.5,
            "temporal": 1.0,
            "context": 0.9 if active else 0.0,
            "recency": 1.0,
        }
        return Candidate("late_night", _pick(_TEMPLATES["late_night"]), components,
                         context={"hour": hour})

    def _candidate_habit_routine(self, now, stamp):
        """
        HABIT: an action the user reliably performs around this hour, that they have not
        performed today.

        The hour histogram is the whole model — `hours[h] / count` is how much of this
        action's history happens in the current hour, which is exactly "temporal relevance"
        without needing anything trained.
        """
        hour = stamp.hour
        best = None
        for key, record in self.habits.actions().items():
            count = record.get("count", 0)
            if count < self.config.habit_min_count:
                continue
            hours = record.get("hours") or []
            if len(hours) != 24 or not hours[hour]:
                continue

            # Already did it recently — suggesting it now would be plainly wrong.
            last_ms = record.get("last_ms", 0.0)
            if last_ms and (now * 1000.0 - last_ms) < 6 * 3600 * 1000.0:
                continue

            temporal = _clamp(hours[hour] / float(max(1, max(hours))))
            habit = _clamp(count / 12.0)
            # Freshness of the evidence: a routine last seen months ago is not a routine.
            age_days = ((now * 1000.0 - last_ms) / 86400000.0) if last_ms else 999.0
            recency = _clamp(1.0 - (age_days / 14.0))

            components = {"habit": habit, "temporal": temporal,
                          "context": 0.5, "recency": recency}
            action_phrase = _humanize_action(key)
            candidate = Candidate("habit_routine",
                                  _pick(_TEMPLATES["habit_routine"]).format(action=action_phrase),
                                  components, context={"key": key, "action": action_phrase})
            candidate.score = self.score(candidate)
            if best is None or candidate.score > best.score:
                best = candidate
        return best

    # ──────────────────────────────────────────────────────────────────────
    #                              COOLDOWNS
    # ──────────────────────────────────────────────────────────────────────

    def _kind_cooldown(self, kind):
        if kind == "break":
            return max(self.config.break_cooldown_s, self.config.global_cooldown_s)
        return max(KIND_COOLDOWNS.get(kind, 0) or 0, self.config.global_cooldown_s)

    def _cooldown_ok(self, candidate, now) -> bool:
        """
        Anti-spam gate. Three independent timers, all of which must have expired:
        the global one, the per-kind one, and the per-exact-wording one.

        The last of those is the backstop against the classic failure — a scheduler bug that
        re-fires the identical sentence in a loop cannot get past it even if the other two
        are misconfigured to zero.
        """
        if now - self._last_spoken_any < self.config.global_cooldown_s:
            return False
        if now - self._last_spoken_kind.get(candidate.kind, 0.0) < self._kind_cooldown(candidate.kind):
            return False
        if now - self._last_spoken_text.get(candidate.text, 0.0) < self.config.repeat_cooldown_s:
            return False
        return True

    # ──────────────────────────────────────────────────────────────────────
    #                       SAFETY GATE  ("never interrupt")
    # ──────────────────────────────────────────────────────────────────────

    def is_safe_window(self):
        """
        Returns (ok, reason). Everything that could mean "the user is mid-anything" is
        checked here, and this is the ONLY place proactive speech is authorised.

        The checks are ordered cheapest-first and all read already-resident values.
        """
        if not self.config.enabled:
            return False, "disabled"

        snap = self.runtime.snapshot()
        if snap["shutting_down"]:
            return False, "shutting down"
        if snap["busy"]:
            # PROCESSING / SPEAKING / INTERRUPTING / AUTOMATING — a turn is in flight.
            return False, f"assistant {snap['state'].lower()}"
        if snap["turn_active"]:
            return False, "turn in flight"
        if snap["state"] not in (AssistantState.IDLE, AssistantState.LISTENING):
            return False, f"state {snap['state']}"

        # Anything queued, synthesizing or audible counts as speaking, including a response
        # whose last sentence is still in the playback buffer.
        try:
            if self.is_speaking_fn():
                return False, "audio still playing"
        except Exception:
            return False, "speech state unknown"

        # A barge-in means the user just took the floor. Give it a wide berth: speaking into
        # that gap is the single most annoying thing this subsystem could do.
        if snap["seconds_since_interrupt"] < self.config.quiet_after_interaction_s:
            return False, "recently interrupted"
        if snap["seconds_since_user_utterance"] < self.config.quiet_after_interaction_s:
            return False, "user recently spoke"
        if snap["seconds_since_turn_end"] < self.config.quiet_after_interaction_s:
            return False, "turn just ended"

        return True, "ok"

    # ──────────────────────────────────────────────────────────────────────
    #                          SPEAKING & PHRASING
    # ──────────────────────────────────────────────────────────────────────

    def _try_speak_pending(self, now):
        """Speaks the approved candidate if the moment is safe; otherwise defers or drops it."""
        candidate = self._pending
        if candidate is None:
            return False

        ok, reason = self.is_safe_window()
        if not ok:
            age = now - (candidate.created_s if candidate.created_s is not None else now)
            if age > self.config.max_defer_s:
                self.stats["dropped"] += 1
                print_info(f"[PROACTIVE] dropping stale '{candidate.kind}' candidate "
                           f"after {age:.0f}s ({reason}).")
                self._pending = None
                self._set_state(ProactiveState.OBSERVING)
            else:
                self.stats["deferred"] += 1
                self._set_state(ProactiveState.WAITING_FOR_SAFE_WINDOW)
            return False

        self._set_state(ProactiveState.SPEAKING)
        text = self._phrase(candidate)
        spoken = False
        try:
            if self.speak_fn is not None:
                spoken = bool(self.speak_fn(text))
        except Exception as e:
            print_warning(f"Proactive speech dispatch failed: {e}")
            spoken = False

        self._pending = None
        if spoken:
            self.stats["spoken"] += 1
            self._last_spoken_any = now
            self._last_spoken_kind[candidate.kind] = now
            self._prune_text_cooldowns(now)
            self._last_spoken_text[text] = now
            self.habits.record_suggestion(candidate.kind)
            self._awaiting = {"kind": candidate.kind, "at": now,
                              "deadline": now + 300.0}
            print_system(f"[PROACTIVE] {text}")
            self._set_state(ProactiveState.COOLDOWN)
        else:
            self._set_state(ProactiveState.OBSERVING)
        return spoken

    def _prune_text_cooldowns(self, now):
        """Keeps the per-wording ledger bounded — entries past their cooldown are dead weight."""
        expired = [t for t, ts in self._last_spoken_text.items()
                   if now - ts > self.config.repeat_cooldown_s]
        for t in expired:
            self._last_spoken_text.pop(t, None)

    def _phrase(self, candidate) -> str:
        """
        Produces the exact sentence to speak.

        The template IS the answer unless the LLM is both configured and available, and even
        then the model's output has to survive validation. Everything about this method is
        arranged so that no network, no quota and no cloud outage can stop a suggestion from
        being spoken — the LLM only makes it sound less canned.
        """
        fallback = candidate.text
        if not (self.config.llm_phrasing and self.phrase_fn):
            return fallback

        prompt = (
            "Rewrite this assistant nudge so it sounds natural and spoken aloud. "
            "One short sentence, at most two. No markdown, no emoji, no lists, no URLs, "
            "no explanation of why you are saying it. Reply with the sentence only.\n\n"
            f"Nudge: {fallback}"
        )
        try:
            self.stats["llm_calls"] += 1
            generated = self.phrase_fn(prompt, fallback)
        except Exception:
            return fallback
        return _validate_phrasing(generated, fallback)

    # ──────────────────────────────────────────────────────────────────────
    #                         LEARNING FROM THE USER
    # ──────────────────────────────────────────────────────────────────────

    def on_event(self, event, payload):
        """
        Runtime event-bus subscriber. Called synchronously on the emitting thread, so it
        must stay to counter arithmetic — no disk, no locks held across work.
        """
        try:
            if event == "intent_classified":
                self.note_intents(payload.get("tokens") or [])
                self._react_to_user_text(payload.get("text") or "")
            elif event == "user_utterance":
                self._react_to_user_text(payload.get("text") or "")
            elif event == "barge_in":
                if self._awaiting is not None:
                    self._record_outcome("interrupted")
                # A candidate waiting to speak has just been overtaken by the user.
                self._pending = None
        except Exception:
            pass

    def note_intents(self, tokens):
        """
        Habit learning from the DMM's output.

        Only the ACTION is recorded — the token header plus, for the couple of tokens where
        it is genuinely the identity of the habit, a short normalized payload. Conversation
        tokens ('general', 'realtime', 'deep research') are counted by header alone and
        their payloads are discarded: this model is about what the user does, and storing
        what they asked would make it a transcript log.
        """
        if not self.config.habit_learning:
            return
        for token in tokens:
            key = _habit_key(token)
            if key:
                self.habits.record_action(key)

    def _react_to_user_text(self, text):
        """Classifies the user's next utterance as a reaction to the suggestion just made."""
        if self._awaiting is None or not text:
            return
        lowered = text.strip().lower()
        if any(w in lowered for w in _REJECT_WORDS):
            self._record_outcome("dismissed")
        elif any(lowered.startswith(w) or lowered == w for w in _ACCEPT_WORDS):
            self._record_outcome("accepted")
        else:
            # They said something else entirely — the nudge went unanswered.
            self._record_outcome("ignored")

    def _resolve_pending_outcome(self, now):
        """A suggestion nobody reacted to inside the window counts as ignored."""
        if self._awaiting is not None and now >= self._awaiting["deadline"]:
            self._record_outcome("ignored")

    def _record_outcome(self, outcome):
        awaiting = self._awaiting
        if awaiting is None:
            return
        self._awaiting = None
        self.habits.record_suggestion(awaiting["kind"], outcome=outcome)
        print_info(f"[PROACTIVE] '{awaiting['kind']}' suggestion -> {outcome} "
                   f"(annoyance now {self.habits.annoyance(awaiting['kind']):.2f})")

    # ──────────────────────────────────────────────────────────────────────
    #                             DIAGNOSTICS
    # ──────────────────────────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "state": self.state,
            "enabled": self.config.enabled,
            "current_app": self.current_app,
            "focus_seconds": round(self.focus_seconds(), 1),
            "pending": self._pending.kind if self._pending else None,
            "awaiting_outcome": self._awaiting["kind"] if self._awaiting else None,
            "stats": dict(self.stats),
        }


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SMALL PURE HELPERS                            │
# └────────────────────────────────────────────────────────────────────────┘

def _clamp(value, low=0.0, high=1.0):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _pick(options):
    return random.choice(options) if options else ""


# Token headers whose payload IS the habit's identity (opening Chrome and opening Spotify
# are different habits). Everything else is counted by header only.
_PAYLOAD_HABITS = ("open", "play", "close")

# Headers that describe conversation rather than an action worth predicting.
_CONVERSATION_HEADERS = ("general", "realtime", "deep research", "content", "write",
                         "copy text", "exit")


def _habit_key(token: str):
    """Maps a DMM task token to a bounded, non-identifying habit key, or None."""
    if not token or not isinstance(token, str):
        return None
    lowered = token.strip().lower()
    if not lowered:
        return None
    if any(lowered.startswith(h) for h in _CONVERSATION_HEADERS):
        return None
    for header in _PAYLOAD_HABITS:
        if lowered.startswith(header + " "):
            payload = lowered[len(header):].strip()[:24]
            return f"{header}:{payload}" if payload else None
    # Parameterless tokens ('battery', 'minimize all', 'take screenshot', ...) are their own
    # identity; anything carrying free text is reduced to its first two words so the key
    # space stays bounded.
    return " ".join(lowered.split()[:2])


def _humanize_action(key: str) -> str:
    """Turns a habit key back into something speakable: 'open:chrome' -> 'open chrome'."""
    if ":" in key:
        header, payload = key.split(":", 1)
        return f"{header} {payload}".strip()
    return key


def _validate_phrasing(generated, fallback: str) -> str:
    """
    Accepts an LLM rewording only if it is actually better-formed than the template.

    Rejects anything long, multi-sentence, or carrying screen-only formatting. This is a
    hard gate rather than a cleanup pass because the failure it guards against — a model
    returning a paragraph of explanation, or markdown, straight into the speaker — is far
    worse than the canned template it would replace.
    """
    if not generated or not isinstance(generated, str):
        return fallback
    text = generated.strip().strip('"').strip()
    if not text or len(text) > 220:
        return fallback
    if any(marker in text for marker in ("```", "http://", "https://", "* ", "- ", "|", "#")):
        return fallback
    if text.count(".") + text.count("!") + text.count("?") > 2:
        return fallback
    # Route through the same normalization every other spoken line uses, so proactive speech
    # can never bypass the speech pipeline's rules.
    speakable = speech_safe_text(text)
    return speakable.strip() or fallback


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       DEFAULT WIRING FOR main.py                       │
# └────────────────────────────────────────────────────────────────────────┘

def create_default_agent(tts_engine=None, llm_engine=None, runtime=None):
    """
    Builds a ProactiveAgent wired to the process's ONE TTS engine and ONE LLM engine.

    This is where the integration rules are enforced, in one readable place:

      * speech goes through the existing pipeline via `begin_background_utterance()` +
        `speak()` — no second queue, no second audio stream, and no `begin_turn()`, which
        would clear the interrupt latch a cancelled response depends on;
      * a final `is_playing` check immediately before handing text over closes the small
        window between the safety gate and the queue push;
      * phrasing reuses the shared `CentralizedLLMEngine` (never a new client) and falls
        back to the template on any failure at all.
    """
    runtime = runtime if runtime is not None else get_runtime_state()

    def _speak(text):
        if not (tts_engine and text):
            return False
        # Last-instant recheck. The safety gate ran up to a few milliseconds ago and the
        # user may have started a turn since; losing the nudge is the correct outcome.
        if tts_engine.is_playing or runtime.is_busy() or runtime.is_shutting_down():
            return False
        tts_engine.begin_background_utterance()
        tts_engine.speak(text)
        return True

    def _is_speaking():
        return bool(tts_engine and tts_engine.is_playing)

    phrase_fn = None
    if llm_engine is not None:
        def phrase_fn(prompt, fallback):  # noqa: F811 - deliberate conditional definition
            chunks = []
            for chunk in llm_engine.generate_chat_stream(
                    [{"role": "system",
                      "content": "You rewrite one-line spoken assistant nudges. "
                                 "Reply with the rewritten sentence and nothing else."},
                     {"role": "user", "content": prompt}]):
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > 400:
                    break
            return "".join(chunks).strip() or fallback

    return ProactiveAgent(runtime=runtime, speak_fn=_speak,
                          is_speaking_fn=_is_speaking, phrase_fn=phrase_fn)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     DIAGNOSTIC TEST RUNTIME BLOCK                      │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    from kayra.core.config import load_environment
    load_environment()

    print_system("Proactive agent standalone diagnostic (Ctrl+C to stop). "
                 "Suggestions are printed, not spoken.")
    agent = ProactiveAgent(speak_fn=lambda text: (print_success(f"SUGGESTION -> {text}"), True)[1])
    agent.start()
    try:
        while True:
            time.sleep(5)
            print_info(str(agent.describe()))
    except KeyboardInterrupt:
        agent.stop()
        print_system("Proactive agent stopped.")
