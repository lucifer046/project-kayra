# KAYRA Engine Blueprint

## Project Structure

```text
project-kayra/
├── .env                  # Active environment variables (API keys, models, system config)
├── .env.example          # Template for environment configuration
├── main.py               # Application entrypoint
├── requirements.txt      # Python dependencies
├── data/                 # Dynamic data generation and caching directory
│   └── Files/            # Sub-directory for temp STT/TTS status tracking
├── Reports/              # Generated markdown whitepapers
├── logs/                 # Standardized system activity logs
├── models/               # Offline models directory
│   ├── kokoro.onnx       # Kokoro TTS ONNX Model (82M)
│   └── voices.bin        # Voice profiles binary pack
├── tests/                # Testing sandbox
│   ├── test_engine.py    # LLM Intent & Streaming integration tests
│   └── test_voice.py     # Interactive Kokoro TTS voice sandbox
└── modules/              # Core Intelligence Systems
    ├── __init__.py       # Package definition and namespace exports
    ├── utils.py          # Cyberpunk terminal UI, pathing, and logging
    ├── chatbot.py        # Conversational memory engine & persistent context layers
    ├── llm_engine.py     # Centralized LLM Engine (DMM & Chat Streaming)
    ├── real_time_search.py # DuckDuckGo unauthenticated search engine & RAG
    ├── deep_research.py  # Autonomous multi-step topic decomposition and whitepaper writer
    ├── speech_to_text.py # Continuous Web Speech API via headless Chrome
    ├── text_to_speech.py # Offline Kokoro-ONNX Real-Time Audio Streaming
    ├── air_cursor_engine.py # Spatial hand tracking and hardware click injection
    └── automation_windows.py # High-utility hardware input injection & OS/browser automation
```

## Core Modules Logic & Functions

### Data Flow Architecture

```text
[User Input String (Hinglish/English)]
                 │
                 ▼
     ┌───────────────────────┐
     │   llm_engine.py       │ ◄─── Checks if Local Instance is Alive
     └───────────┬───────────┘
                 │
                 ▼
     ┌───────────────────────┐
     │ Classify Intent (DMM) │ ───► Outputs Clean Parsed Command Arrays
     └───────────┬───────────┘
                 │
       ┌─────────┼──────────────┐
       ▼         ▼              ▼
  ["general"] ["realtime"] ["deep research"]
       │         │              │
       │         │              ▼
       │         │       ┌──────────────────────┐
       │         │       │  DeepResearch.py     │
       │         │       ├──────────────────────┤
       │         │       │ 1. Generate Queries  │
       │         │       │ 2. Scrape Chunks     │
       │         │       │ 3. Compile Whitepaper│
       │         │       └──────────┬───────────┘
       │         │                  │ Save Markdown File
       │         ▼                  ▼
       │   ┌──────────────┐     [Reports/...]
       │   │   ddgs       │
       │   └──────┬───────┘
       │          │ Scrapes Web Snippets
       ▼          ▼
 ┌───────────────────────────┐
 │ Inject Context & Prompt   │ ◄─── Pulls Dynamic Human Identity Map
 └────────────┬──────────────┘
              │
              ▼
 ┌───────────────────────────┐
 │ generate_chat_stream()    │
 └────────────┬──────────────┘
              │
              ▼
   [Live Token Terminal Output (Pure English)]
```


### `modules/utils.py`
**Purpose:** Serves as the central utility backbone. Provides robust absolute path resolution, file-based logging, and the custom, emoji-free premium cyberpunk terminal UI using the `rich` library. It also configures Windows `stdout`/`stderr` encoding dynamically.
* `get_project_root()`: Returns the absolute base directory to prevent relative path breakage.
* `setup_logger(name, log_filename, level)`: Configures a standardized Python logger writing to `logs/`.
* `print_banner(title, subtitle)`: Renders a sleek, professional magenta box panel banner for CLI scripts.
* `print_section(title)`: Renders horizontal divider lines.
* Status Logging Helpers: `print_info`, `print_success`, `print_warning`, `print_error`, `print_critical`, `print_system` providing clean, categorized text tags.

### `modules/llm_engine.py`
**Purpose:** The central orchestrator for Local/Cloud AI models. Acts as the primary "brain" by executing a two-layer architecture: an Intent Classifier (DMM) and a live Chat Stream.
* **Class `CentralizedLLMEngine`:**
  * `__init__()`: Loads environmental configs (`.env`), sets up `OpenAI` client (for local LM Studio/Ollama) and `Cohere`/`Gemini` API clients (for cloud fallback). Initializes the expanded `self.funcs` whitelist and system preamble prompt parameters for the intent parser.
  * `is_online()`: Checks model reachability and network connection to determine execution mode.
  * `classify_intent(query)`: The Decision Making Model (DMM). Analyzes raw input via `cohere` or local APIs to categorize intent into 50+ granular automation shortcuts, hotkey triggers, or conversational routes, utilizing deep few-shot examples.
  * `generate_chat_stream(messages)`: Generates continuous conversational AI replies and streams them token-by-token. 

### `modules/chatbot.py`
**Purpose:** Orchestrates conversational state persistence and memory context tables. Manages both volatile short-term session RAM sliding windows and persistent long-term storage contexts.
* `AnswerModifier(Answer)`: Formats assistant response text to maximize density.
* `load_memory()`: Loads history files, providing automated fallback/index recovery in case of system interrupt.
* `save_memory(memory_list)`: Executes atomic transactional disk writes to avoid concurrency collisions.
* `Chatbot(query)`: Runs sliding memory window alignment logic, interfaces with the central LLM engine streaming channel, indexes queries, and triggers automated storage registers.

### `modules/real_time_search.py`
**Purpose:** Web Search and Retrieval-Augmented Generation (RAG) module. Scrapes live information from DuckDuckGo without any API key or authentication and integrates the structured results as direct system context to generate grounded LLM responses.
* `WebSearch(query)`: Leverages the `DDGS` unauthenticated text backend (with automated fallback to HTML scraper mode) to retrieve the top 5 web results, compiling titles, snippets, and URLs.
* `AnswerModifier(Answer)`: Formats assistant response text to maximize density.
* `load_memory()` and `save_memory(memory_list)`: Atomic transactional database methods handling corruption-resistant memory tracking.
* `RealTimeSearchEngine(query)`: The central orchestrator for the RAG search pipeline. Scrapes the web, builds an augmented user prompt structure, handles sliding short-term and persistent memory contexts, streams answers live to terminal, and persists triggered memory frames.

### `modules/deep_research.py`
**Purpose:** A publication-grade 6-stage autonomous research pipeline inspired by Gemini/ChatGPT Deep Research. Builds a comprehensive knowledge model by dynamically mapping research queries, crawling content links, resolving telemetry gaps, and synthesizing cohesive technical dossiers.
* `generate_research_plan(topic)`: Decomposes a broad central theme into a structured multi-angle query plan containing 5 focus angles and 10 targeted search queries.
* `search_web(query, max_results)`: Scrapes live search vectors and index pools via DuckDuckGo search clients.
* `deep_scrape_top_pages(url_list, max_pages)`: Scrapes and cleans full HTML web page articles via BeautifulSoup, stripping structural boilerplates, tracking domains, and compiling pure text.
* `generate_followup_queries(topic, context)`: Analyses accumulated context blocks to identify knowledge gaps, generating 3 targeted follow-up query sweeps.
* `synthesize_report(topic, context)`: Prompts the LLM to structure, draft, and format a professional, 2000+ word technical whitepaper complete with executive summaries, theme divisions, findings, limitations, future outlooks, and citation references.
* `DeepResearchEngine(topic)`: The master 6-stage orchestrator driving plan creation, broad crawling, deep extraction, gap analysis, gap-filling scrape, and document synthesis while outputting detailed character, domain, and time performance telemetry.

### `modules/speech_to_text.py`
**Purpose:** A flawless background STT engine. Unlike normal Python scripts that lock up the mic, this spins up an invisible headless Chrome browser utilizing the native Web Speech API. 
* **JavaScript Pipeline (`html_code`)**: Detects voice, caches interim results, and triggers a sentence cut off after `800ms` of Voice Activity Detection (VAD) silence. Sentences are pushed to `window.speechQueue`.
* **Class `SpeechToTextEngine`:**
  * `__init__()`: Configures silent, hardware-accelerated Chrome options, injects the JS via a data URL, and bypasses mic permissions. 
  * `listen_and_transcribe()`: Infinite polling loop. Waits for `window.speechQueue.shift()` in the browser, extracting complete sentences back into Python.
  * `clear_queue()`: Flushes unread sentences.
  * `shutdown()`: Hooked to `atexit` to cleanly destroy the invisible Chrome process when Python shuts down.
* **Formatters:**
  * `format_query(query)`: Cleans speech input (capitalization, question marks).
  * `translate_query(query)`: Hooks into `mtranslate` to automatically translate non-English spoken requests to standard English.

### `modules/text_to_speech.py`
**Purpose:** A lightning-fast, 100% offline real-time TTS system powered by Kokoro-ONNX.
* **Class `KokoroOnnx`**: A genius subclass extending the base `Kokoro`. 
  * `stream(text, voice, speed)`: Wraps the native `async create_stream` generator inside a managed background thread with a thread-safe Queue. This converts the asynchronous speech creation process into a synchronous generator, delivering `0ms` startup latency to the speakers.
* **Class `DynamicVoiceEngine`**:
  * `__init__()`: Autodetects `ASSISTANT_GENDER` from `.env` to map to premium voices (`am_adam` or `af_bella`). Performs highly robust fallback path resolutions to locate `kokoro.onnx` and `voices.bin`.
  * `speak(text)`: Consumes the `stream()` audio arrays chunk by chunk, feeding them instantly to `sounddevice` for zero-latency speaker playback.

### `modules/air_cursor_engine.py`
**Purpose:** Real-time spatial hand gesture recognition and pure C-level user32 input injection. Eliminates pyautogui dependency and leverages advanced computer vision to implement seamless cursor navigation, clicking, double clicking, and natural scrolling using webcams.
* **1-Euro Filter Class (`OneEuroFilter`)**:
  * An adaptive, low-pass filter configured with `min_cutoff=2.0` and `beta=0.05`. Aggressively smooths slow movements to eliminate micro-hand wobble and jitter while dynamically widening the cutoff during fast actions to ensure zero-lag tracking.
* **Gesture Recognition Engine (`GestureStateMachine`)**:
  * `classify(landmarks, cam_w, cam_h, now)`: Computes spatial anatomical properties. Classifies extended/curled states using a scale/rotation-invariant relative joint distance algorithm (`dist(wrist, tip) > dist(wrist, pip) * 1.05`).
  * **Hysteresis Click Debouncer**: Confirms left/right pinch gestures over a 3-frame buffer to prevent sensor noise from triggering phantom clicks. Integrates a strict `CLICK_COOLDOWN = 0.45s` to guarantee clean inputs.
  * **3-Finger Programmatic Double Click**: Classifies a simultaneous pinch of Thumb + Index + Middle (Bird Beak pinch) and fires a clean programmatic double click event instantly.
  * **Absolute Gesture Isolation**: Automatically disables click/pinch tracking whenever the scroll navigation pose is active, preventing false triggers during workspace navigation.
  * **Continuous Joystick Scroll**: Anchors a virtual Y-position when the scrolling pose starts, producing continuous upward or downward scroll inputs proportional to how far you hold your hand from the baseline anchor.
* **Webcam Engine (`GestureMouseEngine`)**:
  * `start_capture_loop()`: Spins up OpenCV video stream at 30 FPS. Draws interactive zones utilizing asymmetric anatomical comfort bounds (`MARGIN_X = 100`, `MARGIN_TOP = 100`, `MARGIN_BOTTOM = 185`) to easily reach the bottom edges of standard monitors without cutting off the wrist and forearm at the webcam border.

### `modules/automation_windows.py`
**Purpose:** High-utility hardware input injection, system telemetry aggregator, and OS/browser orchestration module. Simulates hardware keyboard presses, adjusts monitor panels, interacts with active window contexts, and acts as the physical "hands" of the assistant.
* `global_desktop_type(text)`: Inject physical low-level key strings at the current active mouse cursor using fast clipboard and pyautogui fallbacks.
* `OpenApp(app)`: Fuzzy matches local executables, redirects official web targets safely via DuckDuckGo `!ducky` bang vectors, and natively opens URLs/domains (e.g. `github.com`) directly inside the default browser. Handles space, comma, and 'and' separation for simultaneous multi-tab launches.
* `CloseApp(app)`: Win32 process tracker. Iterates open visible windows to locate target strings, focuses, and destroys standalone windows. Bypasses Windows `ForegroundLockTimeout` rules using a dummy ALT key hook to close browser tabs using standard hardware signals (`ctrl+w`) without un-maximizing or resizing windows. Falls back to AppOpener and aggressive process termination.
* `ExecuteCommand(command)`: Processes hardware system commands: volume mute, absolute calibration volume level configurations, WMI-based brightness modifications, sleep/suspend, computer restart, lock workstation, and shutdown.
* `TakeScreenshot(name)`: Snaps full desktop screenshot, resolving folder conflicts dynamically to support OneDrive-synced Desktop paths.
* `ClipboardCopy()`, `ClipboardPaste()`, `ClipboardCopyText(text)`: Performs simulated clipboard injections and direct string copies without keyboard typing simulation.
* `WindowManage(action)`: Orchestrates screen split controls (snap left/right), active window minimize/maximize operations, Alt+Tab focus swaps, Windows Action Center panels, and emoji picker hooks.
* `MediaControl(action)`: Dispatches hardware media key triggers (play, pause, next track, skip, back, stop) to control media players globally.
* `SystemInfo(query)`: Telemetry portal pulling real-time WMI diagnostic statistics for battery %, power states, RAM usage, local storage allocations, CPU load, and system boot uptime.
* `SetTimer(command)`: Starts asynchronous background countdown thread that fires a native Windows OS notification toast when complete.
* `HotkeyShortcut(action)`: Translates natural language into 18 common keyboard shortcut injections (undo, redo, save, zoom, run dialog, task manager).
* `ToggleWifi(action)`: Enables/disables local Wi-Fi adapters using clean PowerShell interface controls.

### `tests/test_engine.py` & `tests/test_voice.py`
**Purpose:** Sandbox integration scripts designed with the new cyberpunk `rich` terminal interfaces. 
* `test_engine.py`: Simulates user prompts, measures Intent Classification execution times, and displays live streaming token generation.
* `test_voice.py`: An interactive REPL playground allowing the developer to type inputs to test the TTS engine's latency, dynamic voice mapping, and pronunciation instantly.

