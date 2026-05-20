# OPTIMUS Engine Blueprint

## Project Structure

```text
project-optimus/
├── .env                  # Active environment variables (API keys, models, system config)
├── .env.example          # Template for environment configuration
├── main.py               # Application entrypoint
├── requirements.txt      # Python dependencies
├── data/                 # Dynamic data generation and caching directory
│   └── Files/            # Sub-directory for temp STT/TTS status tracking
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
    ├── llm_engine.py     # Centralized LLM Engine (DMM & Chat Streaming)
    ├── speech_to_text.py # Continuous Web Speech API via headless Chrome
    └── text_to_speech.py # Offline Kokoro-ONNX Real-Time Audio Streaming
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
  * `__init__()`: Loads environmental configs (`.env`), sets up `OpenAI` client (for local LM Studio/Ollama) and `Cohere`/`Gemini` API clients (for cloud fallback).
  * `is_online()`: Checks model reachability and network connection to determine execution mode.
  * `classify_intent(query)`: The Decision Making Model (DMM). Analyzes raw input via `cohere` or local APIs to categorize intent (e.g., realtime, deep research, general).
  * `generate_chat_stream(messages)`: Generates continuous conversational AI replies and streams them token-by-token. 

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

### `tests/test_engine.py` & `tests/test_voice.py`
**Purpose:** Sandbox integration scripts designed with the new cyberpunk `rich` terminal interfaces. 
* `test_engine.py`: Simulates user prompts, measures Intent Classification execution times, and displays live streaming token generation.
* `test_voice.py`: An interactive REPL playground allowing the developer to type inputs to test the TTS engine's latency, dynamic voice mapping, and pronunciation instantly.
