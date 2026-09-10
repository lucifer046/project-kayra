# ┌────────────────────────────────────────────────────────────────────────┐
# │                     gesture/state_machine.py                           │
# │       The Temporal Gesture FSM And The Action Arbitration Rule         │
# └────────────────────────────────────────────────────────────────────────┘
"""
Frames are not classified independently. This is the module that makes that true.

THE DEFECT THIS REPLACES
------------------------
The v1 engine's `classify()` was a per-frame function with a few counters bolted on. Its mode
was recomputed from scratch every frame, its scroll pose was tested twice with two different
definitions inside the same call, and its click counters were reset by whichever branch ran
last. The consequences were all reported: gesture state flickering, clicks that fired while
scrolling, scroll that reversed on one noisy frame, and gestures that "sometimes stop working"
— the last being a counter left latched by a branch that never ran again.

Here there is ONE state, it is a member, transitions are explicit, and every actionable
gesture goes through the same three-part gate:

    normalized geometry  ->  Hysteresis (enter / exit + dwell)  ->  arbitration  ->  action

ARBITRATION: ONE ACTION PER FRAME, BY A FIXED PRIORITY
------------------------------------------------------
    SCROLL  >  DOUBLE CLICK  >  RIGHT CLICK  >  LEFT CLICK  >  CURSOR

Fixed, not situational, so the behaviour is predictable — the single most valuable property a
gesture system can have. The ordering is by cost of the mistake: firing a click during a
scroll drops the user somewhere they did not intend, while suppressing a click during a scroll
costs them one repeat. Scroll therefore wins, and while it is active the click gates are held
open (reset every frame) rather than merely ignored, so the pinch that inevitably occurs while
two fingers move together cannot accumulate a dwell and fire the instant scrolling ends.

CURSOR IS THE EXCEPTION, AND DELIBERATELY SO
--------------------------------------------
Pointer movement is not arbitrated away by a click — it continues while a pinch is held, which
is what makes drag work. It IS suppressed during scroll, because two fingers moving vertically
to scroll would otherwise also drag the pointer up the screen.

STABILITY GATES ACTIONS, NOT TRACKING
-------------------------------------
`min_stability` and `min_confidence` block clicks and scroll. They never block cursor movement.
A stuttering pointer during noisy tracking is a visible annoyance; a click fired during noisy
tracking lands on whatever is under the pointer, and that is the accidental-click report.
"""

from dataclasses import dataclass

from kayra.input.gesture.filters import Hysteresis, RateLimiter


class GestureState:
    """The states this machine can be in. Exhaustive and mutually exclusive."""

    NO_HAND = "NO_HAND"
    ACQUIRING = "ACQUIRING"            # a hand is present but not yet trusted
    TRACKING = "TRACKING"              # a hand is present, no actionable pose
    CURSOR = "CURSOR"
    LEFT_CLICK_CANDIDATE = "LEFT_CLICK_CANDIDATE"
    LEFT_CLICK_HELD = "LEFT_CLICK_HELD"
    RIGHT_CLICK_CANDIDATE = "RIGHT_CLICK_CANDIDATE"
    RIGHT_CLICK_HELD = "RIGHT_CLICK_HELD"
    DOUBLE_CLICK_HELD = "DOUBLE_CLICK_HELD"
    SCROLL_CANDIDATE = "SCROLL_CANDIDATE"
    SCROLL_ACTIVE = "SCROLL_ACTIVE"
    # THE PAUSE IS A THREE-STATE SEQUENCE, NOT A BOOLEAN. See `_pause` below for the bug that
    # made it one: a per-frame comparator on finger extension produced 206 ACTIVE->PAUSED
    # round trips in 600 frames of an ordinary cursor session.
    PAUSE_CANDIDATE = "PAUSE_CANDIDATE"   # a fist is forming; nothing has changed yet
    PAUSED = "PAUSED"                     # a fist was HELD: the user asked for nothing to happen
    RESUME_CANDIDATE = "RESUME_CANDIDATE"  # the fist is opening; still paused


ALL_STATES = (
    GestureState.NO_HAND, GestureState.ACQUIRING, GestureState.TRACKING, GestureState.CURSOR,
    GestureState.LEFT_CLICK_CANDIDATE, GestureState.LEFT_CLICK_HELD,
    GestureState.RIGHT_CLICK_CANDIDATE, GestureState.RIGHT_CLICK_HELD,
    GestureState.DOUBLE_CLICK_HELD,
    GestureState.SCROLL_CANDIDATE, GestureState.SCROLL_ACTIVE,
    GestureState.PAUSE_CANDIDATE, GestureState.PAUSED, GestureState.RESUME_CANDIDATE,
)

# The only states that mean "the user deliberately stopped gesture control". NO_HAND is NOT
# one of them and never becomes one: no hand in frame means gesture control is running and
# waiting, and reporting that as a pause tells the user they did something they did not do.
PAUSED_STATES = frozenset({GestureState.PAUSED, GestureState.RESUME_CANDIDATE})

# What the status card shows. Short, and about what the user is DOING, not about which internal
# state the machine is in — "Left click" reads; "LEFT_CLICK_HELD" does not.
STATE_LABELS = {
    GestureState.NO_HAND: "No hand",
    GestureState.ACQUIRING: "Acquiring",
    GestureState.TRACKING: "Tracking",
    GestureState.CURSOR: "Cursor",
    GestureState.LEFT_CLICK_CANDIDATE: "Cursor",
    GestureState.LEFT_CLICK_HELD: "Left click",
    GestureState.RIGHT_CLICK_CANDIDATE: "Cursor",
    GestureState.RIGHT_CLICK_HELD: "Right click",
    GestureState.DOUBLE_CLICK_HELD: "Double click",
    GestureState.SCROLL_CANDIDATE: "Tracking",
    GestureState.SCROLL_ACTIVE: "Scroll",
    # A pose that has not yet been held long enough is NOT reported as a pause. Announcing a
    # candidate would put the flap back on the screen while removing it from the state.
    GestureState.PAUSE_CANDIDATE: "Cursor",
    GestureState.PAUSED: "Paused",
    GestureState.RESUME_CANDIDATE: "Paused",
}

# States in which the pointer follows the hand.
POINTER_STATES = frozenset({
    GestureState.CURSOR,
    GestureState.LEFT_CLICK_CANDIDATE, GestureState.LEFT_CLICK_HELD,
    GestureState.RIGHT_CLICK_CANDIDATE, GestureState.RIGHT_CLICK_HELD,
    GestureState.DOUBLE_CLICK_HELD,
})


@dataclass
class GestureDecision:
    """
    Everything one frame decided. The controller executes this and nothing else.

    A value object rather than the controller reading state off the machine, so the execution
    path has exactly one input and can be replayed in a test with no camera, no MediaPipe and
    no mouse.
    """

    state: str = GestureState.NO_HAND
    label: str = "No hand"
    track_pointer: bool = False
    pointer: tuple = None               # raw normalized (x, y), or None
    hand_scale: float = 0.0

    fire_left: bool = False
    fire_right: bool = False
    fire_double: bool = False
    scroll_delta: int = 0

    stability: float = 0.0
    cursor_confidence: float = 0.0
    left_click_confidence: float = 0.0
    right_click_confidence: float = 0.0
    scroll_confidence: float = 0.0

    suppressed: str = ""                # why an otherwise-ready action did not fire
    changed: bool = False               # the state differs from the previous frame

    @property
    def acted(self) -> bool:
        return bool(self.fire_left or self.fire_right or self.fire_double or self.scroll_delta)


class GestureRecognizer:
    """
    The temporal state machine. One instance per gesture runtime.

    Bounded: fixed-size gates, three floats of scroll history, no collections that grow. It
    allocates one `GestureDecision` per frame and nothing else, which is what keeps a 30Hz
    loop free of GC pressure.
    """

    def __init__(self, config):
        self.config = config
        cfg = config

        hold = cfg.pinch_hold_ms / 1000.0
        self._left = Hysteresis(cfg.pinch_enter, cfg.pinch_exit, hold)
        self._right = Hysteresis(cfg.pinch_enter, cfg.pinch_exit, hold)
        # The three-finger gate reads the LARGER of the two pinch separations, so it engages
        # only when index AND middle are both closed on the thumb. Feeding it the smaller one
        # would let a plain index pinch satisfy it and turn every left click into a double.
        self._double = Hysteresis(cfg.double_enter, cfg.double_exit, hold * 1.35)
        # Scroll POSE (two fingers out) and scroll MOTION are separate gates, because the pose
        # is what suppresses clicks and the motion is what actually scrolls. Conflating them is
        # why v1 could scroll from a stationary hand and click from a moving one.
        self._scroll_pose = Hysteresis(0.55, 0.40, cfg.scroll_pose_ms / 1000.0, invert=True)
        self._scroll_up = Hysteresis(cfg.scroll_enter, cfg.scroll_exit, 0.0, invert=True)
        self._scroll_down = Hysteresis(cfg.scroll_enter, cfg.scroll_exit, 0.0, invert=True)

        # THE PAUSE GATE. Inverted, because larger means more closed. `enter` is far outside
        # any resting pose and the dwell makes the pose deliberate — see `_pause`.
        self._fist = Hysteresis(cfg.pause_enter, cfg.pause_exit,
                                cfg.pause_hold_ms / 1000.0, invert=True)

        self._click_gap = RateLimiter(cfg.click_cooldown_ms / 1000.0)
        self._scroll_gap = RateLimiter(cfg.scroll_interval_ms / 1000.0)

        self.state = GestureState.NO_HAND
        self._state_since = 0.0
        self._last_seen = None
        self._scroll_prev_y = None
        self._scroll_prev_t = None
        self._scroll_direction = 0          # -1 up, +1 down, 0 neutral
        self._primary_handedness = ""
        # When the hand stopped looking like a fist, for the resume dwell. None means it
        # still does.
        self._resume_since = None

    # ──────────────────────────────────────────────────────────────────
    #                             LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def reset(self, now: float = 0.0):
        """
        Drops every latched gate. Called when the hand is genuinely lost and at start/stop.

        EVERY gate is reset here, deliberately and exhaustively. The v1 bug where "gestures
        sometimes stop working" was a counter left latched by a branch that stopped running;
        a reset that misses one gate reintroduces exactly that, so this touches all of them.
        """
        for gate in (self._left, self._right, self._double, self._scroll_pose,
                     self._scroll_up, self._scroll_down, self._fist):
            gate.reset()
        self._scroll_prev_y = None
        self._scroll_prev_t = None
        self._scroll_direction = 0
        self._resume_since = None
        self._set_state(GestureState.NO_HAND, now)

    def _set_state(self, state, now):
        if state != self.state:
            self.state = state
            self._state_since = now
            return True
        return False

    # ──────────────────────────────────────────────────────────────────
    #                            THE ONE CALL
    # ──────────────────────────────────────────────────────────────────

    def update(self, features, now: float, grace_active: bool = False) -> GestureDecision:
        """
        One frame in, one decision out.

        `features` is a `HandFeatures` or None. `grace_active` says the hand is missing but
        within the grace period — the machine then HOLDS its state rather than resetting, which
        is what stops a one-frame detection dropout from cancelling a drag or ending a scroll.
        """
        if features is None or not features.valid:
            return self._no_hand(now, grace_active)

        cfg = self.config
        decision = GestureDecision(hand_scale=features.hand_scale,
                                   stability=features.stability,
                                   pointer=features.pointer)

        trusted = (features.stability >= cfg.min_stability
                   and features.detector_confidence >= cfg.min_confidence)

        # ── The pause gesture outranks everything, and it is evaluated first ──
        paused = self._pause(features, decision, now, trusted)
        if paused is not None:
            return paused

        # ── Gate evaluation. Every gate is updated EVERY frame ──
        # Updating unconditionally is what keeps the gates honest: a gate that is only fed
        # while its branch is selected keeps a stale dwell from the last time it ran, which is
        # the latched-counter bug. Arbitration then chooses among their answers; it never
        # decides which of them to compute.
        pose_signal = min(features.extension[0], features.extension[1]) * (
            1.0 if not features.ring_up else 0.0)
        pose_engaged, _ = self._scroll_pose.update(pose_signal, now)

        # ── ARBITRATION, STEP 1: scroll outranks every click ──
        # The click gates are RESET, not merely ignored, while the scroll pose holds. Two
        # fingers travelling together inevitably bring the thumb near a fingertip, and a gate
        # that were only ignored would accumulate its dwell throughout the scroll and fire the
        # instant the pose ended — a click the user never made, at the end of every scroll.
        if pose_engaged:
            self._left.reset()
            self._right.reset()
            self._double.reset()
            return self._scroll(features, decision, now, trusted)

        wider_pinch = max(features.index_pinch, features.middle_pinch)
        double_engaged, double_rising = self._double.update(wider_pinch, now)

        # Index and middle pinches are mutually exclusive by construction: whichever is
        # CLEARLY closer wins and the other is fed a value that cannot engage. Without this a
        # thumb resting between both fingertips satisfies both gates and the machine fires a
        # left and a right click on the same frame.
        left_value, right_value = self._separate_pinches(features)
        left_engaged, left_rising = self._left.update(left_value, now)
        right_engaged, right_rising = self._right.update(right_value, now)

        # Not scrolling: the scroll motion history is stale and must not survive to the next
        # scroll, or the first frame of it computes a velocity across the whole gap.
        self._scroll_prev_y = None
        self._scroll_prev_t = None
        self._scroll_direction = 0
        self._scroll_up.reset()
        self._scroll_down.reset()

        decision.cursor_confidence = min(1.0, features.extension[0] * features.stability + 0.15)
        decision.left_click_confidence = self._confidence(self._left, left_value, now, features)
        decision.right_click_confidence = self._confidence(self._right, right_value, now,
                                                           features)

        if double_engaged:
            if double_rising and trusted and self._click_gap.take(now):
                decision.fire_double = True
            elif double_rising and not trusted:
                decision.suppressed = "unstable"
            # A three-finger pinch must not also arm the single-click gates; releasing them
            # here is what makes the double click ONE action rather than a double plus a left.
            self._left.reset()
            self._right.reset()
            decision.changed = self._set_state(GestureState.DOUBLE_CLICK_HELD, now)
            decision.track_pointer = self._drag_unlocked(now)

        elif right_engaged:
            if right_rising and trusted and self._click_gap.take(now):
                decision.fire_right = True
            elif right_rising and not trusted:
                decision.suppressed = "unstable"
            decision.changed = self._set_state(GestureState.RIGHT_CLICK_HELD, now)
            decision.track_pointer = self._drag_unlocked(now)

        elif left_engaged:
            if left_rising and trusted and self._click_gap.take(now):
                decision.fire_left = True
            elif left_rising and not trusted:
                decision.suppressed = "unstable"
            decision.changed = self._set_state(GestureState.LEFT_CLICK_HELD, now)
            decision.track_pointer = self._drag_unlocked(now)

        elif self._left.candidate or self._right.candidate or self._double.candidate:
            # A pinch is closing but has not satisfied its dwell. THE POINTER IS FROZEN HERE,
            # and this is the single change that makes clicks land where the user aimed.
            #
            # Pinching curls the index finger towards the thumb, and the index tip is the
            # landmark driving the cursor — so the act of clicking physically drags the pointer
            # by roughly half a hand-width, several hundred pixels on screen, in the ~80ms
            # before the click fires. v1 tracked throughout and the click landed wherever that
            # slide ended, which is a large part of the "inaccurate" and "unexpected" reports.
            # Freezing costs nothing: a user who is closing their fingers to click has already
            # finished aiming.
            state = (GestureState.RIGHT_CLICK_CANDIDATE if self._right.candidate
                     else GestureState.LEFT_CLICK_CANDIDATE)
            decision.changed = self._set_state(state, now)
            decision.track_pointer = False

        elif features.index_up:
            decision.changed = self._set_state(GestureState.CURSOR, now)
            decision.track_pointer = True

        else:
            decision.changed = self._set_state(
                GestureState.TRACKING if trusted else GestureState.ACQUIRING, now)

        decision.state = self.state
        decision.label = STATE_LABELS.get(self.state, self.state)
        self._last_seen = now
        self._primary_handedness = features.handedness or self._primary_handedness
        return decision

    # ──────────────────────────────────────────────────────────────────
    #                              BRANCHES
    # ──────────────────────────────────────────────────────────────────

    def _pause(self, features, decision, now, trusted):
        """
        The pause gesture. Returns a decision when it owns the frame, or None.

        THE BUG THIS REPLACES, AND ITS MEASURED SIZE
        --------------------------------------------
        v2.0 tested `features.fist` — `extended_count == 0`, i.e. four independent
        `extension >= 0.55` comparators — once per frame and paused immediately. Every other
        gesture in this file had hysteresis and a dwell; this one had a bare comparator, and the
        comment claimed it "needs no dwell reasoning of its own". That was wrong.

        A real pointing finger is not perfectly straight. Its measured extension sits close to
        0.55, and landmark noise carries it back and forth across that comparator several times
        a second. Measured on the reproduction in `tests/test_gesture_state.py`, with an index
        finger whose mean measured extension is 0.59: **206 ACTIVE -> PAUSED -> ACTIVE round
        trips in 600 frames**, about 8.6 per second, each one a full runtime transition and two
        INFO log lines. At a slightly more relaxed finger the system was PAUSED for 78% of the
        session.

        THE FIX, IN THREE PARTS
        -----------------------
        1. **A continuous signal.** `1 - max(extension)`. A fist requires EVERY finger curled,
           so the MOST EXTENDED finger governs; and a continuous value is something a gate can
           act on, where a count of booleans is not.
        2. **Hysteresis with a real dwell.** Enter at 0.82 (the straightest finger below 0.18
           extended — nowhere near any resting pose), leave at 0.55, and hold for
           `GESTURE_PAUSE_HOLD_MS` (500ms). Both halves are needed: the threshold moves the
           decision away from where noise lives, and the dwell makes it deliberate.
        3. **Resume needs its own sustained evidence.** Opening the hand for one noisy frame
           does not resume control the user deliberately stopped; `GESTURE_RESUME_HOLD_MS`
           (300ms) of consistently-not-a-fist does.

        Entering the pause also requires a TRUSTED frame, exactly as a click does. A pause
        fired from noisy tracking is the same class of mistake as a click fired from it.
        """
        if not self.config.pause_enabled:
            return None

        # `thumb_pinched` separates a FIST from the three-finger BEAK: both curl every finger,
        # and only the thumb's position tells them apart. A beak must reach the double-click
        # gate, not the pause.
        fistness = 0.0 if features.thumb_pinched else (1.0 - max(features.extension))
        engaged, rising = self._fist.update(fistness, now)

        if engaged:
            if rising and not trusted:
                # The pose held, but the frame is not trustworthy enough to act on. Drop the
                # gate rather than pausing on noise; the user can simply keep holding.
                self._fist.reset()
                decision.suppressed = "unstable"
            else:
                self._resume_since = None
                if self.state not in PAUSED_STATES:
                    self._release_all()
                decision.changed = self._set_state(GestureState.PAUSED, now)
                decision.state = self.state
                decision.label = STATE_LABELS[self.state]
                return decision

        # Not fully a fist this frame. If we are PAUSED, that starts (or continues) the resume
        # dwell and NOTHING else happens — a paused assistant must not click on the way out.
        if self.state in PAUSED_STATES:
            # THE RESUME DWELL IS CONTINUOUS, NOT A WALL-CLOCK TIMER. It measures how long the
            # hand has been genuinely open, and it restarts the moment the hand closes again.
            # Without this, a hand that opens for three frames and re-closes still resumes
            # 300ms later — the timer having kept running under a hand that was back in a
            # fist. Caught by the "three frames of an open hand" check below.
            if fistness > self.config.pause_exit:
                self._resume_since = None
                decision.changed = self._set_state(GestureState.PAUSED, now)
                decision.state = self.state
                decision.label = STATE_LABELS[self.state]
                return decision
            if self._resume_since is None:
                self._resume_since = now
            if (now - self._resume_since) * 1000.0 < self.config.resume_hold_ms:
                decision.changed = self._set_state(GestureState.RESUME_CANDIDATE, now)
                decision.state = self.state
                decision.label = STATE_LABELS[self.state]
                return decision
            self._resume_since = None
            self._fist.reset()
            # Fall through: control resumes on this frame, and the ordinary branches below
            # decide what the hand is now doing.
            return None

        # Not paused, and a fist is forming but has not been held long enough.
        #
        # The pointer KEEPS TRACKING and the state is reported as PAUSE_CANDIDATE, whose label
        # is still "Cursor": announcing a candidate as a pause would put the flap back on the
        # screen while removing it from the state. Discrete actions are held, which costs
        # nothing real — a hand closing towards a fist has the thumb away from the fingertips,
        # so `thumb_pinched` is false and no click gate could engage anyway. That guard is
        # also what keeps a pinch made with an otherwise-closed hand out of this branch
        # entirely.
        if self._fist.candidate:
            decision.changed = self._set_state(GestureState.PAUSE_CANDIDATE, now)
            decision.track_pointer = True
            decision.state = self.state
            decision.label = STATE_LABELS[self.state]
            return decision
        return None

    def _no_hand(self, now, grace_active) -> GestureDecision:
        """
        The hand is not in this frame.

        Within the grace period the state is HELD and no action is produced. That asymmetry is
        the point: holding the state means a one-frame dropout does not cancel a drag or reset
        a scroll anchor, while producing no action means a dropout can never itself cause a
        click. Past the grace period everything is dropped and the next hand is a fresh
        acquisition — which is what stops a stale pinch gate from firing the instant a hand
        reappears somewhere else.
        """
        if grace_active and self.state != GestureState.NO_HAND:
            return GestureDecision(state=self.state,
                                   label=STATE_LABELS.get(self.state, self.state),
                                   track_pointer=False, suppressed="hand-lost-grace")
        changed = self._set_state(GestureState.NO_HAND, now)
        self._release_all()
        return GestureDecision(state=GestureState.NO_HAND,
                               label=STATE_LABELS[GestureState.NO_HAND],
                               changed=changed)

    def _scroll(self, features, decision, now, trusted) -> GestureDecision:
        """
        Two fingers out: measure their vertical velocity and turn it into wheel impulses.

        VELOCITY, NOT DISPLACEMENT FROM AN ANCHOR. The v1 engine locked an anchor and scrolled
        continuously in proportion to the offset from it — a joystick. That has two problems in
        practice: the anchor is set on whichever frame the pose was first recognised, so a pose
        recognised mid-movement anchors in the wrong place; and it keeps scrolling forever
        while the hand is held still away from the anchor, which is why scrolling "sometimes
        would not stop". Velocity means a still hand scrolls by nothing, which is what a user
        who has stopped moving expects.

        DIRECTION HYSTERESIS. Two separate gates, one per direction, with a NEUTRAL band
        between the enter and exit thresholds. A direction, once engaged, holds until the
        velocity falls back through the neutral threshold — so one noisy frame in the opposite
        sense cannot reverse the scroll. That is the up/down/up/down chatter, fixed in the only
        place it can be fixed.
        """
        cfg = self.config
        y = features.scroll_anchor
        scale = max(features.hand_scale, 1e-6)

        if self._scroll_prev_y is None or self._scroll_prev_t is None:
            self._scroll_prev_y, self._scroll_prev_t = y, now
            decision.changed = self._set_state(GestureState.SCROLL_CANDIDATE, now)
            decision.state = self.state
            decision.label = STATE_LABELS[self.state]
            return decision

        dt = now - self._scroll_prev_t
        if dt <= 0.0:
            decision.state = self.state
            decision.label = STATE_LABELS[self.state]
            return decision

        # Hand scales per second. Positive = hand moved DOWN the frame (y grows downward).
        velocity = ((y - self._scroll_prev_y) / scale) / dt
        self._scroll_prev_y, self._scroll_prev_t = y, now

        up_engaged, _ = self._scroll_up.update(-velocity, now)
        down_engaged, _ = self._scroll_down.update(velocity, now)

        if up_engaged and not down_engaged:
            self._scroll_direction = -1
        elif down_engaged and not up_engaged:
            self._scroll_direction = 1
        elif not up_engaged and not down_engaged:
            self._scroll_direction = 0

        decision.scroll_confidence = min(1.0, abs(velocity) / max(cfg.scroll_enter, 1e-6)
                                         * features.stability)

        if self._scroll_direction == 0:
            decision.changed = self._set_state(GestureState.SCROLL_CANDIDATE, now)
        else:
            decision.changed = self._set_state(GestureState.SCROLL_ACTIVE, now)
            if not trusted:
                decision.suppressed = "unstable"
            elif self._scroll_gap.take(now):
                # Wheel notch sign: positive rotates the wheel forward, which scrolls the
                # content UP. Hand moving up the frame therefore produces a positive impulse.
                magnitude = min(abs(velocity) * cfg.scroll_gain, cfg.max_scroll_impulse)
                decision.scroll_delta = int(round(-self._scroll_direction * magnitude))

        # The pointer never moves during scroll. Two fingers travelling vertically would
        # otherwise drag the cursor up the screen while the page scrolled under it.
        decision.track_pointer = False
        decision.state = self.state
        decision.label = STATE_LABELS[self.state]
        self._last_seen = now
        return decision

    # ──────────────────────────────────────────────────────────────────
    #                              HELPERS
    # ──────────────────────────────────────────────────────────────────

    def _drag_unlocked(self, now) -> bool:
        """
        Whether a HELD pinch has lasted long enough to be a drag rather than a click.

        The pointer is frozen for `drag_unlock_ms` after a pinch engages, then follows again.
        That split is what lets one gesture serve both: a click is a brief pinch and never
        moves the pointer, a drag is a sustained one and does.
        """
        return (now - self._state_since) * 1000.0 >= self.config.drag_unlock_ms

    def _separate_pinches(self, features):
        """
        Forces index-pinch and middle-pinch to be mutually exclusive before either is gated.

        `MARGIN` is how much closer one must be to claim the frame. Below it neither engages,
        which is correct: a thumb equidistant from both fingertips is a pose whose intent is
        genuinely unclear, and the right answer to an unclear intent is to do nothing.
        """
        MARGIN = 0.06
        left, right = features.index_pinch, features.middle_pinch
        if left < right - MARGIN:
            return left, 99.0
        if right < left - MARGIN:
            return 99.0, right
        return 99.0, 99.0

    def _confidence(self, gate, value, now, features) -> float:
        """
        How sure the machine is about one click gate, 0..1.

        Three multiplied terms: how far INSIDE the threshold the pose is, how far through the
        dwell it has come, and how trustworthy the frame is. A pose that is barely inside the
        threshold on a noisy frame reports low confidence even while engaged, which is what the
        diagnostics view shows and what a future tightening pass would tune against.
        """
        if value > 90.0:
            return 0.0
        depth = max(0.0, min(1.0, (self.config.pinch_exit - value)
                             / max(self.config.pinch_exit - self.config.pinch_enter, 1e-6)))
        return round(depth * max(gate.progress(now), 0.1) * features.stability, 4)

    def _release_all(self):
        # The pause gate is deliberately NOT reset here. `_release_all` runs when a pose is
        # taken over by another branch and on hand loss, and clearing the fist gate there
        # would mean a hand that flickers out for one frame mid-pause resumes control.
        for gate in (self._left, self._right, self._double, self._scroll_pose,
                     self._scroll_up, self._scroll_down):
            gate.reset()
        self._scroll_prev_y = None
        self._scroll_prev_t = None
        self._scroll_direction = 0
