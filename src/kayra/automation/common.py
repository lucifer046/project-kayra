# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation/common.py                           │
# │        Platform-neutral automation: normalizer, planner, executor      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything in the automation pipeline that does NOT touch an operating system.

    token -> normalize_command() -> Action -> classify_action() -> plan_actions()
          -> AutomationRuntime.execute_action() -> <platform dispatch> -> ActionResult

The Windows backend (`automation.windows`) and the Linux backend (`automation.linux`) both
build on this module, so a DMM token means exactly the same structured action on every OS.
Before this split the normalizer lived inside `windows.py`, which imports pyautogui, keyboard
and AppOpener at module level — so on Linux there was no way to reach even the pure string
parsing, and the whole automation layer reported itself "not ported".

RULES
-----
* **No OS imports.** Nothing here imports a desktop library, `ctypes.windll`, `win32*`, or
  spawns a process at import time. `tests/test_linux_automation.py` imports it on Linux.
* **The safety gates stay where they were.** The power-target gate lives in `_build_action`,
  the policy in `automation.policy`; this module only moved them, it did not loosen them.
* **One executor.** `AutomationRuntime` owns policy-then-dispatch-then-record, confirmations,
  the bounded context and the timer service. A backend supplies a `dispatch(action)` callable
  and nothing else decides whether an action runs.
"""

import os
import re
import time
import shlex
import shutil
import asyncio
import threading
import subprocess

from kayra.automation import policy
from kayra.automation.policy import (Action, ActionResult, Status, Risk, classify_action,
                                     ConfirmationManager, AutomationContext, audit,
                                     AUDIT_STARTED, AUDIT_SUCCESS, AUDIT_FAILED,
                                     AUDIT_BLOCKED, AUDIT_CONFIRM, AUDIT_AMBIGUOUS,
                                     is_protected_path, read_confirmation_reply)
from kayra.automation import targets
from kayra.utils import print_info, print_success, print_warning


class TimerService:
    """
    One bounded, cancellable timer service for the whole process.

    The old `SetTimer` started a fresh `threading.Thread` per timer that slept for the whole
    duration. That meant: a thread per timer for up to an hour each, no way to cancel one, no
    way to list them, and nothing that stopped them at shutdown — a two-hour timer kept a
    thread alive and fired a PowerShell message box after the assistant had exited.

    This uses `threading.Timer`, which is still a thread per pending timer but a cancellable
    one, and adds an explicit registry with a hard cap so a stuck loop cannot create thousands.
    `shutdown()` cancels every outstanding timer, so nothing survives the process.
    """

    MAX_ACTIVE = 16

    def __init__(self, notify=None):
        # `notify(label)` is the platform's way of announcing a fired timer (a Windows message
        # box, a Linux desktop notification). Injected so this class stays platform-neutral.
        self._notify = notify
        try:
            self.MAX_ACTIVE = max(1, min(128, int(
                str(os.environ.get("AUTOMATION_MAX_TIMERS", "")).strip())))
        except (TypeError, ValueError):
            pass
        self._lock = threading.RLock()
        self._timers = {}                 # id -> {"timer", "label", "due", "seconds"}
        self._next_id = 1

    def add(self, seconds, label, on_fire=None):
        """Arms a timer. Returns (id, error_message). `id` is None when it was refused."""
        if seconds <= 0:
            return None, "That duration doesn't make sense."
        with self._lock:
            if len(self._timers) >= self.MAX_ACTIVE:
                return None, f"I already have {len(self._timers)} timers running."
            timer_id = self._next_id
            self._next_id += 1

            def _fire():
                with self._lock:
                    self._timers.pop(timer_id, None)
                print_success(f"Timer complete: {label}")
                if on_fire:
                    try:
                        on_fire(label)
                    except Exception:
                        pass
                elif self._notify:
                    try:
                        self._notify(label)
                    except Exception:
                        pass

            handle = threading.Timer(seconds, _fire)
            handle.daemon = True
            handle.name = f"kayra-timer-{timer_id}"
            handle.start()
            self._timers[timer_id] = {"timer": handle, "label": label,
                                      "due": time.time() + seconds, "seconds": seconds}
            return timer_id, None

    def cancel(self, timer_id=None):
        """Cancels one timer, or the most recent when no id is given. Returns its label."""
        with self._lock:
            if not self._timers:
                return None
            if timer_id is None:
                timer_id = max(self._timers)
            entry = self._timers.pop(timer_id, None)
        if entry is None:
            return None
        entry["timer"].cancel()
        return entry["label"]

    def list(self):
        with self._lock:
            now = time.time()
            return [{"id": tid, "label": e["label"], "remaining": max(0.0, e["due"] - now)}
                    for tid, e in sorted(self._timers.items())]

    def shutdown(self):
        """Cancels everything. Called from the assistant's shutdown path."""
        with self._lock:
            entries = list(self._timers.values())
            self._timers.clear()
        for entry in entries:
            try:
                entry["timer"].cancel()
            except Exception:
                pass
        return len(entries)


def parse_clock_reminder(text, now=None):
    """
    ('9:00pm call mom') -> (seconds until 9 pm, 'call mom'). (None, text) without a time.
    A time already past today means tomorrow. Bare hours below 8 with no am/pm are the
    afternoon ("at 3" is 3 pm), the same reading the calendar uses.
    """
    import datetime as _dt
    now = now or _dt.datetime.now()
    low = str(text or "").lower()
    match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$|,)", low)
    if not match or (not match.group(2) and not match.group(3) and "at " not in low):
        return None, text
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "")
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not meridiem and hour < 8:
        hour += 12
    if hour > 23 or minute > 59:
        return None, text
    day = now.date() + _dt.timedelta(days=1 if "tomorrow" in low else 0)
    due = _dt.datetime.combine(day, _dt.time(hour, minute))
    if due <= now:
        due += _dt.timedelta(days=1)
    label = (low[:match.start()] + " " + low[match.end():])
    label = re.sub(r"\b(remind me|to|about|at|tomorrow|today|on|wake me up|wake me)\b", " ", label)
    label = " ".join(label.split()).strip(" ,.") or f"it's {due:%-I:%M %p}"
    return int((due - now).total_seconds()), label


def parse_duration(text):
    """
    Parses '5 minutes', 'an hour and 30 minutes', '90 sec' into seconds. Returns None if it
    cannot. Sums every unit it finds, so compound durations work.
    """
    if not text:
        return None
    lowered = str(text).lower()
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
                                  lowered):
        amount = int(value)
        if unit.startswith(("hour", "hr", "h")):
            total += amount * 3600
        elif unit.startswith(("min", "m")):
            total += amount * 60
        else:
            total += amount
        found = True
    if not found:
        # "an hour", "half an hour"
        if "half an hour" in lowered or "half hour" in lowered:
            return 1800
        if re.search(r"\ban hour\b", lowered):
            return 3600
        if re.search(r"\ba minute\b", lowered):
            return 60
        return None
    return total or None


def _humanize_duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {minutes} minutes" if minutes else "")
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}" + (f" {rest} seconds" if rest else "")
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _writable_base(name):
    """
    Resolves the parent directory for a new file/folder.

    A bare name means the Desktop — that is what "make a folder called notes" means to a
    person. An explicit path is honoured as given.
    """
    raw = str(name).strip().strip('"')
    if os.path.isabs(raw) or "\\" in raw or "/" in raw:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        return os.path.dirname(expanded) or os.getcwd(), os.path.basename(expanded)
    home = os.path.expanduser("~")
    desktop = next((p for p in (os.path.join(home, "OneDrive", "Desktop"),
                                os.path.join(home, "Desktop")) if os.path.isdir(p)), home)
    return desktop, raw


def CreateFolder(name):
    parent, leaf = _writable_base(name)
    if not leaf:
        return ActionResult.failure("What should I call it?")
    path = os.path.join(parent, leaf)
    try:
        if os.path.exists(path):
            return ActionResult.failure(f"{leaf} already exists.")
        os.makedirs(path)
        return (ActionResult.success(f"Created {leaf}.", path=path) if os.path.isdir(path)
                else ActionResult.failure(f"I couldn't create {leaf}."))
    except Exception as e:
        return ActionResult.failure(f"I couldn't create {leaf}.", error=str(e))


def CreateFile(name, content=""):
    parent, leaf = _writable_base(name)
    if not leaf:
        return ActionResult.failure("What should I call it?")
    path = os.path.join(parent, leaf)
    try:
        if os.path.exists(path):
            return ActionResult.failure(f"{leaf} already exists.")
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content or "")
        return (ActionResult.success(f"Created {leaf}.", path=path) if os.path.isfile(path)
                else ActionResult.failure(f"I couldn't create {leaf}."))
    except Exception as e:
        return ActionResult.failure(f"I couldn't create {leaf}.", error=str(e))


def RenamePath(source, new_name):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    path = resolution.target
    destination = os.path.join(os.path.dirname(path), str(new_name).strip())
    try:
        if os.path.exists(destination):
            return ActionResult.failure("Something with that name already exists.")
        os.rename(path, destination)
        return (ActionResult.success("Renamed.", path=destination)
                if os.path.exists(destination) else ActionResult.failure("The rename failed."))
    except Exception as e:
        return ActionResult.failure("The rename failed.", error=str(e))


def CopyPath(source, destination):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    src = resolution.target
    dst_resolution = targets.resolve_path(destination, kind="folder")
    dst = (dst_resolution.target if dst_resolution.ok
           else os.path.expandvars(os.path.expanduser(str(destination))))
    try:
        final = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        if os.path.isdir(src):
            shutil.copytree(src, final)
        else:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            shutil.copy2(src, final)
        return (ActionResult.success("Copied.", path=final) if os.path.exists(final)
                else ActionResult.failure("The copy failed."))
    except Exception as e:
        return ActionResult.failure("The copy failed.", error=str(e))


def MovePath(source, destination):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    src = resolution.target
    dst_resolution = targets.resolve_path(destination, kind="folder")
    dst = (dst_resolution.target if dst_resolution.ok
           else os.path.expandvars(os.path.expanduser(str(destination))))
    try:
        final = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        if os.path.exists(final):
            return ActionResult.failure("Something with that name is already there.")
        shutil.move(src, final)
        return (ActionResult.success("Moved.", path=final) if os.path.exists(final)
                else ActionResult.failure("The move failed."))
    except Exception as e:
        return ActionResult.failure("The move failed.", error=str(e))


def DeletePath(target):
    """
    Deletes a file or folder. CONFIRM-gated by policy — never reached without approval.

    Sends to the Recycle Bin when send2trash is available, because a recoverable delete is
    strictly better and costs nothing. Refuses a protected system location regardless.
    """
    resolution = targets.resolve_path(target)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {target}.")
    path = resolution.target

    if is_protected_path(path):
        return ActionResult.blocked("That's a protected system location. I won't delete it.")

    try:
        try:
            from send2trash import send2trash
            send2trash(path)
            recycled = True
        except Exception:
            recycled = False
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        if os.path.exists(path):
            return ActionResult.failure("I couldn't delete it.")
        return ActionResult.success(
            "Moved to the Recycle Bin." if recycled else "Deleted.", path=path)
    except Exception as e:
        return ActionResult.failure("I couldn't delete it.", error=str(e))


def SearchFiles(query, root=None, limit=20):
    """
    Finds files by name under one folder.

    Bounded on purpose: one root, `os.scandir`, a depth cap and a result cap. A full-disk walk
    is unbounded work that would block a spoken command for minutes.
    """
    base = root or os.path.join(os.path.expanduser("~"))
    needle = str(query).strip().lower()
    if not needle:
        return ActionResult.failure("Search for what?")
    matches = []
    max_depth = 4

    def _walk(directory, depth):
        if depth > max_depth or len(matches) >= limit:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(matches) >= limit:
                        return
                    if entry.name.startswith((".", "$")):
                        continue
                    if needle in entry.name.lower():
                        matches.append(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        _walk(entry.path, depth + 1)
        except (PermissionError, OSError):
            return

    _walk(base, 0)
    if not matches:
        return ActionResult.not_found(f"I couldn't find anything called {query}.")
    for path in matches[:10]:
        print_info(path)
    noun = "match" if len(matches) == 1 else "matches"
    return ActionResult.success(f"Found {len(matches)} {noun}.", matches=matches)


# ── Safe terminal ─────────────────────────────────────────────────────────
def RunShellCommand(command, timeout=None):
    """
    Runs a terminal command that the POLICY has already approved.

    Called only after `classify_shell` returned ALLOW, or CONFIRM plus an explicit yes. The
    command is split into an argument vector and run with `shell=False`, so the user's words
    can never become shell syntax — no chaining, no redirection, no substitution.

    This function does not decide whether the command is safe. That decision belongs to
    `automation_policy` and is made before we get here; duplicating it would create two places
    that can disagree.
    """
    try:
        # Windows keeps backslashes literal (posix=False); POSIX shells need real quoting rules.
        argv = shlex.split(str(command), posix=(os.name != "nt"))
    except ValueError:
        return ActionResult.failure("I couldn't parse that command.")
    if not argv:
        return ActionResult.failure("There was no command to run.")

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, shell=False,
                                   timeout=timeout if timeout is not None else SHELL_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return ActionResult.not_found(f"I couldn't find {argv[0]}.")
    except subprocess.TimeoutExpired:
        return ActionResult.failure("That command took too long, so I stopped it.")
    except Exception as e:
        return ActionResult.failure("The command didn't run.", error=str(e))

    output = (completed.stdout or completed.stderr or "").strip()
    if output:
        print_info(output[:2000])
    if completed.returncode == 0:
        return ActionResult.success("Done.", returncode=0, output_chars=len(output))
    return ActionResult.failure(f"That finished with an error.", returncode=completed.returncode)


# ── Screenshot retention ──────────────────────────────────────────────────
def _env_int(name, default, minimum=1, maximum=100000):
    try:
        return max(minimum, min(maximum, int(str(os.environ.get(name, "")).strip())))
    except (TypeError, ValueError):
        return default


SCREENSHOT_KEEP = _env_int("AUTOMATION_SCREENSHOT_KEEP", 30, 1, 1000)


# Bounded so an approved-but-hung command can never block the assistant indefinitely.
SHELL_TIMEOUT_SECONDS = _env_int("AUTOMATION_SHELL_TIMEOUT_SECONDS", 20, 1, 600)


def prune_screenshots(folder=None, keep=SCREENSHOT_KEEP):
    """
    Keeps the newest `keep` Kayra screenshots and deletes the rest.

    Retention policy, as required of every collection Kayra writes: a screenshot key pressed
    a few times a day is an unbounded pile of PNGs on the user's Desktop within a year. Only
    files matching Kayra's own naming prefix are ever touched.
    """
    try:
        if not folder:
            return 0
        entries = [os.path.join(folder, f) for f in os.listdir(folder)
                   if f.startswith("KAYRA_Screenshot_") and f.endswith(".png")]
        if len(entries) <= keep:
            return 0
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        removed = 0
        for path in entries[keep:]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0


# Exact-match tokens -> (domain, action). Checked BEFORE any prefix rule, which is what keeps
# "close window" and "close tab" from being swallowed by the generic "close " prefix. The old
# router relied on if/elif ordering for this; a dict cannot be reordered by accident.
_EXACT_TOKENS = {
    # window
    "close window": ("window", "close"), "minimize": ("window", "minimize"),
    # The BROAD close, and the only one. It exists as its own token precisely so that the
    # ordinary "close X" can never widen into it by accident: nothing infers this, the user
    # has to say it, and the policy layer confirms it.
    "close everything": ("window", "close_all"),
    "close all windows": ("window", "close_all"),
    "close all": ("window", "close_all"),
    "minimize all": ("window", "minimize_all"), "show desktop": ("window", "minimize_all"),
    "maximize": ("window", "maximize"), "restore": ("window", "restore"),
    "snap left": ("window", "snap_left"), "snap right": ("window", "snap_right"),
    "switch window": ("window", "alt_tab"), "alt tab": ("window", "alt_tab"),
    "task view": ("window", "task_view"), "action center": ("window", "action_center"),
    "notification": ("window", "action_center"), "emoji": ("window", "emoji"),
    # browser
    "close tab": ("browser", "close_tab"), "new tab": ("browser", "new_tab"),
    "next tab": ("browser", "next_tab"), "previous tab": ("browser", "previous_tab"),
    "reopen tab": ("browser", "reopen_tab"), "duplicate tab": ("browser", "duplicate_tab"),
    "go back": ("browser", "back"), "go forward": ("browser", "forward"),
    "refresh": ("browser", "refresh"), "reload": ("browser", "refresh"),
    # media
    "pause": ("media", "pause"), "resume": ("media", "resume"),
    "next track": ("media", "next"), "previous track": ("media", "previous"),
    "stop media": ("media", "stop"), "play pause": ("media", "pause"),
    # clipboard
    "copy": ("clipboard", "copy"), "copy that": ("clipboard", "copy"),
    "paste": ("clipboard", "paste"), "paste that": ("clipboard", "paste"),
    "cut": ("clipboard", "cut"), "clear clipboard": ("clipboard", "clear"),
    "read clipboard": ("clipboard", "read"),
    # information
    "battery": ("info", "battery"), "cpu": ("info", "cpu"), "ram": ("info", "ram"),
    "memory": ("info", "ram"), "disk": ("info", "disk"), "storage": ("info", "disk"),
    "uptime": ("info", "uptime"), "ip address": ("info", "network"),
    # screen
    "screenshot": ("screen", "screenshot"), "take screenshot": ("screen", "screenshot"),
    # keyboard shortcuts
    "undo": ("keyboard", "hotkey"), "redo": ("keyboard", "hotkey"),
    "select all": ("keyboard", "hotkey"), "save": ("keyboard", "hotkey"),
    "save file": ("keyboard", "hotkey"), "find": ("keyboard", "hotkey"),
    "search": ("keyboard", "hotkey"), "print": ("keyboard", "hotkey"),
    "fullscreen": ("keyboard", "hotkey"), "zoom in": ("keyboard", "hotkey"),
    "zoom out": ("keyboard", "hotkey"), "reset zoom": ("keyboard", "hotkey"),
    "task manager": ("keyboard", "hotkey"), "run dialog": ("keyboard", "hotkey"),
    "enter": ("keyboard", "hotkey"), "escape": ("keyboard", "hotkey"),
    # mouse
    "click": ("mouse", "click"), "mouse click": ("mouse", "click"),
    "double click": ("mouse", "double_click"), "right click": ("mouse", "right_click"),
    "scroll up": ("mouse", "scroll_up"), "scroll down": ("mouse", "scroll_down"),
    # timers
    "cancel timer": ("timer", "cancel"), "list timers": ("timer", "list"),
    # wifi
    "wifi on": ("system", "wifi_on"), "wifi off": ("system", "wifi_off"),
}


# Prefix tokens -> (domain, action). Ordered longest-first at build time so a longer literal
# can never be shadowed by a shorter one that happens to be its prefix.
_PREFIX_TOKENS = [
    ("open folder ", ("file", "open_folder")),
    ("open file ", ("file", "open_file")),
    ("create folder ", ("file", "create_folder")),
    ("create file ", ("file", "create_file")),
    ("delete file ", ("file", "delete")),
    ("delete folder ", ("file", "delete")),
    ("rename ", ("file", "rename")),
    ("find file ", ("file", "search")),
    ("search files ", ("file", "search")),
    ("restart app ", ("app", "restart")),
    ("focus ", ("window", "focus")),
    ("switch to ", ("window", "focus")),
    ("google search ", ("browser", "search_web")),
    ("web search ", ("browser", "search_web")),
    ("youtube search ", ("browser", "search_youtube")),
    ("copy text ", ("clipboard", "set")),
    ("terminal ", ("shell", "run")),
    ("run command ", ("shell", "run")),
    ("set timer ", ("timer", "add")),
    ("timer ", ("timer", "add")),
    ("reminder ", ("timer", "reminder")),
    ("remind ", ("timer", "reminder")),
    ("content ", ("app", "content")),
    ("system ", ("system", "raw")),
    ("write ", ("keyboard", "type")),
    ("type ", ("keyboard", "type")),
    ("close all ", ("app", "close_all")),
    ("close ", ("app", "close")),
    ("open ", ("app", "open")),
    ("play ", ("media", "play")),
    # On-screen clicking by visible text or coordinates. "click" alone stays the exact token.
    ("click on ", ("mouse", "click_text")),
    ("click the ", ("mouse", "click_text")),
    ("click at ", ("mouse", "click_at")),
    ("double click on ", ("mouse", "double_click_text")),
    ("right click on ", ("mouse", "right_click_text")),
    ("move mouse to ", ("mouse", "move_to")),
    ("move the mouse to ", ("mouse", "move_to")),
    # Round 3 (2026-11-10). Each is a DMM token of its own; the payload is parsed by
    # `_web_action` / `_extended_action` below into a structured action the policy can judge.
    ("browser ", ("web", "browser")),
    ("meet ", ("web", "meet")),
    ("email ", ("web", "email")),
    ("calendar ", ("web", "calendar")),
    ("message ", ("web", "message")),
    ("diagnose ", ("diagnose", "run")),
    ("screen ", ("screen", "raw")),
    ("scene ", ("scene", "raw")),
    ("plan ", ("plan", "run")),
    ("trigger ", ("trigger", "raw")),
    ("files ", ("files", "raw")),
    ("git ", ("dev", "git")),
]


_PREFIX_TOKENS.sort(key=lambda item: len(item[0]), reverse=True)


# Hotkey names -> key combination. Single source of truth, shared with HotkeyShortcut.
HOTKEY_MAP = {
    "undo": "ctrl+z", "redo": "ctrl+y", "select all": "ctrl+a", "save": "ctrl+s",
    "save file": "ctrl+s", "find": "ctrl+f", "search": "ctrl+f", "cut": "ctrl+x",
    "copy": "ctrl+c", "paste": "ctrl+v", "new tab": "ctrl+t", "close tab": "ctrl+w",
    "refresh": "ctrl+r", "reload": "ctrl+r", "fullscreen": "f11", "print": "ctrl+p",
    "zoom in": "ctrl+plus", "zoom out": "ctrl+minus", "reset zoom": "ctrl+0",
    "task manager": "ctrl+shift+escape", "run dialog": "win+r",
    "enter": "enter", "escape": "esc", "tab": "tab",
}


# "system X" sub-verbs. Shutdown/restart/sign-out become their own actions so the policy layer
# can see them; previously they were a substring test inside ExecuteCommand and executed
# instantly with no confirmation whatsoever.
def _power_refusal_sentence(action):
    """
    What the user hears when a power request could not be targeted. "" for anything else.

    Two situations, two different sentences, because they are different mistakes:

      AMBIGUOUS — nothing in the sentence named a target. Ask which, naming both, so the
                  answer is a choice rather than a yes/no about the wrong thing.
      KAYRA     — the sentence named Kayra and reached the automation layer anyway, which
                  means the transcript missed the local control vocabulary. Say what was
                  understood and how to phrase it, rather than acting on a near-miss.
    """
    if action.domain != "system":
        return ""
    verb = action.parameters.get("verb") or action.action
    word = {"shutdown": "shut down", "restart": "restart",
            "sign_out": "sign out of", "sleep": "put to sleep"}.get(verb, "power off")

    if action.action == "power_ambiguous":
        return (f"Do you mean {word} {ASSISTANT_LABEL}, or {word} the computer? "
                "They are different things, so please say which.")
    if action.action == "power_kayra":
        return (f"That sounded like you meant {ASSISTANT_LABEL} rather than the computer, "
                f"and I did not hear it clearly enough to be sure. "
                f"Say \"{word} {ASSISTANT_LABEL}\" and I will confirm it with you.")
    return ""


def _assistant_alias():
    """
    The name the user calls the assistant, lower-cased. "kayra" when it cannot be read.

    Read lazily and defensively: this module must import without `.env`, and a configuration
    failure must never widen the power-target gate.
    """
    try:
        from kayra.core.config import assistant_name
        return (assistant_name() or "kayra").strip().lower()
    except Exception:
        return "kayra"


ASSISTANT_LABEL = "Kayra"


# ORDER MATTERS: the first needle found in the payload wins, so the most specific phrasings
# come first. "turn off screen" must beat "turn off the computer" for a payload containing
# both, and neither may be beaten by a bare verb.
#
# These find the VERB. They do NOT decide the TARGET — `resolve_power_target` does, and for
# everything in `policy.POWER_VERBS` the normalizer refuses to build an action without an
# explicit one. See the power-target gate in `normalize_command`.
_SYSTEM_VERBS = (
    # Explicit computer-power phrasings, first so a bare verb cannot shadow them.
    # TURNING THE DISPLAY OFF IS NOT SUSPENDING THE MACHINE, and mapping it to "sleep" was a
    # pre-existing mislabel: the sleep branch calls `SetSuspendState`, so "turn off screen"
    # suspended the whole session. It is its own verb now, it blanks the monitor, and it is
    # therefore NOT a power action and needs no target — nothing is lost by getting it wrong.
    ("turn off screen", "screen_off"), ("turn off the screen", "screen_off"),
    ("turn off the display", "screen_off"), ("blank the screen", "screen_off"),
    # Desktop-session controls added with the Linux port. None is a power verb, and each is
    # matched BEFORE the power needles so "turn off bluetooth" can never reach the power gate.
    # The handlers read the payload again for on/off/number, so the wording is kept intact.
    # Undo of Kayra's OWN last automation (volume, workspace, toggles...). A bare "undo"
    # token is still Ctrl+Z in the focused app; only "system undo ..." reaches this.
    ("undo last action", "undo"), ("undo the last", "undo"), ("undo what you", "undo"),
    ("undo that", "undo"), ("revert that", "undo"), ("revert the last", "undo"),
    ("put it back", "undo"), ("undo", "undo"),
    # Device questions phrased as "system ...": answered by the info domain (read-only).
    # Round 3: every phrase here is matched BEFORE the power needles below, so none of
    # them can ever reach the power-target gate ("sleep timer", "restart the docker
    # container", "update my system" are not power requests).
    ("sleep timer", "music_sleep"), ("stop the music in", "music_sleep"),
    ("what did you just do", "recall"), ("what did you do", "recall"), ("why did you", "recall"),
    ("what have you done", "recall"),
    ("morning briefing", "briefing"), ("daily briefing", "briefing"), ("brief me", "briefing"),
    ("briefing", "briefing"),
    ("read my notifications", "notifications"), ("read notifications", "notifications"),
    ("what did i miss", "notifications"), ("any notifications", "notifications"),
    ("clear notifications", "notifications"), ("clear my notifications", "notifications"),
    ("am i in a call", "in_call"),
    ("remember my setup", "session_save"), ("save my setup", "session_save"),
    ("save my layout", "session_save"), ("restore my setup", "session_restore"),
    ("restore my last session", "session_restore"), ("restore yesterday", "session_restore"),
    ("restore my layout", "session_restore"), ("bring back my setup", "session_restore"),
    ("on the left", "layout"), ("side by side", "layout"),
    ("what's open", "list_windows"), ("what is open", "list_windows"),
    ("what windows", "list_windows"), ("which apps are open", "list_windows"),
    ("which workspace is", "where"), ("where is my", "where"), ("where's my", "where"),
    ("where is ", "where"), ("where's ", "where"),
    ("close everything except", "close_except"), ("close all except", "close_except"),
    ("close all windows except", "close_except"),
    ("move all", "move_all"), ("group my windows", "tidy"), ("tidy up my windows", "tidy"),
    ("organize my windows", "tidy"), ("other screen", "other_monitor"),
    ("other monitor", "other_monitor"), ("make it bigger", "resize"), ("make it smaller", "resize"),
    ("window bigger", "resize"), ("window smaller", "resize"),
    ("switch audio", "audio_output"), ("audio output", "audio_output"),
    ("sound output", "audio_output"), ("switch to my headphones", "audio_output"),
    ("switch to headphones", "audio_output"), ("switch to speakers", "audio_output"),
    ("switch to the speakers", "audio_output"), ("use the speakers", "audio_output"),
    ("use my headphones", "audio_output"), ("play through", "audio_output"),
    ("audio devices", "audio_list"), ("sound devices", "audio_list"),
    ("microphone", "mic"), ("my mic", "mic"), ("the mic", "mic"), ("mic volume", "mic"),
    ("mute mic", "mic"), ("unmute mic", "mic"),
    ("connect to wifi", "wifi_connect"), ("connect to the wifi", "wifi_connect"),
    ("connect to network", "wifi_connect"), ("join wifi", "wifi_connect"),
    ("forget wifi", "wifi_forget"), ("forget the wifi", "wifi_forget"),
    ("forget network", "wifi_forget"), ("forget the network", "wifi_forget"),
    ("share wifi", "wifi_share"), ("share the wifi", "wifi_share"),
    ("wifi password", "wifi_share"), ("wifi qr", "wifi_share"),
    ("earbuds", "bt_device"), ("headphones battery", "bt_device"), ("connect my", "bt_device"),
    ("disconnect my", "bt_device"), ("connect the", "bt_device"), ("disconnect the", "bt_device"),
    ("check for updates", "updates"), ("any updates", "updates"),
    ("updates available", "updates"), ("update my system", "update_system"),
    ("update the system", "update_system"), ("update everything", "update_system"),
    ("update my computer", "update_system"),
    ("uninstall", "uninstall"), ("remove the app", "uninstall"), ("install", "install"),
    ("set refresh rate", "display_set"), ("change refresh rate", "display_set"),
    ("refresh rate to", "display_set"), ("scale the screen", "display_set"),
    ("display scale", "display_set"), ("screen scale", "display_set"),
    ("rotate the screen", "display_set"), ("rotate screen", "display_set"),
    ("touchpad", "touchpad"), ("trackpad", "touchpad"),
    ("keyboard layout", "kb_layout"), ("switch language", "kb_layout"),
    ("keyboard backlight", "kb_backlight"), ("keyboard light", "kb_backlight"),
    ("wallpaper", "wallpaper"), ("dark mode", "theme"), ("light mode", "theme"),
    ("theme", "theme"),
    ("close the app using", "close_hog"), ("close whatever is using", "close_hog"),
    ("what's using", "top_processes"), ("what is using", "top_processes"),
    ("top processes", "top_processes"), ("using the most", "top_processes"),
    ("eating my", "top_processes"), ("hogging", "top_processes"),
    ("docker", "docker"), ("container", "docker"),
    ("run my tests", "run_tests"), ("run the tests", "run_tests"), ("run tests", "run_tests"),
    ("start the dev server", "dev_server"), ("start dev server", "dev_server"),
    ("power saver", "power_profile"), ("battery saver", "power_profile"),
    ("power profile", "power_profile"), ("power mode", "power_profile"),
    ("performance mode", "power_profile"), ("balanced mode", "power_profile"),
    ("battery", "battery"), ("charging", "battery"),
    ("temperature", "temperature"), ("how hot", "temperature"), ("overheating", "temperature"),
    ("wifi network", "wifi_info"), ("which wifi", "wifi_info"), ("wifi name", "wifi_info"),
    ("network name", "wifi_info"), ("signal strength", "wifi_info"),
    ("bluetooth devices", "bt_devices"), ("connected devices", "bt_devices"),
    ("screen resolution", "displays"), ("display info", "displays"),
    ("refresh rate", "displays"), ("monitors", "displays"),
    ("device info", "summary"), ("system info", "summary"), ("system summary", "summary"),
    ("specs", "summary"), ("about this computer", "summary"), ("about my computer", "summary"),
    ("cpu", "cpu"), ("processor usage", "cpu"), ("memory usage", "ram"), ("ram", "ram"),
    ("disk space", "disk"), ("storage", "disk"), ("uptime", "uptime"),
    ("ip address", "network"),
    ("move window to workspace", "workspace_move"), ("move this to workspace", "workspace_move"),
    ("move it to workspace", "workspace_move"), ("send window to workspace", "workspace_move"),
    ("move to workspace", "workspace_move"), ("workspace", "workspace"),
    ("bluetooth", "bluetooth"),
    ("do not disturb", "dnd"), ("dont disturb", "dnd"), ("don't disturb", "dnd"),
    ("notifications", "dnd"),
    ("night light", "night_light"), ("nightlight", "night_light"), ("blue light", "night_light"),
    ("now playing", "now_playing"), ("what's playing", "now_playing"),
    ("what is playing", "now_playing"), ("current song", "now_playing"),
    ("toggle floating", "float"), ("float window", "float"), ("floating", "float"),
    ("pin window", "pin"), ("unpin window", "pin"),
    ("center window", "center"), ("centre window", "center"),
    ("turn off the computer", "shutdown"), ("turn off my computer", "shutdown"),
    ("turn off the pc", "shutdown"), ("turn off my pc", "shutdown"),
    ("turn off the laptop", "shutdown"), ("turn off my laptop", "shutdown"),
    ("power down the computer", "shutdown"), ("power down my computer", "shutdown"),
    ("sign out", "sign_out"), ("log out", "sign_out"), ("logout", "sign_out"),
    ("shutdown", "shutdown"), ("shut down", "shutdown"), ("power off", "shutdown"),
    ("restart", "restart"), ("reboot", "restart"),
    ("lock", "lock"),
    ("sleep", "sleep"),
    ("mute", "volume"), ("volume", "volume"), ("brightness", "brightness"),
)


_INFO_VERBS = frozenset({"battery", "temperature", "wifi_info", "bt_devices", "displays",
                         "summary", "cpu", "ram", "disk", "uptime", "network"})


def normalize_command(command, context=None):
    """
    DMM token -> structured `Action`.

    Contextual references ("it", "this", "that") are resolved HERE, against the bounded
    automation context, and only when the context actually holds a fresh referent. When it
    does not, the action keeps the pronoun and carries a low confidence, which the policy
    layer turns into a confirmation rather than a guess.
    """
    raw = (command or "").strip()
    lowered = raw.lower()
    if not lowered:
        return None

    # 1. Exact tokens first — this is what prevents prefix shadowing structurally.
    if lowered in _EXACT_TOKENS:
        domain, action = _EXACT_TOKENS[lowered]
        parameters = {}
        if domain == "keyboard" and action == "hotkey":
            parameters["keys"] = HOTKEY_MAP.get(lowered, lowered)
            parameters["name"] = lowered
        return Action(domain, action, target="current", parameters=parameters, raw=raw)

    # 1a. "open the third result" with no `browser` prefix is the page's result list (C3),
    # never an app called "the third result".
    hit = result_request(re.sub(r"^browser\s+", "", lowered))
    if hit:
        return Action("browser", "page_result", target=str(hit[0]),
                      parameters={"nth": hit[0], "new_tab": hit[1]}, raw=raw)

    # 1b. A request to share the screen, however it arrived ("share screen", "click on share
    # screen", "start screen sharing"): ONE meet.share, never a click or a new meeting (M0.1).
    if _BARE_SHARE.match(lowered):
        request = share_request(lowered)
        if request is not None:
            return _share_action(request, raw)

    # 2. Prefix tokens, longest first.
    for prefix, (domain, action) in _PREFIX_TOKENS:
        if lowered.startswith(prefix):
            payload = raw[len(prefix):].strip()
            return _build_action(domain, action, payload, raw, context)

    # 3. Keyword fallbacks for phrasings the DMM emits without a canonical prefix.
    for keyword, (domain, action) in (
            ("screenshot", ("screen", "screenshot")),
            ("screen capture", ("screen", "screenshot")),
            ("wifi", ("system", "wifi_toggle")),
            ("wi-fi", ("system", "wifi_toggle"))):
        if keyword in lowered:
            return Action(domain, action, target=lowered, raw=raw)

    if lowered in HOTKEY_MAP:
        return Action("keyboard", "hotkey", target="current",
                      parameters={"keys": HOTKEY_MAP[lowered], "name": lowered}, raw=raw)

    return None


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1,
             "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}


def _ordinal(text):
    """('second save' -> (2, 'save')). nth is 1 when no ordinal is spoken."""
    words = str(text or "").strip().split()
    if words and words[0].lower() in ("the",):
        words = words[1:]
    if words and words[0].lower() in _ORDINALS:
        return _ORDINALS[words[0].lower()], " ".join(words[1:])
    return 1, " ".join(words)


def _strip(text, *words):
    out = str(text or "").strip()
    for word in words:
        out = re.sub(rf"^\s*{word}\b\s*", "", out, flags=re.I)
    return out.strip(" .?!")


# Where a page should open (round 5, A3). Matched at the END of the target only, so a site
# whose name contains one of these words is never cut. Order matters: longest first.
_WHERE_PHRASES = (
    (r"(?:in|on|into) (?:this|that|the|my) (?:same |current |existing |open )?tab", "current"),
    (r"(?:in|on) (?:the )?(?:same|current) (?:tab|page)", "current"),
    (r"(?:right )?here|in place|instead", "current"),
    (r"(?:in|on) (?:a |another |one more )?(?:new|separate|different|fresh) tab", "tab"),
    (r"(?:in|on) (?:a |another )?(?:new|separate|different|fresh) (?:browser )?window", "window"),
    (r"(?:in|on) (?:an? )?(?:incognito|private|secret)(?: mode| window| tab)?|incognito|privately",
     "incognito"),
    (r"(?:next to|beside|after) (?:this|the current|my current) tab", "next"),
    (r"(?:in|on|using|with) (?:the |my )?(?:browser|chrome|google chrome|web)", "browser"),
)


def _split_where(target):
    """'google sheets in that tab' -> ('google sheets', 'current'). ('x', None) when unsaid."""
    text = str(target or "").strip()
    for pattern, where in _WHERE_PHRASES:
        m = re.search(rf"\s*,?\s*\b(?:{pattern})\s*[.?!]*$", text, re.I)
        if m and m.start() > 0:
            return text[:m.start()].strip(" ,"), where
    return text, None


def merge_tab_steps(actions):
    """
    "open new tab and open Google Sheets in that tab" arrives as TWO tokens, `new tab` and
    `open google sheets in that tab`. Run literally they race: Ctrl+T, then a navigation that
    lands in whichever tab won. One utterance, one flow (A4): the `new tab` is dropped and the
    open that follows it is told to use a new tab. An `app.open` after `new tab` is a web open
    — the user said tab.
    """
    out = []
    for action in actions:
        previous = out[-1] if out else None
        if previous is not None and previous.domain == "browser" and previous.action == "new_tab" \
                and ((action.domain == "browser" and action.action in
                      ("open_url", "search_web", "search_youtube"))
                     or (action.domain == "app" and action.action == "open")):
            if action.parameters.get("where") not in ("window", "incognito"):
                action.parameters["where"] = "tab"
            if action.domain == "app":
                action.parameters["web"] = True
            out[-1] = action
            continue
        out.append(action)
    return out


def merge_share_steps(actions):
    """
    "share the LM Studio window" sometimes arrives as `focus lm studio` + `meet share window`
    (the audit log of 2026-11-10). Run literally, the focus takes Meet out of the front and
    the share then picks the wrong thing. The name belongs to the share (M0.3): the focus is
    dropped and the share is told which window.
    """
    out = []
    for action in actions:
        previous = out[-1] if out else None
        if previous is not None and action.domain == "meet" and action.action == "share" \
                and previous.domain in ("window", "app") and previous.action in ("focus", "switch") \
                and not action.parameters.get("name") and action.target in ("monitor", "window"):
            action.target = "window"
            action.parameters["name"] = previous.target
            out[-1] = action
            continue
        if action.domain == "meet" and action.action == "share":
            # One utterance shares ONE thing: a second share in the same batch (the DMM said
            # it twice in different words) keeps whichever named something specific.
            earlier = next((a for a in out if a.domain == "meet" and a.action == "share"), None)
            if earlier is not None:
                if action.parameters.get("name") and not earlier.parameters.get("name"):
                    out[out.index(earlier)] = action
                continue
        out.append(action)
    return out


# ── Screen sharing (BROWSER_CONTROL_PLAN §12, M0.1 / M1) ──
# Every way of asking to share, read ONCE into {surface, name, audio}. `surface` is what
# getDisplayMedia calls it: monitor | window | browser. `name` is the window, tab or monitor
# the user named ("lm studio", "youtube", "#2"), or "".
_SHARE_WORD = re.compile(r"\b(shar(?:e|ing)|screen ?share|screensharing|present(?:ing| now)?)\b")
_SHARE_STOP = re.compile(r"\b(stop|end|quit|cancel|finish)\b.*\b(shar|present)|"
                         r"\b(shar\w*|present\w*)\b.*\b(stop|off)\b$")
_SHARE_STATUS = re.compile(r"^(?:am i|are we|is (?:my|the) screen|is anything|what am i)\b.*"
                           r"\b(shar|present)|^(?:sharing|share) status$")
_SCREEN_WORDS = r"(?:entire |whole |full |my |the |this )*(?:screen|desktop|display|monitor)"
_MONITOR_ORDINAL = re.compile(
    r"\b(?:my |the )?(first|second|third|other|external|left|right|main|primary|laptop|built ?in)"
    r" (?:screen|monitor|display)\b|\b(?:screen|monitor|display) (\d)\b")


def share_request(text):
    """
    'start screen sharing' -> {'surface': 'monitor', 'name': ''}
    'share the lm studio window' -> {'surface': 'window', 'name': 'lm studio'}
    'present this tab' / 'share the youtube tab' -> {'surface': 'browser', 'name': '' | 'youtube'}
    'share my second screen' -> {'surface': 'monitor', 'name': '#2'}
    None when the words are not a request to share (or are a request to STOP sharing).
    """
    low = " ".join(re.sub(r"[^\w\s#-]", " ", str(text or "").lower()).split())
    if not low or not _SHARE_WORD.search(low) or _SHARE_STOP.search(low) or _SHARE_STATUS.search(low):
        return None
    audio = bool(re.search(r"\bwith (?:the )?(?:system )?(?:audio|sound)\b", low))
    low = re.sub(r"\s*\bwith (?:the )?(?:system )?(?:audio|sound)\b", "", low)
    instead = bool(re.search(r"\binstead\b", low))
    low = re.sub(r"\s*\binstead\b", "", low).strip()
    out = {"surface": "monitor", "name": "", "audio": audio, "instead": instead}
    m = _MONITOR_ORDINAL.search(low)
    if m:
        word = m.group(1) or m.group(2)
        out["name"] = {"first": "#1", "main": "#1", "primary": "#1", "laptop": "#1",
                       "built in": "#1", "builtin": "#1", "second": "#2", "other": "#2",
                       "external": "#2", "third": "#3", "left": "<", "right": ">"}.get(word, f"#{word}")
        return out
    # The thing being shared: whatever follows the share verb, minus the filler words.
    m = re.search(r"\b(?:shar(?:e|ing)|present(?:ing)?|screen ?share)\b\s*(.*)$", low)
    rest = m.group(1) if m else ""
    rest = re.sub(r"^(?:screen\s+)?(?:of|on|in)\s+(?:this|the|my)\s+(?:meet|meeting|call)\s*", "", rest)
    rest = re.sub(r"^(?:screen|now)\b\s*", "", rest)
    rest = re.sub(r"\s*\b(?:in|on|to|with|into) (?:the |this |my )?(?:meet|meeting|call|google meet)\b.*$", "", rest)
    rest = re.sub(r"^(?:and |then )?(?:share |present )?", "", rest).strip()
    if re.search(r"\btab\b", rest):
        name = re.sub(r"\btab\b.*$", "", rest).strip()
        name = re.sub(r"^(?:the|my|a|this|that|current|chrome|browser)\b\s*", "", name).strip()
        name = re.sub(r"^(?:the|my|a|this|that|current|chrome|browser)\b\s*", "", name).strip()
        out.update(surface="browser", name=name)
        return out
    if re.search(r"\b(?:window|app|application)\b", rest):
        name = re.sub(r"\s*\b(?:window|app|application)\b.*$", "", rest).strip()
        name = re.sub(r"^(?:the|my|a|this|that|one|an)\b\s*", "", name).strip()
        out.update(surface="window", name="" if name in ("this", "that", "a", "") else name)
        return out
    if not rest or re.fullmatch(_SCREEN_WORDS + r"?(?: now)?", rest) or rest in ("everything", "it"):
        return out
    if re.match(r"(?:with|to|for)\b", rest):
        return out
    # "share LM Studio" — a name with no surface word is a window.
    out.update(surface="window", name=re.sub(r"^(?:the|my)\b\s*", "", rest).strip())
    return out


def _share_action(request, raw):
    surface = request["surface"]
    params = {"name": request["name"]}
    if request.get("audio"):
        params["audio"] = True
    if request.get("instead"):
        params["instead"] = True
    return Action("meet", "share", target=surface, parameters=params, raw=raw)


# A DMM token that is a share request but arrived without the `meet` prefix — "share screen",
# "click on share screen", "start screen sharing", "present now" (M0.1). Only phrasings that
# name a SCREEN surface or presenting are taken; "click on share" on a video page stays a click.
_BARE_SHARE = re.compile(
    r"^(?:(?:browser )?(?:click|press|tap|hit)(?: on)?(?: the)? |start |begin |please )?"
    r"(?:(?:start|begin) )?(?:screen ?shar(?:e|ing)|screensharing|present(?:ing)?(?: now)?$|"
    r"present(?:ing)? (?:my|the|this|a) (?:\w+ )*(?:screen|window|tab)\b|"
    r"shar(?:e|ing) (?:(?:my|the|this|a|entire|whole|full|second|other)\s+)*"
    r"(?:screen|desktop|display|monitor|window|tab)\b|shar(?:e|ing) \S.* (?:window|tab)\b)")


_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}


def tab_position(text):
    """
    "the third tab" -> "#3", "the last tab" -> "#-1", "the second last tab" -> "#-2",
    "tab 4" -> "#4", "the tab on the right" -> "+1", "the tab on the left" -> "-1".
    None when the words name a site instead of a position.
    """
    low = " ".join(str(text or "").lower().replace("-", " ").split())
    low = re.sub(r"^(?:the|my)\s+", "", low)
    m = re.fullmatch(r"tab (?:on|to) the (right|left)|(right|left) tab", low)
    if m:
        return "+1" if (m.group(1) or m.group(2)) == "right" else "-1"
    m = re.fullmatch(r"(?:(second|third) )?last(?: tab)?|(?:(second|third) to last)(?: tab)?", low)
    if m:
        back = m.group(1) or m.group(2)
        return f"#-{_ORDINALS.get(back, 1)}"
    word = re.sub(r"\s+tab$", "", low)
    if word in _ORDINALS:
        return f"#{_ORDINALS[word]}"
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th) tab|(\d{1,2})(?:st|nd|rd|th)|tab (?:number )?(\d{1,2})", low)
    if m:
        number = int(next(g for g in m.groups() if g))
        if number > 0:
            return f"#{number}"
    return None


def _web_action(kind, payload, raw):
    """'browser …', 'meet …', 'email …', 'calendar …', 'message …' -> a structured action."""
    text = (payload or "").strip()
    low = text.lower().strip(" .?!")
    if kind == "browser":
        m = re.match(r"(?:list|show|what are)?\s*(?:my\s+)?(?:open\s+)?tabs$|what tabs", low)
        if m:
            return Action("browser", "tabs_list", target="all", raw=raw)
        m = re.match(r"close (?:all )?(?:the )?other tabs|close all tabs except (?:this|the current)(?: one| tab)?", low)
        if m:
            return Action("browser", "tabs_close_others", target="others", raw=raw)
        # ── Part 2b ──
        m = re.fullmatch(r"(un)?mute (?:all |every )?(?:the )?(?:other tabs|tabs except (?:this|the current)(?: one| tab)?|everything (?:else|except (?:this|the current)(?: one| tab)?))", low)
        if m:
            return Action("browser", "tabs_mute_others", target="others",
                          parameters={"on": not m.group(1)}, raw=raw)
        if re.fullmatch(r"what (?:tabs? )?did i (?:just )?close|(?:show |list )?(?:my )?recently closed(?: tabs)?|what did i close", low):
            return Action("browser", "tabs_recently_closed", target="closed", raw=raw)
        m = re.fullmatch(r"(?:reopen|restore|bring back) (?:the )?(.+?) tab(?: (?:that )?i (?:just )?closed)?", low)
        if m and m.group(1) not in ("last", "closed", "last closed"):
            return Action("browser", "tab_reopen", target=m.group(1), raw=raw)
        if re.fullmatch(r"(?:free up|save|reduce) (?:some )?memory(?: in chrome| in the browser)?|(?:put|send) (?:the |my )?(?:background |other )?tabs to sleep|(?:discard|sleep) (?:the |my )?(?:background |other )?tabs", low):
            return Action("browser", "tabs_discard", target="background", raw=raw)
        m = re.fullmatch(r"(?:find|which is|where is|show me) (?:the |my )?tab (?:where i was |that )?(?:reading |looking )?(?:about|on|with|mentioning|has) (.+)", low)
        if m:
            return Action("browser", "tab_find", target=m.group(1), raw=raw)
        if re.fullmatch(r"hard (?:reload|refresh)(?: (?:this|the) page)?|(?:reload|refresh) (?:this page |the page )?without (?:the )?cache", low):
            return Action("browser", "page_reload", target="current", parameters={"hard": True}, raw=raw)
        m = re.fullmatch(r"(?:set )?(?:the )?(?:page )?zoom (?:the page )?(?:to |at )?(\d{2,3})(?: ?%| percent)?", low)
        if m:
            return Action("browser", "page_zoom", target=m.group(1), raw=raw)
        m = re.fullmatch(r"copy (?:the |this )?(?:page'?s? |tab'?s? )?(link|url|address|title)(?: of (?:this|the) (?:page|tab))?", low)
        if m:
            return Action("browser", "page_copy", target="title" if m.group(1) == "title" else "link", raw=raw)
        # ── Part 2c ──
        found = _part_2c_action(low, raw)
        if found is not None:
            return found
        if re.fullmatch(r"close (?:all )?(?:the |my )?duplicate(?: tabs)?s?|close (?:the )?duplicates?(?: tabs)?|remove duplicate tabs", low):
            return Action("browser", "tabs_close_duplicates", target="duplicates", raw=raw)
        m = re.fullmatch(r"close (?:all )?(?:the |my )?(?:other )?tabs (?:to|on) the (right|left)", low)
        if m:
            return Action("browser", "tabs_close_side", target=m.group(1), raw=raw)
        m = re.fullmatch(r"close (.+)", low)
        position = tab_position(m.group(1)) if m else None
        if position:
            return Action("browser", "tab_close", target=position, parameters={"all": False},
                          raw=raw)
        m = re.match(r"close (all )?(?:the |my )?(.+?) tabs?$", low)
        if m:
            return Action("browser", "tab_close", target=m.group(2),
                          parameters={"all": bool(m.group(1))}, raw=raw)
        m = re.fullmatch(r"(?:switch|go|jump|move|take me) (?:to|over to) (.+)", low)
        position = tab_position(m.group(1)) if m else None
        if position:
            return Action("browser", "tab_switch", target=position, raw=raw)
        m = re.match(r"(?:switch|go|jump|take me) to (?:the |my )?(.+?)(?: tab)?$", low)
        if m and m.group(1) not in ("top", "bottom"):
            return Action("browser", "tab_switch", target=m.group(1), raw=raw)
        m = re.match(r"(un)?mute (?:this|the|my)?\s*(.*?)\s*tab$", low)
        if m:
            return Action("browser", "tab_mute", target=m.group(2) or "current",
                          parameters={"on": not m.group(1)}, raw=raw)
        if re.match(r"(un)?pin (?:this )?tab", low):
            return Action("browser", "tab_pin", target="current", raw=raw)
        m = re.match(r"(?:double )?click (?:on )?(.+)$", low)
        if m:
            nth, label = _ordinal(_strip(m.group(1), "the"))
            label = re.sub(r"\s+(button|link|tab|icon|option)$", "", label)
            return Action("browser", "page_click", target=label, parameters={"nth": nth}, raw=raw)
        m = re.match(r"(?:fill|set) (?:in )?(?:the )?(.+?) (?:field |box )?(?:with|to) (.+)$", text, re.I)
        if m:
            return Action("browser", "page_type", target=m.group(1),
                          parameters={"text": m.group(2), "submit": False}, raw=raw)
        m = re.match(r"(?:type|write|enter) (.+?) (?:in|into) (?:the )?(.+?)(?: field| box| bar)?$", text, re.I)
        if m:
            return Action("browser", "page_type", target=m.group(2),
                          parameters={"text": m.group(1), "submit": False}, raw=raw)
        m = re.match(r"search (?:this page |the page |the site )?for (.+)$", text, re.I)
        if m:
            return Action("browser", "page_type", target="search",
                          parameters={"text": m.group(1), "submit": True}, raw=raw)
        m = re.match(r"scroll (?:down |up )?(?:to|until(?: you see)?) (.+)$", low)
        if m and m.group(1) not in ("top", "the top", "bottom", "the bottom"):
            return Action("browser", "page_scroll", target=m.group(1),
                          parameters={"direction": "down", "until": m.group(1)}, raw=raw)
        m = re.match(r"scroll (?:to )?(?:the )?(up|down|top|bottom)", low)
        if m:
            return Action("browser", "page_scroll", target=m.group(1),
                          parameters={"direction": m.group(1)}, raw=raw)
        if re.match(r"(summari[sz]e|tldr|give me the gist of)", low):
            return Action("browser", "page_summarize", target="current", raw=raw)
        if re.match(r"read (?:out )?(?:this |the )?(?:page|article|it)", low):
            return Action("browser", "page_read", target="current", raw=raw)
        m = re.match(r"find (.+?) (?:on|in) (?:this |the )?page$", low)
        if m:
            return Action("browser", "page_find", target=m.group(1), raw=raw)
        if re.match(r"what can i click|what buttons|list (?:the )?buttons|what's on (?:this|the) page", low):
            return Action("browser", "page_controls", target="current", raw=raw)
        m = re.match(r"(?:open|find)? ?(?:the )?(?:page|article|site|tab)? ?(?:i|that i) (?:read|visited|saw|opened)(.*)$", low)
        if m or low.startswith(("history ", "from history")):
            return Action("browser", "history_open", target=_strip(m.group(1) if m else low, "history", "from history", "about", "on"),
                          parameters={"days": 2 if "yesterday" in low else 7}, raw=raw)
        m = re.match(r"(?:open )?(?:my )?bookmark(?:ed)? (.+)$", low)
        if m:
            return Action("browser", "bookmark_open", target=_strip(m.group(1), "for", "called"), raw=raw)
        m = re.match(r"(?:open|go to|visit|load|navigate to)\s+(.+)$", text, re.I)
        if m and not re.match(r"(?:the )?(?:page|article|site|tab)? ?(?:i|that i) (?:read|visited|saw|opened)",
                              m.group(1), re.I):
            return _build_action("app", "open", m.group(1), raw, None)
        if low in ("update", "update extension", "update the extension", "reload extension",
                   "reload the extension", "update yourself", "reload the bridge"):
            return Action("browser", "bridge_update", target="bridge", raw=raw)
        if low in ("set up", "setup", "connect", "set up the bridge", "install"):
            return Action("browser", "bridge_setup", target="bridge", raw=raw)
        if low in ("status", "bridge status", "are you connected"):
            return Action("browser", "bridge_status", target="bridge", raw=raw)
        return Action("browser", "page_click", target=_strip(low, "the"), parameters={"nth": 1},
                      confidence=0.5, raw=raw)

    if kind == "meet":
        if _SHARE_STATUS.search(low):
            return Action("meet", "share_status", target="current", raw=raw)
        request = share_request(low)
        if request is not None:
            return _share_action(request, raw)
        if re.match(r"(?:join|start|open)\b", low):
            code = re.search(r"([a-z]{3}-[a-z]{4}-[a-z]{3})|(https?://meet\.google\.com/\S+)", low)
            return Action("meet", "join", target=code.group(0) if code else "new", raw=raw)
        if re.search(r"(share|present).*(stop|end)|(stop|end).*(shar|present)", low):
            return Action("meet", "stop_share", target="current", raw=raw)
        for word, op in (("mic", "mic"), ("microphone", "mic"), ("mute", "mic"),
                         ("camera", "camera"), ("video", "camera"), ("caption", "captions"),
                         ("hand", "hand"), ("leave", "leave"), ("hang up", "leave"),
                         ("end call", "leave"), ("chat", "chat")):
            if word in low:
                state = (False if re.search(r"\b(off|mute|disable|stop)\b", low) and op != "leave"
                         else True if re.search(r"\b(on|unmute|enable|start)\b", low) else None)
                if word == "mute" and "unmute" not in low:
                    state = False
                return Action("meet", op, target="current", parameters={"on": state}, raw=raw)
        return Action("meet", "join", target="new", raw=raw)

    if kind == "email":
        one = _which_email(low, implied=True)
        folder = re.match(r"(?:open|go to|show)?\s*(?:my |the )?(inbox|sent(?: mail)?|all mails?|starred|drafts?|"
                          r"spam|trash|bin|important|snoozed|scheduled)(?: folder| mails?| emails?)?$", low)
        if folder:
            return Action("email", "folder", target=folder.group(1), raw=raw)
        for words, verb in ((r"reply all", "reply_all"), (r"reply", "reply"), (r"forward", "forward"),
                            (r"archive", "archive"), (r"delete|trash it|bin it", "delete"),
                            (r"(go )?back( to (the )?inbox)?$", "back"), (r"mark (it )?(as )?unread", "unread"),
                            (r"star", "star"), (r"(report )?spam", "spam")):
            if re.match(words, low):
                return Action("email", verb, target="current", raw=raw)
        if re.match(r"(open|show|click)", low) and one:
            return Action("email", "open", target=one["query"] or one["which"],
                          parameters=one, raw=raw)
        if re.match(r"(check|any|new|do i have)", low) or low in ("unread",):
            return Action("email", "check", target="inbox", raw=raw)
        if re.match(r"(read|what does|what's in|summari[sz]e)", low):
            if re.search(r"\b(this|it|current|open)\b", low) and not one:
                return Action("email", "read", target="this", parameters={"which": "this"}, raw=raw)
            if one and not re.search(r"\b(emails|mails|inbox|messages)\b", low):
                return Action("email", "read", target=one["query"] or one["which"],
                              parameters=one, raw=raw)
            who = re.search(r"from (.+)$", low)
            return Action("email", "read", target=who.group(1) if who else "inbox", raw=raw)
        if re.match(r"send", low) and len(low.split()) <= 3:
            return Action("email", "send", target="draft", raw=raw)
        m = re.match(r"(?:draft|write|compose|send)(?: an?)?(?: email| mail| reply)?(?: to (\S+(?: \S+)?))?"
                     r"(?: about ([^,]+?))?(?:,? saying (.+))?$", text, re.I)
        if m:
            return Action("email", "draft", target=(m.group(1) or "").strip(),
                          parameters={"subject": (m.group(2) or "").strip(),
                                      "body": (m.group(3) or "").strip()}, raw=raw)
        return Action("email", "check", target="inbox", raw=raw)

    if kind == "calendar":
        m = re.match(r"(?:add|create|schedule|book|new)(?: an?)?(?: event| meeting)?\s*(.*)$", low)
        if m:
            return Action("calendar", "add", target=m.group(1) or "event",
                          parameters={"text": text}, raw=raw)
        return Action("calendar", "agenda", target="tomorrow" if "tomorrow" in low else "today",
                      raw=raw)

    if kind == "message":
        if ":" in text:
            who, _, body = text.partition(":")
        else:
            m = re.match(r"(?:to )?(\S+)\s+(?:saying |that )?(.+)$", text)
            who, body = (m.group(1), m.group(2)) if m else (text, "")
        return Action("message", "send", target=_strip(who, "to"),
                      parameters={"text": body.strip(), "app": "whatsapp"}, raw=raw)
    return None


# ── Part 2c (BROWSER_CONTROL_PLAN B2, B4, B5, C3, C4) ──
GROUP_COLORS = {"grey": "grey", "gray": "grey", "blue": "blue", "red": "red", "yellow": "yellow",
                "green": "green", "pink": "pink", "purple": "purple", "violet": "purple",
                "cyan": "cyan", "teal": "cyan", "orange": "orange"}
_RESULT_NOUNS = r"(?:search )?(?:result|video|link|product|repo|repository|post|item|listing|article|hit)s?"
_RESULT_ORDINAL = (r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|top|"
                   r"\d{1,2}(?:st|nd|rd|th)?)")
_RESULT_RE = re.compile(
    r"(?:open|click(?: on)?|go to|show me|play|visit|take me to) (?:the )?" + _RESULT_ORDINAL +
    r" " + _RESULT_NOUNS + r"(?: on (?:this|the) page)?( in (?:a )?new tab)?|"
    r"(?:open|click(?: on)?|go to) " + r"(?:search )?(?:result|video|link) (?:number )?(\d{1,2})"
    r"( in (?:a )?new tab)?")
_ORDINAL_N = {"first": 1, "top": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}


def result_request(low):
    """'open the third result' -> (3, new_tab=False); 'play the last video' -> (-1, False)."""
    m = _RESULT_RE.fullmatch(low.strip(" .?!"))
    if not m:
        return None
    word = m.group(1) or m.group(3)
    nth = _ORDINAL_N.get(word) or int(re.sub(r"\D", "", word) or 0)
    return (nth, bool(m.group(2) or m.group(4))) if nth else None


def _group_name(text):
    name = re.sub(r"^(?:the|my|this|that)\s+", "", text.strip())
    name = re.sub(r"\s+(?:tab )?group$", "", name).strip()
    return "current" if name in ("", "this", "current", "this tab's", "the current") else name


def _part_2c_action(low, raw):
    """The Part 2c shapes, or None. Called before the older tab rules in `_web_action`."""
    # C3 — the Nth result of a search / list page.
    hit = result_request(low)
    if hit:
        return Action("browser", "page_result", target=str(hit[0]),
                      parameters={"nth": hit[0], "new_tab": hit[1]}, raw=raw)
    if re.fullmatch(r"(?:what are|read(?: me)?|list|show me) (?:the )?(?:search )?(?:results|videos)(?: on (?:this|the) page)?", low):
        return Action("browser", "page_results", target="current", raw=raw)
    # C4 — find with highlight, and step through it.
    m = re.fullmatch(r"(?:find|highlight|search for|look for) (.+?) (?:on|in) (?:this |the )?page", low) \
        or re.fullmatch(r"highlight (?:all |every )?(?:the )?(?:word |words )?(.+)", low)
    if m:
        return Action("browser", "page_find", target=m.group(1).strip("'\""), raw=raw)
    if re.fullmatch(r"(?:go to (?:the )?)?(?:next|find next) (?:match|result|one|occurrence|highlight)|find next", low):
        return Action("browser", "page_find_step", target="next", parameters={"step": 1}, raw=raw)
    if re.fullmatch(r"(?:go to (?:the )?)?(?:previous|prev|last|find previous) (?:match|one|occurrence|highlight)|find previous", low):
        return Action("browser", "page_find_step", target="previous", parameters={"step": -1}, raw=raw)
    if re.fullmatch(r"(?:clear|remove|hide) (?:the |all )?(?:highlights?|highlighting|matches)|stop (?:finding|highlighting)|clear (?:the )?(?:find|search)", low):
        return Action("browser", "page_find_clear", target="current", raw=raw)
    # B2 — everything except the pinned tabs.
    if re.fullmatch(r"close (?:all |every )?(?:the )?(?:tabs )?(?:but|except(?: for)?|other than) (?:the )?pinned(?: ones| tabs)?|close (?:all |every )?(?:the )?unpinned tabs?", low):
        return Action("browser", "tabs_close_unpinned", target="unpinned", raw=raw)
    # B4 — tab groups.
    if re.fullmatch(r"(?:list|show|what are|what) (?:my |the )?(?:tab )?groups(?: do i have)?|what tab groups do i have", low):
        return Action("browser", "groups_list", target="all", raw=raw)
    m = re.fullmatch(r"(?:group|put|gather|collect) (?:all )?(?:of )?(?:my |the |these )?(.+?) tabs(?: (?:together|in(?:to)? (?:a|one) group))?(?: (?:as|into|in|called|named|under) (?:a group called |the )?(.+?)(?: group)?)?", low)
    if m and m.group(1) not in ("other", "all", "these", "my", "the"):
        return Action("browser", "tabs_group", target=m.group(1),
                      parameters={"title": (m.group(2) or "").strip()}, raw=raw)
    m = re.fullmatch(r"(?:add|move|put) (?:this|the current) tab (?:to|into|in) (?:the |my )?(.+?)(?: tab)? group", low)
    if m:
        return Action("browser", "group_add", target=_group_name(m.group(1)), raw=raw)
    m = re.fullmatch(r"(?:rename|call|name) (?:the |my |this )?(.*?)(?:tab )?group (?:to |as )?(.+)", low)
    if m:
        return Action("browser", "group_rename", target=_group_name(m.group(1)),
                      parameters={"title": m.group(2).strip()}, raw=raw)
    m = re.fullmatch(r"(?:make|colou?r|change|turn|set) (?:the |my |this )?(.*?)(?:tab )?group(?:'?s? colou?r)? (?:to |as )?(\w+)", low)
    if m and m.group(2) in GROUP_COLORS:
        return Action("browser", "group_color", target=_group_name(m.group(1)),
                      parameters={"color": GROUP_COLORS[m.group(2)]}, raw=raw)
    m = re.fullmatch(r"(collapse|fold|close up|minimi[sz]e|expand|unfold|open up|open) (all (?:the |my )?(?:tab )?groups|(?:the |my |this )?(.*?)(?:tab )?group)", low)
    if m:
        collapse = m.group(1) in ("collapse", "fold", "close up", "minimise", "minimize")
        target = "all" if m.group(2).startswith("all") else _group_name(m.group(3) or "")
        return Action("browser", "group_collapse", target=target,
                      parameters={"collapsed": collapse}, raw=raw)
    m = re.fullmatch(r"ungroup (?:all(?: (?:the |my )?(?:tab )?groups)?|everything)", low)
    if m:
        return Action("browser", "group_ungroup", target="all", raw=raw)
    m = re.fullmatch(r"ungroup (?:the |my |this )?(.*?)(?:tab|tabs|tab group|group)?", low)
    if m:
        return Action("browser", "group_ungroup", target=_group_name(m.group(1) or ""), raw=raw)
    m = re.fullmatch(r"close (?:the |my |this )?(.+?) (?:tab )?group", low)
    if m:
        return Action("browser", "group_close", target=_group_name(m.group(1)), raw=raw)
    # B5 — windows.
    m = re.fullmatch(r"(?:move|send|put|pop|open) (?:this|the current) tab (?:out )?(?:in(?:to)?|to|as) (?:a |its own |a new |a separate )+window(?: on the other (?:screen|monitor|display))?|pop (?:this|the current) tab out|(?:move|send) (?:this|the current) tab to the other (?:screen|monitor|display)", low)
    if m:
        return Action("browser", "tab_detach", target="current",
                      parameters={"monitor": bool(re.search(r"other (?:screen|monitor|display)", low))},
                      raw=raw)
    if re.fullmatch(r"(?:merge|combine|join) (?:all )?(?:my |the )?(?:chrome |browser )?windows(?: together| into one)?|(?:put|move|bring) all (?:my |the )?tabs (?:into|in) (?:one|this|a single) window", low):
        return Action("browser", "windows_merge", target="all", raw=raw)
    return None


def _which_email(low, implied=False):
    """
    'the second unread email' / 'latest mail' / 'email from ana' -> {which, nth, query}.
    `implied`: inside an "email ..." command the noun is understood ("email open latest").
    """
    low = " " + (low or "") + " "
    query = ""
    m = re.search(r"\b(?:from|by|sent by) (.+?)(?: about .+)?\s*$", low)
    if m:
        query = m.group(1).strip(" .")
    m2 = re.search(r"\babout (.+?)\s*$", low)
    if m2 and not query:
        query = m2.group(1).strip(" .")
    nth, which = 1, "first"
    if re.search(r"\b(latest|newest|most recent|recent|new)\b", low):
        which = "latest"
    elif re.search(r"\boldest\b", low):
        which = "oldest"
    elif re.search(r"\blast\b", low):
        which = "last"
    for word, number in _ORDINALS.items():
        if number > 0 and re.search(rf"\b{word}\b", low):
            nth, which = number, ("nth" if number > 1 else "first")
            break
    if re.search(r"\bunread\b", low):
        which = "unread"
    selector = bool(re.search(r"\b(latest|newest|recent|oldest|last|unread|first|second|third|"
                              r"fourth|fifth|one|\d+(st|nd|rd|th))\b", low))
    plural = re.search(r"\b(emails|mails|messages|inbox)\b", low)
    if plural and not query and which not in ("nth",) and not re.search(
            r"\b(the|my) (latest|newest|first|last) (e-?mail|mail|message)\b", low):
        return None                         # "my emails", "my unread emails": the whole list
    if not (query or re.search(r"\b(e-?mails?|mails?|messages?)\b", low) or (implied and selector)):
        return None
    return {"which": which, "nth": nth, "query": query}


def _extended_action(domain, payload, raw):
    """'diagnose …', 'screen …', 'scene …', 'plan …', 'trigger …', 'files …', 'git …'."""
    text = (payload or "").strip()
    low = text.lower().strip(" .?!")
    if domain == "diagnose":
        if re.match(r"(fix|repair|apply)", low):
            return Action("diagnose", "fix", target="last", raw=raw)
        return Action("diagnose", "run", target=low or "computer", raw=raw)
    if domain == "screen":
        for pattern, op in ((r"(stop|end|finish) record", "record_stop"),
                            (r"record", "record"),
                            (r"(copy|grab|extract) (the )?text", "copy_text"),
                            (r"read", "read"),
                            (r"(explain|what does|what's) (this |the )?(error|message|warning)", "explain"),
                            (r"(what|describe|tell me|what's|look|see|explain|help|summari)", "describe")):
            if re.match(pattern, low):
                return Action("screen", op, target=text, raw=raw)
        return Action("screen", "describe", target=text, raw=raw)
    if domain == "scene":
        m = re.match(r"(?:save|remember|capture)(?: this| the current)?(?: layout| setup| scene)?(?: as)? (.+)$", low)
        if m:
            return Action("scene", "save", target=_strip(m.group(1), "mode", "scene"), raw=raw)
        if re.match(r"(list|what|which|show)", low):
            return Action("scene", "list", target="all", raw=raw)
        m = re.match(r"(?:delete|remove|forget) (.+)$", low)
        if m:
            return Action("scene", "delete", target=_strip(m.group(1)).replace(" mode", ""), raw=raw)
        name = re.sub(r"\b(mode|scene|setup)\b", "", _strip(low, "start", "run", "activate", "switch to",
                                                              "enter", "go into", "set up"))
        return Action("scene", "run", target=" ".join(name.split()) or "work", raw=raw)
    if domain == "plan":
        return Action("plan", "run", target=text, raw=raw)
    if domain == "trigger":
        if re.match(r"(list|what|which|show)", low):
            return Action("trigger", "list", target="all", raw=raw)
        m = re.match(r"(?:cancel|delete|remove|stop|clear) (.+)$", low)
        if m:
            return Action("trigger", "cancel", target=m.group(1), raw=raw)
        return Action("trigger", "add", target=text, raw=raw)
    if domain == "files":
        for pattern, op in ((r"(organi[sz]e|tidy|clean up|sort)", "organize"),
                            (r"(duplicate|dupes)", "duplicates"),
                            (r"(space|taking up|biggest|large|storage)", "space"),
                            (r"(recent|today|worked on|yesterday|last)", "recent"),
                            (r"(summari[sz]e|what does|read)", "summarize"),
                            (r"(convert|compress|resize|turn)", "convert"),
                            (r"(back ?up|backup)", "backup"),
                            (r"(find|search|where|locate)", "find")):
            if re.match(pattern, low) or (op in ("duplicates", "space", "recent") and re.search(pattern, low)):
                bulk = op in ("organize", "backup")
                return Action("files", op, target=text, parameters={"bulk": bulk}, raw=raw)
        return Action("files", "find", target=text, raw=raw)
    if domain == "dev":
        if re.match(r"commit", low):
            message = re.sub(r"^commit\s*(?:with message|message|saying)?\s*", "", text, flags=re.I)
            return Action("dev", "git_commit", target="repo", parameters={"message": message},
                          raw=raw)
        if re.match(r"(branch|which branch|what branch)", low):
            return Action("dev", "git_branch", target="repo", raw=raw)
        return Action("dev", "git_status", target="repo", raw=raw)
    return None


def _build_action(domain, action, payload, raw, context):
    """Builds the action for a prefix token, resolving pronouns and sub-verbs."""
    if domain == "web":
        return _web_action(action, payload, raw)
    if domain in ("diagnose", "screen", "scene", "plan", "trigger", "files", "dev") and \
            action in ("run", "raw", "git"):
        return _extended_action(domain, payload, raw)
    parameters = {}
    confidence = 1.0
    target = payload

    # ── Contextual reference resolution ──
    if payload and payload.lower().strip(" .?!") in targets.CURRENT_TARGET_WORDS:
        # "close it" is UNTYPED: the thing the user opened a moment ago might have been a
        # site, an app or a file, and the untyped resolver returns whichever was established
        # most recently. Only the file domain pins a kind, because "delete it" reaching for a
        # browser tab would be nonsense.
        kind = "file" if domain == "file" else None
        referent = context.resolve_referent(kind) if context else None
        if referent:
            target = referent
            parameters["resolved_from"] = payload
        else:
            # No fresh referent. Keep the pronoun and drop confidence — the policy layer will
            # ask rather than let a `close` act on a guess.
            confidence = 0.3

    if domain == "keyboard" and action == "type":
        return Action(domain, action, target="current",
                      parameters={"text": payload}, raw=raw)

    if domain == "shell":
        return Action("shell", "run", target=payload,
                      parameters={"command": payload}, raw=raw)

    if domain == "timer":
        seconds = parse_duration(payload)
        label = payload
        if not seconds:
            # "reminder 9:00pm call mom", "remind me at 7 am to stretch": a clock time.
            seconds, label = parse_clock_reminder(payload)
        return Action("timer", action, target=payload,
                      parameters={"seconds": seconds, "label": label},
                      confidence=1.0 if seconds else 0.4, raw=raw)

    if domain == "system" and action == "raw":
        lowered_payload = (payload or "").lower()
        for needle, verb in _SYSTEM_VERBS:
            if needle not in lowered_payload:
                continue

            # A question about the device is a READ. It becomes an info action, which the
            # planner may run concurrently and the policy always allows.
            if verb in _INFO_VERBS:
                return Action("info", verb, target=payload, raw=raw)
            # "move/send/put this window to an empty workspace" — any phrasing that MOVES a
            # window rather than going somewhere. The needle table cannot list every article.
            if verb == "workspace" and re.match(r"\s*(move|send|put|throw|shift)\b",
                                                lowered_payload):
                verb = "workspace_move"

            # ── THE POWER-TARGET GATE ──
            # `needle in payload` is a SUBSTRING test, and for the power verbs a substring
            # test was the whole of the safety logic. "system shutdown the engine car" — a
            # malformed transcript of a request about KAYRA's engine — matched "shutdown" and
            # became `Action("system", "shutdown")`, one reflexive "yes" away from ending the
            # user's Windows session.
            #
            # The substring match is kept, because it is a fine way to find the VERB. What it
            # may no longer do is decide the TARGET. Nothing infers a computer shutdown any
            # more; the user has to name one.
            if verb in policy.POWER_VERBS:
                target_kind = policy.resolve_power_target(payload, _assistant_alias())
                if target_kind == policy.TARGET_KAYRA:
                    # A lifecycle request that reached the automation layer only because the
                    # transcript missed the local control vocabulary. Hand it back; never
                    # touch the machine for it.
                    return Action("system", "power_kayra", target=payload,
                                  parameters={"command": payload, "verb": verb,
                                              "power_target": target_kind},
                                  confidence=0.9, raw=raw)
                if target_kind != policy.TARGET_COMPUTER:
                    return Action("system", "power_ambiguous", target=payload,
                                  parameters={"command": payload, "verb": verb,
                                              "power_target": target_kind},
                                  confidence=0.2, raw=raw)
                return Action("system", verb, target=payload,
                              parameters={"command": payload,
                                          "power_target": target_kind}, raw=raw)

            return Action("system", verb, target=payload,
                          parameters={"command": payload}, raw=raw)
        # NO VERB MATCHED, but the payload may still be a power request phrased in a way the
        # needle table does not carry — "turn off the engine", "power down". A bare "turn off"
        # is deliberately NOT a needle above, because "turn off wifi" and "turn off the
        # camera" are ordinary requests that must keep working; it becomes a power verb only
        # when the sentence also NAMES a target, which is the same evidence the gate wants.
        if any(word in lowered_payload for word in ("turn off", "power down", "turn it off")):
            target_kind = policy.resolve_power_target(payload, _assistant_alias())
            if target_kind == policy.TARGET_KAYRA:
                return Action("system", "power_kayra", target=payload,
                              parameters={"command": payload, "verb": "shutdown",
                                          "power_target": target_kind},
                              confidence=0.9, raw=raw)
            if target_kind == policy.TARGET_COMPUTER:
                return Action("system", "shutdown", target=payload,
                              parameters={"command": payload,
                                          "power_target": target_kind}, raw=raw)

        return Action("system", "raw", target=payload,
                      parameters={"command": payload}, confidence=0.6, raw=raw)

    if domain == "app" and action == "open" and re.search(
            r"\b(e-?mails?|mails?)\b", (target or "").lower()) and re.search(
            r"\b(first|second|third|fourth|fifth|last|latest|newest|recent|oldest|unread|from|"
            r"about|\d+(st|nd|rd|th))\b", (target or "").lower()):
        # "open the first email" is an email, never an application called "first email".
        return _web_action("email", "open " + target, raw)

    if domain == "app" and action == "open":
        target, where = _split_where(target)
        if where:
            parameters["where"] = where
        if re.search(r"\b(?:i|that i|i was) (?:read|visited|saw|opened|reading|watching|watched)\b",
                     target or "", re.I):
            # "open the article I read yesterday" is a HISTORY lookup, not an app or a site.
            return _web_action("browser", target, raw)
        # TYPE THE TARGET HERE, not three layers down. "open X" is an application launch only
        # when X is an application; when X is an address or a web service it is a browser
        # navigation, and the two must not share an execution path. Two dictionary lookups,
        # measured in microseconds — there is no reason to defer this decision.
        if targets.is_explicit_url(target):
            return Action("browser", "open_url", target=target,
                          parameters={"url": targets.normalize_url(target), **parameters},
                          raw=raw)
        if not targets.canonical_app(target) or where:
            url = targets.website_url(target)
            if url:
                return Action("browser", "open_url", target=target,
                              parameters={"url": url, "site": targets.canonical_website(target),
                                          **parameters},
                              confidence=confidence, raw=raw)

    if domain == "app" and action == "close_all":
        # "close all chrome windows" -> the application is "chrome". The trailing noun is
        # grammar, not part of the target.
        target = re.sub(r"\s+(windows|window|tabs|tab|instances)$", "", target or "",
                        flags=re.I).strip()
        if not target or target in ("windows", "window"):
            return Action("window", "close_all", target="all", raw=raw)
        return Action("app", "close_all", target=target, raw=raw)

    if domain == "app" and action == "close":
        # "close this"/"close it" with no referent means the foreground WINDOW, which is both
        # the safest reading and what a person actually means standing at their desk.
        if confidence < 1.0 and (payload or "").lower().strip(" .?!") in targets.CURRENT_TARGET_WORDS:
            return Action("window", "close", target="current", raw=raw)

    if domain == "browser" and action in ("search_web", "search_youtube"):
        target, where = _split_where(target)
        if where:
            parameters["where"] = where

    if domain == "file" and action == "open_file":
        # "open file explorer" is an APPLICATION, not a file called "explorer". The prefix
        # table is longest-first, so `open file ` legitimately wins the literal match — what
        # disambiguates is the target: a curated application name with no file extension was
        # never a filename. Without this, "open file explorer" reached the filesystem
        # resolver, missed, and reported that it could not find a file.
        if targets.canonical_app(target) and not os.path.splitext(str(target))[1]:
            return Action("app", "open", target=target, raw=raw)

    if domain == "file" and action in ("create_file", "create_folder", "rename", "delete"):
        if action == "rename" and " to " in payload.lower():
            source, _, new_name = payload.rpartition(" to ")
            return Action(domain, action, target=source.strip(),
                          parameters={"new_name": new_name.strip()}, raw=raw)
        if action == "delete":
            parameters["bulk"] = bool(re.search(r"[*?]|\ball\b|\beverything\b", payload.lower()))

    return Action(domain, action, target=target, parameters=parameters,
                  confidence=confidence, raw=raw)


# Domains that only READ. Two of these can genuinely run at the same time because neither
# touches the screen, the keyboard, or the foreground window.
_CONCURRENT_SAFE = frozenset({"info"})


def plan_actions(actions):
    """
    Orders actions into execution groups.

    Every group is executed in order; the actions inside one group run concurrently.

    The rule is deliberately conservative: only pure reads share a group. Everything else
    contends for exactly one resource — the foreground window and the keyboard focus — and
    running two of those at once is a race with a visible, wrong outcome.

    This is a correctness fix, not a tuning choice. The old router built every command as an
    `asyncio.to_thread` task and fired them all through one `asyncio.gather`, so
    "open chrome and maximize the window" raced the maximize against the launch, and
    "open chrome, open youtube" sent both keystroke sequences into whatever had focus at that
    instant.
    """
    groups = []
    batch = []
    for action in actions:
        if action.domain in _CONCURRENT_SAFE:
            batch.append(action)
            continue
        if batch:
            groups.append(batch)
            batch = []
        groups.append([action])
    if batch:
        groups.append(batch)
    return groups


# Results that mean "the rules found nothing and nothing happened" — the only results the
# understanding layer may take a second look at. Never for power, files, the shell, reads or
# timers: those either did something already or must not be second-guessed.
_RETHINK_STATUSES = frozenset({Status.NOT_FOUND, Status.UNSUPPORTED})
_NEVER_RETHINK = frozenset({"system", "file", "files", "shell", "info", "timer", "dev",
                            "diagnose", "plan", "trigger", "scene"})


def _is_guess(action):
    """The parser's own catch-all: 'browser <anything>' read as a click, at low confidence."""
    return action.domain == "browser" and action.action == "page_click" and action.confidence < 1.0


def _worth_rethinking(action, result):
    return (result is not None and result.status in _RETHINK_STATUSES
            and action.domain not in _NEVER_RETHINK)


_CONFIRM_PROMPTS = {
    # These are reached ONLY once `resolve_power_target` has returned COMPUTER, so the
    # wording can be specific about the target. It has to be: a prompt that says "your
    # computer" about a request that never named one is how a reflexive "yes" ends a session.
    "system.shutdown": "This will shut down your COMPUTER, not Kayra. Should I go ahead?",
    "system.restart": "This will restart your COMPUTER, not Kayra. Should I go ahead?",
    "system.sign_out": "This will sign you out. Should I go ahead?",
    "system.sleep": "This will put the computer to sleep. Should I go ahead?",
    "system.wifi_off": "This will turn off Wi-Fi. Should I go ahead?",
    "file.delete": "That will delete it. Should I go ahead?",
    "app.kill": "That will force-close it and you may lose unsaved work. Should I go ahead?",
    "window.close_all": "That will close every open window. Should I go ahead?",
}


_TRUST_TTL_S = 3 * 3600          # "don't ask again" lasts for the call, at most three hours
_TRUSTABLE = {"meet.share"}      # the ONLY confirmation that may be switched off by voice
_DONT_ASK = re.compile(r"[\s,.!]*\b(?:and )?(?:(?:don'?t|do not|never|no need to) ask(?: me)?"
                       r"(?: again| that again| for this call| this call)*|stop asking(?: me)?)\b.*$")


def _share_what(action):
    name = (action.parameters or {}).get("name") or ""
    surface = action.target or "monitor"
    if surface == "window":
        return f"the {name} window" if name else "a window"
    if surface == "browser":
        return f"the {name} tab" if name else "a Chrome tab"
    if name:
        return {"#1": "your first screen", "#2": "your second screen", "#3": "your third screen",
                "<": "your left screen", ">": "your right screen"}.get(name, f"the {name} screen")
    return "your entire screen"


def _confirm_prompt(action, reason):
    if action.key == "meet.share":
        # Says WHAT goes to everyone (M0.5): "Share the LM Studio window with everyone in the
        # call?" — a reflexive yes to "share monitor?" is how the whole desktop got shared.
        return (f"Share {_share_what(action)} with everyone in the call? You can say "
                f"\"yes, don't ask again\" for the rest of this call.")
    prompt = _CONFIRM_PROMPTS.get(action.key)
    if prompt:
        return prompt
    if action.domain == "shell":
        return f"I can run that, but {reason}. Should I?"
    what = action.target or action.action.replace("_", " ")
    return f"Just to check — you want me to {action.action.replace('_', ' ')} {what}?"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     THE SHARED EXECUTION RUNTIME                       │
# └────────────────────────────────────────────────────────────────────────┘

def _runtime_state():
    """The process runtime, or None when the automation layer runs standalone."""
    try:
        from kayra.core.runtime_state import get_runtime_state
        return get_runtime_state()
    except Exception:
        return None


class AutomationRuntime:
    """
    Policy -> dispatch -> record, for one platform backend.

    A backend constructs ONE of these with its `dispatch(action) -> ActionResult` and a
    `notify(label)` for fired timers, then exposes the bound methods as its public API
    (`Automation`, `execute_action`, `pending_confirmation`, `resolve_confirmation`,
    `shutdown_automation`). The policy verdict is taken here and nowhere else, so a backend
    cannot forget to ask it — the dispatch callable is never reached for a DENY, and a CONFIRM
    parks the action until the user answers.
    """

    def __init__(self, dispatch, notify=None, refusal=None, on_shutdown=None):
        self._dispatch = dispatch
        self._refusal = refusal or _power_refusal_sentence
        self._on_shutdown = on_shutdown
        self.confirmations = ConfirmationManager()
        self.context = AutomationContext()
        self.timers = TimerService(notify=notify)
        # Steps of a multi-step plan (a scene, a planned goal) waiting behind a confirmation.
        self.pending_plan = []
        # The understanding layer (automation/interpreter.py). `situation_fn` returns what is
        # on screen (the backend sets it); `chat_handoff` is raised when the model decides an
        # "automation" token was really conversation, and the app answers it as chat.
        self.situation_fn = None
        self.chat_handoff = False
        # Confirmations the user switched off by voice ("yes, don't ask again"): key -> expiry.
        # Only `_TRUSTABLE` keys, cleared when the call ends (meet.leave).
        self.trusted = {}

    def take_chat_handoff(self):
        """True once after a request the understanding layer judged to be conversation."""
        flag, self.chat_handoff = self.chat_handoff, False
        return flag

    def _situation(self):
        try:
            return self.situation_fn() if self.situation_fn else {}
        except Exception:
            return {}

    def _understand(self, request, reason=""):
        """Ask the understanding layer. Returns an Interpretation (kind 'none' when off/failed)."""
        from kayra.automation import interpreter
        result = interpreter.interpret(request, self._situation(), reason,
                                       normalize=lambda t: normalize_command(t, self.context),
                                       classify=classify_action)
        print_info(f"[AI] understood {request!r} as {result.kind}"
                   + (f": {result.commands}" if result.commands else ""))
        return result

    # ── one action ──
    def execute_action(self, action, skip_policy=False):
        if action is None:
            return ActionResult.unsupported("I didn't understand that one.")
        audit(AUDIT_STARTED, action)

        if not skip_policy:
            verdict, reason = classify_action(action)
            if verdict == Risk.DENY:
                audit(AUDIT_BLOCKED, action, reason=reason)
                spoken = self._refusal(action)
                return ActionResult.blocked(spoken or f"I won't do that — {reason}.")
            if verdict == Risk.CONFIRM and self.trusted.get(action.key, 0) > time.monotonic():
                verdict = Risk.ALLOW                 # the user said "don't ask again" this call
            if verdict == Risk.CONFIRM:
                prompt = _confirm_prompt(action, reason)
                self.confirmations.request(action, prompt)
                audit(AUDIT_CONFIRM, action, reason=reason)
                return ActionResult(Status.NEEDS_CONFIRMATION, prompt, {"reason": reason}, action)

        try:
            result = self._dispatch(action)
        except Exception as e:
            audit(AUDIT_FAILED, action, error=str(e))
            return ActionResult.failure("That didn't work.", error=str(e))

        if result is None:
            result = ActionResult.unsupported("I don't know how to do that yet.")
        result.action = action

        # A handler may find out only while acting that the request is bigger than it sounded
        # ("close all YouTube tabs" -> 7 tabs, B2). It then returns NEEDS_CONFIRMATION with the
        # exact action to run on "yes" (`detail["confirm"]`, already narrowed to what it found);
        # nothing has happened yet. Registered here, like a policy confirmation.
        if result.status == Status.NEEDS_CONFIRMATION and isinstance(
                (result.detail or {}).get("confirm"), Action):
            pending = result.detail["confirm"]
            self.confirmations.request(pending, result.message)
            audit(AUDIT_CONFIRM, pending, reason="larger than it sounded")
            return result

        if action.key == "meet.leave" and result.ok:
            self.trusted.pop("meet.share", None)     # the call is over, so is the trust
        if result.status == Status.AMBIGUOUS:
            audit(AUDIT_AMBIGUOUS, action, candidates=result.detail.get("candidates"))
        elif result.ok:
            audit(AUDIT_SUCCESS, action)
            self.context.note_action(action, True)
        else:
            audit(AUDIT_FAILED, action, status=result.status, reason=result.message)
        return result

    # ── a batch of DMM tokens ──
    async def translate_and_execute(self, commands):
        if not commands:
            return ""
        actions, unmapped = [], []
        from_model = set()          # ids of actions the understanding layer proposed
        spoken = []
        for command in commands:
            action = normalize_command(command, self.context)
            if action is None or _is_guess(action):
                # The rules don't understand it (or only guessed): ask what was MEANT.
                understood = await asyncio.to_thread(self._understand, command)
                if understood.kind == "automation":
                    for token in understood.commands:
                        proposed = normalize_command(token, self.context)
                        if proposed is not None:
                            from_model.add(id(proposed))
                            actions.append(proposed)
                    continue
                if understood.kind == "chat":
                    self.chat_handoff = True
                    continue
                if understood.kind == "ask":
                    return understood.question
            if action is None:
                unmapped.append(command)
                print_warning(f"Skipping unmapped automation token: '{command}'")
                continue
            actions.append(action)
        if not actions:
            return "" if self.chat_handoff else ("I'm not sure how to do that." if unmapped else "")

        self._set_state("AUTOMATING")
        outcomes = {"ok": 0, "failed": 0, "tokens": []}
        try:
            for group in plan_actions(merge_share_steps(merge_tab_steps(actions))):
                if len(group) == 1:
                    results = [await asyncio.to_thread(self.execute_action, group[0])]
                    if _worth_rethinking(group[0], results[0]) and id(group[0]) not in from_model:
                        results = await asyncio.to_thread(self._rethink, group[0], results[0])
                else:
                    results = await asyncio.gather(
                        *(asyncio.to_thread(self.execute_action, a) for a in group))
                for result in results:
                    if result.message:
                        spoken.append(result.message)
                    if result.status == Status.OK:
                        outcomes["ok"] += 1
                    elif result.status == Status.FAILED:
                        outcomes["failed"] += 1
                        if getattr(result, "action", None):
                            outcomes["tokens"].append(result.action.key)
                    # A question stops the batch: the rest of the plan depended on the answer.
                    if result.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS):
                        return " ".join(spoken)
        finally:
            self._set_state("PROCESSING")
            self._emit(outcomes)
        return " ".join(spoken)

    def _rethink(self, action, result):
        """
        The rules ran and found nothing ("I couldn't find an app called XYZ chat"). Nothing
        happened, so asking the understanding layer and running what it proposes is safe —
        every proposal still goes through the policy. Returns the results to report.
        """
        understood = self._understand(action.raw or action.target, result.message)
        if understood.kind == "chat":
            self.chat_handoff = True
            return [ActionResult(Status.OK, "")]
        if understood.kind == "ask":
            return [ActionResult(Status.AMBIGUOUS, understood.question)]
        if understood.kind != "automation":
            if (result.detail or {}).get("web_fallback"):
                # The model had nothing better: the last resort is the web (top result or a
                # search), exactly what the rules would have done without a model.
                retry = Action(action.domain, action.action, target=action.target,
                               parameters={**action.parameters, "web": True}, raw=action.raw)
                return [self.execute_action(retry)]
            return [result]
        out = []
        for token in understood.commands:
            proposed = normalize_command(token, self.context)
            if proposed is None:
                continue
            outcome = self.execute_action(proposed)
            out.append(outcome)
            if outcome.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS) or not outcome.ok:
                break
        return out or [result]

    async def automation(self, commands):
        return await self.translate_and_execute(commands)

    # ── multi-step plans ──
    def run_steps(self, tokens):
        """
        Runs DMM-style tokens in order, each through the full policy. A step that needs a
        confirmation parks the REST of the plan; "yes" runs it and then carries on, "no"
        drops the remainder. Returns the spoken sentences of what ran.
        """
        spoken = []
        tokens = list(tokens)
        while tokens:
            token = tokens.pop(0)
            action = normalize_command(token, self.context)
            if action is None:
                continue
            result = self.execute_action(action)
            if _worth_rethinking(action, result) and (result.detail or {}).get("web_fallback"):
                result = self._rethink(action, result)[-1]
            if result.message:
                spoken.append(result.message)
            if result.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS):
                self.pending_plan = tokens if result.status == Status.NEEDS_CONFIRMATION else []
                break
        return spoken

    # ── confirmations ──
    def pending_confirmation(self):
        return self.confirmations.prompt if self.confirmations.peek() is not None else None

    def resolve_confirmation(self, reply):
        action = self.confirmations.peek()
        trust = False
        if action is not None and action.key in _TRUSTABLE:
            bare = _DONT_ASK.sub("", str(reply or "").lower()).strip(" ,.!")
            if bare != str(reply or "").lower().strip(" ,.!") and read_confirmation_reply(bare or "yes"):
                trust, reply = True, bare or "yes"
        verdict = read_confirmation_reply(reply)
        if verdict is None:
            return False, ""
        if action is None:
            return False, ""
        if trust and verdict is True:
            self.trusted[action.key] = time.monotonic() + _TRUST_TTL_S
        if verdict is False:
            self.confirmations.cancel()
            audit(AUDIT_BLOCKED, action, reason="declined by user")
            if self.pending_plan:
                self.pending_plan = []
                return True, "Alright, I won't, and I've stopped the rest of the plan."
            return True, "Alright, I won't."
        approved = self.confirmations.consume()
        if approved is None:
            self.pending_plan = []
            return True, "That request expired. Ask me again if you still want it."
        result = self.execute_action(approved, skip_policy=True)
        spoken = [result.message or "Done."]
        if trust:
            spoken.append("I won't ask again before sharing during this call.")
        if self.pending_plan:
            rest, self.pending_plan = self.pending_plan, []
            spoken += self.run_steps(rest)
        return True, " ".join(spoken)

    def shutdown(self):
        cancelled = self.timers.shutdown()
        self.confirmations.cancel()
        if self._on_shutdown:
            try:
                self._on_shutdown()
            except Exception:
                pass
        return cancelled

    # ── runtime bus ──
    @staticmethod
    def _set_state(name):
        runtime = _runtime_state()
        if runtime is None:
            return
        try:
            from kayra.core.runtime_state import AssistantState
            runtime.set_state(getattr(AssistantState, name))
        except Exception:
            pass

    @staticmethod
    def _emit(outcomes):
        runtime = _runtime_state()
        if runtime is None or not (outcomes["ok"] or outcomes["failed"]):
            return
        try:
            runtime.emit("automation_result", ok=outcomes["ok"], failed=outcomes["failed"],
                         tokens=[t for t in outcomes["tokens"] if t])
        except Exception:
            pass


# ┌────────────────────────────────────────────────────────────────────────┐
# │          ANNOUNCER — how background automation speaks                  │
# └────────────────────────────────────────────────────────────────────────┘
# Timers, reminders, triggers, health alerts and progress lines all happen OUTSIDE a turn, so
# they cannot hand a sentence back to the turn loop. `app.bootstrap` installs the speaker
# (`set_announcer`); before that, or in a test, announcements only reach the log.

_ANNOUNCER = {"fn": None}
ANNOUNCED = []                     # the last few announcements, for tests and "what did you say"


def set_announcer(fn):
    _ANNOUNCER["fn"] = fn


def announce(text, urgent=False):
    """Speak `text` unprompted. `urgent` speaks now; otherwise the speaker may wait its turn."""
    text = str(text or "").strip()
    if not text:
        return False
    ANNOUNCED.append(text)
    del ANNOUNCED[:-20]
    fn = _ANNOUNCER["fn"]
    if fn is None:
        print_info(f"[announce] {text}")
        return False
    try:
        fn(text, urgent)
        return True
    except Exception:
        return False
