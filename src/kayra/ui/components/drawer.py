# ┌────────────────────────────────────────────────────────────────────────┐
# │                             drawer.py                                  │
# │              The Sliding Navigation Panel (Home & Chat)                │
# └────────────────────────────────────────────────────────────────────────┘
"""
Navigation for the two screens that do not carry a permanent rail.

WHY AN OVERLAY AND NOT A COLLAPSING COLUMN
------------------------------------------
A drawer that pushes the page aside relayouts everything underneath it — on Home that means
the orb slides, the backdrop's bloom moves, and every elided caption re-elides, twice per
open. An overlay changes nothing below it: the page keeps its geometry, the panel slides in
over the top, and closing it is a pure repaint. That is also why opening it is cheap enough
to animate at all.

THE SCRIM IS PART OF THE CONTROL, NOT DECORATION
------------------------------------------------
The dimmed sheet behind the panel does three jobs: it separates the panel from the content
so the glass edge is legible, it tells the user the page is temporarily not the subject, and
it is the click target that closes the drawer. A drawer that can only be closed by finding
the same small button again is a drawer people leave open.

STATE
-----
It holds exactly one piece: which destination is selected, and only so the correct row is
highlighted. The window owns routing; this emits `navigate(key)` and forgets.
"""

from PySide6.QtCore import (Qt, Signal, QPropertyAnimation, QEasingCurve, QRect,
                            QParallelAnimationGroup, QSequentialAnimationGroup,
                            QPauseAnimation, Property)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFrame, QButtonGroup,
                               QGraphicsDropShadowEffect, QGraphicsOpacityEffect)

from kayra.ui.theme import Color, Space, Size, Motion, Elevation
from kayra.ui.components.navigation import (NavItem, NavIndicator, DESTINATIONS,
                                            _motion_allowed)
from kayra.ui.components.primitives import Caption, Divider, _label
from kayra.ui.components.orb import OrbBadge


class NavigationDrawer(QWidget):
    """
    A glass panel that slides in from the left, over the page.

    Sized and positioned by `sync_geometry()`, which the host calls on resize. It is a child
    of the page container rather than a top-level window, so it is clipped to the content
    area and cannot spill over the window chrome.
    """

    navigate = Signal(str)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DrawerHost")
        self._open = False
        self._animation = None
        self.hide()

        # ── The scrim ──
        # A plain child widget rather than a painted rect on this one, because it has to be
        # the click target: `mousePressEvent` on the scrim closes, and clicks on the panel
        # above it do not reach here.
        self.scrim = _Scrim(self)
        self.scrim.clicked.connect(self.close_drawer)

        # ── The panel ──
        self.panel = QFrame(self)
        self.panel.setObjectName("DrawerPanel")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        self.panel.setFixedWidth(Size.drawer_width)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(Space.md, Space.lg, Space.md, Space.base)
        layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(Space.sm, 0, Space.sm, 0)
        brand_row.setSpacing(Space.sm)
        self.badge = OrbBadge(9)
        brand_row.addWidget(self.badge)
        brand_row.addWidget(_label("KAYRA", "BrandMark"))
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(Space.lg)

        # ONE HOST FOR THE ROWS, AND ONE EFFECT ON IT.
        #
        # The rows fade in slightly AFTER the panel starts moving, which is what makes the
        # drawer read as one gesture rather than as a rectangle that appears with its
        # contents already printed on it. Doing that per row would mean seven
        # QGraphicsOpacityEffects — seven offscreen composite buffers for a 220ms fade — so
        # they share a single effect on their container instead, and it is REMOVED when the
        # animation finishes so nothing composites while the drawer just sits there.
        self._items_host = QWidget(self.panel)
        items_layout = QVBoxLayout(self._items_host)
        items_layout.setContentsMargins(0, 0, 0, 0)
        items_layout.setSpacing(0)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._items = {}
        for key, label, glyph in DESTINATIONS:
            item = NavItem(key, label, glyph, self._items_host)
            self.group.addButton(item)
            self._items[key] = item
            item.clicked.connect(lambda _=False, k=key: self._choose(k))
            items_layout.addWidget(item)
        layout.addWidget(self._items_host)

        # The same sliding mark the permanent rail carries, so the two navigation surfaces
        # express "you are here" identically.
        self.indicator = NavIndicator(self._items_host)
        self._indicator_placed = False

        layout.addStretch(1)
        layout.addWidget(Divider())
        footer = QVBoxLayout()
        footer.setContentsMargins(Space.sm, Space.md, Space.sm, 0)
        footer.setSpacing(Space.xxs)
        self.state_label = _label("Starting", "StatusName")
        self.detail_label = Caption("Bringing subsystems up")
        footer.addWidget(self.state_label)
        footer.addWidget(self.detail_label)
        layout.addLayout(footer)

        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(Elevation.drawer_shadow_blur)
        shadow.setOffset(Elevation.drawer_shadow_x, 0)
        shadow.setColor(QColor(0, 0, 0, Elevation.drawer_shadow_alpha))
        self.panel.setGraphicsEffect(shadow)

    # ──────────────────────────────────────────────────────────────────
    #                              OPENING
    # ──────────────────────────────────────────────────────────────────

    def is_open(self):
        return self._open

    def toggle(self):
        self.close_drawer() if self._open else self.open_drawer()

    def open_drawer(self):
        if self._open:
            return
        self._open = True
        self.sync_geometry()
        self.show()
        self.raise_()
        self._slide(to_open=True)
        # Focus lands on the selected row, so Tab and the arrow keys work from the moment it
        # opens rather than from the first click.
        for key, item in self._items.items():
            if item.isChecked():
                item.setFocus()
                break

    def close_drawer(self):
        if not self._open:
            return
        self._open = False
        self._slide(to_open=False)
        self.closed.emit()

    def _slide(self, to_open):
        """
        The panel travels, the scrim fades with it, and the rows arrive a beat later.

        THREE ANIMATIONS, ONE GESTURE — and they are composed rather than stacked. The panel
        and the scrim run together on the same duration so they read as one object; the rows
        run in SEQUENCE behind a short pause, so they land while the panel is still settling
        instead of racing it. Nothing here animates the same property twice, which is the
        specific way overlapping animations produce jitter.
        """
        if self._animation is not None:
            self._animation.stop()

        width = self.panel.width()
        start = self.panel.geometry()
        shown = QRect(0, 0, width, self.height())
        hidden = QRect(-width, 0, width, self.height())

        # REDUCED MOTION SKIPS THE TRAVEL, not the outcome. The drawer still opens and
        # closes; it simply arrives rather than sliding.
        if not _motion_allowed():
            self._clear_items_effect()
            self.panel.setGeometry(shown if to_open else hidden)
            self.scrim.opacity_level = 1.0 if to_open else 0.0
            if not to_open:
                self._finish_close()
            return

        slide = QPropertyAnimation(self.panel, b"geometry", self)
        slide.setDuration(Motion.drawer)
        # OutCubic arriving, InCubic leaving: a panel should decelerate into place and
        # accelerate away, which is what makes the two directions feel deliberate rather
        # than symmetrical.
        slide.setEasingCurve(QEasingCurve.OutCubic if to_open else QEasingCurve.InCubic)
        slide.setStartValue(start if start.width() == width else (hidden if to_open else shown))
        slide.setEndValue(shown if to_open else hidden)

        fade = QPropertyAnimation(self.scrim, b"opacity_level", self)
        fade.setDuration(Motion.drawer)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.setStartValue(self.scrim.opacity_level)
        fade.setEndValue(1.0 if to_open else 0.0)

        group = QParallelAnimationGroup(self)
        group.addAnimation(slide)
        group.addAnimation(fade)
        group.addAnimation(self._items_animation(to_open))
        if not to_open:
            # Hidden only once the panel has finished leaving, or it would vanish mid-slide.
            group.finished.connect(self._finish_close)
        # The effect comes OFF at the end, whichever direction this was: a permanent
        # QGraphicsOpacityEffect means every repaint of those seven rows goes through an
        # offscreen buffer for the rest of the session.
        group.finished.connect(self._clear_items_effect)
        # KEPT ON SELF. A QPropertyAnimation garbage-collected mid-flight simply stops, which
        # looks exactly like a rendering glitch — the same reason `fade_in` returns its
        # animation for the caller to hold.
        self._animation = group
        group.start()

    def _items_animation(self, to_open):
        """
        The rows' fade, delayed on the way in and immediate on the way out.

        Opening, the pause is what creates the sense of the panel arriving first and its
        contents settling into it. Closing, there is nothing to stagger — the whole panel is
        leaving, and holding the rows back would just make them visible against a wall that
        has already moved.
        """
        effect = self._items_host.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(self._items_host)
            self._items_host.setGraphicsEffect(effect)
        effect.setOpacity(effect.opacity() if to_open else 1.0)

        fade = QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(Motion.drawer_items)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.setStartValue(0.0 if to_open else 1.0)
        fade.setEndValue(1.0 if to_open else 0.0)
        if not to_open:
            return fade

        sequence = QSequentialAnimationGroup(self)
        sequence.addAnimation(QPauseAnimation(Motion.drawer_stagger, self))
        sequence.addAnimation(fade)
        return sequence

    def _clear_items_effect(self):
        """Drops the composite layer once the motion is over. Opacity is restored first."""
        effect = self._items_host.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(1.0)
        self._items_host.setGraphicsEffect(None)

    def _finish_close(self):
        if not self._open:
            self.hide()

    def _choose(self, key):
        self.navigate.emit(key)
        # Closing on navigation is the right default: the drawer exists to LEAVE these two
        # screens, so keeping it open over the page the user just asked for would mean two
        # gestures for one intention.
        self.close_drawer()

    # ──────────────────────────────────────────────────────────────────
    #                            GEOMETRY
    # ──────────────────────────────────────────────────────────────────

    def sync_geometry(self):
        """Fills the host. Called by the window on every resize; costs two setGeometry calls."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.setGeometry(0, 0, parent.width(), parent.height())
        self.scrim.setGeometry(0, 0, self.width(), self.height())
        width = self.panel.width()
        self.panel.setGeometry(0 if self._open else -width, 0, width, self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scrim.setGeometry(0, 0, self.width(), self.height())
        width = self.panel.width()
        self.panel.setGeometry(self.panel.x(), 0, width, self.height())

    # ──────────────────────────────────────────────────────────────────
    #                              STATE
    # ──────────────────────────────────────────────────────────────────

    def select(self, key):
        item = self._items.get(key)
        if item is None:
            return
        if not item.isChecked():
            item.setChecked(True)
        self.indicator.move_to(item, animate=self._indicator_placed and _motion_allowed())
        self._indicator_placed = True

    def set_voice(self, state, text, detail):
        """Mirrors the sidebar's footer, from the same resolved state. Derives nothing."""
        from kayra.core.voice_state import ORB_STATE

        self.badge.set_state(ORB_STATE.get(state, "IDLE"))
        self.state_label.setText(text)
        self.detail_label.setText(detail or "")

    def keyPressEvent(self, event):
        # Escape closes. Expected of every overlay, and the drawer has focus while it is open.
        if event.key() == Qt.Key_Escape:
            self.close_drawer()
            return
        super().keyPressEvent(event)


class _Scrim(QWidget):
    """
    The dimmed sheet behind the panel: separator, signal, and close target in one widget.

    Its opacity is a Qt PROPERTY rather than a plain attribute so QPropertyAnimation can
    drive it directly. Animating a QGraphicsOpacityEffect instead would composite the whole
    sheet into an offscreen buffer every frame, which for a flat fill is pure waste.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._opacity = 0.0
        self.setCursor(Qt.ArrowCursor)

    def _get_opacity(self):
        return self._opacity

    def _set_opacity(self, value):
        self._opacity = max(0.0, min(1.0, float(value)))
        self.update()

    # The accessors are named privately so the PROPERTY owns the public name. Defining a
    # method and a Property with the same identifier leaves `scrim.opacity_level` resolving
    # to a float that callers then try to call.
    opacity_level = Property(float, _get_opacity, _set_opacity)

    def paintEvent(self, event):
        if self._opacity <= 0.0:
            return
        painter = QPainter(self)
        color = QColor(Color.base)
        # Never fully opaque: the page has to stay visible underneath or the drawer reads as
        # a new screen rather than as a panel over this one.
        color.setAlphaF(0.62 * self._opacity)
        painter.fillRect(self.rect(), color)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
