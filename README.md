# Optimus-DA

Optimus-DA is a high-performance, intelligent desktop assistant framework. It features a highly adaptable dual-mode **Centralized LLM Engine** paired with a professional **Offline Speech Matrix** (Speech-to-Text & Text-to-Speech). The engine is engineered to run seamlessly in both 100% offline local setups and hybrid cloud modes, ensuring absolute privacy, speed, and reliability.

---

## Key Features

### Centralized LLM Engine (`modules/llm_engine.py`)
* **Hybrid Connectivity Matrix:** Automatically switches between offline local models (such as LM Studio or Ollama) and powerful Cloud APIs (Gemini 2.5 & Cohere Command).
* **Intent-Driven Decision Making Model (DMM):** Uses high-speed classification to parse user inputs into structured system commands (e.g., deep research, application control, search queries, reminders) or general conversational responses.
* **Stream-Based Chunk Generation:** Provides real-time token streaming for low-latency assistant responses.
* **Robust Fallback Engine:** Features multi-level catch-and-retry patterns to handle network disruptions or local server authentication adjustments seamlessly.

### Offline Speech Matrix (`modules/speech_to_text.py` & `modules/text_to_speech.py`)
* **Continuous Speech-to-Text (STT):** Continuous, asynchronous browser-based STT transcription tailored to your preferred language matrix.
* **Premium Offline TTS:** Powered by a premium, local offline Kokoro-ONNX voice engine, delivering human-like speech output with zero network dependencies.

---

## Architectural Layout

```
project-optimus/
├── modules/
│   ├── __init__.py           # Package exports & legacy backwards-compatibility mappings
│   ├── chatbot.py            # Conversational memory engine & persistent context layers
│   ├── llm_engine.py         # Decision Making Model & Chat Stream manager
│   ├── text_to_speech.py     # Local offline Kokoro TTS Engine wrapper
│   ├── speech_to_text.py     # Browser-based Continuous STT engine
│   └── utils.py              # Shared helpers, logs, and path resolution
├── tests/
│   ├── test_engine.py        # LLM Engine integration and fallback tests
│   └── test_voice.py         # Interactive TTS voice playground
├── .env.example              # Central environment configuration template
└── requirements.txt          # Python dependency declarations
```

---

## Quick Start

### 1. Prerequisites & Installation
Ensure you have Python 3.10+ installed. Set up your virtual environment and install the required dependencies:

```bash
# Initialize virtual environment
python -m venv .venv

# Activate virtual environment
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install required modules
pip install -r requirements.txt
```

### 2. Environment Setup
Create your local environment configuration file:
1. Copy `.env.example` to `.env`.
2. Open `.env` and fill in your details:

```ini
# Speech Matrix configurations
INPUT_LANGUAGE=hi-IN
ASSISTANT_VOICE=am_adam

# Local LLM Server (LM Studio / Ollama)
FORCE_ONLINE=False
LOCAL_BASE_URL=http://127.0.0.1:1234/v1
LOCAL_API_KEY=lm-studio   # Update if you have custom auth enabled in your local server

# Cloud APIs (Required if FORCE_ONLINE=True or local server is offline)
CohereAPIKey=your_cohere_api_key
GEMINI_API_KEY=your_gemini_api_key
```

---

## Running the Playgrounds & Integration Tests

### LLM Engine Integration Test
Validates the classification boundaries (DMM) and checks streaming chat responses in both offline and online states.
```bash
python tests/test_engine.py
```
*Note: If running on Windows, the test automatically reconfigures standard input/output to use `UTF-8` to ensure emojis and complex characters print perfectly without console crashes.*

### Interactive TTS Voice Playground
Launches an interactive console utility that lets you type any sentence and hear it synthesized locally using the premium Kokoro TTS voice engine.
```bash
python tests/test_voice.py
```

---

## Conversational Memory & Storage Architecture

The assistant implements a hybrid dual-tier memory system designed for maximum conversation speed, contextual relevance, and durable persistence.

### 1. The Dual-Tier Memory System

* **Short-Term Session Memory (Volatile)**: 
  * Maintained in-memory inside a volatile RAM list (`session_memory`).
  * Stores all dialogues during the current running session.
  * Bounded to a **sliding window of the last 6 messages** (3 conversational exchanges). This bounds context token usage to ensure high execution speeds and prevent model context window overflow.
  
* **Long-Term Context Memory (Persistent)**:
  * Restored from a local database file (`data/conversation.json`) at startup.
  * Injected directly at the top of the LLM context pool on every request, creating a persistent personality baseline and history.
  * Handled dynamically to survive sudden terminal halt events.

### 2. When & How Data is Stored

The assistant is engineered to avoid cluttering long-term memory with small talk. Instead, it selectively commits items to long-term storage based on user directives:

* **Trigger Commands**:
  The system automatically monitors user prompts for explicit memory-save trigger words:
  `"store this"`, `"remember this"`, `"save this"`, `"memorize this"`, `"note this"`
  
* **Storage Execution**:
  When a trigger word is matched, the active user query and the corresponding response are appended to the permanent memory list and committed directly to disk.

### 3. Fail-safe Atomic File Writing

To protect your conversational history from sudden system terminations, the persistence layer utilizes an atomic file transaction scheme:
1. Writes the fresh JSON context payload to a secondary backup location: `data/conversation_backup.json`.
2. Replaces the primary `data/conversation.json` database via an atomic system-level copying transaction (`shutil.copy`).
3. If the primary file ever gets corrupted or experiences half-write failures during unexpected shutdowns, the startup recovery pipeline automatically detects the error and restores the database using the secondary backup copy.

