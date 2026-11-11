# The voice loop

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

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
stop talking, stop listening, sleep, wake, shut down, and turn hand gesture control or the
camera on and off — is matched HERE, before the classifier. One normalization pass and a
frozenset probe; measured **under 10us per utterance** for the lifecycle vocabulary and ~15us
for the longer device phrases.

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
  ("that's all", "bye kayra"); the local layer just gets there first. Adding tokens or touching
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

> **CORRECTION, 2026-11-10 — read before trusting the speed numbers below.** Every timing in
> this section and in "Latency" above was taken with `models/kokoro.onnx`, which was believed
> to be the full-precision graph and is in fact byte-for-byte `kokoro-v1.0.int8.onnx`. The
> QUANTIZED graph is the slow one: RTF ~1.0 on the processor and on CUDA (its quantized
> operators have no CUDA kernels — the "547 Memcpy nodes"). The float graphs measure RTF 0.22
> on the processor and 0.03-0.04 on the RTX 4060 (fp16 +637 MiB VRAM, fp32 +1151 MiB). So
> "CUDA is not a speed-up" and "the int8 pair is faster" are both true only of the int8 file.
> The engine now prefers `kokoro-v1.0.fp16.onnx` (`text_to_speech.VOICE_MODELS`) and
> `setup.py` offers to download it. The provider/DLL/verification rules below are unchanged.

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

### Names on screen (added 2026-11-11, V19)

The audit log of 2026-11-11 was full of names the recognizer could not know: "make her
sharma" for the chat *Megha Sharma IITM*, "sand button" for WhatsApp's *Send*, "punchayat",
"sapphire house". Two changes:

- **`INPUT_LANGUAGE=en-IN`** on the reference machine (was `en-US`). Indian English hears
  Indian names and accents far better. Not `hi-IN`: that is Hindi plus a translation step.
- **The repair stage is told what is on screen.** `automation/screen_vocabulary.names()`
  reads the WhatsApp chat list (any tab), the active tab's Docs/Sheets/Drive file titles and
  its buttons — three extension calls, 0.35 s timeout each, `[]` without the bridge, never
  raises. `app._repair_stage()` injects it with `set_screen_source()`; the input layer still
  imports nothing from automation. Read at most once per `SCREEN_TTL_S` (1.5 s).

Two rules use it, both only for a COMMAND that names something (`COMMAND_HEADS`: open,
click, press, send, message, read, join …; never search/play/type, whose tail is free text):

- **Rule 1 first asks for an alternative that names something on screen**
  (`alternative:screen`) when the top reading names nothing there. Invents nothing.
- **Rule 2a replaces a NAME SLOT** — a run of words between `SLOT_BREAKS` ("the", "chat",
  "button", "and" …) — with the one on-screen name it sounds like (`screen:0.90`). Score =
  60 % consonant skeleton (`sound_skeleton`) + 40 % spelling; ≥ `SCREEN_MATCH` 0.80
  (0.86 and one word for a name of < 3 consonant sounds). Refused when: confidence ≥ 0.95;
  the sentence already contains an on-screen name; the slot is a correct PART of a name
  ("Megha Sharma" of "Megha Sharma IITM"); the first letter differs ("Central" vs "General"
  — the other may exist further down a list that only shows what is in view); two names
  are within `SCREEN_MARGIN` 0.06; the result would be a dangerous control. For send /
  message / reply only the slot after "to" (or, without "to", the first slot) is a name, so
  the message text is never rewritten. A long name also matches its leading words.

A "Send" reached this way is still a risky click and asks first (`policy._RISKY_CLICK`).
Tests: `tests/test_capture_pipeline.py::section_screen_names`.

**What this does NOT fix, stated plainly:** "quit" heard as "great" is not phonetically close
(`q300` vs `g630`) and no guard in rule 2 will ever bridge it. That pair can only be fixed
upstream — by the capture and endpointing work above — or by rule 1, if the recognizer offers
"quit" among its alternatives. Claiming otherwise would be claiming a dictionary by another
name.

## The two spoken lifecycle announcements (2026-09-10)

Two fixed sentences, composed in `app.py` and **never generated**. Neither is routed through
the DMM or the chat model — they have to be identical every time, they have to work with the
network down, and the shutdown one runs after `_cancel_active_work()` when there is nothing
left to wait on a cloud round-trip with.

### Boot

`speak_boot_announcement()` plays ONE line after every subsystem is up and verified and
before the first `Listen()`:

```
Kayra is now online. Local intelligence services are active and I'm ready.
Kayra is now online. Cloud intelligence services are active and I'm ready.
```

- **The tier is read from the LIVE engine (`engine.is_online`), never from `.env`** — the
  same rule `_report_startup` follows. A machine with cloud keys configured and LM Studio
  running is a LOCAL machine, and saying "cloud" there would describe a configuration rather
  than the process about to answer the user.
- **`_boot_announced` is a `threading.Event`, and that is what makes it once per PROCESS.**
  Both front ends reach the same function — `Main_Loop` for the console, `ui.session._boot`
  after its own boot — and without the latch a UI session that also runs a turn loop would
  announce twice.
- Non-blocking: the sentence plays while the microphone opens, so it costs the user nothing
  and a "stop" over it is an ordinary barge-in.
- `presence.boot_line()` is unchanged and still answers a spoken greeting. It is not used for
  the startup line any more: a startup line is the one place predictability beats variety,
  because it is the user's only evidence that boot finished.

### Shutdown

`SHUTDOWN_ANNOUNCEMENT` — *"Kayra shutdown initiated. Powering down in 3... 2... 1...
Goodbye."* — is spoken **blocking**, by `request_shutdown(farewell=True)`, before a single
resource is disposed, and the teardown runs the moment it finishes.

- **A CONFIRMED shutdown announces; a SIGNAL-driven one does not.** `_execute_confirmed`
  (the spoken "shut down Kayra", after the user answered yes), the DMM's `exit` token and the
  UI's Shut down button all pass `farewell=True`. `_force_shutdown` — Ctrl+C, SIGTERM — does
  not: someone pressing Ctrl+C wants the process gone, not a sentence first.
- **There is no acknowledgement in front of it.** `resolve_lifecycle_confirmation` skips
  `confirmation_ack` for `SHUTDOWN` alone, because `request_shutdown` begins by cancelling
  everything queued — so an ack queued a moment earlier is discarded a fraction of a second
  later, which the user hears as a clipped syllable in front of the announcement. Every other
  confirmed control still acknowledges.
- **THIS ENDS KAYRA AND NOTHING ELSE.** A Windows shutdown is a different action with its own
  confirmation, owned by `policy.resolve_power_target` -> `system.shutdown`, and nothing on
  this path can reach it. See the power-target boundary below.

## The turn's visible lifecycle

The terminal reads as one line per stage that actually CHANGED:

```
[VOICE] Listening
[VOICE] Turn #7 · Transcribed: "open chrome"
[VOICE] Turn #7 · Processing
[VOICE] Turn #7 · Kayra: "Chrome is open."
[VOICE] Turn #7 · Speaking
[VOICE] Listening
```

`_voice_flow(stage, detail)` is the only writer, and it de-duplicates on
`(open turn, stage, detail)`.

- **The key carries the OPEN turn, which is 0 between turns.** That is what stops background
  work printing itself as the current turn, and it is why fifty listening callbacks inside
  one state produce one line while a genuine return to listening on the next turn produces
  another. The old `console.print("Listening...")` fired once per loop iteration regardless,
  so an utterance consumed by a control command or dropped as echo printed a second
  "Listening..." with nothing between the two.
- **ONE transcript line per turn, at INFO.** The endpoint's reason and every interim or
  N-best reading are DEBUG: a reader following a turn needs the words that were committed,
  not the recognizer's drafts.
- **The DMM token list moved to DEBUG.** It is a diagnostic, not part of the story; at INFO
  it sat between "Processing" and the reply. `KAYRA_LOG_LEVEL=DEBUG` brings it back.
- `_announce_reply` normalizes whitespace and truncates at 160 characters — a deep-research
  summary in the middle of the lifecycle is the flow this replaced. The full text still
  reaches the console through the service that produced it.
- The streaming branches (`Chatbot`, `RealTimeSearchEngine`) log "Speaking" BEFORE the call
  and the reply after it, because they speak sentence by sentence as the model streams. They
  are announced, never re-spoken: there is still exactly one sentence queue.

## Lifecycle confirmations own the next turn

Three defects, all observed live, all in how a pending confirmation interacts with the turn
after it.

### "Yes yes go to sleep" reached the DMM and was answered "Sleep well"

Five words, so the length rule sent it to UNCLEAR — and an utterance that is not a clean answer
falls through to normal routing. But it plainly IS an answer: the user said yes and then
repeated the thing they were being asked about.

`_restates()` allows an utterance past the length limit when **every word beyond the polarity
belongs to the phrases for the PENDING kind**. That is narrow and checkable: it admits
"yes, go to sleep" against a pending sleep, and still refuses "yes, open chrome", because
"open" and "chrome" are not words in any sleep phrase. The LEAD word decides the polarity and
is the only one that may, so "no, don't go to sleep" stays a refusal.

### Repeated "exit" asked the shutdown question over and over

A noisy recognizer produces "exit" several times. Each one was not an answer, so it cleared the
pending request — and then classified as SHUTDOWN and raised a fresh one.

`ControlConfirmations.answer()` now checks for a **restatement of the same kind BEFORE reading
the reply**, and returns `"restated"`: the question stays on the table, the answer is
*"Please say yes or no."*, and nothing is re-asked.

### "Turn off listening" disabled proactive suggestions

The phrase was not in `PAUSE_LISTENING_PHRASES`, so it fell through to the DMM, which
classified it as `proactive off`. Pausing the microphone and silencing unprompted suggestions
are different subsystems; the DMM guessing between them is what the local vocabulary is for.
Added, along with "turn listening off", "turn off the microphone", "turn off mic" and the
resume mirrors.

**PAUSED and STANDBY stay distinct.** `set_listening` touches nothing but the microphone;
standby suspends proactive activity as a CONSEQUENCE of sleeping, never as the meaning of a
listening command.

---

## Short confirmation answers: "yes" recognised as "S"

**Observed live.** The user says "yes" and the recognizer commits **"S"** — it caught only the
sibilant. It matters more than it looks, because "yes" is the word that authorises a shutdown.

**The forbidden fix**, and the obvious one:

```python
if transcript == "s": transcript = "yes"        # NO.
```

A global substitution corrupts ordinary conversation ("Tell me about S", "My grade is S") and
is the word-replacement dictionary this codebase refuses everywhere else. It is also
unnecessary, because a pending confirmation supplies three pieces of evidence a general
corrector never has: **context** (the space of sensible replies is two words), **N-best** (the
recognizer's own alternatives for this audio), and **shape** (an answer is one or two words).

`voice_control.resolve_short_answer()` is called ONLY from
`ControlConfirmations.answer`, which has already established that a request is pending. Two
routes, in order of how much they invent — which is none, and then almost none:

1. **The recognizer's own N-best.** If it offered "yes" as an alternative reading, that reading
   is the recognizer's. Preferring it is re-ranking; producing one it never proposed is
   invention. No length or confidence guard is needed, because nothing is being invented.
2. **A strict affix of a reply word**, under every guard at once: exactly one token, ≤3
   characters, confidence low or unknown, and no candidate of the opposite polarity.

`_fragment_of` is a **prefix or suffix test, not an edit distance and not a phonetic key**.
Both of those are similarity measures, and similarity is how "yes" gets recovered from "yet",
"mess" or "guess". A strict affix is a statement about the same word being partially heard.

* **The uniqueness that matters is POLARITY, not the word.** "s" is the tail of "yes" and the
  head of "sure" — both affirmative, so there is nothing to be ambiguous about. An earlier
  version demanded one candidate word and refused the one case this exists for. What stays a
  refusal is a token that could be either polarity ("o" → "no" and "okay"), because those two
  outcomes are "execute the shutdown" and "cancel it".
* **A recovered NO does not need uniqueness.** Cancelling is the safe direction.
* **Echo-flagged audio is never recovered.** A degraded token attributed to Kayra's own voice
  must not become an authorisation.
* **Refused: `school`, `system`, `its`, `stop`, `yesterday`, `session`, `essay`, `sunday`,
  `sorry`, `north`, `nothing`** — all too long to be a fragment. And any multi-word utterance,
  which is a sentence, not an answer.
* **The raw transcript is always logged**, with the evidence:
  `Confirmation response: raw="S."  (n-best:'yes')` or `(fragment:'s' of yes/sure)`.

---

## Turn ownership, and work that must not outlive its turn

### Turn numbers were resetting to 1

`logbus.end_turn()` set the only counter there was to 0, so the next `begin_turn()` computed
`0 + 1` and **every turn in a session was "Turn #1"**. The correlation the number exists for
was absent, and nothing could tell whether one turn was newer than another.

Two counters now: `_turn_seq` only ever increases, `_turn` is the turn currently OPEN and is 0
between turns. `latest_turn()` is what a background task compares against —
`current_turn()` is 0 between turns, so a check against it reports a finished turn as still
current in exactly the window where a stale retry is most likely to still be running.

### The turn is opened at the COMMIT, not in the loop

`Listen()` opens it, because that is the only point at which a complete user thought exists and
it is also where the utterance may be CONSUMED by a confirmation or a lifecycle command without
ever reaching the loop. Numbering it in the loop meant consumed utterances had no turn at all,
while the loop printed a second, uncorrelated "committed" line for the ones that did.

Every path in `Listen()` that consumes the utterance calls `logbus.end_turn()`.

### Stale DMM work stops

**Observed:** `[DMM] Retry 4/5`, a new utterance committing, then `Retry 5/5` — a retry chain
outliving its turn, printing into another turn's log and eventually returning a classification
for a question the user had moved on from.

`classify_intent` carries the turn that started it and checks `_turn_superseded()` at entry and
before every retry. Verified live against the real local model: an undisturbed chain ran 4.5s to
exhaustion; the same chain interrupted at 1.2s stopped at **3.0s and returned `[]`** rather than
a stale classification.

`_cancel_active_work()` runs FIRST in the shutdown sequence, before anything a retry depends on
is disposed.

### The message does not repeat the turn

`logbus` already stamps every correlated line. Putting it in the message too produced
`[DMM] Turn #1 · Turn #1 retry 1/5`.

---

## Barge-in needs a sustained voice, not a spike

**Observed:** while Kayra was speaking the visual flipped `ASSISTANT_SPEAKING ↔ USER_SPEAKING`
repeatedly. The page already raises the VAD threshold during playback (`vadEchoMargin`, 7× the
floor rather than 3.2×), but residual echo still crosses it in bursts, and **any** crossing
repainted the state.

Over playback — and only over playback — the detector must now have held its verdict for
`BARGE_IN_VAD_DWELL_MS` (320 ms) before the visual accepts it. A person taking the floor speaks
for a few hundred milliseconds; an echo spike does not.

**This does not slow barge-in.** A real "stop" reaches the assistant through the page's interim
interrupt flag, which sets the turn machine to INTERRUPTING — a different branch, and immediate.
The dwell governs only the case where the VAD alone is guessing, which is exactly where a guess
was wrong. A voice while merely LISTENING is still immediate.

`_compute` stays pure, static and total: the age of the VAD's verdict travels in as a parameter.

## The utterance boundary, and safe control (`src/kayra/core/endpointing.py`, 2026-09-10)

**A user said "यार मेरी girlfriend मुझसे नाराज़ है, बताओ मैं क्या करूँ?" and Kayra shut down
mid-sentence.**

The cause was not the recognizer and not a mis-heard word. It was A SECOND COMMIT POINT. The
recognition page published lifecycle commands straight out of `recognition.onresult`, from a
probe built out of the INTERIM transcript:

```js
const probe = (currentText + " " + interimTranscript).trim();
if (!window.kayraControl) {
    const kind = looksLikeControl(probe);        // <- SHUTDOWN reachable from here
    if (kind) { window.kayraControl = {...}; }
}
```

`_local_control_watcher` polls that flag at ~17Hz and dispatched it. So a transient interim
reading of "exit" — which a recognizer running `hi-IN` produces regularly over Hindi phonemes —
reached `request_shutdown()` **without ever passing through the endpointer**, while the user was
still talking. The VAD was working perfectly and was simply not consulted, because that path did
not go through it.

**The fix is architectural, not lexical.** There is no word list, no "distrust the word exit",
and no replacement dictionary — those are the thing this codebase already refuses elsewhere and
they would eventually mis-fire on a legitimate command. What changed is that only a COMMITTED
utterance can reach a control, and one module decides what committed means.

### One commit point

```
microphone -> AEC -> VAD -> recognizer -> utterance accumulator
   -> ENDPOINT DECISION        <- core/endpointing.py. THE commit point.
   -> transcript repair -> conversation context -> control -> DMM
```

Above the arrow nothing may act on words; below it everything does. **The single exception is
barge-in**, which may still inspect interim text to SILENCE PLAYBACK — silencing is not an
action on the world, it hands the floor back, and its whole value is that it happens before the
endpoint. It cannot start a turn, run automation, end the process or reach the DMM.

- `looksLikeControl()` is DELETED from the page, not guarded. `window.kayraControlPhrases` is
  hard-coded `[]` and nothing assigns `window.kayraControl`. Removing the classifier rather than
  adding a check to it is what makes the bug unreachable from that page.
- `flushUtterance` has exactly ONE call site, and it is the endpoint decision's.
  `tests/test_voice_turn.py` counts them.
- `Listen()` logs `[VOICE] Transcribed: "..."` at the commit, which is the line that says
  where the boundary actually fell. It is the ONE transcript line per turn; the endpoint
  reason and every interim reading are DEBUG. See "The turn's visible lifecycle" below.

### Both signals, and neither alone

* **A final recognition result is not the end of a turn.** It means "this SEGMENT is final".
  Recognizers emit several per sentence, while the speaker keeps going.
* **A silence timer is not the end either.** Results LAG the sound, so "no results for N ms"
  fires mid-sentence whenever the backend is slow — and a clipped word is worse than a
  mis-heard one, because there is nothing left to repair.

Both were already required (`recognizerQuiet && roomQuiet`). What this milestone added is
CONTINUATION EVIDENCE — three reasons to keep listening, all about acoustics and timing, none
about which words were heard:

1. **The user is audibly speaking right now.** `voice_active` blocks the commit outright. This
   one check alone would have prevented the reported failure.
2. **The turn is younger than `min_utterance_ms`** (350ms). A turn 120ms old has not ended.
3. **The transcript is implausibly short for how long the person spoke.** Past
   `continuation_speech_ms` (1500ms), a one- or two-word transcript means the recognizer is
   BEHIND. Such a turn never takes the fast path and waits out `truncated_hangover_scale`×
   the room hangover.

**Rule 3 delays a commit; it never blocks one.** It knows nothing about which word it is and
cannot reject one. If the user genuinely said one word and stopped, the longer hangover expires
and the word is committed correctly a few hundred milliseconds later — asserted.

### Short commands stayed fast

The fix is not "wait two seconds for everything". The hangover is ADAPTIVE and the fast path is
available exactly when the evidence supports it: short, already-COMMITTED, no interim pending,
no voice in the room, no truncation suspicion.

| | measured |
|---|---|
| short control command, endpoint after speech ends | **~500 ms** (unchanged) |
| `decide()` cost | **1.68 µs**, evaluated at 60 ms intervals |
| a sentence still being spoken | cannot commit at all |

### One rule, two implementations

`core.endpointing` is the AUTHORITY and holds every threshold; the page mirrors the predicate
because the decision has to run at 60 ms resolution next to the audio, where a Selenium
round-trip per tick is not available. Same shape as the control vocabulary.
`_capture_tuning()` composes `tuning_payload()` and adds only the microphone's ACOUSTIC
settings, so the two halves cannot disagree about when a turn ends.

Verified live against the real headless Chrome: **10/10 scenarios agree** between the page's
JavaScript and the Python authority, including the reported-failure case.

### VAD hysteresis

Ordinary speech is not a plateau — every stop consonant dips the energy below any single
threshold, so a bare `rms > t` comparator toggled `voice` several times a second inside one
sentence. That produced the `LISTENING -> USER_SPEAKING -> LISTENING` churn in the logs and an
orb that flickered per syllable.

Leaving the speaking state now requires falling to a LOWER threshold (`vadReleaseRatio`, 0.6×)
AND staying there for `vadReleaseMs` (220ms). Both halves are needed — hysteresis alone still
flickers on a deep dip, a hold alone still chatters — the same discipline the gesture layer's
`Hysteresis` gate follows. Measured live: enter 0.01673, exit 0.01004.

---

## Dangerous voice control is confirmation-gated

**A single recognition must never end the process.** The input is a probabilistic transcript of
a room; the cost of being wrong once is the whole session.

```
"Okay Kayra, shut down the engine."
  -> [VOICE] Control candidate: CONTROL_SHUTDOWN (explicit)
  -> [VOICE] Confirmation required
  -> "Just to confirm — should I shut down the Kayra engine?"
"Yes."
  -> [VOICE] Confirmation: YES
  -> [SHUTDOWN] Executing confirmed Kayra shutdown
```

- **`DANGEROUS_KINDS` is `{SHUTDOWN, SLEEP}`.** Note what is NOT in it: `PAUSE_LISTENING` is
  reversible with one button and a user who says "stop listening" wants it NOW; `INTERRUPT` is
  how the user takes the floor back and gating it would defeat barge-in entirely.
- **`_dispatch_control` cannot execute them.** It asks. The ONLY path to a dangerous action is
  `_execute_confirmed`, reached solely from `resolve_lifecycle_confirmation` on an affirmative
  — asserted by walking the AST for callers. The UI's Shut down button, the tray's Quit and the
  signal handler still call `request_shutdown` directly, and should: a button press is already
  unambiguous confirmed intent, and a transcript is not.
- **The confirmation is answered BEFORE the classifier**, for the reason the automation
  confirmation is answered before the DMM: a bare "yes" sent to a classifier comes back as
  conversation and the question would never resolve.
- **Three-way replies, not two.** `YES` / `NO` / `UNCLEAR` / None. "Yeah, go ahead" executes;
  "Yeah, but first tell me my options" is UNCLEAR and re-asks once (`MAX_ASKS` 2, because a
  third question is a loop); "what time is it" is not an answer, clears the request and is
  processed normally. Collapsing UNCLEAR into either of the others is how a dangerous action
  gets executed off a sentence that was really a question.
- **Bounded window.** `CONFIRMATION_TTL_SECONDS` (20s), evaluated on read. A late "yes" does
  nothing.
- **One pending request at a time.** A new dangerous request replaces the old rather than
  stacking — two outstanding questions is a state nobody can answer unambiguously.
- **`explicit` vs bare.** "shut down Kayra" names its target; "exit" does not. BOTH confirm;
  what differs is the wording — a bare request says what it thinks it heard, so a user whose
  sentence was mis-transcribed hears the mistake instead of the consequence.

### The echo interaction, which is the subtle half

Kayra asks the question out loud, so the user's "Yes." lands within a second or two of her own
voice and often overlaps it. If the confirmation were resolved AFTER the echo gate, the one
reply that matters most would be the one most reliably discarded — and a broad post-speech mute
window would do the same, which is why there is not one.

So the confirmation resolves BEFORE the gate, and echo-flagged audio clears a HIGHER BAR
instead of being trusted or discarded: only a clean whole-utterance YES or NO is honoured from
it. That is necessary because Kayra's own question — "Just to confirm — should I shut down the
Kayra engine?" — opens with a word in the affirmative vocabulary. Without the rule she would
re-ask herself in a loop; with a naive "not an answer clears it" she would CANCEL the
confirmation she had just asked. `"none-echo"` therefore leaves the request pending, which is a
different outcome from `"none"` and deliberately so.

### The vocabulary

| Say | Result |
|---|---|
| "shut down" / "shut down Kayra" / "turn off Kayra" / "close Kayra" / "exit Kayra" / "end Kayra" / "shut down the engine" / "turn the engine off" / "exit" / "quit" | **asks**, then shuts down Kayra only |
| "go to sleep" / "put Kayra to sleep" / "enter sleep mode" / "sleep mode" | **asks**, then standby |
| "stop listening" / "pause listening" / "mute listening" / "dont listen" / "stop hearing me" | pauses the microphone **immediately** |
| "wake up" / "wake up Kayra" / "resume listening" / "start listening" | resumes |
| "stop Kayra" | **barge-in**, not shutdown — the name is a filler for the interrupt vocabulary, so this and "Kayra, stop" are the same utterance. Deliberately NOT in the shutdown table; the suite caught it when it briefly was. |

Ordinary sentences containing these words reach the DMM untouched, because matching is exact on
the whole normalized utterance and never a substring: "Why did the program exit?", "The
application exists.", "That is great.", "Why do people sleep so much?" — all verified live.

**Apostrophes are DELETED in normalization, not turned into spaces.** The punctuation sweep was
splitting "don't" into `["don", "t"]`, so every phrase containing one was unmatchable —
silently, because a phrase that cannot match looks exactly like a phrase that was not said. The
tables are written in the normalized form (`"dont listen"`).

**The repair stage's `_is_irreversible` widened from SHUTDOWN to every `DANGEROUS_KIND`.** A
stage that may not invent "exit" must not be able to invent "sleep" either, and one set governs
both so a kind added there is protected here automatically.

---

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
