# AGENTS.md — Kayra

The entry point for any AI coding agent (Claude Code, Codex, Cursor, OpenCode, …) working in
this repository. Read this first; follow the links when you need depth.

| You need | Read |
|---|---|
| Rules, conventions, what not to touch | **this file** |
| The always-needed context: data flow, cross-platform rules, imports, paths, module map | [`CLAUDE.md`](CLAUDE.md) (short; loaded every session) |
| Every subtle gotcha of ONE subsystem, with the bug that taught it | [`docs/reference/`](docs/reference/) — indexed at the top of `CLAUDE.md`; read the one you are changing |
| How the system fits together | [`docs/architecture/OVERVIEW.md`](docs/architecture/OVERVIEW.md) → [`SYSTEM_DESIGN.md`](docs/architecture/SYSTEM_DESIGN.md) |
| Startup / turn / shutdown sequence | [`docs/architecture/EXECUTION_FLOW.md`](docs/architecture/EXECUTION_FLOW.md) |
| What each module owns | [`docs/architecture/MODULES.md`](docs/architecture/MODULES.md) |
| Packages, versions, Python support | [`docs/architecture/DEPENDENCIES.md`](docs/architecture/DEPENDENCIES.md) |
| Windows / Linux / Omarchy specifics | [`docs/platforms/`](docs/platforms/CROSS_PLATFORM.md) |
| Install, configure, debug, test | [`docs/development/`](docs/development/DEVELOPMENT.md), [`tests/TESTING.md`](tests/TESTING.md) |
| Deep subsystem design + rejected alternatives | [`docs/KAYRA_SYSTEM_ARCHITECTURE.md`](docs/KAYRA_SYSTEM_ARCHITECTURE.md), [`docs/KAYRA_UI_ARCHITECTURE.md`](docs/KAYRA_UI_ARCHITECTURE.md) |

## Pending work

[`docs/roadmap/BROWSER_CONTROL_PLAN.md`](docs/roadmap/BROWSER_CONTROL_PLAN.md) — the
browser-control round, built **in parts** (§10 has the part table and what is next). Parts 1–16
and 5P are done; see §10 for what remains (§11 S2, the rest of §13, H2 phrasings, H3 other apps, H4 multi-app, 5P P4). Do the next part only when
the user asks. The user also asked that every round proactively
add the obvious neighbouring features and list them for veto.

## What Kayra is

Kayra, a personal desktop assistant. Voice (headless-browser Web Speech API) or text in;
a small LLM ("the DMM") classifies intent; the turn is routed to conversation, live web search,
autonomous deep research, assistant self-control, or desktop automation; replies are spoken by an
offline Kokoro-ONNX voice with barge-in. Optional hand-gesture pointer control via camera +
MediaPipe. A PySide6 desktop UI sits over the same backend (`--console` skips it).

**Platforms:** Windows 10/11 (original target, full feature set) and Linux x86_64 with
**Omarchy (Arch + Hyprland)** as the first-class Linux target. Desktop automation runs on
both: Win32 on Windows, Hyprland IPC + Wayland tools on Linux (`docs/platforms/LINUX_AUTOMATION.md`).

## Tech stack

Python 3.10–3.14 · PySide6-Essentials (Qt 6 widgets) · Selenium + Chrome/Edge (speech input) ·
kokoro-onnx + ONNX Runtime (CPU or CUDA) · sounddevice/PortAudio · Claude (anthropic) / Groq / Gemini /
local OpenAI-compatible servers (LM Studio, Ollama) · ddgs + BeautifulSoup · OpenCV + MediaPipe ·
psutil · rich. One dependency manifest: `requirements.txt` (PEP 508 platform markers).

## Directory structure

```
run.py              launcher (stdlib only): finds .venv, re-execs, preflight, single-instance lock
setup.py            environment provisioning (stdlib only): venv, deps, ORT/CUDA, models, checks
main.py             compat shim -> kayra.app.main()
requirements.txt    THE dependency manifest
.env.example        every configuration key, annotated (copy to .env; never commit .env)
src/kayra/
  app.py            orchestrator: bootstrap, listen/route loop, lifecycle, shutdown (console front end)
  platforms/        OS abstraction layer (leaf, stdlib only): detect, windows, linux, hyprland, fallback
  core/             leaf utilities: paths, config, runtime_state, voice_state, voice_control,
                    endpointing, hardware, system_profile, logbus, settings_log, conversation_context
  intelligence/     llm_engine (DMM), provider_router, emotion_engine, proactive_presence
  input/            speech_to_text, stt_backend, browsers, transcript_repair, gesture/
  output/           text_to_speech, tts_device (the only onnxruntime importer)
  automation/       desktop control — common (shared pipeline), policy, targets,
                    windows (Win32 hands), linux + linux_apps (Hyprland hands),
                    linux_system, web_macros + browser_bridge (Chrome extension),
                    sites (website table), interpreter (AI understanding layer),
                    diagnostics, screen_ai, workflows, ai, status_file
  extension/        kayra-bridge (Chrome MV3), omarchy-kayra-widget (bar pill)
  services/         chatbot, real_time_search, deep_research, proactive_agent
  memory/           conversation (persistence), store (management)
  utils/            console, timing, text (+ flat façade)
  ui/               PySide6 desktop UI: theme/ components/ views/ bridge session controls application
tests/              standalone assertion suites + run_all.py runner (no pytest)
docs/               architecture/, platforms/, development/, plus the two deep design docs
models/             Kokoro voice model + voices (+ hand_landmarker.task, downloaded by setup)
data/ logs/ Reports/  runtime user data, created on demand (gitignored)
```

## Execution flow (short)

`python run.py` → (system Python) locate/validate `.venv` → re-exec as child under venv Python →
`preflight()` + single-instance lock → `kayra.ui.application.main()` (or `kayra.app.main()` with
`--console`) → `app.bootstrap()` boots TTS + STT in parallel threads, imports services, builds the
LLM engine, starts the proactive agent and (optionally) gestures → loop: `Listen()` → local
control vocabulary → emotion → DMM `classify_intent()` → `Execute_Task()` routes tokens → speech.
Shutdown is `app.request_shutdown()`, always. Full detail: `docs/architecture/EXECUTION_FLOW.md`.

## Hard rules (breaking any of these has caused a real bug)

1. **Automation is one pipeline with two sets of hands.** Platform-neutral logic (normalizer,
   power-target gate, planner, executor, timers, files) goes in `automation/common.py`, never
   in `windows.py` or `linux.py`. OS mechanics go in the backend. Every Linux process start goes
   through `linux.Runner` and every compositor call through `platforms.hyprland.dispatch` — the
   test suite fakes both, and a call that bypasses them is untested. `windows.py` cannot be
   imported on Linux, so its suites stay `n/a:win32` there; change it conservatively.
   **The shell policy only ever gets stricter by voice:** a new rule may add a DENY or
   CONFIRM; loosening one needs the user's explicit say-so and a test row in
   `tests/test_linux_automation.py::section_hardening`.
2. **OS-specific code lives in `kayra.platforms` and nowhere else** (outside `automation/`).
   Shared modules call `platforms.active_window()`, `.reveal_command()`, `.displays()`,
   `.prefers_reduced_motion()`, `.default_browser_hint()`, `.pointer_injector()`. Never add a
   new `if sys.platform` branch to a shared module when a platform function would do. The layer
   is a stdlib-only leaf **below** `core`; it may import nothing else from `kayra`.
3. **Paths only through `kayra.core.paths`** (`data_path()`, `model_path()`, `logs_dir()`, …).
   Never a bare relative path, never a hard-coded drive letter or `/home/...`.
4. **No import-time side effects.** Importing any `kayra` module must not start a model, a
   browser, a thread, a socket or a network client. `bootstrap()` does that.
5. **`automation` never imports `input`; `core` and `platforms` import nothing above them.**
   The STT engine is looked up via `sys.modules` by string (importing it boots a browser).
6. **Never terminate a process by name.** PID-scoped only (the STT engine's `owned_pids`). On
   Linux too: never `pkill chrome` — the user's own browser is a Chrome.
7. **No `shell=True`, no `os.system`, no string-built commands.** Argument vectors only.
8. **One authority per concern:** provider fallback → `provider_router`; voice visual state →
   `core.voice_state`; settings changes → `core.settings_log`; logging → `core.logbus` with a
   `Subsystem.X` constant; shutdown → `app.request_shutdown()`; ONNX Runtime → `tts_device`.
9. **The DMM few-shot list (`dmm_chat_history`) is order-sensitive. Never slice or reorder it**;
   re-run `tests/test_dmm_matrix.py` after touching it.
10. **UI threading:** views never subscribe to the runtime bus or import a backend engine; all
    cross-thread traffic goes through `ui/bridge.py` signals. `ui/session.py` contains no Qt.
11. **Report capability honestly.** Unknown is `None`/"unavailable", never a plausible default
    (no fake 1920×1080, no "GPU" when the session runs on CPU, no "Windows 10" from a stale key).
12. **Models propose, rules and policy decide.** The understanding layer
    (`automation/interpreter.py`) and the planner may suggest commands; every one is parsed by
    `normalize_command` and judged by `classify_action`. Never let a model's output skip that,
    and never accept power, shell or file-deletion commands from a model. Test rigs switch
    `interpreter.ACTIVE` off and stub `web_macros.TOP_RESULTS` — no suite may reach a model or
    the network.
13. **Tier-1 tests must not read or write the host's state** (`.env`, memory store, battery,
    real pointer). Use `tests/_harness.py` (`EnvironmentGuard`, `HostPin`, `TemporaryProject`,
    `RecordingInstaller`, recording injectors).

## Coding conventions

- Plain absolute imports (`from kayra.core.paths import data_path`). Inside `kayra.utils`,
  `kayra.core`, `kayra.memory`: import from submodules, never the `kayra.utils` façade.
- Stdlib imports at module top. Heavy optional libraries (cv2, mediapipe, onnxruntime, evdev,
  pygetwindow) are imported **lazily inside the function that needs them**.
- Module header: the boxed title comment + a docstring explaining *why* the module is shaped
  the way it is (alternatives rejected, measurements). Match that style in new modules.
- Classes with `__slots__` + `to_dict()` for structured results (no dataclasses in core).
- Spoken strings (`ActionResult.message`, proactive lines) are one short sentence, no markdown.

## Naming conventions

- Modules/functions `snake_case`; classes `PascalCase`; constants `UPPER_SNAKE`.
- Private helpers `_leading_underscore`. Platform backends export the **same** public names.
- Legacy capitalised entry points (`Main_Loop`, `Execute_Task`, `OpenApp`, …) are kept for
  compatibility — do not rename them.
- Env vars: `UPPER_SNAKE`, grouped by prefix (`PROACTIVE_*`, `GESTURE_*`, `AUTOMATION_*`,
  `PROVIDER_*`, `KAYRA_*`, `STT_*`, `TTS_*`). Add new keys to `.env.example` with a comment.

## Dependency management

- `requirements.txt` is the only manifest. No pyproject/Poetry/Pipenv/uv lock — `setup.py` is a
  stdlib bootstrap script, not setuptools, and a second system would compete with it.
- Platform-only packages get a marker (`; sys_platform == "win32"` / `"linux"`).
- **Never list an ONNX Runtime variant** in `requirements.txt`; `setup.py` owns that choice
  (CPU vs `onnxruntime-gpu` + pinned NVIDIA CUDA wheels). Never `pip install onnxruntime` by hand.
- Add a ceiling only when a newer release was measured to break Kayra, and say why in a comment
  (example: `mediapipe<1.0`). Verify resolution for every supported Python × OS:
  `uv pip compile requirements.txt --python-version 3.X --python-platform <triple>`.

## Platform-specific development

- **Windows:** `python setup.py`, `python run.py`, venv python is `.venv\Scripts\python.exe`.
- **Linux/Omarchy:** `python3 setup.py`, `python3 run.py`, venv python is `.venv/bin/python`.
  System packages: `portaudio`, `google-chrome` (AUR) for speech input; `/dev/uinput` access for
  gesture clicks. See `docs/platforms/LINUX.md` and `OMARCHY.md`.
- Adding a capability: define the function in **all three** backends (`windows`, `linux`,
  `fallback`) with the same name, expose it in `platforms/__init__.py`, test it in
  `tests/test_platforms.py` with synthetic inputs.

## Testing and debugging

```bash
.venv/bin/python tests/run_all.py            # unit tier (Windows: .venv\Scripts\python)
.venv/bin/python tests/run_all.py --list     # inventory, runs nothing
.venv/bin/python tests/test_platforms.py     # one suite
.venv/bin/python -m pyflakes src/kayra tests run.py setup.py main.py
.venv/bin/python -W error::SyntaxWarning -m py_compile <files>
python3 run.py --doctor                       # interpreter, ORT, CUDA truth, GPU
```

`py_compile` does not catch undefined names — run pyflakes after moving code. A passing UI
suite does not mean the UI looks right; render it. See `tests/TESTING.md`.

## Before you finish a change

- Update the doc that describes what you changed (this file, the matching `docs/reference/*.md`,
  or `docs/…`). `CLAUDE.md` is loaded into every session: keep it short, put detail in a reference.
- For a major feature, security fix, dependency or breaking change, add the next
  `updates/UPDATE_V<n>.md` report and its row in `updates/README.md` (format described there).
- Run the unit tier on your platform and say which platform/Python you ran it on.
- Do not claim support for a platform or Python version you did not run.
- Do not commit or push unless asked.

## graphify

This project has a knowledge graph at `graphify-out/` (gitignored, generated).

- For codebase questions, when `graphify-out/graph.json` exists, first run
  `graphify query "<question>"`; use `graphify path "<A>" "<B>"` and `graphify explain "<concept>"`
  for focused lookups. Read `graphify-out/GRAPH_REPORT.md` only for broad review.
- When the user types `/graphify`, use the installed graphify skill first.
- After modifying code, run `graphify update .` (AST-only, no API cost).
