# ┌────────────────────────────────────────────────────────────────────────┐
# │                       conversation_context.py                          │
# │            What The Conversation Is Currently About (state)            │
# └────────────────────────────────────────────────────────────────────────┘
"""
The assistant's short-term sense of *what we are currently doing together*.

`RuntimeState` answers "what is the assistant DOING right now?" — speaking, listening,
automating. This module answers the other half of the same question: **"what is this
conversation ABOUT right now?"** — the last few things said, the last intent the classifier
produced, the last thing automation touched, whether a question is outstanding, and the
handful of content words the exchange keeps returning to.

It exists because three separate parts of Kayra were each guessing at it:

* the transcript repair stage needs to know which words are PLAUSIBLE right now before it is
  allowed to prefer one recognition alternative over another — without that it is a spelling
  corrector operating on no evidence, which is exactly how an assistant "fixes" a legitimate
  word into the wrong command;
* the proactive presence layer wanted a conversation topic and had none, so its only notion
  of context was the foreground window title;
* the confirmation flow, the standby check and the turn loop each read a different fragment
  of the same picture out of different places.

DESIGN RULES (load-bearing, not preferences)
--------------------------------------------
* **This is STATE, not MEMORY.** `memory/conversation.py` owns durable conversation storage
  and writes to disk. Nothing here is ever persisted, and nothing here survives a restart.
  Two stores with the same name doing different jobs is confusing enough without one of them
  quietly becoming a second transcript log on disk.
* **It is a leaf.** Stdlib only, exactly like `core.runtime_state` and `core.paths`. Anything
  in the package may import it, including the input layer, which must never end up importing
  the intelligence or automation layers to learn what a plausible word is.
* **Everything is bounded.** A fixed-length turn ring, a capped topic vocabulary, a capped
  target list. This object runs for the life of a process that is designed to be left on all
  day, so an unbounded field here is a slow leak by construction.
* **It stores what was SAID, in RAM, for a few turns.** That is unavoidable for the job — a
  repair stage cannot be conservative without knowing the context it is being conservative
  about — but it is deliberately shallow: eight turns, truncated, never written anywhere,
  and `topic_terms()` reduces them to content words with the stopwords removed.
* **A malformed input can never raise here.** It is called from the turn loop and from the
  recognition path; a crash in bookkeeping must not cost the user their command.
"""

import re
import time
import threading
from collections import deque, Counter


def _now_ms() -> float:
    return time.time() * 1000.0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          CONVERSATION MODES                            │
# └────────────────────────────────────────────────────────────────────────┘

class ConversationMode:
    """
    The coarse shape of what is going on. Derived from the last intent, never set by hand.

    It is coarse ON PURPOSE. The repair stage uses it to decide which vocabulary is plausible,
    and a fine-grained mode model would be one more thing that can be wrong about a user who
    is doing two things at once.
    """
    IDLE = "IDLE"                    # nothing has happened yet
    CONVERSATION = "CONVERSATION"    # chat, search, research
    AUTOMATION = "AUTOMATION"        # driving the machine
    CONFIRMING = "CONFIRMING"        # a yes/no question is outstanding


# Token headers that mean "this was talk", not "this was an action".
_CONVERSATION_HEADERS = ("general", "realtime", "deep research", "content", "write")

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")

# Words that carry no topic. Kept deliberately small: this is a topic signal, not a language
# model, and an over-aggressive stoplist throws away the nouns the repair stage needs.
_STOPWORDS = frozenset("""
a about all also am an and any are as at be because been but by can could did do does doing
done down for from get got had has have he her here him his how i if in into is it its just
know let like make me more most my no not now of off on once one only or other our out over
own please put same say see she should so some such than that the their them then there these
they this those to too up us use very want was we were what when where which who why will with
would yes yeah no nope you your
""".split())


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        THE CONVERSATION CONTEXT                        │
# └────────────────────────────────────────────────────────────────────────┘

class ConversationContext:
    """
    Thread-safe, bounded, in-memory view of the current exchange.

    Written by the turn loop (`app.Main_Loop`) and by the UI session, which run the same
    pipeline; read by the transcript repair stage, the proactive presence layer and anything
    else that needs to know what "that" or "it" is currently likely to mean.

    Every accessor is O(1) or O(turns) over a fixed-length ring, because the repair stage
    consults this on the recognition path — between the user finishing a word and Kayra
    acting on it — and anything expensive here shows up directly as command latency.
    """

    MAX_TURNS = 8              # the ring of recent exchanges
    MAX_TOPIC_TERMS = 24       # the content-word vocabulary derived from them
    MAX_TARGETS = 8            # things automation recently acted on
    MAX_TEXT = 240             # per stored utterance; longer is truncated, never kept whole

    def __init__(self, clock_ms=None):
        # Injectable for deterministic tests, exactly like RuntimeState's.
        self._now_ms = clock_ms or _now_ms
        self._lock = threading.RLock()

        self._turns = deque(maxlen=self.MAX_TURNS)      # {"role", "text", "at"}
        self._topic = Counter()                          # content word -> occurrences
        self._targets = deque(maxlen=self.MAX_TARGETS)   # things automation touched

        self._last_intent = ()          # the DMM's tokens for the last classified turn
        self._last_headers = ()         # just their headers ("open", "general", ...)
        self._last_user_text = ""
        self._last_assistant_text = ""
        self._last_automation = None    # {"action", "target", "ok", "at"}

        self._pending_confirmation = ""  # the outstanding question, or ""
        self._mode = ConversationMode.IDLE
        self._mode_before_confirmation = None
        self._turn_count = 0
        self._started_ms = self._now_ms()

    # ──────────────────────────────────────────────────────────────────────
    #                              WRITERS
    # ──────────────────────────────────────────────────────────────────────

    def note_user_turn(self, text):
        """Records what the user said. Safe to call with anything at all."""
        text = self._clean(text)
        if not text:
            return
        with self._lock:
            self._turn_count += 1
            self._last_user_text = text
            self._turns.append({"role": "user", "text": text, "at": self._now_ms()})
            self._absorb_topic(text)

    def note_assistant_turn(self, text):
        """
        Records what Kayra said back.

        The assistant's own words are part of the context a following utterance is
        interpreted in — "yes" means something different after a question than after a
        statement — but they are NOT absorbed into the topic vocabulary. Doing so would let
        the assistant's phrasing bias the repair stage towards words the USER never used,
        which is the failure this whole subsystem is shaped to avoid.
        """
        text = self._clean(text)
        if not text:
            return
        with self._lock:
            self._last_assistant_text = text
            self._turns.append({"role": "assistant", "text": text, "at": self._now_ms()})

    def note_intent(self, tokens):
        """Records the DMM's classification and updates the derived mode."""
        with self._lock:
            # Non-strings are DISCARDED, not stringified. `str(None)` is "none", which is a
            # perfectly plausible-looking token that would then be treated as an intent
            # header — a malformed input turning into a real-looking one is worse than a
            # malformed input being dropped.
            cleaned = tuple(t.strip().lower() for t in (tokens or [])
                            if isinstance(t, str) and t.strip())
            self._last_intent = cleaned[:8]
            self._last_headers = tuple(self._header(t) for t in self._last_intent)
            if self._pending_confirmation:
                self._mode = ConversationMode.CONFIRMING
            elif not cleaned:
                self._mode = ConversationMode.IDLE
            elif all(any(t.startswith(h) for h in _CONVERSATION_HEADERS) for t in cleaned):
                self._mode = ConversationMode.CONVERSATION
            else:
                self._mode = ConversationMode.AUTOMATION

    def note_automation(self, action="", target="", ok=True):
        """
        Records what automation last touched.

        The TARGET is the useful part: an application or site the user has just referred to
        is far more likely to be referred to again in the next utterance, and that is a
        legitimate, evidence-backed reason for the repair stage to prefer a recognition
        alternative naming it.
        """
        with self._lock:
            target = self._clean(target, limit=60).lower()
            self._last_automation = {"action": str(action or "").lower(),
                                     "target": target, "ok": bool(ok),
                                     "at": self._now_ms()}
            if target and target not in self._targets:
                self._targets.append(target)

    def set_pending_confirmation(self, prompt=""):
        """
        Records that a yes/no question is outstanding, or clears it with an empty prompt.

        This is the single most valuable context signal the repair stage has: while a
        confirmation is pending, a short utterance is overwhelmingly likely to be an answer
        to it, and the answer vocabulary is tiny and fixed.
        """
        with self._lock:
            self._pending_confirmation = self._clean(prompt)
            if self._pending_confirmation:
                # Remember what we were doing. Answering "yes" to "shall I close Chrome?"
                # returns the conversation to the automation exchange it interrupted — it
                # does not end the conversation, and reporting IDLE there told the repair
                # stage the exchange had no context when it plainly did.
                if self._mode != ConversationMode.CONFIRMING:
                    self._mode_before_confirmation = self._mode
                self._mode = ConversationMode.CONFIRMING
            elif self._mode == ConversationMode.CONFIRMING:
                self._mode = self._mode_before_confirmation or ConversationMode.IDLE
                self._mode_before_confirmation = None

    def reset(self):
        """Clears the exchange. Used when a session ends; never on an ordinary turn."""
        with self._lock:
            self._turns.clear()
            self._topic.clear()
            self._targets.clear()
            self._last_intent = ()
            self._last_headers = ()
            self._last_user_text = ""
            self._last_assistant_text = ""
            self._last_automation = None
            self._pending_confirmation = ""
            self._mode = ConversationMode.IDLE
            self._mode_before_confirmation = None

    # ──────────────────────────────────────────────────────────────────────
    #                              READERS
    # ──────────────────────────────────────────────────────────────────────

    @property
    def mode(self):
        with self._lock:
            return self._mode

    @property
    def turn_count(self):
        with self._lock:
            return self._turn_count

    def expects_confirmation(self) -> bool:
        with self._lock:
            return bool(self._pending_confirmation)

    def last_intent(self):
        with self._lock:
            return tuple(self._last_intent)

    def last_headers(self):
        with self._lock:
            return tuple(self._last_headers)

    def recent_targets(self):
        """Things automation recently acted on, most recent last."""
        with self._lock:
            return tuple(self._targets)

    def topic_terms(self, limit=None):
        """
        The content words this exchange keeps returning to, most frequent first.

        Bounded and frequency-ordered rather than a bag of everything said: a term the user
        has used twice is evidence, and a term used once three turns ago mostly is not.
        """
        with self._lock:
            limit = limit or self.MAX_TOPIC_TERMS
            return tuple(term for term, _count in self._topic.most_common(limit))

    def topic_summary(self, limit=6):
        """A short human-readable topic line for logs and the presence layer. May be ''."""
        return " ".join(self.topic_terms(limit))

    def recent_turns(self, limit=None):
        with self._lock:
            turns = list(self._turns)
        return turns[-limit:] if limit else turns

    def seconds_since_last_user_turn(self):
        with self._lock:
            for turn in reversed(self._turns):
                if turn["role"] == "user":
                    return (self._now_ms() - turn["at"]) / 1000.0
        return float("inf")

    def snapshot(self) -> dict:
        """One locked read of everything a decision needs, so the values are consistent."""
        with self._lock:
            return {
                "mode": self._mode,
                "turn_count": self._turn_count,
                "expects_confirmation": bool(self._pending_confirmation),
                "pending_confirmation": self._pending_confirmation,
                "last_intent": tuple(self._last_intent),
                "last_headers": tuple(self._last_headers),
                "last_user_text": self._last_user_text,
                "last_assistant_text": self._last_assistant_text,
                "last_automation": dict(self._last_automation) if self._last_automation else None,
                "targets": tuple(self._targets),
                "topic": tuple(t for t, _c in self._topic.most_common(self.MAX_TOPIC_TERMS)),
                "turns": len(self._turns),
                "age_seconds": (self._now_ms() - self._started_ms) / 1000.0,
            }

    def describe(self) -> dict:
        """Compact status for the UI. Never includes a full transcript."""
        snap = self.snapshot()
        return {
            "mode": snap["mode"],
            "turns": snap["turn_count"],
            "topic": " ".join(snap["topic"][:6]),
            "targets": list(snap["targets"])[-3:],
            "awaiting_answer": snap["expects_confirmation"],
        }

    # ──────────────────────────────────────────────────────────────────────
    #                              INTERNALS
    # ──────────────────────────────────────────────────────────────────────

    def _clean(self, text, limit=None):
        if not text or not isinstance(text, str):
            return ""
        return " ".join(text.split())[:(limit or self.MAX_TEXT)]

    @staticmethod
    def _header(token):
        """The token's leading word or two — 'open chrome' -> 'open', 'deep research x' -> 'deep'."""
        parts = str(token or "").split()
        return parts[0] if parts else ""

    def _absorb_topic(self, text):
        """
        Folds an utterance's content words into the topic counter and keeps it bounded.

        Decay rather than eviction: when the counter is full every term is halved and the
        zeroes dropped, so a topic the user has moved on from fades instead of being kept
        alive forever by one early mention. That is the behaviour a topic signal needs — the
        conversation is about what is being said NOW, weighted by what has been said.
        """
        for word in _WORD.findall(text.lower()):
            if len(word) < 3 or word in _STOPWORDS:
                continue
            self._topic[word] += 1
        if len(self._topic) > self.MAX_TOPIC_TERMS:
            for term in list(self._topic):
                self._topic[term] //= 2
                if self._topic[term] <= 0:
                    del self._topic[term]
            # A pathological turn (a hundred distinct long words) can still leave the counter
            # over the cap after one halving; trim to the most frequent and move on.
            if len(self._topic) > self.MAX_TOPIC_TERMS:
                keep = dict(self._topic.most_common(self.MAX_TOPIC_TERMS))
                self._topic = Counter(keep)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One context per process, for the same reason `RuntimeState` and `CentralizedLLMEngine` are
# singletons: two copies would mean the turn loop writing to one while the repair stage read
# the other, and a repair stage reasoning about an empty context is a repair stage with no
# evidence — which is precisely when it must do nothing.

_CONTEXT = None
_CONTEXT_LOCK = threading.Lock()


def get_conversation_context() -> ConversationContext:
    global _CONTEXT
    if _CONTEXT is None:
        with _CONTEXT_LOCK:
            if _CONTEXT is None:
                _CONTEXT = ConversationContext()
    return _CONTEXT
