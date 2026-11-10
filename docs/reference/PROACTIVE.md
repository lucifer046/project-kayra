# Proactive service and presence

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

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
