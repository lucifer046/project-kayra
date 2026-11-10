# Architecture Overview

Kayra is a single Python process that turns speech (or typed text) into one of five kinds of
work and speaks the result. This page is the 5-minute map; [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md)
explains how the parts interact and [`../KAYRA_SYSTEM_ARCHITECTURE.md`](../KAYRA_SYSTEM_ARCHITECTURE.md)
holds the full design history.

## The shape

```
            ┌──────────────────────── kayra process ─────────────────────────┐
 microphone │  input/speech_to_text ──► app.Listen() ──► core/voice_control   │
 (headless  │  (Chrome/Edge Web Speech,      │            (local commands:     │
  browser)  │   VAD + endpointing)           │             stop, sleep, exit)  │
            │                                ▼                                │
 keyboard / │                    intelligence/emotion_engine (tone only)     │
 UI chat ──►│                                ▼                                │
            │                 intelligence/llm_engine  "the DMM"             │
            │                 (intent tokens; provider_router picks the LLM) │
            │                                ▼                                │
            │                       app.Execute_Task()                        │
            │   ┌──────────┬──────────────┬──────────────┬─────────────────┐  │
            │   ▼          ▼              ▼              ▼                 ▼  │
            │ chatbot  real_time_search deep_research  proactive on/off  automation
            │   │          │              │                         (Windows only)
            │   └──────────┴──────┬───────┘                                  │
            │                     ▼                                          │
 speakers ◄─│        output/text_to_speech (Kokoro ONNX, CPU or CUDA)        │
            │                                                                │
 camera ───►│  input/gesture/ (MediaPipe) ──► platforms pointer injector     │
            │                                                                │
            │  core/runtime_state (shared state + event bus) ◄── everything  │
            │  ui/ (PySide6) ◄── bridge signals ◄── ui/session (no Qt)       │
            └────────────────────────────────────────────────────────────────┘
```

## Layers (lowest first)

| Layer | Packages | May import |
|---|---|---|
| Platform | `kayra.platforms` | stdlib only |
| Core | `kayra.core`, `kayra.utils` | stdlib, `platforms`, third-party leaf libs |
| Capabilities | `intelligence`, `input`, `output`, `memory`, `services`, `automation` | core, platforms (automation must not import input) |
| Orchestration | `kayra.app` | everything above |
| Presentation | `kayra.ui` | app via `ui/session.py`; views never touch engines |
| Launch | `run.py`, `setup.py`, `main.py` | stdlib only (`run.py` imports `kayra` only after re-exec) |

## The five kinds of work

| DMM token prefix | Handler | Notes |
|---|---|---|
| `general …` | `services/chatbot.py` | streamed LLM chat with short-term + permanent memory |
| `realtime …` | `services/real_time_search.py` | DuckDuckGo results as RAG context |
| `deep research …` | `services/deep_research.py` | 6-stage agent, writes a report to `Reports/` |
| `proactive on/off`, `exit`, … | `app.Execute_Task` | assistant self-control |
| everything else | `automation/windows.py` | normalize → policy → resolve → plan → execute; **Windows only** |

Lifecycle commands (stop, stop listening, sleep, wake, exit, gestures/camera on/off) are matched
locally in `core/voice_control.py` **before** the DMM — no network, ~10 µs.

## Threads (steady state)

`MainThread` (console loop or Qt event loop) · local control watcher (60 ms poll of the STT page
while audio plays) · TTS synthesis/playback workers · proactive agent (one thread on an Event) ·
gesture camera + processor (only when enabled) · UI runner + listener (UI front end only).

## Platform support at a glance

| Capability | Windows | Linux (Omarchy/Hyprland) | Other Linux |
|---|---|---|---|
| Conversation, search, research, memory | ✅ | ✅ | ✅ |
| Speech output (Kokoro, CPU) | ✅ | ✅ | ✅ |
| Speech output on NVIDIA CUDA | ✅ | ✅ (setup provisions it) | ✅ |
| Speech input | Edge or Chrome | Google Chrome | Google Chrome |
| Desktop UI | ✅ custom chrome | ✅ native decorations | ✅ |
| Proactive context (active window) | ✅ pygetwindow | ✅ Hyprland IPC | time/habit signals only |
| Gesture pointer move | ✅ user32 | ✅ Hyprland IPC | uinput |
| Gesture click/scroll | ✅ | needs `/dev/uinput` access | needs `/dev/uinput` access |
| Desktop automation (apps, windows, hotkeys) | ✅ | ❌ not ported yet | ❌ |

See [`../platforms/CROSS_PLATFORM.md`](../platforms/CROSS_PLATFORM.md).
