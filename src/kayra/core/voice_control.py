# ┌────────────────────────────────────────────────────────────────────────┐
# │                          voice_control.py                              │
# │        The Local Control Vocabulary — matched before the DMM           │
# └────────────────────────────────────────────────────────────────────────┘
"""
Deterministic, offline classification of the handful of things the user says TO Kayra about
Kayra itself: stop talking, stop listening, go to sleep, wake up, shut down.

WHY THIS IS NOT THE DMM'S JOB
-----------------------------
Every one of these commands is about the assistant's own lifecycle, and every one of them is
useless if it is slow or needs the network:

  * "stop" has to silence playback in tens of milliseconds. A Cohere round-trip is ~700ms and
    happens AFTER the ~800ms VAD finalize, so a classifier-routed barge-in is over a second
    late — long enough for the user to say it twice.
  * "exit" has to work when the network is down, when the Cohere key is missing, and while a
    response is still generating.
  * A paused microphone, a sleeping assistant and a shutting-down process are all states in
    which the DMM must not be consulted at all.

So these are matched HERE, locally, before classification — one normalization pass and a
frozenset probe, O(1) in the size of the vocabulary and with no allocation beyond the word
list. Measured under 10us per utterance.

MATCHING IS EXACT, NEVER PREFIX
-------------------------------
The whole utterance, with filler words removed, must equal a phrase in the set. This is the
rule that keeps the two vocabularies apart:

    "stop"            -> INTERRUPT       (barge-in; the audio layer handles it)
    "stop the music"  -> not a control   (falls through to the DMM -> 'stop media')
    "wait"            -> INTERRUPT
    "wait for me"     -> not a control
    "hold"            -> INTERRUPT
    "hold the window" -> not a control
    "turn off kayra"  -> SHUTDOWN        (ends this process)
    "turn off my pc"  -> not a control   (falls through to the DMM -> 'system shutdown')

A `startswith` test would collapse each of those pairs into its first member. There is exactly
one place where a non-exact test is allowed, and it is deliberately scoped: see
`interrupt_in_tail` below.

THE ASSISTANT-SHUTDOWN / MACHINE-SHUTDOWN BOUNDARY
--------------------------------------------------
Kayra shutdown phrases must NAME Kayra (or name its engine, or be one of the two bare words
"exit"/"quit" that have always meant this). Anything that names the machine — "my computer",
"my pc", "this laptop" — is not in this vocabulary at all and reaches the automation layer's
`system.shutdown`, which is CONFIRM-gated. The two sets are disjoint by construction and
`tests/test_voice_control.py` asserts it phrase by phrase.

"STOP KAYRA" IS AN INTERRUPT, NOT A SHUTDOWN — DELIBERATELY
------------------------------------------------------------
"kayra" is a filler word (it is how you address the assistant), so "Kayra, stop" reduces to
"stop". That makes "stop kayra" and "kayra stop" indistinguishable after normalization, and
they are genuinely ambiguous in English. Between the two readings we take the recoverable one:
interrupting costs the user a repeated sentence, quitting costs them the session. "shut down
kayra" / "turn off kayra" / "close kayra" are unambiguous and do quit.
"""

import re
import unicodedata


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             CONTROL KINDS                              │
# └────────────────────────────────────────────────────────────────────────┘
# Distinct names for distinct outcomes. Overloading one generic "control" status is exactly how
# the three "stop"-shaped concepts in this application got confused with each other in the
# first place, so each is its own value and each has exactly one handler.

class ControlKind:
    INTERRUPT = "CONTROL_INTERRUPTED"           # silence what is being SPOKEN
    PAUSE_LISTENING = "CONTROL_LISTENING_PAUSED"  # close the MICROPHONE
    RESUME_LISTENING = "CONTROL_LISTENING_RESUMED"
    SLEEP = "CONTROL_SLEEPING"                  # standby: alive, quiet, not classifying
    WAKE = "CONTROL_AWAKE"
    SHUTDOWN = "CONTROL_SHUTDOWN"               # end the PROCESS


# Kinds that are safe to act on even while the assistant is mid-response. Everything here
# either silences Kayra or ends it, so there is nothing to protect.
IMMEDIATE_KINDS = frozenset({ControlKind.INTERRUPT, ControlKind.SHUTDOWN})

# Kinds a sleeping assistant still answers. Anything else spoken while asleep is ignored
# without being classified, which is the entire point of standby.
WAKE_KINDS = frozenset({ControlKind.WAKE, ControlKind.SHUTDOWN, ControlKind.RESUME_LISTENING})


class ControlCommand:
    """A matched control command. `kind` is a `ControlKind`; `phrase` is what matched."""

    __slots__ = ("kind", "phrase", "text")

    def __init__(self, kind, phrase, text=""):
        self.kind = kind
        self.phrase = phrase
        self.text = text

    def __repr__(self):
        return f"ControlCommand({self.kind}, {self.phrase!r})"

    def __eq__(self, other):
        if isinstance(other, ControlCommand):
            return self.kind == other.kind and self.phrase == other.phrase
        return NotImplemented

    def __hash__(self):
        return hash((self.kind, self.phrase))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            THE VOCABULARY                              │
# └────────────────────────────────────────────────────────────────────────┘

# Barge-in. Shared verbatim with the STT page's `looksLikeInterrupt`, which is injected with
# this exact list at recognition start — one vocabulary, two implementations, never two lists.
#
# Hindi entries are here because INPUT_LANGUAGE is frequently 'hi-IN', in which case interim
# results come back in Devanagari and would never match the Latin entries.
INTERRUPT_PHRASES = [
    # ── the three the user asks for by name ──
    "stop", "wait", "hold",
    # ── safe variations of the same three ──
    "hold on", "hold up", "stop talking", "stop speaking", "wait a second",
    "wait a minute", "hold on a second",
    # ── the pre-existing vocabulary, unchanged ──
    "shut up", "pause", "quiet", "be quiet", "silence",
    "enough", "cancel", "nevermind", "never mind",
    # ── Hindi ──
    "रुको", "रुक", "ठहरो", "बस", "चुप",
    # English interrupt words as the hi-IN recognizer transliterates them. Users speak English
    # commands to Kayra while INPUT_LANGUAGE is hi-IN, and Google returns Devanagari for them.
    "स्टॉप", "वेट", "होल्ड", "रुको जरा",
]

# Stripped before matching, so "Kayra, stop please" reduces to "stop". Note that "please" being
# a filler is what makes "please stop" / "please wait" work without their own entries.
INTERRUPT_FILLERS = {
    "kayra", "please", "just", "ok", "okay", "hey", "yo", "now", "uh", "um",
}

# The assistant's microphone. "stop listening" is NOT a barge-in and NOT a shutdown; it closes
# the microphone and changes nothing else.
PAUSE_LISTENING_PHRASES = [
    "stop listening", "stop listening to me", "pause listening", "pause the microphone",
    "pause the mic", "mute the microphone", "mute the mic", "stop hearing me",
    "close the microphone", "close the mic",
]

RESUME_LISTENING_PHRASES = [
    "start listening", "resume listening", "listen again", "listen to me",
    "open the microphone", "open the mic", "unmute the microphone", "unmute the mic",
]

# Standby. Kayra stays alive, stays configured, stays in memory, and stops doing anything on
# its own. See `app.set_sleeping` for what this does and, importantly, what it does not.
SLEEP_PHRASES = [
    "go to sleep", "sleep kayra", "kayra sleep", "kayra go to sleep",
    "go to standby", "stand by", "take a nap", "go quiet", "go idle",
]

WAKE_PHRASES = [
    "wake up", "wake", "wake kayra", "kayra wake up", "wake up kayra",
    "are you there", "come back",
]

# Ending the PROCESS. Every phrase either names Kayra, names its engine, or is one of the two
# bare words that have always meant exactly this. Nothing here names the machine.
SHUTDOWN_PHRASES = [
    "exit", "quit", "goodbye kayra", "bye kayra",
    "shutdown kayra", "shut down kayra", "kayra shutdown", "kayra shut down",
    "turn off kayra", "kayra turn off", "close kayra", "kill kayra",
    "terminate kayra", "shut yourself down", "turn yourself off", "shut yourself off",
    "turn off your engine", "turn off the engine", "shut down your engine",
    "shut down the engine", "stop your engine", "power down kayra",
    "exit kayra", "quit kayra",
]


# The name is configurable, so a user who renamed the assistant to "Vega" must still be able to
# say "turn off Vega". Rather than templating every phrase at import time (the name is not
# known until `.env` is loaded), the configured name is folded into the FILLER set at match
# time and the phrases are stored in their canonical "kayra" form — one substitution instead of
# a second copy of the whole table. `_alias_words()` builds that set once and caches it.
_CANONICAL_NAME = "kayra"

_KIND_BY_PHRASE = {}


def _register(phrases, kind):
    for phrase in phrases:
        # First registration wins. INTERRUPT is registered first on purpose: if a phrase were
        # ever added to two tables, silencing is the safer of any two outcomes.
        _KIND_BY_PHRASE.setdefault(phrase, kind)


_register(INTERRUPT_PHRASES, ControlKind.INTERRUPT)
_register(PAUSE_LISTENING_PHRASES, ControlKind.PAUSE_LISTENING)
_register(RESUME_LISTENING_PHRASES, ControlKind.RESUME_LISTENING)
_register(SLEEP_PHRASES, ControlKind.SLEEP)
_register(WAKE_PHRASES, ControlKind.WAKE)
_register(SHUTDOWN_PHRASES, ControlKind.SHUTDOWN)

_INTERRUPT_PHRASE_SET = frozenset(INTERRUPT_PHRASES)
_SINGLE_WORD_INTERRUPTS = frozenset(p for p in INTERRUPT_PHRASES if " " not in p)

# Longest phrase in the table, in words. Bounds the suffix scan in `interrupt_in_tail` and the
# early-out in `classify_control`, so neither can degrade on a long utterance.
MAX_CONTROL_WORDS = max(len(p.split()) for p in _KIND_BY_PHRASE)
MAX_INTERRUPT_WORDS = max(len(p.split()) for p in INTERRUPT_PHRASES)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            NORMALIZATION                               │
# └────────────────────────────────────────────────────────────────────────┘

# Punctuation only. NOT a `\w` allow-list: `\w` excludes combining marks (Unicode category Mn),
# which would split Devanagari words apart — "रुको" became "र क" and every Hindi interrupt word
# silently stopped matching. Same bug class as the one fixed in `speech_safe_text`.
_PUNCTUATION = re.compile(r"[^\w\sऀ-ॿ]", re.UNICODE)


def _alias_words(assistant_name=None):
    """
    The filler set for this utterance: the standard fillers plus the configured assistant name.

    Reads `core.config` lazily and defensively — this module must stay usable (and testable)
    before `.env` has been loaded, and a config failure must never make "stop" stop working.
    """
    if assistant_name is None:
        try:
            from kayra.core.config import assistant_name as configured
            assistant_name = configured()
        except Exception:
            assistant_name = _CANONICAL_NAME
    name = (assistant_name or "").strip().lower()
    if not name or name == _CANONICAL_NAME:
        return INTERRUPT_FILLERS
    return INTERRUPT_FILLERS | {name}


def normalize_utterance(text, assistant_name=None):
    """
    An utterance reduced to the words that carry meaning, plus the alias-substituted form.

    Returns (words, canonical_text) where `words` has fillers removed and `canonical_text` is
    the same words re-joined with the configured assistant name rewritten to "kayra" — which is
    the form the phrase tables are written in.
    """
    if not isinstance(text, str):
        return [], ""
    cleaned = unicodedata.normalize("NFC", text).lower()
    cleaned = _PUNCTUATION.sub(" ", cleaned).strip()
    if not cleaned:
        return [], ""

    fillers = _alias_words(assistant_name)
    names = fillers - INTERRUPT_FILLERS or {_CANONICAL_NAME}
    raw_words = cleaned.split()

    # The name is a filler for the interrupt vocabulary ("Kayra, stop" -> "stop") and a
    # meaningful token for the lifecycle vocabulary ("turn off Kayra"). Both readings are
    # produced here so the caller never has to normalize twice.
    #
    # `stripped` drops every filler including the name.
    # `named` drops every filler EXCEPT the name, and rewrites a renamed assistant back to the
    # canonical "kayra" the phrase tables are written in — one substitution instead of a second
    # copy of the whole table.
    stripped = [w for w in raw_words if w not in fillers]
    named = [_CANONICAL_NAME if w in names else w
             for w in raw_words
             if w in names or w == _CANONICAL_NAME or w not in fillers]
    return stripped, " ".join(named)


def classify_control(text, assistant_name=None):
    """
    The local control interpreter. Returns a `ControlCommand`, or None to fall through to the
    DMM.

    Two probes, both exact, both O(1):

      1. the utterance WITH the assistant's name preserved  -> lifecycle phrases
         ("turn off kayra", "kayra go to sleep")
      2. the utterance with fillers (including the name) removed -> interrupt phrases
         ("kayra, please stop" -> "stop")

    Probe 1 runs first so a phrase that names the assistant is read as addressed TO it rather
    than having the name discarded as noise.
    """
    stripped, named = normalize_utterance(text, assistant_name)
    if not stripped and not named:
        return None

    # 1. Lifecycle phrases, name intact.
    if named and len(named.split()) <= MAX_CONTROL_WORDS:
        kind = _KIND_BY_PHRASE.get(named)
        if kind is not None and kind != ControlKind.INTERRUPT:
            return ControlCommand(kind, named, text)

    if not stripped:
        # The whole utterance was filler — "kayra", "hey kayra". Not a command.
        return None

    joined = " ".join(stripped)

    # 2. Everything else, fillers removed.
    if len(stripped) <= MAX_CONTROL_WORDS:
        kind = _KIND_BY_PHRASE.get(joined)
        if kind is not None:
            return ControlCommand(kind, joined, text)

    # 3. Repetition of a single-word interrupt ("stop stop stop") still means stop. Bounded to
    #    the interrupt vocabulary: repeating a lifecycle word does not quit anything.
    if 1 < len(stripped) <= MAX_INTERRUPT_WORDS and all(
            w in _SINGLE_WORD_INTERRUPTS for w in stripped):
        return ControlCommand(ControlKind.INTERRUPT, joined, text)

    return None


def is_interrupt_phrase(text) -> bool:
    """
    True when an utterance is ONLY an interruption command ("stop", "wait", "hold on").

    Exact, never prefix: `is_interrupt_phrase("stop the music")` is False, which is what keeps
    that automation command reaching the DMM instead of being swallowed as a barge-in.
    """
    command = classify_control(text)
    return bool(command is not None and command.kind == ControlKind.INTERRUPT)


def interrupt_in_tail(text, assistant_name=None) -> bool:
    """
    True when the utterance ENDS with an interruption command.

    THIS IS THE ONE PLACE A NON-EXACT TEST IS CORRECT, AND IT IS SCOPED TO ONE SITUATION:
    the recognizer's buffer while the assistant is audible.

    The microphone stays open during playback, so the interim transcript the STT page is
    accumulating when the user barges in is not "stop" — it is whatever echo of Kayra's own
    voice leaked past Chrome's echo canceller, with "stop" appended:

        "...and then the rollout usually takes ten minutes stop"

    An exact whole-utterance test cannot match that, which is precisely why "stop" worked
    sometimes (quiet room, clean AEC, empty buffer) and not others. Matching the TAIL matches
    the word the user actually just said, independently of what came before it.

    It is not used anywhere else, because everywhere else it would be wrong: applied to a
    finalized transcript it would turn "close this tab and stop" into a barge-in. The STT page
    only tail-matches while `window.kayraSpeaking` is true, and the Python side only consults
    this function on the same condition — see `app._local_control_watcher`.
    """
    stripped, _ = normalize_utterance(text, assistant_name)
    if not stripped:
        return False
    for size in range(1, MAX_INTERRUPT_WORDS + 1):
        if size > len(stripped):
            break
        if " ".join(stripped[-size:]) in _INTERRUPT_PHRASE_SET:
            return True
    return False


def control_kinds():
    """Every kind this module can produce. Used by the test suite to prove coverage."""
    return (ControlKind.INTERRUPT, ControlKind.PAUSE_LISTENING, ControlKind.RESUME_LISTENING,
            ControlKind.SLEEP, ControlKind.WAKE, ControlKind.SHUTDOWN)


def phrases_for(kind):
    """Every phrase that classifies to `kind`. Diagnostics and tests only."""
    return tuple(sorted(p for p, k in _KIND_BY_PHRASE.items() if k == kind))
