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

from kayra.ui.theme import Space, Size, repolish
from kayra.ui.components.primitives import (
    Caption, AccentButton, EmptyState, IconButton,
)
from kayra.ui.components.chat_items import MessageRow, AutomationTrace, ThinkingRow
from kayra.ui.views.base import View


class ChatView(View):
    # NO PAGE TITLE. The window chrome already names the screen, and a 20px "Chat" heading
    # above a transcript spends the most valuable row on the page restating what the user just
    # clicked. The transcript starts at the top, which is what makes this read as a native
    # assistant application rather than as a documentation page with a chat box on it.
    title = ""
    subtitle = ""

    def __init__(self, bridge, parent=None):
        super().__init__(bridge, parent)

        # Chat owns its own scrolling transcript, so the page-level scroll area is unused.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = self.scroll.widget()
        if holder is not None:
            holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        # THE READABLE COLUMN, AND THE DOCK'S CLEARANCE.
        #
        # Chat is full-bleed now that the rail is gone, and "use the width" does NOT mean
        # running text edge to edge: a 2560px monitor would give ~280 characters a line, which
        # nobody can read. The transcript is centred in a bounded column instead, and the extra
        # width becomes margin — which is what every application people actually read in does.
        #
        # The bottom margin clears the floating dock. The dock is an overlay owned by the
        # window and is not in this layout, so without this the composer would sit underneath
        # it — the one collision this page can have.
        self.content.setContentsMargins(
            Space.lg, Space.base, Space.lg,
            Size.dock_height + Size.dock_margin_bottom + Space.md)
        self.content.setSpacing(Space.sm)

        self._current_reply = None      # the assistant bubble currently being extended
        self._current_trace = None      # the automation trace for the turn in flight
        self._thinking = None
        self._empty = None
        # Follow the newest message until the user scrolls up to read something.
        self._following = True

        # ONE column holds both the transcript and the composer, so their left and right
        # edges line up exactly. Before, each was added to the page layout separately and the
        # scroll area's own bar width offset one of them by ten pixels — visible as a composer
        # that did not quite sit under the messages.
        self.column = QVBoxLayout()
        self.column.setSpacing(Space.sm)
        self.column.setContentsMargins(0, 0, 0, 0)
        self._build_transcript()
        self._build_composer()

        centred = QHBoxLayout()
        centred.setContentsMargins(0, 0, 0, 0)
        centred.addStretch(1)
        centred.addLayout(self.column, 0)
        centred.addStretch(1)
        self.content.addLayout(centred, 1)

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
        # NO SIDE MARGINS. An 8px right inset for the scrollbar left the bubbles ending eight
        # pixels short of the composer beneath them, which is exactly the kind of near-miss
        # that reads as sloppy without being obviously wrong. The scrollbar is narrow and
        # only present when the transcript overflows; letting it sit over the gutter costs
        # nothing and keeps the two edges of the column identical.
        self.transcript.setContentsMargins(0, Space.md, 0, Space.md)
        # ROOMIER. Turns were `Space.xs` apart, which put a user message and Kayra's reply
        # closer together than the two lines inside a single bubble — so a long exchange read
        # as one wall of text. At `Space.md` the eye separates turns without the transcript
        # feeling sparse.
        self.transcript.setSpacing(Space.md)
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
            "Say something to Kayra",
            "Ask a question, or tell it to do something on your machine — "
            "\u201copen YouTube\u201d, \u201cwhat is on my calendar\u201d, "
            "\u201cclose this window\u201d. Typing and speaking go through the same pipeline.")
        self.transcript.addWidget(self._empty)

        self.transcript_scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # A window resize re-wraps every bubble and changes the content height. Following has
        # to survive that, or the newest message drifts out of view whenever the window moves.
        self.transcript_scroll.viewport().installEventFilter(self)

        self.column.addWidget(self.transcript_scroll, 1)

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
        self.composer.setFixedHeight(Size.control_lg + 2 * Space.sm)
        layout = QHBoxLayout(self.composer)
        layout.setContentsMargins(Space.md, Space.sm, Space.sm, Space.sm)
        layout.setSpacing(Space.sm)

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

        self.column.addWidget(self.composer)

        self.voice_note = Caption("")
        self.voice_note.setVisible(False)
        self.voice_note.setAlignment(Qt.AlignHCenter)
        self.column.addWidget(self.voice_note)

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
        # A NEW USER TURN DEFINITIVELY ENDS THE PREVIOUS REPLY. `_on_state` normally clears
        # the streaming mark when the assistant leaves SPEAKING, but a user who types again
        # before that transition arrives would leave the old bubble wearing an accent edge
        # that says "still arriving" for the rest of the session.
        if self._current_reply is not None:
            self._current_reply.set_streaming(False)
        self._add_row(MessageRow("user", text, meta))
        self._current_reply = None
        self._show_thinking()

    def _on_assistant_message(self, text):
        self._hide_thinking()
        if self._current_reply is None:
            self._current_reply = self._add_row(MessageRow("assistant", text))
            # A REPLY STILL ARRIVING LOOKS DIFFERENT FROM A FINISHED ONE, and it costs no
            # timer: a dynamic property plus one repolish paints an accent edge on the bubble,
            # and `_on_state` clears it when the turn ends. An animated caret would need a
            # timer per bubble, which is the cost this UI does not pay for decoration.
            self._current_reply.set_streaming(True)
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
            if self._current_reply is not None:
                self._current_reply.set_streaming(False)
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
        # The composer's microphone has the same boot-ordering problem Home's control had:
        # this view is constructed before the session boots, `listening_changed` never fires
        # for a value that never changes, and the button would keep whatever it was painted
        # with during the boot window. Re-read the moment there is something to read.
        self._on_listening(self.bridge.listening_enabled())

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

    def resizeEvent(self, event):
        """
        Bounds the conversation column, responsively.

        The column takes the width it is given up to `Size.chat_max`, and on a narrow window
        it simply shrinks — there is no fixed pixel position anywhere, so the same layout
        holds from the 1040px minimum to an ultrawide monitor. Above the maximum the surplus
        becomes margin rather than longer lines.
        """
        super().resizeEvent(event)
        available = max(320, self.width() - 2 * Space.lg)
        width = min(Size.chat_max, available)
        self.transcript_scroll.setFixedWidth(width)
        self.composer.setFixedWidth(width)
        self.voice_note.setFixedWidth(width)
        # Bubbles wrap at a fraction of the column, not at a constant. On a narrow window a
        # 560px bubble would be the whole width and the asymmetry that distinguishes the two
        # speakers would be lost.
        MessageRow.set_available_width(width)

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
