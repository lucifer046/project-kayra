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

    # How many memories are built as widgets. The count in the header is the TRUE total; this
    # bounds only what is rendered, so a store grown over months cannot turn one navigation
    # into thousands of widget constructions.
    MAX_ROWS = 50

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

        # The empty state is a PERMANENT child, shown and hidden — never created and
        # destroyed. `QLayout.takeAt` removes an item from the layout without hiding or
        # deleting the widget, and `deleteLater` only runs when control returns to the event
        # loop, so an empty state "removed" on one render pass is still a visible child at its
        # stale geometry with the new rows laid out on top of it.
        self.saved_empty = EmptyState(
            "Nothing saved yet",
            "Say \"remember this\" during a conversation and it will be kept here.")
        self.saved_card.body.addWidget(self.saved_empty)

        self.content.addWidget(self.saved_card)
        self._build_location()

    def _build_location(self):
        """
        Where memory actually lives, and a way to get there.

        The path is READ FROM THE BACKEND, never composed here — `core.paths` is the single
        source of truth for every filesystem location in Kayra, and a settings screen that
        hardcoded `data\\conversation.json` would be exactly the bare relative path that once
        fragmented the assistant's memory across several files.
        """
        card = Card("Memory storage", flat=True)

        self.location_label = Secondary("—")
        self.location_label.setWordWrap(True)
        card.body.addWidget(self.location_label)

        self.location_detail = Caption("")
        self.location_detail.setWordWrap(True)
        card.body.addWidget(self.location_detail)

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, Space.sm, 0, 0)
        layout.setSpacing(Space.sm)
        layout.addStretch(1)

        open_button = GhostButton("Open file location")
        open_button.setToolTip("Show the memory file in File Explorer")
        open_button.clicked.connect(self._open_location)
        layout.addWidget(open_button)

        card.body.addWidget(row)
        self.content.addWidget(card)

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
        self._refresh_location()
        self._refresh_habits()

    def _refresh_saved(self):
        """
        Rebuilds the list from `list_memories()`, which carries a stable id per record.

        BOUNDED ON PURPOSE. Only the most recent `MAX_ROWS` are built as widgets; the pill
        reports the true total. A store grown over months would otherwise create thousands of
        widgets on every navigation to this screen, which is the cost this screen is least
        able to afford — it is a privacy surface people open and close, not one they live in.
        """
        self._clear_layout(self.saved_body)
        entries = self.bridge.list_memories()

        if not entries:
            self.saved_pill.set_status("Nothing saved", "neutral")
            self.saved_empty.set_message(
                "Nothing saved yet",
                "Say \"remember this\" during a conversation and it will be kept here.")
            self.saved_empty.setVisible(True)
            return

        self.saved_empty.setVisible(False)
        count = len(entries)
        self.saved_pill.set_status(f"{count} item{'s' if count != 1 else ''}", "neutral")
        for entry in entries[:self.MAX_ROWS]:
            self.saved_body.addWidget(_MemoryRow(entry, self._delete_saved))

    def _refresh_location(self):
        described = self.bridge.memory_store() or {}
        path = described.get("path") or "unknown"
        self.location_label.setText(path)
        self.location_label.setToolTip(path)          # the full path, however long
        if described.get("exists"):
            size = described.get("size_bytes", 0)
            self.location_detail.setText(
                f"{described.get('count', 0)} entries · "
                f"{size / 1024:.1f} KB · a rolling backup is kept beside it")
        else:
            self.location_detail.setText(
                "The file is created the first time you ask Kayra to remember something.")

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

    def _delete_saved(self, memory_id):
        """
        Deletes ONE memory, by id, after confirming — and only redraws once it is really gone.

        BY ID, NEVER BY POSITION. The store is appended to by the running assistant, so the
        entry at row 4 when this screen rendered is not necessarily the entry at row 4 when
        the button is clicked. Deleting the wrong memory has no undo, which is why identity
        had to become a property of the record rather than of the list.

        THE ROW STAYS UNTIL PERSISTENCE SUCCEEDS. `delete_memory` returns False when the write
        failed; the screen then says so and leaves the row where it is. A UI that removes a
        row on click and finds it back after a restart is worse than one that admits the
        failure.
        """
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Delete memory")
        box.setText("Delete this memory?")
        box.setInformativeText("It is removed from Kayra's long-term memory permanently.")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return

        deleted, detail = self.bridge.delete_memory(memory_id)
        if not deleted:
            self.saved_pill.set_status("Delete failed", "warning")
            self.saved_empty.set_message("Could not delete that memory",
                                         detail or "The memory store could not be written.")
            self.saved_empty.setVisible(True)
            return
        self._refresh()

    def _clear_saved(self):
        """
        Empties the store, behind a deliberately blunt confirmation.

        The wording states the consequence rather than the action, and the default button is
        Cancel: this is the one control on the screen that can destroy months of the user's
        data in a click, and it should read like it.
        """
        from PySide6.QtWidgets import QMessageBox

        total = (self.bridge.memory_store() or {}).get("count", 0)
        if not total:
            return

        box = QMessageBox(self)
        box.setWindowTitle("Clear all memories")
        box.setText("Delete all stored memories? This cannot be undone.")
        box.setInformativeText(
            f"All {total} saved entries are removed from Kayra's long-term memory "
            f"permanently. Learned routines are not affected.")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return

        cleared, ok = self.bridge.clear_memories()
        if not ok:
            self.saved_pill.set_status("Clear failed", "warning")
            return
        self.saved_pill.set_status(f"Cleared {cleared}", "neutral")
        self._refresh()

    def _open_location(self):
        """
        Reveals the memory file in File Explorer.

        On failure the exact path is put on screen, because "could not open Explorer" without
        the path leaves the user unable to do the thing they were trying to do; with it, they
        can navigate there by hand.
        """
        ok, detail = self.bridge.open_memory_location()
        if not ok:
            self.location_detail.setText(f"Could not open File Explorer. The file is at:\n{detail}")

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

    def __init__(self, entry, on_delete, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(38)

        # `entry` comes from `memory.store.list_memories`, so it always carries an id. The row
        # holds that id and nothing positional: the delete button knows WHICH memory it
        # removes, not merely which row it sits in.
        self._memory_id = str(entry.get("id", ""))
        role = str(entry.get("role", "")).strip().lower()
        text = str(entry.get("content", "") or "")

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
        remove.clicked.connect(lambda: on_delete(self._memory_id))
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
