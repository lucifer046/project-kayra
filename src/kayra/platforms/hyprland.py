# ┌────────────────────────────────────────────────────────────────────────┐
# │                       platforms/hyprland.py                            │
# │             Hyprland IPC — one socket round-trip, no subprocess        │
# └────────────────────────────────────────────────────────────────────────┘
"""
A minimal client for Hyprland's request socket (Omarchy's compositor).

WHY THE SOCKET AND NOT `hyprctl`
--------------------------------
`hyprctl activewindow -j` is the same request wrapped in a process spawn. The proactive agent
asks for the active window on every tick and the gesture pointer moves the cursor up to thirty
times a second; a process spawn on either path is the background cost this codebase refuses
everywhere else (`system_profile.live_metrics` is psutil-only for the same reason). A Unix
socket round-trip is ~0.1 ms.

The socket lives at `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`. Requests
are plain text; a leading `j/` asks for JSON. Every call here is bounded by `TIMEOUT_S` and
returns None on any failure — a compositor that does not answer must never wedge a caller.
"""

import os
import json
import socket

TIMEOUT_S = 0.25


def socket_path(environ=None):
    """The request socket, or None when this is not a Hyprland session."""
    environ = os.environ if environ is None else environ
    signature = environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        return None
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    candidate = os.path.join(runtime, "hypr", signature, ".socket.sock")
    if os.path.exists(candidate):
        return candidate
    legacy = os.path.join("/tmp", "hypr", signature, ".socket.sock")   # Hyprland < 0.40
    return legacy if os.path.exists(legacy) else None


def available(environ=None) -> bool:
    return socket_path(environ) is not None


def request(command, path=None):
    """Sends one request and returns the raw reply text, or None."""
    path = path or socket_path()
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.connect(path)
            sock.sendall(command.encode("utf-8"))
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None


def request_json(command, path=None):
    """`request("j/" + command)` decoded, or None when unreachable or not JSON."""
    reply = request("j/" + command, path)
    if not reply:
        return None
    try:
        return json.loads(reply)
    except ValueError:
        return None


def active_window(path=None):
    """(title, app_class) of the focused window, or None. Empty dict = nothing focused."""
    data = request_json("activewindow", path)
    if not isinstance(data, dict) or not data:
        return None
    return (str(data.get("title") or ""), str(data.get("class") or data.get("initialClass")
                                                 or ""))


def monitors(path=None):
    """The monitor list as Hyprland reports it, or an empty list."""
    data = request_json("monitors", path)
    return data if isinstance(data, list) else []


def animations_enabled(path=None):
    """True/False from the compositor's own setting, or None when it cannot be asked."""
    data = request_json("getoption animations:enabled", path)
    if not isinstance(data, dict):
        return None
    for key in ("int", "bool"):
        if key in data:
            return bool(data[key])
    return None


# ── Dispatching ───────────────────────────────────────────────────────────
# Hyprland 0.55+ configures itself in Lua, and `dispatch` on the socket now evaluates a Lua
# expression: `dispatch hl.dsp.window.close({ window = "address:0x..." })`. The classic
# space-separated form (`dispatch closewindow address:0x...`) is answered with an error
# string, NOT a refused connection — so a client that only spoke the old syntax got a reply,
# treated "a reply" as success, and did nothing. That is exactly how `move_cursor` below
# silently stopped moving the gesture pointer on 0.56. Every dispatch now tries the Lua form
# first and falls back to the legacy one, and success means the reply is literally "ok".

_LUA_STATE = {"supported": None}       # learned once per process from the first reply


def _ok(reply):
    return reply is not None and reply.strip().lower() == "ok"


def lua_string(value) -> str:
    """A Lua string literal. Window addresses and workspace names only — but quoted properly."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'


def dispatch(lua=None, legacy=None, path=None) -> bool:
    """
    Runs one dispatcher. `lua` is the expression after `dispatch` on Lua-configured Hyprland
    (e.g. `hl.dsp.focus({ workspace = "3" })`); `legacy` is the classic form
    (e.g. `workspace 3`). True only when Hyprland answered "ok".

    The syntax that worked is remembered, so a legacy compositor pays for the Lua attempt
    once per process rather than on every 30 Hz pointer update.
    """
    if lua and _LUA_STATE["supported"] is not False:
        if _ok(request(f"dispatch {lua}", path)):
            _LUA_STATE["supported"] = True
            return True
    if legacy and _LUA_STATE["supported"] is not True:
        if _ok(request(f"dispatch {legacy}", path)):
            if lua:
                _LUA_STATE["supported"] = False
            return True
    return False


def clients(path=None):
    """Every mapped client window, as Hyprland reports it. [] when unreachable."""
    data = request_json("clients", path)
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def active_client(path=None):
    """The focused client's full record, or None."""
    data = request_json("activewindow", path)
    return data if isinstance(data, dict) and data.get("address") else None


def workspaces(path=None):
    data = request_json("workspaces", path)
    return data if isinstance(data, list) else []


def active_workspace(path=None):
    data = request_json("activeworkspace", path)
    return data if isinstance(data, dict) else None


def move_cursor(x, y, path=None) -> bool:
    """Moves the pointer to layout coordinates. True when Hyprland acknowledged it."""
    return dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})",
                    f"movecursor {int(x)} {int(y)}", path)
