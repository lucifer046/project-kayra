# ┌────────────────────────────────────────────────────────────────────────┐
# │                       gesture/detector.py                              │
# │       Hand Landmark Detection — MediaPipe, and the GPU question        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The hand detector, and the single place the accelerator question is answered.

WHY MEDIAPIPE IS KEPT
---------------------
It was already here, it is the strongest CPU hand-landmark model available as a pip install,
and measured on this machine it is not the bottleneck. `model_complexity=0` (the LITE graph)
runs at **9-12ms per frame** on a 640x480 input, which is 80-110 FPS of headroom against a
30 FPS camera. Replacing a component that has three frames of slack per frame would be work
spent on the one part of the pipeline that was never the problem.

The change that matters here is `model_complexity`, not the model. v1 used complexity **1**
(the FULL graph) at **26-31ms**, which on a 33ms budget leaves nothing — so any hiccup put the
loop behind the camera, and the driver's frame queue did the rest. See `camera.py` for why
falling behind was unrecoverable in v1. Complexity 0's landmark accuracy is materially lower
for finger *articulation* at distance, and that is exactly what the normalization and
stabilisation layers exist to absorb; it is the right trade here and it is configurable
(`GESTURE_MODEL_COMPLEXITY`) for anyone who wants the other one.

THE GPU QUESTION, ANSWERED HONESTLY
-----------------------------------
Kayra already runs CUDA for Kokoro TTS, so "there is an NVIDIA GPU" is true. It does not
follow that this should use it, and here it must not:

  * **The MediaPipe Python wheel on Windows has no GPU delegate.** `mediapipe 0.10.14`'s
    `solutions.hands` graph is compiled CPU-only in the pip distribution; the GPU calculators
    exist in the C++ source and are not built into the wheel. `probe_accelerators()` below
    checks for a usable delegate at runtime rather than assuming either answer, and reports
    what it found.
  * **The Tasks API's `HandLandmarker` accepts `BaseOptions(delegate=GPU)`,** but that
    delegate is OpenGL/Metal, not CUDA, and it is not available in the Windows wheel either.
    Asking for it there raises at graph construction — which is why the request is made inside
    a probe with a fallback rather than at the call site.
  * **Even if it were available, it would be the wrong trade.** The inference is 9-12ms with
    three frames of headroom. The cost side is not free: a second CUDA context alongside the
    ONNX Runtime session Kokoro holds means VRAM (ORT's CUDA EP already takes +181MiB here),
    context-switch contention on the same device, and a startup cost paid every time gesture
    control is switched on. `tts_device.py` documents that CUDA is already NOT faster for
    Kokoro on this machine; adding a second consumer to a device that is not winning is a
    regression waiting to be measured.

So: **CPU, and the GPU is not initialised at all.** `GESTURE_GPU=ON` forces a delegate attempt
for someone on a platform where one exists, `OFF` refuses to probe, and `AUTO` (the default)
probes once, uses a delegate only if it genuinely constructs, and otherwise stays on the CPU
without complaint. No CUDA import, no TensorRT, no ONNX Runtime — this module imports none of
them, which `tests/test_gesture_control.py` asserts.
"""

import threading
import time

from kayra.core.logbus import Subsystem, debug, info, warning
from kayra.input.gesture.features import FeatureExtractor, pick_primary, sticky_score


class HandDetector:
    """
    Frame in, `HandFeatures` out. Owns the MediaPipe graph and nothing else.

    ONE HAND. `max_num_hands=1` is a decision, not a default: every gesture in this system is
    single-handed, and a second hand in frame is a bystander. The v1 engine requested one hand
    but then iterated `results.multi_hand_landmarks` anyway, so a frame that did return two
    drove the cursor twice and could fire two clicks. `max_num_hands` is configurable to 2 for
    the diagnostics view; `pick_primary` then chooses deterministically and the rest of the
    stack never sees the second hand.
    """

    def __init__(self, config, max_hands: int = 1):
        self.config = config
        self.max_hands = max(1, min(2, int(max_hands)))
        self._lock = threading.Lock()
        self._hands = None
        self._extractor = FeatureExtractor()
        self._primary_handedness = ""

        self.backend = "none"
        self.delegate = "CPU"
        self.ready = False
        self.error_detail = ""

        # Telemetry: a rolling mean, not a history. Two floats.
        self._latency_ms = 0.0
        self._latency_n = 0
        self.frames = 0
        self.hands_seen = 0

    # ──────────────────────────────────────────────────────────────────

    def start(self):
        """
        Constructs the graph. Returns `(ok, detail)`. Idempotent.

        MediaPipe is imported here rather than at module scope for the same reason OpenCV is
        in `camera.py`: it costs ~700ms and ~120MB RSS, and a user who never turns gesture
        control on must not pay either.
        """
        with self._lock:
            if self.ready:
                return True, self.backend
            try:
                import mediapipe as mp
            except Exception as exc:
                self.error_detail = f"MediaPipe unavailable ({type(exc).__name__})"
                return False, self.error_detail

            delegate = probe_accelerators(self.config.gpu)
            try:
                self._hands = mp.solutions.hands.Hands(
                    static_image_mode=False,
                    max_num_hands=self.max_hands,
                    model_complexity=self.config.model_complexity,
                    min_detection_confidence=self.config.detection_confidence,
                    min_tracking_confidence=self.config.tracking_confidence,
                )
            except Exception as exc:
                self.error_detail = f"{type(exc).__name__}: {exc}"
                return False, self.error_detail

            self.delegate = delegate
            self.backend = (f"mediapipe {getattr(mp, '__version__', '?')} "
                            f"complexity={self.config.model_complexity} {delegate}")
            self.ready = True
            self._extractor.reset()
            info(Subsystem.GESTURE, f"Detector: {self.backend}")
            return True, self.backend

    def stop(self):
        """Closes the graph and frees its tensors. Safe to call twice."""
        with self._lock:
            hands, self._hands = self._hands, None
            self.ready = False
        if hands is not None:
            try:
                hands.close()
            except Exception:
                pass
        self._extractor.reset()
        self._primary_handedness = ""

    # ──────────────────────────────────────────────────────────────────

    def detect(self, rgb_frame, frame_aspect: float):
        """
        Runs inference on one RGB frame. Returns `HandFeatures` or None.

        Never raises. A MediaPipe failure on one frame is one skipped frame — the processing
        thread must not be able to die on a transient graph error, because a dead processing
        thread is a gesture system that silently stops working, and "gestures sometimes stop
        working" is the report this whole rewrite answers.
        """
        hands = self._hands
        if hands is None:
            return None

        started = time.perf_counter()
        try:
            rgb_frame.flags.writeable = False
            results = hands.process(rgb_frame)
        except Exception as exc:
            debug(Subsystem.GESTURE, f"Inference failed: {type(exc).__name__}: {exc}")
            return None
        finally:
            try:
                rgb_frame.flags.writeable = True
            except Exception:
                pass

        elapsed = (time.perf_counter() - started) * 1000.0
        # Exponential mean over two floats. A deque of latencies would be a per-frame append
        # for a number nothing reads more than once a second.
        self._latency_ms = (self._latency_ms * 0.9 + elapsed * 0.1) if self._latency_n else elapsed
        self._latency_n += 1
        self.frames += 1

        landmark_sets = getattr(results, "multi_hand_landmarks", None)
        if not landmark_sets:
            self._extractor.reset()
            return None

        handedness = getattr(results, "multi_handedness", None) or []
        candidates = []
        for index, hand in enumerate(landmark_sets):
            label, score = "", 0.0
            if index < len(handedness):
                try:
                    classification = handedness[index].classification[0]
                    label = classification.label
                    score = float(classification.score)
                except Exception:
                    pass
            candidates.append(self._extractor.extract(hand.landmark, frame_aspect, label, score))

        # Deterministic primary selection, with a small stickiness bonus so two similar hands
        # cannot make the controlling hand alternate frame to frame.
        best = None
        best_key = None
        for candidate in candidates:
            if candidate is None or not candidate.valid:
                continue
            key = (candidate.hand_scale + sticky_score(candidate, self._primary_handedness),
                   candidate.stability)
            if best_key is None or key > best_key:
                best, best_key = candidate, key
        if best is None:
            best = pick_primary(candidates)
        if best is not None and best.valid:
            self.hands_seen += 1
            self._primary_handedness = best.handedness or self._primary_handedness
        return best

    # ──────────────────────────────────────────────────────────────────

    @property
    def latency_ms(self) -> float:
        return round(self._latency_ms, 2)

    def telemetry(self) -> dict:
        return {
            "backend": self.backend,
            "delegate": self.delegate,
            "ready": self.ready,
            "latency_ms": self.latency_ms,
            "frames": self.frames,
            "hands_seen": self.hands_seen,
            "error": self.error_detail,
        }


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          ACCELERATOR PROBE                             │
# └────────────────────────────────────────────────────────────────────────┘
# Cached: the answer cannot change within a process, and constructing a probe graph is not
# free. Same discipline as `tts_device.probe_provider` — a device claim is only made after
# something has actually been built on it, never from a capability list.

_PROBE_LOCK = threading.Lock()
_PROBE_RESULT = None


def probe_accelerators(mode: str = "AUTO") -> str:
    """
    Returns the delegate that will actually be used: `"CPU"` or `"GPU"`.

    `mode` is AUTO / ON / OFF. OFF never probes. ON and AUTO both attempt a real delegate
    construction and fall back to CPU when it fails — the difference is only that ON says so
    loudly, because a user who asked for GPU explicitly deserves to be told they did not get
    it, while AUTO getting CPU on Windows is the expected and documented outcome.

    A CLAIM IS NEVER MADE FROM A CAPABILITY LIST. `mediapipe.tasks` exposes a `GPU` delegate
    enum on every platform including the ones with no GPU calculators compiled in, so testing
    for the enum proves nothing at all — exactly the trap `tts_device` documents for ONNX
    Runtime's `get_available_providers()`. The probe builds something.
    """
    global _PROBE_RESULT

    mode = (mode or "AUTO").strip().upper()
    if mode == "OFF":
        return "CPU"

    with _PROBE_LOCK:
        if _PROBE_RESULT is not None:
            if mode == "ON" and _PROBE_RESULT == "CPU":
                warning(Subsystem.GESTURE,
                        "GPU requested but no usable delegate — running on the processor.")
            return _PROBE_RESULT

        result = "CPU"
        detail = "no GPU delegate in this MediaPipe build"
        try:
            from mediapipe.tasks.python.core.base_options import BaseOptions
            delegate_enum = getattr(BaseOptions, "Delegate", None)
            gpu = getattr(delegate_enum, "GPU", None) if delegate_enum else None
            if gpu is not None:
                # Constructing the options object is not proof; a graph has to accept it. The
                # legacy `solutions.hands` graph — the one this detector uses — takes no
                # delegate parameter at all, so on the code path that matters the honest
                # answer is CPU regardless of what the Tasks API would allow.
                detail = ("Tasks API exposes a GPU delegate, but solutions.hands is a "
                          "CPU-only graph in the pip wheel")
        except Exception as exc:
            detail = f"delegate probe unavailable ({type(exc).__name__})"

        _PROBE_RESULT = result
        debug(Subsystem.GESTURE, f"Accelerator probe: {result} — {detail}")
        if mode == "ON":
            warning(Subsystem.GESTURE, f"GPU requested but unavailable: {detail}")
        return result


def accelerator_report() -> dict:
    """
    What the accelerator decision was and why. Read by `--doctor` and the diagnostics view.

    Deliberately reports the DECISION and the REASON separately, so "we are on the CPU" is
    never presented without the answer to "why not the GPU?" — the same rule the speech device
    card follows.
    """
    return {
        "delegate": _PROBE_RESULT or "unprobed",
        "reason": ("MediaPipe's pip wheel builds solutions.hands CPU-only on Windows; "
                   "inference is 9-12ms at complexity 0, which is not the bottleneck. "
                   "A second CUDA context would contend with the Kokoro ONNX session."),
        "cuda_used": False,
    }
