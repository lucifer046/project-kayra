# ┌────────────────────────────────────────────────────────────────────────┐
# │                          share_picker.py                               │
# │   Share exactly what was named: Kayra's lane into the Hyprland portal  │
# └────────────────────────────────────────────────────────────────────────┘
"""
"Share the LM Studio window" (BROWSER_CONTROL_PLAN §12, M1.1).

On Hyprland the screen-share picker is a program xdg-desktop-portal-hyprland runs
(`screencopy:custom_picker_binary`). Kayra ships one, `extension/kayra-share-picker/
kayra-share-picker`, which answers a one-shot request Kayra writes just before pressing
Meet's share button, and otherwise runs the normal picker unchanged. This module is the Kayra
side of that:

    request_window(window) / request_monitor(name)   write the one-shot request
    clear()                                          withdraw it (the share did not happen)
    status() / install() / uninstall()               the xdph.conf line, asked and reversible

The helper's own functions (paths, parsing, selection) are loaded from the helper file, so the
two sides cannot disagree about where the request lives. Nothing here runs at import.

    PYTHONPATH=src .venv/bin/python -m kayra.automation.share_picker status|install|uninstall
"""

import os
import re
import sys
import json
import time
import shutil
import functools
import importlib.machinery
import importlib.util

from kayra.core.paths import project_root

XDPH_KEY = "custom_picker_binary"


def helper_file():
    return os.path.join(project_root(), "extension", "kayra-share-picker", "kayra-share-picker")


@functools.lru_cache(maxsize=1)
def helper():
    """The helper script as a module (it has no .py suffix, so it is loaded by path)."""
    loader = importlib.machinery.SourceFileLoader("kayra_share_picker_helper", helper_file())
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# ── the one-shot request ──

def _write(payload, environ=None):
    path = helper().request_path(environ)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    payload = dict(payload, created=time.time())
    temp = path + ".part"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temp, path)
    return path


def request_window(window, environ=None):
    """`window` is a `linux.Window`. Identity = its Hyprland address, then class + title."""
    return _write({"kind": "window", "address": window.address,
                   "class": window.initial_class or window.cls, "title": window.title}, environ)


def request_monitor(name, environ=None):
    return _write({"kind": "monitor", "name": str(name)}, environ)


def clear(environ=None):
    try:
        os.remove(helper().request_path(environ))
    except OSError:
        pass


def pending(environ=None):
    return os.path.exists(helper().request_path(environ))


# ── the portal configuration ──

def config_path(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hypr", "xdph.conf")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


_LINE = re.compile(r"^(\s*" + XDPH_KEY + r"\s*=\s*)(.*?)\s*$", re.M)


def configured_picker(environ=None):
    """The picker xdph.conf names now, or ''."""
    m = _LINE.search(_read(config_path(environ)))
    return m.group(2).strip() if m else ""


def installed(environ=None):
    current = configured_picker(environ)
    return bool(current) and os.path.realpath(current.split()[0]) == os.path.realpath(helper_file())


def status(environ=None):
    if installed(environ):
        return "installed"
    if not shutil.which("xdg-desktop-portal-hyprland") and \
            not os.path.exists("/usr/lib/xdg-desktop-portal-hyprland"):
        return "no-portal"
    return "not-installed"


def install(environ=None):
    """
    Points xdph at Kayra's helper and records the picker that was there, so the helper falls
    back to exactly it and `uninstall` puts it back. Backs up xdph.conf first. Returns a
    sentence. The portal reads its config at start, so the change applies after it restarts
    (`restart_portal`), which setup.py offers separately.
    """
    if installed(environ):
        return "Kayra's share picker is already set."
    path = config_path(environ)
    text = _read(path)
    previous = configured_picker(environ)
    record = helper().fallback_record(environ)
    os.makedirs(os.path.dirname(record), exist_ok=True)
    with open(record, "w", encoding="utf-8") as handle:
        handle.write((previous or "") + "\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if text:
        shutil.copy2(path, path + ".kayra-backup")
    line = f"{XDPH_KEY} = {helper_file()}"
    if _LINE.search(text):
        text = _LINE.sub(lambda m: m.group(1) + helper_file(), text, count=1)
    elif re.search(r"^\s*screencopy\s*\{", text, re.M):
        text = re.sub(r"^(\s*screencopy\s*\{[^\n]*\n)", lambda m: m.group(1) + f"    {line}\n",
                      text, count=1, flags=re.M)
    else:
        text = (text.rstrip("\n") + "\n\n" if text else "") + f"screencopy {{\n    {line}\n}}\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return ("Kayra's share picker is set" + (f" (it falls back to {previous})" if previous else "")
            + ". It applies after the screen-sharing portal restarts.")


def uninstall(environ=None):
    """Puts back the picker that was configured before (or removes the line)."""
    if not installed(environ):
        return "Kayra's share picker is not set."
    path = config_path(environ)
    try:
        with open(helper().fallback_record(environ), "r", encoding="utf-8") as handle:
            previous = handle.read().strip()
    except OSError:
        previous = ""
    text = _read(path)
    if previous:
        text = _LINE.sub(lambda m: m.group(1) + previous, text, count=1)
    else:
        text = _LINE.sub("", text, count=1)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return "Kayra's share picker is removed" + (f"; {previous} is back." if previous else ".")


def restart_portal(runner=None):
    """Restarts xdg-desktop-portal-hyprland (a user service) so it reads the new config."""
    import subprocess
    argv = ["systemctl", "--user", "restart", "xdg-desktop-portal-hyprland.service"]
    try:
        if runner is not None:
            return bool(runner(argv))
        return subprocess.run(argv, capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def main(argv):
    verb = (argv[1] if len(argv) > 1 else "status").lower()
    if verb == "install":
        print(install())
        print("Portal restarted." if restart_portal() else
              "Restart the portal (or log out and in) for it to apply.")
    elif verb == "uninstall":
        print(uninstall())
        restart_portal()
    else:
        print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
