# ┌────────────────────────────────────────────────────────────────────────┐
# │                             llm_engine.py                              │
# │               Centralized LLM Engine & Intent Classifier               │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements the primary intelligence and intent routing orchestration for the KAYRA project.
It automatically handles local vs. cloud model selection, intent classification (DMM),
and real-time token streaming for low-latency dialogue generation.

Cloud Chat Priority: Groq (primary) -> Gemini (fallback on quota/rate-limit)
DMM: Cohere Command-R (cloud) or local model (offline)
"""

import time
import socket
import requests
import cohere
from urllib.parse import urlparse
from openai import OpenAI

# Robust relative path imports across standalone and package execution
from kayra.core.config import env_values
from kayra.core import logbus
from kayra.core.logbus import Subsystem, info, warning, error, debug, field
from kayra.intelligence.provider_router import (
    ROUTE_CHAT, ROUTE_DECISION, COHERE, GROQ, GEMINI, LOCAL,
    AllProvidersFailed, FailureKind, get_provider_router,
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


class CentralizedLLMEngine:
    """
    Centralized intelligence routing matrix.
    Checks host environments to dynamically swap between offline local endpoints (Ollama/LM Studio)
    and online cloud endpoints (Gemini/Cohere). Houses the Decision-Making Model (DMM) for intent parsing.

    Singleton: every module in the project does `engine = CentralizedLLMEngine()` at import
    time (main.py, chatbot.py, real_time_search.py, automation_windows.py, deep_research.py).
    Without a singleton, that meant up to 5 fully independent instances at every boot — each
    re-probing the local LLM server, re-reading .env, and re-constructing its own Cohere/Groq/
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
        
        # Load model identifier strings configured in profile
        self.cohere_model        = self.env_vars.get("COHERE_DECISION_MODEL", "command-r-plus-08-2024")
        self.groq_model          = self.env_vars.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
        self.gemini_model        = self.env_vars.get("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
        self.local_chat_model    = self.env_vars.get("LOCAL_CHAT_MODEL", "local-model")
        self.local_decision_model = self.env_vars.get("LOCAL_DECISION_MODEL", "local-model")

        # Read raw API keys
        self._groq_key   = self.env_vars.get("GROQ_API_KEY", "").strip()
        self._gemini_key = self.env_vars.get("GEMINI_API_KEY", "").strip()
        self._cohere_key = self.env_vars.get("CohereAPIKey", "").strip()

        force_online = self.env_vars.get("FORCE_ONLINE", "False").lower() == "true"

        # ┌────────────────────────────────────────────────────────┐
        # │                 MODE SELECTION MATRIX                  │
        # └────────────────────────────────────────────────────────┘
        if force_online:
            if not CentralizedLLMEngine._has_booted:
                print_system("FORCE_ONLINE is active. Bypassing local checks to run Cloud Mode.")
            self.is_online = True
            self.is_local_active = False
        else:
            self.local_base_url = self.env_vars.get("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
            self.is_local_active = self._check_local_server()
            self.is_online = not self.is_local_active

        # ┌────────────────────────────────────────────────────────┐
        # │                   CLIENT ALLOCATION                    │
        # └────────────────────────────────────────────────────────┘
        # Priority Rule: Local LLM server (LM Studio / Ollama) ALWAYS wins.
        # Cloud APIs are ONLY activated when no local server is detected.
        # When local is active, ALL operations (DMM + Chat) go through local exclusively.

        # ── Local client (always set if URL configured) ──
        local_key = self.env_vars.get("LOCAL_API_KEY", "lm-studio")
        if not self.is_online:
            self.local_base_url = self.env_vars.get("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
        self.local_client = OpenAI(
            base_url=self.local_base_url if hasattr(self, "local_base_url")
            else "http://127.0.0.1:1234/v1",
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
        # The OpenAI SDK retries transport failures twice by default, with its own backoff.
        # That is a SECOND retry authority underneath the router, and it produces exactly the
        # failure mode the router exists to prevent: one user request becomes three provider
        # calls, the fallback the router would have made instantly is delayed by the SDK's
        # backoff, and a rate-limited key gets hit two more times on the way. Measured live on
        # this machine before the change: a single Groq DMM call took 9.9s and then 24.0s,
        # while the router's own fallback for the same class of failure takes ~700ms.
        #
        # The router is the one authority. A provider call fails once, is classified once, and
        # the next provider gets its turn — see `ProviderRouter.run`.
        #
        # The timeout is the OTHER half of the same rule (1.3: "TIMEOUT: fallback after a
        # bounded timeout"). Without it a hung provider blocks the user indefinitely while two
        # perfectly good fallbacks sit idle.
        timeout = self._provider_timeout()
        if self.is_online:
            self.cohere_client = (
                cohere.Client(api_key=self._cohere_key, timeout=timeout)
                if self._cohere_key else None
            )
            self.groq_client = (
                OpenAI(api_key=self._groq_key, base_url="https://api.groq.com/openai/v1",
                       max_retries=0, timeout=timeout)
                if self._groq_key else None
            )
            self.gemini_client = (
                OpenAI(api_key=self._gemini_key,
                       base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                       max_retries=0, timeout=timeout)
                if self._gemini_key else None
            )
            
        else:
            # Local mode — cloud clients set to None so no accidental cloud calls occur
            self.cohere_client = None
            self.groq_client   = None
            self.gemini_client = None

        # ┌────────────────────────────────────────────────────────┐
        # │              THE PROVIDER ROUTING AUTHORITY            │
        # └────────────────────────────────────────────────────────┘
        # ONE router for the process, holding ONE set of cooldowns. Every provider decision
        # this engine makes goes through it, and neither `classify_intent` nor
        # `generate_chat_stream` contains fallback logic of its own any more — two
        # independent notions of "is this a rate limit?" is exactly how one user request
        # became four provider calls against an already-limited key.
        #
        # Availability is registered as a CALLABLE rather than a bool: the cloud clients
        # exist only in online mode, and a router holding a stale True would route into a
        # `None`. Local is registered too, so the same object can describe the offline
        # routing in the boot report.
        self.router = get_provider_router()
        self.router.register(COHERE, lambda: self.cohere_client is not None)
        self.router.register(GROQ, lambda: self.groq_client is not None)
        self.router.register(GEMINI, lambda: self.gemini_client is not None)
        self.router.register(LOCAL, lambda: not self.is_online)

        if self.is_online:
            self.dmm_status = f"Decision routing: {self.router.describe(ROUTE_DECISION)}"
            self.chat_status = f"Chat routing: {self.router.describe(ROUTE_CHAT)}"
        else:
            self.dmm_status = f"Decision routing: Local ({self.local_decision_model})"
            self.chat_status = f"Chat routing: Local ({self.local_chat_model})"

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
        # IMPORTANT ORDER: Most recent examples (bottom of list) have HIGHEST weight in Cohere,
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
            {"role": "User", "message": "bye jarvis."},
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
            # -- Open & Close (placed LAST for maximum recency weight in Cohere) --
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
            # -- CONTRASTIVE BOUNDARIES (LAST = highest recency weight in Cohere) --
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
        # The hierarchy, stated as a hierarchy. `Cohere > Groq > Gemini` on one line and
        # `Groq > Gemini` on the next is the whole routing policy, visible at a glance at every
        # boot — which is what makes a later `[DMM] Fallback: Groq` legible instead of
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
        username = self.env_vars.get("USERNAME", "User").strip()
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
        Priority: Cohere (cloud DMM) -> Local.

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
                        describe=self._provider_label,
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
    # THE PROMPT CONTRACT IS IDENTICAL ON ALL THREE. The same strict system rule, the same
    # `dmm_preamble` and the same `dmm_chat_history` in the same order reach every provider —
    # only the transport differs (Cohere's native preamble/chat_history parameters versus the
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

    def _provider_label(self, provider):
        """`Cohere (command-r-plus-08-2024)` — the name plus the model actually configured."""
        return f"{provider} ({self._model_for(provider)})"

    def _model_for(self, provider):
        return {
            COHERE: self.cohere_model,
            GROQ: self.groq_model,
            GEMINI: self.gemini_model,
            LOCAL: self.local_chat_model,
        }.get(provider, "unknown")

    def _dmm_call(self, provider, prompt):
        """
        One DMM attempt against one provider. Raises on failure — the ROUTER decides what
        that means and whether anybody else gets a turn. Nothing here retries, catches a
        transport error or sleeps; a second retry authority inside a provider call is how one
        user request turns into four calls against an already rate-limited key.
        """
        if provider == COHERE:
            response_text = ""
            stream = self.cohere_client.chat_stream(
                model=self.cohere_model,
                preamble=self._dmm_system(),
                message=prompt,
                chat_history=self.dmm_chat_history,
                prompt_truncation="OFF",
                temperature=0.1,
            )
            for event in stream:
                if event.event_type == "text-generation":
                    response_text += event.text
            return response_text

        client = self.groq_client if provider == GROQ else self.gemini_client
        if client is None:
            raise RuntimeError(f"{provider} is not configured")
        completion = client.chat.completions.create(
            model=self._model_for(provider),
            messages=self._dmm_messages(prompt),
            temperature=0.1,
            max_tokens=128,
        )
        return completion.choices[0].message.content or ""

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
        response = client.chat.completions.create(
            model=self.local_decision_model,
            messages=self._dmm_messages(prompt),
            temperature=0.1,
            max_tokens=128,
        )
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
            2. Otherwise the CHAT route, which is Groq -> Gemini, in that order and no other.
               Cohere is NOT a chat provider: it leads the DECISION route and appears nowhere
               here. The two hierarchies are separate on purpose and the router holds both.

        Fallback is the router's, not this function's. What used to live here — a private
        string-matching notion of "quota error", a hand-rolled Groq-then-Gemini sequence, and
        no memory of the failure from one request to the next — is exactly the duplication
        that made a rate-limited key get hit again on every following turn.

        Parameters:
            api_messages (list): Full system prompt, context layers and history, OpenAI format.

        Yields:
            str: The next text chunk from whichever provider answered.
        """
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
            if FailureKind.RATE_LIMITED in failure.kinds:
                yield ("\n[Every conversational model is rate-limited right now. "
                       "Please try again shortly.]")
            else:
                yield "\n[No conversational model is available right now.]"
        except Exception as e:
            # A mid-stream failure, after tokens have already reached the user. The router
            # re-raises rather than splicing a second provider's answer into a half-spoken
            # sentence, and it has already stood the provider down so the NEXT turn routes
            # elsewhere. All that is left is to end this one honestly.
            debug(Subsystem.CHAT, f"Stream ended early: {type(e).__name__}: {e}")
            yield "\n[The response was cut short. Please ask again.]"

    def _chat_call(self, provider, api_messages):
        """
        One chat attempt against one provider, as an iterator of text chunks.

        Returns a GENERATOR, so no network work begins until the router pulls the first
        chunk — which is what makes the router's "fallback only before the first token" rule
        meaningful rather than theoretical.
        """
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
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
