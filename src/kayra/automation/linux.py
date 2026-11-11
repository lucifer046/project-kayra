# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation/linux.py                            │
# │           The "hands" on Linux — Hyprland / Omarchy first              │
# └────────────────────────────────────────────────────────────────────────┘
"""
Desktop automation for Linux, built on the same pipeline as Windows:

    DMM token -> common.normalize_command() -> Action -> policy -> _dispatch() -> ActionResult

Only `_dispatch` and the handlers below are Linux-specific. The normalizer, the power-target
gate, the confirmation manager, the planner, the timers and the filesystem handlers are the
shared ones in `automation.common`, so "close chrome" and "system shutdown the engine car"
mean exactly what they mean on Windows.

HOW EACH CAPABILITY IS REACHED
------------------------------
| Capability                 | Mechanism                                   | Fallback          |
|----------------------------|---------------------------------------------|-------------------|
| windows: list/focus/close  | Hyprland IPC socket (`platforms.hyprland`)  | reported, not faked|
| fullscreen/float/pin/move  | Hyprland dispatch (Lua 0.55+, legacy older) |                   |
| workspaces                 | Hyprland dispatch                           |                   |
| launch an application      | XDG desktop entry via `uwsm-app`/`gtk-launch`| Exec= line       |
| keys / typing              | `wtype` (Wayland virtual keyboard)          | `ydotool`         |
| media                      | `playerctl`                                 | MPRIS over `gdbus`|
| volume                     | `wpctl` (PipeWire)                          | `pamixer`         |
| brightness                 | `brightnessctl`                             |                   |
| screenshot                 | `grim` (+ `slurp` for a region)             |                   |
| clipboard                  | `wl-copy` / `wl-paste`                      | pyperclip         |
| Wi-Fi / Bluetooth          | `nmcli` / `bluetoothctl`                    |                   |
| do not disturb / night light| `omarchy-shell` / `hyprsunset`             | `makoctl`         |
| lock / power               | `omarchy-system-lock`/`loginctl`, `systemctl`|                  |

Every external program is called with an ARGUMENT VECTOR and `shell=False`, from a fixed
table — the user's words are only ever an argument, never a command. Nothing here terminates a
process by name: closing is Hyprland's `window.close`, which is what clicking the X does.

ONE INTENT, ONE TARGET — the Windows rule, unchanged. "close chrome" with three Chrome windows
closes the focused one if it is one of them, and otherwise ASKS. "close all chrome" is its own
token and is the only way to widen it.

KAYRA'S OWN WINDOW IS NEVER A TARGET. The desktop UI is a Hyprland client like any other
(class `kayra`); `_is_kayra` excludes it, and the STT browser's PIDs, from every resolution.
"""

import os
import re
import time
import collections
import json
import shutil
import socket
import subprocess
import webbrowser
from urllib.parse import quote_plus

try:
    import psutil
except Exception:                      # pragma: no cover
    psutil = None

from kayra.automation import targets
from kayra.automation import linux_apps
from kayra.automation import common
from kayra.automation.common import (AutomationRuntime, CreateFolder, CreateFile, RenamePath,
                                     CopyPath, MovePath, DeletePath, SearchFiles,
                                     RunShellCommand, _humanize_duration, prune_screenshots,
                                     _power_refusal_sentence, SCREENSHOT_KEEP)
from kayra.automation.policy import ActionResult, Status
from kayra.platforms import hyprland as _hyprland_module



# ┌────────────────────────────────────────────────────────────────────────┐
# │             PROCESS RUNNER (injectable — the test suite fakes it)       │
# └────────────────────────────────────────────────────────────────────────┘

class Runner:
    """The ONLY way this module starts a process. Replaced wholesale by the test suite."""

    def which(self, name):
        return shutil.which(name)

    def run(self, argv, timeout=4.0, input_text=None, stdin_path=None):
        """Runs to completion. Returns (returncode, stdout) — (None, "") when it could not run."""
        try:
            if stdin_path is not None:
                with open(stdin_path, "rb") as handle:
                    done = subprocess.run(argv, stdin=handle, capture_output=True,
                                          shell=False, timeout=timeout)
                return done.returncode, (done.stdout or b"").decode("utf-8", "replace")
            done = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                                  shell=False, timeout=timeout)
            return done.returncode, (done.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return None, ""

    def spawn(self, argv):
        """Starts a detached program (an application launch). True when it started."""
        try:
            subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
            return True
        except OSError:
            return False


RUNNER = Runner()
HYPR = _hyprland_module           # replaced by a fake compositor in the test suite
SLEEP = time.sleep                # replaced by a no-op in the test suite

LAUNCH_TIMEOUT_S = 8.0
CLOSE_TIMEOUT_S = 2.0


def _have(name):
    return bool(RUNNER.which(name))


def _run(*argv, timeout=4.0, input_text=None, stdin_path=None):
    if stdin_path is not None:
        return RUNNER.run(list(argv), timeout=timeout, stdin_path=stdin_path)
    return RUNNER.run(list(argv), timeout=timeout, input_text=input_text)


def _ok(*argv, timeout=4.0, input_text=None):
    code, _ = _run(*argv, timeout=timeout, input_text=input_text)
    return code == 0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          WINDOWS (HYPRLAND)                            │
# └────────────────────────────────────────────────────────────────────────┘

class Window:
    __slots__ = ("address", "cls", "initial_class", "title", "pid", "workspace",
                 "floating", "fullscreen", "pinned", "focus_order", "kayra_owned")

    def __init__(self, client):
        self.address = str(client.get("address") or "")
        self.cls = str(client.get("class") or "").lower()
        self.initial_class = str(client.get("initialClass") or "").lower()
        self.title = str(client.get("title") or "")
        self.pid = client.get("pid") or 0
        workspace = client.get("workspace") or {}
        self.workspace = str(workspace.get("name") or workspace.get("id") or "")
        self.floating = bool(client.get("floating"))
        self.fullscreen = client.get("fullscreen") or 0
        self.pinned = bool(client.get("pinned"))
        self.focus_order = client.get("focusHistoryID", 999)
        self.kayra_owned = _is_kayra(self)

    @property
    def classes(self):
        return {self.cls, self.initial_class} - {""}

    @property
    def selector(self):
        return f"address:{self.address}"

    def label(self):
        return f"{self.cls or 'window'}: {self.title[:40]}"

    def __repr__(self):
        return f"<Window {self.cls} {self.address} {self.title[:30]!r}>"


def _is_kayra(window):
    if window.pid and window.pid == os.getpid():
        return True
    if window.cls == "kayra" or window.initial_class == "kayra":
        return True
    try:
        return bool(window.pid) and window.pid in targets.kayra_owned_pids()
    except Exception:
        return False


def hyprland_ready():
    return HYPR.available()


def list_windows(include_kayra=False):
    windows = [Window(c) for c in HYPR.clients() if c.get("mapped", True) and c.get("address")]
    return windows if include_kayra else [w for w in windows if not w.kayra_owned]


def active_window():
    client = HYPR.active_client()
    return Window(client) if client else None


def _dispatch_window(lua_call, legacy, window=None, **fields):
    """`hl.dsp.window.<lua_call>({ ...fields, window = ... })` with a legacy fallback."""
    parts = [f"{k} = {_lua(v)}" for k, v in fields.items()]
    if window is not None:
        parts.append(f"window = {HYPR.lua_string(window.selector)}")
    lua = f"hl.dsp.window.{lua_call}({{ {', '.join(parts)} }})" if parts else \
        f"hl.dsp.window.{lua_call}()"
    return HYPR.dispatch(lua, legacy)


def _lua(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return HYPR.lua_string(value)


def focus_window(window):
    ok = HYPR.dispatch(f"hl.dsp.focus({{ window = {HYPR.lua_string(window.selector)} }})",
                       f"focuswindow {window.selector}")
    if ok:
        SLEEP(0.12)            # let the compositor hand keyboard focus over before any keys
    return ok


def close_window(window):
    return _dispatch_window("close", f"closewindow {window.selector}", window)


def wait_until_gone(address, timeout=None):
    deadline = time.monotonic() + (CLOSE_TIMEOUT_S if timeout is None else timeout)
    while True:
        if not any(w.address == address for w in list_windows(include_kayra=True)):
            return True
        if time.monotonic() >= deadline:
            return False
        SLEEP(0.1)


def _webapp_site(app):
    """'web.whatsapp.com' for a web-app entry (omarchy-launch-webapp …), else None."""
    try:
        url = linux_apps.webapp_url(getattr(app, "entry", None))
    except Exception:
        url = None
    if not url:
        return None
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0]
    return host[4:] if host.startswith("www.") else host


def _shows_site(window, site):
    """A Chromium --app window of `site`, whatever profile it runs in. The entry's
    StartupWMClass names ONE profile ("…__-Default"); a web app launched with another profile
    has another class ("…__-Profile_1"), so Kayra waited 8 s for a window that was already
    there and then said "WhatsApp isn't open" (the audit log, 2026-11-11)."""
    return bool(site) and any(linux_apps.site_of_window_class(c) == site for c in window.classes)


def _windows_for(target, strict=True):
    """Windows belonging to an AppTarget. Strict = class identity only (for anything destructive)."""
    site = _webapp_site(target)
    matches = [w for w in list_windows() if w.classes & target.classes or _shows_site(w, site)]
    if matches or strict:
        return matches
    needle = target.label.lower()
    return [w for w in list_windows() if needle and needle in w.title.lower()]


def _pick_single(matches, prefer_recent=False):
    """
    One window out of several, or None (= ask). The same rule as `targets.pick_single`: a
    tie is broken by the FOCUSED window only when it is one of the candidates. `prefer_recent`
    (focus only — never close) additionally accepts the most recently focused one.
    """
    if len(matches) == 1:
        return matches[0]
    front = active_window()
    if front is not None:
        for window in matches:
            if window.address == front.address:
                return window
    if prefer_recent and matches:
        return min(matches, key=lambda w: w.focus_order)
    return None


def _describe(windows, limit=3):
    return [w.label() for w in windows[:limit]]


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         APPLICATIONS                                   │
# └────────────────────────────────────────────────────────────────────────┘

def _terminal_argv(program=None):
    """How to open the user's terminal, optionally running `program` (a name or an argv list)."""
    command = [] if not program else (list(program) if isinstance(program, (list, tuple))
                                      else [program])
    for launcher in ("xdg-terminal-exec",):
        if _have(launcher):
            return [launcher] + command
    term = os.environ.get("TERMINAL")
    for candidate in ([term] if term else []) + ["alacritty", "foot", "kitty", "ghostty"]:
        if candidate and _have(candidate):
            return [candidate] + (["-e"] + command if command else [])
    return None


def _launch_argv(app):
    if app.entry is not None:
        desktop_file = f"{app.entry.id}.desktop"
        if _have("uwsm-app"):
            return ["uwsm-app", "--", desktop_file]
        if _have("gtk-launch"):
            return ["gtk-launch", app.entry.id]
        return _exec_argv(app.entry.exec)
    if app.tui:
        argv = _terminal_argv(app.tui)
        return (["uwsm-app", "--"] + argv) if argv and _have("uwsm-app") else argv
    if app.canonical == "terminal":
        argv = _terminal_argv()
        return (["uwsm-app", "--"] + argv) if argv and _have("uwsm-app") else argv
    return None


def _exec_argv(exec_line):
    """A desktop entry's Exec= with its field codes removed. Last resort only."""
    try:
        import shlex
        argv = [a for a in shlex.split(exec_line or "") if not re.fullmatch(r"%[a-zA-Z]", a)]
    except ValueError:
        return None
    return argv or None


def open_application(name):
    panel = linux_apps.settings_panel(name, which=RUNNER.which)
    if panel is not None:
        label, argv = panel
        if argv is None:
            return ActionResult.unsupported(f"I don't have a {label} screen on this desktop.")
        if RUNNER.spawn(argv):
            return ActionResult.success(f"Opening {label}.")
        return ActionResult.failure(f"I couldn't open {label}.")
    app = linux_apps.resolve(name)
    if app is None:
        return ActionResult.not_found(f"I couldn't find an app called {name}.")
    label = app.label

    # Already running -> focus it rather than start a second copy.
    running = _windows_for(app, strict=True) if hyprland_ready() else []
    if running:
        window = _pick_single(running, prefer_recent=True)
        if window is not None and focus_window(window):
            return ActionResult.success(f"{label} is open.", address=window.address)

    if not linux_apps.installed(app):
        return ActionResult.not_found(f"{label.title()} doesn't seem to be installed.")
    argv = _launch_argv(app)
    if not argv:
        return ActionResult.unsupported(f"I don't know how to start {label}.")

    before = {w.address for w in list_windows(include_kayra=True)} if hyprland_ready() else None
    if not RUNNER.spawn(argv):
        return ActionResult.failure(f"{label} didn't start.")

    if before is None:
        return ActionResult.success(f"Opening {label}.")
    window = _wait_for_window(app, before)
    if window is not None:
        return ActionResult.success(f"{label} is open.", address=window.address)
    # Launched, but no window appeared in time. Slow apps (Electron, LibreOffice) are normal;
    # say what is true rather than claiming a window we have not seen.
    return ActionResult.success(f"Starting {label}.")


def _wait_for_window(app, before, timeout=None):
    deadline = time.monotonic() + (LAUNCH_TIMEOUT_S if timeout is None else timeout)
    while time.monotonic() < deadline:
        for window in list_windows():
            if window.address in before:
                continue
            if window.classes & app.classes or not app.classes or _shows_site(window, _webapp_site(app)):
                return window
        SLEEP(0.2)
    return None


def close_target(name, all_instances=False):
    name = (name or "").strip()
    if not name:
        return ActionResult.failure("I didn't catch what to close.")
    if not hyprland_ready():
        return ActionResult.unsupported("Closing windows needs the Hyprland session.")
    lowered = name.lower()

    # 1. A website ("close YouTube") is a TAB in a browser window, not a process.
    app = linux_apps.resolve(lowered)
    if targets.looks_like_site(lowered) and not (app and app.entry and app.canonical):
        fragments = [f.lower() for f in targets.site_fragments(lowered)] or [lowered]
        hits = [w for w in list_windows()
                if _is_browser_window(w) and any(f in w.title.lower() for f in fragments)]
        if len(hits) > 1:
            single = _pick_single(hits)
            if single is None:
                return ActionResult.ambiguous(
                    f"I found {len(hits)} windows showing {name}. Which one should I close?",
                    candidates=_describe(hits))
            hits = [single]
        if hits:
            window = hits[0]
            if not focus_window(window):
                return ActionResult.failure("I couldn't bring that browser window forward.")
            send_keys("ctrl+w")
            return ActionResult.success(f"{name.title()} is closed.")
        if app is None:
            return ActionResult.not_found(
                f"I don't see {name} open. If it's in a background tab I can't see it from here.")

    # 2. An application.
    if app is None:
        return ActionResult.not_found(f"I couldn't find {name} open.")
    matches = _windows_for(app, strict=True)
    if not matches:
        # A web app with no window of its own may be open as a TAB ("close WhatsApp" with
        # WhatsApp in a Chrome tab): close that tab — never something inside it.
        site = _webapp_site(app)
        if site:
            try:
                from kayra.automation import web_macros
                if web_macros.BRIDGE.connected():
                    closed = (web_macros.BRIDGE.call("tabs.close", match=site, all=True) or {}).get("closed", 0)
                    if closed:
                        return ActionResult.success(f"{app.label} is closed.", closed=closed)
            except Exception:
                pass
        return ActionResult.not_found(f"{app.label} isn't open.")
    if all_instances:
        for window in matches:
            close_window(window)
        gone = sum(1 for w in matches if wait_until_gone(w.address))
        if gone:
            return ActionResult.success(
                f"Closed {gone} {app.label} window{'s' if gone != 1 else ''}.", closed=gone)
        return ActionResult.failure(f"{app.label} didn't respond, so it's still open.")
    window = _pick_single(matches)
    if window is None:
        return ActionResult.ambiguous(
            f"You have {len(matches)} {app.label} windows open. Which one should I close?",
            candidates=_describe(matches))
    close_window(window)
    if wait_until_gone(window.address):
        return ActionResult.success(f"{app.label} is closed.")
    return ActionResult.failure(f"{app.label} didn't respond, so it's still open.")


def close_current():
    window = active_window()
    if window is None:
        return ActionResult.not_found("Nothing is in front to close.")
    if window.kayra_owned:
        return ActionResult.blocked("That's my own window. Say \"shut down Kayra\" to close me.")
    close_window(window)
    if wait_until_gone(window.address):
        return ActionResult.success("Closed.")
    return ActionResult.failure("That window didn't close.")


def close_all_windows():
    windows = list_windows()
    for window in windows:
        close_window(window)
    gone = sum(1 for w in windows if wait_until_gone(w.address, timeout=1.0))
    return ActionResult.success(f"Closed {gone} window{'s' if gone != 1 else ''}.", closed=gone)


def focus_target(name):
    if not hyprland_ready():
        return ActionResult.unsupported("Switching windows needs the Hyprland session.")
    app = linux_apps.resolve(name)
    if app is not None:
        matches = _windows_for(app, strict=False)
    else:
        needle = (name or "").lower().strip()
        matches = [w for w in list_windows() if needle and needle in w.title.lower()]
    if not matches:
        return ActionResult.not_found(f"I don't see {name} open.")
    window = _pick_single(matches, prefer_recent=True)
    if window is not None and focus_window(window):
        return ActionResult.success("Done.")
    return ActionResult.failure("I couldn't switch to it.")


def restart_app(name):
    result = close_target(name)
    if not result.ok and result.status != Status.NOT_FOUND:
        return result
    SLEEP(0.6)
    return open_application(name)


def _is_browser_window(window):
    # A Chromium `--app` window (an Omarchy web app: WhatsApp, YouTube, X…) is a browser
    # surface too: the Kayra Bridge extension runs inside it, so clicks, typing and scrolling
    # must go into its page rather than to the screen clicker (round 5, H0.1).
    return bool(window.classes & linux_apps.BROWSER_CLASSES) or any(
        linux_apps.is_app_window_class(c) for c in window.classes)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 WINDOW MANAGEMENT & WORKSPACES                         │
# └────────────────────────────────────────────────────────────────────────┘

def window_action(verb):
    if not hyprland_ready():
        return ActionResult.unsupported("Window control needs the Hyprland session.")
    window = active_window()
    if verb in ("minimize_all",):
        # "Show desktop": the first empty workspace is the Hyprland meaning of a clear desktop.
        return workspace_action("workspace", "empty")
    if verb == "alt_tab":
        ok = HYPR.dispatch("hl.dsp.window.cycle_next()", "cyclenext")
        if ok:
            HYPR.dispatch("hl.dsp.window.bring_to_top()", "bringactivetotop")
        return ActionResult.success("Done.") if ok else ActionResult.failure("I couldn't switch.")
    if verb in ("task_view", "action_center", "emoji"):
        return _omarchy_menu(verb)
    if window is None:
        return ActionResult.not_found("No window is in front.")
    if window.kayra_owned and verb in ("minimize",):
        return ActionResult.blocked("I'd rather not hide myself. Use the window controls instead.")

    if verb == "minimize":
        if _have("omarchy-hyprland-window-minimize") and \
                _ok("omarchy-hyprland-window-minimize", window.address):
            # Only works on a floating workspace; check the window actually moved.
            moved = active_window()
            if moved is None or moved.address != window.address:
                return ActionResult.success("Minimized.")
        ok = _dispatch_window("move", f"movetoworkspacesilent special:minimized,{window.selector}",
                              window, workspace="special:minimized", follow=False)
        origin = window.workspace
        if ok and origin:
            remember_undo("window back where it was",
                          lambda: _dispatch_window("move", f"movetoworkspace {origin},"
                                                   f"{window.selector}", window,
                                                   workspace=origin))
        return ActionResult.success("Minimized. It's on the hidden workspace.") if ok else \
            ActionResult.failure("I couldn't minimize it.")
    if verb in ("maximize", "restore"):
        action = "set" if verb == "maximize" else "unset"
        ok = _dispatch_window("fullscreen", "fullscreen 1", window, mode="maximized",
                              action=action)
        undo_action = "unset" if action == "set" else "set"
        if ok:
            remember_undo("window size back", lambda: _dispatch_window(
                "fullscreen", "fullscreen 1", window, mode="maximized", action=undo_action))
        return ActionResult.success("Done.") if ok else ActionResult.failure("That didn't work.")
    if verb == "fullscreen":
        ok = _dispatch_window("fullscreen", "fullscreen 0", window, mode="fullscreen",
                              action="toggle")
        if ok:
            remember_undo("window out of fullscreen", lambda: _dispatch_window(
                "fullscreen", "fullscreen 0", window, mode="fullscreen", action="toggle"))
        return ActionResult.success("Done.") if ok else ActionResult.failure("That didn't work.")
    if verb in ("snap_left", "snap_right"):
        direction = "l" if verb == "snap_left" else "r"
        ok = _dispatch_window("swap", f"swapwindow {direction}", direction=direction)
        return ActionResult.success("Done.") if ok else ActionResult.failure("That didn't work.")
    if verb == "float":
        ok = _dispatch_window("float", f"togglefloating {window.selector}", window,
                              action="toggle")
        if ok:
            remember_undo("window layout back", lambda: _dispatch_window(
                "float", f"togglefloating {window.selector}", window, action="toggle"))
        return ActionResult.success("Floating." if not window.floating else "Tiled.") if ok \
            else ActionResult.failure("That didn't work.")
    if verb == "pin":
        ok = _dispatch_window("pin", f"pin {window.selector}", window)
        if ok:
            remember_undo("pin back", lambda: _dispatch_window("pin", f"pin {window.selector}",
                                                               window))
        return ActionResult.success("Unpinned." if window.pinned else "Pinned on every workspace.") \
            if ok else ActionResult.failure("That didn't work.")
    if verb == "center":
        ok = _dispatch_window("center", "centerwindow", window)
        return ActionResult.success("Centered.") if ok else ActionResult.failure("That didn't work.")
    return ActionResult.unsupported("I can't do that with windows here.")


def _omarchy_menu(verb):
    """Shell surfaces that have no Hyprland equivalent map to Omarchy's own menus."""
    commands = {"task_view": ("omarchy-menu",), "action_center": ("omarchy-menu",),
                "emoji": ("omarchy-launch-walker", "-m", "symbols")}
    argv = commands.get(verb)
    if argv and _have(argv[0]) and RUNNER.spawn(list(argv)):
        return ActionResult.success("Done.")
    return ActionResult.unsupported("There's no equivalent of that on this desktop.")


_WORDS_TO_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                     "seven": 7, "eight": 8, "nine": 9, "ten": 10, "first": 1, "second": 2,
                     "third": 3, "fourth": 4, "fifth": 5}


def _workspace_name(text):
    lowered = (text or "").lower()
    match = re.search(r"\b(\d{1,2})\b", lowered)
    if match:
        return match.group(1)
    for word, number in _WORDS_TO_NUMBERS.items():
        if re.search(rf"\b{word}\b", lowered):
            return str(number)
    if re.search(r"\bnext\b", lowered):
        return "+1"
    if re.search(r"\b(previous|last|back)\b", lowered):
        return "-1"
    if re.search(r"\b(empty|new|free)\b", lowered):
        return "empty"
    return None


def first_empty_workspace():
    """
    The LOWEST-numbered workspace with no window on it. With windows on 1, 2 and 4, that is 3,
    never 5 — "an empty workspace" means the first gap, in order. Kayra's own window counts:
    it is an app on that workspace like any other.
    """
    occupied = set()
    for c in HYPR.clients():
        workspace = c.get("workspace") or {}
        try:
            number = int(workspace.get("id"))
        except (TypeError, ValueError):
            continue
        if number > 0:
            occupied.add(number)
    number = 1
    while number in occupied:
        number += 1
    return str(number)


def _current_workspace():
    data = HYPR.active_workspace() if hasattr(HYPR, "active_workspace") else None
    return str(data.get("id")) if isinstance(data, dict) and data.get("id") is not None else None


def workspace_action(verb, text):
    if not hyprland_ready():
        return ActionResult.unsupported("Workspaces need the Hyprland session.")
    name = _workspace_name(text)
    if not name:
        return ActionResult.failure("Which workspace?")
    empty = name == "empty"
    if empty:
        name = first_empty_workspace()
    spoken = {"+1": "the next workspace", "-1": "the previous workspace"}.get(
        name, f"workspace {name}" + (", the first empty one" if empty else ""))
    previous = _current_workspace()
    if verb == "workspace_move":
        window = active_window()
        if window is None:
            return ActionResult.not_found("No window is in front to move.")
        if window.kayra_owned:
            return ActionResult.blocked("That's my own window; I'll stay where I am.")
        origin = window.workspace
        ok = _dispatch_window("move", f"movetoworkspace {name},{window.selector}", window,
                              workspace=name)
        if ok and origin:
            remember_undo(f"window back to workspace {origin}",
                          lambda: _dispatch_window("move", f"movetoworkspace {origin},"
                                                   f"{window.selector}", window,
                                                   workspace=origin))
        return ActionResult.success(f"Moved it to {spoken}.") if ok else \
            ActionResult.failure("I couldn't move it.")
    ok = _focus_workspace(name)
    if ok and previous and previous != name:
        remember_undo(f"back to workspace {previous}", lambda: _focus_workspace(previous))
    return ActionResult.success(f"Switched to {spoken}.") if ok else \
        ActionResult.failure("I couldn't switch workspaces.")


def _focus_workspace(name):
    return HYPR.dispatch(f"hl.dsp.focus({{ workspace = {HYPR.lua_string(name)} }})",
                         f"workspace {name}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          KEYBOARD (wtype)                              │
# └────────────────────────────────────────────────────────────────────────┘

_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt",
              "win": "logo", "super": "logo", "logo": "logo", "meta": "logo"}
_KEY_NAMES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
              "tab": "Tab", "left": "Left", "right": "Right", "up": "Up", "down": "Down",
              "plus": "plus", "minus": "minus", "space": "space", "backspace": "BackSpace",
              "delete": "Delete", "home": "Home", "end": "End", "pageup": "Prior",
              "pagedown": "Next", "print": "Print"}


def wtype_argv(combo):
    """'ctrl+shift+t' -> ['wtype', '-M', 'ctrl', '-M', 'shift', '-k', 't', '-m', 'shift', '-m', 'ctrl']."""
    parts = [p.strip().lower() for p in str(combo or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = [_MODIFIERS[p] for p in parts[:-1] if p in _MODIFIERS]
    if len(mods) != len(parts) - 1:
        return None
    key = parts[-1]
    if re.fullmatch(r"f\d{1,2}", key):
        key = key.upper()
    else:
        key = _KEY_NAMES.get(key, key)
    argv = ["wtype"]
    for mod in mods:
        argv += ["-M", mod]
    argv += ["-k", key]
    for mod in reversed(mods):
        argv += ["-m", mod]
    return argv


def send_keys(combo):
    """One key chord to the focused window. False when no injector is available."""
    if not _have("wtype"):
        return False
    argv = wtype_argv(combo)
    return bool(argv) and _ok(*argv)


def type_text(text):
    if not text or not _have("wtype"):
        return False
    return _ok("wtype", "-", input_text=str(text), timeout=max(4.0, len(text) / 20))


# Linux translations for the shared HOTKEY_MAP names that mean something else here.
_LINUX_HOTKEYS = {"task manager": None, "run dialog": None}


def hotkey(name, keys):
    if name in _LINUX_HOTKEYS:
        if name == "task manager":
            return open_application("task manager")
        if _have("omarchy-launch-walker") and RUNNER.spawn(["omarchy-launch-walker"]):
            return ActionResult.success("Done.")
        return ActionResult.unsupported("There's no run dialog on this desktop.")
    front = active_window()
    if front is not None and front.kayra_owned and name in ("undo", "redo", "save", "print"):
        return ActionResult.not_found("My own window is in front, so there's nothing to do that to.")
    if send_keys(keys):
        return ActionResult.success("Done.")
    return ActionResult.unsupported("I can't send keystrokes here. Install wtype.")


_BROWSER_KEYS = {"new_tab": "ctrl+t", "close_tab": "ctrl+w", "next_tab": "ctrl+tab",
                 "previous_tab": "ctrl+shift+tab", "reopen_tab": "ctrl+shift+t",
                 "duplicate_tab": None, "refresh": "ctrl+r", "back": "alt+left",
                 "forward": "alt+right", "fullscreen": "f11"}


def browser_nav(action):
    if action not in _BROWSER_KEYS:
        return ActionResult.unsupported(f"I don't know how to {action.replace('_', ' ')}.")
    front = active_window()
    if front is None or not _is_browser_window(front):
        if action == "close_tab":
            return ActionResult.not_found("No browser is in front, so there's no tab to close.")
        browsers = [w for w in list_windows() if _is_browser_window(w)]
        window = _pick_single(browsers, prefer_recent=True) if browsers else None
        if window is None:
            return ActionResult.not_found("No browser window is open.")
        if not focus_window(window):
            return ActionResult.failure("I couldn't bring the browser forward.")
    if action == "duplicate_tab":
        ok = send_keys("alt+d") and send_keys("alt+Return")
    else:
        ok = send_keys(_BROWSER_KEYS[action])
    if not ok:
        return ActionResult.unsupported("I can't send keystrokes here. Install wtype.")
    return ActionResult.success({"new_tab": "New tab.", "close_tab": "Tab closed.",
                                 "reopen_tab": "Reopened.", "refresh": "Refreshed.",
                                 "back": "Went back.", "forward": "Went forward."}
                                .get(action, "Done."))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         MEDIA (MPRIS)                                  │
# └────────────────────────────────────────────────────────────────────────┘

_MPRIS = "org.mpris.MediaPlayer2"
_MPRIS_PATH = "/org/mpris/MediaPlayer2"


def _gdbus(dest, method, *args):
    return _run("gdbus", "call", "--session", "--dest", dest, "--object-path", _MPRIS_PATH,
                "--method", method, *args, timeout=2.0)


def _mpris_players():
    code, out = _run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                     "--object-path", "/org/freedesktop/DBus",
                     "--method", "org.freedesktop.DBus.ListNames", timeout=2.0)
    if code != 0:
        return []
    names = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)
    # Kayra's own headless speech browser can register an MPRIS name too; it is never the
    # target. Identified by the owning PID — the same PID ledger window resolution uses.
    owned = _kayra_pids()
    return [n for n in names if not owned or _bus_pid(n) not in owned]


def _kayra_pids():
    try:
        return set(targets.kayra_owned_pids()) | {os.getpid()}
    except Exception:
        return {os.getpid()}


def _bus_pid(name):
    code, out = _run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                     "--object-path", "/org/freedesktop/DBus",
                     "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", name,
                     timeout=2.0)
    # gdbus prints "(uint32 3958,)" — the number AFTER the type, not the 32 inside it.
    match = re.search(r"\(\s*(?:uint32\s+)?(\d+)\s*,", out) if code == 0 else None
    return int(match.group(1)) if match else None


def _mpris_status(name):
    code, out = _gdbus(name, "org.freedesktop.DBus.Properties.Get",
                       f"{_MPRIS}.Player", "PlaybackStatus")
    match = re.search(r"'(Playing|Paused|Stopped)'", out) if code == 0 else None
    return match.group(1) if match else "Unknown"


def _mpris_pick():
    players = _mpris_players()
    if not players:
        return None
    statuses = {p: _mpris_status(p) for p in players}
    for wanted in ("Playing", "Paused"):
        for player, status in statuses.items():
            if status == wanted:
                return player
    return players[0]


_PLAYERCTL = {"pause": "pause", "resume": "play", "toggle": "play-pause", "next": "next",
              "previous": "previous", "stop": "stop"}
_MPRIS_METHODS = {"pause": "Pause", "resume": "Play", "toggle": "PlayPause", "next": "Next",
                  "previous": "Previous", "stop": "Stop"}


def media_action(verb):
    if _have("playerctl"):
        if _ok("playerctl", _PLAYERCTL[verb]):
            return ActionResult.success("Done.")
        return ActionResult.not_found("Nothing is playing.")
    if _have("gdbus"):
        player = _mpris_pick()
        if player is None:
            return ActionResult.not_found("Nothing is playing.")
        code, _ = _gdbus(player, f"{_MPRIS}.Player.{_MPRIS_METHODS[verb]}")
        return ActionResult.success("Done.") if code == 0 else \
            ActionResult.failure("The player didn't respond.")
    if send_keys({"pause": "XF86AudioPlay", "resume": "XF86AudioPlay", "toggle": "XF86AudioPlay",
                  "next": "XF86AudioNext", "previous": "XF86AudioPrev",
                  "stop": "XF86AudioStop"}[verb]):
        return ActionResult.success("Done.")
    return ActionResult.unsupported("I can't control media here. Install playerctl.")


def _media_status():
    if _have("playerctl"):
        code, out = _run("playerctl", "status", timeout=2.0)
        return out.strip() if code == 0 else None
    if _have("gdbus"):
        player = _mpris_pick()
        return _mpris_status(player) if player else None
    return None


def now_playing():
    if _have("playerctl"):
        code, out = _run("playerctl", "metadata", "--format", "{{artist}}|{{title}}", timeout=2.0)
        if code == 0 and out.strip():
            artist, _, title = out.strip().partition("|")
            return _now_playing_sentence(artist, title)
    elif _have("gdbus"):
        player = _mpris_pick()
        if player:
            code, out = _gdbus(player, "org.freedesktop.DBus.Properties.Get",
                               f"{_MPRIS}.Player", "Metadata")
            title = re.search(r"'xesam:title': <'([^']*)'>", out)
            artist = re.search(r"'xesam:artist': <\['([^']*)'", out)
            if code == 0 and title:
                return _now_playing_sentence(artist.group(1) if artist else "", title.group(1))
    return ActionResult.not_found("Nothing is playing right now.")


def _now_playing_sentence(artist, title):
    title = (title or "").strip()
    if not title:
        return ActionResult.not_found("Nothing is playing right now.")
    artist = (artist or "").strip()
    return ActionResult.success(f"That's {title} by {artist}." if artist else f"That's {title}.")


def play_on_youtube(query):
    """'play X' — the first YouTube result for X, in the browser. Search page if lookup fails."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        import requests
        page = requests.get(url, timeout=4, headers={"User-Agent": "Mozilla/5.0"}).text
        match = re.search(r'"videoId":"([\w-]{11})"', page)
        if match:
            url = f"https://www.youtube.com/watch?v={match.group(1)}"
    except Exception:
        pass
    result = open_url(url)
    if result.ok:
        result.message = f"Playing {query}."
    return result


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  AUDIO, DISPLAY, RADIOS, NOTIFICATIONS                 │
# └────────────────────────────────────────────────────────────────────────┘

_UP = re.compile(r"\b(up|increase|raise|louder|higher|more|brighter)\b")
_DOWN = re.compile(r"\b(down|decrease|lower|reduce|quieter|less|dimmer|dim)\b")


def _number(text):
    match = re.search(r"(\d{1,3})", text or "")
    return max(0, min(100, int(match.group(1)))) if match else None


def _on_off(text):
    lowered = f" {(text or '').lower()} "
    if re.search(r"\b(off|disable|disabled|stop|deactivate)\b", lowered):
        return False
    if re.search(r"\b(on|enable|enabled|start|activate)\b", lowered):
        return True
    return None


def _volume_level():
    code, out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@", timeout=2.0)
    match = re.search(r"Volume:\s*([\d.]+)", out) if code == 0 else None
    return (float(match.group(1)), "MUTED" in out) if match else (None, None)


def volume_action(text):
    from kayra.automation import linux_system
    per_app = linux_system.app_volume(text) if _have("pactl") else None
    if per_app is not None:
        return per_app
    lowered = (text or "").lower()
    sink = "@DEFAULT_AUDIO_SINK@"
    if _have("wpctl"):
        level, muted = _volume_level()
        if level is not None:
            remember_undo("volume back to %d percent" % round(level * 100),
                          lambda: _ok("wpctl", "set-volume", sink, f"{level:.2f}") and
                          _ok("wpctl", "set-mute", sink, "1" if muted else "0"))
    if not _have("wpctl"):
        if _have("pamixer"):
            return _volume_pamixer(lowered)
        return ActionResult.unsupported("I can't control the volume here. Install wireplumber.")
    if "unmute" in lowered:
        return _result(_ok("wpctl", "set-mute", sink, "0"), "Unmuted.", "I couldn't unmute.")
    if "mute" in lowered:
        return _result(_ok("wpctl", "set-mute", sink, "toggle"), "Done.", "I couldn't mute.")
    number = _number(lowered)
    if number is not None and " by " not in f" {lowered} ":
        ok = _ok("wpctl", "set-volume", "-l", "1.0", sink, f"{number}%")
        return _result(ok, f"Volume is at {number} percent.", "I couldn't change the volume.")
    step = number or 10
    if _DOWN.search(lowered):
        ok = _ok("wpctl", "set-volume", sink, f"{step}%-")
    elif _UP.search(lowered):
        ok = _ok("wpctl", "set-volume", "-l", "1.0", sink, f"{step}%+")
    else:
        return ActionResult.success(_volume_sentence())
    return _result(ok, _volume_sentence(), "I couldn't change the volume.")


def _volume_sentence():
    code, out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@", timeout=2.0)
    match = re.search(r"Volume:\s*([\d.]+)", out) if code == 0 else None
    if not match:
        return "Done."
    level = round(float(match.group(1)) * 100)
    return f"Volume is at {level} percent{', muted' if 'MUTED' in out else ''}."


def _volume_pamixer(lowered):
    if "unmute" in lowered:
        return _result(_ok("pamixer", "-u"), "Unmuted.", "I couldn't unmute.")
    if "mute" in lowered:
        return _result(_ok("pamixer", "-t"), "Done.", "I couldn't mute.")
    number = _number(lowered)
    if number is not None and " by " not in f" {lowered} ":
        return _result(_ok("pamixer", "--set-volume", str(number)),
                       f"Volume is at {number} percent.", "I couldn't change the volume.")
    flag = "-d" if _DOWN.search(lowered) else "-i"
    return _result(_ok("pamixer", flag, str(number or 10)), "Done.",
                   "I couldn't change the volume.")


def brightness_action(text):
    if not _have("brightnessctl"):
        return ActionResult.unsupported("I can't change brightness here. Install brightnessctl.")
    lowered = (text or "").lower()
    code, before = _run("brightnessctl", "-m", timeout=2.0)
    match = re.search(r",(\d+)%,", before) if code == 0 else None
    if match:
        previous = match.group(1)
        remember_undo(f"brightness back to {previous} percent",
                      lambda: _ok("brightnessctl", "-q", "set", f"{previous}%"))
    number = _number(lowered)
    if number is not None and " by " not in f" {lowered} ":
        # Never to zero by voice: a black panel cannot show you how to undo it.
        number = max(5, number)
        ok = _ok("brightnessctl", "-q", "set", f"{number}%")
        return _result(ok, f"Brightness is at {number} percent.", "I couldn't change it.")
    step = number or 15
    if _DOWN.search(lowered):
        ok = _ok("brightnessctl", "-q", "--min-value=5%", "set", f"{step}%-")
    else:
        ok = _ok("brightnessctl", "-q", "set", f"+{step}%")
    return _result(ok, _brightness_sentence(), "I couldn't change the brightness.")


def _brightness_sentence():
    code, out = _run("brightnessctl", "-m", timeout=2.0)
    match = re.search(r",(\d+)%,", out) if code == 0 else None
    return f"Brightness is at {match.group(1)} percent." if match else "Done."


def wifi_action(state):
    if not _have("nmcli"):
        return ActionResult.unsupported("I can't control Wi-Fi here. NetworkManager isn't available.")
    _, out = _run("nmcli", "radio", "wifi", timeout=3.0)
    was_on = out.strip() == "enabled"
    if state is None:
        state = not was_on
    ok = _ok("nmcli", "radio", "wifi", "on" if state else "off", timeout=6.0)
    if ok and was_on != state:
        remember_undo(f"Wi-Fi back {'on' if was_on else 'off'}",
                      lambda: _ok("nmcli", "radio", "wifi", "on" if was_on else "off",
                                  timeout=6.0))
    return _result(ok, f"Wi-Fi is {'on' if state else 'off'}.", "I couldn't change Wi-Fi.")


def bluetooth_action(text):
    if not _have("bluetoothctl"):
        return ActionResult.unsupported("Bluetooth isn't available here.")
    state = _on_off(text)
    _, out = _run("bluetoothctl", "show", timeout=3.0)
    was_on = "Powered: yes" in out
    if state is None:
        state = not was_on
    ok = _ok("bluetoothctl", "power", "on" if state else "off", timeout=6.0)
    if ok and was_on != state:
        remember_undo(f"Bluetooth back {'on' if was_on else 'off'}",
                      lambda: _ok("bluetoothctl", "power", "on" if was_on else "off",
                                  timeout=6.0))
    return _result(ok, f"Bluetooth is {'on' if state else 'off'}.", "I couldn't change Bluetooth.")


def dnd_action(text):
    lowered = (text or "").lower()
    state = _on_off(lowered)
    if state is not None and "notification" in lowered and "disturb" not in lowered:
        state = not state            # "turn off notifications" means do-not-disturb ON
    if _have("omarchy-toggle-notification-silencing"):
        ok = _ok("omarchy-toggle-notification-silencing", timeout=4.0)
        if ok:
            remember_undo("notifications the way they were",
                          lambda: _ok("omarchy-toggle-notification-silencing", timeout=4.0))
        return _result(ok, "Do not disturb toggled.", "I couldn't change notifications.")
    if _have("makoctl"):
        if state is None:
            ok = _ok("makoctl", "mode", "-t", "do-not-disturb")
        else:
            ok = _ok("makoctl", "mode", "-a" if state else "-r", "do-not-disturb")
        return _result(ok, "Do not disturb is on." if state else
                       ("Do not disturb is off." if state is False else "Done."),
                       "I couldn't change notifications.")
    return ActionResult.unsupported("I can't change notification settings here.")


def night_light_action(text):
    state = _on_off(text)
    if _have("omarchy-toggle-nightlight"):
        if state is not None:
            code, out = _run("omarchy-toggle-nightlight", "--status", timeout=3.0)
            try:
                if code == 0 and bool(json.loads(out).get("enabled")) == state:
                    return ActionResult.success(f"Night light is already {'on' if state else 'off'}.")
            except ValueError:
                pass
        ok = _ok("omarchy-toggle-nightlight", timeout=6.0)
        if ok:
            remember_undo("night light the way it was",
                          lambda: _ok("omarchy-toggle-nightlight", timeout=6.0))
        return _result(ok, "Night light toggled." if state is None else
                       f"Night light is {'on' if state else 'off'}.", "I couldn't change it.")
    if _have("hyprctl"):
        ok = _ok("hyprctl", "hyprsunset", "temperature", "4000" if state is not False else "6500")
        return _result(ok, "Done.", "I couldn't change it.")
    return ActionResult.unsupported("Night light isn't available here.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     UNDO OF KAYRA'S OWN ACTIONS                        │
# └────────────────────────────────────────────────────────────────────────┘
# Every REVERSIBLE action records how to put things back. "undo that" pops the newest. Bounded
# (UNDO_LIMIT) and short-lived (UNDO_TTL_S): an undo for something done ten minutes ago is a
# surprise, not a convenience. Irreversible actions (closing, deleting, typing) record nothing,
# so "undo" never pretends it can bring them back.

UNDO_LIMIT = 10
UNDO_TTL_S = 300.0
_UNDO = collections.deque(maxlen=UNDO_LIMIT)


def remember_undo(description, revert):
    _UNDO.append((time.monotonic(), description, revert))


def undo_last():
    now = time.monotonic()
    while _UNDO:
        stamp, description, revert = _UNDO.pop()
        if now - stamp > UNDO_TTL_S:
            continue
        try:
            ok = revert()
        except Exception:
            ok = False
        return ActionResult.success(f"Undone. I put the {description}.") if ok is not False \
            else ActionResult.failure(f"I couldn't put the {description}.")
    return ActionResult.not_found("There's nothing recent of mine to undo.")


def _result(ok, success, failure):
    return ActionResult.success(success) if ok else ActionResult.failure(failure)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SESSION & POWER (CONFIRM-GATED)                    │
# └────────────────────────────────────────────────────────────────────────┘
# Reached only after `policy.classify_action` returned CONFIRM *and* the user said yes, and —
# for the four power verbs — only when `resolve_power_target` named the COMPUTER.

def lock_screen():
    for argv in (("omarchy-system-lock",), ("loginctl", "lock-session"), ("hyprlock",)):
        if _have(argv[0]) and (RUNNER.spawn(list(argv)) if argv[0] == "hyprlock" else _ok(*argv)):
            return ActionResult.success("Locking.")
    return ActionResult.failure("I couldn't lock the screen.")


def power_action(verb, text=""):
    # "shut down the computer in 30 minutes" — confirmed already; schedule it, cancellable.
    delay = re.search(r"\bin (\d+) (minutes?|mins?|hours?)", (text or "").lower())
    if delay:
        seconds = int(delay.group(1)) * (3600 if delay.group(2).startswith("hour") else 60)
        word = {"shutdown": "shut down", "restart": "restart", "sleep": "sleep",
                "sign_out": "sign out"}[verb]
        timer_id, error = RUNTIME.timers.add(
            seconds, f"scheduled {word}",
            on_fire=lambda _l: (common.announce(f"Time's up — I'll {word} now.", urgent=True),
                                power_action(verb)))
        if timer_id is None:
            return ActionResult.failure(error)
        return ActionResult.success(f"I'll {word} the computer in {_humanize_duration(seconds)}. "
                                    "Say cancel timer to stop it.")
    if verb == "sleep" and "hibernat" in (text or "").lower():
        return _result(_ok("systemctl", "hibernate"), "Hibernating.", "Hibernate isn't set up "
                       "on this machine.")
    if verb == "shutdown":
        return _result(_ok("systemctl", "poweroff"), "Shutting down.", "The shutdown was refused.")
    if verb == "restart":
        return _result(_ok("systemctl", "reboot"), "Restarting.", "The restart was refused.")
    if verb == "sleep":
        return _result(_ok("systemctl", "suspend"), "Going to sleep.", "Suspend was refused.")
    if verb == "sign_out":
        if _have("uwsm") and _ok("uwsm", "stop"):
            return ActionResult.success("Signing out.")
        ok = HYPR.dispatch("hl.dsp.exit()", "exit")
        return _result(ok, "Signing out.", "I couldn't sign you out.")
    return ActionResult.unsupported("I don't know that power action.")


def screen_off():
    """
    Blank the display — but only in a way the user can get back from. Hyprland does NOT wake
    a DPMS-off monitor on input unless `misc:mouse_move_enables_dpms` / `key_press_enables_dpms`
    is set, so a bare `dpms off` can leave someone looking at a black screen with no way back
    short of a TTY. Without those options this locks instead (Omarchy's lock also blanks the
    display), which has a well-known way back: unlock.
    """
    wakes = any(_hypr_option(name) for name in ("misc:mouse_move_enables_dpms",
                                                 "misc:key_press_enables_dpms"))
    if wakes:
        ok = HYPR.dispatch('hl.dsp.dpms({ action = "disable" })', "dpms off")
        return _result(ok, "Screen off. Move the mouse to wake it.",
                       "I couldn't turn the screen off.")
    result = lock_screen()
    if result.ok:
        result.message = "Locking, which turns the screen off."
    return result


def _hypr_option(name):
    data = HYPR.request_json(f"getoption {name}")
    if isinstance(data, dict):
        for key in ("int", "bool"):
            if key in data:
                return bool(data[key])
    return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SCREEN, CLIPBOARD, MOUSE, WEB                      │
# └────────────────────────────────────────────────────────────────────────┘

def screenshot_folder():
    home = os.path.expanduser("~")
    configured = os.environ.get("OMARCHY_SCREENSHOT_DIR") or os.environ.get("XDG_PICTURES_DIR")
    for candidate in (configured, os.path.join(home, "Pictures", "Screenshots"),
                      os.path.join(home, "Pictures")):
        if candidate and os.path.isdir(candidate):
            return candidate
    return home


def take_screenshot(region=False):
    if not _have("grim"):
        return ActionResult.unsupported("I can't take screenshots here. Install grim.")
    folder = screenshot_folder()
    path = os.path.join(folder, f"KAYRA_Screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
    argv = ["grim"]
    if region:
        if not _have("slurp"):
            return ActionResult.unsupported("Selecting a region needs slurp.")
        code, geometry = _run("slurp", timeout=30.0)
        if code != 0 or not geometry.strip():
            return ActionResult.failure("Screenshot cancelled.")
        argv += ["-g", geometry.strip()]
    if not _ok(*argv, path, timeout=8.0) or not os.path.exists(path):
        return ActionResult.failure("The screenshot didn't save.")
    copied = _have("wl-copy") and \
        _run("wl-copy", "--type", "image/png", timeout=3.0, stdin_path=path)[0] == 0
    prune_screenshots(folder, SCREENSHOT_KEEP)
    return ActionResult.success("Screenshot saved and copied." if copied else "Screenshot saved.",
                                path=path)


def clipboard_set(text):
    if _have("wl-copy"):
        return _ok("wl-copy", "--", str(text))
    try:
        import pyperclip
        pyperclip.copy(str(text))
        return True
    except Exception:
        return False


def clipboard_get():
    if _have("wl-paste"):
        code, out = _run("wl-paste", "--no-newline", timeout=2.0)
        return out if code == 0 else ""
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


def clipboard_clear():
    return _ok("wl-copy", "--clear") if _have("wl-copy") else clipboard_set("")


POINTER = None          # the test suite installs a recording pointer here
VISION_CLICKS = True    # fall back to the vision model when OCR cannot find the words
_UINPUT_HELP = ("clicking needs access to /dev/uinput. "
                "docs/platforms/LINUX.md has the one-line udev rule.")


def _pointer():
    if POINTER is not None:
        return POINTER
    try:
        from kayra import platforms
        injector, _detail = platforms.pointer_injector()
        return injector
    except Exception:
        return None


def _can_click(injector):
    try:
        return bool(injector) and bool(injector.can_click())
    except Exception:
        return False


def _press(injector, verb):
    left, right = (0x0002, 0x0004), (0x0008, 0x0010)
    if verb in ("click", "click_text", "click_at"):
        injector.click(*left)
    elif verb in ("double_click", "double_click_text"):
        injector.click(*left)
        SLEEP(0.06)
        injector.click(*left)
    elif verb in ("right_click", "right_click_text"):
        injector.click(*right)
    elif verb == "scroll_up":
        injector.wheel(3)
    elif verb == "scroll_down":
        injector.wheel(-3)
    else:
        return False
    return True


def _browser_in_front():
    """True when Chrome is the focused window AND the Kayra Bridge extension is connected."""
    try:
        from kayra.automation import web_macros
        front = active_window()
        return bool(front is not None and _is_browser_window(front) and web_macros.BRIDGE.connected())
    except Exception:
        return False


def _is_open_chat(target, W):
    """'close the group' with WhatsApp in front and no tab group at all: the open chat."""
    try:
        return (str(target).lower() in ("current", "the", "this", "that", "whatsapp")
                and common.front_site(situation) == "web.whatsapp.com" and not W._groups())
    except Exception:
        return False


def _page_scroll(params):
    """A scroll with a direction / place / amount. Into the page when a browser (or Kayra's own
    window, while the user talks to it) is in front; with real keys when another app is, so
    "scroll to the bottom" in a terminal or a PDF viewer does not move a hidden Chrome tab."""
    from kayra.automation import web_macros
    direction = params.get("direction", "down")
    front = None
    try:
        front = active_window()
    except Exception:
        pass
    elsewhere = front is not None and not front.kayra_owned and not _is_browser_window(front)
    if params.get("until") or (web_macros.BRIDGE.connected() and not elsewhere):
        return web_macros.page_scroll(direction, params.get("until"), params.get("where", ""),
                                      params.get("amount", 1.0))
    if not elsewhere:
        return web_macros.page_scroll(direction)             # says how to set the bridge up
    if direction in ("up", "down"):
        return mouse_action(f"scroll_{direction}")
    ok = send_keys("ctrl+home" if direction == "top" else "ctrl+end")
    return ActionResult.success("Done.") if ok else ActionResult.failure("I couldn't scroll that window.")


def mouse_action(verb, target=None):
    # A click or scroll with Chrome in front goes INSIDE the page through the extension: it
    # needs no screenshot, no OCR and no /dev/uinput, and it can click things a screen
    # reader can name but OCR cannot read (icons, collapsed menus, rows).
    if _browser_in_front():
        from kayra.automation import web_macros
        if verb in ("click_text", "double_click_text", "right_click_text") and target:
            nth, label = common._ordinal(str(target))
            label = re.sub(r"\s+(button|link|icon|tab|option)$", "", label.strip())
            result = web_macros.page_click(label, nth)
            if result.status != Status.NOT_FOUND:
                return result
            if not _can_click(_pointer()):
                return result                       # the screen clicker could not click either
        elif verb in ("scroll_up", "scroll_down"):
            return web_macros.page_scroll("up" if verb == "scroll_up" else "down")
    if verb in ("click_text", "double_click_text", "right_click_text"):
        return click_on_text(target, verb)
    if verb in ("click_at", "move_to"):
        return click_at(target, click=(verb == "click_at"))
    injector = _pointer()
    if not _can_click(injector):
        return ActionResult.unsupported("Mouse " + _UINPUT_HELP)
    try:
        if not _press(injector, verb):
            return ActionResult.unsupported("I don't know that mouse action.")
        return ActionResult.success("Done.")
    except Exception as e:
        return ActionResult.failure("The mouse action didn't go through.", error=str(e))


def _focused_monitor():
    monitors = HYPR.monitors() if hasattr(HYPR, "monitors") else []
    return next((m for m in monitors if m.get("focused")), monitors[0] if monitors else None)


def _move_pointer(x, y):
    return HYPR.dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})",
                         f"movecursor {int(x)} {int(y)}")


_NAMED_SPOTS = {"center": (0.5, 0.5), "centre": (0.5, 0.5), "middle": (0.5, 0.5),
                "top left": (0.05, 0.05), "top right": (0.95, 0.05),
                "bottom left": (0.05, 0.95), "bottom right": (0.95, 0.95)}


def click_at(text, click=True):
    """'click at 400 300' (logical pixels on the focused screen) or 'move mouse to center'."""
    monitor = _focused_monitor()
    if monitor is None:
        return ActionResult.unsupported("Pointer control needs the Hyprland session.")
    scale = float(monitor.get("scale") or 1.0)
    width, height = monitor.get("width", 0) / scale, monitor.get("height", 0) / scale
    lowered = (text or "").lower().strip(" .")
    numbers = [int(n) for n in re.findall(r"\d{1,5}", lowered)]
    spot = next((v for k, v in _NAMED_SPOTS.items() if k in lowered), None)
    if len(numbers) >= 2:
        x, y = numbers[0], numbers[1]
        if not (0 <= x <= width and 0 <= y <= height):
            return ActionResult.failure("That point is off the screen.")
    elif spot:
        x, y = width * spot[0], height * spot[1]
    elif not click and lowered:
        return click_on_text(text, "move_to_text")
    else:
        return ActionResult.failure("Where? Give me two numbers, like 400 300.")
    gx, gy = monitor.get("x", 0) + x, monitor.get("y", 0) + y
    if not _move_pointer(gx, gy):
        return ActionResult.failure("I couldn't move the pointer.")
    if not click:
        return ActionResult.success("Done.")
    injector = _pointer()
    if not _can_click(injector):
        return ActionResult.unsupported("I moved the pointer there, but " + _UINPUT_HELP)
    _press(injector, "click")
    return ActionResult.success("Clicked.")


def _norm_word(word):
    return re.sub(r"[^\w]+", "", str(word).lower())


def ocr_words(path):
    """Words tesseract sees in an image: [(text, conf, left, top, width, height, line_key)]."""
    code, out = _run("tesseract", path, "stdout", "--psm", "11", "tsv", timeout=20.0)
    if code != 0:
        return []
    words = []
    for line in out.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
            left, top, width, height = (int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]))
        except ValueError:
            continue
        words.append((cols[11].strip(), conf, left, top, width, height,
                      (cols[2], cols[3], cols[4])))
    return words


def find_text(words, phrase, min_conf=50.0):
    """Every place the whole phrase appears on one OCR line, as (cx, cy, box) in image px."""
    wanted = [w for w in (_norm_word(t) for t in str(phrase).split()) if w]
    if not wanted:
        return []
    lines = collections.OrderedDict()
    for word in words:
        lines.setdefault(word[6], []).append(word)
    hits = []
    for line in lines.values():
        line = sorted(line, key=lambda w: w[2])
        for i in range(len(line) - len(wanted) + 1):
            window = line[i:i + len(wanted)]
            if [_norm_word(w[0]) for w in window] != wanted:
                continue
            if sum(w[1] for w in window) / len(window) < min_conf:
                continue
            left = min(w[2] for w in window)
            top = min(w[3] for w in window)
            right = max(w[2] + w[4] for w in window)
            bottom = max(w[3] + w[5] for w in window)
            hits.append(((left + right) / 2, (top + bottom) / 2, (left, top, right, bottom)))
    return hits


def click_on_text(phrase, verb="click_text"):
    """
    "click on Save": screenshot the focused screen, read it with OCR, find the EXACT words,
    and click their centre. Never clicks something inside Kayra's own window (its chat shows
    the very words you just said), and asks when the words appear more than once.
    """
    nth, phrase = common._ordinal((phrase or "").strip().strip(".\"'"))
    phrase = re.sub(r"\s+(button|link|icon|tab|option)$", "", phrase.strip())
    if not phrase:
        return ActionResult.failure("Click on what?")
    if not (_have("grim") and _have("tesseract")):
        return ActionResult.unsupported("Clicking on text needs grim and tesseract.")
    monitor = _focused_monitor()
    if monitor is None:
        return ActionResult.unsupported("Clicking on text needs the Hyprland session.")
    import tempfile
    # PPM, not PNG: this file is read once by tesseract and deleted, so compressing it is pure
    # cost (measured 151 ms -> 35 ms for a 2880x1800 capture; the OCR itself is ~1.2 s).
    handle, path = tempfile.mkstemp(prefix="kayra-ocr-", suffix=".ppm")
    os.close(handle)
    try:
        if not _ok("grim", "-o", str(monitor.get("name")), "-t", "ppm", path, timeout=6.0):
            return ActionResult.failure("I couldn't capture the screen.")
        words = ocr_words(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    scale = float(monitor.get("scale") or 1.0)
    origin_x, origin_y = monitor.get("x", 0), monitor.get("y", 0)
    hits = [(origin_x + cx / scale, origin_y + cy / scale)
            for cx, cy, _ in find_text(words, phrase)]

    def inside(point, c):
        (x, y), (wx, wy), (ww, wh) = point, c.get("at", (0, 0)), c.get("size", (0, 0))
        return wx <= x <= wx + ww and wy <= y <= wy + wh
    clients = HYPR.clients()
    kayra = [c for c in clients if Window(c).kayra_owned]
    hits = [h for h in hits if not any(inside(h, c) for c in kayra)]
    if not hits and VISION_CLICKS:
        # Nothing readable says it — an icon, a coloured button. Ask the vision model.
        from kayra.automation import screen_ai
        spot = screen_ai.vision_locate(phrase)
        if spot is not None:
            width, height = monitor.get("width", 0) / scale, monitor.get("height", 0) / scale
            hits = [(origin_x + spot[0] * width, origin_y + spot[1] * height)]
            hits = [h for h in hits if not any(inside(h, c) for c in kayra)]
    if not hits:
        return ActionResult.not_found(f"I can't see \"{phrase}\" on the screen.")
    if nth != 1 and len(hits) > 1:
        ordered = sorted(hits, key=lambda h: (round(h[1] / 12), h[0]))   # reading order
        hits = [ordered[-1] if nth == -1 else ordered[min(nth, len(ordered)) - 1]]
    if len(hits) > 1:
        front = HYPR.active_client()
        mine = [h for h in hits if front and inside(h, front)]
        if len(mine) == 1:
            hits = mine
        else:
            return ActionResult.ambiguous(
                f"I can see \"{phrase}\" {len(hits)} times. Which one?",
                candidates=[f"{int(x)},{int(y)}" for x, y in hits[:5]])
    x, y = hits[0]
    if not _move_pointer(x, y):
        return ActionResult.failure("I couldn't move the pointer.")
    if verb == "move_to_text":
        return ActionResult.success("Pointing at it.")
    injector = _pointer()
    if not _can_click(injector):
        return ActionResult.unsupported(f"I'm pointing at {phrase}, but " + _UINPUT_HELP)
    _press(injector, verb)
    return ActionResult.success({"double_click_text": "Double-clicked.",
                                 "right_click_text": "Right-clicked."}.get(verb, "Clicked."))


def _open_site_app(name, url):
    """An installed native app named `name`, else the web app serving `url`; None if neither."""
    try:
        app = linux_apps.resolve(name)
        if app is not None and linux_apps.installed(app) and \
                not (app.classes & linux_apps.BROWSER_CLASSES):
            return open_application(name)
        entry = linux_apps.webapp_for_url(url)
        if entry is not None:
            return open_application(entry.name)
    except Exception:
        return None
    return None


_CHROMIUM_BINARIES = ("google-chrome-stable", "google-chrome", "chromium", "brave",
                      "microsoft-edge-stable", "vivaldi-stable")


def _browser_binary(name=None):
    """The browser binary for 'chrome' / 'chromium' / …, or the first installed one for None."""
    wanted = str(name or "").lower().replace("google ", "")
    if name is not None:
        app = linux_apps.resolve(name)
        if app is None or not (app.classes & linux_apps.BROWSER_CLASSES):
            return None
    for binary in _CHROMIUM_BINARIES:
        if (not wanted or wanted.split()[0] in binary) and _have(binary):
            return binary
    return next((b for b in _CHROMIUM_BINARIES if _have(b)), None)


def open_browser_window(binary, where, url=None):
    """A new (or incognito) browser window, by argv. Used without the extension."""
    flag = "--incognito" if where == "incognito" else "--new-window"
    argv = [binary, flag] + ([url] if url else [])
    if RUNNER.spawn(argv):
        return ActionResult.success("Opened an incognito window." if where == "incognito"
                                    else "Opened a new window.", url=url)
    return ActionResult.failure("The browser didn't start.")


def open_url(url):
    if not url:
        return ActionResult.failure("Open what?")
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    if _have("xdg-open") and RUNNER.spawn(["xdg-open", target]):
        return ActionResult.success("Opening that page.", url=target)
    try:
        webbrowser.open(target, new=2)
        return ActionResult.success("Opening that page.", url=target)
    except Exception as e:
        return ActionResult.failure("I couldn't open that link.", error=str(e))


def open_path(target, kind="any"):
    resolution = targets.resolve_path(target, kind=kind)
    if not resolution.ok:
        return ActionResult.not_found(resolution.reason or f"I couldn't find {target}.")
    path = resolution.target
    if _have("xdg-open") and RUNNER.spawn(["xdg-open", path]):
        return ActionResult.success("Done.", path=path)
    return ActionResult.failure(f"I couldn't open {target}.")


def write_content(topic):
    """'content X' — draft with the shared LLM engine, save to Documents, open in the editor."""
    try:
        from kayra.intelligence.llm_engine import CentralizedLLMEngine
        engine = CentralizedLLMEngine()
        text = "".join(engine.generate_chat_stream(
            [{"role": "system", "content": "You are a professional writer and programmer. Write "
                                           "ready-to-use text or code. No markdown fences, "
                                           "no conversational filler."},
             {"role": "user", "content": str(topic)}]) or [])
    except Exception as e:
        return ActionResult.failure("I couldn't write that right now.", error=str(e))
    if not text.strip():
        return ActionResult.failure("I couldn't write that right now.")
    folder = os.path.join(os.path.expanduser("~"), "Documents", "Kayra")
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", str(topic).lower()).strip("_")[:40] or "draft"
    path = os.path.join(folder, f"{slug}.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if _have("xdg-open"):
        RUNNER.spawn(["xdg-open", path])
    return ActionResult.success("Written and opened.", path=path)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SYSTEM INFORMATION                            │
# └────────────────────────────────────────────────────────────────────────┘

def system_info(query):
    """One spoken sentence answering a question about THIS machine. Read-only."""
    handler = _INFO_HANDLERS.get(query)
    if handler is not None:
        try:
            return handler()
        except Exception as e:                 # a sensor that misbehaves must not break a turn
            return f"I couldn't read that ({type(e).__name__})."
    if psutil is None:
        return "I can't read system information without psutil."
    if query in ("cpu", "processor"):
        load = psutil.cpu_percent(interval=0.1)
        return f"C P U is at {load:.0f} percent across {psutil.cpu_count()} threads."
    if query in ("ram", "memory"):
        memory = psutil.virtual_memory()
        return (f"Memory is at {memory.percent:.0f} percent, "
                f"{memory.available / 1024 ** 3:.1f} gigabytes free.")
    if query in ("disk", "storage"):
        usage = psutil.disk_usage("/")
        return (f"Your system disk is {usage.percent:.0f} percent full, "
                f"{usage.free / 1024 ** 3:.0f} gigabytes free.")
    if query == "uptime":
        seconds = int(time.time() - psutil.boot_time())
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        return (f"Up for {days} days, {hours} hours." if days
                else f"Up for {hours} hours and {rest // 60} minutes.")
    if query in ("network", "ip"):
        for name, addresses in psutil.net_if_addrs().items():
            stats = psutil.net_if_stats().get(name)
            if name == "lo" or (stats and not stats.isup):
                continue
            for entry in addresses:
                if entry.family == socket.AF_INET and not entry.address.startswith("127."):
                    return f"Your I P address is {entry.address}."
        return "I couldn't find an active network connection."
    return "I'm not sure what to check."


def _minutes(total):
    hours, minutes = divmod(int(total), 60)
    if hours and minutes:
        return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minutes"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minutes"


def battery_sentence(status=None):
    """Percent, state (charging / discharging / plugged in but idle / full), rate, time, health."""
    from kayra import platforms
    status = status if status is not None else platforms.battery_status()
    if status is None:
        battery = psutil.sensors_battery() if psutil else None
        if battery is None:
            return "I don't see a battery on this machine."
        state = "charging" if battery.power_plugged else "discharging"
        return f"Battery is at {int(battery.percent)} percent and {state}."
    percent, state = status.get("percent"), status.get("state")
    parts = [f"Battery is at {percent} percent" if percent is not None else "Battery"]
    if state == "charging":
        parts.append("and charging")
        if status.get("watts"):
            parts[-1] += f" at {status['watts']:.0f} watts"
        if status.get("minutes_to_full"):
            parts.append(f"about {_minutes(status['minutes_to_full'])} until full")
    elif state == "discharging":
        parts.append("and running on battery")
        if status.get("watts"):
            parts[-1] += f", drawing {status['watts']:.0f} watts"
        if status.get("minutes_to_empty"):
            parts.append(f"about {_minutes(status['minutes_to_empty'])} left")
    elif state == "full":
        parts.append("and fully charged")
    elif state == "not charging":
        # Plugged in and idle: firmware charge limits and a full-enough cell both look like this.
        parts.append("plugged in but idle, not charging")
    else:
        parts.append("plugged in" if status.get("ac_online") else "on battery")
    sentence = ", ".join(parts[:1] + parts[1:2]) + "".join(f", {p}" for p in parts[2:]) + "."
    sentence = sentence.replace(" percent, and", " percent and").replace(" percent, plugged",
                                                                        " percent, plugged")
    health = status.get("health_percent")
    if health is not None and health < 100:
        sentence += f" Its health is {health} percent of the original capacity."
    if status.get("cycles"):
        sentence += f" {status['cycles']} charge cycles."
    return sentence


def temperature_sentence():
    readings = psutil.sensors_temperatures() if psutil and hasattr(psutil, "sensors_temperatures") else {}
    cpu = None
    for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        for entry in readings.get(chip, []):
            if entry.current and (cpu is None or "package" in (entry.label or "").lower()
                                  or "tctl" in (entry.label or "").lower()):
                cpu = entry.current
        if cpu is not None:
            break
    gpu = None
    if _have("nvidia-smi"):
        code, out = _run("nvidia-smi", "--query-gpu=temperature.gpu",
                         "--format=csv,noheader,nounits", timeout=3.0)
        if code == 0 and out.strip().splitlines()[0].strip().isdigit():
            gpu = int(out.strip().splitlines()[0])
    if cpu is None and gpu is None:
        return "I can't read any temperature sensors here."
    parts = []
    if cpu is not None:
        parts.append(f"the processor is at {cpu:.0f} degrees")
    if gpu is not None:
        parts.append(f"the graphics card is at {gpu} degrees")
    hot = (cpu or 0) >= 90 or (gpu or 0) >= 85
    return ("Right now " + " and ".join(parts) + "." +
            (" That's running hot." if hot else ""))


def wifi_sentence():
    if not _have("nmcli"):
        return "I can't see the network settings here."
    # --rescan no: answer from the last scan. A rescan takes several seconds and is not what
    # "which Wi-Fi am I on?" is asking. Terse output escapes ':' inside an SSID as '\:'.
    code, out = _run("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list",
                     "--rescan", "no", timeout=4.0)
    if code == 0:
        for line in out.splitlines():
            fields = [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]
            if fields and fields[0] == "yes" and len(fields) >= 3:
                return f"You're connected to {fields[1] or 'a hidden network'}, signal {fields[2]} percent."
    _, radio = _run("nmcli", "radio", "wifi", timeout=3.0)
    if radio.strip() == "disabled":
        return "Wi-Fi is turned off."
    return "You're not connected to a Wi-Fi network."


def bluetooth_devices_sentence():
    if not _have("bluetoothctl"):
        return "Bluetooth isn't available here."
    code, out = _run("bluetoothctl", "devices", "Connected", timeout=4.0)
    names = [line.split(" ", 2)[2] for line in out.splitlines()
             if line.startswith("Device ") and len(line.split(" ", 2)) == 3] if code == 0 else []
    if not names:
        return "No Bluetooth devices are connected."
    return "Connected: " + ", ".join(names[:4]) + "."


def displays_sentence():
    monitors = HYPR.monitors() if hasattr(HYPR, "monitors") else []
    if not monitors:
        return "I can't read the display layout here."
    parts = []
    for m in monitors:
        rate = round(float(m.get("refreshRate") or 0))
        parts.append(f"{m.get('name')} at {m.get('width')} by {m.get('height')}"
                     + (f", {rate} hertz" if rate else "")
                     + (f", scale {m.get('scale'):g}" if m.get("scale") not in (None, 1, 1.0) else ""))
    noun = "display" if len(parts) == 1 else "displays"
    return f"{len(parts)} {noun}: " + "; ".join(parts) + "."


def summary_sentence():
    from kayra.core import hardware
    try:
        os_name = hardware.os_info().display_name
    except Exception:
        os_name = None
    try:
        cpu = hardware.cpu_info().model
    except Exception:
        cpu = None
    try:
        gpu = hardware.primary_gpu()
        gpu = gpu.name if gpu is not None else None
    except Exception:
        gpu = None
    memory = psutil.virtual_memory().total / 1024 ** 3 if psutil else None
    parts = []
    if os_name:
        parts.append(f"running {os_name}")
    if cpu:
        parts.append(f"with an {cpu}" if cpu[:1].lower() in "aeiou" else f"with a {cpu}")
    if gpu:
        parts.append(f"an {gpu}" if gpu[:1].lower() in "aeiou" else f"a {gpu}")
    if memory:
        parts.append(f"{memory:.0f} gigabytes of memory")
    if not parts:
        return "I couldn't read this computer's details."
    return "This computer is " + ", ".join(parts) + ". " + battery_sentence().split(".")[0] + "."


_INFO_HANDLERS = {"battery": lambda: battery_sentence(), "temperature": lambda: temperature_sentence(),
                  "wifi_info": lambda: wifi_sentence(),
                  "bt_devices": lambda: bluetooth_devices_sentence(),
                  "displays": lambda: displays_sentence(), "summary": lambda: summary_sentence()}


def power_profile_action(text):
    if not _have("powerprofilesctl"):
        return ActionResult.unsupported("Power profiles aren't available here.")
    lowered = (text or "").lower()
    wanted = ("power-saver" if re.search(r"saver|saving|low|quiet|eco", lowered) else
              "performance" if re.search(r"performance|fast|max|turbo|high", lowered) else
              "balanced" if "balanced" in lowered or "normal" in lowered else None)
    code, out = _run("powerprofilesctl", "get", timeout=3.0)
    current = out.strip() if code == 0 else None
    if wanted is None:
        return ActionResult.success(f"The power profile is {current.replace('-', ' ')}."
                                    if current else "I couldn't read the power profile.")
    if wanted == current:
        return ActionResult.success(f"It's already on {wanted.replace('-', ' ')}.")
    if not _ok("powerprofilesctl", "set", wanted, timeout=4.0):
        return ActionResult.failure("I couldn't change the power profile.")
    if current:
        remember_undo(f"power profile back to {current}",
                      lambda: _ok("powerprofilesctl", "set", current, timeout=4.0))
    return ActionResult.success(f"Power profile set to {wanted.replace('-', ' ')}.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              DISPATCH                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _dispatch(action):
    domain, verb = action.domain, action.action
    target, params = action.target, action.parameters

    if domain == "window":
        if verb == "focus":
            return focus_target(target)
        if verb == "close":
            return close_current()
        if verb == "close_all":
            return close_all_windows()
        return window_action(verb)

    if domain == "app":
        if verb == "open":
            if not target:
                return ActionResult.failure("Open what?")
            where = params.get("where")
            if where in ("window", "incognito") and _browser_binary(target):
                return open_browser_window(_browser_binary(target), where)
            if params.get("web") or where:
                # The user said where in the BROWSER it should go ("in a new tab", "in this
                # tab"), so this is a page, whatever else the name could mean.
                from kayra.automation import web_macros
                return web_macros.open_site(target, None if where == "browser" else where)
            result = open_application(target)
            if result.status == Status.NOT_FOUND:
                # The Windows order, kept: a named user folder or a literal path is the LAST
                # reading of "open X", never the first.
                path = targets.resolve_path(target)
                if path.ok:
                    return open_path(target)
                # No app and no folder: a website we know, else the top web result (A1).
                # "I couldn't find an app called notion" is never the answer for a site.
                from kayra.automation import web_macros
                if linux_apps.canonical(target):
                    # A KNOWN app that is not installed: its web version if it has one
                    # ("spotify web"), never a web search for the app's name.
                    key = linux_apps.canonical(target)
                    web = targets.website_url(f"{target} web") or targets.website_url(f"{key} web")
                    if web:
                        opened = web_macros.open_in(web, None, f"the {target} web player")
                        if opened.ok:
                            opened.message = (f"{target.title()} isn't installed, so I opened "
                                              "its web version.")
                        return opened
                    return result
                if targets.website_url(target):
                    return web_macros.open_site(target, None)
                if web_macros.web_worthy(target):
                    from kayra.automation import interpreter
                    if interpreter.ACTIVE and interpreter.enabled() and not params.get("no_ai"):
                        # Not an app, not a site we know. Let the understanding layer read
                        # it first ("the budget thing", "XYZ chat" inside WhatsApp); the
                        # runtime comes back here with `web` set if the model has nothing.
                        return ActionResult.not_found(result.message, web_fallback=True)
                    return web_macros.open_site(target, None)
            return result
        if verb == "close":
            return close_target(target)
        if verb == "close_all":
            return close_target(target, all_instances=True)
        if verb == "restart":
            return restart_app(target)
        if verb == "content":
            return write_content(target)
        if verb == "kill":
            return ActionResult.unsupported(
                "I only close windows the way the close button does. Ask me to close it.")
        return None

    if domain == "browser" and verb not in ("open_url", "search_web", "search_youtube") \
            and verb not in _BROWSER_KEYS:
        return _dispatch_extended(domain, verb, target, params, action)

    if domain == "browser":
        from kayra.automation import web_macros
        where = params.get("where")
        if verb == "open_url":
            url = params.get("url") or target
            site = params.get("site") or targets.canonical_website(target)
            if site and where is None:
                # A5 / A1: an installed application, or the Omarchy web app for this site,
                # wins over a browser tab. "open youtube in the browser" skips this.
                launched = _open_site_app(target, url)
                if launched is not None:
                    return launched
            return web_macros.open_in(url, None if where == "browser" else where,
                                      web_macros.display_name(site) if site else None)
        if verb == "search_web":
            result = web_macros.open_in(
                f"https://www.google.com/search?q={quote_plus(target or '')}", where, "")
            if result.ok:
                result.message = f"Searching the web for {target}." if target else "Searching."
            return result
        if verb == "search_youtube":
            result = web_macros.open_in(f"https://www.youtube.com/results?search_query="
                                        f"{quote_plus(target or '')}", where, "")
            if result.ok:
                result.message = f"Searching YouTube for {target}." if target else "Searching YouTube."
            return result
        return browser_nav(verb)

    if domain == "media":
        if verb == "play" and target:
            return play_on_youtube(target)
        if verb in _PLAYERCTL:
            return media_action(verb)
        return None

    if domain == "keyboard":
        if verb == "type":
            text = params.get("text", "")
            if not text:
                return ActionResult.failure("Type what?")
            return _result(type_text(text), "Typed.", "I can't type here. Install wtype.")
        if verb == "hotkey":
            name = params.get("name") or ""
            if name == "fullscreen" and active_window() and \
                    not _is_browser_window(active_window()):
                return window_action("fullscreen")
            return hotkey(name, params.get("keys") or target)
        return None

    if domain == "mouse":
        return mouse_action(verb, target)

    if domain == "clipboard":
        if verb in ("copy", "paste", "cut"):
            keys = {"copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x"}[verb]
            return _result(send_keys(keys), {"copy": "Copied.", "paste": "Pasted.",
                                             "cut": "Cut."}[verb],
                           "I can't send keystrokes here. Install wtype.")
        if verb == "set":
            return _result(clipboard_set(target), "Copied to the clipboard.",
                           "I couldn't copy that.")
        if verb == "clear":
            return _result(clipboard_clear(), "Clipboard cleared.", "I couldn't clear it.")
        if verb == "read":
            content = clipboard_get()
            if not content.strip():
                return ActionResult.not_found("The clipboard is empty.")
            return ActionResult.success(
                f"The clipboard has: {content.strip().replace(chr(10), ' ')[:120]}")
        return None

    if domain == "info":
        return ActionResult.success(system_info(verb))

    if domain == "screen":
        if verb == "screenshot":
            region = bool(re.search(r"\b(region|area|part|selection|select)\b",
                                    str(target or "").lower()))
            return take_screenshot(region=region)
        return _dispatch_extended(domain, verb, target, params, action)

    if domain == "file":
        handlers = {"open_folder": lambda: open_path(target, kind="folder"),
                    "open_file": lambda: open_path(target, kind="file"),
                    "create_folder": lambda: CreateFolder(target),
                    "create_file": lambda: CreateFile(target, params.get("content", "")),
                    "rename": lambda: RenamePath(target, params.get("new_name")),
                    "copy": lambda: CopyPath(target, params.get("destination")),
                    "move": lambda: MovePath(target, params.get("destination")),
                    "delete": lambda: DeletePath(target),
                    "search": lambda: SearchFiles(target)}
        handler = handlers.get(verb)
        return handler() if handler else None

    if domain == "timer":
        timers = RUNTIME.timers
        if verb in ("add", "reminder"):
            seconds = params.get("seconds")
            if not seconds:
                return ActionResult.failure("How long should I set it for?")
            label = _humanize_duration(seconds)
            timer_id, error = timers.add(seconds, label)
            return ActionResult.failure(error) if timer_id is None else \
                ActionResult.success(f"Timer set for {label}.")
        if verb == "cancel":
            label = timers.cancel()
            return ActionResult.success(f"Cancelled the {label} timer.") if label else \
                ActionResult.not_found("You don't have any timers running.")
        if verb == "list":
            active = timers.list()
            if not active:
                return ActionResult.success("No timers running.")
            return ActionResult.success(f"{len(active)} running. The next one has "
                                        f"{int(active[0]['remaining'] // 60)} minutes left.")
        return None

    if domain == "system":
        if verb in ("power_ambiguous", "power_kayra"):
            return ActionResult.blocked(_power_refusal_sentence(action))
        text = params.get("command") or target or ""
        if verb == "lock":
            return lock_screen()
        if verb in ("shutdown", "restart", "sleep", "sign_out"):
            return power_action(verb, text)
        if verb == "screen_off":
            return screen_off()
        if verb == "volume":
            return volume_action(text)
        if verb == "brightness":
            return brightness_action(text)
        if verb in ("wifi_on", "wifi_off", "wifi_toggle"):
            state = {"wifi_on": True, "wifi_off": False}.get(verb, _on_off(text))
            return wifi_action(state)
        if verb == "bluetooth":
            return bluetooth_action(text)
        if verb == "dnd":
            return dnd_action(text)
        if verb == "night_light":
            return night_light_action(text)
        if verb == "now_playing":
            return now_playing()
        if verb == "undo":
            return undo_last()
        if verb == "power_profile":
            return power_profile_action(text)
        if verb in ("workspace", "workspace_move"):
            return workspace_action(verb, text)
        if verb in ("float", "pin", "center"):
            return window_action(verb)
        if verb == "raw":
            return ActionResult.unsupported("I'm not sure which setting you mean.")
        return _dispatch_extended(domain, verb, target, params, action)

    if domain == "shell":
        return RunShellCommand(params.get("command") or target)

    return _dispatch_extended(domain, verb, target, params, action)


def _agent():
    from kayra.automation import page_agent
    return page_agent


def _learning(verb, target):
    from kayra.automation import learning
    if verb == "show":
        return ActionResult.success(learning.describe())
    if verb == "forget_last":
        said = learning.forget_last()
        return ActionResult.success(said) if said else ActionResult.not_found("There's nothing I just learned to forget.")
    if verb == "forget_all":
        count = learning.forget_all()
        return ActionResult.success(f"Forgot everything I'd learned ({count} things)." if count else "There was nothing to forget.")
    if verb == "forget_app":
        count = learning.forget_app(target.lower().replace(" ", ""))
        return ActionResult.success(f"Forgot {count} thing{'s' if count != 1 else ''} about {target}." if count
                                    else f"I hadn't learned anything about {target}.")
    if verb == "forget_recipe":
        from kayra.automation import page_agent
        return ActionResult.success(f"Forgot how to {target}.") if page_agent.forget_recipe(target) else \
            ActionResult.not_found(f"I don't have a shortcut for {target}.")
    return None


def _share_warning(action):
    from kayra.automation import web_macros
    return web_macros.share_warning(action)


common.SHARE_WARNING["fn"] = _share_warning      # a reference, not a side effect: nothing runs at import


def _inbox():
    from kayra.automation import inbox
    return inbox


def _profiles():
    from kayra.automation import chrome_profiles
    return chrome_profiles


def _dispatch_extended(domain, verb, target, params, action):
    """Round 3 domains: web, diagnostics, screen, scenes, plans, triggers, files, dev."""
    from kayra.automation import (web_macros as W, diagnostics as D, screen_ai as V,
                                  workflows as F, linux_system as S)
    W.new_command()                       # a fresh "which tab is active?" for this command
    if domain == "learning":
        return _learning(verb, target)
    if domain == "inbox":
        if verb == "all":
            return _inbox().inbox_all()
        if verb == "offers":
            return _inbox().set_message_offers(target == "on")
        return _inbox().reply_anywhere(target, params.get("text", ""), action)
    if domain == "browser":
        table = {"tabs_list": lambda: W.tabs_list(),
                 "tab_switch": lambda: W.tab_switch(target),
                 "tab_close": lambda: W.tab_close(target, params.get("all", False),
                                                  params.get("confirmed_ids"), action),
                 "tabs_close_unpinned": lambda: W.tabs_close_unpinned(params.get("confirmed_ids"), action),
                 "tabs_group": lambda: W.tabs_group(target, params.get("title", "")),
                 "groups_list": lambda: W.groups_list(),
                 "group_add": lambda: W.group_add(target),
                 "group_rename": lambda: W.group_update(target, title=params.get("title", "")),
                 "group_color": lambda: W.group_update(target, color=params.get("color")),
                 "group_collapse": lambda: W.group_open(target) if params.get("or_chat") else
                 W.group_update(target, collapsed=params.get("collapsed", True)),
                 "group_ungroup": lambda: W.group_ungroup(target),
                 "group_close": lambda: W.wa_close() if _is_open_chat(target, W) else
                 W.group_close(target, params.get("confirmed_ids"), action),
                 "tab_detach": lambda: W.tab_detach(params.get("monitor", False)),
                 "windows_merge": lambda: W.windows_merge(),
                 "page_result": lambda: W.page_result(params.get("nth", 1), params.get("new_tab", False)),
                 "page_results": lambda: W.page_results(),
                 "page_find_step": lambda: W.page_find_step(params.get("step", 1)),
                 "page_find_clear": lambda: W.page_find_clear(),
                 "tabs_close_others": lambda: W.tabs_close_others(),
                 "tabs_close_duplicates": lambda: W.tabs_close_duplicates(),
                 "tabs_close_side": lambda: W.tabs_close_side(target),
                 "tabs_mute_others": lambda: W.tabs_mute_others(params.get("on", True)),
                 "tabs_recently_closed": lambda: W.tabs_recently_closed(),
                 "tab_reopen": lambda: W.tab_reopen(target),
                 "tabs_discard": lambda: W.tabs_discard(),
                 "tab_find": lambda: W.tab_find(target),
                 "page_reload": lambda: W.page_reload(params.get("hard", False)),
                 "page_zoom": lambda: W.page_zoom(target),
                 "page_copy": lambda: W.page_copy(target),
                 "tab_mute": lambda: W.tab_mute(target, params.get("on", True)),
                 "tab_pin": lambda: W.tab_pin(),
                 "page_click": lambda: W.page_click(target, params.get("nth", 1)),
                 "page_type": lambda: W.page_type(target, params.get("text", ""),
                                                  params.get("submit", False)),
                 "page_scroll": lambda: _page_scroll(params),
                 "search_close": lambda: W.search_close(),
                 "page_read": lambda: W.page_read(target), "page_summarize": lambda: W.page_summarize(target),
                 "page_ask": lambda: W.page_ask(params.get("question", "")),
                 "page_comments": lambda: W.page_comments(params.get("summarize", False)),
                 "video_summary": lambda: W.video_summary(),
                 "page_translate": lambda: W.page_translate(target),
                 "selection_explain": lambda: W.selection_explain(),
                 "selection_translate": lambda: W.selection_translate(target),
                 "bookmark_add": lambda: W.bookmark_add(params.get("folder", "")),
                 "bookmarks_search": lambda: W.bookmarks_search(target),
                 "bookmark_delete": lambda: W.bookmark_delete(target, params.get("confirmed_ids"), action),
                 "history_clear": lambda: W.history_clear(target, params.get("confirmed", False), action),
                 "history_list": lambda: W.history_list(params.get("days", 1)),
                 "downloads_list": lambda: W.downloads_list(),
                 "download_open": lambda: W.download_open(params.get("confirmed_id"), action),
                 "download_show": lambda: W.download_show(),
                 "download_control": lambda: W.download_control(target),
                 "search_here": lambda: W.search_here(target),
                 "page_help": lambda: W.page_help(),
                 "ws_open": lambda: W.ws_open(target, params.get("kind", "file")),
                 "ws_search": lambda: W.ws_search(target, params.get("kind", "file"), params.get("open", False)),
                 "ws_create": lambda: W.ws_create(target, params.get("title", "")),
                 "sheet_cell": lambda: W.sheet_cell(target),
                 "sheet_calc": lambda: W.sheet_calc(target, params.get("op", "sum"), params.get("where")),
                 "sheet_row": lambda: W.sheet_row(target, params.get("headers", False)),
                 "ws_share": lambda: W.ws_share(target),
                 "slides_present": lambda: W.slides_present(),
                 "slides_nav": lambda: W.slides_nav(target),
                 "ws_recent": lambda: W.ws_recent(),
                 "ws_download": lambda: W.ws_download(),
                 "keep_note": lambda: W.keep_note(params.get("text", "")),
                 "form_responses": lambda: W.form_responses(target),
                 "classroom_due": lambda: W.classroom_due(),
                 "page_find": lambda: W.page_find(target), "page_controls": lambda: W.page_controls(),
                 "history_open": lambda: W.history_open(target, params.get("days", 7)),
                 "bookmark_open": lambda: W.bookmark_open(target),
                 "bridge_setup": lambda: W.bridge_setup(), "bridge_status": lambda: W.bridge_status(),
                 "bridge_update": lambda: W.bridge_update(),
                 # ── Part 6 ──
                 "form_fill": lambda: W.form_fill(params.get("confirmed"), action),
                 "form_choose": lambda: W.form_choose(params.get("field", ""), params.get("option", target),
                                                      params.get("kind", "select"), params.get("state")),
                 "form_submit": lambda: W.form_submit(),
                 "page_key": lambda: W.page_key(target, params.get("count", 1)),
                 "file_upload": lambda: W.file_upload(target, params.get("field", ""),
                                                      params.get("confirmed_path"), action),
                 "video": lambda: W.video_control(params.get("op", target), params.get("value")),
                 "video_subscribe": lambda: W.video_control("subscribe"),
                 "site_search": lambda: W.site_search(params.get("site", ""), target, params.get("sort", ""),
                                                      params.get("lo", 0), params.get("hi", 0), params.get("where")),
                 "shop_refine": lambda: W.shop_refine(params.get("sort", ""), params.get("lo", 0), params.get("hi", 0)),
                 "product_info": lambda: W.product_info(),
                 "profile_set": lambda: W.profile_set(target, params.get("text", "")),
                 "profile_show": lambda: W.profile_show(params.get("read", False)),
                 "profile_forget": lambda: W.profile_forget(target),
                 "profile_clear": lambda: W.profile_forget("all"),
                 # ── Part 6b ──
                 "ai_ask": lambda: W.ai_ask(params.get("bot", "chatgpt"), params.get("question", ""),
                                            bool(params.get("new"))),
                 "ai_read": lambda: W.ai_read("" if target == "current" else target, bool(params.get("full"))),
                 "social": lambda: W.social(target, params.get("site", ""), params.get("name", ""),
                                            params.get("period", ""), params.get("read", True)),
                 # ── Part 5P: Chrome profiles, read from Chrome's own Local State ──
                 "chrome_profile": lambda: _profiles().open_profile(target, params.get("browser", ""),
                                                                    params.get("url", ""), params.get("email", "")),
                 "chrome_profiles": lambda: _profiles().list_profiles(params.get("browser", "")),
                 "chrome_guest": lambda: _profiles().open_guest(params.get("browser", "")),
                 # ── Part 7: the page agent and its recipes ──
                 "agent": lambda: _agent().run(target, params.get("resume"), action),
                 "agent_recipes": lambda: _agent().list_recipes(),
                 # ── Part 12 ──
                 "session_save": lambda: W.session_save(target), "session_open": lambda: W.session_open(target),
                 "sessions_list": lambda: W.sessions_list(), "session_delete": lambda: W.session_delete(target),
                 "reading_add": lambda: W.reading_add(), "reading_list": lambda: W.reading_list(),
                 "reading_next": lambda: W.reading_next(), "reading_clear": lambda: W.reading_clear(),
                 "video_pip": lambda: W.video_pip(target != "off"),
                 "page_screenshot": lambda: W.page_screenshot(target == "full"),
                 "tabs_tidy": lambda: W.tabs_tidy_report(),
                 "open_elsewhere": lambda: W.open_elsewhere(target, params.get("profile", ""))}
        handler = table.get(verb)
        return handler() if handler else None
    if domain == "meet":
        if verb == "present":
            return W.meet_present(target)
        if verb in ("notes", "summary"):
            from kayra.automation import meeting_notes as N
            return N.summarize() if verb == "summary" else N.start() if target == "on" else N.stop()
        if verb in ("record", "record_stop"):
            return V.record(verb == "record")
        if verb in ("people", "react", "pin", "message", "link"):
            return {"people": lambda: W.meet_people(), "react": lambda: W.meet_react(target),
                    "pin": lambda: W.meet_pin(target),
                    "message": lambda: W.meet_chat(params.get("text", ""), bool(params.get("confirmed")), action),
                    "link": lambda: W.meet_link(params.get("to", ""), action)}[verb]()
        if verb == "admit":
            return W.meet_admit(True)
        if verb == "join":
            return W.meet_join(target)
        if verb == "share":
            return W.meet_share(target or "monitor", params.get("name", ""),
                                bool(params.get("audio")), bool(params.get("instead")))
        if verb == "stop_share":
            return W.meet_stop_share()
        if verb == "share_status":
            return W.meet_share_status()
        return W.meet_toggle(verb, params.get("on"))
    if domain == "email":
        if verb in ("reply", "reply_all", "forward", "archive", "delete", "back", "unread", "star", "spam"):
            return W.email_action(verb)
        if verb == "folder":
            return W.email_folder(target)
        if verb == "open":
            return W.email_open(params.get("which", "first"), params.get("nth", 1),
                                params.get("query", ""))
        return {"check": lambda: W.email_check(),
                "read": lambda: W.email_read(target, params.get("which"), params.get("nth", 1),
                                             params.get("query", "")),
                "draft": lambda: W.email_draft(target, params.get("subject", ""),
                                               params.get("body", "")),
                "send": lambda: W.email_send(),
                "search": lambda: W.email_search(target),
                "compose": lambda: W.email_compose(),
                "label": lambda: W.email_label(target)}[verb]()
    if domain == "calendar":
        if verb == "next":
            return W.calendar_next(bool(params.get("join")))
        return W.calendar_add(params.get("text", target)) if verb == "add" else \
            W.calendar_agenda(target)
    if domain == "message":
        if verb == "open":
            return W.wa_open(target)
        if verb == "search":
            return W.wa_search(target, bool(params.get("open")))
        if verb == "close":
            return W.wa_close()
        if verb == "unread":
            return W.wa_unread()
        if verb == "read":
            return W.wa_read(target, bool(params.get("today")), int(params.get("count") or 0))
        if verb == "other":
            return W.wa_other()
        if verb == "manage":
            return W.wa_manage(params.get("op", "mute"), target, params.get("span", ""))
        if verb == "act":
            return W.wa_message(params.get("op", "star"), target, params.get("text", ""), params.get("to", ""),
                                params.get("confirmed"), action)
        if verb == "send_file":
            return W.wa_send_file(target, params.get("what", ""), params.get("confirmed_path"),
                                  params.get("confirmed_chat"), action)
        return W.message_send(target, params.get("text", ""), params.get("confirmed_chat"), action)
    if domain == "diagnose":
        if verb == "fix":
            return ActionResult.success(D.apply_fixes())
        spoken, report = D.diagnose(target)
        return ActionResult.success(spoken, report=report)
    if domain == "screen":
        return {"describe": lambda: V.describe(target if "?" in str(target) else
                                               "What is on my screen? Describe it briefly and point "
                                               "out anything that needs my attention. The user asked: "
                                               + str(target)),
                "explain": lambda: V.explain(target), "read": lambda: V.read_aloud(),
                "copy_text": lambda: V.copy_text(), "record": lambda: V.record(True),
                "record_stop": lambda: V.record(False)}.get(verb, lambda: None)()
    if domain == "scene":
        return {"run": lambda: F.run_scene(target), "save": lambda: F.save_scene(target),
                "list": lambda: F.list_scenes(), "delete": lambda: F.delete_scene(target)}[verb]()
    if domain == "plan":
        return F.run_plan(target)
    if domain == "trigger":
        if verb == "add":
            return F.add_trigger(target, approved=params.get("approved", False))
        return F.list_triggers() if verb == "list" else F.cancel_trigger(target)
    if domain == "files":
        return {"organize": lambda: S.organize_folder(target),
                "duplicates": lambda: S.duplicates(target), "space": lambda: S.space_usage(target),
                "recent": lambda: S.recent_files(target), "summarize": lambda: S.summarize_file(target),
                "convert": lambda: S.convert_file(target), "backup": lambda: S.backup_folder(target),
                "find": lambda: S.find_files(target)}[verb]()
    if domain == "dev":
        return {"git_status": S.git_status, "git_branch": S.git_branch,
                "git_commit": lambda: S.git_commit(params.get("message", ""))}[verb]()
    if domain == "system":
        text = params.get("command") or target or ""
        table = {
            "music_sleep": lambda: _music_sleep(text), "recall": lambda: F.explain_last(text),
            "briefing": lambda: F.briefing(), "notifications": lambda: S.read_notifications(text),
            "in_call": lambda: ActionResult.success("Yes, something is using your microphone."
                                                    if F.in_call() else "No call that I can see."),
            "session_save": lambda: F.save_scene("my setup"),
            "session_restore": lambda: F.run_scene("last session" if re.search(
                r"last|yesterday", text.lower()) else "my setup"),
            "layout": lambda: S.side_by_side(text), "list_windows": lambda: S.list_open_windows(),
            "where": lambda: S.where_is(text), "close_except": lambda: S.close_except(text),
            "move_all": lambda: S.move_all(text), "tidy": lambda: S.tidy_windows(),
            "other_monitor": lambda: S.other_monitor(), "resize": lambda: S.resize(text),
            "audio_output": lambda: S.switch_output(text), "audio_list": lambda: S.list_outputs(),
            "mic": lambda: S.microphone(text), "wifi_connect": lambda: S.wifi_connect(text),
            "wifi_forget": lambda: S.wifi_forget(text), "wifi_share": lambda: S.wifi_share(),
            "bt_device": lambda: S.bluetooth_device(text), "updates": lambda: S.check_updates(),
            "update_system": lambda: S.update_system(),
            "install": lambda: S.install_package(text), "uninstall": lambda: S.install_package(text, True),
            "display_set": lambda: S.display_set(text), "touchpad": lambda: S.touchpad(),
            "kb_layout": lambda: S.keyboard_layout(), "kb_backlight": lambda: S.keyboard_backlight(text),
            "wallpaper": lambda: S.wallpaper(), "theme": lambda: S.theme(text),
            "close_hog": lambda: S.close_hog(text), "top_processes": lambda: S.top_processes(text),
            "docker": lambda: S.docker(text), "run_tests": lambda: S.run_project("tests"),
            "dev_server": lambda: S.run_project("server"),
        }
        handler = table.get(verb)
        return handler() if handler else None
    return None


def _music_sleep(text):
    seconds = common.parse_duration(text) or 30 * 60
    label = _humanize_duration(seconds)
    timer_id, error = RUNTIME.timers.add(
        seconds, "music sleep timer", on_fire=lambda _l: media_action("pause"))
    return ActionResult.failure(error) if timer_id is None else \
        ActionResult.success(f"I'll stop the music in {label}.")

    return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                           PUBLIC API                                   │
# └────────────────────────────────────────────────────────────────────────┘

def _timer_notify(label):
    """A fired timer: spoken, a desktop notification, and a short chime. Fixed argv only."""
    common.announce(f"Reminder: {label}." if not re.match(r"^\d", str(label))
                    else f"Your {label} timer is done.", urgent=True)
    if _have("notify-send"):
        RUNNER.spawn(["notify-send", "-u", "critical", "-a", "Kayra", "Timer complete",
                      str(label)[:80]])
    for sound in ("/usr/share/sounds/freedesktop/stereo/complete.oga",
                  "/usr/share/sounds/freedesktop/stereo/bell.oga"):
        if os.path.exists(sound) and _have("pw-play"):
            RUNNER.spawn(["pw-play", sound])
            break


RUNTIME = AutomationRuntime(_dispatch, notify=_timer_notify, on_shutdown=lambda: _stop_services())
CONTEXT = RUNTIME.context
CONFIRMATIONS = RUNTIME.confirmations
TIMERS = RUNTIME.timers
normalize_command = common.normalize_command
plan_actions = common.plan_actions
execute_action = RUNTIME.execute_action
translate_and_execute = RUNTIME.translate_and_execute
pending_confirmation = RUNTIME.pending_confirmation
resolve_confirmation = RUNTIME.resolve_confirmation
shutdown_automation = RUNTIME.shutdown
take_chat_handoff = RUNTIME.take_chat_handoff


_LAST_FRONT = {}
FRONT_MEMORY_S = 120.0


def situation():
    """
    What is on screen, for the understanding layer: the app in front, the web app / site it
    shows, and Chrome's active tab. Cheap reads only (one compositor query, one extension call
    with a short timeout); never a screenshot.
    """
    out = {}
    try:
        front = active_window()
        if (front is None or front.kayra_owned) and _LAST_FRONT.get("at") and \
                time.monotonic() - _LAST_FRONT["at"] <= FRONT_MEMORY_S:
            # Talking to Kayra's own window: the app the user was in a moment ago (H0.2).
            out.update({k: v for k, v in _LAST_FRONT.items() if k != "at"})
        if front is not None and not front.kayra_owned:
            out["app"] = front.initial_class or front.cls
            out["window_title"] = (front.title or "")[:120]
            site = next((linux_apps.site_of_window_class(c) for c in front.classes
                         if linux_apps.site_of_window_class(c)), None)
            if site:
                out["site"] = site
            _LAST_FRONT.clear()
            _LAST_FRONT.update({k: out[k] for k in ("app", "window_title", "site") if k in out},
                               at=time.monotonic())
    except Exception:
        pass
    try:
        from kayra.automation import web_macros
        if web_macros.BRIDGE.connected():
            out["bridge"] = True
            tab = web_macros.BRIDGE.call("tabs.active", timeout=2.0) or {}
            out["tab_title"] = str(tab.get("title") or "")[:120]
            out["tab_url"] = str(tab.get("url") or "")[:200]
    except Exception:
        pass
    return out


RUNTIME.situation_fn = situation


async def Automation(commands):
    """Public entry point, identical in contract to `automation.windows.Automation`."""
    return await RUNTIME.translate_and_execute(commands)


def start_services(speaker=None):
    """
    Starts the long-lived pieces, called ONCE by `app.bootstrap` (never at import):
      * the Chrome bridge server (127.0.0.1, one thread),
      * the watcher (triggers + health watch, one thread),
      * the notification log (one Kayra-owned dbus-monitor).
    `speaker(text, urgent)` is how background work talks.
    """
    from kayra.automation import browser_bridge, workflows, linux_system
    if speaker is not None:
        common.set_announcer(speaker)
    from kayra.automation import status_file, web_macros
    browser_bridge.BRIDGE.on_hello = web_macros.refresh_stale_extension
    started = {"bridge": browser_bridge.start(), "watcher": workflows.ensure_watcher(),
               "notifications": linux_system.start_notification_log(_inbox().on_notification),
               "status": status_file.start()}
    return started


def _stop_services():
    from kayra.automation import browser_bridge, workflows, linux_system, meeting_notes
    try:
        meeting_notes.stop()                      # the notes of a call still going are saved, not lost
    except Exception:
        pass
    try:
        workflows.save_last_session()
    except Exception:
        pass
    workflows.stop_watcher()
    linux_system.stop_notification_log()
    browser_bridge.BRIDGE.stop()
    from kayra.automation import status_file
    status_file.stop()


def capabilities():
    """What this backend can actually do on THIS machine. Read by the System screen."""
    return {
        "hyprland": hyprland_ready(),
        "keys": _have("wtype"),
        "media": _have("playerctl") or _have("gdbus"),
        "volume": _have("wpctl") or _have("pamixer"),
        "brightness": _have("brightnessctl"),
        "screenshot": _have("grim"),
        "clipboard": _have("wl-copy"),
        "wifi": _have("nmcli"),
        "bluetooth": _have("bluetoothctl"),
        "launch": _have("uwsm-app") or _have("gtk-launch"),
    }
