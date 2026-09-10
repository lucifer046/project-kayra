# ┌────────────────────────────────────────────────────────────────────────┐
# │                             chrome.py                                  │
# │                   Custom Window Chrome (Windows)                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Kayra's own title bar, and the native-hit-test that keeps Windows in charge of the window.

THE RULE THIS MODULE IS BUILT AROUND
------------------------------------
A frameless window is easy; a frameless window that has not quietly broken half of Windows is
not. The usual implementation moves the window from `mouseMoveEvent`, and everything the
operating system does with a title bar is then silently gone: Aero Snap, Win+Arrow, drag to
the top edge to maximise, double-click to maximise, shake to minimise others, the right-click
system menu, edge resize, and the snap layouts flyout on Windows 11.

So none of that is reimplemented here. The window stays frameless, and `WM_NCHITTEST` is
answered with `HTCAPTION` over the title area and `HTLEFT`/`HTTOPRIGHT`/… over the border
band. Windows then performs every one of those behaviours ITSELF, exactly as it would for a
native frame, because as far as it is concerned this IS a native frame — we have only told it
where the parts are.

Two consequences worth knowing:

  * `mouseMoveEvent` appears nowhere in this file, and must not appear. A manual drag would
    take precedence over the hit test and reintroduce the whole problem.
  * On a platform without `WM_NCHITTEST` — Linux, macOS, and the offscreen platform the test
    suite runs on — `install_native_chrome()` declines and the window keeps its native frame.
    A degraded custom title bar is worse than the real one; the design falls back rather than
    half-working.

MAXIMISE
--------
Measured on this machine: Qt's frameless `showMaximized()` already lands on the work area
(1440x900 of a 1440x900 available rect) rather than covering the taskbar, so there is no
`WM_GETMINMAXINFO` handling here. If a future Windows build regresses that, this is where the
fix belongs — not in the window.
"""

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QCursor
from PySide6.QtWidgets import QWidget, QHBoxLayout

from kayra.ui.theme import Color, Size, Space
from kayra.ui.components.primitives import _icon_glyph, _label
from kayra.ui.components.orb import OrbBadge


# Win32 hit-test results. Named here rather than inline so the mapping below reads as a
# diagram of the window's edges.
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

WM_NCHITTEST = 0x0084


def native_chrome_supported():
    """
    True when this platform can answer `WM_NCHITTEST`.

    Windows only, and not under the offscreen platform plugin — there is no window manager
    there to send the message, so a frameless window would simply lose its title bar.
    """
    if sys.platform != "win32":
        return False
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        return app is not None and app.platformName() not in ("offscreen", "minimal")
    except Exception:
        return False


class CaptionButton(QWidget):
    """
    One of the three window controls.

    SIZED TO THE WINDOWS CONVENTION (46x40, wider than tall), not to Kayra's control grid.
    These are the one part of the interface a user does not read as belonging to the
    application — they reach for them by muscle memory, at the size and in the order the
    platform trained them. Making them square and amber would be the single most obvious way
    to say "this is a web page in a window".
    """

    clicked = Signal()

    def __init__(self, kind, tooltip, danger=False, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._danger = danger
        self._hover = False
        self._pressed = False
        self.setFixedSize(Size.chrome_button, Size.chrome_height)
        self.setAttribute(Qt.WA_Hover, True)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.NoFocus)

    def set_kind(self, kind):
        if kind != self._kind:
            self._kind = kind
            self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hover = self._pressed = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        was = self._pressed
        self._pressed = False
        self.update()
        if was and event.button() == Qt.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        if self._hover or self._pressed:
            # Close goes RED on hover, like every Windows close button. The other two take a
            # neutral wash. Consistency with the platform beats consistency with the palette
            # here, and this is the only place in the application where that is true.
            if self._danger:
                fill = QColor(Color.danger)
                fill.setAlphaF(0.90 if self._pressed else 0.75)
            else:
                fill = QColor(255, 255, 255, 0)
                fill.setAlphaF(0.14 if self._pressed else 0.09)
            painter.fillRect(self.rect(), fill)

        color = Color.text if (self._hover and self._danger) else (
            Color.text if self._hover else Color.text_secondary)
        icon = _icon_glyph(self._kind, color, 14)
        icon.paint(painter, int((self.width() - 14) / 2), int((self.height() - 14) / 2),
                   14, 14)
        painter.end()


class AppWindowChrome(QWidget):
    """
    The title bar: brand, live state dot, window title, and the three caption buttons.

    It is NOT a drag handle in the Qt sense — see the module docstring. The empty region
    between the brand and the buttons is reported to Windows as `HTCAPTION`, and Windows does
    the dragging. This widget only has to know WHERE that region is, which it answers through
    `is_caption_at()`.
    """

    minimizeRequested = Signal()
    maximizeRequested = Signal()
    closeRequested = Signal()

    def __init__(self, title="Kayra", parent=None):
        super().__init__(parent)
        self.setObjectName("WindowChrome")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(Size.chrome_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(Space.md, 0, 0, 0)
        layout.setSpacing(Space.sm)

        self.badge = OrbBadge(8)
        self.brand = _label("KAYRA", "ChromeBrand")
        self.title = _label(title, "ChromeTitle")

        layout.addWidget(self.badge)
        layout.addWidget(self.brand)
        layout.addSpacing(Space.xs)
        layout.addWidget(self.title)
        layout.addStretch(1)

        self.minimize_button = CaptionButton("minimize", "Minimize")
        self.maximize_button = CaptionButton("maximize", "Maximize")
        self.close_button = CaptionButton("close", "Close", danger=True)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            layout.addWidget(button)

        self.minimize_button.clicked.connect(self.minimizeRequested.emit)
        self.maximize_button.clicked.connect(self.maximizeRequested.emit)
        self.close_button.clicked.connect(self.closeRequested.emit)

    def set_maximized(self, maximized):
        """The middle button's glyph follows the window, so it never offers what it just did."""
        self.maximize_button.set_kind("restore" if maximized else "maximize")
        self.maximize_button.setToolTip("Restore down" if maximized else "Maximize")

    def set_state(self, state):
        self.badge.set_state(state)

    def set_subtitle(self, text):
        """The screen's name, beside the brand. Empty on Home, where the orb is the heading."""
        self.title.setText(text or "")

    def is_caption_at(self, local_point):
        """
        Is this point part of the draggable caption?

        Everything in the bar EXCEPT the three buttons. The brand and the title are included
        deliberately: a title bar whose text cannot be grabbed is a title bar that feels
        broken, and there is nothing to click on them for.
        """
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            if button.geometry().contains(local_point):
                return False
        return self.rect().contains(local_point)


def install_native_chrome(window, chrome):
    """
    Makes `window` frameless and hands hit-testing to Windows.

    Returns True when it took effect. On any other platform it returns False and CHANGES
    NOTHING — the caller keeps the native title bar, which is the correct fallback.

    The hit test is answered from the CURSOR position rather than from the message's own
    coordinates: `WM_NCHITTEST` packs them as screen coordinates in the low and high words of
    lParam, and unpacking those correctly across multi-monitor setups with negative
    coordinates is a well-known source of off-by-a-monitor bugs. `QCursor.pos()` is the same
    point, already in Qt's coordinate space, and Qt has done the monitor arithmetic.
    """
    if not native_chrome_supported():
        return False

    window.setWindowFlag(Qt.FramelessWindowHint, True)

    margin = Size.resize_margin

    def native_event(event_type, message):
        if event_type != "windows_generic_MSG":
            return False, 0
        try:
            import ctypes
            import ctypes.wintypes          # not pulled in by `import ctypes` alone

            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message != WM_NCHITTEST:
            return False, 0

        # A maximised window has no outside edge to resize from, so only the caption applies.
        position = window.mapFromGlobal(QCursor.pos())
        x, y = position.x(), position.y()
        width, height = window.width(), window.height()

        if not window.isMaximized():
            left = x < margin
            right = x > width - margin
            top = y < margin
            bottom = y > height - margin
            corner = {
                (True, False, True, False): HTTOPLEFT,
                (False, True, True, False): HTTOPRIGHT,
                (True, False, False, True): HTBOTTOMLEFT,
                (False, True, False, True): HTBOTTOMRIGHT,
            }.get((left, right, top, bottom))
            if corner is not None:
                return True, corner
            if left:
                return True, HTLEFT
            if right:
                return True, HTRIGHT
            if top:
                return True, HTTOP
            if bottom:
                return True, HTBOTTOM

        # The caption. Reported in the CHROME's own coordinates so the buttons punch holes in
        # it — a caption region covering the close button would make the button undraggable
        # and unclickable at the same time.
        chrome_point = chrome.mapFrom(window, position)
        if chrome.rect().contains(chrome_point) and chrome.is_caption_at(chrome_point):
            return True, HTCAPTION

        return False, 0

    window.nativeEvent = native_event
    return True


def show_system_menu(window, global_point):
    """
    Opens Windows' own window menu (Move / Size / Minimize / Maximize / Close).

    Right-clicking the title bar is expected to produce it, and a frameless window does not
    get it for free. This asks the OS for the real menu rather than building a replica, so
    every item does exactly what it does everywhere else — including the ones a replica
    always gets wrong, like Size and Move putting the window into keyboard-drag mode.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        handle = int(window.winId())
        menu = ctypes.windll.user32.GetSystemMenu(handle, False)
        if not menu:
            return False
        TPM_RETURNCMD = 0x0100
        TPM_LEFTBUTTON = 0x0000
        command = ctypes.windll.user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_LEFTBUTTON,
            int(global_point.x()), int(global_point.y()), 0, handle, None)
        if command:
            WM_SYSCOMMAND = 0x0112
            ctypes.windll.user32.PostMessageW(handle, WM_SYSCOMMAND, command, 0)
        return True
    except Exception:
        return False
