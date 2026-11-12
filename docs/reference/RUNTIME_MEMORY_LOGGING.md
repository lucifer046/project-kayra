# Runtime state, memory, logging, configuration

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

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

## Configuration

All runtime config lives in `.env` (see `.env.example` for the full annotated list — never
commit a real `.env`). Groups: speech (`INPUT_LANGUAGE`, `ASSISTANT_VOICE`, `TTS_DEVICE_MODE` —
`AUTO` | `GPU` | `CPU`, validated and clamped), local-vs-cloud
(`FORCE_ONLINE`, `LOCAL_*`), cloud API keys (`CLAUDE_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`),
identity (`ASSISTANT_NAME`, `ASSISTANT_GENDER`, `USERNAME`, `USER_GENDER`, `LANGUAGE`), the
proactive service (all `PROACTIVE_*` — master switch, tick cadence, the three cooldowns, the
score threshold, the late-night window, habit-store caps, LLM phrasing), automation
(`AUTOMATION_CONFIRM_TTL_SECONDS`, `AUTOMATION_SHELL_TIMEOUT_SECONDS`,
`AUTOMATION_SCREENSHOT_KEEP`, `AUTOMATION_MAX_TIMERS`), deep research tuning
(`MAX_SUB_QUESTIONS`, `MAX_FOLLOWUP_QUERIES`, `MAX_DEEP_PAGES`, `SEARCH_RESULTS_PER_QUERY`),
provider failover (`PROVIDER_COOLDOWN_*` per failure kind, `PROVIDER_TIMEOUT_SECONDS`),
hand gesture control (all `GESTURE_*` — the two autostart switches, the camera format, the
detector, the accelerator mode, the three preset dials, and every threshold behind them), and
logging (`KAYRA_LOG_LEVEL`, `KAYRA_LOG_FILE`).

**Every geometric gesture threshold is a RATIO of hand scale, never a pixel count** — see the
hand gesture section for why that distinction is the difference between clicking working and
not working at two distances from the camera. The hysteresis inequalities (`release > press`,
`neutral < scroll`) are enforced at construction and a malformed `.env` is corrected and
logged rather than obeyed.

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
