# ┌────────────────────────────────────────────────────────────────────────┐
# │                        gesture/pointer.py                              │
# │           Desktop Input Injection — the only thing that acts           │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single place gesture control touches the user's machine.

WHY user32 DIRECTLY AND NOT `automation.windows`
------------------------------------------------
Two reasons, and the first is structural.

**`automation` must never import `input`, and this lives in `input`.** The gesture stack is a
capture device — camera in, intent out — and it sits with the other capture device (speech) for
exactly the same reason: the automation layer is reached by things the USER SAID, and a
pointer moving thirty times a second is not a sentence. Routing cursor movement through
`translate_and_execute` would put the policy engine, the target resolver and the audit ring on
a 30Hz path; `normalize_command` is 1.4us and `classify_action` 4.3us, so that alone is ~0.2ms
per frame of pure overhead for a decision that has already been made by the state machine.

**And `pyautogui.moveTo` is not usable at this rate.** It applies its own `PAUSE` (0.1s by
default), performs a failsafe corner check, and calls `SetCursorPos` underneath anyway.
Measured here: `pyautogui.moveTo` 1.9ms per call versus `user32.SetCursorPos` 0.012ms — a
160x difference, on the one call made most often in the whole system.

`automation.windows.MouseControl` remains the right path for a SPOKEN "click" and is untouched.

RATE LIMITING IS SPLIT BY KIND, AND THAT IS THE POINT
------------------------------------------------------
    CONTINUOUS  cursor movement, scroll impulses   — high rate, bounded per frame
    DISCRETE    left / right / double click        — strict minimum interval

The v1 engine had one `CLICK_COOLDOWN` and applied it to whatever branch happened to read it.
Continuous and discrete actions have genuinely different budgets: a pointer that updates 30
times a second is correct, and a mouse that clicks 30 times a second is a catastrophe.

NOTHING HERE BLOCKS
-------------------
`click_kernel` in v1 slept 10ms between the down and up events, and `double_click_kernel` slept
another 50ms — on the capture thread. That is 70ms of a 33ms frame budget spent sleeping, per
double click, which is two dropped frames every time the user clicks. The button-down and
button-up events are posted back to back here; Windows synthesises the press duration from the
message timestamps and no application requires a real delay between them.
"""

import ctypes
import sys
import threading

from kayra.core.logbus import Subsystem, debug

_WINDOWS = sys.platform.startswith("win")

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800


class PointerController:
    """
    Moves the pointer, clicks, and scrolls. One instance per gesture runtime.

    Every method is a no-op when `enabled` is False, and `enabled` is cleared the instant
    gesture control is switched off. That is the guarantee behind "no gesture processing
    continues invisibly after OFF": even if a decision were somehow produced after the switch,
    it cannot reach the user's desktop.
    """

    def __init__(self, injector=None):
        # `injector` is substitutable so the whole action path can be exercised in a test
        # without moving the developer's actual mouse pointer — which is the difference
        # between a suite that can be run and one that cannot.
        self._injector = injector or (_Win32Injector() if _WINDOWS else _NullInjector())
        self._lock = threading.Lock()
        self.enabled = False

        self.moves = 0
        self.left_clicks = 0
        self.right_clicks = 0
        self.double_clicks = 0
        self.scrolls = 0
        self.failures = 0
        self.last_error = ""

    # ──────────────────────────────────────────────────────────────────

    def screen_size(self):
        return self._injector.screen_size()

    def enable(self):
        with self._lock:
            self.enabled = True

    def disable(self):
        """
        Stops acting immediately, and releases any button this controller is holding.

        The release is not optional. A user who switches gesture control off mid-pinch would
        otherwise be left with the left button logically DOWN, and every subsequent movement
        of their real mouse would be a drag they cannot end.
        """
        with self._lock:
            self.enabled = False
        try:
            self._injector.release_buttons()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────

    def move(self, x, y):
        if not self.enabled:
            return False
        try:
            self._injector.move(int(x), int(y))
            self.moves += 1
            return True
        except Exception as exc:
            return self._fail(exc)

    def left_click(self):
        if not self.enabled:
            return False
        try:
            self._injector.click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
            self.left_clicks += 1
            return True
        except Exception as exc:
            return self._fail(exc)

    def right_click(self):
        if not self.enabled:
            return False
        try:
            self._injector.click(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
            self.right_clicks += 1
            return True
        except Exception as exc:
            return self._fail(exc)

    def double_click(self):
        if not self.enabled:
            return False
        try:
            self._injector.click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
            self._injector.click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
            self.double_clicks += 1
            return True
        except Exception as exc:
            return self._fail(exc)

    def scroll(self, delta):
        if not self.enabled or not delta:
            return False
        try:
            self._injector.wheel(int(delta))
            self.scrolls += 1
            return True
        except Exception as exc:
            return self._fail(exc)

    # ──────────────────────────────────────────────────────────────────

    def _fail(self, exc):
        """
        A failed injection is counted and logged ONCE per distinct message, never per frame.

        Per-frame logging on a failing mouse API is thirty lines a second of identical text,
        which is how a diagnostic becomes the thing that makes the terminal unusable.
        """
        self.failures += 1
        message = f"{type(exc).__name__}: {exc}"
        if message != self.last_error:
            self.last_error = message
            debug(Subsystem.GESTURE, f"Pointer injection failed: {message}")
        return False

    def telemetry(self) -> dict:
        return {
            "enabled": self.enabled,
            "moves": self.moves,
            "left_clicks": self.left_clicks,
            "right_clicks": self.right_clicks,
            "double_clicks": self.double_clicks,
            "scrolls": self.scrolls,
            "failures": self.failures,
        }


class _Win32Injector:
    """Raw user32. Cheapest available path: `SetCursorPos` is 0.012ms on this machine."""

    def __init__(self):
        self._u32 = ctypes.windll.user32
        self._held = set()

    def screen_size(self):
        return int(self._u32.GetSystemMetrics(0)), int(self._u32.GetSystemMetrics(1))

    def move(self, x, y):
        self._u32.SetCursorPos(x, y)

    def click(self, down, up):
        # Down and up posted back to back, with no sleep. See the module docstring.
        self._held.add(down)
        self._u32.mouse_event(down, 0, 0, 0, 0)
        self._u32.mouse_event(up, 0, 0, 0, 0)
        self._held.discard(down)

    def wheel(self, delta):
        self._u32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def release_buttons(self):
        """Lifts anything still logically held. See `PointerController.disable`."""
        for down in list(self._held):
            up = MOUSEEVENTF_LEFTUP if down == MOUSEEVENTF_LEFTDOWN else MOUSEEVENTF_RIGHTUP
            try:
                self._u32.mouse_event(up, 0, 0, 0, 0)
            except Exception:
                pass
        self._held.clear()


class _NullInjector:
    """Non-Windows, and the default in the test suite. Records nothing, does nothing."""

    def screen_size(self):
        return 1920, 1080

    def move(self, x, y):
        pass

    def click(self, down, up):
        pass

    def wheel(self, delta):
        pass

    def release_buttons(self):
        pass


class RecordingInjector:
    """
    Captures every action instead of performing it. The test suite's pointer.

    Bounded: `MAX_EVENTS` most recent actions. A synthetic-landmark run can push tens of
    thousands of frames through the pipeline, and a test fixture that grows without limit is
    the same defect the production code is not allowed to have.
    """

    MAX_EVENTS = 4096

    def __init__(self, screen=(1920, 1080)):
        from collections import deque
        self._screen = screen
        self.events = deque(maxlen=self.MAX_EVENTS)

    def screen_size(self):
        return self._screen

    def move(self, x, y):
        self.events.append(("move", x, y))

    def click(self, down, up):
        self.events.append(("click", down, up))

    def wheel(self, delta):
        self.events.append(("wheel", delta, 0))

    def release_buttons(self):
        self.events.append(("release", 0, 0))

    # ── Convenience readers ──

    def moves(self):
        return [(x, y) for kind, x, y in self.events if kind == "move"]

    def count(self, kind):
        return sum(1 for event in self.events if event[0] == kind)

    def left_clicks(self):
        return sum(1 for k, d, _ in self.events
                   if k == "click" and d == MOUSEEVENTF_LEFTDOWN)

    def right_clicks(self):
        return sum(1 for k, d, _ in self.events
                   if k == "click" and d == MOUSEEVENTF_RIGHTDOWN)

    def wheel_deltas(self):
        return [d for k, d, _ in self.events if k == "wheel"]
