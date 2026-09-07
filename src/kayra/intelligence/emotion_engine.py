# ┌────────────────────────────────────────────────────────────────────────┐
# │                          emotion_engine.py                             │
# │        Multi-Signal Lexical / Structural / Contextual Mood Engine      │
# └────────────────────────────────────────────────────────────────────────┘
"""
Estimates how the user sounds, so the assistant can adjust TONE — never intent.

WHY THERE IS NO ACOUSTIC ANALYSIS HERE
--------------------------------------
The obvious upgrade is prosody: pitch, energy, speaking rate. It is not implemented, and the
reason is architectural rather than a matter of effort.

Kayra's STT is the Web Speech API running inside headless Chrome. Chrome owns the microphone
and hands Python a *transcript* over the WebDriver wire — **the raw audio never enters this
process at all.** Adding acoustic emotion would therefore require one of:

  1. Opening a SECOND microphone stream in Python. This is exactly what the architecture
     forbids: two capture paths on one device, contending with Chrome for the mic, and a
     second copy of every buffer.
  2. Recording inside the page (MediaRecorder), base64-encoding it and pulling it back through
     Selenium. That is a multi-hundred-kilobyte JSON round-trip per utterance on the same
     WebDriver connection the barge-in watcher polls every 60ms — the one resource in the
     system that must stay responsive.
  3. Adding librosa/numpy/scipy for feature extraction: tens of megabytes of RSS and hundreds
     of milliseconds per utterance, on the hot path of every single turn.

All three cost more than the signal is worth for what the mood is actually used for: one
sentence of tone guidance appended to the system prompt. So this engine uses the signals that
are genuinely free, and says so honestly rather than shipping a placeholder that pretends to
hear tone of voice.

If the STT layer is ever replaced by an in-process recognizer that already holds PCM (local
Whisper, Vosk), the audio becomes free and `analyze()` takes an optional `audio=` signal — the
fusion step below is written so a fourth signal drops in without touching the others.

THE THREE SIGNALS THAT ARE FREE
-------------------------------
  * LEXICAL    — a weighted lexicon over the transcript, with negation, intensifiers, and
                 false-positive control for people *discussing* an emotion.
  * STRUCTURAL — how it was written/said: capitalisation, exclamation, elongation, length.
  * CONTEXTUAL — time of day, and whether the user is repeating themselves or interrupting.

Fusion is confidence-weighted and deterministic. No model, no training, no weights file.

COST
----
Pure string work over one utterance. Measured at tens of microseconds and ~0 MB RSS; see
`tests/test_emotion_engine.py`, which asserts the latency budget.
"""

import re
import time
import threading
import datetime
from collections import deque

from kayra.utils import print_info


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            EMOTION VOCABULARY                          │
# └────────────────────────────────────────────────────────────────────────┘
# Eight states, chosen because each one implies a DIFFERENT response style. A vocabulary of
# thirty emotions would be finer-grained and completely useless: the consumer is one sentence
# of tone guidance in the system prompt, and it cannot act on distinctions this engine has no
# evidence to make.

NEUTRAL = "neutral"
HAPPY = "happy"
EXCITED = "excited"
STRESSED = "stressed"
SAD = "sad"
TIRED = "tired"
FRUSTRATED = "frustrated"
CALM = "calm"

EMOTIONS = (NEUTRAL, HAPPY, EXCITED, STRESSED, SAD, TIRED, FRUSTRATED, CALM)

# How each state should change the assistant's delivery. Consumed by the identity prompt.
TONE_GUIDANCE = {
    HAPPY:      "match their warmth; keep it light",
    EXCITED:    "match their energy; be brisk and enthusiastic",
    STRESSED:   "be calm, concrete and brief; lead with the answer",
    SAD:        "be gentle and unhurried; do not be relentlessly upbeat",
    TIRED:      "be concise; skip the preamble",
    FRUSTRATED: "be direct and practical; no cheerfulness, no apologising at length",
    CALM:       "relaxed and conversational is fine",
}

# The old engine emitted these. Anything that stored a previous label keeps working.
LEGACY_ALIASES = {
    "angry": FRUSTRATED,
    "fear": STRESSED,
    "surprise": EXCITED,
}


class EmotionReading(str):
    """
    The result of one analysis.

    It SUBCLASSES `str` deliberately. `llm_engine.get_identity_prompt(mood=...)`,
    `chatbot.Chatbot(query, tts, mood)` and `real_time_search.RealTimeSearchEngine(query, mood,
    tts)` were all written against a plain capitalised string, and they still receive exactly
    that — `str(reading)` is "Frustrated". New code reads `.emotion`, `.confidence` and
    `.signals` off the same object. No caller had to change, and none of them had to learn a
    new type.
    """

    __slots__ = ("emotion", "confidence", "signals", "scores", "at")

    def __new__(cls, emotion=NEUTRAL, confidence=0.0, signals=(), scores=None):
        label = "Neutral" if emotion == NEUTRAL else emotion.capitalize()
        reading = super().__new__(cls, label)
        reading.emotion = emotion
        reading.confidence = round(float(confidence), 3)
        reading.signals = tuple(signals)
        reading.scores = dict(scores or {})
        reading.at = time.time()
        return reading

    @property
    def is_neutral(self):
        return self.emotion == NEUTRAL

    @property
    def tone(self):
        """One clause of delivery guidance, or "" when there is nothing to say."""
        return TONE_GUIDANCE.get(self.emotion, "")

    def to_dict(self):
        return {"emotion": self.emotion, "confidence": self.confidence,
                "signals": list(self.signals), "scores": self.scores}

    def __repr__(self):
        return (f"<EmotionReading {self.emotion} conf={self.confidence:.2f} "
                f"signals={list(self.signals)}>")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SIGNAL A — LEXICON                            │
# └────────────────────────────────────────────────────────────────────────┘
# Weighted, not binary. "furious" is not the same evidence as "bad", and the old engine scored
# them identically because every hit was worth exactly 1.

_LEXICON = {
    FRUSTRATED: {
        "angry": 1.0, "furious": 1.0, "pissed": 1.0, "rage": 1.0, "livid": 1.0,
        "frustrated": 1.0, "frustrating": 0.9, "infuriating": 1.0, "fed up": 1.0,
        "annoyed": 0.8, "annoying": 0.7, "irritated": 0.8, "irritating": 0.7,
        "hate": 0.8, "sucks": 0.7, "worst": 0.7, "ridiculous": 0.7, "useless": 0.7,
        "garbage": 0.7, "rubbish": 0.6, "stupid": 0.6, "broken": 0.4, "again": 0.2,
        "still not": 0.6, "doesn't work": 0.7, "not working": 0.7, "won't work": 0.7,
        "come on": 0.5, "seriously": 0.4, "why is this": 0.4,
    },
    STRESSED: {
        "stressed": 1.0, "stressful": 0.8, "stress": 0.7, "anxious": 1.0, "anxiety": 0.9,
        "panic": 1.0, "panicking": 1.0, "overwhelmed": 1.0, "swamped": 0.8,
        "worried": 0.9, "worry": 0.7, "nervous": 0.9, "scared": 0.9, "afraid": 0.8,
        "terrified": 1.0, "freaking out": 1.0, "deadline": 0.6, "urgent": 0.6,
        "no time": 0.7, "running out of time": 0.9, "too much": 0.5, "pressure": 0.5,
    },
    SAD: {
        "sad": 1.0, "depressed": 1.0, "depressing": 0.8, "miserable": 1.0, "lonely": 0.9,
        "heartbroken": 1.0, "grief": 1.0, "crying": 0.8, "cried": 0.8, "upset": 0.7,
        "disappointed": 0.8, "disappointing": 0.6, "hurt": 0.5, "awful": 0.6,
        "terrible": 0.5, "gutted": 0.9, "down": 0.3, "lost": 0.3,
    },
    TIRED: {
        "tired": 1.0, "exhausted": 1.0, "exhausting": 0.8, "sleepy": 1.0, "drained": 0.9,
        "burnt out": 1.0, "burned out": 1.0, "burnout": 1.0, "knackered": 1.0,
        "fatigued": 1.0, "worn out": 0.9, "no energy": 0.9, "can't focus": 0.7,
        "long day": 0.8, "up all night": 0.8, "need sleep": 0.9, "half asleep": 0.9,
    },
    HAPPY: {
        "happy": 1.0, "delighted": 0.9, "glad": 0.8, "pleased": 0.8, "grateful": 0.8,
        "wonderful": 0.8, "lovely": 0.7, "love": 0.6, "perfect": 0.7, "excellent": 0.7,
        "fantastic": 0.8, "brilliant": 0.7, "great": 0.5, "nice": 0.4, "good": 0.3,
        "thank you": 0.3, "thanks": 0.25, "worked": 0.3, "it works": 0.6,
    },
    EXCITED: {
        "excited": 1.0, "exciting": 0.8, "thrilled": 1.0, "stoked": 1.0, "pumped": 1.0,
        "amazing": 0.8, "awesome": 0.8, "incredible": 0.8, "wow": 0.7, "whoa": 0.6,
        "can't wait": 1.0, "cant wait": 1.0, "let's go": 0.8, "lets go": 0.8,
        "finally": 0.5, "yes": 0.2, "unbelievable": 0.5,
    },
    CALM: {
        "calm": 1.0, "relaxed": 1.0, "peaceful": 0.9, "chill": 0.7, "no rush": 0.8,
        "take your time": 0.8, "whenever": 0.4, "no worries": 0.6, "all good": 0.5,
        "fine": 0.2, "okay": 0.15,
    },
}

# Longest phrase in the lexicon, so the matcher knows how wide an n-gram window it needs.
_MAX_NGRAM = max(len(phrase.split()) for table in _LEXICON.values() for phrase in table)

# phrase -> (emotion, weight). One flat dict = one O(1) lookup per n-gram.
_PHRASE_INDEX = {}
for _emotion, _table in _LEXICON.items():
    for _phrase, _weight in _table.items():
        _PHRASE_INDEX[_phrase] = (_emotion, _weight)

# Words that flip the meaning of an emotion term within a short window before it.
_NEGATORS = frozenset({"not", "no", "never", "isn't", "isnt", "aren't", "arent", "wasn't",
                       "wasnt", "don't", "dont", "doesn't", "doesnt", "didn't", "didnt",
                       "won't", "wont", "can't", "cant", "nothing", "hardly", "barely"})

_AMPLIFIERS = {"really": 1.4, "very": 1.4, "so": 1.3, "extremely": 1.6, "incredibly": 1.5,
               "super": 1.4, "totally": 1.3, "absolutely": 1.4, "completely": 1.3,
               "insanely": 1.5, "damn": 1.3, "bloody": 1.3}

_DAMPENERS = {"slightly": 0.6, "kind": 0.7, "kinda": 0.7, "sort": 0.7, "somewhat": 0.6,
              "little": 0.7, "bit": 0.7, "mildly": 0.6, "maybe": 0.7, "probably": 0.8}

# ── False-positive control (requirement 8) ────────────────────────────────
# "Why do people get stressed before exams?" contains a strong stress term and says nothing
# whatsoever about how the USER feels. Three cheap tests catch almost all of this class.

_QUESTION_OPENERS = frozenset({"why", "what", "how", "when", "who", "where", "which",
                               "does", "do", "did", "is", "are", "was", "were", "can",
                               "could", "should", "would", "define", "explain", "tell"})

# First-person markers. Their ABSENCE from an emotional sentence is the strongest single hint
# that the sentence is about someone else.
_FIRST_PERSON = frozenset({"i", "i'm", "im", "i've", "ive", "i'd", "id", "i'll", "ill",
                           "me", "my", "mine", "myself", "we", "we're", "were", "our", "us"})

# Third-person subjects that commonly precede an emotion word in a general statement.
_THIRD_PERSON = frozenset({"people", "everyone", "someone", "somebody", "they", "them",
                           "he", "she", "users", "students", "kids", "one", "you"})

# Phrases that mark the sentence as definitional or hypothetical rather than personal.
_DEFINITIONAL = ("what does", "what is", "what's the", "how do you", "how does",
                 "tell me about", "explain", "definition of", "meaning of", "why do",
                 "why does", "why are", "difference between", "example of")

_WORD_RE = re.compile(r"[a-z']+")


def _tokenize(text):
    return _WORD_RE.findall(text.lower())


def _lexical_signal(text, tokens):
    """
    Scores the transcript against the weighted lexicon.

    Returns (scores, evidence, damping) where `damping` in [0,1] records how much the
    false-positive control reduced the result — useful for explaining a decision.
    """
    scores = {emotion: 0.0 for emotion in EMOTIONS}
    if not tokens:
        return scores, 0.0, 1.0

    lowered = text.lower()

    # ── damping: is this sentence even ABOUT the speaker? ──
    damping = 1.0
    has_first_person = any(token in _FIRST_PERSON for token in tokens)
    is_question = lowered.rstrip().endswith("?") or (tokens[0] in _QUESTION_OPENERS)
    is_definitional = any(marker in lowered for marker in _DEFINITIONAL)
    has_third_person = any(token in _THIRD_PERSON for token in tokens)

    if is_definitional and not has_first_person:
        # "Why do people get stressed before exams?" — a request for information.
        damping = 0.1
    elif is_question and not has_first_person:
        damping = 0.2
    elif has_third_person and not has_first_person:
        # "He was furious about it." — someone else's emotion.
        damping = 0.3
    elif is_question and has_first_person:
        # "Why am I so tired today?" — still genuinely about the user, but a question about
        # the feeling is weaker evidence than a statement of it.
        damping = 0.75

    # ── n-gram scan, longest phrase first so "not working" beats "working" ──
    evidence = 0.0
    index = 0
    count = len(tokens)
    while index < count:
        matched = False
        for width in range(min(_MAX_NGRAM, count - index), 0, -1):
            phrase = " ".join(tokens[index:index + width])
            hit = _PHRASE_INDEX.get(phrase)
            if hit is None:
                continue
            emotion, weight = hit

            # Negation inside the three tokens before the match cancels it.
            window = tokens[max(0, index - 3):index]
            if any(token in _NEGATORS for token in window):
                index += width
                matched = True
                break

            # Intensity modifiers in the two tokens before.
            modifier = 1.0
            for token in tokens[max(0, index - 2):index]:
                modifier *= _AMPLIFIERS.get(token, 1.0)
                modifier *= _DAMPENERS.get(token, 1.0)

            contribution = weight * modifier * damping
            scores[emotion] += contribution
            evidence += contribution
            index += width
            matched = True
            break
        if not matched:
            index += 1

    return scores, evidence, damping


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SIGNAL B — STRUCTURE                            │
# └────────────────────────────────────────────────────────────────────────┘
# How something was said, independent of the words. Weak on its own — which is exactly why it
# is weighted low and can only ever tip a near-tie.

_ELONGATION = re.compile(r"([a-z])\1{2,}")


def _structural_signal(text, tokens):
    """Punctuation, capitalisation and length. Returns (scores, evidence)."""
    scores = {emotion: 0.0 for emotion in EMOTIONS}
    if not text.strip():
        return scores, 0.0

    evidence = 0.0
    stripped = text.strip()

    letters = [ch for ch in stripped if ch.isalpha()]
    if len(letters) >= 4:
        caps_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if caps_ratio > 0.6:
            # SHOUTING. Which direction it leans is decided by the lexical signal; this only
            # supplies intensity, split between the two high-arousal states.
            scores[FRUSTRATED] += 0.5
            scores[EXCITED] += 0.5
            evidence += 0.5

    exclamations = stripped.count("!")
    if exclamations:
        boost = min(0.6, 0.25 * exclamations)
        scores[EXCITED] += boost
        scores[FRUSTRATED] += boost * 0.5
        evidence += boost

    if _ELONGATION.search(stripped.lower()):
        # "sooo", "yesss", "pleeease" — emphasis, almost always positive arousal.
        scores[EXCITED] += 0.4
        evidence += 0.4

    if stripped.count("?") >= 2 or "??" in stripped:
        scores[FRUSTRATED] += 0.3
        evidence += 0.3

    # A very long utterance suggests venting or detailed explanation; a very short one after
    # a question suggests terseness. Both are weak, so both are worth little.
    if len(tokens) >= 45:
        scores[STRESSED] += 0.25
        evidence += 0.25
    elif len(tokens) <= 2 and stripped.endswith("."):
        scores[TIRED] += 0.15
        scores[FRUSTRATED] += 0.15
        evidence += 0.15

    return scores, evidence


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SIGNAL C — CONTEXT                              │
# └────────────────────────────────────────────────────────────────────────┘
# Behaviour, not content. Everything here is already in memory: the clock, the last few
# utterances, and whatever the runtime knows about recent interruptions. No new capture, no
# new thread, no new persistence.

_LATE_NIGHT_HOURS = frozenset({0, 1, 2, 3, 4})


def _context_signal(tokens, history, hour, seconds_since_interrupt):
    """Time of day, self-repetition and recent barge-ins. Returns (scores, evidence)."""
    scores = {emotion: 0.0 for emotion in EMOTIONS}
    evidence = 0.0

    if hour in _LATE_NIGHT_HOURS:
        scores[TIRED] += 0.4
        evidence += 0.4

    # Repeating yourself is the clearest behavioural sign of frustration there is, and it costs
    # a set intersection over the last few utterances to detect.
    if tokens and history:
        current = set(tokens)
        if len(current) >= 3:
            for previous in history:
                if not previous:
                    continue
                overlap = len(current & previous) / max(len(current), len(previous))
                if overlap >= 0.7:
                    scores[FRUSTRATED] += 0.6
                    evidence += 0.6
                    break

    # A barge-in moments ago means the user cut the assistant off. Mild impatience signal —
    # deliberately small, because cutting off a long answer is also just efficient.
    if seconds_since_interrupt is not None and seconds_since_interrupt < 30:
        scores[FRUSTRATED] += 0.25
        evidence += 0.25

    return scores, evidence


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE ENGINE                                │
# └────────────────────────────────────────────────────────────────────────┘

# Signal weights. Lexical dominates by design: it is the only signal that carries actual
# semantic content, and requirement 4 is precisely that a weak signal must not override a
# strong one. Structure and context together cannot outvote a clear lexical reading; they can
# only break a tie or add confidence to one.
W_LEXICAL = 0.62
W_STRUCTURAL = 0.20
W_CONTEXT = 0.18

# Below this, the reading is reported as neutral. A guess about someone's emotional state that
# then steers the assistant's tone is worse than no guess at all.
DEFAULT_THRESHOLD = 0.35


class SemanticEmotionEngine:
    """
    Deterministic multi-signal mood estimator.

    Stateless with respect to the caller: `analyze()` is a pure function of its inputs plus a
    small bounded history that only ever adds evidence. Thread-safe — the history is the only
    mutable state and it is lock-guarded.
    """

    HISTORY_SIZE = 8          # readings kept for smoothing
    UTTERANCE_MEMORY = 5      # recent token sets, for self-repetition detection

    def __init__(self, threshold=DEFAULT_THRESHOLD, verbose=False, clock=None):
        self.threshold = threshold
        self.verbose = verbose
        self._clock = clock or time.time
        self._lock = threading.RLock()

        # BOUNDED. This is the entire persistent state of the emotion subsystem: two small
        # deques in RAM, nothing written to disk. The old engine kept none, and an unbounded
        # mood log would have been a slow leak in a process designed to run all day.
        self._history = deque(maxlen=self.HISTORY_SIZE)
        self._recent_utterances = deque(maxlen=self.UTTERANCE_MEMORY)
        self._current = EmotionReading(NEUTRAL, 0.0)

    # ──────────────────────────────────────────────────────────────────────
    #                              ANALYSIS
    # ──────────────────────────────────────────────────────────────────────

    def analyze(self, text, seconds_since_interrupt=None, hour=None):
        """
        Returns an `EmotionReading` for one utterance.

        Args:
            text: the transcript. Anything falsy or non-string yields a neutral reading
                  rather than raising — this runs on every turn and must never be able to
                  break the main loop.
            seconds_since_interrupt: optional, from `RuntimeState`. Only ever adds a small
                  frustration prior.
            hour: optional local hour, injectable so tests are not time-of-day dependent.
        """
        if not isinstance(text, str) or not text.strip():
            return self._finalize(EmotionReading(NEUTRAL, 0.0, ("none",)), record=False)

        tokens = _tokenize(text)
        if not tokens:
            # Punctuation, emoji or symbols with no words at all. The structural signal would
            # happily read "!!!???" as excitement or frustration, but there is nothing being
            # said — intensity without content is not evidence of anything.
            return self._finalize(EmotionReading(NEUTRAL, 0.0, ("none",)), record=False)
        if hour is None:
            hour = datetime.datetime.fromtimestamp(self._clock()).hour

        with self._lock:
            history_snapshot = list(self._recent_utterances)
            previous = list(self._history)

        lexical, lex_evidence, damping = _lexical_signal(text, tokens)
        structural, str_evidence = _structural_signal(text, tokens)
        contextual, ctx_evidence = _context_signal(
            tokens, history_snapshot, hour, seconds_since_interrupt)

        # ── Fusion ──
        # Each signal is normalised by its own evidence mass first, so a signal that fired
        # weakly cannot contribute a large raw number just because its scale differs.
        fused = {emotion: 0.0 for emotion in EMOTIONS}
        signals = []
        for scores, evidence, weight, name in (
                (lexical, lex_evidence, W_LEXICAL, "text"),
                (structural, str_evidence, W_STRUCTURAL, "structure"),
                (contextual, ctx_evidence, W_CONTEXT, "context")):
            if evidence <= 0:
                continue
            signals.append(name)
            scale = weight / evidence
            for emotion, value in scores.items():
                if value:
                    fused[emotion] += value * scale
        # Confidence has to reflect how much evidence there was, not just its shape. A single
        # 0.3-weight token normalised to 1.0 would otherwise look as certain as a paragraph.
        total_evidence = lex_evidence + str_evidence + ctx_evidence

        fused[NEUTRAL] = 0.0
        ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        top_emotion, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        if top_score <= 0:
            return self._finalize(
                EmotionReading(NEUTRAL, 0.0, tuple(signals) or ("none",), fused), tokens=tokens)

        # margin: how clearly the winner won. mass: how much evidence existed at all.
        margin = (top_score - runner_up) / top_score if top_score else 0.0
        mass = min(1.0, total_evidence / 1.5)
        confidence = round(min(1.0, (0.45 + 0.55 * margin) * mass), 3)

        # ── Smoothing ──
        # A borderline reading that agrees with the recent trend is more believable than one
        # that contradicts it. This only ever ADDS confidence to agreement; it never invents
        # an emotion the current utterance had no evidence for.
        agreeing = sum(1 for reading in previous[-3:]
                       if reading.emotion == top_emotion and reading.confidence >= 0.4)
        if agreeing >= 2:
            confidence = round(min(1.0, confidence + 0.12), 3)

        if confidence < self.threshold:
            reading = EmotionReading(NEUTRAL, confidence, tuple(signals), fused)
        else:
            reading = EmotionReading(top_emotion, confidence, tuple(signals), fused)

        if self.verbose:
            print_info(f"[EMOTION] {reading.emotion} conf={reading.confidence:.2f} "
                       f"signals={list(reading.signals)} damping={damping:.2f}")
        return self._finalize(reading, tokens=tokens)

    def analyze_text(self, text):
        """
        Backward-compatible entry point.

        `main.py` has always called `emotion_engine.analyze_text(user_input)` and used the
        result as a string. It still can: `EmotionReading` IS a str. Nothing at the call site
        needed to change.
        """
        return self.analyze(text)

    # ──────────────────────────────────────────────────────────────────────
    #                          BOUNDED STATE
    # ──────────────────────────────────────────────────────────────────────

    def _finalize(self, reading, tokens=None, record=True):
        with self._lock:
            if record:
                self._history.append(reading)
                if tokens:
                    self._recent_utterances.append(set(tokens))
            self._current = reading
        return reading

    @property
    def current(self):
        """The most recent reading. Never None."""
        with self._lock:
            return self._current

    def recent(self, limit=None):
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit else items

    def trend(self, window=3):
        """
        The emotion the last `window` confident readings agree on, or neutral.

        Used where a single turn is too noisy to act on. Cheap: at most eight comparisons.
        """
        with self._lock:
            recent = [r for r in list(self._history)[-window:] if r.confidence >= 0.4]
        if len(recent) < 2:
            return NEUTRAL
        labels = [r.emotion for r in recent]
        top = max(set(labels), key=labels.count)
        return top if labels.count(top) >= 2 else NEUTRAL

    def reset(self):
        """Clears the bounded history. Used between test cases and on session boundaries."""
        with self._lock:
            self._history.clear()
            self._recent_utterances.clear()
            self._current = EmotionReading(NEUTRAL, 0.0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        COMPATIBILITY ALIASES                           │
# └────────────────────────────────────────────────────────────────────────┘
# `main.py` imports `EmotionEngine`; older code and tests may use either name.
EmotionEngine = SemanticEmotionEngine


def normalize_label(label):
    """Maps any historical label ('Angry', 'fear', 'Surprise') onto the current vocabulary."""
    if not label:
        return NEUTRAL
    lowered = str(label).strip().lower()
    lowered = LEGACY_ALIASES.get(lowered, lowered)
    return lowered if lowered in EMOTIONS else NEUTRAL


if __name__ == "__main__":
    engine = SemanticEmotionEngine(verbose=True)
    print("Multi-signal emotion engine. Blank line or 'exit' to quit.\n")
    while True:
        try:
            sample = input("text > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not sample or sample.lower() in ("exit", "quit"):
            break
        result = engine.analyze(sample)
        print(f"  -> {result!r}   (as a string: {str(result)!r})")
