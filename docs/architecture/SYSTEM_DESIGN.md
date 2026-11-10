# System Design

How Kayra's components interact, and the invariants that keep them correct. Each section links
to the section of [`CLAUDE.md`](../../CLAUDE.md) or
[`KAYRA_SYSTEM_ARCHITECTURE.md`](../KAYRA_SYSTEM_ARCHITECTURE.md) that records *why*.

## 1. Shared state and the event bus

`core/runtime_state.py` holds the one answer to "what is the assistant doing?" — `IDLE`,
`LISTENING`, `PROCESSING`, `SPEAKING`, `INTERRUPTING`, `AUTOMATING`, `SHUTTING_DOWN` — plus three
independent axes: `listening` (microphone open), `sleeping` (standby) and the shutdown event.
It also carries a minimal **synchronous** event bus (`emit(name, **payload)`); subscribers run on
the emitting thread. It is a process-wide singleton (`get_runtime_state()`), holds state and
never resources, and imports only the stdlib, so any thread may read it.

`core/voice_state.py` resolves those facts plus STT backend status and the page's VAD into the
single visual state the UI renders (11 states, monotonic revisions, short dwell). No view
composes a caption from raw flags.

## 2. Speech in

```
browser page (served from 127.0.0.1)          Python
getUserMedia(AEC, NS, AGC) ─► AnalyserNode VAD ─► endpoint decision (core/endpointing.py rules)
webkitSpeechRecognition (N-best=5) ─────────────► flushUtterance ─► capture() queue
                                                   ─► transcript_repair (re-rank, guarded)
                                                   ─► app.Listen()
```

- One browser session per process, owned by PID (`SpeechToTextEngine.owned_pids`); death is
  detected with psutil, recovery tears down before rebuilding.
- `input/browsers.py` chooses the browser: explicit `STT_BROWSER`, then the cached verified one,
  then the default (Linux: only if its backend is known), then Google/vendor backends, then
  unknowns (probed). Only success is cached.
- On Linux the driver is resolved with Selenium Manager `--skip-driver-in-path`, because Arch's
  `chromedriver` on PATH belongs to Chromium and does not match Google Chrome.
- Echo rejection is timestamp-based: capture windows vs the TTS engine's audible-window ledger.
- Barge-in ("stop", "wait", "hold") is detected on **interim** results by the local control
  watcher, which publishes `window.kayraSpeaking` in the same poll.

## 3. Intent

`intelligence/llm_engine.py` (`CentralizedLLMEngine`, singleton) sends the raw query with a large
few-shot preamble and parses comma-separated task tokens, accepting only prefixes in `self.funcs`
(single-match + dedup). Provider choice is entirely `intelligence/provider_router.py`:

```
local server up?  → LM Studio / Ollama, nothing else
DECISION (DMM)    → Claude → Groq → Gemini
CHAT              → Claude → Groq → Gemini
```

Eight failure kinds, per-provider cooldowns honouring `Retry-After`, at most one call per
provider per request, SDK retries disabled (`max_retries=0`), no blocking backoff, and streaming
may only fall back before the first chunk.

## 4. Speech out

`output/text_to_speech.py` owns the only sentence queue. `speak()` is non-blocking; synthesis
starts from the first clause; audio goes through one persistent `sd.OutputStream` in 40 ms
slices. `stop()` bumps an epoch, drains both queues and latches `_interrupted` until the next
`begin_turn()`. `output/tts_device.py` is the only `onnxruntime` importer: it preloads CUDA/cuDNN
libraries, probes providers on an 84-byte model, plans `[CUDA, CPU]` (TensorRT never), and
reports the device the live session is *actually* on.

## 5. Automation (Windows only, frozen)

`automation/windows.py` → `normalize_command()` → `policy.classify_action()` (ALLOW / CONFIRM /
DENY) → `targets.resolve_*()` → `plan_actions()` → `execute_action()` with verification. No
process is ever killed by name, no `shell=True`, power actions require an explicit target and a
confirmation. On Linux, the package does not import (its libraries are Windows/X11-bound);
`app.bootstrap()` catches that and substitutes a stub that answers "Desktop control isn't
available on this system yet." See [`../platforms/CROSS_PLATFORM.md`](../platforms/CROSS_PLATFORM.md#automation).

## 6. Proactive behaviour

`services/proactive_agent.py`: one thread, a slow tick, deterministic local scoring
(`0.30·habit + 0.25·temporal + 0.35·context + 0.10·recency − 0.5·annoyance` vs 0.60), three
cooldowns, a safety gate (`is_safe_window()`), and speech only through the shared TTS engine via
`begin_background_utterance()`. `intelligence/proactive_presence.py` adds tiered contextual
candidates (battery, pressure, repeated failures, greetings). The active-window signal comes
from `kayra.platforms` (pygetwindow on Windows, Hyprland IPC on Omarchy).

## 7. Gestures

`input/gesture/`: camera thread → single-frame mailbox → MediaPipe hands (legacy `solutions`
graph when present, Tasks `HandLandmarker` otherwise) → scale-normalised features → One Euro +
dead-zone filters → hysteresis FSM with fixed arbitration → `pointer.py` → the platform
injector (`user32` on Windows; Hyprland IPC + uinput on Linux). It never touches voice state.

## 8. The platform layer

`kayra.platforms` is the boundary between shared logic and the OS. Every backend
(`windows.py`, `linux.py`, `fallback.py`) exports the same functions; `platforms/__init__.py`
loads one lazily from `detect.current().family`. See
[`../platforms/CROSS_PLATFORM.md`](../platforms/CROSS_PLATFORM.md).

## 9. UI

```
views/ ──signals──► bridge.py (QObject, GUI thread) ──callbacks──► session.py (no Qt) ──► kayra.app
```

`session.py` runs the same turn pipeline as `app.Main_Loop`. Runtime events are marshalled to
the GUI thread by queued signals. On POSIX, SIGTERM/SIGINT/SIGHUP reach the authoritative
shutdown through a wakeup-fd + `QSocketNotifier` bridge in `ui/application.py`.

## 10. Persistence

| Data | Owner | Location |
|---|---|---|
| Conversation memory | `memory/conversation.py` (atomic: backup then primary) | `data/conversation.json` |
| Habits | `services/proactive_agent.py` (atomic tmp+replace) | `data/habits.json` |
| Verified speech browser | `input/browsers.py` (success only) | `data/browser_support.json` |
| Single-instance lock | `run.py` (`pid` + `create_time`) | `data/kayra.lock` |
| Deep-research reports | `services/deep_research.py` | `Reports/` |
| Rotating debug log (opt-in) | `core/logbus.py` | `logs/` |

All paths come from `core/paths.py`, anchored on the repository root on every OS.
