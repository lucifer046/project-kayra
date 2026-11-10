# ┌────────────────────────────────────────────────────────────────────────┐
# │                        platforms/windows.py                            │
# │                  The Windows backend (user32, Explorer)                │
# └────────────────────────────────────────────────────────────────────────┘
"""
Windows implementations of the platform contract (see `kayra.platforms`).

Everything here was previously inline in the module that needed it — the pointer injector in
`input/gesture/pointer.py`, the display query in `core/hardware.py`, the reduced-motion read in
`ui/components/backdrop.py`, the Explorer vector in `memory/store.py`, the default-browser
ProgId in `input/browsers.py`. It moved here UNCHANGED in behaviour so that the Linux backend
could sit beside it with the same function names. Importing this module on another OS is safe:
nothing touches `ctypes.windll` or `winreg` until a function is called.
"""

import os
import ctypes

NAME = "windows"

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800


# ── Active window ─────────────────────────────────────────────────────────

def active_window():
    """(title, app_id) of the foreground window, or None. app_id is unknown here ("")."""
    try:
        import pygetwindow as gw
    except Exception:
        return None
    try:
        window = gw.getActiveWindow()
    except Exception:
        return None
    if window is None or not getattr(window, "title", None):
        return None
    return window.title, ""


def active_window_support():
    import importlib.util
    if importlib.util.find_spec("pygetwindow") is not None:
        return True, "pygetwindow"
    return False, "pygetwindow is not installed"


# ── Revealing a path in the file manager ─────────────────────────────────

def reveal_command(path):
    """
    `explorer.exe /select,<path>` for an existing file, else the folder. An argument VECTOR:
    `/select` and the path are ONE argument, exactly as Explorer requires.
    """
    path = os.path.abspath(path)
    if os.path.exists(path) and os.path.isfile(path):
        return ["explorer.exe", f"/select,{path}"]
    target = path if os.path.isdir(path) else os.path.dirname(path)
    return ["explorer.exe", target]


FILE_MANAGER_LABEL = "File Explorer"


# ── Accessibility ─────────────────────────────────────────────────────────

def prefers_reduced_motion():
    """True when Windows' "Show animations" is off. None when it cannot be asked."""
    try:
        SPI_GETCLIENTAREAANIMATION = 0x1042
        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(ok) and enabled.value == 0
    except Exception:
        return None


# ── Displays ──────────────────────────────────────────────────────────────

def displays():
    """
    (count, width, height, scale) for the primary display, measured.

    `GetSystemMetrics(SM_CXSCREEN)` returns the DPI-*virtualised* size unless the process
    declared per-monitor awareness — 1440x900 on a 2880x1800 panel at 200%.
    `EnumDisplaySettingsW(ENUM_CURRENT_SETTINGS)` reports the adapter's ACTUAL mode and is
    unaffected by the calling process's awareness; the scale is the ratio between the two.
    """
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        count = int(user32.GetSystemMetrics(80))          # SM_CMONITORS

        class _DEVMODE(ctypes.Structure):
            _fields_ = [("dmDeviceName", wintypes.WCHAR * 32),
                        ("dmSpecVersion", wintypes.WORD),
                        ("dmDriverVersion", wintypes.WORD),
                        ("dmSize", wintypes.WORD),
                        ("dmDriverExtra", wintypes.WORD),
                        ("dmFields", wintypes.DWORD),
                        ("dmPositionX", ctypes.c_long),
                        ("dmPositionY", ctypes.c_long),
                        ("dmDisplayOrientation", wintypes.DWORD),
                        ("dmDisplayFixedOutput", wintypes.DWORD),
                        ("dmColor", ctypes.c_short),
                        ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short),
                        ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short),
                        ("dmFormName", wintypes.WCHAR * 32),
                        ("dmLogPixels", wintypes.WORD),
                        ("dmBitsPerPel", wintypes.DWORD),
                        ("dmPelsWidth", wintypes.DWORD),
                        ("dmPelsHeight", wintypes.DWORD),
                        ("dmDisplayFlags", wintypes.DWORD),
                        ("dmDisplayFrequency", wintypes.DWORD),
                        ("dmICMMethod", wintypes.DWORD),
                        ("dmICMIntent", wintypes.DWORD),
                        ("dmMediaType", wintypes.DWORD),
                        ("dmDitherType", wintypes.DWORD),
                        ("dmReserved1", wintypes.DWORD),
                        ("dmReserved2", wintypes.DWORD),
                        ("dmPanningWidth", wintypes.DWORD),
                        ("dmPanningHeight", wintypes.DWORD)]

        mode = _DEVMODE()
        mode.dmSize = ctypes.sizeof(_DEVMODE)
        width = height = 0
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):   # ENUM_CURRENT_SETTINGS
            width, height = int(mode.dmPelsWidth), int(mode.dmPelsHeight)
        if not width or not height:
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))

        scale = 1.0
        virtual_width = int(user32.GetSystemMetrics(0))
        if virtual_width and width:
            scale = round(width / float(virtual_width), 2) or 1.0
        return max(0, count), max(0, width), max(0, height), scale
    except Exception:
        return 0, 0, 0, 1.0


# ── Default browser ───────────────────────────────────────────────────────

def default_browser_hint():
    """
    The lower-cased ProgId of the default https handler (e.g. `chromehtml`), or None.

    Read from the per-user UrlAssociations UserChoice, which is what Windows Settings writes.
    A READ of a value the user set; nothing here modifies it.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice")
        with key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except (OSError, FileNotFoundError):
        return None
    return (prog_id or "").lower() or None


# ── Pointer injection (gesture control) ──────────────────────────────────

class Win32Injector:
    """Raw user32. Cheapest available path: `SetCursorPos` is 0.012ms on the reference host."""

    name = "user32"

    def __init__(self):
        self._u32 = ctypes.windll.user32
        self._held = set()

    def screen_size(self):
        return int(self._u32.GetSystemMetrics(0)), int(self._u32.GetSystemMetrics(1))

    def move(self, x, y):
        self._u32.SetCursorPos(x, y)

    def click(self, down, up):
        # Down and up posted back to back, with no sleep. Windows synthesises the press
        # duration from the message timestamps.
        self._held.add(down)
        self._u32.mouse_event(down, 0, 0, 0, 0)
        self._u32.mouse_event(up, 0, 0, 0, 0)
        self._held.discard(down)

    def wheel(self, delta):
        self._u32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def release_buttons(self):
        """Lifts anything still logically held."""
        for down in list(self._held):
            up = MOUSEEVENTF_LEFTUP if down == MOUSEEVENTF_LEFTDOWN else MOUSEEVENTF_RIGHTUP
            try:
                self._u32.mouse_event(up, 0, 0, 0, 0)
            except Exception:
                pass
        self._held.clear()


def pointer_injector():
    """(injector, detail). The injector is None when the desktop cannot be driven."""
    try:
        return Win32Injector(), "user32"
    except Exception as exc:
        return None, f"user32 unavailable ({type(exc).__name__})"


def automation_support(which=None, compositor=None):
    """None: on Windows readiness is the import check in `core.system_profile`."""
    return None


def battery_status(root=None):
    """None: Windows reads its battery through psutil in the automation layer."""
    return None
