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
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox, QSizePolicy, QMessageBox,
)

from kayra.ui.theme import Color, Space, Size, repolish
from kayra.ui.components.primitives import (
    Card, Caption, Secondary, StatusPill, AccentButton, GhostButton, Toggle,
    SectionLabel, Divider, RowRule, _label,
)
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

    def __init__(self, name, help_text, control, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, Space.sm, 0, Space.sm)
        layout.setSpacing(Space.lg)

        text = QVBoxLayout()
        text.setSpacing(1)
        title = _label(name, "SettingName")
        text.addWidget(title)
        if help_text:
            help_label = _label(help_text, "SettingHelp", wrap=True)
            help_label.setMaximumWidth(Size.settings_label_max)
            text.addWidget(help_label)

        # One column, one width, one right edge — for every control type.
        holder = QWidget()
        holder.setFixedWidth(Size.control_field)
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(Space.sm)
        holder_layout.addStretch(1)
        holder_layout.addWidget(control)

        layout.addLayout(text, 1)
        layout.addWidget(holder, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.control = control


class SettingsView(View):
    title = "Settings"
    subtitle = "Configuration is stored in .env. Secrets are never displayed."

    # (env key, label, help, kind, options)
    GENERAL = (
        ("ASSISTANT_NAME", "Assistant name", "What Kayra calls itself.", "text", None),
        ("USERNAME", "Your name", "Used when Kayra addresses you.", "text", None),
    )
    VOICE = (
        ("ASSISTANT_VOICE", "Voice", "Kokoro voice model, e.g. am_adam or af_bella.",
         "text", None),
        ("INPUT_LANGUAGE", "Recognition language", "Language code for speech input.",
         "choice", ["en-US", "en-GB", "en-IN", "hi-IN", "es-ES", "fr-FR", "de-DE"]),
        ("STT_BROWSER", "Speech-input browser",
         "Which browser runs recognition. 'auto' picks one that can actually transcribe.",
         "choice", ["auto", "chrome", "edge", "brave", "chromium"]),
    )
    MODELS = (
        ("FORCE_ONLINE", "Always use cloud models",
         "Skip the local model check entirely at startup.", "bool", None),
        ("LOCAL_BASE_URL", "Local model endpoint",
         "An OpenAI-compatible server, e.g. http://localhost:1234/v1.", "text", None),
    )
    KEYS = (
        ("CohereAPIKey", "Cohere",
         "Intent classification. Without it, every request is treated as conversation."),
        ("GROQ_API_KEY", "Groq", "Primary conversational model."),
        ("GEMINI_API_KEY", "Gemini", "Fallback when Groq is rate-limited."),
    )

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._controls = {}
        self._secret_fields = {}

        self._build_group("General", self.GENERAL)
        self._build_group("Voice and speech", self.VOICE)
        self._build_device()
        self._build_group("Models", self.MODELS)
        self._build_keys()
        self._build_proactive()
        self._build_actions()
        self.content.addStretch(1)

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
            note = Caption("Changes apply the next time Kayra starts.")
            note.setContentsMargins(0, Space.sm, 0, 0)
            card.body.addWidget(note)
        self.content.addWidget(card)

    def _make_control(self, env_key, kind, options):
        value = self._current(env_key)
        if kind == "bool":
            control = Toggle(str(value).strip().lower() in ("true", "1", "yes", "on"))
        elif kind == "choice":
            control = QComboBox()
            control.setEditable(False)
            control.addItems(options or [])
            control.setFixedWidth(Size.control_field)
            if value and value in (options or []):
                control.setCurrentText(value)
            elif value:
                control.addItem(value)
                control.setCurrentText(value)
        else:
            control = QLineEdit(str(value))
            control.setFixedWidth(Size.control_field)
        return control

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
        self.device_combo.setFixedWidth(Size.control_field)
        self.device_combo.currentIndexChanged.connect(self._on_device_mode)

        card.body.addWidget(SettingRow(
            "TTS device",
            "Automatic prefers a GPU when one is genuinely usable. GPU requires one and says "
            "so if it falls back. CPU forces the processor.",
            self.device_combo))
        card.body.addWidget(RowRule())

        self.device_status_pill = StatusPill("Checking", "neutral")
        self.device_active = Secondary("Reading the live session\u2026")
        self.device_active.setWordWrap(True)
        self.device_detail = Caption("")
        self.device_detail.setWordWrap(True)
        card.body.addWidget(self.device_active)
        card.body.addWidget(self.device_detail)
        card.add_header_widget(self.device_status_pill)

        card.body.addWidget(RowRule())
        self.gpu_line = Caption("GPU information unavailable")
        self.gpu_line.setWordWrap(True)
        card.body.addWidget(self.gpu_line)

        self.content.addWidget(card)
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

    def _build_keys(self):
        card = Card("API keys")
        card.body.setSpacing(0)
        intro = Caption(
            "Stored in .env and never shown. Type a new value to replace an existing key; "
            "leave a field empty to keep what is already stored.")
        intro.setContentsMargins(0, 0, 0, Space.sm)
        card.body.addWidget(intro)

        for index, (env_key, label, help_text) in enumerate(self.KEYS):
            if index:
                card.body.addWidget(RowRule())
            present = bool(self._current(env_key))
            field = QLineEdit()
            field.setEchoMode(QLineEdit.Password)
            field.setProperty("secret", "true")
            repolish(field)
            field.setFixedWidth(Size.control_field - 66)
            field.setPlaceholderText("Stored — type to replace" if present else "Not set")
            self._secret_fields[env_key] = field

            control = QWidget()
            row = QHBoxLayout(control)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(Space.sm)
            pill = StatusPill("Set" if present else "Missing",
                              "success" if present else "warning")
            pill.setFixedWidth(58)
            row.addWidget(pill)
            row.addWidget(field)

            card.body.addWidget(SettingRow(label, help_text, control))
        self.content.addWidget(card)

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
        self.content.addWidget(card)

    def _build_actions(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Space.sm)

        self.status = Caption("")
        save = AccentButton("Save changes")
        save.clicked.connect(self._save)
        reload_button = GhostButton("Reload")
        reload_button.clicked.connect(self._reload)

        layout.addWidget(self.status, 1)
        layout.addWidget(reload_button)
        layout.addWidget(save)
        self.content.addWidget(row)

    # ──────────────────────────────────────────────────────────────────

    def _save(self):
        from kayra.core.config import write_env_values

        updates = {}
        for env_key, (control, kind) in self._controls.items():
            if kind == "bool":
                updates[env_key] = "True" if control.isChecked() else "False"
            elif kind == "choice":
                updates[env_key] = control.currentText().strip()
            else:
                updates[env_key] = control.text().strip()

        # The device dropdown is a live control AND a persisted one: it applies immediately
        # through the running engine and is written here so the choice survives a restart.
        if getattr(self, "device_combo", None) is not None:
            updates["TTS_DEVICE_MODE"] = self.device_combo.currentData()

        # Secrets: only send what was actually typed. An untouched field must never overwrite
        # a stored key with an empty string.
        for env_key, field in self._secret_fields.items():
            typed = field.text().strip()
            if typed:
                updates[env_key] = typed

        if write_env_values(updates):
            for env_key, field in self._secret_fields.items():
                if field.text().strip():
                    field.clear()
                    field.setPlaceholderText("Stored — type to replace")
            self.status.setText("Saved. Restart Kayra for model and voice changes to apply.")
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
            else:
                control.setText(str(value))
        self.status.setText("Reloaded from .env.")

    def on_show(self):
        self.proactive_toggle.blockSignals(True)
        self.proactive_toggle.setChecked(self.bridge.proactive_enabled())
        self.proactive_toggle.blockSignals(False)
        self._refresh_device()
        self._gpu_timer.start()

    def on_hide(self):
        # The only timer on this screen, stopped the moment it is not visible. Nothing else
        # here polls, and the GPU sampler behind it parks itself once requests stop arriving.
        self._gpu_timer.stop()
