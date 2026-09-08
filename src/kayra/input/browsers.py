# ┌────────────────────────────────────────────────────────────────────────┐
# │                             browsers.py                                │
# │        Browser Discovery, Preference and Recognition Capability        │
# └────────────────────────────────────────────────────────────────────────┘
"""
Decides WHICH browser the STT engine drives, so Kayra does not require Chrome specifically.

THE PROBLEM THIS SOLVES, AND THE TRAP INSIDE IT
-----------------------------------------------
Kayra's speech input is the Web Speech API running in a headless Chromium browser. The obvious
generalisation — "use whatever the user's default browser is" — is wrong, and wrong in the
worst way: silently.

`webkitSpeechRecognition` is not self-contained. In Chrome it streams audio to Google's speech
service using a per-browser API key compiled into the build. A Chromium derivative that ships
without that key still exposes the whole API surface: the object exists, `start()` succeeds,
`onstart` fires. Recognition then dies with `onerror{error: "network"}` and never returns a
transcript.

Measured on the development host (Chrome 152, Edge 152, Brave 152), each launched headless
against the real recognition page:

    Chrome   session started, no error                      -> USABLE
    Edge     session started, no error, produced a result   -> USABLE (Microsoft's own backend)
    Brave    started, then error "network", session ended   -> UNUSABLE

Brave removes Google's speech endpoint deliberately, as a privacy decision. That is a property
of the build, not of the network or the machine.

This matters more than it first appears, because the STT page's `onerror` handler treats
`network` as a transient condition and restarts recognition. In a browser with no backend that
is an infinite restart loop: the assistant looks alive, consumes CPU, and never hears anything.
Capability detection is therefore a correctness requirement, not a nicety.

WHAT THIS MODULE DOES
---------------------
1. Finds the browsers actually installed on this machine.
2. Reads the user's default browser from the Windows registry.
3. Orders candidates: the user's explicit choice, then their default, then known-good ones.
4. Records which browser was verified to actually recognise, so the cost is paid once.

It does NOT launch anything. `speech_to_text.py` owns the session; this module only answers
"which browser, and in what order do I try them?".

WHY NOT JUST ALWAYS USE EDGE
----------------------------
Edge is preinstalled on every Windows 11 machine and works, so it is the reason "the user has
no Chrome" is a solvable problem rather than a fatal one. But it is not made the unconditional
default: a user who has Chrome and prefers it should get it, and a machine where Edge has been
removed or policy-blocked still needs the fallback chain. Preference is honoured where it
works; capability decides where it does not.
"""

import os
import json
import functools

from kayra.core.paths import data_path
from kayra.core.logbus import Subsystem, info, warning


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       KNOWN BROWSER FAMILIES                           │
# └────────────────────────────────────────────────────────────────────────┘
# `driver` selects the Selenium driver class. Every Chromium derivative other than Edge is
# driven by ChromeDriver with `binary_location` pointed at its executable; Edge has its own
# driver because its automation protocol build differs.
#
# `recognition` is the PRIOR, not the verdict:
#   "google"  — ships Google's speech key (Chrome only)
#   "vendor"  — has its own first-party backend (Edge → Microsoft)
#   "none"    — known to ship without any backend; do not waste a probe
#   "unknown" — must be verified at runtime
#
# Firefox is deliberately absent. `media.webspeech.recognition.enable` is off by default and
# there is no bundled recognition backend, so it cannot serve this role at all — listing it
# would only produce a slower path to the same failure.

_FAMILIES = {
    "chrome":   {"label": "Google Chrome",  "driver": "chrome", "recognition": "google"},
    "edge":     {"label": "Microsoft Edge", "driver": "edge",   "recognition": "vendor"},
    "brave":    {"label": "Brave",          "driver": "chrome", "recognition": "none"},
    "chromium": {"label": "Chromium",       "driver": "chrome", "recognition": "unknown"},
    "opera":    {"label": "Opera",          "driver": "chrome", "recognition": "unknown"},
    "vivaldi":  {"label": "Vivaldi",        "driver": "chrome", "recognition": "unknown"},
}

# Where each browser installs. Per-user installs under LOCALAPPDATA are checked too — Brave in
# particular installs there by default, which is why a Program Files-only scan misses it.
_CANDIDATE_PATHS = {
    "chrome": [
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    ],
    "brave": [
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "chromium": [
        r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    ],
    "opera": [
        r"%PROGRAMFILES%\Opera\opera.exe",
        r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
    ],
    "vivaldi": [
        r"%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe",
        r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
    ],
}

# Substrings identifying a family from the registry ProgId of the default browser. Brave's
# ProgId carries a per-installation suffix (BraveHTML.FE6EYDMJR34A7AVVYBFC4QRSMI), so matching
# is by prefix rather than equality.
_PROGID_HINTS = (
    ("chromehtml", "chrome"),
    ("bravehtml", "brave"),
    ("msedgehtm", "edge"),
    ("msedgedhtml", "edge"),
    ("operastable", "opera"),
    ("vivaldi", "vivaldi"),
    ("chromiumhtm", "chromium"),
)


class BrowserSpec:
    """One installed browser Kayra could drive."""

    __slots__ = ("key", "label", "driver", "binary", "recognition", "is_default")

    def __init__(self, key, binary, is_default=False):
        family = _FAMILIES[key]
        self.key = key
        self.label = family["label"]
        self.driver = family["driver"]
        self.recognition = family["recognition"]
        self.binary = binary
        self.is_default = is_default

    # Chrome is driven without an explicit binary path (ChromeDriver finds it); every other
    # Chromium derivative needs `binary_location`, or ChromeDriver launches Chrome instead.
    def needs_binary_location(self):
        return self.driver == "chrome" and self.key != "chrome"

    def __repr__(self):
        return f"<BrowserSpec {self.key} default={self.is_default} recog={self.recognition}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            DISCOVERY                                   │
# └────────────────────────────────────────────────────────────────────────┘

def _expand(path):
    expanded = os.path.expandvars(path)
    # An unset variable expands to itself on Windows ("%PROGRAMFILES(X86)%" on a 32-bit host),
    # which would otherwise be treated as a relative path.
    return None if "%" in expanded else expanded


def default_browser_key():
    """
    The family key of the user's default browser, or None.

    Read from the per-user UrlAssociations UserChoice, which is what Windows Settings actually
    writes. This is a READ of a registry value the user set; nothing here modifies it.
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

    lowered = (prog_id or "").lower()
    for hint, family in _PROGID_HINTS:
        if lowered.startswith(hint):
            return family
    return None


@functools.lru_cache(maxsize=1)
def discover_browsers():
    """
    Every supported browser installed on this machine, default-first.

    Cached: the set of installed browsers does not change during one Kayra run, and this is on
    the boot path.
    """
    default_key = default_browser_key()
    found = []
    for key, paths in _CANDIDATE_PATHS.items():
        for raw in paths:
            path = _expand(raw)
            if path and os.path.isfile(path):
                found.append(BrowserSpec(key, path, is_default=(key == default_key)))
                break
    found.sort(key=lambda spec: not spec.is_default)
    return tuple(found)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      CAPABILITY CACHE (positive only)                  │
# └────────────────────────────────────────────────────────────────────────┘
# Only SUCCESS is persisted, and this is the important asymmetry.
#
# A `network` error means "recognition did not reach a backend". For Brave that is structural.
# For Chrome on a machine that happens to be offline, it is temporary and says nothing about
# the browser. Persisting a negative verdict would let one offline boot permanently demote a
# perfectly good browser, and the user would never know why.
#
# So: remember what worked (a fast path worth having), and re-decide failures every run.

_CACHE_FILE = "browser_support.json"


def _load_cache():
    try:
        with open(data_path(_CACHE_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_working(key, version=None):
    """Records that `key` was verified to actually recognise speech."""
    try:
        data = _load_cache()
        data["verified"] = {"browser": key, "version": version}
        tmp = data_path(_CACHE_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, data_path(_CACHE_FILE))
    except OSError:
        pass            # A cache that cannot be written is a slower boot, not a failure.


def previously_working():
    """The family key last verified to work, or None."""
    entry = _load_cache().get("verified") or {}
    key = entry.get("browser")
    return key if key in _FAMILIES else None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             SELECTION                                  │
# └────────────────────────────────────────────────────────────────────────┘

def candidates(preference=None):
    """
    Browsers to try, best first.

    Order, and the reasoning for it:

      1. An explicit `STT_BROWSER` setting. The user asked for it by name; honour it, and if it
         cannot recognise, say so rather than quietly using something else.
      2. The browser verified to work on a previous run — skips re-probing the common case.
      3. The user's DEFAULT browser, but only when it is not known to lack a backend. Preferring
         the default is the point of this module; preferring it into an infinite restart loop
         is not.
      4. Everything else that has a real backend (`google` / `vendor`), then unknowns.

    A browser whose prior is "none" is never promoted, but it is still returned LAST so that a
    machine where it is the only browser gets a clear, specific failure instead of "no browser
    found".
    """
    installed = discover_browsers()
    if not installed:
        return ()

    by_key = {spec.key: spec for spec in installed}
    ordered = []

    def add(spec):
        if spec is not None and spec not in ordered:
            ordered.append(spec)

    if preference:
        add(by_key.get(preference.strip().lower()))

    add(by_key.get(previously_working()))

    for spec in installed:
        if spec.is_default and spec.recognition != "none":
            add(spec)

    for tier in ("google", "vendor", "unknown"):
        for spec in installed:
            if spec.recognition == tier:
                add(spec)

    for spec in installed:      # known-backendless, last resort
        add(spec)

    return tuple(ordered)


def describe_selection(preference=None):
    """A one-line human summary for the boot log. Pure; performs no I/O beyond discovery."""
    installed = discover_browsers()
    if not installed:
        return "no supported browser found"
    default = next((s.label for s in installed if s.is_default), "unknown")
    return (f"{len(installed)} browser(s) available "
            f"[{', '.join(s.label for s in installed)}]; default: {default}")


def warn_about_default(chosen):
    """
    Explains, once, when the user's default browser could not be used.

    Worth saying out loud: the user set that default deliberately, and silently using something
    else is the kind of behaviour that looks like a bug later.
    """
    installed = discover_browsers()
    default = next((s for s in installed if s.is_default), None)
    if default is None or chosen is None or default.key == chosen.key:
        return
    if default.recognition == "none":
        warning(Subsystem.STT,
                f"{default.label} is your default browser but ships without a speech "
                f"recognition backend, so voice input cannot use it. Using {chosen.label} "
                f"instead — your default browser is unchanged and is not launched.")
    else:
        info(Subsystem.STT,
             f"Using {chosen.label} rather than your default ({default.label}), which "
             f"could not start a recognition session.")
