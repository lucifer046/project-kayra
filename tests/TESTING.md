# Kayra Testing Guide

The authoritative guide to testing Kayra. Written for someone who did not implement the code:
every command below is one you can copy, and every "expected result" is what a healthy run
actually prints.

Kayra's tests are **standalone scripts**, not a pytest suite. Each one is run directly, prints
`PASS` / `FAIL` per check, and exits non-zero if anything failed. `tests/run_all.py` runs them
by category and totals them. There is no `pytest` dependency and no `conftest.py` — the shared
scaffolding lives in `tests/_harness.py`.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Windows 10 or 11 | The automation layer is Win32. Everything else is portable. |
| Python 3.11 in `.venv/` | Created by `python setup.py`. 3.10 is the floor. |
| A speech model in `models/` | Only the integration and audio suites need it. |
| Microsoft Edge or Google Chrome | Only the STT suites need one. |

**Nothing in the unit tier needs a GPU, a camera, a microphone, a browser, a network
connection or an API key.** Suites that do need one detect its absence and skip.

Check what this machine can exercise:

```bash
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'tests'); import _harness; print(_harness.describe_host())"
```

Expected: a line like `3.11.9 on Windows  ·  nvidia  ·  camera  ·  mic`. Any of those may read
`no-nvidia` / `no-camera` / `no-mic`; that is information, not a problem.

---

## 2. Environment Setup

```bash
python setup.py
```

Run once, and again after changing `requirements.txt` or moving the project to a different
machine. It creates `.venv/`, installs dependencies, and provisions the speech runtime for
**whatever graphics hardware this machine actually has** — see §20.

You never activate the virtual environment by hand. Every command in this document names
`.\.venv\Scripts\python.exe` explicitly, which is also what keeps a stray system-Python run
from producing confusing import errors.

Before shipping a change, compile and lint:

```bash
.\.venv\Scripts\python.exe -m compileall -q src tests run.py setup.py main.py
```

```bash
.\.venv\Scripts\python.exe -m pyflakes src/kayra tests run.py setup.py main.py
```

`compileall` does not catch undefined names. **Run pyflakes specifically after moving files** —
a past reorganisation left a module importing one name and using another, and the assistant
crashed on boot with a `NameError` while compiling cleanly.

---

## 3. Test Levels

| Level | What it means | Safe to run unattended? |
|---|---|---|
| **unit** | Deterministic. No network, no hardware, no real state written. | Yes, always. |
| **integration** | Needs a model provider, a browser, or audio output. | Yes, but slower and it can hit an API quota. |
| **hardware** | Needs a real camera, microphone or GPU. Skips when absent. | Yes. |
| **manual** | Needs a person to speak or hold up a hand. | **No** — never run by the runner. |

The category of every suite is recorded in `tests/run_all.py`, which is also where the
regression matrix in §23 comes from — there is one list, not two that can drift.

---

## 4. Running Unit Tests

The default, and the one you run most:

```bash
.\.venv\Scripts\python.exe tests\run_all.py
```

Expected: a table of 22 suites, every `status` reading `ok`, and a `TOTAL` line with **0 in the
`fail` column**. Takes about 4 minutes; `test_gesture_control.py` (~140 s) and
`test_camera_runtime.py` (~34 s) dominate it because both deliberately cycle their runtimes
many times to catch leaks.

One suite at a time:

```bash
.\.venv\Scripts\python.exe tests\test_automation.py
```

A subset by name or feature:

```bash
.\.venv\Scripts\python.exe tests\run_all.py --only gesture
```

The inventory, without running anything:

```bash
.\.venv\Scripts\python.exe tests\run_all.py --list
```

---

## 5. Running Integration Tests

These reach a model provider, a real browser, or the sound card.

```bash
.\.venv\Scripts\python.exe tests\run_all.py --integration
```

`test_dmm_matrix.py` is paced under Cohere's rate limit and takes about three minutes. **It
follows the same local-first routing the assistant does**, so with an LM Studio or Ollama
server running it measures the LOCAL model, not Cohere — the numbers are not comparable
between the two. If you are checking a classifier change, know which one you measured.

---

## 6. Running Hardware Tests

```bash
.\.venv\Scripts\python.exe tests\run_all.py --hardware
```

Several unit suites also have a `--live` flag that adds real-hardware checks:

```bash
.\.venv\Scripts\python.exe tests\run_all.py --live
```

| Suite | What `--live` adds |
|---|---|
| `test_automation.py` | Read-only Win32 window enumeration. Closes and focuses nothing. |
| `test_stt_backend.py` | Real browser discovery. |
| `test_camera_runtime.py` | Ten real open/close cycles, asserting **under 3 leaked threads per cycle**. |

That camera cycling check is the one that found a genuine leak: the previous `CAP_DSHOW`
backend leaked **17.2 threads per cycle**, and twenty toggles from the Home screen took the
process from 5 threads / 25 MB to 334 threads / 237 MB. Run it after any camera change.

---

## 7. Running Live Voice Tests

**MANUAL — a person has to speak.** The runner never launches these.

```bash
.\.venv\Scripts\python.exe tests\test_barge_in_live.py
```

It checks the microphone is actually live before it starts, because a muted input device looks
exactly like broken barge-in.

Expected: it prompts you, you speak, and it reports whether "stop" silenced playback within its
budget. Say **"stop"**, **"wait"** or **"hold"** while it is talking.

Failure symptoms and what they mean:

| Symptom | Likely cause |
|---|---|
| Nothing is transcribed at all | The chosen browser has no speech backend — see §8. |
| "stop" works in silence but not over a long answer | The interim-result tail match; check `[VOICE]` lines. |
| The interrupt is followed by a stray command | `clear_queue()` did not clear `currentText`. |

---

## 8. Running STT Tests

Session lifecycle, ownership and recovery:

```bash
.\.venv\Scripts\python.exe tests\test_stt_lifecycle.py
```

**Run this with your own browser open.** That is the interesting case: Kayra tracks its
processes by PID, never by name, and this suite proves a recovery does not touch the
browsing session you have open.

Backend switching (Edge ↔ Chrome ↔ Automatic), no hardware needed:

```bash
.\.venv\Scripts\python.exe tests\test_stt_backend.py
```

Which browsers on this machine can actually transcribe:

```bash
.\.venv\Scripts\python.exe tests\test_browser_selection.py
```

Expected: 66 checks pass. A browser exposing `webkitSpeechRecognition` proves nothing — Brave
ships the API with no backend behind it, starts a session, and then fails with
`onerror{error:'network'}` and never returns a transcript. Edge and Chrome carry first-party
backends and are trusted; anything else is probed.

Relevant log lines: `[STT] Backend: <name>`, `[STT] Session recovered`, `[STT] Switch refused`.

---

## 8b. Running Voice-Turn and Safe-Control Tests

The suite for the utterance boundary, the dangerous-control confirmation, and the DMM retry
contract.

```bash
.\.venv\Scripts\python.exe tests\test_voice_turn.py
```

Expected: 366 checks. **SAFE** — no microphone, no camera, no browser, no network, and **no
provider of any kind**. The DMM section runs against a fake local model, so it works while
Cohere, Groq and Gemini are all rate-limited, which is the state this milestone was built in.

What it covers:

| Section | Asserts |
|---|---|
| 1 | One commit point. The page cannot classify a lifecycle command or publish a control; `flushUtterance` has one call site. |
| 2 | The endpoint scenario table, driven through the real predicate — a final segment arriving mid-speech, four segments becoming one turn, a breath inside a sentence, speech resuming during the grace window, a lone word after long speech, mixed Hindi/English, a lagging backend. |
| 3 | Every rule of `core.endpointing.decide()`, plus its cost and configuration clamps. |
| 4 | The page mirrors the Python decision — every threshold and every reason string. |
| 5 | Control classification, and the sentences that must NOT be controls. |
| 6 | The confirmation state machine on an injected clock, including the echo interaction. |
| 7 | Five retries, never a sixth, and the wall-clock budget. |
| 8 | An AST walk proving no path reaches a dangerous action without a confirmation. |
| 9 | Degraded short answers — "yes" committing as "S". N-best re-ranking, the strict-affix route and every guard, plus the false positives (`school`, `system`, `its`) and an AST proof that no word-to-word replacement map exists. |
| 10 | The power-target boundary: `shutdown the engine car` can never become a Windows shutdown, and an explicit computer target still can (confirmed). |
| 11 | Turn ownership — monotonic turn numbers, supersession, and a real retry chain abandoning when its turn is replaced. |
| 12 | Retry classification (terminal vs retryable), bounded backoff, and the local-model health cooldown. |

### Testing the endpoint against the REAL browser

The page's JavaScript and the Python authority must agree. To check that on this machine,
boot a session and drive both over the same scenarios:

```bash
.\.venv\Scripts\python.exe tests\test_capture_pipeline.py
```

That covers the structural contract. For a live comparison against the running page, the
technique is: `get_shared_engine()`, then `driver.execute_script` reading `window.kayraVad`
and `window.kayraEndpoint`, which the endpointer publishes on every tick.

### Testing the DMM retries against YOUR local model

```bash
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from kayra.core.config import load_environment; load_environment(); from kayra.intelligence.llm_engine import CentralizedLLMEngine as E; print(E().classify_intent('open chrome'))"
```

Expected on a healthy local model: `['open chrome']`. On a model that answers nothing you
will see `Retry 1/5` … `Retry 5/5` and then
`Model exhausted 5 retries. Treating this as conversation.` — never a sixth attempt, and
never a traceback.

Failure symptoms:

| Symptom | Meaning |
|---|---|
| `Retry 1/3` | the bound was not raised; check `MAX_DMM_EMPTY_RETRIES` |
| a sixth attempt | the recursion is not passing its counter |
| `Retry budget of 15s spent after N attempt(s)` | your local model is slower than ~3 s per attempt. Not a defect — raise `KAYRA_DMM_RETRY_BUDGET_SECONDS` if you want all five |
| a traceback per retry | an empty completion is being treated as an exception |

---

## 9. Running TTS Tests

The ONNX Runtime device layer — providers, CUDA verification, and the truthfulness rule:

```bash
.\.venv\Scripts\python.exe tests\test_tts_device.py
```

Expected: 144 checks pass, with 1 group skipped on a machine where CUDA genuinely works
(there is no broken-CUDA state to simulate). On a machine with no NVIDIA GPU the CUDA groups
skip instead — **that is not a failure.**

Speech output end to end (needs the model and a sound card):

```bash
.\.venv\Scripts\python.exe tests\test_audio_pipeline.py
```

What device speech is really on, outside the tests:

```bash
python run.py --doctor
```

Expected: the interpreter, the ONNX Runtime package and version, every offered provider,
whether CUDA is **verified usable** (a real session, not a provider name), and the GPU
telemetry. This is the thing to ask for when speech output is on the wrong device.

---

## 10. Running LLM Provider Tests

Routing, failover and cooldowns — **no provider is ever called**; the clock is injected:

```bash
.\.venv\Scripts\python.exe tests\test_provider_router.py
```

Expected: 124 checks pass, including a 20-request storm proving a rate-limited Cohere key is
called **once**, not twenty times.

Real calls (needs keys or a local server):

```bash
.\.venv\Scripts\python.exe tests\test_dmm_matrix.py
```

Expected against Cohere: 53/53 intent boundaries, 0 duplicate tokens, 0 unexecutable tokens.
Against a local model it typically scores 50–51/53; the failing cases are the documented flaky
boundaries. **The matrix is not deterministic at the last decimal** — re-run before concluding
a change caused a single-case drop.

Relevant log lines: `[LLM] Provider: Cohere`, `[LLM] Result: RATE_LIMITED`,
`[LLM] Fallback: Groq`.

---

## 11. Running Automation Tests

```bash
.\.venv\Scripts\python.exe tests\test_automation.py
```

```bash
.\.venv\Scripts\python.exe tests\test_target_resolution.py
```

Expected: 263 and 108 checks. **Dry by default: nothing is closed, no key is pressed, no
window is moved.** That restraint is deliberate — a test suite for a module whose job is to
close windows and delete files must not be something you hesitate to run.

Add read-only Win32 checks:

```bash
.\.venv\Scripts\python.exe tests\test_automation.py --live
```

Even `--live` never closes, kills or focuses anything.

These suites also walk the AST to prove three things stay true: no `shell=True`, no
`os.system`, and **no path that terminates a process by name** (`taskkill /f /im chrome.exe`
would take the user's browsing session *and* Kayra's own speech session down with it).

---

## 12. Running Memory Tests

```bash
.\.venv\Scripts\python.exe tests\test_memory_store.py
```

Expected: 138 checks. The suite sandboxes itself into a temp directory and restores afterwards
— **your real conversation history is never read, written or deleted.**

It also asserts by AST that "clear all" contains no file-removal call at all: clearing writes
an empty list, it never deletes, moves or truncates the store.

Relevant log lines: `[MEMORY] Deleted: id=…`, `[MEMORY] Cleared: N memories`. Memory **content**
never appears in a log, by design — a terminal log is the least private place it could go.

---

## 13. Running Gesture Tests

Three tiers, in increasing order of what they touch.

**SAFE — synthetic hands, fake camera, recording pointer:**

```bash
.\.venv\Scripts\python.exe tests\test_gesture_state.py
```

```bash
.\.venv\Scripts\python.exe tests\test_gesture_control.py
```

Expected: 174 and 248 checks. `test_gesture_control.py` takes ~140 s because it cycles the
runtime repeatedly to prove nothing accumulates.

**LIVE — the real camera, no pointer movement:**

```bash
.\.venv\Scripts\python.exe tests\test_camera_runtime.py --live
```

**REAL — moves your actual mouse. Asks first.**

```bash
.\.venv\Scripts\python.exe tests\test_gesture_live.py
```

Safe by default: without `--real-mouse` the pointer controller records instead of acting, so
you can run it while reading its output.

```bash
.\.venv\Scripts\python.exe tests\test_gesture_live.py --real-mouse
```

> **DANGEROUS.** This moves the cursor and injects clicks on your desktop. It requires an
> explicit typed confirmation, and it refuses outright when the process cannot reach the
> interactive input desktop — Windows silently returns FALSE from `SetCursorPos` there, so
> every injection would *look* like it worked.

Expected with `--real-mouse`:

- index finger extended → the cursor tracks your hand
- index + thumb pinch → left click
- middle + thumb pinch → right click
- two fingers moving → scroll, which **stops when your hand stops**
- a closed fist held ~0.5 s → pauses; an open hand held ~0.3 s → resumes
- lowering your hand → "No hand in frame", **not** "Paused"

Failure symptoms:

| Symptom | Where to look |
|---|---|
| Cursor jitters at rest | dead-zone and One Euro settings in `gesture/config.py` |
| Clicks land beside the target | the pointer must FREEZE while a pinch is forming |
| Scrolling will not stop | it is velocity-based; an offset-from-anchor implementation joysticks |
| Rapid Active ↔ Paused flapping | the pause gate needs hysteresis **and** a dwell |
| Gestures work then stop | a gate not reset on every frame |

Diagnostics only, no UI:

```bash
.\.venv\Scripts\python.exe -m kayra.input.gesture --doctor
```

---

## 14. Running UI Tests

```bash
.\.venv\Scripts\python.exe tests\test_ui.py
```

Expected: 631 checks. Qt runs offscreen and the backend is fully stubbed — no window appears,
no engine starts.

**A passing UI suite does not mean the interface looks right.** Every fault a past refinement
pass fixed — 1590 px of bare background across five screens, widgets drawn on top of each
other, clipped labels — was present while 131 checks passed. Those checks are written *after* a
rendered review finds something. Review by rendering the screens and looking at them (§22).

The suite includes a **hardware-portability section** that renders Home against five machines
that do not exist (RTX 3050, RTX 4060, Intel Iris Xe, AMD Radeon, and no GPU at all) and fails
if any value from a *different* machine appears on screen.

It also asserts that the suite modified nothing the developer owns, comparing `.env` before and
after — a past run of it silently rewrote the developer's real `.env`.

**Section 17 covers the redesigned shell** (2026-09-10): that exactly one navigation surface
appears per screen, that the floating dock reads real backend state and remembers none of its
own, that the drawer opens/closes/navigates, that Home fills the viewport at 1040, 1440 and
1920 without scrolling, that the dock never covers the composer or the lowest panel, and that
the window chrome answers `WM_NCHITTEST` rather than reimplementing the drag.

**On the offscreen platform the custom title bar is deliberately NOT installed** — there is no
window manager to send the hit-test message — so the suite asserts the fallback (`native_chrome
is False`, native frame kept) rather than the frameless path. The frameless path is a manual
check: run the app, and confirm drag, double-click-to-maximise, Aero Snap, Win+Arrow, edge
resize and the right-click system menu all still work.

---

## 15. Running Camera Tests

```bash
.\.venv\Scripts\python.exe tests\test_camera_runtime.py
```

Expected: 64 checks, no camera required — a fake camera drives the mailbox, the recovery path
and the "consumer 4× slower than the camera" freeze scenario.

With the real device:

```bash
.\.venv\Scripts\python.exe tests\test_camera_runtime.py --live
```

Expected: ten open/close cycles leaking **under 3 threads per cycle** (measured: +0.1). If this
number climbs, check `GESTURE_CAMERA_BACKEND` — `CAP_DSHOW` leaked 17.2 threads per cycle and
took 1655 ms to open, against 516 ms for the `AUTO` default.

---

## 16. Running Proactive Presence Tests

```bash
.\.venv\Scripts\python.exe tests\test_proactive_agent.py
```

```bash
.\.venv\Scripts\python.exe tests\test_proactive_presence.py
```

Expected: 140 and 213 checks. Both **pin the host** — a fixed battery level, a fixed CPU load —
because the agent suite once failed nine checks purely because the developer's laptop had
dropped to 12% and unplugged. The presence layer was right to raise a CRITICAL battery
candidate; the suite was wrong to read the real battery.

The presence suite counts 720 evaluations and asserts **zero LLM calls** during them: deciding
whether to speak is integer arithmetic over resident values, never a cloud round-trip.

---

## 17. Running Conversation Context Tests

Conversation context lives with the capture pipeline that consumes it:

```bash
.\.venv\Scripts\python.exe tests\test_capture_pipeline.py
```

Expected: 132 checks covering the context and its bounds, the phonetic key and edit distance,
N-best re-ranking, and **every condition the repair stage refuses on** — including an AST proof
that no word-replacement dictionary exists and that a shutdown can never be invented.

The two are one suite on purpose: the repair stage's entire reason for consulting the context
is to decide what is plausible, and testing them apart would test neither contract.

---

## 18. Running Shutdown / Recovery Tests

```bash
.\.venv\Scripts\python.exe tests\test_voice_control.py
```

Expected: 190 checks, including the shutdown **order** (shutdown flag → gesture runtime →
proactive agent → timers → audio → browser → PID reap → UI hooks) and idempotency, against a
fully stubbed backend with `os._exit` replaced. Nothing is actually shut down.

STT recovery:

```bash
.\.venv\Scripts\python.exe tests\test_stt_lifecycle.py
```

Voice state resolution across all 1176 fact combinations:

```bash
.\.venv\Scripts\python.exe tests\test_voice_state.py
```

---

## 19. Running Setup / Environment Tests

Launcher, interpreter ownership and import origin:

```bash
.\.venv\Scripts\python.exe tests\test_environment.py
```

Dependency provisioning — **installs nothing**:

```bash
.\.venv\Scripts\python.exe tests\test_setup_runtime.py
```

Expected: 138 checks. `setup._pip` is replaced by a recorder that answers success and writes
down what it was asked to do; the assertions are made against that record. Actually performing
the decision would mean a ~1.4 GB download per fixture and would leave your `.venv` in whatever
state the last synthetic machine described.

Hardware and OS identification:

```bash
.\.venv\Scripts\python.exe tests\test_hardware_profile.py
```

Expected: 188 checks. It prints what it read off this machine — OS, CPU, every GPU, the
display — then drives seven synthetic machines through the same detector.

---

## 20. GPU Testing

**Kayra's only GPU acceleration path is CUDA.** An AMD or Intel machine runs speech on the
processor, and that is a supported, correct outcome — not a degraded one. Kokoro synthesizes at
roughly real time on a modern CPU either way.

What this machine has, and what speech is on:

```bash
python run.py --doctor
```

Four separate facts, and conflating any two produces a lie:

| Fact | Read from |
|---|---|
| A GPU exists | the registry (`core.hardware`) |
| A GPU provider is *offered* | `get_available_providers()` |
| Its DLLs actually *load* | the CUDA/cuDNN runtime and the DLL search path |
| A session actually *uses* it | `session.get_providers()[0]` |

ONNX Runtime does **not** raise when a provider's DLLs are missing. It logs, drops the
provider, and returns a working CPU session — so anything trusting the second fact reports
"GPU" while the CPU does all the work.

Setup's report distinguishes three verdicts, and the distinction matters:

| Verdict | Meaning |
|---|---|
| `PASS` | An NVIDIA GPU is present and a real CUDA session initialised. |
| `FAIL` | An NVIDIA GPU is present and something is broken. The reason names it. |
| `NOT APPLICABLE (no NVIDIA GPU)` | There is no NVIDIA GPU. Nothing is wrong. |

**Never install ONNX Runtime by hand.** `setup.py` owns which variant is present. A manual
`pip install onnxruntime` on an NVIDIA machine silently replaces the GPU build with the CPU
one, and nothing in the application can tell.

On a machine with **no NVIDIA GPU**, setup installs no `nvidia-*` wheel of any kind, and
reconciles an environment that was provisioned elsewhere by replacing `onnxruntime-gpu` with
`onnxruntime` and removing the orphaned CUDA runtime wheels.

---

## 21. Test Safety

Marked on every suite. **Read this before running anything with `--real-mouse`.**

| Marker | Meaning |
|---|---|
| **SAFE** | No mouse movement, no file deleted, no real memory or config modified, no API call. |
| **LIVE** | Uses the real camera, microphone or browser — but does not act on your desktop. |
| **REAL** | Moves the mouse or interacts with the desktop. |
| **DANGEROUS** | Requires an explicit typed confirmation. |

Every suite in the unit tier is **SAFE**. That is enforced structurally, not by convention:

- `EnvironmentGuard` (`tests/_harness.py`) snapshots `.env`, the conversation store, the habit
  store and the browser cache before a run and **fails the run** if any of them changed. It
  restores them too, so a misbehaving suite does not leave damage — but the write is still a
  failure the run has to answer for.
- `HostPin` replaces `pressure_sample()` so no tier-1 suite can read the real battery or CPU
  load.
- `RecordingInstaller` replaces `setup._pip` so no test can install or uninstall a package.
- `FakeMouse` records pointer calls instead of performing them.
- `TemporaryProject` redirects `core.paths` at a temp directory, so persistence code runs for
  real against a store nobody owns.

Both classes of protection exist because both mistakes were actually made here: a UI test wrote
the developer's real `.env`, and an agent test read the developer's real battery.

The **only** commands in this document that touch your desktop are:

```bash
.\.venv\Scripts\python.exe tests\test_gesture_live.py --real-mouse
```

and running the application itself.

---

## 22. Manual UI Checklist

Start the desktop interface:

```bash
python run.py
```

Voice and terminal only, no window:

```bash
python run.py --console
```

### Home

- [ ] **Machine line** names the OS product, the real build, the processor and installed RAM.
      On a Windows 11 machine it must say **Windows 11**, never Windows 10, and the build must
      be a real build number (e.g. `26200`) — never `10`.
- [ ] **Processor / Memory meters** move and are not pinned at 0 % or 100 %.
- [ ] **Graphics card** names *this* machine's adapter. On an NVIDIA machine it shows
      utilization, VRAM used/total and temperature. On an AMD or Intel machine it names the
      adapter and says **telemetry unavailable** — it must never say "No GPU detected", and it
      must never draw an unmeasured utilization as 0 %.
- [ ] The **header pill** says `TTS: GPU` or `TTS: CPU` — that is what speech is *using*, which
      is a different fact from what hardware exists.
- [ ] **Camera preview** shows a live image when the camera is on, and the gesture line says
      what the hand is doing.
- [ ] **Gesture toggle** uses a different *glyph* when off, not just a different shade.
- [ ] **Presence card** hides itself when the presence layer is not running.
- [ ] **Shut down Kayra** confirms first, then disables itself while teardown runs.

### Settings

- [ ] **Speech device** — the AUTO/GPU/CPU dropdown *and*, separately, what the live session is
      actually on. With no engine it says "Would use", never "Active device".
- [ ] **Speech backend** — requested and active shown separately. When they disagree the pill
      reads `Not applied`, not `Ready`.
- [ ] **Proactive presence** — live and persisted.
- [ ] No API key value is ever rendered; fields are masked and never populated.

### Memory

- [ ] List, delete one, clear all, open the file location.
- [ ] Deleting removes the row you clicked, not the one at that index — the store is appended
      to while the screen is open.
- [ ] A failed write leaves the row on screen rather than reporting a success.

### System

- [ ] **Operating system** and **OS version** are separate rows, both correct.
- [ ] One row per real graphics adapter, each with its video memory and driver version.
- [ ] **Display** shows the real resolution and scale — not `1920x1080` on a high-DPI laptop.
- [ ] The system drive is flagged, and it is the drive Windows is on, not whichever one you
      launched from.

### Orb and voice state

Drive each and confirm the caption and the orb agree:

| Say / do | Expected |
|---|---|
| nothing, microphone open | **Listening** |
| speak | **Listening…** / user speaking |
| ask a question | **Processing**, then **Speaking** |
| "stop" while it speaks | falls silent immediately; you have the floor |
| "stop listening" | **Listening paused**, microphone closed. No question asked. |
| "go to sleep" → "yes" | asks first, then **Standby**. "no" cancels. |
| "wake up" | back to **Listening** |
| pull the STT session | **Reconnecting…**, never a false "paused" |
| "shut down the engine" → "yes" | asks first, then **Shutting down**, then OFFLINE |

### The voice-reliability checks this milestone exists for

**These need a real person and a real microphone. Nothing else substitutes for them.**

| Say this | Expected |
|---|---|
| a long sentence, continuously, for several seconds | **ONE** `[VOICE] Utterance committed:` line with the whole sentence. Not several. |
| "Okay Kayra, मेरी girlfriend मुझसे नाराज़ है, बताओ मैं क्या करूँ?" | one committed turn, an ordinary conversational reply, and **no shutdown** |
| a sentence with a pause in the middle | still one turn — the pause must not split it |
| "Why did the program exit?" | ordinary conversation. No confirmation, no shutdown. |
| "Shutdown the engine car." | NEVER a Windows shutdown. Kayra asks which target you meant. |
| "Shut down the computer." | a COMPUTER confirmation, naming the computer explicitly |
| "Turn off listening." | microphone paused. Proactive suggestions are NOT touched. |
| "Exit." twice in a row | one confirmation, then "Please say yes or no." — not two questions |
| answer a confirmation with "Yes yes go to sleep." | accepted as YES; it must not reach the chatbot |
| answer a confirmation with "S" (say "yes" quietly) | accepted as YES, and the log shows `raw="S."` with its evidence |
| say "S" with NO confirmation pending | ordinary speech. Never YES. |
| "The application exists." | ordinary conversation |
| "exit" | **asks** "should I shut down the Kayra engine?" — say "no" |
| "Okay Kayra, shut down." → "yes" | asks, then exits once |
| "Okay Kayra, go to sleep." → "yes" | asks, then standby. The COMPUTER must not sleep. |
| "stop listening" | pauses within about half a second, no question |

What to watch in the log while you do it:

```
[VOICE] State: LISTENING -> USER_SPEAKING     (once, when you start)
[VOICE] State: USER_SPEAKING -> LISTENING     (once, when you stop)
[VOICE] Utterance committed: "..."            (ONCE per thing you said)
[VOICE] Control candidate: CONTROL_SHUTDOWN
[VOICE] Confirmation required
[VOICE] Confirmation: YES | NO
[SHUTDOWN] Executing confirmed Kayra shutdown
```

**Failure symptoms:**

| Symptom | Where to look |
|---|---|
| several `Utterance committed` lines for one sentence | the endpoint fired mid-speech. Raise `STT_VAD_HANGOVER_MS`; check `[DEBUG] [VAD]` for whether the room went quiet |
| the reply starts before you finish | same — and check that `window.kayraVad.ready` is true; with no VAD the endpointer degrades to recognizer-only |
| `USER_SPEAKING ↔ LISTENING` several times a second | VAD hysteresis. Check `STT_VAD_RELEASE_RATIO` / `STT_VAD_RELEASE_MS` |
| a short command feels laggy | `STT_FAST_ENDPOINT_MS`; also confirm the transcript is ≤3 words, or it takes the baseline window |
| Kayra shuts down without asking | a regression in `_dispatch_control`. `tests/test_voice_turn.py` section 8 should fail |
| the confirmation is asked but "yes" is ignored | the echo gate. `resolve_lifecycle_confirmation` must run BEFORE it and receive `echo=` |

Silence is still **Listening**. If an open microphone ever reads "paused", that is the bug the
voice state machine exists to prevent.

---

## 23. Troubleshooting

| Symptom | What to check |
|---|---|
| **UI shows the wrong Windows version** | `python -c "import sys;sys.path.insert(0,'src');from kayra.core import hardware;print(hardware.os_info().to_dict())"`. `source` must read `registry`. The registry's `ProductName` says "Windows 10" on Windows 11 by design — the build number is the truth. |
| **UI shows the wrong CPU or GPU** | `.\.venv\Scripts\python.exe tests\test_hardware_profile.py` — section 1 prints exactly what was read. |
| **Video memory reads "unknown"** | The driver is too old to publish the 64-bit `qwMemorySize`. The 32-bit field saturates at 4095 MiB and is deliberately refused rather than shown. |
| **CUDA unavailable** | `python run.py --doctor`. If CUDA is *offered* but not *usable*, the CUDA/cuDNN wheels are missing — re-run `python setup.py`. |
| **"CUDA: FAIL" on a machine with no NVIDIA GPU** | It should read `NOT APPLICABLE`. If it says FAIL, `detect_graphics()` found an NVIDIA adapter — check `.\.venv\Scripts\python.exe tests\test_setup_runtime.py` section 1. |
| **GPU absent / CPU-only PC** | Expected and supported. Speech runs on the processor at roughly real time. |
| **STT backend unavailable** | `[STT]` lines at boot. Brave ships the speech API with no backend; Edge is preinstalled on Windows 11 and works. |
| **Browser recovery loops** | `[STT] Session recovered` repeating. A `network` error is bounded to 3 attempts and only while nothing has ever been recognised. |
| **Microphone unavailable** | `.\.venv\Scripts\python.exe tests\test_audio_pipeline.py`. A muted device looks exactly like broken barge-in. |
| **Camera unavailable** | `.\.venv\Scripts\python.exe -m kayra.input.gesture --doctor`. Most webcams are exclusive-access — close anything else using it. |
| **Camera preview blank** | The preview is *pulled* by the UI on its own timer from the same single-frame mailbox the detector reads. There is no second `VideoCapture`. Check `[CAMERA]` lines. |
| **Gesture not responding** | `--doctor` first. Then `tests\test_gesture_live.py` (safe mode) to see what the FSM decided. |
| **Provider rate limit** | `[LLM] Result: RATE_LIMITED` followed by `[LLM] Fallback:`. That is the router working. A provider-supplied `Retry-After` always wins over the configured cooldown. |
| **Memory file missing** | It is created on first write. `.\.venv\Scripts\python.exe tests\test_memory_store.py` covers the missing-store case. |
| **Tests are noisy** | `KAYRA_LOG_LEVEL=WARNING`. `KAYRA_LOG_FILE=<path>` adds a rotating debug log. |
| **A suite fails only on your machine** | It should not. That is the defect — file it as a host-dependence bug, not as a local quirk. Two such bugs have already been fixed here. |

---

## 24. Expected Results

### Regression matrix

| Suite | Feature | Type | Needs | Writes real state | ~Time |
|---|---|---|---|---|---|
| `test_automation.py` | Automation pipeline | unit | none | temp dir only | 6s |
| `test_target_resolution.py` | Target resolution | unit | none | nothing | 3s |
| `test_voice_control.py` | Local control vocabulary | unit | none | nothing | 5s |
| `test_voice_state.py` | Voice state machine | unit | none | nothing | 3s |
| `test_capture_pipeline.py` | Capture, repair and context | unit | none | nothing | 3s |
| `test_voice_turn.py` | Utterance turn and safe control | unit | none | nothing | 3s |
| `test_emotion_engine.py` | Emotion engine | unit | none | nothing | 3s |
| `test_proactive_agent.py` | Proactive agent | unit | none | temp habit store | 6s |
| `test_proactive_presence.py` | Proactive presence | unit | none | nothing | 5s |
| `test_provider_router.py` | Provider routing | unit | none | nothing | 4s |
| `test_memory_store.py` | Memory management | unit | none | temp store only | 3s |
| `test_logging.py` | Structured logging | unit | none | nothing | 3s |
| `test_stt_backend.py` | STT backend switching | unit | none | nothing | 4s |
| `test_browser_selection.py` | Browser capability | unit | none | nothing | 3s |
| `test_gesture_state.py` | Gesture FSM and filters | unit | none | nothing | 6s |
| `test_gesture_control.py` | Gesture lifecycle | unit | none | nothing | 141s |
| `test_camera_runtime.py` | Camera runtime | unit | none | nothing | 34s |
| `test_ui.py` | Desktop interface | unit | none | nothing | 25s |
| `test_hardware_profile.py` | Hardware / OS detection | unit | none | nothing | 4s |
| `test_setup_runtime.py` | setup.py provisioning | unit | none | nothing | 3s |
| `test_environment.py` | Launcher and environment | unit | none | nothing | 6s |
| `test_tts_device.py` | ONNX Runtime device | unit | onnxruntime | nothing | 20s |
| `test_audio_pipeline.py` | Audio pipeline | integration | Kokoro model | nothing | 30s |
| `test_stt_lifecycle.py` | STT session lifecycle | integration | Chrome or Edge | nothing | 60s |
| `test_dmm_matrix.py` | DMM intent boundaries | integration | a model provider | nothing | 180s |
| `test_DMM.py` | DMM smoke | integration | a model provider | nothing | 20s |
| `test_engine.py` | LLM engine smoke | integration | a model provider | nothing | 20s |
| `test_voice.py` | Speech output smoke | integration | Kokoro model + audio out | nothing | 20s |
| `test_barge_in_live.py` | Barge-in | **manual** | a person to speak | nothing | 120s |
| `test_gesture_live.py` | Gesture, real hand | **manual** | a person and a camera | nothing | 300s |

### Per-suite check counts

A healthy unit run on any machine:

| Suite | Checks | Suite | Checks |
|---|---|---|---|
| `test_ui.py` | 631 | `test_logging.py` | 149 |
| `test_automation.py` | 263 | `test_tts_device.py` | 144 |
| `test_gesture_control.py` | 248 | `test_proactive_agent.py` | 140 |
| `test_proactive_presence.py` | 213 | `test_memory_store.py` | 138 |
| `test_voice_state.py` | 196 | `test_setup_runtime.py` | 138 |
| `test_voice_control.py` | 190 | `test_capture_pipeline.py` | 132 |
| `test_hardware_profile.py` | 188 | `test_stt_backend.py` | 127 |
| `test_gesture_state.py` | 174 | `test_provider_router.py` | 124 |
| `test_emotion_engine.py` | 119 | `test_target_resolution.py` | 108 |
| `test_browser_selection.py` | 66 | `test_environment.py` | 65 |
| `test_camera_runtime.py` | 64 | | |

**Total: 3990 checks across 22 unit suites, ~166 s.**

The counts above are what a green run prints, not a target to hit. If a number drops, a check
was removed — find out which one and why.

### What a healthy result looks like

- **Unit tier:** every `status` reads `ok`; the `fail` column totals **0**. One skip in
  `test_tts_device.py` is normal (a broken-CUDA state cannot be simulated where CUDA works).
- **Skips are information, not failures.** "This machine has no NVIDIA GPU" and "the NVIDIA
  path is broken" are different facts, and a suite that reported the first as the second would
  teach you to ignore red.
- **A failure is never fixed by changing the expectation.** If a check is wrong, say why in the
  comment and change the contract it asserts; if it is right, fix the code.
