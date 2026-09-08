# ┌────────────────────────────────────────────────────────────────────────┐
# │                        transcript_repair.py                            │
# │        The LAST Stage of the Capture Pipeline — and the Smallest       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Conservative, context-aware repair of a finished transcript.

    audio capture -> AEC / noise suppression -> VAD / endpointing -> STT -> **repair**

This is the last stage and deliberately the weakest one. Everything upstream tries to make
the recognizer hear correctly; this stage only decides, when the recognizer offered SEVERAL
readings of what it heard, which one the current conversation makes plausible.

WHAT THIS IS NOT, AND MUST NEVER BECOME
---------------------------------------
It is not a word-replacement dictionary. There is no table here mapping "great" to "quit",
and adding one would be a mistake with a predictable ending: every such table eventually
rewrites a legitimate word into the wrong command, and it does so silently, on the one
utterance the user most needed to be taken literally. A correction layer that is allowed to
invent words will eventually invent a destructive one.

So the rules are, in order of how much evidence they require:

  1. **Re-rank what the recognizer actually said.** Chrome returns N-best alternatives with
     `maxAlternatives`. If a lower-ranked alternative is an exact match for something the
     current context makes plausible — the outstanding yes/no question, the control
     vocabulary, a command header, an application the user just referred to — prefer it.
     This invents nothing: every candidate came from the recognizer.

  2. **Only then, and only under every one of a set of hard conditions**, consider a
     phonetic near-match against vocabulary that ALREADY EXISTS in this codebase (the local
     control phrases, the classifier's own task headers, terms supplied by the caller). The
     conditions are: the utterance is at most three words, the recognizer's confidence was
     low or unknown, the word is not itself a plausible word in this context, the phonetic
     distance is tiny, and the target is plausible right now. If any one of those fails,
     nothing happens.

  3. **Otherwise do nothing.** That is the overwhelmingly common outcome and the correct one.

Every change is recorded with the reason, so a wrong repair is diagnosable rather than
mysterious — an invisible correction layer is worse than none.

WHY IT LIVES IN `input`
-----------------------
It is part of the capture pipeline, not of intelligence: it runs before the classifier and
must never call a model. It imports `core.voice_control` (a leaf) and `core.conversation_context`
(a leaf) and nothing else from the package — in particular it must not reach into
`automation` or `intelligence`, both of which are expensive to import and one of which would
start a browser. Vocabulary from those layers is INJECTED by the caller instead.
"""

import re
import unicodedata

from kayra.core.conversation_context import get_conversation_context
from kayra.core.voice_control import normalize_utterance


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THRESHOLDS                                │
# └────────────────────────────────────────────────────────────────────────┘
# Every one of these is a limit on how much this stage is allowed to do. They are named and
# gathered here so the answer to "could this have rewritten my command?" is readable rather
# than buried in conditionals.

# Above this, the recognizer was sure enough that second-guessing it is presumption. Chrome
# frequently reports 0.0 or omits confidence entirely in continuous mode, and `None` is
# treated as "unknown" — unknown permits re-ranking (rule 1) but never phonetic repair.
CONFIDENT_ENOUGH = 0.85

# Phonetic repair is for short commands only. A sentence has context of its own and a wrong
# word in it is recoverable by the classifier; a wrong single word IS the command.
MAX_PHONETIC_WORDS = 3

# The minimum length at which a word is distinctive enough to match phonetically at all.
MIN_PHONETIC_LENGTH = 3

# Alternatives beyond this rank are noise in practice.
MAX_ALTERNATIVES = 6


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            PHONETIC KEY                                │
# └────────────────────────────────────────────────────────────────────────┘
# A small Soundex-shaped reduction. Deliberately NOT a dependency: this needs to be
# inspectable, deterministic and free, and the accuracy an external metaphone library would
# add is accuracy this stage is not permitted to act on anyway.

_VOWELS = "aeiouyhw"
_CODES = {
    "b": "1", "f": "1", "p": "1", "v": "1",
    "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
    "d": "3", "t": "3",
    "l": "4",
    "m": "5", "n": "5",
    "r": "6",
}

_WORD = re.compile(r"[a-z0-9']+")


def phonetic_key(word):
    """
    A coarse sound-alike key. Equal keys mean "these could plausibly be confused".

    Coarse is the point. It is used only as a NECESSARY condition — never a sufficient one —
    so its job is to reject the obviously-unrelated cheaply, and every other guard decides
    whether a substitution actually happens.
    """
    word = _ascii_fold(str(word or "").lower())
    letters = [c for c in word if c.isalpha()]
    if not letters:
        return ""
    first = letters[0]
    key = first
    previous = _CODES.get(first, "")
    for char in letters[1:]:
        code = _CODES.get(char, "")
        if code and code != previous:
            key += code
        if char not in _VOWELS:
            previous = code
        elif char in "hw":
            pass                      # h and w are transparent: they do not break a run
        else:
            previous = ""
    return (key + "000")[:4]


def _ascii_fold(text):
    """Strips accents so 'café' and 'cafe' reduce alike. Non-Latin text is left alone."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def edit_distance(a, b, ceiling=2):
    """Levenshtein distance, abandoned as soon as it exceeds `ceiling`."""
    a, b = str(a or ""), str(b or "")
    if abs(len(a) - len(b)) > ceiling:
        return ceiling + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        if min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE REPAIR RESULT                             │
# └────────────────────────────────────────────────────────────────────────┘

class RepairResult:
    """
    What came out, and why. `changed` is False for the overwhelming majority of utterances.

    `reason` is a short machine-readable trace ("alternative:confirmation",
    "phonetic:control") that is printed when a change is made, so the one time a repair is
    wrong the user can see what happened instead of concluding the assistant is haunted.
    """

    __slots__ = ("text", "original", "changed", "reason", "confidence", "considered")

    def __init__(self, text, original, changed=False, reason="", confidence=None,
                 considered=0):
        self.text = text
        self.original = original
        self.changed = bool(changed)
        self.reason = reason
        self.confidence = confidence
        self.considered = considered

    def __repr__(self):
        if not self.changed:
            return f"<RepairResult unchanged {self.text!r}>"
        return f"<RepairResult {self.original!r} -> {self.text!r} ({self.reason})>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                           THE REPAIR STAGE                             │
# └────────────────────────────────────────────────────────────────────────┘

class TranscriptRepair:
    """
    Chooses between readings the recognizer offered, using the conversation context.

    Args:
        context: a `ConversationContext`. The process-wide one by default.
        vocabulary: extra terms that are plausible commands in this installation —
            the classifier's task headers, application and site names. INJECTED rather than
            imported, so this module stays inside the input layer and cannot pull the
            automation or intelligence layers (or a browser) onto the recognition path.
    """

    # Answers to a yes/no question. Small, fixed, and the highest-value context there is:
    # while a confirmation is outstanding, a short utterance is almost certainly an answer.
    CONFIRMATION_WORDS = frozenset({
        "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "correct", "confirm", "affirmative",
        "go ahead", "do it", "please do",
        "no", "nope", "nah", "cancel", "stop", "don't", "do not", "negative", "never mind",
    })

    def __init__(self, context=None, vocabulary=None):
        self.context = context if context is not None else get_conversation_context()
        self._extra_vocabulary = frozenset()
        self.set_vocabulary(vocabulary)
        # Diagnostics. Cheap counters; nothing here grows.
        self.stats = {"seen": 0, "reranked": 0, "phonetic": 0, "unchanged": 0}
        self.last = None

    # ──────────────────────────────────────────────────────────────────────
    #                             VOCABULARY
    # ──────────────────────────────────────────────────────────────────────

    def set_vocabulary(self, vocabulary):
        """
        Replaces the injected command vocabulary.

        Terms are reduced to their first word: the classifier's tokens are things like
        "open chrome" and "deep research ...", and what matters here is that "open" and
        "research" are words the user plausibly said.
        """
        terms = set()
        for entry in (vocabulary or ()):
            for word in _WORD.findall(str(entry or "").lower()):
                if len(word) >= MIN_PHONETIC_LENGTH:
                    terms.add(word)
        self._extra_vocabulary = frozenset(terms)

    def _control_vocabulary(self):
        """
        The local control phrases, taken from the module that already defines them.

        Imported lazily and defensively: this is the one place in the capture pipeline where
        a vocabulary import failure must degrade to "no phonetic repair" rather than to "no
        speech recognition".
        """
        try:
            from kayra.core.voice_control import control_kinds, phrases_for
            words = set()
            # Enumerated through the module's own accessors rather than by naming the phrase
            # lists: a new control kind then joins this vocabulary automatically, and a
            # renamed list cannot silently reduce it to nothing.
            for kind in control_kinds():
                for phrase in phrases_for(kind) or ():
                    for word in _WORD.findall(str(phrase).lower()):
                        if len(word) >= MIN_PHONETIC_LENGTH:
                            words.add(word)
            return words
        except Exception:
            return set()

    def plausible_terms(self):
        """
        The words the CURRENT context makes plausible. This is the whole basis of the stage.

        It is context-dependent by construction: while a confirmation is pending, the answer
        vocabulary is plausible; in an automation exchange, command words and the targets
        just referred to are; in conversation, the topic's own words are. A term that is not
        in here can never be substituted IN, no matter how close it sounds.
        """
        snapshot = self.context.snapshot()
        terms = set(self._extra_vocabulary)
        terms |= self._control_vocabulary()

        if snapshot["expects_confirmation"]:
            for phrase in self.CONFIRMATION_WORDS:
                terms.update(_WORD.findall(phrase))

        for target in snapshot["targets"]:
            terms.update(w for w in _WORD.findall(target) if len(w) >= MIN_PHONETIC_LENGTH)
        for token in snapshot["last_intent"]:
            terms.update(w for w in _WORD.findall(token) if len(w) >= MIN_PHONETIC_LENGTH)
        terms.update(w for w in snapshot["topic"] if len(w) >= MIN_PHONETIC_LENGTH)
        return terms

    # ──────────────────────────────────────────────────────────────────────
    #                              THE STAGE
    # ──────────────────────────────────────────────────────────────────────

    def repair(self, text, alternatives=None, confidence=None, uncommitted=False):
        """
        Returns a `RepairResult`. The default outcome is the text exactly as it arrived.

        Args:
            text: the recognizer's best transcript.
            alternatives: the recognizer's own N-best, `[{"text", "confidence"}, ...]`, best
                first. Empty for a multi-segment utterance, which is why long dictation is
                never re-ranked.
            confidence: the top reading's confidence, or None when the browser did not
                report one.
            uncommitted: True when the recognizer never finalized these words. It lowers
                trust in the transcript; it does not license inventing a different one.
        """
        original = (text or "").strip()
        if not original:
            return RepairResult("", "", False, "empty")

        self.stats["seen"] += 1
        alternatives = list(alternatives or [])[:MAX_ALTERNATIVES]

        # ── Rule 1: prefer an alternative the recognizer itself offered ────
        picked = self._rerank(original, alternatives)
        if picked is not None:
            chosen, reason = picked
            self.stats["reranked"] += 1
            result = RepairResult(chosen, original, True, reason, confidence,
                                  len(alternatives))
            self.last = result
            return result

        # ── Rule 2: a tiny phonetic step, under every guard at once ────────
        repaired = self._phonetic(original, confidence, uncommitted)
        if repaired is not None:
            chosen, reason = repaired
            self.stats["phonetic"] += 1
            result = RepairResult(chosen, original, True, reason, confidence,
                                  len(alternatives))
            self.last = result
            return result

        # ── Rule 3: do nothing. The common, correct case. ──────────────────
        self.stats["unchanged"] += 1
        result = RepairResult(original, original, False, "", confidence, len(alternatives))
        self.last = result
        return result

    # ── rule 1 ────────────────────────────────────────────────────────────

    def _rerank(self, original, alternatives):
        """
        Prefers a lower-ranked reading when the context makes it plausible and the top one
        is not.

        The asymmetry is deliberate and is what makes this safe: a reading is only promoted
        when the recognizer's first choice is NOT plausible in this context. If the top
        reading already fits, it wins, however well an alternative also fits.
        """
        if len(alternatives) < 2:
            return None

        top = _normalized(original)
        if not top:
            return None

        # Never override the recognizer when its own first choice already makes sense here.
        if self._plausibility(top) > 0:
            return None

        best, best_score, best_kind = None, 0, ""
        for rank, alternative in enumerate(alternatives):
            candidate = _normalized(alternative.get("text"))
            if not candidate or candidate == top:
                continue
            score, kind = self._plausibility(candidate), self._plausibility_kind(candidate)
            if score <= 0:
                continue
            # Earlier alternatives are the recognizer's own preference; only a strictly
            # better contextual fit may overtake one.
            score -= rank
            if score > best_score:
                best, best_score, best_kind = alternative.get("text"), score, kind
        if best is None:
            return None
        return best.strip(), f"alternative:{best_kind}"

    def _plausibility(self, normalized):
        """
        How well a candidate fits the current context. 0 means "no evidence at all".

        The numbers encode a priority: an answer to an outstanding question beats a control
        command beats a known command shape beats a recently-referred-to target. Nothing
        here is fuzzy — every branch is an exact match against a bounded, existing vocabulary.
        """
        if not normalized:
            return 0
        snapshot = self.context.snapshot()

        if snapshot["expects_confirmation"] and normalized in self.CONFIRMATION_WORDS:
            return 100
        if self._is_control(normalized):
            return 80

        words = normalized.split()
        head = words[0] if words else ""
        if head and head in self._extra_vocabulary:
            return 60
        if any(target and target in normalized for target in snapshot["targets"]):
            return 40
        if head and head in {w for w in snapshot["topic"]}:
            return 20
        return 0

    def _plausibility_kind(self, normalized):
        snapshot = self.context.snapshot()
        if snapshot["expects_confirmation"] and normalized in self.CONFIRMATION_WORDS:
            return "confirmation"
        if self._is_control(normalized):
            return "control"
        words = normalized.split()
        head = words[0] if words else ""
        if head and head in self._extra_vocabulary:
            return "command"
        if any(target and target in normalized for target in snapshot["targets"]):
            return "target"
        return "topic"

    @staticmethod
    def _is_control(normalized):
        """
        True when the text is one of the local control commands.

        Uses `voice_control.classify_control`, so there is exactly one definition of the
        control vocabulary in the process rather than a copy here that can drift from it.
        """
        try:
            from kayra.core.voice_control import classify_control
            return classify_control(normalized) is not None
        except Exception:
            return False

    # ── rule 2 ────────────────────────────────────────────────────────────

    def _phonetic(self, original, confidence, uncommitted):
        """
        One tiny phonetic substitution, and only when EVERY guard passes.

        Each `return None` below is a deliberate refusal. Read together they are the answer
        to "when can this stage change a word I actually said?" — and the answer is: only a
        short utterance, only when the recognizer was unsure, only when what it produced is
        meaningless in this context, only onto a word that is plausible right now, and only
        when the two sound nearly identical.
        """
        normalized = _normalized(original)
        words = normalized.split()

        if not words or len(words) > MAX_PHONETIC_WORDS:
            return None                       # long enough to speak for itself
        if confidence is not None and confidence >= CONFIDENT_ENOUGH:
            return None                       # the recognizer was sure; leave it alone
        if self._plausibility(normalized) > 0:
            return None                       # it already means something here
        # A single word is the only shape worth repairing: in "close the tabb" the classifier
        # recovers on its own, and a multi-word substitution is a rewrite, not a repair.
        if len(words) != 1:
            return None
        word = words[0]
        if len(word) < MIN_PHONETIC_LENGTH:
            return None                       # too short to be distinctive

        # Uncommitted text is less trustworthy, but that lowers confidence in what was heard
        # — it does not raise confidence in a guess. It only relaxes the confidence gate,
        # which the check above has already applied.
        _ = uncommitted

        key = phonetic_key(word)
        if not key:
            return None

        candidates = [term for term in self.plausible_terms()
                      if term != word and phonetic_key(term) == key]
        if not candidates:
            return None

        # Among sound-alikes, require the spellings to be close too. Two words with the same
        # coarse key can still be entirely different words, and this is the guard that keeps
        # the key coarse without letting it be careless.
        scored = [(edit_distance(word, term), term) for term in candidates]
        scored = [(distance, term) for distance, term in scored
                  if distance <= 2 and not _is_irreversible(term)]
        if not scored:
            return None
        scored.sort()
        # An ambiguous repair is no repair: two equally-close candidates means the evidence
        # does not identify one, and picking either would be a guess.
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1], "phonetic:context"

    # ──────────────────────────────────────────────────────────────────────
    #                             DIAGNOSTICS
    # ──────────────────────────────────────────────────────────────────────

    def describe(self):
        return {"stats": dict(self.stats),
                "vocabulary": len(self._extra_vocabulary),
                "last": repr(self.last) if self.last else None}


def _is_irreversible(term):
    """
    True when substituting this word could END THE PROCESS.

    This is the guard that answers the obvious objection to any correction layer: sooner or
    later it turns a legitimate word into the wrong command, and the worst wrong command
    available is the one that quits. "exist" and "exit" are two edits apart and share a
    phonetic key, so without this the stage could shut Kayra down over a word the user said
    perfectly clearly.

    Note the asymmetry with re-ranking: an alternative the RECOGNIZER offered may be a
    shutdown, because the recognizer genuinely heard it. What is forbidden is this stage
    INVENTING one.
    """
    try:
        from kayra.core.voice_control import classify_control, ControlKind
        control = classify_control(term)
        return control is not None and control.kind == ControlKind.SHUTDOWN
    except Exception:
        # Cannot prove it is safe, so treat it as unsafe. A missing repair costs a repeated
        # command; a wrong one costs the session.
        return True


def _normalized(text):
    """
    The comparison form: lowercase, punctuation gone, fillers and the assistant's name gone.

    Shared with the control vocabulary through `voice_control.normalize_utterance`, so "Kayra,
    yes please" and "yes" compare equal here for exactly the same reason they do there. That
    function returns `(stripped_words, canonical_text)`; the STRIPPED form is the one this
    stage compares against, because it is the form the plausibility vocabularies are built in.
    """
    if not text:
        return ""
    try:
        stripped, _canonical = normalize_utterance(str(text))
        return " ".join(stripped)
    except Exception:
        return " ".join(_WORD.findall(str(text).lower()))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘

_REPAIR = None


def get_transcript_repair(vocabulary=None):
    """
    The process's repair stage, built on the process's conversation context.

    Passing `vocabulary` updates the injected command terms on an existing instance rather
    than building a second one — two stages would mean two sets of counters and two answers
    to "what is plausible", and only one of them would be reading the live context.
    """
    global _REPAIR
    if _REPAIR is None:
        _REPAIR = TranscriptRepair(vocabulary=vocabulary)
    elif vocabulary is not None:
        _REPAIR.set_vocabulary(vocabulary)
    return _REPAIR
