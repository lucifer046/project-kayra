# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/status_file.py                        │
# │      Kayra's state for the Omarchy bar widget (and anything else)       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Publishes what Kayra is doing to `$XDG_RUNTIME_DIR/kayra/status.json`:

    {"state": "LISTENING", "listening": true, "sleeping": false, "pid": 1234, "ts": ...}

Written only when something CHANGES (a runtime-bus event), atomically (tmp + replace), into
the per-user runtime directory — RAM-backed, private to the user, cleared at logout. The
`kayra.status` Omarchy bar widget (extension/omarchy-kayra-widget) reads it. At shutdown the
file says OFFLINE, so the widget never shows a stale "Listening" for a Kayra that is gone.
"""

import os
import json
import time

_STATE = {"state": "IDLE", "listening": True, "sleeping": False}
_SUBSCRIBED = {"fn": None}


def status_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/kayra-{os.getuid()}"
    return os.path.join(runtime, "kayra", "status.json")


def write(**changes):
    _STATE.update(changes)
    payload = dict(_STATE, pid=os.getpid(), ts=round(time.time(), 1))
    path = status_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _on_event(event, payload):
    if event == "state_changed":
        state = payload.get("state")
        write(state=getattr(state, "name", None) or str(state).split(".")[-1].upper())
    elif event == "listening_changed":
        write(listening=bool(payload.get("listening")))
    elif event == "sleeping_changed":
        write(sleeping=bool(payload.get("sleeping")))


def start():
    """Subscribes to the runtime bus and writes the first snapshot. Idempotent."""
    if _SUBSCRIBED["fn"] is not None:
        return True
    try:
        from kayra.core.runtime_state import get_runtime_state
        runtime = get_runtime_state()
        snap = runtime.snapshot()
        state = snap.get("state")
        _STATE.update(state=getattr(state, "name", None) or str(state).split(".")[-1].upper(),
                      listening=bool(snap.get("listening")), sleeping=bool(snap.get("sleeping")))
        runtime.subscribe(_on_event)
        _SUBSCRIBED["fn"] = _on_event
    except Exception:
        pass
    return write()


def stop():
    if _SUBSCRIBED["fn"] is not None:
        try:
            from kayra.core.runtime_state import get_runtime_state
            get_runtime_state().unsubscribe(_SUBSCRIBED["fn"])
        except Exception:
            pass
        _SUBSCRIBED["fn"] = None
    write(state="OFFLINE", listening=False)
