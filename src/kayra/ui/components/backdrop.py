# ┌────────────────────────────────────────────────────────────────────────┐
# │                            backdrop.py                                 │
# │                  The Ambient Layer Behind Everything                   │
# └────────────────────────────────────────────────────────────────────────┘
"""
The faint moving ground the whole application sits on.

WHAT IT IS, AND WHAT IT DELIBERATELY IS NOT
-------------------------------------------
Four layers, in order, none of them loud:

  1. a vertical wash, top slightly lighter than bottom — the only thing giving the window a
     sense of up and down;
  2. a geometric hairline lattice, drawn once into a pixmap and never repainted;
  3. a warm radial bloom, positioned by the view (Home puts it behind the orb) — this is
     what makes the orb look like it is LIT rather than pasted on;
  4. a small number of slow drifting motes.

There is NO photograph, no noise texture, no neon and no circuit-board clip art. A
photographic backdrop behind text on a near-black ground costs contrast the type cannot
spare, and a "cyber" pattern competes with the one element on Home that is meant to hold the
eye. Everything here is drawn from the theme's own tokens, so it is Kayra's rather than
stock, and it stays within a few points of the base ground.

THE COST, WHICH IS THE REASON IT IS SHAPED THIS WAY
---------------------------------------------------
This paints behind every screen, so it is bounded harder than the orb is:

  * 8 frames per second, and a 90-second motion cycle. The movement is felt, not watched —
    which is both the aesthetic requirement and the reason it costs a fraction of what the
    orb does.
  * the lattice is rasterised ONCE per resize into a pixmap. Redrawing ~40 hairlines every
    frame would be the single most expensive thing in the application, and it never changes.
  * the timer STOPS COMPLETELY when the widget is hidden, so a minimised window costs zero.
  * motes are a fixed-length list generated once. Nothing is appended per frame, so there is
    no collection here that can grow.
  * `WA_OpaquePaintEvent` — this widget always fills its whole rect, so Qt is told not to
    clear it first. That halves the fill work on every frame.

REDUCED MOTION
--------------
`set_animated(False)` stops the timer and paints one still frame. The window turns it off
when the platform reports a reduced-motion preference, and the still frame is a complete
picture rather than a blank one — nothing is lost except the drift.
"""

import math
import random

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QPixmap, QRadialGradient, QLinearGradient
from PySide6.QtWidgets import QWidget

from kayra.ui.theme import Color, Motion


class AmbientBackdrop(QWidget):
    """
    The animated ground. Add it as the FIRST child of a container and it stays behind.

    It is transparent to the mouse, so it never intercepts a click meant for the content
    above it — a full-bleed backdrop that swallowed clicks would break every control on the
    page.
    """

    MOTES = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # This widget fills its entire rect on every paint, so Qt need not clear it first.
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.lower()

        self._phase = 0.0
        self._animated = True
        self._lattice = None            # cached QPixmap, rebuilt on resize only
        # Bloom focus in NORMALISED coordinates, so it survives every resize without the
        # view having to recompute a pixel position. Home moves it behind the orb.
        self._focus = QPointF(0.5, 0.40)
        self._bloom_strength = 1.0

        # Deterministic: seeded so the pattern is the same every launch. A backdrop that
        # rearranged itself between sessions would be noticeable in exactly the way this is
        # designed not to be.
        rng = random.Random(0x4159)
        self._motes = [(rng.random(), rng.random(),
                        0.35 + rng.random() * 0.9,          # drift speed
                        0.6 + rng.random() * 1.9)           # radius, logical px
                       for _ in range(self.MOTES)]

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(1000 / Motion.backdrop_fps)))
        self._timer.timeout.connect(self._advance)

    # ──────────────────────────────────────────────────────────────────
    #                             CONTROL
    # ──────────────────────────────────────────────────────────────────

    def set_focus(self, x, y, strength=1.0):
        """
        Places the warm bloom, in normalised 0..1 coordinates.

        Home calls this with the orb's centre so the glow reads as the orb's own light
        spilling onto the page. Chat pulls it to the top and dims it, because there the
        transcript is the subject and a bloom in the middle of the text would be a
        distraction rather than depth.
        """
        self._focus = QPointF(max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
        self._bloom_strength = max(0.0, min(1.5, strength))
        self.update()

    def set_animated(self, animated):
        """Reduced motion: stop drifting, keep the picture."""
        self._animated = bool(animated)
        self._sync_timer()
        self.update()

    # Hidden widgets do not animate. `showEvent`/`hideEvent` fire for minimise, for tab
    # changes inside a QStackedWidget, and when the window is dismissed to the tray — the
    # same hooks the orb relies on, for the same reason.
    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _sync_timer(self):
        if self._animated and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def _advance(self):
        # One full cycle per `backdrop_cycle_s`. Kept small and exact by wrapping at tau.
        self._phase += math.tau / (Motion.backdrop_cycle_s * Motion.backdrop_fps)
        if self._phase > math.tau:
            self._phase -= math.tau
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._lattice = None            # the static layer is size-dependent; rebuild lazily

    # ──────────────────────────────────────────────────────────────────
    #                             PAINTING
    # ──────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            painter.end()
            return

        self._paint_wash(painter, width, height)

        if self._lattice is None:
            self._lattice = self._build_lattice(width, height)
        if self._lattice is not None:
            painter.drawPixmap(0, 0, self._lattice)

        self._paint_bloom(painter, width, height)
        self._paint_motes(painter, width, height)
        painter.end()

    def _paint_wash(self, painter, width, height):
        """The vertical ground. Two stops, four points of lightness between them."""
        gradient = QLinearGradient(0.0, 0.0, 0.0, float(height))
        gradient.setColorAt(0.0, QColor(Color.backdrop_top))
        gradient.setColorAt(1.0, QColor(Color.backdrop_bottom))
        painter.fillRect(0, 0, width, height, gradient)

    def _build_lattice(self, width, height):
        """
        The static geometry, rasterised once.

        A shallow diagonal lattice rather than a square grid: a square grid on a dark UI
        reads as a spreadsheet, and a diagonal one reads as depth. The spacing is coarse
        (96px) and the alpha is a few points above the ground, so at normal viewing distance
        it registers as texture rather than as lines.

        Returned at the device pixel ratio so the hairlines stay 1px on a scaled display
        instead of being upscaled into grey smears.
        """
        if width <= 0 or height <= 0:
            return None
        ratio = max(1.0, float(self.devicePixelRatioF() or 1.0))
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(Color.backdrop_grid))
        pen.setWidthF(1.0)
        painter.setPen(pen)

        spacing = 96
        # Two opposed diagonals, drawn far enough past each edge that the pattern is
        # continuous in the corners.
        span = width + height
        for offset in range(-height, span, spacing):
            painter.drawLine(offset, 0, offset + height, height)
        pen.setColor(QColor(Color.backdrop_grid))
        painter.setPen(pen)
        for offset in range(0, span + height, spacing * 2):
            painter.drawLine(offset, 0, offset - height, height)

        # One horizon hairline at the golden ratio. It is the only straight horizontal in the
        # backdrop and it quietly anchors the composition.
        pen.setColor(QColor(Color.backdrop_grid))
        painter.setPen(pen)
        painter.drawLine(0, int(height * 0.618), width, int(height * 0.618))
        painter.end()
        return pixmap

    def _paint_bloom(self, painter, width, height):
        """
        The warm radial glow. Breathes very slowly; this is the only animated large fill.

        Sized against the SHORTER edge so an ultrawide window does not get a bloom stretched
        across half a metre of screen, and a tall narrow one still gets a visible one.
        """
        if self._bloom_strength <= 0.0:
            return
        breath = 0.5 + 0.5 * math.sin(self._phase * 2.0)
        radius = min(width, height) * (0.62 + 0.05 * breath) * self._bloom_strength
        if radius <= 1.0:
            return
        center = QPointF(self._focus.x() * width, self._focus.y() * height)

        gradient = QRadialGradient(center, radius)
        warm = QColor(Color.backdrop_bloom)
        inner = QColor(warm)
        inner.setAlphaF(min(1.0, 0.30 * self._bloom_strength))
        mid = QColor(warm)
        mid.setAlphaF(min(1.0, 0.11 * self._bloom_strength))
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(0.45, mid)
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))

        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)

    def _paint_motes(self, painter, width, height):
        """
        Slow drifting specks. Eighteen of them, and that number is a decision.

        Enough that the layer is alive at a glance; few enough that no one counts them and
        that the whole layer is under twenty ellipses per frame. They rise, wrap at the top,
        and drift sideways on a long sine so nothing moves in a straight line.
        """
        painter.setPen(Qt.NoPen)
        base = QColor(Color.backdrop_particle)
        for index, (x0, y0, speed, radius) in enumerate(self._motes):
            travel = (y0 - self._phase * speed * 0.16) % 1.0
            sway = math.sin(self._phase * 1.7 + index) * 0.012
            x = (x0 + sway) % 1.0
            # Fade in and out at the extremes so a mote never pops as it wraps.
            fade = math.sin(travel * math.pi)
            color = QColor(base)
            color.setAlphaF(0.05 + 0.20 * fade)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x * width, travel * height), radius, radius)


def prefers_reduced_motion():
    """
    True when the platform asks for reduced motion.

    Qt has no cross-platform accessor for this, so the answer is read from the two places
    that DO carry it on Windows: the `KAYRA_REDUCED_MOTION` override (which is also how the
    test suite asserts the behaviour without touching the machine's settings), and the
    system's own animation preference through `SystemParametersInfoW`.

    Anything unreadable returns False — a machine that cannot be asked has not asked for
    reduced motion, and defaulting to "off" would silently strip the design from every user
    whose platform we failed to query.
    """
    import os

    override = (os.environ.get("KAYRA_REDUCED_MOTION") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False

    try:
        import ctypes

        SPI_GETCLIENTAREAANIMATION = 0x1042
        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(ok) and enabled.value == 0
    except Exception:
        return False
