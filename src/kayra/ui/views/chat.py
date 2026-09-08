# ┌────────────────────────────────────────────────────────────────────────┐
# │                              chat.py                                   │
# │                     The Conversation Interface                         │
# └────────────────────────────────────────────────────────────────────────┘
"""
Typed and spoken conversation in one transcript.

WHY BOTH SOURCES SHARE ONE TRANSCRIPT
-------------------------------------
Voice and text are the same conversation to the user, and they are the same turn to the
backend — both go through the identical emotion → DMM → Execute_Task pipeline. Splitting them
into separate histories would be a presentation invention with no basis in what the system
actually does, and it would make a mixed session unreadable.

Spoken turns are marked, because knowing whether Kayra heard you correctly is the single most
useful thing this screen can tell a voice user.

STREAMING
---------
Kayra's replies arrive one spoken sentence at a time from the session's output tap, so the
assistant's bubble is created empty and extended as sentences land. That is why an assistant
turn is one growing bubble rather than a stack of fragments.

A turn is considered finished when the assistant state leaves SPEAKING/PROCESSING. There is no
end-of-reply event to subscribe to, and inventing one would mean changing the services; the
state machine already carries the information.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLineEdit, QSizePolicy, QFrame,
)

from kayra.ui.theme import Color, Space, Size, Motion, repolish
from kayra.ui.components.primitives import (
    Card, Caption, StatusPill, AccentButton, GhostButton, EmptyState, Divider,
    IconButton, _label,
)
from kayra.ui.components.chat_items import MessageRow, AutomationTrace, ThinkingRow
from kayra.ui.views.base import View


class ChatView(View):
    title = "Chat"
    subtitle = "Type or speak — both go through the same pipeline."

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        # Chat owns its own scrolling transcript, so the page-level scroll area is unused.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._current_reply = None      # the assistant bubble currently being extended
        self._current_trace = None      # the automation trace for the turn in flight
        self._thinking = None
        self._empty = None
        # Follow the newest message until the user scrolls up to read something.
        self._following = True

        self._build_transcript()
        self._build_composer()

        # One shared timer drives the thinking indicator. It runs ONLY while a reply is
        # pending, so an idle chat screen has no timer at all.
        self._think_timer = QTimer(self)
        self._think_timer.setInterval(90)
        self._think_timer.timeout.connect(self._animate_thinking)

        bridge.userMessage.connect(self._on_user_message)
        bridge.assistantMessage.connect(self._on_assistant_message)
        bridge.systemMessage.connect(self._on_system_message)
        bridge.errorOccurred.connect(self._on_error)
        bridge.intentClassified.connect(self._on_intent)
        bridge.stateChanged.connect(self._on_state)
        bridge.listeningChanged.connect(self._on_listening)
        bridge.bootFinished.connect(self._on_boot_finished)

    # ──────────────────────────────────────────────────────────────────
    #                            TRANSCRIPT
    # ──────────────────────────────────────────────────────────────────

    def _build_transcript(self):
        self.transcript_scroll = QScrollArea()
        self.transcript_scroll.setWidgetResizable(True)
        self.transcript_scroll.setFrameShape(QFrame.NoFrame)
        self.transcript_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        holder = QWidget()
        self.transcript = QVBoxLayout(holder)
        self.transcript.setContentsMargins(0, 0, Space.sm, Space.md)
        self.transcript.setSpacing(Space.xs)
        # The stretch goes FIRST so messages settle against the bottom of the viewport, the way
        # a conversation does. With it at the end, a short exchange floated at the top of a
        # mostly-empty page and read as though the screen had failed to load.
        self.transcript.addStretch(1)
        self.transcript_scroll.setWidget(holder)

        # The empty state lives permanently in the layout and is HIDDEN rather than removed.
        # Deleting it on the first message means it can never come back — and `Clear` then
        # leaves a blank rectangle that is indistinguishable from a screen that failed to
        # paint. Visibility is also free; reparenting is not.
        self._empty = EmptyState(
            "Nothing said yet",
            "Ask Kayra a question, or tell it to do something on your machine.")
        self.transcript.addWidget(self._empty)

        self.transcript_scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # A window resize re-wraps every bubble and changes the content height. Following has
        # to survive that, or the newest message drifts out of view whenever the window moves.
        self.transcript_scroll.viewport().installEventFilter(self)

        self.content.addWidget(self.transcript_scroll, 1)

    def _build_composer(self):
        """
        The composer reads as ONE object: a bar that contains the field and its controls.

        Previously these were three separate widgets sitting next to each other — a bordered
        input, a filled Send button and a ghost Stop — which gave the bottom of the screen
        three competing rectangles and no clear anchor. Wrapping them in a single surface is
        what makes it read as a place to type rather than a row of controls.

        The microphone is a STATUS indicator, not a push-to-talk switch. Kayra's microphone is
        always open; a button that appeared to arm it would be lying about how the assistant
        works. It shows whether voice input is live and routes a click to the same place the
        orb does.
        """
        self.composer = QWidget()
        self.composer.setObjectName("Composer")
        self.composer.setAttribute(Qt.WA_StyledBackground, True)
        self.composer.setFixedHeight(Size.control + 2 * Space.xs + 2)
        layout = QHBoxLayout(self.composer)
        layout.setContentsMargins(Space.sm, Space.xs, Space.xs, Space.xs)
        layout.setSpacing(Space.xs)

        self.mic_button = IconButton("mic", "Voice input", checkable=True)
        self.mic_button.setCheckable(False)
        self.mic_button.clicked.connect(self._on_mic_clicked)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Message Kayra…")
        self.input.returnPressed.connect(self._submit)
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._on_text_changed)

        self.send_button = AccentButton("Send")
        self.send_button.setMinimumWidth(76)
        self.send_button.clicked.connect(self._submit)
        self.send_button.setEnabled(False)

        self.stop_button = IconButton("stop", "Stop speaking  (Ctrl+.)")
        self.stop_button.clicked.connect(self.bridge.interrupt)
        self.stop_button.setEnabled(False)

        layout.addWidget(self.mic_button)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.send_button)

        self.content.addWidget(self.composer)

        self.voice_note = Caption("")
        self.voice_note.setVisible(False)
        self.content.addWidget(self.voice_note)

    def _on_text_changed(self, text):
        # An accent-filled button that does nothing is the loudest dead control on a screen.
        self.send_button.setEnabled(bool(text.strip()))

    def _on_mic_clicked(self):
        """
        Toggles listening.

        The microphone button in the composer is the same control as Home's "Pause listening"
        — one action, two places, one state. It does NOT arm push-to-talk: Kayra's microphone
        is continuous, and a button that appeared to hold it open would misrepresent how the
        assistant works.
        """
        if not self.bridge.voice_available():
            self._on_system_message(
                "Voice input is not available in this session — typing still works.", "warning")
            return
        self.bridge.set_listening(not self.bridge.listening_enabled())

    def _on_listening(self, listening):
        """
        The composer must never suggest Kayra can hear you when the microphone is closed.

        The placeholder carries it as well as the icon: someone typing is looking at the field,
        not at the button beside it.
        """
        self.mic_button.setProperty("paused", not listening)
        self.mic_button.set_icon("mic" if listening else "mic_off")
        self.mic_button.setToolTip("Pause listening" if listening else "Start listening")
        repolish(self.mic_button)
        self.input.setPlaceholderText(
            "Message Kayra…" if listening else "Message Kayra…  (listening paused)")
        if not listening:
            # The property is what colours the button; the live LISTENING highlight would
            # otherwise linger from before the pause.
            self.mic_button.setProperty("listening", False)
            repolish(self.mic_button)

    # ──────────────────────────────────────────────────────────────────
    #                             MESSAGES
    # ──────────────────────────────────────────────────────────────────

    def _add_row(self, widget):
        if self._empty is not None:
            self._empty.setVisible(False)
        # Appended after the leading stretch, which keeps content bottom-aligned.
        self.transcript.addWidget(widget)
        self._follow()
        return widget

    # ──────────────────────────────────────────────────────────────────
    #                        FOLLOW-THE-LATEST
    # ──────────────────────────────────────────────────────────────────
    # WHAT WAS WRONG. Appending a widget and calling `bar.setValue(bar.maximum())` on a
    # zero-timer does not work, and the reason is Qt's layout timing rather than the delay
    # being too short. When the deferred call runs, the new widget has been laid out but the
    # scroll area's `maximum` is computed from the content widget's size hint, which is only
    # recalculated when the layout is activated. So `maximum` is still the value from BEFORE
    # the message arrived, the bar goes to a stale bottom, and the newest message sits
    # partially below the viewport. It is worse for exactly the messages that matter most:
    # a tall reply moves `maximum` further, so the taller the message the more of it is cut
    # off. A longer timer only makes it intermittent, which is why "add a delay" is not a fix.
    #
    # The sequence that IS correct, and why each step is load-bearing:
    #
    #   1. `adjustSize()` on the content widget    -> its size hint is recomputed
    #   2. `activate()` on the layout              -> children get their final geometry
    #   3. `setValue(maximum)`                     -> now maximum reflects the new content
    #   4. one deferred repeat on the next turn    -> catches anything that grows AFTER the
    #                                                 first pass: a word-wrapped label whose
    #                                                 height depends on the width it is finally
    #                                                 given, a streamed sentence appended to an
    #                                                 existing bubble, a font metric resolved
    #                                                 late. This is a correction, not the
    #                                                 mechanism — step 3 is already right in
    #                                                 the common case.
    #
    # There is no sleep anywhere, and no pixel offset is guessed.

    # How close to the bottom still counts as "at the bottom". One line of text: enough to
    # survive a stray wheel notch or a trackpad's inertia, small enough that a deliberate
    # scroll away is respected immediately.
    FOLLOW_THRESHOLD_PX = 48

    def _at_bottom(self):
        bar = self.transcript_scroll.verticalScrollBar()
        return (bar.maximum() - bar.value()) <= self.FOLLOW_THRESHOLD_PX

    def _follow(self, force=False):
        """
        Scrolls to the newest content — unless the user has deliberately scrolled up.

        `force` is for the cases where the user's own action created the content (sending a
        message, switching to the screen): they are not reading history, they are watching for
        a reply, and pinning them to the bottom is what they expect.
        """
        if not (force or self._following):
            return
        self._scroll_to_end()
        # One deferred correction for content whose height is not final on the first pass.
        QTimer.singleShot(0, self._scroll_to_end_deferred)

    def _scroll_to_end(self):
        """Recompute the layout, THEN move the bar. The order is the whole fix."""
        holder = self.transcript_scroll.widget()
        if holder is not None:
            layout = holder.layout()
            if layout is not None:
                layout.activate()
            holder.adjustSize()
        bar = self.transcript_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _scroll_to_end_deferred(self):
        # Only if still following: the user may have scrolled up in the intervening event
        # loop turn, and yanking them back is the behaviour this whole section avoids.
        if self._following:
            self._scroll_to_end()

    def _on_scrolled(self, value):
        """
        Tracks whether the user is reading history.

        Driven by the scrollbar's own signal rather than by wheel/key events, so it is correct
        however the position changed — wheel, drag, keyboard, or the programmatic scroll above
        (which lands at the bottom and therefore re-arms following, exactly as it should).
        """
        self._following = self._at_bottom()

    def _submit(self):
        text = self.input.text().strip()
        if not text:
            return
        if not self.bridge.ready:
            self._on_system_message("Kayra is still starting up.", "warning")
            return
        self.input.clear()
        # The user just spoke: they are waiting for an answer, not reading history.
        self._following = True
        self.bridge.submit_text(text)

    def _on_user_message(self, text, source):
        meta = "spoken" if source == "voice" else None
        self._add_row(MessageRow("user", text, meta))
        self._current_reply = None
        self._show_thinking()

    def _on_assistant_message(self, text):
        self._hide_thinking()
        if self._current_reply is None:
            self._current_reply = self._add_row(MessageRow("assistant", text))
        else:
            # A streamed sentence makes an EXISTING bubble taller. No widget is added, so the
            # scroll range still changes and the same follow rule has to run.
            self._current_reply.append_text(text)
            self._follow()

    def _on_system_message(self, text, tone):
        self._add_row(MessageRow("system", text, show_time=False))

    def _on_error(self, message):
        self._hide_thinking()
        self._add_row(MessageRow("error", message, show_time=False))

    def _on_intent(self, text, tokens):
        """
        Renders an automation trace for the machine-control part of a turn.

        Only the ACTION tokens are shown. Conversational routing (`general …`, `realtime …`)
        is not something the user asked to see — the reply is the evidence — and printing raw
        DMM tokens for it would be exactly the "internal logs in the chat" the brief rules out.
        """
        actions = [t for t in tokens
                   if not t.strip().lower().startswith(
                       ("general ", "realtime ", "deep research ", "proactive ", "exit"))]
        if not actions:
            return
        steps = [(_humanise(token), "pending") for token in actions]
        self._current_trace = self._add_row(AutomationTrace("Actions", steps))

    def _on_state(self, state, previous):
        busy = state in ("PROCESSING", "SPEAKING", "AUTOMATING")
        self.stop_button.set_enabled_look(busy)
        # Only highlight the microphone when it is genuinely open AND capturing.
        self.mic_button.setProperty(
            "listening", state == "LISTENING" and self.bridge.listening_enabled())
        repolish(self.mic_button)
        if state == "LISTENING" and previous in ("SPEAKING", "PROCESSING", "AUTOMATING"):
            self._current_reply = None      # the turn ended; the next reply starts a new bubble
            if self._current_trace is not None:
                self._current_trace.complete_all("ok")
                self._current_trace = None
        if not busy:
            self._hide_thinking()

    def _on_boot_finished(self, ok, detail):
        if not ok:
            self._on_error(f"Kayra could not start: {detail}")
            return
        if not self.bridge.tts_available():
            # Stated rather than silently degraded: the transcript is captured from the speech
            # stream, so without speech output there is nothing to capture.
            self.voice_note.setText(
                "Speech output is unavailable, so Kayra's replies are shown on the console "
                "rather than here.")
            self.voice_note.setVisible(True)
        elif not self.bridge.voice_available():
            self.voice_note.setText("Voice input is unavailable — typing still works normally.")
            self.voice_note.setVisible(True)
        self.mic_button.set_enabled_look(self.bridge.voice_available())

    # ──────────────────────────────────────────────────────────────────
    #                        THINKING INDICATOR
    # ──────────────────────────────────────────────────────────────────

    def _show_thinking(self):
        if self._thinking is not None:
            return
        self._thinking = self._add_row(ThinkingRow())
        self._think_timer.start()

    def _hide_thinking(self):
        self._think_timer.stop()
        if self._thinking is not None:
            self._thinking.setParent(None)
            self._thinking.deleteLater()
            self._thinking = None

    def _animate_thinking(self):
        if self._thinking is not None:
            self._thinking.advance()

    # ──────────────────────────────────────────────────────────────────
    #                            LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if (event.type() == QEvent.Resize
                and watched is self.transcript_scroll.viewport()):
            # Re-wrapping changes every bubble's height; hold the bottom if we were there.
            self._follow()
        return super().eventFilter(watched, event)

    def on_show(self):
        self._on_listening(self.bridge.listening_enabled())
        self.input.setFocus()
        # Entering the screen always lands on the newest message.
        self._following = True
        self._follow(force=True)

    def on_hide(self):
        self._think_timer.stop()


def _humanise(token):
    """
    Turns a DMM token into something a person would say.

    Kept small and honest: the token vocabulary is stable and short, so a mapping is more
    truthful than trying to prettify arbitrary strings. Anything unmapped is shown with its
    payload intact rather than hidden.
    """
    token = token.strip()
    lowered = token.lower()
    for prefix, phrasing in (
        ("open ", "Open {}"),
        ("close ", "Close {}"),
        ("play ", "Play {}"),
        ("google search ", "Search the web for {}"),
        ("youtube search ", "Search YouTube for {}"),
        ("content ", "Write {}"),
        ("system ", "System: {}"),
    ):
        if lowered.startswith(prefix):
            return phrasing.format(token[len(prefix):].strip() or "…")
    return token[:1].upper() + token[1:]
