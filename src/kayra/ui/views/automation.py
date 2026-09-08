# ┌────────────────────────────────────────────────────────────────────────┐
# │                           automation.py                                │
# │              What Kayra Is Doing To The Machine, And Did               │
# └────────────────────────────────────────────────────────────────────────┘
"""
The automation centre: the current task as a pipeline, and the verified history behind it.

WHERE THE DATA COMES FROM
-------------------------
`automation.policy.recent_audit()` — the bounded ring the backend already writes for every
automation decision. The UI keeps no history of its own. A second record would be a second
source of truth that could disagree with the audit log, and the audit log is the one the
safety layer actually writes.

WHAT IS DELIBERATELY NOT SHOWN
------------------------------
Handler names, action keys, window handles and parameter dictionaries. Those exist in the audit
entry and belong in `logs/automation.log`. This screen answers "what did it do to my computer",
which is a question about the machine, not about Python.

The pipeline visual mirrors the real stages the automation layer runs — normalize, policy,
resolve, plan, execute, verify — because that IS the sequence, and showing a truthful pipeline
is what makes a failure legible: a task that stopped at "resolve" was ambiguous, and one that
stopped at "policy" was refused.
"""

import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy

from kayra.ui.theme import Color, Space, Motion, Font
from kayra.ui.components.primitives import (
    Card, Caption, Secondary, StatusPill, SectionLabel, EmptyState, Divider,
    ListRow, ActionStatus, _label,
)
from kayra.ui.views.base import View


STAGES = ("Understand", "Check policy", "Find target", "Plan", "Execute", "Verify")


class Pipeline(QWidget):
    """
    The six-stage automation pipeline as a horizontal track.

    `set_progress(index, failed)` marks how far the current task got. A failed stage is drawn
    in red and the track stops there — which is the whole point: a stalled pipeline shows you
    where it stalled.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._index = -1
        self._failed = False
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_progress(self, index, failed=False):
        self._index = index
        self._failed = failed
        self.update()

    def reset(self):
        self._index = -1
        self._failed = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        count = len(STAGES)
        if count < 2:
            return
        margin = 14
        span = self.width() - margin * 2
        step = span / (count - 1)
        cy = 22

        pen = QPen(QColor(Color.border))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.drawLine(margin, cy, self.width() - margin, cy)

        if self._index >= 0:
            done_pen = QPen(QColor(Color.danger if self._failed else Color.copper))
            done_pen.setWidthF(1.8)
            painter.setPen(done_pen)
            painter.drawLine(margin, cy, margin + step * min(self._index, count - 1), cy)

        font = painter.font()
        font.setPixelSize(Font.micro)
        painter.setFont(font)

        for i, stage in enumerate(STAGES):
            x = margin + step * i
            reached = self._index >= i
            is_current = self._index == i

            if reached and self._failed and is_current:
                color = QColor(Color.danger)
            elif reached:
                color = QColor(Color.automation)
            else:
                color = QColor(Color.border_strong)

            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            radius = 5.0 if is_current else 3.5
            painter.drawEllipse(QPointF(x, cy), radius, radius)

            # LABEL CLIPPING, AND THE FIX.
            # The label rect used to be centred on the dot: `x - step/2` wide by `step`. For
            # the first stage that starts at a NEGATIVE x, so Qt clipped the left of the word
            # and the pipeline's first step rendered as "derstand". The rect is now clamped
            # into the widget and the text aligned to whichever edge it is against, so the
            # end labels read correctly at any width.
            painter.setPen(QColor(Color.text_secondary if reached else Color.text_tertiary))
            left = x - step / 2
            align = Qt.AlignHCenter
            if left < 0:
                left, align = 0.0, Qt.AlignLeft
            elif left + step > self.width():
                left, align = self.width() - step, Qt.AlignRight
            painter.drawText(QRectF(left, cy + 12, step, 24), align | Qt.AlignTop, stage)
        painter.end()


class AutomationView(View):
    title = "Automation"
    subtitle = "What Kayra is doing on this machine, and what it has already done."

    # Audit event names the backend writes, mapped to the pipeline stage they represent.
    STAGE_FOR_EVENT = {
        "normalized": 0, "policy": 1, "denied": 1, "confirm_required": 1,
        "resolved": 2, "ambiguous": 2, "not_found": 2, "unavailable": 2,
        "planned": 3, "executing": 4, "executed": 4, "failed": 4, "verified": 5,
    }
    FAILURE_EVENTS = frozenset({"denied", "failed", "not_found", "unavailable", "ambiguous"})

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._seen = 0

        self.current_card = Card("Current task")
        self.current_pill = StatusPill("Idle", "neutral")
        self.current_card.add_header_widget(self.current_pill)
        self.current_label = Secondary("Nothing running.")
        self.pipeline = Pipeline()
        self.current_card.body.addWidget(self.current_label)
        self.current_card.body.addWidget(self.pipeline)
        self.content.addWidget(self.current_card)

        self.history_card = Card("Recent actions")
        self.history_pill = StatusPill("—", "neutral")
        self.history_card.add_header_widget(self.history_pill)
        self.history_body = QVBoxLayout()
        self.history_body.setSpacing(Space.xxs)
        self.history_card.body.addLayout(self.history_body)
        self._history_empty = EmptyState(
            "No automation yet",
            "Ask Kayra to open an app, manage a window or control media.")
        self.history_body.addWidget(self._history_empty)
        self.history_card.body.addStretch(1)

        # The history is a LIST, so it takes the height the page has rather than sitting in a
        # short card above 450px of empty background — which was more than half this screen at
        # 900px tall and read as a page that had failed to render.
        self.content.addWidget(self.history_card, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(Motion.activity_interval)
        self._timer.timeout.connect(self._refresh)

        bridge.automationStarted.connect(self._on_started)
        bridge.stateChanged.connect(self._on_state)

    def _on_started(self, commands):
        readable = " · ".join(humanise_command(str(c)) for c in commands if str(c).strip())
        self.current_label.setText(readable or "Working…")
        self.current_pill.set_status("Running", "accent")
        self.pipeline.set_progress(0)

    def _on_state(self, state, previous):
        if state == "AUTOMATING":
            self.current_pill.set_status("Running", "accent")
        elif previous == "AUTOMATING":
            self.current_pill.set_status("Idle", "neutral")
            self.current_label.setText("Nothing running.")
            self.pipeline.reset()

    def _refresh(self):
        """
        Rebuilds the history only when the audit ring actually grew.

        The ring is bounded and this screen refreshes on a timer, so rebuilding unconditionally
        would discard and recreate the same widgets forever — the "constantly rebuilding
        widgets" cost the brief calls out. Comparing the length first makes the common case a
        single integer comparison.
        """
        entries = self.bridge.recent_automation(40)
        if len(entries) == self._seen:
            return
        self._seen = len(entries)

        collapsed = collapse_audit(entries)

        # Hidden, never destroyed: a cleared history has to be able to show it again, and a
        # blank panel is indistinguishable from a broken one.
        if self._history_empty is not None:
            self._history_empty.setVisible(not entries)

        while self.history_body.count() > 1:
            item = self.history_body.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        for entry in reversed(collapsed[-14:]):
            self.history_body.addWidget(_AuditRow(entry))

        # Count the COLLAPSED rows, not the raw audit entries. "4 actions" over two visible
        # rows is the header disagreeing with the list underneath it.
        total = len(collapsed)
        noun = "action" if total == 1 else "actions"
        failures = sum(1 for e in collapsed if e.get("event") in self.FAILURE_EVENTS)
        if failures:
            self.history_pill.set_status(f"{total} {noun} · {failures} blocked", "warning")
        else:
            self.history_pill.set_status(f"{total} {noun}", "neutral")

        # Drive the pipeline from the newest entry, so a live task animates through the stages.
        if entries:
            newest = entries[-1]
            stage = self.STAGE_FOR_EVENT.get(newest.get("event"))
            if stage is not None:
                self.pipeline.set_progress(stage,
                                           newest.get("event") in self.FAILURE_EVENTS)

    def on_show(self):
        self._seen = -1        # force one rebuild on entry
        self._refresh()
        self._timer.start()

    def on_hide(self):
        self._timer.stop()


# Internal action keys -> what actually happened to the machine. The audit ring stores
# `app.open` / `system.shutdown` because that is the policy layer's vocabulary; putting those
# on screen tells the user about Python rather than about their computer, which is exactly what
# this screen is not for. Anything unmapped falls back to a de-underscored form, so a new
# handler degrades to something readable instead of disappearing.
ACTION_VERBS = {
    "app.open": "Open", "app.close": "Close", "app.close_all": "Close all",
    "app.kill": "Force-close", "app.restart": "Restart", "app.content": "Write",
    "window.close": "Close window", "window.close_all": "Close every window",
    "window.focus": "Switch to", "window.minimize": "Minimise",
    "window.minimize_all": "Show desktop", "window.maximize": "Maximise",
    "window.restore": "Restore", "window.snap_left": "Snap left",
    "window.snap_right": "Snap right", "window.alt_tab": "Switch window",
    "window.task_view": "Task view", "window.action_center": "Notifications",
    "browser.open_url": "Open", "browser.close_tab": "Close tab",
    "browser.new_tab": "New tab", "browser.next_tab": "Next tab",
    "browser.previous_tab": "Previous tab", "browser.reopen_tab": "Reopen tab",
    "browser.refresh": "Refresh", "browser.back": "Go back", "browser.forward": "Go forward",
    "browser.search_web": "Search the web for", "browser.search_youtube": "Search YouTube for",
    "media.play": "Play", "media.pause": "Pause", "media.resume": "Resume",
    "media.next": "Next track", "media.previous": "Previous track", "media.stop": "Stop media",
    "system.shutdown": "Shut down", "system.restart": "Restart the computer",
    "system.sign_out": "Sign out", "system.sleep": "Sleep", "system.lock": "Lock",
    "system.volume": "Volume", "system.brightness": "Brightness",
    "system.wifi_on": "Wi-Fi on", "system.wifi_off": "Wi-Fi off",
    "file.open_folder": "Open folder", "file.open_file": "Open file",
    "file.create_folder": "New folder", "file.create_file": "New file",
    "file.delete": "Delete", "file.rename": "Rename", "file.search": "Find",
    "clipboard.copy": "Copy", "clipboard.paste": "Paste", "clipboard.set": "Copy text",
    "screen.screenshot": "Screenshot", "timer.add": "Set a timer",
    "timer.cancel": "Cancel timer", "keyboard.type": "Type", "keyboard.hotkey": "Shortcut",
    "shell.run": "Run a command", "info.battery": "Battery", "info.cpu": "Processor",
    "info.ram": "Memory", "info.disk": "Storage",
}


def humanise_action(action, target=""):
    """`app.open` + `chrome` -> `Open Chrome`. Never shows a dotted key."""
    key = str(action or "").strip()
    verb = ACTION_VERBS.get(key)
    if verb is None:
        verb = key.split(".")[-1].replace("_", " ").strip().capitalize() or "Action"
    target = str(target or "").strip()
    if not target or target.lower() in ("current", "none"):
        return verb
    if target.startswith(("http://", "https://")):
        target = target.split("//", 1)[-1].rstrip("/")
    return f"{verb} {target[:1].upper() + target[1:]}"


def humanise_command(token):
    """A DMM token -> a phrase. Used for the current task line."""
    token = str(token or "").strip()
    lowered = token.lower()
    for prefix, phrasing in (
        ("open ", "Open {}"), ("close all ", "Close all {}"), ("close ", "Close {}"),
        ("play ", "Play {}"), ("focus ", "Switch to {}"),
        ("google search ", "Search the web for {}"),
        ("youtube search ", "Search YouTube for {}"),
        ("content ", "Write {}"), ("system ", "{}"), ("set timer ", "Set a timer for {}"),
    ):
        if lowered.startswith(prefix):
            rest = token[len(prefix):].strip()
            return phrasing.format(rest[:1].upper() + rest[1:] if rest else "…")
    return token[:1].upper() + token[1:] if token else ""


# Ranks the audit events by how far through the pipeline they are, so collapsing a run of
# entries for one action keeps the FURTHEST one — which is the outcome — rather than whichever
# happened to be written last.
_EVENT_RANK = {
    "normalized": 0, "policy": 1, "planned": 2, "resolved": 3, "executing": 4,
    "executed": 5, "verified": 6,
    # Terminal states outrank everything: an action that was denied did not then succeed.
    "confirm_required": 10, "ambiguous": 11, "not_found": 11, "unavailable": 11,
    "failed": 12, "denied": 13,
}


def collapse_audit(entries):
    """
    One row per ACTION, not one row per pipeline stage.

    The audit ring records every stage the automation layer passes through, so a single
    "open chrome" writes `normalized`, `resolved` and `executed` — three consecutive rows
    saying the same thing with a different chip. That is a log, and this screen is explicitly
    not one: it answers "what did Kayra do to my computer", and the answer is "it opened
    Chrome", once, with the outcome.

    Consecutive entries for the same (action, target) collapse into the furthest-progressed
    one. Consecutive is the right scope — the same app opened twice, a minute apart, is two
    things the user did and deserves two rows.
    """
    collapsed = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identity = (entry.get("action"), entry.get("target"))
        if collapsed and collapsed[-1][0] == identity:
            previous = collapsed[-1][1]
            if (_EVENT_RANK.get(str(entry.get("event")), -1)
                    >= _EVENT_RANK.get(str(previous.get("event")), -1)):
                collapsed[-1] = (identity, entry)
            continue
        collapsed.append((identity, entry))
    return [entry for _identity, entry in collapsed]


class _AuditRow(ListRow):
    """One audit entry, rendered in the user's terms rather than the log's."""

    TONE = {
        "denied": ("Blocked", "danger"),
        "failed": ("Failed", "danger"),
        "not_found": ("Not found", "warning"),
        "unavailable": ("Unavailable", "warning"),
        "ambiguous": ("Asked", "warning"),
        "confirm_required": ("Confirm?", "warning"),
        "executed": ("Done", "success"),
        "verified": ("Verified", "success"),
        # A row that collapsed to one of these never reached execution, so it is still in
        # flight rather than finished. "Resolved" as a final state told the user nothing.
        "resolved": ("Working", "neutral"),
        "planned": ("Working", "neutral"),
        "executing": ("Working", "neutral"),
        "normalized": ("Working", "neutral"),
        "policy": ("Working", "neutral"),
    }

    def __init__(self, entry, parent=None):
        super().__init__(parent)

        event = str(entry.get("event", ""))
        label, tone = self.TONE.get(event, (event.replace("_", " ").title() or "Event", "neutral"))

        description = humanise_action(entry.get("action"), entry.get("target")) or "—"

        stamp = entry.get("ts")
        when = time.strftime("%H:%M", time.localtime(stamp)) if stamp else ""

        badge = ActionStatus(label, tone)

        # The badge sizes itself to its own text so nothing is ever clipped ("Requires
        # confirmation" is more than twice the width of "Done"). A fixed-width COLUMN around
        # it keeps every description starting on the same left edge, which is what the fixed
        # pill width used to buy at the cost of truncating the longer words.
        holder = QWidget()
        holder.setFixedWidth(132)
        holder_row = QHBoxLayout(holder)
        holder_row.setContentsMargins(0, 0, 0, 0)
        holder_row.setSpacing(0)
        holder_row.addWidget(badge)
        holder_row.addStretch(1)

        text = _label(description, "ActionName")
        text.setWordWrap(False)

        self.row.addWidget(holder)
        self.row.addWidget(text, 1)
        self.row.addWidget(Caption(when))
