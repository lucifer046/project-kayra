# ┌────────────────────────────────────────────────────────────────────────┐
# │                              timing.py                                 │
# │                  Boot Instrumentation & Wall Clock                     │
# └────────────────────────────────────────────────────────────────────────┘
"""
`StageTimer` records how long each boot stage took and prints the breakdown at startup, and
`now_ms()` is the single wall-clock helper the audio layer times capture windows against.

Both are here rather than in the modules that use them because the boot stages are measured
across four different subsystems, and a timer that each subsystem owned a copy of could not
produce one coherent report.
"""

import time as _time
import threading as _threading

from rich.rule import Rule

from kayra.utils.console import console, safe_print


class StageTimer:
    """
    Monotonic stage stopwatch. `mark()` records the elapsed time since construction
    for a named milestone; `report()` renders every mark in order.

    Thread-safe: boot stages are marked from several worker threads in parallel.
    """

    def __init__(self, label: str = "startup"):
        self.label = label
        self.t0 = _time.perf_counter()
        self._marks = []
        self._lock = _threading.Lock()

    def mark(self, name: str, quiet: bool = False) -> float:
        """Records a milestone and returns seconds elapsed since the timer started."""
        elapsed = _time.perf_counter() - self.t0
        with self._lock:
            self._marks.append((name, elapsed))
        if not quiet:
            safe_print(f"[dim]\[TIMING][/dim] [text]{name}[/text] [dim]+{elapsed:.2f}s[/dim]")
        return elapsed

    def elapsed(self) -> float:
        """Seconds since the timer was created."""
        return _time.perf_counter() - self.t0

    def report(self, title: str = None):
        """Prints every recorded mark, in the order it was recorded."""
        with self._lock:
            marks = list(self._marks)
        if not marks:
            return
        console.print()
        console.print(Rule(f"[bold white]{(title or self.label).upper()} TIMINGS[/bold white]",
                           style="dim magenta", align="left"))
        for name, elapsed in marks:
            console.print(f"  [dim]+{elapsed:6.2f}s[/dim]  [text]{name}[/text]")
        console.print()


def now_ms() -> float:
    """
    Wall-clock milliseconds since the epoch.

    Deliberately wall-clock (not monotonic) because these timestamps are compared
    against JavaScript `Date.now()` values produced inside the headless-Chrome STT
    page — both clocks are the same host clock, so the two are directly comparable.
    """
    return _time.time() * 1000.0
