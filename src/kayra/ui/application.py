# ┌────────────────────────────────────────────────────────────────────────┐
# │                           application.py                               │
# │            Window Shell, Routing, Tray & Ambient Assistant             │
# └────────────────────────────────────────────────────────────────────────┘
"""
The application shell: one control-centre window, one ambient assistant, one tray icon.

UI-FIRST STARTUP
----------------
The window is created and shown BEFORE the backend boots. Qt costs about 260ms to reach a
painted window; the Kayra backend takes roughly 4.4s to bring up the speech model, the browser
session and the model clients. Booting first and painting afterwards would give a four-second
black screen for no reason.

So: paint, then boot on a worker thread, and report progress in the shell. Perceived startup
becomes a quarter of a second, and the sidebar shows exactly which subsystem is still coming up.

TWO SURFACES, ONE SESSION
-------------------------
The ambient window and the control centre are views onto the SAME `KayraBridge`. There is one
backend session for the process; closing the main window does not stop it, and the tray keeps
the assistant reachable. That is what makes "close the window" safe — it hides a view, it does
not kill the assistant.

SHUTDOWN
--------
Quitting hands over to `kayra.app._force_shutdown` through the bridge. That path is
authoritative and unchanged: it sets the runtime shutdown flag, stops the proactive agent,
cancels timers, silences audio, tears down the browser session and reaps the processes Kayra
owns BY PID — in that order, for reasons documented in the architecture. The UI does not
reorder it, does not duplicate it, and does not try to clean up first.
"""

import sys
import os

from PySide6.QtCore import Qt, QTimer, QPoint, QSize, QThread, QMetaObject
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QAction, QGuiApplication,
                           QCursor)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QSystemTrayIcon, QMenu, QSizePolicy, QLabel,
)

from kayra.ui import theme
from kayra.ui.theme import Color, Space, Size, Motion
from kayra.ui.bridge import KayraBridge
from kayra.ui.controls import KayraControls
from kayra.ui.components.navigation import Sidebar
from kayra.ui.components.orb import AssistantOrb, OrbBadge
from kayra.ui.components.primitives import Caption, GhostButton, _label
from kayra.ui.components.backdrop import AmbientBackdrop, prefers_reduced_motion
from kayra.ui.components.navigation import _motion_allowed
from kayra.ui.components.dock import FloatingDock
from kayra.ui.components.chrome import (AppWindowChrome, install_native_chrome,
                                        show_system_menu)
from kayra.ui.views.home import HomeView
from kayra.ui.views.chat import ChatView
from kayra.ui.views.automation import AutomationView
from kayra.ui.views.memory import MemoryView
from kayra.ui.views.activity import ActivityView
from kayra.ui.views.system import SystemView
from kayra.ui.views.settings import SettingsView


def _app_icon(state="IDLE"):
    """
    The tray/window icon: Kayra's ring, reduced to something legible at 16px.

    Drawn rather than shipped as a file so it can carry the assistant's state colour, and so
    the application has no binary asset dependency at all.
    """
    from kayra.ui.theme import STATE_COLORS
    # 64px logical at the screen's ratio: the tray shows this at 16px and the window
    # decoration at 32px, so it is always a DOWNSCALE, which stays crisp.
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.setDevicePixelRatio(1.0)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(STATE_COLORS.get(state, Color.accent))
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(6.0)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(10, 10, size - 20, size - 20)

    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(size // 2 - 7, size // 2 - 7, 14, 14)
    painter.end()
    return QIcon(pixmap)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        AMBIENT ASSISTANT                               │
# └────────────────────────────────────────────────────────────────────────┘

class AmbientAssistant(QWidget):
    """
    Kayra when the dashboard is closed: a small frameless panel with the orb and state.

    ONE APPLICATION, TWO PRESENTATIONS — NEVER BOTH AT ONCE.
    The ambient panel and the control centre are two views of the SAME session. It used to sit
    on top of the desktop permanently, including while the full window was open, which meant a
    floating duplicate of the status the dashboard was already showing a few hundred pixels
    away. It now appears only when the dashboard is not.

    It shows state and the last thing said; it is not a second chat window, because a floating
    panel that grows into an application is exactly how ambient UI becomes clutter.
    """

    # How far the pointer may travel before a press counts as a drag rather than a click.
    # Without this every attempt to nudge the panel also opened the dashboard, because a drag
    # ends with a release over the widget and that is indistinguishable from a click.
    DRAG_THRESHOLD_PX = 4

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(Size.ambient_width, Size.ambient_height)

        surface = QWidget(self)
        surface.setObjectName("AmbientSurface")
        surface.setGeometry(0, 0, Size.ambient_width, Size.ambient_height)

        layout = QHBoxLayout(surface)
        layout.setContentsMargins(Space.md, Space.md, Space.md, Space.md)
        layout.setSpacing(Space.md)

        # Not interactive: the PANEL handles press/move/release so a drag started on the orb
        # is still a drag. An orb that emitted `clicked` on its own would open the dashboard
        # in the middle of repositioning the window.
        self.orb = AssistantOrb(64, interactive=False)
        self.orb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.orb)

        text = QVBoxLayout()
        text.setSpacing(0)
        self.state_label = _label("Starting", "CardTitle")
        self.detail_label = Caption("Bringing subsystems up")
        self.detail_label.setMaximumWidth(200)
        text.addWidget(self.state_label)
        text.addWidget(self.detail_label)
        layout.addLayout(text, 1)

        self._drag_offset = None
        self._press_origin = None
        self._dragged = False
        self._listening = True

        # The ambient panel is often the ONLY thing on screen, so it is the surface where a
        # wrong caption costs the most — and it was the clearest instance of the bug: it wrote
        # its label from `listeningChanged` and then refused to update it from `stateChanged`
        # while `_listening` was false, so a single stale boolean could pin "Listening paused"
        # on screen indefinitely. It now renders the resolved state and holds no flag.
        self._voice_revision = -1
        bridge.voiceStateChanged.connect(self._on_voice_state)
        bridge.assistantMessage.connect(self._on_said)
        self.orb.clicked.connect(self.request_open)

        self.setToolTip("Click to open Kayra · drag to move")

    # ──────────────────────────────────────────────────────────────────
    #                            SIGNALS OUT
    # ──────────────────────────────────────────────────────────────────

    def request_open(self):
        """
        Asks for the dashboard.

        Routed through the window that owns this panel rather than searched for with
        `activeWindow()` — a frameless tool window is often the active window itself, so the
        old lookup could return the panel and silently do nothing.
        """
        handler = getattr(self, "on_open_requested", None)
        if callable(handler):
            handler()

    # ──────────────────────────────────────────────────────────────────
    #                              STATE
    # ──────────────────────────────────────────────────────────────────

    def _on_voice_state(self, state, text, detail, revision):
        """Renders the resolved voice state, dropping anything a newer transition has overtaken."""
        if revision <= self._voice_revision:
            return
        self._voice_revision = revision
        self._voice_state = state

        from kayra.core.voice_state import ORB_STATE, ORB_AMPLITUDE, VoiceState

        self.orb.set_state(ORB_STATE.get(state, "IDLE"), ORB_AMPLITUDE.get(state))
        self.state_label.setText(text)
        if state == VoiceState.PAUSED:
            self.detail_label.setText("Click to open and start listening")
        else:
            self.detail_label.setText(detail or "")

    def _sync_voice(self):
        snapshot = self.bridge.voice_runtime_state() or {}
        if snapshot:
            self._on_voice_state(snapshot.get("state", "OFFLINE"), snapshot.get("text", ""),
                                 snapshot.get("detail", ""), int(snapshot.get("revision", 0)))

    def _on_said(self, text):
        """
        Shows the last thing Kayra said, but never over a state that matters more.

        A spoken sentence is the LEAST important thing this panel can show: a paused
        microphone, a reconnecting session or a shutdown in progress all change what the user
        can do, and a leftover sentence sitting where that line should be is how the panel
        came to look correct while being wrong.
        """
        from kayra.core.voice_state import VoiceState
        if getattr(self, "_voice_state", None) in (VoiceState.PAUSED, VoiceState.STANDBY,
                                                   VoiceState.RECOVERING, VoiceState.STOPPING,
                                                   VoiceState.ERROR, VoiceState.OFFLINE):
            return
        self.detail_label.setText(text if len(text) < 46 else text[:43].rstrip() + "…")

    # ──────────────────────────────────────────────────────────────────
    #                            PLACEMENT
    # ──────────────────────────────────────────────────────────────────

    def place_default(self):
        """
        Bottom-right of the screen the user is actually on — never a hardcoded coordinate.

        `screenAt(cursor)` follows the pointer, so on a multi-monitor desktop the panel
        appears on the monitor being used rather than always on the primary one. Falls back to
        the primary screen when the cursor is off every screen (which happens between monitors
        of different heights).
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 24)

    def ensure_on_screen(self):
        """
        Pulls the panel back if it is off every screen.

        A monitor can be unplugged, or the layout rearranged, while the panel is parked on it —
        after which a frameless always-on-top window with no taskbar entry is unreachable. This
        is the only positional correction applied: there is deliberately no edge snapping,
        because the brief asks for free placement and a panel that jumps as you release it is
        worse than one that does not.
        """
        frame = self.frameGeometry()
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(frame):
                return
        self.place_default()

    # ──────────────────────────────────────────────────────────────────
    #                        DRAG, AND ONLY THEN
    # ──────────────────────────────────────────────────────────────────
    # A frameless window has no title bar, so the whole surface is the handle — which means
    # the same gesture has to serve two purposes. Press records an origin; movement past a
    # small threshold promotes it to a drag and latches `_dragged`; release opens the
    # dashboard ONLY if the press never became a drag.

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_origin = event.globalPosition().toPoint()
            self._drag_offset = self._press_origin - self.frameGeometry().topLeft()
            self._dragged = False

    def mouseMoveEvent(self, event):
        if self._drag_offset is None or not (event.buttons() & Qt.LeftButton):
            return
        position = event.globalPosition().toPoint()
        if not self._dragged:
            delta = position - self._press_origin
            if max(abs(delta.x()), abs(delta.y())) < self.DRAG_THRESHOLD_PX:
                return                  # still a click, as far as anyone can tell
            self._dragged = True
        # `move` to a global position works across monitors of different scale factors: Qt
        # maps the logical coordinate onto whichever screen contains it.
        self.move(position - self._drag_offset)

    def mouseReleaseEvent(self, event):
        was_drag = self._dragged
        self._drag_offset = None
        self._press_origin = None
        self._dragged = False
        if event.button() == Qt.LeftButton and not was_drag:
            self.request_open()
        elif was_drag:
            self.ensure_on_screen()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            PAGE HOST                                   │
# └────────────────────────────────────────────────────────────────────────┘

class _PageHost(QWidget):
    """
    The area below the title bar: the rail, the routed stack, and the two overlays.

    THE OVERLAYS ARE CHILDREN, NOT LAYOUT ITEMS. The dock floats over the page and the
    drawer covers it; neither may take space from the content, because the moment either did,
    opening the drawer would relayout Home — the orb would slide, the backdrop's bloom would
    move, and every elided caption would re-elide, twice per open. Children positioned in
    `resizeEvent` cost two `setGeometry` calls and change nothing underneath them.

    This class exists so that positioning lives in ONE place. Doing it from the window meant
    reaching through two layers of layout to find the content rect, and getting a different
    answer depending on whether the rail happened to be visible.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dock = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reposition_overlays()

    def reposition_overlays(self):
        """Centres the dock over the CONTENT."""
        dock = self.dock
        # `isHidden()`, NOT `isVisible()`. A widget whose parent chain is not yet shown
        # reports `isVisible() == False` even when nobody hid it, so the visibility test would
        # skip positioning for the whole of construction and the dock would sit at (0, 0) in
        # the corner until the first resize after the window was shown.
        if dock is None or dock.isHidden():
            return
        dock.adjustSize()
        # Centred on the content area rather than on the window, so the rail — when one is
        # showing — does not push the dock visually off-centre. On Home and Chat there is no
        # rail, so the two are the same; keeping the arithmetic honest means the dock stays
        # correct if a rail is ever shown alongside it.
        left = getattr(self, "content_left", 0)
        width = max(1, self.width() - left)
        dock.move(left + (width - dock.width()) // 2,
                  self.height() - dock.height() - Size.dock_margin_bottom)
        dock.raise_()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          CONTROL CENTRE                                │
# └────────────────────────────────────────────────────────────────────────┘

class KayraWindow(QMainWindow):
    """The full application window: chrome, rail or dock, routed content stack."""

    # The two screens that give up the permanent rail for the full window width and the
    # floating dock. Everything else keeps the rail: those are dense utility screens where a
    # visible list of destinations is worth its 224 pixels, and where a dock floating over a
    # settings form would cover the last row of it.
    DOCK_SCREENS = ("home", "chat")

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        # ONE action layer, shared by the dock, the shortcuts and anything else that acts.
        # See `ui/controls.py`: the same press must not be implemented twice.
        self.controls = KayraControls(bridge, self)
        self._shutting_down = False

        self.setWindowTitle("Kayra")
        self.setWindowIcon(_app_icon())
        self.setMinimumSize(Size.min_window_width, Size.min_window_height)
        self.resize(Size.default_window_width, Size.default_window_height)

        root = QWidget()
        root.setObjectName("RootSurface")
        self.setCentralWidget(root)

        # ── The ambient ground ──
        # A plain child, positioned by hand and lowered, rather than a layout item: it has to
        # sit BEHIND everything including the rail, and a layout item would take space.
        self.backdrop = AmbientBackdrop(root)
        self.backdrop.setGeometry(0, 0, root.width(), root.height())
        # Respect the platform's reduced-motion preference. The still frame is a complete
        # picture, so nothing is lost except the drift.
        self.backdrop.set_animated(not prefers_reduced_motion())

        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ── Custom window chrome, WHERE THE PLATFORM SUPPORTS IT ──
        # On anything but Windows — and under the offscreen platform the test suite uses —
        # `install_native_chrome` declines and the native title bar stays. A degraded custom
        # title bar is worse than the real one, so this falls back rather than half-working.
        self.chrome = AppWindowChrome("Kayra")
        self.chrome.minimizeRequested.connect(self.showMinimized)
        self.chrome.maximizeRequested.connect(self._toggle_maximized)
        self.chrome.closeRequested.connect(self.close)
        self.chrome.setContextMenuPolicy(Qt.CustomContextMenu)
        self.chrome.customContextMenuRequested.connect(
            lambda point: show_system_menu(self, self.chrome.mapToGlobal(point)))
        self.native_chrome = install_native_chrome(self, self.chrome)
        if self.native_chrome:
            shell.addWidget(self.chrome)
        else:
            self.chrome.setVisible(False)

        self.host = _PageHost(root)
        shell.addWidget(self.host, 1)

        layout = QHBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self.navigate_to)
        layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("ContentArea")
        layout.addWidget(self.stack, 1)

        # Views are constructed eagerly but do no work until shown — every screen that costs
        # anything starts its timer in `on_show` and stops it in `on_hide`.
        self.views = {}
        for key, factory in (
            ("home", HomeView), ("chat", ChatView), ("automation", AutomationView),
            ("memory", MemoryView), ("activity", ActivityView), ("system", SystemView),
            ("settings", SettingsView),
        ):
            view = factory(bridge)
            self.views[key] = view
            self.stack.addWidget(view)

        # ── The floating dock ──
        self.dock = FloatingDock(self.host)
        self.host.dock = self.dock
        self._wire_dock()

        self._current = None
        self._current_key = None
        self.navigate_to("home")
        self.sidebar.select("home")

        self._voice_revision = -1
        bridge.stateChanged.connect(self._on_state)
        bridge.voiceStateChanged.connect(self._on_voice_state)
        bridge.listeningChanged.connect(self._on_listening)
        bridge.gestureStateChanged.connect(self.dock.set_gesture_status)
        bridge.bootStage.connect(lambda name: self.sidebar.set_state("STARTING", name))
        bridge.bootFinished.connect(self._on_boot_finished)

        self._install_shortcuts()

    # ──────────────────────────────────────────────────────────────────
    #                              THE DOCK
    # ──────────────────────────────────────────────────────────────────

    def _wire_dock(self):
        """
        Every dock press routes to `self.controls` or to routing. NOTHING is decided here.

        The dock emits intent and this connects it; the action itself lives in one place so
        the tray, the shortcuts and the dock cannot drift into three behaviours.
        """
        # THE PRIMARY BUTTON IS THE MICROPHONE NOW, not a second way into Chat. It routes to
        # exactly the same action the keyboard shortcut and the tray use — there is no second
        # listening state anywhere, and `_toggle_listening` reads the runtime before it acts.
        self.dock.homeRequested.connect(lambda: self.navigate_to("home"))
        self.dock.chatRequested.connect(lambda: self.navigate_to("chat"))
        self.dock.listeningToggled.connect(self._toggle_listening)
        self.dock.cameraToggled.connect(self._toggle_camera)
        self.dock.gestureToggled.connect(self._toggle_gesture)
        self.dock.shutdownRequested.connect(self._request_shutdown)

    def _toggle_listening(self):
        self.controls.toggle_listening()
        self._sync_dock_state()

    def _toggle_camera(self):
        # RE-READ AFTERWARDS, never trust the request. A click that fails — the camera is
        # unplugged, another application holds it — must leave the control showing OFF, not
        # showing the state the user asked for.
        self.controls.toggle_camera()
        self.dock.set_gesture_status(self.bridge.gesture_status() or {})

    def _toggle_gesture(self):
        self.controls.toggle_gesture()
        self.dock.set_gesture_status(self.bridge.gesture_status() or {})

    def _request_shutdown(self):
        """
        Confirms, then hands over to the ONE authoritative shutdown path.

        This does not stop threads, close browsers or silence audio itself: `controls`
        delegates to `bridge.shutdown(hard=True)`, which is `app.request_shutdown` — the same
        thing the spoken "turn off Kayra" and the tray's Quit call. A second teardown living
        in the UI would inevitably drift out of step with the ordering the backend depends on.
        """
        if self._shutting_down:
            return
        if not self.controls.confirm_shutdown():
            return
        self._shutting_down = True
        # Latched off and never re-enabled: teardown takes a couple of seconds and a second
        # press during that window re-enters a shutdown that is already half done.
        self.dock.set_shutting_down()
        home = self.views.get("home")
        if home is not None and hasattr(home, "mark_shutting_down"):
            home.mark_shutting_down()

    def _sync_dock_state(self):
        """One synchronous read of everything the dock displays. It caches nothing."""
        snapshot = self.bridge.voice_runtime_state() or {}
        if snapshot:
            self.dock.set_listening(snapshot.get("listening", True),
                                    known=bool(snapshot.get("listening_known", True)))
        else:
            self.dock.set_listening(self.bridge.listening_enabled(),
                                    known=self.bridge.listening_known())
        self.dock.set_gesture_status(self.bridge.gesture_status() or {})

    # ──────────────────────────────────────────────────────────────────
    #                             ROUTING
    # ──────────────────────────────────────────────────────────────────

    def uses_dock(self, key):
        """Home and Chat get the dock; every other screen keeps the permanent rail."""
        return key in self.DOCK_SCREENS

    def navigate_to(self, key):
        view = self.views.get(key)
        if view is None:
            return
        if view is self._current:
            return
        outgoing = self._current_key
        outgoing_view = self._current
        snapshot = None
        if (outgoing is not None and outgoing_view is not None
                and _motion_allowed() and outgoing_view.isVisible()):
            try:
                snapshot = outgoing_view.grab()
            except Exception:
                snapshot = None
        if self._current is not None:
            self._current.on_hide()
        self.stack.setCurrentWidget(view)
        self._current = view
        self._current_key = key
        self._apply_shell(key)
        view.on_show()
        self.sidebar.select(key)
        self._animate_page_in(view, outgoing, key, snapshot=snapshot)

    # ──────────────────────────────────────────────────────────────────
    #                        THE PAGE TRANSITION
    # ──────────────────────────────────────────────────────────────────
    # The destination fades and travels a short way into place. NOTHING IS TORN DOWN AND
    # NOTHING IS REBUILT: `setCurrentWidget` has already happened, `on_show()` has already
    # run, and this animates the widget that is now on screen. Chat keeps its transcript,
    # the orb keeps its state, the camera keeps its frames — the transition is a repaint,
    # not a reload.
    #
    # A temporary snapshot of the outgoing page sits behind the incoming view during the
    # transition so the screen never blinks empty or cuts abruptly. One effect, one widget.

    # Where a screen travels in from, so a route reads as having a direction. Home and Chat
    # sit side by side in the dock, so moving between them travels the way the eye expects.
    _ROUTE_ORDER = ("home", "chat", "automation", "memory", "activity", "system", "settings")

    def _travel_sign(self, outgoing, incoming):
        try:
            return 1 if (self._ROUTE_ORDER.index(incoming)
                         >= self._ROUTE_ORDER.index(outgoing)) else -1
        except ValueError:
            return 1

    def _animate_page_in(self, view, outgoing, incoming, snapshot=None):
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QParallelAnimationGroup
        from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel

        if outgoing is None or not _motion_allowed():
            return                      # the first paint is an arrival, not a transition

        # A page already mid-transition is not re-animated from wherever it happened to be:
        # the previous animation is stopped and its effect dropped first, so two runs cannot
        # drive the same opacity in opposite directions.
        self._end_page_animation()

        group = QParallelAnimationGroup(self)

        if snapshot is not None and not snapshot.isNull():
            overlay = getattr(self, "_transition_overlay", None)
            if overlay is None:
                overlay = QLabel(self.stack)
                overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self._transition_overlay = overlay
            overlay.setPixmap(snapshot)
            overlay.setGeometry(0, 0, self.stack.width(), self.stack.height())
            overlay_effect = QGraphicsOpacityEffect(overlay)
            overlay_effect.setOpacity(1.0)
            overlay.setGraphicsEffect(overlay_effect)
            overlay.show()
            overlay.lower()

            overlay_fade = QPropertyAnimation(overlay_effect, b"opacity", self)
            overlay_fade.setDuration(Motion.nav_transition)
            overlay_fade.setEasingCurve(QEasingCurve.InOutCubic)
            overlay_fade.setStartValue(1.0)
            overlay_fade.setEndValue(0.0)
            group.addAnimation(overlay_fade)

        view.raise_()
        effect = QGraphicsOpacityEffect(view)
        effect.setOpacity(0.0)
        view.setGraphicsEffect(effect)

        fade = QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(Motion.nav_transition)
        fade.setEasingCurve(QEasingCurve.InOutCubic)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        group.addAnimation(fade)

        if Motion.nav_travel > 0:
            offset = Motion.nav_travel * self._travel_sign(outgoing, incoming)
            view.move(offset, 0)
            travel = QPropertyAnimation(view, b"pos", self)
            travel.setDuration(Motion.nav_transition)
            travel.setEasingCurve(QEasingCurve.InOutCubic)
            travel.setStartValue(QPoint(offset, 0))
            travel.setEndValue(QPoint(0, 0))
            group.addAnimation(travel)

        group.finished.connect(self._end_page_animation)
        self._page_animation = group
        self._page_view = view
        group.start()

    def _end_page_animation(self):
        """
        Drops the composite layer and puts the page back on its exact pixel.

        THE EFFECT MUST COME OFF. A QGraphicsOpacityEffect left in place routes every repaint
        of that screen through an offscreen buffer for the rest of the session — which on
        Home means the orb and the live camera preview, the two things in this application
        that repaint most.
        """
        group = getattr(self, "_page_animation", None)
        if group is not None:
            group.stop()
            self._page_animation = None
        view = getattr(self, "_page_view", None)
        if view is not None:
            view.setGraphicsEffect(None)
            view.move(0, 0)
            self._page_view = None
        overlay = getattr(self, "_transition_overlay", None)
        if overlay is not None:
            overlay.setGraphicsEffect(None)
            overlay.hide()
            overlay.clear()

    def _apply_shell(self, key):
        """
        The sidebar is permanently and statically visible across ALL screens.
        Home and Chat additionally show the floating dock centered over the content area.
        """
        docked = self.uses_dock(key)
        self.sidebar.setVisible(True)
        self.dock.setVisible(docked and not self._shutting_down)

        # The chrome names the screen, EXCEPT on Home — there the orb is the heading and a
        # title bar repeating "Home" says nothing.
        self.chrome.set_subtitle("" if key == "home" else key.title())

        # `content_left` is the rail's width so the dock centres on the content rather than
        # on the full window width.
        self.host.content_left = self.sidebar.width()
        self.dock.set_current_screen(key)
        if docked:
            self._sync_dock_state()
        self.host.reposition_overlays()

    def _install_shortcuts(self):
        """Ctrl+1..7 for destinations; Ctrl+K jumps to Chat, the thing people want most."""
        from PySide6.QtGui import QKeySequence, QShortcut

        for index, (key, _label_text, _glyph) in enumerate(Sidebar.DESTINATIONS, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda k=key: self.navigate_to(k))

        chat = QShortcut(QKeySequence("Ctrl+K"), self)
        chat.activated.connect(lambda: self._focus_chat())

        # Ctrl+. is BARGE-IN and stays barge-in: it cancels the sentence being spoken.
        stop = QShortcut(QKeySequence("Ctrl+."), self)
        stop.activated.connect(self.bridge.interrupt)

        # Ctrl+M is the MICROPHONE. A separate key for a separate concept, so neither can be
        # reached by muscle memory for the other. Routed through `controls` rather than
        # calling the bridge here, so the keyboard, the dock and the tray run one code path.
        mic = QShortcut(QKeySequence("Ctrl+M"), self)
        mic.activated.connect(self._toggle_listening)

    def _focus_chat(self):
        self.navigate_to("chat")
        view = self.views.get("chat")
        if view is not None:
            view.input.setFocus()

    # ──────────────────────────────────────────────────────────────────
    #                              STATE
    # ──────────────────────────────────────────────────────────────────

    def _on_state(self, state, previous):
        """
        The window ICON follows the assistant's work — a separate fact from the microphone.

        The taskbar icon is about "is Kayra busy", which is exactly what `AssistantState`
        means, so this stays connected to `stateChanged`. The sidebar's words come from the
        voice state instead; the two are different questions and used to be answered from one
        value, which is why the sidebar could say "Idle" beside an orb that was reconnecting.
        """
        self.setWindowIcon(_app_icon(state))

    def _on_voice_state(self, state, text, detail, revision):
        """
        Fans the ONE resolved voice state out to every shell surface that shows it.

        Three surfaces, one source. The rail, the drawer and the title-bar dot all render
        what they are given and none of them re-derives it — which is the whole reason
        `core.voice_state` exists, and why a stale revision is dropped here once rather than
        in three places.
        """
        if revision <= self._voice_revision:
            return
        self._voice_revision = revision
        self.sidebar.set_voice(state, text, detail)
        from kayra.core.voice_state import ORB_STATE
        self.chrome.set_state(ORB_STATE.get(state, "IDLE"))

    def _on_listening(self, listening, known=True):
        """The dock's microphone control. A readout, never a remembered value."""
        self.dock.set_listening(listening, known=known)

    def _detail_for(self, state):
        if state == "IDLE":
            return "Ready" if self.bridge.voice_available() else "Ready — typing only"
        return {
            "LISTENING": "Microphone open",
            "PROCESSING": "Working it out",
            "SPEAKING": "Responding",
            "AUTOMATING": "Controlling applications",
            "ERROR": "Something failed",
        }.get(state, "")

    def _on_boot_finished(self, ok, detail):
        if ok:
            snapshot = self.bridge.voice_runtime_state() or {}
            if snapshot:
                self._on_voice_state(snapshot.get("state", "OFFLINE"),
                                     snapshot.get("text", ""), detail or snapshot.get("detail", ""),
                                     int(snapshot.get("revision", 0)))
            else:
                self.sidebar.set_state(self.bridge.state(), detail)
        else:
            self.sidebar.set_state("ERROR", "Startup failed")
        # A SHELL BUILT BEFORE THE BACKEND IS READY MUST RE-READ WHEN IT BECOMES READY. The
        # dock is constructed seconds before the session boots, and an event-driven control is
        # never corrected for a value that never changed — so without this the microphone
        # button keeps whatever it was painted with during the boot window, indefinitely. This
        # is the same defect Home's control had, arriving at a new surface.
        if not self._shutting_down:
            self._sync_dock_state()

    # ──────────────────────────────────────────────────────────────────
    #                          WINDOW CHROME
    # ──────────────────────────────────────────────────────────────────

    def _toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.chrome.set_maximized(self.isMaximized())

    # ──────────────────────────────────────────────────────────────────
    #                             WINDOW
    # ──────────────────────────────────────────────────────────────────

    def attach_ambient(self, ambient):
        """
        Binds the ambient panel to this window and makes the two mutually exclusive.

        ONE PRESENTATION AT A TIME. Whichever is showing, the other is hidden — the panel is
        the compact form of this window, not a companion to it. Both observe the same
        `KayraBridge`, so neither holds state and switching between them costs a show and a
        hide.
        """
        self.ambient = ambient
        ambient.on_open_requested = self.show_control_centre
        self._sync_presentation()

    def _sync_presentation(self):
        """The ambient panel is visible exactly when this window is not."""
        ambient = getattr(self, "ambient", None)
        if ambient is None:
            return
        if getattr(self, "_ambient_muted", False):
            ambient.hide()
            return
        if self.isVisible() and not self.isMinimized():
            ambient.hide()
        else:
            ambient.ensure_on_screen()
            ambient.show()

    def set_ambient_allowed(self, allowed):
        """Tray toggle: lets the user suppress the panel entirely."""
        self._ambient_muted = not bool(allowed)
        self._sync_presentation()

    def show_control_centre(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._sync_presentation()

    # The presentation swap has to follow the window however it is shown or hidden — the tray,
    # the close button, a minimise, or the taskbar. Hooking the events rather than every call
    # site is what stops the two surfaces from ever being visible together.
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The backdrop is not a layout item — it has to sit behind the rail as well as the
        # content — so its geometry follows the root by hand. Two integers per resize.
        root = self.centralWidget()
        if root is not None and getattr(self, "backdrop", None) is not None:
            self.backdrop.setGeometry(0, 0, root.width(), root.height())
            self.backdrop.lower()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_presentation()
        self.chrome.set_maximized(self.isMaximized())

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_presentation()

    def changeEvent(self, event):
        from PySide6.QtCore import QEvent
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            # Minimising is "closed" as far as the presentation is concerned: the dashboard is
            # not on screen, so the compact form should be.
            self._sync_presentation()
            # The middle caption button must never offer what the window has just done.
            self.chrome.set_maximized(self.isMaximized())

    def closeEvent(self, event):
        """
        Closing collapses the dashboard into the ambient panel. It does not quit.

        A voice assistant that stops listening because its window was closed is not an
        assistant. Closing is a change of PRESENTATION: the dashboard goes away, the compact
        panel appears, and the session underneath is untouched — same process, same runtime,
        same microphone state, same proactive agent.

        Quitting stays explicit and stays where it was: the tray menu, which calls the
        backend's own `_force_shutdown`. That ordering is authoritative and this method does
        not participate in it.
        """
        event.ignore()
        self.hide()                     # hideEvent brings the ambient panel up

        tray = getattr(self, "tray", None)
        if tray is not None and tray.isVisible() and not getattr(self, "_close_notice_shown",
                                                                 False):
            self._close_notice_shown = True
            tray.showMessage("Kayra is still running",
                             "The dashboard is closed, but Kayra is still here in the corner. "
                             "Quit from the tray icon to stop it.",
                             QSystemTrayIcon.Information, 4000)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              TRAY                                      │
# └────────────────────────────────────────────────────────────────────────┘

def _build_tray(app, window, ambient, bridge):
    tray = QSystemTrayIcon(_app_icon(), app)
    tray.setToolTip("Kayra — starting")

    menu = QMenu()
    open_action = QAction("Open Kayra", menu)
    open_action.triggered.connect(window.show_control_centre)

    ambient_action = QAction("Show the compact assistant", menu)
    ambient_action.setCheckable(True)
    ambient_action.setChecked(True)
    # Routed through the window, which owns the "one presentation at a time" rule. Showing the
    # panel directly from here would put it on screen alongside the open dashboard.
    ambient_action.toggled.connect(window.set_ambient_allowed)

    stop_action = QAction("Stop speaking", menu)
    stop_action.triggered.connect(bridge.interrupt)

    # Listening is its own item, worded so it cannot be mistaken for quitting. This is the
    # tray's copy of the same control the dashboard and the composer expose.
    listen_action = QAction("Pause listening", menu)

    def toggle_listening():
        bridge.set_listening(not bridge.listening_enabled())

    listen_action.triggered.connect(toggle_listening)
    bridge.listeningChanged.connect(
        lambda listening: listen_action.setText(
            "Pause listening" if listening else "Start listening"))

    quit_action = QAction("Quit Kayra", menu)

    def quit_kayra():
        # Hand over to the backend's authoritative shutdown; it ends the process itself, and
        # takes the tray icon down through the pre-exit hook rather than here. The `app.quit()`
        # below is unreachable in practice and kept only for the case where the backend never
        # booted, leaving nothing to shut down.
        quit_action.setEnabled(False)
        bridge.shutdown(hard=True)
        app.quit()

    quit_action.triggered.connect(quit_kayra)

    menu.addAction(open_action)
    menu.addAction(ambient_action)
    menu.addSeparator()
    menu.addAction(listen_action)
    menu.addAction(stop_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    tray.activated.connect(
        lambda reason: window.show_control_centre()
        if reason == QSystemTrayIcon.Trigger else None)

    # The tooltip is the resolved voice state, verbatim. It used to be composed from the
    # assistant state plus a `listening_enabled()` read, which is the same two-fact
    # composition that produced "Idle · listening paused" while an STT session was
    # reconnecting. The ICON still follows the assistant state, because a taskbar icon is
    # about whether Kayra is busy — a different question with a different answer.
    bridge.stateChanged.connect(
        lambda state, previous: tray.setIcon(_app_icon(state)))
    bridge.voiceStateChanged.connect(
        lambda state, text, detail, revision: tray.setToolTip(f"Kayra — {text}"))
    tray.show()
    window.tray = tray
    return tray


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            ENTRY POINT                                 │
# └────────────────────────────────────────────────────────────────────────┘

def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    # High-DPI: Qt 6 scales by default, but the rounding policy matters on the 125% and 150%
    # scale factors Windows laptops ship with — PassThrough avoids the half-pixel seams that
    # Round produces on 1px borders, which this design uses heavily.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(argv)
    app.setApplicationName("Kayra")
    app.setOrganizationName("Kayra")
    # Closing the last window must not end the process: the tray keeps the assistant alive.
    app.setQuitOnLastWindowClosed(False)

    theme.apply(app)

    enable_voice = "--no-voice" not in argv
    bridge = KayraBridge(enable_voice=enable_voice)

    window = KayraWindow(bridge)
    ambient = AmbientAssistant(bridge, window)
    ambient.place_default()
    window.attach_ambient(ambient)

    # Showing the dashboard hides the panel through `showEvent` -> `_sync_presentation`.
    window.show()

    if QSystemTrayIcon.isSystemTrayAvailable():
        _build_tray(app, window, ambient, bridge)

    # PRESENTATION COMES OFF THE SCREEN LAST, not first.
    #
    # The backend's shutdown runs its resource teardown and then calls the hooks registered
    # here, so the window and the tray icon disappear only once the assistant behind them
    # genuinely has. Doing it the other way round — hiding the UI and then tearing down —
    # shows the user a finished shutdown while nine browser processes are still being reaped,
    # which is exactly the illusion that made "did it actually quit?" a real question.
    #
    # The hook does presentation and nothing else. It stops no threads, closes no browsers and
    # releases no resources; every one of those belongs to `app.request_shutdown` and has an
    # order that this must not second-guess.
    # THREAD AFFINITY, because this hook has two callers on two different threads.
    #
    # The Home button and the tray's Quit run on the GUI thread, where `hide()` is a direct,
    # legal call. A spoken "turn off Kayra" runs on the local-control watcher thread, where it
    # is not: touching a widget from a worker is undefined behaviour that usually appears to
    # work. So the hook checks which thread it is on and posts the call across when it has to,
    # through Qt's own queued invocation. The shutdown sequence gives each hook a two-second
    # budget, which the GUI thread — idle by this point — needs a single event-loop turn of.
    def _hide_presentation():
        targets = [w for w in (getattr(window, "tray", None), ambient, window) if w is not None]
        on_gui_thread = QThread.currentThread() is app.thread()
        for widget in targets:
            try:
                if on_gui_thread:
                    widget.hide()
                else:
                    QMetaObject.invokeMethod(widget, "hide", Qt.QueuedConnection)
            except Exception:
                pass

    try:
        from kayra import app as kayra_app
        kayra_app.on_before_exit(_hide_presentation)
    except Exception:
        pass

    # Boot AFTER the first paint. A zero-timer defers to the next event-loop turn, by which
    # point the window is on screen — this is what turns a 4.4s black screen into a window that
    # appears immediately and fills in.
    QTimer.singleShot(0, bridge.start)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
