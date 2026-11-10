# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/linux_apps.py                         │
# │         Installed applications on Linux: the XDG desktop-entry index   │
# └────────────────────────────────────────────────────────────────────────┘
"""
"Is X installed, how do I start it, and which window is it?" — answered on Linux.

The Windows build reads the Start-Menu index; the Linux equivalent is the XDG desktop-entry
set every launcher (walker, rofi, GNOME Shell) already uses: `*.desktop` files under
`$XDG_DATA_HOME/applications` and each `$XDG_DATA_DIRS/applications`, including Flatpak's
export directories. Reading them is a few hundred small text files, cached for five minutes.

THE SAME RULE AS WINDOWS: NO FUZZY MATCH
----------------------------------------
"open YouTube" once opened File Explorer and "open GitHub" launched Git GUI, because a
difflib fallback picked whatever looked closest. Nothing here does that. A spoken name
resolves through, in order:

  1. the curated alias table (`LINUX_APPS`) — "chrome", "files", "vs code", "calculator"...
  2. an EXACT desktop-entry Name, GenericName-free, case- and punctuation-insensitive
  3. an EXACT desktop-file id, or the last dotted part of one (`org.gnome.Nautilus` -> nautilus)
  4. a curated terminal program on PATH (btop, lazygit, nvim...) opened in the terminal

and anything else is NOT FOUND, said out loud.

WINDOW IDENTITY
---------------
A Hyprland client carries a `class` (the Wayland app_id). For a well-behaved app that is the
desktop-file id or its `StartupWMClass`; Chromium-based browsers use their binary name. Every
entry therefore carries a set of `classes` built from all three, and a running window belongs
to an application when its class is in that set — an exact comparison, never a substring.
"""

import os
import re
import glob
import time
import shutil
import threading

# Curated table: canonical name -> spoken aliases, desktop ids to try (first installed wins),
# and extra window classes. The canonical names match `automation.targets.APP_REGISTRY` where
# the app exists on both systems, so "chrome" means the same thing everywhere.
LINUX_APPS = {
    "chrome":   {"aliases": ("google chrome", "chrome", "gchrome"),
                 "ids": ("google-chrome", "google-chrome-stable"),
                 "classes": ("google-chrome", "google-chrome-stable")},
    "chromium": {"aliases": ("chromium",), "ids": ("chromium",), "classes": ("chromium",)},
    "brave":    {"aliases": ("brave", "brave browser"),
                 "ids": ("brave-browser", "brave"), "classes": ("brave-browser",)},
    "edge":     {"aliases": ("edge", "microsoft edge", "msedge"),
                 "ids": ("microsoft-edge", "microsoft-edge-stable"),
                 "classes": ("microsoft-edge", "microsoft-edge-stable")},
    "firefox":  {"aliases": ("firefox", "mozilla", "mozilla firefox"),
                 "ids": ("firefox", "org.mozilla.firefox"), "classes": ("firefox",)},
    "zen":      {"aliases": ("zen", "zen browser"), "ids": ("zen", "zen-browser", "app.zen_browser.zen"),
                 "classes": ("zen", "zen-alpha", "zen-beta")},
    "vscode":   {"aliases": ("vs code", "vscode", "visual studio code", "code editor", "code"),
                 "ids": ("code", "com.microsoft.VSCode", "visual-studio-code", "code-oss",
                         "codium", "vscodium"),
                 "classes": ("code", "code-oss", "codium", "code-url-handler",
                             "com.microsoft.vscode")},
    "explorer": {"aliases": ("file explorer", "explorer", "file manager", "files",
                             "my computer", "this pc", "nautilus"),
                 "ids": ("org.gnome.Nautilus", "nautilus", "thunar", "org.kde.dolphin",
                         "pcmanfm"),
                 "classes": ("org.gnome.nautilus", "nautilus", "thunar", "org.kde.dolphin")},
    "terminal": {"aliases": ("terminal", "console", "command prompt", "cmd", "shell",
                             "powershell", "power shell"),
                 "ids": (), "classes": ("alacritty", "foot", "kitty", "ghostty",
                                        "com.mitchellh.ghostty", "org.wezfurlong.wezterm")},
    "notepad":  {"aliases": ("notepad", "text editor", "gedit"),
                 "ids": ("org.gnome.TextEditor", "gnome-text-editor", "org.gnome.gedit",
                         "org.kde.kate", "mousepad"),
                 "classes": ("org.gnome.texteditor", "org.gnome.gedit")},
    "neovim":   {"aliases": ("neovim", "nvim", "vim"), "ids": ("nvim",), "classes": ()},
    "spotify":  {"aliases": ("spotify",), "ids": ("spotify", "com.spotify.Client"),
                 "classes": ("spotify",)},
    "discord":  {"aliases": ("discord",), "ids": ("discord", "com.discordapp.Discord", "vesktop"),
                 "classes": ("discord", "vesktop")},
    "telegram": {"aliases": ("telegram",), "ids": ("org.telegram.desktop", "telegramdesktop"),
                 "classes": ("org.telegram.desktop", "telegramdesktop")},
    "slack":    {"aliases": ("slack",), "ids": ("slack", "com.slack.Slack"), "classes": ("slack",)},
    "signal":   {"aliases": ("signal",), "ids": ("signal-desktop", "signal"), "classes": ("signal",)},
    "obsidian": {"aliases": ("obsidian",), "ids": ("obsidian", "md.obsidian.Obsidian"),
                 "classes": ("obsidian",)},
    "vlc":      {"aliases": ("vlc", "vlc player"), "ids": ("vlc", "org.videolan.VLC"),
                 "classes": ("vlc",)},
    "mpv":      {"aliases": ("mpv",), "ids": ("mpv",), "classes": ("mpv",)},
    "calc":     {"aliases": ("calculator", "calc"),
                 "ids": ("omacalc", "org.gnome.Calculator", "gnome-calculator", "qalculate-gtk",
                         "org.kde.kcalc"),
                 "classes": ("org.gnome.calculator", "qalculate-gtk")},
    "obs":      {"aliases": ("obs", "obs studio"), "ids": ("com.obsproject.Studio", "obs"),
                 "classes": ("com.obsproject.studio", "obs")},
    "steam":    {"aliases": ("steam",), "ids": ("steam",), "classes": ("steam",)},
    "lmstudio": {"aliases": ("lm studio", "lmstudio"), "ids": ("ai.elementlabs.lmstudio", "lm-studio", "lmstudio"),
                 "classes": ("ai.elementlabs.lmstudio", "lm studio")},
    "settings": {"aliases": ("settings", "system settings", "control panel"),
                 "ids": ("org.gnome.Settings", "gnome-control-center", "systemsettings"),
                 "classes": ("org.gnome.settings",)},
    # Task manager on Linux is a terminal program; see TUI_PROGRAMS.
    "taskmgr":  {"aliases": ("task manager", "taskmgr", "system monitor", "activity monitor"),
                 "ids": ("btop", "org.gnome.SystemMonitor", "gnome-system-monitor"),
                 "classes": ("btop", "org.gnome.systemmonitor")},
}

# Terminal programs worth opening by name. A command on PATH that is NOT in this set is never
# executed from "open X": PATH is full of things whose name is a common word.
TUI_PROGRAMS = frozenset({"btop", "htop", "top", "lazygit", "lazydocker", "nvim", "vim",
                          "yazi", "ranger", "impala", "bluetui", "wiremix", "ncdu", "cava"})

BROWSER_CLASSES = frozenset({
    "google-chrome", "google-chrome-stable", "chromium", "brave-browser", "firefox",
    "microsoft-edge", "microsoft-edge-stable", "vivaldi-stable", "zen", "zen-alpha",
    "zen-beta", "librewolf", "opera", "helium",
})

# Settings surfaces. On Omarchy these are the shell's own menu and bar panels, not apps; the
# GNOME/KDE control centres are the fallback elsewhere. Each value is a list of argv choices,
# first installed wins. Matched EXACTLY on the normalized phrase, like everything here.
_OMARCHY_PANEL = lambda name: ("omarchy-shell", "shell", "toggle", f"omarchy.{name}")
SETTINGS_PANELS = {
    ("settings", "control panel", "system settings", "setup", "preferences", "configuration"):
        [("omarchy-menu", "toggle", "setup"), ("gnome-control-center",), ("systemsettings",)],
    ("system menu", "omarchy menu", "main menu", "start menu", "menu"):
        [("omarchy-menu", "toggle", "root")],
    ("wifi settings", "wi-fi settings", "network settings", "internet settings", "wifi menu",
     "network panel"):
        [_OMARCHY_PANEL("network"), ("gnome-control-center", "wifi"), ("nm-connection-editor",)],
    ("bluetooth settings", "bluetooth menu", "bluetooth panel"):
        [_OMARCHY_PANEL("bluetooth"), ("gnome-control-center", "bluetooth"), ("blueman-manager",)],
    ("sound settings", "audio settings", "volume settings", "sound panel", "audio panel"):
        [_OMARCHY_PANEL("audio"), ("gnome-control-center", "sound"), ("pavucontrol",)],
    ("power settings", "battery settings", "power panel", "battery panel"):
        [_OMARCHY_PANEL("power"), ("gnome-control-center", "power")],
    ("display settings", "monitor settings", "screen settings", "display panel"):
        [_OMARCHY_PANEL("monitor"), ("gnome-control-center", "display")],
    ("keyboard shortcuts", "keybindings", "key bindings", "shortcuts"):
        [("omarchy-menu-keybindings",)],
    ("theme settings", "appearance settings", "style settings", "themes"):
        [("omarchy-menu", "toggle", "style")],
}
_PANEL_LOOKUP = {}
for _names, _choices in SETTINGS_PANELS.items():
    for _name in _names:
        _PANEL_LOOKUP[_name] = (_names[0], _choices)


def settings_panel(name, which=shutil.which):
    """(label, argv) for a settings surface, or None. Only an installed choice is returned."""
    cleaned = " ".join(re.sub(r"[^\w\s-]", " ", str(name or "").lower()).replace("the ", " ")
                       .replace("my ", " ").replace("open ", " ").split())
    entry = _PANEL_LOOKUP.get(cleaned)
    if entry is None:
        return None
    label, choices = entry
    for argv in choices:
        if which(argv[0]):
            return label, list(argv)
    return label, None


_FILLERS = {"the", "my", "a", "an", "up", "please", "app", "application", "program",
            "window", "just", "now"}

_ALIAS_TO_CANONICAL = {}
for _canonical, _spec in LINUX_APPS.items():
    _ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _spec["aliases"]:
        _ALIAS_TO_CANONICAL[_alias] = _canonical


def normalize_name(name) -> str:
    cleaned = re.sub(r"[^\w\s.+-]", " ", str(name or "").lower())
    return " ".join(w for w in cleaned.split() if w not in _FILLERS)


def canonical(name):
    """Spoken name -> curated canonical key, or None. One dict probe."""
    cleaned = normalize_name(name)
    if not cleaned:
        return None
    return _ALIAS_TO_CANONICAL.get(cleaned) or _ALIAS_TO_CANONICAL.get(
        " ".join(str(name or "").lower().split()))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         DESKTOP-ENTRY INDEX                            │
# └────────────────────────────────────────────────────────────────────────┘

class DesktopEntry:
    __slots__ = ("id", "name", "exec", "wm_class", "path", "terminal")

    def __init__(self, id, name, exec="", wm_class="", path="", terminal=False):
        self.id = id
        self.name = name
        self.exec = exec
        self.wm_class = wm_class
        self.path = path
        self.terminal = terminal

    @property
    def classes(self):
        """Every window class this application plausibly reports. Lower-case."""
        out = {self.id.lower(), self.id.split(".")[-1].lower()}
        if self.wm_class:
            out.add(self.wm_class.lower())
        binary = os.path.basename((self.exec.split() or [""])[0]).lower()
        if binary and binary not in ("env", "flatpak", "sh", "bash", "uwsm-app", "gtk-launch"):
            out.add(binary)
        return out

    def __repr__(self):
        return f"<DesktopEntry {self.id} {self.name!r}>"


def application_dirs(environ=None):
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~")
    data_home = environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    data_dirs = (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    extra = [os.path.join(data_home, "flatpak", "exports", "share"),
             "/var/lib/flatpak/exports/share"]
    seen, out = set(), []
    for base in [data_home] + data_dirs + extra:
        path = os.path.join(base, "applications")
        if base and path not in seen and os.path.isdir(path):
            seen.add(path)
            out.append(path)
    return out


def parse_desktop_file(path, desktop_id):
    """One `[Desktop Entry]` group -> DesktopEntry, or None for hidden/non-app entries."""
    fields = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            in_main = False
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    if in_main:
                        break
                    in_main = line == "[Desktop Entry]"
                    continue
                if in_main and "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    fields.setdefault(key.strip(), value.strip())
    except OSError:
        return None
    if fields.get("Type", "Application") != "Application":
        return None
    if fields.get("Hidden", "").lower() == "true" or fields.get("NoDisplay", "").lower() == "true":
        return None
    name = fields.get("Name")
    if not name:
        return None
    try_exec = fields.get("TryExec")
    if try_exec and not (os.path.isabs(try_exec) and os.path.exists(try_exec)) \
            and not shutil.which(try_exec):
        return None
    return DesktopEntry(desktop_id, name, fields.get("Exec", ""),
                        fields.get("StartupWMClass", ""), path,
                        fields.get("Terminal", "").lower() == "true")


class _Index:
    TTL = 300.0

    def __init__(self):
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._by_id = {}
        self._by_name = {}
        self._by_short = {}

    def invalidate(self):
        with self._lock:
            self._loaded_at = 0.0

    def _load(self, dirs=None):
        by_id = {}
        for directory in (dirs if dirs is not None else application_dirs()):
            for path in glob.glob(os.path.join(directory, "**", "*.desktop"), recursive=True):
                desktop_id = os.path.relpath(path, directory)[:-len(".desktop")].replace("/", "-")
                if desktop_id in by_id:            # earlier XDG dir wins, per the spec
                    continue
                entry = parse_desktop_file(path, desktop_id)
                if entry is not None:
                    by_id[desktop_id] = entry
        by_name, by_short = {}, {}
        for entry in by_id.values():
            by_name.setdefault(normalize_name(entry.name), entry)
            by_short.setdefault(entry.id.split(".")[-1].lower(), entry)
        self._by_id = {k.lower(): v for k, v in by_id.items()}
        self._by_name, self._by_short = by_name, by_short

    def ensure(self, dirs=None):
        with self._lock:
            if dirs is not None or time.monotonic() - self._loaded_at > self.TTL:
                self._load(dirs)
                self._loaded_at = time.monotonic()

    def by_id(self, desktop_id):
        self.ensure()
        return self._by_id.get(str(desktop_id or "").lower())

    def by_name(self, name):
        self.ensure()
        cleaned = normalize_name(name)
        if not cleaned:
            return None
        return (self._by_name.get(cleaned) or self._by_id.get(cleaned.replace(" ", "-"))
                or self._by_short.get(cleaned.replace(" ", "")))

    def entries(self):
        self.ensure()
        return list(self._by_id.values())


INDEX = _Index()


class AppTarget:
    """What "open X" resolved to: a desktop entry, a terminal program, or the terminal."""
    __slots__ = ("label", "entry", "tui", "classes", "canonical")

    def __init__(self, label, entry=None, tui=None, classes=(), canonical=None):
        self.label = label
        self.entry = entry
        self.tui = tui
        self.classes = frozenset(c.lower() for c in classes if c)
        self.canonical = canonical

    def __repr__(self):
        return f"<AppTarget {self.label} entry={self.entry} tui={self.tui}>"


def resolve(name):
    """Spoken name -> AppTarget, or None. Exact identities only — see the module docstring."""
    key = canonical(name)
    if key:
        spec = LINUX_APPS[key]
        classes = set(spec["classes"])
        if key == "terminal":
            return AppTarget("terminal", None, None, classes, key)
        for desktop_id in spec["ids"]:
            entry = INDEX.by_id(desktop_id)
            if entry is not None:
                return AppTarget(entry.name, entry, None, classes | entry.classes, key)
        for desktop_id in spec["ids"]:
            if desktop_id in TUI_PROGRAMS and shutil.which(desktop_id):
                return AppTarget(desktop_id, None, desktop_id, classes | {desktop_id}, key)
        # Curated but not installed. Return the classes anyway so a RUNNING instance (an
        # AppImage, a manual install) can still be focused or closed.
        return AppTarget(spec["aliases"][0], None, None, classes, key)

    entry = INDEX.by_name(name)
    if entry is not None:
        return AppTarget(entry.name, entry, None, entry.classes, None)

    cleaned = normalize_name(name)
    if cleaned in TUI_PROGRAMS and shutil.which(cleaned):
        return AppTarget(cleaned, None, cleaned, {cleaned}, None)
    return None


def installed(target) -> bool:
    return bool(target and (target.entry or target.tui or target.canonical == "terminal"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     WEB APPS (Omarchy and friends)                     │
# └────────────────────────────────────────────────────────────────────────┘
# An Omarchy web app is a desktop entry whose Exec is `omarchy-launch-webapp <url>`; it runs as
# a Chrome `--app` window whose class is `chrome-<host><path>__-<profile>` (for example
# `chrome-web.whatsapp.com__-Default`). Kayra treats such a window as a BROWSER surface (the
# Kayra Bridge extension runs inside it) and prefers the web app when the user names its site.

_WEBAPP_LAUNCHERS = ("omarchy-launch-webapp", "omarchy-webapp-handler")
_APP_WINDOW_PREFIXES = ("chrome-", "brave-", "msedge-", "chromium-", "vivaldi-")
# Hosts that serve several unrelated products: the first path segment must match too, so the
# Google Maps web app is never what "open google" launches.
_SHARED_HOSTS = frozenset({"google.com", "docs.google.com", "music.youtube.com"})


def _host_path(url):
    raw = str(url or "").strip().strip('"').lower()
    raw = re.sub(r"^[a-z]+://", "", raw)
    host, _, path = raw.partition("/")
    host = host[4:] if host.startswith("www.") else host
    return host, path.split("?")[0].strip("/").split("/")[0]


def webapp_url(entry):
    """The URL a web-app desktop entry opens, or None for an ordinary application."""
    exec_line = getattr(entry, "exec", "") or ""
    if not any(launcher in exec_line for launcher in _WEBAPP_LAUNCHERS):
        return None
    m = re.search(r"https?://[^\s\"']+", exec_line)
    return m.group(0) if m else None


def webapp_for_url(url, entries=None):
    """The installed web app that serves `url`, or None. Exact host (and path when shared)."""
    host, segment = _host_path(url)
    if not host:
        return None
    for entry in (entries if entries is not None else INDEX.entries()):
        target = webapp_url(entry)
        if not target:
            continue
        app_host, app_segment = _host_path(target)
        if app_host != host:
            continue
        if host in _SHARED_HOSTS and app_segment != segment:
            continue
        return entry
    return None


def is_app_window_class(cls):
    """True for a Chromium `--app` window class such as `chrome-web.whatsapp.com__-Default`."""
    low = str(cls or "").lower()
    return low.startswith(_APP_WINDOW_PREFIXES) and "__" in low


def site_of_window_class(cls):
    """'chrome-web.whatsapp.com__-Default' -> 'web.whatsapp.com'. None for anything else."""
    if not is_app_window_class(cls):
        return None
    body = str(cls).split("-", 1)[1].split("__", 1)[0]
    return body.split("_", 1)[0] or None
