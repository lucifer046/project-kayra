# ┌────────────────────────────────────────────────────────────────────────┐
# │                        kayra.input.gesture                             │
# │                   Hand Gesture Control — package API                   │
# └────────────────────────────────────────────────────────────────────────┘
"""
Hand gesture control: camera in, pointer out.

    from kayra.input.gesture import get_gesture_controller
    controller = get_gesture_controller()
    controller.set_gesture(True)

THE PIPELINE, AND WHICH MODULE OWNS EACH STAGE

    camera.py       capture           one VideoCapture, one thread, a single-frame mailbox
    detector.py     detection         MediaPipe, one hand, deterministic primary selection
    features.py     normalisation     every distance divided by hand scale
    filters.py      stabilisation     One Euro + dead-zone + outlier gate + velocity ceiling
    state_machine.py temporal logic   hysteresis gates, dwell times, action arbitration
    pointer.py      injection         user32, split rate limits for continuous vs discrete
    controller.py   the runtime       lifecycle, threading, preview, telemetry, events
    config.py       every threshold   read once from .env, clamped, normalized

WHY THIS LIVES IN `input` AND NOT IN `automation`

The camera is a capture device and this package's job is to work out what the user MEANT,
exactly as the speech package does. It reaches the desktop through `pointer.py` — 25 lines of
`user32` with no shell, no subprocess and no target resolution — rather than through
`automation.windows`, because `automation` must never import `input` (importing the STT engine
boots a browser, and the rule that prevents that is structural) and because the automation
pipeline's normalize/policy/resolve/plan/execute stages are the right cost for a sentence and
the wrong cost for a 30Hz pointer update. A SPOKEN "click" still goes through automation,
unchanged.

IMPORTING THIS PACKAGE IS FREE. No camera, no MediaPipe, no OpenCV, no thread. `cv2` and
`mediapipe` are imported inside `CameraSource._open` and `HandDetector.start`, so a machine
without either can still import, inspect and test everything here — and a user who never turns
gesture control on never pays their ~950ms of import time.
"""

from kayra.input.gesture.config import (
    GestureConfig, ENV_KEYS, camera_enabled_default, gesture_enabled_default,
)
from kayra.input.gesture.camera import CameraSource, CameraStatus
from kayra.input.gesture.controller import (
    GestureController, GestureRuntimeState, RUNTIME_LABELS,
    get_gesture_controller, gesture_controller_if_running, reset_gesture_controller,
)
from kayra.input.gesture.detector import HandDetector, accelerator_report, probe_accelerators
from kayra.input.gesture.features import (
    FeatureExtractor, HandFeatures, pick_primary, LANDMARK_COUNT,
)
from kayra.input.gesture.filters import (
    OneEuroFilter, OutlierGate, PointerStabilizer, Hysteresis, RateLimiter,
)
from kayra.input.gesture.pointer import PointerController, RecordingInjector
from kayra.input.gesture.state_machine import (
    GestureRecognizer, GestureDecision, GestureState, ALL_STATES, STATE_LABELS,
)

__all__ = [
    "GestureConfig", "ENV_KEYS", "camera_enabled_default", "gesture_enabled_default",
    "CameraSource", "CameraStatus",
    "GestureController", "GestureRuntimeState", "RUNTIME_LABELS",
    "get_gesture_controller", "gesture_controller_if_running", "reset_gesture_controller",
    "HandDetector", "accelerator_report", "probe_accelerators",
    "FeatureExtractor", "HandFeatures", "pick_primary", "LANDMARK_COUNT",
    "OneEuroFilter", "OutlierGate", "PointerStabilizer", "Hysteresis", "RateLimiter",
    "PointerController", "RecordingInjector",
    "GestureRecognizer", "GestureDecision", "GestureState", "ALL_STATES", "STATE_LABELS",
]
