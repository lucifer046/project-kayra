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
import requests
import cohere
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
    """
    _has_booted = False

    def __init__(self):
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

    def run_boot_sequence(self, tts_engine=None):
        """Prints and speaks the boot sequence line by line."""
        import time
        from modules.utils import print_system

        # 1. Announce DMM
        print_system(self.dmm_status)
        if tts_engine:
            tts_engine.speak(self.dmm_status)
        time.sleep(0.1)

        # 2. Announce Chat Model
        print_system(self.chat_status)
        if tts_engine:
            tts_engine.speak(self.chat_status)
        time.sleep(0.1)

        # Valid intents/commands acceptable by the system parser
        self.funcs = [
            "exit", "general", "realtime", "open", "close", "play",
            "generate image", "system", "content", "google search", 
            "youtube search", "reminder", "deep research",
            "screenshot", "take screenshot", "copy", "paste", "copy text",
            "snap left", "snap right", "minimize all", "show desktop",
            "switch window", "alt tab", "task view", "maximize", "minimize",
            "close window", "action center", "notification", "emoji",
            "pause", "resume", "next track", "previous track", "stop media",
            "battery", "cpu", "ram", "disk", "uptime", "ip address",
            "timer", "set timer",
            "undo", "redo", "select all", "save file", "save", "find", "search",
            "new tab", "close tab", "refresh", "reload", "fullscreen", "print",
            "zoom in", "zoom out", "reset zoom", "task manager", "run dialog",
            "wifi", "write", "type"
        ]
        
        # Preamble instructions to restrict DMM responses to structured task labels
        self.dmm_preamble = """
            You are a very accurate Decision-Making Model, which decides what kind of a query is given to you.
            You will decide whether a query is a 'general' query, a 'realtime' query, or is asking to perform any task or automation like 'open facebook, instagram', 'can you write a application and open it in notepad'.
            
            *** DO NOT ANSWER THE QUERY DIRECTLY. JUST DECIDE WHAT KIND OF QUERY IT IS AND FORMAT IT EXACTLY AS INSTRUCTED BELOW. ***
            
            =========================================
            INTENT CLASSIFICATION & FORMATTING RULES
            =========================================
            
            [1. KNOWLEDGE & CONVERSATION]
            -> Respond with 'deep research (topic)' if the query explicitly requests to deeply analyze, research, do a deep dive, or write an exhaustive technical report on a specific topic. Example: if the query is 'do deep research on carbon batteries' respond with 'deep research carbon batteries'.
            
            -> Respond with 'general (query)' if a query can be answered by an LLM model (conversational AI chatbot) and doesn't require any up-to-date information like if the query is 'who was akbar?' respond with 'general who was akbar?'.
               - Also use 'general (query)' if the query lacks a proper noun or is incomplete/ambiguous (e.g., uses pronouns like he, she, it, him, her). Examples: 'who is he?', "what's his networth?", 'tell me more about him.'.
               - Also use 'general (query)' if the query is asking about time, day, date, month, year, etc.
               
            -> Respond with 'realtime (query)' if a query cannot be answered by an LLM model (because they don't have realtime data) and requires up-to-date information like if the query is 'who is indian prime minister' respond with 'realtime who is indian prime minister', if the query is 'what is today's news?' respond with 'realtime what is today's news?'.
               - Also use 'realtime (query)' if it asks about any specific individual or thing using proper nouns. Examples: 'who is akshay kumar', "tell me about facebook's recent update.", 'tell me news about coronavirus.', "what is today's headline?".

            [2. SYSTEM & MEDIA AUTOMATION]
            -> Respond with 'open (application name or website name)' if a query is asking to open any application like 'open facebook'. If asking to open multiple, respond with 'open 1st application name, open 2nd application name' and so on.
            
            -> Respond with 'close (application name)' if a query is asking to close any application like 'close notepad'. If asking to close multiple, respond with 'close 1st application name, close 2nd application name' and so on.
            
            -> Respond with 'play (song or genre)' if a query is asking to play any song or listen to music, like 'play afsanay by ys' or 'i want to listen to rock music'. If asking to play multiple songs, respond with 'play 1st song name, play 2nd song name' and so on. If the user just says 'play music', respond with 'play music'.
            
            -> Respond with 'generate image (image prompt)' if a query is requesting to generate an image with a given prompt like 'generate image of a lion'. If asking to generate multiple images, respond with 'generate image 1st image prompt, generate image 2nd image prompt' and so on.
            
            -> Respond with 'reminder (datetime with message)' if a query is requesting to set a reminder like 'set a reminder at 9:00pm on 25th june for my business meeting.' respond with 'reminder 9:00pm 25th june business meeting'.
            
            -> Respond with 'system (task name)' if a query is asking to mute, unmute, volume up, volume down, increase/decrease brightness, lock the system, shutdown, restart, or sleep. Use the exact user phrasing for the task. Examples:
               - 'mute the sound' -> 'system mute'
               - 'increase volume by 20%' -> 'system increase volume by 20%'
               - 'set brightness to 50%' -> 'system brightness 50%'
               - 'lock my pc' -> 'system lock'
               - 'shutdown the computer' -> 'system shutdown'
            
            [3. CONTENT GENERATION & SEARCH]
            -> Respond with 'content (topic)' if a query is asking to write any type of content like application, codes, emails or anything else. If asking to write multiple types of content, respond with 'content 1st topic, content 2nd topic' and so on.
            
            -> Respond with 'google search (topic)' if a query is asking to search a specific topic on Google. If asking to search multiple topics, respond with 'google search 1st topic, google search 2nd topic' and so on.
            
            -> Respond with 'youtube search (topic)' if a query is asking to search a specific topic on YouTube. If asking to search multiple topics, respond with 'youtube search 1st topic, youtube search 2nd topic' and so on.
            
            [4. DESKTOP CONTROLS & UTILITIES]
            -> Respond with 'take screenshot' if the user asks to capture the screen, take a screenshot, or snap the screen.
            
            -> Respond with 'copy' if the user asks to copy something. Respond with 'paste' if the user asks to paste. Respond with 'copy text (message)' if the user wants to copy specific text to clipboard.
            
            -> Respond with 'write (text)' or 'type (text)' if the user asks to type or write text at the current cursor position. Example: 'type hello world' -> 'write hello world'.
            
            -> For window management, respond EXACTLY with the matching command:
               - 'minimize all windows' or 'show desktop' -> 'minimize all'
               - 'snap this to the left' -> 'snap left'
               - 'snap to right' -> 'snap right'
               - 'switch window' or 'alt tab' -> 'switch window'
               - 'maximize this window' -> 'maximize'
               - 'minimize this window' -> 'minimize'
               - 'close this window' -> 'close window'
               - 'open task view' -> 'task view'
               - 'open action center' or 'open notifications' -> 'notification'
               - 'open emoji picker' -> 'emoji'
            
            -> For media playback, respond EXACTLY with:
               - 'pause the music' or 'play music' -> 'pause'
               - 'resume the music' -> 'resume'
               - 'next song' or 'skip track' -> 'next track'
               - 'previous song' or 'go back' -> 'previous track'
               - 'stop the music' -> 'stop media'
            
            -> For system info queries, respond EXACTLY with:
               - 'check battery' or 'battery status' -> 'battery'
               - 'how much ram' or 'memory usage' -> 'ram'
               - 'cpu info' or 'processor info' -> 'cpu'
               - 'disk space' or 'storage info' -> 'disk'
               - 'system uptime' -> 'uptime'
               - 'what is my ip' or 'network info' -> 'ip address'
            
            -> Respond with 'set timer (duration)' if the user asks to set a timer or countdown. Example: 'set a timer for 5 minutes' -> 'set timer 5 minutes'.
            
            -> For keyboard shortcuts, respond EXACTLY with:
               - 'undo that' -> 'undo'
               - 'redo that' -> 'redo'
               - 'select all' -> 'select all'
               - 'save the file' -> 'save file'
               - 'find something' -> 'find'
               - 'search the page' -> 'search'
               - 'open a new tab' -> 'new tab'
               - 'close this tab' -> 'close tab'
               - 'refresh the page' -> 'refresh'
               - 'reload the page' -> 'reload'
               - 'print this' -> 'print'
               - 'go fullscreen' -> 'fullscreen'
               - 'zoom in' -> 'zoom in'
               - 'zoom out' -> 'zoom out'
               - 'reset zoom' -> 'reset zoom'
               - 'open task manager' -> 'task manager'
               - 'open run dialog' -> 'run dialog'
            
            -> For wifi control, respond with:
               - 'turn off wifi' or 'disable wifi' -> 'wifi off'
               - 'turn on wifi' or 'enable wifi' -> 'wifi on'
            
            =========================================
            MULTI-TASKING & FALLBACK PROTOCOLS
            =========================================
            *** MULTI-TASKING: If the query is asking to perform multiple tasks like 'open facebook and close whatsapp' respond with 'open facebook, close whatsapp'
            *** CONVERSATION END: If the user is saying goodbye or wants to end the conversation like 'bye jarvis.' respond with 'exit'.
            *** FALLBACK: Respond with 'general (query)' if you can't decide the kind of query or if a query is asking to perform a task which is not mentioned above.
            """
        
        # Few-shot conversational history to teach DMM target output alignment
        # IMPORTANT ORDER: Most recent examples (bottom of list) have HIGHEST weight in Cohere.
        # Exit/bye examples are intentionally placed early so open/close examples dominate recency.
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
            {"role": "User", "message": "close youtube"},
            {"role": "Chatbot", "message": "close youtube"},
            {"role": "User", "message": "Open youtube."},
            {"role": "Chatbot", "message": "open youtube"},
            {"role": "User", "message": "open youtube"},
            {"role": "Chatbot", "message": "open youtube"},
        ]

    def get_identity_prompt(self):
        """
        Compiles the system persona instructions for the assistant based on env configuration.
        Constructs the identity prompt dynamically based on the configured name, gender,
        target language, and username.

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
        
        prompt = (
            f"Hello, my username is {username}. You are a highly intelligent, empathetic, and witty AI companion named {name}. "
            f"Your gender profile is {gender}. You must always respond and converse fluently in {lang}. "
            f"You have access to real-time, up-to-date information from the internet.\n\n"
            f"CRITICAL BEHAVIORAL DIRECTIVES:\n"
            f"1. Be highly conversational, relatable, and human-like. Speak to me like a close, trusted friend.\n"
            f"2. Show personality! Feel free to be witty, charming, and expressive instead of robotic and formal.\n"
            f"3. Keep your answers reasonably concise but do not sacrifice conversational flow.\n"
            f"4. Do not tell the time unless explicitly requested.\n"
            f"5. Never provide conversational 'notes' or disclaimers in the output. Just talk naturally.\n"
            f"6. Under no circumstances should you ever mention your training data, AI architecture, or model limitations.\n"
            f"7. Always address me as '{user_title}' instead of my username during the conversation."
        )
        return prompt

    def _check_local_server(self):
        """
        Pings the local model endpoint using a lightweight GET request.
        
        Verification Strategy:
            Issues a quick ping to f"{local_base_url}/models" with a tight timeout of 1.5 seconds.
            If the response status code is 200 (Success) or 401 (Unauthorized but alive),
            we confidently conclude the local offline server (LM Studio/Ollama) is responsive.

        Returns:
            bool: True if the local endpoint is alive and responsive, False otherwise.
        """
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
                for msg in self.dmm_chat_history[:40]:
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
            raw_tasks = [i.strip() for i in response_text.split(",")]

            # Filter generated task strings, keeping only those that match known intent headers
            parsed_task = []
            for task in raw_tasks:
                for func in self.funcs:
                    if task.lower().startswith(func):
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
            print_error("Cohere Rate Limit Reached. Cooling down for 10 seconds...")
            time.sleep(10)
            return self.classify_intent(prompt=prompt, retries=retries)

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