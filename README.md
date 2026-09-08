# Kayra - Desktop Assistant

Kayra is a Jarvis-inspired desktop assistant for Windows. You speak or type; it works out what
you meant, and then either answers you, searches the live web, writes a research report, or
takes hold of your machine and does the thing.

It listens continuously through a headless browser's speech engine, classifies intent with a
small LLM, and speaks back with an offline neural voice. You can interrupt it mid-sentence.

```
    voice or text  ──▶  local control  ──▶  intent (DMM)  ──▶  chat / search / research
                             │                                 automation ──▶ your desktop
                             └── stop · sleep · shut down            │
                                                                     ▼
                                                        speech out (Kokoro ONNX)
```

**Everything here works offline** except live web search and the cloud model path. Point it at
a local LLM server and it never touches the network.

---

## Table of contents

- [What it can do](#what-it-can-do)
- [Tech stack](#tech-stack)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running Kayra](#running-kayra)
- [The desktop interface](#the-desktop-interface)
- [Talking to Kayra](#talking-to-kayra)
- [Command reference](#command-reference)
- [Speech device: CPU or GPU](#speech-device-cpu-or-gpu)
- [Debugging guide](#debugging-guide)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Known limitations](#known-limitations)

---

## What it can do

|                                 |                                                                                                                                     |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Talk**                  | Conversation with memory, from a cloud or a local model                                                                             |
| **Search**                | Live DuckDuckGo retrieval, answered by the model with fresh context                                                                 |
| **Research**              | A 6-stage autonomous agent that writes a full markdown report into`Reports/`                                                      |
| **Control your PC**       | Open and close apps and sites, windows, media keys, hotkeys, clipboard, screenshots, timers, volume, brightness, Wi-Fi, system info |
| **Listen while it talks** | Say "stop" and it stops mid-sentence                                                                                                |
| **Speak first**           | An optional proactive agent that occasionally offers something useful, on a strict budget                                           |
| **Read the room**         | A text-only mood estimate that adjusts tone, never intent                                                                           |
| **Show you**              | A native desktop dashboard, or pure terminal if you prefer                                                                          |

---

## Tech stack

**Language & runtime**

- Python 3.11 (3.10 minimum) in a project-local `.venv`

**Speech in**

- Web Speech API driven inside a **headless Chrome or Edge** via `selenium`
- Voice-activity segmentation in the page itself; `mtranslate` for non-English input

**Speech out**

- **Kokoro-82M** neural TTS (`kokoro-onnx`) on **ONNX Runtime**, fully offline
- `sounddevice` / `soundfile` / `numpy` for a persistent low-latency output stream
- Optional **CUDA** execution via `onnxruntime-gpu` + the NVIDIA CUDA 12.8 / cuDNN 9 runtime wheels

**Intelligence**

- `cohere` - Command-R for intent classification (the "DMM")
- `openai` client against **Groq** (primary chat), **Gemini** (fallback), or any local
  OpenAI-compatible server (LM Studio / Ollama)

**Automation**

- `pywin32` for window enumeration, focus and `WM_CLOSE`
- `pyautogui`, `keyboard`, `pynput` for input injection
- `AppOpener` for application launching, `pyperclip` for the clipboard
- `psutil` for process and telemetry work, `send2trash` for safe deletion

**Retrieval**

- `ddgs` (with `duckduckgo-search` as a fallback name) + `beautifulsoup4`

**Interface**

- **PySide6** (Qt 6) - a native dashboard with a tray icon and an ambient panel
- `rich` for the terminal front end

**Vision (standalone)**

- `mediapipe` + `opencv-python` for the hand-gesture mouse

---

## Requirements

|                   |                                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------- |
| **OS**      | Windows 10/11. Conversation and speech work elsewhere;**desktop automation is Windows-only** |
| **Python**  | 3.10+ (3.11 is what this is developed and verified against; 3.12 works but is unverified)          |
| **Browser** | Chrome**or** Edge. Edge ships with Windows 11, so you probably already qualify               |
| **Audio**   | A working output device, and a microphone for voice input                                          |
| **Disk**    | ~2 GB for dependencies, ~120 MB for the speech model, ~1.4 GB more if you enable GPU speech        |
| **GPU**     | Optional. NVIDIA only, and read [Speech device](#speech-device-cpu-or-gpu) before you bother       |

You do **not** need the CUDA Toolkit, and you do **not** need to activate the virtual
environment by hand.

---

## Installation

### 1. Get the speech model

Kayra will not synthesize speech without it. Download from
[kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases) and drop the files
into `models/`:

```
models/
  kokoro-v1.0.int8.onnx     ← preferred: quantized, much faster
  voices-v1.0.bin
```

The full-precision pair (`kokoro.onnx` + `voices.bin`) also works - Kayra will use it and warn
that the quantized one is faster.

### 2. Run setup

```bash
cd project-kayra
python setup.py
```

That is the whole install. `setup.py` runs on your **system** Python - the virtual environment
does not have to exist yet - and it:

- checks your Python version
- creates or reuses `.venv`
- installs everything in `requirements.txt`
- **provisions the speech runtime**: picks the right ONNX Runtime build for your hardware,
  removes conflicting ones, installs the matching NVIDIA CUDA/cuDNN wheels on an NVIDIA machine,
  and then **proves a GPU session actually initializes** before claiming it works
- creates `.env` from `.env.example` and prompts for API keys
- probes for a local LLM server
- verifies every import, the audio device, and the speech-input browser

It **never** overwrites an existing `.env`, and **never** deletes a `.venv` without an explicit
`y` that defaults to no.

It finishes with a report. Every line is a fact read from the environment that will actually run
Kayra:

```
  ✓ Python                       3.11.9 (venv: 3.11.9)
  ✓ Virtual environment          ready
  ✓ Dependencies                 installed
  ✓ Kayra package                importable
    Local LLM                    available
  ✓ Cloud keys                   Cohere SET, chat SET
  ✓ Speech model                 full precision
  ✓ Speech device                GPU ready (CUDAExecutionProvider verified)
  ✓ Audio output                 ready
  ✓ Speech-input browser         Microsoft Edge
  ✓ Automation                   ready
```

Re-running `setup.py` is safe and is also how you **repair** a broken environment.

> **Never `pip install onnxruntime` yourself.** The three ONNX Runtime distributions all install
> into the same folder, so a manual install silently replaces the GPU build with the CPU one and
> nothing in the app can tell. `setup.py` owns that decision.

---

## Configuration

Everything lives in `.env` at the project root. `.env.example` is the annotated template - it
documents every key. Never commit a real `.env`.

### The keys you will actually touch

```ini
# ── Who you are talking to ────────────────────────────────────────────
ASSISTANT_NAME=KAYRA
ASSISTANT_GENDER=Female
USERNAME=YourName
USER_GENDER=Male
LANGUAGE=English

# ── Speech ────────────────────────────────────────────────────────────
INPUT_LANGUAGE=en-US        # or hi-IN, en-IN, en-GB, es-ES, ...
ASSISTANT_VOICE=af_bella    # Kokoro voice: af_bella, am_adam, ...
TTS_DEVICE_MODE=AUTO        # AUTO | GPU | CPU
STT_BROWSER=auto            # auto | chrome | edge | brave | chromium

# ── Where the thinking happens ────────────────────────────────────────
FORCE_ONLINE=False          # True skips the local-server check entirely
LOCAL_BASE_URL=http://localhost:1234/v1
LOCAL_CHAT_MODEL=local-model
LOCAL_DECISION_MODEL=local-model

# ── Cloud keys (only needed when no local server is running) ───────────
CohereAPIKey=...            # intent classification - without it, everything is chat
GROQ_API_KEY=...            # primary conversational model
GEMINI_API_KEY=...          # fallback when Groq is rate-limited
```

### Model routing, in one paragraph

Kayra is **local-first**. On startup it probes `LOCAL_BASE_URL`; if something answers, *all*
traffic goes there and no cloud client is even constructed. If nothing answers, it uses Cohere
for intent and Groq → Gemini for chat. Set `FORCE_ONLINE=True` to skip the local check.

**Without a Cohere key and without a local server, every request is treated as conversation** -
no automation, no search routing. That is the single most common cause of "it just chats at me".

### The rest

`.env.example` also covers the proactive agent (20 knobs), automation bounds (confirmation
timeout, shell timeout, screenshot retention, timer cap) and deep-research tuning. All have
sensible defaults and are range-clamped, so a malformed value cannot crash anything.

You can edit most of these from **Settings** in the desktop interface instead.

---

## Running Kayra

```bash
python run.py
```

That is it. `run.py` finds `.venv`, re-executes itself inside it, runs preflight, takes a
single-instance lock, and starts the assistant. **You never activate the environment yourself.**

| Command                     | What it does                                                                   |
| --------------------------- | ------------------------------------------------------------------------------ |
| `python run.py`           | Desktop interface (the default)                                                |
| `python run.py --console` | Voice + terminal only, no window                                               |
| `python run.py --doctor`  | Report Python, ONNX Runtime, providers, verified CUDA and GPU stats, then exit |
| `python run.py --force`   | Start even if a stale lock is held                                             |
| `python run.py --help`    | Usage                                                                          |

Equivalent entry points, all landing in the same place:

```bash
python main.py        # backward-compatibility shim
python -m kayra       # from inside the virtual environment
```

On start you will see which interpreter and which ONNX Runtime are in play:

```
  ·  Python  3.11.9  D:\...\project-kayra\.venv\Scripts\python.exe
  ·  ONNX Runtime  onnxruntime-gpu 1.26.0
```

### Stopping it

Say **"exit"** or **"turn off Kayra"**, click **Shut down Kayra** on the Home screen, use
**Quit Kayra** in the tray, or press **Ctrl+C**. All four run the same teardown, in the same
order, and clean up only the browser processes Kayra owns.

---

## The desktop interface

Seven screens, reachable with **Ctrl+1** … **Ctrl+7**:

**Home** - the assistant orb and what it is doing; recent activity; CPU/RAM; live GPU
statistics and which device speech is actually running on; the shutdown button.
**Chat** - the conversation, streamed sentence by sentence as it is spoken.
**Automation** - every action Kayra has taken, with the decision behind it.
**Memory** - what it has saved, and what it has learned about your routines.
**Activity** - a timeline of the session.
**System** - a hardware readiness report.
**Settings** - identity, voice, language, browser, model routing, API keys, speech device.

| Shortcut      |                                                     |
| ------------- | --------------------------------------------------- |
| `Ctrl+K`    | Jump to Chat                                        |
| `Ctrl+.`    | **Barge-in** - stop the sentence being spoken |
| `Ctrl+M`    | **Microphone** - pause or resume listening    |
| `Ctrl+1..7` | Navigate                                            |

Closing the window does **not** quit - it collapses into a small draggable ambient panel, and
Kayra keeps running in the tray. Quitting is deliberate and explicit.

If PySide6 is missing, `run.py` falls back to the console and tells you why.

---

## Talking to Kayra

### Lifecycle commands - these never reach the model

Four different ideas, four different commands. They are matched locally, in microseconds,
before any classifier, so they work with the network down.

| Say                                                       | What happens                                                                                                                                          |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| **"stop"** · "wait" · "hold"                      | Silences the sentence being spoken. Keeps listening.                                                                                                  |
| **"stop listening"**                                | Closes the microphone. Kayra keeps running.                                                                                                           |
| **"go to sleep"** · "Kayra sleep"                  | Standby: stops proactive speech and ignores everything but a wake word.**Keeps the microphone open** - otherwise it could not hear you wake it. |
| **"wake up"**                                       | Back to normal.                                                                                                                                       |
| **"exit"** · "turn off Kayra" · "shut down Kayra" | Ends the process, cleanly.                                                                                                                            |

Two boundaries that are deliberately sharp:

- **"stop"** interrupts speech. **"stop the music"** is a media command and reaches automation.
- **"turn off Kayra"** quits the assistant. **"turn off my PC"** is a Windows shutdown and goes
  through the confirmation gate.

A paused microphone cannot hear "start listening" - that is unavoidable. Resume from the Home
button, `Ctrl+M`, or the tray. If you want Kayra quiet but still listening, use **sleep**.

### Confirmations

Anything risky is confirmed out loud before it happens - shutting down the computer, closing
everything, forcing an application to quit. Answer with a plain "yes" or "no". "Yes and open
Chrome" is treated as a new instruction, not an authorisation.

---

## Command reference

Say these, or type them into Chat.

### Applications and websites

- `"open notepad"` - launches the app
- `"open github.com"` / `"open youtube"` - opens the site in your browser
- `"open chrome, open spotify"` - several at once

Kayra decides whether a name is an *application* or a *website* before doing anything, and it
never guesses at a name it does not recognise - a miss is reported rather than approximated.

### Closing things

- `"close notepad"` - closes **one** window, the way clicking its X does
- `"close youtube"` - closes the browser window showing YouTube
- `"close this window"` / `"close this tab"` - the one in front
- `"close all chrome"` - every Chrome window (you have to say *all*)
- `"close everything"` - confirmed first

One command closes one thing unless you explicitly widen it. If it is ambiguous, Kayra asks.

### Windows and desktop

- `"snap left"` / `"snap right"` - split-screen
- `"maximize"` / `"minimize"` / `"minimize all"` / `"show desktop"`
- `"switch window"` / `"task view"`
- `"open action center"` / `"open emoji picker"`

### Media

- `"pause"` / `"resume"` · `"next track"` / `"previous track"` · `"stop media"`

### System

- `"mute"` / `"volume up"` / `"set volume to 80%"`
- `"set brightness to 30%"`
- `"lock the computer"`
- `"turn off wifi"` / `"turn on wifi"`
- `"shut down my computer"` / `"restart"` - **confirmed first**

### Telemetry

`"battery status"` · `"how much ram is used"` · `"check disk space"` · `"cpu info"` ·
`"system uptime"` · `"what is my ip address"`

Answered from in-process counters, in about a millisecond.

### Keyboard and clipboard

- `"undo"` / `"redo"` · `"save"` · `"select all"` · `"find"`
- `"new tab"` / `"close this tab"` · `"refresh"` · `"fullscreen"` · `"zoom in"` / `"zoom out"`
- `"task manager"` · `"run dialog"`
- `"copy"` / `"paste"` · `"copy text: hello"` · `"type hello there"`

### Screenshots, timers, files

- `"take a screenshot"` - saved and verified, then reported
- `"set a timer for 5 minutes"` - native toast on completion
- `"create folder reports"` · `"find file budget"` · `"delete file old.txt"`

### Search and research

- `"what's the weather in Tokyo"` - live retrieval, answered
- `"search youtube for lofi mixes"` - opens the results page
- `"run deep research on solid state batteries"` - a 6-stage pipeline (plan → search → extract →
  gap analysis → follow-up → synthesis) that writes a full markdown report into `Reports/`.
  It takes minutes, and it says so before it starts.

### Proactive suggestions

- `"stop proactive suggestions"` / `"don't interrupt me"` - off for the session
- `"enable proactive mode"` - back on

### Gesture control (standalone)

A separate MediaPipe hand-tracking mouse, not wired into the assistant:

```bash
.venv\Scripts\python.exe -m kayra.input.gesture
```

| Gesture                 |                   |
| ----------------------- | ----------------- |
| Index finger extended   | Move the cursor   |
| Thumb + index pinch     | Left click / drag |
| Thumb + middle pinch    | Right click       |
| Thumb + index + middle  | Double click      |
| Index + middle extended | Joystick scroll   |

---

## Speech device: CPU or GPU

`TTS_DEVICE_MODE` in `.env`, or the **TTS device** dropdown in Settings.

| Mode     | Behaviour                                                                         |
| -------- | --------------------------------------------------------------------------------- |
| `AUTO` | Use the GPU when it genuinely initializes, CPU otherwise. Quiet either way.       |
| `GPU`  | Require the GPU. If it cannot start, fall back to CPU and**say so loudly**. |
| `CPU`  | Force the CPU. No GPU runtime is initialized at all.                              |

Kayra never claims a device it is not on. Settings shows the mode you chose **and, separately**,
the provider the live session is actually using - because those two disagree exactly when
something is wrong, and that is when you need to see it.

### Read this before enabling GPU

**On an RTX 4060 Laptop with the full-precision model, CUDA is not faster.** Measured:

|                  | CPU             | CUDA     |
| ---------------- | --------------- | -------- |
| Real-time factor | **0.888** | 0.934    |
| Process RAM      | +404 MB         | +1021 MB |
| VRAM             | 0               | +181 MiB |

ONNX Runtime adds *547 Memcpy nodes* to the graph because the CUDA provider does not implement
every operator Kokoro uses, so it shuttles data between host and device throughout. GPU mode is
correct, supported and honestly reported - it is just not a speed-up for this model. **CPU is a
perfectly good choice**, and it is the default outcome when no GPU runtime is present.

If you do want it, `setup.py` handles the whole thing: `onnxruntime-gpu`, the pinned CUDA 12.8
and cuDNN 9 wheels (~1.4 GB), and a real session probe to prove it works. No CUDA Toolkit.

---

## Debugging guide

Start here, always:

```bash
python run.py --doctor
```

It prints the interpreter, the environment, the ONNX Runtime build, every execution provider,
whether CUDA is **verified** usable (a real session, not just a provider name), and live GPU
statistics. Most of the problems below are diagnosed by that one command.

---

### "It says to run setup.py"

The virtual environment is missing or broken.

```bash
python setup.py
```

If it still fails, delete `.venv` and let setup rebuild it (it will ask first).

---

### My edits do nothing

`run.py` checks this and refuses to start with an explanation. It means the `.venv` interpreter
is importing `kayra` from somewhere else - a global `pip install`, a stale `PYTHONPATH`, or a
second checkout earlier on the path. Remove the other copy.

Confirm where it is coming from:

```bash
.venv\Scripts\python.exe -c "import kayra; print(kayra.__file__)"
```

It must be under this project's `src\kayra\`.

---

### Settings says GPU but Active device says CPU

This is the interesting one, and it is **not** a display bug - it is Kayra telling you the
truth. ONNX Runtime lists `CUDAExecutionProvider` the moment `onnxruntime-gpu` is installed,
whether or not the CUDA runtime it needs is present. When it is missing, ONNX Runtime does not
raise: it logs, drops the provider, and hands back a working CPU session.

```bash
python run.py --doctor
```

Look at **CUDA VERIFIED usable**. If it is `no`, the reason line names what is missing -
typically `cublasLt64_12.dll`, meaning the NVIDIA runtime wheels are absent.

```bash
python setup.py
```

That installs them and then proves a CUDA session initializes. If it still fails, the usual
cause is an NVIDIA driver older than the CUDA version the ONNX Runtime build targets.

---

### "Conflicting ONNX Runtime packages installed"

`onnxruntime`, `onnxruntime-gpu` and `onnxruntime-directml` all install into the same folder, so
having two means one silently overwrote the other. `pip uninstall` alone makes it *worse* -
their file manifests overlap, so removing one deletes files the other needs.

```bash
python setup.py
```

It removes all of them and reinstalls exactly one, at a pinned version.

---

### Kayra cannot hear me

**Check the browser first.** Speech recognition needs a speech *backend*, and not every Chromium
browser has one. Chrome uses Google's, Edge uses Microsoft's, and **Brave deliberately ships
neither** - the API exists, `start()` succeeds, and no transcript ever arrives.

Kayra detects this and switches to a browser that works, saying so at startup:

```
Brave is your default browser, but it ships without a speech recognition backend,
so voice input cannot use it. Using Microsoft Edge for speech input instead.
```

That is normal and nothing is wrong. If you want to force a choice, set `STT_BROWSER=chrome` (or
`edge`) in `.env` - though a browser without a backend is still rejected.

Then check the obvious ones:

- Is the microphone muted, or claimed exclusively by another app?
- Is Windows microphone privacy blocking it? *Settings → Privacy → Microphone*
- Is Kayra paused? Look at Home - it says **Listening paused** in as many words.
- Is Kayra asleep? Say **"wake up"**.

Diagnose the session itself:

```bash
.venv\Scripts\python.exe tests\test_stt_lifecycle.py
```

---

### Saying "stop" does not stop it

It should, within about 50 ms. If it does not:

1. Is speech actually still playing, or is the model still *generating*? Kayra cancels both, but
   there is a moment at the very start of a turn where nothing has been queued yet.
2. Check the microphone is live at all - a muted input device looks exactly like broken barge-in,
   which is why the live test checks for it first:

```bash
.venv\Scripts\python.exe tests\test_barge_in_live.py     # needs you to speak
```

3. Verify the vocabulary and the plumbing without hardware:

```bash
.venv\Scripts\python.exe tests\test_voice_control.py
```

Note that **"stop the music" spoken while Kayra is talking** is treated as a barge-in - at the
instant you have said only the first word, it is indistinguishable from one. Say it while she is
silent and it reaches media control normally.

---

### Kayra answers herself

The microphone stays open while she speaks, so she hears her own voice. Utterances captured
while audio was leaving the sound card are dropped as echo. If this recurs, it usually means the
audio device changed underneath her.

```bash
.venv\Scripts\python.exe tests\test_audio_pipeline.py
```

---

### No sound

- Check the Windows output device, and that nothing holds it in exclusive mode.
- Is the model present? `models/` needs either `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin` or
  `kokoro.onnx` + `voices.bin`. Setup reports this as **Speech model: MISSING**.
- Kayra degrades to muted rather than refusing to start, and says so at boot.

---

### It just chats at me instead of doing things

Intent classification is not running. Either:

- **No Cohere key and no local server** - every request degrades to conversation. This is stated
  at boot.
- **Cohere is rate-limited** - you will see cooldown warnings, then the same degradation.

Check what is routing:

```bash
.venv\Scripts\python.exe tests\test_dmm_matrix.py
```

The first line tells you which model answered. Note it follows the same local-first rule as the
assistant - with LM Studio running, it measures your *local* model, not Cohere.

---

### "Another instance is already running"

The single-instance lock is `data/kayra.lock`. It holds a PID *and* a process creation time, so
a recycled PID cannot be mistaken for a live Kayra. Shutdown ends in a hard exit, so the file is
always left behind - that is expected, and staleness is detected rather than assumed.

If you are certain nothing is running:

```bash
python run.py --force
```

---

### Leftover browser processes

Kayra tracks the browser processes it started **by PID** and reaps only those. It will never
kill a browser by name, because an application *you* asked it to open becomes its child too - a
name sweep would close your own browsing session.

To clean up an orphaned run by hand, walk the process tree from its ChromeDriver. Do **not**
`taskkill /IM chrome.exe`; on a typical machine that kills your browser along with Kayra's.

---

### The window will not open

PySide6 is missing or failed to load. Kayra falls back to the console and prints the reason.

```bash
.venv\Scripts\python.exe -m pip install PySide6-Essentials
```

---

### Something broke after I changed code

```bash
.venv\Scripts\python.exe -m py_compile main.py setup.py run.py $(find src tests -name "*.py")
.venv\Scripts\python.exe -m pyflakes src/kayra tests run.py setup.py main.py
```

`py_compile` does not catch undefined names - run **both**, especially after moving files.

---

### Where the logs and state live

```
data/kayra.lock              single-instance lock
data/conversation.json       long-term memory (+ .backup)
data/habits.json             learned routines - counters only, never transcripts
data/browser_support.json    which browsers verified as able to transcribe
logs/                        runtime logs
Reports/                     deep-research output
```

---

## Testing

`tests/` are standalone diagnostic scripts, not a pytest suite. Run each directly. They assert
and exit non-zero.

### Fast - no audio, browser or network

```bash
.venv\Scripts\python.exe tests\test_automation.py         # 263 checks
.venv\Scripts\python.exe tests\test_ui.py                 # 322 checks
.venv\Scripts\python.exe tests\test_voice_control.py      # 186 checks
.venv\Scripts\python.exe tests\test_tts_device.py         # 143 checks
.venv\Scripts\python.exe tests\test_proactive_agent.py    # 140 checks
.venv\Scripts\python.exe tests\test_emotion_engine.py     # 119 checks
.venv\Scripts\python.exe tests\test_target_resolution.py  # 108 checks
.venv\Scripts\python.exe tests\test_browser_selection.py  #  64 checks
.venv\Scripts\python.exe tests\test_environment.py        #  63 checks
```

### Needs hardware or network

```bash
.venv\Scripts\python.exe tests\test_audio_pipeline.py     # needs the TTS model
.venv\Scripts\python.exe tests\test_stt_lifecycle.py      # needs a browser
.venv\Scripts\python.exe tests\test_dmm_matrix.py         # needs a model endpoint
.venv\Scripts\python.exe tests\test_voice.py              # TTS sandbox
```

### Needs a human

```bash
.venv\Scripts\python.exe tests\test_barge_in_live.py      # you have to speak
```

A passing UI suite does **not** mean the interface looks right. Render the screens and look at
them.

---

## Project layout

```
project-kayra/
├── run.py                  launcher - finds .venv, re-execs, locks, starts
├── setup.py                environment provisioning and verification
├── main.py                 compatibility shim
├── requirements.txt
├── .env.example            every setting, annotated
├── models/                 Kokoro ONNX weights (you supply these)
├── data/  logs/  Reports/  runtime state and output
├── docs/
│   ├── KAYRA_SYSTEM_ARCHITECTURE.md    how and why every subsystem works
│   └── KAYRA_UI_ARCHITECTURE.md        the desktop interface
├── tests/
└── src/kayra/
    ├── app.py              orchestrator: boot, listen/route loop, lifecycle
    ├── core/               paths, config, runtime state, local voice control
    ├── intelligence/       LLM routing + intent classifier, emotion engine
    ├── input/              speech-to-text, browser selection, gesture
    ├── output/             Kokoro TTS, ONNX device manager
    ├── automation/         the "hands": actions, safety policy, target resolution
    ├── services/           chat, live search, deep research, proactive agent
    ├── memory/             conversation persistence
    ├── utils/              console, timing, text normalisation
    └── ui/                 PySide6 dashboard
```

Deep detail - including what was tried and rejected, and why - lives in
[`docs/KAYRA_SYSTEM_ARCHITECTURE.md`](docs/KAYRA_SYSTEM_ARCHITECTURE.md).

---

## Known limitations

Stated plainly. Several are deliberate trade-offs rather than defects.

- **Windows only** for automation. Conversation and speech work elsewhere; window management,
  app control and hotkeys do not.
- **Background browser tabs are invisible.** Windows exposes one handle per browser window and
  its title reflects only the active tab, so "close YouTube" cannot find a YouTube tab that is
  not in front. Kayra reports that rather than closing something else.
- **GPU speech is not faster** for this model. See [Speech device](#speech-device-cpu-or-gpu).
- **DirectML and ROCm are recognised but untested.** Only CUDA is used for speech.
- **Standby does not release the microphone**, because "wake up" is a spoken command. Use "stop
  listening" if you want it genuinely closed.
- **Emotion detection is text-only.** It cannot hear "Fine." said angrily - the raw audio never
  enters the process.
- **Cloud intent classification is Cohere-only**, with no fallback. Without it and without a
  local server, everything becomes conversation.
- **No speaker identification.** Anyone audible can issue commands, bounded only by the safety
  policy and its confirmation prompts.
- **The proactive agent offers, it does not act.** It can tell you a routine is due; it will not
  run it for you.
