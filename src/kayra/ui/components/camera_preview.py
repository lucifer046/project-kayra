# ┌────────────────────────────────────────────────────────────────────────┐
# │                  components/camera_preview.py                          │
# │            The Home Camera Panel — pull, never push                    │
# └────────────────────────────────────────────────────────────────────────┘
"""
A small live camera rectangle, and the offline state that replaces it.

IT PULLS. NOTHING PUSHES FRAMES AT IT.
--------------------------------------
The widget owns a QTimer at the preview rate and asks the bridge for the newest frame. The
alternative — a `frameReady` signal carrying an image from the gesture thread — is a queued Qt
signal per frame, and a queued signal is a QUEUE: if the GUI thread is busy laying out a chat
message when three frames arrive, all three are delivered afterwards and every one of them is
painted. That is a backlog by construction, and the requirement is explicitly that the UI
queue must never grow.

A pull model cannot have a backlog. The gesture runtime keeps exactly one preview buffer and
overwrites it; a frame this widget does not ask for is simply never seen. Stale frames are
dropped by not existing.

IT NEVER TOUCHES THE CAMERA
---------------------------
No `VideoCapture`, no OpenCV, no colour conversion — the buffer arrives as ready-to-paint
RGB888 because the gesture thread converted it there, off the GUI thread. `tests/test_ui.py`
asserts by AST that no UI module imports `cv2`.

ASPECT IS PRESERVED, THE PANEL LETTERBOXES
------------------------------------------
`Qt.KeepAspectRatio` inside the rectangle it is given. A stretched face is worse than a smaller
one, and a 16:9 camera in a 4:3 panel is the common case rather than the exotic one.

THE HEIGHT IS FIXED; THE WIDTH IS NOT, AND THAT IS LOAD-BEARING
----------------------------------------------------------------
`setFixedSize` here clipped the Hand gesture card off the right edge of Home's bottom strip,
which is the SAME defect the System card's footprint caption caused once already: a widget
that declares a hard minimum width forces its card to that width, the row's minimum then
exceeds the window, and the last card is pushed off the screen. Caught by rendering the page
and looking at it — the layout test passed throughout, exactly as CLAUDE.md warns.

So the height is fixed (the strip is a fixed-height glance and a growing preview would resize
it) and the width is `Ignored`, so the panel takes whatever the card can spare and letterboxes
the image inside it.
"""

from PySide6.QtCore import Qt, QTimer, QSize, QRect
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget, QSizePolicy

from kayra.ui.theme import Color, Font, Radius


class CameraPreview(QWidget):
    """
    A fixed-size live camera rectangle. Shows an offline state when there is no frame.

    The timer runs ONLY while the widget is visible (`showEvent` / `hideEvent`), which is the
    same rule every other polling surface in this application follows. A preview timer ticking
    behind the Settings screen would be pure cost.
    """

    DEFAULT_SIZE = QSize(196, 110)
    MIN_WIDTH = 72

    def __init__(self, bridge, size=None, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setObjectName("CameraPreview")
        # A bare QWidget ignores stylesheet background and border unless this is set. That trap
        # has already rendered the System score tiles as bare text once in this codebase.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._size = size or self.DEFAULT_SIZE
        self.setFixedHeight(self._size.height())
        self.setMaximumWidth(self._size.width())
        self.setMinimumWidth(self.MIN_WIDTH)
        # EXPANDING between a small minimum and the intended size. `Ignored` was tried first
        # and is wrong here for a reason worth recording: an Ignored widget has no size
        # PREFERENCE at all, so a centring layout hands it its minimum and the preview
        # rendered as a 72px sliver on a card with room for 164. Expanding keeps the panel
        # from forcing a card wider than it should be (the minimum is what a row's minimum
        # adds up from) while still taking the space that is there.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._pixmap = None
        self._message = "Camera off"
        self._live = False

        self._timer = QTimer(self)
        self._timer.setInterval(66)          # ~15 FPS, independent of the processing rate
        self._timer.timeout.connect(self._pull)

    def sizeHint(self):
        return QSize(self._size.width(), self._size.height())

    def minimumSizeHint(self):
        # Explicit, and small. This number is what a row of cards adds up to decide whether it
        # fits in the window; see the module docstring for the card it pushed off the screen.
        return QSize(self.MIN_WIDTH, self._size.height())

    # ──────────────────────────────────────────────────────────────────

    def set_message(self, text):
        """The offline caption: 'Camera off', 'Starting…', or an error."""
        if text != self._message:
            self._message = text or ""
            if not self._live:
                self.update()

    def set_live(self, live):
        """
        Starts or stops asking for frames.

        Clearing the pixmap on the way down is not cosmetic. A preview that keeps showing the
        last frame after the camera is released tells the user the camera is still on, which is
        the one thing a camera indicator must never get wrong.
        """
        live = bool(live)
        if live == self._live:
            return
        self._live = live
        if not live:
            self._pixmap = None
        self._sync_timer()
        self.update()

    # ──────────────────────────────────────────────────────────────────

    def _sync_timer(self):
        if self._live and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _pull(self):
        frame = None
        try:
            frame = self.bridge.camera_frame()
        except Exception:
            frame = None
        if not frame:
            # Not an error: the camera may be between frames, or starting. The last painted
            # image simply stays until a new one arrives or `set_live(False)` clears it.
            return
        buffer, width, height = frame
        # `QImage` does not copy, so the pixmap conversion must happen before `buffer` can be
        # collected — `fromImage` does copy, which is why the QImage is a local.
        image = QImage(buffer, width, height, width * 3, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    # ──────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(Color.inset))
        painter.drawRoundedRect(rect, Radius.sm, Radius.sm)

        if self._live and self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(rect.size(), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            x = rect.x() + (rect.width() - scaled.width()) // 2
            y = rect.y() + (rect.height() - scaled.height()) // 2
            painter.save()
            path_rect = QRect(x, y, scaled.width(), scaled.height())
            painter.setClipRect(path_rect)
            painter.drawPixmap(x, y, scaled)
            painter.restore()
        else:
            painter.setPen(QPen(QColor(Color.text_tertiary)))
            font = painter.font()
            font.setPointSize(Font.caption)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, self._message)

        painter.setPen(QPen(QColor(Color.border), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, Radius.sm, Radius.sm)
        painter.end()
