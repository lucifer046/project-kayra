# ┌────────────────────────────────────────────────────────────────────────┐
# │                             system.py                                  │
# │       Device, Capability Analysis, Live Resources & System Guide       │
# └────────────────────────────────────────────────────────────────────────┘
"""
The System screen: what this machine is, how well it suits Kayra, and what is limiting it.

THREE SCORES, NOT ONE
---------------------
Compatibility, performance and readiness are scored and shown SEPARATELY, because collapsing
them destroys the only information a user can act on. A powerful workstation with no API key
is perfectly capable and completely unready; a modest laptop that is fully configured is
completely ready and merely slow. A single blended percentage describes neither, and worse, it
gives no clue which one to fix.

Every score is the mean of named checks, and the checks are listed under it. Nothing here is a
number the user has to take on faith, and nothing is invented: values that cannot be read
reliably — GPU utilisation, and VRAM on a card whose 32-bit WMI field has saturated — are shown
as unavailable rather than estimated.

COST
----
Static facts are collected ONCE by `system_profile.device_profile()`, on a worker thread, and
cached for the process lifetime. That collection makes a single batched WMI call and takes
around three seconds, which is why it must never happen on the GUI thread — this screen shows
a placeholder and fills in when the worker returns.

Live metrics are psutil-only, refresh every two seconds, and ONLY while this screen is visible.
No subprocess is ever spawned on the refresh path.
"""

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy, QGridLayout

from kayra.ui.theme import Font, Color, Space, Motion, VERDICT_COLORS, VERDICT_LABELS
from kayra.ui.components.primitives import (
    Card, Caption, Secondary, StatusPill, SectionLabel, Meter, StatRow, EmptyState,
    Disclosure, ActionStatus,
    Divider, GhostButton, _label,
)
from kayra.ui.views.base import View


SCORE_EXPLANATIONS = {
    "compatibility": "Can Kayra operate correctly on this device?",
    "performance": "How comfortably can this device run Kayra?",
    "readiness": "Is this installation configured and ready right now?",
}


# Loaders currently in flight. This module-level reference is what makes closing the window
# during an analysis safe, and it is not decoration — see `_AnalysisLoader`.
_IN_FLIGHT = set()


class _AnalysisLoader(QObject):
    """
    Runs the device analysis off the GUI thread and delivers it as a queued signal.

    `analysis()` is not free: it collects the static profile, enumerates audio devices through
    `sounddevice` (~150 ms) and probes for a browser with a speech backend. On the GUI thread
    that is a visible freeze on entry; per visit it is a repeated one. It runs once, here, and
    `system_profile` caches the result for the process.

    The hardware half of that used to dominate it — one batched PowerShell/CIM call measured
    at 4.41 s. `core.hardware` reads the same facts from the registry in under a millisecond,
    so this thread now exists for the audio and browser probes rather than for the WMI call.

    WHY NOT QThread. The obvious implementation — a QObject moved onto a QThread parented to the
    view — crashes when the view is destroyed while the worker is still running: Qt destroys the
    parented QThread, prints "QThread: Destroyed while thread is still running" and aborts the
    process. That is not a theoretical case; it is what happens when someone opens System and
    closes the window within three seconds, and it showed up immediately in the test suite.

    A plain daemon thread plus a standalone emitter avoids the lifetime problem entirely:

      * the emitter is NOT parented to the view, so the view can be destroyed freely;
      * Qt disconnects the view's slot automatically when the view dies, so a late result is
        simply dropped instead of delivered to freed memory;
      * `_IN_FLIGHT` keeps the emitter alive until the thread has finished emitting, which is
        the one thing garbage collection could otherwise get wrong.
    """

    finished = Signal(object)

    def start(self):
        import threading

        _IN_FLIGHT.add(self)

        def work():
            try:
                from kayra.core.system_profile import analysis
                result = analysis()
            except Exception as exc:               # a worker must never take the app down
                result = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                self.finished.emit(result)
            finally:
                _IN_FLIGHT.discard(self)

        threading.Thread(target=work, name="kayra-ui-profile", daemon=True).start()


class ScoreTile(QWidget):
    """One of the three headline scores, with the question it answers."""

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        # A plain QWidget does NOT paint a stylesheet background or border unless this attribute
        # is set — only QFrame and friends do it implicitly. Without it the tile silently
        # rendered as bare text on the page ground while every QFrame-based Card around it drew
        # correctly, which is the single most common way Qt styling appears to "not work".
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Space.base, Space.md, Space.base, Space.md)
        layout.setSpacing(Space.xxs)

        self.label = SectionLabel(key)
        # A token-sized metric rather than a hand-set point size — every large number in the
        # application is now the same face, weight and size.
        # A dimmed placeholder, not a 34px em-dash. At metric weight the bare "—" rendered as
        # a heavy horizontal bar and read as a broken value rather than as "still measuring".
        self.value = _label(
            f"<span style='color:{Color.text_disabled}'>—</span>", "MetricLarge")
        self.value.setTextFormat(Qt.RichText)
        self.grade = StatusPill("Checking", "neutral")
        self.question = Caption(SCORE_EXPLANATIONS.get(key, ""))
        self.question.setWordWrap(True)

        layout.addWidget(self.label)
        layout.addSpacing(Space.xs)
        layout.addWidget(self.value)
        layout.addSpacing(Space.xs)
        layout.addWidget(self.grade, alignment=Qt.AlignLeft)
        layout.addSpacing(Space.sm)
        layout.addWidget(self.question)

    def set_score(self, score, grade):
        # "/ 100" is set small and tertiary so the NUMBER is what the eye lands on. A score
        # rendered at the same weight as its denominator reads as a fraction, not a verdict.
        self.value.setText(
            f"{score}<span style='font-size:{Font.caption}px;color:{Color.text_tertiary}'>"
            f" / 100</span>")
        self.value.setTextFormat(Qt.RichText)
        tone = "success" if score >= 75 else "warning" if score >= 40 else "danger"
        self.grade.set_status(grade, tone)


class SystemView(View):
    title = "System"
    subtitle = "This device, how well it suits Kayra, and what is limiting it."

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        self._analysis = None
        self._loader = None

        self._build_scores()
        self._build_resources()
        self._build_findings()
        self._build_device()
        self._build_guide()
        self.content.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(Motion.metrics_interval)
        self._timer.timeout.connect(self._refresh_metrics)

    # ──────────────────────────────────────────────────────────────────
    #                              SCORES
    # ──────────────────────────────────────────────────────────────────

    def _build_scores(self):
        row = QHBoxLayout()
        row.setSpacing(Space.base)
        self.tiles = {}
        for key in ("compatibility", "performance", "readiness"):
            tile = ScoreTile(key)
            self.tiles[key] = tile
            row.addWidget(tile)
        self.content.addLayout(row)

    # ──────────────────────────────────────────────────────────────────
    #                         LIVE RESOURCES
    # ──────────────────────────────────────────────────────────────────

    def _build_resources(self):
        card = Card("Live resources")
        self.resource_pill = StatusPill("—", "neutral")
        card.add_header_widget(self.resource_pill)

        grid = QGridLayout()
        grid.setHorizontalSpacing(Space.lg)
        grid.setVerticalSpacing(Space.sm)

        self.cpu_meter = Meter("Processor")
        self.ram_meter = Meter("Memory")
        self.disk_meter = Meter("System drive")
        self.kayra_meter = Meter("Kayra")

        grid.addWidget(self.cpu_meter, 0, 0)
        grid.addWidget(self.ram_meter, 0, 1)
        grid.addWidget(self.disk_meter, 1, 0)
        grid.addWidget(self.kayra_meter, 1, 1)
        card.body.addLayout(grid)

        # Stated plainly rather than filled with a plausible-looking number.
        card.body.addWidget(Caption(
            "GPU utilisation is not shown: it cannot be read reliably without vendor-specific "
            "libraries, and Kayra runs no work on the GPU."))
        self.content.addWidget(card)

    # ──────────────────────────────────────────────────────────────────
    #                            FINDINGS
    # ──────────────────────────────────────────────────────────────────

    def _build_findings(self):
        self.findings_card = Card("Subsystem analysis")
        self.findings_pill = StatusPill("Analysing", "neutral")
        self.findings_card.add_header_widget(self.findings_pill)
        self.findings_body = QVBoxLayout()
        self.findings_body.setSpacing(Space.sm)
        self.findings_card.body.addLayout(self.findings_body)
        self.findings_body.addWidget(Caption("Reading this device…"))
        self.content.addWidget(self.findings_card)

    def _build_device(self):
        self.device_card = Card("Device")
        self.device_body = QVBoxLayout()
        self.device_body.setSpacing(0)
        self.device_card.body.addLayout(self.device_body)
        self.device_body.addWidget(Caption("Reading hardware…"))
        self.content.addWidget(self.device_card)

    # ──────────────────────────────────────────────────────────────────
    #                          SYSTEM GUIDE
    # ──────────────────────────────────────────────────────────────────

    def _build_guide(self):
        """
        The guide as questions the user can open, not a wall of prose.

        Every section used to be printed at once: five headings and five paragraphs, roughly
        900px of continuous text at the bottom of an already-long page. Nobody reads that, and
        it buried the one part that is actually about THIS machine — the advice — under four
        paragraphs of general explanation.

        The advice section is the exception and stays open: it is the answer to "what is
        limiting my setup", which is the question the whole page exists to answer.
        """
        card = Card("System guide")
        card.body.setSpacing(Space.xxs)
        card.body.addWidget(Caption(
            "How Kayra uses this machine, and what actually changes its speed."))
        card.body.addSpacing(Space.xs)

        for heading, text in GUIDE_SECTIONS:
            section = Disclosure(_question_form(heading))
            body = Secondary(text)
            body.setWordWrap(True)
            section.add(body)
            card.body.addWidget(section)

        advice = Disclosure("What would help most on this machine?", expanded=True)
        self.guide_advice = QVBoxLayout()
        self.guide_advice.setSpacing(Space.xxs)
        advice.body.addLayout(self.guide_advice)
        self.guide_advice.addWidget(Caption("Waiting for the device analysis…"))
        self.guide_advice_label = advice.header
        card.body.addWidget(advice)

        self.content.addWidget(card)

    # ──────────────────────────────────────────────────────────────────
    #                             LOADING
    # ──────────────────────────────────────────────────────────────────

    def _start_analysis(self):
        """Kicks off the one-time device analysis. Idempotent: repeated visits cost nothing."""
        if self._analysis is not None or self._loader is not None:
            return
        self._loader = _AnalysisLoader()
        self._loader.finished.connect(self._on_analysis)
        self._loader.start()

    def _on_analysis(self, result):
        if not isinstance(result, dict) or "error" in result:
            self.findings_pill.set_status("Unavailable", "danger")
            self._replace(self.findings_body, [Secondary(
                f"The device could not be analysed: {result.get('error', 'unknown error')}")])
            return

        self._analysis = result
        for key, tile in self.tiles.items():
            tile.set_score(result["scores"].get(key, 0), result["grades"].get(key, "—"))

        self._render_findings(result)
        self._render_device(result["profile"])
        self._render_advice(result)

    def _replace(self, layout, widgets):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for widget in widgets:
            layout.addWidget(widget)

    def _render_findings(self, result):
        widgets = []
        for group in ("compatibility", "performance", "readiness"):
            findings = result["groups"].get(group, [])
            if not findings:
                continue
            widgets.append(SectionLabel(group))
            for finding in findings:
                widgets.append(_FindingRow(finding))
        self._replace(self.findings_body, widgets)

        blockers = len(result.get("blockers", []))
        attention = len(result.get("attention", []))
        if blockers:
            self.findings_pill.set_status(f"{blockers} blocking", "danger")
        elif attention:
            self.findings_pill.set_status(f"{attention} to review", "warning")
        else:
            self.findings_pill.set_status("All clear", "success")

    def _render_device(self, profile):
        """
        The device sheet. EVERY value here is read from the profile — there is no literal
        model name, capacity or resolution anywhere in this method, and the test suite
        asserts that by rendering the screen against synthetic machines.
        """
        from kayra.core.system_profile import human_bytes, os_summary

        product, version = os_summary()

        rows = [
            StatRow("Processor", profile["cpu_name"] or "unknown", mono=False),
            StatRow("Cores", f"{profile['cpu_cores'] or '?'} physical / "
                             f"{profile['cpu_threads'] or '?'} logical"),
            StatRow("Base clock", f"{profile['cpu_max_mhz']:.0f} MHz"
                                  if profile["cpu_max_mhz"] else "unknown"),
            StatRow("Memory", human_bytes(profile["ram_total"])),
        ]

        # One row per real adapter, so a switchable-graphics laptop shows both rather than
        # whichever one happened to win a sort. Virtual adapters are omitted: naming the
        # Microsoft Basic Display Adapter as "your graphics" helps nobody.
        real = [g for g in profile.get("gpus") or [] if not g.get("software")]
        if real:
            for adapter in real[:3]:
                label = "Graphics" if adapter is real[0] else "Graphics (also)"
                rows.append(StatRow(label, adapter["name"], mono=False))
                rows.append(StatRow("  Video memory", adapter["memory_text"]))
                if adapter.get("driver_version"):
                    rows.append(StatRow("  Driver", adapter["driver_version"]))
        else:
            rows.append(StatRow("Graphics", "not detected", mono=False))

        rows.extend([
            # The CORRECTED product name and the REAL build. This row used to read
            # "(build 10)" from `platform.release()`, which is 10 on every Windows 11
            # machine — see `core/hardware.py` for why every cheap source lies here.
            StatRow("Operating system", product, mono=False),
            StatRow("OS version", version or "unknown", mono=False),
            StatRow("Architecture", profile["architecture"] or "unknown"),
        ])
        if profile.get("screen_width") and profile.get("screen_height"):
            scale = profile.get("screen_scale") or 1.0
            monitors = profile.get("monitor_count") or 1
            detail = f"{profile['screen_width']} x {profile['screen_height']}"
            if scale and abs(scale - 1.0) > 0.01:
                detail += f" at {scale:g}x"
            if monitors > 1:
                detail += f"  ·  {monitors} monitors"
            rows.append(StatRow("Display", detail))
        rows.extend([
            StatRow("Audio devices", f"{profile['audio_outputs']} output / "
                                     f"{profile['audio_inputs']} input"),
            StatRow("Python", profile["python_version"]),
        ])
        for disk in profile["disks"][:4]:
            label = f"Drive {disk['mount']}"
            if disk.get("system"):
                label += " (system)"
            rows.append(StatRow(
                label,
                f"{human_bytes(disk['free'])} free of {human_bytes(disk['total'])}"))
        self._replace(self.device_body, rows)

    def _render_advice(self, result):
        """
        The guide's actionable half: only findings that carry real advice, worst first.

        An empty list is a good outcome and says so, rather than leaving a blank region that
        looks like a rendering failure.
        """
        advisable = [f for f in result.get("blockers", []) + result.get("attention", [])
                     if f.advice]
        advisable.sort(key=lambda f: f.rank)

        if not advisable:
            self._replace(self.guide_advice, [Secondary(
                "Nothing is holding Kayra back on this machine.")])
            return

        widgets = []
        for finding in advisable[:6]:
            widgets.append(_AdviceRow(finding))
        self._replace(self.guide_advice, widgets)

    # ──────────────────────────────────────────────────────────────────
    #                          LIVE REFRESH
    # ──────────────────────────────────────────────────────────────────

    def _refresh_metrics(self):
        from kayra.core.system_profile import live_metrics, human_bytes

        metrics = live_metrics()
        self.cpu_meter.set_value(metrics["cpu_percent"])
        self.ram_meter.set_value(
            metrics["ram_percent"],
            f"{human_bytes(metrics['ram_used'])} / {human_bytes(metrics['ram_total'])}")
        self.disk_meter.set_value(
            metrics["disk_percent"],
            f"{human_bytes(metrics['disk_total'] - metrics['disk_used'])} free")

        total = metrics["ram_total"] or 1
        share = metrics["kayra_memory"] / total * 100.0
        self.kayra_meter.set_value(
            share,
            f"{human_bytes(metrics['kayra_memory'])} · {metrics['kayra_processes']} proc")

        worst = max(metrics["cpu_percent"], metrics["ram_percent"], metrics["disk_percent"])
        if worst >= 90:
            self.resource_pill.set_status("Under pressure", "warning")
        else:
            self.resource_pill.set_status("Healthy", "success")

    # ──────────────────────────────────────────────────────────────────
    #                            LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def on_show(self):
        self._refresh_metrics()
        self._timer.start()
        self._start_analysis()

    def on_hide(self):
        self._timer.stop()


# `system_profile` verdicts -> the semantic tone the badge uses. Kept next to the row that
# renders it so the mapping is visible where it matters; `theme.VERDICT_COLORS` remains the
# source for anything that needs the colour itself.
_VERDICT_TONE = {
    "READY": "success",
    "GOOD": "success",
    "LIMITED": "warning",
    "REQUIRES_CONFIGURATION": "warning",
    "NOT_AVAILABLE": "danger",
}


class _FindingRow(QWidget):
    """One checked fact: verdict pill, subsystem, value, and the reason underneath."""

    def __init__(self, finding, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, Space.xxs, 0, Space.xxs)
        layout.setSpacing(Space.xxs)

        top = QHBoxLayout()
        top.setSpacing(Space.md)

        # The verdict badge, in the same component the Automation history uses. Previously a
        # `StatusPill`, which on this page rendered as a column of what looked like outlined
        # buttons — "Ready" appeared clickable, and it is not.
        badge = ActionStatus(VERDICT_LABELS.get(finding.verdict, finding.verdict),
                             _VERDICT_TONE.get(finding.verdict, "neutral"))
        holder = QWidget()
        holder.setFixedWidth(124)
        holder_row = QHBoxLayout(holder)
        holder_row.setContentsMargins(0, 0, 0, 0)
        holder_row.setSpacing(0)
        holder_row.addWidget(badge)
        holder_row.addStretch(1)

        # HIERARCHY: subsystem, then status, then explanation. The subsystem is what the row
        # is ABOUT, so it carries the weight; the measured value is secondary; the reason sits
        # underneath in the quietest tier.
        name = _label(finding.subsystem, "SubsystemName")
        value = _label(finding.summary, "Mono")
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        top.addWidget(holder)
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(value)
        layout.addLayout(top)

        reason = finding.advice or finding.detail
        if reason:
            note = Caption(reason)
            note.setWordWrap(True)
            note.setContentsMargins(124 + Space.md, 0, 0, 0)   # aligned under the name column
            layout.addWidget(note)


class _AdviceRow(QWidget):
    def __init__(self, finding, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, Space.xxs, 0, Space.xxs)
        layout.setSpacing(Space.sm)

        tone = "danger" if finding.verdict == "NOT_AVAILABLE" else "warning"
        pill = StatusPill(finding.subsystem, tone)
        pill.setMinimumWidth(140)
        layout.addWidget(pill)
        layout.addWidget(Secondary(finding.advice), 1)


# The guide's headings are written as statements ("WHAT ACTUALLY DETERMINES SPEED"); a
# collapsed section reads better as the question it answers, which is also what tells the user
# whether it is worth opening.
_GUIDE_QUESTIONS = {
    "HOW KAYRA USES THIS MACHINE": "How does Kayra use this machine?",
    "WHAT ACTUALLY DETERMINES SPEED": "What actually determines Kayra's speed?",
    "WHAT IT COSTS WHILE RUNNING": "What does Kayra cost while it is running?",
    "WHAT RAM AFFECTS": "What does RAM affect?",
    "WHAT THE GPU DOES": "How does Kayra use my GPU?",
    "WHAT THE DISK AFFECTS": "What does the disk affect?",
    "WHY LOCAL SPEECH MATTERS": "Why does local speech performance matter?",
}


def _question_form(heading):
    key = str(heading).strip().upper()
    if key in _GUIDE_QUESTIONS:
        return _GUIDE_QUESTIONS[key]
    # Unmapped headings degrade to sentence case rather than shouting.
    return key.capitalize() + "?"


GUIDE_SECTIONS = (
    ("How Kayra uses this machine",
     "Kayra runs entirely on the processor. Speech recognition happens inside a headless "
     "browser session it owns and controls; speech synthesis runs a neural voice model on the "
     "CPU; intent classification is a short network request unless a local model is "
     "configured. Nothing runs on the GPU."),

    ("What actually determines speed",
     "The delay before Kayra starts speaking is dominated by speech synthesis, which is "
     "CPU-bound and runs at roughly real time. More processor threads reduce it; a faster "
     "drive or more memory will not. The quantized voice model is the single change that "
     "measurably improves it."),

    ("What it costs while running",
     "Roughly 470MB and around nine processes with the speech session open — most of that is "
     "the browser doing the listening, not Kayra itself. It sits idle between requests: the "
     "proactive agent's check costs microseconds and runs once every twenty seconds."),

    ("Working offline",
     "With a local model server configured, conversation and intent classification need no "
     "network at all. Speech synthesis is already offline. Live search and deep research are "
     "the only features that genuinely require the internet."),

    ("Privacy",
     "Audio never leaves this process as audio: the browser returns text. Conversations are "
     "only stored when you explicitly ask. Learned routines record counts and times, never "
     "what was said."),
)
