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

import os
import time
import socket
import requests
import cohere
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import dotenv_values

# Robust relative path imports across standalone and package execution
try:
    from .utils import print_info, print_warning, print_error, print_system, print_success
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_system, print_success
    except ImportError:
        from utils import print_info, print_warning, print_error, print_system, print_success


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

        # Resolve absolute pathways to locate .env profile parameters dynamically
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.env_vars = dotenv_values(os.path.join(project_root, ".env")) or {}
        
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
        self.local_client = OpenAI(base_url=self.local_base_url if hasattr(self, "local_base_url") else "http://127.0.0.1:1234/v1", api_key=local_key)

        # ── Cloud clients (only activated when local server is NOT running) ──
        if self.is_online:
            self.cohere_client = cohere.Client(api_key=self._cohere_key) if self._cohere_key else None
            self.groq_client = (
                OpenAI(api_key=self._groq_key, base_url="https://api.groq.com/openai/v1")
                if self._groq_key else None
            )
            self.gemini_client = (
                OpenAI(api_key=self._gemini_key,
                       base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
                if self._gemini_key else None
            )
            
            self.dmm_status = f"Decision making initialised with Cohere ({self.cohere_model})" if self.cohere_client else "Decision making initialised with None (Offline)"
            
            chat_models = []
            if self.groq_client: chat_models.append("Groq")
            if self.gemini_client: chat_models.append("Gemini")
            
            self.chat_status = f"Chat model initialised with {' & '.join(chat_models)}" if chat_models else "Chat model initialised with None (Offline)"
            
        else:
            # Local mode — cloud clients set to None so no accidental cloud calls occur
            self.cohere_client = None
            self.groq_client   = None
            self.gemini_client = None
            
            self.dmm_status = f"Decision making initialised with Local LLM ({self.local_decision_model})"
            self.chat_status = f"Chat model initialised with Local LLM ({self.local_chat_model})"

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
            # applications, windows, tabs
            "open", "close", "close window", "close tab", "new tab",
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
            E. MULTI-INTENT, EXIT AND FALLBACK
            =========================================================
            *** MULTI-TASKING: one token per requested action, in the order requested.
                'open facebook and close whatsapp' -> 'open facebook, close whatsapp'
                'who is akshay kumar and what is his net worth' -> 'realtime who is akshay kumar and what is his net worth'
                Only split when the actions are genuinely different; a single question about
                one subject stays a single token.
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
            {"role": "User", "message": "generate image of a lion and generate image of a cat"},
            {"role": "Chatbot", "message": "generate image of a lion, generate image of a cat"},
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
        ]

    def run_boot_sequence(self, tts_engine=None):
        """
        Prints and speaks the model-routing status lines. Purely cosmetic narration —
        `self.funcs`/`self.dmm_preamble`/`self.dmm_chat_history` are already set in __init__,
        so classify_intent()/generate_chat_stream() work correctly whether or not this is ever
        called (callers that don't care about the boot narration, e.g. test scripts, can skip it).
        """
        print_system(self.dmm_status)
        print_system(self.chat_status)

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
            mood (str): Optional detected user mood (e.g. "Angry", "Happy", "Sad") from the
                        SemanticEmotionEngine, used to steer tone/empathy for this turn.

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

        if mood and mood.strip().lower() not in ("", "neutral"):
            prompt += (
                f"\n\nEMOTIONAL CONTEXT: {username} currently sounds {mood} based on their phrasing. "
                f"Adjust your tone with genuine empathy to match — reassure if they seem upset or anxious, "
                f"match their energy if they seem happy or excited — without explicitly announcing that you detected their mood."
            )

        return prompt

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
    def classify_intent(self, prompt: str, retries: int = 0):
        """
        Classifies user prompt inputs into structured system task tokens.
        Priority: Cohere (cloud DMM) -> Local.

        Parameters:
            prompt (str): Raw user query string.
            retries (int): Internal counter managing query planning retry recursion.

        Returns:
            list: List of parsed task labels matching standard intents.
        """
        response_text = ""
        try:
            if self.is_online:
                if self.cohere_client:
                    # ── Primary: Cohere Command-R streaming DMM ──
                    strict_system = (
                        "SYSTEM RULE: You are an intent classification engine. "
                        "Your ONLY job is to output a comma-separated list of intent tokens. "
                        "DO NOT answer the user's question. DO NOT explain. DO NOT add any prose. "
                        "ONLY output tokens like: 'general query', 'realtime query', 'play song', 'open app', 'exit', etc.\n\n"
                        + self.dmm_preamble.strip()
                    )
                    stream = self.cohere_client.chat_stream(
                        model=self.cohere_model,
                        preamble=strict_system,
                        message=prompt,
                        chat_history=self.dmm_chat_history,
                        prompt_truncation='OFF',
                        temperature=0.1
                    )
                    for event in stream:
                        if event.event_type == "text-generation":
                            response_text += event.text
                else:
                    print_warning("Cohere API key missing. DMM requires Cohere for online intent routing.")
                    return ["general " + prompt]
            else:
                # ── Offline Mode ──
                strict_system = (
                    "SYSTEM RULE: You are an intent classification engine. "
                    "Your ONLY job is to output a comma-separated list of intent tokens. "
                    "DO NOT answer the user's question. DO NOT explain. DO NOT add any prose. "
                    "ONLY output tokens like: 'general query', 'realtime query', 'play song', 'open app', 'exit', etc.\n\n"
                    + self.dmm_preamble.strip()
                )
                local_messages = [{"role": "system", "content": strict_system}]
                # NOTE: Deliberately NOT sliced. The few-shot examples above are ordered so the
                # highest-value disambiguating pairs (open/close, window management) sit LAST for
                # maximum recency weight — truncating this list would silently drop exactly those.
                for msg in self.dmm_chat_history:
                    role = "user" if msg["role"] == "User" else "assistant"
                    local_messages.append({"role": role, "content": msg['message']})
                local_messages.append({
                    "role": "user",
                    "content": prompt
                })

                local_response = self.local_client.chat.completions.create(
                    model=self.local_decision_model,
                    messages=local_messages,
                    temperature=0.1,
                    max_tokens=128,
                )
                response_text = local_response.choices[0].message.content

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
                            print_warning(
                                f"DMM emitted a placeholder payload ('{task}'). "
                                "Substituting the user's actual words."
                            )
                            task = f"{header} {prompt}"
                            task_lower = task.lower()
                        break
                # Guard against the DMM itself emitting the exact same token twice
                dedup_key = task_lower
                if dedup_key in seen_tasks:
                    continue
                seen_tasks.add(dedup_key)
                parsed_task.append(task)

            # Intercept empty or failed token responses to attempt recursive retries
            if len(parsed_task) == 0:
                if retries < 3:
                    print_warning(f"Empty token response. Retrying DMM step #{retries + 1}...")
                    return self.classify_intent(prompt=prompt, retries=retries + 1)
                else:
                    return ["general " + prompt]
            return parsed_task

        except cohere.TooManyRequestsError:
            # Bounded backoff. This used to recurse with the SAME `retries` value, so a
            # sustained rate limit (a trial key under load does this readily) meant unbounded
            # recursion: the assistant would sit in a 10-second-per-frame loop until the
            # stack blew, with no way out and no message to the user.
            if retries >= 3:
                print_error("Cohere rate limit persisted. Routing this query to conversation.")
                return ["general " + prompt]
            cooldown = 5 * (retries + 1)
            print_warning(f"Cohere rate limit reached. Cooling down {cooldown}s "
                          f"[attempt {retries + 1}/3]...")
            time.sleep(cooldown)
            return self.classify_intent(prompt=prompt, retries=retries + 1)

        except Exception as e:
            print_error(f"DMM exception: {e}")
            return ["general " + prompt]

    # ┌────────────────────────────────────────────────────────────────────────┐
    # │              2. CHAT & SEARCH STREAMING CHUNKS GENERATOR               │
    # └────────────────────────────────────────────────────────────────────────┘
    def generate_chat_stream(self, api_messages):
        """
        Token-by-token generation channel powering direct low-latency feedback logs on CLI.

        Execution Priority:
            1. Local LLM (LM Studio / Ollama) — HIGHEST PRIORITY. If running, all generation
               routes here exclusively. Zero cloud calls are made.
            2. Groq — Primary cloud provider when no local server is detected.
            3. Gemini — Auto-fallback if Groq quota/rate-limit is exceeded.

        Parameters:
            api_messages (list): Full system prompt, context layers, and history blocks in OpenAI format.

        Yields:
            str: Next text token string chunk generated by the active model engine.
        """
        if not self.is_online:
            # ── Offline Mode: Local model only ──
            try:
                stream = self.local_client.chat.completions.create(
                    model=self.local_chat_model,
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
                print_info(f"Generating via Local Model: {self.local_chat_model}")
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
            except Exception as e:
                yield f"\n[Local Engine Failure: {e}]"
            return

        # ── Online Mode: Try Groq first, fall back to Gemini ──
        QUOTA_SIGNALS = (
            "rate_limit", "quota", "429", "resource_exhausted",
            "too many requests", "ratelimitexceeded"
        )

        def _is_quota_error(exc: Exception) -> bool:
            return any(s in str(exc).lower() for s in QUOTA_SIGNALS)

        # --- Attempt 1: Groq ---
        if self.groq_client:
            try:
                stream = self.groq_client.chat.completions.create(
                    model=self.groq_model,
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
                print_info(f"Generating via Groq: {self.groq_model}")
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                return  # Groq succeeded, done
            except Exception as e:
                if _is_quota_error(e):
                    print_warning(f"Groq quota reached. Switching to Gemini fallback...")
                else:
                    print_error(f"Groq stream error: {e}. Trying Gemini fallback...")

        # --- Attempt 2: Gemini fallback ---
        if self.gemini_client:
            try:
                stream = self.gemini_client.chat.completions.create(
                    model=self.gemini_model,
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
                print_info(f"Generating via Gemini: {self.gemini_model}")
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                return  # Gemini succeeded, done
            except Exception as e:
                if _is_quota_error(e):
                    yield "\n[All cloud quotas exhausted. Please wait a moment before trying again.]"
                else:
                    yield f"\n[Gemini Engine Failure: {e}]"
                return

        yield "\n[No available cloud chat provider. Check your API keys in .env]"