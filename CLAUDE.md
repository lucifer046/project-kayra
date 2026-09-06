# Kayra — Project Context for AI Agents

> Personal, Jarvis-inspired desktop assistant for Windows. Voice or text in, LLM-routed intent
> classification, and hands-on system automation out. This file orients any AI agent working in
> this repo (Claude Code, or another tool via `AGENTS.md`) — architecture, conventions, and
> gotchas that aren't obvious from reading a single file in isolation. Read this before making
> cross-module changes; update it when you change something it describes.
>
> User-facing install/feature docs live in `README.md` and `blueprint.md` — this file is for
> agents making changes, not for end users.

## What this is

Kayra listens (voice via headless-Chrome Web Speech API, or typed input), classifies the
query's intent with a small LLM ("the DMM"), and routes it to conversation, live web search,
autonomous deep research, or direct Windows automation (open/close apps, media keys, window
management, system info, hotkeys, clipboard, screenshots, timers). It speaks responses back
with an offline Kokoro-ONNX TTS voice and supports voice barge-in (interrupting it mid-sentence).

## Architecture at a glance (data flow)

```
main.py Main_Loop():
  Listen()                                  [voice via modules/speech_to_text.py, or keyboard]
    -> SemanticEmotionEngine.analyze_text()  [modules/emotion_engine.py]           -> mood
    -> CentralizedLLMEngine.classify_intent()[modules/llm_engine.py — "the DMM"]   -> [task tokens]
    -> Execute_Task() routes each token:
         "general ..."       -> modules/chatbot.py             conversational LLM + memory
         "realtime ..."      -> modules/real_time_search.py    DuckDuckGo RAG + LLM
         "deep research ..." -> modules/deep_research.py       6-stage autonomous research agent
         everything else     -> modules/automation_windows.py  system/app/hotkey automation ("hands")
    -> modules/text_to_speech.py speaks the result (Kokoro ONNX, streamed sentence-by-sentence)
```

Three more subsystems run independently of that loop:
- **the barge-in watcher** (`main.py::_barge_in_watcher`) — daemon thread polling the STT page
  for interrupt words every 60ms while audio is playing, and calling `tts_engine.stop()` from
  outside the main loop. It has to live outside the loop: while a response is generating and
  speaking, `Main_Loop` is blocked inside `Execute_Task` and cannot poll the microphone at all.
- **`modules/proactive_agent.py`** — background daemon thread, tracks the active foreground
  window via `pygetwindow`, and fires cooldown-gated proactive suggestions (fatigue/late-night
  nudges) through the same TTS engine. Opt-out via `PROACTIVE_AGENT_ENABLED=False` in `.env`.
- **`modules/air_cursor_engine.py`** — standalone MediaPipe hand-gesture mouse replacement. Not
  wired into `main.py`; run directly with `python -m modules.air_cursor_engine`.

## Module map

| Module | Responsibility |
|---|---|
| `main.py` | Orchestrator: boot sequence, main listen/route loop, shutdown handling |
| `modules/llm_engine.py` | `CentralizedLLMEngine` — local-vs-cloud model routing, the DMM intent classifier, chat streaming, identity/system prompt. Singleton (see below). |
| `modules/chatbot.py` | General conversational path: memory-augmented chat |
| `modules/real_time_search.py` | Live DuckDuckGo web search RAG path |
| `modules/deep_research.py` | Multi-stage autonomous research report generator (saves to `Reports/`) |
| `modules/automation_windows.py` | All Windows system/app/hotkey automation — the "hands" |
| `modules/speech_to_text.py` | Headless-Chrome Web Speech API STT: managed single browser session (state machine, in-place recovery, PID-scoped teardown), loopback page server for a secure context, capture timestamps, interim-result interrupt detection |
| `modules/text_to_speech.py` | Kokoro-ONNX offline TTS: epoch-cancellable synthesis/playback pipeline, persistent audio stream, audible-window ledger for echo rejection |
| `modules/emotion_engine.py` | Zero-RAM keyword-lexicon mood classifier |
| `modules/proactive_agent.py` | Background habit tracking + proactive suggestions |
| `modules/air_cursor_engine.py` | Standalone MediaPipe hand-gesture mouse control |
| `modules/utils.py` | Console/logging helpers, project-root resolution, shared conversation-memory persistence, `StageTimer` boot instrumentation, `SentenceStreamer`, `speech_safe_text` |
| `tests/*.py` | Manual diagnostic entry points, **not** an automated pytest suite — run each directly. `test_audio_pipeline.py` (barge-in, echo rejection, speech normalization), `test_stt_lifecycle.py` (session reuse, recovery, process ownership) and `test_dmm_matrix.py` (intent-boundary accuracy) assert and exit non-zero; `test_barge_in_live.py` needs a human to speak |

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
  work correctly on a freshly constructed engine whether or not it is ever called. `main.py`
  calls it without a TTS engine on purpose; see the cold-start notes.

## Automation command routing — order-sensitive

`modules/automation_windows.py::translate_and_execute()` is an ordered `if/elif` chain matching
on `cmd_lower.startswith(...)` or keyword membership. Several literal DMM tokens are prefixes of
a more generic branch — e.g. `"close window"` / `"close tab"` both start with `"close "`, which
is also the generic "close this app by name" prefix. Exact-match branches for these two **must**
be checked before the generic `"close "` branch, or they get silently shadowed and routed to
`CloseApp("window")` / `CloseApp("tab")` instead of the intended Alt+F4 / Ctrl+W (this was a real
bug, fixed 2026-09-03). If you add a new literal token, check it doesn't collide with an
existing `startswith()` prefix earlier in the chain — add an exact-match branch above the
colliding prefix if it does.

## Model routing (`modules/llm_engine.py`)

- **Local-first**: on construction, probes `LOCAL_BASE_URL` (LM Studio/Ollama) — a TCP connect
  with a short per-address budget (`LOCAL_PROBE_TIMEOUT_SECONDS`, default 0.15s) followed by an
  HTTP ping only if the port is open. If alive, ALL chat + DMM traffic routes through the local
  endpoint exclusively — cloud clients aren't even constructed. Set `FORCE_ONLINE=True` in
  `.env` to skip the local check entirely. See the cold-start notes for why the plain HTTP ping
  had to go.
- **Cloud DMM**: Cohere Command-R only, no fallback — if the Cohere key is missing while online,
  `classify_intent` degrades every query to `general <query>`.
- **Cloud chat**: Groq (primary) -> Gemini (auto-fallback on quota/rate-limit errors).
- **`CentralizedLLMEngine` is a singleton** (`__new__` returns one shared instance per process).
  Every module that does `engine = CentralizedLLMEngine()` at import time (`main.py`,
  `chatbot.py`, `real_time_search.py`, `automation_windows.py`, `deep_research.py`) shares one
  object, one set of API clients, and one local-server probe. Do not reintroduce independent
  instances — before the singleton (fixed 2026-09-03) the engine was constructed up to 5x at
  every boot, each re-probing the local server and re-building Cohere/Groq/Gemini clients.
- Verified compatible: `cohere` package's `chat_stream(message=, preamble=, chat_history=,
  prompt_truncation=)` v1-style Client API works unchanged from 6.1.0 through the current 7.1.1
  — don't "fix" this thinking it's deprecated without re-checking against the installed version.

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

### Barge-in

Three independent pieces have to hold for "stop" to work; removing any one silently breaks it:

1. **Detection off the main loop.** `_barge_in_watcher` polls; `Main_Loop` cannot, because it
   is inside `Execute_Task` for the whole response.
2. **Detection on interim results.** The STT page flags interrupts from *interim* recognition
   results into `window.kayraInterrupt`, skipping both the 800ms VAD finalize and the
   `mtranslate` network round-trip. Waiting for the finalized transcript costs ~1s.
3. **Epoch cancellation in the TTS engine.** `stop()` bumps `_epoch`, drains the text and audio
   queues, aborts the output stream, and latches `_interrupted` until the next `begin_turn()`.
   The latch is what stops a still-running LLM stream from resurrecting the cancelled answer
   one `speak()` call at a time.

Consequently: **the TTS engine owns the only sentence queue.** `chatbot.py` and
`real_time_search.py` feed `tts_engine.speak` through `utils.SentenceStreamer` and must not
spawn their own speech worker thread — a private queue holds a backlog that survives `stop()`,
which is exactly how the old implementation kept talking after being interrupted.

Interrupt phrases are matched **exactly** (after stripping filler words), never as a prefix:
`is_interrupt_phrase("stop the music")` must be False or that automation command gets swallowed
instead of reaching the DMM. The same rule is implemented twice — `is_interrupt_phrase()` in
Python and `looksLikeInterrupt()` in the STT page — and the two must agree; `tests/test_DMM.py`
aside, `tests/test_audio_pipeline.py` covers the Python half.

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

### Cold start

`main.py` boots the TTS model and headless Chrome on threads *before* the heavy action-module
imports, then joins both before the main loop — so the three slowest stages overlap, and no
command can run against a half-initialized subsystem. Keep that ordering: moving the
`modules.chatbot` / `modules.automation_windows` imports back above the thread launches
re-serializes the boot.

- `StageTimer` (`utils.py`) records each stage; `BOOT.report()` prints the breakdown every run.
- `_check_local_server()` does a TCP connect with a short per-address budget before any HTTP
  call. The old bare `requests.get(..., timeout=1.5)` measured **3.0s** on a host with no local
  server: Windows Firewall drops (rather than refuses) connections to closed loopback ports, and
  "localhost" resolves to two address families, so the timeout was paid twice.
- Boot narration is one short spoken line. `run_boot_sequence()` prints the routing diagnostics
  and only speaks them if handed a TTS engine — `main.py` deliberately calls it without one.
  Speaking all four old startup lines cost ~19s of speech.

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
  PID and every Chrome PID beneath it. This is not a stylistic preference: `automation_windows`
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

## Memory

- Two tiers: `session_memory` (in-RAM list, capped to the last 6 messages, per-process — reset
  on restart) and `permanent_memory` (JSON file, only appended when the user says a trigger
  phrase: "store this", "remember this", "save this", "memorize this", "note this").
- Persistence helpers live in `modules/utils.py`: `get_data_paths()`, `load_conversation_memory()`,
  `save_conversation_memory()`. Always resolve paths via `get_project_root()` — never hardcode
  `"data\\conversation.json"` as a bare relative path (it used to be, and silently fragmented
  memory across files if the process wasn't launched from the project root; fixed 2026-09-03).
- Writes are atomic: the backup file is written first, then copied over the primary, so a crash
  mid-write can't corrupt the primary DB.
- `chatbot.py` and `real_time_search.py` both consume these same helpers — don't reintroduce
  a second copy of load/save/answer-cleanup logic in a new module; add it to `utils.py` instead.

## Configuration

All runtime config lives in `.env` (see `.env.example` for the full annotated list — never
commit a real `.env`). Groups: speech (`INPUT_LANGUAGE`, `ASSISTANT_VOICE`), local-vs-cloud
(`FORCE_ONLINE`, `LOCAL_*`), cloud API keys (`CohereAPIKey`, `GROQ_API_KEY`, `GEMINI_API_KEY`),
identity (`ASSISTANT_NAME`, `ASSISTANT_GENDER`, `USERNAME`, `USER_GENDER`, `LANGUAGE`), proactive
agent (`PROACTIVE_AGENT_ENABLED`, `PROACTIVE_FATIGUE_MINUTES`), deep research tuning
(`MAX_SUB_QUESTIONS`, `MAX_FOLLOWUP_QUERIES`, `MAX_DEEP_PAGES`, `SEARCH_RESULTS_PER_QUERY`).

## Dev workflow

- Python 3.11 virtualenv at `.venv/`. Install: `.venv\Scripts\pip install -r requirements.txt`.
- Run: `python main.py` (boots TTS + STT on threads in parallel with the LLM-engine imports,
  joins both, then enters the main loop; `BOOT.report()` prints the per-stage breakdown).
- `tests/*.py` are standalone diagnostic scripts (mostly no pytest runner or assertions) — run
  each directly, e.g. `python tests/test_engine.py`. They exercise real API calls when online.
  The exceptions assert and exit non-zero: `test_audio_pipeline.py` (time-to-first-word,
  barge-in cancellation, echo gate, interrupt phrases, speech normalization),
  `test_stt_lifecycle.py` (single session, no accumulation across cycles, crash recovery,
  PID-scoped shutdown — run it with your own Chrome open, that is the interesting case) and
  `test_dmm_matrix.py` (53 intent-boundary cases; paced to stay under Cohere's rate limit).
  `test_barge_in_live.py` needs a human to speak and checks the microphone is actually live
  first, because a muted input device looks exactly like broken barge-in.
- Sanity-check any change with `python -m py_compile main.py modules/*.py tests/*.py` before
  assuming it's correct — several of the bugs above were only caught by actually importing and
  exercising the code, not just reading it.
- After any code change, run `graphify update .` (see `AGENTS.md`) to keep the knowledge graph
  current for future `graphify query` lookups.
- This project's `.venv` has all real dependencies installed (including the Windows-only/hardware
  ones — `mediapipe`, `opencv-python`, `pygetwindow`, `keyboard`, `pyautogui`) — prefer testing
  against it over assuming a package is unavailable.

## Style conventions already in place

- Every module uses a 3-way import fallback for intra-package imports (`from .utils import ...`
  -> `from modules.utils import ...` -> `from utils import ...`) so files work both as part of
  the package and run standalone (`python modules/chatbot.py`). Keep this pattern for new
  intra-package imports rather than a bare `from modules.x import y`.
- Stdlib imports belong at module top-level, not re-imported inline inside functions on every
  call — several of these were cleaned up 2026-09-03 (`re`/`threading` in
  `automation_windows.py` and `text_to_speech.py`); don't reintroduce the pattern.
- Logging/console output goes through `modules/utils.py`'s `print_info` / `print_success` /
  `print_warning` / `print_error` / `print_system` (Rich-themed), not bare `print()`.
