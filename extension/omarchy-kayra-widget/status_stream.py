#!/usr/bin/env python3
"""Streams Kayra's status file to the bar widget: one JSON line whenever it changes.

Stdlib only, one stat() a second. A status older than its process (Kayra died without
writing OFFLINE) is reported as OFFLINE, so the bar never lies.
"""
import os
import sys
import json
import time

PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/kayra-{os.getuid()}",
                    "kayra", "status.json")


def read():
    try:
        with open(PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"state": "OFFLINE"}
    pid = data.get("pid")
    if pid and not os.path.exists(f"/proc/{pid}"):
        data["state"] = "OFFLINE"
    return data


last = None
while True:
    data = read()
    line = json.dumps(data, sort_keys=True)
    if line != last:
        print(line, flush=True)
        last = line
    time.sleep(1.0)
