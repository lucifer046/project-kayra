# Kayra — Project Context for AI Agents

> **Pending work:** [`docs/roadmap/BROWSER_CONTROL_PLAN.md`](docs/roadmap/BROWSER_CONTROL_PLAN.md) —
> the planned full browser-control round. Implement it only when the user asks.
>
> **Start with [`AGENTS.md`](AGENTS.md)** — the concise rules, layout and workflow for every agent.
>
> **This file is deliberately short (split 2026-11-10).** It used to be ~250 KB and was loaded
> into every agent session. What stays here is what every change needs: the data flow, the
> cross-platform rules for the area under active work, the import and path rules, the module
> map and the style conventions. Every subsystem's long-form notes — the gotchas, each with
> the bug that taught it — moved VERBATIM to `docs/reference/`. **Read the matching reference
> before changing a subsystem, and update it (not this file) when you change what it
> describes.** Keep this file under ~45 KB.
>
> Depth beyond that: **`docs/KAYRA_SYSTEM_ARCHITECTURE.md`** (how and why each subsystem is
> shaped), **`docs/KAYRA_UI_ARCHITECTURE.md`** (the interface). User-facing docs: `README.md`
> and `docs/development/`.

## Reference index — read before touching the subsystem

| Changing… | Read first |
|---|---|
| The voice loop | [`docs/reference/VOICE_LOOP.md`](docs/reference/VOICE_LOOP.md) — Speech in, speech out: echo rejection, barge-in, endpointing, confirmations, the STT browser session, the voice state machine, TTS devices. |
| Intent classification and model routing | [`docs/reference/INTELLIGENCE.md`](docs/reference/INTELLIGENCE.md) — The DMM and its prompt contract, the provider router, retry bounds, the emotion engine. |
| The automation layer | [`docs/reference/AUTOMATION.md`](docs/reference/AUTOMATION.md) — Normalizer, policy, confirmations, target resolution, planner, the power-target boundary (the Windows-era detail; Linux rules stay in CLAUDE.md). |
| The desktop UI | [`docs/reference/UI.md`](docs/reference/UI.md) — The Qt boundary, shells, theme, the Qt traps already hit, shutdown from the UI. |
| Proactive service and presence | [`docs/reference/PROACTIVE.md`](docs/reference/PROACTIVE.md) — When Kayra may speak unprompted: scoring, cooldowns, the safety gate, the presence layer. |
| Hand gesture control | [`docs/reference/GESTURE.md`](docs/reference/GESTURE.md) — Camera, detector, features, filters, the gesture state machine, the pointer. |
| Launcher, setup and hardware detection | [`docs/reference/SETUP_AND_HARDWARE.md`](docs/reference/SETUP_AND_HARDWARE.md) — run.py / setup.py, ONNX Runtime provisioning, OS and GPU identity. |
| Runtime state, memory, logging, configuration | [`docs/reference/RUNTIME_MEMORY_LOGGING.md`](docs/reference/RUNTIME_MEMORY_LOGGING.md) — RuntimeState and its bus, the conversation store and its management, logbus, the .env groups. |
| Dev workflow and the test suites | [`docs/reference/DEV_WORKFLOW.md`](docs/reference/DEV_WORKFLOW.md) — Commands, the test tiers, what each suite covers, past test incidents. |
| Linux automation, the Chrome bridge | [`docs/platforms/LINUX_AUTOMATION.md`](docs/platforms/LINUX_AUTOMATION.md), [`docs/platforms/CHROME_BRIDGE.md`](docs/platforms/CHROME_BRIDGE.md) |
| Tests: commands, healthy output, failure symptoms | [`tests/TESTING.md`](tests/TESTING.md) |

## Run, check, test

```
python setup.py                          # once, or to repair the environment
python run.py [--console] [--doctor]     # every time; never activate the venv by hand
.venv/bin/python -m pyflakes src/kayra tests run.py setup.py main.py     # after moving files
.venv/bin/python tests/run_all.py        # the unit tier (~1 min); --integration, --live, --list
```

On Windows the interpreter is `.venv\Scripts\python.exe`. No pytest: each suite is its own
process. A tier-1 suite must not read the host (battery, load, `.env`, memory store) — see
[`docs/reference/DEV_WORKFLOW.md`](docs/reference/DEV_WORKFLOW.md). After a code change run
`graphify update .`.

## Measured facts that overturn older notes (2026-11-10)

- **The quantized Kokoro model is the SLOW one.** `models/kokoro.onnx` is byte-for-byte
  `kokoro-v1.0.int8.onnx` and synthesizes at roughly real time on CPU and GPU alike. The float
  graphs run at RTF ~0.22 on the processor and ~0.04 on the RTX 4060 (fp16: +637 MiB VRAM).
  `text_to_speech.VOICE_MODELS` is the preference order and holds the table; `setup.py` offers
  the fp16 file. Older text in the references saying "int8 is several times faster" or "CUDA
  is not a speed-up" describes the int8 graph only.
- **The local DMM is ~0.4 s warm** (gemma-4-e4b, 5.6K-token prompt, cached); the few-shot
  prompt already suppresses thinking. `_dmm_local` still sends `reasoning_effort: none` so a
  heavier thinking model cannot spend its 128-token budget reasoning and return nothing.
- **`classify_intent` has an exact-match fast path** (`llm_engine.fast_intent`): an utterance
  that IS a complete command ("pause", "next tab", "volume 40") never reaches a model. Whole
  utterance only, never a prefix — the same rule as `core.voice_control`. `DMM_FAST_PATH=off`.
- **`automation.ai.summarize` and local vision run with thinking off**: 10.1 s -> 2.1 s for a
  page summary, 7.8 s -> 1.8 s for a screen description on the reference laptop.
- **Browser flows wait for readiness, not for a clock**: `web_macros._until` / `_wait_for`.
  Do not add a fixed `SLEEP` in front of a page action.

## What this is

Kayra listens (voice via headless-Chrome Web Speech API, or typed input), classifies the
query's intent with a small LLM ("the DMM"), and routes it to conversation, live web search,
autonomous deep research, or direct Windows automation (open/close apps, media keys, window
management, system info, hotkeys, clipboard, screenshots, timers). It speaks responses back
with an offline Kokoro-ONNX TTS voice and supports voice barge-in (interrupting it mid-sentence).

## Architecture at a glance (data flow)

```
kayra.app Main_Loop():
  Listen()                                  [voice via src/kayra/input/speech_to_text.py, or keyboard]
    -> classify_control()                    [src/kayra/core/voice_control.py — LOCAL, pre-DMM]
         stop / wait / hold  -> barge-in     (audio layer; never reaches the classifier)
         stop listening      -> set_listening(False)
         go to sleep / wake  -> set_sleeping()
         exit / turn off Kayra -> request_shutdown()
         hand gesture on/off -> set_gesture_control()   [camera + pointer, never voice state]
         camera on/off       -> set_camera()
         nothing matched     -> fall through to the DMM below
    -> SemanticEmotionEngine.analyze_text()  [src/kayra/intelligence/emotion_engine.py]           -> mood
    -> CentralizedLLMEngine.classify_intent()[src/kayra/intelligence/llm_engine.py — "the DMM"]   -> [task tokens]
    -> Execute_Task() routes each token:
         "general ..."       -> src/kayra/services/chatbot.py             conversational LLM + memory
         "realtime ..."      -> src/kayra/services/real_time_search.py    DuckDuckGo RAG + LLM
         "deep research ..." -> src/kayra/services/deep_research.py       6-stage autonomous research agent
         "proactive on/off"  -> src/kayra/services/proactive_agent.py    session switch for unprompted speech
         everything else     -> src/kayra/automation/windows.py  system/app/hotkey automation ("hands")
              normalize_command() -> Action        [structured; the sentence is parsed ONCE]
              classify_action()   -> ALLOW/CONFIRM/DENY        [src/kayra/automation/policy.py]
              resolve_*()         -> Resolution                [src/kayra/automation/targets.py]
              plan_actions()      -> ordered groups
              execute_action()    -> ActionResult + verification -> spoken sentence
    -> src/kayra/output/text_to_speech.py speaks the result (Kokoro ONNX, streamed sentence-by-sentence)
         -> src/kayra/output/tts_device.py picks the execution provider (AUTO / GPU / CPU)
    -> RUNTIME.emit(...)                                       [src/kayra/core/runtime_state.py event bus]
```

Two more subsystems run independently of that loop:
- **the local control watcher** (`app.py::_local_control_watcher`, aliased `_barge_in_watcher`)
  — daemon thread polling the STT page every 60ms while audio is playing, for BOTH interrupt
  words and lifecycle commands, and acting on them from outside the main loop. It has to live
  outside the loop: while a response is generating and speaking, `Main_Loop` is blocked inside
  `Execute_Task` and cannot poll the microphone at all. It also PUBLISHES `window.kayraSpeaking`
  in the same round-trip, which is what makes "stop" reliable — see the barge-in section.
- **`src/kayra/services/proactive_agent.py`** — ONE daemon thread sleeping on an Event, waking on a slow
  tick to look for a reason to speak. It reads assistant state from the shared runtime and
  never writes it. Full design below.

A third runs alongside them and shares nothing with them but the event bus:
- **`src/kayra/input/gesture/`** — hand gesture control. Two threads of its own (`kayra-camera`
  capturing into a single-frame mailbox, `kayra-gesture` processing the newest frame), its own
  two switches, and its own runtime state. It EMITS `gesture_state` on the runtime bus and
  writes nothing: camera activity must never be able to pause the microphone or move the
  assistant state machine. Off by default; started by the Home toggles, the Settings card, the
  spoken "turn on hand gesture control", or `GESTURE_ENABLED` in `.env`.

All of them read the shared assistant state from **`src/kayra/core/runtime_state.py`**, which is where
"what is the assistant doing right now?" lives. It used to be a module-global in the
orchestrator; it had to move the moment a second thread needed a truthful answer to that
question, because a global in `app.py` cannot be read without importing `app.py` and re-running
its boot.

## Cross-platform: Windows and Linux (added 2026-11-09)

Kayra now runs on Linux, with **Omarchy (Arch + Hyprland)** as the first-class target. Full
strategy: `docs/platforms/CROSS_PLATFORM.md`; per-OS detail in `docs/platforms/`.

- **`kayra.platforms` is the OS boundary** — `detect.py` (family, distro, Omarchy, session,
  Hyprland), `windows.py`, `linux.py`, `fallback.py` (identical function names),
  `hyprland.py` (IPC socket client, never `hyprctl`). A stdlib-only leaf BELOW `core`:
  `core.hardware` may import it, it imports nothing else from `kayra`
  (`tests/test_platforms.py` and `test_hardware_profile.py` assert this). New OS questions go
  there, never into a new `if sys.platform` branch in a shared module.
- **Windows code moved verbatim, behaviour unchanged.** `pointer._Win32Injector` and
  `store.explorer_command` remain as aliases into `platforms.windows`. `hardware.py` gates its
  registry paths on `winreg` being importable rather than on `sys.platform`, so the test suite
  can serve synthetic Windows machines on Linux with a stub `winreg`.
- **Automation runs on both OSes (ported 2026-11-09).** `automation.load_backend()` picks
  `automation.windows` (Win32) or `automation.linux` (Hyprland IPC + wtype, wpctl,
  brightnessctl, grim, wl-clipboard, nmcli, bluetoothctl, playerctl/MPRIS-over-gdbus). The
  normalizer, power-target gate, planner, timers, file handlers and the `AutomationRuntime`
  executor live in `automation.common` and are shared; `windows.py` re-exports them under
  their old names (its own executor is unchanged). Linux apps resolve from XDG desktop entries
  (`automation.linux_apps`) by EXACT identity only. Full guide and roadmap:
  `docs/platforms/LINUX_AUTOMATION.md`. Tests: `tests/test_linux_automation.py` (fake
  compositor + recording runner); the two Win32 suites are still `n/a:win32` on Linux.
- **Round 2 (2026-11-10) — see the changelog in `docs/platforms/LINUX_AUTOMATION.md`.**
  The shell policy now DENIES stopping/disabling/masking ANY service, `kill`/`pkill`,
  critical-package and forced removals, recursive chmod/chown of system paths or `~`,
  `rm -rf ~`, and kernel/boot/firewall/mount/user-management tools; inline code (`-c/-e/-m`)
  and network kill-switches are CONFIRM. Clicking by visible text is OCR (`grim` +
  `tesseract`) on the focused monitor, never inside Kayra's own window, CONFIRM-gated for
  risky words; real clicks need `/dev/uinput`. "system undo …" pops Kayra's own bounded undo
  stack (10 actions, 5 min) — a bare "undo" stays Ctrl+Z. "an empty workspace" is always the
  LOWEST-numbered one with no window. Battery state comes from sysfs
  (`platforms.battery_status()`).
- **Round 3 — assistant features (2026-11-10).** New modules under `automation/`: `ai` (bounded model
  calls: `summarize` with a deterministic fallback, `see` = vision, local LM Studio first),
  `diagnostics`, `browser_bridge` + `extension/kayra-bridge` (Chrome MV3 extension over
  ws://127.0.0.1:17893, accepted ONLY from Origin chrome-extension://gdllbbphfpgfcgmpjjklkkkpebimeeel),
  `web_macros` (tabs/pages/Gmail/Calendar/WhatsApp/Meet), `screen_ai`, `workflows` (scenes,
  planner, triggers, the ONE `kayra-watch` thread, briefing, recall, call detection),
  `linux_system`, `status_file` (+ `extension/omarchy-kayra-widget`). Rules that matter:
  * Models REWORD, SEE, or PROPOSE; they never AUTHORISE. Planned steps and the
    understanding layer's commands are validated by `normalize_command` + `classify_action`
    (DENY dropped, CONFIRM still asks) and run through the same executor; `run_steps` parks
    the rest of a plan behind a confirmation.
  * **The understanding layer** (`automation/interpreter.py`, 2026-11-10): when the rules
    cannot parse a token, only guessed (`browser <anything>` as a click), or ran and found
    NOTHING (NOT_FOUND/UNSUPPORTED — nothing happened), `AutomationRuntime` asks a model what
    the user meant, with the screen context (`linux.situation()`: app/web app in front, Chrome's
    active tab) and `interpreter.CATALOG`. Replies: automation commands / chat (the app answers
    via `take_chat_handoff()` → Chatbot) / ask one question / none. Never accepted from it:
    shell, plans, triggers, power, file delete/move/rename. Its own proposals are never
    re-interpreted (one model call per request). Unknown names: rules → model → web result.
    Only the failure path pays the call, and it is `ai.fast()`: local model, THINKING OFF (`reasoning_effort: none`), temperature 0 — ~0.5 s instead of ~4 s on gemma-4-e4b, because ~85 % of a thinking model's output there is hidden reasoning. Keep the worked examples in `_SYSTEM`; accuracy without thinking depends on them (12/12 measured). `interpreter.ACTIVE` is switched off by every test
    rig; `AUTOMATION_AI_INTERPRETER=off` disables it. Parse model JSON with `ai.complete`
    (verbatim) — `ai.summarize` strips code fences and used to swallow ```json replies.
  * Every new `system …` phrase sits BEFORE the power needles in `_SYSTEM_VERBS` ("sleep timer",
    "restart the docker container" must never reach the power gate). Asserted in test_assistant_automation.
  * Background speech goes through `common.announce` → `app.speak_unprompted`, which holds
    non-urgent lines during a turn, a call (another app recording the mic) or standby.
  * `linux.start_services()` is the only thing that starts the bridge, watcher, notification
    log (Kayra's one kept child process, reaped by handle) and status file — called from
    `app.bootstrap`, never at import. Teardown is in `RUNTIME.shutdown()`.
  * DMM tokens are split on commas: no new command may need a comma inside it (the preamble
    says so; use "saying" / ":").
  * With Chrome in front and the extension connected, `mouse.click_text` / scroll go INTO the
    page (`linux._browser_in_front`), never to OCR. Gmail rows are opened by the extension's
    `gmail.open`, not by a click on a name. A new extension command needs a manifest version
    bump AND `web_macros.REQUIRED_VERSION`; an older extension answers "unknown command",
    which `_call` turns into the spoken "reload the extension" instruction.
  * Tests: `tests/test_assistant_automation.py` (unit, 326), `tests/test_browser_extension.py` (integration,
    real headless Chrome). Guide: `docs/platforms/LINUX_AUTOMATION.md`, `docs/platforms/CHROME_BRIDGE.md`.
- **Round 5 — browser control, in parts (2026-11-10).** Plan and part table:
  `docs/roadmap/BROWSER_CONTROL_PLAN.md` §10. Part 1 done: `automation/sites.py` is the site
  table (`targets.WEBSITE_REGISTRY` is a view; folder names are never sites; "spotify" and
  "whatsapp" stay app names); "open X" = app → Omarchy web app → site → top web result →
  search; `common._split_where` + `merge_tab_steps`; web-app windows are browser surfaces.
  `web_macros.TOP_RESULTS` does network I/O — every test rig must stub it (the Linux `Rig`
  does). A new extension feature must check `_outdated(min)`; 1.1.0+ extensions reload
  themselves. Tests: `tests/test_browser_control.py`.
- **Hyprland 0.55+ dispatch is LUA.** `dispatch closewindow …` is answered with an error
  STRING, not a refused connection, so "got a reply" is not success. Always go through
  `platforms.hyprland.dispatch(lua, legacy)`, which tries `hl.dsp.*` first, falls back, and
  counts only a literal `ok`. The old `movecursor` call had silently stopped moving the
  gesture pointer on 0.56.
- **Speech input on Linux = Google Chrome.** Two traps, both fixed: (1) Arch's `chromedriver`
  on PATH belongs to Chromium and Selenium Manager prefers a PATH driver even when its major
  version mismatches, so Chrome failed to start and Kayra fell back to Chromium —
  `SpeechToTextEngine._matching_driver` resolves the driver with `--skip-driver-in-path` on
  Linux; (2) Omarchy's DEFAULT browser is Chromium, which starts a recognition session without
  error and never transcribes — on Linux an `unknown`-backend default no longer outranks an
  installed Google Chrome (otherwise the probe passes and Chromium is cached as "verified").
- **MediaPipe ≥ 0.10.30 removed `mp.solutions`**, and those are the only wheels for Python
  3.13+. `detector.py` uses `solutions` when present and a Tasks `HandLandmarker` adapter
  (`_TasksHands`, same `process()` contract) otherwise, reading `models/hand_landmarker.task`
  (setup downloads it). **Every mediapipe 1.x SIGKILLs** during HandLandmarker construction on
  the reference host, independent of Kayra — hence `mediapipe<1.0`.
- **Signals in the UI.** `bootstrap()` runs on the session worker thread in the desktop UI, so
  its `signal.signal` calls silently failed and SIGTERM killed Python without reaping the
  speech browser. `ui/application.py::_install_signal_bridge` (POSIX only) uses
  `signal.set_wakeup_fd` + `QSocketNotifier` — no timer — to run `bridge.shutdown(hard=True)`.
- **Linux hardware identity** comes from os-release, `/proc/cpuinfo`, sysfs PCI (class 0x03),
  `/proc/driver/nvidia`, `pci.ids` and amdgpu `mem_info_vram_total`; NVIDIA VRAM is unknown in
  sysfs (0, rendered "unknown"). An AMD APU's graphics are named from the CPU brand string.
- **Gesture pointer on Linux**: Hyprland `dispatch movecursor` (logical coordinates, focused
  monitor origin) + uinput (python-evdev) for buttons/wheel. `/dev/uinput` is root-only by
  default; without access the pointer moves and clicks are reported unavailable once.
- **PySide6 6.12.0 crashes CPython 3.10/3.11** (`Fatal Python error: none_dealloc` while the
  orb paints) — a shiboken refcount bug that 3.12+'s immortal `None` masks. `requirements.txt`
  caps it `<6.12` for `python_version < "3.12"`; 6.11.3 and 6.10.3 pass `test_ui.py` there.
- **`--no-voice` only skips the UI listener thread** — `bootstrap()` still starts STT (mic) and
  TTS (speaks the boot line). Do not use it as a "silent" test mode.
- **Python 3.10–3.14** resolve on Windows and Linux; the unit tier was run on Linux for each.
  3.15 is unreleased. See `docs/architecture/DEPENDENCIES.md`.

## Package layout and import rules

```
src/kayra/
├── app.py            orchestrator
├── platforms/        OS abstraction: detect, windows, linux, hyprland, fallback
│                     ← stdlib only; imports NOTHING from kayra (below core)
├── core/             paths, config, runtime_state, voice_state, conversation_context,
│                    voice_control, endpointing, hardware, system_profile, logbus,
│                    settings_log
│                     ← imports nothing from the rest of kayra
├── intelligence/     llm_engine (DMM), provider_router, emotion_engine, proactive_presence
├── input/            speech_to_text, stt_backend, transcript_repair, browsers,
│                    gesture/ (package: camera, detector, features, filters,
│                              state_machine, pointer, controller, config)
├── output/           text_to_speech, tts_device
├── automation/       windows (hands), policy (safety), targets (resolution)
├── services/         chatbot, real_time_search, deep_research, proactive_agent
├── memory/           conversation (persistence), store (management)
├── utils/            console, timing, text  (+ a flat façade in __init__)
└── ui/               desktop interface: theme/ components/ views/ + bridge, session,
                     controls (the action layer every control surface calls)
```

- **`core` is a leaf** (above `platforms`, the only thing it may import from `kayra`).
  Anything may import it; it imports nothing back. That is what makes
  `runtime_state` safe for the proactive thread and `paths` safe for everything else.
- **`automation` must never import `input`.** Importing `speech_to_text` boots a headless
  Chrome as a side effect, and the user asking to close a window must not start a browser. The
  resolver reads STT ownership out of `sys.modules` by string instead — see the ownership note.
  This is also why hand gesture control lives in `input/gesture/` and injects pointer events
  through 25 lines of `user32` rather than through `automation.windows`: it is a capture device
  that happens to drive the mouse, and a 30Hz pointer update must not pay for the
  normalize/policy/resolve/plan/execute pipeline a spoken sentence needs.
- **Modules inside `kayra.utils`, `kayra.core` and `kayra.memory` import from the SUBMODULES
  directly** (`from kayra.utils.console import ...`), never from the `kayra.utils` façade.
  Importing a package from one of its own members is how import cycles start.
- **The old 3-way import fallback is gone.** It existed so files could run standalone from a
  flat `modules/` folder. Use plain absolute imports (`from kayra.core.paths import ...`).
- **No import-time side effects anywhere.** Importing `kayra` or `kayra.app` must not start a
  model, open a browser or touch the microphone. `bootstrap()` does that, and only when called.
  The test suites depend on this to import and inspect modules.

## Path rules

**`src/kayra/core/paths.py` is the single source of truth. Nothing else may guess.**

- The repo root is computed ONCE from that file's own location. Every other path derives from
  it, so a file moving to a different directory depth cannot silently change the answer.
- **Never use a bare relative path** like `"data\conversation.json"`. Those resolve against the
  CURRENT WORKING DIRECTORY, and launching Kayra from outside the project folder silently
  fragmented the assistant's memory across several files.
- Use `data_path()`, `model_path()`, `logs_dir()`, `reports_dir()`, `conversation_paths()`.
- The layout is anchored on the REPO root (the directory holding `run.py`), not the package,
  because `models/`, `data/`, `logs/` and `Reports/` are user data that live beside the code.
- `get_project_root()` remains as an alias for the historical name.

## Module map

| Module | Responsibility |
|---|---|
| `src/kayra/app.py` | Orchestrator ONLY: boot sequence, listen/route loop, event emission, lifecycle, shutdown. Domain logic belongs in the service that owns it — the proactive branch in `Execute_Task`, for instance, flips a service switch and says so, it does not implement the policy |
| `src/kayra/core/paths.py` | The single source of truth for every filesystem location. Imports only the stdlib |
| `src/kayra/core/endpointing.py` | THE utterance boundary — one pure `decide()` that says whether the user has finished speaking, from acoustics and timing only. Every threshold lives here and the recognition page is configured FROM it, so the JS and the Python cannot drift. A LEAF: stdlib only |
| `src/kayra/core/hardware.py` | THE machine's identity: OS product/version/build (corrected for the stale `ProductName`), the processor's real branded name, and every graphics adapter with its PCI vendor, its 64-bit VRAM and its driver. Registry-read, 0.4 ms, no subprocess. A LEAF: stdlib plus optional psutil |
| `src/kayra/core/logbus.py` | THE structured log format — `[TIME] [LEVEL] [SUBSYSTEM] message`, canonical subsystem names, level threshold from `KAYRA_LOG_LEVEL`, credential redaction on every line, turn correlation, optional rotating file log. Renders through `utils.console.safe_print`, so there is still one Console. Leaf: stdlib + `core.paths` only |
| `src/kayra/core/settings_log.py` | The ONE place a setting change is announced and committed. `record()` for a plain change, `apply()` for one that does runtime work — request, run, verify, commit — so a failed change is never reported as a success. Refuses to print the value of a credential |
| `src/kayra/core/voice_state.py` | `VoiceStateMachine` — the authoritative answer to "what should the user be told about the microphone right now?", resolved from facts, with monotonic revisions and a short dwell. Leaf: stdlib only |
| `src/kayra/core/conversation_context.py` | What the conversation is currently ABOUT: recent turns, the last intent, automation targets, an outstanding question, the topic's content words. STATE, never persisted; a leaf, stdlib only. Read by the transcript repair stage and the presence layer |
| `src/kayra/input/transcript_repair.py` | The LAST stage of the capture pipeline: re-ranks the recognizer's own N-best against the conversation context, with one tightly-guarded phonetic step. No dictionary, no model, and it can never invent a shutdown |
| `src/kayra/core/voice_control.py` | The LOCAL control vocabulary — barge-in, listening pause/resume, standby, shutdown, and hand gesture control / camera on-off — matched exactly, before the DMM, with no network and no model. Leaf module: imports only the stdlib plus a lazy `core.config` read for the assistant's name |
| `src/kayra/output/tts_device.py` | THE ONNX Runtime layer, and the only module in `src/` that imports `onnxruntime`. CUDA/cuDNN DLL preparation (`preload_dlls`), verified provider probing against an 84-byte model, AUTO/GPU/CPU selection, structured `RuntimeDiagnostics`, and self-parking GPU telemetry. TensorRT is discoverable but never planned. Never claims a device the live session is not on |
| `src/kayra/core/config.py` | ONE cached parse of `.env`, exported into `os.environ` by `load_environment()`. Before this, eight modules each parsed it at import time with their own guess at the project root |
| `src/kayra/memory/conversation.py` | Long-term conversation persistence, and the ONLY writer of the store (atomic: backup written first, then copied over the primary) |
| `src/kayra/intelligence/llm_engine.py` | `CentralizedLLMEngine` — local-vs-cloud model selection, the DMM intent classifier, chat streaming, identity/system prompt. Singleton (see below). It no longer contains ANY fallback logic: every provider decision goes through the router |
| `src/kayra/intelligence/provider_router.py` | THE provider routing authority. Two ordered chains (DECISION, CHAT), eight failure kinds, per-provider cooldowns honouring `Retry-After`, sequential fallback with at most one call per provider per request, and the provider/fallback log lines. Imports no SDK and starts no thread |
| `src/kayra/input/stt_backend.py` | The authoritative speech-input backend state (`requested` vs `active`) and the live switch. Reads the engine out of `sys.modules` and NEVER imports it — importing starts a browser |
| `src/kayra/memory/store.py` | Memory MANAGEMENT over `memory.conversation`: stable per-record ids, delete-one, clear-all, the store path, and revealing it in File Explorer. Owns no persistence of its own |
| `src/kayra/services/chatbot.py` | General conversational path: memory-augmented chat |
| `src/kayra/services/real_time_search.py` | Live DuckDuckGo web search RAG path |
| `src/kayra/services/deep_research.py` | Multi-stage autonomous research report generator (saves to `Reports/`) |
| `src/kayra/automation/windows.py` | The "hands": every handler that touches the machine, plus the normalizer, planner and executor that drive them |
| `src/kayra/automation/policy.py` | `Action` / `ActionResult`, the ALLOW-CONFIRM-DENY policy for actions and shell commands, the confirmation manager, the bounded automation context, and the audit log. Pure logic — no hardware, no model |
| `src/kayra/automation/targets.py` | Target resolution over Win32: typed targets (`TargetType`), the canonical application registry, the canonical **website** registry, installed-app availability, the open-target pipeline, ranked matching, `pick_single` (the one-intent-one-target rule), ambiguity detection, Kayra-owned PID exclusion, focus/close primitives and their verification |
| `src/kayra/input/browsers.py` | Which browser runs speech recognition: discovery, default-browser detection, capability priors and the verified-working cache. Launches nothing |
| `src/kayra/input/speech_to_text.py` | Headless browser Web Speech API STT (Chrome / Edge / any Chromium build with a speech backend): managed single browser session (state machine, in-place recovery, PID-scoped teardown), loopback page server for a secure context, capture timestamps, interim-result interrupt detection |
| `src/kayra/output/text_to_speech.py` | Kokoro-ONNX offline TTS: epoch-cancellable synthesis/playback pipeline, persistent audio stream, audible-window ledger for echo rejection |
| `src/kayra/intelligence/emotion_engine.py` | Multi-signal mood estimator: weighted lexicon + structure + context, confidence-aware fusion, false-positive damping. Text-only by design (see below). 14.3us per call, no threads, no audio, no persistence |
| `src/kayra/core/runtime_state.py` | `RuntimeState` — thread-safe assistant state, user-activity timestamps, turn bookkeeping and a minimal synchronous event bus. Process-wide singleton via `get_runtime_state()`. Holds STATE, never RESOURCES; imports nothing but the stdlib, so anything may import it. |
| `src/kayra/services/proactive_agent.py` | Proactive suggestion service: cheap observation, local scoring, cooldowns, habit model, safety gate. Decoupled from the engines — it takes `speak_fn`/`is_speaking_fn`/`phrase_fn` callables; `create_default_agent()` does the real wiring |
| `src/kayra/input/gesture/` | Hand gesture control. `camera.py` is THE capture owner (one `VideoCapture`, one thread, a single-frame mailbox); `detector.py` is the only hand-graph construction site; `features.py` normalizes every distance by hand scale; `filters.py` is One Euro + dead-zone + outlier gate + speed ceiling; `state_machine.py` is the temporal FSM and the action arbitration; `pointer.py` is the only thing that touches the desktop; `controller.py` owns the lifecycle, the preview and the telemetry; `config.py` holds every threshold |
| `src/kayra/utils/` | Split by responsibility: `console.py` (Rich theme, logger, print_* helpers, the UTF-8 stream fix), `timing.py` (`StageTimer`, `now_ms`), `text.py` (`speech_safe_text`, `SentenceStreamer`, `answer_modifier`). `__init__.py` is a flat façade over the three |
| `tests/*.py` | Manual diagnostic entry points, **not** an automated pytest suite — run each directly. `test_audio_pipeline.py` (barge-in, echo rejection, speech normalization), `test_stt_lifecycle.py` (session reuse, recovery, process ownership), `test_dmm_matrix.py` (intent-boundary accuracy), `test_voice_control.py` (the local control vocabulary, the Kayra-vs-computer shutdown boundary, tail matching, JS/Python agreement, shutdown order and idempotency), `test_tts_device.py` (ONNX Runtime, CUDA verification and provider truthfulness), `test_environment.py` (launcher interpreter ownership, import origin, setup provisioning), `test_proactive_agent.py` and `test_emotion_engine.py` assert and exit non-zero; `test_barge_in_live.py` needs a human to speak |

## Style conventions already in place

- **Plain absolute imports** (`from kayra.core.paths import project_root`). The old 3-way
  fallback (`from .utils` -> `from modules.utils` -> `from utils`) is GONE — it existed so files
  could run standalone out of a flat `modules/` folder, and the package makes it unnecessary.
  Don't reintroduce it.
- **Modules inside `kayra.utils`, `kayra.core` and `kayra.memory` import from the submodules
  directly**, never from the `kayra.utils` façade — importing a package from one of its own
  members is how import cycles start. Everything else may use the façade.
- **No import-time side effects.** Importing a module must not start a model, a browser, a
  thread or a network client.
- Stdlib imports belong at module top-level, not re-imported inline inside functions on every
  call — several of these were cleaned up 2026-09-03 (`re`/`threading` in
  `automation/windows.py` and `output/text_to_speech.py`); don't reintroduce the pattern.
- **Never call `keyboard.press_and_release()` directly in the automation layer.** Use
  `automation.windows.send_keys()`, which normalises the modifier state first. See the
  stuck-modifier note above for the failure it prevents.
- **Never build a shell command by string concatenation, and never pass `shell=True`.** Use an
  argument vector with `shell=False`. The test suite AST-asserts both.
- **Never terminate a process by name.** PID-scoped only. AST-asserted.
- **New code logs through `kayra.core.logbus`**, with a `Subsystem.X` constant — never a
  literal string, and never a second layer reporting an event its owner already reported.
  `kayra.utils`'s `print_info` / `print_success` / `print_warning` / `print_error` /
  `print_system` remain valid for existing call sites and are still never bare `print()`.
- **Every setting change goes through `kayra.core.settings_log`.** One announcement per
  change, from one place; a change that does runtime work uses `apply()` so a failure cannot
  be reported as a success.
- **No view resolves a voice state.** It renders what `voiceStateChanged` gives it and drops
  anything whose revision is not newer.
- **A view built before the backend is ready must connect `bootFinished` and re-read.** An
  event-driven control is never corrected for a value that never changed, so a pre-boot read
  is latched forever. Never report "not booted yet" as a negative fact.
- **A tier-1 test must not read the host.** No real battery, no real CPU load, no real `.env`,
  no real memory store — pin them. A suite whose verdict depends on the charge level or the
  developer's configuration is not a suite anybody can trust, and both mistakes were made here
  before they were caught.
- **All paths through `kayra.core.paths`.** No bare relative paths, no re-deriving the project
  root with nested `os.path.dirname` calls.
