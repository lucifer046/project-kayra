# ┌────────────────────────────────────────────────────────────────────────┐
# │                               text.py                                  │
# │           Response Shaping & Speech-Safe Output Normalization          │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything that turns a model's raw output into something a person can hear.

`SentenceStreamer` decides WHEN a partial stream is speakable; `speech_safe_text` decides
WHAT the speaker should receive. They are the two halves of the same job and they belong
together: the console keeps the model's own formatting, and only the speech copy is
normalized.
"""

import datetime
import re as _re

def answer_modifier(answer: str) -> str:
    """Strips blank lines from a raw LLM response to maximize terminal/TTS density."""
    non_empty_lines = [line for line in answer.split("\n") if line.strip()]
    return "\n".join(non_empty_lines)


def real_time_info() -> str:
    """Compiles the current host date/time as a compact system-context string."""
    now = datetime.datetime.now()
    return (
        f"Current Time: {now.strftime('%I:%M %p')}\n"
        f"Day: {now.strftime('%A')}, Date: {now.strftime('%d %B %Y')}"
    )

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     STREAMING SENTENCE SEGMENTATION                    │
# └────────────────────────────────────────────────────────────────────────┘
# Shared by chatbot.py and real_time_search.py (per the "one implementation in
# utils.py" convention) so both conversational paths start speaking at the same
# point in the token stream instead of each carrying its own inline splitter.

class SentenceStreamer:
    """
    Turns an LLM token stream into speakable sentences as early as it safely can.

    The first utterance is what the user actually perceives as "response latency", so it
    is allowed to break at a clause boundary (comma / semicolon / dash) as soon as it is
    long enough to sound natural. This matters more than it looks: Kokoro on CPU
    synthesizes at roughly real time, so a 75-character opening sentence costs ~4s before
    the first sound, while a 30-character opening clause costs well under 1s and the rest
    of the answer synthesizes underneath it while it plays. Every later sentence waits for
    real terminal punctuation, which keeps prosody intact.

    Usage:
        streamer = SentenceStreamer(tts.speak)
        for token in stream: streamer.feed(token)
        streamer.flush()
    """

    TERMINATORS = (". ", "? ", "! ", ".\n", "?\n", "!\n", "\n")
    CLAUSE_BREAKS = (", ", "; ", ": ", " - ", " — ")

    # Never break the opening utterance before this many characters — below it the
    # fragment is too short to carry natural prosody.
    MIN_FIRST_CLAUSE_CHARS = 8

    def __init__(self, on_sentence, first_chunk_min_chars: int = 20, stop_check=None):
        """
        Args:
            on_sentence (callable): Invoked with each completed sentence.
            first_chunk_min_chars (int): Minimum length before the FIRST utterance is
                allowed to break at a clause boundary.
            stop_check (callable): Optional predicate; when it returns True the streamer
                stops emitting (the user interrupted).
        """
        self.on_sentence = on_sentence
        self.first_chunk_min_chars = first_chunk_min_chars
        self.stop_check = stop_check
        self.buffer = ""
        self.emitted = 0

    def _emit(self, sentence: str):
        if sentence.strip() and not self._stopped():
            self.on_sentence(sentence.strip())
            self.emitted += 1

    def _stopped(self) -> bool:
        return bool(self.stop_check and self.stop_check())

    def feed(self, token: str):
        """Adds a token to the buffer and emits any sentence that has become complete."""
        if self._stopped():
            return
        self.buffer += token

        while True:
            cut = self._find_cut()
            if cut is None:
                break
            self._emit(self.buffer[:cut])
            self.buffer = self.buffer[cut:].lstrip()

    def _find_cut(self):
        """Returns the index just past the earliest valid split point, or None."""
        best = None
        for term in self.TERMINATORS:
            idx = self.buffer.find(term)
            if idx != -1:
                end = idx + len(term)
                best = end if best is None else min(best, end)

        if best is not None:
            return best

        # Early-start allowance for the opening utterance only. The minimum offset keeps
        # us from emitting a one-word fragment ("Sure,") that sounds clipped.
        if self.emitted == 0 and len(self.buffer) >= self.first_chunk_min_chars:
            best_break = None
            for brk in self.CLAUSE_BREAKS:
                idx = self.buffer.find(brk, self.MIN_FIRST_CLAUSE_CHARS)
                if idx != -1:
                    end = idx + len(brk)
                    best_break = end if best_break is None else min(best_break, end)
            if best_break is not None:
                return best_break
        return None

    def flush(self):
        """Emits whatever is left in the buffer at the end of the stream."""
        if self.buffer.strip():
            self._emit(self.buffer)
        self.buffer = ""


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    SPEECH-SAFE OUTPUT NORMALIZATION                    │
# └────────────────────────────────────────────────────────────────────────┘
# The console shows the model's response as written; the TTS engine must receive a
# version that SOUNDS right. These are different artifacts and this is the only place
# that converts between them, so display output is never destroyed to suit the speaker.
#
# The rule is: remove what is purely visual, and SAY what is meaningful. The previous
# cleaner was `re.sub(r'[^\w\s\.,!\?\-\'"]', '', text)`, which deleted every symbol it
# did not recognise — so "50%" was spoken as "50", "$20" as "20" and "C++" as "C".
# Losing the unit is worse than mispronouncing it.


# Emoji / pictographs / dingbats / variation selectors. Spoken, these are either silence
# or a garbled word; either way they are decoration, not content.
_EMOJI_PATTERN = _re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F000-\U0001F0FF"  # tiles, cards
    "\U00002190-\U000021FF"  # arrows
    "\U00002B00-\U00002BFF"  # misc symbols & arrows
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002700-\U000027BF"
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=_re.UNICODE,
)

# Symbols worth pronouncing rather than deleting. Order matters: currency prefixes are
# handled before the generic sweep so "$20" becomes "20 dollars", not "20".
_SPOKEN_SYMBOLS = (
    (_re.compile(r"(?<=\d)\s*%"), " percent"),
    (_re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)"), r"\1 dollars"),
    (_re.compile("\u20b9" + r"\s*(\d[\d,]*(?:\.\d+)?)"), r"\1 rupees"),
    (_re.compile("\u20ac" + r"\s*(\d[\d,]*(?:\.\d+)?)"), r"\1 euros"),
    (_re.compile("\u00a3" + r"\s*(\d[\d,]*(?:\.\d+)?)"), r"\1 pounds"),
    (_re.compile(r"(?<=\d)\s*\u00b0\s*C\b"), " degrees Celsius"),
    (_re.compile(r"(?<=\d)\s*\u00b0\s*F\b"), " degrees Fahrenheit"),
    (_re.compile(r"(?<=\d)\s*\u00b0"), " degrees"),
    (_re.compile(r"&"), " and "),
    (_re.compile(r"(?<=\w)\s*=\s*(?=\w)"), " equals "),
    (_re.compile(r"(?<=\d)\s*\+\s*(?=\d)"), " plus "),
    (_re.compile(r"(?<=\w)/(?=\w)"), " slash "),
)

# Punctuation the TTS engine uses for prosody and that SentenceStreamer needs for
# segmentation. Everything else that is not a letter, digit, mark or space is dropped.
_KEEPABLE_PUNCTUATION = set(".,!?;:'\"()-–—…%$₹€£+/")

# Sentence terminators from other scripts, mapped to the ASCII equivalents Kokoro
# understands. Devanagari danda is the one that matters for the hi-IN input path.
_FOREIGN_TERMINATORS = {"।": ".", "॥": ".", "，": ",", "。": ".", "！": "!", "？": "?"}


# Typographic punctuation, mapped to the ASCII forms the category filter keeps. LLM output
# is full of these: without the mapping, U+2019 (a right single quotation mark, category Pf)
# is dropped as an unspeakable symbol and every contraction breaks apart — "Rust's" becomes
# "Rust s" and "can't" becomes "can t", which is audible and wrong.
_PUNCT_NORMALIZE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ", "\u200a": " ",
    "\u200b": "", "\ufeff": "",
})


def _strip_unspeakable(text: str) -> str:
    """
    Drops characters with no spoken form, using Unicode CATEGORY rather than a character
    allow-list.

    A regex allow-list such as `[^\w\s.,!?]` looks equivalent but silently destroys
    non-Latin text: Python's `\w` excludes combining marks (category Mn), so Devanagari
    vowel signs are stripped and "यह ठीक है" degrades to broken consonants. Keeping
    every letter, digit and mark avoids that entire class of bug.
    """
    import unicodedata

    out = []
    for ch in text:
        if ch in _FOREIGN_TERMINATORS:
            out.append(_FOREIGN_TERMINATORS[ch])
            continue
        if ch.isspace() or ch in _KEEPABLE_PUNCTUATION:
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category[0] in ("L", "N", "M"):
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out)


def speech_safe_text(text: str, keep_code: bool = False) -> str:
    """
    Converts an LLM response into something that sounds natural read aloud.

    Removes what exists only on screen (markdown syntax, emoji, table pipes, rules,
    bullet glyphs, bare URLs, citation brackets) and pronounces what carries meaning
    (percentages, currency, degrees, common operators).

    Args:
        text (str): The response as the model wrote it.
        keep_code (bool): Keep the contents of fenced code blocks (spoken as text). Use
            when the user explicitly asked to be read code; by default a fenced block is
            replaced by a short spoken placeholder, because reading punctuation-dense
            source aloud is unintelligible.

    Returns:
        str: Speech-ready text. Never returns None; returns "" for empty input.
    """
    if not text:
        return ""

    # Normalize typographic punctuation FIRST so later stages see plain ASCII quotes,
    # apostrophes and dashes.
    out = text.translate(_PUNCT_NORMALIZE)

    # ── Fenced code blocks ──
    if keep_code:
        out = _re.sub(r"```[a-zA-Z0-9_+-]*\n?", " ", out)
    else:
        out = _re.sub(r"```[a-zA-Z0-9_+-]*\n.*?```", " (code shown on screen) ", out, flags=_re.S)
        out = _re.sub(r"```[a-zA-Z0-9_+-]*", " ", out)

    # ── Links: keep the label, drop the target ──
    out = _re.sub(r"!?\[([^\]]*)\]\((?:[^)]*)\)", r"\1", out)          # [label](url)
    out = _re.sub(r"<(https?://[^>]+)>", " ", out)                      # <url>
    out = _re.sub(r"\bhttps?://\S+", " ", out)                          # bare url
    out = _re.sub(r"\bwww\.\S+", " ", out)

    # ── Markdown structure ──
    out = _re.sub(r"^\s{0,3}#{1,6}\s*", "", out, flags=_re.M)           # headers
    out = _re.sub(r"^\s*>\s?", "", out, flags=_re.M)                    # block quotes
    out = _re.sub(r"^\s*[-*+•]\s+", "", out, flags=_re.M)               # bullet markers
    out = _re.sub(r"^\s*\d+[.)]\s+", "", out, flags=_re.M)              # numbered list markers
    out = _re.sub(r"\|", " ", out)                                      # table pipes
    # Rules AFTER pipe removal, so a markdown table's "| --- | --- |" separator row is a
    # bare dash run by the time this sees it.
    out = _re.sub(r"^[\s\-:=_*]*[-:=_*][\s\-:=_*]*$", " ", out, flags=_re.M)
    out = _re.sub(r"(\*\*\*|\*\*|\*|___|__|_)(?=\S)(.+?)(?<=\S)\1", r"\2", out, flags=_re.S)
    out = _re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", out)                   # inline code
    out = _re.sub(r"~~(.+?)~~", r"\1", out, flags=_re.S)                # strikethrough

    # ── Citation / footnote brackets: "[1]", "[2, 3]" ──
    out = _re.sub(r"\[\s*\d+(?:\s*[,;]\s*\d+)*\s*\]", " ", out)

    # ── Emoji and leftover decoration ──
    out = _EMOJI_PATTERN.sub(" ", out)

    # ── Pronounce meaningful symbols ──
    for pattern, replacement in _SPOKEN_SYMBOLS:
        out = pattern.sub(replacement, out)

    # ── Drop remaining symbols that have no spoken form, keeping every letter/digit/mark
    #    and the sentence punctuation the TTS engine needs for prosody and segmentation.
    out = _strip_unspeakable(out)

    # ── Punctuation hygiene ──
    out = _re.sub(r"([!?.,;:])\1{1,}", r"\1", out)                      # "!!!" -> "!"
    out = _re.sub(r"\s+([.,!?;:])", r"\1", out)                         # " ." -> "."
    out = _re.sub(r"\(\s*\)", " ", out)                                 # empty parens
    out = _re.sub(r"[ \t]{2,}", " ", out)
    out = _re.sub(r"\n{2,}", "\n", out)
    out = _re.sub(r"[ \t]*\n[ \t]*", "\n", out)

    return out.strip()
