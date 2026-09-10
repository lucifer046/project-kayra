# ┌────────────────────────────────────────────────────────────────────────┐
# │                               orb.py                                   │
# │                      The Kayra Assistant Visual                        │
# └────────────────────────────────────────────────────────────────────────┘
"""
Kayra's presence: a segmented ring around a core, drawn with QPainter.

THE DESIGN
----------
Three concentric elements, each carrying information rather than decoration:

  * an outer RING of discrete segments — the technical register of the whole interface, and
    the element that rotates and brightens to show activity;
  * an inner CORE that breathes, whose brightness follows the assistant's state;
  * a soft radial HALO in the state colour, which is what makes the thing feel lit rather
    than drawn.

Deliberately not a glowing sphere and not a reactor: the segmented ring reads as instrumentation,
which is the intended register — technical and calm, not cinematic.

Every state is distinguishable by MOTION as well as colour, so it survives a monochrome screen
and does not depend on hue discrimination:

    IDLE         slow shallow breath, segments still, present but quiet
    LISTENING    segments pulse outward in sequence, amber
    PROCESSING   ring rotates steadily, copper
    SPEAKING     core amplitude follows a waveform, bright gold
    AUTOMATING   two opposed arcs sweep, deep amber
    PROACTIVE    one soft swell around the whole ring, green
    ERROR        single slow red pulse, no rotation

A STATIC GAUGE, AND WHY
-----------------------
Outside the live segments sits a neutral hairline circle with four cardinal ticks. It never
animates and never takes the state colour. That is deliberate: it is the dial the moving parts
are read against, and it is what separates "instrument" from "glowing orb". Six primitives.

RESTING PRESENCE
----------------
Every segment keeps a floor of 0.22 alpha. An earlier version resolved to roughly 0.13 at
idle, which on a #0E0E10 ground is invisible — the assistant's primary visual rendered as a
faint smudge, and Home lost its focal point entirely.

COST
----
This is the only continuously animated element in the application, so its cost is bounded
explicitly rather than left to chance:

  * repaint is capped at 30fps while busy and drops to 12fps when idle — an idle assistant is
    the common case, and a still ring does not need 30 frames a second;
  * the timer STOPS COMPLETELY when the widget is hidden, so a minimised window or a
    background tab costs exactly nothing;
  * geometry is recomputed only on resize, never per frame;
  * there is no pixmap cache to invalidate and no compositing layer — one QPainter pass over
    roughly forty primitives, which is trivial for the software rasteriser.
"""

import math

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QRadialGradient, QBrush
from PySide6.QtWidgets import QWidget

from kayra.ui.theme import Color, Motion, STATE_COLORS

SEGMENTS = 48


class AssistantOrb(QWidget):
    """
    The assistant visual. Set `state` to any `AssistantState` value or a UI-only state.

    Emits `clicked` so it can double as the primary voice control in compact layouts.
    """

    clicked = Signal()

    def __init__(self, diameter=200, interactive=False, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._state = "IDLE"
        self._phase = 0.0            # advances every frame; drives all motion
        self._level = 0.0            # 0..1 activity envelope, eased toward the target
        self._target_level = 0.0
        self._interactive = interactive

        if interactive:
            self.setCursor(Qt.PointingHandCursor)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

        self._center = QPointF(diameter / 2, diameter / 2)
        self._radius = diameter / 2

    # ──────────────────────────────────────────────────────────────────
    #                              STATE
    # ──────────────────────────────────────────────────────────────────

    def state(self):
        return self._state

    def diameter(self):
        return self._diameter

    def set_diameter(self, diameter):
        """
        Resizes the orb. Home calls this on every window resize.

        A FIXED 208px ORB IN A COLUMN THAT GROWS TO 770px is a small drawing in a large empty
        space, and on a 1920px monitor that is most of what the middle of Home looks like.
        The orb is the one element on the page that genuinely earns more room, so it takes it.

        Everything here is already resize-safe: geometry is recomputed in `resizeEvent` and
        the paint is proportional to `self._radius`, so this costs one relayout and one
        repaint. Guarded on a real change, because a resize storm would otherwise call
        `setFixedSize` on every intermediate pixel width.
        """
        diameter = max(96, int(diameter))
        if diameter == self._diameter:
            return
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self._center = QPointF(diameter / 2, diameter / 2)
        self._radius = diameter / 2
        self.update()

    DEFAULT_LEVELS = {
        "IDLE": 0.26, "OFFLINE": 0.08, "STARTING": 0.45,
        "LISTENING": 0.85, "PROCESSING": 0.60, "SPEAKING": 0.95,
        "AUTOMATING": 0.70, "INTERRUPTING": 0.50, "PROACTIVE": 0.55,
        "ERROR": 0.35, "SHUTTING_DOWN": 0.08,
    }

    def set_state(self, state, amplitude=None):
        """
        Sets the visual state, and optionally its activity amplitude.

        THE ORB RENDERS; IT DOES NOT DECIDE. It has no timer that reasons about state, no
        inference from silence, and no opinion about whether the microphone is open — it is
        handed a state and an amplitude by `kayra.core.voice_state` through the bridge, and
        draws them. That is what makes it impossible for this component to be the thing that
        disagrees with the caption beside it.

        `amplitude` exists so LISTENING and USER_SPEAKING can be the SAME animation at
        different energies. The travelling wave is already the "I am hearing you" element; a
        second visual for the same fact would be one more thing that can fall out of step.
        """
        state = (state or "IDLE").upper()
        level = self.DEFAULT_LEVELS.get(state, 0.2) if amplitude is None \
            else max(0.0, min(1.0, float(amplitude)))
        if state == self._state and abs(level - self._target_level) < 0.01:
            return
        self._state = state
        # The activity envelope is a target rather than a jump, so a state change eases in over
        # a few frames instead of snapping — the difference between "alive" and "flickering".
        self._target_level = level
        self._sync_timer()
        self.update()

    def _color(self):
        return QColor(STATE_COLORS.get(self._state, Color.state_idle))

    # ──────────────────────────────────────────────────────────────────
    #                         ANIMATION CONTROL
    # ──────────────────────────────────────────────────────────────────
    # Hidden widgets do not animate. `showEvent`/`hideEvent` are the reliable hooks: they fire
    # for minimise, for tab changes inside a QStackedWidget, and when the ambient window is
    # dismissed to the tray.

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _sync_timer(self):
        if not self.isVisible():
            self._timer.stop()
            return
        busy = self._state not in ("IDLE", "OFFLINE", "SHUTTING_DOWN")
        fps = Motion.orb_fps if busy else Motion.orb_fps_idle
        self._timer.start(max(1, int(1000 / fps)))

    def _advance(self):
        self._phase += 0.06
        if self._phase > math.tau * 1000:
            self._phase = 0.0                       # keep the float small and exact
        # Ease the envelope toward its target: fast to rise, slower to settle.
        delta = self._target_level - self._level
        self._level += delta * (0.25 if delta > 0 else 0.12)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._center = QPointF(self.width() / 2, self.height() / 2)
        self._radius = min(self.width(), self.height()) / 2

    def mousePressEvent(self, event):
        if self._interactive and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    # ──────────────────────────────────────────────────────────────────
    #                             PAINTING
    # ──────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        accent = self._color()
        cx, cy = self._center.x(), self._center.y()
        outer = self._radius * 0.93
        inner = self._radius * 0.63

        self._paint_halo(painter, cx, cy, accent)
        self._paint_gauge(painter, cx, cy, outer)
        self._paint_ring(painter, cx, cy, outer, inner, accent)
        self._paint_core(painter, cx, cy, inner, accent)
        painter.end()

    def _paint_gauge(self, painter, cx, cy, outer):
        """
        A static hairline circle just outside the segments, plus four cardinal ticks.

        This is what makes the thing read as an INSTRUMENT rather than a glowing blob. It is
        neutral, never animated and never coloured by state: it is the dial the moving parts
        are read against, and a dial that moves is not a dial. Six primitives, no per-frame
        maths — it costs nothing.
        """
        if self._radius < 40:
            return                                  # too small to be anything but noise
        ring = QColor(Color.border)
        pen = QPen(ring)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        gauge = outer + self._radius * 0.045
        painter.drawEllipse(QPointF(cx, cy), gauge, gauge)

        tick = QColor(Color.border_strong)
        pen.setColor(tick)
        pen.setWidthF(1.2)
        painter.setPen(pen)
        for index in range(4):
            angle = index * math.pi / 2 - math.pi / 2
            painter.drawLine(
                QPointF(cx + math.cos(angle) * gauge, cy + math.sin(angle) * gauge),
                QPointF(cx + math.cos(angle) * (gauge + self._radius * 0.05),
                        cy + math.sin(angle) * (gauge + self._radius * 0.05)))

    def _paint_halo(self, painter, cx, cy, accent):
        """A soft radial wash. Cheap, and it is what stops the ring looking like clip art."""
        strength = 0.14 + 0.24 * self._level
        gradient = QRadialGradient(cx, cy, self._radius)
        glow = QColor(accent)
        glow.setAlphaF(strength)
        mid = QColor(accent)
        mid.setAlphaF(strength * 0.35)
        gradient.setColorAt(0.0, glow)
        gradient.setColorAt(0.55, mid)
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(QPointF(cx, cy), self._radius, self._radius)

    def _paint_ring(self, painter, cx, cy, outer, inner, accent):
        """
        The segmented ring. Each segment's brightness is a function of the state's motion model,
        which is what makes the states distinguishable without relying on colour.
        """
        thickness = max(2.0, self._radius * 0.05)
        pen = QPen()
        pen.setWidthF(thickness)
        pen.setCapStyle(Qt.RoundCap)      # round caps stop the ring reading as a saw blade

        rotation = self._ring_rotation()

        for index in range(SEGMENTS):
            fraction = index / SEGMENTS
            angle = fraction * math.tau + rotation
            intensity = self._segment_intensity(fraction)

            # Every segment keeps a floor of presence. At the old 0.13 resting alpha the whole
            # ring was invisible against #0E0E10 — the assistant's primary visual read as a
            # smudge, which is the single worst thing this component can do.
            color = QColor(accent)
            color.setAlphaF(max(0.22, min(1.0, intensity)))
            pen.setColor(color)
            painter.setPen(pen)

            length = outer - inner
            reach = 0.35 + 0.65 * min(1.0, intensity)
            start = QPointF(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner)
            end = QPointF(cx + math.cos(angle) * (inner + length * reach),
                          cy + math.sin(angle) * (inner + length * reach))
            painter.drawLine(start, end)

    def _ring_rotation(self):
        if self._state == "PROCESSING":
            return self._phase * 0.9
        if self._state == "AUTOMATING":
            return self._phase * 0.5
        if self._state == "STARTING":
            return self._phase * 1.3
        return 0.0

    def _segment_intensity(self, fraction):
        """Per-segment brightness in 0..1. One branch per state; all cheap trigonometry."""
        state = self._state
        base = 0.30 + 0.30 * self._level

        if state == "LISTENING":
            # A travelling wave: segments brighten in sequence around the ring.
            wave = math.sin(fraction * math.tau * 2 - self._phase * 2.2)
            return base + 0.65 * self._level * max(0.0, wave) ** 2

        if state == "SPEAKING":
            # Two overlaid harmonics read as an amplitude envelope rather than a rotation.
            a = math.sin(fraction * math.tau * 3 - self._phase * 3.1)
            b = math.sin(fraction * math.tau * 5 + self._phase * 1.7)
            return base + 0.55 * self._level * abs(a * 0.6 + b * 0.4)

        if state == "PROCESSING":
            # A bright arc chasing the rotation, with a decaying tail.
            head = (fraction * math.tau) % math.tau
            return base + 0.75 * self._level * max(0.0, math.cos(head - 0.0)) ** 6

        if state == "AUTOMATING":
            # Two opposed sweeps: unmistakably "working on something", not "listening".
            a = max(0.0, math.cos(fraction * math.tau)) ** 8
            b = max(0.0, math.cos(fraction * math.tau + math.pi)) ** 8
            return base + 0.70 * self._level * (a + b)

        if state == "ERROR":
            return base + 0.45 * abs(math.sin(self._phase * 0.9))

        if state == "PROACTIVE":
            # A single soft swell around the whole ring: present, unhurried, easy to ignore.
            return base + 0.30 * self._level * (0.5 + 0.5 * math.sin(self._phase * 0.7))

        # IDLE and everything unlisted: a slow, even breath. Shallow on purpose — an idle
        # assistant should be visibly THERE and visibly doing nothing.
        return base + 0.18 * (0.5 + 0.5 * math.sin(self._phase * 0.55))

    def _paint_core(self, painter, cx, cy, inner, accent):
        """The centre disc. Its radius breathes; its brightness follows the activity envelope."""
        breath = 0.5 + 0.5 * math.sin(self._phase * (1.6 if self._state == "SPEAKING" else 0.6))
        radius = inner * (0.42 + 0.10 * breath * (0.3 + self._level))

        gradient = QRadialGradient(cx, cy, radius)
        core = QColor(accent)
        core.setAlphaF(0.30 + 0.50 * self._level)
        edge = QColor(accent)
        edge.setAlphaF(0.0)
        gradient.setColorAt(0.0, core)
        gradient.setColorAt(0.7, QColor(accent.red(), accent.green(), accent.blue(),
                                        int(60 * (0.3 + self._level))))
        gradient.setColorAt(1.0, edge)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # A hairline containing circle. Without it the core dissolves into the halo and the
        # whole visual loses its edge.
        outline = QColor(accent)
        outline.setAlphaF(0.40 + 0.35 * self._level)
        pen = QPen(outline)
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), inner * 0.52, inner * 0.52)


class OrbBadge(QWidget):
    """
    A small state dot for dense contexts — the title bar, the tray tooltip, a list row.

    Shares the orb's colour vocabulary so state reads identically everywhere, but draws two
    primitives instead of fifty and animates only when the state is one that matters.
    """

    def __init__(self, diameter=10, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter + 6, diameter + 6)
        self._state = "IDLE"
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_state(self, state):
        self._state = (state or "IDLE").upper()
        active = self._state in ("LISTENING", "SPEAKING", "PROCESSING", "AUTOMATING", "ERROR")
        if active and self.isVisible():
            self._timer.start(int(1000 / Motion.orb_fps_idle))
        else:
            self._timer.stop()
        self.update()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self):
        self._phase += 0.12
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(STATE_COLORS.get(self._state, Color.state_idle))
        center = QPointF(self.width() / 2, self.height() / 2)

        if self._timer.isActive():
            halo = QColor(color)
            halo.setAlphaF(0.18 + 0.22 * (0.5 + 0.5 * math.sin(self._phase)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(center, self._diameter / 2 + 3, self._diameter / 2 + 3)

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(center, self._diameter / 2, self._diameter / 2)
        painter.end()
