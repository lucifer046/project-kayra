# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation_targets.py                            │
# │        Window / Application / Browser Target Resolution (Win32)        │
# └────────────────────────────────────────────────────────────────────────┘
"""
Answers the question every automation action has to answer before it does anything:
**exactly which thing on this machine does the user mean?**

Resolution is a first-class step, separate from execution, because the two failure modes are
completely different. An executor that also resolves its own target has no way to say "I found
three of those" — it can only act on one of them and hope. Everything here returns a
`Resolution` with a status, and the executor refuses to move until that status is RESOLVED.

Why this module exists at all
-----------------------------
The previous `CloseApp()` did three things that were actively dangerous:

  1. It matched the requested name as a SUBSTRING of every visible window title and then
     posted WM_CLOSE to **every** match. "close code" would close every window with "code"
     anywhere in its title.
  2. Its final fallback was `taskkill /f /im <name>.exe`, which force-kills every process of
     that name — the user's entire browser session, and Kayra's own STT Chrome with it.
  3. It could not tell a browser TAB target ("close YouTube") from an application target
     ("close Spotify"), so YouTube was looked up as though it were an executable.

All three are fixed here: matches are scored and ranked rather than swept, Kayra-owned
processes are excluded by PID, and site targets resolve against browser windows.

Known limitation, stated plainly
--------------------------------
Windows exposes one window handle per browser window, and its title reflects only the
**active tab**. Enumerating background tabs needs UI Automation (pywinauto/uiautomation) or a
browser remote-debugging port, neither of which is installed here and neither of which is
worth its cost for this. So a site in a background tab is not visible to this resolver, and
`resolve_site` says so honestly instead of closing the wrong thing. `_TAB_ENUMERATION_NOTE`
marks the extension point.
"""

import os
import re
import sys
import time
import threading

try:
    import win32gui
    import win32process
    import win32con
    _WIN32 = True
except Exception:                     # pragma: no cover - non-Windows / missing pywin32
    win32gui = win32process = win32con = None
    _WIN32 = False

try:
    import psutil
except Exception:                     # pragma: no cover
    psutil = None

from kayra.utils import print_info, print_warning


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            RESOLUTION MODEL                            │
# └────────────────────────────────────────────────────────────────────────┘

class Resolution:
    """Outcome of a target lookup. `matches` is populated for RESOLVED and AMBIGUOUS."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"       # the mechanism itself is missing (no pywin32, etc.)

    __slots__ = ("status", "matches", "reason")

    def __init__(self, status, matches=None, reason=""):
        self.status = status
        self.matches = matches or []
        self.reason = reason

    @property
    def ok(self):
        return self.status == self.RESOLVED

    @property
    def target(self):
        """The single resolved match. Only meaningful when `ok`."""
        return self.matches[0] if self.matches else None

    def __repr__(self):
        return f"<Resolution {self.status} n={len(self.matches)} {self.reason}>"


class WindowInfo:
    """One visible top-level window."""

    __slots__ = ("hwnd", "title", "pid", "exe", "kayra_owned")

    def __init__(self, hwnd, title, pid, exe, kayra_owned=False):
        self.hwnd = hwnd
        self.title = title
        self.pid = pid
        self.exe = exe
        self.kayra_owned = kayra_owned

    @property
    def app(self):
        """Canonical application name for this window, e.g. 'chrome'."""
        return canonical_from_exe(self.exe)

    @property
    def is_browser(self):
        return self.app in BROWSER_APPS

    def __repr__(self):
        return f"<Window {self.exe} pid={self.pid} {self.title[:40]!r}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       CANONICAL APPLICATION REGISTRY                   │
# └────────────────────────────────────────────────────────────────────────┘
# A bounded, O(1) alias table. This is what makes "open Chrome", "launch chrome",
# "bring up google chrome" and "switch to the browser" all reach the same target without a
# system-wide search or an LLM call. Anything not in the table still works — it falls through
# to the existing AppOpener path — this table just makes the common cases instant and exact.

APP_REGISTRY = {
    "chrome":   {"exe": "chrome.exe",    "aliases": ("google chrome", "chrome", "gchrome")},
    "brave":    {"exe": "brave.exe",     "aliases": ("brave", "brave browser")},
    "edge":     {"exe": "msedge.exe",    "aliases": ("edge", "microsoft edge", "msedge")},
    "firefox":  {"exe": "firefox.exe",   "aliases": ("firefox", "mozilla", "mozilla firefox")},
    "opera":    {"exe": "opera.exe",     "aliases": ("opera",)},
    "vscode":   {"exe": "code.exe",      "aliases": ("vs code", "vscode", "visual studio code",
                                                     "code editor", "the editor")},
    "explorer": {"exe": "explorer.exe",  "aliases": ("file explorer", "explorer", "my computer",
                                                     "this pc", "file manager", "files")},
    "terminal": {"exe": "windowsterminal.exe",
                 "aliases": ("terminal", "windows terminal", "wt", "console")},
    "cmd":      {"exe": "cmd.exe",       "aliases": ("command prompt", "cmd")},
    "powershell": {"exe": "powershell.exe", "aliases": ("powershell", "power shell")},
    "notepad":  {"exe": "notepad.exe",   "aliases": ("notepad",)},
    "spotify":  {"exe": "spotify.exe",   "aliases": ("spotify",)},
    "discord":  {"exe": "discord.exe",   "aliases": ("discord",)},
    "telegram": {"exe": "telegram.exe",  "aliases": ("telegram",)},
    "whatsapp": {"exe": "whatsapp.exe",  "aliases": ("whatsapp", "whats app")},
    "slack":    {"exe": "slack.exe",     "aliases": ("slack",)},
    "vlc":      {"exe": "vlc.exe",       "aliases": ("vlc", "vlc player")},
    "settings": {"exe": "systemsettings.exe", "aliases": ("settings", "windows settings")},
    "taskmgr":  {"exe": "taskmgr.exe",   "aliases": ("task manager", "taskmgr")},
    "calc":     {"exe": "calculatorapp.exe", "aliases": ("calculator", "calc")},
    "word":     {"exe": "winword.exe",   "aliases": ("word", "microsoft word", "ms word")},
    "excel":    {"exe": "excel.exe",     "aliases": ("excel", "microsoft excel")},
    "powerpoint": {"exe": "powerpnt.exe", "aliases": ("powerpoint", "power point")},
    "obs":      {"exe": "obs64.exe",     "aliases": ("obs", "obs studio")},
    "steam":    {"exe": "steam.exe",     "aliases": ("steam",)},
}

BROWSER_APPS = frozenset({"chrome", "brave", "edge", "firefox", "opera"})

# alias -> canonical, and exe -> canonical. Both built once; both O(1) at lookup time.
_ALIAS_TO_CANONICAL = {}
_EXE_TO_CANONICAL = {}
for _canonical, _spec in APP_REGISTRY.items():
    _ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _spec["aliases"]:
        _ALIAS_TO_CANONICAL[_alias] = _canonical
    _EXE_TO_CANONICAL[_spec["exe"].lower()] = _canonical

# Filler words stripped before an application lookup, so "open up the chrome browser please"
# reduces to "chrome". Deliberately small — an aggressive stripper mangles real app names.
_APP_FILLERS = {"the", "my", "a", "an", "up", "please", "app", "application", "program",
                "window", "browser", "just", "now"}

# Words that mean "whatever is in front of me right now".
CURRENT_TARGET_WORDS = frozenset({
    "current", "this", "that", "it", "here", "active", "foreground",
    "this one", "current one", "the current one", "this window", "the window",
    "current window", "active window", "this app", "the app", "current app",
    "this tab", "the tab", "current tab", "active tab",
})

# Well-known sites, so a site target can be recognised without a network lookup. The values
# are the fragments that plausibly appear in a browser window title for that site.
SITE_HINTS = {
    "youtube":   ("youtube", "youtu.be"),
    "gmail":     ("gmail", "google mail", "inbox"),
    "github":    ("github",),
    "twitter":   ("twitter", "x.com"),
    "reddit":    ("reddit",),
    "netflix":   ("netflix",),
    "linkedin":  ("linkedin",),
    "instagram": ("instagram",),
    "facebook":  ("facebook",),
    "stackoverflow": ("stack overflow", "stackoverflow"),
    "chatgpt":   ("chatgpt", "chat.openai"),
    "claude":    ("claude",),
    "whatsapp web": ("whatsapp",),
    "amazon":    ("amazon",),
    "wikipedia": ("wikipedia",),
    "spotify web": ("spotify",),
    "drive":     ("google drive", "drive.google"),
    "docs":      ("google docs", "docs.google"),
}

_DOMAIN_RE = re.compile(
    r"\.(com|org|net|in|io|ai|co|dev|me|xyz|gov|edu|info|app|tech|site|online|live|pro|cc|tv|gg|us|uk|eu)"
    r"(/|$|\s)")

# Extension point for real tab enumeration. See the module docstring for why it is not wired.
_TAB_ENUMERATION_NOTE = (
    "Only the active tab of each browser window is visible through Win32 window titles. "
    "Background tabs need UI Automation or a browser debugging port."
)


def canonical_from_exe(exe: str) -> str:
    """'chrome.exe' -> 'chrome'; unknown executables map to their own stem."""
    name = (exe or "").lower()
    if name in _EXE_TO_CANONICAL:
        return _EXE_TO_CANONICAL[name]
    return name[:-4] if name.endswith(".exe") else name


def canonical_app(name: str):
    """
    Maps a spoken application name to its canonical key, or None if unknown.

    Bounded work: one dictionary hit after a small token filter. No process scan, no LLM, no
    filesystem search — this runs on the hot path of every "open X" command.
    """
    if not name:
        return None
    cleaned = re.sub(r"[^\w\s.+-]", " ", name.strip().lower())
    cleaned = " ".join(w for w in cleaned.split() if w not in _APP_FILLERS)
    if not cleaned:
        return None
    if cleaned in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[cleaned]
    # Try the un-filtered form too, for names that legitimately contain a filler word.
    plain = " ".join(name.strip().lower().split())
    return _ALIAS_TO_CANONICAL.get(plain)


def app_executable(canonical: str):
    spec = APP_REGISTRY.get(canonical)
    return spec["exe"] if spec else None


def looks_like_site(target: str) -> bool:
    """True when a target names a website rather than an installed application."""
    if not target:
        return False
    lowered = target.strip().lower()
    if canonical_app(lowered) in BROWSER_APPS:
        return False                      # "chrome" is the browser, not a site
    if lowered in SITE_HINTS:
        return True
    if lowered.startswith(("http://", "https://", "www.")):
        return True
    return bool(_DOMAIN_RE.search(lowered))


def site_fragments(target: str):
    """Title fragments that would indicate this site is open. Bounded, lowercase."""
    lowered = (target or "").strip().lower()
    lowered = re.sub(r"^https?://", "", lowered).rstrip("/")
    lowered = re.sub(r"^www\.", "", lowered)
    if lowered in SITE_HINTS:
        return tuple(SITE_HINTS[lowered])
    base = lowered.split("/")[0]
    stem = base.split(".")[0] if "." in base else base
    fragments = {base, stem}
    if stem in SITE_HINTS:
        fragments.update(SITE_HINTS[stem])
    return tuple(f for f in fragments if f)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      KAYRA-OWNED PROCESS EXCLUSION                     │
# └────────────────────────────────────────────────────────────────────────┘
# THE most important safety rule in this file.
#
# The STT subsystem runs a headless Chrome that Kayra owns, tracked by PID (never by name).
# `automation_windows.OpenApp` launches user applications through AppOpener, which uses
# subprocess.Popen — so a Chrome window Kayra opened FOR THE USER is also a child of this
# process. Name-based reasoning cannot tell those apart; PID ownership can.
#
# When the user says "close Chrome", the STT session must be invisible to the resolver. It is
# also headless, so it has no visible top-level window and would normally not appear at all —
# but "normally" is not a safety guarantee, and this costs one set lookup per window.

def kayra_owned_pids():
    """
    PIDs of processes Kayra owns and must never target.

    Reads the LIVE STT engine only if `speech_to_text` is already imported. It must never
    import or construct it: doing so from the automation layer would boot a headless Chrome
    session as a side effect of the user asking to close a window.
    """
    # The canonical name first, then the pre-reorganisation names. The lookup is by STRING on
    # purpose — an `import` here would boot a browser — which also means a rename silently
    # turns this safety property off instead of raising. It did exactly that: after the move to
    # `kayra.input.speech_to_text`, only the two dead names were listed, so this returned an
    # empty set and Kayra's own Chrome stopped being excluded from window enumeration.
    owned = set()
    module = None
    for name in ("kayra.input.speech_to_text", "modules.speech_to_text", "speech_to_text"):
        module = sys.modules.get(name)
        if module is not None:
            break
    if module is None:
        return owned
    try:
        engine_cls = getattr(module, "SpeechToTextEngine", None)
        instance = getattr(engine_cls, "_active_instance", None) if engine_cls else None
        if instance is not None:
            owned |= set(getattr(instance, "owned_pids", ()) or ())
    except Exception:
        pass
    return owned


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          WINDOW ENUMERATION                            │
# └────────────────────────────────────────────────────────────────────────┘

# Shell surfaces that are technically visible top-level windows but are never what a user
# means by "a window".
_IGNORED_WINDOW_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow",
                           "ApplicationFrameWindow.Ghost", "Button"}
_IGNORED_TITLES = {"", "program manager", "windows input experience", "settings",
                   "search", "start", "microsoft text input application"}

# A single enumeration is a few milliseconds, but a multi-step command can ask for one three
# or four times in a row. This cache makes those free without ever serving state old enough to
# be wrong — a quarter-second is far shorter than any user-visible window change.
_CACHE_TTL = 0.25
_cache = {"at": 0.0, "windows": []}
_cache_lock = threading.Lock()

_pid_exe_cache = {}                   # pid -> exe name; bounded below
_PID_CACHE_MAX = 256


def _exe_for_pid(pid):
    if not pid:
        return ""
    cached = _pid_exe_cache.get(pid)
    if cached is not None:
        return cached
    name = ""
    if psutil is not None:
        try:
            name = psutil.Process(pid).name().lower()
        except Exception:
            name = ""
    if len(_pid_exe_cache) >= _PID_CACHE_MAX:
        # Bounded cache: a long-running session opens and closes many processes, and an
        # unbounded pid->name map is a slow leak. Cheapest correct policy is to drop it all.
        _pid_exe_cache.clear()
    _pid_exe_cache[pid] = name
    return name


def list_windows(force=False):
    """
    Every visible, titled top-level window, newest enumeration cached for `_CACHE_TTL`.

    O(number of visible windows), typically 20-40 on a desktop. Kayra-owned windows are
    flagged here, once, so no caller can forget to check.
    """
    if not _WIN32:
        return []

    now = time.time()
    with _cache_lock:
        if not force and (now - _cache["at"]) < _CACHE_TTL:
            return list(_cache["windows"])

    owned = kayra_owned_pids()
    windows = []

    def _enum(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if title.strip().lower() in _IGNORED_TITLES:
                return True
            try:
                if win32gui.GetClassName(hwnd) in _IGNORED_WINDOW_CLASSES:
                    return True
            except Exception:
                pass
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            windows.append(WindowInfo(hwnd, title, pid, _exe_for_pid(pid),
                                      kayra_owned=pid in owned))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        print_warning(f"Window enumeration failed: {e}")
        return []

    with _cache_lock:
        _cache["at"] = time.time()
        _cache["windows"] = windows
    return list(windows)


def invalidate_cache():
    """Called after any action that changes the window set, so the next read is truthful."""
    with _cache_lock:
        _cache["at"] = 0.0


def foreground_window():
    """The window the user is actually looking at, or None."""
    if not _WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        owned = kayra_owned_pids()
        return WindowInfo(hwnd, title, pid, _exe_for_pid(pid), kayra_owned=pid in owned)
    except Exception:
        return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              SCORING                                   │
# └────────────────────────────────────────────────────────────────────────┘
# Matches are RANKED, never swept. The old implementation closed every window whose title
# contained the requested substring; this picks the best match and reports AMBIGUOUS when the
# runners-up are just as good.

def _score_window(window: WindowInfo, needle: str, canonical=None) -> float:
    title = (window.title or "").lower()
    app = window.app
    score = 0.0

    if canonical and app == canonical:
        score += 1.0                        # the process really is that application
    if needle:
        if title == needle:
            score += 1.0
        elif title.startswith(needle) or title.endswith(needle):
            score += 0.6
        elif needle in title:
            score += 0.4
        # Whole-word hit beats an accidental substring ("code" inside "Encoder").
        if re.search(rf"\b{re.escape(needle)}\b", title):
            score += 0.3
        if needle in app:
            score += 0.5
    return score


def _rank(matches, ambiguity_margin=0.25):
    """
    Turns scored candidates into a Resolution.

    AMBIGUOUS when the second-best is within `ambiguity_margin` of the best — that is the case
    where picking one would be a coin flip, and a coin flip that closes a window is not
    acceptable.
    """
    if not matches:
        return Resolution(Resolution.NOT_FOUND)
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score = matches[0][0]
    tied = [w for score, w in matches if best_score - score <= ambiguity_margin]
    if len(tied) > 1:
        return Resolution(Resolution.AMBIGUOUS, tied, "several equally good matches")
    return Resolution(Resolution.RESOLVED, [matches[0][1]])


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            PUBLIC RESOLVERS                            │
# └────────────────────────────────────────────────────────────────────────┘

def resolve_window(target, windows=None):
    """
    Resolves a window target.

    `target` may be "current"/"this"/... for the foreground window, or an application or title
    fragment. Kayra-owned windows are excluded unconditionally.
    """
    if not _WIN32:
        return Resolution(Resolution.UNAVAILABLE, reason="window control needs pywin32")

    if not target or str(target).strip().lower() in CURRENT_TARGET_WORDS:
        window = foreground_window()
        if window is None:
            return Resolution(Resolution.NOT_FOUND, reason="no foreground window")
        if window.kayra_owned:
            return Resolution(Resolution.NOT_FOUND, reason="foreground window belongs to Kayra")
        return Resolution(Resolution.RESOLVED, [window])

    needle = str(target).strip().lower()
    canonical = canonical_app(needle)
    pool = windows if windows is not None else list_windows()

    scored = []
    for window in pool:
        if window.kayra_owned:
            continue
        score = _score_window(window, needle, canonical)
        if score > 0:
            scored.append((score, window))
    return _rank(scored)


def resolve_application(name):
    """
    Resolves an application target.

    Returns RESOLVED with the running windows of that app (all of them — closing an
    application legitimately means all its windows), or NOT_FOUND when it is not running.
    Whether the app is *installed* is a different question, answered by the launcher.
    """
    if not name:
        return Resolution(Resolution.NOT_FOUND, reason="no application named")
    canonical = canonical_app(name)
    needle = str(name).strip().lower()

    if not _WIN32:
        return Resolution(Resolution.UNAVAILABLE, reason="application control needs pywin32")

    matches = []
    for window in list_windows():
        if window.kayra_owned:
            continue
        if canonical and window.app == canonical:
            matches.append(window)
        elif not canonical and (needle in window.app or needle in (window.title or "").lower()):
            matches.append(window)

    if not matches:
        return Resolution(Resolution.NOT_FOUND, reason=f"{name} is not running")
    return Resolution(Resolution.RESOLVED, matches)


def resolve_site(site, windows=None):
    """
    Resolves a website target to the browser window showing it.

    This is the "close YouTube" path, and it is the reason the resolver exists. YouTube is not
    an executable; it is a page inside a browser, and treating it as a process name is how the
    old code ended up at `taskkill /f /im youtube.exe`.

    NOT_FOUND carries the honest reason when nothing matched: the site may simply be in a
    background tab, which Win32 cannot see (see `_TAB_ENUMERATION_NOTE`).
    """
    if not _WIN32:
        return Resolution(Resolution.UNAVAILABLE, reason="browser control needs pywin32")

    fragments = site_fragments(site)
    if not fragments:
        return Resolution(Resolution.NOT_FOUND, reason="no site named")

    pool = windows if windows is not None else list_windows()
    scored = []
    for window in pool:
        if window.kayra_owned or not window.is_browser:
            continue
        title = (window.title or "").lower()
        best = 0.0
        for fragment in fragments:
            if fragment and fragment in title:
                # A site name at the END of a browser title is the strongest signal there is:
                # Chrome renders "<page title> - YouTube - Google Chrome".
                best = max(best, 1.0 if f"- {fragment}" in title or title.startswith(fragment)
                           else 0.7)
        if best:
            scored.append((best, window))

    if not scored:
        return Resolution(Resolution.NOT_FOUND, reason=_TAB_ENUMERATION_NOTE)
    return _rank(scored)


def resolve_browser(preferred=None):
    """
    Resolves 'the browser'.

    With a name, that browser. Without one, the browser the user was most plausibly using:
    the foreground window if it is a browser, otherwise the only running browser, otherwise
    AMBIGUOUS — because closing "the browser" when two are open is a guess with consequences.
    """
    if not _WIN32:
        return Resolution(Resolution.UNAVAILABLE, reason="browser control needs pywin32")

    if preferred:
        return resolve_application(preferred)

    front = foreground_window()
    if front is not None and front.is_browser and not front.kayra_owned:
        return Resolution(Resolution.RESOLVED, [front])

    browsers = [w for w in list_windows() if w.is_browser and not w.kayra_owned]
    if not browsers:
        return Resolution(Resolution.NOT_FOUND, reason="no browser window is open")

    by_app = {}
    for window in browsers:
        by_app.setdefault(window.app, []).append(window)
    if len(by_app) == 1:
        return Resolution(Resolution.RESOLVED, browsers)
    return Resolution(Resolution.AMBIGUOUS, browsers, "more than one browser is running")


def resolve_path(target, kind="any"):
    """
    Resolves a file or folder name to an absolute path.

    Checks the well-known user folders by name first (that is what "open my Downloads folder"
    means), then treats the target as a literal path. Deliberately does NOT walk the disk: a
    recursive search is unbounded work on the hot path, and a wrong hit here is a destructive
    operation on the wrong file.
    """
    if not target:
        return Resolution(Resolution.NOT_FOUND, reason="no path named")

    raw = str(target).strip().strip('"')
    home = os.path.expanduser("~")
    known = {
        "downloads": os.path.join(home, "Downloads"),
        "download": os.path.join(home, "Downloads"),
        "documents": os.path.join(home, "Documents"),
        "docs": os.path.join(home, "Documents"),
        "desktop": os.path.join(home, "Desktop"),
        "pictures": os.path.join(home, "Pictures"),
        "photos": os.path.join(home, "Pictures"),
        "music": os.path.join(home, "Music"),
        "videos": os.path.join(home, "Videos"),
        "home": home,
        "user folder": home,
    }
    # OneDrive-redirected folders are the real location on many Windows installs.
    onedrive = os.path.join(home, "OneDrive")
    if os.path.isdir(onedrive):
        for name in ("Desktop", "Documents", "Pictures"):
            redirected = os.path.join(onedrive, name)
            if os.path.isdir(redirected):
                known[name.lower()] = redirected

    key = " ".join(raw.lower().replace("my ", "").replace(" folder", "").split())
    if key in known and os.path.exists(known[key]):
        return Resolution(Resolution.RESOLVED, [known[key]])

    expanded = os.path.expandvars(os.path.expanduser(raw))
    if os.path.exists(expanded):
        if kind == "folder" and not os.path.isdir(expanded):
            return Resolution(Resolution.NOT_FOUND, reason="that is a file, not a folder")
        if kind == "file" and not os.path.isfile(expanded):
            return Resolution(Resolution.NOT_FOUND, reason="that is a folder, not a file")
        return Resolution(Resolution.RESOLVED, [os.path.abspath(expanded)])

    return Resolution(Resolution.NOT_FOUND, reason=f"I couldn't find {raw}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        WINDOW ACTIONS + VERIFICATION                   │
# └────────────────────────────────────────────────────────────────────────┘

# Time to let the input queue settle after a focus change before sending keystrokes to the
# newly-focused window. Measured empirically: below ~150ms Chrome intermittently drops the
# first accelerator, and a dropped Ctrl+T followed by a delivered Ctrl+W closes the window.
FOCUS_SETTLE_SECONDS = 0.25


def _tap_alt():
    """
    Synthetic ALT down+up.

    Windows refuses `SetForegroundWindow` from a process that does not own the current
    foreground window (ForegroundLockTimeout); a key event in our own input queue satisfies
    that check. The UP event is not optional and the ordering is not cosmetic — see
    `_release_modifiers` for what happens when ALT is left logically down.
    """
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)   # ALT down
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)   # ALT up  (KEYEVENTF_KEYUP)
    except Exception:
        pass


def release_modifiers():
    """Public alias — the executor normalises modifiers before every injected keystroke."""
    _release_modifiers()


def _release_modifiers():
    """
    Force-releases ALT, CTRL and SHIFT.

    This exists because of a real, reproducible failure. `focus_window` taps ALT to defeat
    the foreground lock; if Windows still considers ALT held when the NEXT keystroke arrives,
    "Ctrl+T" is delivered as "Ctrl+Alt+T" and Windows opens the Alt-Tab task switcher instead.
    Observed end to end: the new tab silently never opened, the following "close this tab"
    Ctrl+W then landed on a browser window holding a single tab, and the whole window closed.

    Sending an explicit UP for each modifier costs three API calls and removes the entire
    failure mode.
    """
    try:
        import ctypes
        for code in (0x12, 0x11, 0x10):        # VK_MENU (alt), VK_CONTROL, VK_SHIFT
            ctypes.windll.user32.keybd_event(code, 0, 2, 0)
    except Exception:
        pass


def focus_window(window: WindowInfo, timeout=1.0, settle=True) -> bool:
    """
    Brings a window to the foreground and VERIFIES that it got there.

    Order matters: a plain `SetForegroundWindow` is tried FIRST and usually succeeds, and the
    ALT tap is only used as a fallback when it does not. The old code tapped ALT
    unconditionally, which meant every focus change carried the stuck-modifier risk described
    in `_release_modifiers` even when nothing needed it.

    On success the modifiers are explicitly released and the input queue is given a moment to
    settle, so the caller's next keystroke reaches the window it is aimed at.
    """
    if not _WIN32 or window is None:
        return False

    try:
        if win32gui.IsIconic(window.hwnd):
            win32gui.ShowWindow(window.hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    def _is_front():
        try:
            return win32gui.GetForegroundWindow() == window.hwnd
        except Exception:
            return False

    if _is_front():
        # Already there. Still normalise the modifier state and settle: the fast path used to
        # return immediately, so a modifier left held by an EARLIER focus change survived into
        # the caller's keystroke. That is the whole bug — see `_release_modifiers`.
        _release_modifiers()
        if settle:
            time.sleep(FOCUS_SETTLE_SECONDS)
        invalidate_cache()
        return True

    # Attempt 1: ask politely.
    try:
        win32gui.SetForegroundWindow(window.hwnd)
    except Exception:
        pass

    deadline = time.time() + min(timeout, 0.3)
    while time.time() < deadline:
        if _is_front():
            break
        time.sleep(0.02)

    # Attempt 2: defeat the foreground lock, then ask again.
    if not _is_front():
        _tap_alt()
        try:
            win32gui.SetForegroundWindow(window.hwnd)
        except Exception:
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_front():
            break
        time.sleep(0.02)

    if not _is_front():
        _release_modifiers()
        return False

    # Never leave a modifier logically held for the caller's keystroke.
    _release_modifiers()
    if settle:
        time.sleep(FOCUS_SETTLE_SECONDS)
    invalidate_cache()
    return True


def close_window(window: WindowInfo) -> bool:
    """
    Asks a window to close politely (WM_CLOSE) — never a force kill.

    WM_CLOSE is what clicking the X does: the application gets to run its shutdown, prompt
    about unsaved work, and decline. That is the correct semantics for "close this window" and
    it is why nothing in this module calls taskkill.
    """
    if not _WIN32 or window is None:
        return False
    try:
        win32gui.PostMessage(window.hwnd, win32con.WM_CLOSE, 0, 0)
        invalidate_cache()
        return True
    except Exception:
        return False


def window_exists(hwnd) -> bool:
    if not _WIN32:
        return False
    try:
        return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def wait_until_gone(hwnd, timeout=2.0) -> bool:
    """Verification for a close: polls until the handle is really gone."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not window_exists(hwnd):
            invalidate_cache()
            return True
        time.sleep(0.05)
    return not window_exists(hwnd)


def wait_for_app_window(canonical, timeout=8.0):
    """
    Verification for a launch: waits for a window belonging to `canonical` to appear.

    Bounded and polled rather than event-driven on purpose — hooking window-creation events
    would mean a message pump and a permanent thread for something that happens a few times a
    day and is over in under a second.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in list_windows(force=True):
            if window.kayra_owned:
                continue
            if window.app == canonical:
                return window
        time.sleep(0.15)
    return None


def describe_windows(windows, limit=3):
    """
    Short spoken description of a set of candidates, for an ambiguity question.

    Titles are trimmed hard: this is read aloud, and a full browser tab title is a paragraph.
    """
    names = []
    for window in windows[:limit]:
        title = (window.title or "").strip()
        for separator in (" - ", " — ", " | "):
            if separator in title:
                title = title.split(separator)[0].strip()
                break
        names.append(title[:40] or window.app)
    return names
