# ┌────────────────────────────────────────────────────────────────────────┐
# │                            test_assistant_automation.py                              │
# │   Diagnostics, Chrome bridge, screen AI, workflows, system controls    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_assistant_automation.py — round 3 (assistant features) of the Linux automation layer.

    .venv/bin/python tests/test_assistant_automation.py

Tier 1, hardware-free. Fakes for everything that touches the machine or the network:
a fake compositor and a recording process runner (from test_linux_automation), a fake
Chrome extension for the bridge, fake text/vision models, and temporary folders for every
file Kayra writes. The ONE real socket is the bridge test, which binds 127.0.0.1 on a free
port to prove the handshake refuses any Origin but the extension's.

SAFE: nothing is launched, clicked, typed, sent, installed or deleted outside a temp folder.
"""

import os
import sys
import json
import time
import base64
import socket
import hashlib
import tempfile
import datetime
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, describe_host, run  # noqa: E402
from test_linux_automation import (FakeHyprland, RecordingRunner, Rig, client,  # noqa: E402
                                   ALL_TOOLS, FakePointer)

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.automation import (common, linux, policy, ai, diagnostics, web_macros,  # noqa: E402
                              browser_bridge, screen_ai, workflows, linux_system)
from kayra.automation.policy import Status, Risk  # noqa: E402

check = Checker("assistant automation")
n = common.normalize_command


class FakeBridge:
    """The Chrome extension, as a table of replies."""

    def __init__(self, connected=True, tabs=None, controls=None, title="Inbox (3) - Gmail"):
        self._connected = connected
        self.calls = []
        self.tabs = tabs or [{"id": 1, "title": "Inbox (3) - Gmail", "url": "https://mail.google.com/mail/u/0/", "active": True},
                             {"id": 2, "title": "Lo-fi - YouTube", "url": "https://www.youtube.com/watch?v=x"},
                             {"id": 3, "title": "Meet - abc", "url": "https://meet.google.com/abc-defg-hij"}]
        self.controls = controls or [{"name": "Turn off microphone"}, {"name": "Share screen"}]
        self.title = title
        self.clickable = {"turn off microphone", "share screen", "send", "join now", "save",
                          "rahul", "sign in", "more", "all mails", "reply", "archive", "delete"}
        self.old = False

    def connected(self):
        return self._connected

    def call(self, cmd, timeout=10.0, **args):
        self.calls.append((cmd, args))
        if cmd == "tabs.list":
            return self.tabs
        if cmd == "tabs.active":
            return {"title": self.title}
        if cmd == "tabs.activate":
            hits = [t for t in self.tabs if args.get("match", "") in (t["title"] + t["url"]).lower()]
            if not hits:
                raise browser_bridge.BridgeError("no tab")
            return hits[0]
        if cmd == "tabs.close":
            if args.get("exceptActive"):
                return {"closed": len(self.tabs) - 1}
            hits = [t for t in self.tabs if args.get("match", "") in (t["title"] + t["url"]).lower()]
            if len(hits) > 1 and not args.get("all"):
                return {"closed": 0, "ambiguous": hits}
            return {"closed": len(hits)}
        if cmd == "page.click":
            label = args["text"].lower()
            return {"clicked": label in self.clickable, "label": args["text"], "matches": 1}
        if cmd == "page.type":
            if "password" in args.get("field", ""):
                return {"typed": False, "reason": "password or payment field"}
            return {"typed": True, "field": args.get("field")}
        if cmd == "page.text":
            return {"title": "Article", "url": "https://x", "text": "Kayra is an assistant. " * 30}
        if cmd == "page.controls":
            return self.controls
        if cmd == "gmail.open":
            if self.old:
                raise browser_bridge.BridgeError("unknown command gmail.open")
            if args.get("query") == "nobody":
                return {"opened": False, "reason": "no email matches nobody"}
            return {"opened": True, "from": "Bank", "subject": "Your statement", "date": "Mon"}
        if cmd == "history.search":
            return [{"title": "Attention is all you need", "url": "https://arxiv.org/abs/1706.03762"}]
        if cmd in ("tabs.open", "tabs.navigate"):
            return {"id": 9, "title": "New", "url": args.get("url")}
        return {}

    def cmds(self):
        return [c for c, _ in self.calls]


class WebRig(Rig):
    def __init__(self, bridge=None, **kw):
        super().__init__(**kw)
        self.bridge = bridge or FakeBridge()

    def __enter__(self):
        super().__enter__()
        self._web = (web_macros.BRIDGE, web_macros.SLEEP)
        web_macros.BRIDGE, web_macros.SLEEP = self.bridge, (lambda s: None)
        return self

    def __exit__(self, *exc):
        web_macros.BRIDGE, web_macros.SLEEP = self._web
        return super().__exit__(*exc)


class Models:
    """Replaces every model call; records what was asked."""

    def __init__(self, text="", vision=None):
        self.text, self.vision, self.asked = text, vision, []

    def __enter__(self):
        self._saved = (ai.TEXT_MODEL, ai.VISION_MODEL)
        ai.TEXT_MODEL = lambda messages: (self.asked.append(messages), self.text)[1]
        ai.VISION_MODEL = (lambda q, img, mime: self.vision) if self.vision is not None else \
            (lambda q, img, mime: None)
        return self

    def __exit__(self, *exc):
        ai.TEXT_MODEL, ai.VISION_MODEL = self._saved
        return False


class TempData:
    """Points workflows' data files and diagnostics' reports at a temp folder."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="kayra-assistant-")
        self._data = workflows._data
        workflows._data = lambda name: os.path.join(self.dir, name)
        import kayra.core.paths as paths
        self._reports = paths.reports_dir
        paths.reports_dir = lambda: self.dir
        return self

    def __exit__(self, *exc):
        import shutil
        import kayra.core.paths as paths
        workflows._data = self._data
        paths.reports_dir = self._reports
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     1. PARSING EVERY NEW PHRASE                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_parsing():
    check.section("[1] Every new phrasing becomes one structured action")
    table = {
        "browser list tabs": ("browser.tabs_list", None),
        "browser switch to gmail tab": ("browser.tab_switch", "gmail"),
        "browser close the youtube tab": ("browser.tab_close", "youtube"),
        "browser close other tabs": ("browser.tabs_close_others", None),
        "browser click on the second reply button": ("browser.page_click", "reply"),
        "browser type hello world into search": ("browser.page_type", "search"),
        "browser search for shoes": ("browser.search_here", "shoes"),     # Part 3, G2
        "browser scroll until you see pricing": ("browser.page_scroll", "pricing"),
        "browser summarize this page": ("browser.page_summarize", None),
        "browser read this page": ("browser.page_read", None),
        "browser open the article i read yesterday about transformers": ("browser.history_open", None),
        "browser set up": ("browser.bridge_setup", None),
        "meet join abc-defg-hij": ("meet.join", "abc-defg-hij"),
        "meet mic off": ("meet.mic", None), "meet unmute": ("meet.mic", None),
        "meet share entire screen": ("meet.share", "monitor"),
        "meet stop sharing": ("meet.stop_share", None), "meet leave": ("meet.leave", None),
        "email check": ("email.check", None), "email send": ("email.send", None),
        "email open latest": ("email.open", None),
        "email draft to a@b.com about lunch saying see you at 1": ("email.draft", "a@b.com"),
        "calendar today": ("calendar.agenda", "today"),
        "calendar add standup tomorrow at 10am": ("calendar.add", None),
        "message rahul: running late": ("message.send", "rahul"),
        "diagnose computer": ("diagnose.run", None), "diagnose fix": ("diagnose.fix", None),
        "screen describe": ("screen.describe", None), "screen explain this error": ("screen.explain", None),
        "screen read": ("screen.read", None), "screen copy text": ("screen.copy_text", None),
        "screen record": ("screen.record", None), "screen stop recording": ("screen.record_stop", None),
        "scene work": ("scene.run", "work"), "scene save this layout as study": ("scene.save", "study"),
        "scene list": ("scene.list", None), "plan get me ready for my presentation": ("plan.run", None),
        "trigger when the download finishes shut down the computer": ("trigger.add", None),
        "trigger list": ("trigger.list", None), "trigger cancel download": ("trigger.cancel", None),
        "files organize downloads": ("files.organize", None), "files space": ("files.space", None),
        "files recent today": ("files.recent", None), "files summarize resume.pdf": ("files.summarize", None),
        "git status": ("dev.git_status", None), "git commit fix the bug": ("dev.git_commit", None),
        "click on the second save": ("mouse.click_text", None),
        "system switch to headphones": ("system.audio_output", None),
        "system mute my mic": ("system.mic", None), "system connect my earbuds": ("system.bt_device", None),
        "system connect to wifi home": ("system.wifi_connect", None),
        "system check for updates": ("system.updates", None),
        "system update my system": ("system.update_system", None),
        "system install vlc": ("system.install", None), "system uninstall vlc": ("system.uninstall", None),
        "system what's using my cpu": ("system.top_processes", None),
        "system close the app using the most memory": ("system.close_hog", None),
        "system set refresh rate to 60": ("system.display_set", None),
        "system theme catppuccin": ("system.theme", None), "system next wallpaper": ("system.wallpaper", None),
        "system read my notifications": ("system.notifications", None),
        "system morning briefing": ("system.briefing", None),
        "system what did you just do": ("system.recall", None),
        "system what's open": ("system.list_windows", None), "system where is chrome": ("system.where", None),
        "system chrome on the left and vs code on the right": ("system.layout", None),
        "system move all browsers to workspace 2": ("system.move_all", None),
        "system remember my setup": ("system.session_save", None),
        "system restore my last session": ("system.session_restore", None),
        "system set a sleep timer for 30 minutes": ("system.music_sleep", None),
        "system run my tests": ("system.run_tests", None),
        "system restart the docker container db": ("system.docker", None),
    }
    for token, (key, target) in table.items():
        action = n(token)
        ok = action is not None and action.key == key and (target is None or action.target == target)
        check(f"'{token}' -> {key}", ok, action)
    check("'browser click on the second reply button' keeps nth=2",
          n("browser click on the second reply button").parameters.get("nth") == 2)
    check("none of the new 'system' phrases reaches the power gate",
          all(n(t).key != "system.power_ambiguous" for t in table if t.startswith("system ")))
    check("'system shut down the computer in 30 minutes' is still the confirmed power action",
          n("system shut down the computer in 30 minutes").key == "system.shutdown")
    seconds, label = common.parse_clock_reminder("9:00pm call mom", datetime.datetime(2026, 1, 1, 8))
    check("'reminder 9:00pm call mom' is due at 9 pm with the label kept",
          seconds == 13 * 3600 and label == "call mom", (seconds, label))
    seconds, _ = common.parse_clock_reminder("7 am wake up", datetime.datetime(2026, 1, 1, 8))
    check("a time already past today means tomorrow", seconds == 23 * 3600, seconds)
    check("'at 3' means 3 pm", common.parse_clock_reminder("at 3 stretch",
                                                           datetime.datetime(2026, 1, 1, 8))[0] == 7 * 3600)

    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    funcs = CentralizedLLMEngine().funcs
    for token in ("browser", "meet", "email", "calendar", "message", "diagnose", "screen",
                  "scene", "plan", "trigger", "files", "git", "click on"):
        check(f"'{token}' is in the DMM acceptance gate", token in funcs)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     2. SAFETY OF THE NEW POWERS                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_safety():
    check.section("[2] Sending, spending, sharing and removing all ask first")
    for token in ("email send", "message rahul: hi", "calendar add lunch tomorrow at 1pm",
                  "meet share entire screen", "browser close other tabs", "diagnose fix",
                  "git commit wip", "system update my system", "system install vlc",
                  "system uninstall vlc", "system forget the wifi home",
                  "system close everything except chrome", "system close the app using the most memory",
                  "browser click on delete account", "system restart the docker container db",
                  "files organize downloads", "files back up documents"):
        verdict = policy.classify_action(n(token))[0]
        check(f"CONFIRM {token}", verdict == Risk.CONFIRM, verdict)
    for token in ("browser list tabs", "email check", "calendar today", "diagnose computer",
                  "screen describe", "system morning briefing", "system docker status",
                  "browser click on sign in"):
        check(f"ALLOW {token}", policy.classify_action(n(token))[0] == Risk.ALLOW)

    bridge = FakeBridge()
    with WebRig(bridge) as rig:
        for token in ("email send", "message rahul: hi", "meet share entire screen",
                      "system install vlc", "diagnose fix"):
            result = rig.do(token)
            check(f"'{token}' waits for a yes", result.status == Status.NEEDS_CONFIRMATION)
            linux.CONFIRMATIONS.cancel()
        check("nothing reached Chrome or the machine before a yes",
              bridge.calls == [] and rig.runner.calls == [], (bridge.calls, rig.runner.calls))
        rig.do("email send")
        handled, spoken = linux.resolve_confirmation("yes")
        check("after 'yes', Send is clicked", ("page.click", {"text": "Send", "nth": 1}) in bridge.calls,
              spoken)

    with WebRig() as rig:
        result = rig.do("system uninstall linux")
        handled, spoken = linux.resolve_confirmation("yes")
        check("removing a core package is refused even after yes",
              "core system" in spoken and rig.runner.spawned == [], spoken)
        rig.runner.spawned.clear()
        rig.do("system install vlc")
        linux.resolve_confirmation("yes")
        check("installs run in a visible terminal (you type the password, not Kayra)",
              rig.runner.spawned and rig.runner.spawned[0][0] == "xdg-terminal-exec"
              and "vlc" in rig.runner.spawned[0], rig.runner.spawned)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. THE BRIDGE SERVER (real socket)                 │
# └────────────────────────────────────────────────────────────────────────┘

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ws_connect(port, origin):
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET /kayra HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nOrigin: {origin}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = sock.recv(1024)
        if not chunk:
            break
        reply += chunk
    return sock, reply.decode("latin-1"), key


def _client_frame(text):
    payload = text.encode()
    mask = os.urandom(4)
    head = bytes([0x81, 0x80 | len(payload)]) if len(payload) < 126 else \
        bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big")
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def section_bridge():
    check.section("[3] The Chrome bridge only talks to its own extension")
    port = _free_port()
    server = browser_bridge.BridgeServer(port=port)
    check("the server starts on 127.0.0.1", server.start())
    try:
        sock, reply, _ = _ws_connect(port, "https://evil.example.com")
        check("a web page's Origin is refused (403)", reply.startswith("HTTP/1.1 403"), reply[:40])
        sock.close()
        sock, reply, _ = _ws_connect(port, "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        check("another extension's Origin is refused", reply.startswith("HTTP/1.1 403"))
        sock.close()
        sock, reply, key = _ws_connect(port, f"chrome-extension://{browser_bridge.EXTENSION_ID}")
        expected = base64.b64encode(hashlib.sha1((key + browser_bridge._GUID).encode()).digest()).decode()
        check("Kayra's own extension is accepted (101 + correct accept key)",
              reply.startswith("HTTP/1.1 101") and expected in reply, reply[:60])
        sock.settimeout(5)
        sock.sendall(_client_frame(json.dumps({"type": "hello", "version": "1.0.0"})))
        deadline = time.time() + 2
        while not server.connected and time.time() < deadline:
            time.sleep(0.02)
        check("the server reports connected", server.connected)

        def answer():
            opcode, payload = browser_bridge.read_frame(sock)
            msg = json.loads(payload)
            sock.sendall(_client_frame(json.dumps({"id": msg["id"], "ok": True,
                                                   "result": {"echo": msg["cmd"], "args": msg["args"]}})))
        threading.Thread(target=answer, daemon=True).start()
        result = server.call("tabs.list", timeout=3, x=1)
        check("a command round-trips through real WebSocket frames",
              result == {"echo": "tabs.list", "args": {"x": 1}}, result)
        check("the extension version from 'hello' is recorded", server.version == "1.0.0")

        def fail():
            opcode, payload = browser_bridge.read_frame(sock)
            msg = json.loads(payload)
            sock.sendall(_client_frame(json.dumps({"id": msg["id"], "ok": False, "error": "nope"})))
        threading.Thread(target=fail, daemon=True).start()
        try:
            server.call("page.click", timeout=3)
            check("an extension error raises BridgeError", False)
        except browser_bridge.BridgeError as e:
            check("an extension error raises BridgeError", "nope" in str(e))
        sock.close()
        deadline = time.time() + 2
        while server.connected and time.time() < deadline:
            time.sleep(0.02)
        check("a closed extension is noticed", not server.connected)
        try:
            server.call("tabs.list", timeout=1)
            check("calling with no extension fails fast", False)
        except browser_bridge.BridgeError:
            check("calling with no extension fails fast", True)
    finally:
        server.stop()
    with socket.socket() as probe:
        # SO_REUSEADDR: closed client connections linger in TIME_WAIT; only a LISTENING
        # socket would still block the bind.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            freed = True
        except OSError:
            freed = False
    check("stopping the bridge releases its port", freed)
    check("and it can start again", server.start())
    server.stop()

    big = browser_bridge.encode_frame(b"x" * 70000)
    check("frames over 64 KB use the 8-byte length", big[1] == 127 and len(big) == 70010)
    manifest = json.load(open(os.path.join(browser_bridge.extension_dir(), "manifest.json")))
    der = base64.b64decode(manifest["key"])
    derived = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
    check("the manifest key really produces EXTENSION_ID", derived == browser_bridge.EXTENSION_ID,
          derived)
    check("the extension never asks for cookies, debugger or native messaging access "
          "(downloads was approved for 1.6.0, plan decision D5)",
          not ({"cookies", "debugger", "nativeMessaging"} & set(manifest["permissions"])))
    js = open(os.path.join(browser_bridge.extension_dir(), "background.js")).read()
    check("the extension only connects to 127.0.0.1", "ws://127.0.0.1" in js and "fetch(" not in js)
    check("the extension refuses password fields itself", "password" in js and "never types" in js)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       4. INSIDE CHROME                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_web():
    check.section("[4] Tabs, pages, Gmail, Calendar, WhatsApp and Meet through the bridge")
    with WebRig(FakeBridge(connected=False),
                runner=RecordingRunner(ALL_TOOLS + ("google-chrome-stable",))) as rig:
        result = rig.do("browser list tabs")
        check("without the extension Kayra says how to connect it",
              result.status == Status.UNSUPPORTED and "browser set up" in result.message)
        result = rig.do("browser set up")
        check("'browser set up' opens chrome://extensions and the extension folder",
              any("chrome://extensions/" in a for a in rig.runner.spawned) and
              any(a[0] == "xdg-open" for a in rig.runner.spawned), rig.runner.spawned)
        result = rig.do("email draft to a@b.com about lunch saying see you at 1")
        check("a draft still opens by URL without the extension",
              any("view=cm" in a[-1] and "a%40b.com" in a[-1] for a in rig.runner.spawned))

    with WebRig() as rig:
        bridge = rig.bridge
        result = rig.do("browser list tabs")
        check("tabs are listed by title", result.ok and "3 tabs" in result.message
              and "Lo-fi" in result.message, result.message)
        rig.do("browser switch to gmail tab")
        check("'gmail tab' activates mail.google.com", ("tabs.activate", {"match": "mail.google.com"}) in bridge.calls)
        bridge.tabs.append({"id": 4, "title": "Music - YouTube", "url": "https://youtube.com/m"})
        result = rig.do("browser close the youtube tab")
        check("two YouTube tabs -> ask which", result.status == Status.AMBIGUOUS, result.message)
        result = rig.do("browser click on sign in")
        check("a page click names what it clicked", result.ok and "sign in" in result.message.lower())
        result = rig.do("browser click on the second banana")
        check("a missing button is said, not faked", result.status == Status.NOT_FOUND)
        check("the ordinal reaches the extension", bridge.calls[-1][1].get("nth") == 2)
        result = rig.do("browser fill password with hunter2")
        check("a password field is refused", result.status == Status.BLOCKED)
        with Models(text="It explains Kayra."):
            result = rig.do("browser summarize this page")
        check("a page summary comes from the model", result.message == "It explains Kayra.")
        with Models(text=""):
            result = rig.do("browser summarize this page")
        check("no model -> an honest fallback, not silence", "couldn't reach a model" in result.message)
        result = rig.do("browser open the article i read yesterday about transformers")
        check("history: the best match is opened",
              any(c == "tabs.open" and "1706.03762" in a.get("url", "") for c, a in bridge.calls))
        result = rig.do("email check")
        check("unread count comes from Gmail's title", "3 unread" in result.message, result.message)
        check("Gmail is activated, not opened twice",
              ("tabs.activate", {"match": "mail.google.com"}) in bridge.calls)

    when = web_macros.parse_when("standup tomorrow at 10am", datetime.datetime(2026, 1, 5, 9))
    check("'tomorrow at 10am'", when == datetime.datetime(2026, 1, 6, 10, 0), when)
    when = web_macros.parse_when("review on friday at 3", datetime.datetime(2026, 1, 5, 9))
    check("'friday at 3' is the coming Friday, 3 pm", when == datetime.datetime(2026, 1, 9, 15), when)
    check("no time -> None", web_macros.parse_when("sometime soon") is None)

    with WebRig() as rig:
        rig.do("calendar add standup tomorrow at 10am")
        linux.resolve_confirmation("yes")
        opened = [a.get("url") for c, a in rig.bridge.calls if c == "tabs.open"]
        check("a calendar event is a prefilled template, then Save",
              opened and "action=TEMPLATE" in opened[0] and "text=standup" in opened[0]
              and ("page.click", {"text": "Save", "nth": 1}) in rig.bridge.calls, opened)

    meet = FakeBridge(controls=[{"name": "Turn off microphone"}, {"name": "Stop presenting"}])
    hypr = FakeHyprland([client("0xc", "google-chrome", "Meet - abc", focus=0),
                         client("0xp", "hyprland-preview-share-picker", "Share", focus=1)],
                        active="0xc")
    with WebRig(meet, hypr=hypr) as rig:
        result = rig.do("meet mic off")
        check("'meet mic off' clicks Meet's own 'Turn off microphone'",
              ("page.click", {"text": "Turn off microphone", "nth": 1}) in meet.calls, result.message)
        result = rig.do("meet share entire screen")
        linux.resolve_confirmation("yes")
        # This fake is ALREADY presenting ("Stop presenting" is on screen), so nothing is
        # clicked twice. The full share flow is test_browser_control.py section [12].
        check("sharing while already presenting says so instead of opening a second picker",
              ("page.click", {"text": "Share screen", "nth": 1}) not in meet.calls)

    whatsapp = FakeBridge(tabs=[{"id": 5, "title": "WhatsApp", "url": "https://web.whatsapp.com/"}])
    with WebRig(whatsapp) as rig:
        rig.do("message rahul: running late")
        linux.resolve_confirmation("yes")
        typed = [a for c, a in whatsapp.calls if c == "page.type"]
        check("WhatsApp: search the contact, open the chat, type and send",
              typed and typed[0]["text"] == "rahul" and typed[-1]["text"] == "running late"
              and typed[-1]["submit"], typed)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         5. DIAGNOSTICS                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_gmail_and_routing():
    check.section("[4b] Gmail: open one email, read it, folders, actions; clicks go into Chrome")
    for token, (key, which, nth, query) in {
            "email open latest": ("email.open", "latest", 1, ""),
            "email open second": ("email.open", "nth", 2, ""),
            "email open unread": ("email.open", "unread", 1, ""),
            "email open from amazon": ("email.open", "first", 1, "amazon"),
            "open the first email": ("email.open", "first", 1, ""),
            "open my latest email": ("email.open", "latest", 1, ""),
            "email read latest": ("email.read", "latest", 1, ""),
            "email read my latest email": ("email.read", "latest", 1, ""),
            "email read from ana": ("email.read", "first", 1, "ana")}.items():
        action = n(token)
        ok = action and action.key == key and action.parameters.get("which") == which \
            and action.parameters.get("nth") == nth and action.parameters.get("query") == query
        check(f"'{token}' -> {key} {which} {nth} {query!r}", ok, action)
    for token, key in (("email read my emails", "email.read"), ("email check", "email.check"),
                       ("email open sent", "email.folder"), ("email go to my sent mails", "email.folder"),
                       ("email open all mail", "email.folder"), ("email reply", "email.reply"),
                       ("email archive", "email.archive"), ("email delete", "email.delete"),
                       ("email back", "email.back")):
        action = n(token)
        check(f"'{token}' -> {key}", action and action.key == key, action)
    check("'email read my emails' summarizes the INBOX, not one email",
          not n("email read my emails").parameters.get("which"))
    check("deleting an email asks first", policy.classify_action(n("email delete"))[0] == Risk.CONFIRM)

    with WebRig() as rig:
        result = rig.do("email open latest")
        check("'open the latest email' opens it and says what it is",
              result.ok and "Bank" in result.message and "Your statement" in result.message, result.message)
        check("it asked the extension for the newest by date",
              ("gmail.open", {"which": "latest", "nth": 1, "query": ""}) in rig.bridge.calls)
        with Models(text="Your bank statement for October is ready."):
            result = rig.do("email read latest")
        check("'read my latest email' opens it and reads THAT email",
              "statement for October" in result.message and
              any(c == "gmail.open" for c, _ in rig.bridge.calls), result.message)
        result = rig.do("email open from nobody")
        check("no matching email is said, not faked", result.status == Status.NOT_FOUND)
        result = rig.do("email open sent")
        check("folders are navigated by URL, no click needed",
              any(c == "tabs.navigate" and a.get("url", "").endswith("#sent") for c, a in rig.bridge.calls))
        rig.do("email open all mail")
        check("'all mail' is #all", any(a.get("url", "").endswith("#all") for c, a in rig.bridge.calls))
        result = rig.do("email reply")
        check("'reply' clicks Gmail's Reply", result.ok and ("page.click", {"text": "Reply", "nth": 1})
              in rig.bridge.calls, result.message)

    old = FakeBridge()
    old.old = True
    with WebRig(old) as rig:
        result = rig.do("email open latest")
        check("an outdated extension says exactly how to update it",
              "reload" in result.message and "chrome://extensions" in result.message, result.message)

    chrome = FakeHyprland([client("0xc", "google-chrome", "Inbox - Gmail", focus=0)], active="0xc")
    with WebRig(hypr=chrome) as rig:
        result = rig.do("click on more")
        check("'click on more' with Chrome in front clicks INSIDE the page",
              result.ok and ("page.click", {"text": "more", "nth": 1}) in rig.bridge.calls, result.message)
        rig.do("click on all mails button")
        check("trailing 'button' is dropped", ("page.click", {"text": "all mails", "nth": 1}) in rig.bridge.calls)
        check("no screenshot or OCR was used", not any(a[0] in ("grim", "tesseract") for a in rig.runner.calls))
        rig.do("scroll down")
        check("'scroll down' scrolls the page", ("page.scroll", {"direction": "down", "until": ""}) in rig.bridge.calls)
        result = rig.do("click on nonexistent thing")
        check("not on the page and no uinput -> says it can't find it in the page",
              result.status == Status.NOT_FOUND and "page" in result.message, result.message)
    editor = FakeHyprland([client("0xf", "foot", "shell", focus=0)], active="0xf")
    with WebRig(hypr=editor, runner=GrimRunnerLite()) as rig:
        rig.do("click on more")
        check("with a terminal in front the screen clicker is used, not Chrome",
              not any(c == "page.click" for c, _ in rig.bridge.calls))


class GrimRunnerLite(RecordingRunner):
    def __init__(self):
        super().__init__(ALL_TOOLS + ("tesseract",))


def section_diagnostics():
    check.section("[5] Diagnose: probes -> findings -> spoken summary -> report -> fixes")
    for text, scope in (("why is my laptop slow", "slow"), ("why is the fan so loud", "heat"),
                        ("what's draining my battery", "battery"), ("why is the internet slow", "network"),
                        ("is my computer secure", "security"), ("what crashed", "crashes"),
                        ("my computer", "full"), ("disk space", "disk")):
        check(f"'{text}' -> {scope}", diagnostics.scope_for(text) == scope, diagnostics.scope_for(text))

    applied = []
    fake_findings = [
        diagnostics.Finding("disk", diagnostics.WARN, "Trash holds 5 GB",
                            diagnostics.Fix("empty the Trash", lambda: applied.append("trash") or "Trash emptied.")),
        diagnostics.Finding("heat", diagnostics.BAD, "CPU at 97°C"),
        diagnostics.Finding("cpu", diagnostics.OK, "CPU at 5%"),
    ]
    saved = dict(diagnostics.SCOPES)
    diagnostics.SCOPES["full"] = (lambda: list(fake_findings),)
    try:
        with TempData() as data, Models(text="Your processor is overheating, and the Trash is "
                                             "large. I can fix it."), Rig() as rig:
            result = rig.do("diagnose computer")
            check("the spoken result is the model's summary", "overheating" in result.message,
                  result.message)
            check("findings were sorted worst first", diagnostics.LAST["findings"][0].level == "bad")
            report = result.detail.get("report")
            check("a Markdown report is written", report and os.path.exists(report))
            body = open(report).read() if report else ""
            check("the report has every finding and the fix", "CPU at 97°C" in body and "empty the Trash" in body)
            check("progress was announced before the work", any("diagnos" in a.lower()
                                                                for a in common.ANNOUNCED[-3:]))
            result = rig.do("diagnose fix")
            check("'fix it' asks first", result.status == Status.NEEDS_CONFIRMATION)
            check("nothing fixed before yes", applied == [])
            handled, spoken = linux.resolve_confirmation("yes")
            check("after yes every offered fix runs", applied == ["trash"] and "Trash emptied" in spoken, spoken)
        with Models(text=""):
            spoken = diagnostics.summarize(fake_findings, "full")
        check("no model -> a deterministic sentence from the same findings",
              "1 problem" in spoken and "97" in spoken, spoken)
    finally:
        diagnostics.SCOPES.clear()
        diagnostics.SCOPES.update(saved)

    class FakePsutil:
        class _P:
            def __init__(self, mount):
                self.mountpoint, self.fstype = mount, "ext4"

        class _U:
            percent = 95.0

        def disk_partitions(self, all=False):
            return [self._P("/")]

        def disk_usage(self, path):
            return self._U()

        def sensors_temperatures(self):
            return {}

    saved_psutil, saved_gw = diagnostics.psutil, diagnostics.default_gateway
    diagnostics.psutil, diagnostics.default_gateway = FakePsutil(), (lambda: "192.168.1.1")
    diagnostics._WATCH.update(spoken={}, services_at=time.time())
    try:
        first = diagnostics.health_alerts(now=1000.0)
        second = diagnostics.health_alerts(now=1060.0)
        later = diagnostics.health_alerts(now=1000.0 + diagnostics.ALERT_REPEAT_S + 1)
    finally:
        diagnostics.psutil, diagnostics.default_gateway = saved_psutil, saved_gw
    check("a full disk is alerted", any("95 percent full" in a for a in first), first)
    check("and not again a minute later", second == [], second)
    check("but again after the repeat window", any("full" in a for a in later), later)

    with Rig(runner=RecordingRunner(ALL_TOOLS + ("coredumpctl",), {
            ("coredumpctl",): (0, json.dumps([{"exe": "/usr/bin/chrome"}] * 3 + [{"exe": "/x/python3.11"}]))})):
        crashes = diagnostics.probe_crashes()
    check("crashes are counted per program", crashes and "chrome ×3" in crashes[0].text, crashes)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          6. SCREEN AI                                  │
# └────────────────────────────────────────────────────────────────────────┘

class GrimRunner(RecordingRunner):
    """grim writes a tiny file; tesseract returns canned text."""

    def __init__(self, ocr="Error: permission denied\nKayra: screen describe", **kw):
        super().__init__(ALL_TOOLS + ("tesseract",), **kw)
        self.ocr = ocr

    def run(self, argv, timeout=4.0, input_text=None, stdin_path=None):
        super().run(argv, timeout, input_text, stdin_path)
        if argv[0] == "grim":
            with open(argv[-1], "wb") as handle:
                handle.write(b"\xff\xd8fakejpeg")
            return 0, ""
        if argv[0] == "tesseract":
            return 0, self.ocr
        return 0, ""


def section_screen():
    check.section("[6] Seeing the screen")
    with Rig(runner=GrimRunner()) as rig, Models(vision="A terminal shows a permission error."):
        result = rig.do("screen describe")
        check("describe uses the vision model", result.ok and "permission error" in result.message
              and result.detail.get("provider") == "test", result.message)
        check("the screenshot is of the focused monitor, downscaled JPEG",
              any(a[0] == "grim" and "-o" in a and "-s" in a and "jpeg" in a for a in rig.runner.calls))
    with Rig(runner=GrimRunner()) as rig, Models(text="It says permission denied.", vision=None):
        result = rig.do("screen explain this error")
        check("no vision model -> OCR text to the text model", "permission denied" in result.message,
              result.message)
    with Rig(runner=GrimRunner()) as rig:
        result = rig.do("screen copy text")
        check("copy text puts OCR on the clipboard without Kayra's echo",
              any(a[:2] == ["wl-copy", "--"] and "permission denied" in a[2] and "Kayra" not in a[2]
                  for a in rig.runner.calls), rig.runner.calls)
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("omarchy-capture-screenrecording",))) as rig:
        rig.do("screen record")
        check("recording uses Omarchy's recorder",
              rig.runner.spawned and rig.runner.spawned[0][0] == "omarchy-capture-screenrecording")
    left = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith(("kayra-see-", "kayra-ocr-"))]
    check("no screenshot is left behind in /tmp", not left, left)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 7. SCENES, PLANS, TRIGGERS, BRIEFING                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_workflows():
    check.section("[7] Scenes, plans, triggers, briefing, recall")
    with TempData(), Rig(runner=RecordingRunner(ALL_TOOLS + ("powerprofilesctl", "omarchy-toggle-notification-silencing"),
                                                {("powerprofilesctl", "get"): (0, "performance\n")})) as rig:
        result = rig.do("scene focus")
        check("a built-in mode runs its steps", result.ok and "Focus mode is ready" in result.message,
              result.message)
        check("its settings actually ran (DND + volume)",
              ["omarchy-toggle-notification-silencing"] in rig.runner.calls
              and any(a[:2] == ["wpctl", "set-volume"] for a in rig.runner.calls))
        result = rig.do("scene nonsense")
        check("an unknown mode lists the real ones", result.status == Status.NOT_FOUND and "work" in result.message)

    windows = [client("0x1", "google-chrome", "x", workspace=1), client("0x2", "foot", "t", workspace=4),
               client("0xk", "kayra", "Kayra", workspace=5)]
    with TempData() as data, Rig(FakeHyprland(windows)) as rig:
        result = rig.do("scene save this layout as study")
        saved = json.load(open(os.path.join(data.dir, "scenes.json")))
        check("saving a layout records apps by workspace, never Kayra",
              saved.get("study") == ["open google chrome", "system move window to workspace 1",
                                     "open terminal", "system move window to workspace 4"], saved)
        check("saved modes appear in the list", "study" in rig.do("scene list").message)

    plan = json.dumps(["system do not disturb on", "terminal rm -rf /", "system volume 50",
                       "system shut down the computer", "system brightness 90"])
    with TempData(), Models(text=plan), Rig(runner=RecordingRunner(ALL_TOOLS + ("omarchy-toggle-notification-silencing",))) as rig:
        tokens = workflows.plan_goal("get me ready for my presentation")
        check("planned steps are validated: shell commands are dropped",
              "terminal rm -rf /" not in tokens and tokens[0] == "system do not disturb on", tokens)
        result = rig.do("plan get me ready for my presentation")
        check("a confirm-class step pauses the plan", result.status == Status.NEEDS_CONFIRMATION
              and linux.RUNTIME.pending_plan == ["system brightness 90"], linux.RUNTIME.pending_plan)
        check("steps before it already ran", ["omarchy-toggle-notification-silencing"] in rig.runner.calls)
        handled, spoken = linux.resolve_confirmation("no")
        check("'no' drops the rest of the plan", "stopped the rest" in spoken
              and not linux.RUNTIME.pending_plan, spoken)
        check("so brightness was never touched", not any(a[0] == "brightnessctl" and "set" in a
                                                         for a in rig.runner.calls))

    for text, kind in (("when the download finishes shut down the computer", "download"),
                       ("when my battery drops below 20 switch to power saver", "battery_below"),
                       ("when it's fully charged remind me to unplug", "battery_above"),
                       ("every day at 9am scene work", "daily"), ("at 6pm remind me to call mom", "once_at"),
                       ("when i'm back online check for updates", "online"),
                       ("in 10 minutes remind me to stretch", "after")):
        condition, action = workflows.parse_trigger(text)
        check(f"trigger '{text}' -> {kind}", condition and condition["type"] == kind, (condition, action))
    check("'shut down the computer' becomes the gated power token",
          workflows.action_tokens("shut down the computer") == ["system shut down the computer"])
    check("'remind me to call mom' becomes a spoken line",
          workflows.action_tokens("remind me to call mom") == ["say call mom"])

    with TempData() as data, Rig(runner=RecordingRunner(ALL_TOOLS + ("powerprofilesctl",),
                                                        {("powerprofilesctl", "get"): (0, "balanced\n")})) as rig:
        result = rig.do("trigger when the download finishes shut down the computer")
        check("a trigger that shuts down asks when it is CREATED",
              result.status == Status.NEEDS_CONFIRMATION and "one-minute warning" in result.message, result.message)
        linux.resolve_confirmation("yes")
        stored = json.load(open(os.path.join(data.dir, "triggers.json")))
        check("after yes it is stored, marked approved", stored and stored[0]["approved"], stored)
        snap_active = {"downloading": True, "battery": {}, "online": True}
        snap_done = {"downloading": False, "battery": {}, "online": True}
        check("no fire while the download is running", workflows.evaluate_triggers(snapshot=snap_active) == [])
        lines = workflows.evaluate_triggers(snapshot=snap_done)
        check("when it finishes: a spoken one-minute warning, not an instant shutdown",
              lines and "in one minute" in lines[0] and ["systemctl", "poweroff"] not in rig.runner.calls, lines)
        check("the shutdown is a cancellable timer", linux.TIMERS.list() and
              linux.TIMERS.cancel() is not None)
        check("a fired one-shot trigger is removed", json.load(open(os.path.join(data.dir, "triggers.json"))) == [])

        rig.do("trigger when my battery drops below 20 switch to power saver")
        check("a harmless trigger needs no confirmation",
              len(json.load(open(os.path.join(data.dir, "triggers.json")))) == 1)
        workflows.evaluate_triggers(snapshot={"downloading": False, "online": True,
                                              "battery": {"percent": 18, "state": "discharging"}})
        check("battery at 18% fires it immediately", ["powerprofilesctl", "set", "power-saver"] in rig.runner.calls)
        rig.do("trigger at 6pm remind me to call mom")
        result = rig.do("trigger list")
        check("triggers are listed in words", "6pm" in result.message.replace(" ", "").lower()
              or "18:00" in result.message, result.message)
        result = rig.do("trigger cancel all")
        check("cancel all clears them", json.load(open(os.path.join(data.dir, "triggers.json"))) == [])
        result = rig.do("trigger every now and then do stuff")
        check("an unknown condition explains what triggers exist", not result.ok and "battery" in result.message)

    daily = {"condition": {"type": "daily", "at": "09:00"}, "created": 0, "last_fired": None, "state": {}}
    nine = datetime.datetime(2026, 1, 5, 9, 1).timestamp()
    check("a daily trigger fires just after its time", workflows._condition_met(daily, nine, {}))
    daily["last_fired"] = nine
    check("but only once that day", not workflows._condition_met(daily, nine + 60, {}))

    with Models(text=""), Rig() as rig:
        saved_weather = workflows.weather
        workflows.weather = lambda: "Sunny, +24°C"
        try:
            result = rig.do("system morning briefing")
        finally:
            workflows.weather = saved_weather
        check("the briefing has a greeting, weather and battery even without a model",
              result.ok and "Sunny" in result.message and "Battery" in result.message, result.message)
    policy.clear_audit()
    with Rig(FakeHyprland([client("0x1", "foot")], active="0x1")) as rig:
        rig.do("maximize")
        result = rig.do("system what did you just do")
        check("'what did you just do' reads the audit log", "maximize" in result.message, result.message)

    streams = json.dumps([{"properties": {"application.name": "Google Chrome", "application.process.id": "777"}}])
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("pactl",), {("pactl",): (0, streams)})):
        from kayra.automation import targets
        saved_pids = targets.kayra_owned_pids
        targets.kayra_owned_pids = lambda: {777}
        try:
            mine = workflows.in_call()
        finally:
            targets.kayra_owned_pids = saved_pids
        check("Kayra's own speech browser recording the mic is NOT a call", mine is False)
        check("another app recording the mic IS a call", workflows.in_call() is True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       8. SYSTEM CONTROLS                               │
# └────────────────────────────────────────────────────────────────────────┘

WPCTL = """Audio
 ├─ Devices:
 │      49. Controller [alsa]
 ├─ Sinks:
 │      66. Ryzen HD Audio Controller Analog Stereo [vol: 0.47]
 │  *  121. realme Buds T310                    [vol: 0.35]
 │
 ├─ Sources:
 │  *   67. Ryzen HD Audio Controller Analog Stereo [vol: 1.00]
 │
 └─ Streams:
"""


def section_system():
    check.section("[8] Audio, Bluetooth, Wi-Fi, software, display, processes, dev, files, windows")
    replies = {("wpctl", "status"): (0, WPCTL),
               ("bluetoothctl", "devices"): (0, "Device 88:0E realme Buds T310\nDevice C9:A1 Toad Ergo 4\n"),
               ("bluetoothctl", "info"): (0, "Battery Percentage: 0x5a (90)\n"),
               ("pactl", "-f"): (0, json.dumps([{"index": 46, "mute": False,
                                                 "properties": {"application.name": "Chromium"}}])),
               ("nmcli", "-t", "-f", "SSID"): (0, "Home_5G\nCafe\n")}
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("pactl",), replies)) as rig:
        check("sinks are parsed with the default marked",
              linux_system.audio_devices("Sinks")[1] == ("121", "realme Buds T310", True))
        result = rig.do("system switch to speakers")
        check("'speakers' picks the analog output", ["wpctl", "set-default", "66"] in rig.runner.calls, result.message)
        result = rig.do("system undo that")
        check("and undo puts sound back on the earbuds", ["wpctl", "set-default", "121"] in rig.runner.calls)
        rig.do("system mute my mic")
        check("'mute my mic' mutes the default source", ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"] in rig.runner.calls)
        rig.do("system lower chromium volume")
        check("per-app volume lowers that app's stream only",
              ["pactl", "set-sink-input-volume", "46", "-15%"] in rig.runner.calls, rig.runner.calls[-1])
        result = rig.do("system earbuds battery")
        check("earbuds battery is read from bluetoothctl", "90 percent" in result.message, result.message)
        rig.do("system connect my earbuds")
        check("'connect my earbuds' connects the paired buds", ["bluetoothctl", "connect", "88:0E"] in rig.runner.calls)
        result = rig.do("system connect to wifi home_5g")
        check("Wi-Fi connects to the exact SSID", ["nmcli", "device", "wifi", "connect", "Home_5G"] in rig.runner.calls)

    from kayra import platforms
    saved_battery = platforms.battery_status
    platforms.battery_status = lambda: {"state": "discharging", "percent": 20}
    try:
        with Rig() as rig:
            rig.do("system update my system")
            handled, spoken = linux.resolve_confirmation("yes")
            check("no system update on a low battery", "Plug in" in spoken and not rig.runner.spawned, spoken)
    finally:
        platforms.battery_status = saved_battery

    monitors = [{"name": "eDP-2", "width": 2880, "height": 1800, "scale": 2.25, "refreshRate": 120.0,
                 "transform": 0, "focused": True}]
    with Rig(FakeHyprland(monitors=monitors)) as rig:
        rig.do("system set refresh rate to 60")
        check("refresh rate keeps resolution and scale",
              ["hyprctl", "keyword", "monitor", "eDP-2,2880x1800@60,auto,2.25,transform,0"] in rig.runner.calls)
        rig.do("system undo")
        check("and is undoable", ["hyprctl", "keyword", "monitor", "eDP-2,2880x1800@120,auto,2.25,transform,0"]
              in rig.runner.calls)

    themes = {("omarchy-theme-current",): (0, "Miasma\n"), ("omarchy-theme-list",): (0, "Catppuccin\nMiasma\nNord\n")}
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("omarchy-theme-set", "omarchy-theme-current", "omarchy-theme-list"), themes)) as rig:
        rig.do("system next theme")
        check("'next theme' moves to the following theme", ["omarchy-theme-set", "Nord"] in rig.runner.calls)
        rig.do("system theme catppuccin")
        check("a named theme is applied", ["omarchy-theme-set", "Catppuccin"] in rig.runner.calls)

    git = {("git", "-C"): (0, "## main...origin/main [ahead 2]\n M a.py\n?? b.py\n")}
    with Rig(runner=RecordingRunner(ALL_TOOLS + ("git",), git)) as rig:
        result = rig.do("git status")
        check("git status in words", "main" in result.message and "1 changed and 1 new" in result.message
              and "2 commits not pushed" in result.message, result.message)

    linux_system._NOTES["items"].clear()
    linux_system.note_notification("Slack", "New message", "Ana: lunch?")
    linux_system.note_notification("Kayra", "my own", "")
    with Rig() as rig:
        result = rig.do("system read my notifications")
        check("notifications are read back, Kayra's own excluded",
              "Slack: New message" in result.message and "my own" not in result.message, result.message)
        result = rig.do("system what did i miss")
        check("'what did I miss' only reports new ones", "No new notifications" in result.message)

    wins = [client("0x1", "google-chrome", "a", workspace=1), client("0x2", "google-chrome", "b", workspace=3),
            client("0x3", "foot", "t", workspace=2)]
    with Rig(FakeHyprland(wins, active="0x3")) as rig:
        result = rig.do("system what's open")
        check("'what's open' groups apps by workspace", "Workspace 1: Google Chrome" in result.message,
              result.message)
        result = rig.do("system where is chrome")
        check("'where is chrome' names both workspaces", "1 and 3" in result.message, result.message)
        rig.do("system move all browsers to workspace 5")
        moves = [d for d in rig.hypr.dispatched if d.startswith("hl.dsp.window.move") and '"5"' in d]
        check("'move all browsers' moves both Chrome windows", len(moves) == 2, moves)

    with tempfile.TemporaryDirectory() as root:
        downloads = os.path.join(root, "Downloads")
        os.makedirs(downloads)
        for name in ("a.pdf", "b.jpg", "c.mp4", "d.crdownload", "e.unknown"):
            open(os.path.join(downloads, name), "w").close()
        with open(os.path.join(downloads, "dup1.bin"), "wb") as f:
            f.write(b"x" * 8000)
        with open(os.path.join(downloads, "dup2.bin"), "wb") as f:
            f.write(b"x" * 8000)
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = root
        try:
            result = linux_system.duplicates("downloads")
            check("duplicates are found by content", "1 sets" in result.message, result.message)
            check("and nothing is deleted", os.path.exists(os.path.join(downloads, "dup2.bin")))
            result = linux_system.organize_folder("organize downloads")
            check("organize moves by type", os.path.exists(os.path.join(downloads, "Documents", "a.pdf"))
                  and os.path.exists(os.path.join(downloads, "Images", "b.jpg")), result.message)
            check("organize never touches an unfinished download",
                  os.path.exists(os.path.join(downloads, "d.crdownload")))
            with open(os.path.join(root, "notes.txt"), "w") as f:
                f.write("Kayra notes. The meeting moved to Friday. " * 5)
            with Rig(), Models(text="The meeting moved to Friday."):
                result = linux_system.summarize_file("summarize notes")
            check("a document is summarized from its text", "Friday" in result.message, result.message)
        finally:
            os.environ["HOME"] = saved_home

    with Rig() as rig:
        result = rig.do("system set a sleep timer for 20 minutes")
        check("the music sleep timer is set", "20 minutes" in result.message and linux.TIMERS.list())
        linux.TIMERS.shutdown()

    common.ANNOUNCED.clear()
    linux._timer_notify("call mom")
    check("a fired reminder is SPOKEN, not just notified", common.ANNOUNCED[-1] == "Reminder: call mom.")


def section_presence():
    check.section("[9a] Wake word and the bar status file")
    from kayra.core import voice_control as vc
    check("wake word is OFF by default", not vc.wake_word_enabled({}))
    check("KAYRA_WAKE_WORD=on turns it on", vc.wake_word_enabled({"KAYRA_WAKE_WORD": "on"}))
    check("off: everything passes", vc.passes_wake_word("open chrome", None, False))
    check("on: room talk without the name is ignored", not vc.passes_wake_word("open chrome", 300, True))
    check("on: 'Kayra, open chrome' passes", vc.passes_wake_word("Kayra, open chrome", 300, True))
    check("on: a follow-up right after a reply passes", vc.passes_wake_word("and maximize it", 5, True))
    check("the name must be a word, not a fragment", not vc.passes_wake_word("okayrating", 300, True))

    from kayra.automation import status_file
    with tempfile.TemporaryDirectory() as runtime:
        saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = runtime
        try:
            status_file.write(state="SPEAKING", listening=True, sleeping=False)
            data = json.load(open(status_file.status_path()))
            check("the status file carries state and pid", data["state"] == "SPEAKING"
                  and data["pid"] == os.getpid(), data)
            check("it is private to the user", oct(os.stat(os.path.dirname(
                status_file.status_path())).st_mode)[-3:] == "700")
            status_file._on_event("sleeping_changed", {"sleeping": True})
            check("bus events update it", json.load(open(status_file.status_path()))["sleeping"])
            status_file.stop()
            check("stop writes OFFLINE", json.load(open(status_file.status_path()))["state"] == "OFFLINE")
        finally:
            if saved is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = saved
    widget = os.path.join(os.path.dirname(browser_bridge.extension_dir()), "omarchy-kayra-widget")
    manifest = json.load(open(os.path.join(widget, "manifest.json")))
    check("the bar widget is a valid Omarchy bar-widget plugin",
          manifest["id"] == "kayra.status" and manifest["entryPoints"]["barWidget"] == "Panel.qml"
          and os.path.exists(os.path.join(widget, "Panel.qml")))


def section_structure():
    check.section("[9] Structure: argv only, no shell, no name-kill, no import side effects")
    import ast
    for module in (diagnostics, web_macros, browser_bridge, screen_ai, workflows, linux_system, ai):
        source = open(module.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        shell = [n for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "shell"
                 and getattr(n.value, "value", None) is True]
        name = module.__name__.rsplit(".", 1)[-1]
        check(f"{name}: no shell=True", not shell)
        check(f"{name}: no os.system", "os.system(" not in source)
        literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        check(f"{name}: no pkill/killall", not ({"pkill", "killall", "taskkill"} & literals))
    check("importing the bridge does not open a socket", browser_bridge.BRIDGE._listener is None
          or browser_bridge.BRIDGE._thread is not None)
    check("importing workflows does not start the watcher", workflows._WATCHER["thread"] is None
          or workflows._WATCHER["thread"].is_alive())


def main():
    print_banner("ASSISTANT AUTOMATION")
    print_info(f"Host: {describe_host()}")
    with EnvironmentGuard() as guard:
        section_parsing()
        section_safety()
        section_bridge()
        section_web()
        section_gmail_and_routing()
        section_diagnostics()
        section_screen()
        section_workflows()
        section_system()
        section_presence()
        section_structure()
    check.section("[10] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
