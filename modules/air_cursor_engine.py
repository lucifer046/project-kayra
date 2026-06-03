# ┌────────────────────────────────────────────────────────────────────────────────────────┐
# │                          air_cursor_engine.py                                              │
# │   KAYRA AIR CURSOR Kernel LEVEL Gesture Engine v4  (1-Euro - FSM - Swipe - D-Click)    │
# └────────────────────────────────────────────────────────────────────────────────────────┘
"""
Complete hand-gesture mouse replacement.

Gestures
─────────────────────────────────────────────────────────────────────────
  ☝  Index only up            → MOVE cursor
  👌  Thumb+Index pinch        → Left Click
  🤏  Thumb+Middle pinch       → Right Click
  🤌  Thumb+Index+Middle pinch → Double Click (3-finger "Bird Beak" pinch)
  ✌  Index+Middle up          → SCROLL  (move hand up/down)

Engineering
─────────────────────────────────────────────────────────────────────────
  - 1-Euro Filter (tuned min_cutoff=2.0, beta=0.05) - zero wobble at rest,
    full responsiveness on fast movements.
  - Hysteresis pinch thresholds (enter 20%, exit 30% of palm size).
  - Double-click via 3-finger pinch (Thumb+Index+Middle bird beak pinch).
  - Scroll: continuous proportional scrolling based on vertical offset from anchor.
  - Crash guard: try/except around entire frame body.
  - OSD HUD: mode, FPS, gesture hint overlaid on preview.
"""

import math
import time
import ctypes
import collections
import cv2
import mediapipe as mp

try:
    from .utils import print_info, print_warning, print_error, print_success, print_banner
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_success, print_banner
    except ImportError:
        from utils import print_info, print_warning, print_error, print_success, print_banner


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   1-EURO FILTER - CASIEZ ET AL. 2012                   │
# └────────────────────────────────────────────────────────────────────────┘
def _sf(t_e: float, cutoff: float) -> float:
    r = 2.0 * math.pi * cutoff * t_e
    return r / (r + 1.0)

def _es(a: float, x: float, xp: float) -> float:
    return a * x + (1.0 - a) * xp

class OneEuroFilter:
    """
    Adaptive low-pass filter. At rest: high smoothing (min_cutoff).
    During fast motion: cutoff rises with velocity so no lag is added.

    Best values for hand cursor tracking (empirically tuned):
        min_cutoff = 2.0   — aggressive rest-smoothing → no wobble
        beta       = 0.05  — moderate velocity adaptation → smooth fast swipes
    """
    def __init__(self, min_cutoff: float = 2.0, beta: float = 0.05,
                 d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta       = beta
        self.d_cutoff   = d_cutoff
        self.x_prev = self.dx_prev = self.t_prev = None

    def __call__(self, t: float, x: float) -> float:
        if self.t_prev is None:
            self.x_prev, self.dx_prev, self.t_prev = float(x), 0.0, float(t)
            return float(x)
        t_e = t - self.t_prev
        if t_e <= 0.0:
            return self.x_prev
        a_d    = _sf(t_e, self.d_cutoff)
        dx     = (x - self.x_prev) / t_e
        dx_hat = _es(a_d, dx, self.dx_prev)
        a      = _sf(t_e, self.min_cutoff + self.beta * abs(dx_hat))
        x_hat  = _es(a, x, self.x_prev)
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat

    def reset(self):
        self.x_prev = self.dx_prev = self.t_prev = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     WINDOWS KERNEL INPUT INJECTION                     │
# └────────────────────────────────────────────────────────────────────────┘
_U32 = ctypes.windll.user32

MOUSEEVENTF_MOVE      = 0x0001
MOUSEEVENTF_LEFTDOWN  = 0x0002
MOUSEEVENTF_LEFTUP    = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP   = 0x0010
MOUSEEVENTF_WHEEL     = 0x0800

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP   = 0x0002

def move_cursor(x: float, y: float):
    _U32.SetCursorPos(int(x), int(y))


def click_kernel(button: str = "left"):
    if button == "left":
        _U32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.01)
        _U32.mouse_event(MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)
    elif button == "right":
        _U32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.01)
        _U32.mouse_event(MOUSEEVENTF_RIGHTUP,   0, 0, 0, 0)


def double_click_kernel():
    click_kernel("left")
    time.sleep(0.05)
    click_kernel("left")


def scroll_kernel(delta: int):
    """
    Sends scroll events via user32 mouse_event.
    
    Scroll Direction Mapping:
      - Positive delta (> 0): Rotates wheel forward/away from the user (Scrolls UP).
      - Negative delta (< 0): Rotates wheel backward/towards the user (Scrolls DOWN).
      
    Increments:
      - The magnitude of 'delta' dictates the scrolling speed/increment.
    """
    # Let ctypes handle signed 32-bit integer casting natively
    _U32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)


def get_screen_size() -> tuple:
    return _U32.GetSystemMetrics(0), _U32.GetSystemMetrics(1)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            GEOMETRY HELPERS                            │
# └────────────────────────────────────────────────────────────────────────┘
def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])

def finger_up(wrist, tip, pip) -> bool:
    """
    True if finger is extended. Scale and rotation invariant.
    Compares the distance from the wrist to the fingertip against the PIP joint.
    """
    return dist(wrist, tip) > dist(wrist, pip)

def map_to_screen(val, in_min, in_max, out_max):
    val = max(in_min, min(in_max, val))
    return (val - in_min) / (in_max - in_min) * out_max


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                OSD HUD                                 │
# └────────────────────────────────────────────────────────────────────────┘
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_MODE_COLORS = {
    "MOVE"      : (50,  230, 120),
    "L-CLICK"   : (0,   200, 255),
    "DBLCLICK"  : (0,   120, 255),
    "R-CLICK"   : (30,  80,  255),
    "SCROLL"    : (255, 190, 0  ),
    "IDLE"      : (100, 100, 100),
}
_HINTS = {
    "MOVE"      : "MOVE",
    "L-CLICK"   : "LEFT CLICK",
    "DBLCLICK"  : "DOUBLE CLICK",
    "R-CLICK"   : "RIGHT CLICK",
    "SCROLL"    : "SCROLL (up/down)",
    "IDLE"      : "---",
}

def draw_hud(frame, mode: str, fps: float):
    h, w = frame.shape[:2]
    col  = _MODE_COLORS.get(mode, (180, 180, 180))
    bar  = frame.copy()
    cv2.rectangle(bar, (0, 0), (w, 40), (15, 15, 15), -1)
    cv2.addWeighted(bar, 0.6, frame, 0.4, 0, frame)
    cv2.putText(frame, _HINTS.get(mode, mode), (10, 27), _FONT, 0.62, col, 2, cv2.LINE_AA)
    cv2.putText(frame, f"{fps:.0f} FPS", (w - 90, 27), _FONT, 0.55, (180, 180, 180), 1, cv2.LINE_AA)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         GESTURE STATE MACHINE                          │
# └────────────────────────────────────────────────────────────────────────┘
class GestureStateMachine:
    PINCH_ENTER = 0.36        # pinch confirmed below this ratio (naturally handles finger pad thickness)
    PINCH_EXIT  = 0.44        # pinch released above this ratio
    
    DBL_ENTER   = 0.48        # 3-finger pinch is slightly wider to allow clustered fingers
    DBL_EXIT    = 0.54

    # Require this many consecutive frames inside threshold before firing.
    # PRIMARY fix for phantom clicks from single-frame noise.
    PINCH_CONFIRM_FRAMES = 3

    # Minimum gap between any two clicks (stops rapid-fire spam)
    CLICK_COOLDOWN  = 0.45    # seconds

    # Scroll — INVERTED (continuous joystick velocity)
    SCROLL_DEADBAND    = 20   # px from anchor to start continuous scrolling
    SCROLL_SENSITIVITY = 5.0  # multiplier for continuous scroll speed

    def __init__(self):
        # Frame-count pinch confirmers
        self._lc_cnt    = 0
        self._rc_cnt    = 0
        self._dc_cnt    = 0
        self._lc_active = False
        self._rc_active = False
        self._dc_active = False

        # Click cooldown tracking
        self._last_click_t = 0.0
        self._last_rc_t    = 0.0
        self._last_dc_t    = 0.0

        # Scroll
        self._scroll_ref_y = None

    @staticmethod
    def _px(lm, idx, cw, ch):
        return int(lm[idx].x * cw), int(lm[idx].y * ch)

    def classify(self, lm, cam_w: int, cam_h: int, now: float) -> dict:
        px = lambda i: self._px(lm, i, cam_w, cam_h)

        wrist   = px(0)
        thumb   = px(4)
        idx_tip = px(8);  idx_pip = px(6);  idx_mcp = px(5)
        mid_tip = px(12); mid_pip = px(10); mid_mcp = px(9)
        rng_tip = px(16); rng_pip = px(14); rng_mcp = px(13)
        pky_tip = px(20); pky_pip = px(18); pky_mcp = px(17)

        palm_size = max(dist(wrist, px(9)), 1.0)
        enter_thr = palm_size * self.PINCH_ENTER
        exit_thr  = palm_size * self.PINCH_EXIT

        # ── Finger-up flags ──────────────────────────────────────────────
        idx_up = finger_up(wrist, idx_tip, idx_pip)
        mid_up = finger_up(wrist, mid_tip, mid_pip)
        rng_up = finger_up(wrist, rng_tip, rng_pip)
        pky_up = finger_up(wrist, pky_tip, pky_pip)

        # Scroll Pose: index + middle extended, ring curled down
        is_scroll_pose = idx_up and mid_up and not rng_up

        lc_dist = dist(thumb, idx_tip)
        rc_dist = dist(thumb, mid_tip)

        # Double Click Counter (3-finger pinch: lc_dist and rc_dist both small)
        enter_thr_dc = palm_size * self.DBL_ENTER
        exit_thr_dc  = palm_size * self.DBL_EXIT
        is_3finger_pinch = (lc_dist < enter_thr_dc and rc_dist < enter_thr_dc)

        # ── Frame-count confirmed pinch (prevents single-frame noise) ────
        # Clicks are only blocked if a scroll gesture is active
        is_nav_active = is_scroll_pose
        if not is_nav_active:
            if is_3finger_pinch:
                self._dc_cnt = min(self._dc_cnt + 1, self.PINCH_CONFIRM_FRAMES + 1)
                # Strictly suppress single clicks to prevent ghost firings!
                self._lc_cnt = 0
                self._rc_cnt = 0
            else:
                if lc_dist > exit_thr_dc or rc_dist > exit_thr_dc:
                    self._dc_cnt = 0
                
                # Left pinch counter
                if lc_dist < enter_thr:
                    self._lc_cnt = min(self._lc_cnt + 1, self.PINCH_CONFIRM_FRAMES + 1)
                elif lc_dist > exit_thr:
                    self._lc_cnt = 0

                # Right pinch counter
                if rc_dist < enter_thr:
                    self._rc_cnt = min(self._rc_cnt + 1, self.PINCH_CONFIRM_FRAMES + 1)
                elif rc_dist > exit_thr:
                    self._rc_cnt = 0
        else:
            # Force-reset pinches immediately if scroll navigation is active
            self._lc_cnt = 0
            self._rc_cnt = 0
            self._dc_cnt = 0

        prev_lc = self._lc_active
        if self._lc_cnt >= self.PINCH_CONFIRM_FRAMES:
            self._lc_active = True
        elif self._lc_cnt == 0:
            self._lc_active = False

        prev_rc = self._rc_active
        if self._rc_cnt >= self.PINCH_CONFIRM_FRAMES:
            self._rc_active = True
        elif self._rc_cnt == 0:
            self._rc_active = False

        prev_dc = self._dc_active
        if self._dc_cnt >= self.PINCH_CONFIRM_FRAMES:
            self._dc_active = True
        elif self._dc_cnt == 0:
            self._dc_active = False

        # ── Click dispatch (rising edge + cooldown) ──────────────────────
        lc_rising = self._lc_active and not prev_lc
        rc_rising = self._rc_active and not prev_rc
        dc_rising = self._dc_active and not prev_dc

        fire_left   = False
        fire_right  = False
        fire_double = False

        if dc_rising and now - self._last_dc_t >= self.CLICK_COOLDOWN:
            fire_double = True
            self._last_dc_t = now
            self._last_click_t = now
        elif lc_rising and now - self._last_click_t >= self.CLICK_COOLDOWN:
            fire_left = True
            self._last_click_t = now

        if rc_rising and now - self._last_rc_t >= self.CLICK_COOLDOWN:
            fire_right = True
            self._last_rc_t = now

        # ── Scroll — INVERTED, continuous joystick velocity ─────────────
        # Hysteresis: if already scrolling, relax the ring finger check so wrist tilts don't drop the pose!
        if self._scroll_ref_y is not None:
            is_scroll_pose = (idx_up and mid_up and not self._lc_active and not self._rc_active)
        else:
            is_scroll_pose = (idx_up and mid_up and not rng_up
                              and not self._lc_active and not self._rc_active)
        scroll_delta = 0

        if is_scroll_pose:
            if self._scroll_ref_y is None:
                self._scroll_ref_y = idx_tip[1]  # Lock the anchor point
                
            # OpenCV/MediaPipe Y-axis is inverted: 0 at the top, increasing downward.
            # dy is calculated as: current_y - anchor_y
            # 
            # Scroll direction registrations:
            # - Hand moved DOWN relative to anchor -> current_y increases -> dy > 0 (positive)
            #   This generates a positive scroll_delta -> triggers scroll UP (wheel rotated away).
            # - Hand moved UP relative to anchor -> current_y decreases -> dy < 0 (negative)
            #   This generates a negative scroll_delta -> triggers scroll DOWN (wheel rotated toward).
            #
            # Speed / Increment:
            # - Scaled proportionally based on the distance (dy) beyond the SCROLL_DEADBAND.
            # - Greater offset results in a larger scroll increment per frame (continuous velocity).
            dy = idx_tip[1] - self._scroll_ref_y
            
            if abs(dy) > self.SCROLL_DEADBAND:
                # Calculate velocity based on how far past the deadband the finger is
                active_dy = dy - (self.SCROLL_DEADBAND * (1 if dy > 0 else -1))
                # Generate continuous scroll delta EVERY FRAME (we do NOT reset the anchor!)
                scroll_delta = int(active_dy * self.SCROLL_SENSITIVITY)
        else:
            self._scroll_ref_y = None

        # ── Mode priority ────────────────────────────────────────────────
        if self._dc_active:
            mode = "DBLCLICK"
        elif self._lc_active:
            mode = "L-CLICK"
        elif self._rc_active:
            mode = "R-CLICK"
        elif is_scroll_pose:
            mode = "SCROLL"
        elif idx_up and not mid_up:
            mode = "MOVE"
        else:
            mode = "IDLE"

        return {
            "mode"        : mode,
            "ptr"         : idx_tip,
            "fire_left"   : fire_left,
            "fire_right"  : fire_right,
            "fire_double" : fire_double,
            "scroll_delta": scroll_delta,
            "lc_active"   : self._lc_active,
        }


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SPATIAL VISION CORE                           │
# └────────────────────────────────────────────────────────────────────────┘
class GestureMouseEngine:
    # Asymmetric margins to keep hand centered in high-accuracy tracking zone.
    # Forearm/wrist comes from the bottom, so MARGIN_BOTTOM must be larger to prevent cutoff.
    MARGIN_X      = 100   # horizontal comfort margin
    MARGIN_TOP    = 100   # vertical margin at the top
    MARGIN_BOTTOM = 185   # vertical margin at the bottom (prevents wrist cut-off)

    def __init__(self):
        print_info("Booting KAYRA Beast Kernel Engine v4 …")

        self.mp_hands = mp.solutions.hands
        self.hands    = self.mp_hands.Hands(
            static_image_mode        = False,
            max_num_hands            = 1,
            model_complexity         = 1,
            min_detection_confidence = 0.75,
            min_tracking_confidence  = 0.75,
        )
        self.mp_draw  = mp.solutions.drawing_utils
        self.mp_style = mp.solutions.drawing_styles

        self.screen_w, self.screen_h = get_screen_size()

        # 1€ filter — tuned for minimal wobble + fast response
        self.fx = OneEuroFilter(min_cutoff=2.0, beta=0.05)
        self.fy = OneEuroFilter(min_cutoff=2.0, beta=0.05)

        self.gsm = GestureStateMachine()

        # Click locks
        self._l_lock = self._r_lock = False

        # FPS ring buffer
        self._fps_buf = collections.deque(maxlen=30)
        self._t_last  = time.perf_counter()

        print_success("Engine armed. Index up -> MOVE | Pinch -> Click")

    def _fps(self) -> float:
        now = time.perf_counter()
        dt  = max(now - self._t_last, 1e-9)
        self._fps_buf.append(1.0 / dt)
        self._t_last = now
        return sum(self._fps_buf) / len(self._fps_buf)

    def start_capture_loop(self):
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS,          30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)   # always get the freshest frame

        if not cap.isOpened():
            print_error("Cannot open webcam.")
            return

        cam_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        cam_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        x_min, x_max = self.MARGIN_X, cam_w - self.MARGIN_X
        y_min, y_max = self.MARGIN_TOP, cam_h - self.MARGIN_BOTTOM

        print_info(f"Camera {cam_w}×{cam_h} | Screen {self.screen_w}×{self.screen_h}")
        print_info("Press 'q' in preview window to quit.")

        while cap.isOpened():
            try:
                # ── Grab frame ──────────────────────────────────────────
                ret, frame = cap.read()
                if not ret:
                    continue

                fps = self._fps()
                now = time.perf_counter()

                frame = cv2.flip(frame, 1)
                rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False
                results = self.hands.process(rgb)
                rgb.flags.writeable = True

                cv2.rectangle(frame, (x_min, y_min), (x_max, y_max), (70, 0, 180), 1)
                mode = "IDLE"

                if results.multi_hand_landmarks:
                    for hand_lm in results.multi_hand_landmarks:
                        self.mp_draw.draw_landmarks(
                            frame, hand_lm,
                            self.mp_hands.HAND_CONNECTIONS,
                            self.mp_style.get_default_hand_landmarks_style(),
                            self.mp_style.get_default_hand_connections_style(),
                        )

                        lm = hand_lm.landmark
                        g  = self.gsm.classify(lm, cam_w, cam_h, now)
                        mode = g["mode"]
                        ptr  = g["ptr"]

                        # ── CURSOR MOVEMENT ──────────────────────────────
                        # Move during MOVE and while pinch is held (for drag)
                        if mode in ("MOVE", "L-CLICK", "R-CLICK", "DBLCLICK"):
                            raw_x = map_to_screen(ptr[0], x_min, x_max, self.screen_w)
                            raw_y = map_to_screen(ptr[1], y_min, y_max, self.screen_h)

                            # 1€ filter applied to screen coordinates directly
                            sx = self.fx(now, raw_x)
                            sy = self.fy(now, raw_y)

                            move_cursor(sx, sy)

                            col = _MODE_COLORS.get(mode, (255, 255, 255))
                            cv2.circle(frame, ptr, 10, col, cv2.FILLED)
                            cv2.circle(frame, ptr, 13, (255, 255, 255), 1, cv2.LINE_AA)

                        # Reset filter when not tracking to avoid stale state
                        elif mode in ("SCROLL", "IDLE"):
                            self.fx.reset()
                            self.fy.reset()

                        # ── CLICKS / DOUBLE CLICKS ───────────────────────
                        if g["fire_double"]:
                            double_click_kernel()
                            print_info("Double Click")
                        elif g["fire_left"]:
                            click_kernel("left")
                            print_info("Left Click")

                        if g["fire_right"]:
                            click_kernel("right")
                            print_info("Right Click")

                        # ── SCROLL ───────────────────────────────────────
                        if g["scroll_delta"] != 0:
                            scroll_kernel(g["scroll_delta"])

                draw_hud(frame, mode, fps)
                cv2.imshow("KAYRA - Air Cursor Engine", frame)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            except KeyboardInterrupt:
                break
            except Exception as e:
                # Swallow transient USB / driver / MediaPipe errors
                print_warning(f"Frame error (skipped): {e}")
                continue

        cap.release()
        cv2.destroyAllWindows()
        self.hands.close()
        print_success("Engine shut down cleanly.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              ENTRY POINT                               │
# └────────────────────────────────────────────────────────────────────────┘
if __name__ == "__main__":
    print_banner("AIR CURSOR ENGINE v4",
                 "1€ · FSM · D-Click · Scroll")
    GestureMouseEngine().start_capture_loop()
