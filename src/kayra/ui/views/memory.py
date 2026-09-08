# ┌────────────────────────────────────────────────────────────────────────┐
# │                             memory.py                                  │
# │                What Kayra Remembers, And How To Remove It              │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything Kayra has kept, in plain language, with a way to delete it.

PRIVACY IS THE FEATURE
----------------------
This screen exists so a user can answer "what does it know about me?" without opening a JSON
file. So it states, on screen, the two facts that matter most and that are genuinely true of
this system:

  * Long-term conversation memory is only written when the user explicitly asks ("remember
    this", "save this", …). Kayra does not record conversations by default.
  * The habit store holds COUNTERS and hour histograms — never transcripts. `_habit_key` in the
    proactive agent drops the payload of every conversational intent before it is recorded, so
    "open:chrome" can be stored and "general what is my bank balance" cannot.

Neither claim is decoration; both are enforced in the backend, and the screen would be
dishonest without them.

DELETION IS REAL
----------------
Removing an item rewrites the store through the same atomic helpers the backend uses
(`save_conversation_memory`, which writes the backup first and then swaps). Nothing here
pretends to delete something it merely hides.
"""

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy

from kayra.ui.theme import Color, Space
from kayra.ui.components.primitives import (
    ListRow, CardAction,
    Card, Caption, Secondary, StatusPill, GhostButton, DangerButton, EmptyState,
    Divider, SectionLabel, _label,
)
from kayra.ui.views.base import View


class MemoryView(View):
    title = "Memory"
    subtitle = "What Kayra has kept, and how to remove it."

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._build_privacy_note()
        self._build_saved()
        self._build_habits()
        # No trailing stretch: `_build_habits` gives the last card a stretch factor, and a
        # stretch after it would win the leftover height back and strand the page above a band
        # of bare background again.

    # ──────────────────────────────────────────────────────────────────

    def _build_privacy_note(self):
        card = Card("Privacy", flat=True)
        card.body.addWidget(Secondary(
            "Kayra does not record your conversations. A conversation is only written to "
            "long-term memory when you explicitly ask it to — by saying \"remember this\", "
            "\"save this\" or \"note this\"."))
        card.body.addWidget(Secondary(
            "Learned routines store counts and times of day only. What you said is never "
            "part of them."))
        self.content.addWidget(card)

    def _build_saved(self):
        self.saved_card = Card("Saved by you")
        self.saved_pill = StatusPill("—", "neutral")
        self.saved_card.add_header_widget(self.saved_pill)

        clear = CardAction("Clear all", tone="danger")
        clear.clicked.connect(self._clear_saved)
        self.saved_card.add_header_widget(clear)

        self.saved_body = QVBoxLayout()
        self.saved_body.setSpacing(Space.xs)
        self.saved_card.body.addLayout(self.saved_body)
        self.content.addWidget(self.saved_card)

    def _build_habits(self):
        self.habits_card = Card("Learned routines")
        self.habits_pill = StatusPill("—", "neutral")
        self.habits_card.add_header_widget(self.habits_pill)
        self.habits_card.body.addWidget(Caption(
            "Used by the proactive agent to notice when something is usually due. "
            "Counts and hours only."))
        self.habits_body = QVBoxLayout()
        self.habits_body.setSpacing(Space.xxs)
        self.habits_card.body.addLayout(self.habits_body)
        self.habits_card.body.addStretch(1)
        # The last card takes the remaining height. Memory used to end 460px above the bottom
        # of the window, leaving a band of bare background that reads as a failed render.
        self.content.addWidget(self.habits_card, 1)

    # ──────────────────────────────────────────────────────────────────

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _refresh(self):
        self._refresh_saved()
        self._refresh_habits()

    def _refresh_saved(self):
        self._clear_layout(self.saved_body)
        entries = self.bridge.conversation_memory()

        if not entries:
            self.saved_pill.set_status("Nothing saved", "neutral")
            self.saved_body.addWidget(EmptyState(
                "Nothing saved yet",
                "Say \"remember this\" during a conversation and it will be kept here."))
            return

        count = len(entries)
        self.saved_pill.set_status(f"{count} item{'s' if count != 1 else ''}", "neutral")
        for index, entry in enumerate(entries[-30:]):
            self.saved_body.addWidget(_MemoryRow(entry, index, self._delete_saved))

    def _refresh_habits(self):
        self._clear_layout(self.habits_body)
        store = self.bridge.habits() or {}
        actions = (store.get("actions") or {}) if isinstance(store, dict) else {}

        if not actions:
            self.habits_pill.set_status("Nothing learned", "neutral")
            self.habits_body.addWidget(EmptyState(
                "No routines yet",
                "Kayra notices patterns after it has seen the same action a few times."))
            return

        ranked = sorted(actions.items(),
                        key=lambda kv: _count_of(kv[1]), reverse=True)[:12]
        self.habits_pill.set_status(f"{len(actions)} tracked", "neutral")
        for key, value in ranked:
            self.habits_body.addWidget(_HabitRow(key, _count_of(value), _peak_hour(value)))

    # ──────────────────────────────────────────────────────────────────

    def _delete_saved(self, index):
        from kayra.memory.conversation import load_conversation_memory, save_conversation_memory
        entries = load_conversation_memory() or []
        recent = entries[-30:]
        if 0 <= index < len(recent):
            target = recent[index]
            try:
                entries.remove(target)
            except ValueError:
                return
            save_conversation_memory(entries)
            self._refresh_saved()

    def _clear_saved(self):
        from PySide6.QtWidgets import QMessageBox
        from kayra.memory.conversation import save_conversation_memory

        box = QMessageBox(self)
        box.setWindowTitle("Clear saved memory")
        box.setText("Delete everything Kayra has saved?")
        box.setInformativeText("This removes the long-term conversation memory permanently. "
                               "Learned routines are not affected.")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() == QMessageBox.Yes:
            save_conversation_memory([])
            self._refresh_saved()

    def on_show(self):
        self._refresh()


def _count_of(value):
    if isinstance(value, dict):
        return int(value.get("count", 0) or 0)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _peak_hour(value):
    """The hour a routine most often happens, from the 24-bucket histogram."""
    if not isinstance(value, dict):
        return None
    hours = value.get("hours")
    if not isinstance(hours, list) or len(hours) != 24 or not any(hours):
        return None
    return max(range(24), key=lambda h: hours[h])


class _MemoryRow(ListRow):
    """
    One saved memory: who said it, what it was, and how to remove it.

    `"User: remember my flight is on the 4th"` was what this rendered — the JSON record's own
    `role` field, capitalised and glued to the front of the text with a colon. That is the
    stored structure showing through, which is exactly what a privacy screen must not do: the
    person is being shown their data, not Kayra's schema. The speaker is now its own quiet
    element and reads as English.
    """

    SPEAKER = {"user": "You", "assistant": "Kayra", "system": "Kayra"}

    def __init__(self, entry, index, on_delete, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(38)

        if isinstance(entry, dict):
            role = str(entry.get("role", "")).strip().lower()
            text = str(entry.get("content", "") or "")
        else:
            role, text = "", str(entry)

        speaker = self.SPEAKER.get(role, "")
        self._text = " ".join(text.split())

        if speaker:
            who = _label(speaker, "Caption")
            who.setFixedWidth(44)
            self.row.addWidget(who)

        self._body = Secondary("")
        self._body.setWordWrap(False)
        self._body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._body.setToolTip(self._text)
        self.row.addWidget(self._body, 1)

        remove = GhostButton("Remove")
        remove.setFixedWidth(78)
        remove.setToolTip("Delete this memory permanently")
        remove.clicked.connect(lambda: on_delete(index))
        self.row.addWidget(remove)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self._body.font())
        self._body.setText(metrics.elidedText(self._text, Qt.ElideRight,
                                              max(60, self._body.width() - 4)))


class _HabitRow(ListRow):
    """One learned routine: what it is, when it usually happens, how often."""

    def __init__(self, key, count, hour, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(34)

        readable = str(key).replace(":", " ").replace("_", " ")
        self.row.addWidget(Secondary(readable[:1].upper() + readable[1:]), 1)
        if hour is not None:
            when = Caption(f"usually around {hour:02d}:00")
            when.setWordWrap(False)      # this used to wrap onto two lines beside the count
            self.row.addWidget(when)
        pill = StatusPill(f"{count}×", "neutral")
        pill.setFixedWidth(52)
        self.row.addWidget(pill)
