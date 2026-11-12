# ┌────────────────────────────────────────────────────────────────────────┐
# │                        platforms/fallback.py                           │
# │            Any other OS (macOS today): every capability absent         │
# └────────────────────────────────────────────────────────────────────────┘
"""
The backend for an operating system Kayra has no implementation for yet. Every function
answers "not available" — never a fabricated value — so the rest of the application runs with
those features off and says so. A future macOS backend replaces this module for `darwin`
without any caller changing.
"""

NAME = "unsupported"
FILE_MANAGER_LABEL = "the file manager"


def active_window():
    return None


def active_window_support():
    return False, "no active-window backend for this operating system"


def reveal_command(path):
    return None


def prefers_reduced_motion():
    return None


def displays():
    return 0, 0, 0, 1.0


def default_browser_hint():
    return None


def pointer_injector():
    return None, "no pointer backend for this operating system"


def automation_support(which=None, compositor=None):
    return {"compositor": False, "missing_core": ["a desktop automation backend"],
            "missing_extras": []}


def battery_status(root=None):
    return None


def browser_user_data_dirs():
    return []
