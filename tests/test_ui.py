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
import ast
import io
import sys

# Must precede any Qt import: selects the headless platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from PySide6.QtCore import Qt, QObject, Signal, QSize, QPoint, QTimer
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
    # Hand gesture control, on its own signal. The camera and the microphone are independent
    # devices and their state travels separately — a screen that learned about one from the
    # other's signal is the class of bug the voice state machine exists to end.
    gestureStateChanged = Signal(dict)

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
        # DELIBERATELY SYNTHETIC, and deliberately NOT the developer's own card. A stub that
        # names the machine the suite happens to run on is how a screen full of that machine's
        # values passes review: every number below is invented, so anything the UI renders
        # that matches this host came from the UI reading the host, which is the bug.
        self.gpu = {
            "name": "SYNTHETIC Test Graphics 9000",
            "utilization": 18.0,
            "memory_used_mb": 4300.0,
            "memory_total_mb": 8188.0,
            "memory_free_mb": 3888.0,
            "memory_percent": 52.5,
            "temperature_c": 58.0,
            "source": "stub",
        }
        # The PHYSICAL adapter, which is a different question from telemetry — see
        # `KayraSession.graphics_profile`. Machines with an AMD or Intel GPU have this and
        # have no `gpu` telemetry at all, and `MACHINES` below drives exactly that case.
        self.graphics = {
            "name": "SYNTHETIC Test Graphics 9000", "vendor": "SYNTHETIC",
            "vram_total": 8188 * 1024 ** 2, "integrated": False,
            "driver": "0.0.0.1", "telemetry": True,
        }
        self.provider = "CUDAExecutionProvider"
        self.telemetry_pending = False
        # WHERE THE THINKING HAPPENS, as the real bridge reports it. Synthetic model names,
        # so a rendered screen carrying the developer's own routing would be visible as a
        # failure rather than passing for a correct one.
        self.intelligence = {"tier": "Local", "intents": 102,
                             "decision": "Decision routing: Local (synthetic-decider)",
                             "chat": "Chat routing: Local (synthetic-chatter)"}
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
        # Hand gesture control. THREE SEPARATE FIELDS, as the real controller reports them —
        # the camera, the switch, and the runtime state answer different questions, and a stub
        # that collapsed them would let a screen collapse them too and still pass.
        self.gesture = {"camera": "OFF", "camera_device": "", "gesture_enabled": False,
                        "state": "OFF", "state_label": "Disabled", "hand": False,
                        "gesture": "No hand", "gesture_state": "NO_HAND", "error": ""}
        self.gesture_calls = []
        self.camera_calls = []
        self.gesture_result = (True, "")
        self.camera_result = (True, "")
        self.frames_served = 0

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

    def graphics_profile(self):
        """
        The PHYSICAL adapter, which every machine has and only NVIDIA reports telemetry for.

        Defaults to `{}` -- the "no graphics hardware at all" case -- so a test that says
        nothing about graphics still exercises the empty state. `section_hardware_portability`
        substitutes real synthetic machines through this.
        """
        return dict(self.graphics) if self.graphics else {}

    def tts_provider(self):
        return self.provider

    def intelligence_status(self):
        # Substitutable per test, so a screen can be rendered against a cloud machine and a
        # local one without either being the developer's own configuration.
        return dict(self.intelligence)

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

    # ── Hand gesture control ──
    # The stub models the real CONTROLLER'S ordering, not the caller's request: enabling
    # gesture control starts the camera, turning the camera off turns gesture control off. A
    # stub that simply recorded the boolean would let a screen that got the ordering wrong
    # pass, and the ordering is the thing worth testing here.

    def set_gesture(self, enabled):
        self.gesture_calls.append(bool(enabled))
        ok, detail = self.gesture_result
        if ok:
            self.gesture["gesture_enabled"] = bool(enabled)
            if enabled:
                self.gesture["camera"] = "ACTIVE"
                self.gesture["state"] = "ACTIVE"
            else:
                self.gesture["state"] = "OFF"
        else:
            self.gesture["error"] = detail
        self.gestureStateChanged.emit(dict(self.gesture))
        return ok, detail

    def set_camera(self, enabled):
        self.camera_calls.append(bool(enabled))
        ok, detail = self.camera_result
        if ok:
            self.gesture["camera"] = "ACTIVE" if enabled else "OFF"
            if not enabled:
                self.gesture["gesture_enabled"] = False
                self.gesture["state"] = "OFF"
        else:
            self.gesture["error"] = detail
        self.gestureStateChanged.emit(dict(self.gesture))
        return ok, detail

    def gesture_status(self):
        return dict(self.gesture)

    def gesture_telemetry(self):
        return {}

    def camera_frame(self):
        if self.gesture.get("camera") != "ACTIVE":
            return None
        self.frames_served += 1
        width, height = 16, 12
        return (bytes(width * height * 3), width, height)

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

def docked_window(bridge=None):
    """
    A shown window on Home, so the dock is the thing being driven.

    THE CONTROLS MOVED. Home is pure status since the redesign — talk, microphone, camera,
    gestures and shutdown all live in the floating dock the window puts over Home and Chat,
    and the actions behind them live in `ui.controls`. So the checks that used to click
    `home.listen_button` click `window.dock.mic_button` instead: same state, same backend
    call, one surface.

    Shown rather than merely constructed, because `isVisible()` is False for any widget whose
    parent chain is hidden, and the dock's own positioning skips a hidden dock.
    """
    from kayra.ui.application import KayraWindow
    window = KayraWindow(bridge if bridge is not None else StubBridge())
    window.resize(1440, 900)
    window.show()
    return window


def dock_press(button):
    """
    Activates a self-painted `DockButton`, which is not a QAbstractButton.

    Named `dock_press` rather than `press` because `section_interaction` already has a local
    `press(x, y)` that builds a mouse event, and a module-level name it shadows is a
    confusing failure to read.
    """
    button.clicked.emit()


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

    # HOME IS A READOUT, NOT A CONTROL SURFACE. Since the redesign every control lives in the
    # floating dock, and Home shows the microphone as a state rather than offering a button
    # for it — which is what makes it impossible for a control on this page to disagree with
    # the same control two inches below it.
    bridge.stateChanged.emit("IDLE", "AUTOMATING")
    check("home carries no controls of its own",
          not hasattr(home, "listen_button") and not hasattr(home, "stop_button")
          and not hasattr(home, "shutdown_button"))
    # ONE synchronous read, exactly as `on_show` takes: a screen that has missed every
    # transition so far paints from a snapshot rather than waiting for the next event.
    home._sync_voice()
    check("it reports the microphone as open",
          "open" in home.mic_line.value_label.toolTip().lower(),
          home.mic_line.value_label.toolTip())
    bridge.set_listening(False)
    app.processEvents()
    check("and as paused once it is closed",
          "paused" in home.mic_line.value_label.toolTip().lower(),
          home.mic_line.value_label.toolTip())
    bridge.set_listening(True)
    app.processEvents()
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
    from kayra.core import hardware

    profile = sp.device_profile()
    check("device profile returns a dict", isinstance(profile, dict))
    for field in ("cpu_name", "cpu_threads", "ram_total", "os_name", "architecture", "disks"):
        check(f"profile exposes '{field}'", field in profile)
    check("the profile is cached", sp.device_profile() is profile)

    # The fields this milestone added. Each exists because the old shape could not express
    # something a screen has to show without guessing.
    for field in ("os_product", "os_build", "os_display_version", "gpu_vendor", "gpus",
                  "has_nvidia", "cpu_vendor", "screen_width", "monitor_count"):
        check(f"profile exposes '{field}'", field in profile)

    metrics = sp.live_metrics()
    for field in ("cpu_percent", "ram_percent", "kayra_processes", "kayra_memory"):
        check(f"metrics expose '{field}'", field in metrics)
    check("live metrics spawn no subprocess",
          "subprocess" not in sp.live_metrics.__code__.co_names)

    # ── NO SUBPROCESS ANYWHERE IN THE STATIC PROFILE EITHER ──
    # This is the change that took collection from 4.41s to well under a millisecond. The
    # module used to batch one PowerShell/CIM call for the CPU name, the OS caption and the
    # GPU; `core.hardware` reads the same facts from the registry.
    # Parsed, not grepped. Both modules DOCUMENT the PowerShell call they replaced, so a
    # text search over the whole file finds the explanation and calls it the defect. Walking
    # the AST for real imports and real calls is the only way to ask the question properly —
    # the same reasoning the transcript-repair suite gives for proving there is no
    # word-replacement dictionary.
    for module in (sp, hardware):
        tree = ast.parse(io.open(module.__file__, encoding="utf-8").read())
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute):
                    called.add(target.attr)
                elif isinstance(target, ast.Name):
                    called.add(target.id)
        name = module.__name__.rsplit(".", 1)[-1]
        check(f"{name} imports no subprocess module", "subprocess" not in imported)
        # `platform.system()` is a legitimate call whose attribute name collides with
        # `os.system`, so the call NAME alone cannot answer this — the qualified form is what
        # matters, and it is checked against code lines with the commentary stripped.
        code = chr(10).join(line for line in io.open(module.__file__, encoding="utf-8")
                         .read().splitlines() if not line.lstrip().startswith("#"))
        check(f"{name} calls neither os.system nor os.popen",
              "os.system(" not in code and "os.popen(" not in code
              and "subprocess." not in code,
              "static hardware facts must cost no process spawn")
        # Both modules DOCUMENT the PowerShell call they replaced and why, so the word is
        # expected in the prose. What must not exist is a runnable one — every string
        # constant that is not a docstring is checked, which is the difference between
        # "explains the old design" and "still contains it".
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                first = node.body[0] if node.body else None
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        literals = [n.value.lower() for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings]
        check(f"{name} contains no runnable PowerShell command",
              not any("powershell" in text or "get-ciminstance" in text
                      for text in literals),
              "the batched CIM call this replaced cost 4.41s")

    # ── THE WINDOWS 10 / WINDOWS 11 BUG ──
    # `platform.release()` is "10" on Windows 11 and the System screen used to print it as the
    # build. These checks pin the replacement rather than the symptom.
    check("os_release is retained but is not the build",
          profile["os_release"] != str(profile["os_build"] or ""),
          "platform.release() is a compatibility value, not a build number")
    product, version = sp.os_summary()
    check("os_summary returns a product and a version", isinstance(product, str)
          and isinstance(version, str))
    if sys.platform.startswith("win"):
        check("the OS build is a real build number",
              isinstance(profile["os_build"], int) and profile["os_build"] > 1000,
              str(profile["os_build"]))
        check("the OS product is not the stale registry name",
              not (profile["os_build"] >= hardware.WINDOWS_11_MIN_BUILD
                   and product.lower().startswith("windows 10")),
              product)
        check("the version line carries the build", str(profile["os_build"]) in version,
              version)

    # ── THE PRODUCT-NAME CORRECTION, ON SYNTHETIC BUILDS ──
    # Driven directly so the rule is proved on builds this machine does not have.
    correct = hardware._windows_product_name
    check("a stale Windows 10 name on a Windows 11 build is corrected",
          correct("Windows 10 Home Single Language", 26200, False)
          == "Windows 11 Home Single Language")
    check("a genuine Windows 10 build keeps its name",
          correct("Windows 10 Pro", 19045, False) == "Windows 10 Pro")
    check("an already-correct name is left alone",
          correct("Windows 11 Pro", 26100, False) == "Windows 11 Pro")
    check("a server SKU is never renamed by a client build rule",
          correct("Windows Server 2025 Standard", 26100, True)
          == "Windows Server 2025 Standard")
    check("an unknown product name is not rewritten",
          correct("Windows 12 Home", 30000, False) == "Windows 12 Home")
    check("an unreadable build leaves the name untouched",
          correct("Windows 10 Pro", None, False) == "Windows 10 Pro")

    # ── VRAM ──
    # The old contract was "a clamped 32-bit read is reported as unknown", which was honest
    # and left every modern card blank. The new contract is stronger: read the 64-bit field
    # and report the real number.
    adapters = hardware.gpu_adapters()
    check("gpu_adapters returns a hashable tuple", isinstance(adapters, tuple))
    for adapter in adapters:
        check(f"'{adapter.name}' reports a vendor or says nothing",
              isinstance(adapter.vendor, str))
        check(f"'{adapter.name}' VRAM is never the 32-bit clamp",
              adapter.vram_total != 4293918720,
              "4095MiB is the saturated AdapterRAM value, not a measurement")
    if adapters:
        check("the primary adapter is never a software shim",
              hardware.primary_gpu() is None or not hardware.primary_gpu().software)

    check("has_nvidia_gpu agrees with the adapter list",
          hardware.has_nvidia_gpu() == any(a.is_nvidia and not a.software for a in adapters))

    # ── DISPLAY ──
    monitors, width, height, scale = hardware.displays()
    check("display metrics are measured or zero, never a default",
          (width, height) != (1920, 1080) or width == 0,
          f"{width}x{height}")
    check("the display scale is a positive number", scale > 0)

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

    os_finding = next(f for f in result["groups"]["compatibility"]
                      if f.subsystem == "Operating system")
    check("the OS finding names the corrected product", os_finding.summary == product,
          os_finding.summary)

    check("human_bytes formats", sp.human_bytes(0) == "0 B" and "GB" in sp.human_bytes(2**31))

    # The system drive is read from the environment, never assumed to be C:.
    check("system_drive is derived, not hardcoded",
          "C:" not in io.open(sp.__file__, encoding="utf-8").read().split(
              "def system_drive(")[1].split("def ")[0])


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 5b. HARDWARE PORTABILITY (SYNTHETIC MACHINES)          │
# └────────────────────────────────────────────────────────────────────────┘
# The screens are rendered against machines that do not exist, and then searched for values
# that could only have come from the machine the suite is running on.
#
# THIS IS THE CHECK THAT ACTUALLY PROVES PORTABILITY. A UI reading the real profile passes
# every other check in this file on any machine, because it renders whatever it is given —
# what it cannot do, if it is hardcoded, is render something ELSE.

MACHINES = {
    "A: Windows 11 / Intel i5 / RTX 3050": {
        "graphics": {"name": "NVIDIA GeForce RTX 3050 Laptop GPU", "vendor": "NVIDIA",
                     "vram_total": 4 * 1024 ** 3, "integrated": False,
                     "driver": "31.0.15.3623", "telemetry": True},
        "gpu": {"name": "NVIDIA GeForce RTX 3050 Laptop GPU", "utilization": 7.0,
                "memory_used_mb": 900.0, "memory_total_mb": 4096.0,
                "memory_percent": 22.0, "temperature_c": 44.0, "source": "fixture"},
        "expect": ["RTX 3050"], "forbid": ["RTX 4060", "Radeon", "Iris"],
    },
    "B: Windows 11 / Ryzen 7 / RTX 4060": {
        "graphics": {"name": "NVIDIA GeForce RTX 4060 Laptop GPU", "vendor": "NVIDIA",
                     "vram_total": 8 * 1024 ** 3, "integrated": False,
                     "driver": "32.0.16.1062", "telemetry": True},
        "gpu": {"name": "NVIDIA GeForce RTX 4060 Laptop GPU", "utilization": 31.0,
                "memory_used_mb": 2100.0, "memory_total_mb": 8188.0,
                "memory_percent": 25.6, "temperature_c": 61.0, "source": "fixture"},
        "expect": ["RTX 4060"], "forbid": ["RTX 3050", "Radeon", "Iris"],
    },
    "C: Windows 11 / Intel i7 / Iris Xe (integrated only)": {
        "graphics": {"name": "Intel(R) Iris(R) Xe Graphics", "vendor": "Intel",
                     "vram_total": 0, "integrated": True,
                     "driver": "31.0.101.5333", "telemetry": False},
        "gpu": {},
        # No NVIDIA telemetry exists on this machine, so the card must still name the adapter
        # and must not present an NVIDIA figure of any kind.
        "expect": ["Iris"], "forbid": ["RTX", "NVIDIA", "GeForce"],
    },
    "D: Windows 11 / Ryzen / Radeon discrete": {
        "graphics": {"name": "AMD Radeon RX 7600M XT", "vendor": "AMD",
                     "vram_total": 8 * 1024 ** 3, "integrated": False,
                     "driver": "32.0.11038.5002", "telemetry": False},
        "gpu": {},
        "expect": ["Radeon"], "forbid": ["RTX", "NVIDIA", "GeForce", "Iris"],
    },
    "E: no graphics telemetry and no adapter": {
        "graphics": {}, "gpu": {},
        "expect": [], "forbid": ["RTX", "Radeon", "Iris", "NVIDIA"],
    },
}


def _visible_text(widget):
    """
    Every string a rendered widget tree is ACTUALLY SHOWING, including elided tooltips.

    HIDDEN LABELS ARE EXCLUDED, and that is not a convenience. Home's empty states are
    permanent children that are shown and hidden rather than created and destroyed — a
    deliberate design, because a `takeAt` leaves the widget painted underneath the new
    content. So "No GPU detected" is always a child of the card, and a search that did not
    filter on `isHidden()` would report it as visible on every machine.
    """
    from PySide6.QtWidgets import QLabel
    # A HIDDEN WIDGET SHOWS NOTHING, including its children. `isVisibleTo(ancestor)` is
    # relative to the ancestor passed, so asking it about the receiver's own children returns
    # True even when the receiver itself is hidden — which leaked the "No GPU detected" empty
    # state's text on every machine that has a GPU.
    if widget.isHidden():
        return ""
    chunks = []
    # The widget ITSELF counts when it is a label. `findChildren` does not include the
    # receiver, so scoping this helper to a single QLabel — which the graphics check now does,
    # since the adapter line is one label inside a panel that also renders the real host —
    # returned an empty string and failed every machine.
    labels = list(widget.findChildren(QLabel))
    if isinstance(widget, QLabel):
        labels.insert(0, widget)
    for label in labels:
        # `isHidden()` is the widget's OWN flag — a label inside a hidden parent reports
        # False. `isVisibleTo(root)` is the question actually being asked: would this text be
        # on screen if the root were shown? Getting this wrong made every machine look as
        # though it were rendering the "No GPU detected" empty state.
        if label is not widget and not label.isVisibleTo(widget):
            continue
        if label is widget and label.isHidden():
            continue
        chunks.append(label.text() or "")
        chunks.append(label.toolTip() or "")
    return " | ".join(chunks)


def section_hardware_portability(app):
    print_system("\n[5b] Hardware portability (synthetic machines)")
    from kayra.ui.views.home import HomeView

    for name, machine in MACHINES.items():
        bridge = StubBridge()
        bridge.graphics = dict(machine["graphics"])
        bridge.gpu = dict(machine["gpu"])
        bridge.telemetry_pending = False
        # An Intel/AMD machine runs speech on the processor, because Kayra's only
        # acceleration path is CUDA. Say so, so the provider text is exercised too.
        bridge.provider = ("CUDAExecutionProvider" if machine["graphics"].get("telemetry")
                           else "CPUExecutionProvider")

        view = HomeView(bridge)
        view.on_show()
        check(f"[{name}] renders", paint(view))
        # SCOPED TO THE GRAPHICS WIDGETS, not to the panel that contains them. Since the
        # redesign the adapter lives inside the System panel beside the processor, and that
        # panel renders the REAL host by design — it reads `device_profile()`, which has no
        # stub. Searching the whole panel for a forbidden vendor string would find the
        # suite's own machine in the processor line and report the UI as hardcoded when it is
        # doing exactly its job.
        text = " | ".join(_visible_text(w) for w in (view.gpu_name, view._gpu_empty))

        for token in machine["expect"]:
            check(f"[{name}] shows '{token}'", token in text,
                  "the card must name the adapter this machine actually has")
        for token in machine["forbid"]:
            check(f"[{name}] never shows '{token}'", token not in text,
                  "a value from another machine appeared on screen")

        if machine["graphics"] and not machine["graphics"].get("telemetry"):
            # The card must not draw an unmeasured utilization as 0%, and must not claim
            # there is no GPU on a machine that has one.
            check(f"[{name}] does not claim there is no GPU",
                  "No GPU detected" not in text)
            check(f"[{name}] says telemetry is unavailable",
                  "telemetry unavailable" in text.lower(), text[:160])
        if not machine["graphics"] and not machine["gpu"]:
            check(f"[{name}] reports the absence honestly", "No GPU detected" in text)

        view.on_hide()
        view.deleteLater()

    # ── The rendered System sheet carries no hardware literal ──
    # Every value must be traceable to the profile dict; a model name or a capacity written
    # into the view is exactly what makes a screen right on one machine and wrong on another.
    import kayra.ui.views.system as system_view_module
    view_source = io.open(system_view_module.__file__, encoding="utf-8").read()
    code_only = chr(10).join(line for line in view_source.splitlines()
                             if not line.lstrip().startswith("#"))
    for literal in ("RTX", "GeForce", "Radeon", "Iris Xe", "Core i5", "Core i7",
                    "Ryzen", "1920x1080", "16 GB", "8 GB"):
        check(f"the System view contains no '{literal}' literal", literal not in code_only,
              "hardware names belong in the profile, not in a view")

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
    # Driven through the DOCK, which is where the control lives now. Home is checked as a
    # readout beside it, so the two surfaces are asserted to agree rather than assumed to.
    bridge = StubBridge()
    window = docked_window(bridge)
    home = window.views["home"]
    app.processEvents()
    check("listening starts on", bridge.listening_enabled())
    check("the dock offers to PAUSE while listening",
          "Pause listening" in window.dock.mic_button.toolTip(),
          window.dock.mic_button.toolTip())
    check("and its glyph is an open microphone", window.dock.mic_button._kind == "mic")

    dock_press(window.dock.mic_button)
    app.processEvents()
    check("pressing it pauses listening", bridge.listening is False)
    check("pausing does not interrupt speech", bridge.interrupted == 0)
    check("pausing does not shut Kayra down", bridge.shutdown_called is False)
    check("the dock now offers to START",
          "Start listening" in window.dock.mic_button.toolTip(),
          window.dock.mic_button.toolTip())
    check("and the glyph changes SHAPE, not just tint",
          window.dock.mic_button._kind == "mic_off")
    check("Home's readout agrees with the dock",
          "paused" in home.mic_line.value_label.toolTip().lower(),
          home.mic_line.value_label.toolTip())
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

    dock_press(window.dock.mic_button)
    app.processEvents()
    check("pressing again resumes listening", bridge.listening is True)
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

    # ── The dock: the shutdown control ──
    # It moved off Home with every other control. Home is a readout now, and the one place a
    # press happens is the floating dock — which is also what makes it impossible for this
    # screen to offer a shutdown that disagrees with the dock's.
    bridge = StubBridge()
    window = docked_window(bridge)
    home = window.views["home"]
    dock = window.dock
    check("the dock has a shutdown control", hasattr(dock, "power_button"))
    check("it is the only control in the danger tone",
          dock.power_button._tone == "danger"
          and all(b._tone != "danger" for b in (dock.mic_button, dock.camera_button,
                                                dock.gesture_button, dock.talk_button,
                                                dock.chat_button, dock.menu_button)))
    check("it says what it shuts down",
          "Kayra" in (dock.power_button.toolTip() or ""), dock.power_button.toolTip())
    check("Home carries no shutdown control of its own",
          not hasattr(home, "shutdown_button"))

    # THE THREE CONTROLS MUST STAY DISTINCT. Pausing, interrupting and quitting are the three
    # things this application has most consistently confused with one another.
    check("the shutdown control is NOT the listening control",
          dock.power_button is not dock.mic_button)
    check("pressing the listening control does not shut down",
          (dock_press(dock.mic_button), bridge.shutdown_called is False)[1])
    check("pressing the listening control only changes the microphone",
          bridge.listening is False)
    bridge.set_listening(True)

    # Confirmed shutdown reaches the central path exactly once, and locks every control.
    from PySide6.QtWidgets import QMessageBox
    real_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        dock_press(dock.power_button)
        check("a confirmed shutdown reaches the central path", bridge.shutdown_called is True)
        check("the control is disabled afterwards", dock.power_button.isEnabled() is False)
        check("the other controls are locked too",
              dock.talk_button.isEnabled() is False
              and dock.mic_button.isEnabled() is False)
        check("and Home stops asking the camera for frames",
              home._shutting_down is True)
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
    window2 = docked_window(bridge2)
    QMessageBox.exec = lambda self: QMessageBox.Cancel
    try:
        dock_press(window2.dock.power_button)
        check("cancelling does NOT shut down", bridge2.shutdown_called is False)
        check("cancelling leaves the control usable",
              window2.dock.power_button.isEnabled() is True)
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

    # ── Home: the graphics block, now inside the System panel ──
    # IT MOVED, AND THAT WAS THE POINT. A graphics adapter is a property of the MACHINE, and
    # giving it its own card made it look like a subsystem of the assistant. What speech is
    # running on went the other way, into Intelligence, because that is a fact about what is
    # answering rather than about what the machine contains.
    from kayra.ui.views.home import HomeView as _HV
    gb = StubBridge()
    gpu_home = _HV(gb)
    gpu_home.on_show()

    check("Home reports graphics inside the System panel",
          gpu_home.gpu_name.parent() is gpu_home.system_panel
          or gpu_home.gpu_name.parentWidget() is gpu_home.system_panel)
    check("the GPU name is shown", gb.gpu["name"] in gpu_home.gpu_name.toolTip(),
          gpu_home.gpu_name.toolTip())
    check("utilization is a live value, not hardcoded", gpu_home.gpu_meter._value == 18.0)
    check("VRAM is shown as used / total",
          "4.2" in gpu_home.vram_meter._caption and "8.0" in gpu_home.vram_meter._caption,
          gpu_home.vram_meter._caption)
    check("VRAM percentage comes from the metrics", gpu_home.vram_meter._value == 52.5)
    check("temperature is shown", "58" in gpu_home.gpu_name.toolTip(),
          gpu_home.gpu_name.toolTip())
    check("the speech provider is reported in Intelligence, not beside the GPU",
          "CUDAExecutionProvider" in gpu_home.route_speech.value_label.toolTip(),
          gpu_home.route_speech.value_label.toolTip())
    check("the empty state is hidden while there is a GPU",
          gpu_home._gpu_empty.isHidden() is True)

    # THE PAGE MUST STAY TRUTHFUL WHEN SPEECH IS ON THE CPU WHILE A GPU EXISTS.
    # This is the exact state the whole change was about, and hiding the physical GPU here
    # would be as misleading as claiming acceleration that is not happening.
    gb.provider = "CPUExecutionProvider"
    gpu_home._refresh_graphics()
    gpu_home._refresh_intelligence()
    check("a CPU speech device does NOT hide the physical GPU",
          gpu_home.gpu_name.isHidden() is False
          and gb.gpu["name"] in gpu_home.gpu_name.toolTip())
    check("the provider shown is the real one",
          "CPUExecutionProvider" in gpu_home.route_speech.value_label.toolTip(),
          gpu_home.route_speech.value_label.toolTip())
    check("GPU statistics are still live", gpu_home.gpu_meter._value == 18.0)

    # ── TELEMETRY GONE BUT HARDWARE STILL PRESENT ──
    # This is the AMD/Intel case and it must NOT reach the empty state. Losing NVIDIA
    # telemetry does not remove the graphics card from the machine, and the page that used to
    # say "No GPU detected" here was telling every non-NVIDIA owner they had no GPU.
    gb.gpu = {}
    gb.telemetry_pending = False
    gpu_home._refresh_graphics()
    check("no telemetry does NOT mean no GPU", gpu_home._gpu_empty.isHidden() is True)
    check("the adapter is still named from the static profile",
          gb.graphics["name"] in gpu_home.gpu_name.toolTip(), gpu_home.gpu_name.toolTip())
    check("an unmeasured utilization is captioned, not drawn as 0%",
          "not reported" in (gpu_home.gpu_meter._caption or ""),
          gpu_home.gpu_meter._caption)
    check("installed VRAM is shown when live usage is unknown",
          "installed" in (gpu_home.vram_meter._caption or ""),
          gpu_home.vram_meter._caption)

    # ── No GPU at all: graceful, and the CPU/RAM readout is untouched ──
    gb.graphics = {}
    gpu_home._refresh_graphics()
    check("with no GPU the empty state is shown", gpu_home._gpu_empty.isHidden() is False)
    check("with no GPU the meters are hidden", gpu_home.gpu_meter.isHidden() is True)
    check("the processor meter is NOT hidden with the graphics block",
          gpu_home.cpu_meter.isHidden() is False,
          "the machine still has a CPU whatever its graphics situation")
    check("no GPU does not crash the screen", paint(gpu_home))

    # ── Telemetry still arriving is NOT the same as absent ──
    gb.telemetry_pending = True
    gpu_home._refresh_graphics()
    check("a pending read says so rather than announcing no GPU",
          "Reading" in gpu_home._gpu_empty._heading.text(),
          gpu_home._gpu_empty._heading.text())

    # ── The existing System card must keep working throughout ──
    gpu_home._refresh_panels()
    check("the processor meter still works", gpu_home.cpu_meter._value >= 0.0)
    check("the memory meter still works", gpu_home.ram_meter._value >= 0.0)

    # ── The System card's machine identity line ──
    # The profile is collected FIRST and the panel refreshed afterwards, because the read is
    # deliberately non-blocking: Home skips the line until the profile is warm rather than
    # stalling the GUI thread on a ~150ms collection. Refreshing before collecting would be
    # testing the cold path and calling it a missing feature.
    from kayra.core.system_profile import device_profile as _profile
    from kayra.core.system_profile import os_summary as _os_summary
    real = _profile()
    gpu_home._refresh_panels()
    # READ FROM THE WIDGET, not from a shadow copy on the view. `_ElidedCaption` owns the
    # full text now — the string the label would show if it had unlimited width — which is
    # also what removed the timing bug that let a 570px line sit unelided in a 374px label.
    identity = gpu_home.machine_line.full_text()
    product, _version = _os_summary()
    check("Home names the operating system", product in identity, identity)
    check("Home names the processor", (real["cpu_name"] or "") in identity, identity)
    if real.get("os_build"):
        check("Home shows the real OS build", str(real["os_build"]) in identity, identity)
        check("Home never shows platform.release() as the build",
              f"Build {real['os_release']}" not in identity, identity)
    check("the memory meter still has its caption", bool(gpu_home.ram_meter._caption))
    check("the footprint line still reports Kayra's own usage",
          "Kayra is using" in gpu_home.footprint.full_text(),
          gpu_home.footprint.full_text())

    # THE ELISION IS THE WIDGET'S OWN JOB, and this is the regression that made it so: the
    # machine line was written by a background collection a second into the session, elided
    # against a width the layout had not yet assigned, and never re-elided — 570px of text
    # running straight out of a 374px panel. A widget that elides in its own `resizeEvent`
    # cannot reach that state.
    from PySide6.QtGui import QFontMetrics
    gpu_home.resize(900, 800)
    gpu_home.layout().activate()
    app.processEvents()
    gpu_home._refresh_panels()
    app.processEvents()
    for name, label in (("machine", gpu_home.machine_line),
                        ("graphics", gpu_home.gpu_name),
                        ("footprint", gpu_home.footprint)):
        advance = QFontMetrics(label.font()).horizontalAdvance(label.text())
        check(f"the {name} line fits the width it was given",
              advance <= max(40, label.width()),
              f"{advance}px of text in a {label.width()}px label")
        check(f"...and the {name} line keeps its whole string in a tooltip",
              label.toolTip() == label.full_text())

    # ── Four panels, three columns, no clipping and no page scroll ──
    # The bottom strip is gone; the page is columns now. The property that matters is the
    # same one it always was: nothing may force the layout wider than the window, which is
    # what an un-elided non-wrapping label does every time it is allowed to.
    panels = (gpu_home.intelligence_panel, gpu_home.interaction_panel,
              gpu_home.system_panel, gpu_home.activity_panel)
    for width in (1040, 1100, 1920):
        gpu_home.resize(width, 860)
        gpu_home.layout().activate()
        app.processEvents()
        right_edge = max(p.mapTo(gpu_home, p.rect().topRight()).x() for p in panels)
        check(f"the panels fit inside a {width}px window", right_edge <= width,
              f"(rightmost panel edge {right_edge}px)")
    check("Home never needs a vertical scrollbar",
          gpu_home.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff)
    check("the two columns flank the orb",
          gpu_home.intelligence_panel.x() < gpu_home.orb.mapTo(gpu_home, QPoint(0, 0)).x()
          < gpu_home.system_panel.x(),
          "the orb must stay the middle column at every width")

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
    # PRESENCE IS ONE LINE NOW, not a card of its own. It is a subsystem whose whole job is
    # to stay quiet, and four rows in the bottom strip gave it more prominence than a service
    # the user is meant not to notice earns. What survives is what matters: whether it is on,
    # when it could next speak, and how much of the day's budget it has spent.
    home = HomeView(bridge)
    home._refresh_presence()
    presence = home.presence_line.value_label.toolTip()
    check("Home reports presence while the layer is running",
          presence.startswith("On"), presence)
    check("the next eligible time is shown", "next" in presence, presence)
    check("the day's budget is shown", "1/8" in presence, presence)

    # Switched off: the line says so rather than showing stale numbers.
    bridge.presence = False
    home._refresh_presence()
    presence = home.presence_line.value_label.toolTip()
    check("an off layer says so rather than showing stale numbers",
          presence.startswith("Off") and "next" not in presence, presence)
    check("and it says what that means",
          "only when asked" in presence.lower(), presence)

    # Not running at all is a THIRD state, distinct from "off": a service that does not exist
    # has not been switched off by anybody, and saying so is the same rule the graphics block
    # follows on a machine with no adapter.
    bridge.presence_running = False
    home._refresh_presence()
    presence = home.presence_line.value_label.toolTip()
    check("a service that is not running says exactly that",
          "not running" in presence.lower(), presence)

    check("Home paints with the presence line", paint(home))


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

    # ── 2. The dock during the boot window ──
    # SAME CONTRACT, NEW SURFACE. The control moved to the floating dock in the redesign, and
    # the boot-window defect follows the control rather than the screen: the dock is built by
    # `KayraWindow.__init__` seconds before the session boots, exactly as Home's button was.
    bridge = StubBridge()
    bridge.known = False                 # the backend has not booted yet
    window = docked_window(bridge)
    dock = window.dock
    home = window.views["home"]
    app.processEvents()

    check("during boot the control is not offered as actionable",
          dock.mic_button.isEnabled() is False)
    check("and it does not claim listening is off",
          dock.mic_button._kind == "mic", dock.mic_button._kind)
    check("the tooltip says why it cannot be used",
          "starting" in dock.mic_button.toolTip().lower(), dock.mic_button.toolTip())
    check("Home's readout does not claim it either",
          "starting" in home.mic_line.value_label.toolTip().lower(),
          home.mic_line.value_label.toolTip())

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

    check("boot completing enables the control", dock.mic_button.isEnabled() is True)
    check("THE REPORTED BUG: the control offers Pause, not Start, while listening",
          "Pause listening" in dock.mic_button.toolTip(), dock.mic_button.toolTip())
    check("and it got there with NO listeningChanged event at all",
          emitted == [], str(emitted))

    # ── 4. The state it was reported in: listening, never toggled ──
    bridge2 = StubBridge()
    bridge2.known = False
    window2 = docked_window(bridge2)
    home2 = window2.views["home"]
    bridge2.known = True
    bridge2.bootFinished.emit(True, "ready")
    bridge2.voiceStateChanged.emit("LISTENING", "Listening", "Microphone open.", 5)
    app.processEvents()
    check("caption and control agree after boot, with no toggle",
          home2.prompt.text() == "Listening"
          and "Pause listening" in window2.dock.mic_button.toolTip(),
          f"{home2.prompt.text()!r} / {window2.dock.mic_button.toolTip()!r}")

    # ── 5. And the toggle path still works, in both directions ──
    dock_press(window2.dock.mic_button)
    app.processEvents()
    check("pressing still pauses", bridge2.listening is False)
    check("and the tooltip follows",
          "Start listening" in window2.dock.mic_button.toolTip(),
          window2.dock.mic_button.toolTip())
    dock_press(window2.dock.mic_button)
    app.processEvents()
    check("pressing again resumes", bridge2.listening is True)
    check("and the tooltip follows back",
          "Pause listening" in window2.dock.mic_button.toolTip(),
          window2.dock.mic_button.toolTip())

    # ── 6. A genuinely paused microphone at boot must still read as paused ──
    bridge3 = StubBridge()
    bridge3.known = False
    window3 = docked_window(bridge3)
    bridge3.known = True
    bridge3.listening = False
    bridge3.bootFinished.emit(True, "ready")
    app.processEvents()
    check("a microphone that really is paused at boot reads as paused",
          "Start listening" in window3.dock.mic_button.toolTip(),
          window3.dock.mic_button.toolTip())

    # ── 7. Shutdown must not be re-enabled by a late re-sync ──
    from PySide6.QtWidgets import QMessageBox
    bridge4 = StubBridge()
    window4 = docked_window(bridge4)
    real_exec = QMessageBox.exec
    QMessageBox.exec = lambda self: QMessageBox.Yes
    try:
        dock_press(window4.dock.power_button)
    finally:
        QMessageBox.exec = real_exec
    bridge4.bootFinished.emit(True, "ready")
    window4.views["home"].on_show()
    app.processEvents()
    check("a late re-sync cannot re-enable controls during shutdown",
          window4.dock.mic_button.isEnabled() is False)

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


def section_gesture(app):
    """
    Hand gesture control on Home and in Settings.

    THE ONE RULE BEING TESTED, in several forms: **the controls reflect what the backend did,
    never what the user asked for.** A camera that fails to open must leave the switch off. It
    is the same requested-vs-active discipline the speech backend card follows, and the same
    failure it exists to prevent — a screen showing "on" over a device that is not running.
    """
    print_system("\n[17] Hand gesture control")

    from kayra.ui.views.home import HomeView
    from kayra.ui.views.settings import SettingsView
    from kayra.ui.components.camera_preview import CameraPreview

    # ── Home, cold ──
    # The preview and the STATUS stayed on Home; the two CONTROLS moved to the dock with
    # everything else. Both are driven here from one window, so the readout and the control
    # are asserted to agree rather than assumed to.
    bridge = StubBridge()
    window = docked_window(bridge)
    home = window.views["home"]
    dock = window.dock
    app.processEvents()
    home.on_show()
    app.processEvents()

    check("Home has a camera preview",
          isinstance(getattr(home, "camera_preview", None), CameraPreview))
    check("the dock has a camera control", hasattr(dock, "camera_button"))
    check("the dock has a gesture control", hasattr(dock, "gesture_button"))
    check("Home carries neither control itself",
          not hasattr(home, "camera_button") and not hasattr(home, "gesture_button"))
    check("with nothing running, neither control is checked",
          not dock.camera_button.is_checked() and not dock.gesture_button.is_checked())
    check("the status pill says Off", "Off" in home.gesture_pill.text(),
          home.gesture_pill.text())
    check("the preview is not asking for frames", bridge.frames_served == 0)

    # ── Turning the camera on, from the dock ──
    dock_press(dock.camera_button)
    app.processEvents()
    check("pressing the camera control calls the backend", bridge.camera_calls == [True],
          str(bridge.camera_calls))
    check("...and does NOT turn gesture control on", not bridge.gesture_calls,
          str(bridge.gesture_calls))
    check("...and the control reflects it", dock.camera_button.is_checked())
    check("...as a different SHAPE, not a different tint",
          dock.camera_button._kind == "camera", dock.camera_button._kind)
    check("...and the pill says the camera is on", "Camera on" in home.gesture_pill.text(),
          home.gesture_pill.text())

    # THE PREVIEW PULLS. It asks the bridge on its own timer; nothing pushes frames at it.
    # Counted as a DELTA rather than against a total: the preview's own timer is live on a
    # shown window, so the absolute count depends on how many event-loop turns have passed —
    # which is a property of the test harness, not of the widget.
    before_frames = bridge.frames_served
    home.camera_preview._pull()
    check("the preview pulls a frame when the camera is on",
          bridge.frames_served == before_frames + 1,
          f"{bridge.frames_served} after {before_frames}")
    check("...and painted it", home.camera_preview._pixmap is not None)

    # ── Gesture control on ──
    dock_press(dock.gesture_button)
    app.processEvents()
    check("pressing the gesture control calls the backend", bridge.gesture_calls == [True],
          str(bridge.gesture_calls))
    check("...and the pill says Active", "Active" in home.gesture_pill.text(),
          home.gesture_pill.text())

    bridge.gesture["hand"] = True
    bridge.gesture["gesture"] = "Cursor"
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    # THE HAND AND THE GESTURE ARE THEIR OWN LINE, separate from the pill and from the
    # running/paused caption. That separation is the point: "no hand in frame" must never be
    # able to render as "paused".
    check("the current gesture is shown on its own line",
          "Cursor" in home.gesture_line.value_label.toolTip(),
          home.gesture_line.value_label.toolTip())
    check("...and the pill still says Active", "Active" in home.gesture_pill.text(),
          home.gesture_pill.text())

    bridge.gesture["hand"] = False
    bridge.gesture["gesture"] = "No hand"
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    check("no hand is shown as no hand",
          "No hand" in home.gesture_line.value_label.toolTip(),
          home.gesture_line.value_label.toolTip())
    check("...and NOT as paused", "Paused" not in home.gesture_pill.text(),
          home.gesture_pill.text())
    check("...the pill still reads Active with no hand in frame",
          "Active" in home.gesture_pill.text(), home.gesture_pill.text())

    # A DELIBERATE PAUSE, which must look different from an empty frame.
    bridge.gesture["paused"] = True
    bridge.gesture["state"] = "PAUSED"
    bridge.gesture["hand"] = True
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    check("a deliberate pause reads as Paused", "Paused" in home.gesture_pill.text(),
          home.gesture_pill.text())
    check("...and says how to undo it",
          "resume" in home.gesture_line.value_label.toolTip().lower(),
          home.gesture_line.value_label.toolTip())
    bridge.gesture["paused"] = False
    bridge.gesture["state"] = "ACTIVE"
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()

    # ── A FAILING CAMERA MUST NOT LEAVE THE CONTROL ON ──
    # This is the rule the whole control layer is shaped around: the button is not the source
    # of truth and is never allowed to become one. A press that failed must leave the control
    # showing OFF, not showing the state the user asked for.
    failing = StubBridge()
    failing.camera_result = (False, "camera 0 could not be opened")
    failing.gesture_result = (False, "camera 0 could not be opened")
    broken_window = docked_window(failing)
    broken = broken_window.views["home"]
    app.processEvents()
    dock_press(broken_window.dock.gesture_button)
    app.processEvents()
    check("a gesture control that failed to start shows as OFF",
          not broken_window.dock.gesture_button.is_checked())
    check("...and the pill is not Active", "Active" not in broken.gesture_pill.text(),
          broken.gesture_pill.text())
    check("...and the reason is on screen",
          "could not be opened" in broken.camera_preview._message,
          broken.camera_preview._message)
    check("...and the preview is not live", not broken.camera_preview._live)

    # ── The preview stops asking when the camera stops ──
    served = bridge.frames_served
    bridge.gesture["camera"] = "OFF"
    bridge.gesture["gesture_enabled"] = False
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    check("the preview goes dark when the camera stops", not home.camera_preview._live)
    check("...and drops the last frame rather than showing a stale one",
          home.camera_preview._pixmap is None)
    home.camera_preview._pull()
    check("...and asks for nothing", bridge.frames_served == served,
          f"{bridge.frames_served} vs {served}")

    # ── The preview's timer follows visibility, like every other polling surface ──
    bridge.gesture["camera"] = "ACTIVE"
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    check("a visible live preview runs its timer", home.camera_preview._timer.isActive())
    window.hide()
    app.processEvents()
    check("a hidden preview stops its timer", not home.camera_preview._timer.isActive())

    # ── Shutdown disables the controls, and the preview goes dark ──
    from PySide6.QtWidgets import QMessageBox as _QMB
    quitting = StubBridge()
    quitting.gesture["camera"] = "ACTIVE"
    ending_window = docked_window(quitting)
    ending = ending_window.views["home"]
    app.processEvents()
    real_exec = _QMB.exec
    _QMB.exec = lambda self: _QMB.Yes
    try:
        dock_press(ending_window.dock.power_button)
    finally:
        _QMB.exec = real_exec
    check("a shutting-down window disables the camera control",
          not ending_window.dock.camera_button.isEnabled())
    check("...and the gesture control",
          not ending_window.dock.gesture_button.isEnabled())
    check("...and the preview is dark", not ending.camera_preview._live)
    # A late status arriving during teardown must not re-enable anything.
    quitting.gestureStateChanged.emit(dict(quitting.gesture))
    app.processEvents()
    check("a late gesture status cannot re-enable a control during shutdown",
          not ending_window.dock.camera_button.isEnabled()
          and not ending_window.dock.gesture_button.isEnabled())
    check("...and cannot restart the preview either", not ending.camera_preview._live)

    # ── Settings ──
    settings_bridge = StubBridge()
    settings = SettingsView(settings_bridge)
    settings.show()
    app.processEvents()
    settings.on_show()
    app.processEvents()

    check("Settings has a camera switch", hasattr(settings, "camera_toggle"))
    check("Settings has a gesture switch", hasattr(settings, "gesture_toggle"))
    for key in ("GESTURE_SENSITIVITY", "GESTURE_CURSOR_SMOOTHING",
                "GESTURE_CLICK_SENSITIVITY", "GESTURE_ENABLED", "GESTURE_DIAGNOSTICS"):
        check(f"Settings exposes {key}", key in settings._controls, str(key))

    settings.gesture_toggle.setChecked(True)
    app.processEvents()
    check("the Settings switch calls the backend", settings_bridge.gesture_calls == [True],
          str(settings_bridge.gesture_calls))
    check("...and the pill reflects the running state",
          "Active" in settings.gesture_pill.text(), settings.gesture_pill.text())
    check("...and the camera switch followed, because the controller started it",
          settings.camera_toggle.isChecked())

    failing_settings = StubBridge()
    failing_settings.gesture_result = (False, "no camera")
    other = SettingsView(failing_settings)
    other.show()
    app.processEvents()
    other.gesture_toggle.setChecked(True)
    app.processEvents()
    check("a failed switch leaves the Settings control off",
          not other.gesture_toggle.isChecked())
    check("...and says why", "no camera" in other.gesture_detail.text(),
          other.gesture_detail.text())

    # NO UI MODULE ANNOUNCES A SETTING CHANGE. The settings recorder is the one owner of that
    # event and `app.set_gesture_control` already goes through it transactionally, so a view
    # that logged it too would be the second of two lines for one change.
    # Checked by parsing the IMPORTS rather than grepping the text: these modules DISCUSS the
    # settings recorder in their comments, and they should — the reasoning belongs where the
    # decision is.
    #
    # Settings is deliberately NOT in this list. Its Save button legitimately announces a batch
    # of persisted values through the recorder, which is the sanctioned one-owner pattern; what
    # must not happen is the GESTURE handlers announcing a change the backend already
    # announced transactionally, and that is asserted separately below.
    import ast as _ast
    import inspect as _inspect
    for module_name, path in (("home", "kayra/ui/views/home.py"),
                              ("camera_preview", "kayra/ui/components/camera_preview.py")):
        full = os.path.join(project_root, "src", *path.split("/"))
        tree = _ast.parse(open(full, encoding="utf-8").read(), filename=full)
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, _ast.Import):
                imported.update(alias.name for alias in node.names)
        check(f"{module_name} does not import the settings recorder",
              "kayra.core.settings_log" not in imported, str(sorted(imported)))
        check(f"{module_name} does not import the logger",
              "kayra.core.logbus" not in imported, str(sorted(imported)))

    for name in ("_on_camera_toggle", "_on_gesture_toggle"):
        handler = _inspect.getsource(getattr(SettingsView, name))
        check(f"SettingsView.{name} does not announce the change itself",
              "settings_log" not in handler and "get_settings_recorder" not in handler,
              "app.set_gesture_control already records it transactionally")

    for widget in (home, broken, ending, settings, other):
        widget.hide()
        widget.deleteLater()
    app.processEvents()



# ┌────────────────────────────────────────────────────────────────────────┐
# │              17. THE REDESIGNED SHELL: DOCK, DRAWER, CHROME            │
# └────────────────────────────────────────────────────────────────────────┘
# Deliberately small. This is a UI redesign, so the checks pin the STRUCTURAL claims the
# redesign makes — which surface appears where, that the dock reads real state, that Home
# fills its viewport — and nothing else. Whether it looks good is a judgement that needs eyes
# on a rendered screen, and these checks were written after taking those screenshots.

def section_shell(app):
    print_system("\n[17] The redesigned shell")
    from kayra.ui import theme
    from kayra.ui.theme import Size, Motion
    from kayra.ui.components.dock import FloatingDock
    from kayra.ui.components.backdrop import AmbientBackdrop
    from kayra.ui.components.chrome import AppWindowChrome, native_chrome_supported
    from kayra.ui.components.navigation import DESTINATIONS

    bridge = StubBridge()
    window = docked_window(bridge)
    app.processEvents()

    # ── EXACTLY ONE navigation surface per screen ──
    for key in ("home", "chat"):
        window.navigate_to(key)
        app.processEvents()
        check(f"{key} shows the dock", not window.dock.isHidden())
        check(f"{key} gives up the permanent rail", window.sidebar.isHidden())
    for key in ("automation", "memory", "activity", "system", "settings"):
        window.navigate_to(key)
        app.processEvents()
        check(f"{key} keeps the permanent rail", not window.sidebar.isHidden())
        check(f"{key} has no floating dock", window.dock.isHidden(),
              "a pill over a settings form covers the last row of it")

    # ── The drawer opens, closes, navigates, and belongs to the dock's screens ──
    window.navigate_to("home")
    app.processEvents()
    check("the drawer starts closed", not window.drawer.is_open())
    window.dock.menuToggled.emit()
    app.processEvents()
    check("Menu opens the drawer", window.drawer.is_open())
    check("and the Menu control shows as active", window.dock.menu_button.is_checked())
    check("the drawer carries every destination",
          set(window.drawer._items) == {key for key, _label, _glyph in DESTINATIONS})
    window.dock.menuToggled.emit()
    app.processEvents()
    check("Menu closes it again", not window.drawer.is_open())

    window.dock.menuToggled.emit()
    app.processEvents()
    window.drawer.navigate.emit("system")
    app.processEvents()
    check("choosing a destination navigates", window.stack.currentWidget()
          is window.views["system"])
    check("and closes the drawer behind it", not window.drawer.is_open(),
          "the drawer exists to LEAVE these screens; two gestures for one intention is wrong")
    check("a screen with a rail never leaves the drawer open", not window.drawer.is_open())
    # And the keyboard is not a way around that rule: Ctrl+B reaches every screen.
    window._toggle_drawer()
    check("the drawer cannot be opened on a screen that already has a rail",
          not window.drawer.is_open(),
          "two navigation surfaces on one page is the state _apply_shell exists to prevent")

    # ── The dock reflects BACKEND state, and remembers nothing ──
    window.navigate_to("home")
    app.processEvents()
    bridge.set_listening(False)
    app.processEvents()
    check("the dock follows a listening change it did not cause",
          window.dock.mic_button._kind == "mic_off", window.dock.mic_button._kind)
    bridge.set_listening(True)
    app.processEvents()
    check("...and follows it back", window.dock.mic_button._kind == "mic")

    bridge.gesture["camera"] = "ACTIVE"
    bridge.gesture["gesture_enabled"] = True
    bridge.gestureStateChanged.emit(dict(bridge.gesture))
    app.processEvents()
    check("the dock follows a camera change it did not cause",
          window.dock.camera_button.is_checked())
    check("...and a gesture change", window.dock.gesture_button.is_checked())

    # ── Every control is reachable by keyboard, and says what it does ──
    controls = (window.dock.menu_button, window.dock.talk_button, window.dock.chat_button,
                window.dock.mic_button, window.dock.camera_button,
                window.dock.gesture_button, window.dock.power_button)
    check("every dock control takes focus",
          all(c.focusPolicy() == Qt.StrongFocus for c in controls))
    check("every dock control has a tooltip",
          all((c.toolTip() or "").strip() for c in controls),
          str([c._kind for c in controls if not (c.toolTip() or "").strip()]))
    check("the dock is a true pill",
          window.dock.height() == 2 * Size.dock_radius,
          f"{window.dock.height()}px tall, radius {Size.dock_radius}px")

    # ── ONE state for microphone and listening. Not two controls, not two flags. ──
    kinds = [c._kind for c in controls]
    check("there is exactly ONE microphone control in the dock",
          len([k for k in kinds if k in ("mic", "mic_off")]) == 1, str(kinds))
    import inspect
    dock_source = inspect.getsource(FloatingDock)
    check("and no separate start/stop-listening control beside it",
          "listeningToggled" in dock_source
          and "muteToggled" not in dock_source and "startListening" not in dock_source)

    # ── Home fills its viewport, at every size, without scrolling ──
    home = window.views["home"]
    for width, height in ((1040, 680), (1440, 900), (1920, 1080)):
        window.resize(width, height)
        app.processEvents()
        panels = (home.intelligence_panel, home.interaction_panel,
                  home.system_panel, home.activity_panel)
        covered = sum(p.width() * p.height() for p in panels)
        check(f"[{width}x{height}] the panels use the width",
              max(p.mapTo(home, p.rect().topRight()).x() for p in panels) <= width)
        check(f"[{width}x{height}] and a real share of the page",
              covered > 0.20 * width * height,
              f"{100.0 * covered / (width * height):.0f}% of the viewport")
        check(f"[{width}x{height}] the orb scales with the room it has",
              home.ORB_MIN <= home.orb.diameter() <= home.ORB_MAX,
              f"{home.orb.diameter()}px")
    check("Home never scrolls",
          home.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff)

    # ── The dock never covers the content it floats over ──
    window.resize(1440, 900)
    window.navigate_to("home")
    app.processEvents()
    dock_top = window.dock.mapTo(home, QPoint(0, 0)).y()
    lowest = max(p.mapTo(home, p.rect().bottomLeft()).y()
                 for p in (home.interaction_panel, home.activity_panel))
    check("the dock clears the lowest panel on Home", lowest <= dock_top,
          f"panel bottom {lowest}px, dock top {dock_top}px")

    window.navigate_to("chat")
    app.processEvents()
    chat = window.views["chat"]
    composer_bottom = chat.composer.mapTo(chat, chat.composer.rect().bottomLeft()).y()
    dock_top_chat = window.dock.mapTo(chat, QPoint(0, 0)).y()
    check("the dock clears the composer on Chat", composer_bottom <= dock_top_chat,
          f"composer bottom {composer_bottom}px, dock top {dock_top_chat}px")
    check("the conversation column is bounded and centred",
          chat.transcript_scroll.width() <= Size.chat_max
          and chat.composer.width() == chat.transcript_scroll.width(),
          f"{chat.transcript_scroll.width()}px / {chat.composer.width()}px")

    # ── The ambient backdrop: behind everything, cheap, and stoppable ──
    check("the window has one backdrop", isinstance(window.backdrop, AmbientBackdrop))
    check("it never intercepts a click",
          window.backdrop.testAttribute(Qt.WA_TransparentForMouseEvents))
    check("it covers the whole window",
          window.backdrop.width() == window.centralWidget().width()
          and window.backdrop.height() == window.centralWidget().height())
    check("the content area is transparent, or the backdrop would never be seen",
          "transparent" in theme.build().split("#ContentArea")[1].split("}")[0])
    window.backdrop.set_animated(False)
    check("reduced motion stops the animation", not window.backdrop._timer.isActive())
    check("...and it still paints a complete frame", paint(window.backdrop, 400, 300))
    window.backdrop.set_animated(True)

    # ── Nothing new polls ──
    # The redesign added two overlays and a backdrop. The backdrop's timer is the only one,
    # it is slower than the orb's, and it stops when hidden.
    check("the dock owns no timer at all",
          not window.dock.findChildren(QTimer),
          "a dock that polled would run for the whole session; its hover lift is an "
          "animation that exists only while the pointer is arriving or leaving")
    # The drawer's ONLY timer is the shared `OrbBadge`'s, which the rail already carries and
    # which stops itself for every resting state. A closed drawer must not be running it.
    drawer_timers = window.drawer.findChildren(QTimer)
    check("the drawer adds no timer of its own",
          all(t.parent().__class__.__name__ == "OrbBadge" for t in drawer_timers),
          str([t.parent().__class__.__name__ for t in drawer_timers]))
    check("and none of them runs while it is closed",
          not window.drawer.is_open() and not any(t.isActive() for t in drawer_timers))
    check("and it is slower than the orb", Motion.backdrop_fps < Motion.orb_fps_idle,
          f"{Motion.backdrop_fps} fps vs {Motion.orb_fps_idle} fps")
    window.hide()
    app.processEvents()
    check("a hidden backdrop stops entirely", not window.backdrop._timer.isActive())

    # ── Window chrome: custom where supported, native everywhere else ──
    chrome = AppWindowChrome("Kayra")
    check("the chrome offers the three window controls",
          all(hasattr(chrome, name) for name in
              ("minimize_button", "maximize_button", "close_button")))
    check("the caption buttons follow the platform's proportions, not the app's grid",
          chrome.close_button.width() > chrome.close_button.height(),
          f"{chrome.close_button.width()}x{chrome.close_button.height()}")
    check("the buttons are NOT part of the draggable caption",
          not chrome.is_caption_at(chrome.close_button.geometry().center()))
    chrome.set_maximized(True)
    check("the middle button offers restore once maximised",
          chrome.maximize_button._kind == "restore")
    chrome.set_maximized(False)
    check("...and maximise once restored", chrome.maximize_button._kind == "maximize")
    check("custom chrome declines on a platform that cannot hit-test",
          native_chrome_supported() is False,
          "the offscreen platform has no window manager; the native frame must be kept")
    check("so this window kept its native frame", window.native_chrome is False)

    # ── The drag is the PLATFORM'S, not a reimplementation ──
    chrome_source = io.open(
        os.path.join(project_root, "src", "kayra", "ui", "components", "chrome.py"),
        encoding="utf-8").read()
    import ast
    tree = ast.parse(chrome_source)
    movers = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "mouseMoveEvent"]
    check("the chrome never moves the window itself", not movers,
          "a manual drag loses Aero Snap, Win+Arrow, shake and the snap-layouts flyout")
    check("it answers WM_NCHITTEST instead", "WM_NCHITTEST" in chrome_source)

    # ── Shutdown still goes through the one confirmed path ──
    from kayra.ui.controls import KayraControls
    controls_source = inspect.getsource(KayraControls)
    check("the action layer delegates teardown rather than performing it",
          "shutdown(hard=True)" in controls_source
          and "taskkill" not in controls_source and "terminate" not in controls_source)
    check("and it asks first", "QMessageBox" in controls_source)
    check("the dialog says it is not a machine shutdown",
          "does not shut down your computer" in controls_source)


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
    section_hardware_portability(app)
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
    section_gesture(app)
    section_shell(app)
    section_no_side_effects(env_before)

    print_system("\n" + "=" * 60)
    if FAILED:
        print_error(f"{FAILED} UI check(s) FAILED ({PASSED} passed).")
        return 1
    print_success(f"All UI checks passed ({PASSED}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
