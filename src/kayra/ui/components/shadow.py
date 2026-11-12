# ┌────────────────────────────────────────────────────────────────────────┐
# │                              shadow.py                                 │
# │          Drop Shadows Drawn Once, Not Re-rendered Every Paint          │
# └────────────────────────────────────────────────────────────────────────┘
"""
A soft drop shadow under a floating widget (the dock, the drawer panel) — and the two classes
that replace Qt's `QGraphicsDropShadowEffect` for them.

WHY
---
`QGraphicsDropShadowEffect` renders its widget OFFSCREEN and blurs the result on EVERY repaint
of that widget. The dock repaints on every hover lift and every state change, so each of those
paid for an offscreen copy of the whole pill plus a Gaussian blur on the CPU — at 2.25x scale,
a few hundred thousand pixels per hover frame for a decoration.

`ShadowLayer` is a sibling placed directly UNDER the target. It draws the shadow as a pixmap
built ONCE per size (a stack of rounded rectangles with falling alpha, which reads as a blur at
these radii), and follows the target's geometry and visibility through an event filter. A hover
repaint of the dock now repaints the dock and nothing else; the shadow is one cached blit.
"""

from PySide6.QtCore import Qt, QEvent, QRectF
from PySide6.QtGui import QPainter, QPixmap, QColor, QPainterPath
from PySide6.QtWidgets import QWidget


class ShadowLayer(QWidget):
    """A cached soft shadow that stays under `target` and moves, resizes and hides with it."""

    def __init__(self, target, radius, blur, offset_x=0, offset_y=0, alpha=170):
        super().__init__(target.parentWidget())
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._target = target
        self._radius = float(radius)
        self._blur = int(blur)
        self._offset = (int(offset_x), int(offset_y))
        self._alpha = int(alpha)
        self._cache = None
        self._cache_key = None
        target.installEventFilter(self)
        self.sync()

    def sync(self):
        target = self._target
        parent = self.parentWidget()
        if target.parentWidget() is not parent and target.parentWidget() is not None:
            self.setParent(target.parentWidget())
        geometry = target.geometry().adjusted(-self._blur, -self._blur, self._blur, self._blur)
        geometry.translate(*self._offset)
        self.setGeometry(geometry)
        self.setVisible(not target.isHidden())
        self.stackUnder(target)

    def eventFilter(self, watched, event):
        if watched is self._target and event.type() in (
                QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide, QEvent.ZOrderChange,
                QEvent.ParentChange):
            self.sync()
        return False

    def _build(self, width, height, ratio):
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        blur = max(1, self._blur)
        steps = max(6, blur // 2)
        # Concentric rounded rectangles from the outside in, each adding a little alpha, so
        # the edge falls off smoothly — a box-blur approximation computed once.
        per_step = self._alpha / 255.0 / steps * 1.6
        for index in range(steps):
            inset = blur * (1.0 - (index + 1) / steps)
            rect = QRectF(blur - inset, blur - inset,
                          width - 2 * (blur - inset), height - 2 * (blur - inset))
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            color = QColor(0, 0, 0)
            color.setAlphaF(min(1.0, per_step))
            path = QPainterPath()
            path.addRoundedRect(rect, self._radius + inset, self._radius + inset)
            painter.fillPath(path, color)
        painter.end()
        return pixmap

    def paintEvent(self, event):
        ratio = max(1.0, float(self.devicePixelRatioF() or 1.0))
        key = (self.width(), self.height(), ratio)
        if key != self._cache_key:
            self._cache = self._build(self.width(), self.height(), ratio)
            self._cache_key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

