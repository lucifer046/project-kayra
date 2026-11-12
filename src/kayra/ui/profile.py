# ┌────────────────────────────────────────────────────────────────────────┐
# │                              profile.py                                │
# │                  Paint Timings, Measured Instead of Guessed            │
# └────────────────────────────────────────────────────────────────────────┘
"""
`KAYRA_UI_PROFILE=1` — every few seconds, one log line with how long each animated component
took to paint and how often it painted:

    [UI] paint over 5s: backdrop 0.41 ms avg / 0.90 max @ 30.0/s · orb 1.20 ms ...

Off (the default) `measure()` returns one shared no-op context manager: no clock read, no
allocation, nothing to pay on a 30 fps paint path.
"""

import os
import time
import threading
from contextlib import contextmanager, nullcontext

REPORT_EVERY_S = 5.0

_ENABLED = str(os.environ.get("KAYRA_UI_PROFILE", "")).strip().lower() in ("1", "true", "yes", "on")
_NULL = nullcontext()
_LOCK = threading.Lock()
_STATS = {}                       # name -> [count, total_s, max_s]
_SINCE = [time.monotonic()]
LAST_REPORT = {}                  # the last reported numbers, for tests and the System screen


def enabled():
    return _ENABLED


def set_enabled(value):
    global _ENABLED
    _ENABLED = bool(value)


@contextmanager
def _timed(name):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        with _LOCK:
            entry = _STATS.setdefault(name, [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += elapsed
            entry[2] = max(entry[2], elapsed)
        _maybe_report()


def measure(name):
    """`with profile.measure("orb"):` around a paint. Free when profiling is off."""
    return _timed(name) if _ENABLED else _NULL


def timed(name):
    """Decorator form, for a `paintEvent`."""
    def decorate(function):
        def wrapper(*args, **kwargs):
            with measure(name):
                return function(*args, **kwargs)
        wrapper.__name__ = function.__name__
        wrapper.__doc__ = function.__doc__
        return wrapper
    return decorate


def _maybe_report():
    now = time.monotonic()
    window = now - _SINCE[0]
    if window < REPORT_EVERY_S:
        return
    with _LOCK:
        stats = {name: tuple(values) for name, values in _STATS.items()}
        _STATS.clear()
        _SINCE[0] = now
    report = {name: {"avg_ms": total / count * 1000.0, "max_ms": peak * 1000.0,
                     "per_s": count / window}
              for name, (count, total, peak) in stats.items() if count}
    LAST_REPORT.clear()
    LAST_REPORT.update(report)
    parts = [f"{name} {r['avg_ms']:.2f} ms avg / {r['max_ms']:.2f} max @ {r['per_s']:.1f}/s"
             for name, r in sorted(report.items())]
    try:
        from kayra.core.logbus import Subsystem, info
        info(Subsystem.UI, f"paint over {window:.0f}s: " + " · ".join(parts))
    except Exception:
        pass
