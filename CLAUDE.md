# Kayra — Project Context for AI Agents

> Personal, Jarvis-inspired desktop assistant for Windows. Voice or text in, LLM-routed intent
> classification, and hands-on system automation out. This file orients any AI agent working in
> this repo (Claude Code, or another tool via `AGENTS.md`) — architecture, conventions, and
> gotchas that aren't obvious from reading a single file in isolation. Read this before making
> cross-module changes; update it when you change something it describes.
>
> User-facing install/feature docs live in `README.md` and `blueprint.md` — this file is for
> agents making changes, not for end users. The full architecture (how each subsystem works,
> why it is shaped that way, what alternatives were rejected and why) lives in
> **`docs/KAYRA_SYSTEM_ARCHITECTURE.md`** — read that when you need depth; read this for the
> rules and gotchas you must not break. The desktop interface has its own document,
> **`docs/KAYRA_UI_ARCHITECTURE.md`**.

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

Three more subsystems run independently of that loop:
- **the local control watcher** (`app.py::_local_control_watcher`, aliased `_barge_in_watcher`)
  — daemon thread polling the STT page every 60ms while audio is playing, for BOTH interrupt
  words and lifecycle commands, and acting on them from outside the main loop. It has to live
  outside the loop: while a response is generating and speaking, `Main_Loop` is blocked inside
  `Execute_Task` and cannot poll the microphone at all. It also PUBLISHES `window.kayraSpeaking`
  in the same round-trip, which is what makes "stop" reliable — see the barge-in section.
- **`src/kayra/services/proactive_agent.py`** — ONE daemon thread sleeping on an Event, waking on a slow
  tick to look for a reason to speak. It reads assistant state from the shared runtime and
  never writes it. Full design below.
- **`src/kayra/input/gesture.py`** — standalone MediaPipe hand-gesture mouse replacement. Not
  wired into `app.py`; run directly with `python -m kayra.input.gesture`.

All three read the shared assistant state from **`src/kayra/core/runtime_state.py`**, which is where
"what is the assistant doing right now?" lives. It used to be a module-global in the
orchestrator; it had to move the moment a second thread needed a truthful answer to that
question, because a global in `app.py` cannot be read without importing `app.py` and re-running
its boot.

## Entry points, setup and run

```
python setup.py     # ONCE: validates Python, creates .venv, installs deps, writes .env
python run.py       # EVERY TIME: finds .venv, re-execs into it, starts the assistant
```

The user never activates the virtual environment by hand.

| File | Role |
|---|---|
| `run.py` | Launcher ONLY. Two phases in one file, told apart by `sys.prefix`: phase 1 (system Python, stdlib only) locates and validates `.venv` and re-execs; phase 2 (venv Python) runs preflight, takes the single-instance lock and calls `kayra.app.main()`. No application logic lives here. |
| `setup.py` | Environment preparation ONLY, and the single source of truth for it. Runs on the SYSTEM interpreter before `.venv` exists, so it imports nothing outside the stdlib and nothing from `kayra`. Creates the venv, installs `requirements.txt`, then PROVISIONS AND VERIFIES the speech runtime: one ONNX Runtime variant, `onnxruntime-gpu` on NVIDIA machines, the pinned CUDA/cuDNN wheels that build needs, and a real CUDA session probe before claiming GPU readiness. Never overwrites `.env`, never deletes a `.venv` without an explicit "y" that defaults to no, never prints a secret. |
| `main.py` | Backward-compatibility shim → `kayra.app.main()`. Kept because `python main.py` has always worked. |
| `src/kayra/__main__.py` | `python -m kayra` → `kayra.app.main()`. |
| `src/kayra/app.py` | THE application: bootstrap, listen/route loop, lifecycle, shutdown. Still the CONSOLE front end. |
| `src/kayra/ui/` | The desktop interface (PySide6). A presentation layer over the same backend — `python run.py` starts it by default, `--console` skips it. |

Things about these that are load-bearing:

- **`run.py` re-execs rather than manipulating `sys.path`.** Native extensions (onnxruntime,
  sounddevice, numpy, psutil, mediapipe) are ABI-bound to the interpreter that installed them;
  pointing `sys.path` at another environment's `site-packages` fails deep inside a native
  import with a message unrelated to the cause.
- **A child process, not `os.execv`.** On Windows `execv` does not replace the process — it
  spawns a new one and lets the original exit, so the console returns to the prompt while Kayra
  is still running and Ctrl+C no longer reaches it.
- **The phase-1 wrapper ignores SIGINT/SIGBREAK while waiting.** Console signals go to the whole
  process group, so both processes get them; the assistant runs a real shutdown and the wrapper
  must survive to report its exit code. Before this, Ctrl+C reported `0xC000013A` no matter how
  cleanly the child exited.
- **The interpreter is proved by `sys.prefix`, and the MODULES are checked too.** Being on the
  venv interpreter does not prove `kayra` came from this project — a global install or a stale
  `PYTHONPATH` produces a `.venv` Python importing someone else's code, and the symptom is
  edits that appear to do nothing. `preflight()` compares `kayra.__file__` against `src/` and
  refuses when they differ.
- **`python run.py --doctor`** prints the interpreter, environment, ORT package/version/
  location, every provider, whether CUDA is VERIFIED usable (a real session, not a provider
  name), and the GPU telemetry — then exits. This is the thing to ask for when speech output is
  on the wrong device.
- **Startup reports the interpreter and the ORT variant**, read from package METADATA rather
  than by importing `onnxruntime` (which costs ~200ms on the cold-start path). Two ORT variants
  installed at once is reported as an error, because they share one package directory and
  whichever was installed last silently wins.
- **A relaunch loop guard** (`KAYRA_RELAUNCHED`). One relaunch is legitimate; a second means
  detection disagrees with reality, and forking forever is the worst form of the duplicate
  instance the lock exists to prevent.
- **The single-instance lock is `(pid, create_time)`, not a bare PID.** Shutdown ends in
  `os._exit(0)`, which skips every `finally`, so the lock file is ALWAYS left behind — staleness
  detection has to be exact rather than relying on cleanup. Windows recycles PIDs.
- **Both scripts degrade to ASCII on a legacy console.** They print box-drawing and check-mark
  glyphs, which raise `UnicodeEncodeError` on cp1252 — and did, in the single most important
  path `run.py` has: a user with no `.venv` got a traceback instead of the "run setup.py"
  instructions. Each reconfigures its streams to UTF-8 where possible AND probes what the stream
  can actually encode. `utils/console.py` does the same for every other entry point, which is
  what keeps the test suites runnable on a legacy console.

## Package layout and import rules

```
src/kayra/
├── app.py            orchestrator
├── core/             paths, config, runtime_state, voice_state, conversation_context,
│                    voice_control, system_profile, logbus, settings_log
│                     ← imports nothing from the rest of kayra
├── intelligence/     llm_engine (DMM), provider_router, emotion_engine, proactive_presence
├── input/            speech_to_text, stt_backend, transcript_repair, browsers,
│                    gesture (standalone)
├── output/           text_to_speech, tts_device
├── automation/       windows (hands), policy (safety), targets (resolution)
├── services/         chatbot, real_time_search, deep_research, proactive_agent
├── memory/           conversation (persistence), store (management)
├── utils/            console, timing, text  (+ a flat façade in __init__)
└── ui/               desktop interface: theme/ components/ views/ + bridge, session
```

- **`core` is a leaf.** Anything may import it; it imports nothing back. That is what makes
  `runtime_state` safe for the proactive thread and `paths` safe for everything else.
- **`automation` must never import `input`.** Importing `speech_to_text` boots a headless
  Chrome as a side effect, and the user asking to close a window must not start a browser. The
  resolver reads STT ownership out of `sys.modules` by string instead — see the ownership note.
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
| `src/kayra/core/logbus.py` | THE structured log format — `[TIME] [LEVEL] [SUBSYSTEM] message`, canonical subsystem names, level threshold from `KAYRA_LOG_LEVEL`, credential redaction on every line, turn correlation, optional rotating file log. Renders through `utils.console.safe_print`, so there is still one Console. Leaf: stdlib + `core.paths` only |
| `src/kayra/core/settings_log.py` | The ONE place a setting change is announced and committed. `record()` for a plain change, `apply()` for one that does runtime work — request, run, verify, commit — so a failed change is never reported as a success. Refuses to print the value of a credential |
| `src/kayra/core/voice_state.py` | `VoiceStateMachine` — the authoritative answer to "what should the user be told about the microphone right now?", resolved from facts, with monotonic revisions and a short dwell. Leaf: stdlib only |
| `src/kayra/core/conversation_context.py` | What the conversation is currently ABOUT: recent turns, the last intent, automation targets, an outstanding question, the topic's content words. STATE, never persisted; a leaf, stdlib only. Read by the transcript repair stage and the presence layer |
| `src/kayra/input/transcript_repair.py` | The LAST stage of the capture pipeline: re-ranks the recognizer's own N-best against the conversation context, with one tightly-guarded phonetic step. No dictionary, no model, and it can never invent a shutdown |
| `src/kayra/core/voice_control.py` | The LOCAL control vocabulary — barge-in, listening pause/resume, standby, shutdown — matched exactly, before the DMM, with no network and no model. Leaf module: imports only the stdlib plus a lazy `core.config` read for the assistant's name |
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
| `src/kayra/input/gesture.py` | Standalone MediaPipe hand-gesture mouse control |
| `src/kayra/utils/` | Split by responsibility: `console.py` (Rich theme, logger, print_* helpers, the UTF-8 stream fix), `timing.py` (`StageTimer`, `now_ms`), `text.py` (`speech_safe_text`, `SentenceStreamer`, `answer_modifier`). `__init__.py` is a flat façade over the three |
| `tests/*.py` | Manual diagnostic entry points, **not** an automated pytest suite — run each directly. `test_audio_pipeline.py` (barge-in, echo rejection, speech normalization), `test_stt_lifecycle.py` (session reuse, recovery, process ownership), `test_dmm_matrix.py` (intent-boundary accuracy), `test_voice_control.py` (the local control vocabulary, the Kayra-vs-computer shutdown boundary, tail matching, JS/Python agreement, shutdown order and idempotency), `test_tts_device.py` (ONNX Runtime, CUDA verification and provider truthfulness), `test_environment.py` (launcher interpreter ownership, import origin, setup provisioning), `test_proactive_agent.py` and `test_emotion_engine.py` assert and exit non-zero; `test_barge_in_live.py` needs a human to speak |

## The emotion engine (rewritten 2026-09-07)

`src/kayra/intelligence/emotion_engine.py`. Estimates how the user SOUNDS so the assistant can
adjust TONE — never intent.

**There is no acoustic analysis, and that is a decision, not an omission.** Kayra's STT is the
Web Speech API inside headless Chrome. Chrome owns the microphone and hands Python a
*transcript* — **the raw audio never enters this process**. Adding prosody would require a
second microphone stream (two capture paths on one device), a `MediaRecorder` round-trip
through Selenium (hundreds of KB per utterance on the same WebDriver connection the barge-in
watcher polls every 60ms), or librosa/scipy (tens of MB RSS, hundreds of ms per turn). All
three cost more than the signal is worth for what the mood is used for: one sentence of tone
guidance on the system prompt. If STT is ever replaced by an in-process recognizer that already
holds PCM, revisit this — the blocker is the audio boundary, not the idea.

- **Three signals, weighted:** lexical `W_LEXICAL = 0.62`, structural `W_STRUCTURAL = 0.20`,
  contextual `W_CONTEXT = 0.18`. Structural is low on purpose — `"!!!"` is intensity without
  content and may only tip a near-tie.
- **Fusion normalises each signal by its OWN evidence mass** (`scale = weight / evidence`)
  before weighting, so a weakly-firing signal cannot contribute a large raw number just because
  its scale differs. This is what stops one weak signal overriding a strong one.
- **Confidence needs both margin and mass**: `(0.45 + 0.55*margin) * mass`, where
  `mass = min(1, total_evidence/1.5)`. Margin alone would make one 0.3-weight token look as
  certain as a paragraph. Below `DEFAULT_THRESHOLD = 0.35` the reading reports `neutral`.
- **False-positive damping is the requirement that actually matters.** Emotional vocabulary
  appears constantly in ordinary questions. Damping is by whether the sentence is even ABOUT
  the speaker: definitional without first person 0.1, question without first person 0.2, third
  person without first person 0.3, question with first person 0.75. Negation in the three
  tokens before a match cancels it. Verified: "Why do people get stressed before exams?" →
  `neutral` (0.07), while "I'm so tired of this" → `tired` (0.87).
- **Eight states only**: neutral, happy, excited, stressed, sad, tired, frustrated, calm.
- **`EmotionReading` subclasses `str`**, so it IS the emotion label wherever a string is
  expected — which is what keeps `Chatbot(query, tts, mood)` and
  `RealTimeSearchEngine(query, mood, tts)` working unchanged — while also carrying
  `.confidence`, `.signals`, `.scores`, `.tone` and `.to_dict()`.
- **Mood never reaches the DMM.** It goes to the chatbot and the search engine and nowhere
  else, so "open Chrome" spoken angrily is still `open chrome`. Emotion influences; intent
  dominates.
- **Nothing is persisted.** A short bounded deque of recent readings, in RAM. There is
  deliberately no emotion file.
- **It must never break a turn.** `app.py` wraps the call in try/except and continues with
  `mood=None`; malformed or non-string input returns a neutral reading rather than raising.
- Measured: 14.3us per `analyze()`, 2.4us construction, +11.7 KB RSS over 50,000 analyses, zero
  threads, zero audio streams, no heavy imports. The test suite asserts all of those.

## The Decision-Making Model (DMM) — how intent classification works

- `CentralizedLLMEngine.classify_intent()` sends the raw user query plus a large few-shot
  preamble (`self.dmm_preamble` + `self.dmm_chat_history`, both set in `__init__`) to Cohere
  (cloud) or a local model.
- The model must respond with a comma-separated list of task tokens, each starting with one of
  the strings in `self.funcs`.
- **Gotcha — duplicate matching**: several `funcs` entries are prefixes of one another
  (`close` / `close window` / `close tab`, `save` / `save file`, `minimize` / `minimize all`,
  `copy` / `copy text`). The parser must match each raw task against the header set **once**,
  not once per matching prefix — a naive per-func loop-append duplicates the task and
  double-executes it downstream (e.g. `"minimize all"` would fire Win+D twice, undoing itself).
  Fixed 2026-09-03; if you touch this loop, keep the single-match + `seen_tasks` dedup.
- **Gotcha — few-shot ordering matters**: `self.dmm_chat_history` is deliberately ordered so
  the highest-value disambiguating examples (open/close, window management) sit **last**, for
  maximum recency weight with Cohere. Never slice/truncate this list (a `[:40]` slice used to
  silently drop exactly those examples in local-model mode — fixed 2026-09-03).
- `run_boot_sequence()` is purely cosmetic narration (prints the model-routing status lines, and
  speaks them only when handed a TTS engine) — `classify_intent()` and `generate_chat_stream()`
  work correctly on a freshly constructed engine whether or not it is ever called. `app.py`
  calls it without a TTS engine on purpose; see the cold-start notes.

## The automation layer (rewired 2026-09-07)

`src/kayra/automation/windows.py` used to be one ordered `if/elif` chain matching on
`cmd_lower.startswith(...)`, with every layer re-reading the user's raw sentence. It is now a
pipeline, and the old chain is gone:

```
DMM token
  -> normalize_command()   Action{domain, action, target, parameters, confidence}
  -> classify_action()     ALLOW | CONFIRM | DENY
  -> resolve_*()           RESOLVED | AMBIGUOUS | NOT_FOUND | UNAVAILABLE
  -> plan_actions()        dependency-ordered groups
  -> execute_action()      handler + verification
  -> ActionResult.message  the sentence main.py speaks
```

**Backward compatibility is total.** Every handler that existed before is still exported with
its original name and signature (`OpenApp`, `CloseApp`, `WindowManage`, `MediaControl`,
`HotkeyShortcut`, `SystemInfo`, `SetTimer`, `TakeScreenshot`, `Clipboard*`, `ExecuteCommand`,
`ToggleWifi`, `WebSearch`, `Content`, `YoutubeSearch`, `PlayYoutube`, `global_desktop_type`,
`translate_and_execute`, `Automation`). `tests/test_automation.py` asserts the list.
`Automation()` now RETURNS the spoken sentence instead of `True`; a non-empty string is still
truthy, so truthiness checks are unaffected.

### Prefix collisions are now structural, not positional

The old chain depended on `elif` ORDER: `close window` and `close tab` had to be tested before
the generic `close ` prefix or they were routed to `CloseApp("window")`. That constraint is
gone. `_EXACT_TOKENS` is a dict consulted before `_PREFIX_TOKENS`, and the prefix list is
sorted longest-first at import. A dict cannot be reordered by accident and a longest-first list
cannot be shadowed, so the class of bug is eliminated rather than documented.

Still true: if you add a literal token, add it to `_EXACT_TOKENS`, and confirm
`tests/test_automation.py`'s normalizer table covers it. Every token in `llm_engine.funcs` must
normalize to something — the suite asserts zero dead tokens.

### Opening a target: application vs website (fixed 2026-09-08)

**"Open YouTube" used to open File Explorer, and "open GitHub" used to launch Git GUI.** Both
came from the same place: every non-URL target went to `AppOpener.open(..., match_closest=True)`,
whose launcher is `os.system("explorer shell:appsFolder\\<id>")` over a CACHED Start-Menu index
with a `difflib` fuzzy fallback at cutoff 0.6. Reproduced on this machine — the index held a
STALE `youtube` entry pointing at a Brave PWA AppsFolder id that no longer exists, and
**explorer answers a dead id by opening a plain File Explorer window** rather than failing;
`github` was absent entirely, so difflib matched "git gui". Neither raised, so the assistant
said it had opened YouTube. The `except` fallback was a DuckDuckGo `!ducky` redirect, which
routed a plain website open through a search engine.

`targets.resolve_open_target()` now decides the target's TYPE first, and each type has exactly
one execution path: explicit URL -> installed application -> canonical website -> curated
app (unconfirmed install) -> exact Start-Menu entry -> named user folder or literal path ->
NOT_FOUND. The folder step is last on purpose ("open Chrome" must never be answered by a
folder) and uses `resolve_path`, which checks the well-known folders by NAME and the path as
given — it never walks the disk.

- **There is no step that searches the machine for a name resembling X and runs it.** A miss is
  reported. `match_closest=True` appears at no call site (AST-asserted by
  `tests/test_target_resolution.py`), and the `!ducky` fallback is gone.
- **`WEBSITE_REGISTRY`** in `targets.py` is the canonical service table (URL + aliases + the
  browser-title fragments used for closing). One line per service. `website_url()` is one hash
  probe — measured **1.7us** — so nothing calls an LLM or the network to learn that YouTube is
  a website. `SITE_HINTS` is now derived from it and keeps its old shape; the legacy contract
  that "spotify" is NOT a site key (the desktop app owns that name; the web player is "spotify
  web") is pinned by `tests/test_automation.py`.
- **`application_available()`** answers "is X installed?" from metadata that already exists: a
  running window, the Windows *App Paths* registry key, then the Start-Menu index **on exact
  names only**. Cached 300s — 0.12ms cold, 1.2us warm. It never walks the disk, and index
  entries with a falsy id are dropped (that empty id is what produced the File Explorer
  window).
- **Application beats website** for a curated app name, so "open Chrome" is never a web page. A
  curated app that will not launch but has a web version (Spotify, WhatsApp) falls back to the
  web player and says so out loud.
- **The type reaches the structured action.** `normalize_command("open youtube")` now yields
  `browser.open_url` with the URL already in `parameters`, not `app.open`. Two dict lookups,
  ~8us total.
- **"Open YouTube" is not "search for YouTube".** Opening is `browser.open_url`; `google
  search X` / `youtube search X` / `realtime X` are unchanged and still distinct.

### Closing a target: one intent, one target (fixed 2026-09-08)

**"Close X" could close several windows.** `resolve_application` returns EVERY window of an
application and the executor looped `WM_CLOSE` over all of them — one sentence, four windows
gone, no question asked. A missed site lookup also fell through into a LOOSE application lookup
matching the name anywhere in a window title, so "close YouTube" could reach a VS Code window
editing `youtube-dl` notes.

- **`targets.pick_single()` is the single-target guarantee.** It narrows a multi-candidate
  resolution to exactly one, tie-broken by the foreground window and only when the foreground
  really is one of the candidates; otherwise AMBIGUOUS and the assistant asks. Nothing on the
  close path iterates a match set any more. It also re-filters Kayra-owned windows, so a match
  set that somehow contained one still cannot be acted on.
- **Strictness scales with risk.** `resolve_application(name, strict=True)` is used for every
  close: for a name Kayra does not curate, only exact process identity counts, never a title
  substring. The loose reading is kept for FOCUS, where a near-miss only brings up the wrong
  window. Do not use the loose reading for anything destructive.
- **A pure site name that is not on screen is a MISS.** It is not an invitation to look for a
  process of that name. Only a name that is ALSO a real application falls through to the app
  branch.
- **Breadth is explicit, never inferred.** `close all <app>` -> `app.close_all`;
  `close everything` / `close all windows` -> `window.close_all`, which is CONFIRM-gated. A
  plain "close chrome" cannot widen into either. Both are DMM tokens the user has to actually
  say.
- **The semantics stay distinct:** "close this"/"close this window" -> the exact foreground
  HWND; "close this tab" -> Ctrl+W to a browser that is ALREADY in front; "close YouTube" ->
  the one browser window showing it; "close Chrome" -> one window; "close that" -> the bounded
  context referent, or the foreground window when nothing fresh applies.
- **Last-tab behaviour is the browser's consequence, not a second close.** Ctrl+W on a
  single-tab window makes the browser close that window. The guarantee Kayra owes is one
  keystroke to one focused window, and that is what the test suite pins.

### Three real dangers that were removed

* **`taskkill /f /im <name>.exe`.** `CloseApp`'s final fallback force-killed every process of a
  name. For "close chrome" that is the user's entire browsing session *and* Kayra's own STT
  Chrome, which would take the microphone down with it. There is now no path in the automation
  stack that terminates a process by name; `tests/test_automation.py` walks the AST to prove it.
  Closing is `WM_CLOSE` to a resolved window — what clicking the X does. `force_close_app()`
  exists for the genuine force case: PID-scoped, protected-process filtered, CONFIRM-gated.
* **The substring sweep.** `CloseApp` posted `WM_CLOSE` to *every* visible window whose title
  contained the requested string. Matches are now scored (`_score_window`) and ranked, and a
  near-tie returns AMBIGUOUS so the assistant asks.
* **Instant shutdown/restart.** `ExecuteCommand` ran `os.system("shutdown /s /t 0")` the moment
  a substring matched — a misheard word could power the machine off mid-sentence. Both are
  CONFIRM actions now, and `ExecuteCommand`'s branch is deliberately inert so the gate cannot
  be routed around.

### Safety policy (`src/kayra/automation/policy.py`)

One place decides whether anything may run. `classify_action` for structured actions,
`classify_shell` for terminal commands; both return ALLOW / CONFIRM / DENY plus a reason that
becomes the spoken refusal.

`classify_shell` PARSES the command rather than string-matching it: shell metacharacters
(chaining, piping, redirection, substitution) are refused outright because every later check
reasons about one command; then the executable stem is checked (path- and extension-insensitive,
so `C:\Windows\System32\format.exe` is `format`); then destructive verbs are judged by SCOPE
against `_is_protected_path`. Nested interpreters (`powershell`, `cmd`, `wscript`) are denied as
shell targets because a nested shell defeats every check above it. Unrecognised executables are
CONFIRM, not ALLOW.

`_is_protected_path` distinguishes `_PROTECTED_TREES` (the Windows directory and everything
under it) from `_PROTECTED_EXACT` (a drive root, the Users folder — protected as a TARGET only).
Conflating them is not a safe default in the direction it looks: an earlier version protected
everything on C:, which blocked every legitimate file operation the user has. Caught by the test
suite, not by reading it.

**Kayra's own first-party PowerShell calls are not governed by this policy** — they are fixed
argument vectors with no user text in them (`_powershell`, the brightness WMI calls, the timer
toast). The policy governs commands that ORIGINATE from something the user said.

### Confirmations

`ConfirmationManager` holds at most one pending action, bound to `Action.fingerprint()` (domain,
action, target and parameters) and expiring after `AUTOMATION_CONFIRM_TTL_SECONDS`. `app.py`
answers it BEFORE the DMM: a bare "yes" sent to the classifier comes back as `general yes` and
gets answered by the chatbot, so the confirmation would never resolve. `read_confirmation_reply`
matches the whole cleaned utterance exactly — "yes and open chrome" is a new instruction, not an
authorisation — and returns None for anything that is not an answer, so an unrelated command
spoken while a prompt is pending still runs normally.

### Target resolution (`src/kayra/automation/targets.py`)

`resolve_open_target`, `resolve_window`, `resolve_application`, `resolve_site`,
`resolve_browser`, `resolve_path`, plus `pick_single`. Every one returns a `Resolution`, never
an action — and a `Resolution` now carries a `kind` (`TargetType`: APPLICATION, WEBSITE, URL,
WINDOW, TAB, FILE, FOLDER, PROCESS, UNKNOWN) as well as a status. Knowing WHAT was resolved is
load-bearing: the "open YouTube" bug was a type confusion, an untyped name walking into an
application launcher.

* **Kayra-owned PID exclusion is the critical safety property.** `kayra_owned_pids()` reads the
  live STT engine's `owned_pids` out of `sys.modules` — it must NEVER import or construct
  `speech_to_text`, because doing so would boot a headless Chrome as a side effect of the user
  asking to close a window.
  **Because the lookup is by STRING, a module RENAME turns this safety property off silently
  instead of raising.** It did exactly that: after the move to `kayra.input.speech_to_text` the
  function still listed only the two pre-reorganisation names, returned an empty set, and
  Kayra's own Chrome stopped being excluded from window enumeration. Fixed 2026-09-07, with two
  regression checks in `tests/test_automation.py` — one pins the current module path, one proves
  the lookup actually finds a live engine's PIDs. If you move the STT module again, update the
  name tuple. Every enumerated window is flagged once, in `list_windows`, so no
  caller can forget to check.
* **"Close YouTube" is a site, not a process.** `looks_like_site` + `resolve_site` match the
  site's title fragments against browser window titles.
* **Known limitation, stated in the module:** Win32 exposes one handle per browser window and
  its title reflects only the ACTIVE tab. A site in a background tab is invisible here.
  `resolve_site` returns NOT_FOUND with that reason rather than closing something else. Real tab
  enumeration needs UI Automation or a debugging port; neither is installed and neither earns
  its cost yet. `_TAB_ENUMERATION_NOTE` marks the extension point.
* `list_windows()` caches for 250ms — a multi-step command asks three or four times, and a
  quarter-second is far shorter than any user-visible window change. `invalidate_cache()` after
  anything that changes the window set.

### The stuck-modifier bug (do not reintroduce)

Bringing a window forward needs a synthetic ALT tap to defeat Windows' `ForegroundLockTimeout`.
If ALT is still logically held when the next accelerator arrives, **Ctrl+T is delivered as
Ctrl+Alt+T and Windows opens the task switcher** — the browser never sees it. Observed end to
end: the new tab silently failed, and the following "close this tab" then landed on a
single-tab window and closed the whole window.

Two things prevent it, and both are load-bearing:
* `focus_window` tries `SetForegroundWindow` FIRST and only taps ALT as a fallback, then
  releases ALT/CTRL/SHIFT and settles for `FOCUS_SETTLE_SECONDS` — **including on the fast path
  where the window is already in front**, which is where the bug actually lived.
* `send_keys()` in `automation/windows.py` normalises the modifier state immediately before every
  injected keystroke. Every injection site goes through it so no future call site can forget.

### Destructive tab actions never hunt for a browser

`close_tab` requires a browser to ALREADY be in front. If the user says "close this tab" while
looking at their editor, the honest answer is "no browser is in front". An earlier version fell
back to "find any browser and close a tab in it", and during testing that closed a tab in a
window full of the user's own work. Non-destructive actions (new tab, refresh, back) may still
focus the one unambiguous browser.

### Planner

`plan_actions` groups actions for execution. Only `_CONCURRENT_SAFE` domains (`info` — pure
reads) share a group; everything else is sequential. This is a correctness fix, not tuning: the
old router built every command as an `asyncio.to_thread` task and fired the lot through one
`asyncio.gather`, so "open chrome and maximize the window" raced the maximize against the
launch, and two keystroke sequences went to whatever had focus at that instant.

### Verification

Actions verify rather than assume. `_open_app_verified` focuses an already-running app instead
of launching a second copy, then waits for a window; `close` polls until the handle is gone;
screenshot diffs the folder; filesystem operations re-`stat` the result. Nothing says "Done."
because a keystroke was sent.

### Efficiency

* `normalize_command` 1.4us, `classify_shell`/`classify_action` 4.3us — no allocation, no I/O.
* `list_windows` 4.9ms cold, 0.0004ms cached.
* `SystemInfo` moved off PowerShell onto psutil: ~0.1ms instead of a 300-900ms process spawn
  per query. Three info queries answer together in ~100ms.
* **Zero LLM calls on the execution path.** The DMM classifies; everything after it is
  arithmetic. `Content()` is the one text-GENERATION feature and is not an execution decision.
* No new threads. Timers use `threading.Timer` via the bounded `TimerService`, cancelled at
  shutdown by `shutdown_automation()`.
* No `shell=True` and no `os.system` anywhere in the stack (AST-asserted).

### Bounded collections

Audit ring 200 entries; automation context is six fixed slots with a 300s referent TTL;
`TimerService.MAX_ACTIVE` 16; screenshots pruned to `AUTOMATION_SCREENSHOT_KEEP` (30); the
window cache is one list; the pid→exe cache clears at 256 entries; `SearchFiles` is depth- and
result-capped and never walks the whole disk.

### Automation responses

`ActionResult.message` is SPOKEN, so it is one short sentence: "Chrome is open.",
"YouTube is closed.", "I don't see netflix open.", "You have more than one browser open. Which
one?". No markdown, no paths, no status codes — those go to `detail` and the audit log.

## Model routing — the provider router (`src/kayra/intelligence/provider_router.py`)

**There is ONE retry/fallback authority in the process, and it is this module.** Before it,
fallback was invented independently in two places inside `llm_engine.py` and the two disagreed
about almost everything — which is how one user request became four provider calls against an
already rate-limited key.

### The hierarchy

```
DECISION (the DMM)   Cohere  ->  Groq  ->  Gemini
CHAT                 Groq    ->  Gemini
```

They are deliberately NOT the same list, and the distinction is explicit in `ROUTE_CHAINS`
rather than implied by the ordering of `if` statements. Cohere leads DECISION because the DMM's
few-shot token contract was written and measured against Command-R; it is not a chat provider
here and appears nowhere in the CHAT chain. Groq leads CHAT because it is the fastest first
token available, and is the DMM's first fallback rather than its primary because the
intent-boundary matrix was tuned against Cohere.

- **Local-first is unchanged and absolute.** With an LM Studio / Ollama server up, ALL traffic
  goes there and the router is not consulted — a chain of one has no decisions in it.
- **The DMM prompt contract is identical on all three providers.** The same strict system rule,
  the same `dmm_preamble` and the same unsliced `dmm_chat_history` reach every one; only the
  transport differs (Cohere's native `preamble`/`chat_history` parameters versus the
  OpenAI-compatible message list Groq and Gemini speak). A fallback that changed the prompt
  would be classifying a different question, and the token contract the whole automation layer
  depends on would silently vary with whoever answered.

### Failure classification

Eight kinds — `RATE_LIMITED`, `AUTH_FAILURE`, `NETWORK_FAILURE`, `TIMEOUT`, `SERVER_ERROR`,
`INVALID_REQUEST`, `MODEL_UNAVAILABLE`, `UNKNOWN` — decided once, from the exception's type name
AND its string form. The kind determines two things and only two: how long the provider stands
down, and whether the request is worth handing to anybody else.

- **`INVALID_REQUEST` is the one kind that does NOT fall through.** Sending an identical
  malformed request to two more providers is three failures instead of one.
- **`AUTH_FAILURE` DOES fall through** — a missing Cohere key says nothing about the Groq key —
  but it carries the longest cooldown (900s), because nothing about the next minute will fix a
  wrong credential and retrying it every turn is the retry storm this exists to prevent.

### Cooldowns

Per-provider, bounded, and configurable (`PROVIDER_COOLDOWN_*`). **A provider-supplied
`Retry-After` always wins over the configured default** — it is the only number that reflects
what that key's budget is actually doing. Verified live: Groq returned `Retry-After: 6` under
load and was skipped for 6s rather than the 60s default.

**There is no blocking backoff anywhere.** The old handler answered a Cohere rate limit with
`sleep(5)`, `sleep(10)`, `sleep(15)` and then degraded to conversation — thirty seconds of
blocked user, ending in the assistant not doing what was asked, on a machine where two other
providers sat idle. Measured after the change, against the developer's genuinely rate-limited
Cohere key: `Cohere RATE_LIMITED -> Groq SUCCESS` in **708ms**, and the next twenty requests do
not touch Cohere at all.

### `max_retries=0` is load-bearing, and it was measured

The OpenAI SDK retries transport failures twice by default with its own backoff. That is a
SECOND retry authority underneath the router and it produces exactly the failure the router
exists to prevent. Measured live on this machine before the change: a single Groq DMM call took
**9.9s** and then **24.0s**; after it, every call in the same test completed in 1.7-2.0s. The
clients also carry a bounded `PROVIDER_TIMEOUT_SECONDS` (default 30, clamped 5-300), which is
the other half of the same rule — a hung provider must hand the turn back rather than block the
user while two good fallbacks sit idle.

### Streaming, and the rule that keeps it safe

`run_stream` may only fall back BEFORE the first chunk has been yielded. Once a token has
reached the user, switching providers would splice two different answers into one sentence —
visibly broken, and worse than the failure it was hiding. A mid-stream failure is therefore
terminal for that request; the provider is still stood down so the NEXT request routes
elsewhere.

### The engine

- **`CentralizedLLMEngine` is still a singleton** and still constructs one set of clients per
  process. It no longer catches `cohere.TooManyRequestsError`, no longer sleeps, and no longer
  defines a private quota-error predicate — `tests/test_provider_router.py` asserts all three.
- **`run_boot_sequence()` is no longer called by `app.py`.** It prints the same provider block
  the startup report prints, and two copies of one fact is duplication. The method is kept for
  standalone diagnostics that boot the engine alone.
- Verified compatible: the `cohere` package's v1-style `chat_stream(message=, preamble=,
  chat_history=, prompt_truncation=)` Client API works unchanged from 6.1.0 through 7.1.1 —
  don't "fix" this thinking it's deprecated without re-checking against the installed version.

## The voice loop — self-listening, barge-in, and latency

The microphone stays open at all times, including while the assistant is speaking. That is
deliberate (barge-in depends on it) and it makes three problems load-bearing. All three were
fixed 2026-09-07; the notes below are the parts that are easy to reintroduce.

### Echo rejection is timestamp-based, not text-based

Every finalized utterance from the STT page carries the wall-clock window it was **captured**
in (`start_ms`/`end_ms`, from `Date.now()`), and the TTS engine keeps a ledger of the windows
in which audio was actually leaving the sound card (`DynamicVoiceEngine.was_audible_between`).
`main.py::_is_self_echo` intersects the two. Anything captured while she was audible is her own
voice and is dropped; the only speech accepted during playback is the interrupt vocabulary.

- **Do not** go back to comparing the transcript against `tts.last_spoken_text` with difflib.
  That was the original filter and it cannot work: playback lags generation by several
  sentences, so the echo reaching the microphone is of a sentence spoken much earlier than the
  one that string holds. Similarity came out near zero and every echo was promoted to a user
  command, which is what made her answer herself.
- **Do not** compare against "is TTS playing right now" either. An utterance is only finalized
  ~800ms (`silenceLimit`) after the speaker stops, so by the time Python sees it the flag has
  already flipped. Only the capture timestamps answer the question.
- `stop()` records `_last_stop_ms`, and an utterance that begins after it bypasses the gate —
  the user just took the floor, so their next command must not be eaten by the echo tail
  margin. `_begin_burst()` clears it again the moment new audio starts.
- Chrome-side AEC/noise-suppression (`primeProcessedMicrophone` in the STT page) reduces how
  much echo arrives in the first place, but it is best-effort — the timestamp gate is the
  guarantee.

### The local control layer (added 2026-09-08)

`src/kayra/core/voice_control.py`. Everything the user says ABOUT Kayra rather than TO it —
stop talking, stop listening, sleep, wake, shut down — is matched HERE, before the classifier.
One normalization pass and a frozenset probe; measured **under 10us per utterance**.

- **It must never call an LLM.** Every command in this vocabulary is about the assistant's own
  lifecycle and is useless if it is slow or needs the network: "stop" has to silence playback in
  tens of milliseconds (a Cohere round-trip is ~700ms AFTER the ~800ms VAD finalize), and "exit"
  has to work with the network down and no Cohere key.
- **Matching is EXACT on the whole utterance, fillers removed.** This is the rule that keeps
  `stop` / `stop the music`, `wait` / `wait for me`, `hold` / `hold the window` and
  `turn off kayra` / `turn off my pc` apart. A `startswith` test collapses every one of those
  pairs into its first member.
- **The DMM was NOT changed for any of this.** The `exit` / `stop listening` / `proactive`
  tokens still exist and still work as the fallback for phrasings only the classifier catches
  ("that's all", "bye jarvis"); the local layer just gets there first. Adding tokens or touching
  `dmm_chat_history` for lifecycle commands would have been the expensive way to buy nothing.

### Why "stop", "wait" and "hold" were unreliable — three defects, none in the vocabulary

1. **The interim probe was polluted by echo.** The page matched
   `looksLikeInterrupt(currentText + interimTranscript)` — the WHOLE accumulated utterance —
   against an exact phrase set. The microphone stays open during playback, so whatever echo
   survived Chrome's canceller was already in `currentText` when the user spoke. The probe was
   not `"stop"` but `"...and then the rollout takes ten minutes stop"`, which no whole-utterance
   test can match. That is why "stop" worked in a quiet room and failed over a long answer: the
   two cases differ only in how much echo had accumulated.
2. **`"hold"` was not in the vocabulary at all** — only `"hold on"` was.
3. **`clear_queue()` cleared the queue but not `currentText`.** After a barge-in the recognizer
   still held the echo plus the interrupt word, and the silence timer pushed that polluted
   string onto the queue ~800ms later, where it arrived as the user's NEXT COMMAND. Clearing the
   queue alone only delayed the problem by one VAD window.

### The fix, and the one rule that keeps it safe

`window.kayraSpeaking` is published from the watcher's existing poll (`poll_controls`), so it
costs no extra round-trip. **While and only while it is set**, `looksLikeInterrupt` also tests
the trailing 1-4 words. Outside playback the match is whole-utterance and exact, as before.

- `interrupt_in_tail()` is a SEPARATE function from `is_interrupt_phrase()` on purpose, so it
  cannot be reached by accident. Applied to a finalized transcript it would turn "close this tab
  and stop" into a barge-in.
- **Do not** widen tail matching to the lifecycle commands. Quitting the process on a misheard
  suffix would be the worst failure this system could have; `looksLikeControl` is
  whole-utterance only and the test suite asserts it.
- Known and documented: interim results arrive incrementally, so at the instant the user has
  said only the first word of "stop the music" over a running answer, that word is
  indistinguishable from a barge-in and Kayra falls silent. That is the right call — the user is
  talking over her — but the rest is then dropped by `clear_queue()`. Spoken while she is
  silent, "stop the music" reaches the DMM unchanged.

### Barge-in

Three independent pieces have to hold for "stop" to work; removing any one silently breaks it:

1. **Detection off the main loop.** `_local_control_watcher` polls; `Main_Loop` cannot, because
   it is inside `Execute_Task` for the whole response.
2. **Detection on interim results.** The STT page flags interrupts from *interim* recognition
   results, skipping both the 800ms VAD finalize and the `mtranslate` network round-trip.
   Waiting for the finalized transcript costs ~1s.
3. **Epoch cancellation in the TTS engine.** `stop()` bumps `_epoch`, drains the text and audio
   queues, aborts the output stream, and latches `_interrupted` until the next `begin_turn()`.
   The latch is what stops a still-running LLM stream from resurrecting the cancelled answer
   one `speak()` call at a time.

Consequently: **the TTS engine owns the only sentence queue.** `chatbot.py` and
`real_time_search.py` feed `tts_engine.speak` through `utils.SentenceStreamer` and must not
spawn their own speech worker thread — a private queue holds a backlog that survives `stop()`,
which is exactly how the old implementation kept talking after being interrupted.

The interrupt rule is implemented twice — `classify_control()` in Python and
`looksLikeInterrupt()` / `looksLikeControl()` in the STT page — and **the two must agree**. The
page is injected with the Python list at recognition start, so there is one vocabulary rather
than two; `tests/test_voice_control.py` asserts the agreement case by case, including live
against a real browser session.

### Standby (sleep / wake)

`app.set_sleeping()`. Standby stops Kayra DOING things: it silences speech, switches the
proactive service off, and makes `Listen()` discard every utterance that is not a control
command — no emotion analysis, no DMM call, no cloud round-trip, no automation.

- **It deliberately does NOT close the microphone.** "Wake up" is a spoken command and a closed
  microphone cannot hear it. "Standby releases the microphone" and "you can wake Kayra by
  speaking to it" are mutually exclusive requirements, and the second is what makes standby
  useful at all. A user who genuinely wants the microphone released says "stop listening", which
  does close it and is undone from the window, the tray or Ctrl+M.
- **Waking RESTORES the proactive setting rather than switching it on**, so a user who had
  suggestions disabled does not get them back by waking.
- `RuntimeState.sleeping` is a THIRD independent axis, not a value of `state` and not the same
  as `listening` — the same argument that already keeps `listening` separate.

### Four "stop"-shaped concepts, and they must never share a flag

| Concept | Cancels | Entry points | Runtime |
|---|---|---|---|
| **Barge-in** | the sentence being SPOKEN | "stop"/"wait"/"hold", the watcher, Ctrl+. , the composer's stop button | `note_interrupt()` |
| **Listening pause** | the MICROPHONE | "stop listening", Home's button, Ctrl+M, the tray | `RuntimeState.listening` |
| **Standby** | unprompted and classified WORK | "go to sleep" / "wake up" | `RuntimeState.sleeping` |
| **Shutdown** | the PROCESS | "exit", "turn off Kayra", Home's Shut down, the tray's Quit, Ctrl+C | `shutdown_event` |

The VISUAL for all four is resolved in one place — `core.voice_state` — from these flags plus
the STT backend status and the page's VAD. No surface composes a caption from two of them any
more; see the voice state machine section.

### Latency

- `speak()` is **non-blocking**: it queues text and returns. Callers that exit the process right
  after speaking (the shutdown farewell) must pass `blocking=True`.
- Speech starts from the first *clause*, not the first sentence — `SentenceStreamer` lets the
  opening utterance break at a comma. Measured 6.94s -> 1.44s to the first spoken word on a
  typical response, because Kokoro on CPU synthesizes at roughly real time and a 78-character
  opening sentence therefore costs ~4s before any sound.
- Audio goes through one persistent `sd.OutputStream`, written in 40ms slices. The previous
  `sd.play()`/`sd.wait()` per chunk paid a device open/close between every chunk and could not
  be aborted mid-chunk.
- The full-precision `models/kokoro.onnx` synthesizes at RTF ~1.0-1.4 and is the largest
  remaining speech-latency cost. `TextToSpeechEngine` asks for the quantized
  `kokoro-v1.0.int8.onnx` / `voices-v1.0.bin` pair first and warns when it falls back.

### TTS device selection — AUTO / GPU / CPU (rewritten 2026-09-08)

`src/kayra/output/tts_device.py` is the ONLY module in `src/` that imports `onnxruntime`, and
the only place that decides whether CUDA is usable. Asserted by `tests/test_environment.py`.

**FOUR different facts. Conflating any two produces a lie:**

| | Read from | On this dev machine |
|---|---|---|
| 1. GPU hardware exists | `nvidia-smi` | **yes** — RTX 4060 Laptop, 8 GiB |
| 2. A GPU provider is OFFERED | `get_available_providers()` | **yes** — Tensorrt, CUDA, CPU |
| 3. Its DLLs actually LOAD | the CUDA/cuDNN runtime + the DLL search path | **now yes** (was no) |
| 4. A session actually USES it | `session.get_providers()[0]` | **now yes** |

**The bug this was rewritten for sat between (2) and (3).** `onnxruntime-gpu 1.26.0` is built
against CUDA 12.8 + cuDNN 9. The venv contained no NVIDIA runtime packages, so:

```
Error loading "onnxruntime_providers_cuda.dll" which depends on
"cublasLt64_12.dll" which is missing. (Error 126)
```

**ONNX Runtime does not raise for this.** It logs, drops the provider, and returns a working
CPU session — so anything trusting (2) reported "GPU" while the CPU did all the work.

**There is a second half, and it is the easy one to miss.** The NVIDIA pip wheels put their
DLLs in `site-packages/nvidia/*/bin`, which Windows does not search. Installing them is
necessary and **not sufficient**: measured here, a session built without
`ort.preload_dlls()` still fell back to `['CPUExecutionProvider']` with every required DLL
present on disk. `prepare_runtime()` is therefore mandatory and runs before the first session.
Do not remove that call, and do not move it after a session build.

- **`prepare_runtime()`** — idempotent, cached, thread-safe. Calls `ort.preload_dlls(cuda,
  cudnn, msvc)` and RECORDS failures rather than swallowing them.
- **`probe_provider()`** — the only check that counts. Builds a session on an **84-byte
  hand-encoded ONNX model** (one `Identity` node; no `onnx` package dependency) and asks the
  SESSION which provider it got. 139ms cold, ~0.002ms cached. Probing with the real 88MB
  Kokoro graph would cost ~1.8s and allocate VRAM to answer the same question.
- **`create_session()`** — prepare -> probe -> build -> **verify again**. The probe can be
  right and the real graph still land on CPU, so the status always follows the session that
  exists, never the prediction.
- **`runtime_diagnostics()`** — one structured object (mode, active device, provider, every
  provider, cuda_available vs cuda_usable, runtime/cuDNN status, failure reason, GPU name,
  VRAM used/free/total, utilization, temperature, ORT version/package/location, interpreter,
  environment). The UI, the boot log, `run.py --doctor` and `setup.py` all read these same
  fields so they cannot drift into three accounts of one machine.

**TensorRT is discoverable but NEVER planned.** `_TTS_PROVIDER_PREFERENCE` is `(CUDA,)` and
`plan_providers()` returns `[CUDA, CPU]`. TensorRT builds an engine on first run (tens of
seconds) and a TensorRT plugin failure must not be able to stand between Kokoro and CUDA —
which is exactly what the original startup did: TensorRT first, TensorRT fails, land on CPU.
`tests/test_tts_device.py` pins this, including "TensorRT offered but CUDA still planned first".

**Kokoro cannot build its own session.** `KokoroOnnx` never calls `Kokoro.__init__` (which
hardcodes `["CPUExecutionProvider"]`); it adopts the verified session through
`Kokoro.from_session`. One session per process, always.

**MEASURED, AND NOT WHAT YOU MIGHT ASSUME.** On this RTX 4060 with the full-precision
`kokoro.onnx`:

| | CPU | CUDA |
|---|---|---|
| session build | 1.14s | 1.53s |
| RTF (synthesis / audio) | **0.888** | 0.934 |
| pure `session.run` | 7.96s | 7.70s |
| process RSS | +404MB | +1021MB |
| VRAM | 0 | +181MiB |

CUDA is **not** a speed-up for this model — ONNX Runtime reports *547 Memcpy nodes added to
the graph for CUDAExecutionProvider*, i.e. many operators the CUDA EP does not implement, so
the graph round-trips between host and device throughout. Those warnings on the console are
left visible on purpose: they are the evidence. GPU mode is correct, supported and truthful; it
is simply not faster here. **Do not "optimise" this by making AUTO prefer CPU** — the mode
contract is the user's to choose and AUTO preferring a genuinely usable GPU is specified
behaviour; the honest numbers live in `.env.example` and in the module docstring so the choice
is informed.

**Enabling GPU is `setup.py`'s job, not the user's.** `configure_speech_runtime()` detects the
GPU, keeps exactly ONE ORT variant, installs `onnxruntime-gpu`, reads the wheel's CUDA BUILD
version, installs the pinned matching NVIDIA runtime wheels (`nvidia-cuda-runtime-cu12`,
`nvidia-cublas-cu12`, `nvidia-cufft-cu12`, `nvidia-cudnn-cu12`), and then PROVES a CUDA session
initializes before claiming readiness. When it fails it names the missing package instead of
saying "GPU unavailable".

**`requirements.txt` deliberately pins no ONNX Runtime variant.** The three distributions all
install into the same `onnxruntime/` directory, and `kokoro-onnx` hard-depends on the CPU
`onnxruntime>=1.20.1` — so every `pip install -r requirements.txt` drags the CPU wheel in on
top of the GPU one. Setup lets that happen and repairs it afterwards by removing every variant
and reinstalling the keeper (uninstalling just the loser would delete files the keeper still
needs, because their RECORD manifests overlap).

**The package name is `onnxruntime-gpu`; the import is and stays `import onnxruntime`.** There
is no module called `onnxruntime_gpu`. `tests/test_tts_device.py` asserts that.

### GPU telemetry and the Home dashboard

`gpu_metrics()` — `nvidia-smi`, sampled by ONE self-parking thread every 5s that exits 20s
after the last request. Returns from a cache in 0.000ms and never blocks a paint path. No
persistent monitor process, no per-refresh subprocess.

Home's **Graphics** card shows the GPU name, utilization, VRAM used/total and temperature, plus
the provider speech is ACTUALLY using in its header pill. **The physical GPU and the TTS device
are reported separately, on purpose:** a machine can have a perfectly good RTX 4060 while speech
runs on the CPU, and hiding the GPU in that case would be as misleading as claiming acceleration
that is not happening. Both the Home card and the Settings device card read the same
`tts_device` values through the bridge, so they cannot disagree.

**A non-wrapping QLabel sets its own full-text width as its minimum.** The System card's
footprint line did exactly that, which was invisible with two cards in the bottom strip and
clipped the third card off the right edge the moment Graphics was added. Captions there are
`QSizePolicy.Ignored` horizontally and elided in `resizeEvent`.

### Cold start

`app.py` boots the TTS model and headless Chrome on threads *before* the heavy action-module
imports, then joins both before the main loop — so the three slowest stages overlap, and no
command can run against a half-initialized subsystem. Keep that ordering: moving the
`kayra.services.chatbot` / `kayra.automation.windows` imports back above the thread launches
re-serializes the boot.

- `StageTimer` (`utils.py`) records each stage; `BOOT.report()` prints the breakdown every run.
- `_check_local_server()` does a TCP connect with a short per-address budget before any HTTP
  call. The old bare `requests.get(..., timeout=1.5)` measured **3.0s** on a host with no local
  server: Windows Firewall drops (rather than refuses) connections to closed loopback ports, and
  "localhost" resolves to two address families, so the timeout was paid twice.
- Boot narration is one short spoken line. `run_boot_sequence()` prints the routing diagnostics
  and only speaks them if handed a TTS engine — `app.py` deliberately calls it without one.
  Speaking all four old startup lines cost ~19s of speech.

## The desktop UI (added 2026-09-07)

`src/kayra/ui/`. PySide6/Qt 6. Full design and reasoning in `docs/KAYRA_UI_ARCHITECTURE.md`;
what follows is the set of rules that must not be broken.

### The boundary, and why it is absolute

```
views/  ──signals──▶  bridge.py (Qt) ──callbacks──▶ session.py (no Qt) ──▶ kayra.app
```

- **No view or component may subscribe to the runtime bus, and none may import a backend
  engine.** Both are asserted by AST in `tests/test_ui.py`.
  The reason is thread affinity, not tidiness: `RuntimeState.emit()` calls subscribers
  SYNCHRONOUSLY on the emitting thread (the turn runner, the barge-in watcher, the proactive
  agent), and Qt widgets may only be touched from the GUI thread. `KayraBridge` lives in the GUI
  thread, so its signals are delivered by queued connection — that is the marshalling point, and
  bypassing it is undefined behaviour that usually appears to work.
- **`session.py` must contain no Qt at all** (asserted). It holds the threads, the boot
  sequencing and the turn pipeline, and staying Qt-free is what makes it testable headless.
- **The UI does not reimplement a turn.** `KayraSession._run_turn` calls the same functions in
  the same order as `app.Main_Loop` — confirmation gate, runtime bookkeeping, emotion, DMM,
  `Execute_Task`. Change the pipeline in `app.py` and the UI inherits it.

### Threads the UI adds

`kayra-ui-runner` (executes turns), `kayra-ui-listener` (blocks in `Listen()`), and a one-shot
`kayra-ui-profile` daemon for the device probe. The listener is unavoidable: `Listen()` blocks
with no timeout, so one thread cannot also service the text box while the microphone is open.
Do not add more.

### Rules that keep it cheap

- **No polling for assistant state.** `RuntimeState.set_state()` emits `state_changed`; the UI
  subscribes. If you find yourself adding a QTimer to read `runtime.state`, stop.
- **Every screen implements `on_show()` / `on_hide()`, and anything that polls starts its timer
  in the former and stops it in the latter.** Exactly one screen may be doing work.
- **The orb stops animating when hidden** (`hideEvent`). It is the only continuously animated
  element; 30fps busy, 12fps idle, zero when not visible.
- **No subprocess on any refresh path.** `system_profile.live_metrics()` is psutil-only.
- Automation history rebuilds only when the audit ring actually grew — comparing lengths first,
  rather than discarding and recreating the same widgets every 1.5s.

### Theme

**No hex colour may appear outside `ui/theme/`** — asserted by the test suite. Views compose
components; components carry an `objectName` the one generated stylesheet targets. Changing a
dynamic property (`variant`, `tone`) requires `theme.repolish(widget)`: Qt does not re-evaluate
property selectors on change, and forgetting it is the most common way Qt theming looks broken.

Direction is amber-on-graphite, deliberately NOT the blue/purple AI default. The accent's hue is
asserted to be in the amber range so a future change cannot quietly drift back to blue.

Interaction states are NAMED tokens (`surface_hover`, `surface_active`, `accent_subtle`,
`disabled`) rather than each component inventing its own "slightly lighter" — which is how a
dark UI ends up with six different hover greys. `automation` is the semantic name; `copper` is
the pigment.

**There is no background image, and that is a decision, not an omission.** The layered graphite
already carries the depth; an image behind text on a near-black ground costs contrast the type
cannot spare; and Home has exactly one focal point — a texture around the orb competes with the
only element that is meant to hold the eye. `ui/assets/` remains the drop-in location.

### Component vocabulary

Views compose components; they never style a widget themselves. The shared set is
`Card`, `StatusPill`, `CardAction`, `SegmentedControl`, `Disclosure`, `IconButton`, `ListRow`,
`Meter`, `StatRow`, `Toggle`, `EmptyState`, `Metric`, `GroupLabel`, `Divider`, `RowRule` plus
the text helpers. **If a row appears on two screens it must be the same component** — Activity,
Automation, Memory and Home all build their rows from `ListRow` for that reason; before the
refinement pass they were four hand-built `QHBoxLayout`s that had already drifted apart in
padding, chip width and timestamp format.

### Empty states are shown and hidden, never created and destroyed

`QLayout.takeAt` removes an item from the LAYOUT; it does not hide or delete the widget, and
`deleteLater` only runs when control returns to the event loop. An empty state that is added on
one render pass and "removed" on the next is therefore still a visible child at its stale
geometry, and the new rows are laid out **on top of it**. That is exactly what happened in
Activity: "Nothing yet" rendered straight through the middle of the timeline text.

Every empty state is now a permanent child outside the rebuilt layout, re-labelled through
`EmptyState.set_message()` and toggled with `setVisible`. A destroyed one also cannot come back
when the list empties again, which leaves a blank rectangle indistinguishable from a failed
paint.

### Three "stop"-shaped concepts, and they must never share a flag

This is the distinction most easily broken by a well-meaning change:

| Concept | What it does | Entry point | Runtime |
|---|---|---|---|
| **Barge-in** | cancels the sentence being SPOKEN | `tts_engine.stop()`, the local control watcher, Ctrl+. , the composer's stop button | `note_interrupt()` |
| **Listening pause** | closes the MICROPHONE | `app.set_listening(False)`, Home's button, Ctrl+M, the tray, the spoken `stop listening` | `RuntimeState.listening` |
| **Standby** | stops unprompted and CLASSIFIED work | `app.set_sleeping(True)`, the spoken `go to sleep` / `wake up` | `RuntimeState.sleeping` |
| **Shutdown** | ends the PROCESS | `app.request_shutdown()`, Home's **Shut down Kayra** button, the tray's Quit, the spoken `exit` / `turn off Kayra` | `shutdown_event` |

A bare **"stop"** is a barge-in and is handled by the audio layer — it never reaches the DMM.
**"stop listening"**, **"go to sleep"**, **"wake up"** and **"exit"** are matched by the local
control layer (`core.voice_control`) BEFORE the DMM; the DMM tokens remain as the fallback for
phrasings only the classifier catches. **"exit"** is the only one that ends the process.

- **Pausing does NOT tear the STT session down.** `SpeechToTextEngine.pause_listening()` calls
  the page's `stopContinuousRecognition()`, which is what actually releases the microphone, and
  leaves the driver, the browser, the loopback page server and the owned-PID ledger untouched.
  A full teardown would cost the 1.3–2.5s session rebuild every time someone paused for a phone
  call. Verified at the browser level: the page's status goes `listening` → `stopped` →
  `listening` across a pause/resume, with all 9 owned PIDs unchanged.
- **`capture()` returns immediately while paused** and the UI listener parks on an Event, so a
  closed microphone costs no polling at all.
- **`start listening` exists as a phrase but cannot help you when it matters.** A paused
  microphone cannot hear the command to un-pause it, so resuming is in practice a manual action
  — the button, Ctrl+M, or the tray. The phrase is in the vocabulary because it costs nothing
  and does the right thing when listening was paused by some other surface; it is not a promise
  that a closed microphone can be reopened by voice. **Standby is the answer for "be quiet but
  keep hearing me"** — that is exactly why it does not close the microphone.
- **`RuntimeState.listening` is a separate axis from `state`,** not another value of it.
  Overloading `state` would make "paused" mutually exclusive with SPEAKING, which is wrong in
  both directions: Kayra can finish a sentence with the microphone already closed, and it can
  be idle while still listening.

### Chat auto-scroll: activate the layout BEFORE reading `maximum`

`bar.setValue(bar.maximum())` on a `QTimer.singleShot(0, ...)` does not work, and adding a
longer delay only makes it intermittent. `maximum` is derived from the content widget's size
hint, which is not recalculated merely because a child was added — so the deferred call reads
the bottom from BEFORE the message arrived and the newest content hangs below the viewport.

It is worst for the messages that matter most: a tall reply moves `maximum` further, so the
longer the answer the more of it is cut off. Measured against the old implementation: a short
message was fine, a long reply overflowed by **249px**, an automation block by **280px**, and a
burst of messages by **1347px**.

The correct order, in `ChatView._scroll_to_end`:

1. `layout.activate()` on the content layout — children get final geometry
2. `adjustSize()` on the content widget — its size hint is recomputed
3. `setValue(maximum)` — now `maximum` reflects the new content
4. one deferred repeat for anything that grows afterwards (a word-wrapped label whose height
   depends on the width it is finally given; a streamed sentence appended to a live bubble)

No sleep, and no guessed pixel offset. **Follow-latest**: `_following` is driven by the
scrollbar's own `valueChanged`, so it is correct however the position changed; scrolling up
more than one line of text (`FOLLOW_THRESHOLD_PX`) parks the view, returning to the bottom
re-arms it, and sending a message always re-pins.

### One presentation at a time: dashboard XOR ambient panel

The ambient panel is the COMPACT FORM of the dashboard, not a companion to it.
`KayraWindow._sync_presentation()` is hooked to `showEvent`, `hideEvent` and
`WindowStateChange`, so the swap follows the window however it is shown or hidden — tray,
close button, minimise, taskbar. Closing the dashboard is a change of presentation and never
touches shutdown; quitting stays in the tray, on the authoritative `_force_shutdown` path.

**Click and drag must be told apart** (`DRAG_THRESHOLD_PX`). A frameless window has no title
bar, so the whole surface is the drag handle — and a drag ends with a release over the widget,
which is indistinguishable from a click. Without the threshold every attempt to nudge the panel
also opened the dashboard. The orb inside it is `WA_TransparentForMouseEvents` for the same
reason: an orb that emitted `clicked` would fire mid-drag.

Placement uses `QGuiApplication.screenAt(QCursor.pos())`, so the panel appears on the monitor
being used rather than the primary one, and `ensure_on_screen()` recovers it if the monitor it
was parked on goes away. There is no edge snapping — free placement was the requirement, and a
panel that jumps as you release it is worse than one that does not.

### Qt traps already hit here — do not reintroduce

- **A plain `QWidget` ignores stylesheet background and border** unless
  `setAttribute(Qt.WA_StyledBackground, True)` is set. QFrame does it implicitly. This silently
  rendered the System score tiles as bare text.
- **`QPainter` on a pixmap with a devicePixelRatio works in LOGICAL units.** Drawing to
  `size * dpr` painted every navigation icon at double scale, showing only the top-left quarter.
- **A word-wrapped `QLabel` reports a tiny width hint**, so inside a layout with a stretch it
  collapses to a narrow column. Chat bubbles measure their text and set a minimum width.
- **A `QThread` parented to a widget aborts the process** if the widget is destroyed while the
  thread runs — which is what happens when someone opens System and closes the window within
  three seconds. The device probe uses a plain daemon thread with a standalone emitter kept
  alive by a module-level set; Qt then drops the late result instead of delivering it to freed
  memory.
- **`psutil.cpu_percent(interval=None)` returns a meaningless first reading.** It is primed once
  in `live_metrics`, or the dashboard opens showing a red 100% processor bar.
- **A stylesheet `min-height` BEATS `setFixedSize`.** The generic `QPushButton` rule sized the
  40x22 `Toggle` to 32px tall and its drawn knob rendered as a clipped half-circle. A
  self-painted control needs its own objectName and a rule neutralising the generic geometry.
- **Qt does not clamp an oversized `border-radius` the way CSS does.** `border-radius: 999px`
  on `StatusPill` fell back to a small radius, so every status chip in the application rendered
  as a rectangle. Use an explicit half-height radius.
- **A `:disabled` rule loses to a more specific `[variant]` selector.** An accent button stayed
  fully amber while disabled — the loudest dead control a screen can have. Variants need their
  own `:disabled` rules.
- **Vertical `padding` ADDS to `min-height` in Qt.** 8px of padding on an input made every field
  48px tall and a two-line settings row 110px.
- **`QIcon.paint(painter, rect)` and `QIcon.pixmap(w, h)` RESCALE the artwork** to fill the
  target. Neither can detect a glyph drawn at the wrong scale, because a quarter-drawn glyph
  fills the target too. To test the high-DPI contract, inspect the stored pixmap
  (`icon.pixmap(QSize(size, size))`) and assert the glyph keeps its margin.
- **A layout stretch AFTER a widget with a stretch factor wins the leftover height back.** A
  trailing `addStretch(1)` silently cancelled the "let the last card fill the page" fix.
- **`QScrollBar.maximum()` is stale until the layout is activated.** See the auto-scroll note
  above; this is the single most consequential Qt timing trap in this UI.
- **A pixmap's `devicePixelRatio` must match the screen's, or Qt upscales it and the strokes go
  soft.** Glyphs hardcoded `dpr=2`, which is an upscale on the 2.5x and 3x displays that exist
  and a mismatch on the 1.25/1.5/1.75x scale factors Windows laptops actually ship at.
  `theme.glyph_dpr()` reads the ratio from the screens, rounds UP to a whole number (a
  fractional ratio puts stroke centres on half pixels) and caches it.
- **`isVisible()` is False for any widget whose parent chain is hidden.** To ask whether a
  widget was deliberately hidden, use `isHidden()` — several tests were wrong before this.
- **A view is CONSTRUCTED AND SHOWN BEFORE THE BACKEND EXISTS**, and an event-driven control
  is never corrected for a value that never changed. `KayraWindow.__init__` builds every
  screen and navigates to Home seconds before `KayraSession` boots; `RuntimeState` emits only
  on a real transition, so a control painted from a pre-boot read stays wrong indefinitely.
  Connect `bootFinished` and re-read. See the boot-window section above for the bug this
  actually caused.

### Shutdown

`app.request_shutdown()` is authoritative (`_force_shutdown` is now its signal-handler adapter,
kept because `ui.session`, `ui.application` and the tests all reach shutdown by that name). The
UI delegates through `bridge.shutdown(hard=True)` and does not reorder, duplicate or precede it
— the ordering (shutdown flag, proactive agent, timers, audio, browser, PID reap, UI hooks)
exists for reasons documented in the architecture. The test suite asserts the session contains
no process-cleanup code of its own, and `tests/test_voice_control.py` asserts the ORDER.

- **Home carries a `Shut down Kayra` button**, on its own row below the two routine controls and
  in the danger tone. It confirms first, then disables itself and the other controls — teardown
  takes a couple of seconds (nine browser processes to reap) and a second click during that
  window was the easiest way to re-enter a shutdown that was already half done. It calls exactly
  what the spoken "turn off Kayra" and the tray's Quit call.
- **`request_shutdown` is idempotent.** Concurrent callers are guarded by an Event plus a lock;
  the first runs the sequence, the rest return.
- **Presentation comes off the screen LAST**, through `app.on_before_exit()`. That hook is for
  the window and the tray icon and nothing else — it stops no threads and releases no resources.
  It checks thread affinity: a click runs it on the GUI thread (direct `hide()`), a spoken
  shutdown runs it on the watcher thread and it posts across with `QMetaObject.invokeMethod`.
  Hiding the UI FIRST would show the user a finished shutdown while the browser session was
  still being reaped.
- **It still ends in `os._exit(0)`, deliberately.** That is the last statement, reached only
  after every resource is released — not a shortcut past cleanup. The process is full of daemon
  threads parked in native code (PortAudio's callback, urllib3 sockets inside Selenium, ONNX
  Runtime's intra-op pool); returning from `main()` instead reliably added seconds to a quit the
  user had already asked for, and occasionally hung.

### Settings: the speech device

The `Speech device` card carries the AUTO / GPU / CPU dropdown and, SEPARATELY, what the live
ONNX session is actually running on. **The two lines are not redundant** — they disagree
whenever a GPU is requested and cannot be initialized, which is the normal case on a stock
install, and a screen showing only the mode would display "GPU" while synthesis ran on the CPU.
With no live engine the card says "Would use", not "Active device": a prediction is never
presented as a measurement. The GPU telemetry line is the only timer on the screen and it runs
only while the screen is visible.

## Browser choice for speech input (added 2026-09-07)

`src/kayra/input/browsers.py`. Kayra does NOT require Chrome. It drives whichever installed
browser can actually transcribe, preferring the user's Windows default.

**The trap, and why capability detection is mandatory.** `webkitSpeechRecognition` is not
self-contained: it needs a speech BACKEND, and different builds ship different ones. Chrome
carries Google's key, Edge uses Microsoft's, and Brave deliberately ships neither. A
backendless browser still exposes the entire API — the object exists, `start()` succeeds,
`onstart` fires — and then recognition dies with `onerror{error:'network'}` and never returns
a transcript. So "launch the user's default browser" is silently wrong, and
`'webkitSpeechRecognition' in window` proves nothing.

Measured live on this host (Chrome 152 / Edge 152 / Brave 152, headless, against the real page):

| Browser | Result |
|---|---|
| Chrome | session started, no error — USABLE |
| Edge | session started, no error, produced a result — USABLE (Microsoft's own backend) |
| Brave | started, then `network`, session ended — UNUSABLE |

**Edge is why "the user has no Chrome" is survivable** — it is preinstalled on every Windows 11
machine and works. Measured: a full STT session on Edge starts in 1.3s with 8 owned processes.

### Rules that are load-bearing

- **The page must NOT restart a `network` failure forever.** `onerror` used to treat `network`
  as transient and restart unconditionally; in a backendless browser that is an infinite loop
  in which the assistant looks alive, burns CPU and hears nothing. It is now bounded
  (`MAX_NETWORK_ERRORS`, 3) and only while `kayraEverRecognized` is false — a successful result
  clears the counter, so a genuine mid-session blip never trips it. `onend` must also refuse to
  resurrect a dead backend, or the bounded check becomes unbounded through that path.
- **Only SUCCESS is cached** (`data/browser_support.json`). A `network` error means "no backend
  reached", which is structural for Brave but temporary for Chrome on an offline machine.
  Persisting a negative would let one offline boot permanently demote a good browser. Rejections
  are session-scoped (`_rejected_browsers`) and never written to disk.
- **Verification must not cost cold start.** A blanket 4s probe on every start added ~2.8s to
  boot and pushed crash recovery 2.5s -> 6.5s. `_probe_budget()` therefore TRUSTS browsers with
  a first-party backend (Chrome, Edge) or a cached verification, and probes only unknown /
  known-backendless ones. Trusted browsers are checked later for free: `capture()` already reads
  the page status every poll in the same round-trip it uses to pop the speech queue, so a dead
  backend is caught within one 50ms poll and `_switch_browser()` moves to the next candidate.
  Measured after this: Chrome 1.4s, Edge 1.3s, recovery back to 2.5s.
- **A backendless browser declares itself dead in 1.55-1.78s** (measured, 3 trials), so the 4s
  ceiling is evidence-based. Keep it comfortably above that if you change it.
- **`binary_location` is required for every Chromium derivative except Chrome.** Without it
  ChromeDriver silently launches Chrome instead — which "works" while ignoring the browser the
  user asked for. Edge uses its own driver class.
- **The user's default browser is never launched or modified.** Kayra reads the registry
  UserChoice to learn the preference and starts its own headless session; it does not touch the
  browser the user actually browses with. When the default cannot be used, say so out loud —
  they set that default deliberately.
- Firefox is deliberately not in `_FAMILIES`: recognition is disabled by default and it has no
  bundled backend, so offering it is only a slower path to the same failure.
- `STT_BROWSER` in `.env` (`auto` | `chrome` | `edge` | `brave` | ...) forces a preference. It
  is a preference, not an override of reality: a browser without a backend is still rejected.
- Tests: `tests/test_browser_selection.py` (64 checks, hardware-free).

## The STT browser session — one per process, owned by PID

`SpeechToTextEngine` owns exactly ONE ChromeDriver and ONE Chrome session for the lifetime of
the Kayra process. `get_shared_engine()` is the only sanctioned entry point (main.py and the
legacy `recognize_speech()` both use it); constructing a second engine while one is live logs
a warning, because that means a second browser.

Lifecycle states: `NOT_STARTED -> STARTING -> READY <-> LISTENING`, plus `RECOVERING`,
`STOPPING`, `STOPPED`, `FAILED`. `_start_session`, `recover` and `shutdown` are the only
writers and each holds `_lifecycle_lock` across the whole transition.

- **Never create a session before the old one is gone.** `recover()` runs teardown, waits for
  the owned PIDs to actually disappear, and only then builds the replacement.
- **Detect death by PROCESS, not by WebDriver.** `_service_alive()` is a `psutil` check on the
  ChromeDriver PID and costs microseconds. Discovering the same thing through Selenium costs
  ~16s of urllib3 connect retries *while holding the driver lock*, which freezes the barge-in
  watcher too. Measured end-to-end recovery: 45s before this, 2.5s after.
- **Never speak WebDriver to a corpse.** `_teardown_session(session_dead=True)` skips
  `execute_script` and `quit()` (~16s each against a dead driver) and uses `service.stop()`
  (0.00s) plus PID termination (0.04s).
- **Process ownership is PID-based, never name-based.** `owned_pids` records the ChromeDriver
  PID and every Chrome PID beneath it. This is not a stylistic preference: `automation/windows.py`
  opens applications through AppOpener, which uses `subprocess.Popen`, so a Chrome window Kayra
  opened FOR THE USER becomes a child of the Kayra process. The old
  `children(recursive=True) + name matches "chrome"` sweep in `_force_shutdown` would have
  closed the user's browsing session on exit. Do not reintroduce name matching, and never
  `taskkill /IM chrome.exe`.
- The recognition page is served from `http://127.0.0.1:<ephemeral>` by a small loopback
  server, NOT a `data:` URL. A `data:` URL has an opaque origin and is not a secure context, so
  `navigator.mediaDevices` is `undefined` there and the page's echo-cancellation request was
  silently dead code. Verified after the change: `isSecureContext` true and
  `echoCancellation: true` in the live track settings.
- Footprint (measured on one host): 10 processes / 541MB before, 9 / 472MB after. The Chrome
  flags responsible were chosen by measuring each set, not copied from a list — see
  `_chrome_options`.

## DMM prompt contract

Measured on `tests/test_dmm_matrix.py` (53 cases across 15 intent boundaries): the classifier
was already at 52/53 before this round of work, so the changes were targeted rather than a
rewrite. Current: 53/53, 0 duplicate-token cases, 0 unexecutable-token cases.

- **`self.funcs` is the acceptance gate, so a token in it that no executor handles is worse
  than useless** — it passes the filter and is then silently dropped by the automation router,
  and the user gets nothing. `generate image` was exactly that (no image module exists) and was
  removed; `save`, `search` and `print` were the opposite problem — implemented in
  `HotkeyShortcut`'s map but never dispatched, now handled by an exact-match tuple in
  `translate_and_execute` branch 9 (exact match, so "search" cannot shadow "google search ...").
- **The broad closes are their own tokens** (`close all`, `close everything`), added
  2026-09-08 with two preamble lines and NO change to `dmm_chat_history` — precisely because
  appending there shifts recency weight and has silently cost a case before. Verified live:
  "Close all my chrome windows." -> `close all chrome`, "Close everything." -> `close
  everything`, while "Close chrome." stays `close chrome` and never widens.
- **Assistant self-control is a DMM token, not an audio interrupt.** `proactive on` /
  `proactive off` are dispatched by `app.py::Execute_Task`, never by the automation router, and
  the preamble states explicitly that a bare "stop" is handled by the audio layer and never
  reaches the classifier. Verified live: "stop proactive suggestions", "don't interrupt me" and
  "disable proactive mode" all classify to `proactive off` while "stop the music" stays
  `stop media`.
- **Placeholders must never reach the output.** The preamble writes `'general ...'`, not
  `'general (query)'`, because the model copied the literal word "query" as the payload. There
  is also a parser guard that substitutes the user's real words if a placeholder payload
  appears — this matters because `deep research` slices its topic out of the token, so a
  placeholder there would research the word "topic".
- Few-shot ordering is unchanged in principle and stricter in practice: the list now ENDS with
  contrastive pairs (close app / close window / close tab, minimize / minimize all, look-up vs
  open-results-page, authoring vs keystrokes, and automation keywords inside ordinary
  questions). Still never slice this list.
- The rate-limit path recurses with `retries + 1` and gives up after 3 attempts. It previously
  recursed with the SAME counter, so a sustained Cohere rate limit was an unbounded recursion.
- **Recency weight is strong enough to matter, so what sits LAST is a design decision.** Adding
  `proactive on` / `proactive off` (2026-09-07) put a media example (`stop the music` ->
  `stop media`) in the final position, and that alone dragged `Undo that.` from `undo` to
  `resume` — 53/53 down to 52/53, reproducibly. Moving an `undo` / `redo` contrastive pair to
  the very end restored 53/53. If you append to `dmm_chat_history`, re-run
  `tests/test_dmm_matrix.py`; do not assume an addition at the end is free.
- A stale `generate image` few-shot pair survived the removal of that token from `self.funcs`,
  so the model was still being taught to emit something the acceptance gate would silently
  discard. Removed 2026-09-07. When you delete a token from `funcs`, delete its examples too.

## Speech output vs display output

`utils.speech_safe_text()` is the only place that converts a model response into what the TTS
engine says. The console keeps the model's own formatting; only the speech copy is normalized.

- Removes what exists only on screen: markdown, emoji, table pipes, rules, bullet glyphs, bare
  URLs, citation brackets, code fences (replaced by a short spoken placeholder).
- **Pronounces what carries meaning**: `%`, currency, degrees, `&`, `=`, `+`, `/`. The previous
  inline cleaner was `re.sub(r'[^\w\s\.,!\?\-\'"]', '', text)`, which deleted every symbol it
  did not recognise — "50%" was spoken as "50" and "$20" as "20".
- Typographic punctuation is mapped to ASCII FIRST. U+2019 is category Pf, so without that
  mapping every contraction broke apart ("Rust's" -> "Rust s").
- The final sweep filters by Unicode CATEGORY (keep L/N/M), not by a `\w` allow-list, because
  `\w` excludes combining marks and would strip Devanagari vowel signs out of Hindi replies.
  This is the same bug class as the one fixed in `is_interrupt_phrase`; don't reintroduce it.
- The identity prompt tells the model not to emit markdown in the first place. Both layers
  exist deliberately and must stay consistent.

## Turn cancellation is epoch-scoped

A response stream captures `tts.turn_token()` once and asks `tts.is_cancelled(token)`. It must
NOT read the global `interrupted` flag: that flag is process-wide and `begin_turn()` clears it,
so a proactive suggestion firing in the window between a barge-in and the chatbot noticing it
would un-cancel the interrupted response and let it carry on speaking. Background utterances
use `begin_background_utterance()`, which never touches the turn's instrumentation.

## The proactive service (`src/kayra/services/proactive_agent.py`)

Rewritten 2026-09-07. The previous implementation was a `schedule`-driven timer that polled the
foreground window every 15s, held a single global cooldown, and spoke through a callback
`app.py` guarded by hand. The current one is a scored candidate engine with an explicit
lifecycle. Read this before touching it — several of the constraints below are the reason it is
shaped the way it is.

### The pipeline, and the one thing it must never become

```
cheap local tick (clock + foreground window + counters)
   -> candidate detected        (three signals: time, habit, context)
   -> deterministic local score
   -> cooldown gate             (global / per-kind / per-wording)
   -> safety gate               ("is the user mid-anything?")
   -> WAIT for a safe window, or drop when stale
   -> phrase it                 (template; LLM optional, validated, never required)
   -> speak through the ONE existing TTS pipeline
   -> learn from the reaction
```

**It must never call an LLM to decide whether to speak.** Every decision above — candidate
existence, relevance, cooldowns, safety — is integer arithmetic over values already resident in
memory. Measured: a full scored `evaluate()` costs 0.0063ms and an idle tick 0.0087ms, so the
observation loop is free; an LLM per tick would be a cloud round-trip every 20 seconds forever.
The LLM is consulted at most once per *spoken* suggestion, purely to reword a candidate that has
already been approved, and the whole subsystem works with the network down.

### Lifecycle

`DISABLED -> IDLE -> OBSERVING -> CANDIDATE -> WAITING_FOR_SAFE_WINDOW -> SPEAKING -> COOLDOWN
-> (OBSERVING) ... STOPPING -> STOPPED`

The states are not decoration. `COOLDOWN` is what makes most ticks skip candidate generation
entirely (the cheapest path, and the one taken for most of the hour after any suggestion), and
`WAITING_FOR_SAFE_WINDOW` is what turns "never interrupt the user" into a deferral rather than a
drop. `set_enabled(False)` (the voice switch) moves straight to `DISABLED` and clears any
pending candidate.

### Triggers (three signals, one candidate each)

| Kind | Signal | Requires |
|---|---|---|
| `break` | context + time + habit | at least 75% of `PROACTIVE_FATIGUE_MINUTES` unbroken focus in one app, a plausible hour, and an app the habit model has actually seen used |
| `late_night` | time | inside `PROACTIVE_LATE_NIGHT_*`, and the user demonstrably still at the machine |
| `habit_routine` | habit | an action with at least `PROACTIVE_HABIT_MIN_COUNT` observations whose hour histogram fires in the current hour, not already done in the last 6h |

### Scoring

`score = 0.30*habit + 0.25*temporal + 0.35*context + 0.10*recency - 0.5*annoyance`, clamped to
[0,1] and compared against `PROACTIVE_SCORE_THRESHOLD` (default 0.60).

**Those weights encode a hard requirement.** Context alone maxes out at 0.35, which is below the
threshold — so foreground-window information can never on its own cause the assistant to speak.
That is enforced by the arithmetic rather than by a special case, and
`tests/test_proactive_agent.py` asserts it. If you change `W_CONTEXT`, keep it under the
threshold.

Deliberately no ML. This decides when the assistant talks unprompted, so it has to be
predictable and debuggable before it is clever; every input is readable off `data/habits.json`
and every decision is printed with its component breakdown.

### Cooldowns

Three independent timers, all of which must have expired (`_cooldown_ok`): global
(`PROACTIVE_GLOBAL_COOLDOWN_MINUTES`, 60), per-kind (break 90, late_night 240, habit_routine
120 — whichever is longer than global wins), and **per-exact-wording**
(`PROACTIVE_REPEAT_COOLDOWN_MINUTES`, 360). The last is the backstop: a scheduler bug that
re-fires the identical sentence in a loop cannot get past it even if the other two are
misconfigured to zero. Its ledger is pruned on every speak, so it stays bounded.

### The safety gate — `is_safe_window()`

The ONLY place proactive speech is authorised. It refuses when the agent is disabled, the
runtime is shutting down, the assistant is in any busy state (PROCESSING / SPEAKING /
INTERRUPTING / AUTOMATING / SHUTTING_DOWN), a turn is open, audio is still queued or draining,
or the user spoke / interrupted / finished a turn within `PROACTIVE_QUIET_SECONDS`.
`create_default_agent` re-checks `is_playing` one more time immediately before queueing, which
closes the small window between the gate and the queue push.

A refusal defers the candidate (state `WAITING_FOR_SAFE_WINDOW`, faster poll) until
`PROACTIVE_MAX_DEFER_SECONDS`, then drops it. A `barge_in` event discards it outright.

### TTS integration — the race this design exists to avoid

Proactive speech goes out through `begin_background_utterance()` + `speak()`, wired in
`create_default_agent`. **`begin_turn()` must never be called from the proactive path.** It
clears the `_interrupted` latch, and a nudge landing in the window between a barge-in and the
chatbot noticing it would then let the cancelled response carry on speaking. That is the same
race `text_to_speech.turn_token` documents from the other side, and it is why the module takes
`speak_fn` as a callable instead of importing the TTS engine — it is structurally unable to
reach the cancellation epoch. `tests/test_proactive_agent.py` greps the source for
`.begin_turn(` as a regression guard.

There is no second audio queue, no second player, no second output stream, and no separate
interruption mechanism: "stop" cancels a proactive line through exactly the same `tts.stop()`
path as any other response.

### LLM usage

Uses the shared `CentralizedLLMEngine` singleton — never a new client. `_phrase()` returns the
template unless phrasing is enabled AND a `phrase_fn` exists AND the result survives
`_validate_phrasing` (max 220 chars, max 2 sentences, no markdown / URLs / code fences /
bullets), after which it still goes through `utils.speech_safe_text`. Anything else falls back
to the template. Any exception falls back to the template. Verified: with the phraser raising,
a suggestion is still produced and spoken.

### Habit model (`data/habits.json`, v2)

Counters and 24-bucket hour histograms only — **never conversation transcripts**.
`note_intents()` records the DMM's action tokens; `general` / `realtime` / `deep research` /
`content` / `write` / `copy text` payloads are dropped by `_habit_key`, and everything else is
reduced to a bounded key (`open:chrome`, `take screenshot`).

Every collection has a retention policy: `actions` is capped at `PROACTIVE_MAX_HABIT_ACTIONS`
(60) and `apps` at `PROACTIVE_MAX_HABIT_APPS` (40), least-observed evicted first; hour
histograms are 24 fixed ints; the candidate "queue" is a single slot, never a list. Measured:
100,000 recorded habit events grow RSS by 0.01MB and the file stays at 39KB. Writes are atomic
(tmp + replace). A v1 file (the old `app_totals` shape) is migrated on load; a corrupt one falls
back to empty.

Reactions are recorded as accepted / ignored / dismissed / interrupted and feed `annoyance()`,
which is deliberately gentle — three ignores roughly halve a candidate's headroom, and it takes
sustained rejection to silence a trigger.

### Voice control

`proactive off` / `proactive on` are DMM tokens (in `self.funcs`, dispatched by
`app.py::Execute_Task`, NOT by the automation router). "stop proactive suggestions", "don't
interrupt me" and "disable proactive mode" all classify to `proactive off`; verified against the
live DMM.

These are unrelated to the audio interrupt and must stay that way. `is_interrupt_phrase()`
matches EXACTLY on the filler-stripped utterance, so "stop proactive suggestions" (3 words) is
never swallowed as a barge-in — the same rule that keeps "stop the music" a real command.

### Startup / shutdown

`start()` returns in ~5ms and the first tick is one interval later, so proactive mode adds
nothing measurable to the cold start. `stop()` sets the Event (the thread is sleeping *on* it,
so it wakes immediately rather than up to a tick later), joins with a bounded timeout,
unsubscribes from the event bus, commits the current app's time and flushes — measured 2.7ms.
`app.py::_force_shutdown` sets `RUNTIME.shutdown_event` and stops the agent FIRST, before the
audio and browser teardown, so it can never hand text to an engine being disposed.

### Known limitations

- The context signal needs `pygetwindow` (Windows). Without it the agent runs on the time and
  habit signals only, and says so at boot.
- `habit_routine` predicts *that* a routine is due, not *what to do about it* — it offers, it
  does not act. Wiring acceptance back into `Automation` is deliberately not done.
- App identity comes from the window title's tail segment, so two apps sharing a title suffix
  collapse into one habit entry.
- Reaction classification is keyword-based ("yes"/"no"/...), so a user who answers a nudge in an
  unusual way is recorded as having ignored it.
- The late-night window is wall-clock, not calendar-aware; it fires on holidays too.

## The speech capture pipeline (rebuilt 2026-09-08)

Recognition accuracy is a PIPELINE, and the order is the design:

```
audio capture -> AEC / noise suppression -> VAD / endpointing -> STT (N-best) -> repair
[--------------------- the recognition page, in the browser ---------------------]  [Python]
```

The ordering is load-bearing and was chosen over the tempting alternative. A misheard word
is easiest to "fix" with a replacement table, and that is the wrong answer: every such table
eventually rewrites a legitimate word into the wrong command, silently, on the one utterance
that most needed to be taken literally. So the effort goes into hearing correctly, and the
correction stage is the smallest, last and most constrained part of the system.

### Stage 1 — capture

`primeProcessedMicrophone()` requests `echoCancellation`, `noiseSuppression`,
`autoGainControl`, mono, 16 kHz — and then READS BACK `track.getSettings()` into
`window.kayraAudioSettings`.

**Constraints are a request, not a promise.** Whether they are granted depends on the browser
and the device, and an assistant that assumes it got echo cancellation misdescribes the one
thing that explains its mistakes. `app._report_audio_pipeline()` prints what was actually
granted and warns about what was not — the same rule the speech-device card follows for the
ONNX provider. Measured on this host: echo cancellation, noise suppression and gain control
all granted, mono, on `Microphone Array (Realtek(R) Audio)`.

**That report runs on its own daemon thread, and must stay there.** `getUserMedia` is
asynchronous, so the session is READY before the device is granted; reading once inline
printed "microphone settings unavailable" on every boot, and waiting inline would have put
the delay into cold start. It polls up to `AUDIO_REPORT_TIMEOUT_S`.

This whole stage only works because the page is served from `http://127.0.0.1` — see
`_PageServer`. On a `data:` URL `navigator.mediaDevices` is undefined and all of it is dead
code.

### Stage 2 — echo

Chrome's canceller removes much of Kayra's own voice at the source, but **it is not the
guarantee and cannot be**: its reference signal is the browser's own playout, and Kayra's TTS
plays through PortAudio in the Python process. The guarantee remains the capture-timestamp
ledger (`app._is_self_echo` against `was_audible_between`) — do not weaken it on the strength
of the AEC.

What the page adds is an ECHO-AWARE VAD threshold: while `window.kayraSpeaking` is set, the
voice threshold is raised to `vadEchoMargin` (7.0) from `vadMargin` (3.2), and the noise floor
is **not** adapted at all. Learning the floor from Kayra's own voice would raise it until the
detector went deaf. Verified live: threshold 0.0349 while speaking vs 0.0190 while quiet.

### Stage 3 — VAD and endpointing

A `Float32Array` RMS reading off an `AnalyserNode` every 50 ms, with an adaptive noise floor
(slow EMA over quiet frames only). Voice is RMS above a MULTIPLE of the learned floor, not a
constant — a fixed threshold is wrong in a quiet room and a noisy one both.

**The endpoint needs BOTH conditions**: the recognizer quiet AND the room quiet
(`recognizerQuiet && roomQuiet`). Recognizer-silence alone was the old rule and it is wrong in
a way that costs words: results LAG the sound by a variable amount, so a fixed "no results for
800ms" ends the utterance mid-sentence whenever the backend is slow. A clipped word is not a
misheard word — it is a missing one.

Three timings, all configurable:
* `fastEndpointMs` (420) — a SHORT, already-committed command endpoints sooner. "stop" should
  not cost 800 ms.
* `interimGraceMs` (1400) — words the recognizer has NOT committed extend the wait.
* `maxWaitMs` (6000) — nothing waits forever.

**The truncation bug this replaced:** the old timer pushed `currentText`, which accumulates
FINAL segments only, and cleared everything. Interim text that had not been finalized was
discarded — so a word spoken just before the endpoint could produce nothing at all.
`flushUtterance()` now delivers it, flagged `uncommitted`, and the repair stage knows to trust
it less. Delivering a doubtful word beats delivering none.

The microphone is **never** connected to `audioCtx.destination` (the test suite asserts it):
playing the mic back through the speakers is the feedback loop the rest of this pipeline
exists to suppress. With no WebAudio the endpointer degrades exactly to the old
recognizer-only behaviour rather than failing.

### Stage 4 — recognition

`recognition.maxAlternatives = 5`. This is the single most important line for accuracy,
because it is what lets the next stage prefer a reading **the recognizer itself proposed**
rather than inventing one. Each finalized segment's alternatives and confidences travel with
the utterance on the queue.

`_alternatives()` returns them **only for a single committed segment**. For a multi-segment
utterance the honest answer is "none": combining per-segment lists would manufacture readings
the recognizer never proposed.

### Stage 5 — conservative repair (`src/kayra/input/transcript_repair.py`)

Three rules, in order of the evidence they demand:

1. **Re-rank the recognizer's own N-best** when the top reading is NOT plausible in the
   current context and a lower one is. Invents nothing. The asymmetry is what makes it safe:
   if the recognizer's first choice already fits, it wins however well an alternative also
   fits.
2. **One tiny phonetic step**, only when EVERY guard passes: ≤3 words (and in practice exactly
   one), confidence below `CONFIDENT_ENOUGH` (0.85) or unknown, the word means nothing in this
   context, the target IS plausible right now, same coarse phonetic key, edit distance ≤2, and
   an unambiguous winner. Two equally close candidates is a refusal, not a coin toss.
3. **Otherwise nothing** — the overwhelmingly common and correct outcome.

**It can never invent a shutdown.** `_is_irreversible()` filters any phonetic target that
`classify_control` maps to `SHUTDOWN`. "exist" and "exit" are one edit apart and share a
phonetic key, so without this the stage would eventually quit the assistant over a word the
user said perfectly. Note the asymmetry: an alternative the RECOGNIZER offered may be a
shutdown, because it genuinely heard it; what is forbidden is this stage manufacturing one.
The exception fallback returns `True` (unsafe) — a missing repair costs a repeated command, a
wrong one costs the session.

**There is no word-replacement dictionary and there must never be one.** The test suite
asserts this STRUCTURALLY, by walking the parsed module for a dict literal mapping words to
words — a grep would fail on the docstring that promises there isn't one. The only string map
is `_CODES`, a single-letter phonetic alphabet.

**Vocabulary is INJECTED, never imported.** `app._repair_stage()` passes
`CentralizedLLMEngine.funcs` (102 terms on this install). That is what keeps `kayra.input`
from reaching into `kayra.intelligence` on the recognition path, and it means the plausible
words are exactly the ones this assistant can act on. The control vocabulary comes from
`voice_control.control_kinds()` / `phrases_for()` — enumerated through the module's own
accessors, so a new control kind joins automatically and a rename cannot silently empty it.

Every change prints `[TRANSCRIPT] 'x' -> 'y' (reason)`. An invisible correction layer is
worse than none.

**What this does NOT fix, stated plainly:** "quit" heard as "great" is not phonetically close
(`q300` vs `g630`) and no guard in rule 2 will ever bridge it. That pair can only be fixed
upstream — by the capture and endpointing work above — or by rule 1, if the recognizer offers
"quit" among its alternatives. Claiming otherwise would be claiming a dictionary by another
name.

## Conversation context (`src/kayra/core/conversation_context.py`, added 2026-09-08)

`RuntimeState` answers "what is the assistant DOING?"; this answers "what is the conversation
ABOUT?" — recent turns, the last intent, the last automation target, whether a question is
outstanding, and the content words the exchange keeps returning to.

It exists because three parts of Kayra were each guessing at it: the repair stage needs to
know what is PLAUSIBLE before it may prefer one reading over another; the presence layer
wanted a topic and had only a window title; and the confirmation flow, the standby check and
the turn loop each read a fragment of the same picture from a different place.

- **STATE, not MEMORY.** `memory/conversation.py` owns durable storage and writes to disk.
  Nothing here is persisted, and the suite asserts the module contains no file I/O at all.
- **A leaf.** Stdlib only — asserted. Anything may import it, including `input`, which must
  never import `intelligence` to learn what a plausible word is.
- **Bounded everywhere:** 8 turns, 24 topic terms, 8 targets, 240 chars per utterance. The
  topic counter DECAYS (halve-and-drop) rather than evicting, so a topic the user moved on
  from fades instead of being kept alive by one early mention.
- **Non-strings are discarded, not stringified.** `str(None)` is `"none"`, a plausible-looking
  token that would then be treated as an intent header. A malformed input turning into a
  real-looking one is worse than being dropped.
- **The assistant's own words are remembered but never enter the topic vocabulary.** Letting
  them would bias the repair stage towards words the USER never said.
- **Clearing a confirmation RESTORES the previous mode**, it does not zero it. Answering "yes"
  to "shall I close Chrome?" returns to the automation exchange it interrupted; reporting IDLE
  there told the repair stage the conversation had no context at the moment it most obviously
  did. Found live.
- Written by `app.Main_Loop` AND `ui.session._run_turn`, in the same order, next to the
  existing `RUNTIME` bookkeeping — the UI does not reimplement a turn, and that includes the
  state a turn maintains. One process-wide accessor, for the same reason `RuntimeState` has
  one: two copies would mean the turn loop writing to one while the repair stage read the
  other.

Presence consumes it too: `PresenceSignals.topic` / `.conversation_mode` fill the "current
conversation topic" signal that section 6 of the presence design asked for and had no source
for, and the topic reaches the LLM realization contract as `recent_topic` — content words the
user actually used, never a summary Kayra invented.

## Proactive presence (`src/kayra/intelligence/proactive_presence.py`, added 2026-09-08)

The contextual layer on top of the proactive service. It answers ONE question — *does the
assistant have a genuinely good reason to say something right now?* — and it is an
EXTENSION of the agent above, not a replacement for it. The thread, the habit model, the
safety gate, the global cooldown and the single route to the TTS pipeline are all unchanged;
presence plugs into three seams and adds nothing else:

```
agent.evaluate()        -> presence.candidates(signals)     extra reasons to speak
agent._presence_...     -> presence.should_speak(candidate) extra suppression rules
agent._phrase()         -> presence.llm_prompt(candidate)   the wording contract
```

There is no second agent, no second thread, no second TTS queue, no second event bus, no
second cooldown architecture and no second store. `git grep threading.Thread` in this module
returns nothing, and `tests/test_proactive_presence.py` asserts that.

### What it adds

| Kind | Tier | Signal |
|---|---|---|
| `battery_low` | CRITICAL | unplugged and below `PROACTIVE_BATTERY_WARN_PERCENT` |
| `system_pressure` | CRITICAL / IMPORTANT | RAM or CPU held above threshold for N consecutive samples |
| `repeated_failure` | IMPORTANT | the same action failed twice inside the window (anticipation) |
| `work_session` | IMPORTANT | an unbroken stretch of INTERACTION passed a milestone |
| `late_night` | SOCIAL | inside the late window, user demonstrably present |
| `user_return` | SOCIAL | foreground window unreadable for a long run, then readable again |
| `repeated_action` | AMBIENT | the same request several times in a few minutes |
| `dry_remark` | AMBIENT | one observational remark, only where the context carries it |

Plus two REACTIVE surfaces that are not candidates at all: `greeting()` (the contextual reply
to a bare greeting) and `boot_line()` (the one spoken startup line, now aware of the hour).

### The rules that are load-bearing

- **The default is silence, and it is enforced by arithmetic.** A candidate must clear its
  tier's score floor, the per-kind cooldown, the cooldown GROUP, the presence spacing, the
  daily budget and a similarity check against what was said recently — and THEN the agent's
  global cooldown and `is_safe_window()`. `should_speak()` returns `(False, reason)` for
  everything it does not positively approve.
- **The tier IS the priority, and no evidence promotes a candidate out of it.** Each tier is
  `base + evidence * strength`; the strongest possible AMBIENT remark scores below the
  CRITICAL floor, so an aside can never masquerade as a warning. Merge order in
  `evaluate()` is `(tier, score)`, so an IMPORTANT observation always outranks a
  higher-scoring AMBIENT one.
- **Only CRITICAL bypasses the agent's global cooldown.** Staying quiet about a dying battery
  for the rest of an hour because a break was suggested at the top of it would be the cooldown
  working correctly and the assistant failing anyway. During the global cooldown the tick
  calls `evaluate(critical_only=True)`, which builds two candidates from numbers already
  sampled — the cheap path stays cheap.
- **The cooldown GROUP is what stops one situation being remarked on three times.** `break`,
  `work_session`, `late_night` and `dry_remark` all describe "you have been at this a long
  time"; they share the `fatigue` group, so a long night produces one remark, not three true
  ones in a row.
- **ZERO LLM calls to decide anything.** `candidates()` and `should_speak()` are integer and
  string arithmetic over resident values. Measured **14.6us** per full evaluation in the
  suite, **31.4us** live. `PROACTIVE_PRESENCE_LLM_PHRASING` is OFF by default and, even when
  on, `llm_prompt()` returns None for every kind where wording does not benefit — a warning is
  never sent to a model, because a warning that waits on a cloud call is a worse warning.
- **Repetition detection must not depend on the spacing.** It did: the similarity window was
  `min_gap_s * 24`, so setting the spacing to zero — a legitimate configuration — switched
  repetition detection off entirely rather than making it stricter. It is now a fixed
  `REPETITION_WINDOW_S` (6h). The backstop against saying the same thing twice cannot be a
  function of how often speech is allowed.
- **`annoyance_fn` is a lambda, not a bound method.** Binding `self.habits.annoyance` captures
  the habit store that exists at construction, and anything that later replaces `self.habits`
  leaves presence consulting the old one. That is not hypothetical — it made presence read the
  developer's real `habits.json` inside a suite that had isolated everything else.
- **"Good night" is a farewell, not a greeting.** `day_part()` has four values and English has
  three salutations; `salutation()` maps night to evening. Found in live testing, where the
  engine correctly read 02:15 as night and then said the one thing nobody says on being
  greeted. The suite now checks all 24 hours.
- **`is_greeting()` matches the WHOLE utterance**, name and filler stripped — the same rule
  that keeps the local control vocabulary safe. "hello" is a greeting; "hello, open Chrome" is
  an instruction, and a prefix match would swallow it.
- **The address is a placeholder in every template, and every pool has a wording without it.**
  After the form of address is used, address-free wordings are preferred for
  `PROACTIVE_ADDRESS_GAP_MINUTES`, which is what keeps "sir" out of every sentence.
- **The agent's `late_night_enabled` is authoritative over both layers.** `owns_kind()` lets
  presence take over the wording for that kind, but the agent's switch is checked first and
  presence candidates of that kind are filtered in `_presence_candidates` — one setting,
  `PROACTIVE_LATE_NIGHT_ENABLED`, read by both.
- **Nothing new is watched.** The foreground window is the sample the agent ALREADY takes
  (forwarded including its `None` case, which is what "the machine is locked or unattended"
  means); system pressure is `system_profile.pressure_sample()`, which is three psutil reads
  and deliberately NOT `live_metrics()` — that one also walks Kayra's process tree for the
  System screen, and a tree walk every minute forever to learn a memory percentage is exactly
  the background cost this codebase does not accept.
- **Task completion and failure reporting are NOT duplicated here.** The automation layer
  already returns the sentence to speak for every action, including failures. Presence adds
  only what that layer cannot see: that the SAME action has now failed more than once.

### Integration points outside the module

- `services/proactive_agent.py` — constructs it, merges its candidates, forwards
  `user_utterance` (once per turn, from `user_utterance` only — both `app.Main_Loop` and
  `ui.session` emit that AND `intent_classified`, so counting both would double every session
  length), `intent_classified`, `automation_result` and `barge_in`, and records
  `note_spoken()` only for a line that actually reached the speaker.
- `automation/windows.py` — `translate_and_execute` emits `automation_result` with counts and
  action identities. Never the user's words.
- `app.py` — `presence_engine()`, the contextual boot line, and the greeting branch in
  `Execute_Task`, which is resolved ONCE per task (calling the builder in the `elif` condition
  and again in its body rendered two wordings and recorded both as "recently said").
- `ui/` — a Proactive presence card in Settings (live AND persisted, like the device
  dropdown) and a Presence card on Home that hides itself when the layer is not running.

### Known limitations

- Return detection infers "away" from the foreground window being unreadable. A user who sits
  reading a full-screen document Kayra cannot title is not distinguishable from one who left.
- The work-session stretch counts INTERACTIONS with Kayra, not time at the machine. Someone
  working silently for three hours has, as far as this signal is concerned, not been working.
- `pressure_sample()` reports CPU only from its second call onward (psutil's first reading is
  meaningless), so the first minute of a session has memory and battery but no CPU figure.
- The daily budget is wall-clock calendar, like the late-night window. It rolls at midnight
  regardless of whether the user's day did.

## Live speech-backend switching (`src/kayra/input/stt_backend.py`, added 2026-09-08)

`STT_BROWSER` used to be a `.env` value read exactly once, inside `SpeechToTextEngine.__init__`.
Choosing "Google Chrome" in Settings therefore wrote a string to a file and did nothing else:
the live session kept running on whatever it started with, the screen showed the new value, and
the two disagreed until the next restart. **A setting that changes only a screen is not a
setting.**

### REQUESTED is not ACTIVE, and they are separate fields

```
requested_backend   what the user asked for      "chrome"
active_backend      what is running right now    "edge", or None
status              how that came to be          OFF/STARTING/LISTENING/PAUSED/
                                                 RECOVERING/STOPPING/ERROR
```

They are equal on the happy path and DIFFERENT whenever a switch failed. A screen that renders
"Google Chrome" because a dropdown says Chrome, while Edge holds the microphone, is the exact
lie this module exists to prevent — Settings shows both lines, and the pill reads
`Not applied` rather than `Ready` when they disagree.

`auto` MATCHES any active backend by definition: automatic means "whichever one works", so a
session on Edge under `auto` is a satisfied request, not a mismatch.

### The transaction

```
record the request -> stop the old session -> start the requested one ->
verify it can transcribe -> publish -> log -> commit to .env
```

`.env` is written ONLY on success. A setting that failed to apply must not come back after a
restart claiming to be the configuration.

- **An explicit choice is STRICT.** `SpeechToTextEngine._start_session(strict=True)` restricts
  the attempt to the named browser and nothing else. With the ordinary candidate list,
  choosing Chrome on a machine where Chrome cannot reach a backend would quietly bring up Edge
  and report success. `auto` never uses strict mode — the capability logic in
  `kayra.input.browsers` is unchanged.
- **A failed switch restores the previous backend**, so a wrong setting does not leave Kayra
  deaf, and then reports the failure. Returning True on a failed start is the whole thing this
  path exists to prevent.
- **Teardown happens before the rebuild, and waits.** `_teardown_session` then
  `_await_owned_termination`, the same order `recover()` uses — two live sessions would mean
  two browsers holding the microphone.
- **Only Kayra's own PIDs are reaped.** `owned_pids` is unchanged, so a switch cannot touch the
  user's browser. Verified live: seven switches, 16/16 of the developer's own browser processes
  alive throughout, 0 leaked, 8/8 Kayra processes reaped at shutdown.
- **Pause is a separate axis and a switch does not change it.** Switching while listening was
  paused leaves it paused.
- **A request arriving mid-switch is REFUSED, not queued** — queueing would mean a burst of
  dropdown clicks each tearing the browser down in turn. The user's latest choice is still
  recorded as `requested`.

### The engine lookup must never import the STT module

`STTBackendManager._engine()` reads `SpeechToTextEngine._active_instance` out of `sys.modules`
by STRING — the same technique, and the same reasoning, as
`automation.targets.kayra_owned_pids`: a Settings screen asking "which backend is active?" must
not boot a headless browser as a side effect.

**Because the lookup is by string, a module RENAME turns this off silently rather than
raising** — exactly what happened to `kayra_owned_pids` after the package reorganisation.
`ENGINE_MODULES` pins the path and `tests/test_stt_backend.py` asserts it against the live
module. If the STT module moves, update that tuple.

### Measured

Edge -> Chrome -> Edge -> Automatic -> Chrome, live, on this machine: **3.5-3.6s per switch**,
one active backend after every one, zero leaked processes. A named browser that is not
installed: **not committed**, `requested=Vivaldi`, `active=Google Chrome`, `matches=False`.


## The voice state machine (`src/kayra/core/voice_state.py`, added 2026-09-08)

The assistant visual said **"Listening paused" while the user was talking to it.**

That was not a wrong label; it was four independent writers to one piece of screen. The ambient
panel wrote its caption from `listeningChanged`, Home wrote its own from a cached `_listening`
plus a cached `_state`, the sidebar wrote a third, and the orb inferred a fourth from whatever
`set_state` reached it last. Each was correct about the fact it held and none held all of them,
so the screen showed whichever writer spoke most recently — and during an STT recovery, or the
instant after a barge-in, that was reliably the wrong one.

So this does not patch a label. It resolves the whole question ONCE, from facts, and publishes
the answer. **Nothing downstream may infer a voice state; it renders the one it is given.**

### The states

`OFFLINE`, `STARTING`, `LISTENING`, `USER_SPEAKING`, `PROCESSING`, `ASSISTANT_SPEAKING`,
`PAUSED`, `STANDBY`, `RECOVERING`, `STOPPING`, `ERROR`.

Deliberately NOT the same enum as `AssistantState`. That one is the turn machine; this also has
to express a closed microphone, standby, a session being rebuilt, and the difference between a
microphone that is open and a user who is talking into it.

### The precedence, and why it is this order

1. **Shutdown** outranks everything — the only irreversible thing here.
2. **Standby** outranks the turn machine: a sleeping Kayra may still be draining a final
   sentence, and reporting SPEAKING then invites the user to talk to something that will not
   answer.
3. **The TURN outranks the SESSION.** If the STT session drops while a reply is generating, the
   truthful headline is that Kayra is working — the microphone is not what the user is waiting
   on.
4. **A barge-in is `USER_SPEAKING`**, not "interrupted". The old label described what had
   happened to Kayra; the user needs to know they have the floor.
5. **Session trouble sits ABOVE the pause check**, so an STT recovery reads as "Reconnecting…"
   and can never render as a false pause.
6. **`PAUSED` requires the microphone to be deliberately closed.** Nothing else reaches it.
7. Everything left over is `LISTENING`, with VAD choosing between `LISTENING` and
   `USER_SPEAKING`.

**SILENCE IS STILL LISTENING.** A microphone that is open and hearing nothing is LISTENING. A
late transcript, a slow interim result or a quiet VAD window does not change it — and there is
no `transcript`, `silence` or `timeout` input to the machine at all, so it is structurally
incapable of turning one into a pause. `tests/test_voice_state.py` sweeps every fact
combination and asserts none of them can produce PAUSED with the microphone open.

### Revisions

Every committed transition carries a monotonically increasing revision. A Qt callback that
arrives late — a queued signal delivered after a newer one, a timer that fired during a
switch — carries a revision no greater than the one already rendered and is DROPPED. Without
this, an old asynchronous callback repaints a state that is no longer true: not just the wrong
writer, but the right writer arriving in the wrong order. Every consuming view keeps
`_voice_revision` and returns early.

### Debounce

Three transient states have a minimum dwell — `USER_SPEAKING` 260ms (bridges the gap between
two words), `RECOVERING` 400ms (a 300ms reconnect must not flash), `STARTING` 250ms. Nothing is
over half a second, and `IMMEDIATE_STATES` exempts everything the user just did (pause, standby,
shutdown, an error) and everything the assistant is now doing for them (processing, speaking,
hearing them). Only a fall back to a RESTING state can ever be delayed, which is exactly the
set where one noisy sample produces a visible flicker.

### Absorbing shutdown

Once `shutting_down` is observed the machine latches: the only reachable states are `STOPPING`
and `OFFLINE`. A late VAD sample from a watcher thread that has not noticed yet cannot repaint
a live microphone. The latch is on the OBSERVATION, not on the state's identity — `OFFLINE` is
also the state the machine starts in, and treating it as absorbing unconditionally makes the
machine unable to boot.

### Where the facts come from

`app.py` is the ONLY thing that feeds the machine, because it is the only thing that sees every
fact. Four producers each report the one thing they know:

| Producer | Fact |
|---|---|
| the runtime bus (`state_changed`) | what the turn machine is doing |
| `set_listening` / `set_sleeping` | what the user asked for |
| the local control watcher | what the page's VAD hears, read from the poll it ALREADY makes |
| the STT backend manager | what the session is doing |

VAD costs nothing extra: `poll_controls` reads `window.kayraVad.voice` in the same round-trip
it uses for the interrupt flags. A separate poll would be a second Selenium command per tick
over the driver lock the capture loop needs.

**One transition log, in one place, at INFO** (`[VOICE] State: LISTENING -> USER_SPEAKING`). Not
per animation frame, not per VAD sample, and never again in the UI.

### The boot window: a screen built before the backend is ready

`KayraWindow.__init__` builds every view and shows Home **several seconds before**
`KayraSession` finishes booting. Anything a screen paints in that window is painted from
whatever the bridge can answer with no backend behind it, and **an answer given then is not a
measurement**.

This produced a reported bug that looked like the orb bug but is a different one: the
microphone button read **"Start listening" beside an orb that was listening**, and only fixed
itself after the user toggled listening off and on again.

Three things had to line up for it:

1. `KayraSession.listening_enabled()` returned `False` when `self._runtime is None`. That is an
   ABSENCE OF INFORMATION reported as a NEGATIVE FACT — the same defect class as
   requested-vs-active, and the same one this milestone is otherwise about.
2. Home's `on_show()` painted the control from that value during the boot window.
3. Nothing ever corrected it. `RuntimeState._listening` starts `True` and never changes, so
   `set_listening` — **correctly** — never emits `listening_changed`. There was no event to
   fix the label with, which is why only a real toggle (two genuine events) repaired it.

**The rule, and it is general: a screen constructed before the backend is ready must RE-READ
when it becomes ready.** `bootFinished` is that signal, and Home and Chat both connect to it.

The fix has three parts, because fixing any one alone leaves the bug reachable:

* `listening_enabled()` no longer claims the microphone is closed before boot — it returns the
  runtime's own starting value — and `listening_known()` says whether the answer is a
  measurement at all. `voice_runtime_state()` carries both, so the caption, the orb and the
  control all come from ONE read; taking the caption from the snapshot and the button from a
  separate `listening_enabled()` call is exactly how the two came to disagree.
* While the answer is unknown the control is **disabled**, not guessed at. That is also the
  truth about what it can do: `set_listening` returns False before the session exists, so an
  enabled button there would be a control that silently does nothing.
* `bootFinished` re-syncs, and `_shutting_down` latches so a late re-sync cannot re-enable
  controls during a teardown already in progress.

`tests/test_ui.py::section_boot_ordering` pins all three, including that the correction happens
with **zero** `listeningChanged` events — proving the fix does not secretly depend on one.
Verified against the real backend: sampled once a second across a 9-second boot, the button
read "Pause listening" throughout and agreed with the microphone at the end with no toggle
performed.

### Two ordering defects found by the live boot test, not by the unit tests

* **`barge_in` must not latch "the user is speaking".** That event is emitted by every path
  that silences speech, including ones with no user in them — entering standby and starting a
  shutdown both cancel playback and both emit it. Setting `voice_active=True` on it latched the
  fact with nothing to clear it, and the visual read "Listening…" for the rest of the session
  with the room silent. A real spoken barge-in is already covered twice without it.
* **`set_sleeping` silences AFTER setting the standby flag, not before.** `_interrupt_speech`
  moves the turn machine to INTERRUPTING, which resolves to USER_SPEAKING, so silencing first
  rendered a phantom `LISTENING -> USER_SPEAKING -> STANDBY` with nobody talking. The silencing
  still happens before the function returns, so Kayra cannot fall asleep mid-sentence and keep
  talking.


## Memory management (`src/kayra/memory/store.py`, added 2026-09-08)

`memory/conversation.py` stays the ONLY owner of the file and of the atomic write. This is the
management surface over it — there is no second store, and a management layer that kept its own
copy would disagree with the first the moment a chat turn appended while the screen was open.

### Stable identity

The Memory screen previously deleted **by position** — the row's index into the last thirty
entries. That is wrong in a way that is easy to miss and impossible to recover from: the store
is appended to by the running assistant, so the entry at index 4 when the screen rendered is not
necessarily the entry at index 4 when the button is clicked.

Every record now carries an `id`: a short content-derived hash, **written into the record** the
first time it is seen and persisted with it. Content-derived so the same store always yields the
same ids; persisted so a later edit cannot orphan an id the UI is holding; disambiguated by an
occurrence ordinal so two identical memories remain two deletable things. A colliding id from a
hand-edited file is re-issued rather than making deletion ambiguous.

**The migration is additive.** No field is removed or renamed, `role`/`content` are untouched,
and `chatbot.py` reads it unchanged. A non-dict entry from an older build is WRAPPED, never
dropped.

### The rules

- **Delete re-reads the store**, never trusting what the screen is holding — between the render
  and the click a chat turn may have appended.
- **Persistence is verified before anything is reported as gone.** A failed write returns False
  and the UI leaves the row on screen; a UI that removes a row on click and finds it back after
  a restart is worse than one that admits the failure.
- **"Clear all" writes an empty list. It never deletes, moves or truncates a file.**
  `tests/test_memory_store.py` walks the AST for `remove`/`unlink`/`rmtree`/`rmdir`/`truncate`
  and asserts there are none, and that `shutil` is not imported.
- **Explorer is launched with an argument VECTOR and `shell=False`** — `["explorer.exe",
  "/select,<path>"]`, one argument, as Explorer requires. No `os.system`, no `cmd /c`, no
  `powershell`, and never a concatenated string. A missing file opens the parent folder instead,
  because `/select` on a missing path opens Documents, which is a confusing non-answer.
  Explorer's exit code is deliberately NOT treated as failure — it routinely returns 1 having
  worked; a failure to LAUNCH is reported with the exact path so the user can navigate by hand.
- **Logs carry counts and ids, never content.** `[MEMORY] Deleted: id=a1b2c3d4e5f6`,
  `[MEMORY] Cleared: 37 memories`. This is by construction the most sensitive text in the
  process and a terminal log is the least private place it could end up.
- The path comes from `core.paths` and is never composed in the UI.


## Structured terminal logging (`src/kayra/core/logbus.py`, added 2026-09-08)

One shape for every line Kayra's newer subsystems emit:

```
[21:48:03] [INFO   ] [STT] Backend: Google Chrome
[21:52:14] [INFO   ] [AUTO] Turn #184 · Target: YouTube
```

`utils/console.py` still owns the THEME and the `print_*` helpers, and every historical call
site keeps working. This owns the FORMAT, and renders through `safe_print`, so there is still
exactly one Console and one place that copes with a closed terminal.

- **One canonical name per subsystem.** `Subsystem.STT`, never `[Speech input]` / `[Speech]` /
  `[Recognizer]` in three places. `tests/test_logging.py` walks the AST of the retrofitted
  modules and fails on any literal string passed as a subsystem.
- **One owner per event.** The provider router owns provider/fallback lines; the STT backend
  manager owns backend transitions AND session recovery; the settings recorder owns setting
  changes; `app` owns voice-state transitions. No UI module imports the logger at all —
  asserted. Three real duplicates were removed while building this: the provider block printed
  by both `run_boot_sequence` and the startup report, the AEC/microphone line printed by both
  `_report_audio_pipeline` and the startup report, and the TTS provider printed by both
  `text_to_speech` and the startup report.
- **DEBUG is not printed at INFO** — interim transcripts, VAD levels, presence scores, provider
  exception text and dropped stale revisions all live there. Nothing is HIDDEN; it is moved.
- **An expected recoverable failure never dumps a traceback at INFO.** `logbus.exception()`
  logs one line and keeps the traceback at DEBUG. A rate limit prints `Result: RATE_LIMITED`
  and `Fallback: Groq`, not a provider stack trace.
- **Never a secret.** `redact()` runs on every message, including ones this module did not
  compose — an SDK error that echoes the key it was handed is exactly the case a call site
  would not think to guard. It rewrites only values whose NAME identifies them as a credential
  plus a few unmistakable key shapes: over-redacting makes a real failure undebuggable.
- **The level is configuration, not a code change:** `KAYRA_LOG_LEVEL` (DEBUG/INFO/WARNING/
  ERROR, default INFO), read from the process environment first so one run can be made verbose
  without editing anything. `KAYRA_LOG_FILE` adds a rotating 2 MB × 3 debug log.
- **Third-party loggers are raised to WARNING, never disabled and never raised past it** —
  urllib3 announcing every WebDriver connection at 17Hz is noise; a real Selenium failure is
  not. ONNX Runtime is deliberately untouched: its provider warnings are the evidence for the
  GPU account `tts_device` gives.
- `SUCCESS` is its own printable word but the SAME threshold as INFO: someone who quietened the
  log to warnings does not want a stream of successes either.

### The startup report

`app._report_startup()` runs after every subsystem is up, on one thread, and states what Kayra
is actually running with — model routing, speech provider and device, speech backend and
microphone, memory count, presence. **Every value is read from the LIVE subsystem, never from
configuration**: a boot report that recited `.env` would say "GPU" on a machine where synthesis
is running on the processor.


## Shared runtime state (`src/kayra/core/runtime_state.py`)

`RuntimeState` is the assistant's state machine plus a minimal synchronous event bus, and it is
a process-wide singleton (`get_runtime_state()`) for the same reason `CentralizedLLMEngine` is:
two copies would give two disagreeing answers to "is the assistant speaking?", and the proactive
agent would be reading the one nobody writes.

- States: `IDLE`, `LISTENING`, `PROCESSING`, `SPEAKING`, `INTERRUPTING`, `AUTOMATING`,
  `SHUTTING_DOWN`. The last five are `BUSY_STATES` — nothing unprompted may be spoken in any of
  them. `AUTOMATING` is set around the `Automation()` dispatch in `Execute_Task`.
- Timestamps: `note_user_utterance()` / `note_interrupt()` / `end_turn()`. The proactive safety
  gate is expressed entirely in terms of these, so they must keep being called — the barge-in
  watcher calls `note_interrupt()`, and `Main_Loop` closes the turn in a `finally` (a turn left
  latched open by a crashed turn would read as "user is mid-command" forever and mute the
  proactive agent for the rest of the session).
- `snapshot()` takes ONE locked read of everything a policy decision needs, so a decision is
  never made against half-updated values.
- Events: `emit(name, **payload)` fans out synchronously to subscribers and swallows their
  exceptions — it is called from the main loop and the barge-in watcher, and a listener bug must
  not be able to wedge either. Emitted: `user_utterance`, `intent_classified`, `barge_in`.
- The clock is injectable (`RuntimeState(clock_ms=...)`) so the "is it safe to speak?" policy can
  be tested at exact offsets instead of by sleeping. Production always uses the wall clock.

It holds STATE, never RESOURCES: it does not own the TTS engine, the STT session or the LLM
client and must never import them, so anything is free to import it.

## Memory

- Two tiers: `session_memory` (in-RAM list, capped to the last 6 messages, per-process — reset
  on restart) and `permanent_memory` (JSON file, only appended when the user says a trigger
  phrase: "store this", "remember this", "save this", "memorize this", "note this").
- **Management** — listing with stable ids, deleting one, clearing all, and revealing the file
  in Explorer — lives in `memory/store.py`. See the memory management section above; the
  persistence helpers below remain the only writers.
- Persistence helpers live in `src/kayra/utils/`: `get_data_paths()`, `load_conversation_memory()`,
  `save_conversation_memory()`. Always resolve paths via `get_project_root()` — never hardcode
  `"data\\conversation.json"` as a bare relative path (it used to be, and silently fragmented
  memory across files if the process wasn't launched from the project root; fixed 2026-09-03).
- Writes are atomic: the backup file is written first, then copied over the primary, so a crash
  mid-write can't corrupt the primary DB.
- `chatbot.py` and `real_time_search.py` both consume these same helpers — don't reintroduce
  a second copy of load/save/answer-cleanup logic in a new module; add it to `utils.py` instead.

## Configuration

All runtime config lives in `.env` (see `.env.example` for the full annotated list — never
commit a real `.env`). Groups: speech (`INPUT_LANGUAGE`, `ASSISTANT_VOICE`, `TTS_DEVICE_MODE` —
`AUTO` | `GPU` | `CPU`, validated and clamped), local-vs-cloud
(`FORCE_ONLINE`, `LOCAL_*`), cloud API keys (`CohereAPIKey`, `GROQ_API_KEY`, `GEMINI_API_KEY`),
identity (`ASSISTANT_NAME`, `ASSISTANT_GENDER`, `USERNAME`, `USER_GENDER`, `LANGUAGE`), the
proactive service (all `PROACTIVE_*` — master switch, tick cadence, the three cooldowns, the
score threshold, the late-night window, habit-store caps, LLM phrasing), automation
(`AUTOMATION_CONFIRM_TTL_SECONDS`, `AUTOMATION_SHELL_TIMEOUT_SECONDS`,
`AUTOMATION_SCREENSHOT_KEEP`, `AUTOMATION_MAX_TIMERS`), deep research tuning
(`MAX_SUB_QUESTIONS`, `MAX_FOLLOWUP_QUERIES`, `MAX_DEEP_PAGES`, `SEARCH_RESULTS_PER_QUERY`),
provider failover (`PROVIDER_COOLDOWN_*` per failure kind, `PROVIDER_TIMEOUT_SECONDS`), and
logging (`KAYRA_LOG_LEVEL`, `KAYRA_LOG_FILE`).

The automation and provider knobs are all bounds, not behaviour switches: there is deliberately
no setting that disables the safety policy, the confirmation prompt, the provider cooldown or
the fallback order. Every provider cooldown is clamped to 0-86400s and the request timeout to
5-300s, so a malformed `.env` cannot stand a provider down for a week or reintroduce an
unbounded hang.

**Two settings are LIVE, not next-start:** `STT_BROWSER` switches the running speech session
(see the live speech-backend section) and `TTS_DEVICE_MODE` switches the running ONNX session.
Both are transactional — applied first, written to `.env` only on success — so a change that
failed cannot come back after a restart claiming to be the configuration.

`ProactiveConfig` reads the process environment (`os.environ`), not `dotenv_values`, so it
depends on `app.py` having called `load_dotenv()` first — which it does, before the module is
imported. Every knob has a defensive default and is range-clamped, so a malformed `.env` cannot
crash a background thread at boot.

## Dev workflow

```
python setup.py           # once (or after changing requirements.txt, or to repair the runtime)
python run.py             # every time — desktop UI, no venv activation needed
python run.py --console   # voice + terminal only, no UI
python run.py --doctor    # interpreter, ONNX Runtime, providers, verified CUDA, GPU stats
```

**Never install ONNX Runtime by hand.** `setup.py` owns which variant is present and installs
the matching CUDA runtime wheels; a manual `pip install onnxruntime` on an NVIDIA machine
silently replaces the GPU build with the CPU one, and nothing in the application can tell.

- Python 3.11 virtualenv at `.venv/` (3.10 is the floor; 3.12 is allowed but unverified).
  `setup.py` creates and validates it; don't hand-roll the install.
- Sanity-check any change before assuming it's correct:
  ```
  .venv/Scripts/python.exe -m py_compile main.py setup.py run.py $(find src tests -name "*.py")
  .venv/Scripts/python.exe -m pyflakes src/kayra tests run.py setup.py main.py
  ```
  **Run pyflakes specifically after moving files.** `py_compile` does not catch undefined names,
  and the reorganisation left `utils/timing.py` importing `console, print_info` while using
  `safe_print` and `Rule` — the assistant crashed on boot with `NameError`, and compiling was
  clean.
- `tests/*.py` are standalone diagnostic scripts, not a pytest suite — run each directly.
  Three tiers:
  - **A passing UI suite does NOT mean the interface looks right.** Every fault the refinement
    pass fixed — 1590px of bare background across five screens, widgets drawn on top of each
    other, clipped labels, internal names on screen, ragged control widths — was present while
    131 checks passed. Those checks are written AFTER a rendered review finds something, so the
    specific fault cannot return silently. Review by rendering the screens and looking at them.
  - **Hardware-free, assert, exit non-zero — run these first.** `test_automation.py` (263
    checks: normalizer table, DMM token coverage, policy, confirmations, target resolution and
    ambiguity, context referents, planner ordering, filesystem, timers, audit, AST assertions,
    performance; DRY by default, `--live` adds read-only Win32 checks), `test_proactive_agent.py`
    (140 checks), `test_emotion_engine.py` (119 checks), `test_browser_selection.py` (64
    checks), `test_ui.py` (299 checks; Qt `offscreen`, backend fully stubbed),
    `test_voice_control.py` (186 checks: the interrupt/lifecycle vocabulary, the
    Kayra-vs-computer shutdown boundary, tail matching, the JS/Python agreement, and shutdown
    idempotency + ORDER against a fully stubbed backend with `os._exit` replaced),
    `test_capture_pipeline.py` (132 checks: the conversation context and its bounds, the
    phonetic key and distance, N-best re-ranking, every condition the repair stage REFUSES
    on — including an AST proof that no word-replacement table exists and that a shutdown
    can never be invented — the recognition page's capture/VAD/endpointing contract, and the
    turn-loop wiring),
    `test_proactive_presence.py` (213 checks: greeting routing across the clock and across
    absences, every contextual candidate and the evidence it requires, the full suppression
    matrix, the tier arithmetic, tone and phrasebook discipline, the zero-LLM-during-
    evaluation claim asserted by counting 720 evaluations, integration with the real agent,
    speech routing and bounded state),
    `test_tts_device.py` (143 checks: ORT variant sanity, DLL preparation, the real cached
    provider probe, mode validation, provider planning with TensorRT excluded, the failure
    modes simulated by substituting the provider list, the structured diagnostic, a real
    session in every mode plus a real runtime switch, and telemetry cost),
    `test_environment.py` (63 checks: `run.py` interpreter ownership and the sys.path rule,
    a real refusal to run on the system interpreter, import origin, the single-ORT-import rule,
    `setup.py` provisioning/pins/repair, and agreement between setup's CUDA probe and the
    application's),
    `test_target_resolution.py` (108 checks: the website registry, open-target typing,
    single-target close, strict-vs-loose matching, close semantics, tab semantics, AST proof
    that no call site fuzzy-matches, STT protection, and the resolution performance budget),
    `test_provider_router.py` (124 checks: the two hierarchies and that they stay separate,
    all eight failure kinds plus `Retry-After`, exact fallback order with at most one call per
    provider per request, cooldown expiry on an injectable clock, a 20-request storm test
    proving one Cohere call rather than twenty, streaming fallback and the no-splice rule, the
    fallback log lines, and integration with the real engine and its unsliced DMM prompt),
    `test_stt_backend.py` (127 checks: requested-vs-active, every transition the brief names,
    a named backend that cannot start, switching while listening/paused/recovering, one
    teardown per switch with teardown before rebuild, rapid and re-entrant requests, the
    backend logs, and an AST proof that the engine lookup never imports the STT module;
    `--live` adds real browser discovery),
    `test_memory_store.py` (138 checks: stable content-derived ids and their persistence,
    listing an empty/missing/corrupted store, delete-by-id including the position bug it
    replaced, clear-all plus an AST proof that no file-removal call exists, atomicity through
    the existing helper, the Explorer argument vector, and that no memory content reaches a
    log; sandboxed into a temp directory and restored),
    `test_voice_state.py` (196 checks: all 1176 fact combinations resolving to a declared
    state, silence never becoming PAUSED, barge-in, recovery never becoming a false pause,
    standby vs pause, absorbing shutdown, revisions and staleness, the dwell table, all the
    named sequences end to end, the orb amplitude contract, and the leaf-module rule) and
    `test_logging.py` (149 checks: the one format, canonical subsystem names with an AST proof
    that no call site invents one, level thresholds and DEBUG staying out of INFO, nine shapes
    of secret redacted plus benign text left intact, one-owner-per-event asserted against
    every module that could duplicate it, the settings recorder's transactional shape,
    third-party noise control that does not disable anything, and cost).
  - **Needs network or hardware.** `test_dmm_matrix.py` (53 intent-boundary cases, paced under
    Cohere's rate limit — but see the note above: it follows the same local-first routing as the
    assistant, so with LM Studio up it measures the LOCAL model), `test_audio_pipeline.py`
    (44 checks; needs the TTS model),
    `test_stt_lifecycle.py` (needs Chrome — run it with your OWN Chrome open, that is the
    interesting case), `test_DMM.py` / `test_engine.py` / `test_voice.py` (live API calls).
  - **Needs a human.** `test_barge_in_live.py` — checks the microphone is actually live first,
    because a muted input device looks exactly like broken barge-in.
- Last full run (2026-09-08, after the routing / backend / memory / voice-state round):
  **2578 tier-1 checks, 2574 passing**. Per suite: 263 automation, 140 proactive agent,
  119 emotion, 63 browser selection, 186 voice control, 108 target resolution, 132 capture
  pipeline, 213 proactive presence, 141 TTS device, 63 environment, 416 UI, 124 provider
  router, 127 STT backend, 138 memory store, 196 voice state, 149 logging.
- **A tier-1 suite must not read the host's state, and two of them did.** Both were found by
  a run going red on unchanged code, which is the only honest way to find this class of bug:
  - `test_proactive_agent.py` read the machine's REAL battery through
    `system_profile.pressure_sample()`. The suite was green all afternoon and then failed 9
    checks because the laptop had dropped to **12% and unplugged** — the presence layer
    correctly raised a CRITICAL `battery_low` candidate, which by design outranks every
    candidate those tests exercise. The presence layer was right; the suite was wrong. It now
    pins a healthy, plugged-in, unloaded host in `_pin_host_environment()`, and the tests that
    care about the thresholds still drive `pressure_sample` directly with their own values.
    (`test_proactive_presence.py` already injected its own samples and was unaffected.)
  - `test_ui.py`'s speech-backend checks drove the real `SettingsView._on_backend`, which
    persists on a committed switch — so with a stubbed bridge reporting success it rewrote the
    DEVELOPER'S OWN `.env`. `NoEnvWrites` now blocks and records those writes (making them
    assertable: a committed switch must persist, a failed one must not), and
    `section_no_side_effects` compares the whole `.env` before and after the run so any future
    writer is named rather than discovered later.
- **Four failures are PRE-EXISTING on `HEAD` and unrelated to this work** — verified by
  stashing the working tree and re-running:
  - `test_browser_selection` ×1 — `'None' means no explicit preference` reads `STT_BROWSER`
    from this machine's `.env`, which is `chrome`, so the check depends on the developer's
    configuration rather than on the code.
  - `test_tts_device` ×2 and `test_environment` ×1 — the CUDA-usable assertions, on a machine
    whose `.env` sets `TTS_DEVICE_MODE=CPU`.
- Live, against real providers, a real headless browser and real audio: a genuinely
  rate-limited Cohere key falling back to Groq in **708ms** (the old path was 5+10+15s of
  blocking sleep followed by a degrade), seven live speech-backend switches at **3.5-3.6s**
  each with 0 leaked processes and 16/16 of the user's own browser processes untouched, and a
  full boot -> turn -> barge-in -> recovery -> pause -> standby -> shutdown sequence with
  every voice transition correct and shutdown ending OFFLINE. The microphone control was
  sampled once a second across a 9-second real boot and agreed with the microphone throughout,
  with **zero** `listeningChanged` events fired.
- **The DMM matrix on this machine currently routes to the LOCAL LM Studio model**, not Cohere,
  because `.env` leaves `FORCE_ONLINE` unset and `core.config.env()` gives `.env` precedence
  over the process environment. Against the local model it scores **50-51/53** with 0
  duplicate-token and 0 unexecutable-token cases; the failing cases are the documented flaky
  boundaries (`Show me the desktop.`, `Copy that.`, general-vs-deep-research on a long
  question), and `exit` and the negative-control categories are 100% in every run. The
  documented 53/53 baseline was measured against Cohere and is not comparable to this. Nothing
  in the classifier was changed this round — the local control layer intercepts lifecycle
  commands BEFORE the DMM rather than adding tokens to it.
- `tests/test_stt_lifecycle.py` reports 2 pre-existing failures on this host (`exactly one
  ChromeDriver is owned`, `still exactly one session after recovery`). Verified pre-existing by
  running it against `HEAD` with the working tree stashed.
- **The DMM matrix is not deterministic at the last decimal.** One run scored 52/53 on
  `"Show me the desktop."` (`task view` instead of `minimize all`); the same case then
  classified correctly three times in a row and the next full run was 53/53. Re-run before
  concluding a change caused a single-case drop.
- After any code change, run `graphify update .` (see `AGENTS.md`) to keep the knowledge graph
  current for future `graphify query` lookups.
- This project's `.venv` has all real dependencies installed (including the Windows-only/hardware
  ones — `mediapipe`, `opencv-python`, `pygetwindow`, `keyboard`, `pyautogui`) — prefer testing
  against it over assuming a package is unavailable.
- **Cleaning up a Kayra process by hand:** walk the PID tree from its ChromeDriver, never match
  on `chrome.exe`. Verified during this round — one orphaned run owned 8 processes while 6 other
  `chrome.exe` processes on the same machine belonged to the user. A name sweep kills all 14.

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
