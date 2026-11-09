# Update V1 — Baseline: the repository as cloned

| | |
|---|---|
| **Version** | V1 (baseline) |
| **Code state** | commit `929371b` on `main` — *"feat: Animation optamization and UI tweeks with overall performance improvement."* (2026-09-10) |
| **Package version** | `kayra.__version__ = "2.0.0"` |
| **Recorded** | 2026-11-09 |
| **Purpose of this report** | Write down what the project was, as cloned, before the cross-platform work, and summarise what was changed from it. The full detail of the change is in [`UPDATE_V2.md`](UPDATE_V2.md). |

---

## 1. What Kayra was

Kayra, a personal **Windows** desktop assistant. Voice input (headless Chrome/Edge Web
Speech API) or typed text; a small LLM ("the DMM") classifies the request into task tokens;
the request is routed to conversation, live web search, a six-stage deep-research agent,
assistant self-control, or Windows desktop automation; replies are spoken by an offline
Kokoro-ONNX voice with barge-in. Optional hand-gesture mouse control through a webcam and
MediaPipe. A PySide6 desktop UI sits over the same backend.

### History up to the baseline

33 commits from 2026-05-20 to 2026-09-10. The major milestones:

| Date | Milestone |
|---|---|
| 2026-05-20 | Project blueprint; chatbot with persistent dual memory |
| 2026-05-22 | Real-time search and deep research; framework renamed to Kayra |
| 2026-05-23 | Windows automation; 6-stage research pipeline; DMM with 50+ intents |
| 2026-05-24 | Strict DMM token routing, synchronised TTS boot, offline local LLM support, barge-in filter |
| 2026-06-03 | Semantic emotion engine; air-cursor (gesture) engine |
| 2026-09-07 | Package refactor into `src/kayra/`; managed STT session; epoch-cancellable TTS |
| 2026-09-08 | NVIDIA GPU provisioning for speech; target-resolution fixes |
| 2026-09-09 | Runtime reliability and voice-interaction hardening |
| 2026-09-10 | Redesigned UI shell, installation control, animation/performance work |

## 2. What the baseline contained

### Repository layout (131 tracked files)

```
AGENTS.md            short file: "read CLAUDE.md" + graphify instructions
CLAUDE.md            246 KB agent reference (architecture, gotchas, measurements)
README.md            user guide, debugging guide, command reference
blueprint.md         OLD project blueprint (described a modules/ layout that no longer existed)
main.py run.py setup.py requirements.txt .env.example
docs/KAYRA_SYSTEM_ARCHITECTURE.md   docs/KAYRA_UI_ARCHITECTURE.md
models/kokoro.onnx  models/voices.bin
src/kayra/   app.py + automation/ core/ input/ intelligence/ memory/ output/ services/ ui/ utils/
tests/       30 test scripts + run_all.py + _harness.py + TESTING.md
.agents/     graphify rules for other agent tools
```

### Source size by package

| Package | Files | Lines | Role |
|---|---|---|---|
| `app.py` | 1 | 2,402 | orchestrator: boot, listen/route loop, lifecycle, shutdown |
| `automation/` | 4 | 4,839 | Windows desktop control (policy, target resolution, handlers) |
| `core/` | 12 | 5,573 | paths, config, runtime state, voice state, control vocabulary, endpointing, hardware, logging |
| `input/` | 15 | 7,467 | speech-to-text, browser choice, transcript repair, gesture package |
| `intelligence/` | 5 | 3,999 | LLM engine (DMM), provider router, emotion engine, proactive presence |
| `output/` | 3 | 1,778 | Kokoro TTS and the ONNX Runtime device layer |
| `services/` | 5 | 2,537 | chatbot, real-time search, deep research, proactive agent |
| `memory/` | 3 | 434 | conversation persistence and management |
| `ui/` | 28 | 12,348 | PySide6 desktop interface |
| `utils/` | 4 | 571 | console, timing, text helpers |

### Features

- Continuous voice input with VAD/endpointing, echo rejection, barge-in ("stop", "wait",
  "hold"), confirmation-gated lifecycle commands (sleep, shutdown).
- Intent classification with a provider router: local LM Studio/Ollama first, else
  Cohere → Groq → Gemini for intent and Groq → Gemini for chat, with cooldowns.
- Conversation with memory, DuckDuckGo search, autonomous research reports.
- Offline Kokoro speech on CPU or NVIDIA CUDA (verified-session rule).
- Windows automation: open/close apps and websites, window management, media keys, hotkeys,
  clipboard, screenshots, timers, system info, confirmation-gated power actions.
- Proactive suggestions and contextual presence (battery, load, routines, greetings).
- Hand-gesture pointer control (MediaPipe `solutions.hands`, Win32 `user32`).
- Desktop UI: Home, Chat, Automation, Memory, Activity, Settings, System screens, tray,
  ambient floating panel.

### Platform and runtime support

| Item | Baseline |
|---|---|
| Operating system | **Windows 10/11 only.** Linux was not a target; several modules had Windows-only branches and "Windows-only" warnings |
| Python | 3.10 floor, 3.11 recommended, `MAX_VERIFIED_PYTHON = 3.12` |
| OS-specific code | spread across `core/hardware.py`, `core/system_profile.py`, `input/browsers.py`, `input/gesture/pointer.py`, `memory/store.py`, `services/proactive_agent.py`, `ui/components/backdrop.py`, `app.py`, `run.py`, `setup.py` |
| Dependency manifest | `requirements.txt`, no platform markers, mostly open-ended versions |

### Baseline dependencies (`requirements.txt`)

selenium, mtranslate, python-dotenv, rich, requests, psutil, **pygetwindow**, **pywin32**,
pyperclip, send2trash, openai, cohere (<8), kokoro-onnx, sounddevice, numpy, soundfile, ddgs,
**duckduckgo-search** (legacy duplicate), beautifulsoup4, **AppOpener**, **pyautogui**,
**pynput**, **pywhatkit**, **keyboard**, opencv-python, **mediapipe>=0.10** (no ceiling),
**PySide6-Essentials>=6.6** (no ceiling). Bold = packages that turned out to need markers or
ceilings.

### Tests

22 unit suites + integration + manual scripts, run by `tests/run_all.py` (no pytest). The
project's own record: 3,990 unit checks passing on Windows.

## 3. Problems found in the baseline (during the V2 analysis)

| # | Problem | Severity | Platform |
|---|---|---|---|
| 1 | `pywin32` (and other Windows-only packages) had no platform markers — `pip install -r requirements.txt` **failed outright on Linux** | Blocker | Linux |
| 2 | Automation imports `pyautogui`/`pynput`, which raise X11 errors (not `ImportError`) on Linux, so boot could crash | High | Linux |
| 3 | Hardware detection read only the Windows registry: Linux showed no GPU, no distro name | Medium | Linux |
| 4 | Arch's `chromedriver` (built for Chromium) on PATH was paired with Google Chrome → Chrome failed to start, Kayra fell back to Chromium | High | Linux |
| 5 | Omarchy's default browser (Chromium) starts a speech session but never transcribes; it would be preferred and cached as "verified" → deaf assistant | High | Linux |
| 6 | Desktop UI mode never installed its SIGTERM handler (it runs on a worker thread), so `kill`/logout killed Python and **orphaned the headless browser processes** | High | Linux (UI mode, both OSes in principle) |
| 7 | `mediapipe>=0.10` resolved to releases with no `mp.solutions` (≥ 0.10.30) or that **crash the process** (1.x) — gestures broken on any fresh install, all OSes | High | All |
| 8 | `PySide6-Essentials>=6.6` resolved to 6.12.0, which **aborts CPython 3.10/3.11** — including fresh Windows installs on the recommended 3.11 | High | All |
| 9 | Camera backend names were Windows-only (MSMF/DSHOW) | Low | Linux |
| 10 | `blueprint.md` described a layout that no longer existed (misleading to agents) | Low | Docs |
| 11 | ~90 unused imports and 3 invalid escape sequences (SyntaxWarning on 3.12+) outside automation | Low | Code health |
| 12 | Redundant `duckduckgo-search` dependency; dead `google.generativeai` import probe in setup | Low | Dependencies |

**Baseline result on Linux (Omarchy, Python 3.14), before any change:** after working around
the install failure, 18 of 23 unit suites passed; 5 failed (automation ×2 and voice-turn on a
missing `keyboard` module, hardware profile on the missing registry, setup runtime on GPU
detection disagreement).

## 4. What was changed from this baseline (summary)

Full detail, reasoning and verification: **[`UPDATE_V2.md`](UPDATE_V2.md)**.

- **New platform layer** `src/kayra/platforms/` (detect, windows, linux, hyprland, fallback);
  Windows code moved into it verbatim, shared modules call it instead of branching.
- **Linux / Omarchy support** for everything except desktop automation: hardware identity,
  active window, displays, reduced motion, file-manager reveal, gesture pointer, speech input
  via Google Chrome, POSIX signal-safe shutdown, Linux checks in `setup.py`.
- **Automation left untouched** and isolated: not installed or imported on Linux; Kayra says
  desktop control is unavailable; its tests report `n/a`.
- **Dependencies:** platform markers; `mediapipe>=0.10.14,<1.0` with a Tasks-API fallback;
  `PySide6-Essentials<6.12` on Python < 3.12; `duckduckgo-search` removed; supported Python
  range raised to 3.10–3.14.
- **Bug fixes** for problems 4, 5, 6, 7, 8, 9 above.
- **Cleanup:** `blueprint.md` deleted, unused imports and escape warnings fixed outside
  automation.
- **Documentation:** new `AGENTS.md`, `docs/architecture/`, `docs/platforms/`,
  `docs/development/`, a migration record and this `updates/` folder.
- **Tests:** new `tests/test_platforms.py`; runner gained per-platform suites and `--python`.
  Result: 0 failures on Linux for Python 3.10, 3.11, 3.12, 3.13 and 3.14.
