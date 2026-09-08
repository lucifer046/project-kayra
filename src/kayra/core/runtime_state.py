# ┌────────────────────────────────────────────────────────────────────────┐
# │                          runtime_state.py                              │
# │              Shared Assistant State & Lightweight Event Bus            │
# └────────────────────────────────────────────────────────────────────────┘
"""
The Event / State orchestrator at the centre of the Kayra core.

Every subsystem that needs to know "what is the assistant doing right now?" reads it from
here instead of from a module-level global in `main.py`. That matters most for the proactive
agent: it runs on its own thread and must never speak over the user, so it needs a truthful,
thread-safe answer to that question at any instant.

Two things live in this module and nothing else:

  * `RuntimeState` — a small, lock-protected snapshot of what the assistant is doing plus the
    handful of timestamps that describe how recently the user was involved.
  * a minimal event bus — `emit()` fans a named event out to whoever subscribed. This is how
    `main.py` tells the proactive service that a command was issued without importing it, and
    it is deliberately synchronous and tiny: there is exactly one subscriber in practice, and
    a queue plus a dispatch thread would cost more than it saves.

DESIGN RULES (these are load-bearing, not preferences)
-----------------------------------------------------
* This object holds STATE, never RESOURCES. It does not own the TTS engine, the STT session
  or the LLM client, and it must never import them — anything may import this module.
* Readers here never mutate anything a response stream depends on. In particular, the
  proactive agent reads this state but has no way to touch the TTS engine's cancellation
  epoch through it; a turn's cancellation is owned by `text_to_speech` alone.
* `emit()` swallows subscriber exceptions. A bad listener must not be able to break the
  main loop that emitted the event.
"""

import time
import threading
from collections import deque


def _now_ms() -> float:
    return time.time() * 1000.0


class AssistantState:
    """
    What the assistant is doing right now.

    Single-writer per state in practice: the main loop owns LISTENING / PROCESSING /
    AUTOMATING, the response dispatcher owns SPEAKING, the barge-in watcher owns
    INTERRUPTING and always hands back to LISTENING, and the shutdown handler owns
    SHUTTING_DOWN.
    """
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    AUTOMATING = "AUTOMATING"
    SHUTTING_DOWN = "SHUTTING_DOWN"


# States in which the assistant is mid-turn on the user's behalf. Nothing unprompted may
# be spoken while the runtime is in one of these.
BUSY_STATES = frozenset({
    AssistantState.PROCESSING,
    AssistantState.SPEAKING,
    AssistantState.INTERRUPTING,
    AssistantState.AUTOMATING,
    AssistantState.SHUTTING_DOWN,
})


class RuntimeState:
    """
    Thread-safe view of the assistant's current activity.

    Everything is guarded by one lock and every accessor is O(1) — the proactive agent polls
    `snapshot()` on its tick and the barge-in watcher writes to it at 60ms intervals, so
    contention here would show up directly as speech latency.
    """

    # Bounded: the event log exists for diagnostics, not history. An unbounded list here
    # would be a slow memory leak on a process designed to run all day.
    MAX_EVENT_LOG = 64

    def __init__(self, clock_ms=None):
        # The clock is injectable so the "is it safe to speak?" policy can be tested at
        # exact offsets instead of by sleeping. Production always uses the wall clock.
        self._now_ms = clock_ms or _now_ms
        self._lock = threading.RLock()
        self._state = AssistantState.IDLE
        # Listening is a SEPARATE axis from the assistant state, not another value of it.
        # Overloading `state` would make "paused" mutually exclusive with SPEAKING, which is
        # wrong in both directions: Kayra can finish a sentence with the microphone already
        # closed, and it can be idle while still listening. See `set_listening`.
        self._listening = True
        # Standby is a THIRD independent axis, for the same reason listening is a second one.
        # A sleeping Kayra is not a state of the turn machine: it can be asleep while a final
        # sentence drains, and it is emphatically not IDLE (idle means "ready for your next
        # command", standby means "ignoring everything but 'wake up'").
        self._sleeping = False
        self._state_since_ms = self._now_ms()

        # Timestamps describing how recently the user was involved. The proactive agent's
        # safety gate is expressed entirely in terms of these.
        self._last_user_utterance_ms = 0.0
        self._last_interrupt_ms = 0.0
        self._last_turn_end_ms = 0.0

        self._turn_id = 0
        self._turn_active = False

        self.shutdown_event = threading.Event()

        self._subscribers = []
        self._events = deque(maxlen=self.MAX_EVENT_LOG)

    # ──────────────────────────────────────────────────────────────────────
    #                                STATE
    # ──────────────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def set_state(self, new_state: str):
        """
        Moves the assistant to a new state and announces the transition.

        The `state_changed` event exists so a presentation layer can be event-driven instead of
        polling `state` on a timer. That distinction matters: the desktop UI renders an
        assistant visual whose animation follows this value, and a 10Hz poll to discover
        something the writer already knows is pure waste in a process that is meant to sit idle
        most of the day.

        The emit happens AFTER the lock is released. `_lock` is re-entrant, so emitting inside
        it would not deadlock this thread — but `emit()` calls subscribers synchronously, and a
        subscriber that touched the runtime from another thread would then block on a lock held
        across arbitrary third-party code. Announcing a transition that has already been
        committed is both safe and correct.
        """
        with self._lock:
            if new_state == self._state:
                return
            previous, self._state = self._state, new_state
            self._state_since_ms = self._now_ms()

        self.emit("state_changed", state=new_state, previous=previous)

    def seconds_in_state(self) -> float:
        with self._lock:
            return (self._now_ms() - self._state_since_ms) / 1000.0

    # ──────────────────────────────────────────────────────────────────────
    #                              LISTENING
    # ──────────────────────────────────────────────────────────────────────
    # A third, independent thing. The application has three "stop"-shaped concepts and they
    # must never share a flag:
    #
    #   barge-in          cancels the sentence being spoken   -> note_interrupt() + tts.stop()
    #   listening pause   closes the microphone               -> set_listening(False)
    #   shutdown          ends the process                    -> shutdown_event + _force_shutdown
    #
    # Only the middle one lives here, because it is STATE that several surfaces have to agree
    # on — the main window, the ambient window, the composer and the console loop all read it,
    # and a second copy anywhere would let two of them disagree about whether Kayra can hear.

    @property
    def listening(self) -> bool:
        with self._lock:
            return self._listening

    def set_listening(self, enabled: bool):
        """
        Records whether the microphone is open. Returns True if this call changed it.

        Emits `listening_changed` outside the lock, for the same reason `set_state` does: a
        subscriber that calls back in must not deadlock on a lock we are still holding.
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._listening:
                return False
            self._listening = enabled
        self.emit("listening_changed", listening=enabled)
        return True

    # ──────────────────────────────────────────────────────────────────────
    #                               STANDBY
    # ──────────────────────────────────────────────────────────────────────

    @property
    def sleeping(self) -> bool:
        with self._lock:
            return self._sleeping

    def set_sleeping(self, enabled: bool):
        """
        Records whether the assistant is in standby. Returns True if this call changed it.

        Emitted outside the lock, like every other transition here, so a subscriber that calls
        back in cannot deadlock on a lock we are still holding.
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._sleeping:
                return False
            self._sleeping = enabled
        self.emit("sleeping_changed", sleeping=enabled)
        return True

    def is_busy(self) -> bool:
        """True while the assistant is mid-turn on the user's behalf."""
        with self._lock:
            return self._state in BUSY_STATES

    def is_shutting_down(self) -> bool:
        return self.shutdown_event.is_set()

    # ──────────────────────────────────────────────────────────────────────
    #                          TURN INSTRUMENTATION
    # ──────────────────────────────────────────────────────────────────────

    def begin_turn(self) -> int:
        """Marks the start of a user-initiated turn and returns its id."""
        with self._lock:
            self._turn_id += 1
            self._turn_active = True
            return self._turn_id

    def end_turn(self):
        with self._lock:
            self._turn_active = False
            self._last_turn_end_ms = self._now_ms()

    @property
    def turn_active(self) -> bool:
        with self._lock:
            return self._turn_active

    @property
    def turn_id(self) -> int:
        with self._lock:
            return self._turn_id

    # ──────────────────────────────────────────────────────────────────────
    #                          USER-ACTIVITY MARKERS
    # ──────────────────────────────────────────────────────────────────────

    def note_user_utterance(self):
        with self._lock:
            self._last_user_utterance_ms = self._now_ms()

    def note_interrupt(self):
        with self._lock:
            self._last_interrupt_ms = self._now_ms()

    def seconds_since_user_utterance(self) -> float:
        with self._lock:
            if not self._last_user_utterance_ms:
                return float("inf")
            return (self._now_ms() - self._last_user_utterance_ms) / 1000.0

    def seconds_since_interrupt(self) -> float:
        with self._lock:
            if not self._last_interrupt_ms:
                return float("inf")
            return (self._now_ms() - self._last_interrupt_ms) / 1000.0

    def seconds_since_turn_end(self) -> float:
        with self._lock:
            if not self._last_turn_end_ms:
                return float("inf")
            return (self._now_ms() - self._last_turn_end_ms) / 1000.0

    def snapshot(self) -> dict:
        """One locked read of everything a policy decision needs, so the values are consistent."""
        with self._lock:
            now = self._now_ms()
            return {
                "state": self._state,
                "listening": self._listening,
                "sleeping": self._sleeping,
                "busy": self._state in BUSY_STATES,
                "turn_active": self._turn_active,
                "turn_id": self._turn_id,
                "shutting_down": self.shutdown_event.is_set(),
                "seconds_in_state": (now - self._state_since_ms) / 1000.0,
                "seconds_since_user_utterance": (
                    float("inf") if not self._last_user_utterance_ms
                    else (now - self._last_user_utterance_ms) / 1000.0
                ),
                "seconds_since_interrupt": (
                    float("inf") if not self._last_interrupt_ms
                    else (now - self._last_interrupt_ms) / 1000.0
                ),
                "seconds_since_turn_end": (
                    float("inf") if not self._last_turn_end_ms
                    else (now - self._last_turn_end_ms) / 1000.0
                ),
            }

    # ──────────────────────────────────────────────────────────────────────
    #                             EVENT BUS
    # ──────────────────────────────────────────────────────────────────────

    def subscribe(self, callback):
        """
        Registers `callback(event_name, payload_dict)`.

        Called synchronously on the emitting thread, so a subscriber must return quickly —
        the proactive agent's handler only stamps counters and returns.
        """
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, event: str, **payload):
        """
        Fans an event out to every subscriber.

        Subscriber exceptions are swallowed on purpose: this is called from the main loop and
        from the barge-in watcher, and a listener bug must not be able to wedge either.
        """
        with self._lock:
            subscribers = list(self._subscribers)
            self._events.append((self._now_ms(), event))
        for callback in subscribers:
            try:
                callback(event, payload)
            except Exception:
                pass

    def recent_events(self):
        with self._lock:
            return list(self._events)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One runtime per process, for the same reason CentralizedLLMEngine is a singleton: two
# copies would mean two disagreeing answers to "is the assistant speaking?", and the
# proactive agent would be reading the one nobody writes to.

_RUNTIME = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime_state() -> RuntimeState:
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = RuntimeState()
    return _RUNTIME
