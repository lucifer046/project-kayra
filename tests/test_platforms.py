# ┌────────────────────────────────────────────────────────────────────────┐
# │                          test_platforms.py                             │
# │            The Platform Abstraction Layer — Portability Suite          │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_platforms.py — assertion suite for `kayra.platforms`.

    .venv/bin/python tests/test_platforms.py           (Linux)
    .venv\\Scripts\\python tests\\test_platforms.py     (Windows)

Tier 1: hardware-free and deterministic. Every OS-specific answer is driven through SYNTHETIC
inputs — os-release text, environment dicts, a fake Hyprland socket served from a temporary
directory, a mimeapps.list in a temporary XDG tree, recording transports for the pointer — so
the same verdict is reached on Windows, on Omarchy and on a CI runner with no display.

SAFETY
------
SAFE. It never moves the pointer (the injector is given recording transports), never launches
a file manager (only the argument vector is inspected), opens no device and writes only inside
a temporary directory. An `EnvironmentGuard` asserts nothing the developer owns was touched.
"""

import os
import io
import ast
import sys
import json
import socket
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, describe_host, run  # noqa: E402

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra import platforms  # noqa: E402
from kayra.platforms import detect, hyprland, linux, windows, fallback  # noqa: E402

check = Checker("platform layer")

OMARCHY_RELEASE = 'NAME="Omarchy"\nPRETTY_NAME="Omarchy"\nID=omarchy\nID_LIKE=arch\nVERSION_ID="4.5.0"\n'
ARCH_RELEASE = 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n'
UBUNTU_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
HYPR_ENV = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-1",
            "XDG_CURRENT_DESKTOP": "Hyprland", "HYPRLAND_INSTANCE_SIGNATURE": "abc_1_2"}


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         1. DETECTION                                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_detection():
    check.section("[1] Platform identification")

    check("win32 is the Windows family", detect.platform_family("win32") == "windows")
    check("linux is the Linux family", detect.platform_family("linux") == "linux")
    check("darwin is macOS (no backend yet)", detect.platform_family("darwin") == "macos")
    check("anything else is 'other'", detect.platform_family("freebsd14") == "other")

    parsed = detect.parse_os_release('# comment\nNAME="Arch Linux"\nID=arch\n\nBAD LINE\n')
    check("os-release quotes are stripped", parsed.get("NAME") == "Arch Linux", str(parsed))
    check("comments and malformed lines are ignored", set(parsed) == {"NAME", "ID"})

    with tempfile.TemporaryDirectory() as empty_home:
        omarchy = detect.build_info("linux", HYPR_ENV, detect.parse_os_release(OMARCHY_RELEASE),
                                    home=empty_home)
        check("Omarchy os-release is Omarchy", omarchy.is_omarchy)
        check("Omarchy is Arch-based", omarchy.is_arch_based)
        check("Omarchy label names it", omarchy.label.startswith("Omarchy"), omarchy.label)
        check("Hyprland is detected from its instance signature", omarchy.is_hyprland)
        check("the session is Wayland", omarchy.is_wayland)

        plain_arch = detect.build_info("linux", {"XDG_SESSION_TYPE": "x11"},
                                       detect.parse_os_release(ARCH_RELEASE), home=empty_home,
                                       kernel="6.9.1-arch1-1")
        check("plain Arch is Arch-based", plain_arch.is_arch_based)
        if not os.path.isdir("/usr/share/omarchy"):
            check("plain Arch without the Omarchy tree is not Omarchy", not plain_arch.is_omarchy)
        check("X11 is reported", plain_arch.session_type == "x11")
        check("no Hyprland without its signature", not plain_arch.is_hyprland)

        os.makedirs(os.path.join(empty_home, ".local", "share", "omarchy"))
        older = detect.build_info("linux", {}, detect.parse_os_release(ARCH_RELEASE),
                                  home=empty_home)
        check("older Omarchy (os-release still says Arch) is recognised by its tree",
              older.is_omarchy)

    kernel_only = detect.build_info("linux", {}, detect.parse_os_release(ARCH_RELEASE),
                                    home="/nonexistent", kernel="7.2.15-1-omarchy")
    check("an Omarchy kernel identifies Omarchy", kernel_only.is_omarchy)

    ubuntu = detect.build_info("linux", {"WAYLAND_DISPLAY": "wayland-0"},
                               detect.parse_os_release(UBUNTU_RELEASE), home="/nonexistent")
    check("Ubuntu is not Arch-based", not ubuntu.is_arch_based)
    check("Ubuntu is not Omarchy", not ubuntu.is_omarchy)
    check("a session inferred from WAYLAND_DISPLAY", ubuntu.session_type == "wayland")
    check("the Ubuntu label carries its version", ubuntu.label == "Ubuntu 24.04", ubuntu.label)

    wsl = detect.build_info("linux", {}, {}, home="/nonexistent",
                            kernel="5.15.153.1-microsoft-standard-WSL2")
    check("WSL is recognised from the kernel", wsl.is_wsl)

    win = detect.build_info("win32", {}, {})
    check("Windows info carries no distro", win.is_windows and not win.distro_id)
    check("Windows label", win.label == "Windows")

    current = platforms.current()
    check("current() matches sys.platform",
          current.family == detect.platform_family(sys.platform), repr(current))
    expected = {"windows": "kayra.platforms.windows", "linux": "kayra.platforms.linux"}.get(
        current.family, "kayra.platforms.fallback")
    check("backend() is the module for this OS", platforms.backend().__name__ == expected,
          platforms.backend().__name__)
    print_info(f"      this host: {current.label}  ·  {current!r}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                2. HYPRLAND IPC AGAINST A FAKE COMPOSITOR               │
# └────────────────────────────────────────────────────────────────────────┘

class FakeHyprland:
    """A Unix socket answering Hyprland's request protocol from a fixed table."""

    def __init__(self, root, signature="sig_test", replies=None):
        self.signature = signature
        self.dir = os.path.join(root, "hypr", signature)
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, ".socket.sock")
        self.replies = replies or {}
        self.requests = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(4096).decode()
                self.requests.append(data)
                reply = self.replies.get(data, "unknown request")
                conn.sendall(reply.encode())

    def close(self):
        self._stop = True
        try:
            self._server.close()
        except OSError:
            pass


def section_hyprland():
    check.section("[2] Hyprland IPC")

    if not hasattr(socket, "AF_UNIX"):
        check.skip("Hyprland IPC", "no AF_UNIX sockets on this platform")
        return

    env_none = {}
    check("no signature -> no socket", hyprland.socket_path(env_none) is None)

    monitors = [{"name": "eDP-1", "width": 2880, "height": 1800, "scale": 2.25,
                 "focused": True, "x": 0, "y": 0, "transform": 0},
                {"name": "HDMI-A-1", "width": 1920, "height": 1080, "scale": 1.0,
                 "focused": False, "x": 1280, "y": 0, "transform": 0}]
    replies = {
        "j/activewindow": json.dumps({"class": "google-chrome", "title": "Docs - Google Chrome"}),
        "j/monitors": json.dumps(monitors),
        "j/getoption animations:enabled": json.dumps({"option": "animations:enabled",
                                                      "int": 0, "set": True}),
        "dispatch movecursor 100 200": "ok",
    }
    # AF_UNIX paths are limited to ~108 bytes; the scratch dir can be long, so use /tmp.
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as root:
        server = FakeHyprland(root, replies=replies)
        try:
            env = {"HYPRLAND_INSTANCE_SIGNATURE": server.signature, "XDG_RUNTIME_DIR": root}
            path = hyprland.socket_path(env)
            check("the socket path follows XDG_RUNTIME_DIR/hypr/<sig>", path == server.path,
                  str(path))
            check("available() is True with a live socket", hyprland.available(env))

            check("active window is (title, class)",
                  hyprland.active_window(path) == ("Docs - Google Chrome", "google-chrome"))
            check("monitors are decoded", len(hyprland.monitors(path)) == 2)
            check("animations:enabled int 0 means disabled",
                  hyprland.animations_enabled(path) is False)
            check("movecursor is acknowledged", hyprland.move_cursor(100, 200, path))
            check("an unexpected reply is not an acknowledgement",
                  not hyprland.move_cursor(5, 5, path))
            check("non-JSON replies decode to None",
                  hyprland.request_json("bogus", path) is None)
        finally:
            server.close()

        check("an unreachable socket answers None, never raises",
              hyprland.request("j/monitors", os.path.join(root, "missing.sock")) is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. REVEAL, BROWSER, DISPLAYS                       │
# └────────────────────────────────────────────────────────────────────────┘

def section_capabilities():
    check.section("[3] Capability contracts")

    with tempfile.TemporaryDirectory() as root:
        target = os.path.join(root, "conversation.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("[]")

        vector = windows.reveal_command(target)
        check("Windows reveal is ONE /select argument", vector == ["explorer.exe",
                                                                   f"/select,{target}"])
        check("Windows reveals a missing file's folder",
              windows.reveal_command(os.path.join(root, "nope.json")) == ["explorer.exe", root])

        vector = linux.reveal_command(target)
        if vector is None:
            check.skip("Linux reveal vector", "no gdbus/xdg-open on this host")
        else:
            check("Linux reveal is a list (never a shell string)", isinstance(vector, list))
            check("Linux reveal uses gdbus FileManager1 or xdg-open",
                  os.path.basename(vector[0]) in ("gdbus", "xdg-open"), str(vector))
            joined = " ".join(vector)
            check("the path reaches the command", root in joined, joined)
        quoted = os.path.join(root, "it's here.json")
        with open(quoted, "w", encoding="utf-8") as handle:
            handle.write("[]")
        vector = linux.reveal_command(quoted)
        if vector and os.path.basename(vector[0]) == "gdbus":
            check("a quote in the path cannot break the GVariant literal",
                  "'" not in vector[-2][2:-2], vector[-2])

        # Default browser from a synthetic XDG tree.
        config = os.path.join(root, "config")
        os.makedirs(config)
        with open(os.path.join(config, "mimeapps.list"), "w", encoding="utf-8") as handle:
            handle.write("[Added Associations]\nx-scheme-handler/https=brave.desktop;\n"
                         "[Default Applications]\nx-scheme-handler/https=chromium.desktop\n")
        saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
                                                  "XDG_DATA_HOME")}
        os.environ["XDG_CONFIG_HOME"] = config
        os.environ["XDG_CONFIG_DIRS"] = os.path.join(root, "nothing")
        os.environ["XDG_DATA_HOME"] = os.path.join(root, "nothing")
        try:
            check("the default https handler comes from [Default Applications]",
                  linux.default_browser_hint() == "chromium.desktop",
                  str(linux.default_browser_hint()))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    count, width, height, scale = platforms.displays()
    check("displays() returns four numbers", all(isinstance(v, (int, float))
                                                  for v in (count, width, height, scale)))
    check("an unmeasurable display is zeros, never 1920x1080",
          fallback.displays() == (0, 0, 0, 1.0))
    check("the fallback backend answers 'unavailable' everywhere",
          fallback.active_window() is None and fallback.reveal_command("/x") is None
          and fallback.pointer_injector()[0] is None)
    supported, detail = platforms.active_window_support()
    check("active_window_support() explains itself", isinstance(supported, bool) and detail,
          detail)
    reduced = platforms.prefers_reduced_motion()
    check("reduced motion is tri-state", reduced in (True, False, None), str(reduced))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      4. THE LINUX POINTER INJECTOR                     │
# └────────────────────────────────────────────────────────────────────────┘

class RecordingIPC:
    def __init__(self, mons):
        self._mons = mons
        self.moves = []

    def monitors(self):
        return self._mons

    def move_cursor(self, x, y):
        self.moves.append((x, y))
        return True


class RecordingButtons:
    def __init__(self):
        self.frames = []

    def emit(self, events):
        self.frames.append(list(events))

    def close(self):
        pass


def section_injector():
    check.section("[4] Linux pointer injector (recording transports)")

    mons = [{"width": 2880, "height": 1800, "scale": 2.25, "focused": False,
             "x": 0, "y": 0, "transform": 0},
            {"width": 1920, "height": 1080, "scale": 1.0, "focused": True,
             "x": 1280, "y": 0, "transform": 0}]
    ipc, buttons = RecordingIPC(mons), RecordingButtons()
    inj = linux.LinuxInjector(ipc=ipc, buttons=buttons)
    check("the focused monitor's LOGICAL size is the screen",
          inj.screen_size() == (1920, 1080), str(inj.screen_size()))
    inj.move(10, 20)
    check("moves are offset to the focused monitor's origin", ipc.moves == [(1290, 20)],
          str(ipc.moves))

    scaled = linux.LinuxInjector(ipc=RecordingIPC([dict(mons[0], focused=True)]),
                                 buttons=RecordingButtons())
    check("a 2.25x panel maps to its logical 1280x800", scaled.screen_size() == (1280, 800),
          str(scaled.screen_size()))
    rotated = linux.LinuxInjector(ipc=RecordingIPC([dict(mons[1], transform=1)]),
                                  buttons=RecordingButtons())
    check("a rotated monitor swaps width and height", rotated.screen_size() == (1080, 1920))

    inj.click(0x0002, 0x0004)
    check("a left click is press then release, as separate frames",
          buttons.frames[-2:] == [[(0x01, 0x110, 1)], [(0x01, 0x110, 0)]],
          str(buttons.frames[-2:]))
    inj.click(0x0008, 0x0010)
    check("a right click maps to BTN_RIGHT", buttons.frames[-2][0][1] == 0x111)
    inj.wheel(360)
    check("360 Windows wheel units are three notches", buttons.frames[-1] == [(0x02, 0x08, 3)])
    inj.wheel(-40)
    check("a small negative delta is still one notch down",
          buttons.frames[-1] == [(0x02, 0x08, -1)])
    check("nothing is left held", not inj._held)

    no_clicks = linux.LinuxInjector(ipc=RecordingIPC(mons), buttons=None,
                                    screen=(1920, 1080))
    no_clicks._buttons = None
    no_clicks._buttons_error = "no write access to /dev/uinput"
    try:
        no_clicks.click(0x0002, 0x0004)
        raised = False
    except OSError as exc:
        raised = "uinput" in str(exc)
    check("without uinput a click RAISES with the reason (counted, never silent)", raised)

    from kayra.input.gesture.pointer import PointerController
    controller = PointerController(no_clicks)
    controller.enable()
    controller.left_click()
    check("PointerController counts the failure instead of crashing",
          controller.failures == 1 and "uinput" in controller.last_error,
          controller.last_error)
    controller.disable()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         5. STRUCTURAL RULES                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_structure():
    check.section("[5] The layer stays a leaf")

    package_dir = os.path.dirname(platforms.__file__)
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        source = io.open(os.path.join(package_dir, name), encoding="utf-8").read()
        tree = ast.parse(source)
        bad = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and (node.module or "").startswith("kayra")
               and not (node.module or "").startswith("kayra.platforms")
               and not (node.module == "kayra" and all(a.name == "platforms"
                                                       for a in node.names))]
        check(f"{name} imports nothing from kayra outside the layer", not bad, str(bad))
        imports = {alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        check(f"{name} spawns no subprocess", "subprocess" not in imports)
        shell_true = [n for n in ast.walk(tree) if isinstance(n, ast.keyword)
                      and n.arg == "shell" and getattr(n.value, "value", None) is True]
        check(f"{name} never uses shell=True", not shell_true)

    for module in (windows, linux, fallback):
        for function in ("active_window", "active_window_support", "reveal_command",
                         "prefers_reduced_motion", "displays", "default_browser_hint",
                         "pointer_injector", "automation_support", "battery_status"):
            check(f"{module.__name__.rsplit('.', 1)[-1]} implements {function}",
                  callable(getattr(module, function, None)))

    # The shared modules that used to carry Windows-only code now route through the layer.
    import kayra.services.proactive_agent as agent
    import kayra.memory.store as store
    check("the proactive agent no longer imports pygetwindow directly",
          "import pygetwindow" not in io.open(agent.__file__, encoding="utf-8").read())
    check("the memory store asks the layer for its reveal command",
          "platforms.reveal_command" in io.open(store.__file__, encoding="utf-8").read())


def main():
    print_banner("PLATFORM LAYER")
    print_info(f"Host: {describe_host()}")

    with EnvironmentGuard() as guard:
        section_detection()
        section_hyprland()
        section_capabilities()
        section_injector()
        section_structure()

    check.section("[6] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
