"""
Computer control: policy, target resolution and execution.

    common   - the platform-neutral pipeline (normalizer, planner, executor, timers, files)
    policy   - ALLOW / CONFIRM / DENY, confirmations, audit
    targets  - typed target resolution (Win32 windows; the website/path registries are shared)
    windows  - the Windows hands (Win32, pyautogui, AppOpener)
    linux    - the Linux hands (Hyprland IPC, wtype, PipeWire, NetworkManager, MPRIS)

Importing this package imports nothing heavy. `load_backend()` picks the hands for this OS.
"""

import sys


def backend_name(sys_platform=None) -> str:
    """'windows', 'linux' or '' for a platform with no automation backend."""
    platform = sys_platform or sys.platform
    if platform.startswith("win"):
        return "windows"
    if platform.startswith("linux"):
        return "linux"
    return ""


def load_backend():
    """
    The automation module for this OS. Raises when there is none, or when its dependencies
    are missing — the caller (app.bootstrap) reports that and runs without desktop control.
    """
    name = backend_name()
    if name == "windows":
        from kayra.automation import windows as module
    elif name == "linux":
        from kayra.automation import linux as module
    else:
        raise ImportError(f"no desktop automation backend for {sys.platform}")
    return module
