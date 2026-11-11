# ┌────────────────────────────────────────────────────────────────────────┐
# │                           smooth_scroll.py                             │
# │        Wheel, touchpad and keys that GLIDE instead of jumping          │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §11, S1 + S3 (Part 11). Measured before: Qt moved each wheel notch as ONE
60 px jump, in one frame. Every scroll area in the interface gets this helper
(`install_smooth_scroll`), so scrolling looks the same everywhere:

* **Mouse wheel** — a notch glides with an ease-out (exponential approach, ~95 % of the way in
  ~170 ms). Notches that arrive while a glide is running ADD to its target: a fast spin of the
  wheel is one long continuous glide, never a stutter of restarts.
* **Touchpad** — pixel deltas follow the fingers 1:1 (no glide: a glide behind a finger feels
  like lag). When the fingers lift with speed, the page COASTS and slows down (kinetic
  momentum, ~0.33 s time constant), and stops at the ends.
* **Keyboard** — PageUp / PageDown / Home / End / Up / Down glide too. A key typed into a text
  field is never taken.
* **Frame pacing** — one step per display refresh: the timer runs at the screen's own refresh
  rate (60, 120, 144 Hz…), not a fixed 16 ms. (`QWindow.requestUpdate` was considered; on a
  widget window it is the repaint trigger itself, so a timer at the refresh rate is the
  simpler equivalent here.)
* **Reduced motion** — off: notches and keys move at once, as before.
* **Programmatic scrolling wins.** When something else moves the bar (Chat following a new
  message, a scrollbar drag) the glide stops and adopts the new position instead of fighting it.

**S3, measuring.** With `KAYRA_UI_PROFILE=1` each glide frame is painted synchronously inside
`profile.measure("scroll")`, so the log shows the real cost of a scroll frame and its rate.

**The honest limit (S2 is not here).** This makes the MOTION continuous; it does not make a
frame cheaper. Pages are translucent over the animated backdrop, so Qt repaints every visible
card for each step on the CPU (~27–33 ms measured on Settings / System at 2.25x). The GPU tile
layer (§11 S2) is the step that would bring frames under 8 ms.
"""

import math

from PySide6.QtCore import QObject, QEvent, Qt, QTimer
from PySide6.QtWidgets import QAbstractScrollArea, QApplication

from kayra.ui import profile

GLIDE_TAU_S = 0.057                 # exponential time constant: ~95 % of a glide in ~170 ms
MOMENTUM_TAU_S = 0.325              # a touchpad flick slows to 1/e of its speed in 0.33 s
MIN_FLICK_PX_S = 120.0              # slower than this when the fingers lift: no coasting
STOP_PX_S = 18.0                    # coasting below this stops
SETTLE_PX = 0.5                     # a glide this close to its target lands exactly on it

_KEYS = {Qt.Key_PageDown, Qt.Key_PageUp, Qt.Key_Home, Qt.Key_End, Qt.Key_Down, Qt.Key_Up}


def _reduced_motion():
    try:
        from kayra.ui.components.backdrop import prefers_reduced_motion
        return prefers_reduced_motion()
    except Exception:
        return False


class SmoothScroller(QObject):
    """The glide for ONE scroll area's vertical bar. `step(dt)` advances it (the timer calls it;
    the tests call it directly with a fixed dt, so they need no clock)."""

    def __init__(self, area, reduced=None):
        super().__init__(area)
        self.area = area
        self.bar = area.verticalScrollBar()
        self.reduced = _reduced_motion() if reduced is None else bool(reduced)
        self.position = float(self.bar.value())
        self.target = self.position
        self.velocity = 0.0          # px/s while coasting after a touchpad flick
        self.mode = None             # None | "glide" | "coast" | "touch"
        self._ours = False           # True while WE move the bar (so our own change is not "external")
        self._touch = []             # recent touchpad samples: (pixels, seconds since the previous one)
        self._last_stamp = 0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self._interval_s = self._refresh_interval()
        self.timer.setInterval(max(4, int(round(self._interval_s * 1000))))
        self.frames = 0
        area.viewport().installEventFilter(self)
        area.installEventFilter(self)
        self.bar.valueChanged.connect(self._on_value)

    # ── clock ──
    def _refresh_interval(self):
        screen = self.area.screen() if hasattr(self.area, "screen") else None
        try:
            rate = float(screen.refreshRate()) if screen is not None else 60.0
        except Exception:
            rate = 60.0
        rate = rate if 30.0 <= rate <= 360.0 else 60.0
        return 1.0 / rate

    def _start(self):
        if not self.timer.isActive():
            self.timer.start()

    def _tick(self):
        self.step(self._interval_s)

    # ── the motion ──
    def _limits(self):
        return float(self.bar.minimum()), float(self.bar.maximum())

    def _clamp(self, value):
        low, high = self._limits()
        return min(max(value, low), high)

    def _apply(self, value):
        value = int(round(value))
        if value == self.bar.value():
            return
        self._ours = True
        try:
            if profile.enabled():
                with profile.measure("scroll"):
                    self.bar.setValue(value)
                    self.area.viewport().repaint()
            else:
                self.bar.setValue(value)
        finally:
            self._ours = False

    def step(self, dt):
        """One frame. Returns True while still moving."""
        dt = max(float(dt), 1e-4)
        self.frames += 1
        if self.mode == "glide":
            self.target = self._clamp(self.target)
            gap = self.target - self.position
            if abs(gap) <= SETTLE_PX:
                self.position = self.target
                self._apply(self.position)
                return self._stop()
            self.position += gap * (1.0 - math.exp(-dt / GLIDE_TAU_S))
            self._apply(self.position)
            return True
        if self.mode == "coast":
            self.position += self.velocity * dt
            self.velocity *= math.exp(-dt / MOMENTUM_TAU_S)
            low, high = self._limits()
            if self.position <= low or self.position >= high or abs(self.velocity) < STOP_PX_S:
                self.position = self._clamp(self.position)
                self._apply(self.position)
                return self._stop()
            self._apply(self.position)
            return True
        return self._stop()

    def _stop(self):
        self.mode = None
        self.velocity = 0.0
        self.timer.stop()
        return False

    def glide_by(self, pixels):
        """A wheel notch or a key: move the TARGET; a running glide keeps going toward the new one."""
        if self.mode != "glide":
            self.position = float(self.bar.value())
            self.target = self.position
        self.target = self._clamp(self.target + pixels)
        if self.reduced:
            self.position = self.target
            self._apply(self.target)
            return
        self.mode = "glide"
        self._start()

    def glide_to(self, value):
        self.glide_by(self._clamp(value) - (self.target if self.mode == "glide" else self.bar.value()))

    def follow(self, pixels, seconds):
        """Touchpad: the content follows the fingers exactly; the speed is remembered for the lift."""
        self.mode = "touch"
        self.timer.stop()
        self.position = self._clamp(float(self.bar.value()) + pixels)
        self.target = self.position
        self._apply(self.position)
        self._touch = (self._touch + [(pixels, max(seconds, 1e-3))])[-5:]

    def release(self):
        """The fingers lifted: coast with the recent speed, or stop."""
        recent = self._touch[-4:][1:]           # the first sample has no real time before it
        self._touch = []
        moved = sum(p for p, _s in recent)
        took = sum(s for _p, s in recent)
        speed = moved / took if took and len(recent) >= 2 else 0.0   # one event is a nudge, not a flick
        if self.reduced or abs(speed) < MIN_FLICK_PX_S:
            return self._stop()
        self.mode, self.velocity = "coast", speed
        self.position = float(self.bar.value())
        self._start()
        return True

    # ── input ──
    def _on_value(self, value):
        if self._ours:
            return
        # Someone else moved the bar (Chat following a reply, a drag): stop and adopt it.
        if self.mode in ("glide", "coast"):
            self._stop()
        self.position = self.target = float(value)

    def _notch_pixels(self, units):
        lines = QApplication.wheelScrollLines() or 3
        return -units / 120.0 * lines * max(self.bar.singleStep(), 20)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Wheel and obj is self.area.viewport():
            if event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier):
                return False                       # zoom / horizontal: Qt's own handling
            pixel = event.pixelDelta().y()
            phase = event.phase()
            if pixel and phase != Qt.NoScrollPhase:
                stamp = event.timestamp() or 0
                gap = (stamp - self._last_stamp) / 1000.0 if self._last_stamp and stamp > self._last_stamp else self._interval_s
                self._last_stamp = stamp
                if phase == Qt.ScrollEnd:
                    self.release()
                else:                                          # ScrollUpdate, or the platform's own momentum
                    self.follow(-pixel, min(gap, 0.1))
                return True
            units = event.angleDelta().y()
            if units:
                self.glide_by(self._notch_pixels(units))
                return True
            if phase == Qt.ScrollEnd:
                self.release()
                return True
            return False
        if kind == QEvent.KeyPress and event.key() in _KEYS and not event.modifiers() & ~Qt.KeypadModifier:
            # A key reaches the scroll area only when the focused child did NOT use it (Home in a
            # text field never gets here; Down in a one-line field does, and scrolls as it always
            # did — now gliding).
            key = event.key()
            if key == Qt.Key_Home:
                self.glide_to(self.bar.minimum())
            elif key == Qt.Key_End:
                self.glide_to(self.bar.maximum())
            else:
                size = self.bar.pageStep() if key in (Qt.Key_PageDown, Qt.Key_PageUp) else max(self.bar.singleStep(), 20) * 2
                self.glide_by(size if key in (Qt.Key_PageDown, Qt.Key_Down) else -size)
            return True
        return False


def install_smooth_scroll(area, reduced=None):
    """Gives `area` (a QScrollArea or any QAbstractScrollArea) the glide. Idempotent."""
    if not isinstance(area, QAbstractScrollArea):
        raise TypeError("install_smooth_scroll needs a scroll area")
    existing = getattr(area, "_kayra_smooth", None)
    if existing is not None:
        return existing
    scroller = SmoothScroller(area, reduced=reduced)
    area._kayra_smooth = scroller
    return scroller
