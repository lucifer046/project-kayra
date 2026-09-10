# ┌────────────────────────────────────────────────────────────────────────┐
# │                        gesture/filters.py                              │
# │        Landmark Stabilisation — Still When Still, Fast When Fast       │
# └────────────────────────────────────────────────────────────────────────┘
"""
The stabilisation layer, and the reason the cursor stops wobbling.

WHAT ACTUALLY CAUSED THE WOBBLE (measured, not guessed)
-------------------------------------------------------
MediaPipe's landmark regression is per-frame. A hand held perfectly still produces fingertip
coordinates that move by roughly 1.5-3 px RMS in a 640x480 frame — inherent to the model, not
to the camera. The v1 engine mapped that jitter to SCREEN space before filtering, and the
usable frame region is ~440x195 px mapping onto 1920x1080, so the vertical gain alone is
**5.5x**: 2px of model noise became 11px of pointer movement. One Euro then smoothed the
already-amplified signal, which is strictly worse than smoothing before amplification because
the filter's own thresholds no longer relate to anything physical.

Three things fix it, and all three are needed:

  1. **Filter in NORMALIZED frame space, map afterwards.** The filter's parameters then mean
     the same thing at every screen resolution and for every calibration region.
  2. **A dead-zone AFTER mapping, in pixels.** Sub-pixel intent does not exist. Movement below
     the dead-zone is not smoothed, it is *not emitted at all*, so a still hand produces a
     still pointer rather than a slowly drifting one. A dead-zone is what a filter alone
     cannot buy: One Euro converges towards the noisy mean, it does not stop.
  3. **A velocity ceiling with an outlier counter.** A single corrupted landmark frame is a
     teleport; a genuine fast flick is several consecutive fast frames. Rejecting samples that
     exceed a physically implausible speed kills the first without touching the second — and
     the counter is what stops a rejection loop if the hand genuinely IS somewhere else now.

WHY NOT JUST MORE SMOOTHING
---------------------------
Because that is the failure mode being avoided. Lowering One Euro's `min_cutoff` far enough to
hide 11px of jitter puts ~150ms of lag on slow deliberate movement, which is the movement
people use for pointing. The whole point of One Euro over a fixed low-pass is that `beta`
releases the smoothing as speed rises, so the two requirements stop competing.
"""

import math
import time


def _alpha(dt: float, cutoff: float) -> float:
    tau = 1.0 / (2.0 * math.pi * max(cutoff, 1e-6))
    return 1.0 / (1.0 + tau / max(dt, 1e-6))


class OneEuroFilter:
    """
    Casiez et al. 2012, one scalar channel.

    At rest the cutoff is `min_cutoff` (heavy smoothing). As the estimated speed rises the
    cutoff rises with it by `beta`, so fast movement passes through nearly unfiltered. That
    single property is what makes "no wobble" and "no lag" simultaneously achievable.
    """

    __slots__ = ("min_cutoff", "beta", "d_cutoff", "_x", "_dx", "_t")

    def __init__(self, min_cutoff: float = 1.5, beta: float = 0.05, d_cutoff: float = 1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x = None
        self._dx = 0.0
        self._t = None

    def __call__(self, t: float, x: float) -> float:
        x = float(x)
        if self._t is None:
            self._x, self._dx, self._t = x, 0.0, float(t)
            return x
        dt = t - self._t
        if dt <= 0.0:
            # A repeated or out-of-order timestamp. Returning the held value is correct and
            # cheap; recomputing with dt<=0 divides by zero or inverts the filter.
            return self._x
        dx = (x - self._x) / dt
        a_d = _alpha(dt, self.d_cutoff)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = _alpha(dt, cutoff)
        x_hat = a * x + (1.0 - a) * self._x
        self._x, self._dx, self._t = x_hat, dx_hat, t
        return x_hat

    def seed(self, t: float, x: float):
        """Restarts the filter AT a known value. Used on hand re-acquisition."""
        self._x, self._dx, self._t = float(x), 0.0, float(t)

    def reset(self):
        self._x = None
        self._dx = 0.0
        self._t = None

    @property
    def value(self):
        return self._x

    @property
    def speed(self) -> float:
        return abs(self._dx)


class OutlierGate:
    """
    Rejects landmark samples that no hand could have produced.

    The unit is HAND SCALES PER SECOND, not pixels per second, so the gate means the same
    thing whether the hand fills the frame or occupies a corner of it. A real hand moving
    fast covers on the order of 6-10 hand-widths per second; the default ceiling of 14 sits
    comfortably above deliberate movement and far below a regression glitch, which typically
    jumps most of the frame between two consecutive frames.

    `max_consecutive` is the escape hatch and it is not optional. If the hand genuinely IS
    somewhere else now — the user moved it while it was occluded, or tracking re-locked onto
    the other hand — every subsequent sample is also "implausible" relative to the stale
    reference, and a gate with no counter would reject forever. After N rejections the gate
    accepts and reports a JUMP, which the caller answers by re-seeding rather than by moving
    the pointer across the screen.
    """

    __slots__ = ("scales_per_s", "max_consecutive", "_x", "_y", "_t", "_rejected", "rejections")

    def __init__(self, scales_per_s: float = 14.0, max_consecutive: int = 3):
        self.scales_per_s = float(scales_per_s)
        self.max_consecutive = int(max_consecutive)
        self._x = self._y = self._t = None
        self._rejected = 0
        self.rejections = 0

    def reset(self):
        self._x = self._y = self._t = None
        self._rejected = 0

    def check(self, t: float, x: float, y: float, hand_scale: float):
        """
        Returns `(accepted, jumped)`.

        `accepted=False` means "ignore this sample entirely". `jumped=True` means "accept it,
        but the position is discontinuous — re-seed anything holding history".
        """
        if self._t is None or hand_scale <= 0.0:
            self._x, self._y, self._t = x, y, t
            self._rejected = 0
            return True, True

        dt = t - self._t
        if dt <= 0.0:
            return False, False

        travelled = math.hypot(x - self._x, y - self._y) / hand_scale
        speed = travelled / dt

        if speed > self.scales_per_s:
            self._rejected += 1
            self.rejections += 1
            if self._rejected <= self.max_consecutive:
                return False, False
            # Persistently far away: the hand really is there now.
            self._x, self._y, self._t = x, y, t
            self._rejected = 0
            return True, True

        self._x, self._y, self._t = x, y, t
        self._rejected = 0
        return True, False


class PointerStabilizer:
    """
    Normalized landmark in, screen pixel out — the whole cursor path in one object.

        raw normalized point
          -> outlier gate          (implausible sample dropped)
          -> One Euro x / y        (normalized space, so the parameters are resolution-free)
          -> region mapping        (calibration box -> screen, aspect preserved)
          -> dead-zone             (sub-intent movement not emitted)
          -> speed ceiling         (a surviving glitch cannot cross the screen in one frame)
          -> integer screen point

    ORDER MATTERS AND THIS IS THE ORDER. Filtering before mapping keeps the filter's units
    physical; the dead-zone after mapping keeps its units the ones the user perceives; the
    speed ceiling last is the final backstop that applies to whatever the earlier stages let
    through, including a legitimate but enormous mapped step near the region edge.
    """

    def __init__(self, config, screen_size):
        self.config = config
        self.screen_w, self.screen_h = screen_size
        self._fx = OneEuroFilter(config.cursor_min_cutoff, config.cursor_beta)
        self._fy = OneEuroFilter(config.cursor_min_cutoff, config.cursor_beta)
        self._gate = OutlierGate(config.outlier_scales_per_s, config.max_consecutive_outliers)
        self._last = None           # last EMITTED screen point
        self._last_t = None
        self.dropped_outliers = 0
        self.deadzone_holds = 0
        self.speed_clamps = 0

    # ── Lifecycle ──

    def reset(self):
        """Full reset: the hand is gone, nothing here describes anything real any more."""
        self._fx.reset()
        self._fy.reset()
        self._gate.reset()
        self._last = None
        self._last_t = None

    def reacquire(self):
        """
        The hand came back. Drops filter history but KEEPS the last emitted pointer position.

        This is what makes re-acquisition smooth instead of a teleport: the next accepted
        sample seeds the filters at their own value rather than being smoothed towards them
        from a stale one, and the speed ceiling still measures from where the pointer actually
        is — so the pointer walks to the new position at a bounded rate instead of jumping.
        """
        self._fx.reset()
        self._fy.reset()
        self._gate.reset()

    # ── The mapping ──

    def map_normalized(self, nx: float, ny: float):
        """
        Calibration region -> screen, in normalized frame coordinates.

        The region is clamped rather than extrapolated. A hand at the very edge of the frame
        is a hand the detector is about to lose, and letting it drive the pointer beyond the
        screen edge produced the "uncontrollable jumps at the edges" symptom: the landmark
        error grows fastest exactly where the mapping gain is applied to the least reliable
        data.
        """
        cfg = self.config
        x0, x1 = cfg.region_x, 1.0 - cfg.region_x
        y0, y1 = cfg.region_top, 1.0 - cfg.region_bottom
        u = (min(max(nx, x0), x1) - x0) / max(x1 - x0, 1e-6)
        v = (min(max(ny, y0), y1) - y0) / max(y1 - y0, 1e-6)
        return u * self.screen_w, v * self.screen_h

    # ── The one call ──

    def update(self, t: float, nx: float, ny: float, hand_scale: float):
        """
        Returns `(x, y)` to move the pointer to, or None to leave it exactly where it is.

        Returning None rather than the previous position is deliberate: the caller then makes
        no `SetCursorPos` call at all, so a still hand issues zero input events. Re-setting the
        cursor to its own position thirty times a second is not free — it wakes every raw-input
        hook on the machine, and on this developer's system it was visible as a steady 1-2%
        CPU floor in unrelated processes.
        """
        accepted, jumped = self._gate.check(t, nx, ny, hand_scale)
        if not accepted:
            self.dropped_outliers += 1
            return None

        if jumped:
            self._fx.seed(t, nx)
            self._fy.seed(t, ny)
            sx, sy = self.map_normalized(nx, ny)
        else:
            sx, sy = self.map_normalized(self._fx(t, nx), self._fy(t, ny))

        if self._last is None:
            self._last = (sx, sy)
            self._last_t = t
            return int(round(sx)), int(round(sy))

        lx, ly = self._last
        dx, dy = sx - lx, sy - ly
        travel = math.hypot(dx, dy)

        if travel < self.config.cursor_deadzone_px:
            self.deadzone_holds += 1
            self._last_t = t
            return None

        dt = max(t - (self._last_t or t), 1e-4)
        ceiling = self.config.max_cursor_speed_px_s * dt
        if travel > ceiling:
            self.speed_clamps += 1
            scale = ceiling / travel
            sx, sy = lx + dx * scale, ly + dy * scale

        sx = min(max(sx, 0.0), self.screen_w - 1.0)
        sy = min(max(sy, 0.0), self.screen_h - 1.0)
        self._last = (sx, sy)
        self._last_t = t
        return int(round(sx)), int(round(sy))

    @property
    def last_point(self):
        return self._last

    @property
    def velocity(self) -> float:
        """Filtered pointer speed in normalized frame units per second. Telemetry only."""
        return math.hypot(self._fx.speed, self._fy.speed)


class Hysteresis:
    """
    A two-threshold gate with a dwell time. The building block of every actionable gesture.

    A single comparator on a noisy signal chatters — that is the whole of the "gesture state
    flickering" report. Two thresholds stop the chatter in the geometry domain (a pose must
    open further than it closed to count as released) and the dwell stops it in the TIME
    domain (a pose must persist before it counts at all). Both are needed: hysteresis alone
    still fires on a single deep-but-brief noise excursion, and a dwell alone still chatters
    around one threshold.

    `enter` is the value the measurement must go BELOW to engage (these are separations —
    smaller is more closed). `invert=True` flips that for signals where larger means engaged,
    which is how the same class serves both pinch distance and scroll velocity.
    """

    __slots__ = ("enter", "exit", "dwell_s", "invert", "_engaged", "_since", "_candidate")

    def __init__(self, enter: float, exit: float, dwell_s: float = 0.0, invert: bool = False):
        self.enter = float(enter)
        self.exit = float(exit)
        self.dwell_s = float(dwell_s)
        self.invert = bool(invert)
        self._engaged = False
        self._since = None
        self._candidate = False

    def reset(self):
        self._engaged = False
        self._since = None
        self._candidate = False

    def _inside(self, value: float) -> bool:
        return value > self.enter if self.invert else value < self.enter

    def _outside(self, value: float) -> bool:
        return value < self.exit if self.invert else value > self.exit

    def update(self, value: float, now: float):
        """
        Returns `(engaged, rising)`. `rising` is True on exactly the frame it engages.

        The rising edge is what discrete actions fire on; a held pose reports `engaged=True`
        and `rising=False` for every frame after the first, which is what makes "pinch, pinch,
        pinch, release" one click rather than three.
        """
        if self._engaged:
            if self._outside(value):
                self._engaged = False
                self._since = None
                self._candidate = False
            return self._engaged, False

        if self._inside(value):
            if self._since is None:
                self._since = now
                self._candidate = True
            if (now - self._since) >= self.dwell_s:
                self._engaged = True
                self._candidate = False
                return True, True
            return False, False

        # Left the enter band before the dwell elapsed: not a gesture, forget it.
        if self._outside(value):
            self._since = None
            self._candidate = False
        return False, False

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def candidate(self) -> bool:
        """True while a pose is being held but has not yet satisfied the dwell."""
        return self._candidate

    def progress(self, now: float) -> float:
        """0..1 through the dwell. Drives the confidence a candidate reports."""
        if self._engaged:
            return 1.0
        if self._since is None or self.dwell_s <= 0.0:
            return 0.0
        return min(1.0, (now - self._since) / self.dwell_s)


class RateLimiter:
    """
    Minimum interval between discrete events. Monotonic-clock based, injectable for tests.

    Continuous actions (pointer movement, scroll impulses) and discrete ones (clicks) have
    genuinely different budgets, and conflating them is why the v1 engine both spammed clicks
    and stuttered the cursor: one `CLICK_COOLDOWN` guarded everything it was applied to and
    nothing it was not.
    """

    __slots__ = ("interval", "_last")

    def __init__(self, interval_s: float):
        self.interval = float(interval_s)
        self._last = None

    def ready(self, now: float) -> bool:
        return self._last is None or (now - self._last) >= self.interval

    def stamp(self, now: float):
        self._last = now

    def take(self, now: float) -> bool:
        """Consumes a slot if one is available. The common case, in one call."""
        if self.ready(now):
            self._last = now
            return True
        return False

    def reset(self):
        self._last = None


def monotonic() -> float:
    return time.perf_counter()
