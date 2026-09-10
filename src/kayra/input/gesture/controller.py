# ┌────────────────────────────────────────────────────────────────────────┐
# │                      gesture/controller.py                             │
# │            THE Gesture Runtime — one owner for everything              │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single authoritative gesture-control runtime. One per process.

    camera ──▶ preprocess ──▶ detect ──▶ features ──▶ recogniser ──▶ arbitration ──▶ pointer
                 │
                 └──▶ preview (throttled, same frame, no second capture)

WHAT THIS OWNS
--------------
The camera, the detector, the recogniser, the pointer stabiliser, the pointer controller, the
processing thread, the preview buffer and the runtime state. Nothing else in the process
constructs any of them — `tests/test_gesture_control.py` walks the tree and asserts there is
exactly one `VideoCapture` call site and exactly one `solutions.hands` call site, both here
and in the two modules this owns.

TWO SWITCHES, NOT ONE
---------------------
    Camera:  ON / OFF        — the device
    Gesture: ON / OFF        — whether the device drives the mouse

They are separate because three of the four combinations are meaningful:

    camera ON,  gesture OFF   valid — the preview works, nothing touches the pointer
    camera ON,  gesture ON    valid — the feature, running
    camera OFF, gesture OFF   valid — nothing running, device released
    camera OFF, gesture ON    IMPOSSIBLE, and it is prevented rather than repaired

Enabling gesture control with the camera off starts the camera first and reports a single
outcome; if the camera fails, gesture control stays OFF and says why. Turning the camera off
while gesture control is on turns gesture control off too, in that order, because the
alternative is a gesture runtime waiting forever for frames that will not come.

ONE THREAD
----------
`kayra-gesture` runs whenever the camera is on. With gesture control off it does nothing but
republish a preview frame at the preview rate — a few hundred microseconds every 66ms. With
gesture control on it runs the full pipeline. One thread for both is what guarantees the
preview and the recogniser see the same frame and that there is never a second consumer
racing the mailbox.

IT NEVER TOUCHES VOICE STATE
----------------------------
No call here reaches `set_listening`, `set_sleeping`, the STT engine, the TTS engine or the
voice state machine, and `tests/test_gesture_control.py` asserts that by AST. Camera activity
must never be able to pause the microphone or repaint the orb; the two systems share only the
runtime event bus, in one direction, for notification.
"""

import threading

from kayra.core.logbus import Subsystem, debug, info, warning, error, success
from kayra.core.runtime_state import get_runtime_state
from kayra.input.gesture.camera import CameraSource
from kayra.input.gesture.config import GestureConfig
from kayra.input.gesture.detector import HandDetector, accelerator_report
from kayra.input.gesture.filters import PointerStabilizer, monotonic
from kayra.input.gesture.pointer import PointerController
from kayra.input.gesture.state_machine import (
    GestureRecognizer, GestureState, STATE_LABELS, PAUSED_STATES)


class GestureRuntimeState:
    """
    What the gesture RUNTIME is doing. Distinct from the camera's status and from the
    recogniser's gesture state, and the three are reported separately on purpose — merging
    them is how "camera failed" and "no hand in frame" become the same message.
    """

    OFF = "OFF"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    # RUNNING AND WAITING. There is no hand in frame — gesture control is working perfectly and
    # has nothing to act on.
    #
    # THIS IS A DIFFERENT STATE FROM `PAUSED` AND THE DISTINCTION IS THE POINT. "No hand" is a
    # fact about the world; "paused" is something the user DID. Collapsing them tells a user
    # who simply lowered their hand that they paused the system, which is both wrong and
    # unactionable — there is nothing for them to undo.
    ACTIVE_NO_HAND = "ACTIVE_NO_HAND"
    # Deliberately not acting: the user held a fist, or asked for it. NOTHING ELSE REACHES
    # THIS STATE — not a lost hand, not a noisy frame, not a low-confidence detection.
    PAUSED = "PAUSED"
    ERROR = "ERROR"


# The states in which the runtime is up and processing frames.
RUNNING_STATES = frozenset({GestureRuntimeState.ACTIVE, GestureRuntimeState.ACTIVE_NO_HAND,
                            GestureRuntimeState.PAUSED})


RUNTIME_LABELS = {
    GestureRuntimeState.OFF: "Disabled",
    GestureRuntimeState.STARTING: "Starting",
    GestureRuntimeState.ACTIVE: "Active",
    # Deliberately still "Active": from the user's point of view the feature IS active, it
    # simply cannot see a hand. The `hand` field carries that separately, and Home renders it
    # as its own row.
    GestureRuntimeState.ACTIVE_NO_HAND: "Active",
    GestureRuntimeState.PAUSED: "Paused",
    GestureRuntimeState.ERROR: "Error",
}


class GestureController:
    """
    The runtime. Construct once, via `get_gesture_controller()`.

    Every public method is safe from any thread and every one of them is idempotent, because
    the four surfaces that drive it — the Home toggles, the spoken commands, the settings
    screen and the shutdown path — can and do arrive concurrently.
    """

    def __init__(self, config=None, pointer=None):
        self.config = config or GestureConfig.from_env()
        self._lock = threading.RLock()

        self.camera = CameraSource(self.config)
        self.detector = HandDetector(self.config)
        self.recognizer = GestureRecognizer(self.config)
        self.pointer = pointer or PointerController()
        self.stabilizer = PointerStabilizer(self.config, self.pointer.screen_size())

        self._thread = None
        self._stop = threading.Event()

        self.state = GestureRuntimeState.OFF
        self.gesture_enabled = False
        self.error_detail = ""

        self._last_seq = None
        self._last_hand_t = None
        self._preview = None            # (bytes, width, height) RGB888
        self._preview_lock = threading.Lock()
        self._preview_t = 0.0

        # Telemetry. Counters and two rolling means — nothing that grows.
        self._process_fps = 0.0
        self._fps_t = None
        self._fps_n = 0
        self._decision = None
        self._runtime = None
        # Whether a hand was in the previous frame, so "Detected" and "Lost" are logged on the
        # transition and never per frame.
        self._hand_present = False
        self._published_t = 0.0
        self._published = None

        if self.config.corrections:
            for note in self.config.corrections:
                warning(Subsystem.GESTURE, f"Configuration corrected: {note}")

    # ──────────────────────────────────────────────────────────────────
    #                            THE CAMERA
    # ──────────────────────────────────────────────────────────────────

    def set_camera(self, enabled: bool):
        """
        Turns the camera on or off. Returns `(ok, detail)`.

        Turning it OFF stops gesture control first. The order matters: a gesture runtime whose
        camera has been released spends its grace period, resets, and then polls an empty
        mailbox forever — alive, doing nothing, and reporting ACTIVE. Stopping the consumer
        before the producer means the runtime state is honest at every instant of the
        transition.
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled and self.camera.running:
                return True, "already on"
            if not enabled and not self.camera.running:
                return True, "already off"

        if not enabled:
            if self.gesture_enabled:
                self.set_gesture(False, reason="camera off")
            self._stop_thread()
            self.camera.stop()
            self._publish()
            return True, "off"

        info(Subsystem.CAMERA, "Starting…")
        ok, detail = self.camera.start()
        if not ok:
            self.error_detail = detail
            self._set_state(GestureRuntimeState.ERROR)
            self._publish()
            return False, detail

        self.error_detail = ""
        self._start_thread()
        self._publish()
        return True, detail

    def camera_enabled(self) -> bool:
        return self.camera.running

    # ──────────────────────────────────────────────────────────────────
    #                        THE GESTURE ENGINE
    # ──────────────────────────────────────────────────────────────────

    def set_gesture(self, enabled: bool, reason: str = ""):
        """
        Turns hand gesture control on or off. Returns `(ok, detail)`.

        Enabling starts the camera if it is not already running — the invalid combination is
        prevented here, once, rather than being checked by every caller. If the camera cannot
        start, gesture control does not come on and the failure is the camera's message, not a
        generic one: "gesture control unavailable" tells the user nothing they can fix.
        """
        enabled = bool(enabled)

        if not enabled:
            with self._lock:
                if not self.gesture_enabled:
                    return True, "already off"
                self.gesture_enabled = False
            # The pointer is disabled BEFORE the recogniser is reset, so there is no window in
            # which a decision produced by the last in-flight frame can still reach the mouse.
            self.pointer.disable()
            self.recognizer.reset(monotonic())
            self.stabilizer.reset()
            self.detector.stop()
            self._decision = None
            self._hand_present = False
            self._set_state(GestureRuntimeState.OFF if self.camera.running
                            else GestureRuntimeState.OFF)
            info(Subsystem.GESTURE, "Stopping hand gesture control"
                                    + (f" ({reason})" if reason else ""))
            success(Subsystem.GESTURE, "Control disabled")
            self._publish()
            return True, "off"

        with self._lock:
            if self.gesture_enabled:
                return True, "already on"

        info(Subsystem.GESTURE, "Starting hand gesture control")
        self._set_state(GestureRuntimeState.STARTING)

        if not self.camera.running:
            ok, detail = self.set_camera(True)
            if not ok:
                self.error_detail = detail
                self._set_state(GestureRuntimeState.ERROR)
                self._publish()
                return False, detail

        ok, detail = self.detector.start()
        if not ok:
            self.error_detail = detail
            self._set_state(GestureRuntimeState.ERROR)
            error(Subsystem.GESTURE, f"Detector unavailable: {detail}")
            self._publish()
            return False, detail

        # Screen geometry is re-read on every start: a laptop docked since the last run has a
        # different desktop, and a stabiliser mapping into the old one puts the pointer in the
        # wrong monitor or off the edge entirely.
        self.stabilizer = PointerStabilizer(self.config, self.pointer.screen_size())
        self.recognizer.reset(monotonic())
        self._last_hand_t = None
        self._decision = None
        self._hand_present = False

        with self._lock:
            self.gesture_enabled = True
        self.pointer.enable()
        self._start_thread()
        self._set_state(GestureRuntimeState.ACTIVE)
        self.error_detail = ""
        success(Subsystem.GESTURE, "Control active")
        self._publish()
        return True, "on"

    # ──────────────────────────────────────────────────────────────────
    #                            SHUTDOWN
    # ──────────────────────────────────────────────────────────────────

    def shutdown(self):
        """
        Releases everything. Called from `app.request_shutdown`, and safe to call twice.

        Deliberately does NOT go through `set_gesture`/`set_camera`: those publish state and
        log transitions, and a teardown that narrates itself competes with the shutdown
        sequence's own reporting. This is the quiet path — stop acting, stop the thread,
        release the device, close the graph.
        """
        try:
            self.pointer.disable()
        except Exception:
            pass
        with self._lock:
            self.gesture_enabled = False
        self._stop_thread()
        try:
            self.camera.stop()
        except Exception:
            pass
        try:
            self.detector.stop()
        except Exception:
            pass
        with self._preview_lock:
            self._preview = None
        self.state = GestureRuntimeState.OFF

    # ──────────────────────────────────────────────────────────────────
    #                          THE ONE THREAD
    # ──────────────────────────────────────────────────────────────────

    def _start_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="kayra-gesture",
                                            daemon=True)
            self._thread.start()

    def _stop_thread(self):
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _loop(self):
        """
        The processing loop. Always on the NEWEST frame, never on a queued one.

        There is no `sleep(frame_interval)` pacing here and that is deliberate. The loop blocks
        on nothing and polls the mailbox; when no new frame has arrived it waits a short slice
        and asks again. Pacing to a target rate would make the loop drift against the camera's
        actual cadence, and the whole point of the mailbox is that the loop's rate does not
        have to match anything.
        """
        cfg = self.config
        idle_wait = min(cfg.frame_interval * 0.4, 0.012)

        while not self._stop.is_set():
            frame, seq = self.camera.latest(since_seq=self._last_seq)
            if frame is None:
                if self._stop.wait(idle_wait):
                    break
                continue
            self._last_seq = seq
            self.camera.note_consumed(seq)

            try:
                self._process(frame)
            except Exception as exc:
                # ONE frame is lost. The thread survives, because a dead processing thread is
                # a gesture system that stops working with no message — the exact failure this
                # rewrite exists to remove.
                debug(Subsystem.GESTURE, f"Frame error: {type(exc).__name__}: {exc}")

            self._tick_fps()

    # ──────────────────────────────────────────────────────────────────

    def _process(self, frame):
        """One frame, all the way through. Called only from the gesture thread."""
        import cv2

        cfg = self.config
        now = monotonic()

        if cfg.mirror:
            frame = cv2.flip(frame, 1)

        # The preview is published from the SAME frame, before inference, so a slow inference
        # cannot stall the preview and the two can never disagree about what the camera saw.
        self._maybe_publish_preview(frame, cv2)

        if not self.gesture_enabled or not self.detector.ready:
            return

        height, width = frame.shape[:2]
        aspect = height / max(width, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        features = self.detector.detect(rgb, aspect)

        grace = False
        if features is None or not features.valid:
            if self._last_hand_t is not None:
                elapsed_ms = (now - self._last_hand_t) * 1000.0
                grace = elapsed_ms <= cfg.hand_lost_grace_ms
            if not grace:
                # Past the grace period the hand is genuinely gone. The stabiliser is reset so
                # the next acquisition cannot be smoothed FROM a stale position across the
                # screen, and the recogniser is reset so no gate survives to fire on the
                # first frame of a new hand.
                self.stabilizer.reset()
                self._last_hand_t = None
        else:
            if self._last_hand_t is None:
                # Re-acquisition: filter history dropped, emitted pointer position KEPT, so
                # the pointer walks to the hand under the speed ceiling instead of teleporting.
                self.stabilizer.reacquire()
            self._last_hand_t = now

        decision = self.recognizer.update(features, now, grace_active=grace)
        self._decision = decision

        if decision.changed:
            debug(Subsystem.GESTURE, f"State: {decision.state}")

        if not self.gesture_enabled:
            return                              # switched off during this frame

        if decision.track_pointer and features is not None and features.valid:
            point = self.stabilizer.update(now, features.pointer[0], features.pointer[1],
                                           features.hand_scale)
            if point is not None:
                self.pointer.move(point[0], point[1])

        if decision.fire_double:
            self.pointer.double_click()
            info(Subsystem.GESTURE, "Double click")
        elif decision.fire_left:
            self.pointer.left_click()
            info(Subsystem.GESTURE, "Left click")
        if decision.fire_right:
            self.pointer.right_click()
            info(Subsystem.GESTURE, "Right click")
        if decision.scroll_delta:
            self.pointer.scroll(decision.scroll_delta)

        self._note_hand(decision)

        # THE MAPPING FROM GESTURE STATE TO RUNTIME STATE, AND THE THREE OUTCOMES IT KEEPS
        # APART. Only a DELIBERATE pause reaches PAUSED; an empty frame reaches ACTIVE_NO_HAND;
        # everything else is ACTIVE. The previous version mapped `state == PAUSED` to PAUSED
        # and *everything else* to ACTIVE, which was correct as far as it went — the flapping
        # came from upstream, where a per-frame comparator decided what PAUSED meant.
        if decision.state in PAUSED_STATES:
            target = GestureRuntimeState.PAUSED
        elif decision.state == GestureState.NO_HAND:
            target = GestureRuntimeState.ACTIVE_NO_HAND
        else:
            target = GestureRuntimeState.ACTIVE

        if target != self.state:
            if (target == GestureRuntimeState.PAUSED
                    and self.state != GestureRuntimeState.PAUSED):
                info(Subsystem.GESTURE, "Pause gesture detected")
            self._set_state(target)
        if decision.changed:
            # Throttled, and only when the SHAPE of the status changed. Cursor and
            # left-click-candidate alternate several times a second while a user hovers with
            # their fingers loosely together, and every one of those transitions would
            # otherwise become a queued Qt signal and a repaint of the Home card. The bus is
            # synchronous, so a chatty publisher spends the gesture thread's budget in the
            # subscribers.
            self._publish(throttled=True)

    def _note_hand(self, decision):
        """
        Logs `Hand: Detected` / `Hand: Lost` ON THE TRANSITION, and only then.

        A hand appearing and disappearing is the single most frequent event in this subsystem,
        so it is exactly the one that must never be logged per frame. It is also a genuinely
        useful line — "is it seeing me?" is the first question a user asks — which is why it is
        at INFO rather than being pushed to DEBUG with everything else.
        """
        present = decision.state not in (GestureState.NO_HAND,)
        if present == self._hand_present:
            return
        self._hand_present = present
        info(Subsystem.GESTURE, "Hand: Detected" if present else "Hand: Lost")

    # ──────────────────────────────────────────────────────────────────
    #                             PREVIEW
    # ──────────────────────────────────────────────────────────────────

    def _maybe_publish_preview(self, frame, cv2):
        """
        Converts the current frame to a display-ready RGB buffer, at the PREVIEW rate.

        Throttled independently of the processing rate — 15 FPS by default against 30 FPS of
        processing — because the preview is a reassurance, not an instrument. The conversion
        and resize cost ~0.4ms at the preview size; doing it every processed frame would be
        1.2% of the frame budget spent producing images nobody looks at between repaints.

        SINGLE SLOT, OVERWRITTEN. The UI pulls the newest buffer on its own timer and stale
        buffers are simply replaced. There is no signal carrying frames and no Qt queue to
        grow, which is the whole of the "never let the UI queue grow" requirement — a pull
        model cannot have a backlog.
        """
        now = monotonic()
        if (now - self._preview_t) < self.config.preview_interval:
            return
        self._preview_t = now

        height, width = frame.shape[:2]
        # Fixed WIDTH, height derived. Preserving the source aspect is what stops the preview
        # stretching on a 16:9 camera in a 4:3 panel; the panel letterboxes instead.
        target_w = 240
        target_h = max(1, int(round(height * (target_w / max(width, 1)))))
        small = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        buffer = bytes(rgb.tobytes())
        with self._preview_lock:
            self._preview = (buffer, target_w, target_h)

    def preview(self):
        """
        The newest preview frame as `(rgb_bytes, width, height)`, or None.

        Never blocks and never converts — the conversion already happened on the gesture
        thread. A GUI thread that had to do a colour conversion to paint would be a GUI thread
        that stutters when the camera is busy.
        """
        with self._preview_lock:
            return self._preview

    # ──────────────────────────────────────────────────────────────────
    #                        STATE AND TELEMETRY
    # ──────────────────────────────────────────────────────────────────

    def _set_state(self, state):
        with self._lock:
            if state == self.state:
                return
            previous, self.state = self.state, state
        info(Subsystem.GESTURE, f"State: {previous} -> {state}")

    def _tick_fps(self):
        now = monotonic()
        if self._fps_t is None:
            self._fps_t, self._fps_n = now, 0
            return
        self._fps_n += 1
        span = now - self._fps_t
        if span >= 1.0:
            self._process_fps = self._fps_n / span
            self._fps_t, self._fps_n = now, 0

    def status(self) -> dict:
        """
        The whole picture in one synchronous read. Cheap: no I/O, no locks held across work.

        THE THREE FACTS ARE SEPARATE FIELDS. `camera`, `gesture` and `state` answer different
        questions and a screen that showed one of them as all three would be the same defect
        as a speech card showing the requested device as the active one.
        """
        decision = self._decision
        camera = self.camera.telemetry()
        running = self.gesture_enabled and self.state in RUNNING_STATES
        return {
            "camera": camera["status"],
            "camera_device": camera["device"],
            "gesture_enabled": self.gesture_enabled,
            "state": self.state,
            "state_label": RUNTIME_LABELS.get(self.state, self.state),
            # THREE INDEPENDENT FACTS, THREE FIELDS. "Is the feature on", "did the user pause
            # it", and "can it see a hand" are different questions with different answers, and
            # a screen that derived any of them from another would eventually tell a user who
            # lowered their hand that they had paused the system.
            "paused": bool(running and self.state == GestureRuntimeState.PAUSED),
            "hand": bool(running and decision is not None
                         and decision.state != GestureState.NO_HAND),
            "gesture": (decision.label if decision is not None and running
                        else STATE_LABELS[GestureState.NO_HAND]),
            "gesture_state": (decision.state if decision is not None
                              else GestureState.NO_HAND),
            "error": self.error_detail or camera["error"],
        }

    def telemetry(self) -> dict:
        """
        Diagnostics. Everything measurable, in one dict, for the advanced view and `--doctor`.

        NOT shown on Home. A status card with eleven numbers on it is a status card nobody
        reads; the three facts a user needs are in `status()` and everything here is behind the
        diagnostics switch.
        """
        decision = self._decision
        camera = self.camera.telemetry()
        return {
            "camera": camera,
            "detector": self.detector.telemetry(),
            "pointer": self.pointer.telemetry(),
            "processing_fps": round(self._process_fps, 1),
            "camera_fps": camera["fps"],
            "dropped_frames": camera["dropped"],
            "outliers_dropped": self.stabilizer.dropped_outliers,
            "deadzone_holds": self.stabilizer.deadzone_holds,
            "speed_clamps": self.stabilizer.speed_clamps,
            "cursor_velocity": round(self.stabilizer.velocity, 4),
            "hand_stability": round(decision.stability, 3) if decision else 0.0,
            "gesture": decision.label if decision else "—",
            "gesture_confidence": round(max(
                decision.cursor_confidence, decision.left_click_confidence,
                decision.right_click_confidence, decision.scroll_confidence), 3
            ) if decision else 0.0,
            "accelerator": accelerator_report(),
            "config": self.config.summary(),
        }

    def log_diagnostics(self):
        """One DEBUG line of live telemetry. Called on demand, never per frame."""
        data = self.telemetry()
        debug(Subsystem.GESTURE,
              f"fps={data['processing_fps']} camera_fps={data['camera_fps']} "
              f"latency={data['detector']['latency_ms']}ms "
              f"stability={data['hand_stability']} "
              f"confidence={data['gesture_confidence']} "
              f"dropped={data['dropped_frames']} outliers={data['outliers_dropped']}")

    # ──────────────────────────────────────────────────────────────────
    #                          EVENT PUBLICATION
    # ──────────────────────────────────────────────────────────────────

    # How often status may reach the bus from the per-frame path. Explicit changes (a switch
    # being thrown, an error) publish immediately and ignore this.
    PUBLISH_INTERVAL_S = 0.2

    def _publish(self, throttled=False):
        """
        Announces the current status on the runtime bus.

        NOTIFICATION ONLY, AND IN ONE DIRECTION. Nothing here writes assistant state — not
        `set_state`, not `set_listening`, not `set_sleeping`. The gesture runtime and the voice
        runtime are independent systems that happen to share a process, and the moment camera
        activity can move the voice state machine, "Listening paused" appears on screen because
        somebody turned a camera on.
        """
        try:
            status = self.status()
            if throttled:
                now = monotonic()
                fingerprint = (status["camera"], status["gesture_enabled"], status["state"],
                               status["hand"], status["gesture"])
                if (fingerprint == self._published
                        and (now - self._published_t) < self.PUBLISH_INTERVAL_S):
                    return
                if (now - self._published_t) < self.PUBLISH_INTERVAL_S:
                    return
                self._published, self._published_t = fingerprint, now
            else:
                self._published_t = monotonic()
                self._published = (status["camera"], status["gesture_enabled"],
                                   status["state"], status["hand"], status["gesture"])
            if self._runtime is None:
                self._runtime = get_runtime_state()
            self._runtime.emit("gesture_state", **status)
        except Exception:
            pass


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One controller, for the same reason `RuntimeState` and `CentralizedLLMEngine` have one: two
# would each own a camera, and most webcams cannot be opened twice. `reset_gesture_controller`
# exists for the test suite and shuts the previous one down rather than orphaning its thread.

_CONTROLLER = None
_CONTROLLER_LOCK = threading.Lock()


def get_gesture_controller(config=None, pointer=None) -> GestureController:
    global _CONTROLLER
    if _CONTROLLER is None:
        with _CONTROLLER_LOCK:
            if _CONTROLLER is None:
                _CONTROLLER = GestureController(config=config, pointer=pointer)
    return _CONTROLLER


def gesture_controller_if_running():
    """
    The live controller, or None — WITHOUT constructing one.

    Used by anything that only wants to report on gesture control (the startup report, the
    status card, `--doctor`). Asking "is gesture control running?" must not be the thing that
    starts a camera, which is the same rule `automation.targets.kayra_owned_pids` follows for
    the STT engine.
    """
    return _CONTROLLER


def reset_gesture_controller():
    """Tears the singleton down. Test-suite only."""
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        controller, _CONTROLLER = _CONTROLLER, None
    if controller is not None:
        try:
            controller.shutdown()
        except Exception:
            pass
