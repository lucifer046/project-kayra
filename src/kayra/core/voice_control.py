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
import time
import threading
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
    # Hand gesture control and the camera behind it. TWO kinds, not one, for exactly the
    # reason listening and standby are two: "turn on the camera" and "turn on hand gesture
    # control" are different requests with different outcomes, and a user who wants to see the
    # preview has not asked for anything to start moving their mouse pointer.
    GESTURE_ON = "CONTROL_GESTURE_ON"
    GESTURE_OFF = "CONTROL_GESTURE_OFF"
    CAMERA_ON = "CONTROL_CAMERA_ON"
    CAMERA_OFF = "CONTROL_CAMERA_OFF"


# Kinds that are safe to act on even while the assistant is mid-response. Everything here
# either silences Kayra or ends it, so there is nothing to protect.
IMMEDIATE_KINDS = frozenset({ControlKind.INTERRUPT, ControlKind.SHUTDOWN})

# ── THE TWO KINDS THAT MUST BE ASKED ABOUT FIRST ──────────────────────────
# High-impact and effectively irreversible from the user's side: one ends the process, the
# other stops the assistant doing anything until it is woken. A single recognition of either
# must never execute, because a recognizer running `hi-IN` over a Hindi sentence will
# eventually produce "exit" as a transient reading and the cost of being wrong is the whole
# session.
#
# Note what is NOT here. PAUSE_LISTENING closes the microphone and is reversible with one
# button, one hotkey or one tray click, so it stays immediate — a user who says "stop
# listening" usually wants it now, and making them answer a question first would be the wrong
# trade. INTERRUPT is not here for the same reason and more so: silencing is how the user
# takes the floor back, and gating it would defeat barge-in entirely.
DANGEROUS_KINDS = frozenset({ControlKind.SHUTDOWN, ControlKind.SLEEP})


# Phrases that name what they act on. An utterance containing the assistant's name, its
# engine, or "yourself" is unambiguous about its target in a way a bare verb is not.
#
# BOTH STILL REQUIRE CONFIRMATION — this distinction does not create an unconfirmed path. What
# it changes is the WORDING of the question and what the logs record, and it is the hook a
# future policy would use if the explicit forms ever earned a shorter road.
_EXPLICIT_TARGET_WORDS = frozenset({"kayra", "yourself", "engine"})


def names_target(phrase):
    """True when a matched control phrase says WHAT it is acting on."""
    return any(word in _EXPLICIT_TARGET_WORDS for word in str(phrase or "").split())

# Kinds a sleeping assistant still answers. Anything else spoken while asleep is ignored
# without being classified, which is the entire point of standby.
WAKE_KINDS = frozenset({ControlKind.WAKE, ControlKind.SHUTDOWN, ControlKind.RESUME_LISTENING})

# Kinds that are about a peripheral rather than the assistant's own voice lifecycle. Grouped
# so a caller can reason about "does this touch the camera?" without enumerating four strings,
# and so the shutdown boundary test can assert they are disjoint from SHUTDOWN_PHRASES.
DEVICE_KINDS = frozenset({ControlKind.GESTURE_ON, ControlKind.GESTURE_OFF,
                          ControlKind.CAMERA_ON, ControlKind.CAMERA_OFF})


class ControlCommand:
    """A matched control command. `kind` is a `ControlKind`; `phrase` is what matched."""

    __slots__ = ("kind", "phrase", "text")

    def __init__(self, kind, phrase, text=""):
        self.kind = kind
        self.phrase = phrase
        self.text = text

    @property
    def dangerous(self):
        """True when executing this without asking would be the wrong default."""
        return self.kind in DANGEROUS_KINDS

    @property
    def explicit(self):
        """
        True when the phrase NAMES its target ("shut down kayra"), rather than being a bare
        verb ("exit") that happens to live in the table.

        Used for the wording of the confirmation and for the log line. It does not create an
        unconfirmed path: both forms are confirmed.
        """
        return names_target(self.phrase)

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
    # Added this milestone. This is the ONE lifecycle command that is not confirmation-gated,
    # and it is deliberately the safest one: it closes the microphone and changes nothing else.
    # A user who says it and did not mean it presses one button to undo it; a user who means it
    # usually wants it NOW, and making them answer a question first would be the wrong trade.
    # Written WITHOUT the apostrophe, because that is the form normalization produces.
    "mute listening", "dont listen", "do not listen", "stop hearing", "stop listening now",
    "kayra stop listening", "stop listening kayra",
    # OBSERVED LIVE: "Turn off listening." was not in this table, so it fell through to the
    # DMM, which classified it as `proactive off` — and the user got "Proactive suggestions
    # disabled for this session" when they had asked for the MICROPHONE. Pausing the
    # microphone and silencing unprompted suggestions are different subsystems and different
    # requests; the DMM guessing between them is exactly what the local vocabulary is for.
    "turn off listening", "turn listening off", "turn off the listening",
    "turn off your ears", "stop the microphone", "turn off the microphone",
    "turn off mic", "turn off the mic",
]

RESUME_LISTENING_PHRASES = [
    "start listening", "resume listening", "listen again", "listen to me",
    "open the microphone", "open the mic", "unmute the microphone", "unmute the mic",
    "turn on listening", "turn listening on", "turn on the microphone", "turn on the mic",
    "start the microphone", "keep listening",
]

# Standby. Kayra stays alive, stays configured, stays in memory, and stops doing anything on
# its own. See `app.set_sleeping` for what this does and, importantly, what it does not.
SLEEP_PHRASES = [
    "go to sleep", "sleep kayra", "kayra sleep", "kayra go to sleep",
    "go to standby", "stand by", "take a nap", "go quiet", "go idle",
    # Added this milestone. "Okay Kayra, go to sleep." normalizes to "kayra go to sleep"
    # (the name is kept for the lifecycle probe, "okay" is a filler), which the fourth entry
    # above already covers; these are the remaining natural forms.
    "put kayra to sleep", "put yourself to sleep", "enter sleep mode", "sleep mode",
    "go into sleep mode", "kayra go into sleep mode", "kayra enter sleep mode",
]

WAKE_PHRASES = [
    "wake up", "wake", "wake kayra", "kayra wake up", "wake up kayra",
    "are you there", "come back",
    "kayra wake", "wake up now", "exit sleep mode", "leave sleep mode",
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
    # Added this milestone. Every one of these is CONFIRMATION-GATED, so widening the table is
    # not widening the blast radius — it is widening the set of phrases that get an "are you
    # sure?". That is the trade that makes it safe to accept the bare forms a person actually
    # says ("Okay Kayra, shut down." normalizes to "kayra shut down", which was already here;
    # "shut down." on its own was not).
    "shut down", "shutdown", "end kayra", "kayra stop running",
    # NOT "stop kayra". It reads as a shutdown and has always meant "be quiet" — the name is a
    # filler for the interrupt vocabulary, so "Kayra, stop" and "stop Kayra" are the same
    # utterance. Adding it moved a barge-in into the shutdown table, which the suite caught
    # immediately; silencing must stay the safer reading of any phrase that could be either.
    "shutdown the engine", "turn the engine off", "kayra turn the engine off",
    "turn off the kayra engine", "shut down the kayra engine", "kayra shut down the engine",
    "close the engine", "end the engine", "kill the engine",
]


# ┌──────────────── HAND GESTURE CONTROL AND THE CAMERA ────────────────┐
# Local, deterministic, and never sent to the DMM — for the same three reasons the lifecycle
# vocabulary is not: a user reaching for "turn off hand gesture control" is usually reaching
# for it because the pointer is doing something they did not ask for, and a ~700ms cloud
# round-trip on top of the ~800ms VAD finalize is a second and a half of a mouse they do not
# control. It also has to work with the network down, and it must never be able to be
# classified as something else.
#
# THE BOUNDARY THAT MATTERS: none of these phrases names Kayra, and none of them is a bare
# "turn off". "turn off kayra" is SHUTDOWN, "turn off hand gesture control" is this, "turn off
# my pc" is not in this vocabulary at all and reaches the CONFIRM-gated automation layer. The
# three sets are disjoint by construction and `tests/test_voice_control.py` asserts it phrase
# by phrase — quitting the assistant because someone asked to stop using their webcam would be
# the worst mistake this table could make.
#
# Every phrase spells the feature out. There is deliberately no bare "gesture on" / "gestures
# off": matching is exact on the whole utterance, so a short phrase buys nothing in typing and
# costs the one thing that keeps this safe, which is being unmistakable.
GESTURE_ON_PHRASES = [
    "open hand gesture control", "activate hand gesture control",
    "turn on hand gesture control", "start hand gesture control",
    "enable hand gesture control", "hand gesture control on",
    "open gesture control", "activate gesture control", "turn on gesture control",
    "start gesture control", "enable gesture control", "gesture control on",
    "turn on hand control", "start hand control", "enable hand control",
    "turn on air cursor", "start air cursor", "activate air cursor",
]

GESTURE_OFF_PHRASES = [
    "close hand gesture control", "deactivate hand gesture control",
    "turn off hand gesture control", "stop hand gesture control",
    "disable hand gesture control", "hand gesture control off",
    "close gesture control", "deactivate gesture control", "turn off gesture control",
    "stop gesture control", "disable gesture control", "gesture control off",
    "turn off hand control", "stop hand control", "disable hand control",
    "turn off air cursor", "stop air cursor", "deactivate air cursor",
]

CAMERA_ON_PHRASES = [
    "turn on camera", "turn on the camera", "start camera", "start the camera",
    "open camera", "open the camera", "enable camera", "enable the camera",
    "camera on", "turn on my camera", "turn on the webcam", "turn on webcam",
]

CAMERA_OFF_PHRASES = [
    "turn off camera", "turn off the camera", "stop camera", "stop the camera",
    "close camera", "close the camera", "disable camera", "disable the camera",
    "camera off", "turn off my camera", "turn off the webcam", "turn off webcam",
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
_register(GESTURE_ON_PHRASES, ControlKind.GESTURE_ON)
_register(GESTURE_OFF_PHRASES, ControlKind.GESTURE_OFF)
_register(CAMERA_ON_PHRASES, ControlKind.CAMERA_ON)
_register(CAMERA_OFF_PHRASES, ControlKind.CAMERA_OFF)

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

# Apostrophes are DELETED before the punctuation sweep, not turned into spaces.
#
# The sweep above replaces every non-word character with a space, which splits "don't" into
# ["don", "t"] — two tokens, neither of which is a word. That made every phrase containing an
# apostrophe unmatchable: "don't listen" never reached the pause table and "don't" never read
# as a refusal, silently, because a phrase that cannot match looks exactly like a phrase that
# was not said. Removing the mark instead yields "dont", which is a single token the tables can
# be written against.
#
# U+2019 (RIGHT SINGLE QUOTATION MARK) is included because that is what a recognizer actually
# returns — the same character that broke every contraction in `speech_safe_text` until it was
# mapped there too.
_APOSTROPHES = re.compile(r"['‘’ʼ]")


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
    cleaned = _APOSTROPHES.sub("", cleaned)
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


# ┌────────────────────────────────────────────────────────────────────────┐
# │            CONFIRMATION FOR HIGH-IMPACT CONTROL COMMANDS               │
# └────────────────────────────────────────────────────────────────────────┘
# REQUEST -> CONFIRM -> EXECUTE, and the middle step is not optional.
#
# A single recognition used to be enough to end the process. That is the wrong default for a
# system whose input is a probabilistic transcript of a room: a recognizer running `hi-IN`
# over a Hindi sentence produces "exit" as a transient reading regularly, and the cost of being
# wrong once is the entire session. So the two dangerous kinds now ASK.
#
# WHY THIS IS NOT `automation.policy.ConfirmationManager`.
# That one is the right tool for its own domain and the wrong tool here for two reasons. It
# confirms an `Action` — a structured domain/action/target/parameters record produced by the
# automation normalizer — and a lifecycle command is not one and never becomes one. And
# `core` may not import `automation`; that direction is the one the package layout forbids,
# because `automation` imports things that boot browsers. The two are deliberately similar in
# SHAPE (one pending item, bound to what was asked, expiring on a clock) so that a reader who
# knows one recognises the other, and deliberately separate in code.

# The clock the manager reads. Injectable so the suite can test expiry at exact offsets
# instead of by sleeping, which is the same rule `RuntimeState` follows.
def _confirmation_ttl():
    """
    How long an unanswered lifecycle confirmation stays open, from `.env`, clamped.

    Read lazily and defensively for the same reason the assistant name is: this module must
    stay usable before `.env` has been loaded, and a configuration failure must never make
    "stop" stop working. Clamped to 5-300s — a window of zero would make every confirmation
    unanswerable, and one of an hour would leave a shutdown armed long after the user forgot
    asking for it.
    """
    try:
        from kayra.core.config import env_float
        return env_float("KAYRA_CONFIRMATION_TTL_SECONDS", 20.0, 5.0, 300.0)
    except Exception:
        return 20.0


CONFIRMATION_TTL_SECONDS = 20.0


class ConfirmationReply:
    """How an utterance answers a pending confirmation."""

    YES = "YES"
    NO = "NO"
    # An utterance that is answer-SHAPED but not a clean answer: "yes, but first tell me my
    # options". This is NOT the same as "not an answer", and collapsing the two is how a
    # dangerous action gets executed off a sentence that was really a question. UNCLEAR
    # re-asks; None lets the utterance through as an ordinary command.
    UNCLEAR = "UNCLEAR"


# Whole-utterance vocabularies, matched after fillers are stripped. Conservative on purpose:
# every word here has to be unambiguous ON ITS OWN, because the thing it authorises cannot be
# undone by saying something else afterwards.
CONFIRM_YES_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "sure", "confirm", "confirmed", "affirmative",
    "correct", "right", "proceed", "continue", "do", "it", "go", "ahead", "please",
    "definitely", "absolutely", "ok", "okay",
    # Hindi / Hinglish, for the same reason the interrupt table carries them: with
    # INPUT_LANGUAGE=hi-IN the recognizer returns Devanagari for spoken English too.
    "हाँ", "हां", "जी", "ठीक", "बिल्कुल", "करो", "यस",
})

CONFIRM_NO_WORDS = frozenset({
    "no", "nope", "nah", "cancel", "cancelled", "stop", "dont", "never", "mind",
    "nevermind", "not", "now", "negative", "abort", "forget", "wait",
    "नहीं", "नही", "मत", "रुको", "नो",
})

# Phrases that read as a clean answer even though they are several words. Kept as an explicit
# list rather than derived, because "go ahead" being YES is a fact about English and not
# something a word-set can be trusted to compose.
_CONFIRM_YES_PHRASES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "confirm", "do it", "go ahead", "please do",
    "yes please", "yeah go ahead", "yes do it", "go for it", "that is right",
    "ok do it", "okay do it", "yes confirm", "affirmative", "proceed",
    "हाँ", "हां", "जी हाँ", "ठीक है", "बिल्कुल", "कर दो",
})

_CONFIRM_NO_PHRASES = frozenset({
    "no", "nope", "nah", "cancel", "dont", "do not", "never mind", "nevermind",
    "not now", "stop", "abort", "forget it", "no thanks", "no thank you", "negative",
    "no cancel", "dont do it", "do not do it",
    "नहीं", "नही", "मत करो", "रहने दो",
})

# Beyond this many words an utterance is a sentence, not an answer. "yes but first tell me
# what my options are" is eight words and is not authorisation for anything.
MAX_CONFIRMATION_WORDS = 4


def _restates(words, kind):
    """
    Do these words restate the pending action rather than introduce a new subject?

    "Yes yes go to sleep" is FIVE words, which the length rule below would otherwise send to
    UNCLEAR — and it was observed live doing exactly that, ending up in the DMM and answered
    with "Sleep well". It is plainly an affirmative: the user said yes and then repeated the
    thing they were being asked about.

    So an utterance may exceed the length limit when every word beyond the affirmative belongs
    to the phrase for the PENDING kind. That is a narrow allowance and a checkable one — it
    admits "yes, go to sleep" against a pending sleep, and refuses "yes, open chrome" against
    the same, because "open" and "chrome" are not words in any sleep phrase.
    """
    phrases = phrases_for(kind) if kind else ()
    vocabulary = {word for phrase in phrases for word in phrase.split()}
    if not vocabulary:
        return False
    # Polarity words count as part of the restatement too. "Yes yes go to sleep" repeats the
    # affirmative before repeating the request, and "no, don't go to sleep" negates before
    # repeating it — both are answers, and neither introduces a new subject. What is still
    # refused is a word belonging to NEITHER set, which is what keeps "yes, open chrome" from
    # reading as an answer to a sleep question.
    allowed = vocabulary | CONFIRM_YES_WORDS | CONFIRM_NO_WORDS
    return all(word in allowed for word in words)


def read_confirmation(text, assistant_name=None, pending_kind=None):
    """
    How does this utterance answer a pending confirmation?

    Returns `ConfirmationReply.YES`, `.NO`, `.UNCLEAR`, or **None** when the utterance is not
    an answer at all and should be treated as a new command.

    THE THREE-WAY RESULT IS THE POINT. A two-way one forces every non-"yes" into either
    "execute" or "ignore", and both are wrong for the interesting case:

        "Yeah, go ahead."                 -> YES      (clean, and it is the whole utterance)
        "Yeah, but first tell me..."      -> UNCLEAR  (contains yes, is not an answer)
        "What time is it?"                -> None     (an ordinary question; answer it)

    Matching is on the WHOLE cleaned utterance, never a substring — the same rule that keeps
    "stop the music" from being swallowed as a barge-in. A sentence that merely CONTAINS
    "yes" authorises nothing.
    """
    stripped, _named = normalize_utterance(text, assistant_name)
    if not stripped:
        return None

    joined = " ".join(stripped)

    # 1. A clean, whole-utterance answer.
    if joined in _CONFIRM_YES_PHRASES:
        return ConfirmationReply.YES
    if joined in _CONFIRM_NO_PHRASES:
        return ConfirmationReply.NO

    # 2. A short utterance built ENTIRELY out of one polarity's words. This is what accepts
    #    "yes yes", "sure go ahead", "no not now" without listing every combination.
    if len(stripped) <= MAX_CONFIRMATION_WORDS:
        if all(w in CONFIRM_YES_WORDS for w in stripped):
            return ConfirmationReply.YES
        if all(w in CONFIRM_NO_WORDS for w in stripped):
            return ConfirmationReply.NO

    # 2b. AN AFFIRMATIVE THAT RESTATES THE PENDING ACTION. "Yes yes go to sleep" against a
    #     pending SLEEP is an answer, not a new subject — see `_restates`. Only the words that
    #     belong to the pending kind's own phrases are allowed past the length limit, so
    #     "yes, open chrome" is still not an answer to anything.
    if pending_kind and len(stripped) <= MAX_CONFIRMATION_WORDS * 2:
        lead = stripped[0]
        rest = stripped[1:]
        # The LEAD word decides the polarity, and it is the only one that may. "No, don't go
        # to sleep" opens with a refusal and stays one however many affirmative-adjacent
        # words follow; reading the polarity from anywhere else would let a trailing word
        # flip an answer the user already gave.
        if rest and _restates(rest, pending_kind):
            if lead in CONFIRM_NO_WORDS:
                return ConfirmationReply.NO
            if lead in CONFIRM_YES_WORDS:
                return ConfirmationReply.YES

    # 3. Answer-shaped but not an answer: it OPENS with a polarity word and then keeps going.
    #    "Yeah, but first tell me what my options are." The user is engaging with the question
    #    and has not answered it, so the honest response is to ask again — executing would be
    #    acting on a sentence that was really a request for information.
    first = stripped[0]
    if first in CONFIRM_YES_WORDS or first in CONFIRM_NO_WORDS:
        return ConfirmationReply.UNCLEAR

    # 4. Not about the question at all.
    return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │        DEGRADED SHORT ANSWERS INSIDE AN ACTIVE CONFIRMATION            │
# └────────────────────────────────────────────────────────────────────────┘
# OBSERVED LIVE: the user says "yes" and the recognizer commits **"S"**.
#
# That is not a mishearing of a word — it is the recognizer catching only the sibilant of a
# short, quietly-spoken reply. It matters far more than it looks, because "yes" is the word
# that authorises a shutdown.
#
# THE FIX THAT IS EXPLICITLY FORBIDDEN, and would be the obvious one:
#
#     if transcript == "s": transcript = "yes"          # NO.
#
# A global substitution corrupts ordinary conversation — "Tell me about S", "My grade is S" —
# and it is the word-replacement dictionary this codebase refuses everywhere else. It is also
# unnecessary, because the situation supplies three pieces of evidence a general corrector
# never has:
#
#   1. CONTEXT   — a confirmation is pending, so the space of sensible replies is two words.
#   2. N-BEST    — the recognizer's OWN alternatives for this utterance. If it offered "yes"
#                  as a second reading, preferring that is re-ranking, not invention.
#   3. SHAPE     — a confirmation reply is one or two words. A sentence is not an answer.
#
# So this runs ONLY inside an active confirmation, only on an utterance short enough to be an
# answer, and only when the plain reading produced nothing. Outside a confirmation none of it
# is reachable, and "S" stays "S".
#
# THE ASYMMETRY IS DELIBERATE. A recovered NO cancels something, which is safe in the
# direction that matters; a recovered YES authorises it. So YES needs a unique, unambiguous
# candidate, and anything that could also be a NO is refused outright.

# The reply words a degraded token may be recovered TO. Deliberately the short, common ones —
# a user answering a confirmation says one of these, and nothing is gained by making the
# recovery space larger than the space of things people actually say.
_RECOVERABLE_YES = ("yes", "yeah", "yep", "yup", "sure", "okay")
_RECOVERABLE_NO = ("no", "nope", "nah", "cancel")

# A degraded token this long is not a fragment of a short word, it is a different word.
MAX_DEGRADED_TOKEN = 3

# Above this the recognizer was confident, so its reading stands. `None` (no confidence
# reported) is treated as unknown and therefore eligible, which is the common case here —
# the Web Speech API frequently omits confidence for very short utterances.
DEGRADED_CONFIDENCE_CEILING = 0.85


def _fragment_of(token, word):
    """
    Is `token` a plausible partial recognition of `word`?

    A SUFFIX or a PREFIX, and nothing else. "s" is the tail of "yes"; "ye" is its head. This
    is not an edit distance and not a phonetic key: both of those are similarity measures, and
    similarity is how "yes" ends up recovered from "yet", "mess" or "guess". A strict affix is
    a statement about the same word being partially heard.

    The token must also be genuinely shorter than the word — a token equal to it is not a
    fragment, it is the word, and would already have matched the plain reading.
    """
    if not token or not word or len(token) >= len(word):
        return False
    return word.startswith(token) or word.endswith(token)


def resolve_short_answer(text, alternatives=None, confidence=None, assistant_name=None):
    """
    Recover a YES/NO from a degraded short reply. Returns `(reply, evidence)` or `(None, "")`.

    CALLED ONLY WITH A CONFIRMATION PENDING — `ControlConfirmations.answer` is the sole caller
    and it has already established that. Every guard below is on top of that precondition.

    Two routes, tried in order of how much they invent, which is none and then almost none:

      1. **The recognizer's own N-best.** If it offered "yes" as an alternative reading of this
         utterance, that reading is the recognizer's, not ours. This is the same asymmetry the
         transcript-repair stage documents: preferring a reading the recognizer PROPOSED is
         re-ranking; producing one it never proposed is invention.

      2. **A strict affix of exactly one reply word**, under every guard at once: a single
         token, at most `MAX_DEGRADED_TOKEN` characters, confidence low or unknown, and no
         competing candidate of either polarity. "s" -> "yes" passes. "school", "system",
         "its" do not — they are too long. "o" would match "no" alone and is allowed to
         cancel, never to confirm.
    """
    stripped, _named = normalize_utterance(text, assistant_name)
    if not stripped:
        return None, ""

    # ── Route 1: the recognizer's own alternatives ──
    # No length or confidence guard is needed here, because nothing is being invented: if the
    # recognizer offered a clean "yes" for this audio, that is its reading of it.
    for alternative in (alternatives or []):
        candidate = alternative.get("text") if isinstance(alternative, dict) else alternative
        if not candidate:
            continue
        reply = read_confirmation(candidate, assistant_name)
        if reply in (ConfirmationReply.YES, ConfirmationReply.NO):
            return reply, f"n-best:{str(candidate).strip()[:24]!r}"

    # ── Route 2: a strict affix, under every guard ──
    if len(stripped) != 1:
        return None, ""                     # a sentence is not an answer
    token = stripped[0]
    if len(token) > MAX_DEGRADED_TOKEN:
        return None, ""                     # long enough to be its own word
    if confidence is not None and confidence >= DEGRADED_CONFIDENCE_CEILING:
        return None, ""                     # the recognizer was sure; its reading stands

    yes_hits = [w for w in _RECOVERABLE_YES if _fragment_of(token, w)]
    no_hits = [w for w in _RECOVERABLE_NO if _fragment_of(token, w)]

    # THE UNIQUENESS THAT MATTERS IS POLARITY, NOT THE WORD.
    #
    # "s" is the tail of "yes" AND the head of "sure". An earlier version demanded exactly one
    # candidate word and therefore refused it — which is the one case this whole function was
    # written for. Both candidates are affirmatives, so there is nothing to be ambiguous
    # about: whichever the user said, the answer is yes.
    #
    # What is genuinely ambiguous is a token that could be either polarity, because those two
    # outcomes are "execute the shutdown" and "cancel it". That stays a refusal.
    if yes_hits and no_hits:
        return None, ""
    if yes_hits:
        return ConfirmationReply.YES, f"fragment:{token!r} of {'/'.join(yes_hits)}"
    if no_hits:
        return ConfirmationReply.NO, f"fragment:{token!r} of {'/'.join(no_hits)}"
    return None, ""


class PendingConfirmation:
    """One outstanding question, bound to the command that raised it."""

    __slots__ = ("kind", "phrase", "text", "asked_at", "expires_at", "asks", "turn")

    def __init__(self, kind, phrase, text, asked_at, ttl, asks=1, turn=0):
        self.kind = kind
        self.phrase = phrase
        self.text = text
        self.asked_at = asked_at
        self.expires_at = asked_at + ttl
        self.asks = asks
        # The turn that RAISED the question. Logged when it is answered, so a reader can pair
        # "turn #19 confirmation: YES" with the "turn #18 confirmation required" that caused
        # it — the two are different turns by definition and reading them as one is how an
        # overlapping log becomes unreadable.
        self.turn = turn

    def expired(self, now):
        return now >= self.expires_at

    def __repr__(self):
        return f"PendingConfirmation({self.kind}, {self.phrase!r})"


class ControlConfirmations:
    """
    At most ONE pending lifecycle confirmation, with a bounded lifetime.

    One, not a queue: two outstanding "are you sure?"s about different things is a state no
    user can answer unambiguously, so a new dangerous request REPLACES the old one rather than
    stacking behind it.

    Thread-safe, because the answer can arrive on the turn loop while the question was asked
    from the control watcher — the same two threads that already share `RuntimeState`.
    """

    # An ambiguous answer gets one more question and no more. Asking a third time is a loop,
    # and a user who has been asked twice and not answered plainly did not want this.
    MAX_ASKS = 2

    def __init__(self, ttl=None, clock=None):
        # Why the last answer was read the way it was, for the log line. "" when the plain
        # reading was enough, which is the overwhelmingly common case.
        self.last_evidence = ""
        self._ttl = float(_confirmation_ttl() if ttl is None else ttl)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._pending = None

    # ── State ──

    @property
    def pending(self):
        """The outstanding request, or None. Expiry is evaluated on READ, not on a timer."""
        with self._lock:
            if self._pending is None:
                return None
            if self._pending.expired(self._clock()):
                self._pending = None
                return None
            return self._pending

    def request(self, command, turn=0):
        """Raises a confirmation for `command`. Returns the `PendingConfirmation`."""
        with self._lock:
            now = self._clock()
            self._pending = PendingConfirmation(
                command.kind, command.phrase, command.text, now, self._ttl, turn=turn)
            return self._pending

    def clear(self):
        with self._lock:
            previous, self._pending = self._pending, None
            return previous

    def expire_if_due(self):
        """Returns the request that just expired, or None. For the caller that logs it."""
        with self._lock:
            if self._pending is not None and self._pending.expired(self._clock()):
                expired, self._pending = self._pending, None
                return expired
            return None

    # ── Answering ──

    def answer(self, text, assistant_name=None, echo=False, alternatives=None,
               confidence=None):
        """
        Applies `text` to the pending request.

        Returns `(outcome, request)` where outcome is one of:

            "execute"   -> confirmed. `request` is what to run. Cleared.
            "cancel"    -> refused. Cleared.
            "reask"     -> ambiguous, and we have an ask left. Still pending.
            "cancel"    -> ambiguous and out of asks (see MAX_ASKS).
            "none"      -> not an answer. The request is CLEARED and the utterance is the
                           caller's to process normally.

        THE "none" CASE CLEARS THE REQUEST DELIBERATELY. A user who was asked "shall I shut
        down?" and replied "what's the weather" has moved on; leaving the question armed means
        a "yes" to some later, unrelated exchange could execute it. That is the same reasoning
        the automation confirmation follows, and it is the safer direction: the cost of
        clearing is that the user repeats a command, and the cost of not clearing is a
        shutdown nobody asked for.
        """
        with self._lock:
            request = self.pending
            self.last_evidence = ""
            if request is None:
                return "none", None

            # ── A RESTATEMENT OF THE SAME REQUEST IS NOT A NEW REQUEST ──
            # OBSERVED: a noisy recognizer produces "exit" repeatedly. Each one used to clear
            # the pending confirmation (it is not an answer) and then raise a fresh one, so
            # Kayra asked the shutdown question over and over. The user is not answering and
            # is not asking for something new — they are being misheard.
            #
            # Checked BEFORE the reply is read, because "exit" is not a yes and not a no, and
            # anything that treats it as "not an answer" reaches the clear-and-re-ask path.
            restated = classify_control(text, assistant_name)
            if restated is not None and restated.kind == request.kind:
                return "restated", request

            reply = read_confirmation(text, assistant_name, pending_kind=request.kind)

            # ── DEGRADED SHORT ANSWERS ──
            # Only when the plain reading produced nothing, only inside an active
            # confirmation, and only under the guards in `resolve_short_answer`. See that
            # function for why this is re-ranking rather than a replacement table.
            if reply is None and not echo:
                recovered, evidence = resolve_short_answer(
                    text, alternatives=alternatives, confidence=confidence,
                    assistant_name=assistant_name)
                if recovered is not None:
                    self.last_evidence = evidence
                    reply = recovered

            # ── ECHO-FLAGGED AUDIO CLEARS A HIGHER BAR ──
            # `echo=True` means the capture-timestamp gate believes this was Kayra's own voice
            # coming back through the microphone. Such audio is not discarded here — the user's
            # answer often overlaps the question and discarding it is the failure mode this
            # whole path exists to avoid — but only a CLEAN whole-utterance YES or NO is
            # honoured from it.
            #
            # The case that makes this necessary is Kayra's own question. "Just to confirm —
            # should I shut down the Kayra engine?" opens with a word in the affirmative
            # vocabulary and therefore reads as UNCLEAR, which would have her re-ask herself
            # in a loop. She cannot produce a bare "yes", so the clean forms stay reachable.
            if echo and reply not in (ConfirmationReply.YES, ConfirmationReply.NO):
                # Anything that is not a clean answer, arriving on audio the timestamp gate
                # attributes to Kayra herself, is nobody speaking. It must not execute, must
                # not cancel, and — the part that is easy to get wrong — must not CLEAR the
                # request either: her own question reads as "not an answer", so clearing on it
                # would have her cancel her own confirmation a moment after asking it.
                return "none-echo", request

            if reply == ConfirmationReply.YES:
                self._pending = None
                return "execute", request
            if reply == ConfirmationReply.NO:
                self._pending = None
                return "cancel", request
            if reply == ConfirmationReply.UNCLEAR:
                if request.asks >= self.MAX_ASKS:
                    self._pending = None
                    return "cancel", request
                request.asks += 1
                return "reask", request

            self._pending = None
            return "none", request

    # NOTE on the two "nothing happened" outcomes, which are deliberately different:
    #   "none"      the user said something unrelated. They have moved on, so the request is
    #               CLEARED — leaving it armed means a "yes" to some later exchange could
    #               execute it.
    #   "none-echo" Kayra heard herself. Nobody said anything, so the request STAYS PENDING
    #               and the user still has their full window to answer.


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     WHAT KAYRA ASKS, IN WORDS                          │
# └────────────────────────────────────────────────────────────────────────┘
# Spoken, so they are short and contain no markdown, no lists and no options. Each names
# EXACTLY what will happen and to what — "shut down the Kayra engine", never "shut down your
# computer", because those are different actions and the user asked for one of them.

def confirmation_question(kind, explicit=True, address=""):
    """The sentence Kayra says to ask. `address` is an optional form of address ("sir")."""
    suffix = f", {address}" if address else ""
    if kind == ControlKind.SHUTDOWN:
        if explicit:
            return f"Just to confirm{suffix} — should I shut down the Kayra engine?"
        # A bare verb. The question says out loud what it THINKS it heard, so a user whose
        # sentence was mis-transcribed hears the mistake instead of the consequence.
        return (f"I heard something that sounded like a shutdown request{suffix}. "
                "Should I shut down the Kayra engine?")
    if kind == ControlKind.SLEEP:
        return (f"Would you like me to enter sleep mode and stop responding{suffix}? "
                "I will keep listening for you to wake me.")
    return f"Should I do that{suffix}?"


def confirmation_ack(kind, confirmed):
    """The one-line answer after the user has decided."""
    if not confirmed:
        if kind == ControlKind.SHUTDOWN:
            return "Cancelled. I am still here."
        if kind == ControlKind.SLEEP:
            return "Cancelled. Still listening."
        return "Cancelled."
    if kind == ControlKind.SHUTDOWN:
        return "Understood. Shutting down Kayra."
    if kind == ControlKind.SLEEP:
        return "Understood. Entering sleep mode."
    return "Understood."


def confirmation_expired_line(kind):
    """Said when nobody answered. Deliberately quiet — nothing happened."""
    return ""


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
            ControlKind.SLEEP, ControlKind.WAKE, ControlKind.SHUTDOWN,
            ControlKind.GESTURE_ON, ControlKind.GESTURE_OFF,
            ControlKind.CAMERA_ON, ControlKind.CAMERA_OFF)


def phrases_for(kind):
    """Every phrase that classifies to `kind`. Diagnostics and tests only."""
    return tuple(sorted(p for p, k in _KIND_BY_PHRASE.items() if k == kind))
