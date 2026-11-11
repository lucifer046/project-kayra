# ┌────────────────────────────────────────────────────────────────────────┐
# │                     test_linux_automation.py                           │
# │        The Linux "hands" — fake compositor, fake process runner        │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_linux_automation.py — assertion suite for `kayra.automation.linux` and the shared
`kayra.automation.common` pipeline it is built on.

    .venv/bin/python tests/test_linux_automation.py

Tier 1: hardware-free and deterministic. The compositor is a FakeHyprland object holding a
client list, and every external program goes through a RecordingRunner that returns scripted
output and starts nothing. The assertions are therefore about ARGUMENT VECTORS and DISPATCHES —
exactly what would have reached the machine — and about what did NOT reach it: a denied or
unconfirmed action must leave the runner's log empty.

SAFE. Nothing is launched, no key is typed, no window is touched, no setting changes. The
desktop-entry index is pointed at a temporary XDG tree. Runs on Windows too: the Linux
backend imports no Linux-only library.
"""

import os
import ast
import sys
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, describe_host, run  # noqa: E402

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.automation import common, linux, linux_apps, policy  # noqa: E402
from kayra.automation.policy import Status  # noqa: E402
from kayra.platforms import hyprland  # noqa: E402

check = Checker("Linux automation")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTOMATION_DIR = os.path.join(ROOT, "src", "kayra", "automation")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               FAKES                                    │
# └────────────────────────────────────────────────────────────────────────┘

def client(address, cls, title="", pid=1000, workspace=1, focus=5, floating=False,
           at=(0, 0), size=(100, 100)):
    return {"address": address, "class": cls, "initialClass": cls, "title": title,
            "pid": pid, "workspace": {"id": workspace, "name": str(workspace)},
            "at": list(at), "size": list(size),
            "floating": floating, "fullscreen": 0, "pinned": False,
            "focusHistoryID": focus, "mapped": True}


class FakeHyprland:
    """A compositor in a list. Applies close/focus so verification loops see the result."""

    lua_string = staticmethod(hyprland.lua_string)

    def __init__(self, clients=(), active=None, options=None, up=True, monitors=None,
                 workspace=1):
        self.monitors_list = monitors or [{"name": "eDP-1", "x": 0, "y": 0, "width": 2000,
                                           "height": 1200, "scale": 1.0, "focused": True,
                                           "refreshRate": 60.0}]
        self.workspace = workspace
        self.clients_list = [dict(c) for c in clients]
        self.active = active
        self.options = options or {}
        self.up = up
        self.dispatched = []
        self.refuse = False

    def available(self, environ=None):
        return self.up

    def clients(self, path=None):
        return [dict(c) for c in self.clients_list] if self.up else []

    def active_client(self, path=None):
        for c in self.clients_list:
            if c["address"] == self.active:
                return dict(c)
        return None

    def monitors(self, path=None):
        return [dict(m) for m in self.monitors_list]

    def active_workspace(self, path=None):
        return {"id": self.workspace}

    def request_json(self, command, path=None):
        if command.startswith("getoption "):
            name = command.split(" ", 1)[1]
            return {"option": name, "int": int(bool(self.options.get(name)))}
        return None

    def dispatch(self, lua=None, legacy=None, path=None):
        self.dispatched.append(lua or legacy)
        if self.refuse:
            return False
        text = lua or ""
        address = None
        if 'window = "address:' in text:
            address = text.split('window = "address:', 1)[1].split('"', 1)[0]
        if text.startswith("hl.dsp.window.close"):
            target = address or self.active
            self.clients_list = [c for c in self.clients_list if c["address"] != target]
            if self.active == target:
                self.active = None
        elif text.startswith("hl.dsp.focus") and address:
            self.active = address
        elif text.startswith("hl.dsp.focus({ workspace = "):
            value = text.split('"')[1]
            if value.isdigit():
                self.workspace = int(value)
        return True

    def add(self, c):
        self.clients_list.append(dict(c))


class RecordingRunner:
    """Every process the backend would start, recorded; nothing executed."""

    def __init__(self, tools=(), replies=None, on_spawn=None):
        self.tools = set(tools)
        self.replies = replies or {}
        self.on_spawn = on_spawn
        self.ran = []
        self.spawned = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv, timeout=4.0, input_text=None, stdin_path=None):
        self.ran.append((list(argv), input_text))
        for prefix, reply in self.replies.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return reply
        return 0, ""

    def spawn(self, argv):
        self.spawned.append(list(argv))
        if self.on_spawn:
            self.on_spawn(argv)
        return True

    @property
    def calls(self):
        return [a for a, _ in self.ran] + self.spawned


ALL_TOOLS = ("wtype", "wpctl", "brightnessctl", "grim", "slurp", "wl-copy", "wl-paste",
             "nmcli", "bluetoothctl", "uwsm-app", "gtk-launch", "xdg-open", "playerctl",
             "gdbus", "notify-send", "systemctl", "loginctl", "uwsm", "xdg-terminal-exec",
             "omarchy-system-lock")


class Rig:
    """Installs fakes into `kayra.automation.linux` and puts the originals back."""

    def __init__(self, hypr=None, runner=None):
        self.hypr = hypr or FakeHyprland()
        self.runner = runner or RecordingRunner(ALL_TOOLS)

    def __enter__(self):
        self._saved = (linux.HYPR, linux.RUNNER, linux.SLEEP, linux.LAUNCH_TIMEOUT_S,
                       linux.CLOSE_TIMEOUT_S)
        linux.HYPR, linux.RUNNER, linux.SLEEP = self.hypr, self.runner, (lambda s: None)
        # Verification loops poll a fake that answers instantly; a short deadline keeps a
        # deliberately window-less launch from spinning for the real 8 seconds.
        linux.LAUNCH_TIMEOUT_S, linux.CLOSE_TIMEOUT_S = 0.05, 0.05
        linux.CONFIRMATIONS.cancel()
        from kayra.automation import web_macros
        # "open <unknown>" may look the name up on the web; never from a unit test.
        self._top, web_macros.TOP_RESULTS = web_macros.TOP_RESULTS, (lambda q: [])
        # The understanding layer asks a model; never from a unit test unless one opts in.
        from kayra.automation import interpreter
        self._interp, interpreter.ACTIVE = interpreter.ACTIVE, False
        # Part 7: the page agent asks a model (off unless a test opts in), and what Kayra learns
        # goes to a temporary folder, never the developer's data/.
        import tempfile
        from kayra.automation import page_agent, learning
        self._agent, page_agent.ACTIVE = page_agent.ACTIVE, False
        self._learn_dir = tempfile.mkdtemp(prefix="kayra-learning-")
        self._learn, learning.DATA_DIR = learning.DATA_DIR, self._learn_dir
        learning.LAST.update(kind=None, app=None, said=None, at=0.0)
        web_macros.LAST_PICK.update(said=None, chosen=None, others=[], at=0.0)
        linux.RUNTIME.pending_choice = None
        return self

    def __exit__(self, *exc):
        from kayra.automation import web_macros
        web_macros.TOP_RESULTS = self._top
        from kayra.automation import interpreter
        interpreter.ACTIVE = self._interp
        import shutil
        from kayra.automation import page_agent, learning
        page_agent.ACTIVE, learning.DATA_DIR = self._agent, self._learn
        shutil.rmtree(self._learn_dir, ignore_errors=True)
        linux.RUNTIME.pending_choice = None
        (linux.HYPR, linux.RUNNER, linux.SLEEP, linux.LAUNCH_TIMEOUT_S,
         linux.CLOSE_TIMEOUT_S) = self._saved
        linux.CONFIRMATIONS.cancel()
        linux.TIMERS.shutdown()
        return False

    def do(self, token):
        return linux.execute_action(linux.normalize_command(token, linux.CONTEXT))


DESKTOP = {
    "google-chrome.desktop": "[Desktop Entry]\nType=Application\nName=Google Chrome\n"
                             "Exec=/usr/bin/google-chrome-stable %U\nStartupWMClass=Google-chrome\n",
    "org.gnome.Nautilus.desktop": "[Desktop Entry]\nType=Application\nName=Files\n"
                                  "Exec=nautilus --new-window %U\n",
    "hidden-tool.desktop": "[Desktop Entry]\nType=Application\nName=Hidden Tool\n"
                           "Exec=hidden\nNoDisplay=true\n",
    "ghost.desktop": "[Desktop Entry]\nType=Application\nName=Ghost\nExec=ghost\n"
                     "TryExec=/definitely/not/installed/ghost\n",
    "WhatsApp.desktop": "[Desktop Entry]\nType=Application\nName=WhatsApp\n"
                        "Exec=omarchy-launch-webapp https://web.whatsapp.com/\n"
                        "StartupWMClass=chrome-web.whatsapp.com__-Default\n",
    "Git Gui.desktop": "[Desktop Entry]\nType=Application\nName=Git GUI\nExec=git gui\n",
    "link.desktop": "[Desktop Entry]\nType=Link\nName=Some Link\nURL=https://x\n",
}


def with_desktop_tree(fn):
    with tempfile.TemporaryDirectory() as root:
        apps = os.path.join(root, "applications")
        os.makedirs(apps)
        for name, body in DESKTOP.items():
            with open(os.path.join(apps, name), "w", encoding="utf-8") as handle:
                handle.write(body)
        linux_apps.INDEX.ensure(dirs=[apps])
        saved = linux_apps.INDEX.ensure
        linux_apps.INDEX.ensure = lambda dirs=None: None      # pin the synthetic tree
        try:
            fn()
        finally:
            linux_apps.INDEX.ensure = saved
            linux_apps.INDEX.invalidate()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     1. THE SHARED PIPELINE                             │
# └────────────────────────────────────────────────────────────────────────┘

def section_shared():
    check.section("[1] One normalizer, one policy, on every OS")

    tree = ast.parse(open(common.__file__, encoding="utf-8").read())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    forbidden = {"ctypes", "keyboard", "pyautogui", "pynput", "AppOpener", "win32gui",
                 "win32api", "pywhatkit", "winreg"}
    check("common.py imports no desktop or Win32 library", not (imported & forbidden),
          str(imported & forbidden))

    win_src = open(os.path.join(AUTOMATION_DIR, "windows.py"), encoding="utf-8").read()
    win_tree = ast.parse(win_src)
    defined = {n.name for n in win_tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for name in ("normalize_command", "_build_action", "plan_actions", "parse_duration",
                 "TimerService", "DeletePath", "RunShellCommand"):
        check(f"windows.py no longer defines its own {name}", name not in defined)
    check("windows.py re-exports the shared pipeline",
          "from kayra.automation.common import (" in win_src)

    cases = {
        "open youtube": "browser.open_url", "close chrome": "app.close",
        "close all chrome windows": "app.close_all", "minimize all": "window.minimize_all",
        "system volume 40": "system.volume", "system workspace 3": "system.workspace",
        "system move window to workspace 2": "system.workspace_move",
        "system bluetooth on": "system.bluetooth", "system turn off bluetooth": "system.bluetooth",
        "system do not disturb on": "system.dnd", "system night light on": "system.night_light",
        "system now playing": "system.now_playing", "system toggle floating": "system.float",
        "system turn off screen": "system.screen_off",
        "system shutdown the engine car": "system.power_kayra",
        "system shutdown": "system.power_ambiguous",
        "system shut down the computer": "system.shutdown",
        "set timer 5 minutes": "timer.add",
    }
    for token, key in cases.items():
        action = common.normalize_command(token)
        check(f"'{token}' -> {key}", action is not None and action.key == key,
              action.key if action else None)

    check("'turn off bluetooth' never reaches the power gate",
          policy.classify_action(common.normalize_command("system turn off bluetooth"))[0]
          == policy.Risk.ALLOW)

    from kayra.automation import load_backend, backend_name
    check("backend_name maps linux", backend_name("linux") == "linux")
    check("backend_name maps win32", backend_name("win32") == "windows")
    check("no backend for darwin", backend_name("darwin") == "")
    if sys.platform.startswith("linux"):
        check("load_backend() on Linux is automation.linux", load_backend() is linux)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       2. SAFETY GATES                                  │
# └────────────────────────────────────────────────────────────────────────┘

def section_safety():
    check.section("[2] Nothing reaches the machine without passing the gates")

    with Rig() as rig:
        for token in ("system shutdown", "system shutdown the engine car", "system restart",
                      "system sleep"):
            result = rig.do(token)
            check(f"'{token}' is refused", result.status == Status.BLOCKED, result.status)
        for token in ("system shut down the computer", "system reboot my pc",
                      "close everything", "delete file notes.txt", "wifi off"):
            result = rig.do(token)
            check(f"'{token}' asks first", result.status == Status.NEEDS_CONFIRMATION,
                  result.status)
            linux.CONFIRMATIONS.cancel()
        result = rig.do("terminal sudo rm -rf /home")
        check("sudo is denied", result.status == Status.BLOCKED, result.message)
        result = rig.do("terminal bash -c ls")
        check("a nested shell is denied", result.status == Status.BLOCKED)
        check("NOTHING was executed by any of the above", rig.runner.calls == [],
              str(rig.runner.calls))
        check("and nothing was dispatched to the compositor", rig.hypr.dispatched == [])

        rig.do("system shut down the computer")
        handled, spoken = linux.resolve_confirmation("no")
        check("'no' cancels the shutdown", handled and rig.runner.calls == [])
        rig.do("system shut down the computer")
        handled, spoken = linux.resolve_confirmation("yes")
        check("'yes' runs systemctl poweroff, exactly",
              ["systemctl", "poweroff"] in rig.runner.calls, str(rig.runner.calls))

    for command, verdict in (("systemctl poweroff", "CONFIRM"), ("reboot", "CONFIRM"),
                             ("pkexec true", "DENY"), ("doas ls", "DENY"),
                             ("shred secret", "DENY"), ("df -h", "ALLOW"),
                             ("uname -a", "ALLOW"), ("ls ; rm -rf ~", "DENY")):
        check(f"shell policy: {command!r} is {verdict}",
              policy.classify_shell(command)[0] == verdict, policy.classify_shell(command))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. APPLICATIONS                                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_applications():
    check.section("[3] Applications: exact identities, focus before launch, verified windows")

    def body():
        chrome = linux_apps.resolve("chrome")
        check("'chrome' resolves to the google-chrome entry",
              chrome and chrome.entry and chrome.entry.id == "google-chrome")
        check("its window class includes StartupWMClass", "google-chrome" in chrome.classes)
        files = linux_apps.resolve("files")
        check("'files' resolves to Nautilus", files and files.entry.id == "org.gnome.Nautilus")
        check("'file explorer' means the file manager too",
              linux_apps.resolve("file explorer").entry.id == "org.gnome.Nautilus")
        check("an exact desktop Name resolves (WhatsApp web app)",
              linux_apps.resolve("whatsapp").entry.id == "WhatsApp")
        check("NoDisplay entries are not offered", linux_apps.resolve("hidden tool") is None)
        check("TryExec that is missing hides the entry", linux_apps.resolve("ghost") is None)
        check("Type=Link is not an application", linux_apps.resolve("some link") is None)
        check("NO fuzzy matching: 'github' does not become Git GUI",
              linux_apps.resolve("github") is None)
        check("NO fuzzy matching: 'chrom' is not chrome", linux_apps.resolve("chrom") is None)
        check("a curated app that is not installed is reported as such",
              not linux_apps.installed(linux_apps.resolve("spotify")))

        # Launch with verification: the fake compositor maps a window when spawned.
        hypr = FakeHyprland([client("0xk", "kayra", "Kayra", focus=0)], active="0xk")
        runner = RecordingRunner(ALL_TOOLS, on_spawn=lambda argv: hypr.add(
            client("0xc1", "google-chrome", "New Tab - Google Chrome", focus=0)))
        with Rig(hypr, runner) as rig:
            result = rig.do("open chrome")
            check("open chrome launches through uwsm-app with the desktop id",
                  runner.spawned == [["uwsm-app", "--", "google-chrome.desktop"]],
                  str(runner.spawned))
            check("and reports the window it saw", result.ok and "is open" in result.message,
                  result.message)

            runner.spawned.clear()
            result = rig.do("open chrome")
            check("a running app is FOCUSED, not launched twice",
                  runner.spawned == [] and result.ok, str(runner.spawned))
            check("the focus targeted that window's address",
                  any('address:0xc1' in d for d in hypr.dispatched))

            result = rig.do("open obsidian")
            check("a missing app is NOT_FOUND, said out loud",
                  result.status == Status.NOT_FOUND and "installed" in result.message,
                  result.message)
            runner.spawned.clear()
            result = rig.do("open spotify")
            check("a missing app with a web version opens it and says so (as on Windows)",
                  result.ok and "isn't installed" in result.message and
                  any("open.spotify.com" in " ".join(a) for a in runner.spawned), result.message)
            result = rig.do("open github")
            check("'open github' opens the website (never Git GUI)",
                  any(a[:1] == ["xdg-open"] and "github.com" in a[1] for a in runner.spawned),
                  str(runner.spawned))

        runner = RecordingRunner([t for t in ALL_TOOLS if t != "uwsm-app"])
        with Rig(FakeHyprland(), runner) as rig:
            rig.do("open files")
            check("without uwsm, gtk-launch is used",
                  runner.spawned and runner.spawned[0] == ["gtk-launch", "org.gnome.Nautilus"],
                  str(runner.spawned))
        runner = RecordingRunner(ALL_TOOLS)
        with Rig(FakeHyprland(), runner) as rig:
            rig.do("open terminal")
            check("'open terminal' uses the XDG terminal launcher",
                  runner.spawned and runner.spawned[0][-1] == "xdg-terminal-exec",
                  str(runner.spawned))

    with_desktop_tree(body)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                4. CLOSING: ONE INTENT, ONE TARGET                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_close():
    check.section("[4] Closing: one intent, one target, never Kayra")

    def body():
        two = [client("0xa", "google-chrome", "Docs", focus=2),
               client("0xb", "google-chrome", "Mail", focus=3),
               client("0xk", "kayra", "Kayra", focus=0),
               client("0xf", "foot", "shell", focus=1)]
        with Rig(FakeHyprland(two, active="0xk")) as rig:
            result = rig.do("close chrome")
            check("two Chrome windows, neither focused -> ASK",
                  result.status == Status.AMBIGUOUS, result.status)
            check("and nothing was closed", len(rig.hypr.clients_list) == 4)

        with Rig(FakeHyprland(two, active="0xb")) as rig:
            result = rig.do("close chrome")
            check("the focused Chrome window is the one closed",
                  result.ok and [c["address"] for c in rig.hypr.clients_list] == ["0xa", "0xk", "0xf"],
                  str([c["address"] for c in rig.hypr.clients_list]))
            check("closing uses window.close with the exact address",
                  any(d.startswith("hl.dsp.window.close") and "0xb" in d
                      for d in rig.hypr.dispatched))

        with Rig(FakeHyprland(two, active="0xf")) as rig:
            result = rig.do("close all chrome")
            check("'close all chrome' closes both and only both",
                  result.ok and {c["address"] for c in rig.hypr.clients_list} == {"0xk", "0xf"},
                  result.message)

        with Rig(FakeHyprland(two, active="0xk")) as rig:
            result = rig.do("close window")
            check("closing the current window when it is Kayra is refused",
                  result.status == Status.BLOCKED, result.message)
            result = rig.do("close kayra")
            check("'close kayra' finds nothing to close", not result.ok)
            check("Kayra's window survives everything",
                  any(c["address"] == "0xk" for c in rig.hypr.clients_list))

        site = [client("0xy", "google-chrome", "Lo-fi beats - YouTube - Google Chrome", focus=1),
                client("0xe", "foot", "editor", focus=0)]
        runner = RecordingRunner(ALL_TOOLS)
        with Rig(FakeHyprland(site, active="0xe"), runner) as rig:
            result = rig.do("close youtube")
            check("'close youtube' focuses the browser showing it",
                  any('address:0xy' in d for d in rig.hypr.dispatched))
            check("and closes the TAB with Ctrl+W, not the window",
                  ["wtype", "-M", "ctrl", "-k", "w", "-m", "ctrl"] in runner.calls
                  and len(rig.hypr.clients_list) == 2, str(runner.calls))
        with Rig(FakeHyprland([client("0xe", "foot", "editor")], active="0xe")) as rig:
            result = rig.do("close youtube")
            check("a site that is not on screen is a miss",
                  result.status == Status.NOT_FOUND and rig.runner.calls == [], result.message)

        runner = RecordingRunner(ALL_TOOLS)
        with Rig(FakeHyprland([client("0xe", "foot", "editor")], active="0xe"), runner) as rig:
            result = rig.do("close tab")
            check("'close tab' with no browser in front sends nothing",
                  result.status == Status.NOT_FOUND and runner.calls == [], result.message)

        with Rig(FakeHyprland(up=False)) as rig:
            result = rig.do("close chrome")
            check("without Hyprland, closing says so instead of guessing",
                  result.status == Status.UNSUPPORTED, result.message)

    with_desktop_tree(body)


# ┌────────────────────────────────────────────────────────────────────────┐
# │             5. WINDOWS, WORKSPACES, KEYS                               │
# └────────────────────────────────────────────────────────────────────────┘

def section_windows_and_keys():
    check.section("[5] Window layout, workspaces and keystrokes")

    wins = [client("0x1", "foot", "shell", focus=0), client("0xk", "kayra", "Kayra", focus=1)]
    with Rig(FakeHyprland(wins, active="0x1")) as rig:
        rig.do("maximize")
        check("maximize = fullscreen mode maximized on that window",
              any('fullscreen' in d and 'maximized' in d and '0x1' in d
                  for d in rig.hypr.dispatched), str(rig.hypr.dispatched))
        rig.do("system workspace 3")
        check("'workspace 3' focuses workspace \"3\"",
              'hl.dsp.focus({ workspace = "3" })' in rig.hypr.dispatched)
        rig.do("system move window to workspace two")
        check("'move window to workspace two' moves the focused window to \"2\"",
              any(d.startswith("hl.dsp.window.move") and 'workspace = "2"' in d and "0x1" in d
                  for d in rig.hypr.dispatched), str(rig.hypr.dispatched[-1:]))
        rig.do("system toggle floating")
        check("toggle floating dispatches window.float toggle",
              any(d.startswith("hl.dsp.window.float") and "toggle" in d
                  for d in rig.hypr.dispatched))
        rig.do("minimize all")
        check("'show desktop' switches to the first empty workspace (2 here)",
              'hl.dsp.focus({ workspace = "2" })' in rig.hypr.dispatched,
              str(rig.hypr.dispatched[-1:]))
        result = rig.do("system workspace")
        check("a workspace with no number asks which", not result.ok)

    with Rig(FakeHyprland(wins, active="0xk")) as rig:
        result = rig.do("system move window to workspace 4")
        check("Kayra's own window is never moved", result.status == Status.BLOCKED)

    for combo, argv in (("ctrl+shift+t", ["wtype", "-M", "ctrl", "-M", "shift", "-k", "t",
                                          "-m", "shift", "-m", "ctrl"]),
                        ("f11", ["wtype", "-k", "F11"]),
                        ("enter", ["wtype", "-k", "Return"]),
                        ("alt+left", ["wtype", "-M", "alt", "-k", "Left", "-m", "alt"]),
                        ("win+r", ["wtype", "-M", "logo", "-k", "r", "-m", "logo"])):
        check(f"wtype argv for {combo}", linux.wtype_argv(combo) == argv,
              linux.wtype_argv(combo))
    check("an unknown modifier is refused, not guessed", linux.wtype_argv("hyper+x") is None)

    with Rig(FakeHyprland(wins, active="0x1")) as rig:
        rig.do("write hello -- world")
        check("typed text goes through stdin, never as an argument",
              (["wtype", "-"], "hello -- world") in rig.runner.ran, str(rig.runner.ran))
        rig.do("undo")
        check("'undo' is ctrl+z", ["wtype", "-M", "ctrl", "-k", "z", "-m", "ctrl"]
              in rig.runner.calls)
    with Rig(FakeHyprland(wins, active="0x1"), RecordingRunner(())) as rig:
        result = rig.do("undo")
        check("without wtype, keystrokes are UNSUPPORTED (not a fake success)",
              result.status == Status.UNSUPPORTED, result.message)


# ┌────────────────────────────────────────────────────────────────────────┐
# │             6. AUDIO, DISPLAY, RADIOS, MEDIA                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_system():
    check.section("[6] Volume, brightness, radios, media, screenshots")

    sink = "@DEFAULT_AUDIO_SINK@"
    replies = {("wpctl", "get-volume"): (0, "Volume: 0.40\n"),
               ("brightnessctl", "-m"): (0, "intel_backlight,backlight,40,40%,100\n")}
    with Rig(runner=RecordingRunner(ALL_TOOLS, replies)) as rig:
        rig.do("system volume 40")
        check("'volume 40' sets exactly 40%, capped at 100",
              ["wpctl", "set-volume", "-l", "1.0", sink, "40%"] in rig.runner.calls)
        rig.do("system increase volume by 20%")
        check("'increase volume by 20%' is +20%",
              ["wpctl", "set-volume", "-l", "1.0", sink, "20%+"] in rig.runner.calls)
        rig.do("system volume down")
        check("'volume down' is -10%", ["wpctl", "set-volume", sink, "10%-"] in rig.runner.calls)
        rig.do("system mute")
        check("'mute' toggles mute", ["wpctl", "set-mute", sink, "toggle"] in rig.runner.calls)
        rig.do("system unmute")
        check("'unmute' unmutes", ["wpctl", "set-mute", sink, "0"] in rig.runner.calls)
        result = rig.do("system brightness 0")
        check("brightness is never set to 0 by voice",
              ["brightnessctl", "-q", "set", "5%"] in rig.runner.calls, result.message)
        rig.do("system bluetooth on")
        check("bluetooth on", ["bluetoothctl", "power", "on"] in rig.runner.calls)
        rig.do("wifi on")
        check("wifi on", ["nmcli", "radio", "wifi", "on"] in rig.runner.calls)

        rig.do("pause")
        check("pause uses playerctl when present", ["playerctl", "pause"] in rig.runner.calls)
        rig.do("next track")
        check("next track", ["playerctl", "next"] in rig.runner.calls)

    no_playerctl = [t for t in ALL_TOOLS if t != "playerctl"]
    bus = ("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus")
    runner = RecordingRunner(no_playerctl, {
        bus + ("--object-path", "/org/freedesktop/DBus", "--method",
               "org.freedesktop.DBus.ListNames"):
            (0, "(['org.mpris.MediaPlayer2.spotify', 'org.mpris.MediaPlayer2.chromium.instance77'],)"),
        bus + ("--object-path", "/org/freedesktop/DBus", "--method",
               "org.freedesktop.DBus.GetConnectionUnixProcessID", "org.mpris.MediaPlayer2.spotify"):
            (0, "(uint32 4242,)"),
        bus + ("--object-path", "/org/freedesktop/DBus", "--method",
               "org.freedesktop.DBus.GetConnectionUnixProcessID",
               "org.mpris.MediaPlayer2.chromium.instance77"):
            (0, "(uint32 77,)"),
    })
    from kayra.automation import targets
    saved = targets.kayra_owned_pids
    targets.kayra_owned_pids = lambda: {77}
    try:
        with Rig(runner=runner):
            players = linux._mpris_players()
    finally:
        targets.kayra_owned_pids = saved
    check("MPRIS players owned by Kayra's speech browser are excluded",
          players == ["org.mpris.MediaPlayer2.spotify"], str(players))
    check("the bus PID is the number after uint32, not the 32",
          linux.__dict__["re"].search(r"\(\s*(?:uint32\s+)?(\d+)\s*,", "(uint32 77,)").group(1)
          == "77")

    with tempfile.TemporaryDirectory() as shots:
        runner = RecordingRunner(ALL_TOOLS)
        original_run = runner.run

        def run(argv, timeout=4.0, input_text=None, stdin_path=None):
            if argv and argv[0] == "grim":
                open(argv[-1], "wb").close()
            return original_run(argv, timeout, input_text, stdin_path)
        runner.run = run
        os.environ["OMARCHY_SCREENSHOT_DIR"] = shots
        try:
            with Rig(runner=runner) as rig:
                result = rig.do("take screenshot")
        finally:
            os.environ.pop("OMARCHY_SCREENSHOT_DIR", None)
        check("a screenshot is grim into the screenshot folder",
              result.ok and any(a[0] == "grim" and a[-1].startswith(shots) for a in runner.calls),
              result.message)
        check("and it is copied to the clipboard as PNG",
              ["wl-copy", "--type", "image/png"] in runner.calls)

    with Rig(FakeHyprland(options={"misc:mouse_move_enables_dpms": True})) as rig:
        rig.do("system turn off the screen")
        check("screen off uses DPMS when input wakes it",
              any("dpms" in d for d in rig.hypr.dispatched))
    with Rig(FakeHyprland(options={})) as rig:
        result = rig.do("system turn off the screen")
        check("and LOCKS instead when nothing would wake a blank screen",
              ["omarchy-system-lock"] in rig.runner.calls and not rig.hypr.dispatched,
              result.message)


# ┌────────────────────────────────────────────────────────────────────────┐
# │             7. HYPRLAND DISPATCH, READINESS, STRUCTURE                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_platform():
    check.section("[7] Hyprland dispatch syntax, readiness finding, structure")

    sent = []
    saved_request, saved_state = hyprland.request, dict(hyprland._LUA_STATE)

    def lua_compositor(command, path=None):
        sent.append(command)
        return "ok" if command.startswith("dispatch hl.") else "error: lua syntax"

    def legacy_compositor(command, path=None):
        sent.append(command)
        return "ok" if not command.startswith("dispatch hl.") else "Invalid dispatcher"
    try:
        hyprland.request = lua_compositor
        hyprland._LUA_STATE["supported"] = None
        check("Lua-configured Hyprland: the Lua form is used",
              hyprland.move_cursor(10, 20) and sent[-1].startswith("dispatch hl.dsp.cursor.move"))
        sent.clear()
        hyprland.move_cursor(1, 2)
        check("and the legacy form is never tried again", len(sent) == 1, str(sent))

        hyprland.request = legacy_compositor
        hyprland._LUA_STATE["supported"] = None
        check("legacy Hyprland: falls back to `movecursor`",
              hyprland.move_cursor(10, 20) and sent[-1] == "dispatch movecursor 10 20")
        sent.clear()
        hyprland.move_cursor(3, 4)
        check("and stops paying for the Lua attempt", sent == ["dispatch movecursor 3 4"],
              str(sent))
        check("a reply that is not 'ok' is not success",
              not hyprland.dispatch("hl.dsp.no_op()", None))
    finally:
        hyprland.request = saved_request
        hyprland._LUA_STATE.update(saved_state)
    check("lua_string escapes quotes", hyprland.lua_string('a"b') == '"a\\"b"')

    from kayra.platforms import linux as platform_linux
    tools = {"wtype", "uwsm-app", "xdg-open", "wpctl"}
    support = platform_linux.automation_support(which=lambda c: c in tools, compositor=True)
    check("readiness: core tools present, extras missing",
          support["missing_core"] == [] and "screenshots" in support["missing_extras"], support)
    from kayra.core import system_profile as sp
    full = sp._linux_automation_finding({"compositor": True, "missing_core": [],
                                         "missing_extras": []})
    check("a complete Hyprland setup is READY", full.verdict == sp.READY, full.verdict)
    nohypr = sp._linux_automation_finding({"compositor": False, "missing_core": [],
                                           "missing_extras": []})
    check("no compositor is LIMITED and names Hyprland",
          nohypr.verdict == sp.LIMITED and "Hyprland" in nohypr.advice)
    nokeys = sp._linux_automation_finding({"compositor": True,
                                           "missing_core": ["keyboard input"],
                                           "missing_extras": []})
    check("a missing core tool is LIMITED and named", nokeys.verdict == sp.LIMITED
          and "keyboard input" in nokeys.summary)
    check("'not ported' is gone from the profile",
          "has not been ported" not in open(sp.__file__, encoding="utf-8").read())

    source = open(linux.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    shell_true = [n for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "shell"
                  and getattr(n.value, "value", None) is True]
    check("linux.py never passes shell=True", not shell_true)
    check("linux.py never calls os.system", "os.system(" not in source)
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and isinstance(n.value, str)}
    check("no process is terminated by name (no pkill/killall/kill literals)",
          not ({"pkill", "killall", "taskkill", "xkill"} & literals))
    runner_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Runner")
    inside = {id(n) for n in ast.walk(runner_cls)}
    outside = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute)
               and getattr(n.value, "id", None) == "subprocess" and id(n) not in inside]
    check("every process start goes through Runner (the one injectable seam)", not outside,
          str(outside))

    # The UI never needs to know which backend runs: the four entry points exist.
    for name in ("Automation", "pending_confirmation", "resolve_confirmation",
                 "shutdown_automation", "execute_action", "normalize_command"):
        check(f"linux exposes {name}", callable(getattr(linux, name, None)))

    with Rig() as rig:
        spoken = asyncio.run(linux.Automation(["battery", "set timer 2 minutes"]))
        check("Automation() returns one spoken sentence for a batch",
              isinstance(spoken, str) and "Timer set for 2 minutes" in spoken, spoken)
        check("shutdown_automation cancels pending timers", linux.shutdown_automation() == 1)


# ┌────────────────────────────────────────────────────────────────────────┐
# │          8. HARMFUL COMMANDS AND CRITICAL SERVICES                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_hardening():
    check.section("[8] Harmful commands and critical services are refused")
    matrix = {
        "DENY": ["systemctl stop NetworkManager", "systemctl --user stop pipewire",
                 "systemctl disable sshd", "systemctl mask bluetooth", "systemctl kill dbus",
                 "systemctl isolate rescue.target", "service NetworkManager stop",
                 "loginctl terminate-user lucifer", "kill -9 1", "kill 4242",
                 "killall chrome", "pacman -Rns linux", "pacman -Rdd glibc", "yay -R systemd",
                 "chmod -R 777 /", "chmod -R 700 ~", "chown -R root /home", "rm -rf ~",
                 "rm -rf $HOME", "rm -rf ~/../../etc", "rm -rf /usr", "dd if=/dev/zero of=/dev/sda",
                 "mkfs.ext4 /dev/sda1", "modprobe -r nvidia", "mount /dev/sda1 /mnt",
                 "swapoff -a", "init 0", "passwd", "useradd evil", "iptables -F",
                 "grub-install /dev/sda", "sudo ls", "pkexec ls", "bash -c ls",
                 "ls ; rm -rf ~", "stress-ng --cpu 64", "sysctl -w kernel.panic=1"],
        "CONFIRM": ["systemctl restart NetworkManager", "systemctl start nginx", "node -e x",
                    "systemctl poweroff", "shutdown -h now", "yay -R vlc", "pacman -Syu",
                    "pip install requests", "python -c print(1)", "git clean -fdx", "npm install",
                    "git reset --hard", "nmcli networking off", "chmod +x run.sh",
                    "rm notes.txt", "rm -rf ~/Downloads/old"],
        "ALLOW": ["systemctl status nginx", "df -h", "uname -a", "python --version",
                  "python train.py", "pip list", "pacman -Qi linux",
                  "git status", "nmcli device status", "ip addr", "free -h"],
    }
    for verdict, commands in matrix.items():
        for command in commands:
            got = policy.classify_shell(command)
            check(f"{verdict:7} {command}", got[0] == verdict, got)

    with Rig() as rig:
        for token in ("terminal systemctl stop NetworkManager", "terminal kill -9 1",
                      "terminal pacman -Rns linux", "terminal rm -rf ~"):
            result = rig.do(token)
            check(f"'{token}' is refused out loud", result.status == Status.BLOCKED
                  and result.message, result.message)
        check("none of them ran anything", rig.runner.calls == [], str(rig.runner.calls))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 9. BATTERY AND DEVICE QUESTIONS                        │
# └────────────────────────────────────────────────────────────────────────┘

def _supply(root, name, **files):
    path = os.path.join(root, name)
    os.makedirs(path)
    for key, value in files.items():
        with open(os.path.join(path, key), "w", encoding="utf-8") as handle:
            handle.write(str(value))


def section_battery():
    check.section("[9] Battery state, rate, time and health from the kernel")
    from kayra.platforms import linux as platform_linux

    with tempfile.TemporaryDirectory() as root:
        _supply(root, "ACAD", type="Mains", online=1)
        _supply(root, "BAT1", type="Battery", status="Charging", capacity=66,
                charge_now=2550000, charge_full=3884000, charge_full_design=4580000,
                current_now=2785000, voltage_now=16798000, cycle_count=0)
        status = platform_linux.battery_status(root)
    check("charge-based battery: charging", status["state"] == "charging", status)
    check("watts = current x voltage", status["watts"] == 46.8, status["watts"])
    check("time to full from the charge gap", status["minutes_to_full"] == 29,
          status["minutes_to_full"])
    check("health = full / design", status["health_percent"] == 85)
    check("a cycle count of 0 is 'unknown', not zero", status["cycles"] is None)
    check("AC adapter online", status["ac_online"])

    with tempfile.TemporaryDirectory() as root:
        _supply(root, "AC", type="Mains", online=0)
        _supply(root, "BAT0", type="Battery", status="Discharging", capacity=40,
                energy_now=20000000, energy_full=50000000, energy_full_design=50000000,
                power_now=10000000, cycle_count=212)
        status = platform_linux.battery_status(root)
    check("energy-based battery: discharging", status["state"] == "discharging")
    check("2 hours left at 10 W from 20 Wh", status["minutes_to_empty"] == 120)
    check("cycles reported when known", status["cycles"] == 212)

    with tempfile.TemporaryDirectory() as root:
        _supply(root, "hidpp_battery_0", type="Battery", scope="Device", status="Discharging",
                capacity=90)
        check("a mouse battery is not THE battery", platform_linux.battery_status(root) is None)
    check("no power_supply directory -> None",
          platform_linux.battery_status("/nonexistent/kayra") is None)

    sentences = {
        "charging": {"state": "charging", "percent": 66, "watts": 46.8, "minutes_to_full": 29,
                     "health_percent": 85},
        "discharging": {"state": "discharging", "percent": 40, "watts": 10.0,
                        "minutes_to_empty": 120},
        "not charging": {"state": "not charging", "percent": 80, "ac_online": True},
        "full": {"state": "full", "percent": 100},
    }
    expect = {"charging": ("charging", "until full", "health is 85"),
              "discharging": ("running on battery", "2 hours left"),
              "not charging": ("plugged in but idle", "not charging"),
              "full": ("fully charged",)}
    for state, status in sentences.items():
        spoken = linux.battery_sentence(status)
        check(f"battery sentence for '{state}' says so",
              all(word in spoken for word in expect[state]) and "66 percent" in spoken
              if state == "charging" else all(word in spoken for word in expect[state]),
              spoken)

    for token, key in (("battery", "info.battery"), ("system battery status", "info.battery"),
                       ("system is it charging", "info.battery"),
                       ("system cpu temperature", "info.temperature"),
                       ("system which wifi am i on", "info.wifi_info"),
                       ("system bluetooth devices", "info.bt_devices"),
                       ("system screen resolution", "info.displays"),
                       ("system device info", "info.summary"),
                       ("system power profile performance", "system.power_profile")):
        action = common.normalize_command(token)
        check(f"'{token}' -> {key}", action and action.key == key, action and action.key)
    check("device questions are reads the policy always allows",
          policy.classify_action(common.normalize_command("system device info"))[0] == "ALLOW")

    replies = {("nmcli", "-t"): (0, "no:Other:80\nyes:Home\\:5G:59\n")}
    with Rig(runner=RecordingRunner(ALL_TOOLS, replies)):
        spoken = linux.wifi_sentence()
    check("Wi-Fi name with an escaped colon is read whole", "Home:5G" in spoken and "59" in spoken,
          spoken)
    with Rig(FakeHyprland(monitors=[{"name": "eDP-2", "width": 2880, "height": 1800,
                                     "scale": 2.25, "refreshRate": 120.0, "focused": True}])):
        spoken = linux.displays_sentence()
    check("display answer has resolution, refresh and scale",
          "2880 by 1800" in spoken and "120 hertz" in spoken and "2.25" in spoken, spoken)
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("powerprofilesctl",),
                                    {("powerprofilesctl", "get"): (0, "balanced\n")})) as rig:
        result = rig.do("system power profile power saver")
        check("power saver sets power-saver",
              ["powerprofilesctl", "set", "power-saver"] in rig.runner.calls, result.message)


# ┌────────────────────────────────────────────────────────────────────────┐
# │          10. EMPTY WORKSPACE, SETTINGS, UNDO, CLICKING                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_workspace_settings_undo():
    check.section("[10] First empty workspace, settings screens, undo")

    used = [client("0x1", "foot", workspace=1), client("0x2", "foot", workspace=2),
            client("0x4", "foot", workspace=4), client("0x6", "foot", workspace=6),
            client("0xs", "foot", workspace=-98)]
    with Rig(FakeHyprland(used, active="0x1")) as rig:
        check("windows on 1, 2, 4, 6 -> the first empty is 3 (never 5)",
              linux.first_empty_workspace() == "3")
        result = rig.do("system workspace empty")
        check("'open an empty workspace' goes to 3",
              'hl.dsp.focus({ workspace = "3" })' in rig.hypr.dispatched, result.message)
        check("and says it is the first empty one", "workspace 3" in result.message
              and "first empty" in result.message, result.message)
        rig.do("system move window to an empty workspace")
        check("moving a window to an empty workspace also picks 3",
              any(d.startswith("hl.dsp.window.move") and 'workspace = "3"' in d
                  for d in rig.hypr.dispatched), str(rig.hypr.dispatched[-1:]))
    with Rig(FakeHyprland([client("0x1", "foot", workspace=w) for w in (1, 2, 3)])):
        check("1, 2, 3 busy -> 4", linux.first_empty_workspace() == "4")
    with Rig(FakeHyprland([])):
        check("nothing open -> 1", linux.first_empty_workspace() == "1")

    tools = ALL_TOOLS + ("omarchy-menu", "omarchy-shell")
    for phrase, argv in (("control panel", ["omarchy-menu", "toggle", "setup"]),
                         ("settings", ["omarchy-menu", "toggle", "setup"]),
                         ("wifi settings", ["omarchy-shell", "shell", "toggle", "omarchy.network"]),
                         ("bluetooth settings", ["omarchy-shell", "shell", "toggle",
                                                 "omarchy.bluetooth"]),
                         ("sound settings", ["omarchy-shell", "shell", "toggle", "omarchy.audio"])):
        with Rig(runner=RecordingRunner(tools)) as rig:
            result = rig.do(f"open {phrase}")
            check(f"'open {phrase}' opens {argv[-1]}", rig.runner.spawned == [argv],
                  str(rig.runner.spawned))
    with Rig(runner=RecordingRunner(("uwsm-app",))) as rig:
        result = rig.do("open control panel")
        check("no settings tool installed -> said, not faked",
              result.status == Status.UNSUPPORTED and not rig.runner.spawned, result.message)

    linux._UNDO.clear()
    replies = {("wpctl", "get-volume"): (0, "Volume: 0.35\n")}
    with Rig(FakeHyprland([client("0x1", "foot")], active="0x1", workspace=2),
             RecordingRunner(ALL_TOOLS, replies)) as rig:
        rig.do("system volume 80")
        result = rig.do("system undo last action")
        check("undo restores the previous volume exactly",
              ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.35"] in rig.runner.calls,
              result.message)
        rig.do("system workspace 5")
        result = rig.do("system undo that")
        check("undo after a workspace switch goes back to workspace 2",
              rig.hypr.dispatched[-1] == 'hl.dsp.focus({ workspace = "2" })', result.message)
        result = rig.do("system undo")
        check("nothing left -> says so", result.status == Status.NOT_FOUND, result.message)
        rig.do("close window")
        result = rig.do("system undo")
        check("closing records no fake undo", result.status == Status.NOT_FOUND)
    check("a bare 'undo' is still Ctrl+Z in the focused app",
          common.normalize_command("undo").key == "keyboard.hotkey")
    check("the undo stack is bounded", linux._UNDO.maxlen == linux.UNDO_LIMIT)


class FakePointer:
    def __init__(self, can=True):
        self.can = can
        self.clicks = []

    def can_click(self):
        return self.can

    def click(self, down, up):
        self.clicks.append((down, up))

    def wheel(self, delta):
        self.clicks.append(("wheel", delta))


def _tsv(*rows):
    head = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    lines = [head] + ["5\t1\t%d\t1\t%d\t1\t%d\t%d\t%d\t%d\t%s\t%s" % r for r in rows]
    return "\n".join(lines) + "\n"


def section_clicking():
    check.section("[11] Clicking by the words on screen")

    words = linux.ocr_words.__wrapped__ if hasattr(linux.ocr_words, "__wrapped__") else None
    sample = [("Save", 95, 100, 50, 40, 20, ("1", "1", "1")),
              ("As", 92, 145, 50, 20, 20, ("1", "1", "1")),
              ("Save", 30, 600, 50, 40, 20, ("2", "1", "1")),
              ("Cancel", 90, 300, 50, 60, 20, ("1", "1", "1"))]
    hits = linux.find_text(sample, "save as")
    check("a two-word phrase matches on one line", len(hits) == 1
          and hits[0][0] == (100 + 165) / 2, hits)
    check("low-confidence OCR is ignored", len(linux.find_text(sample, "save")) == 1)
    check("partial words do not match ('sav')", linux.find_text(sample, "sav") == [])

    monitor = [{"name": "eDP-1", "x": 100, "y": 0, "width": 2000, "height": 1200,
                "scale": 2.0, "focused": True}]
    tsv = _tsv((1, 1, 400, 200, 80, 40, "96", "Downloads"),
               (2, 1, 1400, 900, 80, 40, "96", "Downloads"),
               (3, 1, 600, 600, 80, 40, "96", "Delete"))
    replies = {("tesseract",): (0, tsv)}
    kayra = client("0xk", "kayra", "Kayra", at=(750, 400), size=(300, 300), focus=0)
    files = client("0xf", "org.gnome.nautilus", "Files", at=(100, 0), size=(500, 500), focus=1)

    pointer = FakePointer()
    saved = linux.POINTER
    linux.POINTER = pointer
    try:
        with Rig(FakeHyprland([kayra, files], active="0xf", monitors=monitor),
                 RecordingRunner(ALL_TOOLS + ("tesseract",), replies)) as rig:
            result = rig.do("click on downloads")
            check("the match inside Kayra's own window is ignored, the other is clicked",
                  result.ok and pointer.clicks == [(0x0002, 0x0004)], result.message)
            check("image pixels map to layout: x = 100 + 440/2, y = 220/2",
                  'hl.dsp.cursor.move({ x = 320, y = 110 })' in rig.hypr.dispatched,
                  str(rig.hypr.dispatched))
            check("the screenshot is taken of the focused monitor only",
                  any(a[:3] == ["grim", "-o", "eDP-1"] for a in rig.runner.calls))
            result = rig.do("click on delete")
            check("clicking 'Delete' asks first", result.status == Status.NEEDS_CONFIRMATION)
            check("and nothing was clicked for it", len(pointer.clicks) == 1)
            linux.CONFIRMATIONS.cancel()
            result = rig.do("click on nonexistent")
            check("text that is not on screen is a miss", result.status == Status.NOT_FOUND)
            rig.do("right click on downloads")
            check("right click uses the right button", pointer.clicks[-1] == (0x0008, 0x0010))

        no_kayra = [files]
        with Rig(FakeHyprland(no_kayra, active=None, monitors=monitor),
                 RecordingRunner(ALL_TOOLS + ("tesseract",), replies)) as rig:
            result = rig.do("click on downloads")
            check("two matches, none in the focused window -> ask",
                  result.status == Status.AMBIGUOUS, result.message)

        linux.POINTER = FakePointer(can=False)
        with Rig(FakeHyprland([files], active="0xf", monitors=monitor),
                 RecordingRunner(ALL_TOOLS + ("tesseract",), replies)) as rig:
            result = rig.do("click on downloads")
            check("without uinput: the pointer moves there and Kayra says why it can't click",
                  result.status == Status.UNSUPPORTED and "uinput" in result.message
                  and any("cursor.move" in d for d in rig.hypr.dispatched), result.message)
        linux.POINTER = pointer
        with Rig(FakeHyprland(monitors=monitor)) as rig:
            rig.do("click at 400 300")
            check("'click at 400 300' is relative to the focused monitor",
                  'hl.dsp.cursor.move({ x = 500, y = 300 })' in rig.hypr.dispatched)
            result = rig.do("click at 5000 10")
            check("a point off the screen is refused", not result.ok)
            rig.do("move mouse to center")
            check("'move mouse to center' moves without clicking",
                  'hl.dsp.cursor.move({ x = 600, y = 300 })' in rig.hypr.dispatched)
    finally:
        linux.POINTER = saved


def main():
    print_banner("LINUX AUTOMATION")
    print_info(f"Host: {describe_host()}")
    with EnvironmentGuard() as guard:
        section_shared()
        section_safety()
        section_applications()
        section_close()
        section_windows_and_keys()
        section_system()
        section_platform()
        section_hardening()
        section_battery()
        section_workspace_settings_undo()
        section_clicking()
    check.section("[12] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
