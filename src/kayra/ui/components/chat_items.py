# ┌────────────────────────────────────────────────────────────────────────┐
# │                           chat_items.py                                │
# │                Conversation Bubbles & Automation Traces                │
# └────────────────────────────────────────────────────────────────────────┘
"""
The pieces a conversation is built from.

WHY BUBBLES ARE ASYMMETRIC RATHER THAN AVATARED
-----------------------------------------------
The user's turn sits right-aligned on an amber-washed surface; Kayra's sits left-aligned on
neutral graphite. Alignment plus surface does the work avatars usually do, at a fraction of the
visual weight — and avoids the two failure modes of avatar chat UIs on a dark ground, which are
a column of bright circles competing with the text and a robot glyph that instantly makes the
product feel like a toy.

AUTOMATION IS NOT A CHAT MESSAGE
--------------------------------
When Kayra acts on the machine, the interesting content is a sequence of verified steps, not a
sentence. Those get a distinct copper-tinted trace block listing what actually happened. The
spoken sentence still appears as a normal reply, because that is what Kayra said; the trace
shows what it did.

The trace is written in the user's terms. Internal identifiers — handler names, action keys,
window handles — stay in the audit log where they belong.
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

from kayra.ui.theme import Color, Font, Space, Radius
from kayra.ui.components.primitives import Caption, _label


# The readable measure for a conversation. Wider than this and the eye loses the line
# return; the assistant's replies used to run the full width of a 1440px window, which is
# roughly 160 characters a line — unreadable for anything longer than a sentence.
MAX_BUBBLE_WIDTH = 560


def _timestamp(when=None):
    return time.strftime("%H:%M", time.localtime(when or time.time()))


class MessageBubble(QFrame):
    """
    One conversational turn.

    `role` is "user", "assistant", "system" or "error"; the stylesheet does the rest via
    objectName, so no colour appears in this file.
    """

    OBJECT_NAMES = {
        "user": "BubbleUser",
        "assistant": "BubbleAssistant",
        "system": "BubbleSystem",
        "error": "BubbleError",
    }

    def __init__(self, role, text, meta=None, stamp=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.setObjectName(self.OBJECT_NAMES.get(role, "BubbleAssistant"))
        self.setMaximumWidth(MAX_BUBBLE_WIDTH)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Space.xxs)

        self.text_label = QLabel(text)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if role == "system":
            self.text_label.setObjectName("Secondary")
        layout.addWidget(self.text_label)

        # THE METADATA GOES IN THE BUBBLE.
        # It used to be a separate Caption in the ROW, bottom-aligned beside the bubble — so
        # every message had a small "00:36" floating in the margin, on the left for the user's
        # turns and on the right for Kayra's. Four ragged columns of timestamps down a
        # transcript is noise, and it made the bubbles look mis-aligned rather than deliberate.
        parts = [p for p in (meta, stamp) if p]
        if parts:
            self.meta_label = _label(" · ".join(parts), "BubbleMeta")
            self.meta_label.setAlignment(
                Qt.AlignRight if role == "user" else Qt.AlignLeft)
            layout.addWidget(self.meta_label)
        else:
            self.meta_label = None

        self._fit_width()

    def _fit_width(self):
        """
        Sizes the bubble to its text, up to the maximum.

        A word-wrapped QLabel reports a tiny width hint — it is willing to wrap to almost
        nothing — so inside a layout with a stretch it collapses to a narrow column and a
        one-line reply wraps over four lines. Measuring the text and asking for that width
        (capped) is what makes short messages short and long messages wrap at a readable
        measure instead of a random one.
        """
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(self.text_label.font())
        ideal = metrics.horizontalAdvance(self.text_label.text())
        if self.meta_label is not None:
            meta_metrics = QFontMetrics(self.meta_label.font())
            ideal = max(ideal, meta_metrics.horizontalAdvance(self.meta_label.text()))
        padding = 2 * Space.base
        self.setMinimumWidth(int(min(MAX_BUBBLE_WIDTH, max(110, ideal + padding))))

    def append_text(self, fragment):
        """
        Grows the bubble as sentences stream in.

        Kayra's replies arrive one spoken sentence at a time from the output tap, so an
        assistant turn is a single bubble that extends rather than a stack of one-line bubbles,
        which is both closer to the truth and far easier to read.
        """
        current = self.text_label.text()
        joined = f"{current} {fragment}".strip() if current else fragment
        self.text_label.setText(joined)
        self._fit_width()


class MessageRow(QWidget):
    """
    Aligns one bubble in the transcript.

    Alignment is the ONLY signal this level carries: the user's turn goes right, everything
    else goes left. The time and the "spoken" marker live inside the bubble, where they line
    up with its edge instead of drifting in the margin.
    """

    def __init__(self, role, text, meta=None, show_time=True, parent=None):
        super().__init__(parent)
        self.role = role

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, Space.xxs, 0, Space.xxs)
        layout.setSpacing(0)

        self.bubble = MessageBubble(role, text, meta,
                                    stamp=_timestamp() if show_time else None)

        if role == "user":
            layout.addStretch(1)
            layout.addWidget(self.bubble)
        else:
            layout.addWidget(self.bubble)
            layout.addStretch(1)

    def append_text(self, fragment):
        self.bubble.append_text(fragment)


class ThinkingRow(QWidget):
    """
    The placeholder shown between a submitted turn and the first reply fragment.

    Three dots whose opacity cycles. It is animated by the caller's existing repaint cadence
    rather than by a timer of its own — one more timer for a transient indicator is exactly the
    kind of cost the performance brief rules out.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0
        self.setFixedHeight(28)

    def advance(self):
        self._phase = (self._phase + 1) % 30
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for index in range(3):
            alpha = 0.25 + 0.55 * abs(((self._phase / 10.0) - index) % 3 - 1.5) / 1.5
            color = QColor(Color.text_tertiary)
            color.setAlphaF(min(1.0, alpha))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(6 + index * 12, 12, 5, 5)
        painter.end()


class AutomationTrace(QFrame):
    """
    A compact, verified record of what Kayra actually did on the machine.

    Steps are (label, status) where status is "ok", "fail", "pending" or "info". The check and
    cross are drawn, not typed, so they align on the baseline and cannot fall back to a missing
    glyph on a machine without the right font.
    """

    MARKS = {"ok": Color.success, "fail": Color.danger,
             "pending": Color.text_tertiary, "info": Color.copper}

    def __init__(self, title="Automation", steps=None, parent=None):
        super().__init__(parent)
        self.setObjectName("AutomationTrace")
        self.setMaximumWidth(MAX_BUBBLE_WIDTH)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(Space.sm, Space.sm, Space.sm, Space.sm)
        self._layout.setSpacing(Space.xs)

        header = _label(title.upper(), "SectionLabel")
        self._layout.addWidget(header)
        self._steps = []

        self._steps_layout = QVBoxLayout()
        self._steps_layout.setSpacing(Space.xxs)
        self._layout.addLayout(self._steps_layout)

        for label, status in (steps or []):
            self.add_step(label, status)

    def add_step(self, label, status="ok"):
        step = _TraceStep(label, status, self.MARKS.get(status, Color.text_tertiary))
        self._steps_layout.addWidget(step)
        self._steps.append(step)
        return step

    def set_step_status(self, index, status):
        """Marks a pending step done or failed as the result arrives."""
        if 0 <= index < len(self._steps):
            self._steps[index].set_status(status, self.MARKS.get(status, Color.text_tertiary))

    def complete_all(self, status="ok"):
        for index in range(len(self._steps)):
            if self._steps[index].status() == "pending":
                self.set_step_status(index, status)


class _TraceStep(QWidget):
    def __init__(self, label, status, color, parent=None):
        super().__init__(parent)
        self._status = status
        self._color = QColor(color)
        self.setFixedHeight(22)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 0, 0)
        layout.setSpacing(Space.sm)
        text = _label(label, "Secondary")
        layout.addWidget(text)
        layout.addStretch(1)

    def status(self):
        return self._status

    def set_status(self, status, color):
        self._status = status
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = painter.pen()
        pen.setColor(self._color)
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        cy = self.height() / 2
        if self._status == "ok":
            painter.drawPolyline([_pt(2, cy), _pt(5, cy + 3.2), _pt(10, cy - 3.4)])
        elif self._status == "fail":
            painter.drawLine(_pt(2.5, cy - 3.2), _pt(9.5, cy + 3.2))
            painter.drawLine(_pt(9.5, cy - 3.2), _pt(2.5, cy + 3.2))
        else:
            painter.setBrush(self._color)
            painter.drawEllipse(_pt(6, cy), 2.0, 2.0)
        painter.end()


def _pt(x, y):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)
