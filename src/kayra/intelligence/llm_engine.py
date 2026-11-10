# ┌────────────────────────────────────────────────────────────────────────┐
# │                             llm_engine.py                              │
# │               Centralized LLM Engine & Intent Classifier               │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements the primary intelligence and intent routing orchestration for the KAYRA project.
It automatically handles local vs. cloud model selection, intent classification (DMM),
and real-time token streaming for low-latency dialogue generation.

Cloud routing (both the DMM and chat): Claude Haiku -> Groq -> Gemini, each only when its key
is set. A local model server, when running, takes everything unless CLOUD_FIRST is on.
"""

import re
import time
import socket
import requests
from urllib.parse import urlparse
from openai import OpenAI

# Robust relative path imports across standalone and package execution
from kayra.core.config import env_values
from kayra.core import logbus
from kayra.core.logbus import Subsystem, info, warning, error, debug, field
from kayra.intelligence.provider_router import (
    ROUTE_CHAT, ROUTE_DECISION, CLAUDE, GROQ, GEMINI, LOCAL,
    AllProvidersFailed, FailureKind, get_provider_router, is_no_credit,
    # The router's own failure taxonomy, reused rather than re-invented: whether a failure is
    # worth retrying is answered from ONE vocabulary, and the local path gets the same reading
    # of an exception that the cloud path does.
    classify_failure,
)
from kayra.utils import print_system


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        THE DMM RETRY BOUND                             │
# └────────────────────────────────────────────────────────────────────────┘
# How many times an EMPTY token response is retried before the request is treated as
# conversation. Five.
#
# This governs ONE failure class — the provider answered and the answer contained no usable
# token — and nothing else. Transport failures, rate limits, timeouts and auth errors are the
# provider router's business and never reach it; a rate-limited key therefore still costs
# exactly one call per provider per request, which is the property the router exists to
# guarantee and which five retries here must not be allowed to undo.
#
# Five rather than three because the local-model path is where empty completions actually
# happen: a small local model that samples only stop-tokens, or one still warming its KV
# cache, produces them regularly, and each attempt is an in-process round-trip on the user's
# own machine rather than a metered call. Three attempts was degrading classifiable requests
# to conversation often enough for the user to notice.
#
# There is no backoff and there must not be one. The retry is not waiting for anything to
# recover — the model is up, it just produced nothing — so a sleep would add latency to a
# turn the user is waiting on and buy nothing.
MAX_DMM_EMPTY_RETRIES = 5

# ── WHAT IS WORTH RETRYING ───────────────────────────────────────────────
# An empty completion is a transient sampling outcome and is worth asking again. A malformed
# request, a bad credential or a model that is not there are not: the next four attempts will
# fail identically, and the only thing five of them buys is fifty seconds of a user waiting
# for the same answer.
#
# These names are the provider router's (`FailureKind`), reused rather than re-invented, so
# "is this worth retrying?" is answered from one vocabulary. The router owns TRANSPORT
# retries; this owns retries over the model's OUTPUT, and the two must not become two
# authorities for the same thing.
RETRYABLE_DMM_FAILURES = frozenset({"EMPTY_RESPONSE", "TRANSIENT", "SERVER_ERROR", "UNKNOWN"})
TERMINAL_DMM_FAILURES = frozenset({"INVALID_REQUEST", "AUTH_FAILURE", "MODEL_UNAVAILABLE"})

# ── BACKOFF ──────────────────────────────────────────────────────────────
# The first retry is IMMEDIATE. The model is up and merely produced nothing, so there is
# nothing to wait for on the first re-ask, and a delay there is pure added latency on the
# common case — which is a retry that succeeds.
#
# Later attempts get a small, bounded, LINEAR pause. Not exponential: this is not congestion
# control and the local server is not overloaded; it is a token sampler that produced a stop
# token, and a growing wait would only make the worst case worse. Measured against LM Studio
# here an empty completion already costs ~2.0s of inference, so the pause is a rounding error
# next to the attempt itself and exists only to give a model that is still warming a moment.
DMM_RETRY_DELAY_MS = 120
DMM_MAX_RETRY_DELAY_MS = 600

# ── HEALTH COOLDOWN ──────────────────────────────────────────────────────
# A model that answered nothing for three consecutive REQUESTS is not having a bad sample, it
# is not working. Running five retries per utterance against it costs the user ten seconds a
# turn and cannot succeed. After the third, it is stood down briefly and every request goes
# straight to the fallback the architecture already has.
#
# This is a per-model latch, not a circuit breaker with half-open probing: the next request
# after the cooldown expires IS the probe, and one success clears it. Anything more elaborate
# would be a second retry authority.
def _turn_superseded(turn):
    """
    Has the turn that started this work been replaced by a newer one?

    THE RULE IS "NEWER TURN EXISTS", not "turn ended". A retry chain that is still running
    when the user says something else is working on a question they have moved on from, and
    every further attempt costs the current turn's latency and prints into its log. Turn 0
    means "not correlated" — boot-time and diagnostic calls — and is never superseded.
    """
    if not turn:
        return False
    # `latest_turn()`, NOT `current_turn()`. Between turns the open turn is 0, so a check
    # against it would report a finished turn as still current in exactly the window where a
    # stale retry is most likely to still be running.
    return logbus.latest_turn() > turn


DMM_UNHEALTHY_AFTER = 3
DMM_HEALTH_COOLDOWN_SECONDS = 20.0

# A WALL-CLOCK CEILING ON TOP OF THE COUNT, because a count alone is not a bound on the user's
# wait. Measured against the local model on this host: an empty completion costs ~2.0s, so five
# retries is ~10s of a person sitting in front of a silent assistant for a request that ends in
# "treat this as conversation" anyway.
#
# The budget does NOT replace the count — with a backend at 2s/attempt all five still run,
# which is the case the count was raised for. It stops a SLOWER backend from turning five
# retries into half a minute. Whichever limit is reached first wins, and the log says which.
#
# There is still no sleep anywhere in the retry path. This is a deadline, not a backoff: the
# model is up and merely produced nothing, so waiting between attempts would add latency and
# buy nothing.
def _retry_budget_seconds():
    """The budget, from `.env`, clamped to 2-120s. A budget of zero would disable the retry."""
    try:
        from kayra.core.config import env_float
        return env_float("KAYRA_DMM_RETRY_BUDGET_SECONDS", 15.0, 2.0, 120.0)
    except Exception:
        return 15.0


DMM_RETRY_BUDGET_SECONDS = 15.0

# ── A PER-ATTEMPT CEILING, WHICH IS A DIFFERENT BOUND FROM THE BUDGET ────
# The budget above bounds the WHOLE retry chain. It does not bound ONE attempt, and that gap
# is what made the worst case unpredictable: a local server that accepts a request and then
# takes twelve seconds to answer nothing spends almost the entire chain on a single call, so
# the five retries the count promises never happen and the user waits the full budget for one
# useless answer.
#
# Each attempt therefore gets its own deadline. Three seconds is comfortably above the ~2.0s
# an empty completion costs against LM Studio on this host, so a healthy backend never meets
# it; what it stops is one hung call eating the chain. It is CLAMPED BY THE REMAINING BUDGET
# at every call site, so the per-attempt bound can never extend the request past the deadline
# — the two limits compose, and whichever is tighter wins.
#
# This is a REQUEST TIMEOUT handed to the client, not a sleep and not a second retry
# authority: the SDK is constructed with `max_retries=0`, so a timeout raises once and the
# retry decision stays here, where the count and the budget already live.
DMM_ATTEMPT_TIMEOUT_SECONDS = 3.0


def _attempt_timeout_seconds(budget_left=None):
    """The deadline for ONE attempt: the per-attempt ceiling, capped by what is left."""
    timeout = DMM_ATTEMPT_TIMEOUT_SECONDS
    if budget_left is not None:
        timeout = min(timeout, max(0.05, float(budget_left)))
    return timeout


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 THE FAST PATH: commands that ARE their token           │
# └────────────────────────────────────────────────────────────────────────┘
# "pause", "next tab", "volume 40": the whole utterance already IS the token the classifier
# would emit, so asking a model costs a round-trip (~0.4 s local, ~0.7 s cloud) to be told what
# was said. The rule that keeps this safe is the one the local control vocabulary uses: the
# match is EXACT on the WHOLE utterance (name and "please" removed), never a prefix or a
# substring — so "pause" is here and "pause for a second and tell me a joke" is not, and
# "stop the music" still reaches the model. Words that are also ordinary conversation on their
# own ("search", "save", "print", "find", "enter", "restore", "battery") are deliberately
# absent, as are the broad closes. `DMM_FAST_PATH=off` disables it.
_FAST_COMMANDS = frozenset({
    "pause", "resume", "next track", "previous track",
    "new tab", "close tab", "next tab", "previous tab", "reopen tab", "duplicate tab",
    "go back", "go forward", "refresh", "reload",
    "maximize", "minimize", "minimize all", "show desktop", "snap left", "snap right",
    "close window", "switch window", "alt tab", "task view", "fullscreen",
    "zoom in", "zoom out", "reset zoom",
    "undo", "redo", "select all", "copy", "paste", "cut",
    "read clipboard", "clear clipboard",
    "take screenshot", "screenshot", "scroll up", "scroll down",
    "cancel timer", "list timers", "wifi on", "wifi off",
})
_FAST_PATTERNS = (
    (re.compile(r"^(?:set )?(?:the )?volume (?:to )?(\d{1,3})(?: percent)?$"), "system volume {0}"),
    (re.compile(r"^(?:set )?(?:the )?brightness (?:to )?(\d{1,3})(?: percent)?$"), "system brightness {0}"),
    (re.compile(r"^mute$"), "system mute"),
    (re.compile(r"^unmute$"), "system unmute"),
    # Slides (Part 4, E3): two words the local model reads as chat.
    (re.compile(r"^(next|previous) slide$"), "browser {0} slide"),
    (re.compile(r"^(?:go )?back a slide$"), "browser previous slide"),
)


# Rules before the model (tuning pass V9): a callable (utterance) -> [token] | None that the
# automation backend registers at boot (`common.direct_tokens`). The intelligence layer never
# imports automation; it only calls what it was handed. None = no router (tests, Windows).
RULE_ROUTER = None


def set_rule_router(fn):
    global RULE_ROUTER
    RULE_ROUTER = fn


def _fast_path_enabled(engine):
    value = str((getattr(engine, "env_vars", None) or {}).get("DMM_FAST_PATH", "on"))
    return value.strip().lower() not in ("0", "off", "false", "no", "disabled")


def fast_intent(text, assistant_name=""):
    """The token for an utterance that is itself a complete command, else None. No model."""
    cleaned = re.sub(r"[^\w\s%]", " ", str(text or "").lower()).replace("%", " percent")
    words = cleaned.split()
    name = str(assistant_name or "").lower().strip()
    words = [w for w in words if w != "please" and (not name or w != name)]
    while words and words[0] in ("hey", "ok", "okay"):
        words.pop(0)
    if not words or len(words) > 6:
        return None
    cleaned = " ".join(words)
    if cleaned in _FAST_COMMANDS:
        return cleaned
    for pattern, template in _FAST_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            return template.format(*match.groups())
    return None



# ┌────────────────────────────────────────────────────────────────────────┐
# │                       CLOUD MODELS AND THEIR DEFAULTS                  │
# └────────────────────────────────────────────────────────────────────────┘
# Every id is overridable in `.env`. Each provider has a DECISION model (the DMM: a handful of
# output tokens, latency is everything) and a CHAT model (a spoken reply); by default they are
# the same model, called two different ways, so one key is enough for everything.
#
# Claude Haiku 5.5: the fastest first token and the cheapest call of the three, with vision.
# Groq: gpt-oss-20b answers the DMM fastest; gpt-oss-120b talks better.
# Gemini: the `-latest` alias follows Google's current Flash model, so a retirement (the reason
# `gemini-2.5-flash` stopped answering) cannot strand a configuration again.
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"
GROQ_DEFAULT_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_DEFAULT_DECISION_MODEL = "openai/gpt-oss-20b"
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# The keys that, when they change, rebuild that provider's client and clear its cooldown.
PROVIDER_KEYS = {
    CLAUDE: ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"),
    GROQ: ("GROQ_API_KEY",),
    GEMINI: ("GEMINI_API_KEY",),
}


_MEETING_CODE = re.compile(r"\b([a-z]{3})[\s-]*([a-z]{4})[\s-]*([a-z]{3})\b", re.I)
_SHARE_WORDS = re.compile(r"\b(shar(?:e|ing)|screen ?share|present(?:ing| now)?)\b", re.I)


def _guard_meeting_code(task, prompt):
    """
    A `meet …` token carrying a meeting code the user never said -> None (dropped), or, when
    the user was asking to share their screen, `meet <the user's own words>` so the share
    parser reads them. Every other token is returned unchanged. The code counts as said when
    its ten letters appear in order in the utterance ("abc defg hij" spoken aloud included).
    """
    lowered = task.lower()
    if not lowered.startswith("meet "):
        return task
    code = re.search(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b", lowered)
    if code is not None:
        letters = re.sub(r"[^a-z]", "", code.group(0))
        said = re.sub(r"[^a-z]", "", str(prompt or "").lower())
        if letters in said:
            return task
    elif not (lowered.startswith("meet join") or lowered.startswith("meet start")) \
            or not _SHARE_WORDS.search(str(prompt or "")) \
            or re.search(r"\b(?:join|new (?:meeting|call)|(?:start|create|begin) an? "
                         r"(?:new )?(?:meeting|call))\b", str(prompt or ""), re.I):
        return task
    # A fabricated code, or "start …" read as a new meeting when the user said share.
    if _SHARE_WORDS.search(str(prompt or "")):
        warning(Subsystem.DMM, "A share request was emitted as a meeting join; "
                               "re-reading it from the user's words.")
        return f"meet {' '.join(str(prompt).split())}"
    warning(Subsystem.DMM, f"Meeting code the user never said was dropped ({task!r}).")
    return None


def _real_key(value):
    """A key as typed, or "" for a blank or the `.env.example` placeholder."""
    value = str(value or "").strip()
    if not value or value.lower().startswith(("your_", "gsk_your", "sk-ant-your")):
        return ""
    return value


def claude_speed_args(model, effort="low"):
    """
    The request fields that make a Claude call fast, for THIS model.

    Haiku 5.5 thinks by default; a spoken reply and a three-token classification need no
    reasoning pass, and on a voice turn that pass is the wait. Thinking can be turned off on
    Haiku 5.5 (at effort high or below), not on Opus 5.5 or Sonnet 5.5, where low effort is the
    lever. The Claude 4.5-and-older models take neither field (thinking is off there anyway).
    """
    name = str(model or "").lower()
    if name.startswith(("claude-3", "claude-haiku-4", "claude-sonnet-4-5", "claude-opus-4-1",
                        "claude-opus-4-0", "claude-sonnet-4-0")):
        return {}
    effort = effort if effort in ("low", "medium", "high") else "low"
    args = {"output_config": {"effort": effort}}
    if name.startswith("claude-haiku-5"):
        args["thinking"] = {"type": "disabled"}
    return args


def to_claude_messages(api_messages):
    """
    OpenAI-format messages -> (system text, Claude messages).

    Every caller of `generate_chat_stream` builds the OpenAI shape (system layers, history,
    the query). Claude takes the system prompt as its own field and only user/assistant turns
    in `messages`, so the system entries are joined in order into one system prompt. Text
    parts of a list-shaped content are kept; a conversation must start with the user.
    """
    system_parts, messages = [], []
    for message in api_messages or []:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content
                                if isinstance(part, dict) and part.get("type") == "text")
        content = str(content or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        if not content.strip():
            continue
        messages.append({"role": "assistant" if role == "assistant" else "user",
                         "content": content})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "Continue."})
    return "\n\n".join(system_parts), messages


def claude_text(response):
    """The reply's text blocks, joined. Read by TYPE: a response may begin with thinking."""
    return "".join(getattr(block, "text", "") for block in getattr(response, "content", None) or []
                   if getattr(block, "type", "") == "text")


class CentralizedLLMEngine:
    """
    Centralized intelligence routing matrix.
    Checks host environments to dynamically swap between offline local endpoints (Ollama/LM Studio)
    and online cloud endpoints (Claude/Groq/Gemini). Houses the Decision-Making Model (DMM) for intent parsing.

    Singleton: every module in the project does `engine = CentralizedLLMEngine()` at import
    time (main.py, chatbot.py, real_time_search.py, automation_windows.py, deep_research.py).
    Without a singleton, that meant up to 5 fully independent instances at every boot — each
    re-probing the local LLM server, re-reading .env, and re-constructing its own Claude/Groq/
    Gemini API clients. `__new__` here ensures they all share ONE instance/one set of clients.
    """
    _instance = None
    _has_booted = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return  # Shared singleton already fully constructed — nothing to redo.
        self._initialized = True

        # One cached parse of .env for the whole process — see kayra/core/config.py.
        self.env_vars = env_values()
        
        self._load_models()
        self._load_local_settings()
        force_online, cloud_first = self._cloud_pinned()

        # ┌────────────────────────────────────────────────────────┐
        # │                 MODE SELECTION MATRIX                  │
        # └────────────────────────────────────────────────────────┘
        # LOCAL FIRST, unless the user pinned the cloud (FORCE_ONLINE, or CLOUD_FIRST with a
        # cloud key). Re-checked while running — see `_sync_mode`.
        self._mode_checked_at = time.monotonic()
        if force_online or cloud_first:
            if not CentralizedLLMEngine._has_booted:
                reason = "FORCE_ONLINE" if force_online else "CLOUD_FIRST"
                print_system(f"{reason} is active. Bypassing local checks to run Cloud Mode.")
            self.is_online = True
            self.is_local_active = False
        else:
            self.is_local_active = self._check_local_server()
            self.is_online = not self.is_local_active

        # ┌────────────────────────────────────────────────────────┐
        # │                   CLIENT ALLOCATION                    │
        # └────────────────────────────────────────────────────────┘
        # Priority Rule: a local LLM server (LM Studio / Ollama) wins unless the cloud is
        # forced. When local is active, ALL operations (DMM + Chat) go through it exclusively.
        local_key = self._setting("LOCAL_API_KEY", "lm-studio")
        self.local_client = OpenAI(
            base_url=self.local_base_url,
            api_key=local_key,
            # Same rule as the cloud clients: one attempt, bounded. A local server that has
            # hung should hand the turn back rather than be retried behind the caller's back.
            max_retries=0,
            timeout=self._provider_timeout(),
        )

        # ── Cloud clients (only activated when local server is NOT running) ──
        #
        # `max_retries=0` IS LOAD-BEARING, AND IT WAS MEASURED, NOT ASSUMED.
        #
        # The OpenAI and Anthropic SDKs retry transport failures twice by default, with their
        # own backoff. That is a SECOND retry authority underneath the router, and it produces
        # exactly the failure mode the router exists to prevent: one user request becomes
        # three provider calls, the fallback the router would have made instantly is delayed by
        # the SDK's backoff, and a rate-limited key gets hit two more times on the way.
        # Measured live on this machine before the change: a single Groq DMM call took 9.9s and
        # then 24.0s, while the router's own fallback for the same class of failure takes
        # ~700ms. The timeout is the other half of the same rule: a hung provider must hand
        # the turn back rather than block the user while good fallbacks sit idle.
        self.claude_client = self.groq_client = self.gemini_client = None
        if self.is_online:
            self._build_cloud_clients()

        # ┌────────────────────────────────────────────────────────┐
        # │              THE PROVIDER ROUTING AUTHORITY            │
        # └────────────────────────────────────────────────────────┘
        # ONE router for the process, holding ONE set of cooldowns. Every provider decision
        # this engine makes goes through it, and neither `classify_intent` nor
        # `generate_chat_stream` contains fallback logic of its own.
        #
        # Availability is registered as a CALLABLE rather than a bool: the cloud clients
        # exist only in online mode and are rebuilt when a key changes, and a router holding a
        # stale True would route into a `None`.
        self.router = get_provider_router()
        self.router.register(CLAUDE, lambda: self.claude_client is not None)
        self.router.register(GROQ, lambda: self.groq_client is not None)
        self.router.register(GEMINI, lambda: self.gemini_client is not None)
        self.router.register(LOCAL, lambda: not self.is_online)
        self._refresh_status()

        # Secure the boot-lock so prints never repeat on subsequent instantiations
        CentralizedLLMEngine._has_booted = True

        # Valid intents/commands acceptable by the system parser, and the DMM's few-shot
        # training prompt. Set here (not in run_boot_sequence) so classify_intent() works
        # immediately on any freshly constructed instance, regardless of whether the caller
        # ever invokes run_boot_sequence() — a standalone diagnostic script that only needs
        # classify_intent() shouldn't have to know that unrelated method exists.
        # The accepted-token vocabulary, grouped by the category it belongs to. This list is
        # the gate: `classify_intent` discards anything the model emits that does not start
        # with one of these, so a token here that no executor handles is worse than useless —
        # it passes the filter and is then silently dropped by the automation router.
        # Every token below is dispatched by modules/automation_windows.py or by main.py.
        self.funcs = [
            # conversation / retrieval / research
            "general", "realtime", "deep research", "exit",
            # assistant self-control (handled in app.py, not by the automation router)
            "proactive on", "proactive off",
            # Closing the microphone. There is no matching "start listening" token on purpose:
            # a paused microphone cannot hear the command to un-pause it, so resuming is a
            # manual UI action. See `app.set_listening`.
            "stop listening",
            # applications, windows, tabs
            "open", "close", "close all", "close everything",
            "close window", "close tab", "new tab",
            "minimize", "minimize all", "maximize", "show desktop",
            "snap left", "snap right", "switch window", "alt tab", "task view",
            "action center", "notification", "emoji",
            # media
            "play", "pause", "resume", "next track", "previous track", "stop media",
            # system control & information
            "system", "wifi",
            "battery", "cpu", "ram", "disk", "uptime", "ip address",
            # authoring, input, clipboard
            "content", "write", "type", "copy", "paste", "copy text",
            # browser/editor hotkeys
            "undo", "redo", "select all", "save", "save file", "find", "search",
            "refresh", "reload", "fullscreen", "print",
            "zoom in", "zoom out", "reset zoom", "task manager", "run dialog",
            # search pages & utilities
            "google search", "youtube search",
            "screenshot", "take screenshot", "timer", "set timer", "reminder",
            # ── added with the automation upgrade (2026-09-07) ──
            # Every one of these is dispatched by automation_windows.normalize_command; the
            # acceptance gate must never contain a token no executor handles.
            "focus", "switch to", "restart app", "restore",
            "next tab", "previous tab", "reopen tab", "duplicate tab",
            "go back", "go forward",
            "open folder", "open file", "create folder", "create file",
            "delete file", "delete folder", "rename", "find file", "search files",
            "cut", "clear clipboard", "read clipboard",
            "cancel timer", "list timers",
            "click", "double click", "right click", "scroll up", "scroll down",
            "terminal", "run command",
            # ── Round 3 (2026-11-10). Each is parsed by automation.common. ──
            "browser", "meet", "email", "calendar", "message", "diagnose", "screen",
            "scene", "plan", "trigger", "files", "git",
            "click on", "click the", "click at", "double click on", "right click on",
            "move mouse to",
        ]
        
        # Preamble instructions to restrict DMM responses to structured task labels.
        #
        # Organised by intent CATEGORY (conversation / retrieval / app / window / tab / media /
        # system / info / input / utility). The category names are for the classifier's
        # reasoning only — they are never emitted; the parser expects the literal tokens.
        #
        # Kept deliberately tight: this preamble plus `dmm_chat_history` is re-sent on every
        # single classification, so redundant prose is paid for on every user turn.
        self.dmm_preamble = """
            You are a Decision-Making Model. You classify the user's request into system task tokens.

            *** DO NOT ANSWER THE QUERY. OUTPUT ONLY TOKENS. ***

            OUTPUT CONTRACT (violating this breaks the system):
            - Output a comma-separated list of task tokens and NOTHING else.
            - No prose, no greeting, no explanation, no markdown, no quotes, no emojis.
            - Only use the token names defined below. Never invent a token.
            - Where a token carries text ('general', 'realtime', 'deep research', 'open',
              'close', 'play', 'content', 'write', 'system', ...), that text is THE USER'S
              OWN WORDS. Copy them. Never write the literal words 'query', 'topic', 'text'
              or '...' as the payload — those are placeholders in these instructions, not
              output. 'what is the capital of japan' -> 'general what is the capital of
              japan', NOT 'general query'.
            - Never emit the same token twice for one request.
            - Emit ONE token per distinct action the user actually asked for. Do not invent
              extra actions, and do not merge two genuinely different actions into one.

            =========================================================
            A. DECIDING BETWEEN CONVERSATION, RETRIEVAL AND RESEARCH
            =========================================================
            Judge the SENTENCE AS A WHOLE. Never pick an automation token just because an
            automation keyword ("open", "close", "play", "find", "screenshot", "minimize")
            happens to appear inside a sentence that is really a question or a discussion.

            -> 'general ...' — answerable from an LLM's own knowledge, or conversational,
               or about the current time/date, or too vague/pronoun-bound to resolve
               ('who is he?', 'tell me more about him.').
            -> 'realtime ...' — needs current information the model cannot know: news,
               prices, weather, live status, "who is the current ...", anything about a
               named person/company/product where freshness matters. Also use this when the
               user asks to look something up or search the web for an ANSWER.
            -> 'deep research ...' — only when the user explicitly asks for deep research,
               a deep dive, an exhaustive report, or a thorough investigation of a topic.

            Boundary rules:
            - "search the web for X" / "look up X" / "google what X is" -> 'realtime X'
              (the user wants the ANSWER).
            - 'google search (topic)' / 'youtube search (topic)' ONLY when the user wants the
              SEARCH RESULTS PAGE opened in a browser, e.g. "search youtube for lofi mixes".
            - A how-to question is conversation, not an instruction to perform the action.

            =========================================================
            B. APPLICATIONS, WINDOWS AND TABS  (do not confuse these)
            =========================================================
            -> 'open (app or website)'  — launch an application or site: "open chrome",
               "open github.com". Multiple: 'open chrome, open telegram'.
            -> 'close (app name)'       — close a NAMED application: "close spotify".
                                          ONE window/instance, never all of them.
            -> 'close all (app name)'   — ONLY when the user explicitly says all/every:
                                          "close all chrome windows" -> 'close all chrome'.
            -> 'close everything'       — ONLY for "close everything" / "close all windows".
            -> 'close window'           — close the CURRENT/THIS window (no app named).
            -> 'close tab'              — close the CURRENT/THIS browser tab.
            -> 'new tab'                — open a new browser tab (NOT 'open').
            -> Websites by name are 'open (site)' with the user's own words, including WHERE:
               "open google sheet" -> 'open google sheet'; "open notion in this tab" ->
               'open notion in this tab'; "open youtube in incognito" -> 'open youtube in
               incognito'; "open new tab and open google sheets in that tab" -> 'new tab,
               open google sheets in that tab'; "I need to check Google Sheets" -> 'open
               google sheets'; "update the chrome extension" -> 'browser update'.
            The distinction is what the user named: an application -> 'close (app)';
            "this window" -> 'close window'; "this tab" -> 'close tab'.

            Window tokens (emit exactly): 'minimize' (this window), 'minimize all' (every
            window / show the desktop), 'maximize', 'snap left', 'snap right',
            'switch window' (also for alt tab), 'task view', 'notification' (action centre
            / notifications), 'emoji'.

            =========================================================
            C. MEDIA
            =========================================================
            -> 'play (song, artist or genre)' — start specific music: "play let her go",
               "i want to listen to rock music" -> 'play rock music'.
            -> 'pause'       — pause whatever is playing (also a bare "play/pause" toggle).
            -> 'resume'      — resume paused playback.
            -> 'next track'  / 'previous track' — skip forward/back.
            -> 'stop media'  — stop playback entirely ("stop the music").
            "Play" only means media when the user is asking for audio; "tell me about the
            play Hamlet" is conversation.

            =========================================================
            D. SYSTEM, INFORMATION, INPUT AND UTILITIES
            =========================================================
            -> 'system (task)' — volume, brightness, mute/unmute, lock, shutdown, restart,
               sleep. Keep the user's wording: 'mute the sound' -> 'system mute';
               'increase volume by 20%' -> 'system increase volume by 20%';
               'set brightness to 50' -> 'system brightness 50%'; 'lock my pc' -> 'system lock'.
               Desktop-session controls are 'system' too, again in the user's words:
               'go to workspace 3' -> 'system workspace 3'; 'move this window to workspace 2'
               -> 'system move window to workspace 2'; 'turn on bluetooth' -> 'system
               bluetooth on'; 'enable do not disturb' -> 'system do not disturb on';
               'what song is this' -> 'system now playing'; 'make this window float' ->
               'system toggle floating'; 'turn on night light' -> 'system night light on';
               'open an empty workspace' -> 'system workspace empty'; 'is my laptop
               charging' -> 'system battery status'; 'how hot is my cpu' -> 'system
               temperature'; 'which wifi am I on' -> 'system wifi network'; 'switch to
               performance mode' -> 'system power profile performance'; 'undo what you just
               did' -> 'system undo last action' (a bare 'undo' stays the Ctrl+Z hotkey).
            -> 'click on (visible text)' — click a button or link by the words on it:
               "click the save button" -> 'click on save'; 'double click on (text)',
               'right click on (text)', 'click at (x) (y)', 'move mouse to (text or place)'.
               Settings screens are opened with 'open': "open control panel" -> 'open
               control panel'; "open wifi settings" -> 'open wifi settings'.
            -> 'wifi on' / 'wifi off' — enable/disable Wi-Fi.
            -> System information about THIS machine (emit exactly): 'battery', 'ram', 'cpu',
               'disk', 'uptime', 'ip address'. These are about the user's own computer — a
               general question about what RAM is remains 'general'.
            -> Clipboard: 'copy', 'paste', 'copy text (message)' for copying specific text.
            -> 'write (text)' — type text at the cursor: "type hello world" -> 'write hello world'.
            -> 'content (topic)' — compose something written: an email, an essay, code, a
               document. "write me an email to my boss" -> 'content email to my boss'.
               'content' is for AUTHORING; 'write' is for KEYSTROKES.
            -> 'take screenshot' — capture the screen.
            -> 'set timer (duration)' — "set a timer for 5 minutes" -> 'set timer 5 minutes'.
            -> 'reminder (datetime message)' — "remind me at 9pm on 25 june about the meeting"
               -> 'reminder 9:00pm 25 june meeting'.
            -> Editing/browser hotkeys (emit exactly): 'undo', 'redo', 'select all', 'save',
               'save file', 'find', 'search', 'print', 'refresh', 'reload', 'fullscreen',
               'zoom in', 'zoom out', 'reset zoom', 'task manager', 'run dialog'.

            =========================================================
            D2. FOCUS, TABS, FILES, MOUSE AND TERMINAL
            =========================================================
            -> 'focus (app)' — bring an app to the front: "switch to vs code", "go to chrome",
               "bring the terminal up" -> 'focus vs code' / 'focus chrome' / 'focus terminal'.
               This is NOT 'switch window' — that one is a blind alt-tab, used only when the
               user names no target ("switch windows", "alt tab").
            -> 'restart app (name)' — close and reopen an application.
            -> Browser tabs (emit exactly): 'new tab', 'close tab', 'next tab',
               'previous tab', 'reopen tab', 'duplicate tab', 'go back', 'go forward',
               'refresh'.
            -> Files and folders: 'open folder (name)' ("open my downloads folder"),
               'open file (name)', 'create folder (name)', 'create file (name)',
               'rename (old) to (new)', 'find file (name)', 'delete file (name)'.
               A path or folder is NOT an app: "open my downloads folder" is
               'open folder downloads', never 'open downloads'.
            -> Clipboard extras: 'cut', 'read clipboard', 'clear clipboard'.
            -> Mouse (emit exactly): 'click', 'double click', 'right click', 'scroll up',
               'scroll down'.
            -> Timers: 'cancel timer', 'list timers'.
            -> 'terminal (command)' — ONLY when the user explicitly asks to run a shell or
               terminal command: "run git status in the terminal" -> 'terminal git status'.
               A question about a command is conversation, not execution.

            =========================================================
            D3. INSIDE CHROME, DIAGNOSTICS, SCREEN, MODES, PLANS
            =========================================================
            NEVER put a comma INSIDE one of these commands — commas separate commands. Use
            ' saying ' or ':' instead.
            -> 'browser (what to do in the open web page or tabs)' — keep the user's words:
               'list my tabs' -> 'browser list tabs'; 'go to my gmail tab' -> 'browser switch
               to gmail tab'; 'close the youtube tab' -> 'browser close youtube tab'; 'go to
               the third tab' -> 'browser switch to the third tab'; 'close duplicate tabs' ->
               'browser close duplicate tabs'; 'close the tabs on the right' -> 'browser close
               tabs to the right'; 'what did I just close' -> 'browser what did i just close';
               'find the tab where I was reading about rust' -> 'browser find the tab about
               rust'; 'copy this page's link' -> 'browser copy the link'; 'open the third
               result' / 'play the second video' -> 'browser open the third result' / 'browser
               play the second video'; 'find pricing on this page' -> 'browser find pricing on
               this page', then 'next match' -> 'browser next match'; 'close all my YouTube
               tabs' -> 'browser close all youtube tabs'; 'close everything except the pinned
               tabs' -> 'browser close all but pinned'; 'group my college tabs' -> 'browser group
               my college tabs'; 'make the research group blue' / 'collapse the research group'
               / 'rename this group to thesis' / 'ungroup the research group' -> 'browser ' +
               the same words; 'move this tab to a new window' -> 'browser move this tab to a
               new window'; 'merge my Chrome windows' -> 'browser merge my chrome windows';
               ABOUT THE PAGE IN FRONT: a QUESTION about the page is answered from it, never a
               word search: 'what does this page say about pricing' -> 'browser what does this
               page say about pricing' (NOT 'browser find pricing on this page', which only
               highlights a word); 'read the comments' / 'what are people saying
               about this' / 'summarize the reviews' -> 'browser ' + the same words; 'summarize
               this video' -> 'browser summarize this video'; 'translate this page to Hindi' ->
               'browser translate this page to hindi'; 'explain the selected text' -> 'browser
               explain the selected text'; 'bookmark this page' -> 'browser bookmark this page';
               'delete the bookmark for X' -> 'browser delete the bookmark for x'; 'clear today's
               history' -> 'browser clear today's history'; DOWNLOADS are files, not closed
               tabs: 'what did I download' -> 'browser what did i download' (NOT 'what did i
               just close'); 'open my last download' / 'pause the download' -> 'browser ' + the
               same words; 'close it' / 'go
               back to it' after Kayra opened a tab -> 'browser close it' / 'browser go back to
               it'; 'search this sheet for Ravi' / 'search for Ravi here' -> 'browser search for
               ravi here' (a web search for facts stays 'google search ...'); 'what can I do
               here' -> 'browser what can i do here'; FORMS AND VIDEOS: 'fill this form with
               my details' / 'select India in the country dropdown' / 'tick I agree' / 'submit
               this form' / 'upload my resume here' / 'press tab' -> 'browser ' + the same
               words; with a video in front 'skip 10 seconds' / 'go back 30 seconds' / 'go to
               2:30' / 'play at 1.5x' / 'skip the ad' -> 'browser ' + the same words; 'search
               Amazon for earbuds under 2000' -> 'browser search amazon for earbuds under 2000';
               on a shop page 'how much is this' / 'sort by price low to high' -> 'browser ' +
               the same words; 'click
               the sign in button' -> 'browser click on sign in'; 'type hello in the search
               box' -> 'browser type hello into search'; 'summarize this page' -> 'browser
               summarize this page'; 'read this article' -> 'browser read this page'; 'open
               the article I read yesterday about transformers' -> 'browser open the article
               i read yesterday about transformers'; 'connect to chrome' -> 'browser set up'.
               A click INSIDE the browser page is 'browser click on X'; a click anywhere else
               on the screen is 'click on X'.
            -> 'meet (action)' — Google Meet: 'join my meeting abc-defg-hij' -> 'meet join
               abc-defg-hij' (ONLY a code the user actually said; 'start a meeting' -> 'meet
               join new'); 'mute me' -> 'meet mic off'; 'turn on my camera' -> 'meet camera
               on'; SCREEN SHARING is always ONE 'meet share' token, never a click and never
               'meet join': 'share my screen' / 'start screen sharing' / 'present now' -> 'meet
               share screen'; 'share the LM Studio window' -> 'meet share the lm studio window'
               (NOT 'focus lm studio'); 'share this tab' -> 'meet share this tab'; 'share my
               second screen' -> 'meet share my second screen'; 'share it with sound' -> 'meet
               share screen with audio'; 'am I sharing' -> 'meet am i sharing'; 'stop sharing'
               -> 'meet stop sharing'; 'leave the call' -> 'meet leave'.
            -> Gmail: 'email check' (how many unread); 'email open latest' ("open my latest
               email", "open the newest mail"); 'email open first' / 'email open second' ("open
               the first/second email"); 'email open from (name)'; 'email open unread';
               'email read latest' ("read my latest email" = open it AND read it); 'email read
               this' ("read this email"); 'email read inbox' ("read my emails"); 'email open
               sent' / 'email open all mail' / 'email open starred' / 'email open drafts'
               ("go to my sent mails"); 'email reply', 'email forward', 'email archive',
               'email delete', 'email back'; 'email draft to (address) about (subject) saying
               (text)', 'email send'. Opening an email is NEVER 'open (something)' — it is
               'email open ...'. 'search my mail for the fee receipt' -> 'email search for fee
               receipt'; 'show my unread emails from this week' -> 'email show unread from this
               week'; 'compose a new email' -> 'email compose'; 'open my Work label' -> 'email
               open my work label'.
            -> GOOGLE WORKSPACE files are 'browser ...': 'open my budget sheet' -> 'browser open
               my budget sheet'; 'open the doc about the internship' -> 'browser open the doc
               about the internship'; 'search my docs for offer letter' -> 'browser search my docs
               for offer letter'; 'find the PDF Ravi sent me' -> 'browser find the pdf ravi sent
               me'; 'create a new sheet called Expenses' -> 'browser create a new sheet called
               expenses'; 'go to cell B12' -> 'browser go to cell b12'; 'what's the total of
               column C' -> 'browser what's the total of column c'; 'read row 5' -> 'browser read
               row 5'; 'share this doc with ana@x.com' -> 'browser share this doc with ana@x.com';
               'start the slideshow' / 'next slide' / 'end the slideshow' -> 'browser ' + the same
               words; 'open my recent files' -> 'browser open my recent files'; 'add a note
               saying buy milk' -> 'browser add a note saying buy milk'; 'open the responses of
               my feedback form' -> 'browser open the responses of my feedback form'; 'what
               assignments are due' -> 'browser what assignments are due'. A Google file is
               NEVER 'open (app)'.
            -> ANY "click on X" / "scroll down" while the user is in the browser is fine as
               plain 'click on X' / 'scroll down': Kayra sends it into the page itself.
            -> 'calendar today', 'calendar tomorrow', 'calendar friday', 'calendar add (title)
               (day) at (time)'; 'what's my next meeting' -> 'calendar next meeting'; 'join my
               next meeting' -> 'calendar join my next meeting'. In a call, 'let them in' /
               'admit everyone' -> 'meet admit everyone'.
            -> 'message (contact): (text)' — WhatsApp: "tell rahul I'm running late" ->
               'message rahul: i'm running late'. Other WhatsApp requests: 'open the PR group on
               WhatsApp' -> 'message open the pr group'; 'who messaged me on WhatsApp' ->
               'message who messaged me'; 'what did they say in the core team today' -> 'message
               read the core team today'.
            -> 'diagnose (what)' — "diagnose my computer", "why is my laptop slow", "why is the
               fan so loud", "what's draining my battery", "why is the internet slow", "is my
               computer secure", "what crashed" -> 'diagnose computer' / 'diagnose slow' /
               'diagnose heat' / 'diagnose battery' / 'diagnose network' / 'diagnose security'
               / 'diagnose crashes'. 'fix it' right after a diagnosis -> 'diagnose fix'.
            -> 'screen (question)' — about what is VISIBLE: "what's on my screen", "explain
               this error", "read this to me", "copy the text on screen", "record my screen",
               "stop recording" -> 'screen describe' / 'screen explain this error' / 'screen
               read' / 'screen copy text' / 'screen record' / 'screen stop recording'.
            -> 'scene (name)' — modes: "work mode", "start movie mode", "save this layout as
               study", "what modes do I have" -> 'scene work' / 'scene movie' / 'scene save
               study' / 'scene list'.
            -> 'plan (goal)' — a goal needing several different steps that no single command
               covers: "get me ready for my presentation" -> 'plan get me ready for my
               presentation'.
            -> 'trigger (when ...) (what to do)' — automations: "when the download finishes
               shut down the computer" -> 'trigger when the download finishes shut down the
               computer'; "every day at 9 start work mode"; "list my automations" -> 'trigger
               list'; "cancel the download automation" -> 'trigger cancel download'.
            -> 'files (request)' — "organize my downloads", "find the pdf I downloaded
               yesterday", "what's taking up space", "what did I work on today", "summarize
               resume.pdf", "convert report.docx to pdf", "back up my documents" -> 'files
               organize downloads' / 'files find the pdf i downloaded yesterday' / 'files space'
               / 'files recent today' / 'files summarize resume.pdf' / ...
            -> 'git status', 'git branch', 'git commit (message)'.
            -> More 'system' wording kept verbatim: 'system switch to headphones', 'system
               mute my mic', 'system connect my earbuds', 'system earbuds battery', 'system
               connect to wifi (name)', 'system check for updates', 'system update my system',
               'system install (app)', 'system uninstall (app)', 'system what's using my cpu',
               'system close the app using the most memory', 'system set refresh rate to 60',
               'system theme catppuccin', 'system next wallpaper', 'system read my
               notifications', 'system morning briefing' ("brief me", "good morning, what's
               my day"), 'system what did you just do', 'system what's open', 'system where is
               chrome', 'system chrome on the left and vs code on the right', 'system move all
               browsers to workspace 2', 'system remember my setup', 'system restore my last
               session', 'system set a sleep timer for 30 minutes', 'system run my tests',
               'system docker status'.

            =========================================================
            E. MULTI-INTENT, EXIT AND FALLBACK
            =========================================================
            *** MULTI-TASKING: one token per requested action, in the order requested.
                'open facebook and close whatsapp' -> 'open facebook, close whatsapp'
                'who is akshay kumar and what is his net worth' -> 'realtime who is akshay kumar and what is his net worth'
                Only split when the actions are genuinely different; a single question about
                one subject stays a single token.
            *** ASSISTANT SELF-CONTROL: the user is talking about Kayra's own unprompted
                suggestions, NOT about audio playback.
                "stop proactive suggestions" / "don't interrupt me" / "disable proactive mode"
                / "stop giving me suggestions" -> 'proactive off'
                "enable proactive mode" / "you can suggest things again" -> 'proactive on'
            -> 'stop listening' — close the microphone: "stop listening", "pause listening",
               "pause the microphone". Not 'exit', which quits Kayra.
                A bare "stop" is NOT this token — it is handled by the audio layer and never
                reaches you. "stop the music" is 'stop media'.
            *** EXIT: goodbye / "that's all" / "exit" -> 'exit'
            *** FALLBACK: if you cannot confidently place the request, or it asks for
                something not listed above, emit 'general ' followed by their words. Never guess an
                automation token.
            """
        
        # Few-shot conversational history to teach DMM target output alignment.
        #
        # IMPORTANT ORDER: Most recent examples (bottom of list) have HIGHEST weight with every model,
        # so the list ends with the CONTRASTIVE pairs — each correct classification sitting
        # next to the near-miss it is most often confused with (close app / close window /
        # close tab, minimize / minimize all, look-up vs open-results-page, authoring vs
        # keystrokes, and automation keywords appearing inside ordinary questions).
        # Exit/bye examples are intentionally placed early so they never dominate recency.
        # NEVER slice or truncate this list: a `[:40]` slice once silently dropped exactly
        # these disambiguating examples in local-model mode.
        self.dmm_chat_history = [
            # -- Conversation & Knowledge --
            {"role": "User", "message": "how are you?"},
            {"role": "Chatbot", "message": "general how are you?"},
            {"role": "User", "message": "chat with me."},
            {"role": "Chatbot", "message": "general chat with me."},
            {"role": "User", "message": "who is he?"},
            {"role": "Chatbot", "message": "general who is he?"},
            {"role": "User", "message": "who is akshay kumar and what's his networth?"},
            {"role": "Chatbot", "message": "realtime who is akshay kumar, general what's his networth?"},
            {"role": "User", "message": "what is todays date by the way remind me that i have a dancing performance on 5th aug 11:00pm"},
            {"role": "Chatbot", "message": "general what is today's date, reminder 11:00pm 5 aug dancing performance"},
            {"role": "User", "message": "run a deep research query on solid state hydrogen storage vectors"},
            {"role": "Chatbot", "message": "deep research solid state hydrogen storage vectors"},
            # -- Exit (placed early so it is NOT the freshest pattern) --
            {"role": "User", "message": "bye kayra."},
            {"role": "Chatbot", "message": "exit"},
            {"role": "User", "message": "Exit."},
            {"role": "Chatbot", "message": "exit"},
            # -- Media & Content --
            {"role": "User", "message": "play afsanay by ys and play let her go"},
            {"role": "Chatbot", "message": "play afsanay by ys, play let her go"},
            {"role": "User", "message": "i want to listen to some rock music"},
            {"role": "Chatbot", "message": "play rock music"},
            {"role": "User", "message": "search weather on google and search java on google"},
            {"role": "Chatbot", "message": "google search weather, google search java"},
            {"role": "User", "message": "search tutorial on youtube and search cooking on youtube"},
            {"role": "Chatbot", "message": "youtube search tutorial, youtube search cooking"},
            # -- System & Hardware --
            {"role": "User", "message": "mute the sound and turn up the volume"},
            {"role": "Chatbot", "message": "system mute, system volume up"},
            {"role": "User", "message": "increase volume by 30 percent"},
            {"role": "Chatbot", "message": "system increase volume by 30%"},
            {"role": "User", "message": "set brightness to 50"},
            {"role": "Chatbot", "message": "system brightness 50%"},
            {"role": "User", "message": "take a screenshot"},
            {"role": "Chatbot", "message": "take screenshot"},
            {"role": "User", "message": "check battery status and show me ram usage"},
            {"role": "Chatbot", "message": "battery, ram"},
            {"role": "User", "message": "set a timer for 5 minutes"},
            {"role": "Chatbot", "message": "set timer 5 minutes"},
            {"role": "User", "message": "pause the music"},
            {"role": "Chatbot", "message": "pause"},
            {"role": "User", "message": "skip to next song"},
            {"role": "Chatbot", "message": "next track"},
            {"role": "User", "message": "lock the computer and turn off wifi"},
            {"role": "Chatbot", "message": "system lock, wifi off"},
            {"role": "User", "message": "type hello world in the search bar"},
            {"role": "Chatbot", "message": "write hello world"},
            {"role": "User", "message": "what is my ip address and check cpu info"},
            {"role": "Chatbot", "message": "ip address, cpu"},
            {"role": "User", "message": "copy that and paste it"},
            {"role": "Chatbot", "message": "copy, paste"},
            {"role": "User", "message": "undo that and save the file"},
            {"role": "Chatbot", "message": "undo, save file"},
            {"role": "User", "message": "open task manager"},
            {"role": "Chatbot", "message": "task manager"},
            {"role": "User", "message": "open the emoji picker"},
            {"role": "Chatbot", "message": "emoji"},
            {"role": "User", "message": "refresh this page"},
            {"role": "Chatbot", "message": "refresh"},
            {"role": "User", "message": "zoom in a bit"},
            {"role": "Chatbot", "message": "zoom in"},
            {"role": "User", "message": "snap this window to the left"},
            {"role": "Chatbot", "message": "snap left"},
            {"role": "User", "message": "minimize all windows and take a screenshot"},
            {"role": "Chatbot", "message": "minimize all, take screenshot"},
            # -- Window Management (near end for recency) --
            {"role": "User", "message": "Minimize window."},
            {"role": "Chatbot", "message": "minimize"},
            {"role": "User", "message": "Maximize window."},
            {"role": "Chatbot", "message": "maximize"},
            # -- Open & Close (placed LAST for maximum recency weight) --
            {"role": "User", "message": "open chrome and tell me about mahatma gandhi."},
            {"role": "Chatbot", "message": "open chrome, general tell me about mahatma gandhi."},
            {"role": "User", "message": "open chrome and open telegram"},
            {"role": "Chatbot", "message": "open chrome, open telegram"},
            {"role": "User", "message": "open github.com and open claude.ai"},
            {"role": "Chatbot", "message": "open github.com, open claude.ai"},
            {"role": "User", "message": "close notepad and close spotify"},
            {"role": "Chatbot", "message": "close notepad, close spotify"},
            {"role": "User", "message": "Close youtube."},
            {"role": "Chatbot", "message": "close youtube"},
            {"role": "User", "message": "Open youtube."},
            {"role": "Chatbot", "message": "open youtube"},
            # -- CONTRASTIVE BOUNDARIES (LAST = highest recency weight) --
            # Each pair puts a correct classification next to the near-miss it is most often
            # confused with, which is what the model actually needs to separate them. These
            # sit at the end deliberately; see the ordering note above.
            # close: named app vs this window vs this tab
            {"role": "User", "message": "close spotify"},
            {"role": "Chatbot", "message": "close spotify"},
            {"role": "User", "message": "close this window"},
            {"role": "Chatbot", "message": "close window"},
            {"role": "User", "message": "close this tab"},
            {"role": "Chatbot", "message": "close tab"},
            # minimize one vs all
            {"role": "User", "message": "minimize this window"},
            {"role": "Chatbot", "message": "minimize"},
            {"role": "User", "message": "minimize everything"},
            {"role": "Chatbot", "message": "minimize all"},
            # look-up-the-answer vs open-the-results-page
            {"role": "User", "message": "search the web for the latest iphone price"},
            {"role": "Chatbot", "message": "realtime latest iphone price"},
            {"role": "User", "message": "search youtube for lofi mixes"},
            {"role": "Chatbot", "message": "youtube search lofi mixes"},
            # authoring vs keystrokes
            {"role": "User", "message": "write me an email to my boss about the delay"},
            {"role": "Chatbot", "message": "content email to my boss about the delay"},
            {"role": "User", "message": "type hello world"},
            {"role": "Chatbot", "message": "write hello world"},
            # automation keyword inside a conversational sentence -> conversation
            {"role": "User", "message": "how do i take a screenshot on a mac?"},
            {"role": "Chatbot", "message": "general how do i take a screenshot on a mac?"},
            {"role": "User", "message": "what's the best way to close a business deal?"},
            {"role": "Chatbot", "message": "general what's the best way to close a business deal?"},
            {"role": "User", "message": "tell me about the play hamlet"},
            {"role": "Chatbot", "message": "general tell me about the play hamlet"},
            {"role": "User", "message": "explain how to minimize latency in a web app"},
            {"role": "Chatbot", "message": "general explain how to minimize latency in a web app"},
            # proactive self-control vs stopping playback vs a real "stop" command
            {"role": "User", "message": "stop the music"},
            {"role": "Chatbot", "message": "stop media"},
            {"role": "User", "message": "stop giving me proactive suggestions"},
            {"role": "Chatbot", "message": "proactive off"},
            {"role": "User", "message": "don't interrupt me"},
            {"role": "Chatbot", "message": "proactive off"},
            {"role": "User", "message": "enable proactive mode again"},
            {"role": "Chatbot", "message": "proactive on"},
            # Editing keystrokes are not media controls. This pair sits at the very END on
            # purpose: with a media token in the last position, "undo that" was being pulled
            # to 'resume' by sheer recency weight (measured, 52/53 -> 53/53).
            {"role": "User", "message": "undo that"},
            {"role": "Chatbot", "message": "undo"},
            {"role": "User", "message": "redo that"},
            {"role": "Chatbot", "message": "redo"},
            # named-target focus vs blind alt-tab
            {"role": "User", "message": "switch to vs code"},
            {"role": "Chatbot", "message": "focus vs code"},
            {"role": "User", "message": "switch windows"},
            {"role": "Chatbot", "message": "switch window"},
            # a folder is not an application
            {"role": "User", "message": "open my downloads folder"},
            {"role": "Chatbot", "message": "open folder downloads"},
            {"role": "User", "message": "open spotify"},
            {"role": "Chatbot", "message": "open spotify"},
            # tab navigation vs track navigation
            {"role": "User", "message": "go to the next tab"},
            {"role": "Chatbot", "message": "next tab"},
            {"role": "User", "message": "play the next song"},
            {"role": "Chatbot", "message": "next track"},
            # running a command vs talking about one
            {"role": "User", "message": "run git status in the terminal"},
            {"role": "Chatbot", "message": "terminal git status"},
            {"role": "User", "message": "what does git status do?"},
            {"role": "Chatbot", "message": "general what does git status do?"},
        ]

    def run_boot_sequence(self, tts_engine=None):
        """
        Prints and speaks the model-routing status lines. Purely cosmetic narration —
        `self.funcs`/`self.dmm_preamble`/`self.dmm_chat_history` are already set in __init__,
        so classify_intent()/generate_chat_stream() work correctly whether or not this is ever
        called (callers that don't care about the boot narration, e.g. test scripts, can skip it).
        """
        # The hierarchy, stated as a hierarchy. `Claude > Groq > Gemini` for each route is the
        # whole routing policy, visible at a glance at every boot — which is what makes a later `[DMM] Fallback: Groq` legible instead of
        # surprising. Both lines come from the ONE router, so the boot report and the live
        # routing cannot drift into two accounts of the same configuration.
        info(Subsystem.LLM, "Providers")
        if self.is_online:
            field(Subsystem.LLM, "DMM", self.router.describe(ROUTE_DECISION))
            field(Subsystem.LLM, "Chat", self.router.describe(ROUTE_CHAT))
        else:
            field(Subsystem.LLM, "DMM", f"Local ({self.local_decision_model})")
            field(Subsystem.LLM, "Chat", f"Local ({self.local_chat_model})")

        # Narration is opt-in. main.py calls this WITHOUT a TTS engine and speaks one short
        # consolidated line instead: reading both status strings aloud cost ~7s of speech
        # before the assistant was usable, and made a fast boot sound like a slow one.
        if tts_engine:
            tts_engine.speak(self.dmm_status)
            time.sleep(0.1)
            tts_engine.speak(self.chat_status)
            time.sleep(0.1)

    def get_identity_prompt(self, mood: str = None):
        """
        Compiles the system persona instructions for the assistant based on env configuration.
        Constructs the identity prompt dynamically based on the configured name, gender,
        target language, and username.

        Parameters:
            mood: Optional detected user mood for this turn. Either a plain string or an
                  `emotion_engine.EmotionReading` (which is a str subclass carrying
                  `.confidence` and `.tone`). Steers TONE only — never intent.

        Returns:
            str: Compiled system alignment payload instruction block.
        """
        name = self.env_vars.get("ASSISTANT_NAME", "").strip()
        if not name:
            name = "Kayra"
            gender = "Female"
        else:
            gender = self.env_vars.get("ASSISTANT_GENDER", "Female").strip()
        lang = self.env_vars.get("LANGUAGE", "English").strip()
        username = self._setting("USERNAME", "User")
        user_gender = self.env_vars.get("USER_GENDER", "Male").strip()

        user_title = "Ma'am" if user_gender.lower() == "female" else "Sir"

        # NOTE ON THE FORMATTING RULES BELOW: this response is spoken by the TTS engine, so
        # markdown and symbols are not neutral decoration — they are either pronounced as
        # literal noise or removed by `speech_safe_text()`, which changes what the user hears.
        # Asking the model not to emit them is better than cleaning up afterwards; both
        # layers exist and are deliberately consistent with each other.
        prompt = (
            f"Hello, my username is {username}. You are a highly intelligent, empathetic, and witty AI companion named {name}. "
            f"Your gender profile is {gender}. You must always respond and converse fluently in {lang}.\n\n"
            f"YOU ARE BEING SPOKEN ALOUD. Your reply is converted to speech and heard, not read.\n\n"
            f"HOW TO TALK:\n"
            f"1. Talk like a close, trusted friend — warm, natural and direct. Show personality: be witty and "
            f"expressive rather than formal and robotic.\n"
            f"2. Open with the answer, not with filler. Never begin with 'Certainly!', 'Of course!', "
            f"'I would be happy to assist you with that' or any similar throat-clearing. "
            f"'Sure, I can help with that' is how a person actually says it.\n"
            f"3. Match the length to what was asked. A quick question gets a sentence or two; a request for an "
            f"explanation, comparison or walkthrough gets the detail it genuinely needs. Do not pad a short "
            f"answer to sound thorough, and do not compress a real explanation into one line.\n"
            f"4. Use plain spoken sentences. No markdown, no asterisks, no bullet points, no numbered lists, no "
            f"headers, no tables, no emojis, no code fences — none of that survives being spoken aloud. When you "
            f"need to list things, say them: 'there are three: X, Y and Z'.\n"
            f"5. If I explicitly ask for code or exact notation, give it plainly and keep the explanation around "
            f"it conversational.\n"
            f"6. Never add conversational 'notes', disclaimers, or a summary of what you just said.\n"
            f"7. Do not tell the time unless explicitly requested.\n"
            f"8. Under no circumstances should you ever mention your training data, AI architecture, or model limitations.\n"
            f"9. Address me as '{user_title}' — naturally, the way a person would, not in every sentence.\n"
            f"10. Only rely on up-to-date web information when it is explicitly provided to you in this context. "
            f"Otherwise, answer from your own trained knowledge and say so plainly if you are unsure about anything recent — never invent facts or pretend to have browsed the web."
        )

        # EMOTION INFLUENCES TONE, NEVER INTENT.
        #
        # This is the ONLY place a detected mood reaches the model, and all it can do is add a
        # delivery instruction. It cannot change what the user asked for: intent classification
        # happens in the DMM, which never sees the mood at all.
        #
        # `mood` may be a plain string (the historical contract) or an `EmotionReading`, which
        # subclasses str and additionally carries `.confidence` and `.tone`. Both work here.
        if mood and str(mood).strip().lower() not in ("", "neutral"):
            confidence = getattr(mood, "confidence", None)
            tone = getattr(mood, "tone", "")

            # A low-confidence guess about someone's emotional state is worse than no guess:
            # it steers the whole reply on weak evidence. The engine already returns neutral
            # below its own threshold; this is the second gate for callers passing raw strings.
            if confidence is None or confidence >= 0.45:
                guidance = (f" Specifically: {tone}." if tone else "")
                prompt += (
                    f"\n\nEMOTIONAL CONTEXT: {username} currently sounds {mood} based on how "
                    f"they phrased this.{guidance} Adjust only your TONE and length to suit — "
                    f"never change what they actually asked for, and never announce that you "
                    f"detected their mood."
                )

        return prompt

    # ── Cloud providers: models, clients, live key reload ──────────────

    def _load_models(self):
        """Model ids and keys from configuration. Re-run by `reload_providers`."""
        env = self.env_vars
        claude = env.get("CLAUDE_MODEL", "").strip() or CLAUDE_DEFAULT_MODEL
        self.claude_model = claude
        self.claude_decision_model = env.get("CLAUDE_DECISION_MODEL", "").strip() or claude
        self.claude_effort = env.get("CLAUDE_EFFORT", "low").strip().lower() or "low"
        self.groq_model = env.get("GROQ_CHAT_MODEL", "").strip() or GROQ_DEFAULT_CHAT_MODEL
        self.groq_decision_model = (env.get("GROQ_DECISION_MODEL", "").strip()
                                    or GROQ_DEFAULT_DECISION_MODEL)
        self.gemini_model = env.get("GEMINI_CHAT_MODEL", "").strip() or GEMINI_DEFAULT_MODEL
        self.gemini_decision_model = (env.get("GEMINI_DECISION_MODEL", "").strip()
                                      or self.gemini_model)
        self._keys = {
            provider: next((_real_key(env.get(name)) for name in names
                            if _real_key(env.get(name))), "")
            for provider, names in PROVIDER_KEYS.items()
        }

    def _build_cloud_clients(self, only=None):
        """
        Constructs the cloud client for each provider that has a key (or only for `only`).

        The Anthropic SDK is imported HERE, not at module top: importing this module must stay
        cheap and side-effect free, and a missing package (setup not re-run since Claude was
        added) must cost the Claude provider, not the whole engine.
        """
        timeout = self._provider_timeout()
        wanted = set(only or PROVIDER_KEYS)
        if CLAUDE in wanted:
            self.claude_client = None
            if self._keys.get(CLAUDE):
                try:
                    import anthropic
                    self.claude_client = anthropic.Anthropic(
                        api_key=self._keys[CLAUDE], max_retries=0, timeout=timeout)
                except ImportError:
                    warning(Subsystem.LLM, "A Claude key is set but the 'anthropic' package is "
                                           "not installed. Run: python setup.py")
        if GROQ in wanted:
            self.groq_client = (OpenAI(api_key=self._keys[GROQ], base_url=GROQ_BASE_URL,
                                       max_retries=0, timeout=timeout)
                                if self._keys.get(GROQ) else None)
        if GEMINI in wanted:
            self.gemini_client = (OpenAI(api_key=self._keys[GEMINI], base_url=GEMINI_BASE_URL,
                                         max_retries=0, timeout=timeout)
                                  if self._keys.get(GEMINI) else None)

    def _setting(self, name, default):
        """
        A configuration value, with BLANK treated as unset.

        `.get(name, default)` returns "" for a line like `LOCAL_BASE_URL=` — which is exactly
        what the Settings screen writes when a field is left empty. That blank endpoint made
        the local probe dial port 80, find nothing, and send every request to the cloud while
        LM Studio was running.
        """
        value = str(self.env_vars.get(name) or "").strip()
        return value or default

    def _load_local_settings(self):
        self.local_chat_model = self._setting("LOCAL_CHAT_MODEL", "local-model")
        self.local_decision_model = self._setting("LOCAL_DECISION_MODEL", "local-model")
        self.local_base_url = self._setting("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")

    def _cloud_pinned(self):
        """(FORCE_ONLINE, CLOUD_FIRST-with-a-key). Either one means: do not use the local model."""
        truthy = ("1", "true", "yes", "on")
        force_online = self._setting("FORCE_ONLINE", "False").lower() in truthy
        cloud_first = (self._setting("CLOUD_FIRST", "False").lower() in truthy
                       and any(self._keys.values()))
        return force_online, cloud_first

    # How often the local server is re-checked while running. The probe is a loopback TCP
    # connect (refused in well under a millisecond when nothing listens), plus one GET when it
    # does — so this bounds log noise and the rare slow probe, not CPU.
    MODE_RECHECK_SECONDS = 10.0

    def _sync_mode(self, force=False):
        """
        Follows the local server WHILE KAYRA RUNS. LM Studio started after Kayra used to leave
        the whole session on the cloud (the choice was made once, at boot); LM Studio quitting
        left it calling a dead local server. Now: local whenever it answers, the cloud chain
        otherwise — unless the user pinned the cloud, in which case nothing here changes.
        """
        if not force and time.monotonic() - getattr(self, "_mode_checked_at", 0.0) \
                < self.MODE_RECHECK_SECONDS:
            return
        self._mode_checked_at = time.monotonic()
        force_online, cloud_first = self._cloud_pinned()
        if force_online or cloud_first:
            local_up = False
        else:
            try:
                local_up = self._check_local_server()
            except Exception:
                local_up = False
        if local_up == (not self.is_online):
            return
        self.is_local_active = local_up
        self.is_online = not local_up
        if self.is_online and not any((self.claude_client, self.groq_client, self.gemini_client)):
            self._build_cloud_clients()
        self._refresh_status()
        info(Subsystem.LLM, "Local model server is up: using it" if local_up else
             "Local model server is not answering: using the cloud models")

    def routing_summary(self):
        """What answers right now, for the Settings screen: {"mode", "local_url", "answering"}."""
        if self.is_online:
            chain = self.router.describe(ROUTE_CHAT)
            return {"mode": "cloud", "local_url": self.local_base_url,
                    "answering": f"Cloud: {chain}"}
        return {"mode": "local", "local_url": self.local_base_url,
                "answering": f"LM Studio ({self.local_chat_model})"}

    def _refresh_status(self):
        if self.is_online:
            self.dmm_status = f"Decision routing: {self.router.describe(ROUTE_DECISION)}"
            self.chat_status = f"Chat routing: {self.router.describe(ROUTE_CHAT)}"
        else:
            self.dmm_status = f"Decision routing: Local ({self.local_decision_model})"
            self.chat_status = f"Chat routing: Local ({self.local_chat_model})"

    def reload_providers(self):
        """
        Applies changed cloud keys and model ids WITHOUT a restart. Returns the providers whose
        key changed.

        Before this, a key saved in Settings did nothing until Kayra restarted, and the
        router still held the old key's verdict — a wrong key stands a provider down for 15
        minutes — so a corrected key looked broken too. Now the changed provider's client is
        rebuilt and its cooldown forgotten, so the very next request tries it.

        The local/cloud MODE is not re-decided here: that changes what the whole session is
        talking to and stays a startup decision. A key saved while a local server is active is
        stored and used at the next start (or immediately with CLOUD_FIRST on).
        """
        from kayra.core.config import env_values as read_env, reset_cache
        reset_cache()
        before = dict(getattr(self, "_keys", {}))
        self.env_vars = read_env()
        self._load_models()
        changed = [name for name in PROVIDER_KEYS if self._keys.get(name) != before.get(name)]
        old_url = self.local_base_url
        self._load_local_settings()
        if self.local_base_url != old_url:
            self.local_client = OpenAI(base_url=self.local_base_url,
                                       api_key=self._setting("LOCAL_API_KEY", "lm-studio"),
                                       max_retries=0, timeout=self._provider_timeout())
        was_online = self.is_online
        # The routing toggles apply NOW too: turning "Prefer cloud" on or off, or fixing the
        # local endpoint, re-decides local vs cloud on the spot.
        self._sync_mode(force=True)
        if self.is_online and not was_online:
            changed = list(PROVIDER_KEYS)
        if self.is_online:
            # Every client is rebuilt (a model id or the timeout may have changed too); only
            # the providers whose KEY changed lose their cooldown.
            self._build_cloud_clients()
        if changed:
            self.router.clear_cooldowns(changed)
            info(Subsystem.LLM, "Provider keys updated: " + ", ".join(changed))
        self._refresh_status()
        return changed

    def _provider_timeout(self):
        """
        The per-request budget handed to every model client, in seconds.

        A BOUND, not a behaviour switch — like every automation knob in this codebase. It is
        clamped rather than trusted: a malformed `.env` must not be able to set an infinite
        timeout, which would reintroduce the hang the bound exists to prevent, nor a
        sub-second one, which would make every provider look broken.
        """
        try:
            value = float(str(self.env_vars.get("PROVIDER_TIMEOUT_SECONDS", "30")).strip())
        except (TypeError, ValueError):
            value = 30.0
        return min(300.0, max(5.0, value))

    def _check_local_server(self):
        """
        Pings the local model endpoint using a lightweight GET request.
        
        Verification Strategy:
            1. A raw TCP connect to the endpoint's host:port. A closed local port refuses
               the connection in about a millisecond, so the overwhelmingly common
               "no local server running" case costs essentially nothing.
            2. Only if the port is open, the HTTP ping to /models confirms it is really an
               OpenAI-compatible server. Both 200 (Success) and 401 (Unauthorized but
               alive) count as responsive.

        Why the TCP pre-check exists: this used to be a bare `requests.get(..., timeout=1.5)`,
        which measured at ~3.0s on a host with no local server — "localhost" resolves to both
        ::1 and 127.0.0.1, and the full timeout was paid once per address family. That single
        call was the largest contributor to cold-start latency in cloud mode.

        Returns:
            bool: True if the local endpoint is alive and responsive, False otherwise.
        """
        parsed = urlparse(self.local_base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Per-address budget. Windows Firewall silently DROPS connections to a closed
        # loopback port instead of refusing them, so a failed attempt always costs the full
        # timeout — and "localhost" resolves to two families, so the cost is paid twice.
        # A local server that is actually listening accepts a loopback TCP connection in
        # well under a millisecond, so this can be very tight without false negatives.
        probe_timeout = float(self.env_vars.get("LOCAL_PROBE_TIMEOUT_SECONDS", "0.15"))

        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return False

        # IPv4 first: local model servers bind 127.0.0.1 far more often than ::1.
        addresses.sort(key=lambda a: 0 if a[0] == socket.AF_INET else 1)

        port_open = False
        for family, socktype, proto, _canon, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(probe_timeout)
            try:
                sock.connect(sockaddr)
                port_open = True
            except OSError:
                continue
            finally:
                sock.close()
            if port_open:
                break

        if not port_open:
            return False

        try:
            response = requests.get(f"{self.local_base_url}/models", timeout=1.5)
            # Both 200 (Success) and 401 (Unauthorized/Auth required) show the server is alive
            return response.status_code in [200, 401]
        except (requests.ConnectionError, requests.Timeout):
            return False
    
    # ┌────────────────────────────────────────────────────────────────────────┐
    # │                    1. DECISION MAKING MODEL (DMM)                      │
    # └────────────────────────────────────────────────────────────────────────┘
    # ── LOCAL MODEL HEALTH ────────────────────────────────────────────────
    # Process-wide, because there is one engine per process and one local server behind it.
    # Plain attributes rather than a class: the state is two numbers and a timestamp, and a
    # structure would imply more machinery than exists.

    def _local_unhealthy(self):
        """True while the local model is standing down after repeated empty completions."""
        until = getattr(self, "_local_cooldown_until", 0.0)
        if until and time.monotonic() < until:
            return True
        if until:
            # Expired. The next request IS the probe; clear the latch so it is actually made.
            self._local_cooldown_until = 0.0
            self._local_empty_streak = 0
        return False

    def _note_local_result(self, produced_tokens):
        """
        Records whether a REQUEST (not an attempt) produced anything usable.

        Counted per request rather than per retry on purpose: five empty attempts inside one
        request are one piece of evidence about the model, not five.
        """
        if produced_tokens:
            self._local_empty_streak = 0
            self._local_cooldown_until = 0.0
            return
        streak = getattr(self, "_local_empty_streak", 0) + 1
        self._local_empty_streak = streak
        if streak >= DMM_UNHEALTHY_AFTER:
            self._local_cooldown_until = time.monotonic() + DMM_HEALTH_COOLDOWN_SECONDS
            warning(Subsystem.DMM,
                    f"Local model produced nothing on {streak} consecutive requests; "
                    f"standing it down for {DMM_HEALTH_COOLDOWN_SECONDS:.0f}s")

    def classify_intent(self, prompt: str, retries: int = 0, deadline: float = 0.0,
                        turn: int = 0):
        """
        Classifies user prompt inputs into structured system task tokens.
        Priority: a running local model; otherwise the DECISION route (Claude -> Groq -> Gemini).

        Parameters:
            prompt (str): Raw user query string.
            retries (int): Internal counter managing query planning retry recursion.
            deadline (float): Internal. `time.monotonic()` after which no further empty-response
                retry is started, whatever the counter says. Set on the first call.

        Returns:
            list: List of parsed task labels matching standard intents.
        """
        if not deadline:
            deadline = time.monotonic() + _retry_budget_seconds()
            # THE TURN THAT OWNS THIS REQUEST. Recorded once, on the first call, and carried
            # through every retry — see `_turn_superseded`. It is NOT printed by these lines:
            # `logbus` already stamps every correlated line with the open turn, and putting it
            # in the message too produced "[DMM] Turn #1 · Turn #1 retry 1/5".
            turn = turn or logbus.current_turn()

        # ── STALE WORK STOPS ──
        # OBSERVED: "[DMM] Retry 4/5", then a new utterance committed, then "Retry 5/5" — a
        # retry chain outliving the turn that started it, printing into another turn's log and
        # eventually returning a classification for a question the user had moved on from.
        if _turn_superseded(turn):
            debug(Subsystem.DMM,
                  f"cancelled: superseded by turn #{logbus.latest_turn()}")
            return []

        if not retries and _fast_path_enabled(self):
            token = fast_intent(prompt, (getattr(self, "env_vars", None) or {}).get("ASSISTANT_NAME", "Kayra"))
            if token:
                debug(Subsystem.DMM, f"fast path: {token}")
                return [token]
            if RULE_ROUTER is not None:
                try:
                    routed = RULE_ROUTER(prompt)
                except Exception as exc:          # a rule bug must never cost the turn
                    debug(Subsystem.DMM, f"rule route skipped: {exc}")
                    routed = None
                if routed:
                    debug(Subsystem.DMM, f"rule route: {routed}")
                    return list(routed)

        if not retries:
            # Local or cloud, re-checked here (throttled) — AFTER the fast path, so a command
            # that needs no model never pays for the probe.
            self._sync_mode()

        if not self.is_online and self._local_unhealthy():
            # The model has answered nothing for several requests running. Five more attempts
            # cannot change that, and the user is waiting.
            warning(Subsystem.DMM,
                    "Local model is standing down after repeated empty responses; "
                    "treating this as conversation.")
            return ["general " + prompt]

        try:
            if self.is_online:
                try:
                    response_text = self.router.run(
                        ROUTE_DECISION,
                        lambda provider: self._dmm_call(provider, prompt),
                        describe=lambda provider: self._provider_label(provider, ROUTE_DECISION),
                    )
                except AllProvidersFailed:
                    # Every decision provider is unconfigured or standing down. Degrading to
                    # conversation is the honest answer: the assistant can still talk, it
                    # simply cannot classify. The router has already said which provider
                    # failed and why, so this line adds the CONSEQUENCE and nothing else.
                    warning(Subsystem.DMM,
                            "No decision provider answered. Treating this as conversation.")
                    return ["general " + prompt]
            else:
                try:
                    # ONE attempt, with its OWN deadline. `deadline` bounds the chain; this
                    # bounds the call, so a backend that hangs hands the turn back in ~3s and
                    # the remaining retries still get to happen.
                    response_text = self._dmm_local(
                        prompt,
                        timeout=_attempt_timeout_seconds(deadline - time.monotonic()))
                except Exception as exc:
                    # NOT EVERY FAILURE IS WORTH FIVE ATTEMPTS. A malformed request, a bad
                    # credential or a model that is not loaded will fail identically four more
                    # times; the only thing repeating it buys is the user's time. The router's
                    # own vocabulary is reused so "is this worth retrying?" is answered once.
                    kind, _retry_after = classify_failure(exc)
                    if kind in TERMINAL_DMM_FAILURES:
                        warning(Subsystem.DMM,
                                f"local model: {kind} — not retryable. "
                                "Treating this as conversation.")
                        self._note_local_result(False)
                        return ["general " + prompt]
                    # Retryable, and the router does not own the local path (local-first is a
                    # chain of one), so the retry happens here under the same bounds as an
                    # empty completion.
                    debug(Subsystem.DMM, f"local model: {kind}")
                    response_text = ""

            # Clean and split response text into discrete tasks
            response_text = response_text.replace("\n", "")
            raw_tasks = [i.strip() for i in response_text.split(",") if i.strip()]

            # Filter generated task strings, keeping only those that match a known intent header.
            # IMPORTANT: match each task against the header set ONCE (not once per matching prefix) —
            # several headers are prefixes of one another (e.g. "close" / "close window" / "close tab",
            # "save" / "save file", "minimize" / "minimize all"), so a naive per-func append duplicates
            # the task once per overlapping header and double-executes it downstream (e.g. "minimize all"
            # would fire Win+D twice, re-opening every window it just minimized).
            # Tokens whose payload is free text taken from the user. If the model echoes the
            # placeholder from the preamble instead ("general query"), the payload is
            # meaningless — main.py hands `original_query` to the chatbot so conversation
            # still works by luck, but `deep research` slices the payload out of the token
            # and would research the word "topic". Repair it rather than executing it.
            TEXT_CARRYING = ("general", "realtime", "deep research")
            PLACEHOLDERS = ("query", "topic", "the query", "the topic", "text", "...",
                            "user query", "your query")

            parsed_task = []
            seen_tasks = set()
            for task in raw_tasks:
                task_lower = task.lower()
                if not any(task_lower.startswith(func) for func in self.funcs):
                    continue

                for header in TEXT_CARRYING:
                    if task_lower.startswith(header + " "):
                        payload = task[len(header):].strip().strip("()'\"")
                        if payload.lower().rstrip(".?!") in PLACEHOLDERS:
                            warning(Subsystem.DMM,
                                    f"Placeholder payload emitted ({task!r}). "
                                    "Substituting the words the user actually said.")
                            task = f"{header} {prompt}"
                            task_lower = task.lower()
                        break
                # A meeting code the user never said is the preamble's EXAMPLE code
                # ("abc-defg-hij"), emitted as a real one — it opened a stranger's meeting
                # on 2026-11-10 (BROWSER_CONTROL_PLAN §12, M0.2). A code must come from the
                # user's words; otherwise a share request is re-read from those words and
                # anything else is dropped.
                task = _guard_meeting_code(task, prompt)
                if task is None:
                    continue
                task_lower = task.lower()
                # Guard against the DMM itself emitting the exact same token twice
                dedup_key = task_lower
                if dedup_key in seen_tasks:
                    continue
                seen_tasks.add(dedup_key)
                parsed_task.append(task)

            # Intercept empty or failed token responses to attempt bounded retries.
            if len(parsed_task) == 0:
                budget_left = deadline - time.monotonic()
                if _turn_superseded(turn):
                    debug(Subsystem.DMM,
                          f"retry abandoned: superseded by turn "
                          f"#{logbus.latest_turn()}")
                    self._note_local_result(False)
                    return []
                if retries < MAX_DMM_EMPTY_RETRIES and budget_left > 0:
                    # A retry over the model's OUTPUT, not over a transport failure — the
                    # router owns the latter and this must never become a second retry
                    # authority for it. It re-enters the router, so a provider that has
                    # meanwhile been stood down is skipped rather than hammered.
                    #
                    # RAISED FROM 3 TO 5 for the local-model path. A local server is the
                    # opposite case from a cloud one: an empty completion there is a cheap,
                    # local, genuinely transient event (a sampler that produced only
                    # stop-tokens, a model still warming), and each attempt costs one
                    # in-process round-trip rather than a metered API call. Three attempts
                    # was leaving classifiable requests degraded to conversation.
                    #
                    # THIS IS NOT FIVE RETRIES AFTER EVERY FAILURE. It is reached only when
                    # the provider ANSWERED and the answer parsed to zero usable tokens. A
                    # transport failure never arrives here — the router has already
                    # classified it, stood the provider down and moved on — so a rate-limited
                    # cloud key still costs exactly one call per provider per request.
                    # ONE progress line per retry, at INFO, and NOT at WARNING.
                    # Five visually dominant warnings for a condition the assistant recovers
                    # from on its own is noise that trains a reader to skim; the WARNING is
                    # kept for the exhaustion, which is the part that has a consequence.
                    info(Subsystem.DMM,
                         f"empty response — retry {retries + 1}/"
                         f"{MAX_DMM_EMPTY_RETRIES}")
                    # Bounded, linear, and ZERO on the first retry — the common case is a
                    # retry that succeeds, and delaying it is pure added latency.
                    delay_ms = min(DMM_RETRY_DELAY_MS * retries, DMM_MAX_RETRY_DELAY_MS)
                    if delay_ms:
                        time.sleep(min(delay_ms, max(0.0, budget_left * 1000.0)) / 1000.0)
                    return self.classify_intent(prompt=prompt, retries=retries + 1,
                                                deadline=deadline, turn=turn)
                # Exhausted. Say so ONCE, at WARNING, with no traceback: an empty completion is
                # an expected outcome of a small model, not a defect to dump a stack for. The
                # line names WHICH limit was reached, because "the model kept answering
                # nothing" and "the model is too slow to ask five times" are different
                # problems with different fixes.
                if retries >= MAX_DMM_EMPTY_RETRIES:
                    warning(Subsystem.DMM,
                            f"exhausted {MAX_DMM_EMPTY_RETRIES} retries — "
                            "treating this as conversation")
                else:
                    warning(Subsystem.DMM,
                            f"spent its {_retry_budget_seconds():.0f}s retry budget "
                            f"after {retries + 1} attempt(s) — treating this as "
                            "conversation")
                self._note_local_result(False)
                return ["general " + prompt]
            self._note_local_result(True)
            return parsed_task

        except Exception as e:
            # Transport failures never reach here any more — the router classifies them,
            # stands the provider down and moves to the next one. What is left is a genuine
            # defect in the parsing below, so it keeps its traceback, at DEBUG.
            from kayra.core.logbus import exception as log_exception
            log_exception(Subsystem.DMM, "Intent parsing failed", e)
            return ["general " + prompt]

    # ── Per-provider DMM calls ────────────────────────────────────────────
    # THE PROMPT CONTRACT IS IDENTICAL ON EVERY PROVIDER. The same strict system rule, the same
    # `dmm_preamble` and the same `dmm_chat_history` in the same order reach every provider —
    # only the transport differs (Claude's own system field and message list versus the
    # OpenAI-compatible message list Groq and Gemini speak). A fallback that changed the
    # prompt would be classifying a different question, and the token contract the whole
    # automation layer depends on would silently vary with whichever provider answered.

    _DMM_SYSTEM_RULE = (
        "SYSTEM RULE: You are an intent classification engine. "
        "Your ONLY job is to output a comma-separated list of intent tokens. "
        "DO NOT answer the user's question. DO NOT explain. DO NOT add any prose. "
        "ONLY output tokens like: 'general query', 'realtime query', 'play song', "
        "'open app', 'exit', etc.\n\n"
    )

    def _dmm_system(self):
        return self._DMM_SYSTEM_RULE + self.dmm_preamble.strip()

    def _dmm_messages(self, prompt):
        """
        The DMM few-shot exchange as an OpenAI-compatible message list.

        NOTE: deliberately NOT sliced. The examples are ordered so the highest-value
        disambiguating pairs (open/close, window management, undo/redo) sit LAST for maximum
        recency weight — truncating this list has silently dropped exactly those before.
        """
        messages = [{"role": "system", "content": self._dmm_system()}]
        for msg in self.dmm_chat_history:
            role = "user" if msg["role"] == "User" else "assistant"
            messages.append({"role": role, "content": msg["message"]})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _dmm_claude_request(self, prompt):
        """
        The DMM as a Claude request: the same contract, with the ~5.6K-token few-shot prefix
        CACHED. The system prompt and the examples never change between turns, so after the
        first call they are read from the prompt cache (faster, and a tenth of the price); only
        the user's words are new. The breakpoint sits on the LAST example, so everything before
        the user's words is one cached prefix.
        """
        history = []
        for msg in self.dmm_chat_history:
            history.append({"role": "user" if msg["role"] == "User" else "assistant",
                            "content": [{"type": "text", "text": msg["message"]}]})
        if history:
            history[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        history.append({"role": "user", "content": prompt})
        return dict(
            model=self.claude_decision_model,
            max_tokens=128,
            system=[{"type": "text", "text": self._dmm_system(),
                     "cache_control": {"type": "ephemeral"}}],
            messages=history,
            **claude_speed_args(self.claude_decision_model, "low"),
        )

    def _provider_label(self, provider, route=ROUTE_CHAT):
        """`Claude (claude-haiku-5-5)` — the name plus the model actually configured."""
        return f"{provider} ({self._model_for(provider, route)})"

    def _model_for(self, provider, route=ROUTE_CHAT):
        if route == ROUTE_DECISION:
            return {
                CLAUDE: self.claude_decision_model,
                GROQ: self.groq_decision_model,
                GEMINI: self.gemini_decision_model,
                LOCAL: self.local_decision_model,
            }.get(provider, "unknown")
        return {
            CLAUDE: self.claude_model,
            GROQ: self.groq_model,
            GEMINI: self.gemini_model,
            LOCAL: self.local_chat_model,
        }.get(provider, "unknown")

    # A reasoning model (gpt-oss on Groq, Gemini Flash) thinks before it answers unless told
    # not to, and on a 128-token classification that hidden pass is the latency and can eat
    # the whole budget. Asked with the lowest effort each provider accepts; a provider or
    # model that rejects the field is asked once more without it, and that is remembered.
    _OPENAI_DMM_REASONING = {GROQ: "low", GEMINI: "none"}

    def _openai_dmm(self, provider, prompt):
        client = self.groq_client if provider == GROQ else self.gemini_client
        if client is None:
            raise RuntimeError(f"{provider} is not configured")
        request = dict(model=self._model_for(provider, ROUTE_DECISION),
                       messages=self._dmm_messages(prompt), max_tokens=256)
        effort = self._OPENAI_DMM_REASONING.get(provider)
        skip = getattr(self, "_no_reasoning_field", set())
        if effort and provider not in skip:
            try:
                completion = client.chat.completions.create(reasoning_effort=effort, **request)
                return completion.choices[0].message.content or ""
            except Exception as exc:
                if classify_failure(exc)[0] != FailureKind.INVALID_REQUEST:
                    raise
                self._no_reasoning_field = skip | {provider}
        completion = client.chat.completions.create(temperature=0.1, **request)
        return completion.choices[0].message.content or ""

    def _dmm_call(self, provider, prompt):
        """
        One DMM attempt against one provider. Raises on failure — the ROUTER decides what
        that means and whether anybody else gets a turn. Nothing here retries a transport
        error or sleeps; a second retry authority inside a provider call is how one user
        request turns into four calls against an already rate-limited key.
        """
        if provider == CLAUDE:
            if self.claude_client is None:
                raise RuntimeError("Claude is not configured")
            return claude_text(self.claude_client.messages.create(
                **self._dmm_claude_request(prompt)))
        return self._openai_dmm(provider, prompt)

    def _dmm_local(self, prompt, timeout=None):
        """
        The offline DMM. Not routed: local-first is absolute, so when a local server is up
        there is exactly one provider, and a chain of one is a chain with no decisions in it.

        `timeout` is this ONE attempt's deadline, supplied by the retry loop and already
        capped by what remains of the retry budget. `with_options` returns a shallow copy of
        the client rather than mutating the shared one, so a short-deadline DMM call cannot
        change the timeout of the chat stream running beside it.
        """
        client = self.local_client
        if timeout:
            try:
                client = client.with_options(timeout=timeout)
            except Exception:
                client = self.local_client
        # THINKING OFF. The DMM answers with a handful of tokens; a thinking model (Qwen 3.x,
        # Gemma 4) that reasons first can spend the whole 128-token budget on hidden reasoning
        # and return EMPTY content, which is the retry storm above. Measured on gemma-4-e4b the
        # few-shot prompt already suppresses thinking (3-9 output tokens, ~0.4 s warm), so this
        # costs nothing there and is what keeps a heavier thinking model on the same contract.
        # A server that rejects the field is asked once more without it, and remembered.
        request = dict(model=self.local_decision_model, messages=self._dmm_messages(prompt),
                       temperature=0.1, max_tokens=128)
        if getattr(self, "_local_no_think", True):
            try:
                response = client.chat.completions.create(
                    extra_body={"reasoning_effort": "none"}, **request)
                return response.choices[0].message.content
            except Exception as e:
                if "reasoning" not in str(e).lower() and type(e).__name__ != "BadRequestError":
                    raise
                self._local_no_think = False
        response = client.chat.completions.create(**request)
        return response.choices[0].message.content

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │              2. CHAT & SEARCH STREAMING CHUNKS GENERATOR               │
    # └────────────────────────────────────────────────────────────────────────┘
    def generate_chat_stream(self, api_messages):
        """
        Token-by-token generation channel for every conversational surface.

        ROUTING
            1. Local LLM (LM Studio / Ollama) — HIGHEST PRIORITY. When one is running, all
               generation goes there exclusively and zero cloud calls are made.
            2. Otherwise the CHAT route, Claude -> Groq -> Gemini, each only with its key.

        Fallback is the router's, not this function's. What used to live here — a private
        string-matching notion of "quota error", a hand-rolled provider sequence, and
        no memory of the failure from one request to the next — is exactly the duplication
        that made a rate-limited key get hit again on every following turn.

        Parameters:
            api_messages (list): Full system prompt, context layers and history, OpenAI format.

        Yields:
            str: The next text chunk from whichever provider answered.
        """
        self._sync_mode()
        if not self.is_online:
            # ── Offline: local model only. Not routed; see `_dmm_local`. ──
            try:
                stream = self.local_client.chat.completions.create(
                    model=self.local_chat_model,
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
                info(Subsystem.CHAT, f"Provider: Local ({self.local_chat_model})")
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
            except Exception as e:
                error(Subsystem.CHAT, f"Local engine failure: {type(e).__name__}")
                debug(Subsystem.CHAT, str(e))
                yield f"\n[Local Engine Failure: {e}]"
            return

        try:
            for chunk in self.router.run_stream(
                ROUTE_CHAT,
                lambda provider: self._chat_call(provider, api_messages),
                describe=self._provider_label,
            ):
                yield chunk
        except AllProvidersFailed as failure:
            # Every chat provider is unconfigured or standing down. One clear sentence, in the
            # stream, because that is the only channel the user is actually watching — and no
            # further recursion: an exhausted chain does not become answerable by asking again.
            yield "\n[" + self.chain_failure_sentence(failure) + "]"
        except Exception as e:
            # A mid-stream failure, after tokens have already reached the user. The router
            # re-raises rather than splicing a second provider's answer into a half-spoken
            # sentence, and it has already stood the provider down so the NEXT turn routes
            # elsewhere. All that is left is to end this one honestly.
            debug(Subsystem.CHAT, f"Stream ended early: {type(e).__name__}: {e}")
            yield "\n[The response was cut short. Please ask again.]"

    def chain_failure_sentence(self, failure):
        """
        One honest sentence for an exhausted chain, naming each provider and WHY.

        It used to say "every conversational model is rate-limited" whenever any attempt was a
        rate limit — including when the real problem was a retired model or a plan with no
        access to it, which sent the user off to check limits on a key that had plenty.
        """
        reasons = {
            FailureKind.RATE_LIMITED: "is rate-limited",
            FailureKind.AUTH_FAILURE: "rejected its API key",
            FailureKind.MODEL_UNAVAILABLE: "cannot serve the configured model on this key",
            FailureKind.NETWORK_FAILURE: "could not be reached",
            FailureKind.TIMEOUT: "timed out",
            FailureKind.SERVER_ERROR: "had a server error",
            FailureKind.INVALID_REQUEST: "rejected the request",
        }

        def reason(name, kind, exc):
            if is_no_credit(exc):
                return f"{name} has no API credit on its account"
            return f"{name} {reasons.get(kind, 'failed')}"

        if not failure.attempts:
            if not any(self.router.configured(name) for name in (CLAUDE, GROQ, GEMINI)):
                return "No cloud model is configured. Add a Claude, Groq or Gemini key in Settings."
            return "Every configured model is cooling down after an error. Please try again shortly."
        parts = [reason(name, kind, exc) for name, kind, exc in failure.attempts]
        missing = [n for n in (CLAUDE, GROQ, GEMINI) if not self.router.configured(n)]
        hint = (" Add a " + " or ".join(missing) + " key in Settings to have a fallback."
                if missing and len(parts) == 1 else "")
        return "No model could answer: " + "; ".join(parts) + "." + hint

    def _chat_call(self, provider, api_messages):
        """
        One chat attempt against one provider, as an iterator of text chunks.

        Returns a GENERATOR, so no network work begins until the router pulls the first
        chunk — which is what makes the router's "fallback only before the first token" rule
        meaningful rather than theoretical.
        """
        if provider == CLAUDE:
            if self.claude_client is None:
                raise RuntimeError("Claude is not configured")
            system, messages = to_claude_messages(api_messages)
            request = dict(model=self.claude_model, max_tokens=16000, messages=messages,
                           **claude_speed_args(self.claude_model, self.claude_effort))
            if system:
                request["system"] = system
            with self.claude_client.messages.stream(**request) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
            return

        client = self.groq_client if provider == GROQ else self.gemini_client
        if client is None:
            raise RuntimeError(f"{provider} is not configured")
        stream = client.chat.completions.create(
            model=self._model_for(provider),
            messages=api_messages,
            temperature=0.7,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 KEY CHECKS FOR THE SETTINGS SCREEN                     │
# └────────────────────────────────────────────────────────────────────────┘
# The Settings screen's "Test" button. ONE tiny request with the key and model on screen, so
# a wrong key, a model the key cannot use or an account with no credit is found out there,
# not in the middle of a conversation. Module-level and engine-free: it builds a throwaway
# client and touches neither the router nor its cooldowns.

PROVIDER_MODEL_SUGGESTIONS = {
    CLAUDE: ("claude-haiku-5-5", "claude-haiku-4-5", "claude-sonnet-5-5"),
    GROQ: ("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
    GEMINI: ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"),
}


def _probe_client(provider, key, timeout):
    if provider == CLAUDE:
        import anthropic
        return anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout)
    base = GROQ_BASE_URL if provider == GROQ else GEMINI_BASE_URL
    return OpenAI(api_key=key, base_url=base, max_retries=0, timeout=timeout)


def _probe_sentence(provider, model, exc):
    kind, _ = classify_failure(exc)
    text = str(exc).lower()
    if is_no_credit(exc):
        hint = (" A claude.ai subscription does not include API usage; add credit at "
                "console.anthropic.com → Plans & Billing." if provider == CLAUDE else "")
        return kind, f"The key is valid, but the {provider} account has no API credit.{hint}"
    if kind == FailureKind.AUTH_FAILURE:
        if "credit" in text or "billing" in text or "402" in text:
            return kind, f"The key is valid but the {provider} account has no credit."
        return kind, f"{provider} rejected this key."
    if kind == FailureKind.MODEL_UNAVAILABLE:
        if "credit" in text or "billing" in text:
            return kind, f"The key is valid but the {provider} account has no credit."
        return kind, f"The key works, but it cannot use the model '{model}'."
    if kind == FailureKind.RATE_LIMITED:
        return kind, f"The key works; {provider} is rate-limiting it right now."
    if kind == FailureKind.NETWORK_FAILURE:
        return kind, f"Could not reach {provider}. Check the internet connection."
    if kind == FailureKind.TIMEOUT:
        return kind, f"{provider} did not answer in time."
    if kind == FailureKind.SERVER_ERROR:
        return kind, f"{provider} had a server error. Try again in a moment."
    if kind == FailureKind.INVALID_REQUEST:
        return kind, f"{provider} refused the request for '{model}'."
    return kind, f"{provider} failed: {type(exc).__name__}."


def probe_provider(provider, key, model, timeout=15.0):
    """
    {"ok": bool, "kind": str | None, "message": str, "seconds": float, "models": [ids]}.

    Never raises. `models` is what the key can use, when the provider will list it, so the
    screen can offer real choices after a successful test.
    """
    key = _real_key(key)
    model = str(model or "").strip()
    if not key:
        return {"ok": False, "kind": FailureKind.AUTH_FAILURE, "seconds": 0.0, "models": [],
                "message": "No key entered or stored."}
    if not model:
        return {"ok": False, "kind": FailureKind.INVALID_REQUEST, "seconds": 0.0, "models": [],
                "message": "Choose a model first."}
    started = time.monotonic()
    try:
        client = _probe_client(provider, key, timeout)
        if provider == CLAUDE:
            client.messages.create(model=model, max_tokens=16,
                                   messages=[{"role": "user", "content": "Reply with: OK"}],
                                   **claude_speed_args(model, "low"))
        else:
            client.chat.completions.create(model=model, max_tokens=32,
                                           messages=[{"role": "user",
                                                      "content": "Reply with: OK"}])
    except ImportError:
        return {"ok": False, "kind": FailureKind.UNKNOWN, "seconds": 0.0, "models": [],
                "message": "The 'anthropic' package is missing. Run: python setup.py"}
    except Exception as exc:
        kind, sentence = _probe_sentence(provider, model, exc)
        return {"ok": False, "kind": kind, "message": sentence, "models": [],
                "seconds": round(time.monotonic() - started, 2)}
    seconds = round(time.monotonic() - started, 2)
    return {"ok": True, "kind": None, "seconds": seconds,
            "message": f"Works. {model} answered in {seconds:.1f} s.",
            "models": list_provider_models(provider, key, client=client)}


def list_provider_models(provider, key, client=None, timeout=10.0):
    """The chat-capable model ids this key can use, sorted; [] when the list is unavailable."""
    try:
        client = client or _probe_client(provider, _real_key(key), timeout)
        ids = [str(getattr(m, "id", "") or "") for m in client.models.list()]
    except Exception:
        return []
    skip = ("embed", "whisper", "tts", "guard", "audio", "image", "imagen", "veo", "aqa",
            "transcribe", "orpheus", "playai", "learnlm", "-live", "native-audio")
    ids = [i[len("models/"):] if i.startswith("models/") else i for i in ids]
    ids = sorted({i for i in ids if i and not any(s in i.lower() for s in skip)})
    if provider == CLAUDE:
        ids = [i for i in ids if i.startswith("claude-")]
    if provider == GEMINI:
        ids = [i for i in ids if i.startswith("gemini-")]
    return ids[:60]
