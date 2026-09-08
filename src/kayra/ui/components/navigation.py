# ┌────────────────────────────────────────────────────────────────────────┐
# │                           navigation.py                                │
# │                    Sidebar Navigation & Brand Rail                     │
# └────────────────────────────────────────────────────────────────────────┘
"""
The left rail: brand, destinations, and the persistent assistant status footer.

Navigation is a QButtonGroup of checkable buttons rather than a QListWidget. That choice buys
three things a styled list cannot: real keyboard semantics (Tab reaches each item, Space
activates), per-item focus rings from the stylesheet, and the ability to put a live status
badge inside an item without fighting a delegate.

Icons are drawn as small geometric glyphs instead of loaded from an icon font or SVG set. A
font dependency would have to ship with the app and a raster set would need a copy per DPI;
eight primitives drawn with QPainter are sharp at any scale factor and cost nothing.
"""

import math

from PySide6.QtCore import Qt, Signal, QSize, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup, QLabel, QSizePolicy,
)

from kayra.ui.theme import (Color, Font, Space, Size, STATE_LABELS, STATE_COLORS,
                            repolish, glyph_dpr)
from kayra.ui.components.primitives import Caption, Divider, _label
from kayra.ui.components.orb import OrbBadge


def _glyph(kind, color, size=16, dpr=None):
    """
    A small monochrome pictogram, drawn at device-pixel-ratio for crisp high-DPI output.

    Deliberately abstract: a house, a speech line, a node graph. Recognisable at 16px is the
    only requirement, and detail at this size becomes mud.
    """
    dpr = glyph_dpr() if dpr is None else dpr
    pixmap = QPixmap(size * dpr, size * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    # LOGICAL units, not device pixels. QPainter on a pixmap with a devicePixelRatio works in
    # logical coordinates and scales up itself; drawing to `size * dpr` therefore painted at
    # twice the intended scale and only the top-left quarter of every icon was visible.
    s = float(size)
    m = s * 0.16                      # margin
    inner = s - 2 * m

    if kind == "home":
        painter.drawPolyline([QPointF(m, s * 0.48), QPointF(s / 2, m),
                              QPointF(s - m, s * 0.48)])
        painter.drawRect(QRectF(s * 0.24, s * 0.46, s * 0.52, s * 0.36))
    elif kind == "chat":
        painter.drawRoundedRect(QRectF(m, m, inner, inner * 0.72), s * 0.12, s * 0.12)
        painter.drawPolyline([QPointF(s * 0.3, m + inner * 0.72),
                              QPointF(s * 0.3, s - m), QPointF(s * 0.52, m + inner * 0.72)])
    elif kind == "automation":
        painter.drawEllipse(QPointF(s * 0.28, s * 0.28), s * 0.11, s * 0.11)
        painter.drawEllipse(QPointF(s * 0.72, s * 0.28), s * 0.11, s * 0.11)
        painter.drawEllipse(QPointF(s * 0.5, s * 0.75), s * 0.11, s * 0.11)
        painter.drawLine(QPointF(s * 0.34, s * 0.37), QPointF(s * 0.46, s * 0.65))
        painter.drawLine(QPointF(s * 0.66, s * 0.37), QPointF(s * 0.54, s * 0.65))
    elif kind == "memory":
        painter.drawRoundedRect(QRectF(m, m, inner, inner), s * 0.14, s * 0.14)
        painter.drawLine(QPointF(s * 0.32, s * 0.38), QPointF(s * 0.68, s * 0.38))
        painter.drawLine(QPointF(s * 0.32, s * 0.55), QPointF(s * 0.68, s * 0.55))
        painter.drawLine(QPointF(s * 0.32, s * 0.72), QPointF(s * 0.55, s * 0.72))
    elif kind == "activity":
        painter.drawPolyline([QPointF(m, s * 0.62), QPointF(s * 0.34, s * 0.62),
                              QPointF(s * 0.45, s * 0.32), QPointF(s * 0.58, s * 0.78),
                              QPointF(s * 0.68, s * 0.5), QPointF(s - m, s * 0.5)])
    elif kind == "system":
        painter.drawRoundedRect(QRectF(s * 0.26, s * 0.26, s * 0.48, s * 0.48), s * 0.08, s * 0.08)
        for i in range(3):
            offset = s * (0.36 + i * 0.14)
            painter.drawLine(QPointF(offset, m * 0.7), QPointF(offset, s * 0.26))
            painter.drawLine(QPointF(offset, s * 0.74), QPointF(offset, s - m * 0.7))
            painter.drawLine(QPointF(m * 0.7, offset), QPointF(s * 0.26, offset))
            painter.drawLine(QPointF(s * 0.74, offset), QPointF(s - m * 0.7, offset))
    elif kind == "settings":
        # Sliders, not a gear. A gear at 16px is a circle with six stubs radiating from it,
        # which renders as an asterisk or a sun — it did exactly that here, and it was the one
        # icon in the rail that did not read as the same family as the others. Two tracks with
        # a handle each is unambiguous at this size and matches the 1.4px line weight of every
        # other glyph.
        for index, (y, knob) in enumerate(((0.34, 0.62), (0.66, 0.38))):
            painter.drawLine(QPointF(m, s * y), QPointF(s - m, s * y))
            painter.drawEllipse(QPointF(s * knob, s * y), s * 0.10, s * 0.10)
    painter.end()

    icon = QIcon()
    icon.addPixmap(pixmap, QIcon.Normal)
    return icon


class NavItem(QPushButton):
    """One destination. Checkable; the stylesheet paints the active rule and fill."""

    def __init__(self, key, label, glyph, parent=None):
        super().__init__(label, parent)
        self.key = key
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIcon(_glyph(glyph, Color.text_secondary))
        self.setIconSize(QSize(16, 16))
        self.setFocusPolicy(Qt.StrongFocus)
        self._glyph = glyph
        self.toggled.connect(self._recolor)

    def _recolor(self, checked):
        # The icon is a pixmap, so the stylesheet cannot tint it; it is redrawn on state change.
        # That happens once per navigation, not per paint.
        self.setIcon(_glyph(self._glyph, Color.accent if checked else Color.text_secondary))


class Sidebar(QWidget):
    """
    Brand, destinations and a live status footer.

    Emits `navigate(key)`. It holds no application state of its own beyond which item is
    checked — the window owns routing.
    """

    navigate = Signal(str)

    DESTINATIONS = (
        ("home", "Home", "home"),
        ("chat", "Chat", "chat"),
        ("automation", "Automation", "automation"),
        ("memory", "Memory", "memory"),
        ("activity", "Activity", "activity"),
        ("system", "System", "system"),
        ("settings", "Settings", "settings"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(Size.sidebar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, Space.base, 0, Space.md)
        layout.setSpacing(0)

        # ── Brand ──
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(Space.base, 0, Space.base, 0)
        brand_row.setSpacing(Space.sm)
        self.badge = OrbBadge(9)
        brand = _label("KAYRA", "BrandMark")
        brand_row.addWidget(self.badge)
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(Space.lg)

        # ── Destinations ──
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._items = {}
        for key, label, glyph in self.DESTINATIONS:
            item = NavItem(key, label, glyph, self)
            self.group.addButton(item)
            self._items[key] = item
            item.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
            layout.addWidget(item)

        layout.addStretch(1)

        # ── Status footer: always-visible answer to "is it working?" ──
        layout.addWidget(Divider())
        footer = QVBoxLayout()
        footer.setContentsMargins(Space.base, Space.md, Space.base, 0)
        footer.setSpacing(Space.xxs)
        self.state_label = _label("Starting", "StatusName")
        self.detail_label = Caption("Bringing subsystems up")
        footer.addWidget(self.state_label)
        footer.addWidget(self.detail_label)

        # The microphone gets its own line. Folding it into the assistant state would mean
        # "Idle" had to mean both "waiting for you to speak" and "not listening at all",
        # which are opposite situations for the person reading it.
        self.listening_label = Caption("")
        self.listening_label.setObjectName("ListeningStatus")
        footer.addWidget(self.listening_label)
        layout.addLayout(footer)

    def select(self, key):
        item = self._items.get(key)
        if item is not None and not item.isChecked():
            item.setChecked(True)

    def set_state(self, state, detail=None):
        self.badge.set_state(state)
        self.state_label.setText(STATE_LABELS.get(state, state.title()))
        if detail is not None:
            self.detail_label.setText(detail)

    def set_voice(self, state, text, detail):
        """
        Renders the resolved voice state: the badge, the headline and the footer note.

        THE FOOTER NOTE IS NOW DRIVEN BY THE STATE, NOT BY A BOOLEAN. It used to be written
        from `listeningChanged` alone, so it said "Listening paused" for every reason the
        microphone was not producing words — including an STT session being rebuilt, which is
        the opposite of paused. Only the two states that genuinely mean "Kayra is not acting
        on what you say" show it now.
        """
        from kayra.core.voice_state import ORB_STATE, VoiceState

        self.badge.set_state(ORB_STATE.get(state, "IDLE"))
        self.state_label.setText(text)
        self.detail_label.setText(detail or "")

        quiet = state in (VoiceState.PAUSED, VoiceState.STANDBY)
        self.listening_label.setText("Listening paused" if quiet else "")
        self.listening_label.setProperty("paused", quiet)
        repolish(self.listening_label)
        self.listening_label.setVisible(quiet)

    def set_listening(self, listening):
        """
        Kept for the boot window, before the voice state machine has anything to say.

        Not the caption's owner any more: `set_voice` is. A second writer to the same label is
        precisely how this footer came to contradict the screen beside it.
        """
        quiet = not listening
        self.listening_label.setText("Listening paused" if quiet else "")
        self.listening_label.setProperty("paused", quiet)
        repolish(self.listening_label)
        self.listening_label.setVisible(quiet)
