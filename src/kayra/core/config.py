# ┌────────────────────────────────────────────────────────────────────────┐
# │                              config.py                                 │
# │                  Single Cached Reader for `.env`                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
One parse of `.env`, shared by the whole process.

WHY THIS EXISTS
---------------
Before the reorganisation, eight different modules each did:

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_vars = dotenv_values(os.path.join(root, ".env")) or {}

at import time. That is eight opens, eight reads and eight parses of the same small file on
every cold start — and, more importantly, eight independent guesses at where the project root
is. The moment a file moved to a different directory depth, its guess silently pointed
somewhere else and every setting it read fell back to a default. Nothing crashed; the
assistant just quietly stopped honouring the user's configuration.

Both problems disappear here: the path comes from `core.paths`, and the parse is cached.

`load_environment()` additionally exports the values into `os.environ`, because several
subsystems (`ProactiveConfig`, the automation bounds) read `os.environ` directly so they can
be constructed in tests without a `.env` file at all.
"""

import os
import functools

try:
    from dotenv import dotenv_values, load_dotenv
except Exception:                       # pragma: no cover - dotenv is a hard dependency
    dotenv_values = None
    load_dotenv = None

from kayra.core.paths import env_file


@functools.lru_cache(maxsize=1)
def env_values() -> dict:
    """
    The parsed contents of `.env`, cached for the life of the process.

    Returns an empty dict when the file is absent — a missing `.env` is a first-run condition,
    not an error, and every consumer already supplies its own default.
    """
    if dotenv_values is None:
        return {}
    try:
        values = dict(dotenv_values(env_file()) or {})
    except Exception:
        return {}
    # `NAME=@keyring` means the key lives in the system keyring (see `core.credentials`).
    # Resolved here so no reader ever sees the marker or learns where the key is kept.
    if any(str(v or "").strip() == "@keyring" for v in values.values()):
        from kayra.core import credentials
        credentials.resolve(values)
    return values


def load_environment(override: bool = False) -> dict:
    """
    Parses `.env` and exports it into `os.environ`.

    Called ONCE, from `app.bootstrap()`, before any subsystem is constructed. Modules that
    read `os.environ` directly (the proactive tuning knobs, the automation bounds) depend on
    this having happened; modules that read `env()` do not.
    """
    if load_dotenv is not None:
        try:
            load_dotenv(env_file(), override=override)
        except Exception:
            pass
    values = env_values()
    # `load_dotenv` exported a keyring MARKER as the value; replace it with the resolved key
    # so a subsystem reading `os.environ` sees what `env()` sees.
    for name, value in values.items():
        if os.environ.get(name, "").strip() == "@keyring":
            os.environ[name] = str(value or "")
    return values


def env(name: str, default=None):
    """One configuration value, `.env` first and then the real environment."""
    value = env_values().get(name)
    if value is None or str(value).strip() == "":
        value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, minimum=None, maximum=None) -> int:
    try:
        value = int(str(env(name, default)).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(name: str, default: float, minimum=None, maximum=None) -> float:
    try:
        value = float(str(env(name, default)).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def assistant_name(default: str = "Kayra") -> str:
    """Used in enough places to be worth naming once."""
    return (env("ASSISTANT_NAME") or default).strip() or default


def stored_raw(name: str):
    """
    A setting exactly as written in `.env` — the `@keyring` marker NOT resolved. Lets the
    Settings screen say WHERE a key is kept without ever reading the key itself.
    """
    if dotenv_values is None:
        return None
    try:
        return (dotenv_values(env_file()) or {}).get(name)
    except Exception:
        return None


def reset_cache():
    """Forgets the cached parse. For tests and for `setup.py` rewriting `.env` in-process."""
    env_values.cache_clear()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          WRITING SETTINGS BACK                         │
# └────────────────────────────────────────────────────────────────────────┘
# Added for the desktop UI's Settings screen. `setup.py` has its own writer, but that script
# runs on the SYSTEM interpreter before the virtual environment exists and deliberately imports
# nothing from `kayra` — so it cannot be the shared implementation, and importing a root-level
# setup script from inside the package to save a few lines would be worse than this.

SECRET_KEYS = frozenset({
    "CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY",
    "OPENAI_API_KEY", "HF_TOKEN",
    "CohereAPIKey",     # no longer used; still hidden if an old .env carries it
})


def is_secret(name: str) -> bool:
    """
    Whether a setting's VALUE must never be rendered.

    Matched by explicit name and by shape, because a key added to `.env` later will not be in
    the list above and the safe default for an unknown credential is to hide it.
    """
    if name in SECRET_KEYS:
        return True
    upper = name.upper()
    return any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


def write_env_values(updates: dict) -> bool:
    """
    Updates `.env` in place, preserving comments, ordering and untouched settings.

    Rewriting the file from a parsed dict would silently delete every comment in it, and
    `.env.example` is heavily annotated precisely so users can understand their own
    configuration. So each key is rewritten on the line where it already lives, and only
    genuinely new keys are appended.

    Returns True on success. A value of None removes nothing — it is skipped — because an
    absent key and an empty key mean different things to the readers above.
    """
    if not updates:
        return True

    path = env_file()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        else:
            lines = []
    except OSError:
        return False

    remaining = {k: v for k, v in updates.items() if v is not None}

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in remaining:
            lines[index] = f"{name}={remaining.pop(name)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Added by the Kayra settings screen")
        for name, value in remaining.items():
            lines.append(f"{name}={value}")

    # Written via a temporary file and swapped in, so an interrupted write cannot leave the
    # user with a truncated `.env` and an assistant that has lost all its configuration.
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False

    _owner_only(path)

    # The cached parse is now stale, and `os.environ` is what several subsystems read.
    reset_cache()
    resolved = env_values()
    for name, value in updates.items():
        if value is not None:
            os.environ[name] = str(resolved.get(name, value) if value == "@keyring" else value)
    return True


def _owner_only(path):
    """
    `.env` can hold API keys, so on POSIX it is readable by its owner only (0600). A file
    written with the default umask is usually world-readable, which on a shared machine means
    every other account can read the keys.
    """
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_secret(name, value):
    """
    Stores an API key typed into Settings: in the system keyring when there is one (`.env`
    then holds `NAME=@keyring`), otherwise in `.env` itself (owner-only). An empty value
    REMOVES the key from both. Returns where it went: "keyring", "file" or "removed", or ""
    on failure.
    """
    from kayra.core import credentials
    value = str(value or "").strip()
    if not value:
        credentials.delete(name)
        return "removed" if write_env_values({name: ""}) else ""
    if credentials.store(name, value):
        return "keyring" if write_env_values({name: credentials.MARKER}) else ""
    return "file" if write_env_values({name: value}) else ""
