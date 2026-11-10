# Execution Flow

## 1. Launch — `run.py`

```
python run.py [--console] [--doctor] [--force] [--no-voice]
│
├─ PHASE 1 (whatever interpreter the user typed; stdlib only)
│   ├─ sys.prefix is not .venv?
│   │   ├─ .venv missing/broken ──► print "run setup.py", exit 1
│   │   ├─ KAYRA_RELAUNCHED already set ──► refuse (relaunch loop guard)
│   │   └─ spawn CHILD: <.venv python> run.py <args>, ignore SIGINT/SIGBREAK, wait, return its code
│   │        (a child, not os.execv: on Windows execv returns the console to the prompt)
│
└─ PHASE 2 (inside .venv)
    ├─ preflight(): sys.path gets src/, kayra.__file__ must be under src/ (no stale installs)
    ├─ --doctor ──► interpreter, ORT package/providers, verified CUDA, GPU telemetry, exit
    ├─ single-instance lock data/kayra.lock = (pid, create_time); --force skips it
    └─ --console ? kayra.app.main() : kayra.ui.application.main()
                                       (falls back to console if PySide6 is missing)
```

Venv interpreter: `.venv\Scripts\python.exe` (Windows) / `.venv/bin/python` (Linux).

## 2. Boot — `kayra.app.bootstrap()`

| Stage | What | Notes |
|---|---|---|
| 0 | `load_environment()`, quiet third-party loggers, subscribe voice-state | `.env` parsed once |
| 1 | start `kayra-boot-tts` and `kayra-boot-stt` threads | Kokoro session + headless browser in parallel |
| 2 | import services, construct `CentralizedLLMEngine` | overlaps stage 1; automation import failure is isolated here |
| 3 | join both boot threads, report boot errors | nothing runs against a half-initialised engine |
| 4 | start proactive agent, optional gestures | returns immediately; first tick one interval later |
| 5 | `_report_startup()`, install signal handlers (main thread only) | every value read from the live subsystem |

The UI front end calls the same `bootstrap()` from `ui/session.py` on its runner thread after
the first paint; its POSIX signal handling is installed separately on the GUI thread.

## 3. One turn

```
Listen()                                    (opens the turn at the commit point)
 ├─ capture() ── endpoint decided in the page (VAD AND recognizer quiet; continuation evidence)
 ├─ echo gate (_is_self_echo) ── pending lifecycle confirmation answered FIRST
 ├─ transcript repair (N-best re-rank; tightly guarded phonetic step; never invents shutdown)
 ├─ classify_control() ── stop / stop listening / sleep / wake / exit / gestures / camera
 │      dangerous kinds (shutdown, sleep) ask for confirmation; nothing else reaches the DMM
 └─ return text
Main_Loop / session._run_turn
 ├─ automation confirmation pending? answer it (exact "yes"/"no")
 ├─ emotion_engine.analyze_text() ── mood for tone only
 ├─ llm_engine.classify_intent() ── tokens (retries bounded by count, budget, per-attempt timeout)
 └─ Execute_Task(tokens)
      general → Chatbot (streams sentences into TTS)
      realtime → RealTimeSearchEngine
      deep research → DeepResearchEngine
      proactive on/off, exit → app
      other → Automation()  (Windows) | stub sentence (Linux)
 finally: RUNTIME.end_turn(), logbus.end_turn(), state LISTENING
```

## 4. Background loops

- **Local control watcher** — while audio plays, polls the page every 60 ms for interrupt
  flags and VAD in one round-trip; calls `tts.stop()` on barge-in.
- **Proactive agent** — wakes on a slow tick, scores candidates locally, speaks only through
  the safety gate.
- **Gesture** — camera thread fills a one-slot mailbox; processor thread infers, filters, and
  drives the pointer.
- **GPU telemetry** — self-parking `nvidia-smi` sampler, only when an NVIDIA GPU exists.

## 5. Shutdown — `app.request_shutdown()` (the only path)

Callers: spoken "exit"/"turn off Kayra" (after confirmation), the UI's Shut down button, the
tray's Quit, the DMM `exit` token, and signals (`SIGINT`/`SIGTERM`/`SIGBREAK` in console mode;
`SIGTERM`/`SIGINT`/`SIGHUP` via the Qt bridge in UI mode on POSIX).

1. announce (`shutdown_event`), cancel active work (stale DMM retries stop)
2. state → `SHUTTING_DOWN` (busy; nothing unprompted may speak)
3. stop the proactive agent; 3b. stop gestures (camera released, buttons lifted)
4. cancel automation timers
5. silence speech (optional blocking farewell first, for confirmed shutdowns)
6. tear down the STT browser by PID; 7. verify owned PIDs are gone
8. run UI pre-exit hooks (window + tray hide last)
9. `os._exit(code)` — deliberately, after everything above

Idempotent: concurrent callers are serialised by an Event + lock.
