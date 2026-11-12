# ┌────────────────────────────────────────────────────────────────────────┐
# │                            credentials.py                              │
# │            API Keys in the System Keyring, Not in a Text File          │
# └────────────────────────────────────────────────────────────────────────┘
"""
Where a cloud API key typed into the Settings screen is kept.

THE SYSTEM KEYRING FIRST. On Linux that is the Secret Service (GNOME Keyring / KWallet),
unlocked with the login session; on Windows the Credential Manager. Either way the key is
encrypted at rest, and `.env` carries only a marker:

    CLAUDE_API_KEY=@keyring

`core.config.env_values()` resolves the marker, so every reader of configuration — the model
engine, vision, setup — keeps reading `env("CLAUDE_API_KEY")` and never learns where the key
lives.

THE FILE IS THE FALLBACK, not an error. A bare Hyprland session may run no Secret Service at
all, and a missing keyring must not make the Settings screen unable to save a key. Then the
key goes into `.env` itself, and `config.write_env_values` makes that file readable by its
owner only (0600).

WHY A MARKER, AND NOT "ALWAYS ASK THE KEYRING". A keyring lookup is a D-Bus round trip, and a
locked keyring can raise an unlock prompt. With the marker, a configuration that never used
the keyring — every test rig, every `.env` written by hand — never touches it. That is also
what keeps the tier-1 suites from reading the developer's real keyring.

A LEAF: stdlib only, plus the optional `keyring` package imported lazily.
"""

import os
import threading

SERVICE = "kayra"
MARKER = "@keyring"

# The keys the Settings screen may put in the keyring. Anything else stays in `.env`.
KEYRING_NAMES = frozenset({"CLAUDE_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"})

_LOCK = threading.Lock()
_STATE = {"backend": None, "checked": False}


def _disabled():
    return str(os.environ.get("KAYRA_KEYRING", "")).strip().lower() in ("0", "off", "false", "no")


def _backend():
    """The usable keyring module, or None. Decided once per process."""
    with _LOCK:
        if _STATE["checked"]:
            return _STATE["backend"]
        _STATE["checked"] = True
        if _disabled():
            return None
        try:
            import keyring
            from keyring.backends import fail
            chosen = keyring.get_keyring()
            # The "fail" and "null" backends mean "no real keyring here": they either raise on
            # every call or silently store nothing. Neither may hold an API key.
            name = type(chosen).__module__ + "." + type(chosen).__name__
            if isinstance(chosen, fail.Keyring) or "null" in name.lower():
                return None
            _STATE["backend"] = keyring
        except Exception:
            _STATE["backend"] = None
        return _STATE["backend"]


def available():
    """True when keys can be stored encrypted in the system keyring."""
    return _backend() is not None


def backend_label():
    """Where a key saved now would go, in words for the Settings screen."""
    keyring = _backend()
    if keyring is None:
        return "the .env file (owner-only)"
    try:
        name = type(keyring.get_keyring()).__module__.lower()
    except Exception:
        name = ""
    if "windows" in name:
        return "Windows Credential Manager"
    if "kwallet" in name:
        return "KWallet"
    if "secretservice" in name or "libsecret" in name:
        return "the system keyring"
    if "macos" in name:
        return "the macOS Keychain"
    return "the system keyring"


def get(name):
    """The stored key, or "" when it is not there or the keyring cannot be read."""
    keyring = _backend()
    if keyring is None:
        return ""
    try:
        return keyring.get_password(SERVICE, name) or ""
    except Exception:
        return ""


def store(name, value):
    """
    Puts `value` in the keyring. Returns True when it is there (and `.env` should hold the
    marker), False when the caller must fall back to the file.
    """
    if name not in KEYRING_NAMES or not value:
        return False
    keyring = _backend()
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE, name, value)
        return keyring.get_password(SERVICE, name) == value
    except Exception:
        return False


def delete(name):
    """Removes a key from the keyring. Silent when it was not there."""
    keyring = _backend()
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, name)
    except Exception:
        pass


def resolve(values):
    """
    Replaces every `@keyring` marker in a parsed configuration with the stored key ("" when
    the keyring cannot supply it — an absent key, not the marker, is what readers expect).
    Returns the same dict.
    """
    for name, value in list(values.items()):
        if str(value or "").strip() == MARKER:
            values[name] = get(name)
    return values


def _reset_for_tests():
    with _LOCK:
        _STATE["backend"] = None
        _STATE["checked"] = False
