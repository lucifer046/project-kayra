# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_gesture_state.py                            │
# │   Synthetic Hands — features, stabilisation, and the temporal FSM      │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_gesture_state.py — standalone diagnostic for the gesture decision layer.

    .venv\\Scripts\\python tests\\test_gesture_state.py

HARDWARE-FREE. No camera, no MediaPipe, no mouse, no Qt. Every pose is a deterministic set of
21 landmark coordinates built by `hand()` below, so a click, a scroll, a dropout and a
corrupted frame can all be asserted exactly — at exact times, on an exact clock.

WHY SYNTHETIC LANDMARKS ARE THE RIGHT TEST HERE, AND WHERE THEY STOP
--------------------------------------------------------------------
Everything in this file is downstream of detection: given these landmarks at these times, what
does the machine decide? That question has one correct answer and no hardware in it, and a
suite that needed a camera and a human hand is a suite nobody runs before committing.

What this file explicitly CANNOT tell you is whether the system feels good to use. Landmark
noise from a real camera is not Gaussian, real hands do not hold a pose perfectly, and no
fixture here can measure whether the cursor feels sluggish. That is what the real-camera
validation is for, and no claim about the system being "fixed" rests on this file alone.

SECTIONS
  1. Landmark normalisation and scale invariance.
  2. Hand stability, and the poses it must and must not trust.
  3. One Euro, the dead-zone, the outlier gate and the speed ceiling.
  4. Hysteresis: enter, exit, dwell, and the rising edge.
  5. Cursor.
  6. Left click, right click, and that repetition produces ONE click.
  7. Scroll: activation, direction hysteresis, and that a still hand does not scroll.
  8. Conflict resolution — never two actions from one frame.
  9. Hand loss, the grace period, and re-acquisition.
 10. Jitter and outlier injection, measured.
 11. Legacy gestures: double click and the fist pause.
 12. Cost.
"""

import os
import sys
import math
import time
import random
import statistics

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.input.gesture.config import GestureConfig
from kayra.input.gesture.features import (
    FeatureExtractor, HandFeatures, pick_primary, LANDMARK_COUNT, MIN_HAND_SCALE,
)
from kayra.input.gesture.filters import (
    OneEuroFilter, OutlierGate, PointerStabilizer, Hysteresis, RateLimiter,
)
from kayra.input.gesture.state_machine import (
    GestureRecognizer, GestureState, ALL_STATES, STATE_LABELS, POINTER_STATES, PAUSED_STATES,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SYNTHETIC HAND FIXTURES                         │
# └────────────────────────────────────────────────────────────────────────┘
# Built in WIDTH-NORMALIZED geometry (the space the feature extractor works in) and converted
# back to raw MediaPipe normalized coordinates on the way out, so the fixtures exercise the
# aspect correction rather than bypassing it.

ASPECT = 480.0 / 640.0

_WRIST = (0.50, 0.62)
_MCP = {"index": (0.44, 0.45), "middle": (0.50, 0.45),
        "ring": (0.555, 0.45), "pinky": (0.61, 0.45)}
_CHAIN = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12),
          "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}

ALL_UP = {"index": True, "middle": True, "ring": True, "pinky": True}
INDEX_ONLY = {"index": True, "middle": False, "ring": False, "pinky": False}
TWO_FINGER = {"index": True, "middle": True, "ring": False, "pinky": False}
FIST = {"index": False, "middle": False, "ring": False, "pinky": False}


def hand(extended=None, thumb=None, offset=(0.0, 0.0), scale=1.0, noise=0.0, rng=None):
    """
    One synthetic hand. Returns 21 raw-normalized (x, y) pairs.

    `scale` shrinks or grows the whole hand about the wrist, which is how the scale-invariance
    checks put the same POSE at two distances from the camera and require the same answer.
    """
    rng = rng or random.Random(11)
    ox, oy = offset
    wx, wy = _WRIST

    def place(x, y):
        x = wx + (x - wx) * scale + ox
        y = wy + (y - wy) * scale + oy
        if noise:
            x += rng.uniform(-noise, noise)
            y += rng.uniform(-noise, noise)
        return (x, y)

    extended = extended or INDEX_ONLY
    points = [None] * LANDMARK_COUNT
    points[0] = place(wx, wy)
    tips = {}
    for name, (mcp_i, pip_i, dip_i, tip_i) in _CHAIN.items():
        mx, my = _MCP[name]
        points[mcp_i] = place(mx, my)
        if extended.get(name, False):
            points[pip_i] = place(mx, my - 0.05)
            points[dip_i] = place(mx, my - 0.09)
            points[tip_i] = place(mx, my - 0.13)
            tips[name] = (mx, my - 0.13)
        else:
            points[pip_i] = place(mx, my - 0.05)
            points[dip_i] = place(mx, my - 0.03)
            points[tip_i] = place(mx, my - 0.005)
            tips[name] = (mx, my - 0.005)

    tx, ty = thumb if thumb is not None else (0.34, 0.50)
    points[1] = place(0.42, 0.60)
    points[2] = place(0.38, 0.56)
    points[3] = place(tx + 0.02, ty + 0.03)
    points[4] = place(tx, ty)

    return [(x, y / ASPECT) for (x, y) in points], tips


def pinch_hand(which, **kwargs):
    """
    A pinch of `which` finger against the thumb.

    The pinching finger is CURLED and the thumb meets it there, which is what a real pinch
    does — and it matters: a fixture that put the thumb on an extended fingertip would also put
    it near the ADJACENT fingertip, satisfying the three-finger gate and turning every left
    click into a double. The pose the user actually makes does not have that problem, and the
    fixture must not invent one.
    """
    extended = dict(ALL_UP)
    extended[which] = False
    if which == "index":
        extended["ring"] = extended["pinky"] = False
    else:
        extended["ring"] = extended["pinky"] = False
    _, tips = hand(extended, **kwargs)
    tip = tips[which]
    points, _ = hand(extended, thumb=(tip[0] + 0.015, tip[1] + 0.015), **kwargs)
    return points


def fist_hand(**kwargs):
    """
    A closed fist: every finger curled AND the thumb resting across the hand, not on a tip.

    The thumb position is what separates this from the three-finger beak — both curl every
    finger, and `HandFeatures.thumb_pinched` is the only thing that tells them apart.
    """
    extended = {"index": False, "middle": False, "ring": False, "pinky": False}
    points, _ = hand(extended, thumb=(0.36, 0.52), **kwargs)
    return points


def relaxed_point(straightness, noise=0.0, rng=None, offset=(0.0, 0.0)):
    """
    A pointing hand whose index finger is only PARTLY straight.

    The default `hand(INDEX_ONLY)` fixture points with a perfectly straight finger, which
    measures at extension 1.00 — saturated, and nowhere near any threshold. Real fingers are
    not like that, and the pause flap lived entirely in the range this fixture covers. A
    fixture that cannot produce a half-extended finger cannot reproduce the bug, which is why
    the first attempt at a reproduction came back clean.
    """
    rng = rng or random.Random(11)
    ox, oy = offset
    points = [None] * LANDMARK_COUNT

    def place(x, y):
        if noise:
            x += rng.uniform(-noise, noise)
            y += rng.uniform(-noise, noise)
        return (x + ox, y + oy)

    points[0] = place(*_WRIST)
    for name, (mcp_i, pip_i, dip_i, tip_i) in _CHAIN.items():
        mx, my = _MCP[name]
        reach = straightness if name == "index" else 0.0
        points[mcp_i] = place(mx, my)
        points[pip_i] = place(mx, my - 0.05)
        points[dip_i] = place(mx, my - (0.03 + 0.06 * reach))
        points[tip_i] = place(mx, my - (0.005 + 0.125 * reach))
    points[1] = place(0.42, 0.60)
    points[2] = place(0.38, 0.56)
    points[3] = place(0.36, 0.53)
    points[4] = place(0.34, 0.50)
    return [(x, y / ASPECT) for (x, y) in points]


def beak_hand(**kwargs):
    """Index AND middle both curled onto the thumb — the three-finger double-click pose."""
    extended = {"index": False, "middle": False, "ring": False, "pinky": False}
    _, tips = hand(extended, **kwargs)
    mid = ((tips["index"][0] + tips["middle"][0]) / 2.0,
           (tips["index"][1] + tips["middle"][1]) / 2.0)
    points, _ = hand(extended, thumb=mid, **kwargs)
    return points


def config(**overrides):
    cfg = GestureConfig(**overrides)
    cfg.normalize()
    return cfg


def drive(recognizer, extractor, frames, dt=1.0 / 30.0, start=1000.0, grace=None):
    """Feeds a sequence of landmark lists and returns the decisions, one per frame."""
    decisions = []
    now = start
    for index, landmarks in enumerate(frames):
        features = None if landmarks is None else extractor.extract(landmarks, ASPECT,
                                                                    "Right", 0.9)
        grace_active = bool(grace(index)) if grace else False
        decisions.append(recognizer.update(features, now, grace_active=grace_active))
        now += dt
    return decisions


def fresh(cfg=None):
    cfg = cfg or config()
    return GestureRecognizer(cfg), FeatureExtractor(), cfg


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     1. NORMALISATION AND SCALE                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_normalisation():
    print_system("\n[1] Landmark normalisation and scale invariance")

    extractor = FeatureExtractor()
    near = extractor.extract(pinch_hand("index", scale=1.6), ASPECT, "Right", 0.9)
    extractor2 = FeatureExtractor()
    far = extractor2.extract(pinch_hand("index", scale=0.6), ASPECT, "Right", 0.9)

    check("a hand near the camera is valid", near.valid, f"scale={near.hand_scale:.3f}")
    check("a hand far from the camera is valid", far.valid, f"scale={far.hand_scale:.3f}")
    check("hand scale genuinely differs between the two",
          near.hand_scale > far.hand_scale * 2.0,
          f"{near.hand_scale:.3f} vs {far.hand_scale:.3f}")
    # THE WHOLE POINT OF THE REWRITE: the same POSE gives the same normalized measurement at
    # two very different distances. A pixel threshold cannot do this, and that is why v1's
    # clicking stopped working when the user leaned in.
    check("the SAME pinch measures the same at both distances",
          abs(near.index_pinch - far.index_pinch) < 0.04,
          f"{near.index_pinch:.3f} vs {far.index_pinch:.3f}")
    check("both are below the pinch threshold",
          near.index_pinch < 0.34 and far.index_pinch < 0.34)

    # Aspect correction: a vertical span and a horizontal span of equal PIXEL length must
    # measure equal here, whatever the frame's aspect ratio.
    flat = FeatureExtractor().extract(hand(INDEX_ONLY)[0], 1.0, "Right", 0.9)
    wide = FeatureExtractor().extract(hand(INDEX_ONLY)[0], ASPECT, "Right", 0.9)
    check("aspect correction changes the measured geometry",
          abs(flat.hand_scale - wide.hand_scale) > 1e-6,
          f"{flat.hand_scale:.4f} vs {wide.hand_scale:.4f}")

    open_hand = FeatureExtractor().extract(hand(ALL_UP)[0], ASPECT, "Right", 0.9)
    fist = FeatureExtractor().extract(hand(FIST)[0], ASPECT, "Right", 0.9)
    check("an open hand reports four fingers extended", open_hand.extended_count == 4,
          str(open_hand.extension))
    check("a fist reports none", fist.extended_count == 0, str(fist.extension))
    check("a fist is recognised as a fist", fist.fist)
    check("an open hand is recognised as open", open_hand.open_hand)
    check("index-only is not the two-finger pose",
          not FeatureExtractor().extract(hand(INDEX_ONLY)[0], ASPECT).two_finger_pose)
    check("index+middle IS the two-finger pose",
          FeatureExtractor().extract(hand(TWO_FINGER)[0], ASPECT).two_finger_pose)

    # Malformed input is DROPPED, never raised on. One bad frame must cost one frame.
    for bad, label in ((None, "None"), ([], "empty"), ([(0.0, 0.0)] * 5, "too few"),
                       ([(float("nan"), 0.0)] * 21, "NaN"),
                       ([(0.5, 0.5)] * 21, "degenerate")):
        result = FeatureExtractor().extract(bad, ASPECT)
        check(f"malformed landmarks ({label}) yield an invalid reading, no exception",
              isinstance(result, HandFeatures) and not result.valid)

    tiny = FeatureExtractor().extract(hand(INDEX_ONLY, scale=0.1)[0], ASPECT, "Right", 0.9)
    check("a hand below the minimum scale is refused rather than measured",
          not tiny.valid, f"MIN_HAND_SCALE={MIN_HAND_SCALE}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          2. HAND STABILITY                             │
# └────────────────────────────────────────────────────────────────────────┘

def section_stability():
    print_system("\n[2] Hand stability")

    steady = FeatureExtractor()
    readings = [steady.extract(hand(INDEX_ONLY)[0], ASPECT, "Right", 0.95) for _ in range(8)]
    check("a perfectly still hand converges to high stability",
          readings[-1].stability > 0.85, f"{readings[-1].stability:.3f}")
    check("the FIRST frame of a new hand is not yet trusted",
          readings[0].stability < 0.5, f"{readings[0].stability:.3f}")

    noisy = FeatureExtractor()
    rng = random.Random(5)
    scores = [noisy.extract(hand(INDEX_ONLY, noise=0.02, rng=rng)[0], ASPECT, "Right", 0.95)
              .stability for _ in range(12)]
    check("a violently noisy hand never reaches the action threshold",
          max(scores[3:]) < 0.5, f"max={max(scores[3:]):.3f}")

    low_conf = FeatureExtractor()
    for _ in range(6):
        reading = low_conf.extract(hand(INDEX_ONLY)[0], ASPECT, "Right", 0.3)
    check("low detector confidence caps stability", reading.stability < 0.35,
          f"{reading.stability:.3f}")

    # The factors MULTIPLY, so a good detector score cannot rescue INCOHERENT landmarks — a
    # hand whose points moved in different directions, which is the model re-fitting rather
    # than the hand moving.
    mixed = FeatureExtractor()
    mixed.extract(hand(INDEX_ONLY)[0], ASPECT, "Right", 0.99)
    deformed = mixed.extract(hand(INDEX_ONLY, noise=0.03, rng=random.Random(2))[0],
                             ASPECT, "Right", 0.99)
    check("high confidence does NOT rescue incoherent landmarks",
          deformed.stability < 0.4, f"{deformed.stability:.3f}")

    # ...but a hand that simply MOVED, rigidly, stays trusted. Scoring on raw displacement
    # instead of on the deformation residual would suppress every click made while the hand
    # was in motion, which is the same as making drag impossible.
    rigid = FeatureExtractor()
    for _ in range(5):
        rigid.extract(hand(INDEX_ONLY)[0], ASPECT, "Right", 0.95)
    moved = rigid.extract(hand(INDEX_ONLY, offset=(0.04, 0.0))[0], ASPECT, "Right", 0.95)
    check("a hand that moved rigidly stays trusted", moved.stability > 0.55,
          f"{moved.stability:.3f}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         3. THE FILTER STACK                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_filters():
    print_system("\n[3] One Euro, dead-zone, outlier gate, speed ceiling")

    euro = OneEuroFilter(1.0, 12.0)
    check("the first sample passes through untouched", euro(0.0, 0.5) == 0.5)
    check("a repeated timestamp returns the held value", euro(0.0, 0.9) == 0.5)
    settled = 0.5
    for i in range(1, 40):
        settled = euro(i / 30.0, 1.0)
    check("a step converges towards the new value", settled > 0.95, f"{settled:.4f}")
    euro.reset()
    check("reset clears the history", euro.value is None)
    euro.seed(0.0, 0.25)
    check("seed restarts AT a value", euro.value == 0.25)

    gate = OutlierGate(scales_per_s=14.0, max_consecutive=3)
    accepted, jumped = gate.check(0.0, 0.5, 0.5, 0.17)
    check("the first sample is accepted and flagged as a jump", accepted and jumped)
    accepted, _ = gate.check(1 / 30.0, 0.51, 0.5, 0.17)
    check("a plausible move is accepted", accepted)
    accepted, _ = gate.check(2 / 30.0, 0.95, 0.05, 0.17)
    check("an implausible jump is REJECTED", not accepted)
    # ...but not forever. A hand that really is somewhere else must be reachable.
    for i in range(3, 8):
        accepted, jumped = gate.check(i / 30.0, 0.95, 0.05, 0.17)
        if accepted:
            break
    check("a persistent new position is eventually accepted, as a JUMP", accepted and jumped,
          f"after {i - 2} rejections")
    check("the gate counted its rejections", gate.rejections >= 3, str(gate.rejections))

    cfg = config()
    stab = PointerStabilizer(cfg, (1920, 1080))
    first = stab.update(0.0, 0.5, 0.5, 0.17)
    check("the stabiliser emits a point for the first sample", first is not None, str(first))
    held = stab.update(1 / 30.0, 0.5001, 0.5001, 0.17)
    check("sub-dead-zone movement emits NOTHING (the pointer is left alone)", held is None)
    check("the dead-zone hold was counted", stab.deadzone_holds == 1)

    # The speed ceiling is the last backstop, and it applies to whatever got through.
    # The outlier gate is relaxed here on purpose: it would reject this jump first, and
    # the point of this check is the ceiling BELOW it, which is the backstop for a large
    # but plausible mapped step near the region edge.
    fast = PointerStabilizer(config(max_cursor_speed_px_s=600.0,
                                    outlier_scales_per_s=60.0), (1920, 1080))
    fast.update(0.0, 0.2, 0.5, 0.17)
    for i in range(1, 4):
        fast.update(i / 30.0, 0.8, 0.5, 0.17)
    check("the speed ceiling clamps a huge mapped step", fast.speed_clamps >= 1,
          f"clamps={fast.speed_clamps}")

    # Mapping: the calibration region, clamped rather than extrapolated.
    mapped = stab.map_normalized(cfg.region_x, cfg.region_top)
    check("the region's top-left maps to the screen origin",
          abs(mapped[0]) < 1e-6 and abs(mapped[1]) < 1e-6, str(mapped))
    corner = stab.map_normalized(1.0 - cfg.region_x, 1.0 - cfg.region_bottom)
    check("the region's bottom-right maps to the screen corner",
          abs(corner[0] - 1920) < 1e-6 and abs(corner[1] - 1080) < 1e-6, str(corner))
    beyond = stab.map_normalized(0.0, 0.0)
    check("a hand OUTSIDE the region is clamped, never extrapolated",
          beyond == (0.0, 0.0), str(beyond))

    limiter = RateLimiter(0.3)
    check("a fresh rate limiter is ready", limiter.take(100.0))
    check("it refuses inside the interval", not limiter.take(100.1))
    check("it allows again after the interval", limiter.take(100.4))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          4. HYSTERESIS                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_hysteresis():
    print_system("\n[4] Hysteresis: enter, exit, dwell, rising edge")

    gate = Hysteresis(enter=0.34, exit=0.46, dwell_s=0.08)
    engaged, rising = gate.update(0.30, 0.0)
    check("inside the threshold but not yet dwelled: not engaged", not engaged and not rising)
    check("...and reported as a candidate", gate.candidate)
    engaged, rising = gate.update(0.30, 0.05)
    check("still inside the dwell: not engaged", not engaged)
    engaged, rising = gate.update(0.30, 0.09)
    check("past the dwell: ENGAGED, with a rising edge", engaged and rising)
    engaged, rising = gate.update(0.30, 0.20)
    check("held: still engaged, NO second rising edge", engaged and not rising)

    # The hysteresis band. This is what stops the chatter.
    engaged, _ = gate.update(0.40, 0.25)
    check("a value between enter and exit does NOT release", engaged, "0.34 < 0.40 < 0.46")
    engaged, _ = gate.update(0.50, 0.30)
    check("a value past the exit threshold releases", not engaged)

    gate.reset()
    gate.update(0.30, 1.0)
    gate.update(0.50, 1.02)          # left the band before the dwell elapsed
    engaged, rising = gate.update(0.30, 1.05)
    check("a pose abandoned before its dwell does not fire late", not engaged and not rising)

    inverted = Hysteresis(enter=0.55, exit=0.22, dwell_s=0.0, invert=True)
    engaged, rising = inverted.update(0.60, 0.0)
    check("an inverted gate engages ABOVE its threshold", engaged and rising)
    engaged, _ = inverted.update(0.30, 0.1)
    check("an inverted gate holds through its neutral band", engaged, "0.22 < 0.30 < 0.55")
    engaged, _ = inverted.update(0.10, 0.2)
    check("an inverted gate releases below its exit", not engaged)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            5. CURSOR                                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_cursor():
    print_system("\n[5] Cursor")

    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [hand(INDEX_ONLY)[0] for _ in range(10)])
    check("an extended index finger is CURSOR",
          all(d.state == GestureState.CURSOR for d in decisions), decisions[-1].state)
    check("the pointer is tracked in CURSOR", all(d.track_pointer for d in decisions))
    check("the pointer carries the index tip",
          decisions[-1].pointer is not None and len(decisions[-1].pointer) == 2)
    check("no action fires from cursor movement alone",
          not any(d.acted for d in decisions))
    check("cursor confidence is reported", decisions[-1].cursor_confidence > 0.0,
          f"{decisions[-1].cursor_confidence:.3f}")

    # Movement across the frame: the pointer follows and nothing else happens.
    rec, ext, cfg = fresh()
    moving = [hand(INDEX_ONLY, offset=(0.02 * i, 0.0))[0] for i in range(14)]
    decisions = drive(rec, ext, moving)
    xs = [d.pointer[0] for d in decisions if d.pointer]
    check("the pointer moves with the hand", xs[-1] > xs[0] + 0.15, f"{xs[0]:.3f}->{xs[-1]:.3f}")
    check("fast movement fires no clicks", not any(d.acted for d in decisions))

    check("every declared state has a label",
          all(state in STATE_LABELS for state in ALL_STATES))
    check("every pointer state is a declared state",
          all(state in ALL_STATES for state in POINTER_STATES))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       6. CLICKS, AND ONLY ONE                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_clicks():
    print_system("\n[6] Left click, right click, and the one-click rule")

    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [pinch_hand("index") for _ in range(20)])
    lefts = sum(1 for d in decisions if d.fire_left)
    check("index + thumb produces a left click", lefts >= 1)
    # THE REQUIREMENT FROM THE BRIEF: pinch, pinch, pinch, release is ONE click.
    check("a HELD pinch produces exactly ONE left click", lefts == 1, f"{lefts} clicks")
    check("no right click comes with it", not any(d.fire_right for d in decisions))
    check("no double click comes with it", not any(d.fire_double for d in decisions))
    check("the machine reaches LEFT_CLICK_HELD",
          any(d.state == GestureState.LEFT_CLICK_HELD for d in decisions))
    check("left-click confidence is reported",
          max(d.left_click_confidence for d in decisions) > 0.0)

    # THE POINTER IS FROZEN WHILE THE CLICK IS BEING MADE. This is what makes a click land
    # where the user aimed rather than where the pinch dragged it.
    before_fire = [d for d in decisions[:6]]
    check("the pointer is anchored during the pinch candidate and the click",
          not any(d.track_pointer for d in before_fire),
          str([d.state for d in before_fire]))
    check("a SUSTAINED pinch unlocks the pointer again (drag)",
          decisions[-1].track_pointer, f"after {cfg.drag_unlock_ms:.0f}ms")

    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [pinch_hand("middle") for _ in range(20)])
    rights = sum(1 for d in decisions if d.fire_right)
    check("middle + thumb produces a right click", rights >= 1)
    check("a HELD middle pinch produces exactly ONE right click", rights == 1, f"{rights}")
    check("no left click comes with it", not any(d.fire_left for d in decisions))
    check("the machine reaches RIGHT_CLICK_HELD",
          any(d.state == GestureState.RIGHT_CLICK_HELD for d in decisions))

    # Release and re-pinch DOES click again — the cooldown must not make the feature unusable.
    rec, ext, cfg = fresh()
    sequence = ([pinch_hand("index")] * 8 + [hand(INDEX_ONLY)[0]] * 10
                + [pinch_hand("index")] * 8 + [hand(INDEX_ONLY)[0]] * 10
                + [pinch_hand("index")] * 8)
    decisions = drive(rec, ext, sequence)
    clicks = sum(1 for d in decisions if d.fire_left)
    check("three separate pinches produce three clicks", clicks == 3, f"{clicks}")

    # The cooldown blocks a re-pinch that arrives faster than a human means it.
    rec, ext, cfg = fresh(config(click_cooldown_ms=1000.0))
    sequence = ([pinch_hand("index")] * 5 + [hand(INDEX_ONLY)[0]] * 4) * 4
    decisions = drive(rec, ext, sequence)
    clicks = sum(1 for d in decisions if d.fire_left)
    check("the click cooldown limits rapid re-pinching", clicks == 1, f"{clicks} in 1s")

    # An UNSTABLE hand may not click at all.
    rec, ext, cfg = fresh()
    rng = random.Random(4)
    noisy = [pinch_hand("index", noise=0.02, rng=rng) for _ in range(24)]
    decisions = drive(rec, ext, noisy)
    check("a violently unstable hand fires NO click",
          not any(d.fire_left or d.fire_right or d.fire_double for d in decisions))

    # Low detector confidence is refused too, through the same gate.
    rec = GestureRecognizer(config())
    ext = FeatureExtractor()
    now = 1000.0
    fired = False
    for _ in range(20):
        features = ext.extract(pinch_hand("index"), ASPECT, "Right", 0.2)
        fired = fired or rec.update(features, now).fire_left
        now += 1 / 30.0
    check("low detector confidence blocks the click", not fired)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             7. SCROLL                                  │
# └────────────────────────────────────────────────────────────────────────┘

def section_scroll():
    print_system("\n[7] Two-finger scroll")

    # A STILL two-finger pose must not scroll. This is the defect in v1's anchor model: it
    # scrolled forever while the hand was held away from the anchor.
    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [hand(TWO_FINGER)[0] for _ in range(30)])
    check("a STILL two-finger hand scrolls by nothing",
          not any(d.scroll_delta for d in decisions))
    check("...and reaches the scroll pose", any(d.state in (GestureState.SCROLL_CANDIDATE,
                                                            GestureState.SCROLL_ACTIVE)
                                                for d in decisions))

    # Moving UP the frame scrolls the content up (a positive wheel delta).
    rec, ext, cfg = fresh()
    up = [hand(TWO_FINGER, offset=(0.0, -0.006 * i))[0] for i in range(30)]
    decisions = drive(rec, ext, up)
    deltas = [d.scroll_delta for d in decisions if d.scroll_delta]
    check("moving two fingers UP scrolls", bool(deltas), str(deltas[:3]))
    check("...and every impulse is in the same direction",
          all(delta > 0 for delta in deltas), str(deltas[:5]))
    check("scroll reaches SCROLL_ACTIVE",
          any(d.state == GestureState.SCROLL_ACTIVE for d in decisions))
    check("no click fires during a scroll",
          not any(d.fire_left or d.fire_right or d.fire_double for d in decisions))
    check("the pointer does NOT move while scrolling",
          not any(d.track_pointer for d in decisions
                  if d.state in (GestureState.SCROLL_ACTIVE, GestureState.SCROLL_CANDIDATE)))
    check("scroll confidence is reported",
          max(d.scroll_confidence for d in decisions) > 0.0)

    rec, ext, cfg = fresh()
    down = [hand(TWO_FINGER, offset=(0.0, 0.006 * i))[0] for i in range(30)]
    deltas = [d.scroll_delta for d in drive(rec, ext, down) if d.scroll_delta]
    check("moving two fingers DOWN scrolls the other way", bool(deltas) and
          all(delta < 0 for delta in deltas), str(deltas[:5]))

    # DIRECTION HYSTERESIS. One noisy frame in the opposite sense must not reverse the scroll.
    rec, ext, cfg = fresh()
    frames = []
    for i in range(40):
        step = -0.006 * i
        if i in (14, 22, 30):
            step += 0.004          # a single frame that momentarily moves the other way
        frames.append(hand(TWO_FINGER, offset=(0.0, step))[0])
    deltas = [d.scroll_delta for d in drive(rec, ext, frames) if d.scroll_delta]
    reversals = sum(1 for a, b in zip(deltas, deltas[1:]) if a * b < 0)
    check("a noisy frame does not reverse the scroll direction", reversals == 0,
          f"{reversals} reversals in {len(deltas)} impulses")

    # Impulse magnitude is bounded, whatever the velocity.
    rec, ext, cfg = fresh(config(max_scroll_impulse=200))
    violent = [hand(TWO_FINGER, offset=(0.0, -0.05 * i))[0] for i in range(20)]
    deltas = [abs(d.scroll_delta) for d in drive(rec, ext, violent) if d.scroll_delta]
    check("the scroll impulse is capped", not deltas or max(deltas) <= 200,
          f"max={max(deltas) if deltas else 0}")

    # The impulse RATE is limited independently of the frame rate.
    rec, ext, cfg = fresh(config(scroll_interval_ms=100.0))
    frames = [hand(TWO_FINGER, offset=(0.0, -0.006 * i))[0] for i in range(30)]
    impulses = sum(1 for d in drive(rec, ext, frames) if d.scroll_delta)
    check("scroll impulses are rate-limited", impulses <= 12, f"{impulses} in 1s at 100ms")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    8. CONFLICT RESOLUTION                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_conflicts():
    print_system("\n[8] Conflict resolution — one action per frame")

    for name, frames in (
            ("cursor", [hand(INDEX_ONLY)[0]] * 20),
            ("left pinch", [pinch_hand("index")] * 20),
            ("right pinch", [pinch_hand("middle")] * 20),
            ("beak", [beak_hand()] * 20),
            ("scroll", [hand(TWO_FINGER, offset=(0.0, -0.006 * i))[0] for i in range(24)]),
            ("fist", [hand(FIST)[0]] * 12),
    ):
        rec, ext, cfg = fresh()
        decisions = drive(rec, ext, frames)
        worst = max((int(d.fire_left) + int(d.fire_right) + int(d.fire_double)
                     + int(bool(d.scroll_delta))) for d in decisions)
        check(f"'{name}' never produces two actions in one frame", worst <= 1, f"max={worst}")

    # A thumb equidistant between index and middle tips is AMBIGUOUS, and the right answer to
    # an ambiguous intent is to do nothing — not to pick one at random.
    extended = {"index": False, "middle": False, "ring": False, "pinky": False}
    _, tips = hand(extended)
    midpoint = ((tips["index"][0] + tips["middle"][0]) / 2.0,
                (tips["index"][1] + tips["middle"][1]) / 2.0 - 0.06)
    ambiguous, _ = hand(extended, thumb=midpoint)
    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [ambiguous] * 20)
    check("an ambiguous pinch never fires BOTH a left and a right click",
          not any(d.fire_left and d.fire_right for d in decisions))

    # The state is always one of the declared ones.
    rec, ext, cfg = fresh()
    everything = ([hand(INDEX_ONLY)[0]] * 5 + [pinch_hand("index")] * 8
                  + [hand(TWO_FINGER, offset=(0.0, -0.006 * i))[0] for i in range(10)]
                  + [hand(FIST)[0]] * 5 + [None] * 5 + [hand(ALL_UP)[0]] * 5)
    decisions = drive(rec, ext, everything)
    check("every state reached is a declared state",
          all(d.state in ALL_STATES for d in decisions),
          str(sorted({d.state for d in decisions})))
    check("a mixed sequence never produces two actions in one frame",
          all((int(d.fire_left) + int(d.fire_right) + int(d.fire_double)
               + int(bool(d.scroll_delta))) <= 1 for d in decisions))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    9. HAND LOSS AND RE-ACQUISITION                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_hand_loss():
    print_system("\n[9] Hand loss, grace period, re-acquisition")

    rec, ext, cfg = fresh()
    frames = [pinch_hand("index")] * 8 + [None] * 2 + [pinch_hand("index")] * 8
    decisions = drive(rec, ext, frames, grace=lambda i: 8 <= i < 10)
    check("a two-frame dropout inside the grace period HOLDS the state",
          decisions[8].state == decisions[7].state, decisions[8].state)
    check("...and produces no action", not decisions[8].acted and not decisions[9].acted)
    check("...and does not move the pointer", not decisions[8].track_pointer)
    check("the dropout is reported as suppressed",
          decisions[8].suppressed == "hand-lost-grace", decisions[8].suppressed)
    clicks = sum(1 for d in decisions if d.fire_left)
    check("a brief dropout does not turn one pinch into two clicks", clicks == 1, f"{clicks}")

    # PAST the grace period everything is dropped, and a returning hand starts clean.
    rec, ext, cfg = fresh()
    frames = [pinch_hand("index")] * 8 + [None] * 12 + [pinch_hand("index")] * 12
    decisions = drive(rec, ext, frames, grace=lambda i: False)
    check("past the grace period the machine reports NO_HAND",
          decisions[10].state == GestureState.NO_HAND)
    check("...and fires nothing while the hand is absent",
          not any(d.acted for d in decisions[8:20]))
    check("a returning hand can click again",
          any(d.fire_left for d in decisions[20:]))

    # A hand that leaves and returns SOMEWHERE ELSE must not fire on arrival.
    rec, ext, cfg = fresh()
    frames = ([hand(INDEX_ONLY)[0]] * 8 + [None] * 8
              + [pinch_hand("index", offset=(0.25, -0.15))] * 3)
    decisions = drive(rec, ext, frames)
    check("a hand reappearing elsewhere does not click on its first frames",
          not any(d.fire_left for d in decisions[16:19]),
          "stability gates the first frames of a new hand")

    check("no-hand decisions carry no pointer",
          all(d.pointer is None for d in decisions[8:16]))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    10. JITTER AND OUTLIERS, MEASURED                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_jitter():
    print_system("\n[10] Jitter and outlier injection, measured")

    cfg = config()
    rng = random.Random(3)
    stab = PointerStabilizer(cfg, (1920, 1080))
    raw, filtered = [], []
    now = 0.0
    for _ in range(300):
        nx = 0.5 + rng.gauss(0, 0.0035)
        ny = 0.5 + rng.gauss(0, 0.0035)
        raw.append(stab.map_normalized(nx, ny))
        point = stab.update(now, nx, ny, 0.17)
        if point:
            filtered.append(point)
        now += 1 / 30.0

    def spread(points):
        return (statistics.pstdev([p[0] for p in points])
                + statistics.pstdev([p[1] for p in points])) / 2.0

    raw_spread, filtered_spread = spread(raw), spread(filtered)
    print_info(f"      stationary hand: raw {raw_spread:.2f}px -> filtered "
               f"{filtered_spread:.2f}px, {len(filtered)}/300 frames emitted a move")
    check("filtering materially reduces stationary jitter",
          filtered_spread < raw_spread * 0.6,
          f"{raw_spread:.2f} -> {filtered_spread:.2f}")
    check("a still hand leaves the pointer alone most of the time",
          len(filtered) < 230, f"{len(filtered)}/300 moves")
    check("...but the pointer is not frozen outright", len(filtered) > 20)

    # A stationary NOISY hand must produce no clicks and no scroll at all.
    rec, ext, _ = fresh()
    rng = random.Random(9)
    decisions = drive(rec, ext,
                      [hand(INDEX_ONLY, noise=0.004, rng=rng)[0] for _ in range(200)])
    check("a stationary jittering hand fires no click", not any(d.fire_left or d.fire_right
                                                                or d.fire_double
                                                                for d in decisions))
    check("...and no scroll", not any(d.scroll_delta for d in decisions))
    states = {d.state for d in decisions[10:]}
    check("...and the gesture state does not flicker between conflicting gestures",
          states <= {GestureState.CURSOR, GestureState.TRACKING, GestureState.ACQUIRING,
                     GestureState.LEFT_CLICK_CANDIDATE, GestureState.RIGHT_CLICK_CANDIDATE},
          str(sorted(states)))

    # OUTLIER INJECTION: single corrupted frames must not teleport the pointer.
    stab = PointerStabilizer(cfg, (1920, 1080))
    now, last, biggest = 0.0, None, 0.0
    for i in range(120):
        nx, ny = (0.95, 0.05) if i in (30, 31, 70, 100) else (0.5, 0.5)
        point = stab.update(now, nx, ny, 0.17)
        if point and last:
            biggest = max(biggest, math.hypot(point[0] - last[0], point[1] - last[1]))
        if point:
            last = point
        now += 1 / 30.0
    print_info(f"      outlier injection: largest emitted step {biggest:.1f}px, "
               f"{stab.dropped_outliers} samples dropped")
    check("corrupted landmark frames do not teleport the pointer", biggest < 120.0,
          f"{biggest:.1f}px")
    check("...and they were actually detected", stab.dropped_outliers >= 4,
          str(stab.dropped_outliers))

    # A corrupted frame must not click either.
    rec, ext, _ = fresh()
    frames = [hand(INDEX_ONLY)[0]] * 10
    frames[5] = pinch_hand("index", offset=(0.3, -0.25))
    decisions = drive(rec, ext, frames)
    check("one corrupted frame does not fire a click",
          not any(d.fire_left for d in decisions))

    # Latency, measured against the raw target, for a slow move and a fast one.
    def lag(step, count):
        s = PointerStabilizer(cfg, (1920, 1080))
        t, errors = 0.0, []
        for i in range(count):
            nx = 0.30 + step * i
            point = s.update(t, nx, 0.5, 0.17)
            target = s.map_normalized(nx, 0.5)
            if point:
                errors.append(abs(point[0] - target[0]))
            t += 1 / 30.0
        mean = sum(errors) / max(len(errors), 1)
        per_frame = step * (1920 / (1.0 - 2 * cfg.region_x))
        return mean, mean / max(per_frame, 1e-6) * 33.3

    slow_px, slow_ms = lag(0.004, 90)
    fast_px, fast_ms = lag(0.020, 40)
    print_info(f"      cursor lag: slow move {slow_px:.0f}px / {slow_ms:.0f}ms, "
               f"fast flick {fast_px:.0f}px / {fast_ms:.0f}ms")
    check("slow deliberate movement is not sluggish", slow_ms < 100.0, f"{slow_ms:.0f}ms")
    # THE POINT OF ONE EURO: the filter must RELEASE under speed, so fast movement is not
    # penalised more than slow movement. A single EMA gets this backwards.
    check("fast movement is at least as responsive as slow movement", fast_ms <= slow_ms + 5,
          f"{fast_ms:.0f}ms vs {slow_ms:.0f}ms")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     11. THE LEGACY GESTURES                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_legacy():
    print_system("\n[11] Legacy gestures preserved: double click, fist pause")

    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [beak_hand() for _ in range(24)])
    doubles = sum(1 for d in decisions if d.fire_double)
    check("the three-finger beak produces a double click", doubles >= 1)
    check("a HELD beak produces exactly ONE double click", doubles == 1, f"{doubles}")
    check("a double click does not also fire a left click",
          not any(d.fire_left for d in decisions))
    check("a double click does not also fire a right click",
          not any(d.fire_right for d in decisions))
    check("the machine reaches DOUBLE_CLICK_HELD",
          any(d.state == GestureState.DOUBLE_CLICK_HELD for d in decisions))

    # The fist still pauses — but it is now a HELD pose, not a per-frame comparator. Section
    # 13 owns that behaviour in full; this only checks the gesture survived the rewrite.
    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [fist_hand() for _ in range(40)])
    check("a HELD closed fist still pauses",
          any(d.state == GestureState.PAUSED for d in decisions),
          str(sorted({d.state for d in decisions})))
    check("nothing happens while paused",
          not any(d.acted for d in decisions))

    # A fist must clear latched gates, so opening the hand starts clean.
    rec, ext, cfg = fresh()
    frames = [pinch_hand("index")] * 6 + [fist_hand()] * 40 + [hand(INDEX_ONLY)[0]] * 20
    decisions = drive(rec, ext, frames)
    check("leaving a fist does not fire a stale click",
          not any(d.fire_left for d in decisions[46:]))

    # An open hand is not an actionable gesture and must produce nothing.
    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [hand(ALL_UP)[0] for _ in range(15)])
    check("a flat open hand produces no action", not any(d.acted for d in decisions))

    # Deterministic primary-hand selection when two are visible.
    big = FeatureExtractor().extract(hand(INDEX_ONLY, scale=1.4)[0], ASPECT, "Right", 0.9)
    small = FeatureExtractor().extract(hand(INDEX_ONLY, scale=0.7)[0], ASPECT, "Left", 0.9)
    check("the larger hand is chosen as primary", pick_primary([small, big]) is big)
    check("...regardless of the order they arrive in",
          pick_primary([big, small]) is pick_primary([small, big]))
    check("an invalid candidate is never chosen",
          pick_primary([FeatureExtractor().extract(None, ASPECT), big]) is big)
    check("no hands means no primary", pick_primary([]) is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             12. COST                                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_cost():
    print_system("\n[12] Cost")

    landmarks = hand(INDEX_ONLY)[0]
    extractor = FeatureExtractor()
    started = time.perf_counter()
    for _ in range(3000):
        extractor.extract(landmarks, ASPECT, "Right", 0.9)
    extract_us = (time.perf_counter() - started) / 3000 * 1e6

    rec = GestureRecognizer(config())
    features = extractor.extract(landmarks, ASPECT, "Right", 0.9)
    now = 1000.0
    started = time.perf_counter()
    for _ in range(3000):
        rec.update(features, now)
        now += 1 / 30.0
    decide_us = (time.perf_counter() - started) / 3000 * 1e6

    stab = PointerStabilizer(config(), (1920, 1080))
    now = 0.0
    started = time.perf_counter()
    for i in range(3000):
        stab.update(now, 0.5 + (i % 7) * 0.001, 0.5, 0.17)
        now += 1 / 30.0
    stabilize_us = (time.perf_counter() - started) / 3000 * 1e6

    print_info(f"      features {extract_us:.1f}us · decision {decide_us:.1f}us · "
               f"stabiliser {stabilize_us:.1f}us  "
               f"(total {extract_us + decide_us + stabilize_us:.1f}us of a 33,000us frame)")
    total = extract_us + decide_us + stabilize_us
    check("the whole decision layer costs well under 1% of a frame", total < 300.0,
          f"{total:.1f}us")

    # BOUNDED BY CONSTRUCTION. A 30Hz loop that runs all day must not accumulate anything.
    rec = GestureRecognizer(config())
    extractor = FeatureExtractor()
    now = 1000.0
    for i in range(20000):
        pose = landmarks if i % 3 else hand(TWO_FINGER, offset=(0.0, -0.001 * (i % 40)))[0]
        rec.update(extractor.extract(pose, ASPECT, "Right", 0.9), now)
        now += 1 / 30.0
    check("the extractor's history stays bounded",
          len(extractor._scales) <= FeatureExtractor.HISTORY
          and len(extractor._spans) <= FeatureExtractor.HISTORY,
          f"{len(extractor._scales)} scales")
    check("the recogniser holds no growing collection",
          not any(isinstance(value, (list, dict, set))
                  for value in vars(rec).values()),
          str([k for k, v in vars(rec).items() if isinstance(v, (list, dict, set))]))


def section_pause():
    """
    The pause gesture, and the ACTIVE <-> PAUSED flap it used to produce.

    THE BUG THIS SECTION EXISTS FOR. v2.0 tested `features.fist` — four independent
    `extension >= 0.55` comparators — once per frame and paused immediately. Every other
    gesture in the system had hysteresis and a dwell; this one had a bare comparator. A real
    pointing finger is not perfectly straight, its measured extension sits close to 0.55, and
    landmark noise carried it across that comparator several times a second.

    `section_pause_flap_reproduction` below measures the old behaviour's rate directly, so the
    number in the report is a measurement rather than a claim.
    """
    print_system("\n[13] The pause gesture")

    # ── A sustained fist pauses, exactly once ──
    rec, ext, cfg = fresh()
    frames = [hand(INDEX_ONLY)[0]] * 20 + [fist_hand()] * 40 + [hand(INDEX_ONLY)[0]] * 5
    decisions = drive(rec, ext, frames)
    states = [d.state for d in decisions]
    entries = sum(1 for a, b in zip(states, states[1:])
                  if a not in PAUSED_STATES and b in PAUSED_STATES)
    check("a sustained fist pauses", GestureState.PAUSED in states)
    check("...exactly once", entries == 1, f"{entries} entries")
    check("...after a real dwell, not on the first frame",
          states[20] != GestureState.PAUSED, states[20])
    check("...and it passes through PAUSE_CANDIDATE on the way",
          GestureState.PAUSE_CANDIDATE in states, str(sorted(set(states))))
    check("nothing is clicked while paused",
          not any(d.acted for d in decisions if d.state in PAUSED_STATES))
    check("the pointer does not move while PAUSED",
          not any(d.track_pointer for d in decisions if d.state == GestureState.PAUSED))
    check("...but a forming fist has not taken control away yet",
          any(d.track_pointer for d in decisions
              if d.state == GestureState.PAUSE_CANDIDATE),
          "a candidate is not a pause")

    # ── TRANSIENT fist-like frames must NOT pause. This is the reported bug. ──
    for count, label in ((1, "one frame"), (2, "two frames"), (4, "four frames")):
        rec, ext, cfg = fresh()
        frames = []
        for _ in range(6):
            frames += [hand(INDEX_ONLY)[0]] * 12 + [fist_hand()] * count
        decisions = drive(rec, ext, frames)
        paused = [d for d in decisions if d.state in PAUSED_STATES]
        check(f"a fist-like flicker of {label} never pauses", not paused,
              f"{len(paused)} paused frames")

    # ── A hand whose fingers sit NEAR the old comparator must never pause ──
    # This is the exact reported situation: an ordinary cursor session with a relaxed pointing
    # finger. The old code produced 206 ACTIVE->PAUSED->ACTIVE round trips in 600 frames here.
    for straightness in (0.62, 0.55, 0.50, 0.45):
        rec, ext, cfg = fresh()
        rng = random.Random(4)
        frames = [relaxed_point(straightness, noise=0.004, rng=rng,
                                offset=(0.0006 * (i % 60), 0.0004 * (i % 40)))
                  for i in range(600)]
        decisions = drive(rec, ext, frames, dt=1.0 / 25.0)
        states = [d.state for d in decisions]
        flips = sum(1 for a, b in zip(states, states[1:])
                    if (a in PAUSED_STATES) != (b in PAUSED_STATES))
        measured = ext.extract(frames[-1], ASPECT, "Right", 0.9).extension[0]
        check(f"a relaxed pointing finger (extension {measured:.2f}) never pauses",
              flips == 0, f"{flips} ACTIVE<->PAUSED transitions in 600 frames")

    # ── While PAUSED, brief noise must not resume ──
    rec, ext, cfg = fresh()
    frames = ([hand(INDEX_ONLY)[0]] * 10 + [fist_hand()] * 40
              + [hand(INDEX_ONLY)[0]] * 3 + [fist_hand()] * 25)
    decisions = drive(rec, ext, frames)
    tail = [d.state for d in decisions[-20:]]
    check("three frames of an open hand do not resume from a pause",
          all(state in PAUSED_STATES for state in tail), str(sorted(set(tail))))
    check("...and nothing is clicked on the way",
          not any(d.acted for d in decisions[50:]))

    # ── A SUSTAINED open hand does resume ──
    rec, ext, cfg = fresh()
    frames = [hand(INDEX_ONLY)[0]] * 10 + [fist_hand()] * 40 + [hand(INDEX_ONLY)[0]] * 30
    decisions = drive(rec, ext, frames)
    check("a sustained open hand resumes control",
          decisions[-1].state == GestureState.CURSOR, decisions[-1].state)
    check("...through RESUME_CANDIDATE",
          GestureState.RESUME_CANDIDATE in [d.state for d in decisions],
          str(sorted({d.state for d in decisions})))
    check("...and resuming does not fire a click",
          not any(d.fire_left or d.fire_right or d.fire_double for d in decisions[50:]))

    # ── NO_HAND IS NOT A PAUSE. The distinction the brief calls critical. ──
    rec, ext, cfg = fresh()
    frames = [hand(INDEX_ONLY)[0]] * 15 + [None] * 40 + [hand(INDEX_ONLY)[0]] * 15
    decisions = drive(rec, ext, frames)
    states = [d.state for d in decisions]
    check("an empty frame reaches NO_HAND", GestureState.NO_HAND in states)
    check("...and NEVER reaches PAUSED",
          not any(state in PAUSED_STATES for state in states), str(sorted(set(states))))
    check("...and the hand returning does not pause either",
          states[-1] == GestureState.CURSOR, states[-1])

    # ── SCROLL IS NOT A PAUSE ──
    rec, ext, cfg = fresh()
    frames = ([hand(INDEX_ONLY)[0]] * 10
              + [hand(TWO_FINGER, offset=(0.0, -0.005 * i))[0] for i in range(40)]
              + [hand(INDEX_ONLY)[0]] * 15)
    decisions = drive(rec, ext, frames)
    states = [d.state for d in decisions]
    check("scrolling never passes through a pause",
          not any(state in PAUSED_STATES for state in states), str(sorted(set(states))))
    check("...and scroll returns to CURSOR, not to a pause",
          states[-1] == GestureState.CURSOR, states[-1])

    # ── CLICKS ARE NOT A PAUSE ──
    for finger in ("index", "middle"):
        rec, ext, cfg = fresh()
        frames = ([hand(INDEX_ONLY)[0]] * 10 + [pinch_hand(finger)] * 25
                  + [hand(INDEX_ONLY)[0]] * 10)
        states = [d.state for d in drive(rec, ext, frames)]
        check(f"a {finger} pinch never passes through a pause",
              not any(state in PAUSED_STATES for state in states),
              str(sorted(set(states))))

    # A BEAK IS NOT A FIST. Both curl every finger; only the thumb tells them apart.
    rec, ext, cfg = fresh()
    decisions = drive(rec, ext, [beak_hand()] * 40)
    states = [d.state for d in decisions]
    check("the three-finger beak is a double click, never a pause",
          not any(state in PAUSED_STATES for state in states), str(sorted(set(states))))
    check("...and it still fires exactly one double click",
          sum(1 for d in decisions if d.fire_double) == 1)

    # ── An UNSTABLE fist must not pause either ──
    rec, ext, cfg = fresh()
    rng = random.Random(8)
    decisions = drive(rec, ext, [fist_hand(noise=0.02, rng=rng) for _ in range(60)])
    check("a violently unstable fist does not pause",
          not any(d.state in PAUSED_STATES for d in decisions),
          "the same stability gate that guards clicks")

    # ── The gate can be switched off entirely ──
    rec, ext, cfg = fresh(config(pause_enabled=False))
    decisions = drive(rec, ext, [fist_hand()] * 60)
    check("GESTURE_PAUSE_ENABLED=False disables the pause gesture",
          not any(d.state in PAUSED_STATES for d in decisions))


def section_pause_flap_reproduction():
    """
    The old behaviour, measured, so the fix has a number attached to it.

    Reimplements the v2.0 rule — `extended_count == 0`, per frame, no dwell, no hysteresis —
    over the SAME frames the fixed machine sees, and counts what it would have done. This is
    not a test of production code; it is the evidence that the reported symptom was real and
    that the fix addresses its actual cause.
    """
    print_system("\n[14] The flap, measured before and after")

    print_info("      index straightness | measured ext | OLD rule flips | NEW machine flips")
    worst_old = 0
    for straightness in (0.70, 0.62, 0.55, 0.50, 0.45):
        rec, ext, cfg = fresh()
        rng = random.Random(4)
        frames = [relaxed_point(straightness, noise=0.004, rng=rng,
                                offset=(0.0006 * (i % 60), 0.0004 * (i % 40)))
                  for i in range(600)]

        # The OLD rule, applied to the same features the new machine sees.
        probe = FeatureExtractor()
        old_states, measured = [], []
        for landmarks in frames:
            features = probe.extract(landmarks, ASPECT, "Right", 0.9)
            measured.append(features.extension[0] if features.valid else 0.0)
            old_states.append(features.valid and features.extended_count == 0
                              and not features.thumb_pinched)
        old_flips = sum(1 for a, b in zip(old_states, old_states[1:]) if a != b)

        new_states = [d.state in PAUSED_STATES for d in drive(rec, ext, frames, dt=1 / 25.0)]
        new_flips = sum(1 for a, b in zip(new_states, new_states[1:]) if a != b)
        worst_old = max(worst_old, old_flips)

        print_info(f"      {straightness:>17.2f} | {sum(measured) / len(measured):>12.2f} | "
                   f"{old_flips:>14d} | {new_flips:>17d}")
        check(f"straightness {straightness:.2f}: the fixed machine does not flap",
              new_flips == 0, f"{new_flips} transitions")

    check("the old rule genuinely flapped on this data", worst_old > 50,
          f"{worst_old} transitions in 600 frames — the symptom was real")


def main():
    print_banner("GESTURE STATE DIAGNOSTIC",
                 "synthetic hands · hysteresis · stabilisation · arbitration")
    section_normalisation()
    section_stability()
    section_filters()
    section_hysteresis()
    section_cursor()
    section_clicks()
    section_scroll()
    section_conflicts()
    section_hand_loss()
    section_jitter()
    section_legacy()
    section_pause()
    section_pause_flap_reproduction()
    section_cost()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All gesture state checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
