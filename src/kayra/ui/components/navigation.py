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

from PySide6.QtCore import (Qt, Signal, QSize, QRect, QRectF, QPointF,
                            QPropertyAnimation, QEasingCurve)
from PySide6.QtGui import QPainter, QColor, QPen, QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup, QLabel, QSizePolicy,
)

from kayra.ui.theme import (Color, Font, Space, Size, Motion, STATE_LABELS, STATE_COLORS,
                            repolish, glyph_dpr)
from kayra.ui.components.primitives import Caption, Divider, _label
from kayra.ui.components.orb import OrbBadge


# THE ONE LIST OF DESTINATIONS. Both navigation surfaces read it: the persistent rail on the
# five utility screens and the sliding drawer on Home and Chat. Two copies would be two places
# to add a screen, and the second one would be forgotten.
DESTINATIONS = (
    ("home", "Home", "home"),
    ("chat", "Chat", "chat"),
    ("automation", "Automation", "automation"),
    ("memory", "Memory", "memory"),
    ("activity", "Activity", "activity"),
    ("system", "System", "system"),
    ("settings", "Settings", "settings"),
)


def _motion_allowed():
    """
    Whether shell transitions may animate.

    Reads the SAME platform preference the ambient backdrop reads, so a user who has asked
    for reduced motion gets one consistent answer across the whole interface rather than a
    still backdrop beside a sliding rail.
    """
    try:
        from kayra.ui.components.backdrop import prefers_reduced_motion
        return not prefers_reduced_motion()
    except Exception:
        return True


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


class NavIndicator(QWidget):
    """
    The amber rail marking the active destination, which SLIDES between items.

    WHY THIS IS A WIDGET AND NOT A STYLESHEET RULE. The active item used to be marked by
    `QPushButton#NavItem:checked { border-left: 2px solid accent }`. Qt applies a property
    selector instantly and there is nothing to animate — so changing screens made the mark
    vanish from one row and appear on another in the same frame, which is precisely the
    "sudden" feel this pass is here to remove. A separate widget has a geometry, and a
    geometry can be animated.

    It is a SIBLING of the items, positioned over the rail's left edge, so it never
    participates in the column's layout and cannot shift the rows it marks. Nothing here
    polls: the animation runs only while it is travelling.
    """

    WIDTH = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedWidth(self.WIDTH)
        self.hide()

        self._animation = QPropertyAnimation(self, b"geometry", self)
        self._animation.setDuration(Motion.indicator)
        # OutCubic: fast away, settling in. A rail that decelerates into its destination
        # reads as having arrived; a linear one reads as having been dragged.
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def move_to(self, item, animate=True):
        """
        Travels to `item`. Pass `animate=False` for the first paint and for reduced motion —
        an indicator that animates in from nowhere on startup is motion with nothing to say.
        """
        if item is None:
            self.hide()
            return
        parent = self.parentWidget()
        if parent is None:
            return
        top_left = item.mapTo(parent, QPointF(0, 0).toPoint())
        # Inset vertically so the rail marks the item rather than spanning it edge to edge.
        inset = max(4, item.height() // 5)
        target = QRect(top_left.x(), top_left.y() + inset,
                       self.WIDTH, max(2, item.height() - 2 * inset))

        self._animation.stop()
        if not animate or self.isHidden():
            self.setGeometry(target)
            self.show()
            self.raise_()
            return
        self._animation.setStartValue(self.geometry())
        self._animation.setEndValue(target)
        self._animation.start()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(Color.accent))
        painter.drawRoundedRect(self.rect(), 1.0, 1.0)
        painter.end()


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

    # Kept as a class attribute because callers reach for it there (the window builds its
    # Ctrl+1..7 shortcuts from it). It IS the module-level tuple, not a second copy.
    DESTINATIONS = DESTINATIONS

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(Size.sidebar)

        layout = QVBoxLayout(self)
        # Padded on both sides now, so the rail reads as a COLUMN OF CARDS floating in a
        # panel rather than as full-bleed rows butted against the window edge. That single
        # change is most of what separates the new rail from a generic admin sidebar.
        layout.setContentsMargins(Space.md, Space.base, Space.md, Space.md)
        layout.setSpacing(0)

        # ── NO BRAND MARK HERE ──
        # The name used to head this rail, and it was the THIRD "KAYRA" on the screen: the
        # title bar carries it, Home's identity block carries it, and this one repeated it a
        # few pixels from both. A wordmark earns its place by telling the reader whose
        # software this is; the third instance tells them nothing and costs the rail its
        # quietest, most useful space. The destinations start at the top instead.
        #
        # `self.badge` SURVIVES, because it was never branding — it is the live assistant
        # state dot, driven by `set_state`/`set_voice_state` below, and it now sits in the
        # status footer next to the state it has always been reporting. Deleting it would
        # have meant deleting a status readout while removing a logo.

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

        # A CHILD OF THE RAIL, not of the column: it is positioned over the items rather than
        # laid out among them, so it can move without disturbing a single row.
        self.indicator = NavIndicator(self)
        self._indicator_placed = False

        layout.addStretch(1)

        # ── Status footer: always-visible answer to "is it working?" ──
        layout.addWidget(Divider())
        footer = QVBoxLayout()
        footer.setContentsMargins(Space.sm, Space.md, Space.sm, 0)
        footer.setSpacing(Space.xxs)
        self.badge = OrbBadge(9)
        self.state_label = _label("Starting", "StatusName")
        # The dot and the word it stands for, on one line. They were two readings of one
        # fact at opposite ends of the rail before.
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(Space.xs)
        state_row.addWidget(self.badge)
        state_row.addWidget(self.state_label)
        state_row.addStretch(1)
        footer.addLayout(state_row)
        self.detail_label = Caption("Bringing subsystems up")
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
        if item is None:
            return
        if not item.isChecked():
            item.setChecked(True)
        # The FIRST placement is instant. An indicator that slides in from the top-left on
        # startup is animating a transition that never happened.
        self.indicator.move_to(item, animate=self._indicator_placed and _motion_allowed())
        self._indicator_placed = True

    def resizeEvent(self, event):
        # The items move when the rail is resized, so the mark that points at one has to
        # follow — without animating, because a window resize is not a navigation.
        super().resizeEvent(event)
        for key, item in self._items.items():
            if item.isChecked():
                self.indicator.move_to(item, animate=False)
                break

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
