# ┌────────────────────────────────────────────────────────────────────────┐
# │                              paths.py                                  │
# │                  The Single Source of Truth for Paths                  │
# └────────────────────────────────────────────────────────────────────────┘
"""
Every filesystem location Kayra uses resolves through this module.

WHY THIS EXISTS
---------------
Before the package reorganisation, several modules computed their own project root with
`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` — a expression whose answer
depends on how deeply nested the file happens to be. When files moved, those expressions
silently pointed somewhere else. Worse, some code used bare relative paths like
`"data\\conversation.json"`, which resolve against the CURRENT WORKING DIRECTORY: launching
Kayra from anywhere but the project folder silently fragmented the assistant's memory across
several files.

Both classes of bug are structurally impossible now. `project_root()` is computed ONCE, from
this file's own location, and every other path is derived from it. Nothing else in the
codebase is allowed to guess.

The layout is anchored on the repository root (the directory containing `run.py`), NOT on the
package, because `models/`, `data/`, `logs/` and `Reports/` are user data that live beside the
code rather than inside it.
"""

import os
import functools

# src/kayra/core/paths.py -> src/kayra/core -> src/kayra -> src -> <repo root>
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.dirname(_PACKAGE_DIR)
_REPO_ROOT = os.path.dirname(_SRC_DIR)


@functools.lru_cache(maxsize=1)
def project_root() -> str:
    """Absolute path to the repository root, independent of the working directory."""
    return _REPO_ROOT


@functools.lru_cache(maxsize=1)
def package_root() -> str:
    """Absolute path to the installed `kayra` package."""
    return _PACKAGE_DIR


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def data_dir() -> str:
    """Persistent assistant state: conversation memory, habits, STT status file."""
    return _ensure(os.path.join(project_root(), "data"))


def models_dir() -> str:
    """ONNX voice models and weights. Never created implicitly — a missing one is a real error."""
    return os.path.join(project_root(), "models")


def logs_dir() -> str:
    return _ensure(os.path.join(project_root(), "logs"))


def reports_dir() -> str:
    """Deep-research output."""
    return _ensure(os.path.join(project_root(), "Reports"))


def docs_dir() -> str:
    return os.path.join(project_root(), "docs")


def env_file() -> str:
    return os.path.join(project_root(), ".env")


def env_example_file() -> str:
    return os.path.join(project_root(), ".env.example")


def venv_dir() -> str:
    return os.path.join(project_root(), ".venv")


def venv_python() -> str:
    """The interpreter inside the project virtual environment, per platform."""
    import sys
    if sys.platform.startswith("win"):
        return os.path.join(venv_dir(), "Scripts", "python.exe")
    return os.path.join(venv_dir(), "bin", "python")


def data_path(*parts) -> str:
    """A path inside `data/`, e.g. `data_path("habits.json")`."""
    return os.path.join(data_dir(), *parts)


def model_path(*parts) -> str:
    return os.path.join(models_dir(), *parts)


def conversation_paths():
    """(primary, backup) for the long-term conversation database."""
    return data_path("conversation.json"), data_path("conversation_backup.json")


# Backward-compatible alias: `get_project_root()` was the historical name and is used widely
# in comments, docs and any code outside this repository.
get_project_root = project_root
