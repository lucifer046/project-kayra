# ┌────────────────────────────────────────────────────────────────────────┐
# │                        gesture/config.py                               │
# │      Every Gesture Threshold, In One Place, Read Once From .env        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single source of truth for every number the gesture stack uses.

WHY A MODULE RATHER THAN CONSTANTS ON THE CLASSES
-------------------------------------------------
The v1 engine scattered its thresholds across three classes as class attributes, and half of
them were PIXEL distances (`pinch < 30 px`, `release > 40 px`, `SCROLL_DEADBAND = 20`). A pixel
threshold is a threshold that changes meaning when the user moves. At arm's length a hand
spans ~90px of a 640px frame and a 30px pinch gate is a third of the whole hand — every close
finger pose reads as a pinch. Leaning in, the same hand spans ~260px and a real pinch never
gets under 30px, so clicking stops working entirely. Both symptoms were reported, and they are
the same bug seen from two distances.

**Every geometric threshold here is therefore a RATIO of hand scale, not a pixel count.** The
only pixel values that survive are the ones that genuinely belong to the screen rather than to
the hand: the cursor dead-zone and the cursor speed ceiling, which are about the pointer.

THE THREE USER-FACING DIALS
---------------------------
The settings screen exposes three words — Low / Medium / High — for three ideas, and this
module turns each into the handful of numbers it actually means. That indirection is the
point: a user asked to choose a `min_cutoff` is a user who will choose wrong, and a settings
screen with fourteen spin boxes is a settings screen nobody touches.

  * `GESTURE_SENSITIVITY`     — how eagerly a pose is recognised (confirmation time, stability
                                floor, confidence floor).
  * `GESTURE_CURSOR_SMOOTHING`— stable-when-still versus responsive-when-moving.
  * `GESTURE_CLICK_SENSITIVITY` — how closed a pinch must be, and how long it must hold.

Everything else is an environment variable with a safe default and a hard clamp, because a
malformed `.env` must not be able to produce a cursor that cannot be stopped or a click that
fires every frame.
"""

import os
from dataclasses import dataclass, field

from kayra.core.config import env, env_int, env_float, env_bool


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            THE THREE DIALS                             │
# └────────────────────────────────────────────────────────────────────────┘

LEVELS = ("LOW", "MEDIUM", "HIGH")


def _level(name, default="MEDIUM"):
    raw = (env(name, default) or default).strip().upper()
    return raw if raw in LEVELS else default


# Gesture sensitivity. HIGH reacts sooner and demands less evidence; LOW makes the user mean
# it. The confirmation TIME is the important half — a pose held for 120ms is a deliberate
# pose, and a pose seen for one frame is noise, whatever its geometry says.
_SENSITIVITY = {
    #            confirm_ms  min_stability  min_confidence
    "LOW":      (170.0,      0.62,          0.72),
    "MEDIUM":   (120.0,      0.50,          0.62),
    "HIGH":     (85.0,       0.38,          0.52),
}

# Cursor smoothing, as One Euro filter parameters.
#
# `min_cutoff` sets how hard a STATIONARY hand is smoothed (lower = smoother = more lag on
# slow moves); `beta` sets how quickly that smoothing is released as the hand speeds up
# (higher = more responsive to fast movement). The pairing is what makes "stable when still,
# responsive when moving" achievable at all — a single EMA can only buy one of the two.
#
# These are NOT "more smoothing" as the level rises. HIGH smoothing means a lower min_cutoff
# AND a higher beta: heavier at rest, and still released fully under motion. Cranking
# min_cutoff down alone is exactly the sluggish-cursor failure this must avoid.
#
# BETA IS IN THE TENS, NOT THE HUNDREDTHS, AND THAT IS THE WHOLE FIX. One Euro's velocity term
# is `beta * |speed|`, and the speed here is in NORMALIZED FRAME UNITS PER SECOND — a fast hand
# flick is about 0.6 units/s, and a deliberate slow move about 0.12. The v1 engine used
# `beta=0.05`, which was tuned for a filter running on SCREEN PIXELS where the same movement
# reads as ~1700 units/s. In normalized space that beta contributes 0.006 Hz to a 1.5 Hz
# cutoff: the adaptive half of the adaptive filter was, in effect, switched off, and every
# movement got the rest-smoothing. That is why the v1 cursor felt laggy despite carrying a
# filter specifically designed not to be.
#
# Measured on the jitter/ramp benchmark in `tests/test_gesture_control.py`, at MEDIUM:
#     beta 0.055  ->  slow-move lag 99ms, fast-flick lag 80ms
#     beta 12.0   ->  slow-move lag 39ms, fast-flick lag 16ms
# with the SAME rest-jitter reduction (8.3px raw -> 3.5px filtered).
_SMOOTHING = {
    #            min_cutoff  beta   deadzone_px
    "LOW":      (1.6,        16.0,  1.0),
    "MEDIUM":   (1.0,        12.0,  2.0),
    "HIGH":     (0.6,        8.0,   3.0),
}

# Click sensitivity, as normalized pinch ratios (fingertip separation / hand scale).
#
# EXIT MUST EXCEED ENTER. That inequality is the hysteresis, and it is asserted at
# construction rather than trusted: an `.env` that inverted them would produce a state machine
# that enters and leaves a pinch on the same frame, which is the flicker this exists to stop.
_CLICK = {
    #            enter  exit   hold_ms
    "LOW":      (0.28,  0.40,  110.0),
    "MEDIUM":   (0.34,  0.46,  80.0),
    "HIGH":     (0.42,  0.55,  55.0),
}


def _clamp(value, low, high):
    return max(low, min(high, value))


@dataclass
class GestureConfig:
    """
    Immutable-by-convention configuration for one gesture runtime.

    Constructed once by the controller and passed down. Nothing below reads `os.environ` or
    `.env` for itself — a threshold read in two places is a threshold that can differ in two
    places, and the v1 engine had exactly that between its state machine and its render code.
    """

    # ── Camera ──
    camera_index: int = 0
    camera_width: int = 640
    camera_height: int = 480
    target_fps: int = 30
    preview_fps: int = 15
    mirror: bool = True
    # AUTO | MSMF | DSHOW. Which OpenCV capture backend to ask for.
    #
    # AUTO (`CAP_ANY`) is the default because it was MEASURED to be the best here, and because
    # capture-backend behaviour is machine-specific enough that hardcoding one is a liability.
    # See `CameraSource._open` for the numbers: DSHOW, which this module originally hardcoded
    # on the strength of an unmeasured claim, is three times slower to open AND leaks eight
    # times as many threads per open/close cycle.
    camera_backend: str = "AUTO"

    # ── Detector ──
    model_complexity: int = 0
    detection_confidence: float = 0.6
    tracking_confidence: float = 0.5
    gpu: str = "AUTO"                       # AUTO | ON | OFF

    # ── Landmark stabilisation ──
    cursor_min_cutoff: float = 1.0
    cursor_beta: float = 12.0
    cursor_deadzone_px: float = 2.0
    max_cursor_speed_px_s: float = 6000.0
    # An outlier is a landmark jump no hand can make. Expressed in HAND SCALES per second so
    # it means the same thing at every distance from the camera.
    outlier_scales_per_s: float = 14.0
    max_consecutive_outliers: int = 3

    # ── Cursor mapping ──
    # The usable region of the frame, as fractions. Asymmetric at the bottom on purpose: the
    # forearm enters from below, so a symmetric box clips the wrist and the hand scale
    # collapses just as the user reaches the bottom of the screen.
    region_x: float = 0.16
    region_top: float = 0.14
    region_bottom: float = 0.34
    edge_dwell_px: float = 2.0

    # ── The pause gesture ──
    # A CLOSED FIST PAUSES GESTURE CONTROL, AND IT IS THE ONLY GESTURE THAT CAN.
    #
    # These thresholds exist because the first version had none. It tested
    # `extended_count == 0` — four independent `extension >= 0.55` comparators — once per
    # frame, with no dwell and no hysteresis, and treated the result as an immediate pause. A
    # real pointing finger is not perfectly straight; its measured extension sits near 0.55,
    # and landmark noise carries it across that comparator several times a second. Measured on
    # the reproduction in `tests/test_gesture_state.py`: **206 ACTIVE->PAUSED->ACTIVE round
    # trips in 600 frames**, roughly 8.6 per second, on an ordinary cursor session.
    #
    # The signal is now `1 - max(extension)`: a fist requires EVERY finger curled, so the most
    # extended finger governs, and it is continuous so a gate can act on it. `enter` is far
    # from any resting pose (0.82 means the straightest finger is below 0.18 extended) and the
    # dwell makes the pose deliberate.
    pause_enabled: bool = True
    pause_enter: float = 0.82
    pause_exit: float = 0.55
    pause_hold_ms: float = 500.0
    # Leaving the pause needs its own sustained evidence, or one noisy frame of a half-open
    # hand resumes control the user deliberately stopped.
    resume_hold_ms: float = 300.0

    # ── Gesture gating ──
    confirm_ms: float = 120.0
    min_stability: float = 0.50
    min_confidence: float = 0.62
    hand_lost_grace_ms: float = 220.0
    reacquire_ms: float = 180.0

    # ── Clicks ──
    pinch_enter: float = 0.34
    pinch_exit: float = 0.46
    pinch_hold_ms: float = 80.0
    click_cooldown_ms: float = 320.0
    double_enter: float = 0.46
    double_exit: float = 0.60
    # How long a pinch must be HELD before the pointer starts following the hand again.
    #
    # THIS IS WHY CLICKS LAND WHERE THE USER AIMED. Pinching physically curls the index
    # finger towards the thumb, which moves the index tip — the very landmark driving the
    # cursor — by roughly half a hand-width. Without this the pointer slides several hundred
    # pixels during the act of clicking, and the click lands on whatever it slid onto. The
    # pointer is therefore FROZEN from the moment a pinch becomes a candidate until it has
    # been held past this threshold, at which point the user is evidently dragging and the
    # pointer follows again.
    drag_unlock_ms: float = 260.0

    # ── Scroll ──
    # Normalized vertical velocity of the two-finger pair, in hand-scales per second.
    scroll_enter: float = 0.55
    scroll_exit: float = 0.22
    scroll_pose_ms: float = 130.0
    scroll_gain: float = 220.0
    max_scroll_impulse: int = 360
    scroll_interval_ms: float = 45.0

    # ── Diagnostics ──
    diagnostics: bool = False

    # ── Levels, kept for reporting ──
    sensitivity: str = "MEDIUM"
    smoothing: str = "MEDIUM"
    click_sensitivity: str = "MEDIUM"

    # Non-configurable derived values.
    telemetry_history: int = field(default=60, repr=False)

    # ──────────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls):
        """
        Builds a configuration from `.env` / the process environment.

        Every value is clamped. The clamps are not defensive decoration: `GESTURE_TARGET_FPS=0`
        is a division by zero in the capture loop and `GESTURE_CLICK_COOLDOWN_MS=0` is a mouse
        that clicks thirty times a second, and both are one typo away.
        """
        sensitivity = _level("GESTURE_SENSITIVITY")
        smoothing = _level("GESTURE_CURSOR_SMOOTHING")
        clicking = _level("GESTURE_CLICK_SENSITIVITY")

        confirm_ms, min_stability, min_confidence = _SENSITIVITY[sensitivity]
        min_cutoff, beta, deadzone = _SMOOTHING[smoothing]
        pinch_enter, pinch_exit, pinch_hold = _CLICK[clicking]

        cfg = cls(
            camera_index=env_int("GESTURE_CAMERA_INDEX", 0, 0, 16),
            camera_width=env_int("GESTURE_CAMERA_WIDTH", 640, 320, 1920),
            camera_height=env_int("GESTURE_CAMERA_HEIGHT", 480, 240, 1080),
            target_fps=env_int("GESTURE_TARGET_FPS", 30, 5, 60),
            preview_fps=env_int("GESTURE_PREVIEW_FPS", 15, 1, 30),
            mirror=env_bool("GESTURE_CAMERA_MIRROR", True),
            camera_backend=(env("GESTURE_CAMERA_BACKEND", "AUTO") or "AUTO").strip().upper(),

            model_complexity=env_int("GESTURE_MODEL_COMPLEXITY", 0, 0, 1),
            detection_confidence=env_float("GESTURE_DETECTION_CONFIDENCE", 0.6, 0.1, 0.95),
            tracking_confidence=env_float("GESTURE_TRACKING_CONFIDENCE", 0.5, 0.1, 0.95),
            gpu=(env("GESTURE_GPU", "AUTO") or "AUTO").strip().upper(),

            cursor_min_cutoff=env_float("GESTURE_CURSOR_MIN_CUTOFF", min_cutoff, 0.1, 12.0),
            cursor_beta=env_float("GESTURE_CURSOR_BETA", beta, 0.0, 80.0),
            cursor_deadzone_px=env_float("GESTURE_CURSOR_DEADZONE", deadzone, 0.0, 12.0),
            max_cursor_speed_px_s=env_float("GESTURE_CURSOR_MAX_SPEED", 6000.0, 500.0, 40000.0),
            outlier_scales_per_s=env_float("GESTURE_OUTLIER_SCALES", 14.0, 3.0, 60.0),

            region_x=env_float("GESTURE_REGION_X", 0.16, 0.0, 0.40),
            region_top=env_float("GESTURE_REGION_TOP", 0.14, 0.0, 0.40),
            region_bottom=env_float("GESTURE_REGION_BOTTOM", 0.34, 0.0, 0.45),

            pause_enabled=env_bool("GESTURE_PAUSE_ENABLED", True),
            pause_enter=env_float("GESTURE_PAUSE_THRESHOLD", 0.82, 0.30, 1.0),
            pause_exit=env_float("GESTURE_PAUSE_RELEASE_THRESHOLD", 0.55, 0.10, 0.99),
            pause_hold_ms=env_float("GESTURE_PAUSE_HOLD_MS", 500.0, 80.0, 3000.0),
            resume_hold_ms=env_float("GESTURE_RESUME_HOLD_MS", 300.0, 40.0, 3000.0),

            confirm_ms=env_float("GESTURE_CONFIRM_MS", confirm_ms, 20.0, 600.0),
            min_stability=env_float("GESTURE_MIN_STABILITY", min_stability, 0.0, 0.95),
            min_confidence=env_float("GESTURE_MIN_CONFIDENCE", min_confidence, 0.0, 0.95),
            hand_lost_grace_ms=env_float("GESTURE_HAND_LOST_GRACE_MS", 220.0, 0.0, 1500.0),
            reacquire_ms=env_float("GESTURE_REACQUIRE_MS", 180.0, 0.0, 1500.0),

            pinch_enter=env_float("GESTURE_CLICK_THRESHOLD", pinch_enter, 0.10, 0.80),
            pinch_exit=env_float("GESTURE_CLICK_RELEASE_THRESHOLD", pinch_exit, 0.12, 1.20),
            pinch_hold_ms=env_float("GESTURE_CLICK_HOLD_MS", pinch_hold, 10.0, 500.0),
            click_cooldown_ms=env_float("GESTURE_CLICK_COOLDOWN_MS", 320.0, 60.0, 3000.0),
            drag_unlock_ms=env_float("GESTURE_DRAG_UNLOCK_MS", 260.0, 0.0, 2000.0),

            scroll_enter=env_float("GESTURE_SCROLL_THRESHOLD", 0.55, 0.05, 4.0),
            scroll_exit=env_float("GESTURE_SCROLL_HYSTERESIS", 0.22, 0.01, 3.0),
            scroll_pose_ms=env_float("GESTURE_SCROLL_POSE_MS", 130.0, 20.0, 800.0),
            scroll_gain=env_float("GESTURE_SCROLL_GAIN", 220.0, 20.0, 2000.0),
            max_scroll_impulse=env_int("GESTURE_SCROLL_MAX_IMPULSE", 360, 40, 2400),
            scroll_interval_ms=env_float("GESTURE_SCROLL_INTERVAL_MS", 45.0, 10.0, 400.0),

            diagnostics=env_bool("GESTURE_DIAGNOSTICS", False),

            sensitivity=sensitivity,
            smoothing=smoothing,
            click_sensitivity=clicking,
        )
        cfg.double_enter = _clamp(cfg.pinch_enter + 0.12, 0.15, 0.95)
        cfg.double_exit = _clamp(cfg.pinch_exit + 0.14, 0.20, 1.40)
        cfg.normalize()
        return cfg

    # ──────────────────────────────────────────────────────────────────

    def normalize(self):
        """
        Enforces the invariants the state machines rely on. Called after every construction.

        THE HYSTERESIS INEQUALITIES ARE NOT ADVISORY. `exit <= enter` collapses a two-state
        gate into a comparator, and a comparator on a noisy signal is the flicker every one of
        these gates exists to prevent. Rather than refusing to start on a bad `.env` — which
        would take the whole feature down over one typo — the exit threshold is pushed above
        the enter threshold by a minimum margin and the runtime reports the correction.
        """
        corrections = []

        if self.pinch_exit <= self.pinch_enter:
            self.pinch_exit = round(self.pinch_enter * 1.25, 4)
            corrections.append("pinch release raised above the pinch threshold")
        if self.double_exit <= self.double_enter:
            self.double_exit = round(self.double_enter * 1.20, 4)
            corrections.append("double-click release raised above its threshold")
        if self.pause_exit >= self.pause_enter:
            self.pause_exit = round(self.pause_enter * 0.65, 4)
            corrections.append("pause release lowered below the pause threshold")
        if self.scroll_exit >= self.scroll_enter:
            self.scroll_exit = round(self.scroll_enter * 0.45, 4)
            corrections.append("scroll neutral lowered below the scroll threshold")
        # The three-finger gate must be strictly wider than the two-finger one, or a plain
        # index pinch can satisfy it and every left click becomes a double click.
        if self.double_enter <= self.pinch_enter:
            self.double_enter = round(self.pinch_enter + 0.10, 4)
            corrections.append("double-click threshold widened past the single-click one")

        self.region_x = _clamp(self.region_x, 0.0, 0.40)
        self.region_top = _clamp(self.region_top, 0.0, 0.40)
        self.region_bottom = _clamp(self.region_bottom, 0.0, 0.45)
        if self.region_top + self.region_bottom >= 0.85:
            self.region_top, self.region_bottom = 0.14, 0.34
            corrections.append("cursor region reset — the margins left no usable area")

        self.corrections = tuple(corrections)
        return self

    # ── Derived, read-only ──

    @property
    def frame_interval(self) -> float:
        return 1.0 / max(1, self.target_fps)

    @property
    def preview_interval(self) -> float:
        return 1.0 / max(1, self.preview_fps)

    def summary(self) -> str:
        return (f"{self.camera_width}x{self.camera_height}@{self.target_fps} "
                f"sensitivity={self.sensitivity} smoothing={self.smoothing} "
                f"click={self.click_sensitivity}")


# The environment keys this module reads, in the order they appear in `.env.example`. Exported
# so the test suite can assert the documentation and the code agree — a documented setting
# nothing reads, and a read setting nothing documents, are both silent bugs.
ENV_KEYS = (
    "GESTURE_ENABLED",
    "GESTURE_CAMERA_INDEX",
    "GESTURE_CAMERA_WIDTH",
    "GESTURE_CAMERA_HEIGHT",
    "GESTURE_CAMERA_MIRROR",
    "GESTURE_CAMERA_BACKEND",
    "GESTURE_TARGET_FPS",
    "GESTURE_PREVIEW_FPS",
    "GESTURE_MODEL_COMPLEXITY",
    "GESTURE_DETECTION_CONFIDENCE",
    "GESTURE_TRACKING_CONFIDENCE",
    "GESTURE_GPU",
    "GESTURE_SENSITIVITY",
    "GESTURE_CURSOR_SMOOTHING",
    "GESTURE_CLICK_SENSITIVITY",
    "GESTURE_CURSOR_MIN_CUTOFF",
    "GESTURE_CURSOR_BETA",
    "GESTURE_CURSOR_DEADZONE",
    "GESTURE_CURSOR_MAX_SPEED",
    "GESTURE_OUTLIER_SCALES",
    "GESTURE_REGION_X",
    "GESTURE_REGION_TOP",
    "GESTURE_REGION_BOTTOM",
    "GESTURE_PAUSE_ENABLED",
    "GESTURE_PAUSE_THRESHOLD",
    "GESTURE_PAUSE_RELEASE_THRESHOLD",
    "GESTURE_PAUSE_HOLD_MS",
    "GESTURE_RESUME_HOLD_MS",
    "GESTURE_CONFIRM_MS",
    "GESTURE_MIN_STABILITY",
    "GESTURE_MIN_CONFIDENCE",
    "GESTURE_HAND_LOST_GRACE_MS",
    "GESTURE_REACQUIRE_MS",
    "GESTURE_CLICK_THRESHOLD",
    "GESTURE_CLICK_RELEASE_THRESHOLD",
    "GESTURE_CLICK_HOLD_MS",
    "GESTURE_CLICK_COOLDOWN_MS",
    "GESTURE_DRAG_UNLOCK_MS",
    "GESTURE_SCROLL_THRESHOLD",
    "GESTURE_SCROLL_HYSTERESIS",
    "GESTURE_SCROLL_POSE_MS",
    "GESTURE_SCROLL_GAIN",
    "GESTURE_SCROLL_MAX_IMPULSE",
    "GESTURE_SCROLL_INTERVAL_MS",
    "GESTURE_DIAGNOSTICS",
)


def camera_enabled_default() -> bool:
    """Whether the camera should come up with the application. OFF unless asked for."""
    return env_bool("GESTURE_CAMERA_AUTOSTART", False)


def gesture_enabled_default() -> bool:
    """
    Whether gesture control should come up with the application.

    OFF by default and deliberately so: a feature that moves the user's mouse pointer must be
    something they turned on, not something they discover happening.
    """
    return env_bool("GESTURE_ENABLED", False)


def diagnostics_enabled() -> bool:
    return env_bool("GESTURE_DIAGNOSTICS", False) or bool(
        os.environ.get("KAYRA_LOG_LEVEL", "").strip().upper() == "DEBUG")
