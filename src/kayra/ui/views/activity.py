# ┌────────────────────────────────────────────────────────────────────────┐
# │                            activity.py                                 │
# │                  A Session Timeline, Lightly Filtered                  │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything that happened this session, newest first.

SESSION-SCOPED ON PURPOSE
-------------------------
This is a live timeline held in memory and capped, not a durable log. Kayra deliberately does
not persist a record of everything it hears — that is the same privacy position the Memory
screen states — so a history that survived restarts would either have to start recording
conversations or lie about being complete. `logs/` holds the durable technical record for
anyone who needs it.

The cap is enforced on insertion. An assistant that runs all day would otherwise accumulate
widgets indefinitely, which is a memory leak with a nice appearance.
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy

from kayra.ui.theme import Color, Space, repolish
from kayra.ui.components.primitives import (
    Card, Caption, Secondary, StatusPill, CardAction, EmptyState, SegmentedControl,
    ListRow, _label,
)
from kayra.ui.views.base import View


MAX_ENTRIES = 200

CATEGORIES = (
    ("all", "All"),
    ("conversation", "Conversation"),
    ("automation", "Automation"),
    ("system", "System"),
)


class ActivityView(View):
    title = "Activity"
    subtitle = "What happened in this session."

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._entries = []          # (category, tone, label, text, timestamp)
        self._filter = "all"

        self._build_filters()

        self.list_card = Card("Timeline")
        self.count_pill = StatusPill("0 events", "neutral")
        self.list_card.add_header_widget(self.count_pill)
        clear = CardAction("Clear")
        clear.clicked.connect(self._clear)
        self.list_card.add_header_widget(clear)

        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        self.list_card.body.addLayout(self.body)

        # Permanent, and outside the layout that `_render` tears down every pass — which is
        # what makes "show or hide" possible instead of "create and destroy".
        self._empty = EmptyState("Nothing yet", "Activity from this session appears here.")
        self.list_card.body.addWidget(self._empty)
        self.list_card.body.addStretch(1)

        # Nearly 500px of the 880px page used to be empty background below this card. A
        # timeline should occupy the page it is the subject of.
        self.content.addWidget(self.list_card, 1)

        bridge.userMessage.connect(
            lambda text, source: self._add("conversation", "accent",
                                           "You" + (" (voice)" if source == "voice" else ""), text))
        bridge.assistantMessage.connect(
            lambda text: self._add("conversation", "neutral", "Kayra", text))
        bridge.automationStarted.connect(
            lambda commands: self._add("automation", "accent", "Automation",
                                       ", ".join(str(c) for c in commands)))
        bridge.systemMessage.connect(
            lambda text, tone: self._add("system", "warning", "System", text))
        bridge.errorOccurred.connect(
            lambda message: self._add("system", "danger", "Error", message))
        bridge.bootFinished.connect(
            lambda ok, detail: self._add("system", "success" if ok else "danger",
                                         "Startup", detail))
        bridge.stateChanged.connect(self._on_state)

        self._render()

    def _build_filters(self):
        """
        One segmented control.

        These were four full-height QPushButtons in a row: 38px tall, bordered, evenly
        weighted — which made the filters the loudest thing on a page whose entire purpose is
        the content below them, and read as four unrelated actions rather than four modes of
        one thing.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Space.sm)

        self._segmented = SegmentedControl(list(CATEGORIES), current="all")
        self._segmented.changed.connect(self._set_filter)
        layout.addWidget(self._segmented)
        layout.addStretch(1)
        self.content.addWidget(row)

    def _set_filter(self, key):
        self._filter = key
        self._render()

    def _on_state(self, state, previous):
        # Only transitions worth reading. Recording every IDLE/LISTENING flip would bury the
        # events a person actually cares about under machine chatter.
        if state in ("AUTOMATING", "ERROR", "SHUTTING_DOWN"):
            self._add("system", "neutral", "State", state.title())

    def _add(self, category, tone, label, text):
        text = (text or "").strip()
        if not text:
            return
        self._entries.append((category, tone, label, text, time.time()))
        if len(self._entries) > MAX_ENTRIES:
            del self._entries[:-MAX_ENTRIES]
        if self.isVisible():
            self._render()

    def _clear(self):
        self._entries.clear()
        self._render()

    def _render(self):
        """
        Rebuilds the visible timeline.

        THE BUG THIS FIXES. `takeAt` removes an item from the LAYOUT; it does not delete or
        even hide the widget, and `deleteLater` only runs when control returns to the event
        loop. The empty state added on a previous pass was therefore still a visible child of
        this widget, un-laid-out at its last geometry, while the new rows were laid out on top
        of it — so "Nothing yet" rendered straight through the middle of the timeline text.
        That is the overlapping-widgets fault a rendered review catches and a signal test
        never will.

        Hiding the widget as it leaves the layout closes the window between removal and
        deletion, and the empty state is now a permanent child that is shown or hidden rather
        than created and destroyed.
        """
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setVisible(False)
                widget.deleteLater()

        visible = [e for e in self._entries
                   if self._filter == "all" or e[0] == self._filter]
        self.count_pill.set_status(
            f"{len(visible)} event{'s' if len(visible) != 1 else ''}", "neutral")

        # Live counts on the segments, so the filter says what it will show before it is used.
        for key, _label_text in CATEGORIES:
            count = (len(self._entries) if key == "all"
                     else sum(1 for e in self._entries if e[0] == key))
            self._segmented.set_count(key, count)

        if not visible:
            self._empty.set_message(
                "Nothing yet" if not self._entries else "Nothing in this filter",
                "Activity from this session appears here." if not self._entries
                else "Try another category, or clear the filter.")
            self._empty.setVisible(True)
            return

        self._empty.setVisible(False)
        for category, tone, label, text, stamp in reversed(visible[-60:]):
            self.body.addWidget(_ActivityRow(tone, label, text, stamp))

    def on_show(self):
        self._render()


class _ActivityRow(ListRow):
    """One timeline entry: source, what happened, when."""

    def __init__(self, tone, label, text, stamp, parent=None):
        super().__init__(parent)

        pill = StatusPill(label, tone)
        # A single chip width down the column. Variable-width chips made the text column step
        # in and out on every row, which is what turned this list into a ragged log.
        pill.setFixedWidth(104)

        self._text = " ".join((text or "").split())
        self._body = Secondary("")
        self._body.setWordWrap(False)
        self._body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._body.setToolTip(self._text)

        self.row.addWidget(pill)
        self.row.addWidget(self._body, 1)
        # Minutes, not seconds. Second-precision on a conversation timeline is noise: nobody
        # reads it, and it widens every row by three characters.
        self.row.addWidget(Caption(time.strftime("%H:%M", time.localtime(stamp))))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self._body.font())
        self._body.setText(metrics.elidedText(self._text, Qt.ElideRight,
                                              max(60, self._body.width() - 4)))
