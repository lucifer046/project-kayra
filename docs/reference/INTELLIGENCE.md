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
