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
    return ActionResult.failure("That finished with an error.", returncode=completed.returncode)


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

    # 1a''. Workspace phrasings without the `browser` prefix (Part 4): "open my budget sheet"
    # is a file in Sheets, never an app called "my budget sheet".
    found = profile_action(lowered, raw, raw)           # Part 5P: "open my Work Chrome"
    if found is not None:
        return found
    found = part_7_action(lowered, raw, raw)            # Part 7: the page agent, learning
    if found is not None:
        return found
    found = _bare_workspace(lowered, raw)
    if found is not None:
        return found

    # 1a-fix. Fix round V11: closing a search, a described search, a scroll with a place, and
    # WhatsApp chats named with "in whatsapp" / "chat" — before the prefix rules misread them.
    found = _bare_fix(lowered.strip(" .?!"), raw)
    if found is not None:
        return found
    found = _bare_part_6(lowered.strip(" .?!"), raw)      # Part 6, when it can only be that
    if found is not None:
        return found
    found = _bare_part_6b(lowered.strip(" .?!"), raw)     # Part 6b, when it can only be that
    if found is not None:
        return found

    # 1a'. Part 3 phrasings the DMM may send without the `browser` prefix: they are about
    # the page in front, never an app ("translate this page", "bookmark this page").
    if _BARE_PAGE.match(lowered):
        found = part_3_action(lowered, raw, raw)
        if found is not None:
            return found

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


def _bare_present(action):
    """A share request that only said "present" / "start presenting" — no screen, window or tab."""
    raw = str(action.raw or "").lower()
    return action.target == "monitor" and not action.parameters.get("name") and \
        bool(re.search(r"\bpresent", raw)) and not re.search(r"screen|window|tab|monitor|display|desktop", raw)


def _share_action(request, raw):
    surface = request["surface"]
    params = {"name": request["name"]}
    if request.get("audio"):
        params["audio"] = True
    if request.get("instead"):
        params["instead"] = True
    return Action("meet", "share", target=surface, parameters=params, raw=raw)


_BARE_PAGE = re.compile(
    r"^(?:translate (?:this|the) (?:page|site|article)|bookmark (?:this|the current) (?:page|tab|site)|"
    r"(?:read|show|summari[sz]e) (?:me )?(?:the )?(?:top |latest )?(?:comments|reviews)|"
    r"(?:summari[sz]e|what is|what's) (?:this|the) (?:youtube )?video|what (?:does|do) (?:this|the) page say|"
    r"what can i (?:do|say) here|(?:clear|delete) (?:my )?(?:browsing )?history|"
    r"open (?:my |the )?(?:last|latest) download|what did i (?:just )?download|explain (?:the |this )?(?:selected|highlighted))")


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


# ── Fix round V11: scrolling, one search action, closing a search, WhatsApp by name ──
# What may be scrolled. "side" = the list on the left (chats, labels); everything else is
# whatever fills the middle of the view: the open mail, the chat, the page.
_SCROLL_WHERE = {"chat list": "side", "chats": "side", "sidebar": "side", "side bar": "side", "list": "side",
                 "menu": "side", "mail": "", "email": "", "message": "", "messages": "", "thread": "",
                 "chat": "", "conversation": "", "group": "", "inbox": "", "page": "", "sheet": "",
                 "spreadsheet": "", "doc": "", "document": "", "article": "", "screen": "", "window": "",
                 "tab": "", "site": "", "website": "", "results": "", "feed": ""}
_SCROLL_FILLER = frozenset("the this my to of in on a at it here now whole current open one that all way "
                           "please page just bit little".split())
_SCROLL_ENDS = ((r"\b(?:very )?(?:top|start|beginning|first message)\b", "top"),
                (r"\b(?:very )?(?:bottom|end|last message|latest message)\b", "bottom"),
                (r"\b(?:back )?up(?:wards?)?\b", "up"), (r"\bdown(?:wards?)?\b|\bfurther\b|\bmore\b", "down"))


def scroll_request(low):
    """
    'scroll the mail to the bottom' -> {"direction": "bottom", "where": "", "amount": 1.0}.
    None when the words name something to scroll TO ("scroll to pricing"): that is a search
    of the page, handled by the older rule.
    """
    low = low.strip(" .?!")
    m = re.fullmatch(r"(?:go|jump|take me|move|get)(?: back)? to the (top|bottom|end|start|beginning) of "
                     r"(?:the|this|my) ([a-z ]+)", low)
    if m and m.group(2) in _SCROLL_WHERE:
        return {"direction": "top" if m.group(1) in ("top", "start", "beginning") else "bottom",
                "where": _SCROLL_WHERE[m.group(2)], "amount": 1.0}
    m = re.fullmatch(r"(?:please )?(?:can you )?(?:keep )?scroll(?:ing)?\b(.*)", low)
    if not m:
        return None
    rest, direction, amount, where = f" {m.group(1)} ", None, 1.0, ""
    for pattern, name in _SCROLL_ENDS:
        if re.search(pattern, rest):
            direction, rest = name, re.sub(pattern, " ", rest, count=1)
            break
    if " all the way " in rest and direction in ("up", "down"):
        direction = "top" if direction == "up" else "bottom"
    for pattern, factor in ((r"\ba (?:little |tiny |small )?bit\b|\ba little\b|\bslightly\b|\bslowly\b", 0.4),
                            (r"\bhalf(?: a| the)?(?: page)?\b", 0.6), (r"\ba lot\b|\bfast(?:er)?\b|\btwo pages\b", 2.0)):
        if re.search(pattern, rest):
            amount, rest = factor, re.sub(pattern, " ", rest, count=1)
            break
    for name in sorted(_SCROLL_WHERE, key=len, reverse=True):
        if re.search(rf"\b{name}\b", rest):
            where, rest = _SCROLL_WHERE[name], re.sub(rf"\b{name}\b", " ", rest, count=1)
            break
    if any(word not in _SCROLL_FILLER for word in rest.split()):
        return None
    return {"direction": direction or "down", "where": where, "amount": amount}


_SEARCH_BOX = r"(?:the |this |that )?search(?: bar| box| button| icon| field| option| input)?"
_SEARCH_CLOSE_RE = re.compile(
    r"(?:close|clear|cancel|exit|dismiss|hide|leave|stop|remove|delete|erase|empty|reset|get out of|come out of|go out of) "
    r"(?:the |this |my |that )?(?:text (?:in|from|of) (?:the )?)?(?:[a-z]+ )?search(?:ing)?"
    r"(?: bar| box| field| results?| panel| mode| input| text| query)*"
    r"(?: (?:in|on|of|from) (?:the |my )?[a-z]+(?: web)?)?"
    r"(?: and (?:go|step|get|take me) back(?: to (?:the |my )?(?:chats?|chat list|inbox|normal))?)?|"
    r"(?:go|step|get|take me) back to (?:my |the )?(?:chats|chat list)")
_WA_CLOSE_CHAT = re.compile(r"(?:close|exit|leave|get out of|come out of) (?:this |the |that |current |open )*"
                            r"(?:whats ?app )?(?:chat|group|conversation)(?: (?:in|on) whats ?app)?")
_WA_NTH = re.compile(r"(?:the )?(first|second|third|fourth|fifth|top|1st|2nd|3rd|4th|5th) (?:search )?(?:result|one|chat|group)\b.*")


def search_request(low, text=None):
    """
    The words to search for when the user describes the STEPS of a search — "click on the
    search button and search for council details", "search for rahul in the search bar" — or
    None. It is one action: find the search box, click it, type, submit. It used to be a
    click on a button called "search button and search for council details".
    """
    low = low.strip(" .?!")
    m = re.fullmatch(r"(?:click|tap|press|go to|open|focus|select|use)(?: on| in)? " + _SEARCH_BOX +
                     r"(?:,| and| then)+ (?:search(?: for)?|type(?: in)?|write|enter|look for|look up|find) (.+?)"
                     r"(?:(?:,| and| then)+ (?:press|hit|click|tap)(?: on)? (?:the )?(?:enter|search|go)(?: button| key)?)?", low) \
        or re.fullmatch(r"(?:search(?: for)?|look for|look up|find) (.+?) (?:in|on|using|with|from|through) " + _SEARCH_BOX, low)
    if not m:
        return None
    query = m.group(1).strip()
    found = re.search(re.escape(query), text or "", re.I)
    return found.group(0) if found else query


_WA_SUFFIX = r"(?: (?:on|in|from) (?:my )?whats ?app(?: web)?)"
_WA_NAMED_RE = re.compile(r"(?:(?:open|go to|show|show me|switch to|read|summari[sz]e|catch me up on|what(?:'s| is) new in|"
                          r"search(?: for)?|find|look for|look up) .+" + _WA_SUFFIX +
                          r"|search (?:my )?whats ?app for .+)(?: and open (?:it|that|the (?:chat|group|first one)))?")
_WA_SEARCH = re.compile(r"(?:search|find|look for|look up)(?: (?:my )?whats ?app)?(?: for)? (?:the |my |our )?(.+?)"
                        r"(?: chat| group| conversation)?" + _WA_SUFFIX + r"?"
                        r"( and open (?:it|that|the (?:chat|group|first one)))?")
_WA_READ_RECENT = re.compile(r"(?:read|summari[sz]e)(?: me)? (?:the |my )?(?:most )?(?:recent|latest|last|new|newest) chats?")
_NUM = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|couple of|few)"
_WA_AGE = r"(?:last|latest|recent|most recent|newest|new|past|previous)"
# "read the last three messages of mota panda", "open and read the latest 3 recent chats from X"
_WA_LAST_N = re.compile(
    r"(?:open (?:and|then) )?(?:read|show|tell|give|check|summari[sz]e|what (?:are|were))(?: me)?(?: out)? (?:the |my )?"
    r"((?:" + _WA_AGE + r" |" + _NUM + r" )+)(?:chats?|messages?|texts?|msgs?) "
    r"(?:of|from|in|by|with|on) (?:the |my |our )?(.+?)(?: chat| group| conversation)?" + _WA_SUFFIX + r"?")
_WA_LAST_N_POSS = re.compile(
    r"(?:open (?:and|then) )?(?:read|show|tell me|summari[sz]e)(?: me)?(?: out)? (.+?)(?:'s|s') "
    r"((?:" + _WA_AGE + r" |" + _NUM + r" )+)(?:chats?|messages?|texts?|msgs?)" + _WA_SUFFIX + r"?")
_WA_LAST_HERE = re.compile(r"(?:read|show|tell|give)(?: me)?(?: out)? (?:the |my )?((?:" + _WA_AGE + r" |" + _NUM + r" )+)"
                           r"(?:chats?|messages?|texts?|msgs?)(?: here| in (?:this|the) (?:chat|group))?")
_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "couple of": 2, "few": 3}


def last_messages_request(low):
    """('mota panda', 3) for "read the last three messages of mota panda"; None otherwise.
    count 0 = the words gave no number ("the latest messages from X")."""
    low = low.strip(" .?!")
    m = _WA_LAST_N.fullmatch(low)
    name, words = (m.group(2), m.group(1)) if m else (None, None)
    if not m:
        m = _WA_LAST_N_POSS.fullmatch(low)
        if not m:
            return None
        name, words = m.group(1), m.group(2)
    name = re.sub(r"^(?:the|my|our)\s+", "", name.strip())
    found = re.search(r"\d{1,2}|couple of|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|few)\b", words)
    count = 0 if not found else int(found.group(0)) if found.group(0).isdigit() else _COUNT_WORDS[found.group(0)]
    if not name or name in ("this", "it", "that", "here", "whatsapp", "whats app"):
        name = "current" if count else "recent"      # no chat named: the open one / the newest
    return name, min(count, 30)


_WA_RECENT = re.compile(r"(?:the )?(?:most )?(?:recent|latest|last|newest|new)(?: one| chat| message| messages)?")


def _bare_fix(lowered, raw):
    """Shapes the DMM sends without a prefix and that the prefix rules get WRONG: "close the
    search bar" (an app called "search bar"), "open graphics team group in whatsapp" (an app),
    "scroll the mail to bottom" (nothing), "click on the search button and search for X"."""
    if _SEARCH_CLOSE_RE.fullmatch(lowered):
        return Action("browser", "search_close", target="search", raw=raw)
    query = search_request(lowered, raw)
    if query:
        return Action("browser", "search_here", target=query, raw=raw)
    if lowered.startswith(("scroll", "keep scroll", "please scroll")) or " of the " in lowered or " of this " in lowered:
        request = scroll_request(lowered)
        if request:
            return Action("browser", "page_scroll", target=request["direction"], parameters=request, raw=raw)
    if _WA_NAMED_RE.fullmatch(lowered) or _WA_READ_RECENT.fullmatch(lowered) or last_messages_request(lowered):
        return _web_action("message", raw, raw)
    if _WA_CLOSE_CHAT.fullmatch(lowered) and re.search(r"\b(?:chat|conversation|whats ?app)\b", lowered):
        return Action("message", "close", target="current", raw=raw)     # "close the chat"
    # "open the graphics team group": a Chrome tab group by that name, else WhatsApp's group.
    if re.fullmatch(r"open (?:the |my |our )?.+ group", lowered) and "tab group" not in lowered:
        return _part_2c_action(lowered, raw)
    return _named_chat(lowered, raw)


def _named_chat(low, raw):
    """'open rahul chat' with nothing saying WhatsApp: a chat is a WhatsApp chat unless an app
    or a site is really called that ("google chat")."""
    m = re.fullmatch(r"(?:open|go to|show me|switch to) (?:the |my |our )?(.+?) (?:chat|conversation|dm)", low)
    if m and m.group(1) not in _NOT_A_NAME and not (targets.website_url(f"{m.group(1)} chat")
                                                    or _known_app(f"{m.group(1)} chat")):
        return _web_action("message", re.sub(r"^browser\s+", "", raw, flags=re.I), raw)
    return None


def _web_action(kind, payload, raw):
    """'browser …', 'meet …', 'email …', 'calendar …', 'message …' -> a structured action."""
    text = (payload or "").strip()
    low = text.lower().strip(" .?!")
    if kind == "browser":
        if _SEARCH_CLOSE_RE.fullmatch(low):
            return Action("browser", "search_close", target="search", raw=raw)
        if _WA_NAMED_RE.fullmatch(low) or _WA_READ_RECENT.fullmatch(low) or last_messages_request(low):
            return _web_action("message", payload, raw)      # "browser open X group in whatsapp"
        found = profile_action(low, raw, text)             # Part 5P: Chrome profiles
        if found is not None:
            return found
        found = part_7_action(low, raw, text)              # Part 7: the page agent, recipes
        if found is not None and found.domain == "browser":
            return found
        found = part_6b_action(low, raw, text)             # Part 6b: AI sites, Reddit / LinkedIn / GitHub
        if found is not None:
            return found
        found = _named_chat(low, raw)
        if found is not None:
            return found
        found = part_6_action(low, raw, text)              # Part 6: forms, video, site search, shopping
        if found is not None:
            return found
        query = search_request(low, text)
        if query:
            return Action("browser", "search_here", target=query, raw=raw)
        request = scroll_request(low)
        if request:
            return Action("browser", "page_scroll", target=request["direction"], parameters=request, raw=raw)
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
        # ── Part 4 ──
        found = part_4_action(low, raw, text)
        if found is not None:
            return found
        # ── Part 3 ──
        found = part_3_action(low, raw, text)
        if found is not None:
            return found
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
        if re.search(r"\bjoin\b.*\bnext (?:meeting|call|event)\b", low):
            return Action("calendar", "next", target="next", parameters={"join": True}, raw=raw)
        if re.fullmatch(r"(?:admit|let in|accept)(?: them| everyone| all| the people waiting| people)?(?: in)?|"
                        r"let (?:them|everyone|people) in", low):
            return Action("meet", "admit", target="everyone", raw=raw)
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
        # ── Part 4, E5 ──
        m = re.fullmatch(r"(?:show|open|find|list)?\s*(?:my |the )?unread(?: emails| mails| messages)?"
                         r"(?: (?:from|in|since) (?:this|the past|the last|last) (week|month|day)| (?:from )?today)?", low)
        if m and low not in ("unread",) and not low.startswith("open"):   # "open unread" opens one
            span = {"week": "7d", "month": "30d", "day": "1d"}.get(m.group(1) or "", "1d" if "today" in low else "")
            return Action("email", "search", target="is:unread" + (f" newer_than:{span}" if span else ""), raw=raw)
        m = re.fullmatch(r"(?:search|find|look)(?: in| through)? (?:my |the )?(?:mail|mails|email|emails|inbox|gmail)?\s*"
                         r"(?:for )?(.+)", low)
        if m and re.match(r"(?:search|look)", low) or (m and re.match(r"find (?:the |my )?(?:mail|email)s? (?:about|with|for)", low)):
            query = re.sub(r"^(?:the |my |mails? |emails? )?(?:about |with |for )?", "", m.group(1)).strip()
            found = re.search(re.escape(query), text, re.I)
            return Action("email", "search", target=found.group(0) if found else query, raw=raw)
        if re.fullmatch(r"(?:compose|write|start)(?: a)?(?: new)?(?: blank)?(?: email| mail)?|new (?:email|mail)", low):
            return Action("email", "compose", target="new", raw=raw)
        m = re.fullmatch(r"(?:open|show|go to) (?:my |the )?(.+?) label|(?:open|show) (?:the )?label (.+)", low)
        if m:
            return Action("email", "label", target=(m.group(1) or m.group(2)).strip(), raw=raw)
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
        # ── Part 4, E6 ──
        if re.search(r"\bjoin\b.*\bnext\b|\bnext (?:meeting|call|event)\b.*\bjoin\b", low):
            return Action("calendar", "next", target="next", parameters={"join": True}, raw=raw)
        if re.search(r"\b(?:next|upcoming) (?:meeting|event|call|appointment)\b|what'?s next\b", low):
            return Action("calendar", "next", target="next", parameters={"join": False}, raw=raw)
        day = re.search(r"\b(today|tomorrow|day after tomorrow|monday|tuesday|wednesday|thursday|friday|"
                        r"saturday|sunday)\b", low)
        m = re.match(r"(?:add|create|schedule|book|new)(?: an?)?(?: event| meeting)?\s*(.*)$", low)
        if m:
            return Action("calendar", "add", target=m.group(1) or "event",
                          parameters={"text": text}, raw=raw)
        return Action("calendar", "agenda", target=day.group(1) if day else "today", raw=raw)

    if kind == "message":
        # ── Part 6b, F5: a reply in the open chat, a file to a chat — before every other shape ──
        found = message_6b_action(re.sub(_WA_SUFFIX + r"$", "", low).strip(), raw, text)
        if found is not None:
            return found
        # ── Part 5, H0.4: open / read / who messaged me, before the send shapes ──
        if re.fullmatch(r"(?:who|anyone|anybody) (?:messaged|texted|wrote to|pinged) me(?: on whatsapp)?(?: today)?|"
                        r"(?:any |show |read |list |check )?(?:my )?unread(?: chats| messages| whatsapp messages)?|"
                        r"(?:do i have )?(?:any )?new messages(?: on whatsapp)?", low):
            return Action("message", "unread", target="whatsapp", raw=raw)
        if _WA_CLOSE_CHAT.fullmatch(low):
            return Action("message", "close", target="current", raw=raw)
        if re.fullmatch(r"(?:open|search|show|find|go to|check)(?: (?:it|that|the|a|chat|group|one))*", low):
            return None          # a verb with no name is not a chat called "it", nor a message to "open"
        asked = last_messages_request(low)
        if asked:
            return Action("message", "read", target=asked[0], parameters={"today": False, "count": asked[1]}, raw=raw)
        m = _WA_LAST_HERE.fullmatch(low)
        if m:                                    # "read the last three messages": the open chat
            found = re.search(r"\d{1,2}|couple of|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|few)\b", m.group(1))
            count = 0 if not found else int(found.group(0)) if found.group(0).isdigit() else _COUNT_WORDS[found.group(0)]
            return Action("message", "read", target="recent" if not count else "current",
                          parameters={"today": False, "count": min(count, 30)}, raw=raw)
        m = re.fullmatch(r"(?:open|click(?: on)?|select|go to|show me)(?: search)? result (?:number )?(\d{1,2})", low) \
            or re.fullmatch(r"(?:open|click(?: on)?|select|go to|show me) " + _WA_NTH.pattern + _WA_SUFFIX + r"?", low)
        if m:
            word = m.group(1)
            return Action("message", "open", target=f"#{_ORDINAL_N.get(word) or int(re.sub(r'[^0-9]', '', word) or 1)}", raw=raw)
        m = _WA_SEARCH.fullmatch(low)
        if m and ":" not in text and " saying " not in low:
            return Action("message", "search", target=m.group(1).strip(),
                          parameters={"open": bool(m.group(2))}, raw=raw)
        m = re.fullmatch(r"(?:open|go to|show|switch to|show me|click(?: on)?) (?:the |my |our )?(.+?)"
                         r"(?: chat| group| conversation| dm)?" + _WA_SUFFIX + r"?", low)
        if m and ":" not in text and " saying " not in low:
            name = re.sub(r"\s+(?:chat|group|conversation|dm)$", "", m.group(1)).strip()
            if _WA_RECENT.fullmatch(name):
                name = "recent"                  # "open the latest chat": the newest one
            return Action("message", "open", target=name, raw=raw)
        m = re.fullmatch(r"(?:read|summari[sz]e|catch me up on|what(?:'s| is) new in|what did (?:they|people|everyone) say in)"
                         r" (?:the |my |our )?(.+?)(?: chat| group| conversation)?( today)?" + _WA_SUFFIX + r"?", low)
        if m and ":" not in text:
            name = m.group(1).strip()
            name = "current" if name in ("this", "it", "this chat", "the chat", "messages", "the messages") else name
            if _WA_RECENT.fullmatch(name):
                name = "recent"                  # the open chat, else the one with the newest message
            return Action("message", "read", target=name, parameters={"today": bool(m.group(2))}, raw=raw)
        text = re.sub(r"^(?:tell|text|message|ping|whatsapp|send (?:a )?(?:message|text) to)\s+", "", text, flags=re.I)
        if ":" in text:
            who, _, body = text.partition(":")
        else:
            m = re.match(r"(?:to )?(\S+)\s+(?:saying |that )?(.+)$", text)
            who, body = (m.group(1), m.group(2)) if m else (text, "")
        return Action("message", "send", target=_strip(who, "to"),
                      parameters={"text": body.strip(), "app": "whatsapp"}, raw=raw)
    return None


# ── Part 4 (BROWSER_CONTROL_PLAN E1–E8, Google Workspace) ──
WS_KINDS = {"sheet": "sheet", "sheets": "sheet", "spreadsheet": "sheet", "spreadsheets": "sheet",
            "doc": "doc", "docs": "doc", "document": "doc", "documents": "doc",
            "deck": "slides", "decks": "slides", "presentation": "slides", "presentations": "slides",
            "slides": "slides", "slide deck": "slides", "form": "form", "forms": "form",
            "file": "file", "files": "file", "pdf": "pdf", "pdfs": "pdf", "drive": "file"}
_KIND_WORDS = r"(sheet|spreadsheet|doc|document|deck|presentation|slides|slide deck|form|file|pdf)"
_NOT_A_NAME = {"", "google", "a", "an", "the", "my", "new", "this", "that", "blank", "empty",
               "first", "last", "latest"}
_CALC = {"total": "sum", "sum": "sum", "average": "avg", "mean": "avg", "avg": "avg",
         "maximum": "max", "max": "max", "highest": "max", "biggest": "max", "largest": "max",
         "minimum": "min", "min": "min", "lowest": "min", "smallest": "min", "count": "count"}


def spoken_email(text):
    """'ana at x dot com' / 'ana dot r at gmail dot com' / 'ana@x.com' -> the address, or ''."""
    t = " ".join(str(text or "").lower().replace(" underscore ", "_").replace(" dash ", "-").split())
    t = re.sub(r"\s+(?:at|@)\s+", "@", t)
    t = re.sub(r"\s+(?:dot|\.)\s+", ".", t)
    t = t.replace(" ", "")
    return t if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", t) else ""


def part_4_action(low, raw, text=None):
    """Google Workspace shapes (Sheets, Docs, Slides, Drive, Keep, Forms, Classroom), or None."""
    text = text if text is not None else raw
    low = low.strip(" .?!")
    # E8 form responses first: "open the responses of my feedback form" is not a file called
    # "responses of my feedback".
    m = re.fullmatch(r"(?:open|show) (?:the )?responses (?:of|for|to|from) (?:my |the )?(.+?)(?: form)?|"
                     r"(?:open|show) (?:my |the )?(.+?) form(?:'s)? responses", low)
    if m:
        return Action("browser", "form_responses", target=(m.group(1) or m.group(2)).strip(), raw=raw)
    # G5: "open the sheet Ravi sent last week" — files shared BY someone are searched for their name.
    m = re.fullmatch(r"(?:open|find|show me) (?:me )?(?:the |my |a )?(pdf|sheet|spreadsheet|doc|document|deck|presentation|file|form)"
                     r" (?:that |which )?([a-z][a-z .'-]{0,40}?) (?:sent|shared|gave|emailed)(?: me| with me| to me)?"
                     r"(?: (?:last week|this week|yesterday|today|recently|last month|earlier))?", low)
    if m and m.group(2).strip() not in _NOT_A_NAME and m.group(2).strip() not in ("you", "i", "we"):
        return Action("browser", "ws_search", target=m.group(2).strip(),
                      parameters={"kind": WS_KINDS[m.group(1)], "open": True}, raw=raw)
    # E1/E2/E3/E4 — open a file BY NAME ("open my budget sheet", "open the doc about the internship").
    m = re.fullmatch(r"open (?:up )?(?:my |the |our )?(.+?) " + _KIND_WORDS, low)
    if m and m.group(1).strip() not in _NOT_A_NAME and not re.match(r"(?:new|a new|blank|google)\b", m.group(1)):
        name = re.sub(r"^(?:google )", "", m.group(1)).strip()
        return Action("browser", "ws_open", target=name, parameters={"kind": WS_KINDS[m.group(2)]}, raw=raw)
    m = re.fullmatch(r"open (?:my |the )?" + _KIND_WORDS + r" (?:about|called|named|titled|on|for|with) (.+)", low)
    if m:
        return Action("browser", "ws_open", target=re.sub(r"^(?:the|my|a) ", "", m.group(2).strip()),
                      parameters={"kind": WS_KINDS[m.group(1)]}, raw=raw)
    # search / find files
    m = re.fullmatch(r"(?:search|look) (?:in |through )?(?:my |the )?(sheets|spreadsheets|docs|documents|slides|"
                     r"presentations|decks|forms|drive|files|google drive) (?:for )?(.+)", low)
    if m:
        return Action("browser", "ws_search", target=m.group(2), parameters={"kind": WS_KINDS.get(m.group(1).replace("google ", ""), "file")}, raw=raw)
    m = re.fullmatch(r"find (?:me )?(?:the |my |a )?(pdf|sheet|spreadsheet|doc|document|deck|presentation|file|form)"
                     r"(?: that| which)? (.+?)(?: sent me| shared with me| shared| gave me| emailed me)?", low)
    if m and m.group(1) and m.group(2).strip() not in _NOT_A_NAME and not re.match(r"(?:about|on) this page", m.group(2)):
        query = re.sub(r"^(?:from|by|about|called|named) ", "", m.group(2).strip())
        return Action("browser", "ws_search", target=query, parameters={"kind": WS_KINDS[m.group(1)], "open": True}, raw=raw)
    # create
    m = re.fullmatch(r"(?:create|make|start|open) (?:a |an )?(?:new |blank |empty )(?:google )?"
                     r"(sheet|spreadsheet|doc|document|presentation|deck|slides|slide deck|form)"
                     r"(?: (?:called|named|titled|for) (.+))?", low)
    if m:
        title = ""
        if m.group(2):
            found = re.search(re.escape(m.group(2)), text, re.I)
            title = found.group(0) if found else m.group(2)
        return Action("browser", "ws_create", target=WS_KINDS[m.group(1)], parameters={"title": title}, raw=raw)
    # E1 Sheets — cell, calculations, rows.
    m = re.fullmatch(r"(?:go|jump|move|take me) to (?:cell )?([a-z]{1,3}\s?\d{1,6})|(?:select|open) cell ([a-z]{1,3}\s?\d{1,6})", low)
    if m:
        return Action("browser", "sheet_cell", target=(m.group(1) or m.group(2)).replace(" ", "").upper(), raw=raw)
    m = re.fullmatch(r"(?:what(?:'s| is)|tell me|give me|calculate|find) (?:the )?(total|sum|average|mean|avg|maximum|max|"
                     r"highest|biggest|largest|minimum|min|lowest|smallest|count) (?:value )?(?:of|in|for) "
                     r"(?:the )?(?:column )?(.+?)(?: column)?(?: in this sheet)?", low)
    if m:
        column, params = m.group(2).strip(), {"op": _CALC[m.group(1)]}
        w = re.fullmatch(r"(.+?)(?: column)? (?:where|when|for rows where|if) (?:the )?(.+?) (?:is|=|equals|says) (.+)", column)
        if w:
            column, params["where"] = w.group(1).strip(), [w.group(2).strip(), w.group(3).strip()]
        return Action("browser", "sheet_calc", target=column, parameters=params, raw=raw)
    m = re.fullmatch(r"(total|sum(?: up)?|add up|average|count) (?:the )?(?:values (?:in|of) )?(?:column )?([a-z]{1,3}|.+? column)"
                     r"(?: in this sheet)?", low)
    if m:                                        # G5: "… and total column C" — the imperative shape
        verb = m.group(1).split()[0].replace("add", "total")
        return Action("browser", "sheet_calc", target=re.sub(r" column$", "", m.group(2)).strip(),
                      parameters={"op": _CALC.get(verb, "sum")}, raw=raw)
    if re.fullmatch(r"how many (?:rows|entries|records|lines|people|items)(?: are (?:there|in this sheet)| does this sheet have| in this sheet)?", low):
        return Action("browser", "sheet_calc", target="rows", parameters={"op": "rows"}, raw=raw)
    m = re.fullmatch(r"read (?:me )?(?:out )?row (?:number )?(\d{1,6})", low)
    if m:
        return Action("browser", "sheet_row", target=m.group(1), raw=raw)
    m = re.fullmatch(r"read (?:me )?(?:out )?(?:the )?(?:column|row) (?:headers|headings|names)|what are the columns(?: in this sheet)?", low)
    if m:
        return Action("browser", "sheet_row", target="1", parameters={"headers": True}, raw=raw)
    # E2 Docs — share (prefill only; you press Send).
    m = re.fullmatch(r"share (?:it|this|this (?:doc|document|sheet|file|deck|presentation)) with (.+)", low)
    if m and spoken_email(m.group(1)):
        return Action("browser", "ws_share", target=spoken_email(m.group(1)), raw=raw)
    # E3 Slides.
    if re.fullmatch(r"(?:start|begin) (?:the )?(?:slideshow|slide show)|present (?:this|the|my) (?:deck|slides|presentation)|"
                    r"(?:start|begin) presenting (?:the|this|my) (?:deck|slides|presentation)|play (?:the )?slideshow", low):
        return Action("browser", "slides_present", target="current", raw=raw)
    m = re.fullmatch(r"(next|previous|prev|last) slide|(?:go )?(back|forward) (?:a|one) slide|(end|exit|stop|close) (?:the )?"
                     r"(?:slideshow|slide show|presentation)", low)
    if m:
        verb = "next" if (m.group(1) == "next" or m.group(2) == "forward") else \
               "previous" if (m.group(1) or m.group(2)) else "end"
        return Action("browser", "slides_nav", target=verb, raw=raw)
    # E4 Drive.
    if re.fullmatch(r"(?:open|show) (?:my )?recent (?:files|documents|docs)(?: in drive)?|what (?:files )?did i (?:open|work on) recently", low):
        return Action("browser", "ws_recent", target="recent", raw=raw)
    if re.fullmatch(r"download (?:this|the) (?:file|doc|document|sheet|pdf)", low):
        return Action("browser", "ws_download", target="current", raw=raw)
    # E8 Keep / Forms / Classroom.
    m = re.fullmatch(r"(?:add|take|make|write|create) a (?:new )?(?:keep )?note(?: in keep)?(?: saying|:| that| to)? (.+)|"
                     r"note down (.+)|keep note (.+)", low)
    if m:
        body = m.group(1) or m.group(2) or m.group(3)
        found = re.search(re.escape(body), text, re.I)
        return Action("browser", "keep_note", target="keep", parameters={"text": found.group(0) if found else body}, raw=raw)
    if re.fullmatch(r"what (?:assignments|homework|work) (?:are|is) (?:due|pending)(?: in classroom)?|"
                    r"(?:show|list|read) (?:my )?(?:pending |due )?(?:assignments|classroom work|homework)|what'?s due in classroom", low):
        return Action("browser", "classroom_due", target="due", raw=raw)
    return None


# ── Part 6 (BROWSER_CONTROL_PLAN D1–D5 forms, F1 video, F2/F3 site search and shopping) ──
# Decision D1 (2026-11-11): shopping is search and open ONLY — "add this to cart", "buy it",
# "checkout" become a click the policy REFUSES (policy._SHOPPING_STOP). D2: the form profile
# holds name, email, phone, postal address, college, roll number, date of birth and gender.
_SHOP_STOP_SAID = re.compile(
    r"(?:please )?(?:(add)(?: (?:this|it|that|the product|this product|this item|the item|one))? to (?:the |my )?"
    r"(?:cart|bag|basket|trolley)|(buy)(?: (?:this|it|that|one))?(?: now| for me)?|(order) (?:this|it|that)(?: for me)?|"
    r"(place) (?:the |my |an )?order|(?:proceed to |go to )?(check) ?out|(pay)(?: for (?:it|this))?(?: now)?|"
    r"(?:go to|open|show me|view) (?:the |my )?(cart|bag|basket))")
_SHOP_TARGET = {"add": "add to cart", "buy": "buy now", "order": "place order", "place": "place order",
                "check": "checkout", "pay": "pay now", "cart": "go to cart", "bag": "go to cart",
                "basket": "go to cart"}
_FORM_FILL_RE = re.compile(
    r"(?:auto ?fill|fill(?: in| out| up)?|complete) (?:this |the |that )?(?:form|page|fields?|application)"
    r"(?: (?:with|using|from) (?:my )?(?:details|info|information|data|profile|saved details))?(?: for me)?|"
    r"fill (?:in |out )?my (?:details|info|information|data)(?: (?:in|on|into) (?:this|the) (?:form|page))?|"
    r"auto ?fill(?: (?:it|this|the form))?|put my details (?:in|into) (?:this|the) form")
_SUBMIT_RE = re.compile(r"(?:submit|send) (?:this|the|my) (?:form|application|response|responses|answers)|"
                        r"submit(?: it| this| now| the page)?|(?:press|click|hit)(?: on)? (?:the )?submit(?: button)?")
_FIELD_WORDS = r"(?: drop ?down| dropdown| drop-down| field| menu| list| options?| question| box| select)"
_CHOOSE_RE = re.compile(r"(?:select|choose|pick|set) (.+?) (?:in|from|for|as) (?:the |my )?(.+?)(" + _FIELD_WORDS + r")?")
_KNOWN_FORM_FIELDS = re.compile(r"(?:gender|sex|country|state|city|course|branch|programme|program|year|semester|"
                                r"category|department|degree|batch|section|title|salutation|nationality|religion|"
                                r"blood group|size|colour|color|quantity|language|month|day)")
_CHECK_RE = re.compile(r"(tick|check|untick|uncheck|un tick|un check|deselect|unselect|select|turn on|turn off|switch on|switch off)"
                       r"(?: off)? (?:the |on |that )?(.+?)( (?:check ?box|box|tick ?box|radio button|toggle|switch|option))?")
_DATE_RE = re.compile(r"(?:set|fill|enter|put|change|pick|choose) (?:in )?(?:the |my )?(.*?(?:date of birth|birth ?date|date|dob|"
                      r"birthday|deadline)(?: of [a-z]+)?)(?: field| box)? (?:to|as|with|:) (.+)")
_KEY_RE = re.compile(r"(?:press|hit|tap) (?:the )?(enter|return|tab|shift tab|escape|esc|space(?:bar)?|backspace|delete|"
                     r"(?:up|down|left|right)(?: arrow)?|page up|page down|home|end)(?: key| button)?"
                     r"(?: (\d+) times| (twice|thrice))?")
_UPLOAD_RE = re.compile(r"(upload|attach|add|put|select|choose) (?:my |the |a |an |this )?(.+?)(?: file)?"
                        r"( here| to (?:this|the) (?:page|form|site|email|mail|application|message)| in (?:this|the) (?:form|page)|"
                        r" to it| in it| to the upload(?: box| field)?)?")
_PROFILE_SET_RE = re.compile(
    r"(?:remember|save|store|note|keep)(?: that)? my (.+?) (?:is|as|=|:) (.+)|"
    r"(?:remember|save|store|note) (.+?) as my (.+)|"
    r"(?:set|update|change) my (.+?) (?:to|as) (.+?) (?:for forms|in my (?:form )?profile)|"
    r"my (.+?) is (.+?) (?:for forms|in my (?:form )?profile)")
_PROFILE_SHOW_RE = re.compile(
    r"what (?:details|info|information) (?:do you have|have you got|did i give you|have you saved)(?: for forms| about me| of mine)?|"
    r"what(?:'s| is) in my (?:form )?profile|(show|read|tell me|list) (?:me )?my (?:form )?(?:profile|details for forms|form details|saved details)")
_PROFILE_FORGET_RE = re.compile(
    r"forget my (.+?)(?: from (?:my )?(?:form )?profile| for forms)?|"
    r"(?:delete|remove|clear|erase) my (.+?) (?:from (?:my )?(?:form )?profile|for forms)|"
    r"(?:delete|remove|clear|erase|forget) (?:my |the )?(?:form profile|form details|details for forms|saved form details)")
_SECS = r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?)"
_SEEK_FWD = re.compile(r"(?:skip|jump|go|fast ?forward|forward|move|seek)(?: ahead| forward)?(?: by)? " + _SECS +
                       r"(?: ahead| forward)?(?: in (?:the|this) video)?")
_SEEK_BACK = re.compile(r"(?:go back|rewind|skip back|jump back|move back|seek back|back)(?: by)? " + _SECS +
                        r"(?: back)?(?: in (?:the|this) video)?|(?:go|skip|jump|move) " + _SECS + r" back(?:wards?)?")
_SEEK_TO = re.compile(r"(?:go|jump|skip|seek|move) to (\d{1,2}):(\d{2})(?::(\d{2}))?(?: in (?:the|this) video)?|"
                      r"(?:go|jump|skip|seek) to (\d+) minutes?(?: and (\d+) seconds?)?(?: in (?:the|this) video)?|"
                      r"(?:restart|replay) (?:the |this )?video|start (?:the |this )?video (?:over|again)|"
                      r"go (?:back )?to the (?:start|beginning) of (?:the|this) video")
_SPEED_RE = re.compile(r"(?:set |change )?(?:the )?(?:playback |video )?speed (?:to )?(\d+(?:\.\d+)?|half|normal)(?: ?x| times)?|"
                       r"play (?:it |this |the video )?(?:at )?(\d+(?:\.\d+)?) ?(?:x|times)(?: speed)?|(\d+(?:\.\d+)?) ?x speed|"
                       r"(?:play (?:it |the video )?at )?(normal|regular|default) speed|"
                       r"(?:speed up|play faster|faster) (?:the |this )?video|(?:the )?video faster|"
                       r"slow (?:the |this )?video down|slow down (?:the |this )?video|play (?:it |the video )?slower")
_CAPTIONS_RE = re.compile(r"(?:turn|switch) (on|off) (?:the )?(?:captions|subtitles|cc|closed captions)|"
                          r"(?:turn|switch) (?:the )?(?:captions|subtitles|cc) (on|off)|(?:captions|subtitles) (on|off)|"
                          r"(show|hide|enable|disable) (?:the )?(?:captions|subtitles)")
_VIDEO_SIMPLE = (
    (r"skip (?:the |this )?(?:ads?|advertisement|commercial)s?(?: now)?", "skipAd", None),
    (r"(?:play )?(?:the )?next video|skip (?:this|the) video|play the next one", "next", None),
    (r"like (?:this|the) video|(?:give (?:it|this video) a )(?:like|thumbs up)|thumbs up (?:this|the) video", "like", True),
    (r"(?:unlike|remove (?:my |the )?like (?:from|on)) (?:this|the) video|unlike (?:it|this)", "like", False),
    (r"(?:save|add|put) (?:this |it |the video |this video )?(?:to|in|on) (?:my )?watch later(?: list)?|watch (?:this|it) later", "watchLater", None),
    (r"(?:make|put|turn) (?:the |this )?video (?:full ?screen|in full ?screen)|full ?screen (?:the |this )?video|"
     r"video (?:in )?full ?screen|(?:watch|play) (?:it |this |the video )?in full ?screen", "fullscreen", None),
    (r"(?:turn on |switch to |use |open )?(?:the )?(?:theat(?:er|re)|cinema) mode", "theater", None),
    (r"(?:open |use |switch to )?(?:the )?mini ?player", "miniplayer", None),
    (r"(?:pause|stop) (?:the |this )?video", "pause", None),
    (r"(?:play|resume|continue|unpause) (?:the |this )?video", "play", None),
    (r"mute (?:the |this )?video", "mute", True),
    (r"unmute (?:the |this )?video", "mute", False),
    (r"how (?:long is|much longer is) (?:this|the) video|how much (?:time )?is left(?: in (?:the|this) video)?|"
     r"where am i in (?:the|this) video|what video is this|what(?:'s| is) (?:this|the) video called", "state", None),
)
_IMAGES_RE = re.compile(r"(?:show me |find |search for |search |get me |look up )?(?:images|pictures|pics|photos) of (?!my\b)(.+)")
_SITE_SEARCH_RE = re.compile(r"search (?:on )?(.+?) for (.+)|(?:search|look up|look for|find|show me) (?:for )?(.+?) (?:on|in) "
                             r"(?!this\b|the page\b|the site\b|this page\b|this site\b)(.+?)(?: website| site| dot com)?|"
                             r"(.+?) search (?:for )?(.+)")
_NOT_SEARCH_SITES = frozenset({"drive", "docs", "sheets", "slides", "forms", "gmail", "whatsapp web", "calendar",
                               "keep", "classroom", "photos", "contacts"})
_SORT_WORDS = ((r"low(?:est)? to high(?:est)?|cheapest|lowest price|price ascending|least expensive|low price", "price_asc"),
               (r"high(?:est)? to low(?:est)?|most expensive|highest price|price descending|costliest", "price_desc"),
               (r"(?:best|top|highest|customer) rat(?:ed|ing)s?|ratings?|reviews?|best reviewed", "rating"),
               (r"newest|latest|new arrivals|most recent|recent", "newest"),
               (r"popular(?:ity)?|best ?sell(?:ing|ers?)|relevance", "popular"))
_SHOP_REFINE_RE = re.compile(
    r"sort (?:it |them |the results |this |the products |by )?(?:by )?(.+)|"
    r"(?:show|list)(?: me)? (?:the )?(cheapest|most expensive|best rated|top rated|newest|latest)(?: ones?| products?| items?| results?)?(?: first)?|"
    r"(?:only )?(?:show|filter|keep)(?: me)?(?: only)?(?: (?:the )?(?:results|ones|products|items))?(?: that are| which are)?(?: priced)? "
    r"((?:under|below|less than|over|above|more than|between|from) .+)")
_PRODUCT_RE = re.compile(
    r"how much (?:is|does) (?:this|it|that)(?: one)?(?: cost| product| item)?|"
    r"what(?:'s| is) (?:the |its )?(?:price|cost|rating|star rating)(?: of (?:this|it|the) ?(?:product|item)?)?(?: here)?|"
    r"(?:read|tell me|give me) (?:the |about )?(?:this |the )?product(?: details| info)?|"
    r"(?:what|how) (?:are|is) (?:the |its )?(?:ratings?|reviews? score|stars?)(?: of this(?: product)?)?|"
    r"is (?:this|it) (?:in stock|available)|what(?:'s| is) this product")
_SHOP_HOSTS = re.compile(r"(?:^|\.)(?:amazon\.[a-z.]+|flipkart\.com|myntra\.com|meesho\.com|ajio\.com|nykaa\.com|"
                         r"croma\.com|reliancedigital\.in|snapdeal\.com|tatacliq\.com|ebay\.[a-z.]+|bestbuy\.com|"
                         r"walmart\.com|etsy\.com|decathlon\.in)$")
_VIDEO_HOSTS = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be|vimeo\.com|twitch\.tv|netflix\.com|primevideo\.com|"
                          r"hotstar\.com|jiocinema\.com|dailymotion\.com|coursera\.org|udemy\.com|nptel\.ac\.in)$")
_TIMES = {"twice": 2, "thrice": 3}


def _num(text):
    """'one point five' -> '1.5', 'ten' -> '10' (the speech recogniser writes both)."""
    from kayra.automation.web_macros import spoken_numbers
    out = spoken_numbers(text)
    out = re.sub(r"(\d) (?:point|dot) (\d+)", r"\1.\2", out)
    return re.sub(r"\b(\d+) thousand\b", lambda m: str(int(m.group(1)) * 1000), out)


def _seconds(amount, unit):
    value = float(amount)
    return int(value * 60) if unit.startswith("m") else int(value)


def price_filter(query):
    """('earbuds', lo, hi) from 'earbuds under 2000' / 'between 500 and 1000' / 'over 2k rupees'."""
    q = _num(query.lower())
    money = r"(?:rs\.? ?|₹ ?|inr )?(\d[\d,]*)(k)?(?: ?rupees| ?rs\.?| ?inr| ?bucks)?"
    amount = lambda m, i: int(m.group(i).replace(",", "")) * (1000 if m.group(i + 1) else 1)
    lo = hi = 0
    m = re.search(r"\s*\b(?:between|from) " + money + r" (?:and|to|-) " + money + r"\b", q)
    if m:
        lo, hi = amount(m, 1), amount(m, 3)
    else:
        m = re.search(r"\s*\b(?:under|below|less than|cheaper than|within|up ?to|max(?:imum)?|not more than) " + money, q)
        if m:
            hi = amount(m, 1)
        else:
            m = re.search(r"\s*\b(?:over|above|more than|at least|min(?:imum)?|starting (?:at|from)) " + money, q)
            if m:
                lo = amount(m, 1)
    rest = (q[:m.start()] + q[m.end():]).strip() if m else query
    return (rest if m else query), lo, hi


def sort_request(text):
    """'price low to high' -> 'price_asc', or ''."""
    low = text.lower()
    for words, key in _SORT_WORDS:
        if re.search(r"\b(?:" + words + r")\b", low):
            return key
    return ""


def _site_search_action(low, raw, text):
    m = _IMAGES_RE.fullmatch(low)
    if m:
        found = re.search(re.escape(m.group(1)), text, re.I)
        return Action("browser", "site_search", target=found.group(0) if found else m.group(1),
                      parameters={"site": "images"}, raw=raw)
    m = _SITE_SEARCH_RE.fullmatch(low)
    tail = re.search(r",? (?:and )?(?:(?:sorted|sort|ordered) by .+|(?:cheapest|lowest price|best rated|newest) first|"
                     r"(?:under|below|less than|between|over|above) .+)$", low)
    if tail and not (m and targets.canonical_website((m.group(1) or m.group(4) or m.group(5) or "").strip())):
        # "search for X on flipkart sorted by price": the clause after the site belongs to the query.
        head = _SITE_SEARCH_RE.fullmatch(low[:tail.start()])
        if head and (head.group(3) or head.group(1)):
            low = (f"search {head.group(1)} for {head.group(2)}" if head.group(1)
                   else f"search {head.group(4)} for {head.group(3)}") + low[tail.start():]
            text = low
            m = _SITE_SEARCH_RE.fullmatch(low)
    if not m:
        return None
    name, query = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3)) if m.group(3) else (m.group(5), m.group(6))
    site = targets.canonical_website(name.strip())
    if not site or site in _NOT_SEARCH_SITES or site == "google" or (m.group(5) and site == "youtube"):
        return None                    # "google search X" / "youtube search X" stay the tokens they were
    from kayra.automation import sites
    if site not in ("amazon", "flipkart") and not sites.search_url(site, "x"):
        return None
    found = re.search(re.escape(query.strip()), text, re.I)
    query = found.group(0) if found else query.strip()
    params = {"site": site}
    query, where = _split_where(query)
    if where:
        params["where"] = where
    sorted_by = re.search(r",?(?: and)? (?:sorted|sort|ordered|order) by (.+)$|,?(?: and)? (cheapest|lowest price|best rated|newest) first$", query, re.I)
    if site in ("amazon", "flipkart"):
        if sorted_by:
            params["sort"] = sort_request(sorted_by.group(1) or sorted_by.group(2))
            query = query[:sorted_by.start()].strip()
        lead = re.match(r"(?:the )?(cheapest|best rated|top rated|newest|latest) ", query, re.I)
        if lead:
            params["sort"] = sort_request(lead.group(1))
            query = query[lead.end():]
        query, lo, hi = price_filter(query)
        if lo:
            params["lo"] = lo
        if hi:
            params["hi"] = hi
    if not query.strip():
        return None
    return Action("browser", "site_search", target=query.strip(), parameters=params, raw=raw)


def _video_action(low, raw):
    num = _num(low)
    for pattern, op, value in _VIDEO_SIMPLE:
        if re.fullmatch(pattern, low):
            params = {"op": op}
            if value is not None:
                params["value"] = value
            key = "video_subscribe" if op == "subscribe" else "video"
            return Action("browser", key, target=op, parameters=params, raw=raw)
    if re.fullmatch(r"subscribe(?: to)?(?: (?:this|the|that) channel| them| him| her| this creator| to (?:this|the) channel)?", low):
        return Action("browser", "video_subscribe", target="subscribe", parameters={"op": "subscribe"}, raw=raw)
    m = _SEEK_FWD.fullmatch(num)
    if m:
        return Action("browser", "video", target="seek", parameters={"op": "seek", "value": _seconds(m.group(1), m.group(2))}, raw=raw)
    m = _SEEK_BACK.fullmatch(num)
    if m:
        amount, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return Action("browser", "video", target="seek", parameters={"op": "seek", "value": -_seconds(amount, unit)}, raw=raw)
    if re.fullmatch(r"rewind(?: (?:a bit|a little|the video))?|go back a (?:bit|little) in (?:the|this) video", low):
        return Action("browser", "video", target="seek", parameters={"op": "seek", "value": -10}, raw=raw)
    if re.fullmatch(r"fast ?forward(?: (?:a bit|a little|the video))?", low):
        return Action("browser", "video", target="seek", parameters={"op": "seek", "value": 10}, raw=raw)
    m = _SEEK_TO.fullmatch(num)
    if m:
        if m.group(1):
            secs = int(m.group(1)) * 60 + int(m.group(2)) if not m.group(3) else \
                int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        elif m.group(4):
            secs = int(m.group(4)) * 60 + int(m.group(5) or 0)
        else:
            secs = 0
        return Action("browser", "video", target="seekTo", parameters={"op": "seekTo", "value": secs}, raw=raw)
    m = _SPEED_RE.fullmatch(num)
    if m:
        said = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if said:
            value = {"half": 0.5, "normal": 1.0, "regular": 1.0, "default": 1.0}.get(said)
            value = value if value is not None else float(said)
        else:
            value = "+0.25" if re.search(r"faster|speed up", low) else "-0.25"
        return Action("browser", "video", target="rate", parameters={"op": "rate", "value": value}, raw=raw)
    m = _CAPTIONS_RE.fullmatch(low)
    if m:
        word = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        return Action("browser", "video", target="captions",
                      parameters={"op": "captions", "value": word in ("on", "show", "enable")}, raw=raw)
    return None


def part_6_action(low, raw, text=None):
    """Forms, the video, site search and shopping (Part 6), or None."""
    text = text if text is not None else raw
    low = " ".join(low.strip(" .?!").split())
    # D1 — shopping stops before the cart: the click is built so the policy can refuse it.
    m = _SHOP_STOP_SAID.fullmatch(low)
    if m:
        word = next(g for g in m.groups() if g)
        return Action("browser", "page_click", target=_SHOP_TARGET[word], parameters={"nth": 1}, raw=raw)
    # D3 the profile, D3 fill, D4 submit
    m = _PROFILE_SET_RE.fullmatch(low)
    if m:
        from kayra.automation import form_profile as P
        groups = m.groups()
        words, value = next((groups[i], groups[i + 1]) for i in (0, 2, 4, 6) if groups[i])
        if groups[2]:
            words, value = groups[3], groups[2]          # "save X as my email"
        key = P.key_for(words)
        if key:
            found = re.search(re.escape(value), text, re.I)
            return Action("browser", "profile_set", target=key,
                          parameters={"text": found.group(0) if found else value}, raw=raw)
    m = _PROFILE_SHOW_RE.fullmatch(low)
    if m:
        return Action("browser", "profile_show", target="profile",
                      parameters={"read": bool(m.group(1)) or "what" in low and "in my" in low}, raw=raw)
    m = _PROFILE_FORGET_RE.fullmatch(low)
    if m:
        from kayra.automation import form_profile as P
        words = m.group(1) or m.group(2)
        if not words or re.fullmatch(r"(?:form )?(?:profile|details|form details|details for forms|saved details|everything)", words):
            return Action("browser", "profile_clear", target="all", raw=raw)
        key = P.key_for(words)
        if key:
            return Action("browser", "profile_forget", target=key, raw=raw)
    if _FORM_FILL_RE.fullmatch(low):
        return Action("browser", "form_fill", target="current", raw=raw)
    if _SUBMIT_RE.fullmatch(low):
        return Action("browser", "form_submit", target="current", raw=raw)
    # D1 dates, ticks and drop-downs
    m = _DATE_RE.fullmatch(low)
    if m:
        from kayra.automation import form_profile as P
        if P.parse_date(_num(m.group(2))):
            return Action("browser", "form_choose", target=P.parse_date(_num(m.group(2))),
                          parameters={"field": m.group(1), "kind": "date", "option": P.parse_date(_num(m.group(2)))}, raw=raw)
    m = _CHECK_RE.fullmatch(low)
    if m:
        verb, label, box = m.group(1), m.group(2), m.group(3)
        if verb in ("tick", "untick", "un tick", "uncheck", "un check", "deselect", "unselect") or box:
            if not (verb in ("select", "turn on", "turn off", "switch on", "switch off", "check") and not box):
                state = verb in ("tick", "check", "select", "turn on", "switch on")
                return Action("browser", "form_choose", target=label.strip(),
                              parameters={"field": label.strip(), "kind": "check", "state": state}, raw=raw)
    m = _CHOOSE_RE.fullmatch(low)
    if m and not re.match(r"(?:all|everything|the (?:first|second|third|last) (?:result|one|tab))$", m.group(1)):
        found = re.search(re.escape(m.group(1)), text, re.I)
        return Action("browser", "form_choose", target=found.group(0) if found else m.group(1),
                      parameters={"field": m.group(2).strip(), "kind": "select",
                                  "said_field": bool(m.group(3)) or bool(_KNOWN_FORM_FIELDS.fullmatch(m.group(2).strip()))},
                      raw=raw)
    # D5 upload
    m = _UPLOAD_RE.fullmatch(low)
    if m and (m.group(1) in ("upload", "attach") or m.group(3)) and m.group(2) not in ("it", "this", "that", "a file", "file"):
        what = re.sub(r"^(?:my|the|a|an|this)\s+", "", m.group(2)).strip()
        if what and not re.match(r"(?:note|reminder|tab|bookmark|row|label|event)\b", what):
            return Action("browser", "file_upload", target=what, raw=raw)
    # D2 real keys into the page
    m = _KEY_RE.fullmatch(_num(low))
    if m:
        count = int(m.group(2)) if m.group(2) else _TIMES.get(m.group(3) or "", 1)
        return Action("browser", "page_key", target=m.group(1), parameters={"count": count}, raw=raw)
    # F1 the video
    found = _video_action(low, raw)
    if found is not None:
        return found
    # F2/F3 site search, sort and filter results, the product
    found = _site_search_action(low, raw, text)
    if found is not None:
        return found
    m = _SHOP_REFINE_RE.fullmatch(_num(low))
    if m:
        said = m.group(1) or m.group(2) or ""
        sort = sort_request(said) if said else ""
        _, lo, hi = price_filter(m.group(3) or said)
        if sort or lo or hi:
            return Action("browser", "shop_refine", target=sort or "price",
                          parameters={"sort": sort, "lo": lo, "hi": hi}, raw=raw)
    if _PRODUCT_RE.fullmatch(low):
        return Action("browser", "product_info", target="current", raw=raw)
    return None


def part_6_direct(low, text, situation_fn=None):
    """['browser …'] when a Part 6 utterance is a complete command whatever is on screen (or is
    one given what is in front); None when the classifier should decide."""
    action = part_6_action(low, f"browser {text}", text)
    if action is None:
        return None
    verb = action.action
    host = front_site(situation_fn) if verb in ("video", "shop_refine", "product_info", "form_choose") else ""
    if verb == "page_click":
        return [f"browser {text}"]                         # shopping stop: refused, rules-first
    if verb in ("form_fill", "form_submit", "profile_set", "profile_show", "profile_forget", "profile_clear",
                "file_upload", "site_search", "video_subscribe"):
        if verb == "form_submit" and not re.search(r"\bform|application|response|answers|submit button\b", low):
            return None                                    # a bare "submit it": the model decides
        if verb == "file_upload" and not re.match(r"(?:upload|attach)\b", low):
            return None
        return [f"browser {text}"]
    if verb == "video":
        explicit = re.search(r"\b(?:video|ads?|advertisement|captions|subtitles|watch later|theat(?:er|re) mode|"
                             r"mini ?player|speed|\d(?:\.\d+)? ?x)\b", low)
        return [f"browser {text}"] if explicit or _VIDEO_HOSTS.search(host or "") else None
    if verb == "form_choose":
        params = action.parameters
        if params.get("kind") in ("check", "date") or params.get("said_field"):
            return [f"browser {text}"]
        return None
    if verb in ("shop_refine", "product_info"):
        return [f"browser {text}"] if _SHOP_HOSTS.search(host or "") else None
    return None


def _bare_part_6b(lowered, raw):
    """A 6b shape the DMM sent without a prefix: an AI site, Reddit / LinkedIn / GitHub, or a
    WhatsApp reply / file that names WhatsApp."""
    tokens = part_6b_direct(lowered, raw.strip(), None)
    if not tokens:
        return None
    return normalize_command(tokens[0])


def _bare_part_6(lowered, raw):
    """A Part 6 shape the DMM sent without the `browser` prefix, when it can only be that."""
    text = raw.strip()
    tokens = part_6_direct(lowered, text, None)
    if tokens:
        return part_6_action(lowered, raw, text)
    return None


# ── Part 6b: ask an AI site (F4), WhatsApp replies and files (F5), Reddit / LinkedIn / GitHub (F6) ──
_BOT = r"(chat ?gpt|chat g p t|gpt|open ?ai|claude(?: ?\.? ?ai)?|(?:google )?gemini|bard)"
_AI_BOT_KEY = (("claude", "claude"), ("gemini", "gemini"), ("bard", "gemini"), ("gpt", "chatgpt"),
               ("g p t", "chatgpt"), ("open", "chatgpt"))
_AI_ASK_RE = re.compile(r"(?:please |can you |could you |go and |go )?(?:ask|check with|consult|query) " + _BOT +
                        r"( (?:in|with|on) a (?:new|fresh) chat)?(?: and ask(?: it)?)?[,:]? ?(.*)")
_AI_NEW_RE = re.compile(r"(?:open|start|begin|make) (?:a )?(?:new|fresh) (?:chat|conversation) (?:in|on|with) " + _BOT +
                        r"(?: and ask(?: it)?[,:]? (.+))?|(?:open|start) (?:a )?(?:new|fresh) " + _BOT + r" (?:chat|conversation)")
_AI_SAYS_RE = re.compile(r"what (?:does|would|will) " + _BOT + r" (say|think) (?:about|of|on) (.+)")
_AI_READ_RE = re.compile(r"(?:read|tell me|what(?:'s| is))(?: me| out)? (?:the |" + _BOT +
                         r"(?:'s| s)? )(whole |full |entire |complete |last |latest )?(?:answer|reply|response)(?: again)?"
                         r"|what did " + _BOT + r" (?:say|answer|reply)")


def _ai_bot(word):
    word = (word or "").lower()
    return next((key for needle, key in _AI_BOT_KEY if needle in word), "chatgpt")


def _ai_question(low, text, start):
    """The question as the user said it (their capitals), from `start` in the lowered sentence."""
    said = low[start:].strip(" ,:")
    found = re.search(re.escape(said), text, re.I) if said else None
    q = found.group(0) if found else said
    q = re.sub(r"^(?:to|whether)\s+", "", q, flags=re.I)
    if re.match(r"about\s+", q, re.I):
        q = "Tell me " + q
    return q.strip()


_FILE_SAID = (r"(?:file|document|doc|pdf|photo|picture|pic|image|screenshot|screen shot|resume|cv|video|report|"
              r"presentation|ppt|slides|notes|certificate|marksheet|mark sheet|invoice|receipt|ticket|letter|"
              r"assignment|spreadsheet|zip|[\w-]+\.(?:pdf|docx?|pptx?|xlsx?|png|jpe?g|zip|txt|csv|mp4))")
_SEND_FILE_RE = re.compile(r"(?:send|share|forward) (?:him |her |them )?((?:my|the|this|that|a|an|our|last|latest) (?:[\w.' -]+ )?"
                           + _FILE_SAID + r")(?: file)? (?:to|with) (.+?)" + _WA_SUFFIX + r"?")
_SEND_FILE_HERE_RE = re.compile(r"(?:send|share|attach|forward) ((?:my|the|this|that|a|an|our|last|latest) (?:[\w.' -]+ )?"
                                + _FILE_SAID + r")(?: file)? (?:here|in this chat|to this chat|in (?:this|the) (?:group|conversation))")
_REPLY_RE = re.compile(r"(?:reply|respond|answer|write back|text back)(?: to (?:this chat|this message|this group|this|them|"
                       r"him|her|the chat|it|the last message|the group))?(?: (?:saying|with|that))?[,:]? (.+)|"
                       r"(?:send|type and send|write) (?:a message )?(?:here|in this chat)(?: (?:saying|that))?[,:]? (.+)")
_REPLY_TO_RE = re.compile(r"(?:reply|respond|write back) to (.+?) (?:saying|with|that|:) ?(.+)")

_SOCIAL_SITE = r"(linked ?in|git ?hub|reddit)"
_SOCIAL_WHAT = r"(notifications?|messages?|inbox|dms?|invitations?|connection requests?|feed|posts?)"
_SOCIAL_READ_RE = re.compile(r"(?:read|check|show|tell me|what(?:'s| is| are)|any|do i have any|list)(?: me)?(?: in)? (?:my |the )?"
                             r"(?:new |latest |unread |recent )?" + _SOCIAL_SITE + r" " + _SOCIAL_WHAT +
                             r"|(?:read|check|show|what are|any)(?: me)? (?:my |the )?(?:new |latest |unread )?" + _SOCIAL_WHAT +
                             r" (?:on|in|from) " + _SOCIAL_SITE)
_SOCIAL_OPEN_RE = re.compile(r"(?:open|go to|show me) (?:my |the )?" + _SOCIAL_SITE + r" " + _SOCIAL_WHAT)
_SUBREDDIT = r"(?:r ?/ ?|r slash |the )?([a-z0-9_]{2,21})(?: subreddit)?"
_SUB_OPEN_RE = re.compile(r"(?:open|go to|show me|take me to) (?:r ?/ ?|r slash )([a-z0-9_]{2,21})|"
                          r"(?:open|go to|show me|take me to) (?:the )?([a-z0-9_]{2,21}) subreddit")
_TOP_RE = re.compile(r"(?:what(?:'s| is| are)|show me|read|tell me|read me) (?:the )?(top|best|hottest|most popular|trending|latest) "
                     r"(post|posts)(?: (?:on|in|of|from) (r ?/ ?[a-z0-9_]{2,21}|r slash [a-z0-9_]{2,21}|(?:the )?[a-z0-9_]{2,21} subreddit|reddit))?"
                     r"(?: (today|this week|this month|this year|of all time|all time|right now|this hour))?|"
                     r"what(?:'s| is) trending on reddit(?: (today|this week))?")
_REPO_RE = re.compile(r"open (?:the )?(?:github )?(?:repo|repository) ([\w.-]+(?: ?/ ?[\w.-]+)?)(?: on github)?|"
                      r"open ([\w.-]+ ?/ ?[\w.-]+) on github|open (?:the )?([\w.-]+) (?:repo|repository) on github")
_SOCIAL_KEY = {"notification": "notifications", "notifications": "notifications", "message": "messages",
               "messages": "messages", "inbox": "messages", "dm": "messages", "dms": "messages",
               "invitation": "invitations", "invitations": "invitations", "connection request": "invitations",
               "connection requests": "invitations", "feed": "feed", "post": "posts", "posts": "posts"}


def _site_key6(word):
    return {"linkedin": "linkedin", "linked in": "linkedin", "github": "github", "git hub": "github",
            "reddit": "reddit"}.get(word, word)


def part_6b_action(low, raw, text=None):
    """Ask an AI site, Reddit / LinkedIn / GitHub (browser kind), or None. F5 lives in the message kind."""
    text = text if text is not None else raw
    low = " ".join(low.strip(" .?!").split())
    m = _AI_NEW_RE.fullmatch(low)
    if m:
        bot = _ai_bot(m.group(1) or m.group(3))
        question = _ai_question(low, text, m.start(2)) if m.group(2) else ""
        return Action("browser", "ai_ask", target=question or "new chat",
                      parameters={"bot": bot, "question": question, "new": True}, raw=raw)
    m = _AI_ASK_RE.fullmatch(low)
    if m and (m.group(3) or "").strip(" ,:") and not re.fullmatch(r"(?:it|that|this|him|her|them)", m.group(3).strip()):
        return Action("browser", "ai_ask", target=_ai_question(low, text, m.start(3)),
                      parameters={"bot": _ai_bot(m.group(1)), "question": _ai_question(low, text, m.start(3)),
                                  "new": bool(m.group(2))}, raw=raw)
    m = _AI_SAYS_RE.fullmatch(low)
    if m:
        topic = _ai_question(low, text, m.start(3))
        question = (f"What do you think of {topic}?" if m.group(2) == "think" else f"Tell me about {topic}")
        return Action("browser", "ai_ask", target=question,
                      parameters={"bot": _ai_bot(m.group(1)), "question": question, "new": False}, raw=raw)
    m = _AI_READ_RE.fullmatch(low)
    if m:
        bot_word = m.group(1) or m.group(3)
        return Action("browser", "ai_read", target=_ai_bot(bot_word) if bot_word else "current",
                      parameters={"full": bool(m.group(2) and m.group(2).strip() in ("whole", "full", "entire", "complete"))},
                      raw=raw)
    # F6
    m = _SUB_OPEN_RE.fullmatch(low)
    if m:
        return Action("browser", "social", target="subreddit",
                      parameters={"site": "reddit", "name": m.group(1) or m.group(2), "read": False}, raw=raw)
    m = _TOP_RE.fullmatch(low)
    if m:
        if m.group(5) is not None or "trending on reddit" in low:
            return Action("browser", "social", target="top",
                          parameters={"site": "reddit", "name": "", "period": m.group(5) or "today", "read": True}, raw=raw)
        where = m.group(3) or ""
        if where:
            name = "" if where == "reddit" else re.sub(r"^(?:r ?/ ?|r slash |the )|\s*subreddit$", "", where).strip()
            op = "top" if m.group(2) == "post" or m.group(1) in ("top", "best", "hottest", "most popular") else "posts"
            if m.group(2) == "posts" and not m.group(4):
                op = "posts" if name else "top"
            period = (m.group(4) or "today").replace("right now", "today")
            return Action("browser", "social", target=op,
                          parameters={"site": "reddit", "name": name, "period": period, "read": True}, raw=raw)
        return Action("browser", "social", target="posts",
                      parameters={"site": "", "name": "", "period": "", "read": True}, raw=raw)   # the page in front
    m = _SOCIAL_READ_RE.fullmatch(low)
    if m:
        site = _site_key6(m.group(1) or m.group(4))
        what = _SOCIAL_KEY.get(m.group(2) or m.group(3), "notifications")
        what = "top" if what == "posts" and site == "reddit" else what
        return Action("browser", "social", target=what, parameters={"site": site, "name": "", "period": "", "read": True},
                      raw=raw)
    m = _SOCIAL_OPEN_RE.fullmatch(low)
    if m:
        site, what = _site_key6(m.group(1)), _SOCIAL_KEY.get(m.group(2), "notifications")
        return Action("browser", "social", target=what, parameters={"site": site, "name": "", "period": "", "read": False},
                      raw=raw)
    m = _REPO_RE.fullmatch(low)
    if m:
        name = (m.group(1) or m.group(2) or m.group(3)).replace(" ", "")
        found = re.search(re.escape(name), text.replace(" / ", "/").replace(" /", "/").replace("/ ", "/"), re.I)
        return Action("browser", "social", target="repo",
                      parameters={"site": "github", "name": found.group(0) if found else name, "read": False}, raw=raw)
    m = re.fullmatch(r"(?:search|find|look for) (?:linked ?in )?jobs (?:for|as|of) (.+?) on linked ?in|"
                     r"search linked ?in(?: for)? jobs? (?:for|as) (.+)|find (.+?) jobs on linked ?in", low)
    if m:
        query = (m.group(1) or m.group(2) or m.group(3)).strip()
        found = re.search(re.escape(query), text, re.I)
        return Action("browser", "site_search", target=found.group(0) if found else query,
                      parameters={"site": "linkedin jobs"}, raw=raw)
    return None


def message_6b_action(low, raw, text):
    """F5 inside the message kind: a reply in the open chat, or a file to a chat. None otherwise."""
    m = _SEND_FILE_HERE_RE.fullmatch(low)
    if m:
        return Action("message", "send_file", target="current", parameters={"what": _what(m.group(1))}, raw=raw)
    m = _SEND_FILE_RE.fullmatch(low)
    if m and m.group(2).strip() not in ("me", "myself"):
        who = m.group(2).strip()
        found = re.search(re.escape(who), text, re.I)
        return Action("message", "send_file", target=found.group(0) if found else who,
                      parameters={"what": _what(m.group(1))}, raw=raw)
    m = _REPLY_TO_RE.fullmatch(low)
    if m and m.group(1) not in ("this", "this chat", "them", "him", "her", "the chat", "it", "this message",
                                "the last message", "the group", "this group"):
        body = re.search(re.escape(m.group(2)), text, re.I)
        who = re.search(re.escape(m.group(1).strip()), text, re.I)
        return Action("message", "send", target=who.group(0) if who else m.group(1).strip(),
                      parameters={"text": body.group(0) if body else m.group(2), "app": "whatsapp"}, raw=raw)
    m = _REPLY_RE.fullmatch(low)
    if m:
        said = (m.group(1) or m.group(2)).strip()
        body = re.search(re.escape(said), text, re.I)
        return Action("message", "send", target="current",
                      parameters={"text": body.group(0) if body else said, "app": "whatsapp"}, raw=raw)
    return None


def _what(said):
    return re.sub(r"^(?:my|the|this|that|a|an|our)\s+", "", said.strip()).strip()


def part_6b_direct(low, text, situation_fn=None):
    """['browser …' | 'message …'] for a 6b utterance that is complete as said, else None."""
    action = part_6b_action(low, f"browser {text}", text)
    if action is not None:
        if action.action == "ai_read" and action.target == "current":
            host = front_site(situation_fn)
            return [f"browser {text}"] if host in ("chatgpt.com", "chat.openai.com", "claude.ai", "gemini.google.com") \
                or "whole" in low or "full" in low or "entire" in low else None
        if action.action == "social" and action.target == "posts" and not action.parameters.get("site"):
            return [f"browser {text}"] if "reddit.com" in front_site(situation_fn) else None
        return [f"browser {text}"]
    wa_said = re.search(r"\bwhats ?app\b", low)
    wa_front = front_site(situation_fn) == "web.whatsapp.com" if not wa_said else True
    if not wa_front:
        return None
    bare = re.sub(_WA_SUFFIX + r"$", "", low).strip()
    found = message_6b_action(bare, f"message {text}", text)
    if found is None:
        return None
    if found.action == "send" and found.target != "current" and not wa_said:
        return None                     # "reply to Ravi saying …" with no app named: the model decides
    return [f"message {text}"]


# ── Part 5P: Chrome profiles (BROWSER_CONTROL_PLAN §14) ──
# The SHAPES only. Which profile a name means is decided at run time from Chrome's own
# `Local State` (`automation.chrome_profiles`) — the parser never reads it, so no suite and no
# parse touches the host's profiles.
_PB = r"(google chrome|chrome|chromium|brave|microsoft edge|edge)"
_PROFILE_NOT = re.compile(r"^(?:google|my|our|new|a new|the|incognito|private|a private|guest|full ?screen|dark mode|"
                          r"another|the other|other)$|\b(?:window|tab|tabs|incognito|private mode|screen)\b")
_PROFILE_LIST_RE = re.compile(r"(?:which|what) (?:" + _PB + r" )?profiles (?:do i have|are there|have i got)"
                              r"(?: (?:in|on|for) " + _PB + r")?|(?:list|show|tell)(?: me)? (?:all )?(?:my |the )?"
                              r"(?:" + _PB + r" )?profiles(?: (?:in|on|for) " + _PB + r")?")
_PROFILE_AS_RE = re.compile(r"(?:open|launch|start|run) (?:up )?(?:google )?" + _PB + r" (as|with|for|on|in|using|under|"
                            r"logged in as|signed in as|with the account) (my |the )?(.+?)(?: profile| account| user)?")
_PROFILE_MY_RE = re.compile(r"(?:open|launch|start) (?:up )?(?:my |the )?(.+?)(?:'s|’s| s)? (?:google )?" + _PB +
                            r"(?: profile| window| browser)?")
_PROFILE_SWITCH_RE = re.compile(r"(?:switch|change|go) to (?:my |the )?(.+?) (?:" + _PB + r" )?profile|"
                                r"use (?:my |the )?(.+?) (?:" + _PB + r" )?profile")
_PROFILE_URL_RE = re.compile(r"(?:open|launch) (.+?) in (?:my |the )?(.+?)(?:'s|’s| s)? (?:google )?" + _PB +
                             r"(?: profile| window)?|(?:open|launch) (.+?) in (?:google )?" + _PB +
                             r" (?:as|with|on|for) (?:my |the )?(.+?)(?: profile| account)?")


def _profile_name_ok(name, strict=True):
    """A plausible profile name. `strict` (the user did not say "my" / "as"): a site's name is
    the site ("open chrome with youtube"), never a profile."""
    name = name.strip()
    if not name or _PROFILE_NOT.search(name) or re.search(r"\b(?:in|on|with|at|using|into|and)$", name):
        return False
    return not (strict and targets.website_url(name) and not spoken_email(name))


def profile_action(low, raw, text=None):
    """'open my Work Chrome', 'open Chrome as Ravi', 'open Gmail in my Study Chrome', 'which Chrome
    profiles do I have' -> a structured action, or None."""
    text = text if text is not None else raw
    low = " ".join(low.strip(" .?!").split())

    def act(name, browser, url=""):
        found = re.search(re.escape(name.strip()), text, re.I)
        said = found.group(0) if found else name.strip()
        if said.lower() in ("guest", "a guest", "guest mode"):
            return Action("browser", "chrome_guest", target="guest", parameters={"browser": browser}, raw=raw)
        return Action("browser", "chrome_profile", target=said,
                      parameters={"browser": browser, "url": url, "email": spoken_email(name)}, raw=raw)

    m = _PROFILE_LIST_RE.fullmatch(low)
    if m:
        browser = next((g for g in m.groups() if g), "chrome")
        return Action("browser", "chrome_profiles", target=browser, parameters={"browser": browser}, raw=raw)
    m = re.fullmatch(r"(?:open|launch|start) (?:google )?" + _PB + r" (?:as|in) (?:a )?guest(?: mode)?", low)
    if m:
        return Action("browser", "chrome_guest", target="guest", parameters={"browser": m.group(1)}, raw=raw)
    m = _PROFILE_URL_RE.fullmatch(low)
    if m:
        site, name, browser = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(6), m.group(5))
        url = targets.website_url(site.strip()) or ""
        if url and _profile_name_ok(name):
            return act(name, browser, url)
    m = _PROFILE_AS_RE.fullmatch(low)
    if m and _profile_name_ok(m.group(4), strict=not (m.group(2) in ("as", "logged in as", "signed in as",
                                                                     "with the account", "under") or m.group(3))):
        return act(m.group(4), m.group(1))
    m = _PROFILE_SWITCH_RE.fullmatch(low)
    if m:
        name = m.group(1) or m.group(3)
        browser = m.group(2) or m.group(4) or "chrome"
        if _profile_name_ok(name):
            return act(name, browser)
    m = _PROFILE_MY_RE.fullmatch(low)
    owned = len(low.split()) > 1 and low.split()[1] == "my" or bool(re.search(r"'s|’s", low))
    if m and _profile_name_ok(m.group(1), strict=not owned) and (owned or low.split()[1] == "the"
                                                                 or len(m.group(1).split()) <= 3):
        return act(m.group(1), m.group(2))
    return None


# ── Part 7: the page agent (H1), recipes (H1.1), what Kayra learned (H2) ──
_AGENT_RE = re.compile(r"(?:browser |page )?agent[,:]? (.+)|(?:on|in) (?:this|the) (?:page|site|website|tab)[,:]? (.+)|"
                       r"(?:please )?(?:figure out how to|work out how to) (.+?)(?: (?:on|in) (?:this|the) (?:page|site|website))?|"
                       r"do (?:this|it|that) (?:for me )?on (?:this|the) (?:page|site)[,:]? (.+)")
_RECIPES_RE = re.compile(r"(?:list|show(?: me)?|what are) (?:my |the |your )?(?:page )?(?:shortcuts|recipes)|"
                         r"what (?:page )?(?:shortcuts|recipes) do you (?:know|have|remember)")
_LEARNED_RE = re.compile(r"what (?:have|did) you learn(?:ed|t)?(?: about me| from me| so far)?|"
                         r"show (?:me )?what you(?:'ve| have) learned|what do you remember about how i talk")
_FORGET_LAST_RE = re.compile(r"forget (?:that|what you just learned|the last thing you learned)|unlearn that")
_FORGET_ALL_RE = re.compile(r"forget (?:everything|all) (?:you(?:'ve| have)? learned|you know about how i talk)|"
                            r"unlearn everything|reset (?:everything )?(?:what )?you(?:'ve| have)? learned")
_FORGET_APP_RE = re.compile(r"forget (?:everything|all|what you(?:'ve| have)? learned) (?:about|for|on|in) (?:my )?([\w. ]+)")
_FORGET_RECIPE_RE = re.compile(r"forget (?:the|my|your) (?:page )?(?:shortcut|recipe) (?:for|to) (.+)")
_OTHER_ONE_RE = re.compile(r"(?:no,? )?(?:not that one|(?:open )?the other one|wrong (?:one|chat|group)|"
                           r"i meant the other one|that's the wrong (?:one|chat|group))")


def part_7_action(low, raw, text=None):
    """The page agent, its recipes, and learning — or None."""
    text = text if text is not None else raw
    low = " ".join(low.strip(" .?!").split())
    m = _AGENT_RE.fullmatch(low)
    if m:
        goal = next(g for g in m.groups() if g)
        found = re.search(re.escape(goal), text, re.I)
        return Action("browser", "agent", target=found.group(0) if found else goal, raw=raw)
    if _RECIPES_RE.fullmatch(low):
        return Action("browser", "agent_recipes", target="all", raw=raw)
    if _LEARNED_RE.fullmatch(low):
        return Action("learning", "show", target="all", raw=raw)
    if _FORGET_LAST_RE.fullmatch(low):
        return Action("learning", "forget_last", target="last", raw=raw)
    if _FORGET_ALL_RE.fullmatch(low):
        return Action("learning", "forget_all", target="all", raw=raw)
    m = _FORGET_RECIPE_RE.fullmatch(low)
    if m:
        return Action("learning", "forget_recipe", target=m.group(1).strip(), raw=raw)
    m = _FORGET_APP_RE.fullmatch(low)
    if m and m.group(1).strip() not in ("me", "you"):
        return Action("learning", "forget_app", target=m.group(1).strip().replace("whats app", "whatsapp"), raw=raw)
    if _OTHER_ONE_RE.fullmatch(low):
        return Action("message", "other", target="other", raw=raw)
    return None


def part_7_direct(low, text):
    action = part_7_action(low, text, text)
    if action is None:
        return None
    if action.key == "message.other":
        try:
            from kayra.automation import web_macros as W
            fresh = W.LAST_PICK.get("chosen") and W.CLOCK() - W.LAST_PICK.get("at", 0) <= W.PICK_TTL_S
        except Exception:
            fresh = False
        return [text] if fresh else None               # "no, the other one" only right after a pick
    if action.key == "learning.forget_last":
        from kayra.automation import learning
        fresh = learning.LAST.get("kind") and learning.CLOCK() - learning.LAST.get("at", 0) <= learning.FORGET_WINDOW_S
        return [text] if fresh else None               # otherwise "forget that" is conversation
    return [text]


# ── Rules before the classifier (tuning pass V9) ──
# A whole utterance that the rules understand COMPLETELY and that can only mean one command is
# routed straight to them, skipping the classifier model (~0.45 s on the local model, and its
# occasional misreads: "create a new doc" went to the essay writer, "close it" to Ctrl+W).
# Only these actions qualify; anything open-ended ("search for X", a bare "highlight X") still
# goes to the model, which can tell a web question from a page command.
_DIRECT_BROWSER = frozenset({
    "page_result", "page_results", "page_find_step", "page_find_clear", "tabs_close_unpinned",
    "groups_list", "tabs_group", "group_add", "group_rename", "group_color", "group_collapse",
    "group_ungroup", "group_close", "tab_detach", "windows_merge", "page_ask", "page_comments",
    "video_summary", "page_translate", "selection_explain", "selection_translate", "bookmark_add",
    "bookmarks_search", "bookmark_delete", "history_clear", "history_list", "downloads_list",
    "download_open", "download_show", "download_control", "page_help", "ws_open", "ws_search",
    "ws_create", "ws_share", "slides_present", "ws_recent", "ws_download", "keep_note",
    "form_responses", "classroom_due", "tabs_close_duplicates", "tabs_close_side",
    "tabs_mute_others", "tabs_recently_closed", "tabs_discard", "tab_find", "search_close",
})
_NEEDS_SHEET = frozenset({"sheet_cell", "sheet_calc", "sheet_row"})
_NEEDS_SLIDES = frozenset({"slides_nav"})


def _known_app(name):
    try:
        from kayra.automation import linux_apps
        return linux_apps.resolve(name) is not None
    except Exception:
        return False


def front_site(situation_fn):
    """The site in front: a web-app window's site, or Chrome's active tab when Chrome is in
    front. '' when something else is (Chrome's active tab is NOT a site in front then)."""
    try:
        sit = (situation_fn() if situation_fn else {}) or {}
    except Exception:
        return ""
    if sit.get("site"):
        return str(sit["site"])
    app = str(sit.get("app") or "").lower()
    if any(b in app for b in ("chrome", "chromium", "brave", "edge")) and sit.get("tab_url"):
        host = re.sub(r"^[a-z]+://", "", str(sit["tab_url"]).lower()).split("/")[0]
        return host[4:] if host.startswith("www.") else host
    return ""


def _tab_url(situation_fn):
    try:
        return str(((situation_fn() if situation_fn else {}) or {}).get("tab_url") or "")
    except Exception:
        return ""


# ── G5: several steps in one sentence ──
# "open the budget sheet and total column C", "tick I agree and then submit this form". Each
# half must be a COMPLETE command on its own, and the later half is judged against what the
# earlier one will have put in front ("total column C" needs a Sheet; the open makes one).
# A first step that carries free text (a question to an AI site, a message, a search) is only
# split at "then" / "after that": its own words may contain "and".
_STEP_JOIN = re.compile(r",? (?:and then|and after that|then|after that|and)(?= )", re.I)
_SEQUENTIAL = re.compile(r"(?:and then|and after that|then|after that)$", re.I)
_FREE_TEXT = frozenset({"ai_ask", "site_search", "search_here", "ws_search", "keep_note", "page_ask", "send",
                        "page_find", "email_search", "search"})
MAX_STEPS = 4


def _predicted(token, situation_fn):
    """The screen a step will leave in front, for judging the step after it."""
    try:
        action = normalize_command(token)
    except Exception:
        action = None
    if action is None:
        return situation_fn
    chrome = lambda url: (lambda: {"app": "google-chrome", "tab_url": url})
    p = action.parameters or {}
    if action.domain == "message":
        return lambda: {"site": "web.whatsapp.com", "app": "chrome-web.whatsapp.com__-default"}
    if action.domain == "browser":
        kind = p.get("kind")
        if action.action in ("ws_open", "ws_search", "ws_create") and (kind or action.target) in ("sheet", "doc", "slides"):
            page = {"sheet": "spreadsheets", "doc": "document", "slides": "presentation"}[kind or action.target]
            return chrome(f"https://docs.google.com/{page}/d/predicted/edit")
        if action.action == "site_search" and p.get("site") in ("amazon", "flipkart", "youtube"):
            return chrome({"amazon": "https://www.amazon.in/s?k=x", "flipkart": "https://www.flipkart.com/search?q=x",
                           "youtube": "https://www.youtube.com/results?search_query=x"}[p["site"]])
        if action.action in ("ai_ask", "ai_read"):
            return chrome({"claude": "https://claude.ai/", "gemini": "https://gemini.google.com/app"}
                          .get(p.get("bot") or action.target, "https://chatgpt.com/"))
        if action.action == "social":
            return chrome({"reddit": "https://www.reddit.com/", "linkedin": "https://www.linkedin.com/",
                           "github": "https://github.com/"}.get(p.get("site"), "https://example.com/"))
        if action.action in ("page_result", "open_url"):
            return chrome(str(p.get("url") or "https://example.com/"))
    return situation_fn


def _split_steps(text, situation_fn, depth=0):
    """[tokens] when `text` is two or more complete commands joined by and / then, else None."""
    if depth >= MAX_STEPS - 1:
        return None
    for m in _STEP_JOIN.finditer(text):
        head, tail = text[:m.start()].strip(" ,"), text[m.end():].strip(" ,")
        if not head or not tail:
            continue
        first = _direct_one(head, situation_fn)
        if not first:
            continue
        action = normalize_command(first[-1])
        free = action is not None and action.action in _FREE_TEXT and \
            not (action.action == "ws_search" and action.parameters.get("open"))   # "the sheet Ravi sent": a name
        if free and not _SEQUENTIAL.search(m.group(0).strip(" ,")):
            continue                                    # "ask chatgpt to compare rust and go": one question
        later = _predicted(first[-1], situation_fn)
        rest = _direct_one(tail, later) or _split_steps(tail, later, depth + 1)
        if rest:
            return first + rest
    return None


def direct_tokens(utterance, situation_fn=None):
    """
    [tokens] for an utterance the rules alone fully understand, else None (ask the model).
    Several complete commands joined by "and" / "then" become several tokens, in order (G5);
    a sentence that is ONE command whose words contain "and" stays one.
    """
    text = " ".join(str(utterance or "").strip().rstrip(".?!").split())
    low = text.lower()
    if not low or len(low) > 300 or low.startswith(("browser ", "meet ", "email ", "calendar ")):
        return None
    if _STEP_JOIN.search(text):
        steps = _split_steps(text, situation_fn)
        if steps and len(steps) > 1:
            return steps
    return _direct_one(text, situation_fn)


def _direct_one(utterance, situation_fn=None):
    """
    [token] for an utterance the rules alone fully understand, else None (ask the model).
    Contextual shapes look at the screen only when needed: Sheets maths only with a Sheet in
    front ("what's the total of 25 and 30" is a sum question, not column "25 and 30"), slide
    steps only with Slides in front, "close it" only while Kayra remembers a tab.
    """
    text = " ".join(str(utterance or "").strip().rstrip(".?!").split())
    low = text.lower()
    if not low or len(low) > 160:
        return None
    if low.startswith(("browser ", "meet ", "email ", "calendar ")):
        return None                                     # already a token; let it through
    # Meet sharing in its bare forms ("share my screen", "share the LM Studio window").
    if _BARE_SHARE.match(low) and share_request(low) is not None:
        return [f"meet {text}"]
    if re.fullmatch(r"(?:let them in|admit (?:everyone|all|them)|let (?:everyone|people) in)", low):
        return ["meet admit everyone"]
    # Gmail and Calendar phrasings that cannot be anything else.
    if re.fullmatch(r"(?:search|look) (?:in |through )?(?:my )?(?:mail|email|emails|inbox|gmail) for .+", low):
        return [f"email {text}"]
    if re.fullmatch(r"(?:compose|write|start) (?:a )?new (?:email|mail)|new email", low):
        return ["email compose"]
    if re.fullmatch(r"(?:join|start) (?:my |the )?next (?:meeting|call)", low):
        return ["calendar join my next meeting"]
    if re.fullmatch(r"what'?s? (?:is )?(?:my |the )?next (?:meeting|event|call)(?: on my calendar)?|"
                    r"when is my next (?:meeting|event|call)", low):
        return ["calendar next meeting"]
    # Fix round V11: shapes that are a complete command whatever is in front.
    if _WA_NAMED_RE.fullmatch(low) or _WA_READ_RECENT.fullmatch(low):
        return [f"message {text}"]                      # "open graphics team group in whatsapp"
    if last_messages_request(low) and not re.search(r"\b(?:e-?mails?|mails?|gmail|inbox)\b", low):
        return [f"message {text}"]                      # "read the last three messages of mota panda"
    if profile_action(low, f"browser {text}", text) is not None:
        return [f"browser {text}"]                      # Part 5P: a Chrome profile
    found = part_7_direct(low, text)
    if found is not None or part_7_action(low, text, text) is not None:
        return found                                    # Part 7: the agent, learning ("forget that" only when fresh)
    found = part_6_direct(low, text, situation_fn)
    if found:
        return found                                    # Part 6: forms, video, site search, shopping
    found = part_6b_direct(low, text, situation_fn)
    if found:
        return found                                    # Part 6b: AI sites, WhatsApp files, Reddit / LinkedIn / GitHub
    if search_request(low, text):
        return [f"browser {text}"]                      # "click on the search bar and search for X"
    if low not in ("scroll down", "scroll up") and scroll_request(low):
        return [f"browser {text}"]                      # "scroll the mail to the bottom"
    # H0.2 — the app in front. With WhatsApp in front, "open X", "click on X", "go to X chat"
    # are a CHAT (the bug report: "open XYZ chat" -> "no app called XYZ chat"), "read it" is the
    # open chat, and "who messaged me" is WhatsApp's unread list.
    if front_site(situation_fn) == "web.whatsapp.com":
        if _WA_CLOSE_CHAT.fullmatch(low):
            return ["message close this chat"]              # "close the group" is the open chat here
        if _WA_LAST_HERE.fullmatch(low):
            return [f"message {text}"]                      # "read the last three messages"
        m = re.fullmatch(r"(?:open|click(?: on)?|select|go to|show me) " + _WA_NTH.pattern, low)
        if m:
            return [f"message open result {_ORDINAL_N.get(m.group(1)) or int(m.group(1)[0])}"]
        if _WA_SEARCH.fullmatch(low) and _bare_workspace(low, text) is None and not re.search(
                r"\b(?:google|youtube|web|internet|online|gmail|mail|drive|docs|sheets|amazon|flipkart|maps)\b", low):
            return [f"message {text}"]                      # "search for general panchayat [and open it]"
        m = re.fullmatch(r"(?:open|go to|click(?: on)?|switch to|show me) (?:the |my |our )?(.+?)"
                         r"( chat| group| conversation)?", low)
        if m and not re.match(r"(?:new tab|settings|a new|chrome|whatsapp)\b", m.group(1)) \
                and _bare_workspace(low, text) is None:      # "open the budget sheet" is still a Sheet
            name = m.group(1)
            said_chat = bool(m.group(2))
            # "open youtube" in front of WhatsApp is still YouTube: a known site or app is a
            # chat only when the user said "chat" / "group".
            if said_chat or not (targets.website_url(name) or _known_app(name)):
                found = re.search(re.escape(name), text, re.I)
                return [f"message open {found.group(0) if found else name}"]
        if re.fullmatch(r"(?:read|summari[sz]e) (?:it|this|this chat|the messages|the chat)|what did (?:they|people) say", low):
            return ["message read this"]
        if re.fullmatch(r"(?:read|summari[sz]e|what are|tell me) (?:me )?(?:the |my )?(?:most )?(?:recent|latest|last|new|newest) "
                        r"(?:chat|messages?)", low):
            return ["message read the recent chat"]
        if re.fullmatch(r"(?:who|anyone) (?:messaged|texted|wrote to) me(?: today)?|(?:any |show |read )?unread(?: chats| messages)?", low):
            return ["message who messaged me"]
    # "close it" / "go back to it": only while a tab is remembered (G1).
    if re.fullmatch(r"(?:close|go back to|switch back to|switch to) (?:it|that tab|that one)", low):
        try:
            from kayra.automation import web_macros
            fresh = web_macros.LAST_TAB.get("id") is not None and \
                web_macros.CLOCK() - web_macros.LAST_TAB.get("at", 0) <= web_macros.LAST_TAB_TTL_S
        except Exception:
            fresh = False
        return [f"browser {text}"] if fresh else None
    # The WHOLE browser parser (Parts 1–4): only actions on the allow-list below go rules-first;
    # its last-resort "click on <words>" guess never does.
    action = _web_action("browser", text, f"browser {text}")
    if action is None or action.domain != "browser":
        return None
    if action.action in ("ws_open", "ws_search") and action.parameters.get("kind") in ("file", "pdf") \
            and not re.search(r"\b(?:google )?drive\b", low):
        return None                     # "open the report file" may be a LOCAL file: ask the model
    if action.action in _NEEDS_SHEET:
        return [f"browser {text}"] if "docs.google.com/spreadsheets/" in _tab_url(situation_fn) else None
    if action.action in _NEEDS_SLIDES:
        return [f"browser {text}"] if "docs.google.com/presentation/" in _tab_url(situation_fn) else None
    if action.action == "page_find" and re.search(r"\bon (?:this|the) page\b|\bin this (?:doc|document|sheet)\b", low):
        return [f"browser {text}"]
    return [f"browser {text}"] if action.action in _DIRECT_BROWSER else None


def _bare_workspace(lowered, raw):
    """'open my budget sheet', 'create a new doc called X', 'next slide' without a prefix."""
    # Bare: Google file kinds only — "open the report file / pdf" stays a LOCAL file.
    kinds = r"(?:sheet|spreadsheet|doc|document|deck|presentation|slides|slide deck|form)"
    if re.match(r"(?:open (?:my |the |our )?.+ " + kinds + r"$|open (?:my |the )?" + kinds + r" (?:about|called|named)|"
                r"(?:create|make|start) (?:a |an )?new |(?:next|previous) slide$|start the slideshow|"
                r"(?:go|jump) to cell |what(?:'s| is) the (?:total|sum|average) of |read row \d|"
                r"search my (?:sheets|docs|drive|slides|files) |(?:add|take) a note |find (?:the |my )?pdf |"
                r"(?:open|find|show me) (?:me )?(?:the |my |a )?(?:pdf|" + kinds[3:-1] + r") (?:that |which )?[a-z][a-z .'-]{0,40}? "
                r"(?:sent|shared|gave|emailed)\b)", lowered):
        return part_4_action(lowered, raw, raw)
    return None


# ── Part 3 (BROWSER_CONTROL_PLAN C5, C6, C8–C11, G1, G2, G4) ──
LANGUAGES = {"english": "en", "hindi": "hi", "spanish": "es", "french": "fr", "german": "de",
             "tamil": "ta", "telugu": "te", "bengali": "bn", "bangla": "bn", "marathi": "mr",
             "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "urdu": "ur",
             "japanese": "ja", "chinese": "zh-CN", "korean": "ko", "arabic": "ar",
             "russian": "ru", "portuguese": "pt", "italian": "it", "nepali": "ne",
             "odia": "or", "sanskrit": "sa", "indonesian": "id", "turkish": "tr", "dutch": "nl"}
_PAGE = r"(?:this|the) (?:page|article|site|website|post|doc|document)"
_IT = r"(?:it|that|that tab|this one|that one|that page)"


def part_3_action(low, raw, text=None):
    """The Part 3 shapes (questions, comments, video, translate, bookmarks, history,
    downloads, "it", search here, what can I do here), or None."""
    text = text if text is not None else raw
    low = low.strip(" .?!")
    # C5 — a question about the page.
    m = re.fullmatch(r"what (?:does|do) " + _PAGE + r" (?:say|tell me|mention) (?:about|on|regarding) (.+)", low) \
        or re.fullmatch(r"(?:ask|question) " + _PAGE + r"[:,]? (.+)", low) \
        or re.fullmatch(r"according to " + _PAGE + r",? (.+)", low) \
        or re.fullmatch(r"(?:does|did) " + _PAGE + r" (?:say|mention|talk about) (.+)", low)
    if m:
        return Action("browser", "page_ask", target="current", parameters={"question": low}, raw=raw)
    # C6 — comments, reviews, a video.
    m = re.fullmatch(r"(read|show|list|summari[sz]e|what are|what do people say in|what are people saying in)"
                     r"(?: me)?(?: the)?(?: top| best| latest| first)? (comments|reviews|replies|feedback)"
                     r"(?: (?:on|of|under|for|about) (?:this|the) (?:video|page|post|product|article|item))?", low) \
        or re.fullmatch(r"what (?:do|are) (?:people|users|buyers|viewers) (?:say(?:ing)?|think(?:ing)?)"
                        r"(?: about (?:this|it|the (?:video|product|post)))?", low)
    if m:
        verb = m.group(1) if m.lastindex else "what do"
        return Action("browser", "page_comments", target="current",
                      parameters={"summarize": not str(verb).startswith(("read", "show", "list"))}, raw=raw)
    if re.fullmatch(r"(?:summari[sz]e|tldr|recap|give me the gist of|what is|what's|explain) (?:this|the|that) "
                    r"(?:youtube )?video(?: about| for me)?|what is (?:this|the) video about", low):
        return Action("browser", "video_summary", target="current", raw=raw)
    # C8 — translate, explain the selection.
    m = re.fullmatch(r"translate (?:this|the) (?:page|site|website|article|tab)(?: (?:to|into|in) (\w+))?", low)
    if m:
        return Action("browser", "page_translate", target=(m.group(1) or "english"), raw=raw)
    m = re.fullmatch(r"translate (?:the |this |my )?(?:selected|highlighted|selection)(?: text| words| part)?"
                     r"(?: (?:to|into|in) (\w+))?", low)
    if m:
        return Action("browser", "selection_translate", target=(m.group(1) or "english"), raw=raw)
    if re.fullmatch(r"(?:explain|what does|what do|simplify|define) (?:the |this |my )?(?:selected|highlighted|selection)"
                    r"(?: text| words| part| line)?(?: mean)?(?: to me| for me)?|explain what i (?:selected|highlighted)", low):
        return Action("browser", "selection_explain", target="current", raw=raw)
    # C9 — bookmarks.
    m = re.fullmatch(r"(?:bookmark|save) (?:this|the current) (?:page|tab|site|article)(?: as a bookmark)?"
                     r"(?: (?:in|into|under|to) (?:the |my )?(.+?)(?: folder)?)?|add (?:this|the) (?:page|tab|site) "
                     r"to (?:my )?bookmarks(?: (?:in|under|into) (?:the |my )?(.+?)(?: folder)?)?|bookmark (?:this|it)", low)
    if m:
        folder = (m.group(1) or m.group(2) or "") if m.lastindex else ""
        return Action("browser", "bookmark_add", target="current", parameters={"folder": folder}, raw=raw)
    m = re.fullmatch(r"(?:delete|remove) (?:the |my |this )?bookmarks? (?:for |called |named |of )?(.+)", low)
    if m:
        return Action("browser", "bookmark_delete", target=m.group(1), raw=raw)
    m = re.fullmatch(r"(?:search|find|look for|look in|search in) (?:in )?(?:my )?bookmarks (?:for |about )?(.+)|"
                     r"(?:what|which|show me|list) (?:my )?bookmarks (?:do i have )?(?:for|about|on) (.+)", low)
    if m:
        return Action("browser", "bookmarks_search", target=(m.group(1) or m.group(2)), raw=raw)
    # C10 — history.
    m = re.fullmatch(r"(?:clear|delete|erase|wipe|remove) (?:my |the )?(?:browsing |browser |search )?history"
                     r"(?: (?:from|for|of))? ?(today|the last hour|the past hour|last hour|this hour|the last 15 minutes)?|"
                     r"(?:clear|delete|erase|wipe) (today'?s|the last hour'?s|this hour'?s) (?:browsing )?history|"
                     r"(?:clear|delete|erase|wipe|remove) (?:the |my )?(last hour|past hour|last 15 minutes|today) of "
                     r"(?:my )?(?:browsing |browser )?history", low)
    if m:
        span = (m.group(1) or m.group(2) or m.group(3) or "today").replace("'s", "")
        span = "15m" if "15" in span else ("hour" if "hour" in span else "today")
        return Action("browser", "history_clear", target=span, raw=raw)
    m = re.fullmatch(r"(?:what (?:sites |pages )?did i (?:visit|browse|open|look at|read)|show (?:me )?my (?:recent |browsing )?history|"
                     r"what (?:sites|pages) did i (?:visit|open))(?: (today|yesterday|this week))?", low)
    if m:
        return Action("browser", "history_list", target=m.group(1) or "today",
                      parameters={"days": {"yesterday": 2, "this week": 7}.get(m.group(1) or "", 1)}, raw=raw)
    # C11 — downloads.
    if re.fullmatch(r"(?:what did i (?:just )?download|(?:show|list|read) (?:me )?(?:my |the )?(?:recent |last )?downloads|"
                    r"what'?s downloading|is (?:my|the) download (?:done|finished|complete)|(?:how is|how's) (?:my|the) download(?: going)?)", low):
        return Action("browser", "downloads_list", target="recent", raw=raw)
    if re.fullmatch(r"open (?:my |the )?(?:last|latest|most recent|recent|newest) (?:download|downloaded file|file i downloaded)|"
                    r"open (?:what|the file) i (?:just )?downloaded", low):
        return Action("browser", "download_open", target="latest", raw=raw)
    if re.fullmatch(r"(?:show|reveal|open) (?:my |the )?(?:last |latest )?download(?:ed file)? in (?:the |its )?folder|"
                    r"show (?:it|that) in (?:the |its )?folder|(?:open|show) (?:my |the )?downloads? folder", low):
        return Action("browser", "download_show", target="latest", raw=raw)
    m = re.fullmatch(r"(pause|resume|continue|cancel|stop) (?:the |my |this )?(?:current )?download(?:ing)?", low)
    if m:
        verb = {"continue": "resume", "stop": "cancel"}.get(m.group(1), m.group(1))
        return Action("browser", "download_control", target=verb, raw=raw)
    # G1 — "it": the tab Kayra last opened or switched to.
    m = re.fullmatch(r"(close|switch (?:back )?to|go (?:back )?to|bring (?:back|up)|show me|read|summari[sz]e) " + _IT, low)
    if m:
        verb = m.group(1)
        action = ("tab_close" if verb == "close" else "page_read" if verb == "read"
                  else "page_summarize" if verb.startswith("summari") else "tab_switch")
        return Action("browser", action, target="it", raw=raw)
    # G2 — search where you are.
    m = re.fullmatch(r"search (?:for )?(.+?) (?:here|on this (?:site|page|website)|in this (?:sheet|spreadsheet|doc|document|"
                     r"site|page|tab|inbox|mail|drive|channel|repo|repository))", low) \
        or re.fullmatch(r"search (?:this|the) (?:sheet|spreadsheet|doc|document|site|website|page|inbox|mail|drive|channel|"
                        r"repo|repository|tab)? ?for (.+)", low) \
        or re.fullmatch(r"search (?:for )?(.+)", low)
    if m:
        query = m.group(1)
        found = re.search(re.escape(query), text, re.I)
        return Action("browser", "search_here", target=found.group(0) if found else query, raw=raw)
    # G4 — what can I do here?
    if re.fullmatch(r"what (?:can|could) i (?:do|say|ask(?: you)?) (?:here|on this (?:page|site|website)|with this (?:page|site))|"
                    r"what (?:can|could) you do (?:here|on this (?:page|site))|help(?: me)? (?:here|with this (?:page|site))|"
                    r"what are my options here", low):
        return Action("browser", "page_help", target="current", raw=raw)
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
        # "open the graphics team group": a tab group when one has that name, else WhatsApp's.
        or_chat = m.group(1) == "open" and target not in ("all", "current") and "tab group" not in low
        return Action("browser", "group_collapse", target=target,
                      parameters={"collapsed": collapse, "or_chat": or_chat}, raw=raw)
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


# G5: steps that put a page in front, and steps that act on the page in front.
_OPENS_PAGE = frozenset({"ws_open", "ws_search", "ws_create", "open_url", "site_search", "page_result", "ai_ask",
                         "social", "tab_switch", "email_search"})
_ON_PAGE = frozenset({"sheet_calc", "sheet_row", "sheet_cell", "slides_nav", "slides_present", "page_read",
                      "page_summarize", "page_ask", "page_click", "page_type", "page_scroll", "page_find", "video",
                      "form_fill", "form_choose", "form_submit", "file_upload", "product_info", "shop_refine",
                      "page_result", "page_results", "ai_read", "ws_share", "ws_download", "page_comments"})


def _opens_page(action):
    return (action.domain == "browser" and action.action in _OPENS_PAGE) or \
        (action.domain == "message" and action.action == "open")


def _needs_page(action):
    return (action.domain == "browser" and action.action in _ON_PAGE) or \
        (action.domain == "message" and action.target in ("current", "this", "recent"))


def _is_guess(action):
    """The parser's own catch-all: 'browser <anything>' read as a click, at low confidence."""
    return action.domain == "browser" and action.action == "page_click" and action.confidence < 1.0


def _worth_rethinking(action, result):
    # "X isn't open" is a complete, honest answer to "close X": a model asked to rethink it
    # closed something ELSE (the chat inside WhatsApp, for "close WhatsApp").
    return (result is not None and result.status in _RETHINK_STATUSES
            and action.domain not in _NEVER_RETHINK and action.key not in ("app.close", "message.close"))


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
    # Part 6
    "browser.form_submit": "Submit this form? Once it's sent I can't take it back.",
    "browser.profile_clear": "Forget every detail you gave me for forms?",
    "browser.video_subscribe": "Subscribe to this channel?",
    "learning.forget_all": "Forget everything I've learned about how you talk, and every page shortcut?",
}


# Part 7c: answers to "which one?" — which actions offer a choice, and where the answer teaches.
_CHOICE_TTL_S = 120
_CHOICE_DOMAINS = frozenset({"message", "browser"})
_CHOICE_LEARN = {"message.open": "whatsapp", "message.read": "whatsapp", "message.send": "whatsapp",
                 "message.send_file": "whatsapp"}

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
        # Part 7c: a "which one?" Kayra asked ({action, candidates, question, at}); the next answer
        # that picks one runs the action on it and, for a chat name, teaches the alias.
        self.pending_choice = None
        # Confirmations the user switched off by voice ("yes, don't ask again"): key -> expiry.
        # Only `_TRUSTABLE` keys, cleared when the call ends (meet.leave).
        self.trusted = {}

    def _slides_in_front(self):
        try:
            tab = (self.situation_fn() if self.situation_fn else {}) or {}
        except Exception:
            return False
        return "docs.google.com/presentation/" in str(tab.get("tab_url") or "")

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
            if action.key == "meet.share" and _bare_present(action) and self._slides_in_front():
                # "start presenting" with a Slides deck in front means the SLIDESHOW (Part 4,
                # E3), decided before the share question is ever asked.
                action = Action("browser", "slides_present", target="current", raw=action.raw)
                verdict, reason = classify_action(action)
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
            names = [str(c) for c in ((result.detail or {}).get("candidates") or []) if str(c).strip()]
            if 1 < len(names) <= 6 and action.domain in _CHOICE_DOMAINS:
                self.pending_choice = {"action": action, "candidates": names, "question": result.message,
                                       "at": time.monotonic()}
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
        groups = plan_actions(merge_share_steps(merge_tab_steps(actions)))
        try:
            for index, group in enumerate(groups):
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
                    # G5: behind a yes / no question the REST is parked, not lost — "yes" runs
                    # it after the confirmed step; "no" drops it (as `run_steps` does).
                    if result.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS):
                        rest = [a.raw for g in groups[index + 1:] for a in g if a.raw]
                        self.pending_plan = rest if result.status == Status.NEEDS_CONFIRMATION else []
                        return " ".join(spoken)
                # G5: a later step that acts on the page this one was to open must not run on
                # whatever page is in front instead ("total column C" on the wrong sheet).
                failed = [r for r in results if r.status in (Status.FAILED, Status.NOT_FOUND, Status.UNSUPPORTED)]
                later = [a for g in groups[index + 1:] for a in g]
                if failed and len(group) == 1 and _opens_page(group[0]) and later and _needs_page(later[0]):
                    spoken.append("I stopped there, since the next step needed that page.")
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
            if (proposed.key, proposed.target, proposed.parameters) == (action.key, action.target, action.parameters):
                continue       # the model proposed the SAME thing: it just failed, do not run it twice
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
            if result.status in (Status.FAILED, Status.NOT_FOUND, Status.UNSUPPORTED) and _opens_page(action) and tokens:
                following = normalize_command(tokens[0], self.context)
                if following is not None and _needs_page(following):
                    spoken.append("I stopped there, since the next step needed that page.")
                    break
        return spoken

    # ── confirmations ──
    def pending_confirmation(self):
        if self.confirmations.peek() is not None:
            return self.confirmations.prompt
        choice = self.pending_choice
        if choice and time.monotonic() - choice["at"] <= _CHOICE_TTL_S:
            return choice["question"]
        self.pending_choice = None
        return None

    def resolve_choice(self, reply):
        """(True, sentence) when `reply` answers the pending "which one?", else (False, "").
        An answer that is not a pick drops the question and goes on as a normal request."""
        choice, self.pending_choice = self.pending_choice, None
        if not choice or time.monotonic() - choice["at"] > _CHOICE_TTL_S:
            return False, ""
        low = " ".join(str(reply or "").lower().strip(" .!?").split())
        if re.fullmatch(r"(?:neither|none|none of them|neither of them|cancel|never ?mind|no|nope)(?: of (?:them|those))?", low):
            return True, "Alright."
        names = choice["candidates"]
        picked = None
        m = re.fullmatch(r"(?:the )?(first|second|third|fourth|fifth|sixth|last|1st|2nd|3rd|4th|5th)(?: one)?|"
                         r"(?:number |option )(\d)", low)
        if m:
            index = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
                     "fifth": 4, "5th": 4, "sixth": 5, "last": len(names) - 1}.get(m.group(1) or "", None)
            index = int(m.group(2)) - 1 if m.group(2) else index
            picked = names[index] if index is not None and 0 <= index < len(names) else None
        else:
            from kayra.automation.semantic_match import match, PICK
            said = re.sub(r"^(?:i mean |i meant |the one called |it's |it is |yes,? )", "", low)
            decision = match(said, names)
            picked = decision.best.name if decision.decision == PICK else None
        if picked is None:
            return False, ""
        action = choice["action"]
        chosen = Action(action.domain, action.action, target=picked, parameters=dict(action.parameters), raw=action.raw)
        result = self.execute_action(chosen)
        if result.ok or result.status == Status.NEEDS_CONFIRMATION:
            app = _CHOICE_LEARN.get(action.key)
            if app and action.target and action.target not in ("current", "recent"):
                from kayra.automation import learning
                learning.teach(app, action.target, picked)
        return True, result.message or "Done."

    def resolve_confirmation(self, reply):
        if self.confirmations.peek() is None and self.pending_choice is not None:
            return self.resolve_choice(reply)
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
