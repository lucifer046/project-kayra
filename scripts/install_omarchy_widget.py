#!/usr/bin/env python3
"""
Installs the Kayra status pill into the Omarchy bar.

    python3 scripts/install_omarchy_widget.py            # install + add to the bar
    python3 scripts/install_omarchy_widget.py --remove   # take it out again

What it does, and nothing else:
  1. copies extension/omarchy-kayra-widget to ~/.config/omarchy/plugins/kayra.status,
     pointing the widget at THIS checkout so a click can start Kayra;
  2. adds {"id": "kayra.status"} to the right side of the bar in ~/.config/omarchy/shell.json,
     after writing a timestamped backup of that file next to it;
  3. restarts the Omarchy shell so the bar picks it up.
Stdlib only; safe to run twice.
"""
import os
import sys
import json
import time
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "extension", "omarchy-kayra-widget")
CONFIG = os.path.expanduser("~/.config/omarchy")
TARGET = os.path.join(CONFIG, "plugins", "kayra.status")
SHELL = os.path.join(CONFIG, "shell.json")
ENTRY = {"id": "kayra.status"}


def edit_bar(add):
    if not os.path.exists(SHELL):
        print("No ~/.config/omarchy/shell.json — add the 'kayra.status' widget from Omarchy's bar settings.")
        return
    with open(SHELL, encoding="utf-8") as handle:
        data = json.load(handle)
    right = data.setdefault("bar", {}).setdefault("layout", {}).setdefault("right", [])
    present = any(isinstance(w, dict) and w.get("id") == "kayra.status" for w in right)
    if add == present:
        return
    backup = f"{SHELL}.bak.{int(time.time())}"
    shutil.copy2(SHELL, backup)
    if add:
        right.insert(0, ENTRY)
    else:
        right[:] = [w for w in right if not (isinstance(w, dict) and w.get("id") == "kayra.status")]
    tmp = SHELL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, SHELL)
    print(f"Updated the bar layout (backup: {backup}).")


def main():
    remove = "--remove" in sys.argv
    if remove:
        shutil.rmtree(TARGET, ignore_errors=True)
        edit_bar(False)
    else:
        if not os.path.isdir(CONFIG):
            sys.exit("This is not an Omarchy desktop (~/.config/omarchy is missing).")
        shutil.rmtree(TARGET, ignore_errors=True)
        shutil.copytree(SOURCE, TARGET)
        panel = os.path.join(TARGET, "Panel.qml")
        with open(panel, encoding="utf-8") as handle:
            qml = handle.read()
        qml = qml.replace('(Quickshell.env("HOME") + "/Documents/project-kayra")', json.dumps(ROOT))
        with open(panel, "w", encoding="utf-8") as handle:
            handle.write(qml)
        print(f"Installed the widget to {TARGET}.")
        edit_bar(True)
    if shutil.which("omarchy-restart-shell"):
        subprocess.run(["omarchy-restart-shell"], check=False)
        print("Restarted the Omarchy shell.")


if __name__ == "__main__":
    main()
