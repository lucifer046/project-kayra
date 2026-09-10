# ┌────────────────────────────────────────────────────────────────────────┐
# │                      test_camera_runtime.py                            │
# │      One Capture Owner — the mailbox, recovery, and the release        │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_camera_runtime.py — standalone diagnostic for the camera layer.

    .venv\\Scripts\\python tests\\test_camera_runtime.py
    .venv\\Scripts\\python tests\\test_camera_runtime.py --live   (opens the real camera)

HARDWARE-FREE BY DEFAULT. A fake `cv2` is installed in `sys.modules` before `CameraSource`
imports it, so a camera that is unplugged, that opens and never delivers, that dies halfway
through and that comes back can all be staged exactly — none of which can be arranged with a
real webcam on a developer's desk.

WHAT THIS FILE IS ACTUALLY ABOUT
--------------------------------
The freeze. v1 read the camera synchronously in the same loop as inference, so a slow frame
left the driver's queue holding frames from the past and the loop never caught up. The fix is a
capture thread writing into a SINGLE-SLOT mailbox, and the properties that make it a fix are
testable and tested here:

  * a slow consumer always gets the NEWEST frame, never a queued one
  * frames nobody read are dropped, and counted
  * nothing accumulates, at any consumer speed
  * the device is released on every exit path, including an exception and a shutdown

SECTIONS
  1. Start, stop, and idempotence.
  2. The mailbox: newest-frame semantics and drop counting.
  3. A slow consumer — the freeze scenario, asserted.
  4. Failure: unopenable, opens-but-silent, and dies mid-run.
  5. Bounded recovery, and that it gives up rather than looping.
  6. Release on every path, including shutdown.
  7. Status transitions and telemetry.
  8. Repeated on/off cycling — the resource check that found the DSHOW thread leak.
  9. The same cycling against the real camera (--live only).
 10. Live camera (--live only).
"""

import os
import sys
import time
import types
import threading

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE FAKE cv2                              │
# └────────────────────────────────────────────────────────────────────────┘
# Installed in `sys.modules` BEFORE `CameraSource._open` imports it. That import is deliberately
# inside the function rather than at module scope (so a machine with no OpenCV can still import
# and test the package), and this file is the second reason that placement is right: it makes
# the device substitutable without a mocking framework.

class FakeCapture:
    """A scriptable VideoCapture. Every failure mode this module must survive, on demand."""

    def __init__(self, index, backend=None, state=None, opens=True, silent=False,
                 fail_after=None, recover_at=None, delay=0.0):
        self.index = index
        self._opens = opens
        self._silent = silent
        self._fail_after = fail_after
        self._recover_at = recover_at
        self._delay = delay
        # The read counter is SHARED between every capture the fake module hands out, so
        # "the device is gone" survives a reopen. A per-instance counter models a camera that
        # is healthy again the moment it is reopened, which is a different failure and would
        # let a genuinely dead device look recoverable forever.
        self._state = state if state is not None else {"reads": 0}
        self.released = False
        self.props = {}

    @property
    def reads(self):
        return self._state["reads"]

    def isOpened(self):
        return self._opens and not self.released

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, 0)

    def read(self):
        if self.released:
            return False, None
        if self._delay:
            time.sleep(self._delay)
        self._state["reads"] += 1
        if self._silent:
            time.sleep(0.005)
            return False, None
        if self._fail_after is not None and self.reads > self._fail_after:
            if self._recover_at is None or self.reads < self._recover_at:
                return False, None
        # A frame is a tiny object carrying its own sequence number, which is what lets the
        # mailbox checks assert WHICH frame a consumer got rather than merely that it got one.
        return True, FakeFrame(self.reads)

    def release(self):
        self.released = True


class _Flags:
    """`ndarray.flags`. The detector clears `writeable` before inference, as MediaPipe wants."""

    def __init__(self):
        self.writeable = True


class FakeFrame:
    """Stands in for a numpy array. Carries `shape`, `flags` and a sequence number."""

    def __init__(self, seq):
        self.seq = seq
        self.shape = (480, 640, 3)
        self.flags = _Flags()

    def tobytes(self):
        return bytes(self.shape[0] * self.shape[1] * 3)

    def __eq__(self, other):
        return isinstance(other, FakeFrame) and other.seq == self.seq


def install_fake_cv2(**kwargs):
    """Puts a fake `cv2` in `sys.modules` and returns it. Returns the previous module too."""
    module = types.ModuleType("cv2")
    module.CAP_DSHOW = 700
    module.CAP_PROP_FRAME_WIDTH = 3
    module.CAP_PROP_FRAME_HEIGHT = 4
    module.CAP_PROP_FPS = 5
    module.CAP_PROP_BUFFERSIZE = 38
    module.COLOR_BGR2RGB = 4
    module.INTER_AREA = 3
    module.created = []
    shared = {"reads": 0}
    module.device_state = shared

    def VideoCapture(index, backend=None):
        cap = FakeCapture(index, backend, state=shared, **kwargs)
        module.created.append(cap)
        return cap

    module.VideoCapture = VideoCapture
    module.flip = lambda frame, code: frame
    module.cvtColor = lambda frame, code: frame
    module.resize = lambda frame, size, interpolation=None: frame
    previous = sys.modules.get("cv2")
    sys.modules["cv2"] = module
    return module, previous


def restore_cv2(previous):
    if previous is None:
        sys.modules.pop("cv2", None)
    else:
        sys.modules["cv2"] = previous


def source(**kwargs):
    from kayra.input.gesture.camera import CameraSource
    from kayra.input.gesture.config import GestureConfig
    cfg = GestureConfig()
    cfg.normalize()
    return CameraSource(cfg)


def wait_for(predicate, timeout=3.0, interval=0.01):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       1. START, STOP, IDEMPOTENCE                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_lifecycle():
    print_system("\n[1] Start, stop, idempotence")
    from kayra.input.gesture.camera import CameraStatus

    module, previous = install_fake_cv2()
    try:
        camera = source()
        check("a fresh camera is OFF", camera.status == CameraStatus.OFF)
        check("...and not running", not camera.running)

        ok, detail = camera.start()
        check("start() succeeds against a working device", ok, str(detail))
        check("...and reports ACTIVE", camera.status == CameraStatus.ACTIVE, camera.status)
        check("...and exactly ONE VideoCapture was created", len(module.created) == 1,
              str(len(module.created)))

        # Idempotent: "enable gesture control" and "turn on camera" both call this and neither
        # has to know whether the other already did.
        ok, _ = camera.start()
        check("start() on a running camera is a successful no-op", ok)
        check("...and still exactly one VideoCapture exists", len(module.created) == 1,
              str(len(module.created)))

        capture = module.created[0]
        camera.stop()
        check("stop() reports OFF", camera.status == CameraStatus.OFF, camera.status)
        check("stop() RELEASES the device", capture.released)
        check("...and clears the mailbox", camera.preview_frame() is None)
        camera.stop()
        check("stop() twice is safe", camera.status == CameraStatus.OFF)

        ok, _ = camera.start()
        check("a stopped camera can be started again", ok)
        check("...which creates a second capture, not a leaked first",
              len(module.created) == 2 and module.created[0].released)
        camera.stop()

        threads = [t for t in threading.enumerate() if t.name == "kayra-camera"]
        check("no capture thread survives stop()", not threads, str([t.name for t in threads]))
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            2. THE MAILBOX                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_mailbox():
    print_system("\n[2] The single-slot mailbox")

    module, previous = install_fake_cv2()
    try:
        camera = source()
        camera.start()

        frame, seq = camera.latest()
        check("latest() returns a frame and a sequence", frame is not None and seq > 0,
              f"seq={seq}")

        # `since_seq` is what stops the processor running inference twice on one frame.
        again, same_seq = camera.latest(since_seq=seq)
        check("latest(since_seq=...) returns nothing when no new frame has arrived",
              again is None or same_seq != seq)

        check("the preview reads the SAME slot, with no second capture",
              len(module.created) == 1, str(len(module.created)))
        preview = camera.preview_frame()
        check("preview_frame() returns a frame", preview is not None)

        # NEWEST-FRAME SEMANTICS. Let several frames go by unread, then read: the frame that
        # comes back must be a recent one, not the oldest unread. That is the whole difference
        # between this and a queue, and the whole reason the freeze cannot happen.
        _, before = camera.latest()
        time.sleep(0.25)
        frame, after = camera.latest()
        check("a consumer that fell behind receives the NEWEST frame, not a queued one",
              frame.seq >= after - 1, f"frame.seq={frame.seq} slot_seq={after}")
        check("...and many frames went by in the meantime", after - before > 3,
              f"{after - before} frames")
        camera.stop()
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        3. THE FREEZE SCENARIO                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_slow_consumer():
    print_system("\n[3] A slow consumer — the v1 freeze, asserted away")

    module, previous = install_fake_cv2()
    try:
        camera = source()
        camera.start()

        # A consumer four times slower than the camera. In v1 this is exactly the situation
        # that produced unbounded latency; here the staleness must stay at one frame.
        staleness = []
        last_seq = None
        for _ in range(12):
            frame, seq = camera.latest(since_seq=last_seq)
            if frame is None:
                time.sleep(0.02)
                continue
            camera.note_consumed(seq)
            last_seq = seq
            staleness.append(seq - frame.seq)
            time.sleep(0.12)          # ~4x slower than a 30 FPS camera

        check("a slow consumer never receives a stale frame",
              staleness and max(staleness) <= 1, f"max staleness {max(staleness or [0])}")
        check("frames the consumer never read were DROPPED, and counted",
              camera.frames_dropped > 0, str(camera.frames_dropped))
        check("...and the drop count is far larger than the read count",
              camera.frames_dropped > len(staleness), f"{camera.frames_dropped} dropped")

        # NOTHING ACCUMULATES. The mailbox is one slot, so there is no collection to grow.
        state = vars(camera)
        growing = [key for key, value in state.items()
                   if isinstance(value, (list, dict, set)) and len(value) > 8]
        check("no collection on the camera has grown", not growing, str(growing))
        camera.stop()
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            4. FAILURES                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_failures():
    print_system("\n[4] Camera failures")
    from kayra.input.gesture.camera import CameraStatus

    module, previous = install_fake_cv2(opens=False)
    try:
        camera = source()
        ok, detail = camera.start()
        check("an unopenable camera reports failure", not ok)
        check("...with a reason", bool(detail), str(detail))
        check("...and the status is ERROR", camera.status == CameraStatus.ERROR, camera.status)
        check("...and nothing is left running", not camera.running)
    finally:
        restore_cv2(previous)

    # OPENS BUT NEVER DELIVERS. A real and common failure — a device claimed by another
    # application, or a closed privacy shutter. Reporting success here would leave the UI
    # showing "Active" over a black rectangle forever.
    module, previous = install_fake_cv2(silent=True)
    try:
        camera = source()
        camera._await_first_frame = (lambda timeout: False)   # keep the suite fast
        ok, detail = camera.start()
        check("a camera that opens but delivers nothing reports FAILURE", not ok, str(detail))
        check("...and the device is released", module.created[0].released)
        check("...and the status is ERROR", camera.status == CameraStatus.ERROR)
    finally:
        restore_cv2(previous)

    # No OpenCV at all.
    previous = sys.modules.get("cv2")
    sys.modules["cv2"] = None
    try:
        camera = source()
        ok, detail = camera.start()
        check("a machine with no OpenCV fails cleanly, without an exception",
              not ok and "OpenCV" in str(detail) or not ok, str(detail))
    except Exception as exc:
        check("a machine with no OpenCV fails cleanly, without an exception", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        5. BOUNDED RECOVERY                             │
# └────────────────────────────────────────────────────────────────────────┘

def section_recovery():
    print_system("\n[5] Bounded recovery")
    from kayra.input.gesture import camera as camera_module
    from kayra.input.gesture.camera import CameraStatus, MAX_RECOVERY_ATTEMPTS

    # Dies after a few frames and never comes back: it must give up, not loop.
    module, previous = install_fake_cv2(fail_after=6)
    original_pause = camera_module.RECOVERY_PAUSE_S
    camera_module.RECOVERY_PAUSE_S = 0.02
    try:
        camera = source()
        camera.start()
        settled = wait_for(lambda: camera.status == CameraStatus.ERROR, timeout=5.0)
        check("a camera that stops delivering ends in ERROR", settled, camera.status)
        check("...after a BOUNDED number of attempts",
              len(module.created) <= MAX_RECOVERY_ATTEMPTS + 1,
              f"{len(module.created)} opens for {MAX_RECOVERY_ATTEMPTS} attempts")
        check("...and every capture it opened was released",
              all(cap.released for cap in module.created),
              str([cap.released for cap in module.created]))
        check("...and no capture thread is still spinning",
              wait_for(lambda: not [t for t in threading.enumerate()
                                    if t.name == "kayra-camera"], timeout=2.0))
        camera.stop()
    finally:
        camera_module.RECOVERY_PAUSE_S = original_pause
        restore_cv2(previous)

    # Dies and COMES BACK: it must recover and keep going.
    module, previous = install_fake_cv2(fail_after=6, recover_at=12)
    camera_module.RECOVERY_PAUSE_S = 0.02
    try:
        camera = source()
        camera.start()
        recovered = wait_for(lambda: camera.recoveries >= 1, timeout=5.0)
        check("a camera that comes back is recovered", recovered,
              f"recoveries={camera.recoveries}")
        check("...and returns to ACTIVE",
              wait_for(lambda: camera.status == CameraStatus.ACTIVE, timeout=2.0),
              camera.status)
        check("...and delivers frames again",
              wait_for(lambda: camera.preview_frame() is not None, timeout=2.0))
        camera.stop()
    finally:
        camera_module.RECOVERY_PAUSE_S = original_pause
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     6. RELEASE ON EVERY PATH                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_release():
    print_system("\n[6] The device is released on every exit path")

    from kayra.input.gesture.controller import GestureController
    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.pointer import PointerController, RecordingInjector

    module, previous = install_fake_cv2()
    try:
        cfg = GestureConfig()
        cfg.normalize()
        controller = GestureController(cfg, pointer=PointerController(RecordingInjector()))

        ok, _ = controller.set_camera(True)
        check("the controller starts the camera", ok and controller.camera_enabled())
        controller.shutdown()
        check("shutdown() releases the device", module.created[0].released)
        check("...and reports the camera off", not controller.camera_enabled())
        check("...and leaves no gesture thread",
              wait_for(lambda: not [t for t in threading.enumerate()
                                    if t.name in ("kayra-gesture", "kayra-camera")],
                       timeout=2.0),
              str([t.name for t in threading.enumerate() if t.name.startswith("kayra-")]))
        controller.shutdown()
        check("shutdown() twice is safe", True)

        # Turning the camera off through the ordinary switch must release it too.
        controller = GestureController(cfg, pointer=PointerController(RecordingInjector()))
        controller.set_camera(True)
        opened = module.created[-1]
        controller.set_camera(False)
        check("set_camera(False) releases the device", opened.released)
        check("...and is idempotent", controller.set_camera(False)[0])
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     7. STATUS AND TELEMETRY                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_telemetry():
    print_system("\n[7] Status transitions and telemetry")
    from kayra.input.gesture.camera import CameraStatus

    module, previous = install_fake_cv2()
    try:
        camera = source()
        seen = []
        camera.subscribe(lambda status, prev: seen.append((prev, status)))
        camera.start()
        check("subscribers are told about the transition to ACTIVE",
              any(status == CameraStatus.ACTIVE for _, status in seen), str(seen))
        check("...and about STARTING first",
              any(status == CameraStatus.STARTING for _, status in seen), str(seen))

        # A broken listener must never be able to stop the capture thread.
        camera.subscribe(lambda status, prev: 1 / 0)
        camera.stop()
        check("a listener that raises does not break the camera",
              camera.status == CameraStatus.OFF, camera.status)

        camera.start()
        time.sleep(0.15)
        data = camera.telemetry()
        for key in ("status", "device", "fps", "frames", "dropped", "read_failures",
                    "recoveries", "error"):
            check(f"telemetry carries {key}", key in data, str(sorted(data)))
        check("telemetry counts captured frames", data["frames"] > 0, str(data["frames"]))
        camera.stop()

        camera.unsubscribe(lambda: None)     # unsubscribing something absent is safe
        check("unsubscribing an unknown listener is safe", True)
    finally:
        restore_cv2(previous)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          8. THE REAL CAMERA                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_cycling():
    """
    Camera and gesture toggled on and off many times. Does anything grow?

    THE LEAK THIS SECTION EXISTS FOR. The first version of this module hardcoded OpenCV's
    `CAP_DSHOW` backend on the strength of a performance claim that had never been measured.
    When the resource check was finally run against the real camera, DSHOW turned out to leak
    **17 threads per open/close cycle** and never reclaim them: twenty toggles from the Home
    page took the process from 5 threads and 25 MB to 334 threads and 237 MB. It was also
    three times SLOWER to open than the alternative, so the original justification was wrong
    in both directions.

    Measured, one clean process per backend, 8 cycles each:

        CAP_DSHOW   17.2 threads/cycle   1655 ms to open
        CAP_MSMF     2.0 threads/cycle    529 ms
        CAP_ANY      2.0 threads/cycle    516 ms

    The default is now AUTO, and after the change twenty full cycles leave the thread count
    FLAT at 33-34 with RSS stable around 127 MB.

    The fake camera here cannot reproduce a driver's leak — that is what `--live` is for. What
    it CAN prove, and does, is that the runtime's own threads and collections do not
    accumulate, which is the half of the problem this code owns.
    """
    print_system("\n[8] Repeated on/off cycling")

    from kayra.input.gesture.controller import GestureController
    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.pointer import PointerController, RecordingInjector

    module, previous = install_fake_cv2()
    try:
        cfg = GestureConfig()
        cfg.normalize()
        controller = GestureController(cfg, pointer=PointerController(RecordingInjector()))

        baseline = _kayra_thread_count()
        for _ in range(10):
            ok, detail = controller.set_camera(True)
            if not ok:
                check("camera cycling stays healthy", False, str(detail))
                break
            controller.set_camera(False)

        check("ten camera cycles leave no camera thread behind",
              wait_for(lambda: _kayra_thread_count() == baseline, timeout=3.0),
              f"{_kayra_thread_count()} vs {baseline} kayra threads")
        check("...and every capture that was opened was released",
              all(cap.released for cap in module.created),
              f"{sum(1 for c in module.created if not c.released)} still held")
        check("...and exactly ten captures were opened, not more",
              len(module.created) == 10, str(len(module.created)))

        # The runtime's own collections must not accumulate across cycles either.
        growing = []
        for owner, name in ((controller, "controller"), (controller.camera, "camera"),
                            (controller.recognizer, "recogniser")):
            for key, value in vars(owner).items():
                if isinstance(value, (list, dict, set)) and len(value) > 32:
                    growing.append(f"{name}.{key}={len(value)}")
        check("no collection grew across ten cycles", not growing, str(growing))

        controller.shutdown()
        check("shutdown after cycling leaves nothing running",
              wait_for(lambda: _kayra_thread_count() == 0, timeout=3.0),
              str([t.name for t in threading.enumerate() if t.name.startswith("kayra-")]))
    finally:
        restore_cv2(previous)


def _kayra_thread_count():
    return len([t for t in threading.enumerate()
                if t.name in ("kayra-camera", "kayra-gesture")])


def section_live_cycling():
    """
    The same cycling, against the REAL camera, where a driver leak can actually show up.

    `--live` only. This is the check that found the DSHOW leak, and it is the only kind that
    could have: a fake capture cannot leak a driver's threads.
    """
    print_system("\n[9] Repeated cycling against the real camera")
    import gc

    try:
        import psutil
    except ImportError:
        print_info("      psutil unavailable; skipping.")
        return

    from kayra.input.gesture.controller import GestureController
    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.pointer import PointerController, RecordingInjector

    process = psutil.Process()
    cfg = GestureConfig.from_env()
    controller = GestureController(cfg, pointer=PointerController(RecordingInjector()))

    ok, detail = controller.set_camera(True)
    if not ok:
        print_info(f"      No usable camera ({detail}); skipping.")
        return
    controller.set_camera(False)

    gc.collect()
    settled = process.num_threads()
    settled_rss = process.memory_info().rss / 1e6

    for _ in range(10):
        controller.set_camera(True)
        time.sleep(0.3)
        controller.set_camera(False)
        time.sleep(0.15)

    gc.collect()
    after = process.num_threads()
    after_rss = process.memory_info().rss / 1e6
    per_cycle = (after - settled) / 10.0

    print_info(f"      after one warm-up cycle: {settled} threads, {settled_rss:.0f} MB")
    print_info(f"      after ten more:          {after} threads, {after_rss:.0f} MB "
               f"({per_cycle:+.1f} threads/cycle)")
    print_info(f"      backend: {cfg.camera_backend}")

    # The DSHOW leak was 17 threads per cycle. Anything approaching that is a regression, and
    # the bound is deliberately generous so it fails on a leak rather than on a busy machine.
    check("repeated camera cycling does not leak threads", per_cycle < 3.0,
          f"{per_cycle:+.1f} threads/cycle (DSHOW leaked 17.2)")
    check("...and does not leak memory", (after_rss - settled_rss) < 60.0,
          f"{after_rss - settled_rss:+.0f} MB over ten cycles")

    controller.shutdown()
    check("the real camera is released after cycling", not controller.camera_enabled())


def section_live():
    print_system("\n[8] Live camera")
    from kayra.input.gesture.camera import CameraStatus

    camera = source()
    ok, detail = camera.start()
    if not ok:
        print_info(f"      No usable camera on this machine ({detail}); skipping.")
        return
    check("the real camera opens", ok, str(detail))
    time.sleep(2.0)
    data = camera.telemetry()
    print_info(f"      device={data['device']} fps={data['fps']} frames={data['frames']} "
               f"dropped={data['dropped']} failures={data['read_failures']}")
    check("the real camera delivers frames", data["frames"] > 10, str(data["frames"]))
    check("...at a usable rate", data["fps"] >= 10.0, f"{data['fps']} FPS")
    check("...with no read failures", data["read_failures"] == 0, str(data["read_failures"]))
    frame = camera.preview_frame()
    check("a real frame has a shape", frame is not None and hasattr(frame, "shape"),
          str(getattr(frame, "shape", None)))
    camera.stop()
    check("the real camera releases", camera.status == CameraStatus.OFF)


def main():
    live = "--live" in sys.argv
    print_banner("CAMERA RUNTIME DIAGNOSTIC",
                 "one owner · newest frame · bounded recovery")
    section_lifecycle()
    section_mailbox()
    section_slow_consumer()
    section_failures()
    section_recovery()
    section_release()
    section_telemetry()
    section_cycling()
    if live:
        section_live_cycling()
        section_live()
    else:
        print_info("\n      Run with --live to exercise the real camera.")

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All camera runtime checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
