# Dev workflow and the test suites

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

## Dev workflow

```
python setup.py           # once (or after changing requirements.txt, or to repair the runtime)
python run.py             # every time — desktop UI, no venv activation needed
python run.py --console   # voice + terminal only, no UI
python run.py --doctor    # interpreter, ONNX Runtime, providers, verified CUDA, GPU stats

python -m kayra.input.gesture            # hand gesture control alone, no UI
python -m kayra.input.gesture --doctor   # camera, detector and accelerator report only

.venv/Scripts/python tests/run_all.py               # the whole unit tier, one table
.venv/Scripts/python tests/run_all.py --integration # + network / browser / model suites
.venv/Scripts/python tests/run_all.py --live        # + real-hardware checks where offered
.venv/Scripts/python tests/run_all.py --list        # the inventory; runs nothing
```

**`tests/TESTING.md` is the authoritative testing guide** — exact commands, what a healthy run
prints, what each failure symptom means, and the manual UI checklist. `tests/run_all.py` holds
the ONE inventory (category, feature, what it needs, what real state it touches) and
`--list --markdown` prints the regression matrix that document embeds, so the two cannot drift.

`tests/_harness.py` is the shared scaffolding, and the isolation in it is STRUCTURAL rather
than a convention — because the convention has already failed twice here:

* `EnvironmentGuard` snapshots `.env`, the conversation store, the habit store and the browser
  cache around a whole suite, RESTORES anything that changed, and still FAILS the run for it.
  Silently repairing would hide the defect; refusing to repair would punish the developer for
  a test's mistake. (`test_ui.py` once rewrote the developer's real `.env`.)
* `HostPin` replaces `pressure_sample()` so no tier-1 suite can read the real battery or CPU
  load. (`test_proactive_agent.py` once failed nine checks because the laptop had dropped to
  12% and unplugged — the presence layer was right, the suite was wrong.)
* `RecordingInstaller` replaces `setup._pip`, so the setup suite exercises the real decision
  code and installs NOTHING.
* `TemporaryProject` redirects `core.paths` at a temp directory; `FakeMouse` records instead
  of moving the pointer; `MACHINES` is seven synthetic machines nobody owns.

**No pytest.** The suites boot real subsystems, take the single-instance lock and own browser
processes; a collector that imported them all into one interpreter would have them fighting
over the microphone. `run_all.py` runs each as its own process, exactly as a developer does by
hand, and only decides which ones and reports what happened.

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
    `test_voice_turn.py` (237 checks: the endpoint scenario table driven through the real
    predicate — a final segment arriving mid-speech, four segments becoming one turn, a
    breath inside a sentence, speech resuming during the grace window, a lone word after
    long speech, mixed Hindi/English — plus the dangerous-control confirmation state
    machine on an injected clock, the echo interaction, and the DMM's five-retry contract
    against a FAKE local model with no provider contacted; section 8 walks the AST for any
    path that could reach a dangerous action without a confirmation),
    `test_hardware_profile.py` (188 checks: the Windows product-name correction across seven
    builds, PCI vendor parsing, tri-state integrated classification, the 32-bit VRAM clamp,
    the whole detector driven against seven synthetic machines through a substituted registry,
    the NVIDIA telemetry gate proving no spawn on a non-NVIDIA machine, and an AST walk over
    every module in `src/kayra` proving no runnable string names a specific device),
    `test_setup_runtime.py` (138 checks: `configure_speech_runtime()` per synthetic machine
    with a RECORDING installer that installs nothing, reconciliation both ways, idempotence
    both ways, a stale driver, a genuine CUDA failure, an unrecognised CUDA major, the
    three-way PASS/FAIL/NOT APPLICABLE report, and agreement between setup's registry logic
    and `kayra.core.hardware`),
    `test_environment.py` (65 checks: `run.py` interpreter ownership and the sys.path rule,
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
    third-party noise control that does not disable anything, and cost),
    `test_gesture_state.py` (174 checks: synthetic 21-landmark hands at two distances proving
    scale invariance, the stability score and the deformation-vs-displacement distinction,
    One Euro / dead-zone / outlier gate / speed ceiling, hysteresis enter-exit-dwell and the
    rising edge, one click per held pinch, scroll direction hysteresis under injected noise,
    one-action-per-frame arbitration, the hand-loss grace period, measured jitter and outlier
    injection, the legacy double-click and fist gestures, the pause gate — including a
    side-by-side reproduction of the old per-frame rule's 206 ACTIVE<->PAUSED transitions
    against the fixed machine's zero — and cost),
    `test_gesture_control.py` (248 checks: configuration clamps and the hysteresis invariants,
    AST proof of exactly ONE VideoCapture and ONE hand-graph call site application-wide, AST
    proof that gesture code never touches voice state and that no UI module imports cv2 or
    mediapipe, the two switches and the camera-OFF-gesture-ON combination that cannot exist,
    the whole camera→detector→FSM→pointer path end to end against a fake camera and a fake
    detector, that nothing acts after OFF, the failure paths, the pulled preview, the voice
    vocabulary and its disjointness from shutdown, the app wiring and shutdown ORDER, the
    accelerator decision, the LOG LINE COUNT over a real session, the
    NO_HAND-is-not-PAUSED runtime distinction, and boundedness) and
    `test_camera_runtime.py` (64 checks: start/stop idempotence, newest-frame mailbox
    semantics, the v1 freeze scenario asserted away with a 4x-slow consumer, unopenable and
    silent cameras, bounded recovery that gives up rather than looping, release on every exit
    path including shutdown, telemetry, and ten-times-on-off cycling — the check that
    found the DSHOW thread leak; `--live` runs the same cycling against the real camera and
    asserts under 3 threads per cycle).
  - **Needs network or hardware.** `test_dmm_matrix.py` (53 intent-boundary cases, paced under
    Cohere's rate limit — but see the note above: it follows the same local-first routing as the
    assistant, so with LM Studio up it measures the LOCAL model), `test_audio_pipeline.py`
    (44 checks; needs the TTS model),
    `test_stt_lifecycle.py` (needs Chrome — run it with your OWN Chrome open, that is the
    interesting case), `test_DMM.py` / `test_engine.py` / `test_voice.py` (live API calls).
  - **Needs a human.** `test_barge_in_live.py` — checks the microphone is actually live first,
    because a muted input device looks exactly like broken barge-in.
    `test_gesture_live.py` — walks the ten scenarios A-J with a real camera and a real hand and
    reports what the runtime decided. **Safe by default**: without `--real-mouse` the pointer
    controller records instead of acting, so it can be run while you are reading its output.
    Nothing in the synthetic gesture suites can answer "does the cursor feel right", so no
    claim that gesture control is fixed rests on them alone.
- Last full run (2026-09-09, after the hardware-detection and test-modernisation round):
  **3990 tier-1 checks, 3990 passing, 1 skipped, 166s**, across 22 unit suites —
  `.venv/Scripts/python tests/run_all.py`. Per suite: 553 UI, 263 automation, 248 gesture
  control, 213 proactive presence, 366 voice turn, 307 automation, 210 voice control, 196 voice state, 188 hardware profile,
  174 gesture state, 149 logging, 144 TTS device, 140 proactive agent, 138 memory store,
  138 setup runtime, 132 capture pipeline, 127 STT backend, 124 provider router, 119 emotion,
  108 target resolution, 66 browser selection, 65 environment, 64 camera runtime.
  The one skip is `test_tts_device`'s broken-CUDA simulation, which has nothing to simulate on
  a machine where CUDA genuinely works.
- **THE FOUR LONG-STANDING FAILURES ARE FIXED, AND THEY WERE ALL THE SAME BUG:** a tier-1
  suite reading the developer's own configuration. None of them was a defect in the code they
  were testing.
  - `test_browser_selection` ×1 — the check grouped `None` with the sentinels `"auto"`/`""`/
    `"default"`. Those are the user SAYING "pick for me"; `None` means the caller expressed
    nothing, so the engine reads `STT_BROWSER` from the configuration, which is the whole point
    of having that setting. The check passed on a machine with no `STT_BROWSER` and failed on
    one that had set it, on identical code. Now the configuration is substituted at
    `speech_to_text.env` — NOT through `os.environ`, because `core.config.env()` gives `.env`
    precedence over the process environment — and all three outcomes are pinned explicitly.
  - `test_environment` ×1 — asserted `failure_reason` was EMPTY when CUDA is usable. That field
    explains why the ACTIVE DEVICE is not the GPU, and `"CPU was requested."` is a perfectly
    good explanation: it is the user's own setting. It now asserts the weaker and correct
    thing — that a usable CUDA runtime is never described as BROKEN.
  - `test_tts_device` ×2 — `TextToSpeechEngine()` with no argument reads `TTS_DEVICE_MODE` from
    the configuration, so "the default engine runs on CUDA" was describing the developer's
    setting, and "switching replaced the session" failed because switching to CPU from CPU is
    correctly a no-op. The engine is now constructed with `device_mode="AUTO"`, which is what
    those GPU assertions actually mean, and the no-op case is checked separately.
- **A tier-1 suite must not read the host's state**, and this rule now has a mechanism behind
  it rather than only a history. `tests/_harness.py` supplies `EnvironmentGuard` (fails a run
  that touched `.env`, the conversation store, the habit store or the browser cache),
  `HostPin` (no real battery or CPU load), `RecordingInstaller` (no package is installed) and
  `TemporaryProject` (persistence code runs for real against a store nobody owns). The two
  original incidents:
  - `test_proactive_agent.py` read the machine's REAL battery through
    `system_profile.pressure_sample()`. The suite was green all afternoon and then failed 9
    checks because the laptop had dropped to **12% and unplugged** — the presence layer
    correctly raised a CRITICAL `battery_low` candidate, which by design outranks every
    candidate those tests exercise. The presence layer was right; the suite was wrong. It pins
    a healthy, plugged-in, unloaded host in `_pin_host_environment()`, and the tests that care
    about the thresholds still drive `pressure_sample` directly with their own values.
  - `test_ui.py`'s speech-backend checks drove the real `SettingsView._on_backend`, which
    persists on a committed switch — so with a stubbed bridge reporting success it rewrote the
    DEVELOPER'S OWN `.env`. `NoEnvWrites` blocks and records those writes (making them
    assertable: a committed switch must persist, a failed one must not), and
    `section_no_side_effects` compares the whole `.env` before and after the run so any future
    writer is named rather than discovered later.
- **Two new suites, and one new section, all hardware-portable:**
  - `tests/test_hardware_profile.py` (188 checks) — the OS product correction across seven
    builds including a genuine Windows 10, a Server SKU and a hypothetical Windows 12; PCI
    vendor parsing; integrated classification as a TRI-STATE; the 32-bit VRAM clamp; the whole
    detector driven end to end against seven synthetic machines through a substituted registry;
    and an AST walk over every module in `src/kayra` proving no runnable string names a
    specific device.
  - `tests/test_setup_runtime.py` (138 checks) — `configure_speech_runtime()` against every
    synthetic machine with a recording installer, asserting exactly which distributions were
    requested. **Installs nothing.** Covers reconciliation in both directions, idempotence in
    both directions, a stale driver, a genuine CUDA failure, an unrecognised CUDA major, and
    the three-way PASS / FAIL / NOT APPLICABLE report.
  - `test_ui.py`'s `section_hardware_portability` renders Home against five machines that do
    not exist and fails if any value from a DIFFERENT machine appears on screen. The search is
    scoped to the Graphics card and filtered by `isVisibleTo()` — Home's empty states are
    permanent hidden children, so an unfiltered search reports "No GPU detected" as visible on
    every machine, and the System card beside it renders the REAL host by design.
  UI checks went 468 -> 553; the tier-1 total went 3124 -> 3539, and the
  voice-reliability milestones that followed took it to 3990 across 22 suites.
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


## The hygiene sweep (Part 16, 2026-11-11)

`tests/run_all.py` fingerprints every file under `data/`, `logs/`, `Reports/` and `backups/`
(size + mtime, the running app's `kayra.lock` excepted) before the run and again after it. Any
file added, changed or removed fails the run and is named. Route a suite's writes to a temp
dir (`learning.DATA_DIR`, `backup.ROOT`, `web_macros.FILE_ROOTS`, `KAYRA_AUDIT_LOG`, …) — never
"fix" the sweep. The first sweep (unit tier + the browser integration suites) found nothing.
