# ┌────────────────────────────────────────────────────────────────────────┐
# │                                utils.py                                │
# │                        Shared Helper Utilities                         │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements core, reusable utility tools shared across the KAYRA application.
It includes standardized logging setups, central project path resolution,
and premium terminal UI styling helpers utilizing the 'rich' library.
"""
import os
import sys
import json
import shutil
import logging
import datetime

# Reconfigure stdout/stderr to support UTF-8 characters on Windows legacy consoles
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text

# Premium, modern cyberpunk theme for the KAYRA terminal UI
kayra_theme = Theme({
    "info": "bold cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "critical": "bold red blink",
    "system": "bold magenta",
    "highlight": "bold violet",
    "text": "white",
    "dim": "dim",
})

console = Console(theme=kayra_theme)


def get_project_root():
    """
    Returns the absolute path to the project root directory.
    Guarantees stable resource lookup across different module subfolders.
    """
    # Moves up one level from 'modules/' directory to project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logger(name, log_filename="kayra.log", level=logging.INFO):
    """
    Configures and returns a robust logger instance writing structured logs to the 'logs/' folder.
    
    Parameters:
        name (str): Unique name of the module generating logs.
        log_filename (str): Target filename inside the 'logs/' directory.
        level (logging level): Minimum threshold level for logged events.
    """
    root = get_project_root()
    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    log_path = os.path.join(logs_dir, log_filename)
    
    # Structured format: Timestamp - Module - Level - Message
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handler registration on multiple setups
    if not logger.handlers:
        logger.addHandler(handler)
        
    return logger


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   PREMIUM CONSOLE INTERFACE HELPERS                    │
# └────────────────────────────────────────────────────────────────────────┘

from rich.rule import Rule

def print_banner(title: str, subtitle: str = None):
    """
    Renders an elegant, premium panel banner for application entrypoints.
    """
    from dotenv import dotenv_values
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    env_vars = dotenv_values(env_path) or {}
    
    assistant_name = env_vars.get("ASSISTANT_NAME", "").strip()
    if not assistant_name:
        assistant_name = "Kayra"
        
    title = title.replace("KAYRA", assistant_name.upper())

    banner_text = Text()
    banner_text.append(title.upper(), style="bold white")
    if subtitle:
        banner_text.append(f"\n{subtitle}", style="dim cyan")
    
    panel = Panel(
        banner_text,
        border_style="magenta",
        expand=False,
        padding=(1, 4),
        subtitle=f"[dim]{assistant_name.upper()}[/dim]",
        subtitle_align="right"
    )
    console.print()
    console.print(panel)
    console.print()



def print_section(title: str):
    """
    Renders a section separator with a neat horizontal layout.
    """
    console.print()
    console.print(Rule(f"[bold white]{title.upper()}[/bold white]", style="dim magenta", align="left"))


import os

def safe_print(msg_format: str):
    try:
        console.print(msg_format)
    except ValueError as e:
        if "closed file" in str(e):
            # Terminal was abruptly closed (e.g., via Ctrl+W shortcut hitting the terminal)
            os._exit(1)
        raise

def print_info(msg: str):
    safe_print(f"[info][INFO][/info] [text]{msg}[/text]")

def print_success(msg: str):
    safe_print(f"[success][SUCCESS][/success] [text]{msg}[/text]")

def print_warning(msg: str):
    safe_print(f"[warning][WARNING][/warning] [text]{msg}[/text]")

def print_error(msg: str):
    safe_print(f"[error][ERROR][/error] [text]{msg}[/text]")

def print_critical(msg: str):
    safe_print(f"[critical][CRITICAL][/critical] [text]{msg}[/text]")

def print_system(msg: str):
    safe_print(f"[system][SYSTEM][/system] [text]{msg}[/text]")


# ┌────────────────────────────────────────────────────────────────────────┐
# │              SHARED CONVERSATIONAL MEMORY & CONTEXT HELPERS            │
# └────────────────────────────────────────────────────────────────────────┘
# Centralized here because chatbot.py, real_time_search.py (and previously
# deep_research.py) each carried near-identical copies of this logic. Keeping
# a single implementation also fixes a latent bug in the old per-module
# copies: they read/wrote "data\\conversation.json" as a path relative to the
# CURRENT WORKING DIRECTORY rather than the project root, so the assistant's
# long-term memory would silently split across multiple files (or fail to
# load at all) if launched from anywhere other than the project folder.

def get_data_paths():
    """
    Resolves absolute pathways to the persistent conversation database and its
    rolling backup, independent of the process's current working directory.

    Returns:
        tuple[str, str]: (primary_db_path, backup_db_path)
    """
    data_dir = os.path.join(get_project_root(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "conversation.json"), os.path.join(data_dir, "conversation_backup.json")


def load_conversation_memory():
    """
    Loads long-term conversation history from the persistent JSON database.

    Fault-Tolerance:
        If the primary file is missing or corrupted (e.g. due to a sudden
        process halt mid-write), transparently falls back to the rolling
        secondary backup copy.

    Returns:
        list: Long-term conversational history, or an empty list if neither
              the primary database nor its backup can be read.
    """
    db_file, backup_file = get_data_paths()
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            print_warning("Primary index compromised. Restored data from rolling backup.")
            return data
        except Exception:
            return []


def save_conversation_memory(memory_list):
    """
    Persists the long-term conversation history using an atomic write pattern.

    Corruption-Prevention Strategy:
        1. Write to the backup file first.
        2. Only once that succeeds, copy the backup over the primary file.
        This guarantees the primary database is never left half-written if the
        process is interrupted mid-save.

    Args:
        memory_list (list): The full conversation history to persist.

    Returns:
        bool: True if the write succeeded, False otherwise.
    """
    db_file, backup_file = get_data_paths()
    try:
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(memory_list, f, indent=4, ensure_ascii=False)
        shutil.copy(backup_file, db_file)
        return True
    except Exception as e:
        print_error(f"Persistent storage transaction failed: {e}")
        return False


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
# │                    STARTUP / LATENCY INSTRUMENTATION                   │
# └────────────────────────────────────────────────────────────────────────┘
# Small, always-on stopwatch used to answer "where did the cold start go?" and
# "how long until the first audible word?" without bolting a profiler onto the
# app. Kept here (rather than in main.py) so text_to_speech.py / speech_to_text.py
# can record their own stage marks through the same clock.

import time as _time
import threading as _threading


class StageTimer:
    """
    Monotonic stage stopwatch. `mark()` records the elapsed time since construction
    for a named milestone; `report()` renders every mark in order.

    Thread-safe: boot stages are marked from several worker threads in parallel.
    """

    def __init__(self, label: str = "startup"):
        self.label = label
        self.t0 = _time.perf_counter()
        self._marks = []
        self._lock = _threading.Lock()

    def mark(self, name: str, quiet: bool = False) -> float:
        """Records a milestone and returns seconds elapsed since the timer started."""
        elapsed = _time.perf_counter() - self.t0
        with self._lock:
            self._marks.append((name, elapsed))
        if not quiet:
            safe_print(f"[dim]\[TIMING][/dim] [text]{name}[/text] [dim]+{elapsed:.2f}s[/dim]")
        return elapsed

    def elapsed(self) -> float:
        """Seconds since the timer was created."""
        return _time.perf_counter() - self.t0

    def report(self, title: str = None):
        """Prints every recorded mark, in the order it was recorded."""
        with self._lock:
            marks = list(self._marks)
        if not marks:
            return
        console.print()
        console.print(Rule(f"[bold white]{(title or self.label).upper()} TIMINGS[/bold white]",
                           style="dim magenta", align="left"))
        for name, elapsed in marks:
            console.print(f"  [dim]+{elapsed:6.2f}s[/dim]  [text]{name}[/text]")
        console.print()


def now_ms() -> float:
    """
    Wall-clock milliseconds since the epoch.

    Deliberately wall-clock (not monotonic) because these timestamps are compared
    against JavaScript `Date.now()` values produced inside the headless-Chrome STT
    page — both clocks are the same host clock, so the two are directly comparable.
    """
    return _time.time() * 1000.0


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

import re as _re

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
