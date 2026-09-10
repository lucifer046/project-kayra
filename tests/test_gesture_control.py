# ┌────────────────────────────────────────────────────────────────────────┐
# │                      test_gesture_control.py                           │
# │      The Runtime — ownership, switches, voice, safety, and cost        │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_gesture_control.py — standalone diagnostic for the gesture RUNTIME.

    .venv\\Scripts\\python tests\\test_gesture_control.py

HARDWARE-FREE. A fake `cv2` and a fake `mediapipe` are installed in `sys.modules` before the
runtime imports either, and the pointer is a `RecordingInjector`, so a complete camera →
detection → gesture → click path runs end to end WITHOUT a webcam and WITHOUT moving the
developer's mouse. That last part is not a detail: a suite that moved the real pointer could
not be run while working.

WHAT IT ASSERTS THAT NOTHING ELSE CAN
-------------------------------------
Several of the rules this milestone rests on are STRUCTURAL, and a behavioural test cannot see
them. Those are asserted by walking the parsed source:

  * exactly ONE `VideoCapture` call site in the whole package
  * exactly ONE hand-graph construction site
  * gesture code NEVER touches voice state (`set_listening`, `set_sleeping`, the STT/TTS
    engines, the voice state machine)
  * no UI module imports `cv2` or `mediapipe`
  * no per-frame logging
  * no CUDA/TensorRT/ONNX-Runtime import anywhere in the gesture package
  * no `shell=True`, no `os.system`, no process termination by name

SECTIONS
  1. Configuration: clamps, presets, and the hysteresis invariants.
  2. Single ownership, by AST.
  3. Gesture code never touches voice state, by AST.
  4. The two switches, and the combination that must not exist.
  5. The full pipeline, end to end, with a fake camera and a fake detector.
  6. Nothing acts after OFF.
  7. Failures: no camera, no detector.
  8. The preview.
  9. Voice commands.
 10. Shutdown, and the app wiring.
 11. The accelerator decision.
 12. Cost and boundedness.
"""

import os
import re
import sys
import ast
import glob
import time
import types
import inspect
import threading

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system

# The synthetic hands live in the state-machine suite; there is one fixture set, not two.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gesture_state import (hand, pinch_hand, fist_hand, INDEX_ONLY,
                                TWO_FINGER, ASPECT)
from test_camera_runtime import install_fake_cv2, restore_cv2, wait_for

FAILURES = []

GESTURE_DIR = os.path.join(project_root, "src", "kayra", "input", "gesture")
UI_DIR = os.path.join(project_root, "src", "kayra", "ui")


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def gesture_sources():
    return sorted(glob.glob(os.path.join(GESTURE_DIR, "*.py")))


def ui_sources():
    return sorted(glob.glob(os.path.join(UI_DIR, "**", "*.py"), recursive=True))


def parse(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE FAKE DETECTOR                             │
# └────────────────────────────────────────────────────────────────────────┘

class _Landmark:
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y):
        self.x, self.y, self.z = x, y, 0.0


class _HandLandmarks:
    def __init__(self, points):
        self.landmark = [_Landmark(x, y) for x, y in points]


class _Classification:
    def __init__(self, label, score):
        self.label, self.score = label, score


class _Handedness:
    def __init__(self, label, score):
        self.classification = [_Classification(label, score)]


class _Results:
    def __init__(self, sets, labels):
        self.multi_hand_landmarks = sets
        self.multi_handedness = labels


def install_fake_mediapipe(pose_source):
    """
    A `mediapipe` whose `Hands.process` returns whatever `pose_source()` yields.

    `pose_source` is a zero-argument callable returning a list of 21-point sequences, or None
    for "no hand in this frame". That is the entire surface the detector uses, which is itself
    worth noting: the detector's dependency on MediaPipe is one constructor and one method.
    """
    module = types.ModuleType("mediapipe")
    module.__version__ = "fake-0.10"

    class Hands:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            module.constructed.append(self)

        def process(self, frame):
            if self.closed:
                return _Results(None, None)
            poses = pose_source()
            if not poses:
                return _Results(None, None)
            return _Results([_HandLandmarks(points) for points in poses],
                            [_Handedness("Right", 0.92) for _ in poses])

        def close(self):
            self.closed = True

    module.constructed = []
    solutions = types.ModuleType("mediapipe.solutions")
    hands_module = types.ModuleType("mediapipe.solutions.hands")
    hands_module.Hands = Hands
    solutions.hands = hands_module
    module.solutions = solutions

    previous = {name: sys.modules.get(name) for name in
                ("mediapipe", "mediapipe.solutions", "mediapipe.solutions.hands",
                 "mediapipe.tasks", "mediapipe.tasks.python",
                 "mediapipe.tasks.python.core", "mediapipe.tasks.python.core.base_options")}
    sys.modules["mediapipe"] = module
    sys.modules["mediapipe.solutions"] = solutions
    sys.modules["mediapipe.solutions.hands"] = hands_module
    for name in ("mediapipe.tasks", "mediapipe.tasks.python",
                 "mediapipe.tasks.python.core", "mediapipe.tasks.python.core.base_options"):
        sys.modules.pop(name, None)
    return module, previous


def restore_mediapipe(previous):
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class Scene:
    """A scripted sequence of poses the fake detector replays, one per processed frame."""

    def __init__(self, poses):
        self.poses = list(poses)
        self.index = 0

    def __call__(self):
        if not self.poses:
            return None
        pose = self.poses[min(self.index, len(self.poses) - 1)]
        self.index += 1
        return None if pose is None else [pose]

    def hold(self, pose):
        self.poses = [pose]
        self.index = 0


def controller(scene, **cfg_overrides):
    """A controller wired to the fake camera, the fake detector and a recording pointer."""
    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.controller import GestureController
    from kayra.input.gesture.pointer import PointerController, RecordingInjector

    cfg = GestureConfig(**cfg_overrides)
    cfg.normalize()
    injector = RecordingInjector()
    return GestureController(cfg, pointer=PointerController(injector)), injector


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        1. CONFIGURATION                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_config():
    print_system("\n[1] Configuration")
    from kayra.input.gesture.config import GestureConfig, ENV_KEYS, LEVELS

    cfg = GestureConfig()
    cfg.normalize()

    # THE HYSTERESIS INVARIANTS. `exit <= enter` collapses a two-state gate into a comparator,
    # and a comparator on a noisy signal is the flicker every one of these gates prevents.
    check("the pinch release threshold exceeds the pinch threshold",
          cfg.pinch_exit > cfg.pinch_enter, f"{cfg.pinch_enter} / {cfg.pinch_exit}")
    check("the double-click release exceeds its threshold",
          cfg.double_exit > cfg.double_enter, f"{cfg.double_enter} / {cfg.double_exit}")
    check("the double-click threshold is WIDER than the single-click one",
          cfg.double_enter > cfg.pinch_enter,
          "otherwise every left click also satisfies the beak gate")
    check("the scroll neutral threshold is below the scroll threshold",
          cfg.scroll_exit < cfg.scroll_enter, f"{cfg.scroll_exit} / {cfg.scroll_enter}")
    check("the cursor region leaves usable area",
          cfg.region_top + cfg.region_bottom < 0.85)

    # A malformed `.env` is CORRECTED and reported, never obeyed and never fatal.
    broken = GestureConfig(pinch_enter=0.5, pinch_exit=0.2, scroll_enter=0.3, scroll_exit=0.9,
                           region_top=0.4, region_bottom=0.45)
    broken.normalize()
    check("an inverted pinch hysteresis is corrected", broken.pinch_exit > broken.pinch_enter,
          f"{broken.pinch_enter} / {broken.pinch_exit}")
    check("an inverted scroll hysteresis is corrected",
          broken.scroll_exit < broken.scroll_enter)
    check("a region with no usable area is reset",
          broken.region_top + broken.region_bottom < 0.85)
    check("...and every correction is reported rather than silent",
          len(broken.corrections) >= 3, str(broken.corrections))

    # The presets must actually differ, or the settings screen is offering nothing.
    from kayra.input.gesture.config import _SENSITIVITY, _SMOOTHING, _CLICK
    for name, table in (("sensitivity", _SENSITIVITY), ("smoothing", _SMOOTHING),
                        ("click", _CLICK)):
        check(f"the {name} preset table covers every level",
              set(table) == set(LEVELS), str(sorted(table)))
        check(f"...the {name} levels are genuinely different",
              len({tuple(v) for v in table.values()}) == 3)

    # HIGH smoothing means heavier at rest AND more release under motion — not simply "more
    # filtering", which is the sluggish-cursor failure.
    low, medium, high = _SMOOTHING["LOW"], _SMOOTHING["MEDIUM"], _SMOOTHING["HIGH"]
    check("higher smoothing lowers the rest cutoff", high[0] < medium[0] < low[0],
          f"{low[0]} / {medium[0]} / {high[0]}")
    check("the velocity term is in the tens, not the hundredths",
          all(preset[1] > 1.0 for preset in (low, medium, high)),
          "One Euro runs in normalized frame units here; see the module docstring")

    # A documented setting nothing reads, and a read setting nothing documents, are both bugs.
    example = os.path.join(project_root, ".env.example")
    with open(example, "r", encoding="utf-8") as handle:
        documented = handle.read()
    missing = [key for key in ENV_KEYS if key not in documented]
    check("every gesture setting the code reads is documented in .env.example",
          not missing, str(missing))

    source = inspect.getsource(GestureConfig.from_env)
    unclamped = re.findall(r'env_(?:int|float)\("([A-Z_]+)",\s*[^,)]+\)', source)
    check("every numeric setting is clamped", not unclamped, str(unclamped))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      2. SINGLE OWNERSHIP (AST)                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_ownership():
    print_system("\n[2] Single ownership, by AST")

    capture_sites, graph_sites = [], []
    for path in gesture_sources():
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name.endswith("VideoCapture"):
                capture_sites.append(f"{os.path.basename(path)}:{node.lineno}")
            if "solutions.hands.Hands" in name or name.endswith("Hands"):
                graph_sites.append(f"{os.path.basename(path)}:{node.lineno}")

    check("there is exactly ONE VideoCapture call site in the package",
          len(capture_sites) == 1, str(capture_sites))
    check("...and it is in camera.py",
          capture_sites and capture_sites[0].startswith("camera.py"), str(capture_sites))
    check("there is exactly ONE hand-graph construction site",
          len(graph_sites) == 1, str(graph_sites))
    check("...and it is in detector.py",
          graph_sites and graph_sites[0].startswith("detector.py"), str(graph_sites))

    # The whole application, not just this package.
    all_sources = sorted(glob.glob(os.path.join(project_root, "src", "kayra", "**", "*.py"),
                                   recursive=True))
    outside = []
    for path in all_sources:
        if os.path.dirname(path) == GESTURE_DIR:
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("VideoCapture"):
                outside.append(os.path.relpath(path, project_root))
    check("nothing outside the gesture package opens a camera", not outside, str(outside))

    # The UI consumes; it never captures or converts.
    ui_offenders = []
    for path in ui_sources():
        tree = parse(path)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] in ("cv2", "mediapipe") for name in names):
                ui_offenders.append(f"{os.path.relpath(path, project_root)}:{node.lineno}")
    check("no UI module imports cv2 or mediapipe", not ui_offenders, str(ui_offenders))

    # Importing the package must stay free — no camera, no model, no thread.
    for path in gesture_sources():
        tree = parse(path)
        for node in tree.body:
            check_bad = isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            if check_bad:
                name = ast.unparse(node.value.func)
                check(f"{os.path.basename(path)} has no import-time side effect",
                      name in ("print",), name)

    lazy = inspect.getsource(sys.modules["kayra.input.gesture.camera"])
    check("cv2 is imported lazily, inside the open path",
          "import cv2" in lazy and not re.search(r"^import cv2", lazy, re.M))
    detector_src = inspect.getsource(sys.modules["kayra.input.gesture.detector"])
    check("mediapipe is imported lazily, inside start()",
          "import mediapipe" in detector_src
          and not re.search(r"^import mediapipe", detector_src, re.M))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                3. GESTURE CODE NEVER TOUCHES VOICE                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_voice_isolation():
    print_system("\n[3] Gesture code never touches voice state")

    FORBIDDEN_CALLS = {"set_listening", "set_sleeping", "pause_listening", "resume_listening",
                       "request_shutdown", "begin_turn", "note_interrupt", "set_state"}
    FORBIDDEN_MODULES = {"kayra.input.speech_to_text", "kayra.output.text_to_speech",
                         "kayra.core.voice_state", "kayra.input.stt_backend",
                         "kayra.intelligence.llm_engine"}

    offenders, imports = [], []
    for path in gesture_sources():
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_MODULES:
                imports.append(f"{os.path.basename(path)}:{node.lineno} {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES:
                        imports.append(f"{os.path.basename(path)}:{node.lineno} {alias.name}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    offenders.append(f"{os.path.basename(path)}:{node.lineno} "
                                     f"{node.func.attr}")

    check("the gesture package imports no voice engine", not imports, str(imports))
    check("...and calls nothing that changes voice state", not offenders, str(offenders))

    # It touches the runtime bus in ONE direction, for notification only.
    from kayra.input.gesture.controller import GestureController
    source = inspect.getsource(GestureController)
    check("the controller only EMITS on the runtime bus", "emit(" in source)
    check("...and never sets assistant state",
          ".set_state(" not in source.replace("self._set_state(", ""))

    # Safety rules the rest of the codebase already lives by.
    for path in gesture_sources():
        text = open(path, "r", encoding="utf-8").read()
        base = os.path.basename(path)
        check(f"{base}: no shell=True", "shell=True" not in text)
        check(f"{base}: no os.system", "os.system(" not in text)
        check(f"{base}: no taskkill by name", "taskkill" not in text.lower())
        check(f"{base}: no CUDA / TensorRT / ONNX Runtime import",
              not re.search(r"^\s*(?:from|import)\s+(onnxruntime|torch|tensorrt)", text,
                            re.M), base)

    # NO PER-FRAME LOGGING. A gesture loop that logged every frame would produce thirty lines
    # a second, and the terminal is where the user reads everything else Kayra says.
    process = inspect.getsource(GestureController._process)
    info_calls = re.findall(r"\binfo\(", process)
    # Four: three discrete clicks and the pause announcement. Every one is guarded by an event
    # that can happen at most a few times a second by construction — a rising click edge, or a
    # runtime state transition. The bound is here to stop a fifth appearing unguarded; the
    # behavioural proof that none of them fires per frame is in `section_logging` below.
    check("the per-frame path logs only discrete, edge-guarded events",
          len(info_calls) <= 4, f"{len(info_calls)} info() calls in _process")
    check("...and its state chatter is at DEBUG", "debug(" in process)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    4. THE TWO SWITCHES                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_switches():
    print_system("\n[4] Camera and gesture: two switches, three valid combinations")
    from kayra.input.gesture.controller import GestureRuntimeState

    scene = Scene([hand(INDEX_ONLY)[0]])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)

        check("nothing runs to begin with",
              not ctrl.camera_enabled() and not ctrl.gesture_enabled)
        check("...and the runtime is OFF", ctrl.state == GestureRuntimeState.OFF)

        # camera ON, gesture OFF — valid, and nothing may touch the pointer.
        ok, _ = ctrl.set_camera(True)
        check("the camera can be turned on alone", ok and ctrl.camera_enabled())
        check("...without enabling gesture control", not ctrl.gesture_enabled)
        time.sleep(0.3)
        check("...and nothing touches the pointer", not injector.events, str(injector.events))

        # camera ON, gesture ON.
        ok, _ = ctrl.set_gesture(True)
        check("gesture control can be turned on", ok and ctrl.gesture_enabled)
        check("...and the runtime reports ACTIVE", ctrl.state == GestureRuntimeState.ACTIVE,
              ctrl.state)
        check("...and exactly one camera exists", len(cv2_module.created) == 1)

        ctrl.set_gesture(False)
        ctrl.set_camera(False)

        # THE IMPOSSIBLE COMBINATION: enabling gesture control with the camera off must start
        # the camera, not produce a runtime waiting for frames that will never come.
        ok, _ = ctrl.set_gesture(True)
        check("enabling gesture control with the camera off starts the camera",
              ok and ctrl.camera_enabled() and ctrl.gesture_enabled)
        check("camera OFF + gesture ON is never reachable",
              not (ctrl.gesture_enabled and not ctrl.camera_enabled()))

        # Turning the camera off turns gesture control off with it, in that order.
        ctrl.set_camera(False)
        check("turning the camera off also turns gesture control off",
              not ctrl.gesture_enabled and not ctrl.camera_enabled())

        # Idempotence, from every direction.
        check("set_camera(False) twice is a successful no-op", ctrl.set_camera(False)[0])
        check("set_gesture(False) twice is a successful no-op", ctrl.set_gesture(False)[0])
        ctrl.set_gesture(True)
        check("set_gesture(True) twice is a successful no-op", ctrl.set_gesture(True)[0])
        check("set_camera(True) while running is a successful no-op",
              ctrl.set_camera(True)[0])
        check("...and still exactly one camera was ever opened for this run",
              len([c for c in cv2_module.created if not c.released]) == 1)
        ctrl.shutdown()

        # THE STATE FIELDS ARE SEPARATE. Three facts, three fields.
        status = ctrl.status()
        for key in ("camera", "gesture_enabled", "state", "hand", "gesture"):
            check(f"status carries {key} as its own field", key in status, str(sorted(status)))
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   5. THE FULL PIPELINE, END TO END                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_pipeline():
    print_system("\n[5] Camera → detector → features → FSM → pointer, end to end")

    scene = Scene([hand(INDEX_ONLY, offset=(0.004 * i, 0.0))[0] for i in range(400)])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)
        ok, detail = ctrl.set_gesture(True)
        check("the runtime starts", ok, str(detail))
        check("...and constructed exactly one hand graph",
              len(mp_module.constructed) == 1, str(len(mp_module.constructed)))
        check("...with ONE hand requested",
              mp_module.constructed[0].kwargs.get("max_num_hands") == 1,
              str(mp_module.constructed[0].kwargs))

        moved = wait_for(lambda: len(injector.moves()) > 5, timeout=4.0)
        check("a moving index finger moves the pointer", moved,
              f"{len(injector.moves())} moves")
        xs = [x for x, _ in injector.moves()]
        check("...in the direction the hand moved", len(xs) > 2 and xs[-1] != xs[0],
              f"{xs[0]} -> {xs[-1]}")
        check("...and no click was fired by movement alone",
              injector.left_clicks() == 0 and injector.right_clicks() == 0)

        # A left click, all the way to the injector.
        scene.hold(pinch_hand("index"))
        clicked = wait_for(lambda: injector.left_clicks() >= 1, timeout=4.0)
        check("a pinch produces a real left-click injection", clicked,
              str(injector.left_clicks()))
        time.sleep(0.5)
        check("...exactly one, however long it is held", injector.left_clicks() == 1,
              str(injector.left_clicks()))

        scene.hold(hand(INDEX_ONLY)[0])
        time.sleep(0.3)
        scene.hold(pinch_hand("middle"))
        right = wait_for(lambda: injector.right_clicks() >= 1, timeout=4.0)
        check("a middle pinch produces a real right-click injection", right,
              str(injector.right_clicks()))

        # Scroll.
        scene.poses = [hand(TWO_FINGER, offset=(0.0, -0.005 * i))[0] for i in range(200)]
        scene.index = 0
        scrolled = wait_for(lambda: bool(injector.wheel_deltas()), timeout=4.0)
        check("a two-finger sweep produces wheel injections", scrolled,
              str(injector.wheel_deltas()[:3]))
        check("...all in one direction",
              all(delta > 0 for delta in injector.wheel_deltas()),
              str(injector.wheel_deltas()[:5]))

        telemetry = ctrl.telemetry()
        for key in ("camera", "detector", "pointer", "processing_fps", "camera_fps",
                    "dropped_frames", "outliers_dropped", "hand_stability", "gesture",
                    "gesture_confidence", "cursor_velocity", "accelerator"):
            check(f"telemetry carries {key}", key in telemetry, str(sorted(telemetry)))
        print_info(f"      processing {telemetry['processing_fps']} FPS, camera "
                   f"{telemetry['camera_fps']} FPS, "
                   f"detector {telemetry['detector']['latency_ms']}ms")

        ctrl.shutdown()
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    6. NOTHING ACTS AFTER OFF                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_off():
    print_system("\n[6] Nothing acts after OFF")

    scene = Scene([pinch_hand("index")])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)
        ctrl.set_gesture(True)
        wait_for(lambda: injector.left_clicks() >= 1, timeout=4.0)
        ctrl.set_gesture(False)

        before = len(injector.events)
        scene.poses = [hand(INDEX_ONLY, offset=(0.01 * i, 0.0))[0] for i in range(200)]
        scene.index = 0
        time.sleep(0.6)
        after = [event for event in list(injector.events)[before:] if event[0] != "release"]
        check("no pointer event is produced after gesture control is switched off",
              not after, str(after[:4]))

        check("the pointer controller is disabled", not ctrl.pointer.enabled)
        check("...and a direct call to it is refused", not ctrl.pointer.left_click())
        check("...and a direct move is refused", not ctrl.pointer.move(10, 10))

        # A held button must be RELEASED on the way out, or the user is left dragging.
        releases = [event for event in injector.events if event[0] == "release"]
        check("switching off releases any held button", bool(releases))

        check("the camera keeps running if it was on independently", ctrl.camera_enabled())
        ctrl.shutdown()
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            7. FAILURES                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_failures():
    print_system("\n[7] Failures")
    from kayra.input.gesture.controller import GestureRuntimeState

    scene = Scene([hand(INDEX_ONLY)[0]])

    cv2_module, cv2_prev = install_fake_cv2(opens=False)
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)
        ok, detail = ctrl.set_gesture(True)
        check("gesture control does NOT come on when the camera fails", not ok)
        check("...and says why, in the camera's own words", bool(detail), str(detail))
        check("...and reports ERROR", ctrl.state == GestureRuntimeState.ERROR, ctrl.state)
        check("...and gesture control stays off", not ctrl.gesture_enabled)
        check("...and nothing touched the pointer", not injector.events)
        ctrl.shutdown()
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)

    # Camera fine, detector missing.
    cv2_module, cv2_prev = install_fake_cv2()
    previous = {name: sys.modules.get(name)
                for name in ("mediapipe", "mediapipe.solutions", "mediapipe.solutions.hands")}
    sys.modules["mediapipe"] = None
    sys.modules.pop("mediapipe.solutions", None)
    sys.modules.pop("mediapipe.solutions.hands", None)
    try:
        ctrl, injector = controller(scene)
        ok, detail = ctrl.set_gesture(True)
        check("gesture control does NOT come on without a detector", not ok, str(detail))
        check("...and reports ERROR", ctrl.state == GestureRuntimeState.ERROR)
        check("...and gesture control stays off", not ctrl.gesture_enabled)
        ctrl.shutdown()
    finally:
        restore_mediapipe(previous)
        restore_cv2(cv2_prev)

    # A detector that raises on every frame must cost frames, never the thread.
    class Exploding(Scene):
        def __call__(self):
            raise RuntimeError("inference exploded")

    exploding = Exploding([hand(INDEX_ONLY)[0]])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(exploding)
    try:
        ctrl, injector = controller(exploding)
        ctrl.set_gesture(True)
        time.sleep(0.5)
        alive = [t for t in threading.enumerate() if t.name == "kayra-gesture"]
        check("a detector that raises every frame does not kill the gesture thread",
              bool(alive), str([t.name for t in threading.enumerate()]))
        check("...and nothing is injected", not [e for e in injector.events
                                                 if e[0] != "release"])
        ctrl.shutdown()
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            8. THE PREVIEW                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_preview():
    print_system("\n[8] The preview")

    scene = Scene([hand(INDEX_ONLY)[0]])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)

    # The fake `resize`/`cvtColor` are pass-throughs, so the preview path needs a frame that
    # can produce bytes. A small real-shaped buffer is enough and keeps numpy out of this file.
    class Buffered:
        """A resize result that honours the requested size, so the byte count can be checked."""

        def __init__(self, width, height):
            self.shape = (height, width, 3)

        def tobytes(self):
            return bytes(self.shape[0] * self.shape[1] * 3)

    cv2_module.resize = (lambda frame, size, interpolation=None: Buffered(size[0], size[1]))
    cv2_module.cvtColor = (lambda frame, code:
                           frame if isinstance(frame, Buffered)
                           else Buffered(frame.shape[1], frame.shape[0]))
    try:
        ctrl, injector = controller(scene)
        check("there is no preview before the camera starts", ctrl.preview() is None)

        ctrl.set_camera(True)
        got = wait_for(lambda: ctrl.preview() is not None, timeout=3.0)
        check("the camera alone produces a preview", got)
        frame = ctrl.preview()
        check("the preview is (bytes, width, height)",
              isinstance(frame, tuple) and len(frame) == 3
              and isinstance(frame[0], (bytes, bytearray)), str(type(frame)))
        check("...ready to paint, with no conversion left for the GUI thread",
              len(frame[0]) == frame[1] * frame[2] * 3,
              f"{len(frame[0])} bytes for {frame[1]}x{frame[2]}")

        # The preview is THROTTLED independently of the processing rate, and it is a SINGLE
        # SLOT — a pull model, so there is no queue that can grow.
        check("the preview is throttled below the processing rate",
              ctrl.config.preview_fps < ctrl.config.target_fps,
              f"{ctrl.config.preview_fps} vs {ctrl.config.target_fps}")
        second = ctrl.preview()
        check("reading the preview twice does not consume it", second is not None)

        # ONE capture feeds both the preview and the gesture engine.
        ctrl.set_gesture(True)
        time.sleep(0.3)
        check("turning gesture control on does not open a second camera",
              len(cv2_module.created) == 1, str(len(cv2_module.created)))
        check("...and the preview keeps working", ctrl.preview() is not None)

        ctrl.shutdown()
        check("shutdown clears the preview", ctrl.preview() is None)
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          9. VOICE COMMANDS                             │
# └────────────────────────────────────────────────────────────────────────┘

def section_voice():
    print_system("\n[9] Voice commands")
    from kayra.core.voice_control import (
        classify_control, ControlKind, DEVICE_KINDS, control_kinds, phrases_for,
        SHUTDOWN_PHRASES, is_interrupt_phrase,
    )

    cases = {
        ControlKind.GESTURE_ON: [
            "open hand gesture control", "activate hand gesture control",
            "turn on hand gesture control", "start hand gesture control",
            "enable gesture control", "Kayra, turn on hand gesture control",
        ],
        ControlKind.GESTURE_OFF: [
            "close hand gesture control", "deactivate hand gesture control",
            "turn off hand gesture control", "stop hand gesture control",
            "disable gesture control", "please turn off hand gesture control",
        ],
        ControlKind.CAMERA_ON: [
            "turn on camera", "turn on the camera", "start the camera", "open the camera",
            "camera on", "hey Kayra, turn on the webcam",
        ],
        ControlKind.CAMERA_OFF: [
            "turn off camera", "turn off the camera", "stop the camera", "close the camera",
            "camera off", "turn off my camera",
        ],
    }
    for kind, phrases in cases.items():
        for phrase in phrases:
            command = classify_control(phrase)
            check(f"'{phrase}' -> {kind}",
                  command is not None and command.kind == kind,
                  command.kind if command else "None")

    # THE BOUNDARY THAT MATTERS MOST. Quitting the assistant because someone asked to stop
    # using their webcam would be the worst mistake this table could make.
    for phrase in ("turn off camera", "turn off the camera", "stop hand gesture control",
                   "turn off hand gesture control", "close hand gesture control"):
        command = classify_control(phrase)
        check(f"'{phrase}' is NOT a shutdown", command.kind != ControlKind.SHUTDOWN)
    for phrase in ("turn off kayra", "shut down kayra", "exit", "quit"):
        command = classify_control(phrase)
        check(f"'{phrase}' is STILL a shutdown", command.kind == ControlKind.SHUTDOWN)

    check("the device phrases are disjoint from the shutdown phrases",
          not (set(phrases_for(ControlKind.GESTURE_OFF)) | set(phrases_for(ControlKind.CAMERA_OFF)))
          & set(SHUTDOWN_PHRASES))
    check("...and none of them is an interrupt",
          not any(is_interrupt_phrase(p) for p in
                  phrases_for(ControlKind.GESTURE_ON) + phrases_for(ControlKind.CAMERA_OFF)))
    check("the four new kinds are in control_kinds()",
          DEVICE_KINDS <= set(control_kinds()), str(sorted(DEVICE_KINDS)))

    # Matching stays EXACT: a device phrase embedded in a longer instruction is an instruction.
    for phrase in ("turn off the camera and open chrome", "what is a camera",
                   "turn off camera settings", "how do I turn on the camera"):
        check(f"'{phrase}' falls through to the DMM", classify_control(phrase) is None,
              str(classify_control(phrase)))

    # No LLM anywhere near this path — it has to work with the network down.
    from kayra.core import voice_control
    source = inspect.getsource(voice_control)
    check("the control vocabulary makes no network or model call",
          not any(token in source for token in ("requests.", "cohere", "openai", "http")))

    # And it is fast: this runs on the control watcher's 60ms poll.
    started = time.perf_counter()
    for _ in range(2000):
        classify_control("turn on hand gesture control")
    micros = (time.perf_counter() - started) / 2000 * 1e6
    print_info(f"      classify_control: {micros:.1f}us per utterance")
    check("classification stays well under the watcher's budget", micros < 200.0,
          f"{micros:.1f}us")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     10. SHUTDOWN AND APP WIRING                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_wiring():
    print_system("\n[10] Shutdown and the app wiring")

    import kayra.app as app

    for name in ("set_gesture_control", "set_camera", "gesture_status", "gesture_telemetry",
                 "gesture_preview", "gesture_controller"):
        check(f"app exposes {name}", callable(getattr(app, name, None)))

    dispatch = inspect.getsource(app._dispatch_control)
    for kind in ("GESTURE_ON", "GESTURE_OFF", "CAMERA_ON", "CAMERA_OFF"):
        check(f"_dispatch_control handles {kind}", f"ControlKind.{kind}" in dispatch)

    shutdown = inspect.getsource(app.request_shutdown)
    check("shutdown tears the gesture runtime down", "gesture_controller" in shutdown)
    # BEFORE the audio and browser teardown, so nothing can move the pointer while the process
    # is disappearing and no camera is left held.
    gesture_at = shutdown.index("gesture_controller")
    audio_at = shutdown.index("tts_engine.shutdown()")
    check("...before the audio device is disposed", gesture_at < audio_at)

    # A status read must NEVER construct a controller — that would open a camera to report
    # that the camera is closed.
    from kayra.input.gesture.controller import reset_gesture_controller
    reset_gesture_controller()
    status = app.gesture_status()
    check("gesture_status() on a cold process returns {}", status == {}, str(status))
    check("...without constructing a controller", app.gesture_controller(create=False) is None)
    check("gesture_preview() on a cold process returns None", app.gesture_preview() is None)
    check("gesture_telemetry() on a cold process returns {}", app.gesture_telemetry() == {})

    # The settings recorder owns the announcement, and only it.
    source = inspect.getsource(app.set_gesture_control)
    check("the switch goes through the transactional settings recorder",
          "settings_log.apply(" in source)
    check("...so a failed change is never reported as committed", "committed" in source)

    # The UI delegates and never reimplements.
    from kayra.ui import session as session_module
    session_source = inspect.getsource(session_module)
    check("the session delegates to app.set_gesture_control",
          "set_gesture_control(" in session_source)
    check("...and never constructs a controller of its own",
          "GestureController(" not in session_source)
    check("...and contains no Qt", "PySide6" not in session_source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     11. THE ACCELERATOR DECISION                       │
# └────────────────────────────────────────────────────────────────────────┘

def section_accelerator():
    print_system("\n[11] The accelerator decision")
    from kayra.input.gesture.detector import probe_accelerators, accelerator_report

    for mode in ("OFF", "AUTO", "ON"):
        result = probe_accelerators(mode)
        check(f"probe_accelerators({mode}) returns a real delegate name",
              result in ("CPU", "GPU"), str(result))

    report = accelerator_report()
    for key in ("delegate", "reason", "cuda_used"):
        check(f"the accelerator report carries {key}", key in report, str(sorted(report)))
    check("the report states WHY, not just what", len(report["reason"]) > 40)
    # GPU IS NOT MANDATORY AND MUST NOT BE INITIALISED SPECULATIVELY. Kayra already holds a
    # CUDA context for Kokoro; a second one for a 10ms inference that is not the bottleneck
    # would be contention bought for nothing.
    check("CUDA is not used for hand detection", report["cuda_used"] is False)

    print_info(f"      delegate={report['delegate']}")
    print_info(f"      reason={report['reason']}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       12. COST AND BOUNDEDNESS                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_cost():
    print_system("\n[12] Cost and boundedness")

    scene = Scene([hand(INDEX_ONLY, offset=(0.002 * (i % 50), 0.0))[0] for i in range(600)])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)
        ctrl.set_gesture(True)
        time.sleep(2.0)

        telemetry = ctrl.telemetry()
        print_info(f"      processing {telemetry['processing_fps']} FPS, "
                   f"dropped {telemetry['dropped_frames']}, "
                   f"outliers {telemetry['outliers_dropped']}")
        check("the runtime processes frames at a usable rate",
              telemetry["processing_fps"] > 10.0, str(telemetry["processing_fps"]))

        # BOUNDED. This process is designed to run all day at 30Hz.
        growing = []
        for owner, name in ((ctrl, "controller"), (ctrl.camera, "camera"),
                            (ctrl.recognizer, "recogniser"), (ctrl.stabilizer, "stabiliser")):
            for key, value in vars(owner).items():
                if isinstance(value, (list, dict, set)) and len(value) > 32:
                    growing.append(f"{name}.{key}={len(value)}")
        check("no collection anywhere in the runtime has grown", not growing, str(growing))

        # EXACTLY THE THREADS PROMISED, AND NO MORE.
        names = [t.name for t in threading.enumerate() if t.name.startswith("kayra-")]
        check("exactly one camera thread", names.count("kayra-camera") == 1, str(names))
        check("exactly one gesture thread", names.count("kayra-gesture") == 1, str(names))

        ctrl.shutdown()
        check("shutdown leaves no gesture or camera thread",
              wait_for(lambda: not [t for t in threading.enumerate()
                                    if t.name in ("kayra-camera", "kayra-gesture")],
                       timeout=3.0),
              str([t.name for t in threading.enumerate() if t.name.startswith("kayra-")]))
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)

    # The status read is on a UI path and must be free.
    from kayra.input.gesture.controller import GestureController, reset_gesture_controller
    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.pointer import PointerController, RecordingInjector
    cfg = GestureConfig()
    cfg.normalize()
    idle = GestureController(cfg, pointer=PointerController(RecordingInjector()))
    started = time.perf_counter()
    for _ in range(2000):
        idle.status()
    micros = (time.perf_counter() - started) / 2000 * 1e6
    print_info(f"      status(): {micros:.1f}us")
    check("status() is cheap enough for a UI paint path", micros < 200.0, f"{micros:.1f}us")
    reset_gesture_controller()


def section_logging():
    """
    What the terminal actually shows during a session, counted rather than assumed.

    THE REPORTED SYMPTOM WAS A LOG SYMPTOM AS MUCH AS A STATE ONE. The pause flap produced two
    INFO lines per round trip, roughly eight times a second, which is what made it visible and
    intolerable. So the fix is not complete until the line COUNT over a realistic session is
    asserted, not just the state count — a state machine that no longer flaps but still
    announces something per frame has fixed half the problem.
    """
    print_system("\n[13] Logging: counted over a real session")

    from kayra.core import logbus

    scene = Scene([hand(INDEX_ONLY, offset=(0.003 * (i % 40), 0.0))[0] for i in range(400)])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)

    lines = []
    original = logbus.log

    def capture(level, subsystem, message, *args, **kwargs):
        lines.append((level, subsystem, message))
        return original(level, subsystem, message, *args, **kwargs)

    logbus.log = capture
    try:
        ctrl, injector = controller(scene)
        ctrl.set_gesture(True)
        time.sleep(1.5)

        # A hand that comes and goes, several times.
        for _ in range(3):
            scene.hold(None)
            time.sleep(0.35)
            scene.hold(hand(INDEX_ONLY)[0])
            time.sleep(0.35)

        # FILTERED TO WHAT A USER ACTUALLY SEES. The gesture FSM's own state chatter is at
        # DEBUG by design and is not part of the normal-path budget; counting it here would
        # make the check fail for the very thing that keeps the terminal quiet.
        gesture_lines = [m for level, sub, m in lines
                         if sub == "GESTURE" and level != logbus.DEBUG]
        detected = [m for m in gesture_lines if m == "Hand: Detected"]
        lost = [m for m in gesture_lines if m == "Hand: Lost"]

        check("hand detection is logged on the transition", bool(detected), str(len(detected)))
        check("...and so is hand loss", bool(lost), str(len(lost)))
        # Three disappearances in ~4 seconds of ~30 FPS frames. Per-frame logging would be
        # hundreds of lines; edge logging is a handful.
        check("hand logging is per TRANSITION, not per frame",
              len(detected) + len(lost) <= 12,
              f"{len(detected)} detected + {len(lost)} lost over 3 disappearances")
        check("no pause was announced during an ordinary session",
              not [m for m in gesture_lines if "Pause" in m], str(gesture_lines[:8]))
        check("the whole session stays quiet", len(gesture_lines) <= 20,
              f"{len(gesture_lines)} GESTURE lines in ~4s")

        # A DELIBERATE pause announces itself, once.
        lines.clear()
        scene.hold(fist_hand())
        time.sleep(1.5)
        gesture_lines = [m for level, sub, m in lines
                         if sub == "GESTURE" and level != logbus.DEBUG]
        pauses = [m for m in gesture_lines if m == "Pause gesture detected"]
        check("a held fist announces the pause", len(pauses) == 1, str(pauses))
        transitions = [m for m in gesture_lines if m.startswith("State: ")]
        check("...and produces exactly one runtime transition", len(transitions) == 1,
              str(transitions))
        check("...which is ACTIVE -> PAUSED",
              transitions and transitions[0].endswith("-> PAUSED"), str(transitions))

        ctrl.shutdown()
    finally:
        logbus.log = original
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


def section_runtime_states():
    """
    The runtime state machine, and the distinction the brief calls critical.

    `NO_HAND` and `PAUSED` are different answers to different questions: one is a fact about
    the world, the other is something the user DID. A screen that collapsed them would tell a
    user who simply lowered their hand that they had paused the system — wrong, and
    unactionable, because there is nothing for them to undo.
    """
    print_system("\n[14] Runtime states: no hand is not a pause")

    from kayra.input.gesture.controller import (GestureRuntimeState, RUNTIME_LABELS,
                                                RUNNING_STATES)
    from kayra.input.gesture.state_machine import PAUSED_STATES, GestureState

    check("the runtime has a state for 'running, no hand'",
          hasattr(GestureRuntimeState, "ACTIVE_NO_HAND"))
    check("...and it is NOT the paused state",
          GestureRuntimeState.ACTIVE_NO_HAND != GestureRuntimeState.PAUSED)
    check("...and it reads as Active to the user",
          RUNTIME_LABELS[GestureRuntimeState.ACTIVE_NO_HAND] == "Active",
          RUNTIME_LABELS[GestureRuntimeState.ACTIVE_NO_HAND])
    check("...while PAUSED reads as Paused",
          RUNTIME_LABELS[GestureRuntimeState.PAUSED] == "Paused")
    check("every running state is in RUNNING_STATES",
          {GestureRuntimeState.ACTIVE, GestureRuntimeState.ACTIVE_NO_HAND,
           GestureRuntimeState.PAUSED} <= RUNNING_STATES)
    check("NO_HAND is not one of the gesture machine's paused states",
          GestureState.NO_HAND not in PAUSED_STATES, str(sorted(PAUSED_STATES)))

    scene = Scene([hand(INDEX_ONLY)[0]])
    cv2_module, cv2_prev = install_fake_cv2()
    mp_module, mp_prev = install_fake_mediapipe(scene)
    try:
        ctrl, injector = controller(scene)
        ctrl.set_gesture(True)
        wait_for(lambda: ctrl.status().get("hand"), timeout=3.0)
        status = ctrl.status()
        check("with a hand present the runtime is ACTIVE",
              status["state"] == GestureRuntimeState.ACTIVE, status["state"])
        check("...and reports a hand", status["hand"] is True)
        check("...and is not paused", status["paused"] is False)

        # THE HAND GOES AWAY. This must not become a pause.
        scene.hold(None)
        became = wait_for(lambda: ctrl.status().get("state")
                          == GestureRuntimeState.ACTIVE_NO_HAND, timeout=3.0)
        status = ctrl.status()
        check("an empty frame moves the runtime to ACTIVE_NO_HAND", became, status["state"])
        check("...NOT to PAUSED", status["state"] != GestureRuntimeState.PAUSED)
        check("...and `paused` stays False", status["paused"] is False)
        check("...and `hand` is False", status["hand"] is False)
        check("...and the label still says Active", status["state_label"] == "Active",
              status["state_label"])

        # A DELIBERATE fist.
        scene.hold(fist_hand())
        paused = wait_for(lambda: ctrl.status().get("paused"), timeout=4.0)
        status = ctrl.status()
        check("a held fist moves the runtime to PAUSED", paused, status["state"])
        check("...and `paused` is True", status["paused"] is True)
        check("...and the label says Paused", status["state_label"] == "Paused",
              status["state_label"])

        # And back.
        scene.hold(hand(INDEX_ONLY)[0])
        resumed = wait_for(lambda: not ctrl.status().get("paused"), timeout=4.0)
        check("a sustained open hand resumes", resumed, str(ctrl.status()["state"]))

        # A switched-off engine reports neither a hand nor a pause, whatever the last frame was.
        ctrl.set_gesture(False)
        status = ctrl.status()
        check("a switched-off engine reports no hand", status["hand"] is False)
        check("...and is not paused", status["paused"] is False)
        check("...and reports the gesture as absent", status["gesture"] == "No hand",
              status["gesture"])
        ctrl.shutdown()
    finally:
        restore_mediapipe(mp_prev)
        restore_cv2(cv2_prev)


def main():
    print_banner("GESTURE CONTROL DIAGNOSTIC",
                 "ownership · switches · pipeline · voice · shutdown")
    section_config()
    section_ownership()
    section_voice_isolation()
    section_switches()
    section_pipeline()
    section_off()
    section_failures()
    section_preview()
    section_voice()
    section_wiring()
    section_accelerator()
    section_logging()
    section_runtime_states()
    section_cost()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All gesture control checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
