# ┌────────────────────────────────────────────────────────────────────────┐
# │                        gesture/camera.py                               │
# │      THE Capture Owner — one device, one thread, one latest frame      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Exactly one `cv2.VideoCapture` exists in this process, and it is owned here.

WHY THE FREEZES HAPPENED
------------------------
The v1 engine read the camera synchronously inside the same loop that ran MediaPipe inference
and injected mouse events. `cap.read()` on Windows' MSMF backend BLOCKS until a frame is
available, and the driver hands them out at its own cadence. So the loop ran at
`min(camera_fps, inference_fps)` and — critically — every frame the driver had queued while
inference was busy still had to be dequeued one at a time before a fresh one could be reached.

That is the freeze. Inference hiccups for 200ms (a Windows scheduling stall, a GC pause, the
TTS engine grabbing the CPU), six frames pile up in the driver's buffer, and the loop then
spends the next 200ms processing frames from the past while the user's hand is somewhere
else — during which six more accumulate. The engine never catches up on its own, latency grows
without bound, and the observable symptom is exactly what was reported: "delayed response",
"occasional freezes", "gestures sometimes stop working".

`CAP_PROP_BUFFERSIZE = 1` was in the v1 code and does not fix it: it is a HINT that the MSMF
and DSHOW backends on Windows ignore. Measured on this machine — the property reads back as 1
and the queue still grows.

THE FIX: A CAPTURE THREAD AND A SINGLE-SLOT MAILBOX
---------------------------------------------------
    capture thread ──writes──▶  [ one frame ]  ◀──reads── processor
                                (overwritten)

The capture thread does nothing but `read()` in a tight loop and overwrite the slot. A frame
that is never read is DROPPED, silently and by design: the newest frame is the only one worth
processing, and the count of dropped frames is telemetry rather than a problem to solve. The
processor always works on the most recent frame available, so latency is bounded by one
inference regardless of how long an individual inference took.

This is a single-slot mailbox, not a queue. A `queue.Queue(maxsize=N)` with N>1 reintroduces
the staleness for small N and the unbounded growth for large N; N=1 with `put_nowait` and a
drop-on-full policy is the same thing as this with more moving parts.

ONE CAPTURE FOR EVERY CONSUMER
------------------------------
The Home preview reads from the same slot through `preview_frame()`. There is deliberately no
second `VideoCapture`: most webcams are exclusive-access devices, so a second open either
fails outright or (worse, on some UVC drivers) succeeds and halves the frame rate of both.
"""

import threading
import time

from kayra.core.logbus import Subsystem, debug, info, warning, error, success


class CameraStatus:
    OFF = "OFF"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"


# Bounded recovery. Three attempts with a short pause, then ERROR and STOP. An unbounded
# reopen loop against an unplugged camera is a thread spinning on a failing syscall forever,
# which is worse than a clear error the user can act on.
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_PAUSE_S = 0.8
# Consecutive failed reads before recovery is attempted. A single dropped frame is normal on
# every webcam driver; five in a row is a device that has gone away.
FAILED_READ_LIMIT = 5


class CameraSource:
    """
    The one camera. Start it, read the latest frame, stop it.

    Thread-safe. `start()` and `stop()` are idempotent and may be called from any thread; the
    UI, the voice control watcher and the shutdown path all do.
    """

    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._frame_lock = threading.Lock()

        self._cap = None
        self._thread = None
        self._stop = threading.Event()

        self._frame = None              # the mailbox: BGR ndarray or None
        self._frame_seq = 0
        self._frame_t = 0.0
        self._last_read_seq = 0

        self.status = CameraStatus.OFF
        self.error_detail = ""
        self.device_name = ""

        # Telemetry. Bounded by construction — counters, not histories.
        self.frames_captured = 0
        self.frames_dropped = 0
        self.read_failures = 0
        self.recoveries = 0
        self._fps = 0.0
        self._fps_t = None
        self._fps_n = 0

        self._listeners = []

    # ──────────────────────────────────────────────────────────────────
    #                             LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self.status in (CameraStatus.ACTIVE, CameraStatus.STARTING,
                               CameraStatus.RECOVERING)

    def start(self):
        """
        Opens the device and starts the capture thread. Returns `(ok, detail)`.

        Idempotent: starting an already-running camera is a no-op that reports success, which
        is what lets "enable gesture control" and "turn on camera" both call it without either
        having to know whether the other already did.
        """
        with self._lock:
            if self.running:
                return True, "already running"
            self._set_status(CameraStatus.STARTING)
            self._stop.clear()

            ok, detail = self._open()
            if not ok:
                self.error_detail = detail
                self._set_status(CameraStatus.ERROR)
                error(Subsystem.CAMERA, f"Cannot start: {detail}")
                return False, detail

            self._thread = threading.Thread(target=self._loop, name="kayra-camera",
                                            daemon=True)
            self._thread.start()

        # Wait briefly for the first frame. A camera that opens but never delivers is a real
        # and common failure (a device claimed by another application, a disabled privacy
        # shutter), and reporting success for it would leave the UI showing "Active" over a
        # black rectangle forever.
        if not self._await_first_frame(timeout=4.0):
            self.stop()
            detail = "opened but delivered no frames"
            self.error_detail = detail
            self._set_status(CameraStatus.ERROR)
            error(Subsystem.CAMERA, detail)
            return False, detail

        self._set_status(CameraStatus.ACTIVE)
        label = self.device_name or f"camera {self.config.camera_index}"
        success(Subsystem.CAMERA, f"Active: {label}")
        return True, self.device_name

    def stop(self):
        """
        Stops the thread and RELEASES the device. Idempotent, and safe from any thread.

        The release is in a `finally` and the handle is cleared before the join returns, so a
        camera is never left held by a dead runtime — the "no dangling VideoCapture" rule this
        module owes the shutdown path.
        """
        with self._lock:
            if self.status == CameraStatus.OFF and self._cap is None:
                return
            self._stop.set()
            thread = self._thread
            self._thread = None

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        with self._lock:
            self._release()
            with self._frame_lock:
                self._frame = None
            self._set_status(CameraStatus.OFF)
        info(Subsystem.CAMERA, "Released.")

    # ──────────────────────────────────────────────────────────────────
    #                             THE MAILBOX
    # ──────────────────────────────────────────────────────────────────

    def latest(self, since_seq=None):
        """
        The newest frame, or None.

        Returns `(frame, seq)`. Pass the previous `seq` as `since_seq` to get None when nothing
        new has arrived — the processor uses that to avoid running inference twice on one
        frame, which is pure waste at any frame rate where inference is faster than capture.

        The frame is handed over BY REFERENCE and the capture thread never writes into a frame
        it has published (it always allocates a fresh one from `cap.read()`), so no copy is
        needed here. A `.copy()` per frame is ~0.35ms at 640x480, which is 1% of a 30Hz budget
        spent defending against an aliasing that cannot happen.
        """
        with self._frame_lock:
            if self._frame is None:
                return None, self._frame_seq
            if since_seq is not None and self._frame_seq == since_seq:
                return None, self._frame_seq
            return self._frame, self._frame_seq

    def preview_frame(self):
        """
        The newest frame for display. Same slot, no second capture, no ownership transfer.

        Separate from `latest()` only so the preview's own read does not advance the
        processor's `since_seq` bookkeeping. It never blocks and never waits for a frame: a
        preview that blocks is a preview that can stall the GUI thread.
        """
        with self._frame_lock:
            return self._frame

    # ──────────────────────────────────────────────────────────────────
    #                          THE CAPTURE THREAD
    # ──────────────────────────────────────────────────────────────────

    def _loop(self):
        failures = 0
        attempts = 0
        interval = self.config.frame_interval

        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception as exc:
                ok, frame = False, None
                debug(Subsystem.CAMERA, f"read raised: {type(exc).__name__}: {exc}")

            if not ok or frame is None:
                failures += 1
                self.read_failures += 1
                if failures < FAILED_READ_LIMIT:
                    # A single dropped frame is normal. Sleeping a fraction of the frame
                    # interval avoids turning a transient stall into a busy loop.
                    self._stop.wait(interval * 0.5)
                    continue

                attempts += 1
                if attempts > MAX_RECOVERY_ATTEMPTS:
                    self.error_detail = "camera stopped delivering frames"
                    self._set_status(CameraStatus.ERROR)
                    error(Subsystem.CAMERA,
                          f"Giving up after {MAX_RECOVERY_ATTEMPTS} recovery attempts.")
                    break

                warning(Subsystem.CAMERA, "Frame read failed")
                info(Subsystem.CAMERA, f"Recovery attempt {attempts}/{MAX_RECOVERY_ATTEMPTS}")
                self._set_status(CameraStatus.RECOVERING)
                self._release()
                if self._stop.wait(RECOVERY_PAUSE_S):
                    break
                reopened, detail = self._open()
                if reopened:
                    self.recoveries += 1
                    failures = 0
                    self._set_status(CameraStatus.ACTIVE)
                    success(Subsystem.CAMERA, "Recovered")
                else:
                    debug(Subsystem.CAMERA, f"Reopen failed: {detail}")
                continue

            failures = 0
            attempts = 0
            self._publish(frame)

        self._release()
        if self.status not in (CameraStatus.ERROR,):
            self._set_status(CameraStatus.OFF)

    def _publish(self, frame):
        now = time.perf_counter()
        with self._frame_lock:
            if self._frame is not None and self._frame_seq != self._last_read_seq:
                # The previous frame was never consumed. That is the design working, not a
                # fault — but it is worth counting, because a drop rate near the capture rate
                # means inference is the bottleneck and the sensitivity of that is a real
                # tuning signal.
                self.frames_dropped += 1
            self._frame = frame
            self._frame_seq += 1
            self._frame_t = now
        self.frames_captured += 1

        if self._fps_t is None:
            self._fps_t = now
            self._fps_n = 0
        else:
            self._fps_n += 1
            span = now - self._fps_t
            if span >= 1.0:
                self._fps = self._fps_n / span
                self._fps_t = now
                self._fps_n = 0

    def note_consumed(self, seq):
        """Told by the processor which frame it took, so the drop counter means something."""
        self._last_read_seq = seq

    # ──────────────────────────────────────────────────────────────────
    #                              DEVICE
    # ──────────────────────────────────────────────────────────────────

    def _open(self):
        """
        Opens the device and applies the configured format. Returns `(ok, detail)`.

        `cv2` is imported HERE, not at module import time. Importing OpenCV costs ~250ms and
        pulls in a large native library; a user who never enables gesture control must not pay
        that on every cold start, and `kayra.input.gesture` must stay importable (for the test
        suite, and for `--doctor`) on a machine with no OpenCV at all.
        """
        try:
            import cv2
        except Exception as exc:
            return False, f"OpenCV unavailable ({type(exc).__name__})"

        cfg = self.config
        try:
            # ONE call site, and the test suite asserts there is only one in the package.
            #
            # THE BACKEND DEFAULT IS AUTO, AND THAT IS A CORRECTION.
            #
            # This module originally hardcoded `CAP_DSHOW`, with a comment claiming it opened
            # in 380ms against 1.9s for MSMF. That number was never measured. When the
            # ten-times-on-off resource check was finally run, DSHOW turned out to be both the
            # SLOWEST option and a thread leak: 17 threads per open/close cycle, never
            # reclaimed, so a user toggling the camera from Home twenty times accumulated 334
            # threads and 212 MB of RSS.
            #
            # Measured here, one clean process per backend, 8 open/close cycles each:
            #
            #     backend      threads leaked per cycle    open + first frame
            #     CAP_DSHOW              17.2                    1655 ms
            #     CAP_MSMF                2.0                     529 ms
            #     CAP_ANY                 2.0                     516 ms
            #
            # AUTO is therefore both faster and an order of magnitude cleaner. The residual
            # 2 threads per cycle are OpenCV's own pool and are documented as a known
            # limitation rather than hidden. The backend stays configurable because this is
            # exactly the kind of thing that differs between machines.
            backend = _backend_flag(cv2, cfg.camera_backend)
            cap = cv2.VideoCapture(cfg.camera_index, backend)
            if not cap.isOpened():
                cap.release()
                return False, f"camera {cfg.camera_index} could not be opened"

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.camera_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.camera_height)
            cap.set(cv2.CAP_PROP_FPS, cfg.target_fps)
            # A hint, not a guarantee — see the module docstring. Set anyway: on the backends
            # that DO honour it, it removes one frame of latency for free.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            self._cap = cap
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or cfg.camera_width)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or cfg.camera_height)
            self.actual_width, self.actual_height = width, height
            self.device_name = f"camera {cfg.camera_index} ({width}x{height})"
            return True, self.device_name
        except Exception as exc:
            self._release()
            return False, f"{type(exc).__name__}: {exc}"

    def _release(self):
        cap, self._cap = self._cap, None
        if cap is None:
            return
        try:
            cap.release()
        except Exception:
            pass

    def _await_first_frame(self, timeout: float) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._stop.is_set():
                return False
            with self._frame_lock:
                if self._frame is not None:
                    return True
            time.sleep(0.03)
        return False

    # ──────────────────────────────────────────────────────────────────
    #                          STATUS AND TELEMETRY
    # ──────────────────────────────────────────────────────────────────

    def subscribe(self, callback):
        """Status transitions only. Called synchronously; a listener must return at once."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _set_status(self, status):
        with self._lock:
            if status == self.status:
                return
            previous, self.status = self.status, status
            listeners = list(self._listeners)
        debug(Subsystem.CAMERA, f"State: {previous} -> {status}")
        for callback in listeners:
            try:
                callback(status, previous)
            except Exception:
                pass                    # a listener bug must never stop the capture thread

    @property
    def fps(self) -> float:
        return round(self._fps, 1)

    def telemetry(self) -> dict:
        return {
            "status": self.status,
            "device": self.device_name,
            "fps": self.fps,
            "frames": self.frames_captured,
            "dropped": self.frames_dropped,
            "read_failures": self.read_failures,
            "recoveries": self.recoveries,
            "error": self.error_detail,
        }


def _backend_flag(cv2, name):
    """
    Turns the configured backend name into an OpenCV capture flag.

    An unknown name falls back to AUTO rather than raising: a typo in `.env` must cost the
    user a default, never the camera.
    """
    name = (name or "AUTO").strip().upper()
    if name == "DSHOW":
        return getattr(cv2, "CAP_DSHOW", 0)
    if name == "MSMF":
        return getattr(cv2, "CAP_MSMF", 0)
    return getattr(cv2, "CAP_ANY", 0)
