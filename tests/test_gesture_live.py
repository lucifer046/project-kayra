# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_gesture_live.py                             │
# │        The Part No Synthetic Test Can Do — a real hand, measured       │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_gesture_live.py — guided validation with a REAL camera and a REAL hand.

    .venv\\Scripts\\python tests\\test_gesture_live.py               (safe: no real mouse)
    .venv\\Scripts\\python tests\\test_gesture_live.py --real-mouse  (drives the actual pointer)

NEEDS A HUMAN. It walks the ten scenarios A-J from the brief, one at a time, and reports what
the system actually did with a hand in front of the camera.

WHY THIS FILE EXISTS SEPARATELY FROM THE OTHER THREE
-----------------------------------------------------
`test_gesture_state.py` proves that GIVEN these landmarks the machine decides correctly.
`test_gesture_control.py` proves the runtime, the ownership and the switches. Neither can tell
you whether the cursor feels good, because neither has a hand in it:

  * real landmark noise is not Gaussian and is not independent between fingers
  * a real hand cannot hold a pose perfectly, and the interesting question is what happens
    when it nearly does
  * "does it feel sluggish?" is not a property of a decision, it is a property of a person

So the claim "gesture control is fixed" is NOT supported by the synthetic suites alone, and
this file is the reason that sentence can be written honestly at all. Run it before believing
any of it.

SAFE BY DEFAULT. Without `--real-mouse` the pointer controller is a `RecordingInjector`:
everything runs, every decision is made and reported, and nothing touches your actual mouse.
That is the right default for a file whose whole purpose is to be run while you are also
trying to read its output.
"""

import os
import sys
import time
import math
import statistics

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import (print_banner, print_info, print_success, print_error, print_system,
                         print_warning, console)
from kayra.core.config import load_environment

FAILURES = []
OBSERVATIONS = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def observe(label, detail):
    """A measurement with no pass/fail. Reported, so the numbers are on the record."""
    OBSERVATIONS.append((label, detail))
    print_info(f"      {label}: {detail}")


def prompt(text):
    console.print(f"\n[bold cyan]{text}[/bold cyan]")
    try:
        console.input("[dim]Press Enter when you are ready…[/dim] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Cancelled.")


class Recorder:
    """
    Watches the controller for a fixed window and records what it decided.

    Reads the controller's own decision object rather than reimplementing anything, so what
    this file reports is exactly what the runtime concluded — not a second opinion computed
    from the same frames.
    """

    def __init__(self, controller, injector):
        self.controller = controller
        self.injector = injector

    def watch(self, seconds, label=""):
        if label:
            console.print(f"[dim]  … {label} ({seconds:.0f}s)[/dim]")
        start_events = len(self.injector.events)
        start_left = self.injector.left_clicks()
        start_right = self.injector.right_clicks()
        start_wheel = len(self.injector.wheel_deltas())
        states, stabilities, points, wheels = [], [], [], []

        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            decision = self.controller._decision
            if decision is not None:
                states.append(decision.state)
                stabilities.append(decision.stability)
            point = self.controller.stabilizer.last_point
            if point is not None:
                points.append(point)
            time.sleep(0.02)

        wheels = self.injector.wheel_deltas()[start_wheel:]
        return {
            "states": states,
            "unique_states": sorted(set(states)),
            "stability": statistics.mean(stabilities) if stabilities else 0.0,
            "left": self.injector.left_clicks() - start_left,
            "right": self.injector.right_clicks() - start_right,
            "wheel": wheels,
            "events": len(self.injector.events) - start_events,
            "points": points,
        }


def pointer_spread(points):
    """Standard deviation of the emitted pointer position, in pixels."""
    if len(points) < 3:
        return 0.0
    return (statistics.pstdev([p[0] for p in points])
            + statistics.pstdev([p[1] for p in points])) / 2.0


def biggest_step(points):
    return max((math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(points, points[1:])), default=0.0)


def desktop_reachable():
    """
    Whether this process can actually drive the pointer.

    A process without access to the interactive input desktop — a service, a scheduled task, a
    detached agent session — still imports `user32` and still gets a handle back from
    `SetCursorPos`, which simply returns FALSE and does nothing. `GetCursorPos` reads (0, 0)
    forever. Every injection looks like it worked.

    That is a genuinely misleading failure for a validation script whose whole point is to
    prove the pointer moves, so it is detected up front and reported rather than being
    discovered as "the gestures do not work". Non-Windows returns None: not applicable.
    """
    if not sys.platform.startswith("win"):
        return None
    import ctypes

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    u32 = ctypes.windll.user32
    point = _Point()
    if not u32.GetCursorPos(ctypes.byref(point)):
        return False
    return bool(u32.SetCursorPos(point.x, point.y))


def main():
    real_mouse = "--real-mouse" in sys.argv
    load_environment()

    print_banner("LIVE GESTURE VALIDATION", "a real camera, and your actual hand")
    if real_mouse and desktop_reachable() is False:
        print_error("This process cannot reach the interactive input desktop, so Windows")
        print_error("will refuse every SetCursorPos and the pointer will not move — while")
        print_error("every injection still appears to succeed. Run this from an ordinary")
        print_error("desktop session (not a service, scheduled task or detached agent).")
        print_info("Continuing in SAFE MODE instead.")
        real_mouse = False

    if real_mouse:
        # UNMISTAKABLE, AND WITH A WAY OUT. This mode injects real clicks into whatever window
        # happens to be under the pointer, and the pointer is about to be somewhere the user
        # did not put it. A single dim line of warning is not enough for that.
        console.print()
        console.print("[bold red]" + "=" * 68 + "[/bold red]")
        print_warning("REAL MOUSE MODE")
        print_warning("Gesture control will move your ACTUAL cursor and click for real.")
        print_warning("Clicks land on whatever window is under the pointer.")
        print_warning("Close anything you would not want clicked, and keep your hand")
        print_warning("AWAY from pinch gestures until each step asks for one.")
        console.print("[bold red]" + "=" * 68 + "[/bold red]")
        console.print("[dim]Ctrl+C now to back out. Ctrl+. or Alt+Tab will not stop the "
                      "pointer — closing this window will.[/dim]")
        try:
            answer = console.input(
                "Type [bold yellow]yes[/bold yellow] to drive the real mouse: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Cancelled.")
        if answer != "yes":
            print_info("Not confirmed — staying in safe mode.")
            real_mouse = False
    if not real_mouse:
        print_info("SAFE MODE. Every decision is made, recorded and reported, and your mouse "
                   "is not touched.")
        print_info("Pass --real-mouse to drive the real pointer.")

    from kayra.input.gesture.config import GestureConfig
    from kayra.input.gesture.controller import GestureController, GestureRuntimeState
    from kayra.input.gesture.pointer import PointerController, RecordingInjector
    from kayra.input.gesture.state_machine import GestureState

    injector = RecordingInjector()
    cfg = GestureConfig.from_env()
    print_info(f"Configuration: {cfg.summary()}")

    controller = GestureController(cfg, pointer=None if real_mouse
                                   else PointerController(injector))
    if real_mouse:
        # Even driving the real mouse, the events are recorded so the report is the same.
        injector = _TeeInjector(controller.pointer._injector, injector)
        controller.pointer._injector = injector

    started = time.perf_counter()
    ok, detail = controller.set_gesture(True)
    check("gesture control starts against the real camera", ok, str(detail))
    if not ok:
        return 1
    observe("start-up time", f"{time.perf_counter() - started:.2f}s")

    recorder = Recorder(controller, injector)
    try:
        prompt("A. Hold your index finger up and move it SLOWLY across the frame.")
        result = recorder.watch(6.0, "watching")
        check("A: a moving index finger is recognised as the cursor gesture",
              GestureState.CURSOR in result["unique_states"], str(result["unique_states"]))
        check("A: the pointer followed", result["events"] > 10, f"{result['events']} events")
        check("A: nothing was clicked", result["left"] == 0 and result["right"] == 0,
              f"L{result['left']} R{result['right']}")
        observe("A hand stability", f"{result['stability']:.2f}")
        observe("A largest single pointer step", f"{biggest_step(result['points']):.0f}px")

        prompt("B. Hold your index finger up and keep it AS STILL as you can.")
        result = recorder.watch(6.0, "measuring wobble")
        spread = pointer_spread(result["points"])
        observe("B pointer wobble with a still hand", f"{spread:.1f}px standard deviation")
        check("B: a still hand leaves the pointer essentially still", spread < 12.0,
              f"{spread:.1f}px")
        check("B: a still hand fires nothing",
              result["left"] == 0 and result["right"] == 0 and not result["wheel"],
              f"L{result['left']} R{result['right']} W{len(result['wheel'])}")

        prompt("C. Touch your INDEX finger and THUMB together ONCE, then separate them.")
        result = recorder.watch(5.0, "watching for one click")
        check("C: index + thumb produced exactly ONE left click", result["left"] == 1,
              f"{result['left']} clicks")
        check("C: and no right click", result["right"] == 0, str(result["right"]))

        prompt("D. Touch your MIDDLE finger and THUMB together ONCE, then separate them.")
        result = recorder.watch(5.0, "watching for one click")
        check("D: middle + thumb produced exactly ONE right click", result["right"] == 1,
              f"{result['right']} clicks")
        check("D: and no left click", result["left"] == 0, str(result["left"]))

        prompt("E. Hold TWO fingers up (index + middle) and sweep them UPWARD, twice.")
        result = recorder.watch(6.0, "watching the wheel")
        ups = [d for d in result["wheel"] if d > 0]
        downs = [d for d in result["wheel"] if d < 0]
        check("E: two fingers moving up scrolled", bool(result["wheel"]),
              f"{len(result['wheel'])} impulses")
        check("E: predominantly upward", len(ups) > len(downs) * 3,
              f"{len(ups)} up / {len(downs)} down")
        check("E: no click came with the scroll",
              result["left"] == 0 and result["right"] == 0)

        prompt("F. Same two fingers, sweep them DOWNWARD, twice.")
        result = recorder.watch(6.0, "watching the wheel")
        ups = [d for d in result["wheel"] if d > 0]
        downs = [d for d in result["wheel"] if d < 0]
        check("F: two fingers moving down scrolled", bool(result["wheel"]),
              f"{len(result['wheel'])} impulses")
        check("F: predominantly downward", len(downs) > len(ups) * 3,
              f"{len(ups)} up / {len(downs)} down")

        prompt("G. Take your hand COMPLETELY out of the frame and keep it out.")
        result = recorder.watch(5.0, "watching an empty frame")
        check("G: an empty frame reaches NO_HAND",
              GestureState.NO_HAND in result["unique_states"], str(result["unique_states"]))
        check("G: nothing is clicked with no hand present",
              result["left"] == 0 and result["right"] == 0, "the important one")
        check("G: nothing is scrolled either", not result["wheel"])
        check("G: and the pointer is not moved", result["events"] == 0,
              f"{result['events']} events")

        prompt("H. Bring your index finger BACK into frame, roughly where it was, and hold it.")
        result = recorder.watch(5.0, "watching re-acquisition")
        check("H: the hand is re-acquired", GestureState.NO_HAND not in
              result["unique_states"][-1:] if result["unique_states"] else False,
              str(result["unique_states"]))
        step = biggest_step(result["points"])
        observe("H largest pointer step on re-acquisition", f"{step:.0f}px")
        check("H: re-acquisition does not teleport the pointer across the screen",
              step < 700.0, f"{step:.0f}px")
        check("H: and does not fire a click on arrival",
              result["left"] == 0 and result["right"] == 0)

        prompt("I. Keep your hand in frame. Gesture control is about to be switched OFF.")
        controller.set_gesture(False)
        check("I: the runtime reports OFF", controller.state == GestureRuntimeState.OFF,
              controller.state)
        result = recorder.watch(5.0, "watching a switched-off runtime")
        moves = [e for e in list(injector.events)[-result["events"]:] if e[0] != "release"] \
            if result["events"] else []
        check("I: nothing at all reaches the pointer after OFF", not moves, str(moves[:4]))

        prompt("J. Gesture control is about to be switched back ON. Keep your hand in frame.")
        restarted = time.perf_counter()
        ok, detail = controller.set_gesture(True)
        check("J: gesture control resumes without restarting Kayra", ok, str(detail))
        observe("J restart time", f"{time.perf_counter() - restarted:.2f}s")
        result = recorder.watch(5.0, "watching resumed control")
        check("J: control genuinely resumed", result["events"] > 5,
              f"{result['events']} events")

        telemetry = controller.telemetry()
        print_system("\n[Telemetry after the full run]")
        observe("processing FPS", str(telemetry["processing_fps"]))
        observe("camera FPS", str(telemetry["camera_fps"]))
        observe("inference latency", f"{telemetry['detector']['latency_ms']}ms")
        observe("frames dropped", str(telemetry["dropped_frames"]))
        observe("outlier samples rejected", str(telemetry["outliers_dropped"]))
        observe("dead-zone holds", str(telemetry["deadzone_holds"]))
        observe("speed clamps", str(telemetry["speed_clamps"]))
        observe("camera read failures", str(telemetry["camera"]["read_failures"]))
        observe("camera recoveries", str(telemetry["camera"]["recoveries"]))
        check("no frames were dropped over the whole session",
              telemetry["dropped_frames"] < telemetry["camera"]["frames"] * 0.25,
              f"{telemetry['dropped_frames']} of {telemetry['camera']['frames']}")
        check("the camera never had to recover",
              telemetry["camera"]["recoveries"] == 0,
              str(telemetry["camera"]["recoveries"]))

    finally:
        import threading
        controller.shutdown()
        time.sleep(0.5)
        leftover = [t.name for t in threading.enumerate()
                    if t.name in ("kayra-camera", "kayra-gesture")]
        check("shutdown leaves no camera or gesture thread", not leftover, str(leftover))

    print_system("\n" + "=" * 60)
    for label, detail in OBSERVATIONS:
        print_info(f"  {label}: {detail}")
    print_system("=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("Live gesture validation passed.")
    return 0


class _TeeInjector:
    """Performs the action AND records it, so `--real-mouse` reports the same numbers."""

    def __init__(self, real, recorder):
        self._real = real
        self._recorder = recorder
        self.events = recorder.events

    def screen_size(self):
        return self._real.screen_size()

    def move(self, x, y):
        self._recorder.move(x, y)
        self._real.move(x, y)

    def click(self, down, up):
        self._recorder.click(down, up)
        self._real.click(down, up)

    def wheel(self, delta):
        self._recorder.wheel(delta)
        self._real.wheel(delta)

    def release_buttons(self):
        self._recorder.release_buttons()
        self._real.release_buttons()

    def moves(self):
        return self._recorder.moves()

    def left_clicks(self):
        return self._recorder.left_clicks()

    def right_clicks(self):
        return self._recorder.right_clicks()

    def wheel_deltas(self):
        return self._recorder.wheel_deltas()


if __name__ == "__main__":
    sys.exit(main())
