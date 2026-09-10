# ┌────────────────────────────────────────────────────────────────────────┐
# │                              dock.py                                   │
# │                  The Floating Kayra Control Dock                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
One pill, floating near the bottom of Home and Chat, carrying every control a person reaches
for while talking to Kayra.

WHY A DOCK AND NOT A TOOLBAR
----------------------------
Home and Chat are the two screens where Kayra is the subject rather than a settings surface,
and both want the full width of the window. A permanent left rail on those pages spends 224
pixels on navigation that is not being used, on the two screens where content matters most.
The dock puts the controls where the hand already is — bottom centre, over the content —
and gives navigation back through the Menu button when it is actually wanted.

ONE STATE, NOT TWO
------------------
"Microphone on/off" and "start/stop listening" are the SAME fact, so this dock has ONE
control for them — the PRIMARY button — and there is no second microphone anywhere on it.
Two controls would be two places to read a single state and inevitably two places for it to
be read differently, which is exactly the class of bug `core.voice_state` exists to end.

The primary control carries the state three ways at once, because whether Kayra can hear you
is the single most important thing this bar says: the plate is FILLED when listening and
outlined when paused, the glyph is a struck-through microphone when paused, and the label
reads "Listening" or "Paused". Shape and text both, so it never depends on noticing a shade.

NO TOOLTIPS. NONE.
------------------
A dock button never shows a floating tooltip card. Qt draws those as their own top-level
window, so on a dock sitting 28px off the bottom edge the card is placed against the window
boundary and its rounded border is clipped — a large grey rectangle hanging off the corner of
a control bar whose whole point is to look compact. They are also redundant: every control
here is either labelled or a shape people already know.

What replaces them is `setAccessibleName()` / `setAccessibleDescription()`, which is what a
screen reader reads anyway — a tooltip was never the accessible name, it was a sighted
convenience sitting on top of one. Hover is expressed in COLOUR and a soft glow instead, at
constant dimensions.

WHAT THE DOCK DOES NOT DO
-------------------------
It holds no state. Every button is painted from what the bridge reports and every press is
handed straight back to the bridge; `sync()` re-reads the whole picture. A dock button that
remembered what it was last clicked would be a UI-only fake state, and the failure it
produces — a control showing what the user asked for rather than what happened — is the one
this codebase already fixed for the camera and the speech backend.

SHUTDOWN IS DIFFERENT AND LOOKS DIFFERENT
-----------------------------------------
It ends the process, so it is the only button in the danger tone, it sits alone past a
divider at the far end, and it never sits adjacent to a routine control. "Pause listening"
and "Shut down Kayra" are precisely the pair that must never be hit by mistake. It also does
not act: it emits, and the window runs the existing confirmation flow.

COST
----
Nothing here polls. The buttons are repainted on hover and press through a short
QVariantAnimation that runs only while the pointer is over them, and `sync()` is called on
the events the rest of the UI already receives.
"""

from PySide6.QtCore import Qt, Signal, QRectF, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QFrame, QSizePolicy,
                               QGraphicsDropShadowEffect)

from kayra.ui.theme import Color, Font, Space, Size, Radius, Motion, Elevation
from kayra.ui.components.primitives import _icon_glyph


class DockButton(QWidget):
    """
    One control in the dock: a drawn glyph on a rounded plate that lifts under the pointer.

    SELF-PAINTED, AND THAT IS DELIBERATE. A stylesheet can give a QPushButton a hover
    colour, but it cannot animate one, and Qt's generic `QPushButton` geometry rules beat
    `setFixedSize` (the `min-height` trap the Toggle control already had to work around).
    Painting the plate here means the hover lift, the press compression and the focus ring
    are all one cheap paint pass with no stylesheet fight.

    KEYBOARD REACHABLE. `StrongFocus` plus Space/Return activation, and a visible focus ring
    that is drawn rather than borrowed from the platform — a focus ring the same colour as
    the plate is not a focus ring.

    NO TOOLTIP, EVER. `describe()` sets the ACCESSIBLE name and description, which is what
    assistive technology actually reads; `setToolTip` appears nowhere in this class and the
    suite asserts that. See the module docstring for why the popup card had to go.
    """

    clicked = Signal()

    # Every label this button may ever show. The width is measured from the WIDEST of them
    # once, at construction, and then fixed — so a state change swaps the text without
    # changing the button's size, the dock's size, or the dock's centred position. A dock
    # that re-centres itself when the microphone is paused is the jitter this pass removes.
    def __init__(self, kind, tone="normal", checkable=False, label="",
                 label_alternatives=(), parent=None):
        super().__init__(parent)
        self._kind = kind
        self._tone = tone                 # "normal" | "primary" | "danger"
        self._checkable = bool(checkable)
        self._checked = False
        self._label = label
        self._hover = 0.0                 # 0..1, animated
        self._pressed = False
        self._enabled_look = True

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)

        width = Size.dock_button
        if label:
            from PySide6.QtGui import QFontMetrics
            font = self.font()
            font.setPixelSize(Font.small)
            metrics = QFontMetrics(font)
            widest = max(metrics.horizontalAdvance(text)
                         for text in (label,) + tuple(label_alternatives))
            width = Size.dock_button + widest + Space.md
        self.setFixedSize(width, Size.dock_button)

        # Runs ONLY while the pointer is arriving or leaving. An idle dock has no timers.
        self._lift = QVariantAnimation(self)
        self._lift.setDuration(Motion.dock_hover)
        self._lift.setEasingCurve(QEasingCurve.OutCubic)
        self._lift.valueChanged.connect(self._on_lift)

    # ── State, all of it set from outside ──

    def describe(self, name, detail=""):
        """
        Sets the ACCESSIBLE name and description. The replacement for a tooltip, not a
        companion to one.

        A screen reader reads the accessible name; it never read the tooltip unless nothing
        else was set. So this is not a downgrade of the popup card — it is the thing the card
        was standing in front of, now set directly.
        """
        self.setAccessibleName(name)
        self.setAccessibleDescription(detail or name)

    def set_tone(self, tone):
        """
        Switches the visual register. Used by the primary control, which is FILLED while
        listening and OUTLINED while paused — the same button, two unmistakable states.
        """
        if tone != self._tone:
            self._tone = tone
            self.update()

    def tone(self):
        return self._tone

    def set_checked(self, checked):
        checked = bool(checked)
        if checked != self._checked:
            self._checked = checked
            self.update()

    def is_checked(self):
        return self._checked

    def set_kind(self, kind):
        """Swaps the glyph. Meaning that changes with state is carried by SHAPE, not tint."""
        if kind != self._kind:
            self._kind = kind
            self.update()

    def set_enabled_look(self, enabled):
        self._enabled_look = bool(enabled)
        self.setEnabled(bool(enabled))
        self.update()

    def set_label(self, text):
        if text != self._label:
            self._label = text
            self.update()

    # ── Interaction ──

    def _on_lift(self, value):
        self._hover = float(value)
        self.update()

    def _animate_to(self, target):
        self._lift.stop()
        self._lift.setStartValue(self._hover)
        self._lift.setEndValue(float(target))
        self._lift.start()

    def enterEvent(self, event):
        super().enterEvent(event)
        if self.isEnabled():
            self._animate_to(1.0)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._animate_to(0.0)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        was = self._pressed
        self._pressed = False
        self.update()
        if was and event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._pressed = True
            self.update()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter) and self._pressed:
            self._pressed = False
            self.update()
            self.clicked.emit()
            return
        super().keyReleaseEvent(event)

    # ── Painting ──

    def _plate_color(self):
        """
        The plate under the glyph. Four states, in the order they take precedence.

        Checked outranks hover: a microphone that is ON must look on whether or not the
        pointer happens to be over it.
        """
        if not self._enabled_look:
            return QColor(0, 0, 0, 0)
        if self._tone == "primary":
            # FILLED WHEN ON, OUTLINED WHEN OFF. The two states of the one control that says
            # whether Kayra can hear you, and they are told apart by fill before colour.
            if not self._checked:
                neutral = QColor(255, 255, 255, 0)
                neutral.setAlphaF(0.05 * self._hover + (0.09 if self._pressed else 0.0))
                return neutral
            base = QColor(Color.accent_press if self._pressed else Color.accent)
            if self._hover > 0 and not self._pressed:
                base = _mix(base, QColor(Color.accent_hover), self._hover)
            return base
        if self._checked:
            tint = QColor(Color.danger if self._tone == "danger" else Color.accent)
            tint.setAlphaF(0.20 + 0.10 * self._hover + (0.06 if self._pressed else 0.0))
            return tint
        neutral = QColor(255, 255, 255, 0)
        neutral.setAlphaF(0.05 * self._hover + (0.09 if self._pressed else 0.0))
        return neutral

    def _glyph_color(self):
        if not self._enabled_look:
            return Color.text_disabled
        if self._tone == "primary":
            # Dark ink on the filled plate; ordinary text on the outlined one. White on amber
            # fails contrast, which is why `text_on_accent` exists.
            return Color.text_on_accent if self._checked else (
                Color.text if self._hover > 0.5 else Color.text_secondary)
        if self._tone == "danger":
            return Color.danger
        if self._checked:
            return Color.accent
        # A hovered control brightens toward primary text; an idle one stays secondary, so
        # the dock is quiet until it is being used.
        return Color.text if self._hover > 0.5 else Color.text_secondary

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # CONSTANT GEOMETRY. The plate no longer rises on hover: the brief asks for a colour
        # change and at most a soft glow, and a control that translates under the pointer is
        # a control whose icon appears to jump. Press insets the plate by half a pixel, which
        # reads as depression without moving anything the eye can track.
        inset = 1.0 + (0.5 if self._pressed else 0.0)
        radius = Radius.lg + 2
        plate = QRectF(inset, inset, self.width() - 2 * inset, self.height() - 2 * inset)

        # THE GLOW. One extra rounded rect at very low alpha, drawn OUTSIDE the plate, fading
        # in with the hover. It is what gives the hover some presence on a neutral control
        # whose plate is nearly transparent — without it, hovering an unchecked icon changed
        # only the glyph and read as nothing happening.
        if self._hover > 0.01 and self._enabled_look:
            glow = QColor(Color.danger if self._tone == "danger" else Color.accent)
            glow.setAlphaF(0.13 * self._hover)
            pen = QPen(glow)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(plate.adjusted(-1.0, -1.0, 1.0, 1.0),
                                    radius + 1, radius + 1)

        color = self._plate_color()
        if color.alpha() > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(plate, radius, radius)

        # A hairline edge, for the two tones that need an outline when they are NOT filled:
        # the danger control, and the primary control while it is paused. On a neutral plate
        # it would draw a box around every idle icon and turn the dock into a row of buttons.
        edge = None
        if self._tone == "danger" and (self._hover > 0 or self._checked):
            edge = QColor(Color.danger_edge)
        elif self._tone == "primary" and not self._checked and self._enabled_look:
            # OUTLINED IS THE OFF STATE, and it has to be visible on its own — this is the
            # button that says whether the microphone is open.
            edge = QColor(Color.border_strong)
        if edge is not None:
            pen = QPen(edge)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(plate, radius, radius)

        # The focus ring: drawn, and in the accent, so keyboard users can see where they are.
        if self.hasFocus():
            pen = QPen(QColor(Color.accent))
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(plate.adjusted(-0.5, -0.5, 0.5, 0.5), radius, radius)

        glyph_color = self._glyph_color()
        icon = _icon_glyph(self._kind, glyph_color, 18)
        glyph_x = (Size.dock_button - 18) / 2 if self._label else (self.width() - 18) / 2
        icon.paint(painter, int(glyph_x), int((self.height() - 18) / 2), 18, 18)

        if self._label:
            painter.setPen(QColor(glyph_color))
            font = painter.font()
            font.setPixelSize(Font.small)
            # `QFont.Weight.Medium`, NOT the token's raw 500. In Qt 6 `setWeight` takes a
            # scoped enum and a bare int reaches it as an unchecked value — which crashed the
            # renderer outright rather than falling back to a nearby weight. The tokens carry
            # CSS weights because the stylesheet is where they are normally used; painting
            # code has to translate.
            font.setWeight(QFont.Weight.Medium)
            painter.setFont(font)
            text_rect = QRectF(Size.dock_button - Space.xs, 0.0,
                               self.width() - Size.dock_button, float(self.height()))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._label)
        painter.end()


def _mix(a, b, t):
    """Linear blend between two QColors. Used for the accent button's hover."""
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t),
                  int(a.alpha() + (b.alpha() - a.alpha()) * t))


class DockDivider(QWidget):
    """A hairline between groups of dock controls. Two primitives, no layout weight."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(1)
        self.setFixedHeight(Size.dock_button - Space.md)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Color.border))
        painter.end()


class FloatingDock(QFrame):
    """
    The pill. Emits intent; it never drives the backend and never decides anything.

    THE ORDER OF THE CONTROLS IS AN ARGUMENT, not an arrangement:

        [ Menu ] [ Home ] [ Chat ] | [ 🎙 Listening ] [ Camera ] [ Gesture ] | [ Power ]
        └──── WHERE YOU ARE ─────┘   └──── WHAT IS SWITCHED ON ─────────────┘   └ ends it ┘

    Two categories, and the divider between them is the argument. Everything left of it
    CHANGES WHICH SCREEN YOU ARE LOOKING AT and touches no device. Everything right of it
    SWITCHES A DEVICE and changes no screen. Mixing the two — which the first version did, by
    putting a big primary button that opened Chat next to the microphone — is what made the
    bar hard to read at a glance: the loudest control on it did the least consequential thing.

    The microphone is the prominent one because it is the control a voice assistant's user
    reaches for most, and it is deliberately shaped UNLIKE the navigation buttons: labelled,
    filled, and the only thing on the bar carrying text. The destructive control is rightmost
    and alone, as far from the routine controls as the pill allows, and it is distinct by
    TONE rather than by size — a dangerous action should be unmistakable when looked for and
    unremarkable when not.
    """

    menuToggled = Signal()
    homeRequested = Signal()
    chatRequested = Signal()
    listeningToggled = Signal()
    cameraToggled = Signal()
    gestureToggled = Signal()
    shutdownRequested = Signal()

    # The primary control's two labels. Declared here so the button can measure BOTH at
    # construction and fix its width to the wider — see `DockButton.__init__`.
    LISTENING_LABEL = "Listening"
    PAUSED_LABEL = "Paused"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FloatingDock")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(Size.dock_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(Space.sm, Space.sm, Space.sm, Space.sm)
        layout.setSpacing(Space.xxs)

        # ── NAVIGATION: where you are ──
        self.menu_button = DockButton("menu", checkable=True)
        self.home_button = DockButton("home", checkable=True)
        self.chat_button = DockButton("chat", checkable=True)

        # ── ACTIONS: what is switched on ──
        # THE PRIMARY CONTROL IS THE MICROPHONE, and it used to be a button that opened Chat.
        # That was the wrong thing to make loudest: a voice assistant's most-reached control
        # is the one that decides whether it can hear you, and "go to the transcript" is a
        # navigation step already served by the icon two places to its left.
        self.mic_button = DockButton("mic", tone="primary", checkable=True,
                                     label=self.LISTENING_LABEL,
                                     label_alternatives=(self.PAUSED_LABEL,))
        self.camera_button = DockButton("camera_off", checkable=True)
        self.gesture_button = DockButton("hand", checkable=True)
        self.power_button = DockButton("power", tone="danger")

        layout.addWidget(self.menu_button)
        layout.addWidget(self.home_button)
        layout.addWidget(self.chat_button)
        layout.addWidget(DockDivider())
        layout.addWidget(self.mic_button)
        layout.addWidget(self.camera_button)
        layout.addWidget(self.gesture_button)
        layout.addWidget(DockDivider())
        layout.addWidget(self.power_button)

        self.menu_button.clicked.connect(self.menuToggled.emit)
        self.home_button.clicked.connect(self.homeRequested.emit)
        self.chat_button.clicked.connect(self.chatRequested.emit)
        self.mic_button.clicked.connect(self.listeningToggled.emit)
        self.camera_button.clicked.connect(self.cameraToggled.emit)
        self.gesture_button.clicked.connect(self.gestureToggled.emit)
        self.power_button.clicked.connect(self.shutdownRequested.emit)

        # Accessible names, set ONCE for the controls whose meaning does not change. The
        # state-dependent ones are described in the setters below, beside the state.
        self.home_button.describe("Home", "Go to the Home screen")
        self.chat_button.describe("Chat", "Go to the conversation")
        self.power_button.describe("Shut down Kayra",
                                   "Stop every service and end Kayra. This does not shut "
                                   "down your computer.")
        self.set_current_screen("home")

        # Qt does not honour `box-shadow` in QSS, so the float is a real effect. One effect on
        # the pill, not one per button — a drop shadow per control would be nine composited
        # layers for a decoration nobody would notice.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(Elevation.dock_shadow_blur)
        shadow.setOffset(0, Elevation.dock_shadow_y)
        shadow.setColor(QColor(0, 0, 0, Elevation.dock_shadow_alpha))
        self.setGraphicsEffect(shadow)

    # ──────────────────────────────────────────────────────────────────
    #                       STATE, READ NOT REMEMBERED
    # ──────────────────────────────────────────────────────────────────

    def set_listening(self, listening, known=True):
        """
        Paints the ONE microphone/listening control.

        `known=False` means the backend has not booted, so there is no answer yet and the
        control is DISABLED rather than guessed at — which is also the truth about what it
        can do, since `set_listening` returns False before the session exists. This is the
        same rule Home's button learned after it spent a whole boot window claiming the
        microphone was closed.
        """
        listening = bool(listening)
        # THREE SIGNALS FOR ONE FACT, and that is deliberate for this control alone: the
        # plate fills, the glyph gains a slash, and the label changes. Whether the microphone
        # is open must not depend on noticing any single one of them.
        self.mic_button.set_checked(listening)
        self.mic_button.set_kind("mic" if listening else "mic_off")
        self.mic_button.set_label(self.LISTENING_LABEL if listening else self.PAUSED_LABEL)
        self.mic_button.describe(
            "Listening" if listening else "Listening paused",
            "Kayra is still starting" if not known
            else "Pause listening" if listening else "Start listening")
        self.mic_button.set_enabled_look(bool(known) and not self._shutting_down())

    def set_gesture_status(self, status):
        """Camera and gesture, from the controller's own report. Infers neither from the other."""
        status = dict(status or {})
        camera_state = str(status.get("camera", "OFF"))
        camera_on = camera_state not in ("OFF", "ERROR")
        gesture_on = bool(status.get("gesture_enabled"))

        self.camera_button.set_checked(camera_on)
        self.camera_button.set_kind("camera" if camera_on else "camera_off")
        self.camera_button.describe(
            "Camera on" if camera_on else "Camera off",
            "Turn the camera off" if camera_on else "Turn the camera on")

        self.gesture_button.set_checked(gesture_on)
        self.gesture_button.describe(
            "Hand gestures on" if gesture_on else "Hand gestures off",
            "Turn hand gesture control off" if gesture_on
            else "Turn hand gesture control on")

    def set_menu_open(self, is_open):
        self.menu_button.set_checked(bool(is_open))
        self.menu_button.describe(
            "Navigation open" if is_open else "Navigation",
            "Hide navigation" if is_open else "Show navigation")

    def set_current_screen(self, key):
        """
        Marks which navigation destination is the current one.

        THE CURRENT SCREEN STAYS SUBTLY LIT rather than being switched off. A Home button
        that looks inert while you are on Home tells you nothing; one that stays highlighted
        answers "where am I" without a second glance, and the other destination is then
        visibly the thing you can go to.
        """
        self.home_button.set_checked(key == "home")
        self.chat_button.set_checked(key == "chat")

    def set_shutting_down(self):
        """
        Latches every control off once teardown starts.

        Teardown takes a couple of seconds — the browser session has nine processes to reap —
        and a second press during that window is the easiest way to re-enter a shutdown that
        is already half done. Never re-enabled: there is nothing after this.
        """
        self._is_shutting_down = True
        for button in (self.home_button, self.chat_button, self.mic_button,
                       self.camera_button, self.gesture_button, self.power_button):
            button.set_enabled_look(False)
        self.power_button.describe("Shutting down", "Kayra is stopping.")

    def _shutting_down(self):
        return getattr(self, "_is_shutting_down", False)
