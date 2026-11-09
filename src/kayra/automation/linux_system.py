# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/linux_system.py                       │
# │   Devices, software, look & feel, processes, dev tools, windows, files │
# └────────────────────────────────────────────────────────────────────────┘
"""
The wide middle of Kayra's Linux hands — everything that is not a window primitive
(`linux.py`), not a website (`web_macros.py`) and not a workflow (`workflows.py`).

Every external program is a FIXED argument vector run through `linux.RUNNER`; the user's
words only ever become an argument (a device name, a package name, a file path), never a
command. Anything that installs, removes, deletes, sends or commits is CONFIRM-gated by the
policy before it gets here; things that need the user's password (system updates, package
installs) are opened in a visible terminal so the USER types the password, never Kayra.
"""

import os
import re
import json
import time
import shutil
import hashlib
import datetime
import threading
import subprocess

try:
    import psutil
except Exception:                      # pragma: no cover
    psutil = None

from kayra.automation import ai
from kayra.automation.policy import ActionResult, CRITICAL_PACKAGES_PREFIXES


def _L():
    from kayra.automation import linux
    return linux


def _ok(success, failure):
    return ActionResult.success(success) if success else ActionResult.failure(failure)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         AUDIO DEVICES                                  │
# └────────────────────────────────────────────────────────────────────────┘

def audio_devices(kind="Sinks"):
    """[(id, name, is_default)] from `wpctl status` for Sinks or Sources."""
    code, text = _L()._run("wpctl", "status", timeout=3.0)
    if code != 0:
        return []
    out, inside = [], False
    for line in text.splitlines():
        if re.search(rf"[├└]─ {kind}:", line):
            inside = True
            continue
        if inside:
            if re.search(r"[├└]─ ", line):
                break
            match = re.search(r"(\*)?\s+(\d+)\.\s+(.+?)\s*(\[vol:.*)?$", line)
            if match:
                out.append((match.group(2), match.group(3).strip(), bool(match.group(1))))
    return out


_OUTPUT_WORDS = {
    "headphones": r"buds|headphone|headset|airpods|bluez|wh-|jbl|sony|bose|boat|realme|earbud",
    "speakers": r"speaker|analog|built-in|laptop|ryzen|intel|hd audio",
    "hdmi": r"hdmi|displayport|monitor|tv",
}


def switch_output(text):
    L = _L()
    sinks = audio_devices("Sinks")
    if not sinks:
        return ActionResult.unsupported("I can't see any audio outputs.")
    low = (text or "").lower()
    want = next((k for k in _OUTPUT_WORDS if k in low or (k == "headphones" and re.search(
        r"earbuds|buds|headset|bluetooth", low)) or (k == "speakers" and "laptop" in low)), None)
    pick = None
    if want:
        pick = next((s for s in sinks if re.search(_OUTPUT_WORDS[want], s[1], re.I)), None)
    if pick is None:
        words = [w for w in re.findall(r"[a-z0-9]+", low) if w not in (
            "switch", "audio", "sound", "output", "to", "my", "the", "use", "play", "on")]
        pick = next((s for s in sinks if any(w in s[1].lower() for w in words)), None)
    if pick is None:
        names = ", ".join(s[1] for s in sinks)
        return ActionResult.not_found(f"Which output? I have: {names}.")
    previous = next((s for s in sinks if s[2]), None)
    if not L._ok("wpctl", "set-default", pick[0]):
        return ActionResult.failure("I couldn't switch the audio output.")
    if previous and previous[0] != pick[0]:
        L.remember_undo(f"audio back on {previous[1]}",
                        lambda: L._ok("wpctl", "set-default", previous[0]))
    return ActionResult.success(f"Sound is now on {pick[1]}.")


def list_outputs():
    sinks = audio_devices("Sinks")
    if not sinks:
        return ActionResult.not_found("I can't see any audio outputs.")
    current = next((s[1] for s in sinks if s[2]), "none")
    return ActionResult.success(f"Playing on {current}. Available: "
                                + ", ".join(s[1] for s in sinks) + ".")


def microphone(text):
    L = _L()
    source = "@DEFAULT_AUDIO_SOURCE@"
    low = (text or "").lower()
    if re.search(r"\bunmute\b|\bon\b|enable", low):
        return _ok(L._ok("wpctl", "set-mute", source, "0"), "Microphone on.", "I couldn't change it.")
    if re.search(r"\bmute\b|\boff\b|disable", low):
        return _ok(L._ok("wpctl", "set-mute", source, "1"), "Microphone muted.",
                   "I couldn't change it.")
    number = re.search(r"(\d{1,3})", low)
    if number:
        level = max(5, min(100, int(number.group(1))))
        return _ok(L._ok("wpctl", "set-volume", source, f"{level}%"),
                   f"Microphone at {level} percent.", "I couldn't change it.")
    if re.search(r"louder|up|increase|raise", low):
        return _ok(L._ok("wpctl", "set-volume", "-l", "1.0", source, "10%+"), "Microphone louder.",
                   "I couldn't change it.")
    if re.search(r"quieter|down|decrease|lower", low):
        return _ok(L._ok("wpctl", "set-volume", source, "10%-"), "Microphone quieter.",
                   "I couldn't change it.")
    code, out = L._run("wpctl", "get-volume", source, timeout=2.0)
    match = re.search(r"([\d.]+)", out)
    level = round(float(match.group(1)) * 100) if code == 0 and match else None
    muted = "MUTED" in out
    return ActionResult.success("Your microphone is muted." if muted else
                                f"Your microphone is at {level} percent." if level is not None
                                else "I can't read the microphone.")


def app_streams():
    """[(index, app_name, muted)] for apps currently playing sound (PulseAudio view)."""
    code, text = _L()._run("pactl", "-f", "json", "list", "sink-inputs", timeout=3.0)
    if code != 0:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for item in data:
        props = item.get("properties") or {}
        name = props.get("application.name") or props.get("application.process.binary") or ""
        out.append((str(item.get("index")), name, bool(item.get("mute"))))
    return out


def app_volume(text):
    """'lower chrome's volume', 'mute discord', 'set spotify to 30'. None if no app is named."""
    L = _L()
    low = (text or "").lower()
    streams = app_streams()
    hit = next((s for s in streams if s[1] and s[1].lower().split()[0] in low), None)
    if hit is None:
        return None
    index, name = hit[0], hit[1]
    if re.search(r"unmute", low):
        return _ok(L._ok("pactl", "set-sink-input-mute", index, "0"), f"{name} unmuted.", "Couldn't.")
    if re.search(r"\bmute\b|silence", low):
        return _ok(L._ok("pactl", "set-sink-input-mute", index, "1"), f"{name} muted.", "Couldn't.")
    number = re.search(r"(\d{1,3})", low)
    if number and " by " not in f" {low} ":
        level = min(100, int(number.group(1)))
        return _ok(L._ok("pactl", "set-sink-input-volume", index, f"{level}%"),
                   f"{name} at {level} percent.", "Couldn't.")
    step = f"-{number.group(1) if number else 15}%" if re.search(
        r"lower|down|quieter|decrease|reduce", low) else f"+{number.group(1) if number else 15}%"
    return _ok(L._ok("pactl", "set-sink-input-volume", index, step),
               f"{name} {'quieter' if step.startswith('-') else 'louder'}.", "Couldn't.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         BLUETOOTH DEVICES                              │
# └────────────────────────────────────────────────────────────────────────┘

def paired_devices():
    code, text = _L()._run("bluetoothctl", "devices", timeout=4.0)
    out = []
    for line in text.splitlines() if code == 0 else []:
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            out.append((parts[1], parts[2]))
    return out


def _match_device(text):
    devices = paired_devices()
    low = (text or "").lower()
    generic = {"earbuds": r"bud|ear|pod|headphone|headset", "headphones": r"bud|headphone|headset|ear",
               "speaker": r"speaker|boom|jbl|soundlink", "mouse": r"mouse|ergo|mx", "keyboard": r"keyboard|keys"}
    for mac, name in devices:
        if any(w in name.lower() for w in re.findall(r"[a-z0-9]{3,}", low)
               if w not in ("connect", "disconnect", "my", "the", "bluetooth", "battery", "device")):
            return mac, name
    for word, pattern in generic.items():
        if word in low or word.rstrip("s") in low:
            hit = next(((m, n) for m, n in devices if re.search(pattern, n, re.I)), None)
            if hit:
                return hit
    return None


def bluetooth_device(text):
    L = _L()
    low = (text or "").lower()
    device = _match_device(text)
    if device is None:
        names = ", ".join(n for _, n in paired_devices())
        return ActionResult.not_found(f"Which device? Paired: {names}." if names else
                                      "No Bluetooth devices are paired.")
    mac, name = device
    if "battery" in low:
        code, info = L._run("bluetoothctl", "info", mac, timeout=4.0)
        match = re.search(r"Battery Percentage:.*\((\d+)\)", info)
        return ActionResult.success(f"{name} is at {match.group(1)} percent." if match else
                                    f"{name} doesn't report its battery.")
    if "disconnect" in low:
        return _ok(L._ok("bluetoothctl", "disconnect", mac, timeout=10.0),
                   f"{name} disconnected.", f"I couldn't disconnect {name}.")
    L._ok("bluetoothctl", "power", "on", timeout=5.0)
    return _ok(L._ok("bluetoothctl", "connect", mac, timeout=15.0), f"{name} connected.",
               f"{name} didn't connect. Is it on and nearby?")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               WI-FI                                    │
# └────────────────────────────────────────────────────────────────────────┘

def wifi_connect(text):
    L = _L()
    name = re.sub(r"^.*?(connect (?:me )?to(?: the)?(?: wifi| wi-fi| network)?)\s*", "", text or "",
                  flags=re.I).strip(" .")
    if not name:
        return ActionResult.failure("Which network?")
    code, out = L._run("nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no",
                       timeout=5.0)
    ssids = [s.replace("\\:", ":") for s in out.splitlines() if s] if code == 0 else []
    exact = next((s for s in ssids if s.lower() == name.lower()), None) or \
        next((s for s in ssids if name.lower() in s.lower()), None)
    if exact is None:
        return ActionResult.not_found(f"I don't see a network called {name}.")
    if L._ok("nmcli", "device", "wifi", "connect", exact, timeout=25.0):
        return ActionResult.success(f"Connected to {exact}.")
    return ActionResult.failure(f"{exact} needs a password I don't have. Say \"open wifi "
                                "settings\" to enter it.")


def wifi_forget(text):
    L = _L()
    name = re.sub(r"^.*?forget(?: the)?(?: wifi| wi-fi| network)?\s*", "", text or "", flags=re.I).strip(" .")
    if not name:
        return ActionResult.failure("Which network should I forget?")
    return _ok(L._ok("nmcli", "connection", "delete", "id", name, timeout=8.0),
               f"Forgot {name}.", f"I don't have a saved network called {name}.")


def wifi_share():
    L = _L()
    if L._have("omarchy-shell") and L.RUNNER.spawn(["omarchy-shell", "shell", "toggle",
                                                    "omarchy.wifiqr"]):
        return ActionResult.success("Here's the Wi-Fi QR code.")
    term = L._terminal_argv(["nmcli", "device", "wifi", "show-password"])
    return _ok(bool(term) and L.RUNNER.spawn(term), "The password and QR code are in the terminal.",
               "I couldn't show it.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        UPDATES AND SOFTWARE                            │
# └────────────────────────────────────────────────────────────────────────┘

def check_updates():
    L = _L()
    if not L._have("checkupdates"):
        return ActionResult.unsupported("I can't check for updates here.")
    code, text = L._run("checkupdates", timeout=40.0)
    repo = [l for l in text.splitlines() if l.strip()] if code == 0 else []
    aur = []
    if L._have("yay"):
        code, text = L._run("yay", "-Qua", timeout=40.0)
        aur = [l for l in text.splitlines() if l.strip()] if code == 0 else []
    total = len(repo) + len(aur)
    if not total:
        return ActionResult.success("Everything is up to date.")
    names = ", ".join(l.split()[0] for l in (repo + aur)[:4])
    return ActionResult.success(f"{total} update{'s' if total != 1 else ''} available, including "
                                f"{names}. Say \"update my system\" to install them.")


def update_system():
    L = _L()
    from kayra import platforms
    battery = platforms.battery_status() or {}
    if battery.get("state") == "discharging" and (battery.get("percent") or 100) < 40:
        return ActionResult.blocked("You're on battery below 40 percent. Plug in first — an "
                                    "update that dies halfway can break the system.")
    command = ["omarchy-update"] if L._have("omarchy-update") else \
        (["yay", "-Syu"] if L._have("yay") else ["sudo", "pacman", "-Syu"])
    term = L._terminal_argv(command)
    if term and L.RUNNER.spawn(term):
        return ActionResult.success("The update is running in a terminal. It will ask for your "
                                    "password there.")
    return ActionResult.failure("I couldn't open a terminal for the update.")


_PACKAGE_ALIASES = {"vs code": "visual-studio-code-bin", "vscode": "visual-studio-code-bin",
                    "chrome": "google-chrome", "spotify": "spotify", "discord": "discord",
                    "steam": "steam", "obs": "obs-studio", "vlc": "vlc", "gimp": "gimp",
                    "zoom": "zoom", "slack": "slack-desktop", "telegram": "telegram-desktop"}


def package_name(text):
    low = re.sub(r"^(install|uninstall|remove|delete|get me|download)\s+", "",
                 (text or "").lower().strip(" .")).replace(" app", "").strip()
    return _PACKAGE_ALIASES.get(low, re.sub(r"[^a-z0-9+._-]+", "-", low).strip("-"))


def install_package(text, remove=False):
    L = _L()
    name = package_name(text)
    if not name:
        return ActionResult.failure("Which app?")
    if remove and name.startswith(CRITICAL_PACKAGES_PREFIXES):
        return ActionResult.blocked(f"{name} is part of the core system. I won't remove it.")
    tool = "yay" if L._have("yay") else "pacman"
    argv = ([tool] if tool == "yay" else ["sudo", tool]) + (["-Rns", name] if remove else ["-S", "--needed", name])
    term = L._terminal_argv(argv)
    if term and L.RUNNER.spawn(term):
        return ActionResult.success(f"{'Removing' if remove else 'Installing'} {name} in a "
                                    "terminal. Confirm it there with your password.")
    return ActionResult.failure("I couldn't open a terminal.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  DISPLAY, INPUT, THEME, WALLPAPER                      │
# └────────────────────────────────────────────────────────────────────────┘

def display_set(text):
    L = _L()
    monitor = L._focused_monitor()
    if monitor is None:
        return ActionResult.unsupported("Display changes need the Hyprland session.")
    low = (text or "").lower()
    name = monitor.get("name")
    width, height = monitor.get("width"), monitor.get("height")
    rate = monitor.get("refreshRate") or 60
    scale = monitor.get("scale") or 1
    transform = monitor.get("transform") or 0
    number = re.search(r"(\d+(?:\.\d+)?)", low)
    if "refresh" in low or "hz" in low or "hertz" in low:
        if not number:
            return ActionResult.failure("Which refresh rate?")
        rate = float(number.group(1))
    elif "scale" in low or "bigger" in low or "smaller" in low:
        if number:
            scale = float(number.group(1))
        else:
            scale = round(scale + (0.25 if "bigger" in low or "up" in low else -0.25), 2)
        scale = max(0.5, min(3.0, scale))
    elif "rotate" in low:
        transform = 1 if "left" in low else 3 if "right" in low else 0 if "normal" in low else 2
    else:
        return ActionResult.failure("Change the refresh rate, scale or rotation?")
    spec = f"{name},{width}x{height}@{rate:g},auto,{scale:g},transform,{transform}"
    previous = (f"{name},{width}x{height}@{monitor.get('refreshRate') or 60:g},auto,"
                f"{monitor.get('scale') or 1:g},transform,{monitor.get('transform') or 0}")
    ok = L._ok("hyprctl", "keyword", "monitor", spec, timeout=5.0)
    if ok:
        L.remember_undo("display settings back", lambda: L._ok("hyprctl", "keyword", "monitor",
                                                               previous, timeout=5.0))
    return _ok(ok, "Display updated. Say \"undo that\" if it looks wrong.",
               "Hyprland refused that display setting.")


def touchpad():
    L = _L()
    return _ok(L._have("omarchy-toggle-touchpad") and L._ok("omarchy-toggle-touchpad"),
               "Touchpad toggled.", "I can't toggle the touchpad here.")


def keyboard_layout():
    L = _L()
    return _ok(L._ok("hyprctl", "switchxkblayout", "all", "next"), "Switched keyboard layout.",
               "I couldn't switch the layout.")


def keyboard_backlight(text):
    L = _L()
    low = (text or "").lower()
    verb = "off" if "off" in low else "up" if re.search(r"\bon\b|up|brighter", low) else \
        "down" if re.search(r"down|dim", low) else "cycle"
    return _ok(L._have("omarchy-brightness-keyboard") and
               L._ok("omarchy-brightness-keyboard", verb), "Keyboard backlight adjusted.",
               "I can't change the keyboard backlight here.")


def theme(text):
    L = _L()
    if not L._have("omarchy-theme-set"):
        return ActionResult.unsupported("Themes are an Omarchy feature.")
    _, current = L._run("omarchy-theme-current", timeout=3.0)
    current = current.strip()
    _, listing = L._run("omarchy-theme-list", timeout=3.0)
    themes = [t.strip() for t in listing.splitlines() if t.strip()]
    low = (text or "").lower()
    pick = next((t for t in themes if t.lower() in low), None)
    if pick is None and re.search(r"next|another|different|change|switch", low) and themes:
        index = themes.index(current) if current in themes else -1
        pick = themes[(index + 1) % len(themes)]
    if pick is None and re.search(r"light", low):
        pick = next((t for t in themes if "light" in t.lower() or "latte" in t.lower()), None)
    if pick is None and re.search(r"dark", low):
        pick = next((t for t in themes if t in ("Tokyo Night", "Catppuccin", "Gruvbox", "Nord",
                                                 "Kanagawa", "Everforest")), None)
    if pick is None:
        return ActionResult.success(f"You're on {current}. Themes: {', '.join(themes[:8])}.")
    if not L._ok("omarchy-theme-set", pick, timeout=30.0):
        return ActionResult.failure(f"I couldn't switch to {pick}.")
    if current:
        L.remember_undo(f"theme back to {current}",
                        lambda: L._ok("omarchy-theme-set", current, timeout=30.0))
    return ActionResult.success(f"Theme set to {pick}.")


def wallpaper():
    L = _L()
    return _ok(L._have("omarchy-theme-bg-next") and L._ok("omarchy-theme-bg-next", timeout=10.0),
               "New wallpaper.", "I can't change the wallpaper here.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       PROCESSES AND DOCKER                             │
# └────────────────────────────────────────────────────────────────────────┘

def top_processes(text):
    from kayra.automation import diagnostics
    by = "memory" if re.search(r"memory|ram", (text or "").lower()) else "cpu"
    rows = diagnostics.top_processes(by, 3)
    if not rows:
        return ActionResult.not_found("I can't read the process list.")
    unit = "of memory" if by == "memory" else "of the processor"
    parts = [f"{r['name']} at {r[by]:.0f} percent" for r in rows]
    return ActionResult.success(f"Most {unit}: " + ", ".join(parts) + ".")


def close_hog(text):
    from kayra.automation import diagnostics
    by = "memory" if re.search(r"memory|ram", (text or "").lower()) else "cpu"
    rows = diagnostics.top_processes(by, 5)
    L = _L()
    for row in rows:
        result = L.close_target(row["name"])
        if result.ok:
            return ActionResult.success(f"Closed {row['name']}, which was using the most "
                                        f"{'memory' if by == 'memory' else 'CPU'}.")
    return ActionResult.not_found("The biggest user is a background process without a window; "
                                  "I only close apps through their windows.")


def docker(text):
    L = _L()
    if not L._have("docker"):
        return ActionResult.unsupported("Docker isn't installed.")
    low = (text or "").lower()
    code, out = L._run("docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}", timeout=8.0)
    if code != 0:
        return ActionResult.failure("Docker isn't running, or I can't reach it.")
    rows = [l.split("\t") for l in out.splitlines() if "\t" in l]
    target = next((r[0] for r in rows if r[0].lower() in low), None)
    for verb in ("restart", "stop", "start"):
        if re.search(rf"\b{verb}\b", low):
            if not target:
                return ActionResult.failure(f"Which container should I {verb}?")
            return _ok(L._ok("docker", verb, target, timeout=60.0), f"{target}: {verb} done.",
                       f"Docker couldn't {verb} {target}.")
    running = [r[0] for r in rows if r[1].startswith("Up")]
    if not rows:
        return ActionResult.success("No containers.")
    return ActionResult.success(f"{len(running)} of {len(rows)} containers running"
                                + (": " + ", ".join(running[:5]) if running else "") + ".")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          DEVELOPER TOOLS                               │
# └────────────────────────────────────────────────────────────────────────┘

def project_dir():
    """The repo the user means: the focused terminal's folder, KAYRA_DEV_PROJECT, or Kayra's."""
    L = _L()
    if L._have("omarchy-cmd-terminal-cwd"):
        code, out = L._run("omarchy-cmd-terminal-cwd", timeout=2.0)
        path = out.strip()
        if code == 0 and path and _git_root(path):
            return _git_root(path)
    configured = os.environ.get("KAYRA_DEV_PROJECT")
    if configured and _git_root(configured):
        return _git_root(configured)
    from kayra.core.paths import project_root
    return _git_root(project_root())


def _git_root(path):
    probe = os.path.abspath(os.path.expanduser(path or ""))
    while probe and probe != os.path.dirname(probe):
        if os.path.isdir(os.path.join(probe, ".git")):
            return probe
        probe = os.path.dirname(probe)
    return None


def git_status():
    L = _L()
    repo = project_dir()
    if not repo:
        return ActionResult.not_found("I'm not in a git project.")
    code, out = L._run("git", "-C", repo, "status", "--porcelain=v1", "--branch", timeout=6.0)
    if code != 0:
        return ActionResult.failure("Git didn't answer.")
    lines = out.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("##") else ""
    branch = head.split("...")[0]
    ahead = re.search(r"ahead (\d+)", head)
    behind = re.search(r"behind (\d+)", head)
    changes = [l for l in lines[1:] if l.strip()]
    untracked = sum(1 for l in changes if l.startswith("??"))
    sentence = (f"{os.path.basename(repo)} is on {branch}, with {len(changes) - untracked} changed "
                f"and {untracked} new files")
    if ahead:
        sentence += f", {ahead.group(1)} commits not pushed"
    if behind:
        sentence += f", {behind.group(1)} behind the remote"
    return ActionResult.success(sentence + ".")


def git_branch():
    L = _L()
    repo = project_dir()
    code, out = L._run("git", "-C", repo or ".", "branch", "--show-current", timeout=4.0)
    return ActionResult.success(f"You're on {out.strip()}.") if code == 0 and out.strip() else \
        ActionResult.not_found("I'm not in a git project.")


def git_commit(message):
    L = _L()
    repo = project_dir()
    if not repo:
        return ActionResult.not_found("I'm not in a git project.")
    message = (message or "").strip()
    if not message:
        return ActionResult.failure("What should the commit message be?")
    if not L._ok("git", "-C", repo, "add", "-A", timeout=15.0):
        return ActionResult.failure("Git couldn't stage the changes.")
    code, out = L._run("git", "-C", repo, "commit", "-m", message, timeout=30.0)
    return _ok(code == 0, f"Committed to {os.path.basename(repo)}.",
               "Nothing to commit." if "nothing to commit" in out else "The commit failed.")


def run_project(kind="tests"):
    L = _L()
    repo = project_dir()
    if not repo:
        return ActionResult.not_found("I'm not in a project.")
    command = None
    if kind == "tests":
        if os.path.exists(os.path.join(repo, "tests", "run_all.py")):
            python = os.path.join(repo, ".venv", "bin", "python")
            command = [python if os.path.exists(python) else "python3", "tests/run_all.py"]
        elif os.path.exists(os.path.join(repo, "package.json")):
            command = ["npm", "test"]
        elif os.path.exists(os.path.join(repo, "Cargo.toml")):
            command = ["cargo", "test"]
        elif os.path.exists(os.path.join(repo, "pyproject.toml")) or os.path.isdir(os.path.join(repo, "tests")):
            command = ["python3", "-m", "pytest"]
    else:
        if os.path.exists(os.path.join(repo, "package.json")):
            command = ["npm", "run", "dev"]
        elif os.path.exists(os.path.join(repo, "manage.py")):
            command = ["python3", "manage.py", "runserver"]
        elif os.path.exists(os.path.join(repo, "run.py")):
            command = ["python3", "run.py"]
    if command is None:
        return ActionResult.not_found(f"I don't know how to run {kind} in {os.path.basename(repo)}.")
    term = L._terminal_argv(["bash", "-c", f"cd {shlex_quote(repo)} && "
                             + " ".join(shlex_quote(c) for c in command) + "; exec bash"])
    return _ok(bool(term) and L.RUNNER.spawn(term),
               f"Running {kind} for {os.path.basename(repo)} in a terminal.", "I couldn't open a terminal.")


def shlex_quote(text):
    import shlex
    return shlex.quote(str(text))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                           NOTIFICATIONS                                │
# └────────────────────────────────────────────────────────────────────────┘
# A Kayra-owned `dbus-monitor` listens for Notify calls on the session bus and keeps the last
# 50 in RAM (never on disk). Started by `start_notification_log()`, stopped at shutdown —
# the ONE child process Kayra keeps, and it is reaped by its own handle, never by name.

_NOTES = {"process": None, "items": [], "lock": threading.Lock(), "read_at": 0.0}


def start_notification_log():
    L = _L()
    if _NOTES["process"] is not None or not L._have("dbus-monitor"):
        return False
    try:
        process = subprocess.Popen(
            ["dbus-monitor", "--session",
             "interface='org.freedesktop.Notifications',member='Notify'"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, shell=False)
    except OSError:
        return False
    _NOTES["process"] = process
    threading.Thread(target=_read_notifications, args=(process,), name="kayra-notifications",
                     daemon=True).start()
    return True


def stop_notification_log():
    process = _NOTES.pop("process", None)
    _NOTES["process"] = None
    if process is not None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            pass


def _read_notifications(process):
    strings, inside = [], False
    for line in process.stdout:
        if "member=Notify" in line:
            if inside:
                _note_from(strings)
            strings, inside = [], True
            continue
        if inside:
            match = re.match(r'\s*string "(.*)"\s*$', line)
            if match:
                strings.append(match.group(1))
            if line.strip().startswith("array [") and len(strings) >= 4:
                _note_from(strings)
                strings, inside = [], False


def _note_from(strings):
    if len(strings) < 4:
        return
    app, summary, body = strings[0], strings[2], strings[3]
    if app.lower() in ("kayra",):
        return
    with _NOTES["lock"]:
        _NOTES["items"].append({"app": app, "summary": summary, "body": body, "at": time.time()})
        del _NOTES["items"][:-50]


def note_notification(app, summary, body=""):
    """Test seam (and a way for Kayra's own code to log one)."""
    _note_from([app, "", summary, body])


def read_notifications(text=""):
    with _NOTES["lock"]:
        items = list(_NOTES["items"])
    if "clear" in (text or "").lower():
        with _NOTES["lock"]:
            _NOTES["items"].clear()
        L = _L()
        if L._have("omarchy-notification-dismiss"):
            L._ok("omarchy-notification-dismiss", "--all")
        return ActionResult.success("Notifications cleared.")
    since = _NOTES["read_at"] if re.search(r"miss|new|since", (text or "").lower()) else 0
    fresh = [n for n in items if n["at"] > since]
    _NOTES["read_at"] = time.time()
    if not fresh:
        return ActionResult.success("No new notifications." if since else
                                    "I haven't seen any notifications yet.")
    parts = [f"{n['app']}: {n['summary']}" + (f", {n['body'][:80]}" if n['body'] else "")
             for n in fresh[-5:]][::-1]
    return ActionResult.success(f"{len(fresh)} notification{'s' if len(fresh) != 1 else ''}. "
                                + ". ".join(parts) + ".")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     WINDOWS: LAYOUTS AND SESSIONS                      │
# └────────────────────────────────────────────────────────────────────────┘

def list_open_windows():
    L = _L()
    windows = L.list_windows()
    if not windows:
        return ActionResult.success("No windows are open.")
    by_ws = {}
    for w in windows:
        by_ws.setdefault(w.workspace, []).append(_pretty(w))
    parts = [f"workspace {ws}: {', '.join(sorted(set(names)))}"
             for ws, names in sorted(by_ws.items(), key=lambda kv: _ws_key(kv[0]))]
    sentence = "; ".join(parts)
    return ActionResult.success(sentence[:1].upper() + sentence[1:] + ".")


def where_is(text):
    L = _L()
    name = re.sub(r"^(where is|where's|which workspace is|find)\s+(my\s+|the\s+)?", "",
                  (text or "").lower()).replace(" on", "").strip(" ?.")
    app = L.linux_apps.resolve(name)
    windows = L._windows_for(app, strict=False) if app else \
        [w for w in L.list_windows() if name in w.title.lower()]
    if not windows:
        return ActionResult.not_found(f"{name.title()} isn't open.")
    spaces = sorted({w.workspace for w in windows}, key=_ws_key)
    return ActionResult.success(f"{(app.label if app else name).title()} is on workspace "
                                + " and ".join(spaces) + ".")


def close_except(text):
    L = _L()
    keep_name = re.sub(r"^.*?(except|but)\s+", "", (text or "").lower()).strip(" .")
    keep = L.linux_apps.resolve(keep_name)
    keep_classes = keep.classes if keep else set()
    closing = [w for w in L.list_windows() if not (w.classes & keep_classes)
               and keep_name not in w.title.lower()]
    for w in closing:
        L.close_window(w)
    return ActionResult.success(f"Closed {len(closing)} window{'s' if len(closing) != 1 else ''}, "
                                f"kept {keep_name}.")


def move_all(text):
    L = _L()
    low = (text or "").lower()
    target = re.search(r"workspace (\w+)", low)
    workspace = L._workspace_name(f"workspace {target.group(1)}") if target else None
    if workspace == "empty":
        workspace = L.first_empty_workspace()
    if not workspace:
        return ActionResult.failure("To which workspace?")
    subject = re.sub(r"^move (all|every)\s+(my\s+|the\s+)?|\s+(windows?\s+)?to workspace.*$", "", low).strip()
    if subject in ("browsers", "browser", "browser windows"):
        windows = [w for w in L.list_windows() if L._is_browser_window(w)]
    else:
        app = L.linux_apps.resolve(subject.rstrip("s")) or L.linux_apps.resolve(subject)
        windows = L._windows_for(app, strict=True) if app else []
    if not windows:
        return ActionResult.not_found(f"I don't see any {subject} open.")
    for w in windows:
        L._dispatch_window("move", f"movetoworkspacesilent {workspace},{w.selector}", w,
                           workspace=workspace, follow=False)
    return ActionResult.success(f"Moved {len(windows)} to workspace {workspace}.")


def tidy_windows():
    """Every app's windows onto the workspace where its first window already is."""
    L = _L()
    home, moved = {}, 0
    for w in sorted(L.list_windows(), key=lambda w: _ws_key(w.workspace)):
        key = w.cls
        if key not in home:
            home[key] = w.workspace
        elif w.workspace != home[key] and not w.workspace.startswith("special"):
            L._dispatch_window("move", f"movetoworkspacesilent {home[key]},{w.selector}", w,
                               workspace=home[key], follow=False)
            moved += 1
    return ActionResult.success(f"Grouped your windows by app; moved {moved}.")


def other_monitor():
    L = _L()
    window = L.active_window()
    if window is None:
        return ActionResult.not_found("No window is in front.")
    if len(L.HYPR.monitors()) < 2:
        return ActionResult.not_found("There's only one screen.")
    return _ok(L._dispatch_window("move", "movewindow mon:+1", window, monitor="+1"),
               "Moved it to the other screen.", "I couldn't move it.")


def resize(text):
    L = _L()
    window = L.active_window()
    if window is None:
        return ActionResult.not_found("No window is in front.")
    grow = not re.search(r"smaller|shrink|narrower|less", (text or "").lower())
    step = 120 if grow else -120
    ok = L.HYPR.dispatch(f"hl.dsp.window.resize({{ x = {step}, y = {step // 2}, relative = true, "
                         f"window = {L.HYPR.lua_string(window.selector)} }})",
                         f"resizeactive {step} {step // 2}")
    if ok:
        L.remember_undo("window size back", lambda: L.HYPR.dispatch(
            f"hl.dsp.window.resize({{ x = {-step}, y = {-step // 2}, relative = true, "
            f"window = {L.HYPR.lua_string(window.selector)} }})", f"resizeactive {-step} {-step // 2}"))
    return _ok(ok, "Bigger." if grow else "Smaller.", "I couldn't resize it.")


def side_by_side(text):
    """'chrome on the left and vs code on the right' -> both on a fresh workspace, in order."""
    L = _L()
    low = (text or "").lower()
    match = re.search(r"(.+?) (?:on the )?left(?:,| and)? (.+?) (?:on the )?right", low) or \
        re.search(r"(?:put )?(.+?) and (.+?) side by side", low) or \
        re.search(r"side by side[: ]+(.+?) and (.+)$", low)
    if not match:
        return ActionResult.failure("Which two apps? Say: Chrome on the left, VS Code on the right.")
    names = [re.sub(r"^(put|place|open|show)\s+|\s+(on|to)\s+the$|\s+on$", "", n).strip(" ,.")
             for n in match.groups()]
    workspace = L.first_empty_workspace()
    placed = []
    for name in names:
        result = L.open_application(name)
        if not result.ok:
            return result
        app = L.linux_apps.resolve(name)
        windows = L._windows_for(app, strict=True) if app else []
        window = L._pick_single(windows, prefer_recent=True) if windows else None
        if window is None:
            return ActionResult.failure(f"{name} has no window yet. Try again in a moment.")
        L._dispatch_window("float", f"settiled {window.selector}", window, action="off")
        L._dispatch_window("move", f"movetoworkspacesilent {workspace},{window.selector}",
                           window, workspace=workspace, follow=False)
        placed.append(app.label if app else name)
    L._focus_workspace(workspace)
    return ActionResult.success(f"{placed[0]} on the left, {placed[1]} on the right, on "
                                f"workspace {workspace}.")


def _pretty(window):
    L = _L()
    for spec_name, spec in L.linux_apps.LINUX_APPS.items():
        if window.classes & {c.lower() for c in spec["classes"]}:
            alias = spec["aliases"][0]
            return " ".join(w if w.isupper() else w.capitalize() for w in alias.split())
    return window.cls.split(".")[-1].replace("-", " ").title() or window.title[:20]


def _ws_key(workspace):
    return (0, int(workspace)) if str(workspace).isdigit() else (1, str(workspace))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               FILES                                    │
# └────────────────────────────────────────────────────────────────────────┘

_TYPES = {"pdf": (".pdf",), "document": (".pdf", ".doc", ".docx", ".odt", ".txt", ".md"),
          "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"), "photo": (".jpg", ".jpeg", ".png", ".heic"),
          "video": (".mp4", ".mkv", ".mov", ".webm"), "music": (".mp3", ".flac", ".m4a", ".ogg"),
          "spreadsheet": (".xlsx", ".xls", ".csv", ".ods"), "presentation": (".pptx", ".ppt", ".odp"),
          "zip": (".zip", ".tar", ".gz", ".7z", ".rar"), "code": (".py", ".js", ".ts", ".rs", ".go", ".c", ".cpp")}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".local",
              ".mozilla", ".npm", ".cargo", ".rustup", "site-packages", ".config"}


def _walk(root, max_files=40000, max_depth=6):
    seen = 0
    root_depth = root.rstrip("/").count("/")
    for path, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if path.count("/") - root_depth >= max_depth:
            dirs[:] = []
        for name in files:
            seen += 1
            if seen > max_files:
                return
            yield os.path.join(path, name)


def _when_window(low):
    now = time.time()
    if "today" in low:
        start = datetime.datetime.combine(datetime.date.today(), datetime.time())
        return start.timestamp(), now
    if "yesterday" in low:
        start = datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=1), datetime.time())
        return start.timestamp(), start.timestamp() + 86400
    if "this week" in low or "last week" in low:
        return now - 7 * 86400, now
    return None


def find_files(text):
    low = (text or "").lower()
    kinds = [ext for word, exts in _TYPES.items() if re.search(rf"\b{word}s?\b", low) for ext in exts]
    window = _when_window(low)
    words = [w for w in re.findall(r"[a-z0-9]{3,}", low) if w not in (
        "find", "search", "where", "locate", "file", "files", "the", "that", "downloaded", "yesterday",
        "today", "week", "last", "this", "and", "for", "called", "named", "my") and w not in _TYPES
        and w.rstrip("s") not in _TYPES]
    root = os.path.expanduser("~/Downloads") if "download" in low else os.path.expanduser("~")
    hits = []
    for path in _walk(root):
        name = os.path.basename(path).lower()
        if kinds and not name.endswith(tuple(kinds)):
            continue
        if words and not all(w in name for w in words):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if window and not (window[0] <= mtime <= window[1]):
            continue
        hits.append((mtime, path))
    if not hits:
        return ActionResult.not_found("I couldn't find a file like that.")
    hits.sort(reverse=True)
    best = hits[0][1]
    L = _L()
    if L._have("xdg-open"):
        L.RUNNER.spawn(["xdg-open", os.path.dirname(best)])
    extra = f" There are {len(hits) - 1} more." if len(hits) > 1 else ""
    return ActionResult.success(f"The newest match is {os.path.basename(best)}, in "
                                f"{os.path.dirname(best).replace(os.path.expanduser('~'), 'home')}. "
                                f"I've opened its folder.{extra}", path=best,
                                matches=[p for _, p in hits[:10]])


def recent_files(text=""):
    """Files the desktop recorded as recently used (XDG recently-used.xbel), today by default."""
    path = os.path.expanduser("~/.local/share/recently-used.xbel")
    low = (text or "").lower()
    window = _when_window(low) or _when_window("today")
    items = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            data = handle.read()
        for href, modified in re.findall(r'<bookmark href="file://([^"]+)"[^>]*?modified="([^"]+)"', data):
            stamp = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
            if window[0] <= stamp <= window[1]:
                import html
                from urllib.parse import unquote
                items.append((stamp, html.unescape(unquote(href))))
    except (OSError, ValueError):
        pass
    if not items:
        return ActionResult.success("I don't see any files you worked on in that time.")
    items.sort(reverse=True)
    names = []
    for _, p in items:
        base = os.path.basename(p)
        if base not in names:
            names.append(base)
    return ActionResult.success(f"You worked on {len(names)} files, most recently "
                                + ", ".join(names[:5]) + ".", files=[p for _, p in items[:20]])


_ORGANIZE = {"Images": _TYPES["image"], "Documents": _TYPES["document"] + _TYPES["spreadsheet"]
             + _TYPES["presentation"], "Videos": _TYPES["video"], "Music": _TYPES["music"],
             "Archives": _TYPES["zip"], "Installers": (".deb", ".rpm", ".appimage", ".pkg.tar.zst", ".exe", ".msi"),
             "Code": _TYPES["code"]}


def organize_folder(text):
    """Sorts loose files in Downloads (or the named folder) into type folders. Never deletes."""
    from kayra.automation import targets
    low = (text or "").lower()
    named = re.search(r"(downloads|desktop|documents|pictures)", low)
    folder = targets.resolve_path(named.group(1) if named else "downloads", kind="folder")
    if not folder.ok:
        return ActionResult.not_found("I couldn't find that folder.")
    root = folder.target
    moved = 0
    for entry in os.scandir(root):
        if not entry.is_file() or entry.name.startswith(".") or entry.name.endswith(
                (".crdownload", ".part", ".download")):
            continue
        bucket = next((b for b, exts in _ORGANIZE.items() if entry.name.lower().endswith(exts)), None)
        if bucket is None:
            continue
        destination = os.path.join(root, bucket)
        os.makedirs(destination, exist_ok=True)
        target = os.path.join(destination, entry.name)
        if os.path.exists(target):
            stem, ext = os.path.splitext(entry.name)
            target = os.path.join(destination, f"{stem} ({int(time.time())}){ext}")
        try:
            shutil.move(entry.path, target)
            moved += 1
        except OSError:
            continue
    return ActionResult.success(f"Sorted {moved} file{'s' if moved != 1 else ''} in "
                                f"{os.path.basename(root)} into folders by type." if moved
                                else f"{os.path.basename(root)} is already tidy.")


def duplicates(text=""):
    from kayra.automation import targets
    named = re.search(r"(downloads|desktop|documents|pictures)", (text or "").lower())
    folder = targets.resolve_path(named.group(1) if named else "downloads", kind="folder")
    root = folder.target if folder.ok else os.path.expanduser("~/Downloads")
    by_size = {}
    for path in _walk(root, max_files=20000):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > 4096:
            by_size.setdefault(size, []).append(path)
    groups, wasted = [], 0
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for path in paths:
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read(1 << 20)).hexdigest()
            except OSError:
                continue
            by_hash.setdefault(digest, []).append(path)
        for same in by_hash.values():
            if len(same) > 1:
                groups.append(same)
                wasted += size * (len(same) - 1)
    if not groups:
        return ActionResult.success(f"No duplicate files in {os.path.basename(root)}.")
    sample = ", ".join(os.path.basename(g[0]) for g in groups[:3])
    return ActionResult.success(f"{len(groups)} sets of duplicates wasting {wasted / 1024 ** 2:.0f} "
                                f"megabytes, like {sample}. I haven't deleted anything.",
                                groups=groups[:20])


def space_usage(text=""):
    L = _L()
    home = os.path.expanduser("~")
    sizes = []
    for entry in os.scandir(home):
        if entry.is_dir(follow_symlinks=False):
            code, out = L._run("du", "-sb", entry.path, timeout=20.0)
            if code == 0 and out.split():
                sizes.append((int(out.split()[0]), entry.name))
    if not sizes:
        return ActionResult.failure("I couldn't measure the folders.")
    sizes.sort(reverse=True)
    parts = [f"{name} {size / 1024 ** 3:.1f} gigabytes" for size, name in sizes[:4]]
    return ActionResult.success("Biggest folders in your home: " + ", ".join(parts) + ".")


def _read_document(path):
    L = _L()
    low = path.lower()
    if low.endswith(".pdf") and L._have("pdftotext"):
        code, text = L._run("pdftotext", "-l", "30", path, "-", timeout=30.0)
        return text if code == 0 else ""
    if low.endswith((".docx", ".odt", ".pptx", ".xlsx")):
        import zipfile
        try:
            with zipfile.ZipFile(path) as archive:
                name = next((n for n in archive.namelist() if n in (
                    "word/document.xml", "content.xml")), None)
                xml = archive.read(name).decode("utf-8", "replace") if name else ""
            return re.sub(r"<[^>]+>", " ", xml)
        except (OSError, zipfile.BadZipFile, KeyError):
            return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(200000)
    except OSError:
        return ""


def summarize_file(text):
    found = find_files(re.sub(r"^(summari[sz]e|what does|read)\s+", "", text or "", flags=re.I))
    if not found.ok:
        return found
    path = found.detail["path"]
    content = " ".join(_read_document(path).split())
    if len(content) < 40:
        return ActionResult.not_found(f"I can't read any text in {os.path.basename(path)}.")
    question = re.search(r"(?:about|say about|regarding) (.+)$", text or "", re.I)
    spoken = ai.summarize(
        "You are Kayra. Summarize this document for the user in 3 to 5 spoken sentences"
        + (f", focusing on: {question.group(1)}" if question else "") + ". No markdown.",
        f"File: {os.path.basename(path)}\n\n{content[:14000]}",
        fallback=f"{os.path.basename(path)} begins: {content[:300]}")
    return ActionResult.success(spoken, path=path)


def convert_file(text):
    L = _L()
    low = (text or "").lower()
    found = find_files(re.sub(r"^(convert|compress|resize|turn)\s+|\s+(to|into) \w+$|\s+by \d+.*$", "", low))
    if not found.ok:
        return found
    path = found.detail["path"]
    stem, ext = os.path.splitext(path)
    if "pdf" in low.split(" to ")[-1] or re.search(r"(to|into) pdf", low):
        ok = L._ok("soffice", "--headless", "--convert-to", "pdf", "--outdir",
                   os.path.dirname(path), path, timeout=120.0) if L._have("soffice") else False
        return _ok(ok, f"Saved {os.path.basename(stem)}.pdf next to it.", "I couldn't convert it.")
    if "compress" in low and ext.lower() in _TYPES["video"]:
        out = f"{stem}-small.mp4"
        term = L._terminal_argv(["ffmpeg", "-i", path, "-vcodec", "libx264", "-crf", "28", out])
        return _ok(bool(term) and L.RUNNER.spawn(term), "Compressing in a terminal; the result "
                   "will be next to the original.", "I couldn't start ffmpeg.")
    if "resize" in low and ext.lower() in _TYPES["image"]:
        percent = re.search(r"(\d{1,3})\s*%", low)
        out = f"{stem}-resized{ext}"
        ok = L._ok("magick", path, "-resize", f"{percent.group(1) if percent else 50}%", out,
                   timeout=60.0) if L._have("magick") else False
        return _ok(ok, f"Saved {os.path.basename(out)}.", "I couldn't resize it.")
    target = re.search(r"(?:to|into) (\w+)$", low)
    if target and ext.lower() in _TYPES["image"]:
        out = f"{stem}.{target.group(1)}"
        ok = L._ok("magick", path, out, timeout=60.0) if L._have("magick") else False
        return _ok(ok, f"Saved {os.path.basename(out)}.", "I couldn't convert it.")
    return ActionResult.unsupported("I can convert documents to PDF, compress videos, and "
                                    "resize or convert images.")


def backup_folder(text):
    from kayra.automation import targets
    L = _L()
    low = (text or "").lower()
    match = re.search(r"back ?up (?:my |the )?(.+?)(?: to (.+))?$", low)
    if not match:
        return ActionResult.failure("Back up which folder, and where to?")
    source = targets.resolve_path(match.group(1), kind="folder")
    if not source.ok:
        return ActionResult.not_found(f"I couldn't find {match.group(1)}.")
    if match.group(2):
        dest = targets.resolve_path(match.group(2), kind="folder")
        destination = dest.target if dest.ok else None
    else:
        media = [os.path.join(root, d) for root in (f"/run/media/{os.environ.get('USER', '')}",
                                                    f"/media/{os.environ.get('USER', '')}")
                 if os.path.isdir(root) for d in os.listdir(root)]
        destination = media[0] if media else os.path.expanduser("~/Backups")
    if not destination:
        return ActionResult.not_found("I couldn't find where to back it up to.")
    stamp = datetime.date.today().isoformat()
    target = os.path.join(destination, f"{os.path.basename(source.target)}-{stamp}")
    os.makedirs(destination, exist_ok=True)
    ok = L._ok("rsync", "-a", source.target + "/", target, timeout=900.0) if L._have("rsync") else False
    return _ok(ok, f"Backed up {os.path.basename(source.target)} to {target}.", "The backup failed.")
