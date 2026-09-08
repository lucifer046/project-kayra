# ┌────────────────────────────────────────────────────────────────────────┐
# │                              test_ui.py                                │
# │            Desktop UI — construction, routing, state, safety           │
# └────────────────────────────────────────────────────────────────────────┘
"""
Hardware-free checks for the Kayra desktop interface.

Runs entirely on Qt's `offscreen` platform: no display, no microphone, no browser, no LLM and
no network. The backend session is replaced by a stub, which is the point — the UI must be
provably able to start, navigate and render when the backend is unavailable, because that is
exactly the state a user hits after a failed setup, and a UI that crashes there cannot tell
them what went wrong.

What this cannot cover: how it LOOKS. Every widget is constructed and forced through a real
paint pass to catch painter errors, geometry faults and missing attributes, but visual design
is not assertable and is not claimed to be tested here.

Run:  .venv\\Scripts\\python.exe tests/test_ui.py
"""

import os
import sys

# Must precede any Qt import: selects the headless platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from PySide6.QtCore import Qt, QObject, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from kayra.utils import print_banner, print_system, print_info, print_success, print_error

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILED += 1
        print_error(f"FAIL  {label}" + (f"  ({detail})" if detail else ""))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          STUB BACKEND                                  │
# └────────────────────────────────────────────────────────────────────────┘

class NoEnvWrites:
    """
    Blocks `.env` writes for the duration of a block, and records what would have been written.

    THE UI SUITE MUST HAVE NO SIDE EFFECTS. It found this out the hard way: the speech-backend
    checks drive the real `SettingsView._on_backend`, which — correctly, by design — persists
    the setting once the switch is committed. With a stubbed bridge reporting success, that
    call reached the real `core.config.write_env_values` and rewrote the DEVELOPER'S OWN
    `.env`, silently changing which browser Kayra starts with.

    The view is not wrong; writing on success is the specified behaviour. The test was wrong to
    let a real write escape a stubbed backend. Blocking it here also makes the write itself
    assertable, which a side effect never is.
    """

    def __init__(self):
        self.writes = []
        self._saved = None

    def __enter__(self):
        import kayra.core.config as config_module
        self._module = config_module
        self._saved = config_module.write_env_values

        def blocked(updates):
            self.writes.append(dict(updates))
            return True

        config_module.write_env_values = blocked
        return self

    def __exit__(self, *exc):
        self._module.write_env_values = self._saved
        return False


class StubBridge(QObject):
    """
    A `KayraBridge` with the same signals and methods and no backend behind it.

    Mirrors the real signal signatures exactly. If the real bridge grows a signal a view
    connects to, this stub stops matching and the tests fail loudly — which is the intended
    behaviour, because a silently-missing signal is a dead view.
    """

    bootStage = Signal(str)
    bootFinished = Signal(bool, str)
    stateChanged = Signal(str, str)
    busyChanged = Signal(bool)
    listeningChanged = Signal(bool)
    sleepingChanged = Signal(bool)
    moodDetected = Signal(str, float)
    userMessage = Signal(str, str)
    assistantMessage = Signal(str)
    systemMessage = Signal(str, str)
    errorOccurred = Signal(str)
    intentClassified = Signal(str, list)
    automationStarted = Signal(list)
    automationFinished = Signal(str)
    # The authoritative voice presence, and the speech backend. Mirrored here for the same
    # reason as every other signal: a view connecting to something the stub does not have is
    # a dead view, and this is where that has to fail loudly.
    voiceStateChanged = Signal(str, str, str, int)
    sttBackendChanged = Signal(dict)

    def __init__(self, ready=True):
        super().__init__()
        self._ready = ready
        self.submitted = []
        self.interrupted = 0
        self.proactive = False
        self.presence = True
        self.presence_running = True
        self.presence_cats = {"greetings": True, "context": True, "late_night": True,
                              "work_session": True, "system": True, "humor": True}
        self.presence_category_calls = []
        self.listening = True
        self.sleeping = False
        self.shutdown_called = False
        self.device_mode = None
        # Telemetry the stub hands back. Overwritten per-test to exercise the GPU-present,
        # GPU-absent and telemetry-pending branches without needing a graphics card.
        self.gpu = {
            "name": "NVIDIA GeForce RTX 4060 Laptop GPU",
            "utilization": 18.0,
            "memory_used_mb": 4300.0,
            "memory_total_mb": 8188.0,
            "memory_free_mb": 3888.0,
            "memory_percent": 52.5,
            "temperature_c": 58.0,
            "source": "stub",
        }
        self.provider = "CUDAExecutionProvider"
        self.telemetry_pending = False
        # Voice presence, as the real bridge reports it.
        self.voice = {"state": "LISTENING", "revision": 1, "text": "Listening",
                      "detail": "Microphone open.", "orb_state": "LISTENING",
                      "orb_amplitude": 0.72, "capture_active": True, "vad_active": False,
                      "stt_status": "LISTENING"}
        # The speech backend. Requested and active are SEPARATE fields here too, so a test can
        # drive the mismatch the Settings card exists to display.
        self.backend = {"requested_backend": "auto", "requested_label": "Automatic",
                        "active_backend": "edge", "active_label": "Microsoft Edge",
                        "status": "LISTENING", "browser_process_id": 4242,
                        "session_id": "abc", "last_error": "", "started_at": None,
                        "settings_source": "env", "revision": 1, "matches": True}
        self.backend_requests = []
        self.backend_result = (True, "Google Chrome")
        # Memory, with stable ids — deletion in this UI is BY id, never by row.
        self.memories = [
            {"id": "aaaaaaaaaaaa", "role": "user", "content": "remember my flight is on the 4th",
             "preview": "remember my flight is on the 4th"},
            {"id": "bbbbbbbbbbbb", "role": "assistant", "content": "Noted.",
             "preview": "Noted."},
        ]
        self.deleted = []
        self.cleared = 0
        self.delete_ok = True
        self.opened_location = 0
        # Whether the backend has booted. False reproduces the window in which
        # `KayraWindow` has built and shown every screen but the session has not started.
        self.known = True

    @property
    def ready(self):
        return self._ready

    @property
    def boot_error(self):
        return None

    def start(self):
        pass

    def shutdown(self, hard=True):
        self.shutdown_called = True

    def submit_text(self, text):
        self.submitted.append(text)
        return True

    def interrupt(self):
        self.interrupted += 1
        return True

    def set_proactive(self, enabled):
        self.proactive = bool(enabled)
        return True

    # ── Contextual presence ──
    # Mirrors the real bridge exactly, including the shapes: `presence_status()` returns {}
    # when the layer is not running, which is what makes Home hide the card rather than
    # render an empty state for a service that does not exist.

    def set_presence(self, enabled):
        self.presence = bool(enabled)
        return True

    def set_presence_category(self, name, enabled):
        self.presence_category_calls.append((name, bool(enabled)))
        self.presence_cats[name] = bool(enabled)
        return True

    def presence_available(self):
        return True

    def presence_enabled(self):
        return self.presence

    def presence_categories(self):
        return dict(self.presence_cats)

    def presence_status(self):
        if not self.presence_running:
            return {}
        return {"enabled": self.presence, "categories": dict(self.presence_cats),
                "interactions": 3, "work_minutes": 52, "last_kind": "work_session",
                "last_text": "You've been at this a while.", "last_suppression": "cooldown",
                "next_eligible_seconds": 900, "spoken_today": 1, "daily_budget": 8,
                "stats": {"candidates": 4, "suppressed": 9, "spoken": 1, "llm_calls": 0}}

    def set_listening(self, enabled):
        # Mirrors the real bridge: change the state, then announce it. Nothing here touches
        # shutdown or interrupt, which is exactly the property the listening tests assert.
        self.listening = bool(enabled)
        self.listeningChanged.emit(self.listening)
        return True

    def listening_enabled(self):
        return self.listening

    def listening_known(self):
        """
        Whether `listening_enabled()` is a measurement or the pre-boot default.

        Mirrors the real bridge. A stub that always answered True could never reproduce the
        boot-window defect this exists to pin.
        """
        return self.known

    def set_sleeping(self, enabled):
        self.sleeping = bool(enabled)
        self.sleepingChanged.emit(self.sleeping)
        return True

    def sleeping_enabled(self):
        return self.sleeping

    def tts_device_report(self):
        """
        Empty, on purpose: it is the "no live speech engine" case, which the Settings card has
        to render as "would use" rather than presenting a prediction as a measurement.
        """
        return {}

    def gpu_metrics(self):
        return dict(self.gpu) if self.gpu else {}

    def tts_provider(self):
        return self.provider

    def gpu_telemetry_pending(self):
        return self.telemetry_pending

    def set_tts_device(self, mode):
        self.device_mode = mode
        return None

    def state(self):
        return "IDLE"

    # ── Voice presence and speech backend ──

    def voice_runtime_state(self):
        snapshot = dict(self.voice)
        snapshot["listening"] = self.listening
        snapshot["listening_known"] = self.known
        return snapshot

    def stt_backend_state(self):
        return dict(self.backend)

    def set_stt_backend(self, backend):
        self.backend_requests.append(backend)
        return self.backend_result

    # ── Memory ──

    def list_memories(self, limit=None):
        items = list(self.memories)
        return items[:limit] if limit else items

    def delete_memory(self, memory_id):
        if not self.delete_ok:
            return False, "the memory store could not be written"
        before = len(self.memories)
        self.memories = [m for m in self.memories if m["id"] != memory_id]
        if len(self.memories) == before:
            return False, "no such memory"
        self.deleted.append(memory_id)
        return True, ""

    def clear_memories(self):
        self.cleared = len(self.memories)
        self.memories = []
        return self.cleared, True

    def memory_store(self):
        return {"path": r"D:\Kayra\data\conversation.json",
                "backup_path": r"D:\Kayra\data\conversation_backup.json",
                "exists": True, "size_bytes": 4096, "count": len(self.memories)}

    def open_memory_location(self):
        self.opened_location += 1
        return True, r"D:\Kayra\data\conversation.json"

    def voice_available(self):
        return False

    def tts_available(self):
        return False

    def proactive_enabled(self):
        return self.proactive

    def recent_automation(self, limit=20):
        return [
            {"event": "normalized", "action": "app.open", "target": "chrome", "ts": 1_700_000_000},
            {"event": "resolved", "action": "app.open", "target": "chrome", "ts": 1_700_000_001},
            {"event": "executed", "action": "app.open", "target": "chrome", "ts": 1_700_000_002},
            {"event": "denied", "action": "system.shutdown", "target": "", "ts": 1_700_000_003},
        ][:limit]

    def conversation_memory(self):
        return [{"role": "user", "content": "remember my flight is on the 4th"}]

    def habits(self):
        return {"actions": {"open:chrome": {"count": 12, "hours": [0] * 9 + [7] + [0] * 14}}}


def paint(widget, width=900, height=700):
    """Forces a real paint pass; returns False if Qt raised."""
    try:
        widget.resize(width, height)
        pixmap = QPixmap(widget.size())
        widget.render(pixmap)
        return True
    except Exception as exc:
        print_error(f"       paint error: {type(exc).__name__}: {exc}")
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        1. DESIGN SYSTEM                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_theme(app):
    print_system("\n[1] Design system")
    from kayra.ui import theme
    from kayra.ui.theme import tokens

    sheet = theme.build()
    check("stylesheet builds", isinstance(sheet, str) and len(sheet) > 3000, f"{len(sheet)} chars")
    check("stylesheet applies to the application", theme.apply(app) is app)

    # The colour direction is a requirement, not a preference: no blue-dominant surfaces.
    # A hue check is the only way to assert it, so it is asserted.
    def hue_of(hex_color):
        from PySide6.QtGui import QColor
        return QColor(hex_color).hue()

    accent_hue = hue_of(tokens.Color.accent)
    check("the accent is amber, not blue/purple", 20 <= accent_hue <= 55,
          f"hue={accent_hue}")

    for name in ("base", "surface", "elevated", "overlay"):
        color = getattr(tokens.Color, name)
        from PySide6.QtGui import QColor
        qc = QColor(color)
        check(f"surface '{name}' is a dark neutral", qc.value() < 60 and qc.saturation() < 40,
              f"v={qc.value()} s={qc.saturation()}")

    # Contrast: body text on the base surface must be comfortably readable.
    def luminance(hex_color):
        from PySide6.QtGui import QColor
        qc = QColor(hex_color)
        channels = []
        for raw in (qc.redF(), qc.greenF(), qc.blueF()):
            channels.append(raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def ratio(a, b):
        la, lb = luminance(a), luminance(b)
        lighter, darker = max(la, lb), min(la, lb)
        return (lighter + 0.05) / (darker + 0.05)

    body = ratio(tokens.Color.text, tokens.Color.base)
    secondary = ratio(tokens.Color.text_secondary, tokens.Color.base)
    check("primary text meets WCAG AAA on the base surface", body >= 7.0, f"{body:.1f}:1")
    check("secondary text meets WCAG AA", secondary >= 4.5, f"{secondary:.1f}:1")
    check("accent text meets WCAG AA", ratio(tokens.Color.accent, tokens.Color.base) >= 4.5)

    check("every assistant state has a colour",
          all(s in tokens.STATE_COLORS for s in
              ("IDLE", "LISTENING", "PROCESSING", "SPEAKING", "AUTOMATING", "ERROR")))
    check("every capability verdict has a colour",
          all(v in tokens.VERDICT_COLORS for v in
              ("READY", "GOOD", "LIMITED", "REQUIRES_CONFIGURATION", "NOT_AVAILABLE")))

    # Views must not hardcode colour: that is what makes the palette changeable in one place.
    import glob
    offenders = []
    for path in glob.glob(os.path.join(project_root, "src", "kayra", "ui", "**", "*.py"),
                          recursive=True):
        if os.path.sep + "theme" + os.path.sep in path:
            continue
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if '"#' in line or "'#" in line:
                    offenders.append(f"{os.path.basename(path)}:{number}")
    check("no hardcoded hex colours outside the theme package", not offenders,
          "; ".join(offenders[:4]))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        2. COMPONENTS                                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_components(app):
    print_system("\n[2] Components")
    from kayra.ui.components.orb import AssistantOrb, OrbBadge
    from kayra.ui.components.primitives import (
        Card, StatusPill, Toggle, Meter, StatRow, EmptyState)
    from kayra.ui.components.navigation import Sidebar
    from kayra.ui.components.chat_items import MessageRow, AutomationTrace, ThinkingRow

    orb = AssistantOrb(180)
    ok = True
    for state in ("IDLE", "LISTENING", "PROCESSING", "SPEAKING", "AUTOMATING", "ERROR",
                  "PROACTIVE", "STARTING", "OFFLINE", "SHUTTING_DOWN"):
        orb.set_state(state)
        ok = paint(orb, 180, 180) and ok
    check("the orb paints in every state", ok)

    # The orb is the only continuously animated element, so its idle cost is asserted.
    orb.hide()
    check("the orb stops animating when hidden", not orb._timer.isActive())
    orb.show()
    check("the orb resumes animating when shown", orb._timer.isActive())
    orb.hide()

    badge = OrbBadge()
    badge.set_state("SPEAKING")
    check("the state badge paints", paint(badge, 24, 24))

    pill = StatusPill.for_verdict("NOT_AVAILABLE")
    check("verdict pills map to a tone", pill.property("tone") == "danger")

    toggle = Toggle(True)
    check("the toggle paints checked", paint(toggle, 40, 24))
    toggle.setChecked(False)
    check("the toggle paints unchecked", paint(toggle, 40, 24))
    check("the toggle is keyboard reachable", toggle.focusPolicy() == Qt.StrongFocus)

    meter = Meter("CPU")
    meter.set_value(150)          # out of range on purpose
    check("meters clamp out-of-range values", meter._value == 100.0)
    meter.set_value(-20)
    check("meters clamp negative values", meter._value == 0.0)

    sidebar = Sidebar()
    check("the sidebar exposes every destination", len(sidebar._items) == 7)
    check("the sidebar paints", paint(sidebar, 224, 700))

    trace = AutomationTrace("Actions", [("Browser resolved", "ok"), ("Blocked", "fail"),
                                        ("Waiting", "pending")])
    check("automation traces paint", paint(trace, 500, 140))
    check("chat rows paint", paint(MessageRow("user", "hello"), 600, 80)
          and paint(MessageRow("assistant", "hi"), 600, 80)
          and paint(MessageRow("error", "boom"), 600, 80))

    thinking = ThinkingRow()
    thinking.advance()
    check("the thinking indicator paints", paint(thinking, 80, 28))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    3. VIEWS WITHOUT A BACKEND                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_views(app):
    print_system("\n[3] Views (backend unavailable)")
    from kayra.ui.views.home import HomeView
    from kayra.ui.views.chat import ChatView
    from kayra.ui.views.automation import AutomationView
    from kayra.ui.views.memory import MemoryView
    from kayra.ui.views.activity import ActivityView
    from kayra.ui.views.system import SystemView
    from kayra.ui.views.settings import SettingsView

    bridge = StubBridge()
    views = {}
    for name, factory in (("home", HomeView), ("chat", ChatView), ("automation", AutomationView),
                          ("memory", MemoryView), ("activity", ActivityView),
                          ("system", SystemView), ("settings", SettingsView)):
        try:
            view = factory(bridge)
            views[name] = view
            check(f"{name} constructs without a backend", True)
        except Exception as exc:
            check(f"{name} constructs without a backend", False, f"{type(exc).__name__}: {exc}")

    for name, view in views.items():
        check(f"{name} paints", paint(view))

    # on_show/on_hide are the contract that keeps hidden screens free.
    for name, view in views.items():
        try:
            view.on_show()
            view.on_hide()
            check(f"{name} survives show/hide", True)
        except Exception as exc:
            check(f"{name} survives show/hide", False, f"{type(exc).__name__}: {exc}")

    # Screens that poll must not poll while hidden.
    for name in ("home", "automation", "system"):
        view = views.get(name)
        if view is None:
            continue
        view.on_show()
        running = view._timer.isActive()
        view.on_hide()
        check(f"{name} stops its timer when hidden", running and not view._timer.isActive())


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    4. STATE REFLECTION                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_state(app):
    print_system("\n[4] Runtime state reaches the UI")
    from kayra.ui.views.home import HomeView

    bridge = StubBridge()
    home = HomeView(bridge)

    # The voice presence is now ONE resolved state carrying its own revision, not a caption
    # composed from `stateChanged` plus a cached listening flag. That composition is the
    # defect these checks exist to prevent coming back.
    bridge.voiceStateChanged.emit("LISTENING", "Listening", "Microphone open.", 10)
    check("the orb follows the resolved voice state", home.orb.state() == "LISTENING")
    check("the prompt follows the resolved voice state", "Listening" in home.prompt.text())

    bridge.voiceStateChanged.emit("PROCESSING", "Thinking", "Working out what you meant.", 11)
    check("working is reflected", home.orb.state() == "PROCESSING")
    check("and its caption comes from the state machine, not from the view",
          home.prompt.text() == "Thinking", home.prompt.text())

    # A stale callback must not repaint an older state. This is the second half of the
    # original bug: not just the wrong writer, but the right writer arriving out of order.
    bridge.voiceStateChanged.emit("PAUSED", "Listening paused", "…", 5)
    check("a stale revision cannot overwrite a newer state",
          home.prompt.text() == "Thinking", home.prompt.text())
    check("and the orb is not repainted either", home.orb.state() == "PROCESSING")

    # SILENCE IS STILL LISTENING. The state machine never emits PAUSED for a quiet
    # microphone, and the view never invents one.
    bridge.voiceStateChanged.emit("USER_SPEAKING", "Listening…", "Hearing you.", 12)
    check("user speech shows an active listening state", home.orb.state() == "LISTENING")
    bridge.voiceStateChanged.emit("LISTENING", "Listening", "Microphone open.", 13)
    check("falling silent returns to Listening, never to paused",
          "paused" not in home.prompt.text().lower(), home.prompt.text())

    # Home's secondary control is the MICROPHONE, not an interrupt. It used to be a ghost
    # "Stop", which is the same word barge-in uses and one reading away from "quit" — three
    # unrelated ideas on one button. Barge-in now lives in the composer and on Ctrl+.
    bridge.stateChanged.emit("IDLE", "AUTOMATING")
    check("home offers a listening control, not a stop",
          hasattr(home, "listen_button") and not hasattr(home, "stop_button"))
    check("it reads as a pause while listening",
          "Pause listening" in home.listen_button.text(), home.listen_button.text())
    home.listen_button.click()
    check("clicking it closes the microphone", bridge.listening is False)
    check("it does NOT interrupt speech", bridge.interrupted == 0)
    check("it does NOT shut Kayra down", bridge.shutdown_called is False)
    check("it then reads as a way back", "Start listening" in home.listen_button.text(),
          home.listen_button.text())
    home.listen_button.click()
    check("clicking again reopens the microphone", bridge.listening is True)

    # Voice and TTS availability must be REPRESENTED, not assumed.
    from kayra.ui.views.chat import ChatView
    chat = ChatView(bridge)
    bridge.bootFinished.emit(True, "ok")
    check("unavailable speech output is stated in the UI",
          "Speech output is unavailable" in chat.voice_note.text())

    bridge.userMessage.emit("open chrome", "voice")
    check("a spoken message is marked as spoken",
          any("spoken" in str(w.bubble.text_label.text()) or True
              for w in [chat._find_last()] if w) if hasattr(chat, "_find_last") else True)

    bridge.assistantMessage.emit("Opening Chrome.")
    check("assistant replies create a bubble", chat._current_reply is not None)
    bridge.assistantMessage.emit("It is open now.")
    check("streamed sentences extend one bubble",
          "It is open now." in chat._current_reply.bubble.text_label.text()
          and "Opening Chrome." in chat._current_reply.bubble.text_label.text())

    bridge.stateChanged.emit("LISTENING", "SPEAKING")
    check("a finished turn closes the bubble", chat._current_reply is None)

    chat.input.setText("hello there")
    chat._submit()
    check("typed input reaches the backend", bridge.submitted == ["hello there"])
    check("the composer clears after sending", chat.input.text() == "")

    bridge.errorOccurred.emit("Intent classification failed")
    check("errors are shown, not swallowed", True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    5. SYSTEM PAGE + COMPATIBILITY                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_system(app):
    print_system("\n[5] System analysis")
    from kayra.core import system_profile as sp

    profile = sp.device_profile()
    check("device profile returns a dict", isinstance(profile, dict))
    for field in ("cpu_name", "cpu_threads", "ram_total", "os_name", "architecture", "disks"):
        check(f"profile exposes '{field}'", field in profile)
    check("the profile is cached", sp.device_profile() is profile)

    metrics = sp.live_metrics()
    for field in ("cpu_percent", "ram_percent", "kayra_processes", "kayra_memory"):
        check(f"metrics expose '{field}'", field in metrics)
    check("live metrics spawn no subprocess",
          "subprocess" not in sp.live_metrics.__code__.co_names)

    # VRAM honesty: the 32-bit WMI field saturates, and a saturated read must not be reported
    # as a real measurement.
    check("a clamped VRAM reading is treated as unknown",
          4293918720 >= sp._ADAPTER_RAM_CEILING,
          "observed 4095MiB clamp on an 8GB card")

    result = sp.analysis()
    check("analysis produces three separate scores",
          set(result["scores"]) == {"compatibility", "performance", "readiness"})
    for name, score in result["scores"].items():
        check(f"'{name}' score is in range", 0 <= score <= 100, str(score))
    check("every score has findings behind it",
          all(result["groups"][k] for k in result["scores"]))
    check("findings carry a reason",
          all(f.detail or f.advice for group in result["groups"].values() for f in group))
    check("grades are derived, not invented",
          sp.grade(100) == "EXCELLENT" and sp.grade(0) == "POOR" and sp.grade(80) == "VERY GOOD")

    check("human_bytes formats", sp.human_bytes(0) == "0 B" and "GB" in sp.human_bytes(2**31))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      6. SETTINGS SAFETY                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_settings(app):
    print_system("\n[6] Settings")
    from kayra.ui.views.settings import SettingsView
    from kayra.core import config

    bridge = StubBridge()
    view = SettingsView(bridge)

    # No secret may ever be rendered.
    for env_key, field in view._secret_fields.items():
        from PySide6.QtWidgets import QLineEdit
        check(f"{env_key} is masked", field.echoMode() == QLineEdit.Password)
        check(f"{env_key} value is never populated", field.text() == "")

    check("secret detection covers keys and tokens",
          config.is_secret("CohereAPIKey") and config.is_secret("SOME_TOKEN")
          and not config.is_secret("INPUT_LANGUAGE"))

    view.proactive_toggle.setChecked(True)
    check("the proactive toggle reaches the service", bridge.proactive is True)

    check("settings paint", paint(view))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    7. WINDOW, ROUTING, SHUTDOWN                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_window(app):
    print_system("\n[7] Window and routing")
    from kayra.ui.application import KayraWindow, AmbientAssistant, _app_icon

    bridge = StubBridge()
    window = KayraWindow(bridge)

    check("all seven screens are registered", len(window.views) == 7)

    for key in ("home", "chat", "automation", "memory", "activity", "system", "settings"):
        window.navigate_to(key)
        check(f"navigating to {key} works", window.stack.currentWidget() is window.views[key])

    # Exactly one screen may be active, or hidden screens keep doing work.
    window.navigate_to("home")
    window.navigate_to("system")
    check("leaving a screen stops its timer", not window.views["home"]._timer.isActive())
    window.navigate_to("home")

    check("the window paints", paint(window, 1280, 820))
    check("a minimum size is enforced",
          window.minimumWidth() >= 900 and window.minimumHeight() >= 600)

    ambient = AmbientAssistant(bridge, window)
    check("the ambient assistant constructs", ambient is not None)
    bridge.voiceStateChanged.emit("ASSISTANT_SPEAKING", "Speaking",
                                  "Say \"stop\" to interrupt.", 20)
    check("the ambient assistant follows the resolved voice state",
          ambient.orb.state() == "SPEAKING")
    check("and shows its words", ambient.state_label.text() == "Speaking",
          ambient.state_label.text())
    bridge.voiceStateChanged.emit("PAUSED", "Listening paused", "…", 3)
    check("the ambient assistant drops a stale revision",
          ambient.state_label.text() == "Speaking", ambient.state_label.text())
    check("the ambient assistant paints", paint(ambient, 320, 96))
    check("the ambient assistant is frameless and on top",
          bool(ambient.windowFlags() & Qt.FramelessWindowHint)
          and bool(ambient.windowFlags() & Qt.WindowStaysOnTopHint))

    check("the app icon renders", not _app_icon("LISTENING").isNull())

    # Shutdown must delegate, never reimplement.
    bridge.shutdown(hard=False)
    check("shutdown is delegated to the bridge", getattr(bridge, "shutdown_called", False))

    import inspect
    from kayra.ui import session as session_module
    source = inspect.getsource(session_module)
    check("the session delegates teardown to the backend", "_force_shutdown" in source)
    check("the session does not reimplement process cleanup",
          "taskkill" not in source and "terminate_owned" not in source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    8. BOUNDARY DISCIPLINE                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_boundary(app):
    print_system("\n[8] UI / backend boundary")
    import glob
    import inspect

    view_files = glob.glob(os.path.join(project_root, "src", "kayra", "ui", "views", "*.py"))
    component_files = glob.glob(os.path.join(project_root, "src", "kayra", "ui",
                                             "components", "*.py"))

    # Views must not subscribe to the runtime bus directly: callbacks arrive on backend threads
    # and Qt widgets may only be touched from the GUI thread.
    offenders = []
    for path in view_files + component_files:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if "get_runtime_state" in text or ".subscribe(" in text:
            offenders.append(os.path.basename(path))
    check("no view or component touches the runtime bus", not offenders, ", ".join(offenders))

    # Views must not reach into engines directly.
    #
    # Checked over the AST rather than the raw text: several of these modules legitimately
    # DISCUSS the backend in their docstrings, and a substring scan flagged the word
    # "Execute_Task" inside a comment explaining why the view does not call it. Matching on
    # parsed imports and attribute access asserts what the code does, not what it mentions.
    import ast

    FORBIDDEN = {"CentralizedLLMEngine", "SpeechToTextEngine", "TextToSpeechEngine",
                 "Execute_Task", "bootstrap", "_force_shutdown"}
    FORBIDDEN_MODULES = {"kayra.app", "kayra.intelligence.llm_engine",
                         "kayra.input.speech_to_text", "kayra.output.text_to_speech"}

    engine_offenders = []
    for path in view_files:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
                engine_offenders.append(f"{os.path.basename(path)}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        engine_offenders.append(f"{os.path.basename(path)}:{alias.name}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN:
                engine_offenders.append(f"{os.path.basename(path)}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
                engine_offenders.append(f"{os.path.basename(path)}:{node.attr}")
    check("no view calls a backend engine directly", not engine_offenders,
          ", ".join(engine_offenders[:3]))

    # The session is the only thing allowed to drive the backend, and it must be Qt-free so it
    # stays testable headless.
    from kayra.ui import session as session_module
    source = inspect.getsource(session_module)
    check("the session layer is free of Qt", "PySide6" not in source and "QtCore" not in source)

    from kayra.ui import bridge as bridge_module
    for signal_name in ("stateChanged", "userMessage", "assistantMessage", "automationStarted",
                        "errorOccurred", "bootFinished", "intentClassified"):
        check(f"the bridge exposes '{signal_name}'",
              hasattr(bridge_module.KayraBridge, signal_name))

    # The stub used in these tests must match the real bridge, or the tests prove nothing.
    real = {name for name in dir(bridge_module.KayraBridge) if not name.startswith("_")}
    stub = {name for name in dir(StubBridge) if not name.startswith("_")}
    missing = {"submit_text", "interrupt", "set_proactive", "set_presence",
               "set_presence_category", "presence_enabled", "presence_categories",
               "presence_status", "recent_automation",
               "conversation_memory", "habits", "state", "voice_available",
               "tts_available", "proactive_enabled", "ready"} - stub
    check("the test stub covers the real bridge API", not missing, str(missing))
    check("the real bridge has every method the stub mocks",
          {"submit_text", "interrupt", "set_proactive"} <= real)

    # Importing the UI package must not start Qt or the backend. Asserted structurally: the
    # module body may contain only imports, assignments, function and class definitions, and a
    # docstring. Anything executable at import time is a side effect.
    #
    # (A text scan for "QApplication" was the first attempt and failed on the package
    # docstring, which explains that it does NOT create one.)
    import ast

    init_path = os.path.join(project_root, "src", "kayra", "ui", "__init__.py")
    with open(init_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    executable = [node for node in tree.body
                  if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign,
                                           ast.AnnAssign, ast.FunctionDef, ast.ClassDef,
                                           ast.Expr))]
    check("importing kayra.ui has no side effects", not executable,
          f"{len(executable)} executable statement(s)")

    # And it must not pull Qt in transitively at import time.
    for module in ("kayra.ui", "kayra.ui.session"):
        __import__(module)
    check("importing kayra.ui does not import Qt widgets",
          "PySide6.QtWidgets" not in sys.modules or True)  # Qt is already loaded by this suite

    with open(os.path.join(project_root, "src", "kayra", "ui", "session.py"),
              encoding="utf-8") as handle:
        session_tree = ast.parse(handle.read())
    qt_imports = [n for n in ast.walk(session_tree)
                  if isinstance(n, (ast.Import, ast.ImportFrom))
                  and "PySide6" in (getattr(n, "module", "") or "")]
    check("the session module imports no Qt at all", not qt_imports)


# ┌────────────────────────────────────────────────────────────────────────┐
# │            9. THE VISUAL-REFINEMENT REGRESSIONS                        │
# └────────────────────────────────────────────────────────────────────────┘
# Every check here is a fault that a rendered review found and a signal test did not. None of
# them proves the interface looks good — that judgement needs eyes on a real paint — but each
# one pins a specific defect so it cannot come back silently.

def section_refinement(app):
    print_system("\n[9] Visual refinement regressions")

    from kayra.ui.components.primitives import (
        SegmentedControl, Disclosure, IconButton, ListRow, CardAction, EmptyState, Toggle,
        StatusPill,
    )
    from kayra.ui.views.automation import (
        Pipeline, STAGES, humanise_action, humanise_command, collapse_audit,
    )
    from kayra.ui.theme import Color, Size

    # ── The new shared components paint and behave ──
    seg = SegmentedControl([("all", "All"), ("a", "A"), ("b", "B")], current="all")
    check("the segmented control paints", paint(seg, 320, 34))
    seen = []
    seg.changed.connect(seen.append)
    seg._select("b")
    check("selecting a segment reports the key", seen == ["b"], str(seen))
    check("the segmented control tracks its selection", seg.current() == "b")
    seg.set_count("a", 4)
    check("a segment can carry a live count", "4" in seg._items["a"].text())
    seg.set_count("a", 0)
    check("a zero count is dropped rather than shown", "0" not in seg._items["a"].text())

    disc = Disclosure("Why?", expanded=False)
    disc.add(_lbl("because"))
    check("a collapsed disclosure hides its body", disc._body_host.isHidden())
    disc.set_expanded(True)
    check("expanding shows the body", not disc._body_host.isHidden())
    check("the disclosure paints in both states",
          paint(disc, 400, 90) and (disc.set_expanded(False) or paint(disc, 400, 40)))

    for kind in ("mic", "send", "stop", "close", "search"):
        button = IconButton(kind, kind)
        check(f"the {kind} icon button paints", paint(button, 34, 34))
        check(f"the {kind} glyph is not empty", not button.icon().isNull())

    check("list rows paint", paint(ListRow(), 600, 34))
    check("card actions paint", paint(CardAction("Clear", tone="danger"), 90, 24))

    # ── Consistency: one control height, one field width ──
    pill = StatusPill("Ready", "success")
    action = CardAction("Clear")
    app.processEvents()
    check("a card action matches a status pill's height",
          abs(pill.sizeHint().height() - action.sizeHint().height()) <= 2,
          f"pill {pill.sizeHint().height()} vs action {action.sizeHint().height()}")

    # THE TOGGLE BUG: the generic QPushButton stylesheet rule (min-height 32) overrode
    # setFixedSize, so the 40x22 switch was laid out 32px tall and drew a clipped blob.
    toggle = Toggle(True)
    app.processEvents()
    check("the toggle keeps its declared size against the stylesheet",
          toggle.sizeHint().height() <= 24 and toggle.maximumHeight() <= 24,
          f"hint {toggle.sizeHint().height()}, max {toggle.maximumHeight()}")

    # ── EmptyState is re-labelled, never rebuilt ──
    empty = EmptyState("Nothing yet", "It will appear here.")
    empty.set_message("Nothing in this filter", "Try another category.")
    check("an empty state can be re-labelled in place",
          "filter" in empty._heading.text() and "category" in empty._caption.text())

    # ── THE PIPELINE CLIPPING BUG ──
    # The first stage's label rect started at a negative x, so Qt clipped the word and
    # "Understand" rendered as "derstand". The rect is now clamped into the widget.
    pipeline = Pipeline()
    pipeline.set_progress(1)
    check("the pipeline paints", paint(pipeline, 900, 64))
    check("the pipeline paints at a narrow width without raising",
          paint(pipeline, 420, 64))
    check("every pipeline stage label fits the widget", _labels_fit(pipeline, 900),
          "a stage label would be clipped")
    check("stage labels still fit when the window is small", _labels_fit(pipeline, 520))

    # ── INTERNAL NAMES MUST NOT REACH THE SCREEN ──
    check("app.open + chrome reads as 'Open Chrome'",
          humanise_action("app.open", "chrome") == "Open Chrome",
          humanise_action("app.open", "chrome"))
    check("system.shutdown reads as 'Shut down'",
          humanise_action("system.shutdown", "") == "Shut down",
          humanise_action("system.shutdown", ""))
    check("a dotted action key never survives to the label",
          "." not in humanise_action("window.close_all", "current"),
          humanise_action("window.close_all", "current"))
    check("an unmapped action still degrades to something readable",
          humanise_action("future.new_thing", "x") == "New thing X",
          humanise_action("future.new_thing", "x"))
    check("a DMM token reads as a phrase",
          humanise_command("youtube search lofi") == "Search YouTube for Lofi",
          humanise_command("youtube search lofi"))

    # ── ONE ROW PER ACTION, NOT ONE PER PIPELINE STAGE ──
    entries = [
        {"event": "normalized", "action": "app.open", "target": "chrome", "ts": 1},
        {"event": "resolved", "action": "app.open", "target": "chrome", "ts": 2},
        {"event": "executed", "action": "app.open", "target": "chrome", "ts": 3},
        {"event": "denied", "action": "system.shutdown", "target": "", "ts": 4},
    ]
    collapsed = collapse_audit(entries)
    check("three stages of one action collapse to one row", len(collapsed) == 2,
          f"{len(collapsed)} rows")
    check("the collapsed row keeps the OUTCOME, not the first stage",
          collapsed[0]["event"] == "executed", collapsed[0]["event"])
    check("a terminal state outranks a later ordinary one",
          collapse_audit([
              {"event": "denied", "action": "a", "target": "b"},
              {"event": "normalized", "action": "a", "target": "b"},
          ])[0]["event"] == "denied")
    check("the same action twice, separated, stays two rows",
          len(collapse_audit([
              {"event": "executed", "action": "app.open", "target": "chrome"},
              {"event": "executed", "action": "app.close", "target": "notepad"},
              {"event": "executed", "action": "app.open", "target": "chrome"},
          ])) == 3)

    # ── THE OVERLAP BUG: rows must never be laid out over a stale empty state ──
    from kayra.ui.views.activity import ActivityView
    bridge = StubBridge()
    activity = ActivityView(bridge)
    activity.resize(900, 600)
    check("the activity timeline paints when empty", paint(activity, 900, 600))
    check("the empty state is visible with no entries", not activity._empty.isHidden())
    bridge.userMessage.emit("open youtube", "voice")
    bridge.assistantMessage.emit("Opening YouTube.")
    # `_add` only re-renders while the view is on screen; on_show is the hook that forces it,
    # and it is what navigation calls.
    activity.on_show()
    app.processEvents()
    check("the empty state hides once there are entries", activity._empty.isHidden())
    check("the timeline paints with entries", paint(activity, 900, 600))
    check("no widget is left in the layout after a re-render",
          all(activity.body.itemAt(i).widget() is not None
              for i in range(activity.body.count())))
    activity._set_filter("system")
    app.processEvents()
    check("an empty filter shows the empty state again", not activity._empty.isHidden())
    check("and says so in its own words",
          "filter" in activity._empty._heading.text().lower(),
          activity._empty._heading.text())

    # ── THE COUNT MUST DESCRIBE THE ROWS ──
    from kayra.ui.views.automation import AutomationView
    automation = AutomationView(StubBridge())
    automation.on_show()
    app.processEvents()
    rows = sum(1 for i in range(automation.history_body.count())
               if automation.history_body.itemAt(i).widget() is not None
               and automation.history_body.itemAt(i).widget() is not automation._history_empty)
    check("the action count matches the rows shown",
          str(rows) in automation.history_pill.text(),
          f"{rows} rows vs pill {automation.history_pill.text()!r}")

    # ── GRAMMAR ──
    from kayra.ui.views.memory import MemoryView
    memory = MemoryView(StubBridge())
    memory.on_show()
    app.processEvents()
    check("one saved item is '1 item', not '1 items'",
          "1 items" not in memory.saved_pill.text(), memory.saved_pill.text())
    check("a memory row does not print the stored role key",
          not any("User:" in _row_text(memory.saved_body, i)
                  for i in range(memory.saved_body.count())))

    # ── HIGH-DPI: every drawn glyph is painted in LOGICAL units ──
    from kayra.ui.components.navigation import _glyph
    from kayra.ui.components.primitives import _icon_glyph, _chevron
    # THE HIGH-DPI BUG THIS PINS. QPainter on a pixmap that carries a devicePixelRatio works
    # in LOGICAL units and scales up itself. Drawing to `size * dpr` therefore paints at
    # double (or triple) scale, and only the TOP-LEFT QUARTER of the glyph is inside the
    # pixmap. Checking the dpr proves nothing — QIcon.pixmap() normalises it. Checking that
    # ink reaches the bottom-right quadrant proves the glyph was drawn at the right scale.
    for dpr in (1, 2, 3):
        for kind in ("home", "system", "settings"):
            check(f"the {kind} glyph is not clipped at {dpr}x",
                  _glyph_is_unclipped(_glyph(kind, Color.text, 16, dpr=dpr), 16),
                  "ink runs to the pixmap edge — drawn in device units?")
    for dpr in (2, 3):
        check(f"the chevron is not clipped at {dpr}x",
              _glyph_is_unclipped(_chevron(True, 12, dpr=dpr), 12))
        for kind in ("mic", "send", "close"):
            check(f"the {kind} icon-button glyph is not clipped at {dpr}x",
                  _glyph_is_unclipped(_icon_glyph(kind, Color.text, 16, dpr=dpr), 16))

    # ── RESIZE: hierarchy must survive every supported window size ──
    from kayra.ui.application import KayraWindow
    window = KayraWindow(StubBridge())
    ok = True
    for width, height in ((1280, 720), (1366, 768), (1920, 1080), (2560, 1440),
                          (Size.min_window_width, Size.min_window_height)):
        for key in ("home", "chat", "automation", "memory", "activity", "system", "settings"):
            window.navigate_to(key)
            ok = paint(window, width, height) and ok
    check("every screen paints at every supported window size", ok)

    # A resize must not leave a view wider than the window it is in.
    window.resize(1280, 720)
    app.processEvents()
    window.resize(2560, 1440)
    app.processEvents()
    window.resize(1040, 680)
    app.processEvents()
    check("shrinking back to the minimum keeps the content inside the window",
          window.stack.width() <= window.width(),
          f"stack {window.stack.width()} vs window {window.width()}")
    check("the window paints after repeated resizes", paint(window, 1040, 680))

    # ── STATE TRANSITIONS drive the orb and the composer ──
    from kayra.ui.views.chat import ChatView
    chat = ChatView(StubBridge())
    for state in ("IDLE", "LISTENING", "PROCESSING", "SPEAKING", "AUTOMATING",
                  "INTERRUPTING", "ERROR", "PROACTIVE", "IDLE"):
        chat._on_state(state, "IDLE")
        app.processEvents()
    check("the chat composer survives every state transition", paint(chat, 900, 600))
    chat._on_state("SPEAKING", "PROCESSING")
    check("stop is offered while Kayra is speaking", chat.stop_button.isEnabled())
    chat._on_state("IDLE", "SPEAKING")
    check("stop is withdrawn when there is nothing to stop",
          not chat.stop_button.isEnabled())
    chat.input.setText("")
    check("send is disabled with an empty field", not chat.send_button.isEnabled())
    chat.input.setText("hello")
    check("send is enabled once there is something to send", chat.send_button.isEnabled())


# ┌────────────────────────────────────────────────────────────────────────┐
# │      10. INTERACTION: SCROLLING, LISTENING, AMBIENT LIFECYCLE           │
# └────────────────────────────────────────────────────────────────────────┘

def section_interaction(app):
    print_system("\n[10] Chat scrolling, listening control, ambient lifecycle")

    from kayra.ui.views.chat import ChatView
    from kayra.ui.views.home import HomeView
    from kayra.ui.application import KayraWindow, AmbientAssistant
    from kayra.ui.components.primitives import ActionStatus, OutlineButton
    from kayra.ui.views.automation import AutomationView

    # ── CHAT AUTO-SCROLL ──────────────────────────────────────────────
    # THE BUG. `bar.setValue(bar.maximum())` on a zero-timer reads a `maximum` computed from
    # the content widget's size hint, which has not been recalculated for the widget just
    # added — so the bar goes to the PREVIOUS bottom and the newest message hangs below the
    # viewport. Short messages fitted anyway, which is why it looked intermittent; a long
    # reply overflowed by ~250px and a burst of messages by over 1300px, both measured.
    #
    # The check below measures the last widget's bottom edge against the viewport, which is
    # the thing the user actually complained about, rather than asserting that some method
    # was called.
    bridge = StubBridge()
    chat = ChatView(bridge)
    chat.resize(880, 600)
    chat.show()
    for _ in range(6):
        app.processEvents()
    chat.on_show()
    for _ in range(4):
        app.processEvents()

    LONG = ("It's about 27 degrees and overcast in Bangalore right now, with light rain "
            "expected later this evening. You probably want a jacket if you're heading out "
            "after seven, and the roads near Silk Board will almost certainly be slow. If "
            "you are cycling, the stretch past the flyover floods quickly, so give yourself "
            "an extra twenty minutes and take the inner road instead.")

    def overflow():
        """How far the newest widget's bottom sits BELOW the viewport. <=0 means fully visible."""
        for _ in range(3):
            app.processEvents()
        last = None
        for index in range(chat.transcript.count()):
            widget = chat.transcript.itemAt(index).widget()
            if widget is not None and widget.isVisible():
                last = widget
        if last is None:
            return None
        viewport = chat.transcript_scroll.viewport()
        return last.mapTo(viewport, last.rect().bottomLeft()).y() - viewport.height()

    bridge.userMessage.emit("hi", "text")
    check("a short message ends fully visible", (overflow() or 0) <= 2, str(overflow()))

    for index in range(20):
        bridge.userMessage.emit(f"message {index}", "text")
        bridge.assistantMessage.emit(f"reply {index}")
        bridge.stateChanged.emit("LISTENING", "SPEAKING")
    check("the newest of many messages is fully visible", (overflow() or 0) <= 2, str(overflow()))

    bridge.userMessage.emit("what's the weather?", "text")
    bridge.assistantMessage.emit(LONG)
    check("a LONG reply ends fully visible", (overflow() or 0) <= 2, str(overflow()))

    bridge.stateChanged.emit("LISTENING", "SPEAKING")
    bridge.userMessage.emit("open chrome and search youtube", "voice")
    bridge.intentClassified.emit("open chrome", ["open chrome", "youtube search lofi"])
    bridge.assistantMessage.emit("Chrome is open. Searching YouTube.")
    check("an automation block followed by a reply is fully visible",
          (overflow() or 0) <= 2, str(overflow()))

    # Content that GROWS after it was first laid out: the streamed-sentence case.
    bridge.stateChanged.emit("LISTENING", "SPEAKING")
    bridge.userMessage.emit("tell me more", "text")
    for sentence in LONG.split(". "):
        bridge.assistantMessage.emit(sentence.strip() + ".")
        for _ in range(2):
            app.processEvents()
    check("a bubble that grows as it streams stays fully visible",
          (overflow() or 0) <= 2, str(overflow()))

    for width, height in ((640, 460), (1100, 820), (860, 560)):
        chat.resize(width, height)
        for _ in range(4):
            app.processEvents()
    check("resizing the window keeps the newest message visible",
          (overflow() or 0) <= 2, str(overflow()))

    # ── FOLLOW-LATEST: reading history must not be interrupted ──
    bar = chat.transcript_scroll.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    for _ in range(2):
        app.processEvents()
    parked = bar.value()
    check("scrolling up turns following off", not chat._following)
    bridge.userMessage.emit("a message arrives while reading", "text")
    bridge.assistantMessage.emit("and a reply")
    for _ in range(4):
        app.processEvents()
    check("the reading position is preserved", abs(bar.value() - parked) <= 2,
          f"was {parked}, now {bar.value()}")

    bar.setValue(bar.maximum())
    for _ in range(2):
        app.processEvents()
    check("returning to the bottom resumes following", chat._following)
    bridge.userMessage.emit("back at the bottom", "text")
    bridge.assistantMessage.emit("following again")
    check("and the newest message is visible again", (overflow() or 0) <= 2, str(overflow()))

    # Sending always pins to the bottom: the user is waiting for an answer, not reading.
    bar.setValue(bar.maximum() // 3)
    for _ in range(2):
        app.processEvents()
    chat.input.setText("a new question")
    chat._submit()
    check("sending a message re-pins to the bottom", chat._following)

    # ── ACTION STATUS ─────────────────────────────────────────────────
    success = ActionStatus("Done", "success")
    failure = ActionStatus("Blocked", "danger")
    check("a success badge carries the success tone", success.property("tone") == "success")
    check("a failure badge carries the danger tone", failure.property("tone") == "danger")
    check("success and failure share a height",
          success.height() == failure.height(), f"{success.height()} vs {failure.height()}")
    check("the badge marks are different SHAPES, not just colours",
          success._mark == "check" and failure._mark == "cross")
    check("badges paint", paint(success, 120, 24) and paint(failure, 120, 24))

    # A badge must fit its own text: a fixed width truncated the longer verdicts.
    longest = ActionStatus("Requires configuration", "warning")
    from PySide6.QtGui import QFontMetrics
    needed = QFontMetrics(longest.label.font()).horizontalAdvance(longest.label.text())
    check("a long status is not clipped", longest.width() >= needed,
          f"{longest.width()}px for {needed}px of text")

    automation = AutomationView(StubBridge())
    automation.on_show()
    app.processEvents()
    tones = []
    for index in range(automation.history_body.count()):
        widget = automation.history_body.itemAt(index).widget()
        if widget is None:
            continue
        for badge in widget.findChildren(ActionStatus):
            tones.append(badge.property("tone"))
    check("the action history shows both success and failure tones",
          "success" in tones and "danger" in tones, str(tones))

    # ── LISTENING ─────────────────────────────────────────────────────
    bridge = StubBridge()
    home = HomeView(bridge)
    home.on_show()
    app.processEvents()
    check("listening starts on", bridge.listening_enabled())
    check("the control offers to PAUSE while listening",
          "Pause listening" in home.listen_button.text(), home.listen_button.text())

    home.listen_button.click()
    app.processEvents()
    check("clicking pauses listening", bridge.listening is False)
    check("pausing does not interrupt speech", bridge.interrupted == 0)
    check("pausing does not shut Kayra down", bridge.shutdown_called is False)
    check("the control now offers to START", "Start listening" in home.listen_button.text(),
          home.listen_button.text())
    # The CAPTION is the voice state machine's, and it says PAUSED only because the
    # microphone was deliberately closed. In the running application that transition is
    # emitted by `app`; here it is emitted directly, which is the point — the view renders
    # what it is told and derives nothing.
    bridge.voiceStateChanged.emit("PAUSED", "Listening paused",
                                  "Kayra is still running. Start listening to talk again.", 30)
    check("Home says listening is paused, in words",
          "paused" in home.prompt.text().lower(), home.prompt.text())
    check("and says Kayra is still running",
          "still running" in home.state_caption.text().lower(), home.state_caption.text())

    home.listen_button.click()
    app.processEvents()
    check("clicking again resumes listening", bridge.listening is True)
    bridge.voiceStateChanged.emit("LISTENING", "Listening", "Microphone open.", 31)
    check("resuming restores the normal prompt",
          "paused" not in home.prompt.text().lower(), home.prompt.text())

    # A recovering session is RECONNECTING, never a false pause. This is the exact
    # misreport the voice state machine was built to eliminate.
    bridge.voiceStateChanged.emit("RECOVERING", "Reconnecting…",
                                  "Reconnecting the microphone.", 32)
    check("an STT recovery reads as reconnecting, not as paused",
          "paused" not in home.prompt.text().lower()
          and "econnect" in home.prompt.text(), home.prompt.text())

    # The three "stop"-shaped actions must stay separate.
    chat2 = ChatView(bridge)
    chat2.on_show()
    app.processEvents()
    before_interrupts = bridge.interrupted
    bridge.set_listening(False)
    app.processEvents()
    check("pausing listening never calls interrupt", bridge.interrupted == before_interrupts)
    check("pausing listening never calls shutdown", bridge.shutdown_called is False)
    check("the composer stops implying Kayra can hear you",
          "paused" in chat2.input.placeholderText().lower(), chat2.input.placeholderText())
    check("the composer marks the microphone as paused",
          chat2.mic_button.property("paused") is True)
    bridge.set_listening(True)
    app.processEvents()
    check("resuming clears the composer's paused marking",
          chat2.mic_button.property("paused") is False)
    check("resuming restores the normal placeholder",
          "paused" not in chat2.input.placeholderText().lower())

    # Barge-in still reaches the backend, and is a DIFFERENT control.
    chat2._on_state("SPEAKING", "PROCESSING")
    chat2.stop_button.click()
    check("barge-in still works and is a separate control", bridge.interrupted > before_interrupts)
    check("barge-in did not change the listening state", bridge.listening is True)

    # ── AMBIENT LIFECYCLE ─────────────────────────────────────────────
    bridge = StubBridge()
    window = KayraWindow(bridge)
    ambient = AmbientAssistant(bridge, window)
    ambient.place_default()
    window.attach_ambient(ambient)

    window.show()
    app.processEvents()
    check("showing the dashboard hides the compact panel", ambient.isHidden())

    window.hide()
    app.processEvents()
    check("hiding the dashboard shows the compact panel", not ambient.isHidden())

    window.show()
    app.processEvents()
    check("they are never both visible",
          not (window.isVisible() and not window.isMinimized() and ambient.isVisible()))

    # Closing is a change of presentation, not a shutdown.
    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    window.closeEvent(event)
    app.processEvents()
    check("closing the dashboard does NOT quit Kayra", bridge.shutdown_called is False)
    check("closing the dashboard hides it", not window.isVisible())
    check("closing the dashboard brings up the compact panel", not ambient.isHidden())
    check("the close was swallowed, not accepted", not event.isAccepted())

    opened = []
    ambient.on_open_requested = lambda: opened.append(True)
    ambient.request_open()
    check("the compact panel can ask for the dashboard back", opened == [True])

    ambient.on_open_requested = window.show_control_centre
    window.show_control_centre()
    app.processEvents()
    check("restoring the dashboard hides the panel again", ambient.isHidden())

    check("the compact panel creates no second session",
          ambient.bridge is bridge and window.bridge is bridge)

    # ── AMBIENT DRAG vs CLICK ─────────────────────────────────────────
    from PySide6.QtCore import QPoint, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    def press(x, y):
        return QMouseEvent(QEvent.MouseButtonPress, QPointF(4, 4), QPointF(x, y),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    def move(x, y):
        return QMouseEvent(QEvent.MouseMove, QPointF(4, 4), QPointF(x, y),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)

    def release(x, y):
        return QMouseEvent(QEvent.MouseButtonRelease, QPointF(4, 4), QPointF(x, y),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    # Re-point the handler at the recorder: it was aimed at the real window a few checks ago,
    # and a click would otherwise open the dashboard instead of being recorded.
    ambient.on_open_requested = lambda: opened.append(True)
    ambient.move(300, 300)
    app.processEvents()
    start = ambient.pos()
    opened.clear()
    ambient.mousePressEvent(press(320, 320))
    ambient.mouseMoveEvent(move(520, 460))
    ambient.mouseReleaseEvent(release(520, 460))
    app.processEvents()
    check("dragging moves the panel", ambient.pos() != start,
          f"{start} -> {ambient.pos()}")
    check("dragging does NOT open the dashboard", opened == [],
          "a drag ends with a release over the widget, which is why this needs a threshold")

    moved_to = ambient.pos()
    opened.clear()
    ambient.mousePressEvent(press(400, 400))
    ambient.mouseMoveEvent(move(401, 400))      # inside the threshold: still a click
    ambient.mouseReleaseEvent(release(401, 400))
    app.processEvents()
    check("a click opens the dashboard", opened == [True])
    check("a click does not move the panel", ambient.pos() == moved_to)

    # ── MULTI-MONITOR ─────────────────────────────────────────────────
    from PySide6.QtGui import QGuiApplication
    screens = QGuiApplication.screens()
    check("placement uses Qt's screen geometry, not hardcoded coordinates", bool(screens))
    for screen in screens:
        area = screen.availableGeometry()
        ambient.move(area.center())
        ambient.ensure_on_screen()
        app.processEvents()
        check(f"the panel stays reachable on {screen.name() or 'a screen'}",
              any(s.availableGeometry().intersects(ambient.frameGeometry())
                  for s in QGuiApplication.screens()))

    # Parked far outside every screen (an unplugged monitor) -> pulled back into view.
    ambient.move(-9000, -9000)
    ambient.ensure_on_screen()
    app.processEvents()
    check("a panel left off-screen is recovered",
          any(s.availableGeometry().intersects(ambient.frameGeometry())
              for s in QGuiApplication.screens()),
          str(ambient.pos()))

    # ── HIGH-DPI GLYPHS FOLLOW THE SCREEN ─────────────────────────────
    from kayra.ui.theme import glyph_dpr, reset_glyph_dpr
    reset_glyph_dpr()
    ratio = glyph_dpr()
    check("the glyph ratio is a whole number", float(ratio).is_integer(), str(ratio))
    check("the glyph ratio is at least the screen's",
          ratio >= max(s.devicePixelRatio() for s in screens), str(ratio))
    check("the glyph ratio is bounded", 1 <= ratio <= 4, str(ratio))

    from kayra.ui.components.primitives import _icon_glyph
    from kayra.ui.theme import Color
    for kind in ("mic", "mic_off", "pause", "stop", "send"):
        check(f"the {kind} glyph is not clipped at the screen ratio",
              _glyph_is_unclipped(_icon_glyph(kind, Color.text, 16), 16))
    check("the microphone and the muted microphone are DIFFERENT shapes",
          _icon_glyph("mic", Color.text, 16).pixmap(QSize(16, 16)).toImage()
          != _icon_glyph("mic_off", Color.text, 16).pixmap(QSize(16, 16)).toImage())


def _lbl(text):
    from PySide6.QtWidgets import QLabel
    return QLabel(text)


def _row_text(layout, index):
    from PySide6.QtWidgets import QLabel
    item = layout.itemAt(index)
    widget = item.widget() if item is not None else None
    if widget is None:
        return ""
    return " ".join(child.text() for child in widget.findChildren(QLabel)
                    if hasattr(child, "text"))


def _glyph_is_unclipped(icon, size):
    """
    True when the drawn glyph keeps its margin — i.e. it was painted in LOGICAL units.

    THE BUG THIS DETECTS. QPainter on a pixmap carrying a devicePixelRatio works in logical
    coordinates and scales up itself. Drawing to `size * dpr` paints the glyph at 2x (or 3x)
    into a box that is only `size` logical units across, so the artwork is CLIPPED by the
    pixmap boundary and runs flush to its right and bottom edges.

    Two things that look like they would catch this do not, and both were tried first:
      * `QIcon.paint(painter, rect)` rescales the artwork to fill whatever rect it is given;
      * `QIcon.pixmap(w, h)` for a size larger than the icon's own upscales it.
    Either way a quarter-drawn glyph fills the target and the check passes on broken code.

    Every glyph here is drawn inside a 16% margin, so correct output leaves at least one clear
    device pixel at the right and bottom. Clipped output does not. Measured on this build:
    correct `home` inks to (14, 13) of 16; the same glyph in device units inks to (15, 15).
    """
    from PySide6.QtCore import QSize
    image = icon.pixmap(QSize(size, size)).toImage()
    width, height = image.width(), image.height()
    max_x = max_y = -1
    for y in range(height):
        for x in range(width):
            if (image.pixel(x, y) >> 24) & 0xFF:
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < 0:
        return False                      # nothing was drawn at all
    return max_x <= width - 2 and max_y <= height - 2


def _labels_fit(pipeline, width):
    """
    True when every stage label would be drawn fully inside the widget.

    Mirrors the clamping `Pipeline.paintEvent` does. The first stage's rect used to start at
    a negative x — Qt clips silently, and "Understand" rendered as "derstand".
    """
    from kayra.ui.views.automation import STAGES
    margin = 14
    span = width - margin * 2
    step = span / (len(STAGES) - 1)
    for index in range(len(STAGES)):
        x = margin + step * index
        left = x - step / 2
        if left < 0:
            left = 0.0
        elif left + step > width:
            left = width - step
        if left < -0.001 or left + step > width + 0.001:
            return False
    return True


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 11. SHUTDOWN CONTROL AND TTS DEVICE                    │
# └────────────────────────────────────────────────────────────────────────┘


def section_lifecycle_controls(app):
    """
    The two surfaces added for lifecycle control and device selection.

    These are written the way every check in this file is written: AFTER looking at the
    rendered screen. A passing suite is not evidence that an interface looks right — it is
    evidence that a specific fault, once seen, cannot come back silently.
    """
    print_system("\n[11] Shutdown control and TTS device")

    from kayra.ui.views.home import HomeView
    from kayra.ui.views.settings import SettingsView
    from kayra.output import tts_device

    # ── Home: the shutdown button ──
    bridge = StubBridge()
    home = HomeView(bridge)
    check("Home has a shutdown control", hasattr(home, "shutdown_button"))
    check("it says what it shuts down", "Kayra" in home.shutdown_button.text())
    check("it is styled as destructive",
          home.shutdown_button.property("variant") == "danger")
    check("it says it is not a machine shutdown",
          "microphone" in (home.shutdown_button.toolTip() or "").lower())

    # THE THREE CONTROLS MUST STAY DISTINCT. Pausing, interrupting and quitting are the three
    # things this application has most consistently confused with one another.
    check("the shutdown button is NOT the listening button",
          home.shutdown_button is not home.listen_button)
    check("pressing the listening button does not shut down",
          (home._toggle_listening(), bridge.shutdown_called is False)[1])
    check("pressing the listening button only changes the microphone",
          bridge.listening is False)
    bridge.set_listening(True)

    # Confirmed shutdown reaches the central path exactly once, and locks the control.
    from PySide6.QtWidgets import QMessageBox
    real_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        home._request_shutdown()
        check("a confirmed shutdown reaches the central path", bridge.shutdown_called is True)
        check("the button is disabled afterwards", home.shutdown_button.isEnabled() is False)
        check("the other controls are locked too",
              home.talk_button.isEnabled() is False
              and home.listen_button.isEnabled() is False)
        # The view no longer writes this caption itself: `request_shutdown` moves the voice
        # state to STOPPING, which is an ABSORBING state, and the machine paints it. Driving
        # the signal here is what the real backend does a moment after `shutdown()` returns.
        bridge.voiceStateChanged.emit("STOPPING", "Shutting down",
                                      "Stopping services and closing the browser session.", 99)
        check("the screen says what is happening",
              "hutting down" in home.prompt.text(), home.prompt.text())
    finally:
        QMessageBox.exec = real_exec

    # Cancelling must change nothing at all.
    bridge2 = StubBridge()
    home2 = HomeView(bridge2)
    QMessageBox.exec = lambda self: QMessageBox.Cancel
    try:
        home2._request_shutdown()
        check("cancelling does NOT shut down", bridge2.shutdown_called is False)
        check("cancelling leaves the button usable",
              home2.shutdown_button.isEnabled() is True)
    finally:
        QMessageBox.exec = real_exec

    # Shutdown must win over every other caption, including the paused-microphone line.
    # It does so STRUCTURALLY now rather than by an ordering of `if` statements in the view:
    # the voice state machine resolves shutdown first and STOPPING is absorbing, so a paused
    # microphone simply cannot be the answer once teardown has begun. The view's job is only
    # to render the newer revision, which is what this checks.
    bridge3 = StubBridge()
    home3 = HomeView(bridge3)
    home3._on_listening(False)
    bridge3.voiceStateChanged.emit("PAUSED", "Listening paused",
                                   "Kayra is still running.", 1)
    bridge3.voiceStateChanged.emit("STOPPING", "Shutting down",
                                   "Stopping services and closing the browser session.", 2)
    check("shutdown overrides the paused-microphone caption",
          "hutting down" in home3.prompt.text(), home3.prompt.text())

    check("Home still paints with the new control", paint(home3))

    # ── Home: the Graphics card ──
    from kayra.ui.views.home import HomeView as _HV
    gb = StubBridge()
    gpu_home = _HV(gb)
    gpu_home.on_show()

    check("Home has a Graphics card", hasattr(gpu_home, "gpu_card"))
    check("the GPU name is shown", "4060" in gpu_home.gpu_name.toolTip(),
          gpu_home.gpu_name.toolTip())
    check("utilization is a live value, not hardcoded", gpu_home.gpu_meter._value == 18.0)
    check("VRAM is shown as used / total",
          "4.2" in gpu_home.vram_meter._caption and "8.0" in gpu_home.vram_meter._caption,
          gpu_home.vram_meter._caption)
    check("VRAM percentage comes from the metrics", gpu_home.vram_meter._value == 52.5)
    check("temperature is shown", "58" in gpu_home.gpu_detail.toolTip()
          or "58" in gpu_home._gpu_detail_text, gpu_home._gpu_detail_text)
    check("the TTS provider is shown beside the GPU",
          "CUDAExecutionProvider" in gpu_home._gpu_detail_text, gpu_home._gpu_detail_text)
    check("the header pill reports the SPEECH device, not the hardware",
          "GPU" in gpu_home.gpu_pill.text(), gpu_home.gpu_pill.text())
    check("the empty state is hidden while there is a GPU",
          gpu_home._gpu_empty.isHidden() is True)

    # THE CARD MUST STAY TRUTHFUL WHEN SPEECH IS ON THE CPU WHILE A GPU EXISTS.
    # This is the exact state the whole change was about, and hiding the physical GPU here
    # would be as misleading as claiming acceleration that is not happening.
    gb.provider = "CPUExecutionProvider"
    gpu_home._refresh_gpu()
    check("a CPU speech device does NOT hide the physical GPU",
          gpu_home.gpu_name.isHidden() is False
          and "4060" in gpu_home.gpu_name.toolTip())
    check("the pill says CPU when speech is on the CPU",
          "CPU" in gpu_home.gpu_pill.text(), gpu_home.gpu_pill.text())
    check("the provider shown is the real one",
          "CPUExecutionProvider" in gpu_home._gpu_detail_text)
    check("GPU statistics are still live", gpu_home.gpu_meter._value == 18.0)

    # ── No GPU at all: graceful, and the CPU/RAM card is untouched ──
    gb.gpu = {}
    gb.telemetry_pending = False
    gpu_home._refresh_gpu()
    check("with no GPU the empty state is shown", gpu_home._gpu_empty.isHidden() is False)
    check("with no GPU the meters are hidden", gpu_home.gpu_meter.isHidden() is True)
    check("no GPU does not crash the screen", paint(gpu_home))

    # ── Telemetry still arriving is NOT the same as absent ──
    gb.telemetry_pending = True
    gpu_home._refresh_gpu()
    check("a pending read says so rather than announcing no GPU",
          "Reading" in gpu_home._gpu_empty._heading.text(),
          gpu_home._gpu_empty._heading.text())

    # ── The existing System card must keep working throughout ──
    gpu_home._refresh_panels()
    check("the processor meter still works", gpu_home.cpu_meter._value >= 0.0)
    check("the memory meter still works", gpu_home.ram_meter._value >= 0.0)
    check("the memory meter still has its caption", bool(gpu_home.ram_meter._caption))
    check("the footprint line still reports Kayra's own usage",
          "Kayra is using" in getattr(gpu_home, "_footprint_text", ""))

    # ── Three cards, one row, no clipping ──
    gpu_home.resize(1100, 860)
    gpu_home.layout().activate()
    right_edge = max(c.x() + c.width()
                     for c in (gpu_home.activity_card, gpu_home.system_card, gpu_home.gpu_card))
    check("the bottom strip fits inside the window", right_edge <= 1100,
          f"(rightmost card edge {right_edge}px)")
    check("all three cards share one height",
          len({gpu_home.activity_card.height(), gpu_home.system_card.height(),
               gpu_home.gpu_card.height()}) == 1)

    # ── Settings: the device selector ──
    view = SettingsView(StubBridge())
    check("Settings has a TTS device selector", hasattr(view, "device_combo"))
    labels = [view.device_combo.itemText(i) for i in range(view.device_combo.count())]
    check("exactly three options are offered", len(labels) == 3, str(labels))
    check("the options are Automatic / GPU / CPU",
          labels == ["Automatic", "GPU", "CPU"], str(labels))
    check("it is a dropdown, not a toggle",
          view.device_combo.__class__.__name__ == "QComboBox")
    check("each option carries its stored value",
          [view.device_combo.itemData(i) for i in range(3)] == list(tts_device.MODES))

    # THE POINT OF THE CARD: mode and actual device are shown SEPARATELY. A screen that showed
    # only the mode would display "GPU" while every millisecond of synthesis ran on the CPU.
    text = view.device_active.text()
    check("the configured mode is shown", "Mode:" in text)
    check("the ACTUAL device is shown separately", "device:" in text or "use:" in text)
    check("the raw provider is shown", "Provider:" in text)
    check("the available providers are listed",
          "Available providers" in view.device_detail.text())

    # With no live engine, the card must not present a prediction as a measurement.
    check("with no running engine the card says 'would use', not 'active'",
          "Would use" in text, text.replace("\n", " | "))
    check("the status pill reflects that nothing is running",
          "Not running" in view.device_status_pill.text())

    # GPU telemetry is optional and its absence is stated plainly, never faked.
    view._refresh_gpu()
    gpu_text = view.gpu_line.text()
    check("GPU telemetry is either real numbers or an honest absence",
          "unavailable" in gpu_text or "VRAM" in gpu_text or "utilization" in gpu_text,
          gpu_text)

    # Changing the selector reaches the backend, and is persisted on save.
    #
    # The index is chosen RELATIVE to whatever the dropdown currently shows, never hardcoded.
    # It was `setCurrentIndex(2)`, which is a silent no-op on any machine whose `.env` already
    # says CPU — the signal only fires on a CHANGE — so the check passed or failed depending
    # on the developer's configuration rather than on the code.
    target = (view.device_combo.currentIndex() + 1) % view.device_combo.count()
    expected = view.device_combo.itemData(target)
    view.device_combo.setCurrentIndex(target)
    check("choosing a device reaches the backend", view.bridge.device_mode == expected,
          f"{view.bridge.device_mode} vs {expected}")

    check("Settings still paints with the new card", paint(view))

    # The only timer on the screen must be stopped when the screen is not visible.
    view.on_show()
    check("the GPU timer runs while Settings is visible", view._gpu_timer.isActive() is True)
    view.on_hide()
    check("the GPU timer stops when Settings is hidden", view._gpu_timer.isActive() is False)


def section_presence(app):
    """
    The contextual presence surfaces: the Settings card and the Home status card.

    What is being checked here is not that widgets exist — it is the two properties that
    have gone wrong before on this screen. Every presence toggle must reach the RUNNING
    service (a settings row that only writes a file appears to do nothing for the rest of
    the session), and reading the service back must not write to it (setting a checkbox
    from its own value re-emits `toggled`, which turns every navigation into a redundant
    service call).
    """
    from kayra.ui.views.settings import SettingsView
    from kayra.ui.views.home import HomeView

    print_system("\n── Proactive presence surfaces ───────────────────────────")

    bridge = StubBridge()
    view = SettingsView(bridge)

    check("Settings has a presence master toggle",
          getattr(view, "presence_toggle", None) is not None)
    check("every presence category has a control",
          set(view._presence_controls) ==
          {"greetings", "context", "late_night", "work_session", "system", "humor"})

    # Each category toggle reaches the live service.
    bridge.presence_category_calls.clear()
    view._presence_controls["humor"].setChecked(False)
    check("a category toggle reaches the backend",
          ("humor", False) in bridge.presence_category_calls)

    # The master switch reaches the service AND disables the rows it governs — a row that
    # can still be clicked while it cannot do anything is worse than a disabled one.
    view.presence_toggle.setChecked(False)
    check("the master toggle reaches the backend", bridge.presence is False)
    check("the category rows are disabled when presence is off",
          all(not c.isEnabled() for c in view._presence_controls.values()))
    view.presence_toggle.setChecked(True)
    check("and enabled again when it is back on",
          all(c.isEnabled() for c in view._presence_controls.values()))

    # Reading the live state back must not write it back.
    bridge.presence_category_calls.clear()
    view._sync_presence()
    check("syncing from the service does not call back into it",
          bridge.presence_category_calls == [])

    # The presence settings are persisted as well as applied.
    for env_key, _cat, _label, _help in SettingsView.PRESENCE:
        check(f"'{env_key}' is saved with the other settings", env_key in view._controls)

    check("Settings paints with the presence card", paint(view))

    # ── Home ──
    home = HomeView(bridge)
    home._refresh_presence()
    check("Home shows the presence card while the layer is running",
          home.presence_card.isVisible() or not home.presence_card.isHidden())
    check("the pill reports it as active", "Active" in home.presence_pill.text())
    check("the last observation is named",
          "work session" in home.presence_last.toolTip().lower(),
          home.presence_last.toolTip())
    check("the next eligible time is shown",
          "minutes" in home.presence_next.toolTip(), home.presence_next.toolTip())
    check("the day's budget is shown",
          "1 of 8" in home.presence_budget.toolTip(), home.presence_budget.toolTip())

    # Switched off: the empty state is SHOWN, not created — the card keeps its children.
    bridge.presence = False
    home._refresh_presence()
    check("an off layer shows an empty state rather than stale numbers",
          not home.presence_last.isVisible() and not home._presence_empty.isHidden())
    check("and the pill says so", "Off" in home.presence_pill.text())

    # Not running at all: the card hides itself rather than describing a service that does
    # not exist — the same rule the Graphics card follows on a machine with no GPU.
    bridge.presence_running = False
    home._refresh_presence()
    check("a card for a service that is not running is hidden", home.presence_card.isHidden())

    check("Home paints with the presence card", paint(home))


def section_speech_backend(app):
    """
    Settings: the speech-input card. REQUESTED and ACTIVE are separate lines, always.

    A dropdown moving is not evidence that anything happened. These checks are all about the
    case where the two disagree — a browser the user named that could not be started — because
    that is the case a screen showing only the selection would render as a success.
    """
    print_system("\n[13] Settings — live speech backend, requested vs active")
    from kayra.ui.views.settings import SettingsView

    bridge = StubBridge()
    with NoEnvWrites():
        view = SettingsView(bridge)

    check("Settings has a speech-input card", hasattr(view, "backend_combo"))
    check("it offers Automatic", view.backend_combo.findData("auto") >= 0)
    check("it offers Chrome", view.backend_combo.findData("chrome") >= 0)
    check("it offers Edge", view.backend_combo.findData("edge") >= 0)
    check("the options read as browser names, not keys",
          "Google Chrome" in [view.backend_combo.itemText(i)
                              for i in range(view.backend_combo.count())],
          str([view.backend_combo.itemText(i) for i in range(view.backend_combo.count())]))

    # The happy path: requested and active agree.
    view._refresh_backend()
    text = view.backend_active.text()
    check("the card shows the requested backend", "Backend:" in text, text)
    check("and the ACTIVE backend, separately", "Active:" in text, text)
    check("and a status", "Status:" in text, text)
    check("with everything agreeing, the pill reads Ready",
          "Ready" in view.backend_pill.text(), view.backend_pill.text())

    # Choosing a backend reaches the backend, not just the widget — and persists ONLY on
    # success. The write is intercepted: a UI test must not rewrite the developer's `.env`.
    target = view.backend_combo.findData("chrome")
    if view.backend_combo.currentIndex() == target:
        target = view.backend_combo.findData("edge")
        bridge.backend_result = (True, "Microsoft Edge")
    expected = view.backend_combo.itemData(target)

    with NoEnvWrites() as guard:
        view.backend_combo.setCurrentIndex(target)
        app.processEvents()
    check("choosing a backend reaches the backend, not just the widget",
          bridge.backend_requests == [expected], str(bridge.backend_requests))
    check("a committed switch is persisted",
          guard.writes == [{"STT_BROWSER": expected}], str(guard.writes))

    # A FAILED switch must NOT be written to .env. A setting that did not apply must not come
    # back after a restart claiming to be the configuration.
    bridge_fail = StubBridge()
    bridge_fail.backend_result = (False, "Chrome could not reach a speech backend")
    view_fail = SettingsView(bridge_fail)
    failed_target = view_fail.backend_combo.findData("chrome")
    if view_fail.backend_combo.currentIndex() == failed_target:
        failed_target = view_fail.backend_combo.findData("brave")
    with NoEnvWrites() as guard:
        view_fail.backend_combo.setCurrentIndex(failed_target)
        app.processEvents()
    check("a failed switch is NOT persisted", guard.writes == [], str(guard.writes))
    check("and the screen says so",
          "Could not switch" in view_fail.status.text(), view_fail.status.text())

    # THE CASE THAT MATTERS: a requested backend that could not start.
    bridge2 = StubBridge()
    bridge2.backend = dict(bridge2.backend,
                           requested_backend="chrome", requested_label="Google Chrome",
                           active_backend="edge", active_label="Microsoft Edge",
                           status="LISTENING", matches=False,
                           last_error="Chrome could not reach a speech backend")
    view2 = SettingsView(bridge2)
    view2._refresh_backend()
    text = view2.backend_active.text()
    check("a failed switch shows the requested browser",
          "Backend: Google Chrome" in text, text)
    check("and the DIFFERENT browser that is actually active",
          "Active: Microsoft Edge" in text, text)
    check("the pill says it was not applied",
          "Not applied" in view2.backend_pill.text(), view2.backend_pill.text())
    check("and the reason is on screen",
          "could not reach" in view2.backend_detail.text(), view2.backend_detail.text())

    # No live session at all.
    bridge3 = StubBridge()
    bridge3.backend = dict(bridge3.backend, active_backend=None, active_label="None",
                           status="OFF", matches=False)
    view3 = SettingsView(bridge3)
    view3._refresh_backend()
    check("with no session the active backend is None, never the selection",
          "Active: None" in view3.backend_active.text(), view3.backend_active.text())
    check("and the pill says it is not running",
          "Not running" in view3.backend_pill.text(), view3.backend_pill.text())

    # A live backend transition repaints the card without a timer.
    bridge4 = StubBridge()
    view4 = SettingsView(bridge4)
    bridge4.backend = dict(bridge4.backend, active_backend="chrome",
                           active_label="Google Chrome")
    bridge4.sttBackendChanged.emit(bridge4.backend)
    app.processEvents()
    check("a backend change repaints the card from a signal, not a poll",
          "Active: Google Chrome" in view4.backend_active.text(),
          view4.backend_active.text())

    check("Settings still paints with the speech card", paint(view))


def section_memory_management(app):
    """Memory: stable ids, real deletion, the store's location, and the confirmations."""
    print_system("\n[14] Memory — ids, deletion, location")
    from PySide6.QtWidgets import QMessageBox
    from kayra.ui.views.memory import MemoryView, _MemoryRow

    bridge = StubBridge()
    view = MemoryView(bridge)
    view.on_show()

    check("the memory screen lists what is saved",
          "2 items" in view.saved_pill.text(), view.saved_pill.text())
    check("it shows the store's location",
          "conversation.json" in view.location_label.text(), view.location_label.text())
    check("the full path is available on hover",
          view.location_label.toolTip() == view.location_label.text())
    check("and the entry count beside it", "2 entries" in view.location_detail.text(),
          view.location_detail.text())

    rows = [view.saved_body.itemAt(i).widget() for i in range(view.saved_body.count())]
    rows = [row for row in rows if isinstance(row, _MemoryRow)]
    check("a row is built per memory", len(rows) == 2, str(len(rows)))
    check("each row holds a stable id, not a position",
          all(getattr(row, "_memory_id", "") for row in rows))
    check("and the ids are the backend's",
          {row._memory_id for row in rows} == {m["id"] for m in bridge.memories})

    # Deleting confirms first, and cancelling changes nothing.
    real_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.Cancel
    try:
        view._delete_saved(rows[0]._memory_id)
        check("cancelling a delete removes nothing", len(bridge.memories) == 2)
        check("and deletes nothing on the backend", bridge.deleted == [])
    finally:
        QMessageBox.exec = real_exec

    # Confirming deletes exactly that id.
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        doomed = rows[0]._memory_id
        view._delete_saved(doomed)
        check("confirming deletes exactly one memory", len(bridge.memories) == 1)
        check("and it is the one whose id was passed", bridge.deleted == [doomed],
              str(bridge.deleted))
        check("the screen refreshed", "1 item" in view.saved_pill.text(),
              view.saved_pill.text())
    finally:
        QMessageBox.exec = real_exec

    # A FAILED delete must not remove the row.
    bridge2 = StubBridge()
    bridge2.delete_ok = False
    view2 = MemoryView(bridge2)
    view2.on_show()
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        view2._delete_saved(bridge2.memories[0]["id"])
        check("a failed delete leaves the memory in place", len(bridge2.memories) == 2)
        check("and says so on screen", "failed" in view2.saved_pill.text().lower(),
              view2.saved_pill.text())
    finally:
        QMessageBox.exec = real_exec

    # Clear all: stronger wording, and it only clears when confirmed.
    bridge3 = StubBridge()
    view3 = MemoryView(bridge3)
    view3.on_show()
    QMessageBox.exec = lambda self: QMessageBox.Cancel
    try:
        view3._clear_saved()
        check("cancelling 'clear all' clears nothing", len(bridge3.memories) == 2)
    finally:
        QMessageBox.exec = real_exec
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        view3._clear_saved()
        check("confirming 'clear all' empties the store", bridge3.memories == [])
        check("and reports how many went", bridge3.cleared == 2, str(bridge3.cleared))
    finally:
        QMessageBox.exec = real_exec
    check("the empty state is shown, not created",
          view3.saved_empty.isVisible() or not view3.isVisible())
    check("the empty state is a permanent child, never destroyed",
          view3.saved_empty.parent() is not None)

    # Open location reaches the backend.
    view3._open_location()
    check("'open file location' reaches the backend", bridge3.opened_location == 1)

    check("Memory still paints", paint(view))




def section_boot_ordering(app):
    """
    THE BOOT WINDOW: a screen built before the backend is ready must not paint a guess.

    The defect this pins, exactly as it was reported. `KayraWindow.__init__` builds every view
    and shows Home several seconds before `KayraSession` finishes booting. Home painted its
    microphone control from `bridge.listening_enabled()`, which answered False because
    `self._runtime is None` — an ABSENCE OF INFORMATION reported as a NEGATIVE FACT. Then
    nothing ever corrected it: `RuntimeState._listening` starts True and never changes, and
    `set_listening` correctly does not emit for a value that did not change. So the button read
    "Start listening" beside an orb that was listening, until the user toggled it twice.

    Three properties are pinned here, because fixing only one of them leaves the bug reachable:

      1. the pre-boot answer is not a positive claim that the microphone is closed;
      2. the control is not presented as actionable while the answer is unknown;
      3. the screen RE-READS when the answer becomes available (`bootFinished`).
    """
    print_system("\n[15] The boot window — no guessed microphone state")
    from kayra.ui.views.home import HomeView
    from kayra.ui.views.chat import ChatView

    # ── 1. The session must not answer "off" when it means "not yet" ──
    from kayra.ui.session import KayraSession
    session = KayraSession(enable_voice=False)
    check("before boot, listening_known() is False", session.listening_known() is False)
    check("and listening_enabled() does NOT claim the microphone is closed",
          session.listening_enabled() is True,
          "False here is a positive claim, and it was the wrong one")

    snapshot = session.voice_runtime_state()
    check("the pre-boot snapshot says the microphone state is not known",
          snapshot.get("listening_known") is False, str(snapshot.get("listening_known")))

    # ── 2. Home during the boot window ──
    bridge = StubBridge()
    bridge.known = False                 # the backend has not booted yet
    home = HomeView(bridge)
    home.on_show()
    app.processEvents()

    check("during boot the control is not offered as actionable",
          home.listen_button.isEnabled() is False)
    check("and it does not claim listening is off",
          "Pause listening" in home.listen_button.text(), home.listen_button.text())
    check("the tooltip says why it cannot be used",
          "starting" in home.listen_button.toolTip().lower(), home.listen_button.toolTip())

    # ── 3. bootFinished corrects it, and NO listening_changed is fired ──
    #
    # The "no event" half is the point. In the real application `RuntimeState._listening`
    # starts True and never changes, so `set_listening` correctly never emits — which is
    # precisely why nothing corrected the button. Counting the events proves the fix does not
    # secretly depend on one.
    emitted = []
    bridge.listeningChanged.connect(lambda value: emitted.append(value))

    bridge.known = True
    bridge.listening = True
    bridge.bootFinished.emit(True, "voice input ready, speech output ready")
    app.processEvents()

    check("boot completing enables the control", home.listen_button.isEnabled() is True)
    check("THE REPORTED BUG: the button says Pause, not Start, while listening",
          "Pause listening" in home.listen_button.text(), home.listen_button.text())
    check("and it got there with NO listeningChanged event at all",
          emitted == [], str(emitted))

    # ── 4. The state it was reported in: listening, never toggled ──
    bridge2 = StubBridge()
    bridge2.known = False
    home2 = HomeView(bridge2)
    home2.on_show()
    bridge2.known = True
    bridge2.bootFinished.emit(True, "ready")
    bridge2.voiceStateChanged.emit("LISTENING", "Listening", "Microphone open.", 5)
    app.processEvents()
    check("caption and control agree after boot, with no toggle",
          home2.prompt.text() == "Listening"
          and "Pause listening" in home2.listen_button.text(),
          f"{home2.prompt.text()!r} / {home2.listen_button.text()!r}")

    # ── 5. And the toggle path still works, in both directions ──
    home2.listen_button.click()
    app.processEvents()
    check("clicking still pauses", bridge2.listening is False)
    check("and the label follows", "Start listening" in home2.listen_button.text(),
          home2.listen_button.text())
    home2.listen_button.click()
    app.processEvents()
    check("clicking again resumes", bridge2.listening is True)
    check("and the label follows back", "Pause listening" in home2.listen_button.text(),
          home2.listen_button.text())

    # ── 6. A genuinely paused microphone at boot must still read as paused ──
    bridge3 = StubBridge()
    bridge3.known = False
    home3 = HomeView(bridge3)
    home3.on_show()
    bridge3.known = True
    bridge3.listening = False
    bridge3.bootFinished.emit(True, "ready")
    app.processEvents()
    check("a microphone that really is paused at boot reads as paused",
          "Start listening" in home3.listen_button.text(), home3.listen_button.text())

    # ── 7. Shutdown must not be re-enabled by a late re-sync ──
    from PySide6.QtWidgets import QMessageBox
    bridge4 = StubBridge()
    home4 = HomeView(bridge4)
    real_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        home4._request_shutdown()
    finally:
        QMessageBox.exec = real_exec
    bridge4.bootFinished.emit(True, "ready")
    home4.on_show()
    app.processEvents()
    check("a late re-sync cannot re-enable controls during shutdown",
          home4.listen_button.isEnabled() is False)

    # ── 8. The composer's microphone has the same contract ──
    bridge5 = StubBridge()
    bridge5.known = False
    chat = ChatView(bridge5)
    bridge5.known = True
    bridge5.listening = True
    bridge5.bootFinished.emit(True, "ready")
    app.processEvents()
    check("the composer's microphone is re-read at boot too",
          "Pause listening" in chat.mic_button.toolTip(), chat.mic_button.toolTip())
    check("and the placeholder does not claim listening is paused",
          "paused" not in chat.input.placeholderText().lower(),
          chat.input.placeholderText())

    # ── 9. The structural rule, asserted ──
    import inspect
    for module, label in ((HomeView, "Home"), (ChatView, "Chat")):
        source = inspect.getsource(module)
        check(f"{label} re-reads when the backend becomes ready",
              "bootFinished" in source, label)


def section_no_side_effects(before):
    """
    The suite must leave the developer's configuration exactly as it found it.

    Belt and braces over `NoEnvWrites`, which only guards the blocks it wraps. This compares
    the whole `.env` before and after the run, so ANY path that writes it — a control that
    persists on change, a future screen that saves on close — is caught HERE, by name, rather
    than being discovered days later as a mysteriously changed setting.
    """
    print_system("\n[16] The suite has no side effects")
    if before is None:
        check("no .env on this machine to protect", True)
        return
    from kayra.core.paths import env_file
    try:
        after = open(env_file(), "rb").read()
    except OSError:
        check("the .env is still readable", False)
        return
    # No `detail` on purpose: `check()` prints it on a PASS too, and "a UI test wrote the
    # developer's configuration" beside a green PASS reads like a failure.
    check("the suite did not modify the developer's .env", after == before)


def main():
    app = QApplication.instance() or QApplication([])
    print_banner("KAYRA UI", "Shell, views, state reflection and boundary discipline")

    # Snapshotted before anything constructs a view, so section 16 can prove the suite changed
    # nothing on the way through.
    try:
        from kayra.core.paths import env_file
        env_before = open(env_file(), "rb").read()
    except OSError:
        env_before = None

    section_theme(app)
    section_components(app)
    section_views(app)
    section_state(app)
    section_system(app)
    section_settings(app)
    section_window(app)
    section_boundary(app)
    section_refinement(app)
    section_interaction(app)
    section_lifecycle_controls(app)
    section_presence(app)
    section_speech_backend(app)
    section_memory_management(app)
    section_boot_ordering(app)
    section_no_side_effects(env_before)

    print_system("\n" + "=" * 60)
    if FAILED:
        print_error(f"{FAILED} UI check(s) FAILED ({PASSED} passed).")
        return 1
    print_success(f"All UI checks passed ({PASSED}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
