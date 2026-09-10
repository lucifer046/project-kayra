# ┌────────────────────────────────────────────────────────────────────────┐
# │                    kayra.input.gesture.__main__                        │
# │            Standalone runner — the same runtime, no UI                 │
# └────────────────────────────────────────────────────────────────────────┘
"""
    python -m kayra.input.gesture            # run gesture control until Ctrl+C
    python -m kayra.input.gesture --doctor   # camera, detector and accelerator report only

Kept because `python -m kayra.input.gesture` has always worked, and because a gesture problem
is much easier to diagnose without a window in the way.

THIS IS NOT A SECOND IMPLEMENTATION. It drives exactly the same `GestureController` the UI and
the voice commands drive — the v1 module was a standalone engine with its own camera loop, its
own state machine and its own mouse code, and that is precisely the duplication the single
ownership rule exists to prevent. Everything here is argument parsing and a `time.sleep`.
"""

import sys
import time

from kayra.core.config import load_environment
from kayra.core.logbus import Subsystem, success, error
from kayra.utils import print_banner, print_info, print_warning
from kayra.input.gesture.controller import get_gesture_controller


def _doctor(controller) -> int:
    """Reports what the hardware and the model stack can actually do, then exits."""
    print_banner("KAYRA GESTURE DOCTOR", "camera · detector · accelerator")

    ok, detail = controller.camera.start()
    print_info(f"Camera: {'OK — ' + str(detail) if ok else 'FAILED — ' + str(detail)}")
    if ok:
        frame = controller.camera.preview_frame()
        print_info(f"First frame: {'received' if frame is not None else 'none'}")

    ready, backend = controller.detector.start()
    print_info(f"Detector: {'OK — ' + str(backend) if ready else 'FAILED — ' + str(backend)}")

    report = controller.telemetry()["accelerator"]
    print_info(f"Accelerator: {report['delegate']}")
    print_info(f"Reason: {report['reason']}")
    print_info(f"Configuration: {controller.config.summary()}")

    controller.shutdown()
    return 0 if ok and ready else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    load_environment()
    controller = get_gesture_controller()

    if "--doctor" in argv:
        return _doctor(controller)

    print_banner("KAYRA AIR CURSOR", "index moves · pinch clicks · two fingers scroll")
    ok, detail = controller.set_gesture(True)
    if not ok:
        error(Subsystem.GESTURE, f"Could not start: {detail}")
        return 1

    print_info("Running. Press Ctrl+C to stop.")
    verbose = "--verbose" in argv or "--debug" in argv
    try:
        while True:
            time.sleep(2.0)
            if verbose:
                controller.log_diagnostics()
    except KeyboardInterrupt:
        print_warning("Stopping…")
    finally:
        controller.shutdown()
        success(Subsystem.GESTURE, "Gesture control stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
