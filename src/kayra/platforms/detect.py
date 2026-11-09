# ┌────────────────────────────────────────────────────────────────────────┐
# │                        platforms/detect.py                             │
# │               Which operating system, distribution and session?        │
# └────────────────────────────────────────────────────────────────────────┘
"""
Runtime platform identification. Stdlib only, no subprocess, cached for the process lifetime.

THREE DIFFERENT QUESTIONS, ANSWERED SEPARATELY
----------------------------------------------
* FAMILY   — windows / linux / macos / other. Decides which backend module is loaded.
* DISTRO   — on Linux, `/etc/os-release` (`ID`, `ID_LIKE`, `PRETTY_NAME`). Decides installation
             advice (pacman vs apt) and is what the System screen names.
* SESSION  — on Linux, Wayland vs X11 and WHICH compositor. Decides how the active window is
             read and how the pointer is driven. Hyprland is detected by its own instance
             signature, which is the only signal that cannot be faked by a generic variable.

Omarchy is an Arch-based distribution. Recent releases identify themselves in os-release
(`ID=omarchy`, `ID_LIKE=arch`); older installs leave os-release as plain Arch and are
recognised by the Omarchy tree under `~/.local/share/omarchy` or `/usr/share/omarchy`. Both are
accepted, and `is_arch_based` is True either way.
"""

import os
import sys
import functools

FAMILY_WINDOWS = "windows"
FAMILY_LINUX = "linux"
FAMILY_MACOS = "macos"
FAMILY_OTHER = "other"


def platform_family(sys_platform=None) -> str:
    """The OS family for `sys.platform` (or the value given, for tests)."""
    value = sys.platform if sys_platform is None else sys_platform
    if value.startswith("win"):
        return FAMILY_WINDOWS
    if value.startswith("linux"):
        return FAMILY_LINUX
    if value == "darwin":
        return FAMILY_MACOS
    return FAMILY_OTHER


def parse_os_release(text) -> dict:
    """`KEY=value` lines -> dict, quotes stripped. Tolerant of comments and blank lines."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _read_os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return parse_os_release(handle.read())
        except OSError:
            continue
    return {}


def _omarchy_tree_present(home=None):
    home = home if home is not None else os.path.expanduser("~")
    return (os.path.isdir(os.path.join(home, ".local", "share", "omarchy"))
            or os.path.isdir("/usr/share/omarchy"))


class PlatformInfo:
    """What this process is running on. Plain data; build it with `current()`."""

    __slots__ = ("family", "distro_id", "distro_like", "distro_name", "distro_version",
                 "is_arch_based", "is_omarchy", "session_type", "desktop", "is_hyprland",
                 "is_wsl")

    def __init__(self, family, distro_id="", distro_like=(), distro_name="",
                 distro_version="", is_arch_based=False, is_omarchy=False, session_type="",
                 desktop="", is_hyprland=False, is_wsl=False):
        self.family = family
        self.distro_id = distro_id
        self.distro_like = tuple(distro_like)
        self.distro_name = distro_name
        self.distro_version = distro_version
        self.is_arch_based = is_arch_based
        self.is_omarchy = is_omarchy
        self.session_type = session_type        # "wayland" | "x11" | "tty" | ""
        self.desktop = desktop                  # XDG_CURRENT_DESKTOP, e.g. "Hyprland"
        self.is_hyprland = is_hyprland
        self.is_wsl = is_wsl

    @property
    def is_windows(self):
        return self.family == FAMILY_WINDOWS

    @property
    def is_linux(self):
        return self.family == FAMILY_LINUX

    @property
    def is_wayland(self):
        return self.session_type == "wayland"

    @property
    def label(self):
        """Human label: `Windows`, `Omarchy (Arch-based)`, `Ubuntu 24.04`, ..."""
        if self.family == FAMILY_WINDOWS:
            return "Windows"
        if self.family == FAMILY_MACOS:
            return "macOS"
        if self.family != FAMILY_LINUX:
            return sys.platform
        if self.is_omarchy:
            return "Omarchy (Arch-based)"
        name = self.distro_name or "Linux"
        return f"{name} {self.distro_version}".strip()

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__} | {"label": self.label}

    def __repr__(self):
        return (f"<PlatformInfo {self.family} distro={self.distro_id or '-'} "
                f"session={self.session_type or '-'} hyprland={self.is_hyprland}>")


def build_info(sys_platform=None, environ=None, os_release=None, home=None, kernel=""):
    """
    Assembles a PlatformInfo from explicit inputs. `current()` passes the real ones; the test
    suite passes synthetic ones, which is why nothing here reads global state directly.
    """
    family = platform_family(sys_platform)
    environ = os.environ if environ is None else environ
    if family != FAMILY_LINUX:
        return PlatformInfo(family)

    release = _read_os_release() if os_release is None else os_release
    distro_id = (release.get("ID") or "").lower()
    like = tuple(part for part in (release.get("ID_LIKE") or "").lower().split() if part)
    omarchy = distro_id == "omarchy" or "omarchy" in kernel.lower() or (
        distro_id in ("arch", "") and _omarchy_tree_present(home))
    arch_based = distro_id in ("arch", "omarchy", "endeavouros", "manjaro", "cachyos",
                               "garuda", "artix") or "arch" in like

    session = (environ.get("XDG_SESSION_TYPE") or "").lower()
    if not session:
        if environ.get("WAYLAND_DISPLAY"):
            session = "wayland"
        elif environ.get("DISPLAY"):
            session = "x11"
    desktop = environ.get("XDG_CURRENT_DESKTOP") or environ.get("DESKTOP_SESSION") or ""
    hyprland = bool(environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    wsl = "microsoft" in kernel.lower() or bool(environ.get("WSL_DISTRO_NAME"))

    return PlatformInfo(
        family, distro_id=distro_id, distro_like=like,
        distro_name=release.get("NAME") or release.get("PRETTY_NAME") or "Linux",
        distro_version=release.get("VERSION_ID") or "",
        is_arch_based=arch_based, is_omarchy=omarchy, session_type=session,
        desktop=desktop, is_hyprland=hyprland, is_wsl=wsl)


@functools.lru_cache(maxsize=1)
def current() -> PlatformInfo:
    """The platform this process runs on. Cached: none of it changes while a process runs."""
    kernel = ""
    try:
        kernel = os.uname().release
    except AttributeError:                  # Windows has no os.uname
        pass
    return build_info(kernel=kernel)
