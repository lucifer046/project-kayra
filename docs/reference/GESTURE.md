# Hand gesture control

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

## Hand gesture control (rewritten 2026-09-09)

`src/kayra/input/gesture/`. Camera in, mouse pointer out. The v1 engine
(`src/kayra/input/gesture.py`, a single 551-line module) was standalone, never wired into
`app.py`, and reported as unreliable in every dimension: cursor wobble, accidental clicks,
flickering gesture state, scroll that reversed or would not stop, occasional freezes, and
gestures that sometimes stopped working. All of it is preserved in git history; none of it
remains in the tree.

```
camera ──▶ preprocess ──▶ detect ──▶ features ──▶ stabilise ──▶ FSM ──▶ arbitrate ──▶ pointer
             │
             └──▶ preview (same frame, throttled, pulled by the UI — no second capture)
```

| Module | Owns |
|---|---|
| `camera.py` | THE capture: one `VideoCapture`, one thread, a SINGLE-FRAME mailbox, bounded recovery |
| `detector.py` | The MediaPipe graph, one hand, deterministic primary selection, the accelerator decision |
| `features.py` | Scale-invariant geometry and the hand-stability score |
| `filters.py` | One Euro, dead-zone, outlier gate, speed ceiling, the `Hysteresis` gate, rate limiters |
| `state_machine.py` | The temporal FSM and the one-action-per-frame arbitration |
| `pointer.py` | The only code that touches the desktop (`user32`), split rate limits |
| `controller.py` | Lifecycle, the two switches, the thread, the preview, telemetry, events |
| `config.py` | Every threshold, read once from `.env`, clamped, and invariant-checked |

### The five defects, and what each one actually was

* **Cursor wobble.** MediaPipe's landmark regression jitters ~1.5-3 px RMS on a perfectly
  still hand — inherent to the model. v1 mapped that into SCREEN space *before* filtering, and
  the usable region maps ~440x195 px onto 1920x1080, so 2px of model noise became 11px of
  pointer movement. Filtering now happens in NORMALIZED frame space and the mapping comes
  after, a pixel DEAD-ZONE follows the mapping (movement below it is not emitted at all — a
  filter converges towards a noisy mean, it does not stop), and a speed ceiling backstops
  both. Measured: 8.3px raw → 3.5px filtered, with 180 of 300 frames emitting no pointer
  event at all for a still hand.
* **Sluggishness, from the fix for the wobble.** v1's One Euro used `beta=0.05`, a value for a
  filter running on screen PIXELS. In normalized units a fast flick is ~0.6 units/s, so that
  beta contributed 0.006 Hz to a 1.5 Hz cutoff — the adaptive half of the adaptive filter was
  effectively switched off and every movement got the rest-smoothing. Beta is now **12.0**.
  Measured at MEDIUM: slow-move lag 99ms → **39ms**, fast-flick lag 80ms → **16ms**, with the
  same rest-jitter reduction.
* **Accidental clicks and lost clicks, from PIXEL thresholds.** `pinch < 30px` is a third of a
  hand at arm's length (everything reads as a pinch) and unreachable when the user leans in
  (nothing does). **Every geometric threshold is now a ratio of hand scale** —
  wrist-to-middle-MCP, the palm's rigid long axis. The suite asserts the same pinch measures
  the same at 1.6x and 0.6x hand size.
* **Freezes.** v1 read the camera synchronously in the same loop as inference. `cap.read()`
  blocks, and frames the driver queued while inference was busy had to be dequeued one at a
  time — so a 200ms hiccup left the loop processing the past and it never caught up. Latency
  grew without bound. `CAP_PROP_BUFFERSIZE=1` was already set and does not fix it: Windows'
  MSMF and DSHOW backends treat it as a hint. The capture is now its own thread writing into a
  **single-slot mailbox**; a frame nobody read is dropped and counted. Measured with a
  consumer 4x slower than the camera: staleness never exceeds one frame.
* **"Gestures sometimes stop working."** v1's counters were reset by whichever `if` branch ran
  last, so a counter could stay latched by a branch that stopped running. Every gate is now
  updated on EVERY frame and `reset()` touches all of them exhaustively.

### The capture backend, and a leak found by finally running the resource check

`camera.py` hardcoded `CAP_DSHOW` with a comment claiming it opened in 380ms against 1.9s for
MSMF. **That number was never measured.** When the ten-times-on-off cycling check was actually
run against the real camera, DSHOW turned out to be both the slowest option and a thread leak.

Measured here, one clean process per backend, 8 open/close cycles each:

| backend | threads leaked per cycle | open + first frame |
|---|---|---|
| `CAP_DSHOW` | **17.2** | **1655 ms** |
| `CAP_MSMF` | 2.0 | 529 ms |
| `CAP_ANY` | 2.0 | 516 ms |

End to end, twenty camera/gesture toggles from Home took the process from 5 threads / 25 MB to
**334 threads / 237 MB**, and none of it came back. The default is now AUTO (`CAP_ANY`), after
which the same twenty toggles leave the thread count **flat at 33-34** and RSS stable around
127 MB. `tests/test_camera_runtime.py --live` asserts under 3 threads per cycle (measured:
**+0.1**), and the hardware-free section asserts the runtime's own threads and collections do
not accumulate.

`GESTURE_CAMERA_BACKEND` keeps this configurable, because capture-backend behaviour genuinely
differs between machines — but it should only be changed with a measurement, which is the
lesson the original comment failed to apply.

### Normalized features — the one idea

`hand_scale` = wrist → middle MCP, in **width-normalized** units (x as given, y multiplied by
`height/width`, so a vertical and a horizontal span of equal pixel length measure equal). Every
distance the state machine sees is divided by it. Finger extension is a CONTINUOUS 0..1 value,
not the boolean `dist(wrist,tip) > dist(wrist,pip)` v1 used — that is a comparator on a noisy
signal and it chattered exactly at the half-curled poses where MOVE and SCROLL meet.

### `stability` gates ACTIONS, never TRACKING

Three multiplied factors: detector confidence, hand-scale steadiness, and landmark coherence.
The asymmetry is deliberate — a stuttering pointer during noisy tracking is an annoyance, a
click fired during noisy tracking lands on whatever is underneath it.

**Coherence is the DEFORMATION RESIDUAL, not the displacement.** Total per-landmark movement
conflates a hand that MOVED (every landmark travels together — a perfectly good detection) with
a hand that was RE-FIT (landmarks travel in different directions — the model guessing). Scoring
on displacement would suppress clicks during every deliberate movement, which makes dragging
impossible. Subtracting the mean translation first leaves rigid motion at full stability and
still catches incoherence. Found by a test that required both properties at once.

### The temporal state machine, and arbitration

Every actionable gesture goes through a `Hysteresis` gate: an ENTER threshold, an EXIT
threshold that must be further out, and a DWELL time. Both halves are needed — hysteresis alone
still fires on one deep brief excursion, a dwell alone still chatters around one threshold.
Actions fire on the RISING EDGE only, which is what makes "pinch, pinch, pinch, release" one
click rather than three.

**Priority is fixed, not situational:** `SCROLL > DOUBLE > RIGHT > LEFT > CURSOR`. While the
scroll pose holds, the click gates are **reset**, not merely ignored — two fingers travelling
together inevitably bring the thumb near a fingertip, and a gate that were only ignored would
accumulate its dwell throughout the scroll and fire the instant the pose ended.

**Index and middle pinches are made mutually exclusive before either is gated.** A thumb
equidistant from both fingertips satisfies neither: an ambiguous intent is answered by doing
nothing, not by picking one.

### The gesture state machine — full transition table (audited 2026-09-09)

Fourteen states. Every actionable transition has an ENTER condition, a HOLD requirement and an
EXIT condition that is distinct from the enter one. Arbitration priority is fixed:
**PAUSE > SCROLL > DOUBLE > RIGHT > LEFT > CURSOR**.

| Current | Condition | Next | Action |
|---|---|---|---|
| *(any)* | hand absent, past `hand_lost_grace_ms` | `NO_HAND` | reset every gate; no action |
| *(any)* | hand absent, within grace | *(held)* | none — `suppressed="hand-lost-grace"` |
| `NO_HAND` | a valid hand appears | `ACQUIRING`/`CURSOR` | stabiliser re-seeds; **no action on arrival** |
| `ACQUIRING` | `stability ≥ min_stability` and `confidence ≥ min_confidence` | `TRACKING` | actions become permitted |
| `TRACKING`/`CURSOR` | index extended | `CURSOR` | pointer tracks |
| `CURSOR` | `fistness > pause_enter` (0.82) | `PAUSE_CANDIDATE` | **pointer still tracks**; discrete actions held |
| `PAUSE_CANDIDATE` | held for `pause_hold_ms` (500ms) **and** frame trusted | `PAUSED` | release all gates; log `Pause gesture detected` |
| `PAUSE_CANDIDATE` | held but frame **not** trusted | `CURSOR` | gate dropped; `suppressed="unstable"` |
| `PAUSE_CANDIDATE` | `fistness < pause_exit` (0.55) | `CURSOR` | dwell abandoned; nothing fired |
| `PAUSED` | `fistness > pause_exit` | `PAUSED` | resume dwell **restarts** |
| `PAUSED` | `fistness ≤ pause_exit` | `RESUME_CANDIDATE` | no action |
| `RESUME_CANDIDATE` | open for `resume_hold_ms` (300ms) **continuously** | `CURSOR`/… | gate reset; ordinary branches resume |
| `RESUME_CANDIDATE` | hand closes again | `PAUSED` | resume dwell discarded |
| `CURSOR` | two-finger pose held `scroll_pose_ms` | `SCROLL_CANDIDATE` | click gates **reset**, not ignored |
| `SCROLL_CANDIDATE` | \|velocity\| > `scroll_enter` | `SCROLL_ACTIVE` | wheel impulse, rate-limited |
| `SCROLL_ACTIVE` | \|velocity\| < `scroll_exit` | `SCROLL_CANDIDATE` | direction cleared |
| `SCROLL_*` | pose lost | `CURSOR` | scroll history dropped |
| `CURSOR` | one pinch closing | `LEFT_/RIGHT_CLICK_CANDIDATE` | **pointer frozen** |
| `*_CLICK_CANDIDATE` | held `pinch_hold_ms`, trusted, cooldown clear | `*_CLICK_HELD` | click fires on the **rising edge only** |
| `*_CLICK_CANDIDATE` | separation > `pinch_exit` | `CURSOR` | nothing fired |
| `*_CLICK_HELD` | held past `drag_unlock_ms` | `*_CLICK_HELD` | pointer tracks again (drag) |
| `*_CLICK_HELD` | separation > `pinch_exit` | `CURSOR` | no second click |
| `CURSOR` | both pinches closed (`wider_pinch < double_enter`) | `DOUBLE_CLICK_HELD` | double click; single gates reset |

**Mutual exclusion is structural, not incidental.** A frame can produce at most one of
`fire_left` / `fire_right` / `fire_double` / `scroll_delta` — asserted across every pose in
`tests/test_gesture_state.py::section_conflicts`. Index and middle pinches are separated by a
margin before either is gated, so a thumb equidistant from both engages neither.

**The runtime state is a different machine and maps from this one:**

| Gesture state | Runtime state | Home shows |
|---|---|---|
| `PAUSED`, `RESUME_CANDIDATE` | `PAUSED` | pill **Paused**, "Open your hand to resume" |
| `NO_HAND` | `ACTIVE_NO_HAND` | pill **Active**, "No hand in frame" |
| everything else | `ACTIVE` | pill **Active**, "Hand detected · Cursor" |

`NO_HAND` NEVER maps to `PAUSED`. That distinction is the whole of §7 of the brief: "no hand"
is a fact about the world, "paused" is something the user did, and telling a user who lowered
their hand that they paused the system is both wrong and unactionable.

### The pointer freezes while a click is being made

This is why clicks land where the user aimed. Pinching physically curls the index finger
towards the thumb, and the index tip is the landmark driving the cursor — so the act of
clicking drags the pointer by roughly half a hand-width in the ~80ms before it fires. The
pointer is frozen from the moment a pinch becomes a candidate until it has been held past
`GESTURE_DRAG_UNLOCK_MS` (260ms), after which the user is evidently dragging and it follows
again. One gesture, both behaviours.

### The pause gesture, and the ACTIVE ↔ PAUSED flap (fixed 2026-09-09)

**This was the one gesture that shipped without a temporal gate, and it flapped.**

v2.0 tested `features.fist` — `extended_count == 0`, i.e. four independent
`extension >= 0.55` comparators — once per frame and paused immediately. The code comment
claimed it "needs no dwell reasoning of its own beyond the extension gate already applied".
That was wrong, and it is worth naming why the mistake was easy to make: every OTHER gesture
in the file goes through `Hysteresis`, so the pause looked like it inherited the discipline
when in fact it bypassed it.

A real pointing finger is not perfectly straight. Its measured extension sits close to 0.55,
and landmark noise carries it back and forth across that comparator several times a second.
Measured on the reproduction now in `tests/test_gesture_state.py`:

| index finger straightness | measured extension | old rule: ACTIVE↔PAUSED transitions / 600 frames |
|---|---|---|
| 0.70 | 0.89 | 0 |
| 0.62 | 0.77 | 0 |
| 0.55 | 0.67 | 0 |
| 0.50 | **0.59** | **156** |
| 0.45 | **0.52** | **206** |

206 transitions in 600 frames is ~8.6 per second, each a full runtime transition and two INFO
log lines. At 0.45 straightness the system was PAUSED for 78% of the session — gesture control
was, in practice, dead for anyone who points with a relaxed finger.

**The fix has three parts, and all three are needed:**

1. **A continuous signal.** `fistness = 1 - max(extension)`. A fist requires EVERY finger
   curled, so the MOST EXTENDED finger governs — and a continuous value is something a gate
   can act on, where a count of booleans is not.
2. **Hysteresis with a real dwell.** Enter at 0.82 (the straightest finger below 0.18
   extended — nowhere near any resting pose), leave at 0.55, hold for `GESTURE_PAUSE_HOLD_MS`
   (500ms). The threshold moves the decision away from where noise lives; the dwell makes it
   deliberate. Entering also requires a TRUSTED frame, exactly as a click does.
3. **A CONTINUOUS resume dwell.** `GESTURE_RESUME_HOLD_MS` (300ms) of consistently-open hand,
   and the timer RESTARTS if the hand closes again. The first attempt used a wall-clock timer
   from the first non-fist frame, which resumed 300ms later even under a hand that had gone
   straight back into a fist — caught by the "three frames of an open hand" check.

After the fix: **0 transitions at every straightness in the table**, while a sustained fist
still pauses exactly once and a sustained open hand resumes.

**`thumb_pinched` is what separates a FIST from the three-finger BEAK.** Both curl every
finger; only the thumb's position tells them apart, and that guard is also what keeps a pinch
made with an otherwise-closed hand from being read as a forming fist.

### Scroll is VELOCITY, not offset from an anchor

v1 locked an anchor and scrolled in proportion to the distance from it — a joystick, which
keeps scrolling forever while the hand is held still away from the anchor. That is why
scrolling was hard to stop. Velocity means a still hand scrolls by nothing. Direction has its
own hysteresis (a separate gate per direction with a neutral band between), so one noisy frame
cannot reverse it; impulses are magnitude-capped and rate-limited.

### Hand loss: HOLD the state, produce NO action

Within `GESTURE_HAND_LOST_GRACE_MS` (220ms) a dropout holds the state — so it cannot cancel a
drag or reset a scroll — while producing nothing, so it can never itself cause a click. Past it
everything is dropped. On re-acquisition the filter history is discarded but the last EMITTED
pointer position is KEPT, so the pointer walks to the hand under the speed ceiling instead of
teleporting.

### Two switches, and the combination that cannot exist

```
camera ON,  gesture OFF   valid — the Home preview works, nothing touches the pointer
camera ON,  gesture ON    valid — the feature, running
camera OFF, gesture OFF   valid — nothing running, the device released
camera OFF, gesture ON    PREVENTED, not repaired
```

Enabling gesture control with the camera off starts the camera first; turning the camera off
turns gesture control off first, in that order (a gesture runtime with no frames reports ACTIVE
while doing nothing). Both switches are LIVE and TRANSACTIONAL: they act on the running
controller and are reflected back from it, so a camera that fails to open leaves the switch
OFF and shows the camera's own message. Same discipline as the speech backend's
requested-vs-active.

### One thread, one camera, one preview

`kayra-gesture` runs whenever the camera is on; with gesture control off it does nothing but
republish a preview frame. The Home preview reads the SAME mailbox — there is no second
`VideoCapture`, because most webcams are exclusive-access devices. The preview is converted to
ready-to-paint RGB888 **on the gesture thread** and the UI **PULLS** it on its own 15 FPS timer:
a signal carrying frames would be a queue, and a queue the GUI thread drains more slowly than
the camera fills it is unbounded latency. A pull model cannot have a backlog.

`tests/test_gesture_control.py` asserts by AST that there is exactly ONE `VideoCapture` call
site and ONE hand-graph construction site in the whole application, and that no UI module
imports `cv2` or `mediapipe`.

### It never touches voice state, and that is asserted

No call in the package reaches `set_listening`, `set_sleeping`, the STT engine, the TTS engine
or the voice state machine — AST-asserted. Camera activity must never be able to pause the
microphone or repaint the orb. The two systems share only the runtime event bus, in one
direction: the controller emits `gesture_state`, and nothing more.

### Why it lives in `input/` and not `automation/`

The camera is a capture device and this package's job is to work out what the user MEANT,
exactly as the speech package does. It reaches the desktop through `pointer.py` — `user32`,
no shell, no subprocess — rather than through `automation.windows`, for two reasons:
`automation` must never import `input`, and the normalize→policy→resolve→plan→execute pipeline
is the right cost for a sentence and the wrong cost for a 30Hz pointer update. Measured:
`pyautogui.moveTo` 1.9ms versus `user32.SetCursorPos` 0.012ms. A SPOKEN "click" still goes
through `automation`, unchanged.

### The GPU question, answered by measurement

**CPU, and no GPU is initialised at all.** `mediapipe 0.10.14`'s pip wheel builds
`solutions.hands` CPU-only on Windows; the Tasks API exposes a GPU delegate enum on every
platform including ones with no GPU calculators compiled in, so testing for the enum proves
nothing (the same trap `tts_device` documents for `get_available_providers()`).

It would be the wrong trade regardless. Measured live on this RTX 4060:

| | Measured |
|---|---|
| Inference, complexity 0 | **10.5-13.5ms** (mean 11.1) — three frames of headroom at 30 FPS |
| Camera FPS / processing FPS | 29.8 / 30.0, then 19.8 / 19.8 as auto-exposure lengthened |
| Dropped frames in 459 | **0** |
| Process CPU | 23.6% of one core |
| Process RSS | +163 MB over baseline |
| **VRAM used by gesture control** | **0 MiB** |
| TTS synthesis alone | 13.01s / sentence |
| TTS synthesis WITH gesture running | 13.30s (**+2.2%**) |
| Gesture FPS while TTS synthesises | 19.8, 9.96ms inference, 0 dropped |

So there is no GPU contention with Kokoro to have — gesture control allocates no VRAM and
holds no CUDA context — and the CPU cost to speech is 2.2%. `GESTURE_GPU=ON` forces a delegate
attempt for a platform where one exists and says so loudly when it fails; `AUTO` (the default)
probes once and stays on the processor without complaint.

**`model_complexity=0` is the change that mattered, not the device.** v1 used complexity 1 at
26-31ms, which on a 33ms budget leaves nothing — and a loop with no slack falls behind the
camera on the first hiccup and, in v1, never caught up.

### Voice commands

`ControlKind.GESTURE_ON / GESTURE_OFF / CAMERA_ON / CAMERA_OFF`, in `core.voice_control`,
matched locally before the DMM like the rest of that vocabulary — a user reaching for "turn off
hand gesture control" is usually reaching for it because the pointer is doing something they
did not ask for, and 1.5s of VAD plus cloud round-trip is the last thing that request should
wait on. Measured at ~15us per utterance.

**The boundary is disjoint by construction and asserted phrase by phrase:** no gesture or
camera phrase names Kayra, and none is a bare "turn off". `turn off kayra` is SHUTDOWN,
`turn off hand gesture control` is this, `turn off my pc` is not in the vocabulary at all and
reaches the CONFIRM-gated automation layer. Matching stays exact on the whole utterance, so
"turn off the camera and open chrome" is an instruction and falls through to the DMM.

There is deliberately no bare "gesture on" / "gestures off": matching is whole-utterance, so a
short phrase buys nothing and costs the thing that keeps this safe, which is being unmistakable.

### UI

Home carries a **Hand gesture** card: a live camera preview, a camera control and a gesture
control (a DIFFERENT GLYPH when off, not a different shade — a camera makes no sound, so
whether it is watching must be readable as a shape), and one line saying what the hand is
doing. Three facts, three rows, and no telemetry: frame rates and confidences live behind
`GESTURE_DIAGNOSTICS`. Settings carries the two live switches plus the three preset dials.

**A fixed-width preview clipped the whole card off the right edge of Home's bottom strip** —
the same defect the System card's footprint caption caused once already. A widget with a hard
minimum width forces its card to that width and the row's minimum then exceeds the window.
Two fixes: the preview is fixed-HEIGHT and expanding-width, and `Card` titles now elide instead
of reporting their full text width as a minimum (which also cut the strip's minimum from
1330px to 961px, below the 1040px `min_window_width` it had been quietly exceeding). Caught by
rendering the page and looking at it — the layout tests passed throughout.

### Shutdown

`request_shutdown` tears the gesture runtime down at step 3b, BEFORE the audio and browser
teardown: a runtime left running past that point could still move the pointer while the process
disappears, and a camera left open is a device no other application can claim. The camera is
released on every exit path including an exception, and `PointerController.disable()` lifts any
button still logically held — a user who switches off mid-pinch must not be left dragging.

### Known limitations

* **The thread count does not return to its pre-camera baseline, but it no longer GROWS.**
  Starting gesture control takes the process from ~5 threads to ~33 (OpenCV's capture pool and
  MediaPipe's TFLite/XNNPACK workers) and stopping it does not give them all back. Measured
  over twenty full on/off cycles the count stays flat at 33-34 and RSS oscillates around
  127 MB, so this is a one-time allocation rather than a leak — which is precisely what the
  DSHOW backend was NOT, and why that check now exists.
* **Real-hand validation needs a person.** `tests/test_gesture_live.py` walks the ten
  scenarios A-J with a real camera and reports what the system decided; the synthetic suites
  cannot answer "does it feel right", and no claim here rests on them alone. It is SAFE BY
  DEFAULT — `--real-mouse` requires an explicit typed confirmation — and it refuses real-mouse
  mode outright when the process cannot reach the interactive input desktop, because Windows
  silently returns FALSE from `SetCursorPos` there and every injection then LOOKS like it
  worked.
* App identity for the primary-hand choice is size-and-stability based, so two hands of very
  similar size and distance can still swap control if one is deliberately raised.
* The camera's own frame rate is whatever the driver gives; on this machine auto-exposure
  dropped it from 30 to 19.8 FPS in ordinary indoor light. The processor tracks it exactly
  (0 dropped frames either way), but gesture latency follows the camera, not the code.
