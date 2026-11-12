# ┌────────────────────────────────────────────────────────────────────────┐
# │                         test_smooth_scroll.py                          │
# │   §11 S1: wheel, touchpad and keys glide — and land exactly            │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_smooth_scroll.py — unit tier, headless Qt (QT_QPA_PLATFORM=offscreen), no clock: every
glide is advanced with `step(dt)` at a fixed 1/120 s.

    .venv/bin/python tests/test_smooth_scroll.py
"""

import os
import sys
import ast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, run  # noqa: E402

from PySide6.QtCore import Qt, QPoint, QPointF, QEvent  # noqa: E402
from PySide6.QtGui import QWheelEvent, QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget, QVBoxLayout, QLineEdit  # noqa: E402

from kayra.utils import print_banner  # noqa: E402
from kayra.ui import smooth_scroll as S  # noqa: E402

check = Checker("smooth scrolling")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DT = 1 / 120


def area(height=4000, reduced=False):
    a = QScrollArea()
    a.resize(400, 300)
    inner = QWidget()
    inner.setMinimumHeight(height)
    layout = QVBoxLayout(inner)
    a.field = QLineEdit()
    layout.addWidget(a.field)
    a.setWidget(inner)
    a.setWidgetResizable(True)
    a.show()
    QApplication.processEvents()
    return a, S.install_smooth_scroll(a, reduced=reduced)


def wheel(a, units=0, pixels=0, phase=Qt.NoScrollPhase):
    event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, pixels), QPoint(0, units),
                        Qt.NoButton, Qt.NoModifier, phase, False)
    QApplication.sendEvent(a.viewport(), event)


def key(a, code):
    QApplication.sendEvent(a, QKeyEvent(QEvent.KeyPress, code, Qt.NoModifier))


def settle(s, limit=400):
    trail = []
    for _ in range(limit):
        moving = s.step(DT)
        trail.append(s.bar.value())
        if not moving:
            break
    return trail


def main():
    print_banner("SMOOTH SCROLLING (§11 S1)")
    QApplication.instance() or QApplication(sys.argv)
    a, s = area()
    notch = abs(s._notch_pixels(120))
    wheel(a, units=-120)
    check("a wheel notch does NOT jump: nothing moves until the first frame", a.verticalScrollBar().value() == 0)
    trail = settle(s)
    check("the glide lands EXACTLY on one notch", trail[-1] == round(notch), (trail[-1], notch))
    steps = [b - a_ for a_, b in zip([0] + trail, trail)]
    check("ease-out: the first frame moves more than the last", steps[0] > steps[-2] > 0 or steps[0] > steps[-1], steps[:3] + steps[-3:])
    check("~95 % of the way within ~200 ms (24 frames at 120 Hz)", trail[min(23, len(trail) - 1)] >= 0.95 * notch,
          (trail[:25], notch))
    check("...and every step is under a quarter notch (no visible jump)", max(steps) <= notch / 4 + 1, max(steps))

    a, s = area()
    wheel(a, units=-120)
    s.step(DT)
    s.step(DT)
    mid = a.verticalScrollBar().value()
    wheel(a, units=-120)
    wheel(a, units=-120)
    trail = settle(s)
    check("rapid notches ACCUMULATE into one glide (3 notches, never moving backwards)",
          trail[-1] == round(3 * notch) and all(b >= a_ for a_, b in zip([mid] + trail, trail)), (trail[-1], 3 * notch))
    wheel(a, units=240)
    settle(s)
    check("scrolling back up glides too", a.verticalScrollBar().value() == round(notch), a.verticalScrollBar().value())

    a, s = area(height=500)
    for _ in range(20):
        wheel(a, units=-120)
    settle(s)
    check("a glide stops at the end, never past it", a.verticalScrollBar().value() == a.verticalScrollBar().maximum())

    a, s = area()
    a.setFocus()
    QApplication.processEvents()
    key(a, Qt.Key_PageDown)
    settle(s)
    check("PageDown glides one page", a.verticalScrollBar().value() == a.verticalScrollBar().pageStep())
    key(a, Qt.Key_End)
    settle(s)
    check("End glides to the bottom", a.verticalScrollBar().value() == a.verticalScrollBar().maximum())
    key(a, Qt.Key_Home)
    settle(s)
    check("Home glides to the top", a.verticalScrollBar().value() == 0)
    key(a.field, Qt.Key_Home)
    check("a key the text field uses (Home in a field) stays the field's: no glide",
          s.mode is None and a.verticalScrollBar().value() == 0)

    a, s = area()
    for _ in range(4):
        wheel(a, pixels=-30, phase=Qt.ScrollUpdate)
    check("touchpad: the page follows the fingers 1:1, at once", a.verticalScrollBar().value() == 120 and s.mode == "touch",
          a.verticalScrollBar().value())
    wheel(a, pixels=0, phase=Qt.ScrollEnd)
    check("touchpad: a flick COASTS after the fingers lift", s.mode == "coast" and s.velocity > 0, (s.mode, s.velocity))
    before = a.verticalScrollBar().value()
    trail = settle(s, limit=2000)
    gaps = [b - a_ for a_, b in zip([before] + trail, trail)]
    check("...slows down and stops by itself", s.mode is None and trail[-1] > before and gaps[0] >= gaps[-1], (before, trail[-1]))
    a, s = area()
    wheel(a, pixels=-1, phase=Qt.ScrollUpdate)
    wheel(a, pixels=0, phase=Qt.ScrollEnd)
    check("a slow lift does not coast", s.mode in (None, "touch") and s.velocity == 0, (s.mode, s.velocity, s._touch))

    a, s = area()
    wheel(a, units=-120)
    s.step(DT)
    a.verticalScrollBar().setValue(900)              # Chat following a new reply
    check("something else moving the bar stops the glide and is not fought", s.mode is None and settle(s)[-1] == 900)
    wheel(a, units=-120)
    settle(s)
    check("...and the next notch starts from where it is now", a.verticalScrollBar().value() == 900 + round(notch))

    a, s = area(reduced=True)
    wheel(a, units=-120)
    check("reduced motion: the notch moves at once, no animation", a.verticalScrollBar().value() == round(notch) and s.mode is None)
    a, s = area()
    check("install is idempotent (one scroller per area)", S.install_smooth_scroll(a) is s)
    check("frames are paced from the screen's refresh rate", 1 / 361 <= s._interval_s <= 1 / 29, s._interval_s)

    # Every scroll area in the interface has the helper.
    missing = []
    for base, _dirs, files in os.walk(os.path.join(ROOT, "src", "kayra", "ui")):
        for name in files:
            if not name.endswith(".py") or name == "smooth_scroll.py":
                continue
            source = open(os.path.join(base, name), encoding="utf-8").read()
            tree = ast.parse(source)
            made = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "QScrollArea"]
            installs = source.count("install_smooth_scroll(")
            if len(made) > installs:
                missing.append(name)
    check("no scroll area in the interface is left without the glide", not missing, missing)
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
