# ┌────────────────────────────────────────────────────────────────────────┐
# │                              home.py                                   │
# │                    The Assistant's Primary Surface                     │
# └────────────────────────────────────────────────────────────────────────┘
"""
Home answers one question before any other: is Kayra there, and what is it doing?

LAYOUT REASONING
----------------
The orb is centred and given real space. Every other dashboard instinct — fill the viewport
with cards, show a metric for everything measurable — was rejected here, because a page that
opens with twelve panels says "control panel", and the product is an assistant. The status
strip and the two supporting panels sit BELOW the fold-line of attention, so they inform
without competing.

Nothing on this screen polls. The state comes from the runtime's `state_changed` event, and
the two panels refresh only while the screen is visible, on a slow interval, from data the
backend already keeps (the automation audit ring and psutil).
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

from kayra.ui.theme import Color, Font, Space, Size, Motion, STATE_LABELS
from kayra.ui.components.orb import AssistantOrb
from kayra.ui.components.primitives import (
    Card, Caption, Secondary, StatusPill, AccentButton, GhostButton, OutlineButton,
    DangerButton, Meter, EmptyState, SectionLabel, ListRow, _label,
)
from kayra.ui.views.base import View


# The GPU statistics and the speech device are DIFFERENT FACTS, and this tooltip is where that
# is said out loud. A machine can have a perfectly good GPU while speech runs on the CPU.
_GPU_DETAIL_TOOLTIP = (
    "The provider speech synthesis is actually using. The GPU above is the physical device, "
    "which exists whether or not Kayra is using it.")


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


class _ActivityLine(ListRow):
    """
    One line of recent activity: who spoke, and what was said.

    Elided rather than wrapped. This panel is a fixed-height glance — a wrapping line would
    resize the bottom strip every time a long reply arrived, and the anchor would jump.
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


class HomeView(View):
    title = ""          # Home is deliberately titleless: the orb IS the heading.

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)
        self.content.setContentsMargins(Space.xl, Space.md, Space.xl, Space.xl)

        # VERTICAL RHYTHM, AND THE BUG IT FIXES.
        # This page used to lay the hero out at the top, the panels under it, and one stretch
        # at the bottom — so on a 900px window roughly 280px of pure background sat below the
        # cards, which does not read as "breathing room", it reads as a screen that failed to
        # finish loading. The stretches now sit AROUND the content: the hero floats in the
        # upper body of the page and the panels anchor the bottom, at every window height.
        # The ratio is deliberately top-light (3:4) so the orb lands slightly above centre,
        # where the eye already is.
        self.content.addStretch(3)
        self._build_hero()
        self.content.addStretch(4)
        self._build_panels()

        self._timer = QTimer(self)
        self._timer.setInterval(Motion.metrics_interval * 2)
        self._timer.timeout.connect(self._refresh_panels)

        # THE VOICE CAPTION HAS ONE SOURCE. This screen used to compose it from a cached
        # `_state` and a cached `_listening`, which is how a user talking to an open
        # microphone could be told "Listening paused": neither cached value was wrong, and
        # together they did not describe the situation. `voiceStateChanged` carries the
        # resolved answer and its revision; nothing here re-derives it.
        self._voice_revision = -1
        # Latched by `_request_shutdown`. Once teardown has begun the controls stay disabled,
        # so a late boot/voice re-sync cannot hand the user a button that re-enters a shutdown
        # already in progress — the same reason the shutdown button disables itself.
        self._shutting_down = False
        bridge.voiceStateChanged.connect(self._on_voice_state)
        # Still needed, and still separate facts: the button label is about what the user can
        # DO, and the orb's colour follows the assistant's work as well as the microphone.
        bridge.listeningChanged.connect(self._on_listening)
        # A SCREEN BUILT BEFORE THE BACKEND IS READY MUST RE-READ WHEN IT BECOMES READY.
        # Home is constructed and shown by `KayraWindow.__init__`, several seconds before the
        # session finishes booting, and `listening_changed` never fires for a value that
        # never changes — so without this the control keeps whatever it was painted with
        # during the boot window. That is the whole defect: the button said "Start listening"
        # beside an orb that was listening, and only two real toggles fixed it.
        bridge.bootFinished.connect(lambda ok, detail: self._sync_voice())
        bridge.userMessage.connect(self._on_user_message)
        bridge.assistantMessage.connect(self._on_assistant_message)

    # ──────────────────────────────────────────────────────────────────
    #                              HERO
    # ──────────────────────────────────────────────────────────────────

    def _build_hero(self):
        hero = QWidget()
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Space.md)
        layout.setAlignment(Qt.AlignHCenter)

        self.orb = AssistantOrb(Size.orb_home, interactive=True)
        self.orb.setToolTip("Click to type to Kayra")
        layout.addWidget(self.orb, alignment=Qt.AlignHCenter)
        layout.addSpacing(Space.sm)

        self.prompt = _label(PROMPTS["STARTING"], "HeroLine")
        self.prompt.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.prompt)

        self.state_caption = Caption("Bringing subsystems up")
        self.state_caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.state_caption)

        # Controls sit under the orb: the two things a voice user actually needs at hand.
        controls = QHBoxLayout()
        controls.setSpacing(Space.sm)
        controls.setAlignment(Qt.AlignHCenter)

        self.talk_button = AccentButton("Talk to Kayra")
        self.talk_button.setMinimumWidth(168)

        # THE SECOND CONTROL IS THE MICROPHONE, NOT A SHUTDOWN.
        # It used to be a ghost "Stop", which is the same word the interrupt uses and one
        # letter away from how people read "quit" — three unrelated ideas competing for one
        # button. This one does exactly one thing: it closes and opens the microphone. Kayra
        # keeps running, keeps speaking, keeps automating. Quitting is the tray's job.
        self.listen_button = OutlineButton("Pause listening", icon="pause")
        self.listen_button.setMinimumWidth(168)

        controls.addWidget(self.talk_button)
        controls.addWidget(self.listen_button)
        layout.addSpacing(Space.sm)
        layout.addLayout(controls)

        # THE THIRD CONTROL IS ON ITS OWN ROW, AND THAT IS THE POINT.
        # Shutdown ends the process. Putting it in the row above, at the same size as the two
        # controls a user presses many times a day, would make the destructive action a
        # neighbour of the routine ones — and "Pause listening" / "Shut down Kayra" are
        # precisely the pair that must never be hit by mistake. It is separated, smaller, and
        # in the danger tone, so it is unmistakable when looked for and unremarkable when not.
        quit_row = QHBoxLayout()
        quit_row.setAlignment(Qt.AlignHCenter)
        self.shutdown_button = DangerButton("Shut down Kayra")
        self.shutdown_button.setToolTip(
            "Close the microphone, stop every service, and end Kayra.")
        quit_row.addWidget(self.shutdown_button)
        layout.addSpacing(Space.xs)
        layout.addLayout(quit_row)

        self.content.addWidget(hero)

        self.orb.clicked.connect(self._focus_chat)
        self.talk_button.clicked.connect(self._focus_chat)
        self.listen_button.clicked.connect(self._toggle_listening)
        self.shutdown_button.clicked.connect(self._request_shutdown)

    def _request_shutdown(self):
        """
        Confirms, then hands over to the ONE authoritative shutdown path.

        This does not stop threads, close browsers or silence audio itself. It calls exactly
        what the spoken "turn off Kayra" and the tray's Quit call — `app.request_shutdown`,
        through the bridge — because a second teardown living in the UI would inevitably drift
        out of step with the ordering the backend depends on.

        The button is disabled BEFORE the backend call and never re-enabled. Teardown takes a
        couple of seconds (the browser session has nine processes to reap), and a second click
        during that window used to be the easiest way to re-enter a shutdown that was already
        half-done.
        """
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Shut down Kayra")
        box.setText("Shut down Kayra?")
        box.setInformativeText(
            "Voice input, speech and every background service stop, and the window closes. "
            "This does not shut down your computer.")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return

        self._shutting_down = True
        self.shutdown_button.setEnabled(False)
        self.shutdown_button.setText("Shutting down\u2026")
        self.talk_button.setEnabled(False)
        self.listen_button.setEnabled(False)
        # The caption is not written here. `request_shutdown` moves the voice state to
        # STOPPING, which is an ABSORBING state, so the machine paints it and nothing —
        # including a late VAD sample from the control watcher — can put it back.
        self.bridge.shutdown(hard=True)

    def _toggle_listening(self):
        # Read the runtime rather than a local flag: the state may have been changed by the
        # spoken "stop listening", by the tray, or by the other window.
        self.bridge.set_listening(not self.bridge.listening_enabled())

    def _focus_chat(self):
        window = self.window()
        if hasattr(window, "navigate_to"):
            window.navigate_to("chat")

    # ──────────────────────────────────────────────────────────────────
    #                             PANELS
    # ──────────────────────────────────────────────────────────────────

    def _build_panels(self):
        row = QHBoxLayout()
        row.setSpacing(Space.base)

        # ── Recent activity ──
        self.activity_card = Card("Recent activity")
        self.activity_pill = StatusPill("—", "neutral")
        self.activity_card.add_header_widget(self.activity_pill)
        self.activity_body = QVBoxLayout()
        self.activity_body.setSpacing(0)
        self.activity_card.body.addLayout(self.activity_body)
        self.activity_card.body.addStretch(1)
        self._activity_empty = EmptyState("No activity yet",
                                          "Ask Kayra to do something and it will appear here.")
        self.activity_body.addWidget(self._activity_empty)

        # ── System readiness ──
        self.system_card = Card("System")
        self.system_pill = StatusPill("Checking", "neutral")
        self.system_card.add_header_widget(self.system_pill)
        self.cpu_meter = Meter("Processor")
        self.ram_meter = Meter("Memory")
        self.system_card.body.addWidget(self.cpu_meter)
        self.system_card.body.addWidget(self.ram_meter)
        self.footprint = Caption("—")
        self.system_card.body.addWidget(self.footprint)

        # ── Graphics ──
        # A THIRD card rather than more rows in "System", for two reasons. The System card is
        # a fixed-height glance and two more meters would overflow it; and the GPU is a
        # different subject — it is present or it is not, and on a machine without one this
        # whole card is hidden rather than showing four empty rows.
        self.gpu_card = Card("Graphics")
        # The header pill carries the thing that must never disagree with Settings: which
        # device SPEECH is actually running on. Not "is there a GPU" — "is Kayra using it".
        self.gpu_pill = StatusPill("—", "neutral")
        self.gpu_card.add_header_widget(self.gpu_pill)

        self.gpu_name = Caption("Looking for a GPU…")
        self.gpu_name.setWordWrap(False)
        self.gpu_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.gpu_card.body.addWidget(self.gpu_name)

        self.gpu_meter = Meter("Utilization")
        self.vram_meter = Meter("VRAM")
        self.gpu_card.body.addWidget(self.gpu_meter)
        self.gpu_card.body.addWidget(self.vram_meter)

        self.gpu_detail = Caption("—")
        # One line, elided. The card is a fixed-height glance, so a detail line that wrapped to
        # two rows on a long provider name would push content past the bottom edge.
        self.gpu_detail.setWordWrap(False)
        self.gpu_detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.gpu_card.body.addWidget(self.gpu_detail)

        # Permanent child, shown and hidden — never created and destroyed. A `takeAt` removes
        # an item from the LAYOUT without hiding the widget, so a rebuilt empty state ends up
        # painted underneath the real content at its stale geometry. That bug has been fixed
        # once in Activity already; this follows the same rule.
        self._gpu_empty = EmptyState("No GPU detected",
                                     "Speech synthesis runs on the processor.")
        self.gpu_card.body.addWidget(self._gpu_empty)
        self._gpu_empty.setVisible(False)

        # ── Proactive presence ──
        # The one screen where a subsystem that is meant to stay quiet can be seen working.
        # It carries three facts and no telemetry for decoration: whether it is on, what it
        # last observed, and when it could next speak. Everything here is already computed —
        # `presence_status()` is a dict read off the running service, with no I/O behind it.
        self.presence_card = Card("Presence")
        self.presence_pill = StatusPill("—", "neutral")
        self.presence_card.add_header_widget(self.presence_pill)
        self.presence_last = Caption("—")
        self.presence_next = Caption("—")
        self.presence_budget = Caption("—")
        for caption in (self.presence_last, self.presence_next, self.presence_budget):
            # Elided, never wrapped, and never allowed to set the card's width — the same
            # rule the System card's footprint line had to learn. A non-wrapping QLabel
            # reports its full text width as its minimum, which is what clipped the last
            # card off the right edge of this strip once already.
            caption.setWordWrap(False)
            caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.presence_card.body.addWidget(caption)
        self._presence_empty = EmptyState("Presence is off",
                                          "Kayra will only speak when you ask.")
        self.presence_card.body.addWidget(self._presence_empty)
        self._presence_empty.setVisible(False)

        # A fixed height keeps the bottom strip from resizing every time a line of activity
        # arrives — an anchor that jumps whenever content changes is not an anchor.
        for card in (self.activity_card, self.system_card, self.gpu_card, self.presence_card):
            card.setFixedHeight(206)

        self.system_card.body.addStretch(1)
        self.gpu_card.body.addStretch(1)
        self.presence_card.body.addStretch(1)

        row.addWidget(self.activity_card, 3)
        row.addWidget(self.system_card, 2)
        row.addWidget(self.gpu_card, 2)
        row.addWidget(self.presence_card, 2)
        self.content.addLayout(row)

    # ──────────────────────────────────────────────────────────────────
    #                             UPDATES
    # ──────────────────────────────────────────────────────────────────

    def _on_voice_state(self, state, text, detail, revision):
        """
        Renders the ONE resolved voice state. Infers nothing.

        STALE CALLBACKS ARE DROPPED. Qt delivers queued signals in order, but this slot is
        also reached from `on_show()` — a synchronous read taken when the screen becomes
        visible — and that read can be overtaken by a transition already in the event queue.
        Comparing revisions makes "the newest state wins" true regardless of arrival order,
        rather than "whichever call happened last wins", which is the ordering assumption
        that produced a stale caption in the first place.
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
        Updates the CONTROL, not the caption.

        The caption is the voice state machine's to write. This is only about what the button
        offers to do next — a separate fact, and the reason `listeningChanged` is still
        connected: the machine resolves what is happening, and the button says what the user
        can change about it.

        `known=False` means the backend has not booted, so there is no answer yet. The button
        is disabled rather than guessed at, which is also the truth about what it can DO:
        `set_listening` returns False before the session exists, so an enabled button there
        would be a control that silently does nothing.
        """
        listening = bool(listening)
        self.listen_button.set_label("Pause listening" if listening else "Start listening")
        self.listen_button.set_icon("pause" if listening else "mic")
        self.listen_button.setToolTip(
            "Kayra is still starting." if not known
            else "Close the microphone. Kayra keeps running." if listening
            else "Open the microphone again.")
        if not self._shutting_down:
            self.listen_button.setEnabled(bool(known))
        self._listening = listening

    def _sync_voice(self):
        """
        Paints from the current voice state, for a screen that has just become visible.

        A screen showing up mid-session has missed every transition so far, so it takes one
        synchronous read — and routes it through `_on_voice_state`, so the revision guard
        applies to it exactly as it does to a signal.
        """
        snapshot = self.bridge.voice_runtime_state() or {}
        if not snapshot:
            return
        # ONE read for the whole voice picture, control included. Reading the caption from
        # the snapshot and the button from a separate `listening_enabled()` call is how the
        # two came to disagree in the first place.
        self._on_listening(snapshot.get("listening", True),
                           known=bool(snapshot.get("listening_known", True)))
        self._on_voice_state(snapshot.get("state", "OFFLINE"), snapshot.get("text", ""),
                             snapshot.get("detail", ""), int(snapshot.get("revision", 0)))

    def _on_user_message(self, text, source):
        self._push_activity("You", text, voice=(source == "voice"))

    def _on_assistant_message(self, text):
        # Only the opening of a reply becomes an activity line; the rest is in Chat.
        self._push_activity("Kayra", text)

    def _push_activity(self, who, text, voice=False):
        """
        Adds one glanceable line.

        `"You: open youtube"` was what this used to render — a log line, with the speaker
        baked into the string and no way to style or align the two parts differently. The row
        now carries the speaker as its own element, so the eye can skip down the left edge and
        the text column stays aligned.
        """
        if self._activity_empty is not None:
            self._activity_empty.setParent(None)
            self._activity_empty.deleteLater()
            self._activity_empty = None

        self.activity_body.insertWidget(0, _ActivityLine(who, text, voice))

        # Bounded: the panel is a glance, not a log. Activity holds the full history.
        while self.activity_body.count() > 4:
            item = self.activity_body.takeAt(self.activity_body.count() - 1)
            if item.widget():
                item.widget().deleteLater()

    def _refresh_panels(self):
        from kayra.core.system_profile import live_metrics, human_bytes

        metrics = live_metrics()
        self.cpu_meter.set_value(metrics["cpu_percent"])
        self.ram_meter.set_value(
            metrics["ram_percent"],
            f"{human_bytes(metrics['ram_used'])} / {human_bytes(metrics['ram_total'])}")

        processes = metrics["kayra_processes"]
        # ELIDED, NOT WRAPPED, AND NOT ALLOWED TO SET THE CARD'S WIDTH.
        #
        # `setWordWrap(False)` alone makes a QLabel report its full text width as its minimum,
        # so this one line silently forced the System card far wider than its layout share.
        # With two cards in the strip that was invisible; adding the Graphics card made it
        # overflow the row and clip the third card off the right edge. `Ignored` lets the
        # layout give the card the width it was allotted, and the text is elided to fit.
        self.footprint.setWordWrap(False)
        self.footprint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._footprint_text = (
            f"Kayra is using {human_bytes(metrics['kayra_memory'])} across "
            f"{processes} process{'es' if processes != 1 else ''}")
        self._elide(self.footprint, self._footprint_text)

        load = max(metrics["cpu_percent"], metrics["ram_percent"])
        if load >= 90:
            self.system_pill.set_status("Under load", "warning")
        else:
            self.system_pill.set_status("Healthy", "success")

        count = len(self.bridge.recent_automation(50))
        self.activity_pill.set_status(
            f"{count} action{'s' if count != 1 else ''}", "neutral")

        self._refresh_gpu()
        self._refresh_presence()

    def _refresh_presence(self):
        """
        Three facts about a subsystem whose whole job is to stay quiet.

        The card hides itself when the layer is not running, rather than showing a plausible
        empty state for a service that does not exist — the same rule the Graphics card
        follows on a machine with no GPU.
        """
        status = self.bridge.presence_status()
        if not status:
            self.presence_card.setVisible(False)
            return
        self.presence_card.setVisible(True)

        enabled = bool(status.get("enabled"))
        for caption in (self.presence_last, self.presence_next, self.presence_budget):
            caption.setVisible(enabled)
        self._presence_empty.setVisible(not enabled)
        if not enabled:
            self.presence_pill.set_status("Off", "neutral")
            return
        self.presence_pill.set_status("Active", "success")

        last = status.get("last_kind")
        self._elide(self.presence_last,
                    f"Last observation: {str(last).replace('_', ' ')}" if last
                    else "Last observation: none yet")

        seconds = int(status.get("next_eligible_seconds") or 0)
        if seconds <= 0:
            next_text = "Next eligible: now, if there is a reason"
        elif seconds < 90:
            next_text = f"Next eligible: in {seconds} seconds"
        else:
            next_text = f"Next eligible: in {seconds // 60} minutes"
        self._elide(self.presence_next, next_text)

        budget = int(status.get("daily_budget") or 0)
        spoken = int(status.get("spoken_today") or 0)
        self._elide(self.presence_budget,
                    f"Spoken today: {spoken}" + (f" of {budget}" if budget else ""))

    @staticmethod
    def _elide(label, text):
        """Fits `text` to the width the label was actually given, with a tooltip for the rest."""
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(label.font())
        label.setToolTip(text)
        label.setText(metrics.elidedText(text, Qt.ElideRight, max(40, label.width() - 4)))

    def _refresh_gpu(self):
        """
        Live GPU statistics, and the device speech is actually running on.

        NOTHING HERE IS HARDCODED and nothing here is inferred. The numbers come from
        `tts_device.gpu_metrics()` (nvidia-smi, sampled on a slow shared timer and read from a
        cache, so this never blocks the GUI thread); the provider comes from the live speech
        session. Both reach this screen through the bridge from the one authority, which is
        what keeps this card and the Settings device card from ever disagreeing.

        THE GPU AND THE TTS DEVICE ARE REPORTED SEPARATELY, ON PURPOSE. A machine can have a
        perfectly good RTX 4060 while speech runs on the CPU — because the CUDA runtime is
        missing, or because the user chose CPU. Hiding the physical GPU in that case would be
        as misleading as claiming acceleration that is not happening, so the card shows the
        real hardware and states the real provider beside it.
        """
        metrics = self.bridge.gpu_metrics()
        provider = self.bridge.tts_provider()

        # ── The speech device pill: what Kayra is USING, not what exists ──
        if not provider:
            self.gpu_pill.set_status("—", "neutral")
        elif "CUDA" in provider or "Dml" in provider or "ROCM" in provider:
            self.gpu_pill.set_status("TTS: GPU", "success")
        else:
            self.gpu_pill.set_status("TTS: CPU", "neutral")

        # ── No GPU, or telemetry not available ──
        if not metrics:
            waiting = self.bridge.gpu_telemetry_pending()
            self._gpu_empty.set_message(
                "Reading GPU…" if waiting else "No GPU detected",
                "Fetching statistics." if waiting
                else "Speech synthesis runs on the processor.")
            self._gpu_empty.setVisible(True)
            for widget in (self.gpu_name, self.gpu_meter, self.vram_meter, self.gpu_detail):
                widget.setVisible(False)
            return

        self._gpu_empty.setVisible(False)
        for widget in (self.gpu_name, self.gpu_meter, self.vram_meter, self.gpu_detail):
            widget.setVisible(True)

        # Elided against the width this label was actually given, not a guessed character
        # count — a GPU name is long and varies wildly between vendors.
        self._elide(self.gpu_name, metrics.get("name") or "GPU")

        utilization = metrics.get("utilization")
        self.gpu_meter.set_value(utilization if utilization is not None else 0.0)

        used, total = metrics.get("memory_used_mb"), metrics.get("memory_total_mb")
        if used is not None and total:
            self.vram_meter.set_value(metrics.get("memory_percent") or 0.0,
                                      f"{used / 1024:.1f} / {total / 1024:.1f} GiB")
        else:
            self.vram_meter.set_value(0.0, "unknown")

        parts = []
        temperature = metrics.get("temperature_c")
        if temperature is not None:
            parts.append(f"{temperature:.0f}\u00b0C")
        parts.append(provider or "speech output not running")
        self._gpu_detail_text = "  \u00b7  ".join(parts)
        self._elide(self.gpu_detail, self._gpu_detail_text)
        self.gpu_detail.setToolTip(_GPU_DETAIL_TOOLTIP)

    # ──────────────────────────────────────────────────────────────────
    #                            LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def resizeEvent(self, event):
        # The captions are elided to the width they were GIVEN, so they have to be recomputed
        # when that width changes. Without this, narrowing the window leaves text overflowing
        # its card and widening it leaves a needlessly truncated line.
        super().resizeEvent(event)
        text = getattr(self, "_footprint_text", "")
        if text:
            self._elide(self.footprint, text)
        name = self.gpu_name.toolTip()
        if name:
            self._elide(self.gpu_name, name)
        detail = getattr(self, "_gpu_detail_text", "")
        if detail:
            self._elide(self.gpu_detail, detail)
            self.gpu_detail.setToolTip(_GPU_DETAIL_TOOLTIP)

    def on_show(self):
        self._refresh_panels()
        # Read the live state on entry: it may have changed while this screen was hidden.
        # `_sync_voice` is the ONE read — it paints the caption, the orb and the control from
        # a single snapshot, and it routes both through the same slots the signals use, so the
        # revision guard applies to a synchronous paint exactly as it does to an asynchronous
        # one.
        self._sync_voice()
        self._timer.start()

    def on_hide(self):
        # Stopping the timer here is what keeps a hidden screen genuinely free. The orb stops
        # itself through its own hideEvent.
        self._timer.stop()
