# Intent classification and model routing

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

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
  preamble (`self.dmm_preamble` + `self.dmm_chat_history`, both set in `__init__`) to the
  cloud DECISION route (Claude -> Groq -> Gemini) or a local model. (Cohere until 2026-11-10;
  the measurements below that name it are history.)
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

## Model routing — the provider router (`src/kayra/intelligence/provider_router.py`)

**There is ONE retry/fallback authority in the process, and it is this module.** Before it,
fallback was invented independently in two places inside `llm_engine.py` and the two disagreed
about almost everything — which is how one user request became four provider calls against an
already rate-limited key.

### The hierarchy (2026-11-10: Claude added, Cohere removed)

```
DECISION (the DMM)   Claude  ->  Groq  ->  Gemini
CHAT                 Claude  ->  Groq  ->  Gemini
```

**One order for both routes, and any one key serves both.** Every provider is a general model
that can classify AND converse; what differs between the routes is how a provider is CALLED,
never who is asked:

| | DECISION (the DMM) | CHAT |
|---|---|---|
| Claude | `CLAUDE_DECISION_MODEL` (default = `CLAUDE_MODEL` = `claude-haiku-5-5`), `max_tokens` 128, thinking OFF + effort low, few-shot prefix CACHED | `CLAUDE_MODEL`, streamed, thinking off + `CLAUDE_EFFORT` (low) |
| Groq | `GROQ_DECISION_MODEL` = `openai/gpt-oss-20b`, `reasoning_effort: low` | `GROQ_CHAT_MODEL` = `openai/gpt-oss-120b` |
| Gemini | `GEMINI_DECISION_MODEL` (default = chat model), `reasoning_effort: none` | `GEMINI_CHAT_MODEL` = `gemini-flash-latest` |

- **Why Cohere went.** `command-r-plus-08-2024` was a 2024 model on a ~1,000-call/month trial
  key, and it was the only reason the two routes differed. Its client, key, Settings field and
  setup prompt are gone (`CohereAPIKey` stays in `SECRET_KEYS` so an old `.env` never prints it).
- **Claude is called through the official `anthropic` SDK**, imported lazily inside
  `_build_cloud_clients` (importing `llm_engine` stays cheap; a missing package costs the Claude
  provider, not the engine). `max_retries=0`, same timeout as the others.
- **`claude_speed_args(model)`** is the one place that knows which Claude model takes which
  speed fields: Haiku 5.x gets `thinking: disabled` + low effort; Opus/Sonnet 5.x get low effort
  only (`disabled` is a 400 there); Haiku 4.5 and older get neither (they reject `effort`). No
  sampling parameter is ever sent to Claude — the current API rejects non-default ones.
- **The DMM's ~5.6K-token prefix is cached on Claude**: a breakpoint on the system block and one
  on the LAST few-shot example, so only the user's words are uncached. Responses are read by
  block TYPE (`claude_text`), never `content[0]` — Haiku 5.5 may lead with a thinking block.
- **`to_claude_messages`** converts the OpenAI-shaped messages every chat caller builds: system
  entries joined in order into Claude's `system`, user/assistant turns kept, a leading user turn
  guaranteed.
- **Gemini's `-latest` alias** is the default because `gemini-2.5-flash` was retired under a
  working configuration (it answered 404 / "limit: 0", which the old text classifier called a
  rate limit). Vision follows the same models (`VISION_CLAUDE_MODEL` / `VISION_GEMINI_MODEL`
  blank = the chat model); vision order is local -> Claude -> Gemini -> Groq.
- **A reasoning field a provider rejects** (Groq/Gemini DMM) is retried ONCE without it and
  remembered (`_no_reasoning_field`) — an INVALID_REQUEST correction, not a transport retry.
- **`CLOUD_FIRST=True`** (with any cloud key) makes the cloud chain answer even while a local
  server runs, for the engine, `ai.fast`/`ai.summarize` and vision. Off by default: local-first
  is otherwise unchanged and absolute. It is the ONE switch on the Settings screen ("Use cloud
  models even when LM Studio is running"); `FORCE_ONLINE` still works from `.env` only.
- **Local vs cloud is followed LIVE** (`_sync_mode`, at most every 10 s, at the start of
  `classify_intent` and `generate_chat_stream`, and immediately on Settings → Save): LM Studio
  started after Kayra is picked up, LM Studio quitting hands over to the cloud. It used to be
  decided once at boot.
- **Blank means unset** (`_setting`). Settings writes `LOCAL_BASE_URL=` for an empty field,
  and `.get(name, default)` returned "" — the probe dialled port 80, found nothing, and every
  request went to the cloud with LM Studio running. Found on the developer's machine.
- **Keys apply without a restart.** Settings → Save calls `bridge.reload_providers()` →
  `CentralizedLLMEngine.reload_providers()`: re-reads `.env`, rebuilds every cloud client,
  and clears the cooldown of ONLY the providers whose key changed (a wrong key's 900 s
  AUTH_FAILURE verdict used to outlive the corrected key). The local/cloud MODE stays a startup
  decision, except that CLOUD_FIRST can switch a local session to the cloud.
- **The exhausted-chain sentence names each provider and the real reason**
  (`chain_failure_sentence`): "Claude is rate-limited; Gemini cannot serve the configured model
  on this key". It used to say "every model is rate-limited" whenever any attempt was a 429.

- **Local-first is otherwise unchanged and absolute.** With an LM Studio / Ollama server up (and
  neither FORCE_ONLINE nor CLOUD_FIRST), ALL traffic goes there and the router is not consulted.
- **The DMM prompt contract is identical on every provider.** The same strict system rule, the
  same `dmm_preamble` and the same unsliced `dmm_chat_history` reach every one; only the
  transport differs. A fallback that changed the prompt would be classifying a different
  question.

### Failure classification

Eight kinds — `RATE_LIMITED`, `AUTH_FAILURE`, `NETWORK_FAILURE`, `TIMEOUT`, `SERVER_ERROR`,
`INVALID_REQUEST`, `MODEL_UNAVAILABLE`, `UNKNOWN`. The kind determines two things and only two:
how long the provider stands down, and whether the request is worth handing to anybody else.

- **The HTTP status decides first (2026-11-10).** `_STATUS_KINDS`: 400/413/422 invalid, 401/402/
  403 auth, 404 model, 408 timeout, 429 rate limit, 529 and any 5xx server. Text matching on the
  type name and message is the fallback for exceptions without a status, and it no longer
  contains bare numbers ("429"/"500" inside a request id or a token count is not evidence).
  **The bug this fixed:** the text check ran "429"/"quota" FIRST, so a retired Gemini model or a
  key whose plan has no allowance for the model was reported as RATE_LIMITED, and the user was
  told every model was rate-limited on keys with plenty of budget.
- **A 429 that is NOT a rate limit** — "limit: 0", "free_tier", "credit balance", "billing",
  "insufficient_quota" — is MODEL_UNAVAILABLE, and its Retry-After is ignored (it would only
  bring the same refusal back every few seconds).
- **`INVALID_REQUEST` falls through too (since 2026-11-10)**, with a 60 s cooldown: no two
  providers receive the same request (Claude's shape differs from the OpenAI-shaped one), so a
  400 from one says nothing about the next. Stopping the chain turned one refusal into "no
  model could answer".
- **No credit is an ACCOUNT problem, whatever the status.** Anthropic answers an account
  without API credit with a **400** `invalid_request_error` ("Your credit balance is too low
  to access the Anthropic API") — found live on the developer's key, which had a claude.ai
  plan and no API credit. `is_no_credit()` classifies it as AUTH_FAILURE and the sentence says
  "Claude has no API credit on its account" (plus "add a Groq or Gemini key" when there is no
  fallback); Test says how to fix it.
- **`AUTH_FAILURE` DOES fall through** — a missing Claude key says nothing about the Groq key —
  but it carries the longest cooldown (900s), because nothing about the next minute will fix a
  wrong credential and retrying it every turn is the retry storm this exists to prevent. A key
  saved in Settings clears it (see the hierarchy notes above).

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

## What is worth retrying, and for how long

`MAX_DMM_EMPTY_RETRIES = 5` is unchanged. What changed is that five retries are no longer spent
on failures that cannot succeed.

* **`TERMINAL_DMM_FAILURES`** — `INVALID_REQUEST`, `AUTH_FAILURE`, `MODEL_UNAVAILABLE`. The next
  four attempts fail identically; the only thing repeating buys is the user's time. Classified
  with the provider router's own `classify_failure`, so "is this worth retrying?" is answered
  from ONE vocabulary.
* **Bounded linear backoff.** `DMM_RETRY_DELAY_MS = 120`, capped at 600, and **zero on the first
  retry** — the common case is a retry that succeeds and delaying it is pure added latency. Not
  exponential: this is a sampler that produced a stop token, not congestion, and a growing wait
  only makes the worst case worse. Capped by the remaining budget so it can never extend a
  request past its deadline.
* **A health cooldown.** Three consecutive empty REQUESTS (not attempts — five empty attempts
  inside one request are one piece of evidence) stand the local model down for 20s, and
  requests go straight to the fallback. The request after the cooldown IS the probe; one
  success clears it.
* **Retry progress is INFO, not WARNING.** Five visually dominant warnings for something the
  assistant recovers from on its own trains a reader to skim. The WARNING is kept for the
  exhaustion, which has a consequence.

---

## The DMM empty-response retry bound

`MAX_DMM_EMPTY_RETRIES = 5` (raised from 3), `DMM_RETRY_BUDGET_SECONDS = 15.0` and
`DMM_ATTEMPT_TIMEOUT_SECONDS = 3.0`.

**THREE BOUNDS, AND THEY BOUND DIFFERENT THINGS.** The count bounds how many times the
model is asked; the budget bounds the whole CHAIN; the attempt ceiling bounds ONE call.
The third was the gap: a local server that accepts a request and then takes twelve
seconds to answer nothing spent almost the entire budget on a single useless call, so
the five retries the count promises never happened. Each attempt now carries its own
deadline, CLAMPED BY THE REMAINING BUDGET at the call site, so the two limits compose
and whichever is tighter wins. Three seconds is comfortably above the ~2.0s an empty
completion costs against LM Studio here, so a healthy backend never meets it.

**It is a REQUEST TIMEOUT, not a sleep and not a second retry authority.** It reaches
the SDK through `client.with_options(timeout=...)` — a shallow copy, so a short-deadline
DMM call cannot change the timeout of the chat stream running beside it — and the client
is constructed with `max_retries=0`, so a timeout raises once and the retry decision
stays where the count and the budget already live.

This governs ONE failure class — the provider answered and the answer parsed to zero usable
tokens — and nothing else. Transport failures, rate limits, timeouts and auth errors are the
provider router's business and never reach it, so **a rate-limited cloud key still costs exactly
one call per provider per request**, which is the property the router exists to guarantee and
which five retries here must not undo.

- **Five rather than three because the LOCAL path is where empty completions happen**: a small
  model that samples only stop-tokens, or one still warming, produces them regularly, and each
  attempt is an in-process round-trip on the user's own machine rather than a metered call.
  Measured live against LM Studio on this host: `Retry 1/5` … `Retry 5/5`, then
  `Model exhausted 5 retries. Treating this as conversation.` — and "what is the capital of
  France" succeeded on attempt 2, so the retry genuinely earns its keep.
- **A count alone is not a bound on the user's wait.** An empty completion costs ~2.0s here, so
  five retries is ~10s of silence for a request that ends in "treat as conversation". The
  budget stops a slower backend turning that into half a minute. Whichever limit is reached
  first wins and the log says which. With a 2s/attempt backend all five still run.
- **No sleep anywhere in the retry path**, and there must not be one. This is a deadline, not a
  backoff: the model is up and merely produced nothing, so waiting adds latency and buys
  nothing. Asserted by walking the AST.
- **No traceback for an expected empty response** — one WARNING line, once.
