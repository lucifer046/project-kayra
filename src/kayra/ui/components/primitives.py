# ┌────────────────────────────────────────────────────────────────────────┐
# │                           primitives.py                                │
# │              Shared Building Blocks for Every Kayra View               │
# └────────────────────────────────────────────────────────────────────────┘
"""
The component vocabulary. Views compose these; they do not style widgets themselves.

Each component here owns its `objectName` (which is what the generated stylesheet targets) and
exposes a small, obvious API. That is the whole contract: a view that wants a card writes
`Card("Title")`, never a QFrame with a hand-written stylesheet, and the palette therefore stays
changeable from `theme/tokens.py` alone.
"""

from PySide6.QtCore import Qt, Signal, QSize, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QSizePolicy,
    QGraphicsOpacityEffect, QButtonGroup,
)

from kayra.ui.theme import (
    Color, Font, Space, Radius, Size, meter_color, repolish, glyph_dpr,
    VERDICT_COLORS, VERDICT_LABELS,
)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            TEXT HELPERS                                │
# └────────────────────────────────────────────────────────────────────────┘

def _label(text, object_name=None, wrap=False, parent=None):
    widget = QLabel(text, parent)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    # Selectable text everywhere it could be useful to copy: values, IDs, error messages.
    widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def PageTitle(text, parent=None):
    return _label(text, "PageTitle", parent=parent)


def Subtitle(text, parent=None):
    return _label(text, "PageSubtitle", wrap=True, parent=parent)


def SectionLabel(text, parent=None):
    """Small uppercase rule-label. The technical register of the interface."""
    return _label(text.upper(), "SectionLabel", parent=parent)


def Caption(text, parent=None):
    return _label(text, "Caption", wrap=True, parent=parent)


def Body(text, parent=None):
    return _label(text, wrap=True, parent=parent)


def Secondary(text, parent=None):
    return _label(text, "Secondary", wrap=True, parent=parent)


def Mono(text, parent=None):
    return _label(text, "Mono", parent=parent)


def Metric(text, large=False, parent=None):
    """
    A large numeric readout.

    Set in the mono face on purpose: tabular figures mean a value going from 9 to 10 does not
    nudge everything beside it, which is the difference between a dashboard that feels stable
    and one that twitches every refresh.
    """
    return _label(text, "MetricLarge" if large else "Metric", parent=parent)


def GroupLabel(text, parent=None):
    """A quieter heading for a group INSIDE a card, one step below a card title."""
    return _label(text, "GroupLabel", parent=parent)


def RowRule(parent=None):
    """A hairline between rows of a dense list. Lighter than `Divider`."""
    line = QFrame(parent)
    line.setObjectName("RowRule")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line


def Divider(parent=None):
    line = QFrame(parent)
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line


def VDivider(parent=None):
    line = QFrame(parent)
    line.setObjectName("VDivider")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedWidth(1)
    return line


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               CARD                                     │
# └────────────────────────────────────────────────────────────────────────┘

class Card(QFrame):
    """
    A titled panel. `body` is the layout callers add content to.

    Cards are used for genuine grouping only. The brief's warning about "huge cards everywhere"
    is real: a page where everything is boxed communicates no hierarchy at all, so several
    views here deliberately place content directly on the page ground instead.
    """

    def __init__(self, title=None, subtitle=None, flat=False, parent=None):
        super().__init__(parent)
        self.setObjectName("CardFlat" if flat else "Card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Space.base, Space.base, Space.base, Space.base)
        outer.setSpacing(Space.md)

        self._title_text = title or ""
        if title:
            header = QHBoxLayout()
            header.setSpacing(Space.sm)
            self.title_label = _label(title, "CardTitle")
            # THE TITLE MUST NOT SET THE CARD'S MINIMUM WIDTH.
            #
            # A non-wrapping QLabel reports its full text width as its minimum, so a card
            # titled "Hand gesture" could not be narrower than that text plus its padding —
            # measured at 180px for those twelve characters. In a row of cards those minima
            # ADD, and the row's minimum then exceeds the window: Home's bottom strip needed
            # 1330px at a `min_window_width` of 1040, and the last card was simply pushed off
            # the right edge. That is the same defect the System card's footprint caption
            # caused once already, arriving from a different direction.
            #
            # The title is therefore elided to the width it is actually given and keeps its
            # full text in a tooltip. Nothing about the card's content changes; what changes is
            # that a card can now be as narrow as its content genuinely needs.
            self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.title_label.setMinimumWidth(0)
            self.title_label.setToolTip(title)
            header.addWidget(self.title_label, 1)
            self.header_slot = header
            outer.addLayout(header)
        else:
            self.title_label = None
            self.header_slot = None

        if subtitle:
            outer.addWidget(Caption(subtitle))

        self.body = QVBoxLayout()
        self.body.setSpacing(Space.sm)
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body)

    def add_header_widget(self, widget):
        """Puts a control (pill, button) on the card's title row."""
        if self.header_slot is not None:
            self.header_slot.addWidget(widget)

    def resizeEvent(self, event):
        # Elided against the width the title ACTUALLY got, not a guessed character count —
        # the same rule Home's activity rows and captions already follow. A fixed truncation
        # would cut a short title on a wide card and overflow a long one on a narrow card.
        super().resizeEvent(event)
        if self.title_label is None or not self._title_text:
            return
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.title_label.font())
        self.title_label.setText(metrics.elidedText(
            self._title_text, Qt.ElideRight, max(24, self.title_label.width())))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            GLASS PANEL                                 │
# └────────────────────────────────────────────────────────────────────────┘

class GlassPanel(QFrame):
    """
    A translucent floating surface. The dashboard equivalent of `Card`, for Home.

    WHY A SECOND PANEL TYPE RATHER THAN RESTYLING `Card`.
    They are different objects. A `Card` is OPAQUE and sits IN a page — it is what Automation,
    Memory, Activity, System and Settings are built from, where content is dense and a solid
    ground is what makes a table readable. A `GlassPanel` is TRANSLUCENT and floats OVER the
    ambient backdrop, which is the whole point on Home: the warm bloom behind the orb bleeds
    through the panels around it instead of stopping dead at their edges, and that continuity
    is most of what separates "one lit surface" from "five rectangles on a dark page".

    Restyling `Card` to be translucent would have applied that to all five utility screens,
    where a moving backdrop behind a settings form is a distraction rather than depth.

    THE TITLE IS AN OVERLINE, not a heading. Small, tracked, uppercase, tertiary — it names
    the panel without competing with the values inside it. That is the difference between a
    dashboard that reads as instrumentation and one that reads as a stack of headed boxes.
    """

    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setObjectName("GlassPanel")
        # A plain QFrame honours a stylesheet background, but the attribute is set anyway so
        # a future change of base class cannot silently lose the fill — the trap that once
        # rendered the System score tiles as bare text.
        self.setAttribute(Qt.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Space.base, Space.md, Space.base, Space.base)
        outer.setSpacing(Space.md)

        self._title_text = title or ""
        if title:
            header = QHBoxLayout()
            header.setSpacing(Space.sm)
            self.title_label = _label(title, "GlassPanelTitle")
            # Same rule as `Card`: a non-wrapping QLabel reports its full text width as its
            # minimum, and in a row of panels those minima ADD until the row cannot fit the
            # window. Elide to the width actually given, keep the full text in a tooltip.
            self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.title_label.setMinimumWidth(0)
            self.title_label.setToolTip(title)
            header.addWidget(self.title_label, 1)
            self.header_slot = header
            outer.addLayout(header)
        else:
            self.title_label = None
            self.header_slot = None

        self.body = QVBoxLayout()
        self.body.setSpacing(Space.sm)
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body)

    def add_header_widget(self, widget):
        """Puts a control (a pill, a small button) on the panel's title row."""
        if self.header_slot is not None:
            self.header_slot.addWidget(widget)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.title_label is None or not self._title_text:
            return
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.title_label.font())
        self.title_label.setText(metrics.elidedText(
            self._title_text, Qt.ElideRight, max(24, self.title_label.width())))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            STATUS PILL                                 │
# └────────────────────────────────────────────────────────────────────────┘

class StatusPill(QLabel):
    """Compact state chip. Tone is semantic, never a raw colour."""

    TONES = ("neutral", "accent", "success", "warning", "danger")

    def __init__(self, text="", tone="neutral", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.set_tone(tone)

    def set_tone(self, tone):
        if tone not in self.TONES:
            tone = "neutral"
        self.setProperty("tone", tone)
        repolish(self)

    def set_status(self, text, tone):
        self.setText(text)
        self.set_tone(tone)

    @classmethod
    def for_verdict(cls, verdict, parent=None):
        """Builds a pill from a `system_profile` verdict, keeping that mapping in one place."""
        tone = {
            "READY": "success", "GOOD": "success",
            "LIMITED": "warning", "REQUIRES_CONFIGURATION": "warning",
            "NOT_AVAILABLE": "danger",
        }.get(verdict, "neutral")
        return cls(VERDICT_LABELS.get(verdict, verdict), tone, parent)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                           ACTION STATUS                                │
# └────────────────────────────────────────────────────────────────────────┘

class ActionStatus(QFrame):
    """
    The outcome of one automation action: a drawn mark and a word, in a tinted field.

    WHY THIS IS NOT JUST A `StatusPill`. Success and failure are the single most important
    distinction on the Automation screen, and as neutral-toned pills they were two dark
    rectangles with slightly different text colours — legible if you looked, invisible at a
    glance, which is the opposite of what a status is for.

    Three things make the difference unmistakable without shouting:

      * a tinted field one surface step above the card, not a near-black wash;
      * a DRAWN mark — a check, a cross, a dot — so the state is carried by SHAPE as well as
        hue and survives a monochrome screen or a red-green colour deficiency;
      * identical metrics for every variant, so a column of them reads as a column instead of
        a ragged edge, and the eye compares colour rather than measuring width.

    It is deliberately quieter than the action title beside it: the title says what Kayra did,
    this says how it went.
    """

    TONES = {
        "success": ("success", "check"),
        "danger": ("danger", "cross"),
        "warning": ("warning", "dot"),
        "neutral": ("neutral", "dot"),
    }

    def __init__(self, text="", tone="neutral", parent=None):
        super().__init__(parent)
        self.setObjectName("ActionStatus")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        # Room for the drawn mark on the left; the label never runs under it.
        layout.setContentsMargins(Space.sm + 12, 0, Space.sm, 0)
        layout.setSpacing(0)

        self.label = _label("", "ActionStatusLabel")
        layout.addWidget(self.label)

        self._mark = "dot"
        self.set_status(text, tone)

    def set_status(self, text, tone="neutral"):
        if tone not in self.TONES:
            tone = "neutral"
        semantic, mark = self.TONES[tone]
        self._mark = mark
        self.label.setText(str(text))
        self.setProperty("tone", semantic)
        repolish(self)
        # Sized to the text, so nothing is ever clipped, with one shared height. Measured
        # rather than guessed: a fixed width truncates "Requires confirmation" and leaves a
        # gulf after "Done".
        from PySide6.QtGui import QFontMetrics
        width = QFontMetrics(self.label.font()).horizontalAdvance(self.label.text())
        self.setFixedHeight(22)
        self.setFixedWidth(int(width + Space.sm * 2 + 12 + 2))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)          # the stylesheet paints the field and the border
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        color = QColor({"success": Color.success, "danger": Color.danger,
                        "warning": Color.warning}.get(self.property("tone"),
                                                      Color.text_secondary))
        pen = QPen(color)
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        x = Space.sm + 1.0
        cy = self.height() / 2.0
        if self._mark == "check":
            painter.drawPolyline([QPointF(x, cy), QPointF(x + 2.8, cy + 3.0),
                                  QPointF(x + 7.6, cy - 3.4)])
        elif self._mark == "cross":
            painter.drawLine(QPointF(x + 0.6, cy - 3.2), QPointF(x + 7.0, cy + 3.2))
            painter.drawLine(QPointF(x + 7.0, cy - 3.2), QPointF(x + 0.6, cy + 3.2))
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x + 3.8, cy), 2.2, 2.2)
        painter.end()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              BUTTONS                                   │
# └────────────────────────────────────────────────────────────────────────┘

def AccentButton(text, parent=None):
    button = QPushButton(text, parent)
    button.setProperty("variant", "accent")
    button.setCursor(Qt.PointingHandCursor)
    return button


def GhostButton(text, parent=None):
    button = QPushButton(text, parent)
    button.setProperty("variant", "ghost")
    button.setCursor(Qt.PointingHandCursor)
    return button


def CardAction(text, tone="neutral", parent=None):
    """
    A compact control for a card's title row, matching `StatusPill`'s height exactly.

    Card headers mix a status pill and an action ("Clear", "Clear all"). A default QPushButton
    is 40px tall against the pill's 22px, so the two sat on visibly different baselines on
    every screen that had both.
    """
    button = QPushButton(text, parent)
    button.setObjectName("CardAction")
    button.setProperty("tone", tone)
    button.setCursor(Qt.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return button


class OutlineButton(QPushButton):
    """
    A secondary control with an outline, a drawn icon and a label.

    Used where an action is important enough to name but must not read as the primary one —
    "Pause listening" beside "Talk to Kayra". Outlined rather than filled precisely so it
    cannot be mistaken for a destructive or primary button; the microphone toggle sitting next
    to a filled amber call-to-action would otherwise look like a second way to start.
    """

    def __init__(self, text, icon=None, parent=None):
        super().__init__(text, parent)
        self.setProperty("variant", "outline")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._icon_kind = None
        self.setIconSize(QSize(15, 15))
        if icon:
            self.set_icon(icon)

    def set_icon(self, kind):
        if kind == self._icon_kind:
            return
        self._icon_kind = kind
        # Redrawn on change rather than per paint: the glyph is a pixmap and the stylesheet
        # cannot tint it. This happens once per state change, not once per frame.
        self.setIcon(_icon_glyph(kind, Color.text_secondary))

    def set_label(self, text):
        if text != self.text():
            self.setText(text)


def DangerButton(text, parent=None):
    button = QPushButton(text, parent)
    button.setProperty("variant", "danger")
    button.setCursor(Qt.PointingHandCursor)
    return button


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               TOGGLE                                   │
# └────────────────────────────────────────────────────────────────────────┘

class Toggle(QPushButton):
    """
    A switch. Drawn rather than styled, because QSS cannot express a sliding knob and the
    checkbox indicator hack looks like a checkbox no matter how it is dressed.

    Keyboard-operable and focus-visible: it is a QPushButton underneath, so Space and Enter
    work and Tab reaches it without any extra handling.
    """

    WIDTH = 40
    HEIGHT = 22

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        # An objectName the stylesheet can neutralise. Without it the generic `QPushButton`
        # rule (min-height 32, padding 8/16, a border and a background) applied to this widget
        # too — Qt's stylesheet minimum WINS over setFixedSize, so the 40x22 switch was laid
        # out 32px tall and its drawn knob rendered as a clipped half-circle blob.
        self.setObjectName("Toggle")
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event):
        """
        Drawn in FLOAT geometry against the widget rect.

        The previous version drew the knob at integer offsets from `self.width()` and the
        track at `rect().adjusted(0,0,-1,-1)`, which on a fractional device-pixel-ratio (the
        125%/150% scaling Windows laptops ship with) put the knob a half pixel outside the
        track and clipped its right edge. Everything here is derived from the rect in float,
        so it lands correctly at any scale factor.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        on = self.isChecked()
        enabled = self.isEnabled()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0

        if not enabled:
            track, edge = QColor(Color.disabled), QColor(Color.border_subtle)
        elif on:
            track, edge = QColor(Color.accent), QColor(Color.accent)
        else:
            track, edge = QColor(Color.inset), QColor(Color.border_strong)

        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)

        pen = QPen(QColor(Color.accent_hover) if self.hasFocus() else edge)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        if not enabled:
            knob = QColor(Color.text_disabled)
        elif on:
            knob = QColor(Color.text_on_accent)
        else:
            knob = QColor(Color.text_secondary)

        inset = 3.0
        diameter = rect.height() - 2 * inset
        x = (rect.right() - inset - diameter) if on else (rect.left() + inset)
        painter.setPen(Qt.NoPen)
        painter.setBrush(knob)
        painter.drawEllipse(QRectF(x, rect.top() + inset, diameter, diameter))
        painter.end()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              METERS                                    │
# └────────────────────────────────────────────────────────────────────────┘

class Meter(QWidget):
    """
    A labelled utilisation bar: name on the left, value on the right, track beneath.

    Colour follows load through `theme.meter_color`, so the thresholds live with the palette
    instead of being reinvented per screen. `set_value` repaints one small widget and never
    rebuilds anything, which is what makes a 2-second refresh free.
    """

    def __init__(self, name, suffix="%", parent=None):
        super().__init__(parent)
        self._name = name
        self._suffix = suffix
        self._value = 0.0
        self._caption = ""
        self._track_color = QColor(Color.meter_track)
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, value, caption=""):
        self._value = max(0.0, min(100.0, float(value)))
        self._caption = caption
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width = self.width()

        name_font = QFont()
        name_font.setPixelSize(Font.small)
        painter.setFont(name_font)
        painter.setPen(QColor(Color.text_secondary))
        painter.drawText(0, 0, width, 16, Qt.AlignLeft | Qt.AlignVCenter, self._name)

        value_font = QFont("Cascadia Mono")
        value_font.setPixelSize(Font.small)
        painter.setFont(value_font)
        painter.setPen(QColor(Color.text))
        readout = self._caption or f"{self._value:.0f}{self._suffix}"
        # Elide rather than overlap: a long caption used to run straight through the name.
        available = width - painter.fontMetrics().horizontalAdvance(self._name) - Space.base
        if painter.fontMetrics().horizontalAdvance(readout) > available > 0:
            from PySide6.QtGui import QFontMetrics
            readout = QFontMetrics(value_font).elidedText(readout, Qt.ElideRight,
                                                          max(24, int(available)))
        painter.drawText(0, 0, width, 16, Qt.AlignRight | Qt.AlignVCenter, readout)

        track_y = 26
        track_h = 4
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._track_color)
        painter.drawRoundedRect(0, track_y, width, track_h, 2, 2)

        filled = int(width * self._value / 100.0)
        if filled > 0:
            painter.setBrush(QColor(meter_color(self._value)))
            painter.drawRoundedRect(0, track_y, max(filled, 4), track_h, 2, 2)
        painter.end()


class StatRow(QWidget):
    """A label/value pair. The workhorse of the System page's device tables."""

    def __init__(self, name, value="", mono=True, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, Space.xs, 0, Space.xs)
        layout.setSpacing(Space.base)

        self.name_label = _label(name, "Secondary")
        self.name_label.setMinimumWidth(150)
        self.value_label = _label(value, "Mono" if mono else None, wrap=True)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(self.name_label)
        layout.addStretch(1)
        layout.addWidget(self.value_label)

    def set_value(self, value):
        self.value_label.setText(str(value))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          EMPTY / LOADING                               │
# └────────────────────────────────────────────────────────────────────────┘

class EmptyState(QWidget):
    """
    Shown wherever a list can legitimately be empty.

    Every empty region in the app gets one of these. A blank panel is indistinguishable from a
    broken panel, and "nothing here yet" plus a reason is the difference between the two.
    """

    def __init__(self, title, detail="", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Space.lg, Space.xl, Space.lg, Space.xl)
        layout.setSpacing(Space.sm)
        layout.setAlignment(Qt.AlignCenter)

        self._heading = _label(title, "Secondary")
        self._heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._heading)

        self._caption = Caption(detail)
        self._caption.setAlignment(Qt.AlignCenter)
        self._caption.setMaximumWidth(420)
        self._caption.setVisible(bool(detail))
        layout.addWidget(self._caption, alignment=Qt.AlignCenter)

    def set_message(self, title, detail=""):
        """
        Re-labels in place.

        Empty states are SHOWN AND HIDDEN, never created and destroyed — a torn-down widget
        cannot come back when the list empties again, and a widget removed from a layout but
        not yet deleted keeps painting at its stale geometry, which is how "Nothing yet" ended
        up drawn straight through the middle of the Activity timeline.
        """
        self._heading.setText(title)
        self._caption.setText(detail)
        self._caption.setVisible(bool(detail))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SEGMENTED CONTROL                               │
# └────────────────────────────────────────────────────────────────────────┘

class SegmentedControl(QWidget):
    """
    Exclusive options rendered as ONE control rather than several buttons.

    Activity's filters used to be four full-size QPushButtons in a row, which reads as four
    unrelated actions — and at 38px tall they were the most prominent thing on the page,
    louder than the content they filter. A segmented track says "these are the modes of one
    thing" with a fraction of the weight.

    Keyboard-operable for free: the segments are real checkable buttons in a QButtonGroup, so
    Tab reaches them and Space activates.
    """

    changed = Signal(str)

    def __init__(self, options, current=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        self.setAttribute(Qt.WA_StyledBackground, True)   # a bare QWidget ignores QSS background
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._items = {}
        for key, label in options:
            button = QPushButton(label, self)
            button.setObjectName("SegmentedItem")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.StrongFocus)
            button.clicked.connect(lambda _=False, k=key: self._select(k))
            self._group.addButton(button)
            self._items[key] = button
            layout.addWidget(button)

        self.select(current or (options[0][0] if options else None))

    def _select(self, key):
        self.select(key)
        self.changed.emit(key)

    def select(self, key):
        button = self._items.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._current = key

    def current(self):
        return getattr(self, "_current", None)

    def set_count(self, key, count):
        """Appends a live count to a segment's label, e.g. 'Automation 4'."""
        button = self._items.get(key)
        if button is None:
            return
        base = button.property("baseText")
        if base is None:
            base = button.text()
            button.setProperty("baseText", base)
        button.setText(f"{base}  {count}" if count else base)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            DISCLOSURE                                  │
# └────────────────────────────────────────────────────────────────────────┘

class Disclosure(QWidget):
    """
    A collapsible section: a clickable header and a body that is hidden until asked for.

    This is how the System guide and Settings' advanced block stay useful without putting
    every explanation on screen at once. The chevron is drawn, so it cannot fall back to a
    missing glyph, and rotates by state rather than being two different characters.

    The body is hidden with `setVisible`, not by removing it, so expanding costs no layout
    rebuild and the state survives a navigation away and back.
    """

    toggled = Signal(bool)

    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QPushButton(title, self)
        self.header.setObjectName("Disclosure")
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setIcon(_chevron(expanded))
        self.header.setIconSize(QSize(12, 12))
        self.header.toggled.connect(self._on_toggled)
        layout.addWidget(self.header)

        self._body_host = QWidget(self)
        self.body = QVBoxLayout(self._body_host)
        self.body.setContentsMargins(Space.lg, Space.xs, Space.sm, Space.md)
        self.body.setSpacing(Space.sm)
        self._body_host.setVisible(expanded)
        layout.addWidget(self._body_host)

    def _on_toggled(self, checked):
        self.header.setIcon(_chevron(checked))
        self._body_host.setVisible(checked)
        self.toggled.emit(checked)

    def set_expanded(self, expanded):
        self.header.setChecked(bool(expanded))

    def add(self, widget):
        self.body.addWidget(widget)
        return widget


def _chevron(expanded, size=12, dpr=None):
    """
    A drawn chevron, pointing right when collapsed and down when expanded.

    Painted in LOGICAL units on a pixmap carrying a devicePixelRatio. Drawing to `size * dpr`
    instead would paint at double scale and show only the top-left quarter — the exact
    high-DPI mistake the navigation icons already had to be fixed for.
    """
    dpr = glyph_dpr() if dpr is None else dpr
    pixmap = QPixmap(size * dpr, size * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(Color.text_tertiary))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = float(size)
    if expanded:
        painter.drawPolyline([QPointF(s * 0.24, s * 0.40), QPointF(s * 0.5, s * 0.66),
                              QPointF(s * 0.76, s * 0.40)])
    else:
        painter.drawPolyline([QPointF(s * 0.40, s * 0.24), QPointF(s * 0.66, s * 0.5),
                              QPointF(s * 0.40, s * 0.76)])
    painter.end()
    return QIcon(pixmap)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                           ICON BUTTON                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _icon_glyph(kind, color, size=16, dpr=None):
    """One drawn pictogram, in the same line weight as the navigation rail."""
    dpr = glyph_dpr() if dpr is None else dpr
    pixmap = QPixmap(size * dpr, size * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    s = float(size)

    if kind == "mic":
        painter.drawRoundedRect(s * 0.36, s * 0.14, s * 0.28, s * 0.44, s * 0.14, s * 0.14)
        painter.drawArc(int(s * 0.24), int(s * 0.36), int(s * 0.52), int(s * 0.42),
                        180 * 16, 180 * 16)
        painter.drawLine(QPointF(s * 0.5, s * 0.72), QPointF(s * 0.5, s * 0.88))
    elif kind == "send":
        painter.drawPolyline([QPointF(s * 0.18, s * 0.5), QPointF(s * 0.82, s * 0.5)])
        painter.drawPolyline([QPointF(s * 0.56, s * 0.26), QPointF(s * 0.82, s * 0.5),
                              QPointF(s * 0.56, s * 0.74)])
    elif kind == "stop":
        painter.drawRoundedRect(s * 0.3, s * 0.3, s * 0.4, s * 0.4, s * 0.08, s * 0.08)
    elif kind == "close":
        painter.drawLine(QPointF(s * 0.28, s * 0.28), QPointF(s * 0.72, s * 0.72))
        painter.drawLine(QPointF(s * 0.72, s * 0.28), QPointF(s * 0.28, s * 0.72))
    elif kind == "search":
        painter.drawEllipse(QPointF(s * 0.44, s * 0.44), s * 0.24, s * 0.24)
        painter.drawLine(QPointF(s * 0.62, s * 0.62), QPointF(s * 0.82, s * 0.82))
    elif kind == "mic_off":
        # The microphone with a slash. A DIFFERENT SHAPE, not just a dimmer colour: "can Kayra
        # hear me" must not depend on noticing a shade.
        painter.drawRoundedRect(s * 0.36, s * 0.14, s * 0.28, s * 0.44, s * 0.14, s * 0.14)
        painter.drawArc(int(s * 0.24), int(s * 0.36), int(s * 0.52), int(s * 0.42),
                        180 * 16, 180 * 16)
        painter.drawLine(QPointF(s * 0.5, s * 0.72), QPointF(s * 0.5, s * 0.88))
        painter.drawLine(QPointF(s * 0.16, s * 0.84), QPointF(s * 0.84, s * 0.16))
    elif kind == "camera":
        painter.drawRoundedRect(QRectF(s * 0.12, s * 0.28, s * 0.62, s * 0.44), 2.0, 2.0)
        painter.drawPolyline([QPointF(s * 0.78, s * 0.42), QPointF(s * 0.90, s * 0.32),
                              QPointF(s * 0.90, s * 0.68), QPointF(s * 0.78, s * 0.58)])
    elif kind == "camera_off":
        # The camera with a slash, for the same reason `mic_off` exists: whether a camera is
        # watching must be readable as a SHAPE, never as a shade of the same shape. This one
        # is the more important of the two — a user cannot hear a camera being on.
        painter.drawRoundedRect(QRectF(s * 0.12, s * 0.28, s * 0.62, s * 0.44), 2.0, 2.0)
        painter.drawPolyline([QPointF(s * 0.78, s * 0.42), QPointF(s * 0.90, s * 0.32),
                              QPointF(s * 0.90, s * 0.68), QPointF(s * 0.78, s * 0.58)])
        painter.drawLine(QPointF(s * 0.10, s * 0.86), QPointF(s * 0.90, s * 0.14))
    elif kind == "hand":
        # Four fingers and a thumb, at the same stroke weight as the rest of the set. Drawn
        # rather than a glyph font: the navigation rail's icons are all drawn, and one font
        # emoji among them reads as a different application's artwork.
        for i, x in enumerate((0.34, 0.48, 0.62)):
            painter.drawLine(QPointF(s * x, s * (0.20 + i * 0.02)), QPointF(s * x, s * 0.62))
        painter.drawLine(QPointF(s * 0.74, s * 0.34), QPointF(s * 0.74, s * 0.62))
        painter.drawArc(int(s * 0.26), int(s * 0.46), int(s * 0.52), int(s * 0.46),
                        180 * 16, 180 * 16)
        painter.drawLine(QPointF(s * 0.26, s * 0.58), QPointF(s * 0.16, s * 0.46))
    elif kind == "pause":
        # Two filled bars. A PAUSE, not the stop square used for barge-in — the two actions
        # are different and must not share a symbol.
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(s * 0.32, s * 0.24, s * 0.11, s * 0.52), 1.2, 1.2)
        painter.drawRoundedRect(QRectF(s * 0.57, s * 0.24, s * 0.11, s * 0.52), 1.2, 1.2)
    elif kind == "menu":
        # Three rules, the middle one short. A plain hamburger is three equal lines and reads
        # as a list; the stepped version reads as a panel that slides, which is what it does.
        painter.drawLine(QPointF(s * 0.18, s * 0.28), QPointF(s * 0.82, s * 0.28))
        painter.drawLine(QPointF(s * 0.18, s * 0.50), QPointF(s * 0.60, s * 0.50))
        painter.drawLine(QPointF(s * 0.18, s * 0.72), QPointF(s * 0.82, s * 0.72))
    elif kind == "power":
        # The IEC power mark: a broken ring with a vertical bar. Universally read as "off",
        # which is exactly what this control does — and it must never be mistaken for the
        # pause bars beside it.
        painter.drawArc(int(s * 0.22), int(s * 0.22), int(s * 0.56), int(s * 0.56),
                        -60 * 16, 300 * 16)
        painter.drawLine(QPointF(s * 0.5, s * 0.14), QPointF(s * 0.5, s * 0.46))
    elif kind == "talk":
        # A speech burst: three rising strokes inside a soft arc. Distinct from `chat`, which
        # is a bubble and means "go to the transcript"; this one means "say something now".
        for index, (x, height) in enumerate(((0.36, 0.16), (0.50, 0.26), (0.64, 0.20))):
            painter.drawLine(QPointF(s * x, s * (0.5 - height)),
                             QPointF(s * x, s * (0.5 + height)))
        painter.drawArc(int(s * 0.14), int(s * 0.14), int(s * 0.72), int(s * 0.72),
                        120 * 16, 120 * 16)
        painter.drawArc(int(s * 0.14), int(s * 0.14), int(s * 0.72), int(s * 0.72),
                        -60 * 16, 120 * 16)
    elif kind == "chat":
        painter.drawRoundedRect(QRectF(s * 0.14, s * 0.18, s * 0.72, s * 0.50),
                                s * 0.12, s * 0.12)
        painter.drawPolyline([QPointF(s * 0.32, s * 0.68), QPointF(s * 0.32, s * 0.86),
                              QPointF(s * 0.54, s * 0.68)])
    elif kind == "minimize":
        painter.drawLine(QPointF(s * 0.24, s * 0.52), QPointF(s * 0.76, s * 0.52))
    elif kind == "maximize":
        painter.drawRoundedRect(QRectF(s * 0.24, s * 0.24, s * 0.52, s * 0.52), 1.5, 1.5)
    elif kind == "restore":
        # Two offset rectangles: the standard "this window is maximised, click to restore"
        # mark. A different SHAPE from `maximize`, not a different tint of it.
        painter.drawRoundedRect(QRectF(s * 0.20, s * 0.32, s * 0.44, s * 0.44), 1.5, 1.5)
        painter.drawPolyline([QPointF(s * 0.34, s * 0.32), QPointF(s * 0.34, s * 0.22),
                              QPointF(s * 0.78, s * 0.22), QPointF(s * 0.78, s * 0.64),
                              QPointF(s * 0.66, s * 0.64)])
    painter.end()
    return QIcon(pixmap)


class IconButton(QPushButton):
    """A square control carrying a drawn glyph and no text."""

    def __init__(self, kind, tooltip="", checkable=False, parent=None):
        super().__init__(parent)
        self.setObjectName("IconButton")
        self._kind = kind
        self.setIcon(_icon_glyph(kind, Color.text_secondary))
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        if tooltip:
            self.setToolTip(tooltip)
        if checkable:
            self.setCheckable(True)
            self.toggled.connect(self._recolor)

    def _recolor(self, checked):
        # The glyph is a pixmap, so the stylesheet cannot tint it; redraw on state change.
        self.setIcon(_icon_glyph(self._kind, Color.accent if checked else Color.text_secondary))

    def set_icon(self, kind):
        """
        Swaps the glyph — a microphone for a struck-through microphone, say.

        The button's MEANING can change with its state (listening vs paused), and a state the
        user must be able to read at a glance should not be carried by colour alone.
        """
        if kind == self._kind:
            return
        self._kind = kind
        self.setIcon(_icon_glyph(kind, Color.accent if (self.isCheckable() and self.isChecked())
                                 else Color.text_secondary))

    def set_enabled_look(self, enabled):
        self.setEnabled(enabled)
        self.setIcon(_icon_glyph(self._kind,
                                 Color.text_secondary if enabled else Color.text_disabled))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             LIST ROW                                   │
# └────────────────────────────────────────────────────────────────────────┘

class ListRow(QFrame):
    """
    One row of a dense list, with a hover state and a consistent height.

    Exists so timelines, action histories and memory lists are literally the same component
    instead of three hand-built QHBoxLayouts that drift apart — which is what the consistency
    audit found across Activity, Automation and Memory.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ListRow")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(Space.sm, Space.xs, Space.sm, Space.xs)
        self.row.setSpacing(Space.md)
        self.setMinimumHeight(34)


def fade_in(widget, duration=None):
    """
    A short opacity fade, used when a view's content is replaced.

    Returns the animation so the caller can keep a reference — a QPropertyAnimation that is
    garbage-collected mid-flight simply stops, which looks like a rendering glitch.
    """
    from PySide6.QtCore import QPropertyAnimation, QEasingCurve
    from kayra.ui.theme import Motion

    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration or Motion.normal)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    animation.start()
    return animation
