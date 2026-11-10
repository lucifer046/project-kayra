# ┌────────────────────────────────────────────────────────────────────────┐
# │                     automation/browser_bridge.py                       │
# │          Kayra <-> the "Kayra Bridge" Chrome extension (WebSocket)     │
# └────────────────────────────────────────────────────────────────────────┘
"""
How Kayra drives the Chrome you actually browse with.

    extension/kayra-bridge (Chrome, MV3)  ──ws://127.0.0.1:17893/kayra──▶  BridgeServer
          tabs, clicks, typing, page text,            JSON {id, cmd, args}
          history, bookmarks, Meet                    JSON {id, ok, result | error}

WHY AN EXTENSION, not the DevTools protocol: Chrome 136+ refuses remote debugging on the
user's normal profile, and a separately-launched debug Chrome is not the browser the user is
signed into. An extension runs INSIDE the user's Chrome, with their sign-ins, and sees real
tabs — including background ones, which window titles never could.

WHY A WEBSOCKET: an MV3 service worker stays alive while a WebSocket exchanges messages
(Chrome 116+), the extension pings every 20 s, and the standard library is enough for the
server side — no new dependency.

SECURITY
* Listens on 127.0.0.1 only.
* Accepts ONLY the handshake whose `Origin` is this extension's fixed ID (`EXTENSION_ID`,
  derived from the public key in its manifest). A web page cannot forge `Origin`, so no site
  you visit can connect and drive your browser.
* One client at a time; a new connection replaces the old one.
* Nothing destructive is decided here — every request comes through the automation policy.

No import-time side effects: the server starts only when `start()` is called (from
`linux.start_services()`, which `app.bootstrap` calls).
"""

import os
import json
import time
import base64
import socket
import hashlib
import threading

EXTENSION_ID = "gdllbbphfpgfcgmpjjklkkkpebimeeel"
DEFAULT_PORT = 17893
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def extension_dir():
    from kayra.core.paths import project_root
    return os.path.join(project_root(), "extension", "kayra-bridge")


def allowed_origins():
    extra = [o.strip() for o in os.environ.get("KAYRA_BRIDGE_EXTENSION_IDS", "").split(",")
             if o.strip()]
    return {f"chrome-extension://{i}" for i in [EXTENSION_ID] + extra}


class BridgeError(Exception):
    pass


class BridgeServer:
    def __init__(self, host="127.0.0.1", port=None):
        self.host = host
        self.port = int(port or os.environ.get("KAYRA_BRIDGE_PORT", DEFAULT_PORT))
        self._listener = None
        self._thread = None
        self._client = None
        self._client_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._next_id = 1
        self._stop = threading.Event()
        self.version = None
        self.connected_at = None

    # ── lifecycle ──
    def start(self):
        if self._thread is not None:
            return True
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(4)
            listener.settimeout(1.0)
        except OSError:
            return False                  # port busy: another Kayra owns the bridge
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="kayra-browser-bridge",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        for sock in (self._client, self._listener):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)      # wakes a thread blocked in accept()/recv()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._client = None
        self._listener = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._client is not None

    # ── requests ──
    def call(self, cmd, timeout=10.0, **args):
        """Sends one command to the extension and waits for its answer."""
        client = self._client
        if client is None:
            raise BridgeError("not connected")
        with self._client_lock:
            msg_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "reply": None}
            self._pending[msg_id] = slot
        try:
            self._send(client, json.dumps({"id": msg_id, "cmd": cmd, "args": args}))
            if not slot["event"].wait(timeout):
                raise BridgeError(f"{cmd} timed out")
            reply = slot["reply"] or {}
            if not reply.get("ok"):
                raise BridgeError(reply.get("error") or f"{cmd} failed")
            return reply.get("result")
        finally:
            self._pending.pop(msg_id, None)

    # ── socket plumbing ──
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="kayra-bridge-client",
                             daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5.0)
            if not self._handshake(conn):
                conn.close()
                return
            conn.settimeout(None)
            old = self._client
            self._client = conn
            self.connected_at = time.time()
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            while not self._stop.is_set():
                frame = read_frame(conn)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:                       # close
                    break
                if opcode == 0x9:                       # ping -> pong
                    self._send(conn, payload, opcode=0xA)
                    continue
                if opcode != 0x1:
                    continue
                self._on_message(payload.decode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            if self._client is conn:
                self._client = None
            for slot in list(self._pending.values()):
                slot["event"].set()
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn):
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 8192:
            chunk = conn.recv(1024)
            if not chunk:
                return False
            request += chunk
        headers = parse_headers(request.decode("latin-1"))
        if headers.get("origin") not in allowed_origins() or "sec-websocket-key" not in headers:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return False
        accept = base64.b64encode(hashlib.sha1(
            (headers["sec-websocket-key"] + _GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        return True

    def _on_message(self, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if msg.get("type") == "hello":
            self.version = msg.get("version")
            return
        slot = self._pending.get(msg.get("id"))
        if slot is not None:
            slot["reply"] = msg
            slot["event"].set()

    def _send(self, conn, data, opcode=0x1):
        payload = data.encode() if isinstance(data, str) else data
        with self._send_lock:
            conn.sendall(encode_frame(payload, opcode))


def parse_headers(text):
    headers = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return headers


def encode_frame(payload, opcode=0x1):
    """A server->client frame (never masked)."""
    head = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + size.to_bytes(2, "big")
    else:
        head += bytes([127]) + size.to_bytes(8, "big")
    return head + payload


def _recv_exact(conn, count):
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frame(conn):
    """(opcode, payload) for one frame, reassembling continuations. None on EOF."""
    message, first_opcode = b"", None
    while True:
        head = _recv_exact(conn, 2)
        if head is None:
            return None
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, size = head[1] & 0x80, head[1] & 0x7F
        if size == 126:
            raw = _recv_exact(conn, 2)
            size = int.from_bytes(raw, "big") if raw else 0
        elif size == 127:
            raw = _recv_exact(conn, 8)
            size = int.from_bytes(raw, "big") if raw else 0
        if size > 16 * 1024 * 1024:
            return None
        mask = _recv_exact(conn, 4) if masked else b""
        payload = _recv_exact(conn, size) if size else b""
        if payload is None or mask is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode >= 0x8:                    # control frames are never fragmented
            return opcode, payload
        if first_opcode is None:
            first_opcode = opcode
        message += payload
        if fin:
            return first_opcode, message


# ── the process-wide bridge ──
BRIDGE = BridgeServer()


def start():
    return BRIDGE.start()


def connected():
    return BRIDGE.connected


def call(cmd, timeout=10.0, **args):
    return BRIDGE.call(cmd, timeout=timeout, **args)
