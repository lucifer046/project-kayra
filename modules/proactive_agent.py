# ┌────────────────────────────────────────────────────────────────────────┐
# │                          proactive_agent.py                            │
# │               Autonomous Habit Tracking & Context Engine               │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module allows the AI to monitor the user's active screen context,
learn their daily habits, and autonomously trigger suggestions based on
time, fatigue, or historical patterns.

Design notes:
- Runs entirely on a background daemon thread; never blocks the main loop.
- All failures are swallowed and logged as warnings — a habit-tracking hiccup
  must never be able to take down the assistant's core conversation loop.
- Fully optional: if `pygetwindow` (Windows-only) or `schedule` are missing,
  the agent degrades to a no-op rather than raising on import/construction.
"""

import os
import json
import time
import threading
import datetime

try:
    import schedule
    SCHEDULE_AVAILABLE = True
except ImportError:
    schedule = None
    SCHEDULE_AVAILABLE = False

# PyGetWindow handles Windows active-window introspection. Not available on other platforms.
try:
    import pygetwindow as gw
except ImportError:
    gw = None

try:
    from .utils import print_info, print_warning, print_error, print_system, print_success, get_project_root
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_system, print_success, get_project_root
    except ImportError:
        from utils import print_info, print_warning, print_error, print_system, print_success, get_project_root


# Windows title suffixes (browser tab names, editor file paths, etc.) that would otherwise
# make the same application register as dozens of distinct "apps" in the habit log.
_TITLE_SEPARATORS = (" - ", " — ", " | ")

# Minimum continuous time (seconds) in a single app before we consider suggesting a break.
FATIGUE_THRESHOLD_SECONDS = int(os.environ.get("PROACTIVE_FATIGUE_MINUTES", "90")) * 60

# How often the tracker samples the active window.
POLL_INTERVAL_SECONDS = 15

# How often accumulated habit data is flushed to disk.
SAVE_INTERVAL_SECONDS = 300


def _normalize_app_name(raw_title: str) -> str:
    """Collapses a raw window title down to a stable 'app identity' string for habit tracking."""
    if not raw_title:
        return "Unknown"
    title = raw_title.strip()
    for sep in _TITLE_SEPARATORS:
        if sep in title:
            # The tail segment is usually the actual application name
            # (e.g. "index.py - Visual Studio Code" -> "Visual Studio Code").
            title = title.split(sep)[-1].strip()
    return title[:60] if title else "Unknown"


class ProactiveAgent:
    """
    Background habit-tracking and proactive-suggestion engine.

    Usage:
        agent = ProactiveAgent()
        agent.start(on_suggestion=lambda text: tts_engine.speak(text))
        ...
        agent.stop()
    """

    def __init__(self):
        print_info("Booting Proactive Context & Habit Engine...")

        # Resolve paths
        self.root_dir = get_project_root()
        self.data_dir = os.path.join(self.root_dir, "data")
        self.habit_file = os.path.join(self.data_dir, "habits.json")
        os.makedirs(self.data_dir, exist_ok=True)

        # Core State Variables
        self.habits = self._load_habits()
        self.is_running = False
        self._thread = None

        # Telemetry Memory
        self.current_app = None
        self.app_start_time = time.time()
        self._last_save_time = time.time()

        # Suggestion Cooldowns (Prevent the AI from spamming the user)
        self.last_suggestion_time = 0.0
        self.cooldown_seconds = 3600  # Max 1 proactive suggestion per hour

        # Threading Locks for thread-safe JSON writing
        self.lock = threading.Lock()

        self.capabilities_ok = gw is not None
        if not self.capabilities_ok:
            print_warning("pygetwindow unavailable — proactive habit tracking will stay idle on this platform.")

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │                         DATABASE MANAGEMENT                            │
    # └────────────────────────────────────────────────────────────────────────┘

    def _load_habits(self):
        """Loads the persistent habit-tracking database, tolerating a missing/corrupt file."""
        try:
            with open(self.habit_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"app_totals": {}, "last_updated": None}

    def _save_habits(self):
        """Persists the habit-tracking database to disk (thread-safe)."""
        with self.lock:
            try:
                self.habits["last_updated"] = datetime.datetime.now().isoformat()
                tmp_path = self.habit_file + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.habits, f, indent=4, ensure_ascii=False)
                os.replace(tmp_path, self.habit_file)
            except Exception as e:
                print_warning(f"Habit database write failed (non-fatal): {e}")

    def _record_usage(self, app_name: str, seconds: float):
        """Accumulates elapsed time for the given app into the running habit totals."""
        totals = self.habits.setdefault("app_totals", {})
        totals[app_name] = round(totals.get(app_name, 0) + seconds, 1)

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │                        ACTIVE WINDOW TRACKING                          │
    # └────────────────────────────────────────────────────────────────────────┘

    def _get_active_app(self):
        """Returns the normalized name of the currently focused application, or None."""
        if not gw:
            return None
        try:
            active = gw.getActiveWindow()
            if active is None or not active.title:
                return None
            return _normalize_app_name(active.title)
        except Exception:
            return None

    def _poll_once(self):
        """Samples the active window once and rolls elapsed time into the habit log."""
        app = self._get_active_app()
        now = time.time()

        if app is None:
            self.current_app = None
            self.app_start_time = now
            return

        if app != self.current_app:
            # Application focus changed — commit the time spent in the previous app.
            if self.current_app is not None:
                self._record_usage(self.current_app, now - self.app_start_time)
            self.current_app = app
            self.app_start_time = now
        # else: still in the same app, elapsed time accrues on the next switch/flush.

        if now - self._last_save_time >= SAVE_INTERVAL_SECONDS:
            if self.current_app is not None:
                self._record_usage(self.current_app, now - self.app_start_time)
                self.app_start_time = now
            self._save_habits()
            self._last_save_time = now

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │                       PROACTIVE SUGGESTION LOGIC                       │
    # └────────────────────────────────────────────────────────────────────────┘

    def _maybe_generate_suggestion(self):
        """
        Cooldown-gated heuristic check for whether a proactive nudge is warranted right now.

        Returns:
            str | None: A suggestion string to speak/print, or None if nothing is warranted.
        """
        now = time.time()
        if now - self.last_suggestion_time < self.cooldown_seconds:
            return None

        # 1. Fatigue check: user has been continuously focused on the same app for a long stretch.
        if self.current_app and (now - self.app_start_time) >= FATIGUE_THRESHOLD_SECONDS:
            minutes = int((now - self.app_start_time) // 60)
            self.last_suggestion_time = now
            return (
                f"You've been in {self.current_app} for about {minutes} minutes straight. "
                f"Might be a good moment to stretch or take a short break."
            )

        # 2. Late-night check: gently nudge toward winding down after midnight.
        hour = datetime.datetime.now().hour
        if 1 <= hour < 5:
            self.last_suggestion_time = now
            return "It's pretty late — don't stay up too much longer if you can help it."

        return None

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │                      BACKGROUND THREAD LIFECYCLE                       │
    # └────────────────────────────────────────────────────────────────────────┘

    def _run_loop(self, on_suggestion):
        """The background daemon thread body: polls, evaluates, and dispatches suggestions."""
        if SCHEDULE_AVAILABLE:
            schedule.every(POLL_INTERVAL_SECONDS).seconds.do(self._safe_tick, on_suggestion)

        while self.is_running:
            try:
                if SCHEDULE_AVAILABLE:
                    schedule.run_pending()
                else:
                    self._safe_tick(on_suggestion)
                time.sleep(1)
            except Exception as e:
                print_warning(f"Proactive agent tick failed (non-fatal): {e}")
                time.sleep(POLL_INTERVAL_SECONDS)

    def _safe_tick(self, on_suggestion):
        """One full poll + suggestion-evaluation cycle, isolated so a single bad tick can't crash the loop."""
        self._poll_once()
        suggestion = self._maybe_generate_suggestion()
        if suggestion and on_suggestion:
            try:
                on_suggestion(suggestion)
            except Exception as e:
                print_warning(f"Proactive suggestion dispatch failed: {e}")

    def start(self, on_suggestion=None):
        """
        Starts the background tracking/suggestion thread.

        Args:
            on_suggestion (callable): Invoked with a str suggestion whenever the agent
                                       decides a proactive nudge is warranted (e.g. wire this
                                       to `tts_engine.speak`). Optional — if omitted, suggestions
                                       are simply logged.
        """
        if not self.capabilities_ok:
            return  # Silently stay idle — nothing to track on this platform.
        if self.is_running:
            return
        self.is_running = True

        def _on_suggestion(text):
            print_system(f"[Proactive Suggestion] {text}")
            if on_suggestion:
                on_suggestion(text)

        self._thread = threading.Thread(target=self._run_loop, args=(_on_suggestion,), daemon=True)
        self._thread.start()
        print_success("Proactive Context & Habit Engine active. Monitoring foreground application usage.")

    def stop(self):
        """Stops the background thread and flushes any pending habit data to disk."""
        self.is_running = False
        if self.current_app is not None:
            self._record_usage(self.current_app, time.time() - self.app_start_time)
        self._save_habits()
        if SCHEDULE_AVAILABLE:
            schedule.clear()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     DIAGNOSTIC TEST RUNTIME BLOCK                      │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_system("Running Proactive Agent in standalone diagnostic mode (Ctrl+C to stop)...")
    agent = ProactiveAgent()
    agent.start(on_suggestion=lambda text: print_success(f"SUGGESTION -> {text}"))
    try:
        while True:
            time.sleep(5)
            print_info(f"Currently tracking: {agent.current_app}")
    except KeyboardInterrupt:
        agent.stop()
        print_system("Proactive agent stopped.")
