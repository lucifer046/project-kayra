# ┌────────────────────────────────────────────────────────────────────────┐
# │                              confirm.py                                │
# │            An In-Window Confirmation, Sized to the Window              │
# └────────────────────────────────────────────────────────────────────────┘
"""
The one confirmation dialog: shut down Kayra, delete a memory, clear all memories, remove an
API key.

WHY NOT `QMessageBox`
---------------------
A stock message box is a separate top-level window. On Hyprland that meant a floating window
with the compositor's border, sized by its own rules, the icon theme's red X and green tick
on the buttons, and none of Kayra's type or colour — the one moment the application asks for
the user's full attention was the least designed thing in it.

This is a CHILD of the window instead: a scrim dims the whole window, and a themed panel sits
in the middle. The panel is `min(MAX_WIDTH, window width - 2 * margin)` wide and its text
wraps, so it fits a 480px tiled window and a 4K one the same way.

BEHAVIOUR
---------
* Modal by construction: the scrim covers every pixel of the window and takes every click.
  A click on the scrim, Esc, or Cancel answers "no".
* Enter answers Cancel — the SAFE choice — because focus starts there. Confirming a
  destructive action takes a deliberate click or Tab then Enter.
* The confirm button is labelled with what it does ("Shut down", "Delete"), never "Yes".
* A short fade unless the platform asks for reduced motion.

`ask()` runs a local event loop and returns True/False, the same contract `exec()` had, so
every caller stays a single readable `if not ask(...): return`.

TESTS: set `ConfirmDialog.ANSWER` to True/False and `ask()` returns it without showing
anything — the same injection pattern `automation.ai.TEXT_MODEL` uses.
"""

from PySide6.QtCore import Qt, QEventLoop, QEvent, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QPushButton

from kayra.ui.theme import Space, Motion, repolish
from kayra.ui.components.primitives import _label


class ConfirmDialog(QWidget):
    """A scrim over the whole window with a themed question panel in the middle."""

    MAX_WIDTH = 440
    MARGIN = 16
    SCRIM_ALPHA = 165
    ANSWER = None          # tests: True / False short-circuits `ask()`

    def __init__(self, window, title, body, confirm_label="Confirm", danger=True):
        super().__init__(window)
        self.setObjectName("ConfirmScrim")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        self._window = window
        self._answer = False
        self._loop = None
        self._scrim = 0.0

        self.panel = QFrame(self)
        self.panel.setObjectName("ConfirmPanel")
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(Space.lg, Space.lg, Space.lg, Space.base)
        layout.setSpacing(Space.sm)
        self.title_label = _label(title, "ConfirmTitle", wrap=True)
        self.body_label = _label(body, "ConfirmBody", wrap=True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addSpacing(Space.sm)

        buttons = QHBoxLayout()
        buttons.setSpacing(Space.sm)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setProperty("variant", "outline")
        self.confirm_button = QPushButton(confirm_label)
        self.confirm_button.setProperty("variant", "danger" if danger else "accent")
        for button in (self.cancel_button, self.confirm_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(96)
            repolish(button)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.cancel_button.clicked.connect(lambda: self._finish(False))
        self.confirm_button.clicked.connect(lambda: self._finish(True))
        self.setTabOrder(self.cancel_button, self.confirm_button)

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(Motion.drawer // 2)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.valueChanged.connect(self._set_scrim)
        window.installEventFilter(self)

    # ── Public API ────────────────────────────────────────────────────

    @classmethod
    def ask(cls, parent, title, body, confirm_label="Confirm", danger=True):
        """Shows the question over `parent`'s window and returns the answer (True = confirm)."""
        if cls.ANSWER is not None:
            return bool(cls.ANSWER)
        window = parent.window() if parent is not None else None
        if window is None:
            return False
        dialog = cls(window, title, body, confirm_label, danger)
        try:
            return dialog._run()
        finally:
            window.removeEventFilter(dialog)
            dialog.hide()
            dialog.deleteLater()

    # ── Geometry: always the whole window, panel centred and fitted ───

    def _fit(self):
        rect = self._window.rect()
        self.setGeometry(rect)
        width = max(200, min(self.MAX_WIDTH, rect.width() - 2 * self.MARGIN))
        self.panel.setFixedWidth(width)
        height = self.panel.layout().heightForWidth(width) if \
            self.panel.layout().hasHeightForWidth() else self.panel.sizeHint().height()
        height = min(max(height, self.panel.minimumSizeHint().height()),
                     rect.height() - 2 * self.MARGIN)
        self.panel.setFixedHeight(max(1, height))
        self.panel.move((rect.width() - width) // 2, (rect.height() - height) // 2)

    def eventFilter(self, watched, event):
        if watched is self._window and event.type() == QEvent.Resize:
            self._fit()
        return False

    # ── Running ───────────────────────────────────────────────────────

    def _run(self):
        from kayra.ui.components.backdrop import prefers_reduced_motion

        self._fit()
        self.show()
        self.raise_()
        self.cancel_button.setFocus(Qt.OtherFocusReason)
        if prefers_reduced_motion():
            self._set_scrim(1.0)
        else:
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self._loop = QEventLoop(self)
        self._loop.exec()
        return self._answer

    def _finish(self, answer):
        self._answer = bool(answer)
        if self._loop is not None and self._loop.isRunning():
            self._loop.quit()

    def _set_scrim(self, value):
        self._scrim = float(value)
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # Outside the panel: the same as Cancel. The panel itself swallows its own clicks.
        if not self.panel.geometry().contains(event.position().toPoint()):
            self._finish(False)
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, int(self.SCRIM_ALPHA * self._scrim)))
        painter.end()
