# ┌────────────────────────────────────────────────────────────────────────┐
# │                           voice_state.py                               │
# │        The Authoritative Voice State — one machine, one revision       │
# └────────────────────────────────────────────────────────────────────────┘
"""
What the microphone and the assistant are actually doing, resolved in ONE place.

THE DEFECT THIS FIXES
---------------------
The assistant visual said "Listening paused" while the user was talking to it.

That was not a wrong label; it was four independent writers to one piece of screen. The
ambient panel wrote its caption from `listeningChanged`, Home wrote its own from a cached
`_listening` flag plus a cached `_state`, the sidebar wrote a third from a third pair, and the
orb inferred a fourth from whatever `set_state` reached it last. Each was correct about the
fact it held and none held all of them, so the screen showed whichever writer spoke most
recently — and during an STT recovery, or the instant after a barge-in, that was reliably the
wrong one.

So this module does not patch a label. It resolves the whole question once, from FACTS, and
publishes the ANSWER. Nothing downstream may infer a voice state; it renders the one it is
given.

FACTS IN, STATE OUT
-------------------
`update()` takes only things that are observably true — the runtime's assistant state, whether
listening is paused, whether standby is on, what the STT backend manager reports, whether the
page's VAD currently hears a voice — and returns the single state implied by them. It has no
timers of its own, holds no opinion about what "should" happen next, and cannot be driven into
a state by anything other than a changed fact.

    LISTENING and USER_SPEAKING are different things, and SILENCE IS STILL LISTENING.

That is the rule the old code broke. A microphone that is open and hearing nothing is
LISTENING. It is not paused, it is not stopped, and a late transcript, a slow interim result
or a quiet VAD window does not change it. PAUSED means the microphone is deliberately closed;
nothing else may produce it.

REVISIONS
---------
Every committed transition carries a monotonically increasing revision. A Qt callback that
arrives late — a queued signal delivered after a newer one, a timer that fired during a
switch — is identified by a revision no greater than the one already rendered, and is
DROPPED. Without this, an old asynchronous callback can repaint a state that is no longer
true, which is the second half of the same bug: not just the wrong writer, but the right
writer arriving in the wrong order.

WHERE IT LIVES, AND WHY
-----------------------
`core`, next to `runtime_state`, and it imports nothing but the stdlib. `RuntimeState` answers
"what is the assistant doing?" and `ConversationContext` answers "what is it about?"; this
answers "what should the user be told about the microphone right now?", which is a third
question with a third set of inputs, and folding it into either of the others would put UI
presentation into a module the proactive agent's safety gate reads.
"""

import time
import threading


def _now_ms() -> float:
    return time.time() * 1000.0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE STATES                                │
# └────────────────────────────────────────────────────────────────────────┘

class VoiceState:
    """
    The eleven states the assistant's voice presence can be in.

    Deliberately NOT the same enum as `AssistantState`. That one is the turn machine, whose
    members are about work in progress; this one also has to express things the turn machine
    has no vocabulary for — a closed microphone, standby, a session being rebuilt, and the
    difference between a microphone that is open and a user who is talking into it.
    """

    OFFLINE = "OFFLINE"                        # no speech input in this process
    STARTING = "STARTING"                      # coming up
    LISTENING = "LISTENING"                    # microphone open, nobody talking — the resting state
    USER_SPEAKING = "USER_SPEAKING"            # microphone open, VAD hears a voice
    PROCESSING = "PROCESSING"                  # working out / carrying out what was said
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"  # Kayra is talking
    PAUSED = "PAUSED"                          # microphone deliberately closed
    STANDBY = "STANDBY"                        # asleep; hearing, but acting on nothing
    RECOVERING = "RECOVERING"                  # the speech session is being rebuilt
    STOPPING = "STOPPING"                      # shutdown in progress
    ERROR = "ERROR"                            # speech input unusable


STATES = (VoiceState.OFFLINE, VoiceState.STARTING, VoiceState.LISTENING,
          VoiceState.USER_SPEAKING, VoiceState.PROCESSING, VoiceState.ASSISTANT_SPEAKING,
          VoiceState.PAUSED, VoiceState.STANDBY, VoiceState.RECOVERING,
          VoiceState.STOPPING, VoiceState.ERROR)

# States in which the microphone is genuinely capturing. Note that STANDBY is one of them:
# standby deliberately does NOT close the microphone, because "wake up" is a spoken command
# and a closed microphone cannot hear it.
CAPTURING_STATES = frozenset({
    VoiceState.LISTENING, VoiceState.USER_SPEAKING, VoiceState.PROCESSING,
    VoiceState.ASSISTANT_SPEAKING, VoiceState.STANDBY,
})

# Terminal ONCE SHUTDOWN HAS BEGUN. Nothing returns to LISTENING from either of these after
# that point, which is what makes "the orb never goes back to listening after shutdown begins"
# structural rather than a matter of ordering luck.
#
# The qualifier matters and was learned the hard way: OFFLINE is also the state the machine
# STARTS in, before any speech session exists. Treating it as absorbing unconditionally makes
# the machine unable to boot — it can never leave OFFLINE to reach STARTING. The absorbing
# behaviour is therefore latched by observing `shutting_down`, not by the state's identity.
TERMINAL_STATES = frozenset({VoiceState.STOPPING, VoiceState.OFFLINE})

# What the user is told. PAUSED and STANDBY deliberately share a phrase — from the user's side
# they are the same situation, "Kayra is not acting on what I say" — while remaining separate
# STATES, because the machine has to keep them apart to leave standby correctly.
STATE_TEXT = {
    VoiceState.OFFLINE: "Offline",
    VoiceState.STARTING: "Starting",
    VoiceState.LISTENING: "Listening",
    VoiceState.USER_SPEAKING: "Listening…",
    VoiceState.PROCESSING: "Thinking",
    VoiceState.ASSISTANT_SPEAKING: "Speaking",
    VoiceState.PAUSED: "Listening paused",
    VoiceState.STANDBY: "Listening paused",
    VoiceState.RECOVERING: "Reconnecting…",
    VoiceState.STOPPING: "Shutting down",
    VoiceState.ERROR: "Speech input unavailable",
}

# The second line: what that means for the user, in their terms.
STATE_DETAIL = {
    VoiceState.OFFLINE: "Voice input is not running.",
    VoiceState.STARTING: "Bringing speech input up.",
    VoiceState.LISTENING: "Microphone open.",
    VoiceState.USER_SPEAKING: "Hearing you.",
    VoiceState.PROCESSING: "Working out what you meant.",
    VoiceState.ASSISTANT_SPEAKING: "Say \"stop\" to interrupt.",
    VoiceState.PAUSED: "Kayra is still running. Start listening to talk again.",
    VoiceState.STANDBY: "Say \"wake up\" when you need it.",
    VoiceState.RECOVERING: "Reconnecting the microphone.",
    VoiceState.STOPPING: "Stopping services and closing the browser session.",
    VoiceState.ERROR: "Speech input could not be started.",
}

# The EXISTING orb vocabulary each state renders in. The visual language is deliberately
# unchanged — this milestone makes the semantic mapping truthful, it does not redesign the
# assistant visual. USER_SPEAKING maps to the listening animation, distinguished by amplitude
# rather than by a different form: the ring is already the "I am hearing you" element, and a
# second visual component for the same fact would be one more thing that can disagree.
ORB_STATE = {
    VoiceState.OFFLINE: "OFFLINE",
    VoiceState.STARTING: "STARTING",
    VoiceState.LISTENING: "LISTENING",
    VoiceState.USER_SPEAKING: "LISTENING",
    VoiceState.PROCESSING: "PROCESSING",
    VoiceState.ASSISTANT_SPEAKING: "SPEAKING",
    VoiceState.PAUSED: "IDLE",
    VoiceState.STANDBY: "IDLE",
    VoiceState.RECOVERING: "STARTING",
    VoiceState.STOPPING: "SHUTTING_DOWN",
    VoiceState.ERROR: "ERROR",
}

# Amplitude, 0..1, for the orb's activity envelope. USER_SPEAKING is the loudest listening
# value in the table: that IS the "you are being recorded right now" signal.
ORB_AMPLITUDE = {
    VoiceState.OFFLINE: 0.08,
    VoiceState.STARTING: 0.45,
    VoiceState.LISTENING: 0.72,
    VoiceState.USER_SPEAKING: 1.00,
    VoiceState.PROCESSING: 0.60,
    VoiceState.ASSISTANT_SPEAKING: 0.95,
    VoiceState.PAUSED: 0.18,
    VoiceState.STANDBY: 0.18,
    VoiceState.RECOVERING: 0.50,
    VoiceState.STOPPING: 0.08,
    VoiceState.ERROR: 0.35,
}


class VoiceTransition:
    """One committed change. Immutable, and carries its own revision."""

    __slots__ = ("state", "previous", "revision", "at_ms", "reason")

    def __init__(self, state, previous, revision, at_ms, reason=""):
        self.state = state
        self.previous = previous
        self.revision = revision
        self.at_ms = at_ms
        self.reason = reason

    @property
    def text(self):
        return STATE_TEXT.get(self.state, self.state.title())

    @property
    def detail(self):
        return STATE_DETAIL.get(self.state, "")

    def to_dict(self):
        return {"state": self.state, "previous": self.previous, "revision": self.revision,
                "at_ms": self.at_ms, "reason": self.reason,
                "text": self.text, "detail": self.detail}

    def __repr__(self):
        return f"<VoiceTransition {self.previous}->{self.state} rev={self.revision}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             THE MACHINE                                │
# └────────────────────────────────────────────────────────────────────────┘

class VoiceStateMachine:
    """
    Resolves the voice state from facts, debounces the transient ones, and numbers the result.

    Thread-safe: facts arrive from the turn runner, the local control watcher and the Qt GUI
    thread, and a torn read here would be a wrong state on screen.
    """

    # Minimum time a TRANSIENT state is held before it may fall back to a resting one.
    #
    # These are short on purpose. Their whole job is to absorb one sample of noise — a VAD
    # window that dips between two words, an STT session that comes back in 300ms — and a
    # debounce long enough to be felt would trade a flickering UI for a lying one. Nothing
    # here is over half a second, and the test suite asserts that.
    DWELL_MS = {
        VoiceState.USER_SPEAKING: 260.0,     # bridges the gap between two words
        VoiceState.RECOVERING: 400.0,        # a 300ms reconnect must not flash on screen
        VoiceState.STARTING: 250.0,
    }

    # States a dwell may NEVER delay, whatever is currently on screen.
    #
    # Two groups, and both are things the user is entitled to see immediately:
    #
    #   * what the USER just did — pausing the microphone, going into standby, quitting — and
    #     a failure they need to know about. A user who presses "pause listening" must see it
    #     happen now, not in 260ms.
    #   * what the ASSISTANT is now doing on their behalf. Someone who finishes a sentence and
    #     sees "Listening" for a quarter of a second before "Thinking" is watching the UI lag
    #     the system, which is the sluggishness a debounce is supposed to avoid causing.
    #
    # What is left — falling back to LISTENING, STARTING or RECOVERING — is exactly the set of
    # transitions where a single noisy sample produces a visible flicker, and exactly the set
    # the dwell exists to smooth.
    IMMEDIATE_STATES = frozenset({
        VoiceState.PAUSED, VoiceState.STANDBY, VoiceState.STOPPING,
        VoiceState.OFFLINE, VoiceState.ERROR,
        VoiceState.PROCESSING, VoiceState.ASSISTANT_SPEAKING, VoiceState.USER_SPEAKING,
    })

    # ── HOW LONG A VOICE MUST PERSIST TO INTERRUPT PLAYBACK ──
    # OBSERVED: while Kayra was speaking the visual flipped ASSISTANT_SPEAKING <-> USER_SPEAKING
    # repeatedly. The page already raises the VAD threshold during playback (`vadEchoMargin`,
    # 7x rather than 3.2x), but residual echo still crosses it in bursts, and ANY crossing was
    # enough to repaint the state.
    #
    # A person taking the floor speaks for a few hundred milliseconds; an echo spike does not.
    # So over playback — and ONLY over playback — the detector must have been saying "voice"
    # continuously for this long before the visual accepts it.
    #
    # THIS DOES NOT SLOW BARGE-IN. A real "stop" reaches the assistant through the page's
    # interim interrupt flag, which sets the turn machine to INTERRUPTING; that is branch 3
    # above and it is immediate. This dwell governs only the case where the VAD alone is
    # guessing, which is exactly where a guess was wrong.
    BARGE_IN_VAD_DWELL_MS = 320.0

    def __init__(self, clock_ms=None):
        self._now_ms = clock_ms or _now_ms
        self._voice_since_ms = (clock_ms or _now_ms)()
        self._lock = threading.RLock()
        self._state = VoiceState.OFFLINE
        self._revision = 0
        self._since_ms = self._now_ms()
        self._facts = {
            "assistant_state": "IDLE",
            "listening": True,
            "sleeping": False,
            "shutting_down": False,
            "voice_available": False,
            "backend_status": "OFF",
            "voice_active": False,
        }
        self._listeners = []
        # Latched the first time `shutting_down` is observed. From then on the only reachable
        # states are STOPPING and OFFLINE — see TERMINAL_STATES for why this is a latch rather
        # than a property of the state itself.
        self._shutting_down_seen = False

    # ── Reading ───────────────────────────────────────────────────────

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def snapshot(self):
        """
        One locked read of the state, its revision and the facts behind it.

        The facts are included on purpose: a diagnostic that shows only the state cannot
        answer "why is it saying that?", which is the question this whole module exists
        because nobody could previously answer.
        """
        with self._lock:
            return {
                "state": self._state,
                "revision": self._revision,
                "text": STATE_TEXT.get(self._state, self._state.title()),
                "detail": STATE_DETAIL.get(self._state, ""),
                "orb_state": ORB_STATE.get(self._state, "IDLE"),
                "orb_amplitude": ORB_AMPLITUDE.get(self._state, 0.2),
                "capture_active": self._state in CAPTURING_STATES,
                "vad_active": bool(self._facts.get("voice_active")),
                "stt_status": self._facts.get("backend_status"),
                "runtime_state": self._facts.get("assistant_state"),
                "seconds_in_state": (self._now_ms() - self._since_ms) / 1000.0,
                "facts": dict(self._facts),
            }

    def is_stale(self, revision):
        """
        Whether a callback carrying `revision` has been overtaken by a newer transition.

        The one line every asynchronous consumer needs. A revision EQUAL to the current one is
        not stale — it is the current state, arriving. Anything lower has been superseded.
        `None` is stale by definition: a callback that carries no revision cannot be shown to
        be current, and rendering it would reintroduce exactly the ordering assumption the
        revision exists to remove.
        """
        if revision is None:
            return True
        with self._lock:
            return int(revision) < self._revision

    # ── Writing ───────────────────────────────────────────────────────

    def update(self, **facts):
        """
        Records new facts and returns the resulting `VoiceTransition`, or None if unchanged.

        Only the facts passed are updated; everything else keeps its last value. That is what
        lets four different producers each report the one thing they actually know — the
        watcher reports VAD, the runtime reports the turn, the backend manager reports the
        session — without any of them having to know or guess the others.
        """
        with self._lock:
            for key, value in facts.items():
                if key in self._facts:
                    if key == "voice_active":
                        self._note_voice(bool(value))
                    self._facts[key] = value
            return self._resolve()

    def _note_voice(self, active):
        """
        Records when the VAD's verdict last CHANGED, so its age can be read.

        Only the transition is stamped, not every sample: `_voice_since_ms` therefore answers
        "how long has the detector been saying this?", which is the question the barge-in
        dwell below needs and the one a per-sample timestamp cannot answer.
        """
        if bool(self._facts.get("voice_active")) != bool(active):
            self._voice_since_ms = self._now_ms()

    def _voice_age_ms(self):
        return self._now_ms() - getattr(self, "_voice_since_ms", 0.0)

    def _resolve(self):
        if self._facts.get("shutting_down"):
            self._shutting_down_seen = True

        # The age of the VAD's current verdict travels IN, so `_compute` stays pure, static
        # and total — every combination of facts still yields exactly one state, which is what
        # lets the suite sweep all of them.
        resolved, reason = self._compute(self._facts, self._voice_age_ms())
        current = self._state

        # Once shutdown has been observed, the machine is absorbing: a late VAD sample from a
        # watcher thread that has not noticed yet, or a backend notification from a session
        # being reaped, must not repaint a live microphone. STOPPING may still become OFFLINE,
        # which is where teardown ends.
        if self._shutting_down_seen and resolved not in TERMINAL_STATES:
            resolved, reason = (
                (VoiceState.OFFLINE, "shut down")
                if current == VoiceState.OFFLINE
                else (VoiceState.STOPPING, "shutdown"))

        if resolved == current:
            return None

        dwell = self.DWELL_MS.get(current, 0.0)
        if dwell and resolved not in self.IMMEDIATE_STATES:
            if (self._now_ms() - self._since_ms) < dwell:
                return None

        previous = current
        self._state = resolved
        self._since_ms = self._now_ms()
        self._revision += 1
        transition = VoiceTransition(resolved, previous, self._revision,
                                     self._since_ms, reason)
        listeners = list(self._listeners)

        # Outside the lock, exactly like `RuntimeState.set_state`: `emit` calls subscribers
        # synchronously, and holding a lock across arbitrary third-party code is how a
        # deadlock gets built.
        for callback in listeners:
            try:
                callback(transition)
            except Exception:
                pass
        return transition

    # ── The resolution itself ─────────────────────────────────────────

    @staticmethod
    def _compute(facts, voice_age_ms=0.0):
        """
        THE PRECEDENCE. Pure, static and total — every combination of facts yields a state.

        Order matters and every step of it is a decision:

          1. Shutdown outranks everything. It is the only irreversible thing here.
          2. Standby outranks the turn machine: a sleeping Kayra may still be draining a final
             sentence, and reporting SPEAKING then would invite the user to talk to something
             that is not going to answer.
          3. The TURN outranks the SESSION. If the STT session drops while a reply is being
             generated, the truthful headline is that Kayra is working, not that the
             microphone is reconnecting — the microphone is not what the user is waiting on.
          4. A barge-in is USER_SPEAKING, not "interrupted". The old label described what had
             happened to Kayra; the user needs to know they have the floor.
          5. Session trouble (recovering / starting / error) comes next, ABOVE the pause check,
             so a recovery reads as "Reconnecting" and can never render as a false pause.
          6. PAUSED requires the microphone to be deliberately closed. Nothing else reaches it:
             not silence, not a late transcript, not a slow interim result.
          7. Everything left over is LISTENING, with VAD choosing between LISTENING and
             USER_SPEAKING. SILENCE IS STILL LISTENING.
        """
        assistant = str(facts.get("assistant_state") or "IDLE").upper()
        backend = str(facts.get("backend_status") or "OFF").upper()
        listening = bool(facts.get("listening", True))
        sleeping = bool(facts.get("sleeping", False))
        shutting_down = bool(facts.get("shutting_down", False))
        available = bool(facts.get("voice_available", False))
        voice_active = bool(facts.get("voice_active", False))

        # 1. Shutdown.
        if shutting_down or assistant == "SHUTTING_DOWN" or backend == "STOPPING":
            return VoiceState.STOPPING, "shutdown"

        # No speech input in this process at all — a text-only session.
        if not available or backend == "OFF":
            return VoiceState.OFFLINE, "no speech input"

        # 2. Standby.
        if sleeping:
            return VoiceState.STANDBY, "standby"

        # 3/4. The turn machine.
        if assistant == "INTERRUPTING":
            return VoiceState.USER_SPEAKING, "barge-in"
        if assistant == "SPEAKING":
            # A SUSTAINED voice arriving over playback is the barge-in, one poll before the
            # turn machine hears about it. Showing the user they have the floor at the instant
            # they take it is the difference between a responsive assistant and one that
            # argues — but the instant has to be real.
            #
            # A single VAD crossing is not evidence of a person while Kayra's own voice is in
            # the room. Requiring the detector to have held its verdict for
            # `BARGE_IN_VAD_DWELL_MS` is what stops residual echo repainting the state several
            # times a sentence. A genuine "stop" does not wait for this: it arrives as
            # INTERRUPTING through the page's interim flag, handled above.
            if voice_active and voice_age_ms >= VoiceStateMachine.BARGE_IN_VAD_DWELL_MS:
                return VoiceState.USER_SPEAKING, "barge-in (vad)"
            return VoiceState.ASSISTANT_SPEAKING, "speaking"
        if assistant in ("PROCESSING", "AUTOMATING"):
            return VoiceState.PROCESSING, "working"

        # 5. Session trouble — above the pause check, deliberately.
        if backend == "RECOVERING":
            return VoiceState.RECOVERING, "session recovering"
        if backend == "ERROR":
            return VoiceState.ERROR, "session unusable"
        if backend == "STARTING":
            return VoiceState.STARTING, "session starting"

        # 6. A deliberately closed microphone, and nothing else.
        if not listening or backend == "PAUSED":
            return VoiceState.PAUSED, "listening paused"

        # 7. Open, and either hearing a voice or not. Both are LISTENING at heart.
        if voice_active:
            return VoiceState.USER_SPEAKING, "voice detected"
        return VoiceState.LISTENING, "idle"

    # ── Subscription ──────────────────────────────────────────────────

    def subscribe(self, callback):
        """`callback(VoiceTransition)`, called synchronously on the thread that committed it."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One machine per process, for the reason the whole module exists: two would be two answers to
# one question, and the screen would show whichever was written most recently.

_MACHINE = None
_LOCK = threading.Lock()


def get_voice_state() -> VoiceStateMachine:
    global _MACHINE
    if _MACHINE is None:
        with _LOCK:
            if _MACHINE is None:
                _MACHINE = VoiceStateMachine()
    return _MACHINE


def reset_voice_state():
    """Drops the process machine. For tests only — nothing in the application calls this."""
    global _MACHINE
    with _LOCK:
        _MACHINE = None
