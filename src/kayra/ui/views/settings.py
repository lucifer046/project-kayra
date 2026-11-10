# ┌────────────────────────────────────────────────────────────────────────┐
# │                            settings.py                                 │
# │                    Configuration, Written Safely                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Edits `.env` through `core.config.write_env_values`, which preserves comments and ordering.

SECRETS ARE NEVER RENDERED
--------------------------
An API key field shows whether a key is SET or MISSING and nothing else. The stored value is
never placed in a widget, never read back into the UI, and never logged — a settings screen
that helpfully displays your credentials is a settings screen that leaks them to every
screenshot and screen-share.

Typing a new value replaces the old one. Leaving the field untouched leaves the stored key
alone, which is why the placeholder distinguishes the two states.

RESTART SEMANTICS ARE STATED, NOT HIDDEN
----------------------------------------
Most of Kayra's configuration is read once during boot: the voice model, the recognition
language, the browser choice, model routing. Changing those writes the file immediately and
takes effect on the next start, and the row says so. Silently writing a value that will not do
anything for an hour is worse than a plain label.

The exception is the proactive switch, which is a live service call and applies at once — so it
is the one control with no restart note.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLineEdit, QComboBox, QSizePolicy,
    QSpacerItem, QFrame,
)

from kayra.ui.theme import Space, Size, repolish
from kayra.ui.components.primitives import (
    Card, Caption, StatusPill, AccentButton, GhostButton, Toggle,
    RowRule, FactGrid, SegmentedControl, OutlineButton, _label,
)

QWIDGETSIZE_MAX = 16777215


def fit_field(control):
    """
    A settings control that is AT MOST `Size.control_field` wide and shrinks below it.

    Every control used to be FIXED at 300px, which is what kept the whole screen from fitting
    a narrow window: a fixed control is a fixed minimum for the row, the card and the page.
    The maximum keeps the single right edge on a wide window; the minimum lets it give way.
    """
    control.setMinimumWidth(120)
    control.setMaximumWidth(Size.control_field)
    control.setSizePolicy(QSizePolicy.Expanding, control.sizePolicy().verticalPolicy())
    return control
from kayra.ui.views.base import View


class SettingRow(QWidget):
    """
    Name, help text and one control. The unit every settings group is built from.

    TWO THINGS MAKE THIS READ AS A FORM RATHER THAN A PILE OF CARDS.

    Height. Each row used to be roughly 110px, so "General" — two text fields — occupied 350
    vertical pixels and the page had to be scrolled to be read. The padding is now one grid
    step and the help text is capped to a measure, which brings a row to about 56px.

    Width. Every control is EXACTLY `Size.control_field` wide. Before, text fields sized
    themselves to 260px minimum and combo boxes to 190px, so the right edge of the form
    stepped in and out down the page — the single most obvious sign that a settings screen was
    assembled rather than designed.
    """

    # Below this width the control moves UNDER its label instead of beside it. A row is the
    # label's readable minimum plus the control column; narrower than that, side by side means
    # either a crushed label or a control pushed past the card's edge.
    STACK_BELOW = Size.control_field + Space.lg + 240

    def __init__(self, name, help_text, control, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingRow")
        layout = QBoxLayout(QBoxLayout.LeftToRight, self)
        layout.setContentsMargins(0, Space.sm, 0, Space.sm)
        layout.setSpacing(Space.lg)
        self._layout = layout

        text = QVBoxLayout()
        text.setSpacing(1)
        title = _label(name, "SettingName")
        text.addWidget(title)
        if help_text:
            help_label = _label(help_text, "SettingHelp", wrap=True)
            help_label.setMaximumWidth(Size.settings_label_max)
            text.addWidget(help_label)

        # One column, one width, one right edge — for every control type — while there is room.
        holder = QWidget()
        holder.setFixedWidth(Size.control_field)
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(Space.sm)
        holder_layout.addWidget(control)
        # ONLY a fixed-size control (a Toggle) is aligned. An aligned widget is given its size
        # HINT, not the space, so aligning a text field or a combo shrank it to its content and
        # brought back the ragged right edge `fit_field` exists to remove.
        self._aligned = (control.minimumWidth() == control.maximumWidth()
                         or control.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed)
        if self._aligned:
            holder_layout.setAlignment(control, Qt.AlignRight | Qt.AlignVCenter)
        # Takes the slack to the RIGHT of a capped control once the row is stacked, so the
        # control starts under its label instead of floating in the middle. Zero-width (and
        # inert) while the row is side by side.
        self._tail = QSpacerItem(0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        holder_layout.addItem(self._tail)
        holder_layout.setStretch(0, 100)      # the control fills to its cap before the spacer
        self._holder, self._holder_layout = holder, holder_layout

        layout.addLayout(text, 1)
        layout.addWidget(holder, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.control = control
        self.stacked = False

    def set_stacked(self, stacked):
        """Control beside the label (False) or under it (True). Idempotent."""
        if stacked == self.stacked:
            return
        self.stacked = stacked
        if stacked:
            self._layout.setDirection(QBoxLayout.TopToBottom)
            self._layout.setSpacing(Space.xs)
            self._layout.setStretch(0, 0)
            self._holder.setMinimumWidth(0)
            self._holder.setMaximumWidth(QWIDGETSIZE_MAX)
            self._layout.setAlignment(self._holder, Qt.Alignment())
            self._holder_layout.setAlignment(
                self.control, (Qt.AlignLeft | Qt.AlignVCenter) if self._aligned
                else Qt.Alignment())
            self._tail.changeSize(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum)
            self._holder_layout.invalidate()
        else:
            self._layout.setDirection(QBoxLayout.LeftToRight)
            self._layout.setSpacing(Space.lg)
            self._layout.setStretch(0, 1)
            self._holder.setFixedWidth(Size.control_field)
            self._layout.setAlignment(self._holder, Qt.AlignRight | Qt.AlignVCenter)
            self._holder_layout.setAlignment(
                self.control, (Qt.AlignRight | Qt.AlignVCenter) if self._aligned
                else Qt.Alignment())
            self._tail.changeSize(0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
            self._holder_layout.invalidate()

    def resizeEvent(self, event):
        # Decided from the row's OWN width, so it holds in any card on any screen without the
        # window having to tell it anything. A direction change alters the height only, so it
        # cannot feed back into the width that decided it.
        super().resizeEvent(event)
        self.set_stacked(self.width() < self.STACK_BELOW)


class SettingsView(View):
    title = "Settings"
    subtitle = ("Configuration is stored in .env. API keys go to the system keyring when "
                "there is one, and are never displayed.")

    # (env key, label, help, kind, options)
    GENERAL = (
        ("ASSISTANT_NAME", "Assistant name", "What Kayra calls itself.", "text", None),
        ("USERNAME", "Your name", "Used when Kayra addresses you.", "text", None),
    )
    VOICE = (
        ("ASSISTANT_VOICE", "Voice", "How Kayra sounds. Applies to the next sentence.",
         "voice", None),
        ("INPUT_LANGUAGE", "Recognition language", "The language you speak to Kayra in.",
         "language", None),
    )
    LANGUAGES = (
        ("en-US", "English (United States)"), ("en-GB", "English (United Kingdom)"),
        ("en-IN", "English (India)"), ("hi-IN", "Hindi (India)"), ("es-ES", "Spanish (Spain)"),
        ("fr-FR", "French (France)"), ("de-DE", "German (Germany)"),
    )
    # What an empty field means, shown in it, so a blank box never looks broken.
    PLACEHOLDERS = {
        "ASSISTANT_NAME": "Kayra",
        "LOCAL_BASE_URL": "http://127.0.0.1:1234/v1",
    }
    # Kokoro voice ids are "<language><gender>_<name>"; the list shows them in words.
    VOICE_LANGUAGES = {
        "a": "American English", "b": "British English", "e": "Spanish", "f": "French",
        "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Mandarin",
    }
    # The four sections the screen is split into, in order.
    SECTIONS = (
        ("general", "General"), ("voice", "Voice"), ("models", "AI models"),
        ("gestures", "Gestures"), ("proactive", "Proactive"),
    )
    # STT_BROWSER is deliberately NOT in the group above any more. Everything there is written
    # to `.env` and applies at the next start; the speech backend now switches the LIVE
    # session, so it has its own card and its own transactional handler — see `_build_speech`.
    # ONE switch decides local vs cloud. Off (the default): LM Studio answers whenever it is
    # running, and the cloud only when it is not. FORCE_ONLINE still works from `.env` but is
    # no longer offered here — two toggles that both meant "use the cloud" was a puzzle.
    MODELS = (
        ("CLOUD_FIRST", "Use cloud models even when LM Studio is running",
         "Off: your local model answers whenever LM Studio is running, and Claude, Groq or "
         "Gemini only when it is not. On: the cloud models answer even while LM Studio runs.",
         "bool", None),
        ("LOCAL_BASE_URL", "LM Studio address",
         "Where your local model server listens. Leave empty for the default.", "text", None),
    )

    # (provider, key name, what it is for, ((model env key, label, help), ...))
    PROVIDERS = (
        ("Claude", "CLAUDE_API_KEY",
         "Asked first, for commands and conversation. A key from console.anthropic.com "
         "(pay-as-you-go; a claude.ai plan does not include API access).",
         (("CLAUDE_MODEL", "Model",
           "Haiku 5.5 is the fastest and cheapest, and can read the screen."),)),
        ("Groq", "GROQ_API_KEY",
         "Used when Claude is not set or cannot answer. Free key: console.groq.com.",
         (("GROQ_CHAT_MODEL", "Chat model", "Answers conversation."),
          ("GROQ_DECISION_MODEL", "Command model",
           "Works out what you asked for. The small model is the fastest."))),
        ("Gemini", "GEMINI_API_KEY",
         "The last fallback, and a second model for reading the screen. Key from "
         "aistudio.google.com.",
         (("GEMINI_CHAT_MODEL", "Model",
           "gemini-flash-latest follows Google's current Flash model."),)),
    )
    # What each model box shows when `.env` names nothing — the engine's own defaults.
    DEFAULT_MODELS = {
        "CLAUDE_MODEL": "claude-haiku-5-5",
        "GROQ_CHAT_MODEL": "openai/gpt-oss-120b",
        "GROQ_DECISION_MODEL": "openai/gpt-oss-20b",
        "GEMINI_CHAT_MODEL": "gemini-flash-latest",
    }
    KEY_STATUS = {
        "keyring": ("In keyring", "success"),
        "file": ("Saved", "success"),
        "missing": ("Missing", "warning"),
    }

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._controls = {}
        self._secret_fields = {}

        self._build_sections()
        self._section("general")
        self._build_group("Identity", self.GENERAL)
        self._section("voice")
        self._build_group("Voice", self.VOICE)
        self._build_speech()
        self._build_device()
        self._section("models")
        self._build_routing()
        self._build_providers()
        self._section("gestures")
        self._build_gesture()
        self._section("proactive")
        self._build_proactive()
        self._build_presence()
        self._build_actions()
        self.content.addStretch(1)
        self.show_section("general")

        # GPU telemetry is the only value on this screen with no event source, so it is the
        # only thing here on a timer — and the timer runs ONLY while the screen is visible
        # (see `on_show` / `on_hide`). `tts_device.gpu_metrics()` returns from a cache and
        # never blocks; its sampler thread parks itself shortly after the last request, so
        # navigating away stops the sampling too.
        self._gpu_timer = QTimer(self)
        self._gpu_timer.setInterval(4000)
        self._gpu_timer.timeout.connect(self._refresh_gpu)

    # ──────────────────────────────────────────────────────────────────

    def _current(self, key, default=""):
        from kayra.core.config import env
        return env(key, default) or default

    def _build_group(self, title, rows, restart_note=True):
        card = Card(title)
        card.body.setSpacing(0)
        for index, (env_key, label, help_text, kind, options) in enumerate(rows):
            control = self._make_control(env_key, kind, options)
            self._controls[env_key] = (control, kind)
            if index:
                card.body.addWidget(RowRule())      # a hairline, not a gap
            card.body.addWidget(SettingRow(label, help_text, control))
        if restart_note:
            note = Caption("Changes apply the next time Kayra starts."
                           if title != "Voice" else
                           "The voice changes on Save; the language at the next start.")
            note.setContentsMargins(0, Space.sm, 0, 0)
            card.body.addWidget(note)
        self._add(card)

    def _make_control(self, env_key, kind, options):
        value = self._current(env_key)
        if kind == "bool":
            control = Toggle(str(value).strip().lower() in ("true", "1", "yes", "on"))
        elif kind == "choice":
            control = QComboBox()
            control.setEditable(False)
            control.addItems(options or [])
            fit_field(control)
            if value and value in (options or []):
                control.setCurrentText(value)
            elif value:
                control.addItem(value)
                control.setCurrentText(value)
        elif kind == "language":
            control = QComboBox()
            control.setEditable(False)
            for code, name in self.LANGUAGES:
                control.addItem(name, code)
            current = value or "en-US"
            if control.findData(current) < 0:
                control.addItem(current, current)
            control.setCurrentIndex(control.findData(current))
            fit_field(control)
        elif kind == "voice":
            control = QComboBox()
            control.setEditable(False)
            voices = self.bridge.available_voices()
            current = value or self.bridge.current_voice() or (
                "am_adam" if str(self._current("ASSISTANT_GENDER", "Female")).lower() == "male"
                else "af_bella")
            if current and current not in voices:
                voices = [current] + list(voices)
            for voice in voices:
                control.addItem(self._voice_label(voice), voice)
            index = control.findData(current)
            control.setCurrentIndex(max(0, index))
            control.setMaxVisibleItems(14)
            fit_field(control)
        else:
            control = QLineEdit(str(value))
            placeholder = self.PLACEHOLDERS.get(env_key)
            if env_key == "USERNAME":
                import getpass
                try:
                    placeholder = getpass.getuser().title()
                except Exception:
                    placeholder = "User"
            if placeholder:
                control.setPlaceholderText(f"{placeholder}  (default)")
            fit_field(control)
        return control

    @classmethod
    def _voice_label(cls, voice):
        """'af_bella' -> 'Bella · American English, female'."""
        prefix, _, name = str(voice).partition("_")
        if len(prefix) != 2 or not name:
            return str(voice)
        language = cls.VOICE_LANGUAGES.get(prefix[0], prefix[0].upper())
        gender = {"f": "female", "m": "male"}.get(prefix[1], "")
        return f"{name.title()}  ·  {language}" + (f", {gender}" if gender else "")

    # ──────────────────────────────────────────────────────────────────
    #                    SECTIONS AND THE PINNED SAVE BAR
    # ──────────────────────────────────────────────────────────────────

    def _build_sections(self):
        """
        One section at a time, chosen from a segmented switcher (a dropdown on a narrow
        window, where five segments do not fit). The page used to be eleven cards in one
        scroll, which is what made it hard to find anything; each section is now one or two
        screens of related settings.
        """
        self.section_switch = SegmentedControl(self.SECTIONS, "general")
        self.section_switch.changed.connect(self.show_section)
        self.section_combo = QComboBox()
        for key, label in self.SECTIONS:
            self.section_combo.addItem(label, key)
        self.section_combo.currentIndexChanged.connect(
            lambda i: self.show_section(self.section_combo.itemData(i)))
        self.section_combo.hide()
        switch_row = QHBoxLayout()
        switch_row.setContentsMargins(0, 0, 0, Space.xs)
        switch_row.addWidget(self.section_switch)
        switch_row.addWidget(self.section_combo)
        switch_row.addStretch(1)
        self.content.addLayout(switch_row)

        self._sections = {}
        for key, _name in self.SECTIONS:
            box = QWidget()
            layout = QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(Space.base)
            box.hide()
            self._sections[key] = (box, layout)
            self.content.addWidget(box)
        self._target = None

    def _section(self, key):
        self._target = self._sections[key][1]

    def _add(self, widget):
        (self._target or self.content).addWidget(widget)

    def show_section(self, key):
        if key not in self._sections:
            return
        for name, (box, _layout) in self._sections.items():
            box.setVisible(name == key)
        self.current_section = key
        self.section_switch.select(key)
        index = self.section_combo.findData(key)
        if index >= 0 and self.section_combo.currentIndex() != index:
            self.section_combo.blockSignals(True)
            self.section_combo.setCurrentIndex(index)
            self.section_combo.blockSignals(False)
        self.scroll.verticalScrollBar().setValue(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 600
        self.section_switch.setVisible(not narrow)
        self.section_combo.setVisible(narrow)

    # ──────────────────────────────────────────────────────────────────
    #                          SPEECH INPUT
    # ──────────────────────────────────────────────────────────────────

    def _build_speech(self):
        """
        Which browser runs recognition — and, SEPARATELY, which one actually is.

        THE TWO LINES ARE NOT REDUNDANT, for exactly the reason the speech-DEVICE card below
        carries two. `Backend` is what the user asked for; `Active` is what the live session
        reports. They agree on the happy path and disagree whenever a named browser could not
        be started, which is the case this card exists to show. Rendering "Google Chrome"
        because the dropdown says Chrome, while Edge holds the microphone, is the specific lie
        the whole live-switching change was made to remove.
        """
        from kayra.input import stt_backend

        card = Card("Speech input")
        card.body.setSpacing(0)

        current = stt_backend.normalize(self._current("STT_BROWSER", stt_backend.AUTO))
        self.backend_combo = QComboBox()
        self.backend_combo.setEditable(False)
        for key in (stt_backend.AUTO, "chrome", "edge", "brave", "chromium"):
            self.backend_combo.addItem(stt_backend.label_for(key), key)
        index = self.backend_combo.findData(current)
        if index < 0:
            self.backend_combo.addItem(stt_backend.label_for(current), current)
            index = self.backend_combo.count() - 1
        self.backend_combo.setCurrentIndex(index)
        fit_field(self.backend_combo)
        self.backend_combo.currentIndexChanged.connect(self._on_backend)

        card.body.addWidget(SettingRow(
            "Backend",
            "Which browser runs speech recognition. Automatic picks one that can actually "
            "transcribe; naming one uses that browser and says so if it cannot start.",
            self.backend_combo))
        card.body.addWidget(RowRule())

        self.backend_pill = StatusPill("Checking", "neutral")
        card.add_header_widget(self.backend_pill)
        self.backend_active = FactGrid(min_column=170, max_columns=3)
        self.backend_active.setText("Status: reading the live session…")
        self.backend_detail = Caption("")
        self.backend_detail.setWordWrap(True)
        card.body.addSpacing(Space.sm)
        card.body.addWidget(self.backend_active)
        card.body.addSpacing(Space.xs)
        card.body.addWidget(self.backend_detail)

        self._add(card)
        self.bridge.sttBackendChanged.connect(lambda _state: self._refresh_backend())
        self._refresh_backend()

    def _on_backend(self, _index):
        """
        Applies a backend change to the RUNNING session, then writes it to `.env`.

        The dropdown is disabled for the duration. A second switch arriving while the first is
        still tearing a browser down is the one genuinely unsafe thing here — the manager
        rejects it as stale anyway, but a control that stays clickable during a multi-second
        operation invites the user to think nothing happened.

        `.env` is written ONLY on success. A setting that failed to apply must not come back
        after a restart claiming to be the configuration.
        """
        target = self.backend_combo.currentData()
        self.backend_combo.setEnabled(False)
        self.backend_pill.set_status("Switching", "warning")
        self.backend_active.setText("Switching the speech backend…")
        try:
            committed, detail = self.bridge.set_stt_backend(target)
        finally:
            self.backend_combo.setEnabled(True)

        if committed:
            from kayra.core.config import write_env_values
            write_env_values({"STT_BROWSER": target})
            self.status.setText(f"Speech input is now using {detail}.")
        else:
            self.status.setText(f"Could not switch speech input: {detail}")
        self._refresh_backend()

    def _refresh_backend(self):
        """Repaints the card from the LIVE backend manager. Requested and active stay separate."""
        from kayra.input import stt_backend

        state = self.bridge.stt_backend_state() or {}
        if not state:
            self.backend_pill.set_status("Not running", "neutral")
            self.backend_active.setText(
                f"Backend: {self.backend_combo.currentText()}\n"
                "Active: None\n"
                "Status: speech input is not running")
            self.backend_detail.setText(
                "This applies at the next start. Speech input is unavailable in this session.")
            return

        status = state.get("status", stt_backend.BackendStatus.OFF)
        matches = bool(state.get("matches"))
        active_label = state.get("active_label", "None")

        if status == stt_backend.BackendStatus.OFF:
            self.backend_pill.set_status("Not running", "neutral")
        elif status == stt_backend.BackendStatus.ERROR or not matches:
            # "Requested one and got another" is a WARNING, not a neutral fact: the user asked
            # for something they did not get, and the screen has to say so.
            self.backend_pill.set_status("Not applied", "warning")
        elif status in (stt_backend.BackendStatus.STARTING,
                        stt_backend.BackendStatus.RECOVERING):
            self.backend_pill.set_status("Starting", "warning")
        elif status == stt_backend.BackendStatus.PAUSED:
            self.backend_pill.set_status("Paused", "neutral")
        else:
            self.backend_pill.set_status("Ready", "success")

        self.backend_active.setText(
            f"Backend: {state.get('requested_label', 'Automatic')}\n"
            f"Active: {active_label}\n"
            f"Status: {status.title()}")

        lines = []
        if state.get("last_error"):
            lines.append(f"Last failure: {state['last_error']}")
        if state.get("requested_backend") == stt_backend.AUTO and active_label != "None":
            lines.append(f"Automatic selected {active_label} because it can reach a speech "
                         f"recognition backend.")
        if state.get("browser_process_id"):
            lines.append(f"Driver process: {state['browser_process_id']}")
        self.backend_detail.setText("\n".join(lines) or
                                    "Changes apply immediately. No restart is needed.")

    def _build_device(self):
        """
        Where speech synthesis runs — and, separately, where it is ACTUALLY running.

        THE TWO LINES ARE NOT REDUNDANT. `Mode` is what the user chose; `Active device` and
        `Provider` are what the live ONNX session reports. They disagree whenever a GPU is
        requested and cannot be initialized — the case this card was built for was a real
        RTX 4060 with a GPU-capable `onnxruntime-gpu` wheel and no CUDA runtime, where ONNX
        Runtime silently dropped the provider and returned a CPU session. Showing only the mode
        would have meant this screen displayed "GPU" while every millisecond of synthesis ran on
        the processor. Reporting the runtime truth is the entire reason this card exists.
        """
        from kayra.output import tts_device

        card = Card("Speech device")
        card.body.setSpacing(0)

        current = tts_device.normalize_mode(self._current("TTS_DEVICE_MODE",
                                                          tts_device.MODE_AUTO))
        self.device_combo = QComboBox()
        self.device_combo.setEditable(False)
        for mode in tts_device.MODES:
            self.device_combo.addItem(tts_device.MODE_LABELS[mode], mode)
        self.device_combo.setCurrentIndex(list(tts_device.MODES).index(current))
        fit_field(self.device_combo)
        self.device_combo.currentIndexChanged.connect(self._on_device_mode)

        card.body.addWidget(SettingRow(
            "TTS device",
            "Automatic prefers a GPU when one is genuinely usable. GPU requires one and says "
            "so if it falls back. CPU forces the processor.",
            self.device_combo))
        card.body.addWidget(RowRule())

        self.device_status_pill = StatusPill("Checking", "neutral")
        self.device_active = FactGrid(min_column=170, max_columns=3)
        self.device_active.setText("Status: reading the live session\u2026")
        self.device_detail = Caption("")
        self.device_detail.setWordWrap(True)
        card.body.addSpacing(Space.sm)
        card.body.addWidget(self.device_active)
        card.body.addSpacing(Space.xs)
        card.body.addWidget(self.device_detail)
        card.add_header_widget(self.device_status_pill)

        card.body.addWidget(RowRule())
        self.gpu_line = Caption("GPU information unavailable")
        self.gpu_line.setWordWrap(True)
        card.body.addWidget(self.gpu_line)

        self._add(card)
        self._refresh_device()

    def _on_device_mode(self, _index):
        """
        Applies a device change to the RUNNING engine, then writes it to .env.

        Switching is safe here because the engine does it safely: `set_device_mode` cancels
        anything being spoken, waits for the pipeline to drain, and builds the replacement
        session BEFORE releasing the old one — so a provider that cannot initialize leaves
        Kayra with the voice it already had rather than mute. The dropdown is disabled for the
        duration, because the one thing that genuinely is not safe is a second switch arriving
        while the first is still swapping the session.
        """
        mode = self.device_combo.currentData()
        self.device_combo.setEnabled(False)
        try:
            applied = self.bridge.set_tts_device(mode)
        finally:
            self.device_combo.setEnabled(True)
        if applied is None:
            self.device_detail.setText(
                "Speech output is not running, so this takes effect at the next start.")
        self._refresh_device()

    def _refresh_device(self):
        """Repaints the card from the live session, or from static discovery if there is none."""
        from kayra.output import tts_device

        report = self.bridge.tts_device_report()
        if not report:
            # No live engine (speech output unavailable, or the UI opened before boot
            # finished). Describe what WOULD happen, and label it as such rather than
            # presenting a prediction as a measurement.
            status = tts_device.describe(self.device_combo.currentData())
            report = status.to_dict()
            report["live"] = False
        else:
            report["live"] = True

        provider = report.get("provider", "unknown")
        device = report.get("device", "unknown")
        on_gpu = report.get("status") == tts_device.DEVICE_GPU

        if not report["live"]:
            self.device_status_pill.set_status("Not running", "neutral")
        elif report.get("fallback"):
            # "Fell back" and "CPU" are different outcomes and must read differently: the first
            # means the user asked for something they did not get.
            self.device_status_pill.set_status("Fell back", "warning")
        else:
            self.device_status_pill.set_status("GPU" if on_gpu else "CPU",
                                               "success" if on_gpu else "neutral")

        prefix = "Active device" if report["live"] else "Would use"
        self.device_active.setText(
            f"Mode: {report.get('mode_label', report.get('mode'))}\n"
            f"{prefix}: {device}\n"
            f"Provider: {provider}")

        detail = report.get("reason") or ""
        available = ", ".join(report.get("available") or []) or "none"
        lines = [f"Available providers: {available}"]
        # TensorRT is deliberately never used for Kokoro — it builds an engine on first run,
        # and a TensorRT failure must not be able to stand between the speech model and CUDA.
        # Saying so here stops "but TensorRT is listed" being a puzzle.
        if any("Tensorrt" in p for p in (report.get("available") or [])):
            lines.append("TensorRT is available but not used for speech synthesis.")
        if detail:
            lines.insert(0, detail)
        self.device_detail.setText("\n".join(lines))

        self._refresh_gpu()

    def _refresh_gpu(self):
        """
        One line of GPU telemetry, or an honest absence of one.

        Never required for speech: a machine with no NVIDIA tooling shows "GPU information
        unavailable" and everything else on this screen works exactly the same.
        """
        from kayra.output import tts_device

        metrics = tts_device.gpu_metrics()
        if not metrics:
            self.gpu_line.setText(
                "GPU information unavailable"
                if not tts_device.gpu_telemetry_available()
                else "GPU information unavailable \u2014 reading\u2026")
            return

        parts = [metrics.get("name") or "GPU"]
        if metrics.get("utilization") is not None:
            parts.append(f"{metrics['utilization']:.0f}% utilization")
        used, total = metrics.get("memory_used_mb"), metrics.get("memory_total_mb")
        if used is not None and total:
            parts.append(f"{used / 1024:.1f} / {total / 1024:.1f} GiB VRAM "
                         f"({metrics['memory_percent']:.0f}%)")
        if metrics.get("temperature_c") is not None:
            parts.append(f"{metrics['temperature_c']:.0f}\u00b0C")
        self.gpu_line.setText("  \u00b7  ".join(parts))

    # ──────────────────────────────────────────────────────────────────
    #                     CLOUD MODELS: KEYS AND MODEL CHOICE
    # ──────────────────────────────────────────────────────────────────

    def _build_routing(self):
        """Local vs cloud: the one switch, the endpoint, and what is ACTUALLY answering now."""
        self._build_group("Local or cloud", self.MODELS, restart_note=False)
        card = self._target.itemAt(self._target.count() - 1).widget()
        self.routing_facts = FactGrid(min_column=200, max_columns=2)
        card.body.addSpacing(Space.sm)
        card.body.addWidget(self.routing_facts)
        self._refresh_routing()

    def _refresh_routing(self):
        state = self.bridge.model_routing()
        if not state:
            self.routing_facts.set_facts([("Answering now", "Kayra is still starting")])
            return
        self.routing_facts.set_facts([
            ("Answering now", state.get("answering", "")),
            ("LM Studio", "running" if state.get("mode") == "local"
             else "not used" if self._controls["CLOUD_FIRST"][0].isChecked()
             else "not running", state.get("local_url", "")),
        ])

    # What each provider is FOR, under its name.
    PROVIDER_ROLES = {"Claude": "First choice", "Groq": "Fallback", "Gemini": "Last fallback"}

    def _build_providers(self):
        """
        One panel per provider: name and role, where its key is kept, the key, its model(s),
        and Test / Remove.

        Models are CHOSEN, never typed. A free-text model box let a typo become "no model
        could answer" at the next request; the list holds the models Kayra knows to work, and
        a successful Test replaces it with exactly what that key can use.

        Keys are SAVED with the Save bar like every other setting and applied to the running
        engine at once. Test checks the key on screen — or the stored one when the field is
        empty — with one tiny request. Remove deletes the stored key after asking.
        """
        card = Card("Cloud models")
        card.body.setSpacing(Space.md)
        self._provider_ui = {}
        self.key_storage = Caption("")
        self.key_storage.setWordWrap(True)
        card.body.addWidget(self.key_storage)

        suggestions = self.bridge.provider_model_suggestions()
        for provider, key_name, purpose, models in self.PROVIDERS:
            panel = QFrame()
            panel.setObjectName("ProviderPanel")
            body = QVBoxLayout(panel)
            body.setContentsMargins(Space.base, Space.md, Space.base, Space.sm)
            body.setSpacing(0)

            header = QHBoxLayout()
            header.setSpacing(Space.sm)
            names = QVBoxLayout()
            names.setSpacing(0)
            names.addWidget(_label(provider, "ProviderName"))
            names.addWidget(_label(self.PROVIDER_ROLES.get(provider, ""), "ProviderRole"))
            header.addLayout(names, 1)
            pill = StatusPill("Missing", "warning")
            header.addWidget(pill, 0, Qt.AlignTop)
            body.addLayout(header)
            body.addSpacing(Space.xs)

            field = QLineEdit()
            field.setEchoMode(QLineEdit.Password)
            field.setProperty("secret", "true")
            repolish(field)
            fit_field(field)
            body.addWidget(SettingRow("API key", purpose, field))
            self._secret_fields[key_name] = field

            combos = []
            for env_key, label, help_text in models:
                combo = QComboBox()
                combo.setEditable(False)
                fit_field(combo)
                current = self._current(env_key) or self.DEFAULT_MODELS.get(env_key, "")
                self._fill_models(combo, suggestions.get(provider, []), current)
                self._controls[env_key] = (combo, "model")
                combos.append(combo)
                body.addWidget(SettingRow(label, help_text, combo))

            actions = QHBoxLayout()
            actions.setContentsMargins(0, Space.sm, 0, Space.xs)
            actions.setSpacing(Space.sm)
            test = OutlineButton("Test key")
            test.setToolTip(f"Send one tiny request to {provider} with this key and model")
            remove = GhostButton("Remove key")
            remove.setProperty("tone", "danger")
            result_pill = StatusPill("", "neutral")
            result_pill.hide()
            result = Caption("")
            result.setWordWrap(True)
            result.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            actions.addWidget(test)
            actions.addWidget(remove)
            actions.addWidget(result_pill)
            actions.addWidget(result, 1)
            body.addLayout(actions)
            card.body.addWidget(panel)

            test.clicked.connect(lambda _=False, p=provider: self._test_provider(p))
            remove.clicked.connect(lambda _=False, p=provider: self._remove_provider_key(p))
            self._provider_ui[provider] = {
                "key_name": key_name, "field": field, "pill": pill, "combos": combos,
                "test": test, "remove": remove, "result": result, "result_pill": result_pill,
            }

        self.bridge.providerTested.connect(self._on_provider_tested)
        self._add(card)
        self._refresh_key_status()

    @staticmethod
    def _fill_models(combo, models, current):
        """The known models plus whatever is configured; the current one selected."""
        combo.blockSignals(True)
        combo.clear()
        names = list(dict.fromkeys([m for m in models if m] + ([current] if current else [])))
        for name in names:
            combo.addItem(name, name)
        index = combo.findData(current) if current else 0
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _refresh_key_status(self):
        status = self.bridge.provider_key_status()
        where = self.bridge.key_storage_label()
        self.key_storage.setText(
            "Kayra asks Claude first, then Groq, then Gemini, for commands and conversation "
            f"alike. One key is enough. New keys are saved in {where} and never shown again.")
        for provider, ui in getattr(self, "_provider_ui", {}).items():
            state = status.get(provider, "missing")
            text, tone = self.KEY_STATUS.get(state, self.KEY_STATUS["missing"])
            ui["pill"].set_status(text, tone)
            ui["field"].setPlaceholderText(
                "Type to replace" if state != "missing" else "Paste a key")
            ui["remove"].setEnabled(state != "missing")

    def _test_provider(self, provider):
        ui = self._provider_ui[provider]
        model = (str(ui["combos"][0].currentData() or ui["combos"][0].currentText()).strip()
                 if ui["combos"] else "")
        ui["test"].setEnabled(False)
        ui["result_pill"].hide()
        ui["result"].setText(f"Testing {model}…")
        self.bridge.test_provider(provider, ui["field"].text().strip(), model)

    def _on_provider_tested(self, provider, result):
        ui = getattr(self, "_provider_ui", {}).get(provider)
        if ui is None:
            return
        ui["test"].setEnabled(True)
        ok = bool(result.get("ok"))
        ui["result_pill"].set_status("Works" if ok else "Failed", "success" if ok else "danger")
        ui["result_pill"].show()
        ui["result"].setText(str(result.get("message") or ""))
        models = result.get("models") or []
        if ok and models:
            suggestions = self.bridge.provider_model_suggestions().get(provider, [])
            for combo in ui["combos"]:
                current = str(combo.currentData() or combo.currentText()).strip()
                ordered = [m for m in suggestions if m in models] + models
                self._fill_models(combo, ordered, current)

    def _remove_provider_key(self, provider):
        from kayra.ui.components.confirm import ConfirmDialog
        if not ConfirmDialog.ask(
                self, f"Remove the {provider} key?",
                f"Kayra will stop using {provider} until a new key is saved. The key is "
                "deleted from this computer and cannot be shown again.",
                confirm_label="Remove key"):
            return
        outcome = self.bridge.save_provider_key(provider, "")
        ui = self._provider_ui[provider]
        ui["field"].clear()
        ui["result_pill"].hide()
        ui["result"].setText(f"{provider} key removed." if outcome.get("where")
                             else "Could not remove the key — check file permissions.")
        self._refresh_key_status()

    # ──────────────────────────────────────────────────────────────────
    #                       HAND GESTURE CONTROL
    # ──────────────────────────────────────────────────────────────────

    # THREE WORDS EACH, NOT FOURTEEN NUMBERS. Every one of these maps to a small table of
    # thresholds in `kayra.input.gesture.config`; the numbers stay in `.env` for anyone who
    # wants them, and the screen offers the three decisions a person can actually make. A
    # settings page with a One Euro `min_cutoff` spin box on it is a page that teaches nothing
    # and gets set wrong.
    GESTURE = (
        ("GESTURE_SENSITIVITY", "Gesture sensitivity",
         "How readily a pose is recognised. Lower means you have to mean it.",
         "choice", ["LOW", "MEDIUM", "HIGH"]),
        ("GESTURE_CURSOR_SMOOTHING", "Cursor smoothing",
         "Higher is steadier when your hand is still. It does not add lag when you move.",
         "choice", ["LOW", "MEDIUM", "HIGH"]),
        ("GESTURE_CLICK_SENSITIVITY", "Click sensitivity",
         "How closed a pinch has to be, and how long it has to hold.",
         "choice", ["LOW", "MEDIUM", "HIGH"]),
        ("GESTURE_ENABLED", "Enable at startup",
         "Whether hand gesture control starts with Kayra. Off by default — something that "
         "moves your pointer should be something you asked for.",
         "bool", None),
        ("GESTURE_DIAGNOSTICS", "Advanced diagnostics",
         "Logs frame rates, latency, stability and dropped frames at DEBUG level.",
         "bool", None),
    )

    def _build_gesture(self):
        """
        The gesture card: two LIVE switches and the persisted preferences below them.

        The two switches at the top behave exactly like the speech-device dropdown — they act
        on the running controller immediately AND are reflected back from it, so a camera that
        refuses to start leaves the switch off rather than showing what was asked for. The
        rows underneath are ordinary `.env` settings that apply at the next start, and the
        card says which is which rather than leaving the user to discover it.
        """
        card = Card("Hand gesture control")
        self.gesture_pill = StatusPill("Off", "neutral")
        card.add_header_widget(self.gesture_pill)
        card.body.setSpacing(0)

        self.camera_toggle = Toggle(False)
        self.camera_toggle.toggled.connect(self._on_camera_toggle)
        card.body.addWidget(SettingRow(
            "Camera",
            "Turns the camera on. Nothing is recorded and nothing leaves this machine. "
            "The preview on Home reads this same stream.",
            self.camera_toggle))
        card.body.addWidget(RowRule())

        self.gesture_toggle = Toggle(False)
        self.gesture_toggle.toggled.connect(self._on_gesture_toggle)
        card.body.addWidget(SettingRow(
            "Hand gesture control",
            "Lets your hand drive the pointer. Starts the camera if it is off. "
            "Applies immediately.",
            self.gesture_toggle))
        card.body.addWidget(RowRule())

        for index, (env_key, label, help_text, kind, options) in enumerate(self.GESTURE):
            control = self._make_control(env_key, kind, options)
            self._controls[env_key] = (control, kind)
            if index:
                card.body.addWidget(RowRule())
            card.body.addWidget(SettingRow(label, help_text, control))

        self.gesture_detail = Caption("The three settings above apply the next time Kayra "
                                      "starts.")
        self.gesture_detail.setContentsMargins(0, Space.sm, 0, 0)
        card.body.addWidget(self.gesture_detail)
        self._add(card)

        self.bridge.gestureStateChanged.connect(self._on_gesture_status)
        # Built before the backend is ready, like every other screen — so it must re-read when
        # it becomes ready. `gestureStateChanged` fires on transitions only, and there is no
        # transition to correct a control that was painted from a pre-boot read.
        self.bridge.bootFinished.connect(lambda ok, detail: self._refresh_gesture())

    # NEITHER OF THESE LOGS ANYTHING, AND THAT IS THE RULE, NOT AN OMISSION. The settings
    # recorder in `kayra.core.settings_log` is the ONE owner of "a setting changed", and
    # `app.set_camera` / `app.set_gesture_control` already go through it transactionally —
    # requested, run, committed only on success. A view that also announced the change would
    # be the second of two lines for one event, and the two would eventually disagree about
    # whether it took. No UI module imports the logger; `tests/test_logging.py` asserts it.

    def _on_camera_toggle(self, enabled):
        if getattr(self, "_gesture_syncing", False):
            return
        self.bridge.set_camera(bool(enabled))
        self._refresh_gesture()

    def _on_gesture_toggle(self, enabled):
        if getattr(self, "_gesture_syncing", False):
            return
        self.bridge.set_gesture(bool(enabled))
        self._refresh_gesture()

    def _refresh_gesture(self):
        self._on_gesture_status(self.bridge.gesture_status() or {})

    def _on_gesture_status(self, status):
        """
        Reflects what the controller actually did. Never what the switch was set to.

        This is the same requested-vs-active discipline the speech backend card follows, and
        for the same reason: a screen showing "on" over a camera that failed to open is the one
        thing a device setting must not do.
        """
        status = dict(status or {})
        camera_state = str(status.get("camera", "OFF"))
        camera_on = camera_state not in ("OFF", "ERROR")
        gesture_on = bool(status.get("gesture_enabled"))
        error = str(status.get("error", ""))

        self._gesture_syncing = True
        try:
            self.camera_toggle.setChecked(camera_on)
            self.gesture_toggle.setChecked(gesture_on)
        finally:
            self._gesture_syncing = False

        if camera_state == "ERROR" or str(status.get("state")) == "ERROR":
            self.gesture_pill.set_status("Error", "danger")
        elif gesture_on:
            self.gesture_pill.set_status("Active", "success")
        elif camera_on:
            self.gesture_pill.set_status("Camera on", "neutral")
        else:
            self.gesture_pill.set_status("Off", "neutral")

        self.gesture_detail.setText(
            error if error else
            "The three settings above apply the next time Kayra starts.")

    def _build_proactive(self):
        card = Card("Proactive agent")
        self.proactive_toggle = Toggle(self.bridge.proactive_enabled())
        # The only live control on the screen: it calls the running service directly.
        self.proactive_toggle.toggled.connect(self.bridge.set_proactive)
        card.body.setSpacing(0)
        card.body.addWidget(SettingRow(
            "Unprompted suggestions",
            "Lets Kayra speak on its own occasionally. Applies immediately, and only for this "
            "session unless you also save it below.",
            self.proactive_toggle))
        card.body.addWidget(RowRule())

        enabled_control = Toggle(
            str(self._current("PROACTIVE_AGENT_ENABLED", "True")).lower() in
            ("true", "1", "yes", "on"))
        self._controls["PROACTIVE_AGENT_ENABLED"] = (enabled_control, "bool")
        card.body.addWidget(SettingRow(
            "Enable at startup", "Whether the service starts with Kayra.", enabled_control))
        self._add(card)

    # (env key, category, label, help)
    PRESENCE = (
        ("PROACTIVE_GREETINGS_ENABLED", "greetings", "Greetings",
         "Varies how Kayra greets you by time of day and how long you have been away."),
        ("PROACTIVE_CONTEXT_ENABLED", "context", "Contextual observations",
         "Lets Kayra notice patterns — repeated failures, long stretches, coming back."),
        ("PROACTIVE_LATE_NIGHT_ENABLED", "late_night", "Late-night awareness",
         "One quiet remark when you are still working in the small hours."),
        ("PROACTIVE_WORK_SESSION_ENABLED", "work_session", "Work-session awareness",
         "A gentle observation after a long unbroken stretch. Never repeated."),
        ("PROACTIVE_SYSTEM_ENABLED", "system", "System observations",
         "Sustained memory or processor pressure, and a low battery."),
        ("PROACTIVE_HUMOR_ENABLED", "humor", "Dry humour",
         "A rare observational remark. Hours between any two."),
    )

    def _build_presence(self):
        """
        The contextual layer's switches.

        Each toggle is BOTH live and persisted, like the speech-device dropdown: it calls the
        running service so the change is felt immediately, and `_save` writes it to `.env` so
        the choice survives a restart. Writing only the file would mean a switch that appears
        to do nothing for the rest of the session, which is the worst kind of setting.

        The master `Unprompted suggestions` switch above stays authoritative — with it off,
        nothing here can make Kayra speak, and the note on the card says so rather than
        leaving the user to discover it.
        """
        card = Card("Proactive presence")
        card.body.setSpacing(0)

        self.presence_toggle = Toggle(self.bridge.presence_enabled())
        self.presence_toggle.toggled.connect(self._on_presence)
        card.body.addWidget(SettingRow(
            "Contextual presence",
            "Allows Kayra to occasionally initiate a short, context-aware remark when there "
            "is a useful reason to do so. Silence is the default.",
            self.presence_toggle))

        live = self.bridge.presence_categories()
        self._presence_controls = {}
        for env_key, category, label, help_text in self.PRESENCE:
            stored = str(self._current(env_key, "True")).strip().lower() in ("true", "1", "yes", "on")
            control = Toggle(bool(live.get(category, stored)))
            control.toggled.connect(
                lambda checked, name=category: self.bridge.set_presence_category(name, checked))
            self._controls[env_key] = (control, "bool")
            self._presence_controls[category] = control
            card.body.addWidget(RowRule())
            card.body.addWidget(SettingRow(label, help_text, control))

        note = Caption("Applies immediately. Saved settings also apply on the next start. "
                       "Turning off Unprompted suggestions silences all of these.")
        note.setContentsMargins(0, Space.sm, 0, 0)
        card.body.addWidget(note)
        self._add(card)

    def _on_presence(self, enabled):
        self.bridge.set_presence(bool(enabled))
        for control in getattr(self, "_presence_controls", {}).values():
            control.setEnabled(bool(enabled))

    def _build_actions(self):
        """The Save bar: OUTSIDE the scroll area, pinned to the bottom of the screen, so saving
        never means scrolling to the end of a section to find the button."""
        bar = QFrame()
        bar.setObjectName("ActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(Space.lg, Space.sm, Space.lg, Space.sm)
        layout.setSpacing(Space.sm)

        self.status = Caption("")
        self.status.setWordWrap(True)
        save = AccentButton("Save changes")
        save.clicked.connect(self._save)
        reload_button = GhostButton("Reload")
        reload_button.clicked.connect(self._reload)

        layout.addWidget(self.status, 1)
        layout.addWidget(reload_button)
        layout.addWidget(save)
        self.layout().addWidget(bar)
        self.action_bar = bar

    def _save(self):
        from kayra.core.config import write_env_values

        updates = {}
        for env_key, (control, kind) in self._controls.items():
            if kind == "bool":
                updates[env_key] = "True" if control.isChecked() else "False"
            elif kind in ("model", "voice", "language"):
                updates[env_key] = str(control.currentData() or control.currentText()).strip()
            elif kind == "choice":
                updates[env_key] = control.currentText().strip()
            else:
                updates[env_key] = control.text().strip()

        # The device dropdown is a live control AND a persisted one: it applies immediately
        # through the running engine and is written here so the choice survives a restart.
        if getattr(self, "device_combo", None) is not None:
            updates["TTS_DEVICE_MODE"] = self.device_combo.currentData()

        previous = {key: self._current(key) for key in updates}

        if write_env_values(updates):
            # ONE announcement, from the ONE recorder. The settings screen does not print a
            # line of its own: the recorder owns setting-change logging, skips the unchanged
            # (this writes every control on Save whether or not it was touched) and refuses to
            # print the value of anything it classifies as a credential.
            from kayra.core.settings_log import get_settings_recorder
            get_settings_recorder().record_many(updates, previous)
            # Keys: only what was actually typed (an untouched field never erases a stored
            # key), each through the keyring-or-file store, applied to the engine at once.
            saved, failed = [], []
            for provider, ui in getattr(self, "_provider_ui", {}).items():
                typed = ui["field"].text().strip()
                if not typed:
                    continue
                outcome = self.bridge.save_provider_key(provider, typed)
                (saved if outcome.get("where") else failed).append(provider)
                if outcome.get("where"):
                    ui["field"].clear()
            # Model ids apply NOW too: the engine re-reads them and rebuilds its clients.
            try:
                self.bridge.reload_providers()
            except Exception:
                pass
            self._refresh_routing()
            # And the voice: the running speech engine switches at the next sentence.
            if "ASSISTANT_VOICE" in updates:
                self.bridge.apply_voice(updates["ASSISTANT_VOICE"])
            self._refresh_key_status()
            if failed:
                self.status.setText("Saved, but the key for " + ", ".join(failed)
                                    + " could not be stored.")
            elif saved:
                self.status.setText("Saved. " + ", ".join(saved) + " key in use now; voice "
                                    "changes apply at the next start.")
            else:
                self.status.setText("Saved. Models apply now; voice changes at the next start.")
        else:
            self.status.setText("Could not write .env — check file permissions.")

    def _reload(self):
        from kayra.core.config import reset_cache
        reset_cache()
        for env_key, (control, kind) in self._controls.items():
            value = self._current(env_key)
            if kind == "bool":
                control.setChecked(str(value).lower() in ("true", "1", "yes", "on"))
            elif kind == "choice":
                if value:
                    control.setCurrentText(value)
            elif kind in ("model", "voice", "language"):
                index = control.findData(value or self.DEFAULT_MODELS.get(env_key, ""))
                if index >= 0:
                    control.setCurrentIndex(index)
            else:
                control.setText(str(value))
        self._refresh_key_status()
        self.status.setText("Reloaded from .env.")

    def on_show(self):
        # The gesture controls may have been changed by voice, or by Home, while this screen
        # was hidden. `gestureStateChanged` fires on transitions only, so a screen returning to
        # view has to take one synchronous read — the same rule Home follows for the microphone.
        self._refresh_gesture()
        self.proactive_toggle.blockSignals(True)
        self.proactive_toggle.setChecked(self.bridge.proactive_enabled())
        self.proactive_toggle.blockSignals(False)
        self._sync_presence()
        self._refresh_backend()
        self._refresh_device()
        self._refresh_key_status()
        self._refresh_routing()
        self._gpu_timer.start()

    def _sync_presence(self):
        """
        Re-reads the LIVE service so the screen cannot show a stale switch.

        Signals are blocked around each write: setting a checkbox from the service's own
        value would otherwise emit `toggled` and write that value straight back, which turns
        a read into a redundant service call on every navigation.
        """
        enabled = self.bridge.presence_enabled()
        self.presence_toggle.blockSignals(True)
        self.presence_toggle.setChecked(enabled)
        self.presence_toggle.blockSignals(False)

        live = self.bridge.presence_categories()
        for category, control in getattr(self, "_presence_controls", {}).items():
            if category in live:
                control.blockSignals(True)
                control.setChecked(bool(live[category]))
                control.blockSignals(False)
            control.setEnabled(bool(enabled))

    def on_hide(self):
        # The only timer on this screen, stopped the moment it is not visible. Nothing else
        # here polls, and the GPU sampler behind it parks itself once requests stop arriving.
        self._gpu_timer.stop()
