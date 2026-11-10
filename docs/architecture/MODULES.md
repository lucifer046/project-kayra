# Module Map

One line per module: what it owns, and what it must never do. "Leaf" means stdlib (plus the
named third-party libraries) and nothing above it in `kayra`.

## Entry points

| File | Owns |
|---|---|
| `run.py` | Launcher only. Locates/validates `.venv`, re-execs as a child, preflight, single-instance lock, `--doctor`. Stdlib only before re-exec. |
| `setup.py` | Environment provisioning only. Venv, `requirements.txt`, ONNX Runtime variant + CUDA wheels with a real session probe, `.env`, model checks, hand-model download, Linux system checks. Stdlib only; never imports `kayra`. |
| `main.py` | Compatibility shim → `kayra.app.main()`. |
| `src/kayra/__main__.py` | `python -m kayra` → `kayra.app.main()`. |

## `kayra.platforms` — OS abstraction (leaf, stdlib only)

| Module | Owns |
|---|---|
| `__init__.py` | The façade: `current()`, `backend()`, `active_window()`, `active_window_support()`, `reveal_command()`, `file_manager_label()`, `prefers_reduced_motion()`, `displays()`, `default_browser_hint()`, `pointer_injector()`. |
| `detect.py` | `PlatformInfo`: family, distro (`/etc/os-release`), Arch-based/Omarchy, session type, desktop, Hyprland, WSL. Pure `build_info()` for tests. |
| `windows.py` | pygetwindow active window, Explorer `/select` vector, `SystemParametersInfoW` animations, `EnumDisplaySettingsW` displays, UserChoice ProgId, `Win32Injector` (user32). |
| `linux.py` | Hyprland active window/monitors/animations, FileManager1 D-Bus or `xdg-open` vector, `/sys/class/drm` displays fallback, `mimeapps.list` default browser, `LinuxInjector` (Hyprland `movecursor` + uinput buttons/wheel). |
| `hyprland.py` | Hyprland request-socket client (`j/activewindow`, `j/monitors`, `getoption`, `dispatch movecursor`). Bounded timeouts, returns None on failure. |
| `fallback.py` | Any other OS: every capability "unavailable". |

## `kayra.core` — leaf utilities

| Module | Owns |
|---|---|
| `paths.py` | Every filesystem location, anchored on the repo root. |
| `config.py` | One cached parse of `.env`, exported to `os.environ`. |
| `runtime_state.py` | Assistant state machine, activity timestamps, turn bookkeeping, sync event bus. |
| `voice_state.py` | The single resolved voice/visual state with revisions and dwell. |
| `voice_control.py` | The local control vocabulary (exact whole-utterance match), confirmations for dangerous kinds, short-answer recovery. |
| `endpointing.py` | The utterance-boundary predicate and every threshold; the page is configured from it. |
| `conversation_context.py` | What the conversation is about (bounded, never persisted). |
| `hardware.py` | OS product/version, CPU name, GPU adapters, displays. Registry on Windows, os-release + sysfs on Linux, via `platforms` for displays. No subprocess. |
| `system_profile.py` | Device profile, live psutil metrics, System-screen findings and scores. |
| `logbus.py` | `[TIME] [LEVEL] [SUBSYSTEM] message`, redaction, turn correlation, optional rotating file. |
| `settings_log.py` | Announce + commit a setting change; `apply()` for transactional ones. |

## Capabilities

| Module | Owns |
|---|---|
| `intelligence/llm_engine.py` | `CentralizedLLMEngine` (singleton): DMM classification, chat streaming, identity prompt. No fallback logic. |
| `intelligence/provider_router.py` | All provider routing, failure classification, cooldowns, fallback logging. |
| `intelligence/emotion_engine.py` | Text-only mood estimate for tone. Never influences intent. |
| `intelligence/proactive_presence.py` | Contextual candidates and suppression rules for the proactive agent. |
| `input/speech_to_text.py` | Headless-browser Web Speech session, page server, capture, PID ownership, recovery. |
| `input/stt_backend.py` | Requested-vs-active speech backend and the live switch. Never imports the STT module. |
| `input/browsers.py` | Browser discovery (Windows paths / Linux PATH), default detection via `platforms`, capability priors, verified cache. |
| `input/transcript_repair.py` | Conservative N-best re-ranking; no word-replacement dictionary. |
| `input/gesture/*` | Camera, detector (solutions or Tasks API), features, filters, FSM, pointer, controller, config. |
| `output/text_to_speech.py` | Kokoro synthesis/playback, epoch cancellation, audible-window ledger. |
| `output/tts_device.py` | The only `onnxruntime` importer: DLL/.so preload, provider probe, AUTO/GPU/CPU, diagnostics, GPU telemetry. |
| `automation/policy.py` | `Action`/`ActionResult`, ALLOW/CONFIRM/DENY, confirmations, audit ring. Pure logic. **Frozen.** |
| `automation/targets.py` | Win32 target resolution, app/website registries, single-target close. **Frozen, Windows.** |
| `automation/windows.py` | Normalizer, planner, executor, every handler. **Frozen, Windows.** |
| `services/chatbot.py` | Conversational path with memory. |
| `services/real_time_search.py` | DuckDuckGo RAG path. |
| `services/deep_research.py` | Six-stage research agent, report to `Reports/`. |
| `services/proactive_agent.py` | Proactive observation, scoring, cooldowns, habit model, safety gate. |
| `memory/conversation.py` | The only writer of the conversation store (atomic). |
| `memory/store.py` | Stable ids, delete/clear, reveal location via `platforms.reveal_command()`. |
| `utils/console.py` · `timing.py` · `text.py` | Rich theme/print helpers · `StageTimer` · `speech_safe_text`, `SentenceStreamer`. |

## Orchestration and presentation

| Module | Owns |
|---|---|
| `app.py` | Bootstrap, listen/route loop, lifecycle (listening, standby, gestures), confirmations, shutdown. |
| `ui/application.py` | `QApplication`, window, ambient panel, tray, POSIX signal bridge, entry `main()`. |
| `ui/bridge.py` | The only QObject between backend callbacks and widgets. |
| `ui/session.py` | Threads, boot sequencing, the turn pipeline. No Qt. |
| `ui/controls.py` | Actions shared by dock, shortcuts and tray. |
| `ui/theme/` · `components/` · `views/` | Design tokens + stylesheet · reusable widgets · screens. |

## `__init__.py` files

All are kept. Most carry the package docstring (the module map for that package);
`utils/__init__.py` is the flat façade; `ui/theme/__init__.py` exports `apply()`;
`input/gesture/__init__.py` exports the controller API; `platforms/__init__.py` is the façade.
`ui/components/__init__.py` and `ui/views/__init__.py` are empty but keep those directories
**regular** packages: without them they become PEP 420 namespace packages, which import
correctly but are skipped by `pkgutil.walk_packages`-style tooling and can silently merge with a
same-named directory elsewhere on `sys.path`.
