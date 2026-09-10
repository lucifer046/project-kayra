# ┌────────────────────────────────────────────────────────────────────────┐
# │                        gesture/features.py                             │
# │       Scale-Invariant Hand Geometry, And How Much To Trust It          │
# └────────────────────────────────────────────────────────────────────────┘
"""
Turns 21 landmarks into the handful of numbers the state machine reasons about.

THE ONE IDEA: EVERY DISTANCE IS DIVIDED BY HAND SCALE
-----------------------------------------------------
`hand_scale` is the wrist-to-middle-MCP distance — the palm's long axis. It is the right
reference for three reasons and each was checked against the alternatives:

  * It is a RIGID span. Wrist and middle MCP are both skeletal; nothing the fingers do changes
    the distance between them, so a pinch ratio measured against it varies only with the
    pinch. Using a fingertip-to-fingertip span (say index MCP to pinky MCP) is nearly as
    rigid but collapses when the hand rotates to face the camera edge-on.
  * It is LARGE relative to landmark noise. ~90px at arm's length in a 640px frame, so 2px of
    regression jitter is ~2% of the reference. Wrist-to-index-MCP is 30% shorter and carries
    correspondingly more noise into every ratio computed from it.
  * It is ALWAYS VISIBLE for the poses this system cares about. Every gesture here is made
    palm-toward-camera, and both points are in the palm.

Aspect correction is applied before any distance is taken. MediaPipe returns coordinates
normalized independently on each axis, so on a 640x480 frame a vertical span of 0.1 is 48px
and a horizontal span of 0.1 is 64px. Comparing them directly — which the v1 engine did, via
`dist()` on raw normalized values in some paths and on pixels in others — makes every ratio
depend on the camera's aspect ratio. Everything here works in **width-normalized** units: x as
given, y multiplied by (height/width). Distances are then comparable, and independent of the
capture resolution.

WHAT `stability` IS FOR
-----------------------
It answers "should this frame be allowed to click the user's mouse?". Three inputs, all free
because they are already computed:

  * detector confidence — what MediaPipe itself thinks
  * hand-scale steadiness — a scale that jumps frame to frame means the model is guessing
  * landmark coherence — a hand whose finger lengths are wildly inconsistent between frames is
    a bad fit, whatever confidence it reports

The gate is asymmetric on purpose: low stability suppresses ACTIONS (clicks, scroll) but not
POINTER movement. A cursor that stutters during noisy tracking is mildly annoying; a click
fired during noisy tracking lands on whatever happens to be under the pointer, and that is the
"accidental clicks" report.
"""

import math
from collections import deque
from dataclasses import dataclass


# ── MediaPipe hand landmark indices ──
WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20

LANDMARK_COUNT = 21

# Below this the reference span is too small for its ratios to mean anything — the hand is
# either far away, edge-on, or a bad detection. Expressed in width-normalized units: 0.045 is
# a palm about 29px wide in a 640px frame, which is past the point where MediaPipe's fingertip
# error exceeds the distances being measured.
MIN_HAND_SCALE = 0.045


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class HandFeatures:
    """
    One frame of hand geometry, everything scale-invariant.

    `points` is width-normalized (x in 0..1, y scaled by the frame aspect), so consumers can
    take distances directly. `pointer` is the RAW normalized index tip, kept unscaled because
    the cursor mapping wants frame fractions, not width-normalized units.
    """

    points: tuple                   # 21 (x, y) width-normalized
    pointer: tuple                  # raw normalized (x, y) of the index tip
    hand_scale: float               # width-normalized palm span
    handedness: str                 # "Left" | "Right" | ""
    detector_confidence: float

    # Normalized separations — the click signals.
    index_pinch: float              # thumb tip <-> index tip, / hand_scale
    middle_pinch: float             # thumb tip <-> middle tip, / hand_scale

    # Finger extension, 0..1 each. Not booleans: a threshold on a continuous value is the
    # caller's business, and the state machine needs the margin to compute confidence.
    extension: tuple                # (index, middle, ring, pinky)

    spread: float                   # index tip <-> middle tip, / hand_scale
    stability: float                # 0..1
    valid: bool

    @property
    def index_up(self) -> bool:
        return self.extension[0] >= 0.55

    @property
    def middle_up(self) -> bool:
        return self.extension[1] >= 0.55

    @property
    def ring_up(self) -> bool:
        return self.extension[2] >= 0.55

    @property
    def pinky_up(self) -> bool:
        return self.extension[3] >= 0.55

    @property
    def extended_count(self) -> int:
        return sum(1 for value in self.extension if value >= 0.55)

    @property
    def two_finger_pose(self) -> bool:
        """Index and middle out, ring and pinky in. The scroll pose."""
        return self.index_up and self.middle_up and not self.ring_up and not self.pinky_up

    @property
    def thumb_pinched(self) -> bool:
        """
        Whether the thumb is meeting a fingertip rather than resting across the hand.

        This is what tells a FIST apart from the three-finger BEAK, and they need telling
        apart: both curl every finger, so `extended_count == 0` is true for each. In a closed
        fist the thumb lies across the middle phalanges and the fingertips are inside the palm,
        so the thumb-to-tip distance stays around 0.65 hand scales; in a beak the thumb is
        touching them, at around 0.18. The 0.55 threshold sits between the two with room on
        both sides, and it is deliberately looser than any click gate — this is a
        disambiguation, not an action.
        """
        return min(self.index_pinch, self.middle_pinch) < 0.55

    @property
    def fist(self) -> bool:
        return self.extended_count == 0 and not self.thumb_pinched

    @property
    def open_hand(self) -> bool:
        return self.extended_count >= 4

    @property
    def scroll_anchor(self) -> float:
        """
        The y the scroll velocity is measured from: the MIDPOINT of the two fingertips.

        A midpoint rather than the index tip alone, because the two fingers move together in a
        deliberate scroll and independently in noise — averaging them halves the noise the
        velocity estimator sees, for free.
        """
        return (self.points[INDEX_TIP][1] + self.points[MIDDLE_TIP][1]) * 0.5


def _extension(points, tip, pip, mcp, scale) -> float:
    """
    How extended one finger is, 0 (curled) to 1 (straight), scale- and rotation-invariant.

    Compares wrist-to-tip against wrist-to-PIP. For a curled finger the tip comes back towards
    the palm and the ratio drops below 1; for an extended one it is meaningfully above. The
    v1 engine used the same comparison as a BOOLEAN (`dist(wrist,tip) > dist(wrist,pip)`),
    which is a comparator on a noisy signal and chattered for exactly the poses where the
    finger is half-curled — the transition between MOVE and SCROLL, i.e. the one place the
    user notices. Keeping it continuous lets the caller apply hysteresis and lets the state
    machine report how sure it is.
    """
    wrist = points[WRIST]
    tip_d = _dist(wrist, points[tip])
    pip_d = _dist(wrist, points[pip])
    if pip_d <= 1e-6:
        return 0.0
    ratio = tip_d / pip_d
    # 0.86 is a firmly curled finger, 1.22 a firmly straight one; measured across the poses in
    # `tests/test_gesture_state.py`'s fixture set. The band is deliberately wide so the
    # midpoint of the ramp is not a place a resting hand sits.
    return max(0.0, min(1.0, (ratio - 0.86) / (1.22 - 0.86)))


class FeatureExtractor:
    """
    Landmarks in, `HandFeatures` out, plus the short history the stability score needs.

    Bounded by construction: `HISTORY` frames of two floats each. There is deliberately no
    landmark history beyond that — smoothing lives in `filters.py` and operates on the one
    channel that needs it, and a rolling buffer of 21 points would be a per-frame allocation
    in the hot path for a signal nothing reads.
    """

    HISTORY = 8

    def __init__(self):
        self._scales = deque(maxlen=self.HISTORY)
        self._spans = deque(maxlen=self.HISTORY)
        self._last_points = None

    def reset(self):
        self._scales.clear()
        self._spans.clear()
        self._last_points = None

    def extract(self, landmarks, frame_aspect: float, handedness: str = "",
                confidence: float = 0.0):
        """
        `landmarks` is any sequence of 21 objects with `.x` / `.y`, or of (x, y) pairs.

        Returns a `HandFeatures` with `valid=False` rather than raising when the input cannot
        support a measurement — a malformed frame must cost one skipped frame, never a
        traceback out of the processing thread.
        """
        points = _as_points(landmarks, frame_aspect)
        if points is None:
            self._last_points = None
            return _invalid(handedness, confidence)

        hand_scale = _dist(points[WRIST], points[MIDDLE_MCP])
        if hand_scale < MIN_HAND_SCALE:
            self._last_points = None
            return _invalid(handedness, confidence)

        thumb = points[THUMB_TIP]
        index_pinch = _dist(thumb, points[INDEX_TIP]) / hand_scale
        middle_pinch = _dist(thumb, points[MIDDLE_TIP]) / hand_scale
        spread = _dist(points[INDEX_TIP], points[MIDDLE_TIP]) / hand_scale

        extension = (
            _extension(points, INDEX_TIP, INDEX_PIP, INDEX_MCP, hand_scale),
            _extension(points, MIDDLE_TIP, MIDDLE_PIP, MIDDLE_MCP, hand_scale),
            _extension(points, RING_TIP, RING_PIP, RING_MCP, hand_scale),
            _extension(points, PINKY_TIP, PINKY_PIP, PINKY_MCP, hand_scale),
        )

        stability = self._stability(points, hand_scale, confidence)

        self._scales.append(hand_scale)
        self._spans.append(_dist(points[INDEX_MCP], points[PINKY_MCP]) / hand_scale)
        self._last_points = points

        raw_pointer = _raw_pointer(landmarks)

        return HandFeatures(
            points=points,
            pointer=raw_pointer,
            hand_scale=hand_scale,
            handedness=handedness or "",
            detector_confidence=float(confidence),
            index_pinch=index_pinch,
            middle_pinch=middle_pinch,
            extension=extension,
            spread=spread,
            stability=stability,
            valid=True,
        )

    # ──────────────────────────────────────────────────────────────────

    def _stability(self, points, hand_scale, confidence) -> float:
        """
        0..1. How much this frame's geometry should be trusted to fire an action.

        The three factors MULTIPLY rather than average, so any one of them being bad is
        disqualifying. An average would let a high detector confidence paper over landmarks
        that are visibly incoherent, and detector confidence is precisely the signal that
        stays high while the fit is wrong — that is why per-frame confidence alone was never
        enough to stop the accidental clicks.
        """
        detector = min(1.0, max(0.0, confidence if confidence > 0.0 else 0.75))

        # Scale steadiness. A palm span that changes by more than a few percent between frames
        # is a model that is guessing at depth, and every ratio derived from it is guessing too.
        if len(self._scales) >= 2:
            recent = list(self._scales)[-4:]
            mean = sum(recent) / len(recent)
            drift = abs(hand_scale - mean) / max(mean, 1e-6)
            scale_score = max(0.0, 1.0 - drift * 6.0)
        else:
            scale_score = 0.6      # not yet known; enough to track, not enough to click

        # Landmark coherence: how much the hand DEFORMED since the previous frame, with its
        # bulk translation removed.
        #
        # THE RESIDUAL, NOT THE DISPLACEMENT, AND THAT DISTINCTION IS THE WHOLE MEASUREMENT.
        # Total per-landmark movement conflates two completely different things: a hand that
        # MOVED (every landmark travels together — a perfectly good detection, and exactly what
        # happens when the user points at something) and a hand that was RE-FIT (landmarks
        # travel in different directions — the model guessing). Scoring on displacement would
        # suppress clicks during every deliberate movement, which would make dragging
        # impossible; scoring on the residual leaves rigid motion at full stability and catches
        # incoherence, which is the signal that actually predicts a bad click.
        if self._last_points is not None:
            deltas = [(a[0] - b[0], a[1] - b[1]) for a, b in zip(points, self._last_points)]
            mean_dx = sum(d[0] for d in deltas) / LANDMARK_COUNT
            mean_dy = sum(d[1] for d in deltas) / LANDMARK_COUNT
            residual = sum(math.hypot(dx - mean_dx, dy - mean_dy) for dx, dy in deltas)
            residual /= (LANDMARK_COUNT * max(hand_scale, 1e-6))
            coherence = max(0.0, 1.0 - residual * 8.0)
        else:
            coherence = 0.6

        return max(0.0, min(1.0, detector * scale_score * coherence))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               HELPERS                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _as_points(landmarks, frame_aspect: float):
    """
    Normalizes any of the accepted landmark shapes into 21 width-normalized (x, y) tuples.

    Accepts MediaPipe's own objects and plain (x, y) pairs so the state machine can be driven
    by synthetic fixtures with no MediaPipe import anywhere near the test suite. That is not a
    convenience: a gesture test that needs a camera is a test nobody runs.
    """
    if landmarks is None:
        return None
    try:
        if len(landmarks) != LANDMARK_COUNT:
            return None
    except TypeError:
        return None

    out = []
    for item in landmarks:
        try:
            if hasattr(item, "x"):
                x, y = float(item.x), float(item.y)
            else:
                x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        out.append((x, y * frame_aspect))
    return tuple(out)


def _raw_pointer(landmarks):
    item = landmarks[INDEX_TIP]
    if hasattr(item, "x"):
        return float(item.x), float(item.y)
    return float(item[0]), float(item[1])


def _invalid(handedness, confidence):
    zero = ((0.0, 0.0),) * LANDMARK_COUNT
    return HandFeatures(
        points=zero, pointer=(0.0, 0.0), hand_scale=0.0,
        handedness=handedness or "", detector_confidence=float(confidence),
        index_pinch=99.0, middle_pinch=99.0, extension=(0.0, 0.0, 0.0, 0.0),
        spread=0.0, stability=0.0, valid=False,
    )


def pick_primary(candidates):
    """
    Chooses ONE hand to control the pointer when the detector reports more than one.

    DETERMINISTIC, and that is the requirement. Two hands in frame used to mean the v1 loop
    ran its `for hand_lm in results.multi_hand_landmarks` body for each of them in turn — so
    both hands drove the cursor within the same frame and both could fire clicks, which reads
    to the user as the pointer fighting itself.

    The rule is: the largest, most stable hand wins, and once chosen it is preferred while it
    stays reasonable (`sticky_score`). Size is the primary key because the controlling hand is
    the one held up towards the camera, and a bystander's hand at the back of the room is
    smaller by a wide margin.

    `candidates` is a sequence of `HandFeatures`. Returns one, or None.
    """
    usable = [c for c in candidates if c is not None and c.valid]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    return max(usable, key=lambda c: (round(c.hand_scale, 3), round(c.stability, 2),
                                      c.detector_confidence))


def sticky_score(candidate, previous_handedness: str) -> float:
    """
    The bonus a hand gets for having been the controlling hand on the previous frame.

    Small but decisive at a tie, which is exactly where the switching happened: two hands of
    similar size make `pick_primary` alternate between them frame to frame, and the cursor
    ping-pongs. Requiring a clear margin to take control means a deliberate swap still works
    (raise one hand higher) while noise does not cause one.
    """
    if not previous_handedness or candidate is None or not candidate.valid:
        return 0.0
    return 0.12 if candidate.handedness == previous_handedness else 0.0
