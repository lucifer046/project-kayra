# ┌────────────────────────────────────────────────────────────────────────┐
# │                         platforms/linux.py                             │
# │      The Linux backend — Hyprland/Omarchy first, generic fallbacks      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Linux implementations of the platform contract (see `kayra.platforms`).

PRIORITY ORDER
--------------
Omarchy runs Hyprland on Wayland, and that is the first-class target: the active window, the
monitor layout, the animation preference and pointer movement all come from Hyprland's IPC
socket (`platforms.hyprland`) — no subprocess, no X server. Other sessions degrade honestly:

| Capability          | Hyprland                     | Other Linux                          |
|---------------------|------------------------------|--------------------------------------|
| active window       | IPC `activewindow`           | unavailable (reported, not guessed)  |
| displays            | IPC `monitors` (real scale)  | `/sys/class/drm` connector count     |
| reduced motion      | IPC `animations:enabled`     | `KAYRA_REDUCED_MOTION` only          |
| pointer move        | IPC `dispatch movecursor`    | uinput absolute device               |
| click / scroll      | uinput (needs /dev/uinput)   | uinput (needs /dev/uinput)           |
| reveal in folder    | FileManager1 D-Bus, xdg-open | FileManager1 D-Bus, xdg-open         |

GENERIC WAYLAND HAS NO PORTABLE "ACTIVE WINDOW" QUERY, and that is a protocol decision, not a
missing library: Wayland deliberately does not let one client read another's title. Hyprland
offers it through its own IPC; GNOME and KDE do not offer it to ordinary clients at all. The
proactive agent already runs on its time and habit signals when the context signal is absent,
so that is what happens there — the same path the Windows build takes without pygetwindow.

UINPUT NEEDS PERMISSION. `/dev/uinput` is root-only by default. `docs/platforms/LINUX.md`
gives the one udev rule that grants it to the logged-in user; without it gesture control still
MOVES the pointer on Hyprland and reports clicks as unavailable rather than failing silently.
"""

import os
import glob
import shutil
from urllib.parse import quote

from kayra.platforms import hyprland

NAME = "linux"
FILE_MANAGER_LABEL = "the file manager"

# Event codes from linux/input-event-codes.h. Written out so this module does not need evdev
# merely to be imported.
_EV_KEY, _EV_REL, _EV_ABS, _EV_SYN = 0x01, 0x02, 0x03, 0x00
_BTN_LEFT, _BTN_RIGHT = 0x110, 0x111
_REL_WHEEL = 0x08
_ABS_X, _ABS_Y = 0x00, 0x01

# The Win32 MOUSEEVENTF_* values `PointerController` passes in. Mapped here so the controller
# stays platform-neutral and the Windows injector keeps its exact historical behaviour.
_DOWN_TO_BUTTON = {0x0002: _BTN_LEFT, 0x0008: _BTN_RIGHT}


# ── Active window ─────────────────────────────────────────────────────────

def active_window():
    """(title, app_class) of the focused window on Hyprland, else None."""
    if not hyprland.available():
        return None
    return hyprland.active_window()


def active_window_support():
    if hyprland.available():
        return True, "Hyprland IPC"
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower() or "unknown"
    return False, (f"no active-window query on this {session} session "
                   "(supported on Hyprland)")


# ── Revealing a path ──────────────────────────────────────────────────────

def _file_manager1_available():
    for base in ("/usr/share/dbus-1/services", "/usr/local/share/dbus-1/services",
                 os.path.expanduser("~/.local/share/dbus-1/services")):
        if os.path.exists(os.path.join(base, "org.freedesktop.FileManager1.service")):
            return True
    return False


def reveal_command(path):
    """
    An argument VECTOR that reveals `path`, or None when no opener exists.

    An existing file is SELECTED through the freedesktop FileManager1 interface when a file
    manager provides it (Nautilus does, on Omarchy); anything else opens the folder with
    `xdg-open`. The URI is percent-encoded, so a quote in the path cannot break out of the
    GVariant literal.
    """
    path = os.path.abspath(path)
    if os.path.isfile(path) and shutil.which("gdbus") and _file_manager1_available():
        uri = "file://" + quote(path)
        return ["gdbus", "call", "--session",
                "--dest", "org.freedesktop.FileManager1",
                "--object-path", "/org/freedesktop/FileManager1",
                "--method", "org.freedesktop.FileManager1.ShowItems",
                f"['{uri}']", ""]
    target = path if os.path.isdir(path) else os.path.dirname(path)
    opener = shutil.which("xdg-open")
    return [opener, target] if opener else None


# ── Accessibility ─────────────────────────────────────────────────────────

def prefers_reduced_motion():
    """True when the compositor's animations are off (Hyprland). None when unknowable."""
    if hyprland.available():
        enabled = hyprland.animations_enabled()
        return None if enabled is None else not enabled
    return None


# ── Displays ──────────────────────────────────────────────────────────────

def _drm_connected():
    count, width, height = 0, 0, 0
    for status in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            with open(status, "r", encoding="ascii") as handle:
                if handle.read().strip() != "connected":
                    continue
        except OSError:
            continue
        count += 1
        if not width:
            try:
                with open(os.path.join(os.path.dirname(status), "modes"), "r",
                          encoding="ascii") as handle:
                    first = handle.readline().strip()        # preferred mode, e.g. 2880x1800
                w, _, h = first.partition("x")
                width, height = int(w), int(h.rstrip("ip"))
            except (OSError, ValueError):
                pass
    return count, width, height


def displays():
    """
    (count, width, height, scale) for the focused/primary display, measured.

    Hyprland reports each monitor's CURRENT mode and its scale. Without it, `/sys/class/drm`
    gives the connector count and the PREFERRED mode — physical pixels, scale unknown (1.0).
    `(0, 0, 0, 1.0)` when nothing answers; never a fabricated 1920x1080.
    """
    if hyprland.available():
        mons = hyprland.monitors()
        if mons:
            primary = next((m for m in mons if m.get("focused")), mons[0])
            try:
                return (len(mons), int(primary.get("width") or 0),
                        int(primary.get("height") or 0),
                        round(float(primary.get("scale") or 1.0), 2))
            except (TypeError, ValueError):
                pass
    count, width, height = _drm_connected()
    return count, width, height, 1.0


# ── Default browser ───────────────────────────────────────────────────────

def default_browser_hint():
    """
    The lower-cased desktop id of the default https handler (`google-chrome.desktop`), or None.

    Read from `mimeapps.list` in the XDG search order — the file `xdg-settings` itself writes —
    rather than by spawning `xdg-settings`, which is a shell script.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dirs = [config_home] + (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    candidates = [os.path.join(d, "mimeapps.list") for d in dirs]
    candidates += [os.path.join(data_home, "applications", "mimeapps.list"),
                   "/usr/share/applications/mimeapps.list"]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        section = ""
        for raw in lines:
            line = raw.strip()
            if line.startswith("["):
                section = line
                continue
            if section in ("[Default Applications]", "") and \
                    line.lower().startswith("x-scheme-handler/https="):
                value = line.split("=", 1)[1].split(";")[0].strip()
                if value:
                    return value.lower()
    return None


# ── Pointer injection (gesture control) ──────────────────────────────────

def uinput_access():
    """(ok, detail) — whether this user may create a uinput device."""
    if not os.path.exists("/dev/uinput"):
        return False, "/dev/uinput does not exist (load the uinput module)"
    if not os.access("/dev/uinput", os.W_OK):
        return False, "no write access to /dev/uinput (see docs/platforms/LINUX.md)"
    import importlib.util
    if importlib.util.find_spec("evdev") is None:
        return False, "python-evdev is not installed"
    return True, "uinput"


class _UInputButtons:
    """A virtual mouse exposing buttons and the wheel (and, optionally, absolute X/Y)."""

    def __init__(self, absolute_size=None):
        from evdev import UInput, AbsInfo, ecodes
        caps = {ecodes.EV_KEY: [_BTN_LEFT, _BTN_RIGHT], ecodes.EV_REL: [_REL_WHEEL]}
        if absolute_size:
            w, h = absolute_size
            caps[ecodes.EV_ABS] = [
                (_ABS_X, AbsInfo(value=0, min=0, max=max(1, w - 1), fuzz=0, flat=0,
                                 resolution=0)),
                (_ABS_Y, AbsInfo(value=0, min=0, max=max(1, h - 1), fuzz=0, flat=0,
                                 resolution=0)),
            ]
        self._ui = UInput(caps, name="kayra-gesture-pointer")

    def emit(self, events):
        for etype, code, value in events:
            self._ui.write(etype, code, value)
        self._ui.syn()

    def close(self):
        try:
            self._ui.close()
        except Exception:
            pass


class LinuxInjector:
    """
    Hyprland IPC for movement, uinput for buttons and the wheel.

    Each capability is reported separately. On a Hyprland session without uinput permission the
    pointer still follows the hand and `click`/`wheel` raise, which `PointerController` counts
    as a failure and logs once — the user is told why clicks do nothing instead of guessing.
    """

    name = "linux"

    def __init__(self, ipc=None, buttons=None, screen=None):
        self._ipc = ipc if ipc is not None else (hyprland if hyprland.available() else None)
        self._origin = (0, 0)
        self._screen = screen or self._layout_size()
        self._held = set()
        self._buttons = buttons
        self._buttons_error = ""
        if self._buttons is None:
            ok, detail = uinput_access()
            if ok:
                try:
                    absolute = None if self._ipc is not None else self._screen
                    self._buttons = _UInputButtons(absolute_size=absolute)
                except Exception as exc:
                    self._buttons_error = f"uinput device failed ({type(exc).__name__})"
            else:
                self._buttons_error = detail
        self.name = "+".join(part for part in (
            "hyprland-ipc" if self._ipc is not None else "",
            "uinput" if self._buttons is not None else "") if part) or "unavailable"

    def _layout_size(self):
        """The focused monitor in LOGICAL pixels (what `movecursor` addresses)."""
        if self._ipc is not None:
            mons = self._ipc.monitors()
            if mons:
                mon = next((m for m in mons if m.get("focused")), mons[0])
                try:
                    scale = float(mon.get("scale") or 1.0) or 1.0
                    width = int(round(int(mon.get("width") or 0) / scale))
                    height = int(round(int(mon.get("height") or 0) / scale))
                    if int(mon.get("transform") or 0) % 2:      # rotated 90/270
                        width, height = height, width
                    self._origin = (int(mon.get("x") or 0), int(mon.get("y") or 0))
                    if width and height:
                        return width, height
                except (TypeError, ValueError):
                    pass
        _, width, height, _ = displays()
        return (width or 1920, height or 1080)

    @property
    def can_click(self):
        return self._buttons is not None

    @property
    def detail(self):
        return self._buttons_error

    def screen_size(self):
        return self._screen

    def move(self, x, y):
        if self._ipc is not None:
            if not self._ipc.move_cursor(self._origin[0] + x, self._origin[1] + y):
                raise OSError("Hyprland did not accept movecursor")
            return
        if self._buttons is None:
            raise OSError(self._buttons_error or "no pointer backend")
        self._buttons.emit([(_EV_ABS, _ABS_X, int(x)), (_EV_ABS, _ABS_Y, int(y))])

    def click(self, down, up):
        if self._buttons is None:
            raise OSError(self._buttons_error or "clicks unavailable")
        button = _DOWN_TO_BUTTON.get(down, _BTN_LEFT)
        self._held.add(button)
        self._buttons.emit([(_EV_KEY, button, 1)])
        self._buttons.emit([(_EV_KEY, button, 0)])
        self._held.discard(button)

    def wheel(self, delta):
        if self._buttons is None:
            raise OSError(self._buttons_error or "scrolling unavailable")
        # Windows wheel units are 120 per notch; evdev REL_WHEEL counts notches.
        notches = int(delta / 120) or (1 if delta > 0 else -1 if delta < 0 else 0)
        if notches:
            self._buttons.emit([(_EV_REL, _REL_WHEEL, notches)])

    def release_buttons(self):
        if self._buttons is None:
            return
        for button in list(self._held):
            try:
                self._buttons.emit([(_EV_KEY, button, 0)])
            except Exception:
                pass
        self._held.clear()

    def close(self):
        if self._buttons is not None:
            self._buttons.close()


def pointer_injector():
    """(injector, detail). None when neither Hyprland IPC nor uinput can drive the pointer."""
    injector = LinuxInjector()
    if injector.name == "unavailable":
        return None, injector.detail or "no pointer backend"
    detail = injector.name
    if not injector.can_click:
        detail += f" (clicks unavailable: {injector.detail})"
    return injector, detail


# ── Desktop automation readiness ──────────────────────────────────────────
# What `kayra.automation.linux` drives, grouped by how much its absence costs. Each entry is
# (label, any-of commands). `shutil.which` only — no process is spawned to answer this.
AUTOMATION_CORE = (
    ("keyboard input", ("wtype", "ydotool")),
    ("app launching", ("uwsm-app", "gtk-launch")),
    ("opening links and files", ("xdg-open",)),
    ("volume", ("wpctl", "pamixer")),
)
AUTOMATION_EXTRAS = (
    ("media control", ("playerctl", "gdbus")),
    ("brightness", ("brightnessctl",)),
    ("screenshots", ("grim",)),
    ("clipboard", ("wl-copy",)),
    ("Wi-Fi", ("nmcli",)),
    ("Bluetooth", ("bluetoothctl",)),
    ("notifications", ("notify-send",)),
)


def automation_support(which=None, compositor=None):
    """
    {"compositor": bool, "missing_core": [labels], "missing_extras": [labels]}.

    The compositor is what window control needs: Hyprland's IPC. Without it applications,
    media, volume and the rest still work; focusing, closing and moving windows do not, and
    the finding says so instead of reporting the whole layer as missing.
    """
    which = which or shutil.which
    compositor = hyprland.available() if compositor is None else compositor
    missing = lambda table: [label for label, cmds in table
                             if not any(which(c) for c in cmds)]
    return {"compositor": bool(compositor), "missing_core": missing(AUTOMATION_CORE),
            "missing_extras": missing(AUTOMATION_EXTRAS)}


# ── Battery, read straight from the kernel ────────────────────────────────
# psutil reports percent and "plugged in"; the kernel also knows WHICH of charging,
# discharging, full and "plugged in but not charging" it is, how fast, and how worn the cell
# is. Units in sysfs are micro-: µWh/µW for energy batteries, µAh/µA/µV for charge ones.

POWER_SUPPLY_DIR = "/sys/class/power_supply"


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _num(path):
    value = _read(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def battery_status(root=None):
    """
    The first battery's state as a dict, or None when the machine has none.

    state: "charging" | "discharging" | "not charging" | "full" | "unknown"
    percent, ac_online, watts (None if unreported), minutes_to_empty / minutes_to_full,
    health_percent (full capacity vs design), cycles (None when the firmware says 0).
    """
    root = root or POWER_SUPPLY_DIR
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return None
    battery = next((os.path.join(root, e) for e in entries
                    if _read(os.path.join(root, e, "type")) == "Battery"
                    and _read(os.path.join(root, e, "scope")) != "Device"), None)
    if battery is None:
        return None
    ac_online = any(_read(os.path.join(root, e, "online")) == "1" for e in entries
                    if _read(os.path.join(root, e, "type")) in ("Mains", "USB", "USB_C",
                                                                "USB_PD"))
    raw_state = (_read(os.path.join(battery, "status")) or "Unknown").lower()
    state = {"charging": "charging", "discharging": "discharging", "full": "full",
             "not charging": "not charging"}.get(raw_state, "unknown")

    def field(name):
        return _num(os.path.join(battery, name))

    now, full, design = field("energy_now"), field("energy_full"), field("energy_full_design")
    rate = field("power_now")                                   # µW
    if now is None:                                              # a charge-based battery
        now, full, design = field("charge_now"), field("charge_full"), field("charge_full_design")
        current, voltage = field("current_now"), field("voltage_now")
        rate_charge = current
        watts = (current * voltage / 1e12) if current and voltage else None
    else:
        rate_charge = rate
        watts = rate / 1e6 if rate else None

    percent = field("capacity")
    if percent is None and now and full:
        percent = round(100 * now / full)
    to_empty = to_full = None
    if rate_charge and now is not None:
        if state == "discharging":
            to_empty = round(60 * now / rate_charge)
        elif state == "charging" and full:
            to_full = round(60 * max(0, full - now) / rate_charge)
    cycles = field("cycle_count")
    return {
        "state": state, "percent": percent, "ac_online": ac_online,
        "watts": round(watts, 1) if watts else None,
        "minutes_to_empty": to_empty, "minutes_to_full": to_full,
        "health_percent": round(100 * full / design) if full and design else None,
        "cycles": cycles or None,
        "level": _read(os.path.join(battery, "capacity_level")),
        "name": os.path.basename(battery),
    }


# Chromium-family browsers keep every profile under one "user data" folder, whose `Local State`
# file lists the profiles (Part 5P). Same format for all four; only the folder differs.
_CHROMIUM_DATA = (("google-chrome", "google-chrome"), ("chromium", "chromium"),
                  ("brave", os.path.join("BraveSoftware", "Brave-Browser")),
                  ("microsoft-edge", "microsoft-edge"))


def browser_user_data_dirs():
    """[(browser key, user-data folder)] for the Chromium-family browsers, in preference order.
    Folders are listed whether or not they exist; the caller checks."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [(key, os.path.join(config_home, sub)) for key, sub in _CHROMIUM_DATA]
