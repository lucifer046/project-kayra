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

from PySide6.QtCore import Qt, Signal, QRectF, QVariantAnimation, QEasingCurve, QEvent
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
            font.setFamily("Segoe UI")
            font.setPixelSize(Font.small)
            font.setWeight(QFont.Weight.Medium)
            # MUST match the letter-spacing used in paintEvent: +0.3px absolute.
            # "Listening" is 9 chars × 0.3px = 2.7px extra; without this the
            # rendered text overflows the allocated width and the last glyph clips.
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
            metrics = QFontMetrics(font)
            widest = max(metrics.horizontalAdvance(text)
                         for text in (label,) + tuple(label_alternatives))
            # Symmetrical capsule: 14px left pad + 18px glyph + 8px gap + widest + 14px right pad
            self._pad_h = 14
            self._gap = 8
            self._glyph_size = 18
            width = self._pad_h + self._glyph_size + self._gap + widest + self._pad_h
        else:
            self._pad_h = 0
            self._gap = 0
            self._glyph_size = 18
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

    def event(self, event):
        # NO TOOLTIPS. Completely swallow and ignore any tooltip event so Qt never creates
        # a floating popup card window. Accessible name and description handle accessibility.
        # QEvent.Type.ToolTip (110) is used, NOT Qt.ToolTip (13, a mouse-button alias) —
        # comparing against the wrong namespace triggers a PySide6 enum-mismatch SystemError.
        if event.type() == QEvent.Type.ToolTip:
            event.ignore()
            return True
        return super().event(event)

    # ── Painting ──

    def _plate_color(self):
        """
        The plate under the glyph. Every plate uses clean translucent glass or rich filled accent.
        Checked outranks hover: an active destination or on-air mic stays visible regardless of pointer.
        """
        if not self._enabled_look:
            return QColor(0, 0, 0, 0)

        # PRIMARY CONTROL (Mic / Listening)
        if self._tone == "primary":
            if not self._checked:
                # Paused: sleek dark glass plate. The plate does NOT respond to hover —
                # only the glyph and the rim do.
                return QColor(255, 255, 255, 10 + (24 if self._pressed else 0))
            # Listening: warm amber filled capsule. Unchanged by hover; a filled state that
            # also brightens under the pointer is two signals for one fact.
            return QColor(Color.accent_press if self._pressed else Color.accent)

        # DANGER CONTROL (Shutdown)
        if self._tone == "danger":
            tint = QColor(Color.danger)
            tint.setAlphaF(0.12 if self._pressed else 0.04)
            return tint

        # ACTIVE NAVIGATION / DEVICES (Home, Chat, Camera ON, Gesture ON)
        if self._checked:
            # Luminous warm amber glass plate — sleek and clear, not muddy
            tint = QColor(Color.accent)
            tint.setAlphaF(0.22 if self._pressed else 0.14)
            return tint

        # IDLE NEUTRAL CONTROLS. Hover contributes NOTHING here: a hovered control is lit,
        # not filled. Press still paints, because a press is a different thing from a hover.
        if self._pressed:
            return QColor(255, 255, 255, 16)
        return QColor(0, 0, 0, 0)

    def _glyph_color(self):
        if not self._enabled_look:
            return Color.text_disabled
        if self._tone == "primary":
            # Dark ink on the filled amber plate; clear warm text on the paused glass plate.
            # Paused uses full primary text (not secondary) — it is a status label, not decoration.
            if self._checked:
                return Color.text_on_accent
            return _lit(QColor(Color.text), self._hover, 0.40)
        if self._tone == "danger":
            # Gently, and no further: the shutdown control is identified by being RED, so a
            # hover that washes it toward white takes the meaning out of the mark.
            return _lit(QColor(Color.danger), self._hover, 0.30)
        if self._checked:
            return _lit(QColor(Color.accent), self._hover, 0.40)
        # THE HOVER IS THE GLYPH. Idle sits at secondary text and rises CONTINUOUSLY to
        # primary text and a little beyond, so the control reads as illuminated from within
        # rather than as a highlighted rectangle. No threshold: a step in brightness at 0.3
        # of the animation is a flicker, not a transition.
        return _lit(_mix(QColor(Color.text_secondary), QColor(Color.text), self._hover),
                    self._hover, 0.30)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # TRUE CONCENTRIC CAPSULE / CIRCLE GEOMETRY.
        # Radius is exactly half the button height (e.g. 20px for a 40px button).
        # A 40x40 button becomes a perfect circle; a labeled button becomes a perfect capsule.
        # This aligns concentric with the outer dock pill's 28px circular end-caps,
        # eliminating corner pinching and awkward crescent gaps.
        #
        # PRESS DEPTH: the plate compresses inward by 0.5px on each axis when pressed.
        # The glyph shifts to follow it — without this, the icon appears to float forward
        # while the plate sinks, which reads as a visual disconnect on a retina display.
        press_offset = 0.5 if self._pressed else 0.0
        inset = 1.0 + press_offset
        plate = QRectF(inset, inset, self.width() - 2 * inset, self.height() - 2 * inset)
        radius = plate.height() / 2.0

        # PLATE BACKGROUND (painted before glow so the glow rim reads as a top-edge highlight
        # rather than a shadow behind — correct for light-from-above rendering model)
        color = self._plate_color()
        if color.alpha() > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(plate, radius, radius)

        # CRISP HAIRLINE BORDERS (Half-pixel inset for ultra-sharp rendering).
        #
        # THE RIM IS THE OTHER HALF OF THE HOVER, and it is drawn a half pixel INSIDE the
        # plate, so it can never be clipped however the dock's own rounding falls. A button
        # that has a stroke gets a brighter stroke; one that does not gets a faint glass rim
        # that fades in with the pointer.
        edge = None
        if self._tone == "primary":
            if not self._checked and self._enabled_look:
                edge = QColor(Color.border_strong)
                if self._hover > 0:
                    edge = _mix(edge, QColor(Color.text_secondary), self._hover * 0.55)
        elif self._tone == "danger":
            # Idle keeps NO rim, exactly as before — the shutdown control is meant to be
            # unremarkable until it is looked for. The rim is purely the hover's doing.
            if self._hover > 0.01 or self._pressed:
                edge = _mix(QColor(Color.danger_edge), QColor(Color.danger),
                            min(1.0, self._hover * 0.55 + (0.25 if self._pressed else 0.0)))
        elif self._checked and self._enabled_look:
            edge = QColor(Color.accent_subtle)
            if self._hover > 0:
                edge = _mix(edge, QColor(Color.accent), self._hover * 0.65)
        elif self._hover > 0.01 and self._enabled_look:
            # Subtle glass rim for hovered neutral buttons
            edge = QColor(255, 255, 255, int(40 * self._hover))

        if edge is not None:
            pen = QPen(edge)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            border_plate = plate.adjusted(0.5, 0.5, -0.5, -0.5)
            border_radius = max(0.0, radius - 0.5)
            painter.drawRoundedRect(border_plate, border_radius, border_radius)

        # THERE IS NO OUTER GLOW, AND THERE MUST NOT BE ONE. A ring painted OUTSIDE the
        # plate (`plate.adjusted(-1.25, ...)`) is drawn beyond this widget's own rectangle,
        # so Qt clips it — and the clip lands unevenly against the dock's rounded end-caps,
        # which is what produced the cut-off rounded shadow around the end buttons. The
        # hover now lives entirely inside the bounds: the glyph brightens and the rim lifts.

        # FOCUS RING (accent, visible for keyboard navigation)
        if self.hasFocus():
            pen = QPen(QColor(Color.accent))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(plate.adjusted(-0.5, -0.5, 0.5, 0.5), radius, radius)

        # GLYPH & LABEL RENDERING (Symmetrical, pixel-aligned, press-compensated)
        glyph_color = self._glyph_color()
        icon = _icon_glyph(self._kind, glyph_color, self._glyph_size)
        # Glyph tracks the press compression: shift 1px down+right with the plate.
        glyph_y = int((self.height() - self._glyph_size) / 2) + (1 if self._pressed else 0)

        if self._label:
            glyph_x = int(self._pad_h) + (1 if self._pressed else 0)
            icon.paint(painter, glyph_x, glyph_y, self._glyph_size, self._glyph_size)

            painter.setPen(QColor(glyph_color))
            font = painter.font()
            font.setFamily("Segoe UI")
            font.setPixelSize(Font.small)
            font.setWeight(QFont.Weight.Medium)
            # Subtle letter-spacing (+0.3px) lifts legibility of the short status label
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
            painter.setFont(font)

            px_shift = 1.0 if self._pressed else 0.0
            text_x = float(self._pad_h + self._glyph_size + self._gap) + px_shift
            text_w = float(self.width() - text_x - self._pad_h)
            text_rect = QRectF(text_x, 0.0, text_w, float(self.height()))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._label)
        else:
            glyph_x = int((self.width() - self._glyph_size) / 2) + (1 if self._pressed else 0)
            icon.paint(painter, glyph_x, glyph_y, self._glyph_size, self._glyph_size)

        painter.end()


def _lit(color, amount, strength):
    """
    Raises a colour toward white by `amount * strength`, keeping its alpha.

    THE HOVER IS AN ILLUMINATION, NOT A HIGHLIGHT. A lit control brightens the mark the eye
    is already on; a highlighted one paints a rectangle around it. This is what the dock's
    buttons do under the pointer instead of filling a plate or casting an outer glow, and it
    is by construction unclippable — nothing is drawn outside the glyph that was already
    being drawn.
    """
    amount = max(0.0, min(1.0, float(amount)))
    if amount <= 0.0:
        return color
    return _mix(QColor(color), QColor(255, 255, 255, QColor(color).alpha()),
                amount * strength)


def _mix(a, b, t):
    """Linear blend between two QColors. Used for the accent button's hover."""
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t),
                  int(a.alpha() + (b.alpha() - a.alpha()) * t))


class DockDivider(QWidget):
    """An airy, elegant separator between groups of dock controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(14)
        self.setFixedHeight(Size.dock_button)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # A 1px stroke centred on a WHOLE coordinate straddles two device pixels and renders
        # as a soft 2px smear. Landing it on a half coordinate is what makes it a hairline.
        line_x = int(self.width() / 2.0) + 0.5
        line_h = 18.0
        y1 = round((self.height() - line_h) / 2.0)
        y2 = y1 + line_h
        pen = QPen(QColor(255, 255, 255, 38))  # soft glass alpha ~0.15 — visible but non-assertive
        pen.setWidthF(1.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(line_x, y1, line_x, y2)
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
        layout.setSpacing(Space.xs)

        # ── NAVIGATION: where you are ──
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
        self.talk_button = self.mic_button  # Load-bearing alias: same primary listening control
        self.camera_button = DockButton("camera_off", checkable=True)
        self.gesture_button = DockButton("hand", checkable=True)
        self.power_button = DockButton("power", tone="danger")

        layout.addWidget(self.home_button)
        layout.addWidget(self.chat_button)
        layout.addWidget(DockDivider())
        layout.addWidget(self.mic_button)
        layout.addWidget(self.camera_button)
        layout.addWidget(self.gesture_button)
        layout.addWidget(DockDivider())
        layout.addWidget(self.power_button)

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
        pass

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
