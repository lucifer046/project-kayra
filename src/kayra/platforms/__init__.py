# ┌────────────────────────────────────────────────────────────────────────┐
# │                        platforms/__init__.py                           │
# │                 The Platform Abstraction Layer (façade)                │
# └────────────────────────────────────────────────────────────────────────┘
"""
The ONE place Kayra's shared code asks an operating-system question.

    from kayra import platforms

    platforms.current()                 # PlatformInfo: family, distro, session, Omarchy?
    platforms.active_window()           # (title, app_id) or None
    platforms.reveal_command(path)      # argument vector for the file manager, or None
    platforms.prefers_reduced_motion()  # True / False / None (unknown)
    platforms.displays()                # (count, width, height, scale)
    platforms.default_browser_hint()    # ProgId (Windows) / desktop id (Linux) / None
    platforms.pointer_injector()        # (injector, detail) for gesture control

WHY A LAYER AND NOT `if sys.platform` AT EACH CALL SITE
-------------------------------------------------------
Before this package, OS knowledge was scattered through `core/hardware.py`,
`input/browsers.py`, `input/gesture/pointer.py`, `memory/store.py`,
`services/proactive_agent.py` and `ui/components/backdrop.py`, each with its own Windows
branch and its own idea of what "not Windows" should do. Adding Linux to each of them would
have doubled that. Every backend module (`windows`, `linux`, `fallback`) exports the SAME
function names, and this module picks one at first use.

RULES
-----
* **A leaf, below `core`.** Stdlib only (plus optional, lazily-imported `evdev` /
  `pygetwindow` inside function bodies), and it imports nothing from the rest of `kayra`. That
  is what lets `core.hardware` — itself a leaf — delegate here.
* **No import-time side effects.** Importing this package does not load a backend; the first
  call does. Importing `platforms.windows` on Linux is safe and vice versa.
* **Absent is reported, never invented.** A capability a platform cannot provide returns
  None / `(False, reason)`; callers already render that as "unavailable".
* **Automation is NOT routed through here.** `kayra.automation.load_backend()` picks the
  hands (`automation.windows` / `automation.linux`); this package only answers the readiness
  question (`automation_support`) and supplies the Hyprland IPC client both use.
"""

import functools
import importlib

from kayra.platforms.detect import (PlatformInfo, current, platform_family, build_info,
                                    parse_os_release, FAMILY_WINDOWS, FAMILY_LINUX,
                                    FAMILY_MACOS, FAMILY_OTHER)

_BACKENDS = {
    FAMILY_WINDOWS: "kayra.platforms.windows",
    FAMILY_LINUX: "kayra.platforms.linux",
}


@functools.lru_cache(maxsize=1)
def backend():
    """The backend module for this OS (`windows`, `linux`, or `fallback`)."""
    name = _BACKENDS.get(current().family, "kayra.platforms.fallback")
    return importlib.import_module(name)


def is_windows() -> bool:
    return current().is_windows


def is_linux() -> bool:
    return current().is_linux


# ── Capability façade. Thin on purpose: the backend owns the behaviour. ──

def active_window():
    """(title, app_id) of the focused window, or None when unknown or unsupported."""
    try:
        return backend().active_window()
    except Exception:
        return None


def active_window_support():
    """(supported, detail) — whether `active_window()` can ever answer on this session."""
    try:
        return backend().active_window_support()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def reveal_command(path):
    """Argument vector that reveals `path` in the file manager, or None."""
    return backend().reveal_command(path)


def file_manager_label() -> str:
    return getattr(backend(), "FILE_MANAGER_LABEL", "the file manager")


def prefers_reduced_motion():
    """True / False from the platform's own accessibility setting, None when unknown."""
    try:
        return backend().prefers_reduced_motion()
    except Exception:
        return None


def displays():
    """(count, width, height, scale) for the primary display; zeros when unmeasurable."""
    try:
        return backend().displays()
    except Exception:
        return 0, 0, 0, 1.0


def default_browser_hint():
    try:
        return backend().default_browser_hint()
    except Exception:
        return None


def browser_user_data_dirs():
    """[(browser key, user-data folder)] for Chromium-family browsers; [] when unknown."""
    try:
        return list(backend().browser_user_data_dirs())
    except Exception:
        return []


def pointer_injector():
    """(injector, detail). The injector is None when the desktop cannot be driven."""
    try:
        return backend().pointer_injector()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def battery_status():
    """Detailed battery state (Linux sysfs), or None when unknown / no battery."""
    try:
        return backend().battery_status()
    except Exception:
        return None


def automation_support():
    """Linux: which desktop-automation tools are present. Windows: None (checked elsewhere)."""
    try:
        return backend().automation_support()
    except Exception:
        return None


__all__ = [
    "PlatformInfo", "current", "platform_family", "build_info", "parse_os_release",
    "FAMILY_WINDOWS", "FAMILY_LINUX", "FAMILY_MACOS", "FAMILY_OTHER",
    "backend", "is_windows", "is_linux", "active_window", "active_window_support",
    "reveal_command", "file_manager_label", "prefers_reduced_motion", "displays",
    "default_browser_hint", "pointer_injector", "automation_support",
    "battery_status", "browser_user_data_dirs",
]
