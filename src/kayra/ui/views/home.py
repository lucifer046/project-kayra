# ┌────────────────────────────────────────────────────────────────────────┐
# │                              home.py                                   │
# │                    The Assistant's Primary Surface                     │
# └────────────────────────────────────────────────────────────────────────┘
"""
Home answers one question before any other: is Kayra there, and what is it doing?

THE COMPOSITION, AND THE ONE IT REPLACED
----------------------------------------
The previous Home stacked a centred hero over a strip of five small cards pinned to the
bottom. On a 1440x900 window that left roughly a third of the viewport as bare background
down both sides of the orb, while every fact the page had to show was compressed into five
206px-tall boxes at the very bottom. It read as a screen that had failed to finish loading,
and it was the specific complaint this redesign answers.

The page is now a THREE-COLUMN INSTRUMENT PANEL that fills the viewport:

    ┌───────────────┬───────────────────────┬───────────────┐
    │ INTELLIGENCE  │                       │    SYSTEM     │
    │               │         ORB           │               │
    ├───────────────┤       Listening       ├───────────────┤
    │ INTERACTION   │     Microphone open   │   ACTIVITY    │
    └───────────────┴───────────────────────┴───────────────┘
                      [ the floating dock ]

Four panels instead of five cards, and they are grouped by SUBJECT rather than by which
subsystem produced them:

  * INTELLIGENCE — where the thinking happens: the live model tier, both routes, and the
    device speech is actually synthesised on.
  * SYSTEM       — the machine: what it is, and what it is doing.
  * INTERACTION  — the input devices: microphone, camera, hands, and the presence layer.
  * ACTIVITY     — what has been said.

That grouping is the point. "GPU utilization" and "VRAM" were their own card before, which
made a property of the machine look like a subsystem of the assistant; they belong in SYSTEM
beside the processor. "Which provider is speech running on" was in that same card, and it
belongs in INTELLIGENCE beside the model routing, because it is a fact about what is
answering rather than about what the machine contains.

WHY THE ORB IS STILL THE MIDDLE COLUMN
--------------------------------------
It is the only element on this page a person looks AT rather than reads. Panels flank it, and
the ambient backdrop's warm bloom is placed behind it (see `on_show`), so the glow reads as
the orb's own light spilling onto the page rather than as a gradient someone added.

CONTROLS LIVE IN THE DOCK, NOT HERE
-----------------------------------
Home is now pure STATUS. Every control a person reaches for — talk, microphone, camera,
gestures, shutdown — is in the floating dock the window puts over this page, and the actions
behind them live in `ui/controls.py` so that dock, the tray and the keyboard shortcuts all
run the same code. Home reading state it cannot change is what makes it impossible for a
control on this screen to disagree with the same control on the dock two inches below it.

NOTHING HERE POLLS THE ASSISTANT. State arrives on `voiceStateChanged`; the panels refresh
only while the screen is visible, on a slow timer, from data the backend already keeps.
"""

from PySide6.QtCore import Qt, QTimer, QSize, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QLinearGradient
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

from kayra.ui.theme import Space, Size, Motion, Color, Font
from kayra.ui.components.orb import AssistantOrb
from kayra.ui.components.primitives import (
    GlassPanel, Caption, StatusPill, Meter, EmptyState, ListRow, RowRule, _label,
)
from kayra.ui.components.camera_preview import CameraPreview
from kayra.ui.views.base import View


# The GPU statistics and the speech device are DIFFERENT FACTS, and this tooltip is where that
# is said out loud. A machine can have a perfectly good GPU while speech runs on the CPU.
_GPU_DETAIL_TOOLTIP = (
    "The provider speech synthesis is actually using. The graphics adapter above is the "
    "physical device, which exists whether or not Kayra is using it.")


PROMPTS = {
    "IDLE": "How can I help?",
    "LISTENING": "Listening…",
    "PROCESSING": "Thinking…",
    "SPEAKING": "Speaking",
    "AUTOMATING": "Working on your machine",
    "INTERRUPTING": "Stopped",
    "ERROR": "Something went wrong",
    "STARTING": "Starting up…",
    "OFFLINE": "Not connected",
    "SHUTTING_DOWN": "Shutting down",
}


class _AccentRule(QWidget):
    """
    A short hairline under the Home wordmark, in the accent, fading out at both ends.

    THE ACCENT LANGUAGE WITHOUT THE ACCENT VOLUME. Setting the wordmark itself in amber
    would put a second warm focal point directly above the orb, which is the one element on
    this page that is meant to hold the eye. A 64px rule says "this is Kayra's colour" in
    about a hundred pixels of ink and then gets out of the way — and because it is centred
    and horizontal, it also reads as an arrow pointing down the column.

    STATIC. No timer, no animation: the orb and the backdrop are the only things on this
    page that move, and a third animated element would be a third thing competing.
    """

    WIDTH = 64
    HEIGHT = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        mid = QColor(Color.accent)
        edge = QColor(Color.accent)
        mid.setAlphaF(0.72)
        edge.setAlphaF(0.0)
        gradient = QLinearGradient(QPointF(0.0, 0.0), QPointF(float(self.width()), 0.0))
        gradient.setColorAt(0.0, edge)
        gradient.setColorAt(0.5, mid)
        gradient.setColorAt(1.0, edge)
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        y = (self.height() - 1.0) / 2.0
        painter.drawRoundedRect(QRectF(0.0, y, float(self.width()), 1.0), 0.5, 0.5)
        painter.end()


class _ActivityLine(ListRow):
    """
    One line of recent activity: who spoke, and what was said.

    Elided rather than wrapped. This panel is a glance — a wrapping line would resize the
    column every time a long reply arrived, and the anchor would jump.
    """

    def __init__(self, who, text, voice=False, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.row.setSpacing(Space.md)

        label = _label(who, "Caption")
        label.setFixedWidth(44)
        if voice:
            label.setToolTip("Spoken")

        body = _label("", "Secondary")
        body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body.setToolTip(text)
        self._text = " ".join((text or "").split())
        self._body = body

        self.row.addWidget(label)
        self.row.addWidget(body, 1)

    def resizeEvent(self, event):
        # Elide against the width the row actually got, not a guessed character count. A fixed
        # `text[:87]` truncates a short line on a wide window and overflows a narrow one.
        super().resizeEvent(event)
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self._body.font())
        self._body.setText(metrics.elidedText(self._text, Qt.ElideRight,
                                              max(40, self._body.width() - 4)))


class _ElidedCaption(QLabel):
    """
    A one-line caption that elides to the width it is GIVEN, and re-elides when that changes.

    WHY THIS EXISTS RATHER THAN A HELPER FUNCTION. The previous approach called an `_elide()`
    helper from wherever the text was set, and that is a TIMING assumption: it reads
    `label.width()`, which is only meaningful after the layout has run. A line written before
    its panel was laid out — the machine identity line, which arrives on a background thread
    a second into the session — measured itself against a stale width, decided nothing needed
    trimming, and then nothing ever re-elided it because `resizeEvent` on the page only fires
    when the WINDOW changes size. Measured: 570px of text sitting in a 374px label, running
    straight out of its panel.

    A widget that elides in its own `resizeEvent` cannot have that bug: the layout tells it
    its width, and that is exactly when it decides. The full text always stays in the tooltip.
    """

    def __init__(self, text="", parent=None):
        super().__init__("", parent)
        self.setObjectName("Caption")
        self.setWordWrap(False)
        # `Ignored`, so the label never forces its panel wider than its layout share — the
        # defect that once clipped the last card off the right edge of this page.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._full = ""
        self.set_full_text(text)

    def set_full_text(self, text):
        self._full = " ".join(str(text or "").split())
        self.setToolTip(self._full)
        self._apply()

    def full_text(self):
        return self._full

    def _apply(self):
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(self._full, Qt.ElideRight,
                                        max(40, self.width() - 4)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()


class _StatusLine(QWidget):
    """
    A label, a value, and an optional state dot — the row every panel here is built from.

    ONE ROW COMPONENT, used by all four panels. Before the redesign the same shape was
    hand-built three different ways across the bottom strip, and they had already drifted
    apart in padding and in how they elided. The value is elided to the width it is GIVEN and
    keeps its full text in a tooltip, so no row can force its panel wider than its layout
    share — the defect that once clipped the last card off the right edge of this page.
    """

    def __init__(self, name, value="—", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Space.md)

        self.name_label = _label(name, "Caption")
        self.name_label.setFixedWidth(78)
        self.value_label = _label(value, "Secondary")
        self.value_label.setWordWrap(False)
        self.value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.value_label.setMinimumWidth(0)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row.addWidget(self.name_label)
        row.addWidget(self.value_label, 1)
        self._value_text = value

    def set_value(self, text):
        self._value_text = text or "—"
        self._elide()

    def _elide(self):
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.value_label.font())
        self.value_label.setToolTip(self._value_text)
        self.value_label.setText(metrics.elidedText(
            self._value_text, Qt.ElideRight, max(40, self.value_label.width() - 2)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class HomeView(View):
    title = ""          # Home is deliberately titleless: the orb IS the heading.

    # How many activity lines the panel keeps. It is a glance, not a log; Activity holds the
    # full history. Larger than the old four because the panel is now a full column tall.
    ACTIVITY_LINES = 7

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        # ── NO PAGE SCROLL ──
        # Home must fit. The base class wraps content in a scroll area for the dense screens;
        # here the holder is made to EXPAND so it always receives exactly the viewport height
        # and the bar never appears. Panels absorb the slack instead of the page growing.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = self.scroll.widget()
        if holder is not None:
            holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        # The bottom margin clears the floating dock. The dock is an overlay owned by the
        # window, so it is not in this layout and cannot reserve its own space — without this
        # it would sit on top of the lowest row of both outer columns.
        self.content.setContentsMargins(
            Space.lg, Space.md, Space.lg,
            Size.dock_height + Size.dock_margin_bottom + Space.md)
        self.content.setSpacing(Space.base)

        self._build_columns()

        self._timer = QTimer(self)
        self._timer.setInterval(Motion.metrics_interval * 2)
        self._timer.timeout.connect(self._refresh_panels)

        # THE VOICE CAPTION HAS ONE SOURCE. This screen used to compose it from a cached
        # `_state` and a cached `_listening`, which is how a user talking to an open
        # microphone could be told "Listening paused": neither cached value was wrong, and
        # together they did not describe the situation. `voiceStateChanged` carries the
        # resolved answer and its revision; nothing here re-derives it.
        self._voice_revision = -1
        self._voice_state = "OFFLINE"
        # Latched by the window when teardown begins, so a late boot or voice re-sync cannot
        # repaint this page as though the assistant were still running.
        self._shutting_down = False

        bridge.voiceStateChanged.connect(self._on_voice_state)
        # Still needed, and still a separate fact: the microphone line is about the DEVICE,
        # and the voice state is about what the assistant is doing with it.
        bridge.listeningChanged.connect(self._on_listening)
        # A SCREEN BUILT BEFORE THE BACKEND IS READY MUST RE-READ WHEN IT BECOMES READY.
        # Home is constructed and shown by `KayraWindow.__init__`, several seconds before the
        # session finishes booting, and an event-driven readout is never corrected for a value
        # that never changed — so without this the panels keep whatever they were painted with
        # during the boot window, indefinitely.
        bridge.bootFinished.connect(lambda ok, detail: self._sync_voice())
        bridge.bootFinished.connect(lambda ok, detail: self._sync_gesture())
        bridge.bootFinished.connect(lambda ok, detail: self._refresh_panels())
        bridge.userMessage.connect(self._on_user_message)
        bridge.assistantMessage.connect(self._on_assistant_message)
        # Hand gesture control arrives on its OWN signal, resolved by the gesture controller.
        # Nothing on this screen derives "is a hand visible?" from the camera state or the
        # other way round — they are three separate facts and they are rendered as three.
        bridge.gestureStateChanged.connect(self._on_gesture_state)

    # ──────────────────────────────────────────────────────────────────
    #                            COMPOSITION
    # ──────────────────────────────────────────────────────────────────

    def _build_columns(self):
        """
        Three columns, each carrying its share of the width, all full height.

        The stretch factors (3 : 4 : 3) give the orb the largest share without letting the
        outer columns collapse. They are RATIOS, not pixel positions: the page has no
        hardcoded geometry anywhere, so the same composition holds at 1040px and at 2560px
        and the panels simply get wider.
        """
        columns = QHBoxLayout()
        columns.setSpacing(Space.base)

        left = QVBoxLayout()
        left.setSpacing(Space.base)
        self.intelligence_panel = self._build_intelligence()
        self.interaction_panel = self._build_interaction()
        left.addWidget(self.intelligence_panel, 4)
        left.addWidget(self.interaction_panel, 6)

        right = QVBoxLayout()
        right.setSpacing(Space.base)
        self.system_panel = self._build_system()
        self.activity_panel = self._build_activity()
        right.addWidget(self.system_panel, 6)
        right.addWidget(self.activity_panel, 4)

        columns.addLayout(left, 3)
        columns.addLayout(self._build_hero(), 4)
        columns.addLayout(right, 3)

        # A minimum per column that three of them plus the gaps still fit inside
        # `min_window_width`. Below that the panels elide rather than the row overflowing.
        for panel in (self.intelligence_panel, self.interaction_panel,
                      self.system_panel, self.activity_panel):
            panel.setMinimumWidth(228)

        self.content.addLayout(columns, 1)

    # ── The centre column ──

    def _build_hero(self):
        """
        The orb, its state and one caption. Nothing else: this column is what the page is FOR.

        Vertically centred by stretches on both sides rather than pinned to the top, so the
        composition holds at any window height instead of leaving a growing gap underneath.
        """
        hero = QVBoxLayout()
        hero.setSpacing(Space.md)

        # ── THE IDENTITY BLOCK ──
        # The centre column used to open with a stretch, so the top third of the most
        # important column on the page was empty while the composition's whole weight sat in
        # the middle. This fills it WITHOUT repeating the sidebar's branding at volume: a
        # small mark, a hairline in the accent, and one line of what Kayra is — then the eye
        # is handed down to the orb, which remains the page's only focal point.
        #
        # The stretch RATIOS are what place it: 3 above, 3 between it and the orb, 4 below.
        # No pixel positions, so the block holds its place at any window height exactly as
        # the rest of this page does — and the slightly smaller share underneath is what
        # keeps the composition off the dock without a hardcoded offset.
        hero.addStretch(3)
        hero.addWidget(self._build_wordmark(), 0, Qt.AlignHCenter)
        hero.addStretch(3)

        self.orb = AssistantOrb(Size.orb_home, interactive=True)
        self.orb.setToolTip("Click to type to Kayra")
        hero.addWidget(self.orb, 0, Qt.AlignHCenter)
        hero.addSpacing(Space.base)

        self.prompt = _label(PROMPTS["STARTING"], "HeroLine")
        self.prompt.setAlignment(Qt.AlignCenter)
        hero.addWidget(self.prompt)

        self.state_caption = Caption("Bringing subsystems up")
        self.state_caption.setAlignment(Qt.AlignCenter)
        hero.addWidget(self.state_caption)

        hero.addStretch(4)
        self.orb.clicked.connect(self._focus_chat)
        return hero

    def _build_wordmark(self):
        """
        KAYRA, an accent hairline, and one restrained line of what it is.

        NOT A SECOND LOGO. The sidebar already carries the name at reading weight, so a
        large mark here would be duplication rather than composition. This one is 22px
        against the status line's 30px, sits well above it, and is the quietest thing on
        the column apart from the caption — its job is to give the empty upper area a
        reason to exist and to point downward.
        """
        block = QWidget()
        block.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        column = QVBoxLayout(block)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(Space.xs)

        mark = _label("KAYRA", "HomeWordmark")
        mark.setAlignment(Qt.AlignCenter)
        # The tracking is applied to the LAST letter too, so a centred wordmark reads as
        # sitting one tracking-step right of centre. Reclaiming that width is the difference
        # between "centred" and "looks centred".
        mark.setContentsMargins(int(Font.tracking_wordmark), 0, 0, 0)
        column.addWidget(mark)

        column.addWidget(_AccentRule(), 0, Qt.AlignHCenter)

        tagline = _label("Your intelligent desktop companion", "HomeTagline")
        tagline.setAlignment(Qt.AlignCenter)
        column.addWidget(tagline)

        return block

    # ── Left column, top ──

    def _build_intelligence(self):
        """
        WHERE THE THINKING HAPPENS. Four facts, every one read from a live subsystem.

        The tier pill is the headline because it is the one thing about a running assistant
        that a user cannot see from the outside and that changes what to expect: a local model
        answers with the network down and a cloud one does not.
        """
        panel = GlassPanel("Intelligence")
        self.intelligence_pill = StatusPill("—", "neutral")
        panel.add_header_widget(self.intelligence_pill)

        self.route_decision = _StatusLine("Decisions", "—")
        self.route_chat = _StatusLine("Conversation", "—")
        self.route_speech = _StatusLine("Speech out", "—")
        self.route_speech.value_label.setToolTip(_GPU_DETAIL_TOOLTIP)
        self.route_input = _StatusLine("Speech in", "—")
        self.route_memory = _StatusLine("Memory", "—")

        # TWO GROUPS, SPREAD, NOT FIVE ROWS STACKED AT THE TOP.
        #
        # Rows plus one trailing stretch is the default a layout gives you, and it is what
        # left a third of every panel as bare background — the same emptiness this redesign
        # exists to remove, arriving one level down. The stretches sit BETWEEN the groups
        # instead, so the panel is filled by its own content and the rule reads as a real
        # division: what MODEL is answering, above what DEVICE is carrying it.
        panel.body.addStretch(1)
        for row in (self.route_decision, self.route_chat):
            panel.body.addWidget(row)
        panel.body.addStretch(1)
        panel.body.addWidget(RowRule())
        panel.body.addStretch(1)
        for row in (self.route_speech, self.route_input, self.route_memory):
            panel.body.addWidget(row)
        panel.body.addStretch(1)
        return panel

    # ── Left column, bottom ──

    def _build_interaction(self):
        """
        THE INPUT DEVICES, and the camera's own picture.

        The preview is here rather than in its own card because "is the camera watching?" is
        an interaction question, and showing the answer as an IMAGE is more honest than any
        pill: a user can see for themselves what Kayra can see. It is fixed-HEIGHT and
        expanding-width so it letterboxes inside whatever the column gets — a fixed-width
        preview forces its panel to that width, which is what clipped the bottom strip off
        the right edge of this page once already.
        """
        panel = GlassPanel("Interaction")
        self.gesture_pill = StatusPill("Off", "neutral")
        panel.add_header_widget(self.gesture_pill)

        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        # THE PREVIEW ABSORBS THIS COLUMN'S SLACK, and it is the right thing to do it with:
        # it is the only element on the page whose value genuinely improves with size, and a
        # bigger picture of what the camera sees is more useful than a bigger gap under four
        # text rows. `setFixedHeight` in the component is replaced by a minimum plus an
        # expanding policy, so it grows into the panel instead of leaving a void beneath it.
        self.camera_preview = CameraPreview(self.bridge, QSize(320, 150))
        self.camera_preview.setMinimumHeight(110)
        # A CEILING, not an unlimited expand. The preview absorbs this column's slack, which
        # is right while the camera is ON — but when it is OFF the same policy produced a
        # 350px black rectangle dominating the panel, which is a lot of visual weight for a
        # device that is not running. Capped, the surplus becomes spacing above the status
        # rows instead, the way the Intelligence panel already distributes its own.
        self.camera_preview.setMaximumHeight(264)
        self.camera_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.camera_preview.setToolTip(
            "The camera Kayra is using for hand gestures. Nothing is recorded or sent "
            "anywhere.")
        preview_row.addWidget(self.camera_preview, 1)
        panel.body.addLayout(preview_row, 1)

        self.mic_line = _StatusLine("Microphone", "—")
        self.camera_line = _StatusLine("Camera", "Off")
        # HAND AND GESTURE ARE THEIR OWN LINE, SEPARATE FROM THE PILL. The pill says whether
        # the FEATURE is running; this says whether it can currently SEE anything and what it
        # is doing. Collapsing them would let "no hand in frame" render as "paused", which
        # tells a user who simply lowered their hand that they did something they did not do.
        self.gesture_line = _StatusLine("Gestures", "Off")
        self.presence_line = _StatusLine("Presence", "—")
        # THE RULE SITS WITH THE ROWS IT GROUPS, not under the preview. Placed directly
        # beneath the preview it was an orphaned hairline with the panel's slack underneath
        # it, which reads as a line that lost its content. Above the rows, the same slack
        # becomes the gap BETWEEN two groups — which is what it actually is.
        panel.body.addStretch(1)
        panel.body.addWidget(RowRule())
        for row in (self.mic_line, self.camera_line, self.gesture_line, self.presence_line):
            panel.body.addWidget(row)
        return panel

    # ── Right column, top ──

    def _build_system(self):
        """
        THE MACHINE. What it is, and what it is doing — including the graphics adapter.

        The GPU used to be its own card, which made a property of the hardware look like a
        subsystem of the assistant. It sits beside the processor now, where it belongs, and
        the whole graphics block hides itself on a machine with no adapter rather than
        showing three empty rows.
        """
        panel = GlassPanel("System")
        self.system_pill = StatusPill("Checking", "neutral")
        panel.add_header_widget(self.system_pill)

        # ONE identity line: the OS product with its real build, the processor's real
        # marketing name, and installed memory. Every value is MEASURED — see
        # `_sync_machine_identity` for why there is no fallback string naming a chip.
        # HIDDEN UNTIL IT HAS SOMETHING TRUE TO SAY. The profile is collected on a background
        # thread and is not ready for the first second of a session, and an em-dash sitting
        # alone under the panel title reads as a defect rather than as "still measuring" —
        # which is exactly the confusion `profile_if_ready()` exists to avoid on the value
        # side. Showing nothing is the honest render of nothing.
        self.machine_line = _ElidedCaption("")
        self.machine_line.setVisible(False)
        panel.body.addWidget(self.machine_line)

        panel.body.addStretch(1)
        self.cpu_meter = Meter("Processor")
        self.ram_meter = Meter("Memory")
        panel.body.addWidget(self.cpu_meter)
        panel.body.addWidget(self.ram_meter)
        panel.body.addStretch(1)

        self.graphics_rule = RowRule()
        panel.body.addWidget(self.graphics_rule)
        self.gpu_name = _ElidedCaption("Looking for a GPU…")
        panel.body.addWidget(self.gpu_name)
        self.gpu_meter = Meter("Utilization")
        self.vram_meter = Meter("VRAM")
        panel.body.addWidget(self.gpu_meter)
        panel.body.addWidget(self.vram_meter)

        # Permanent child, shown and hidden — never created and destroyed. `takeAt` removes an
        # item from the LAYOUT without hiding the widget, so a rebuilt empty state ends up
        # painted underneath the real content at its stale geometry.
        self._gpu_empty = EmptyState("No GPU detected",
                                     "Speech synthesis runs on the processor.")
        panel.body.addWidget(self._gpu_empty)
        self._gpu_empty.setVisible(False)

        panel.body.addStretch(1)
        self.footprint = _ElidedCaption("")
        panel.body.addWidget(self.footprint)
        return panel

    # ── Right column, bottom ──

    def _build_activity(self):
        panel = GlassPanel("Activity")
        self.activity_pill = StatusPill("—", "neutral")
        panel.add_header_widget(self.activity_pill)

        self.activity_body = QVBoxLayout()
        self.activity_body.setSpacing(0)
        panel.body.addLayout(self.activity_body)

        # Permanent child OUTSIDE the rebuilt layout, toggled with `setVisible`. Same rule as
        # the GPU empty state above, and for the same reason: `takeAt` removes an item from
        # the LAYOUT without hiding the widget, so a rebuilt empty state ends up painted
        # underneath the real content at its stale geometry.
        #
        # ABOVE the stretch, not below it. It used to be added after, which put "No activity
        # yet" at the very bottom of a tall empty panel with the space it was explaining
        # sitting above it.
        self._activity_empty = EmptyState(
            "No activity yet", "Ask Kayra to do something and it will appear here.")
        panel.body.addWidget(self._activity_empty)
        panel.body.addStretch(1)
        return panel

    # ──────────────────────────────────────────────────────────────────
    #                     STATE THE WINDOW HANDS DOWN
    # ──────────────────────────────────────────────────────────────────

    def mark_shutting_down(self):
        """
        Called by the window once teardown has been requested.

        The camera stops being asked for frames immediately, so this page cannot keep
        painting a live image of a device that is being released. The caption is NOT written
        here: `request_shutdown` moves the voice state to STOPPING, which is an absorbing
        state, so the machine paints it and nothing can put it back.
        """
        self._shutting_down = True
        self.camera_preview.set_live(False)
        self._timer.stop()

    def _focus_chat(self):
        window = self.window()
        if hasattr(window, "navigate_to"):
            window.navigate_to("chat")

    # ──────────────────────────────────────────────────────────────────
    #                              VOICE
    # ──────────────────────────────────────────────────────────────────

    def _on_voice_state(self, state, text, detail, revision):
        """
        Renders the ONE resolved voice state. Infers nothing.

        STALE CALLBACKS ARE DROPPED. Qt delivers queued signals in order, but this slot is
        also reached from `on_show()` — a synchronous read taken when the screen becomes
        visible — and that read can be overtaken by a transition already in the event queue.
        Comparing revisions makes "the newest state wins" true regardless of arrival order.
        """
        if revision <= self._voice_revision:
            return
        self._voice_revision = revision
        self._voice_state = state

        from kayra.core.voice_state import ORB_STATE, ORB_AMPLITUDE
        self.orb.set_state(ORB_STATE.get(state, "IDLE"), ORB_AMPLITUDE.get(state))
        self.prompt.setText(text)
        self.state_caption.setText(detail)

    def _on_listening(self, listening, known=True):
        """
        Updates the microphone READOUT. There is no control on this page to update.

        `known=False` means the backend has not booted, so there is no answer yet — and the
        line says exactly that instead of naming a state nobody measured. Reporting an
        absence of information as a negative fact is the defect this rule exists to prevent.
        """
        if not known:
            self.mic_line.set_value("Starting…")
            return
        self.mic_line.set_value("Open" if listening else "Paused")

    def _sync_voice(self):
        """
        Paints from the current voice state, for a screen that has just become visible.

        ONE read for the whole voice picture. Reading the caption from the snapshot and the
        microphone from a separate `listening_enabled()` call is how the two came to disagree
        in the first place.
        """
        snapshot = self.bridge.voice_runtime_state() or {}
        if not snapshot:
            return
        self._on_listening(snapshot.get("listening", True),
                           known=bool(snapshot.get("listening_known", True)))
        self._on_voice_state(snapshot.get("state", "OFFLINE"), snapshot.get("text", ""),
                             snapshot.get("detail", ""), int(snapshot.get("revision", 0)))

    # ──────────────────────────────────────────────────────────────────
    #                        CAMERA AND GESTURES
    # ──────────────────────────────────────────────────────────────────

    def _sync_gesture(self):
        """Takes ONE synchronous read of the whole gesture picture and paints from it."""
        self._on_gesture_state(self.bridge.gesture_status() or {})

    def _on_gesture_state(self, status):
        """
        Renders the gesture status. Infers nothing, and never writes back.

        THREE INDEPENDENT FACTS, READ AS THREE. `paused` is something the user DID; `hand` is
        whether anything is in frame; `camera` is whether the device is open. None is derived
        from another, and none is derived from the runtime state string.
        """
        status = dict(status or {})
        camera_state = str(status.get("camera", "OFF"))
        camera_on = camera_state not in ("OFF", "ERROR")
        gesture_on = bool(status.get("gesture_enabled"))
        runtime = str(status.get("state", "OFF"))
        paused = bool(status.get("paused"))
        hand_seen = bool(status.get("hand"))
        error = str(status.get("error", "") or "")

        self.camera_line.set_value(
            "Error" if camera_state == "ERROR"
            else "Starting…" if camera_state == "STARTING"
            else "On" if camera_on else "Off")

        if runtime == "ERROR" or camera_state == "ERROR":
            self.gesture_pill.set_status("Error", "danger")
            self.gesture_line.set_value(error or "The camera is unavailable.")
        elif gesture_on:
            # PAUSED comes from `paused`, never from "there is no hand". The runtime reports
            # ACTIVE_NO_HAND for an empty frame precisely so this line cannot confuse them.
            self.gesture_pill.set_status("Paused" if paused else "Active",
                                         "warning" if paused else "success")
            if paused:
                self.gesture_line.set_value("Paused — open your hand to resume")
            elif hand_seen:
                self.gesture_line.set_value(f"Hand detected · {status.get('gesture') or '—'}")
            else:
                self.gesture_line.set_value("No hand in frame")
        elif camera_on:
            self.gesture_pill.set_status("Camera on", "neutral")
            self.gesture_line.set_value("Off — preview only")
        else:
            self.gesture_pill.set_status("Off", "neutral")
            self.gesture_line.set_value("Off")

        # The camera never keeps painting once teardown has begun.
        self.camera_preview.set_live(camera_on and not self._shutting_down)
        self.camera_preview.set_message(
            error or ("Starting…" if camera_state == "STARTING" else "Camera off"))

    # ──────────────────────────────────────────────────────────────────
    #                             ACTIVITY
    # ──────────────────────────────────────────────────────────────────

    def _on_user_message(self, text, source):
        self._push_activity("You", text, voice=(source == "voice"))

    def _on_assistant_message(self, text):
        # Only the opening of a reply becomes an activity line; the rest is in Chat.
        self._push_activity("Kayra", text)

    def _push_activity(self, who, text, voice=False):
        """
        Adds one glanceable line.

        The row carries the speaker as its own element rather than baking it into the string,
        so the eye can skip down the left edge and the text column stays aligned.
        """
        self._activity_empty.setVisible(False)
        self.activity_body.insertWidget(0, _ActivityLine(who, text, voice))

        # Bounded: the panel is a glance, not a log. Activity holds the full history.
        while self.activity_body.count() > self.ACTIVITY_LINES:
            item = self.activity_body.takeAt(self.activity_body.count() - 1)
            if item.widget():
                item.widget().deleteLater()

    # ──────────────────────────────────────────────────────────────────
    #                             REFRESH
    # ──────────────────────────────────────────────────────────────────

    def _refresh_panels(self):
        from kayra.core.system_profile import live_metrics, human_bytes

        metrics = live_metrics()
        self._sync_machine_identity()

        self.cpu_meter.set_value(metrics["cpu_percent"])
        self.ram_meter.set_value(
            metrics["ram_percent"],
            f"{human_bytes(metrics['ram_used'])} / {human_bytes(metrics['ram_total'])}")

        processes = metrics["kayra_processes"]
        self.footprint.set_full_text(
            f"Kayra is using {human_bytes(metrics['kayra_memory'])} across "
            f"{processes} process{'es' if processes != 1 else ''}")

        load = max(metrics["cpu_percent"], metrics["ram_percent"])
        self.system_pill.set_status(
            *(("Under load", "warning") if load >= 90 else ("Healthy", "success")))

        count = len(self.bridge.recent_automation(50))
        self.activity_pill.set_status(
            f"{count} action{'s' if count != 1 else ''}", "neutral")

        self._refresh_intelligence()
        self._refresh_graphics()
        self._refresh_presence()

    def _refresh_intelligence(self):
        """
        Where the thinking is happening, read from the live subsystems.

        NOTHING HERE COMES FROM `.env`. A machine with cloud keys configured and a local
        server running is a LOCAL machine, and a panel that recited the configuration would
        name a provider that is answering nothing.
        """
        status = self.bridge.intelligence_status() or {}
        if not status:
            self.intelligence_pill.set_status("Starting", "neutral")
        else:
            tier = status.get("tier") or "—"
            # Local is the healthier outcome for a desktop assistant — it works with the
            # network down — so it takes the success tone and cloud stays neutral. Neither is
            # a warning: both are correct configurations.
            self.intelligence_pill.set_status(
                tier, "success" if tier == "Local" else "accent")
            self.route_decision.set_value(_strip_route(status.get("decision")))
            self.route_chat.set_value(_strip_route(status.get("chat")))

        # Speech OUT: the provider the live ONNX session is actually on, plus the requested
        # mode. The two disagree whenever a GPU is asked for and cannot be initialised, which
        # is the normal case on a stock install — so they are shown together rather than the
        # mode alone, which would claim acceleration that is not happening.
        provider = self.bridge.tts_provider()
        report = self.bridge.tts_device_report() or {}
        mode = str(report.get("mode") or "").upper()
        if provider:
            self.route_speech.set_value(
                f"{provider}" + (f"  ·  {mode}" if mode and mode != "AUTO" else ""))
        else:
            self.route_speech.set_value("not running")

        # Speech IN: requested vs active, and they stay separate fields. A screen that showed
        # the dropdown's value while another browser held the microphone is the exact lie
        # `stt_backend` exists to prevent.
        backend = self.bridge.stt_backend_state() or {}
        active = backend.get("active_label") or backend.get("active") or ""
        requested = backend.get("requested_label") or backend.get("requested") or ""
        if active and requested and not backend.get("matches", True):
            self.route_input.set_value(f"{active}  (wanted {requested})")
        else:
            self.route_input.set_value(str(active or requested or "—"))

        memories = self.bridge.conversation_memory()
        self.route_memory.set_value(
            f"{len(memories)} stored" if memories is not None else "—")

    def _sync_machine_identity(self):
        """
        The System panel's identity line: OS product, build, processor, installed memory.

        EVERY VALUE IS MEASURED. There is no fallback string naming a version, a chip or a
        capacity — a machine whose OS build cannot be read shows the product without a build.
        Displaying a plausible default here would be worse than displaying less: a user
        checking what Kayra thinks their machine is has no way to tell an assumption from a
        reading.
        """
        from kayra.core.system_profile import (profile_if_ready, warm_profile,
                                               human_bytes, os_summary)

        # NON-BLOCKING. This runs on the GUI thread every tick and the first collection costs
        # ~150ms (audio device enumeration). Asking for it only once it is ready keeps that
        # off the paint path; until then the line is simply not written, which is correct —
        # there is nothing true to put in it yet.
        profile = profile_if_ready()
        if profile is None:
            warm_profile()
            return
        try:
            product, version = os_summary()
        except Exception:
            return

        parts = []
        if product:
            build = profile.get("os_build")
            parts.append(f"{product}  ·  Build {build}" if build else product)
        elif version:
            parts.append(version)
        cpu = (profile.get("cpu_name") or "").strip()
        if cpu:
            parts.append(cpu)
        if profile.get("ram_total"):
            parts.append(f"{human_bytes(profile['ram_total'])} RAM")

        self.machine_line.set_full_text(
            "  ·  ".join(parts) or "Machine details unavailable")
        self.machine_line.setVisible(True)

    def _refresh_graphics(self):
        """
        The graphics block inside the System panel.

        THREE SOURCES, ANSWERING THREE DIFFERENT QUESTIONS, and none substitutes for another:

          * `graphics_profile()` — WHAT hardware is in this machine. Registry-read, every
            vendor, cached; the answer on an AMD, Intel or NVIDIA machine alike.
          * `gpu_metrics()`      — what that hardware is DOING. `nvidia-smi`, sampled on a
            slow shared timer and read from a cache, so this never blocks the GUI thread.
            NVIDIA-only, because no other vendor exposes it without a new dependency.
          * `tts_provider()`     — what SPEECH is on. Shown in the Intelligence panel, not
            here: it is a fact about what is answering, not about the machine.

        A MISSING MEASUREMENT IS NEVER DRAWN AS A ZERO. An unreported utilization is captioned
        "not reported", because a 0% bar reads as an idle GPU rather than as one whose vendor
        does not tell us.
        """
        metrics = self.bridge.gpu_metrics()
        hardware = self.bridge.graphics_profile()

        graphics_widgets = (self.gpu_name, self.gpu_meter, self.vram_meter,
                            self.graphics_rule)

        # The ONLY case that shows the empty state is genuinely having no adapter at all. An
        # AMD or Intel machine has a real GPU and used to land here, because the only source
        # of GPU facts was NVIDIA telemetry — so the page told the owner of a Radeon that they
        # had no GPU.
        if not hardware and not metrics:
            waiting = self.bridge.gpu_telemetry_pending()
            self._gpu_empty.set_message(
                "Reading GPU…" if waiting else "No GPU detected",
                "Fetching statistics." if waiting
                else "Speech synthesis runs on the processor.")
            self._gpu_empty.setVisible(True)
            for widget in graphics_widgets:
                widget.setVisible(False)
            return

        self._gpu_empty.setVisible(False)
        for widget in graphics_widgets:
            widget.setVisible(True)

        # Telemetry wins when present, because it names the card the driver is actually
        # reporting on; the static profile is the answer for every other vendor. Both are
        # measured, neither is a literal.
        name = (metrics or {}).get("name") or hardware.get("name") or "Graphics"
        vendor = hardware.get("vendor") or ""
        if vendor and not (metrics or {}).get("temperature_c"):
            # Said once, plainly, rather than leaving a flat bar to be interpreted.
            name = f"{name}  ·  {vendor} telemetry unavailable"
        temperature = (metrics or {}).get("temperature_c")
        if temperature is not None:
            name = f"{name}  ·  {temperature:.0f}°C"
        self.gpu_name.set_full_text(name)

        utilization = (metrics or {}).get("utilization")
        if utilization is not None:
            self.gpu_meter.set_value(utilization)
        else:
            self.gpu_meter.set_value(0.0, "not reported")

        used = (metrics or {}).get("memory_used_mb")
        total = (metrics or {}).get("memory_total_mb")
        if used is not None and total:
            self.vram_meter.set_value((metrics or {}).get("memory_percent") or 0.0,
                                      f"{used / 1024:.1f} / {total / 1024:.1f} GiB")
        elif hardware.get("vram_total"):
            # Capacity without live usage: show the capacity, and do not draw a filled bar
            # for a number nobody measured.
            self.vram_meter.set_value(
                0.0, f"{hardware['vram_total'] / 1024 ** 3:.1f} GiB installed")
        elif hardware.get("integrated"):
            self.vram_meter.set_value(0.0, "shared with system memory")
        else:
            self.vram_meter.set_value(0.0, "unknown")

    def _refresh_presence(self):
        """
        The proactive layer, as ONE line rather than its own card.

        It is a subsystem whose whole job is to stay quiet, and it had four rows of its own on
        the old page — more prominence than a service the user is meant not to notice earns.
        What matters is whether it is on and when it could next speak.
        """
        status = self.bridge.presence_status()
        if not status:
            self.presence_line.set_value("not running")
            return
        if not bool(status.get("enabled")):
            self.presence_line.set_value("Off — Kayra speaks only when asked")
            return

        seconds = int(status.get("next_eligible_seconds") or 0)
        if seconds <= 0:
            when = "ready"
        elif seconds < 90:
            when = f"in {seconds}s"
        else:
            when = f"in {seconds // 60}m"
        spoken = int(status.get("spoken_today") or 0)
        budget = int(status.get("daily_budget") or 0)
        self.presence_line.set_value(
            f"On · next {when} · {spoken}" + (f"/{budget} today" if budget else " today"))

    # ──────────────────────────────────────────────────────────────────
    #                            LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    # The orb's share of the centre column, and its bounds. The fractions are deliberately
    # conservative: the orb has to leave room beneath it for the state line and its caption,
    # and an orb that filled its column would crowd both. The ceiling exists because past
    # roughly 340px the segmented ring stops reading as an instrument and starts reading as
    # a decoration, which is the one thing this visual must not become.
    ORB_WIDTH_SHARE = 0.48
    ORB_HEIGHT_SHARE = 0.42
    ORB_MIN = 168
    ORB_MAX = 320

    def _resize_orb(self):
        """
        Sizes the orb to the space the centre column actually has.

        Derived from the page's own width and the SAME stretch ratio the columns are built
        with, so there is no hardcoded pixel geometry and the two cannot drift: change the
        ratio in `_build_columns` and this follows. Height is bounded too, because a short
        wide window has plenty of width and no room to put it.
        """
        left, top, right, bottom = self.content.getContentsMargins()
        usable = max(1, self.width() - left - right - 2 * Space.base)
        centre = usable * 4.0 / 10.0                 # the hero column's stretch share
        vertical = max(1, self.height() - top - bottom)
        self.orb.set_diameter(max(self.ORB_MIN,
                                  min(self.ORB_MAX,
                                      centre * self.ORB_WIDTH_SHARE,
                                      vertical * self.ORB_HEIGHT_SHARE)))

    def resizeEvent(self, event):
        # The captions re-elide THEMSELVES — see `_ElidedCaption`. Nothing here has to
        # remember to, which is what makes the timing bug unreachable rather than fixed.
        super().resizeEvent(event)
        self._resize_orb()

    def on_show(self):
        # THE BLOOM FOLLOWS THE ORB. Home is the one screen with a single focal point, and
        # putting the backdrop's warm glow behind it is what makes the orb read as lit rather
        # than as a drawing on a dark page. Normalised coordinates, so it survives every
        # resize without this screen recomputing a pixel position.
        window = self.window()
        backdrop = getattr(window, "backdrop", None)
        if backdrop is not None:
            backdrop.set_focus(0.5, 0.42, 1.0)
        self._resize_orb()

        self._refresh_panels()
        # Read the live state on entry: it may have changed while this screen was hidden.
        # Both syncs route through the same slots the signals use, so the revision guard
        # applies to a synchronous paint exactly as it does to an asynchronous one.
        self._sync_voice()
        self._sync_gesture()
        if not self._shutting_down:
            self._timer.start()

    def on_hide(self):
        # Stopping the timer here is what keeps a hidden screen genuinely free. The orb and
        # the camera preview stop themselves through their own hideEvent.
        self._timer.stop()


def _strip_route(text):
    """
    Turns the engine's own diagnostic string into a value for a labelled row.

    `dmm_status` reads "Decision routing: Local (qwen)". The row is already labelled
    "Decisions", so repeating that in the value would print the word twice — the same
    duplication the DMM's retry lines had to have removed when `logbus` was already stamping
    the turn number.
    """
    text = str(text or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    return text or "—"
