# The desktop UI

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

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
- **The orb stops animating when hidden** (`hideEvent`). 30fps busy, 12fps idle, zero when
  not visible. The ambient backdrop is the only other continuously animated element, and it
  is slower still: 8fps, and zero when hidden.
- **The dock owns no timer.** Its hover lift is a `QVariantAnimation` that runs only while the
  pointer is arriving or leaving. The suite asserts the dock has no `QTimer` children at all
  and that the drawer adds none of its own.
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

### Two shells, and exactly one navigation surface at a time (2026-09-10)

```
Home, Chat                                  every other screen
─────────────────────────────               ─────────────────────────
no permanent rail                           permanent left rail
floating dock, bottom centre                no dock
navigation drawer on demand                 navigation always visible
```

`KayraWindow.DOCK_SCREENS` is the rule; `_apply_shell()` is the only implementation. **Two
navigation surfaces on one page is the state that must never exist**, which is why one
function shows one and hides the other and closes the drawer when a rail screen is entered.

- **The dock and the drawer are CHILDREN of the page host, not layout items.** Neither may
  take space from the content: the moment either did, opening the drawer would relayout Home
  — the orb slides, the backdrop's bloom moves, every elided caption re-elides twice per
  open. `_PageHost.reposition_overlays()` is the one place their geometry is computed, and it
  tests `isHidden()` rather than `isVisible()` (a widget whose parent chain is not yet shown
  reports invisible, so a visibility test skips positioning for the whole of construction and
  leaves the dock at (0, 0)).
- **Home and Chat reserve the dock's height in their bottom margin.** The dock is an overlay
  and cannot reserve its own space; without the margin it sits on top of Chat's composer and
  the lowest row of Home's outer columns. The suite asserts the clearance on both screens.
- **ONE CONTROL FOR ONE STATE.** "Microphone on/off" and "start/stop listening" are the same
  fact and the dock has ONE control for them. Two would be two places to read a single state
  and inevitably two places for it to be read differently.
- **Home carries no controls at all.** It is pure status; every press is in the dock. That is
  what makes it structurally impossible for a control on that page to disagree with the same
  control two inches below it.
- **The actions live in `ui/controls.py`.** `KayraControls` is what the dock, the keyboard
  shortcuts and the tray all call, so one press cannot have three implementations.
  `confirm_shutdown()` asks and then delegates to `bridge.shutdown(hard=True)` —
  `app.request_shutdown`, unchanged and unduplicated.
- **`DESTINATIONS` is a module-level tuple in `components/navigation.py`,** read by both the
  rail and the drawer. Two copies would be two places to add a screen.

### The ambient backdrop, and why `#ContentArea` is transparent

`components/backdrop.py` paints behind everything: a vertical wash, a diagonal hairline
lattice cached in a pixmap, a warm radial bloom the view positions (Home puts it behind the
orb), and eighteen slow motes. **8 fps, a 90-second cycle, the lattice never repainted, the
timer stopped when hidden** — it is slower than the orb's idle rate and the suite asserts it.

**`#ContentArea` had to become `transparent`.** It was `background-color: base`, an opaque
sheet over the whole content area under which the backdrop painted perfectly and reached not
one pixel of the screen. Anything needing a ground of its own declares one; the page must not.

`prefers_reduced_motion()` reads the platform preference (with a `KAYRA_REDUCED_MOTION`
override for the suite) and `set_animated(False)` keeps the picture and drops the drift.

### Custom window chrome answers WM_NCHITTEST; it never moves the window

`components/chrome.py`. The window is frameless and Windows is still in charge: `HTCAPTION`
over the title area, `HTLEFT`/`HTTOPRIGHT`/… over the border band, and Windows itself then
performs Aero Snap, Win+Arrow, drag-to-maximise, double-click-to-maximise, shake, edge resize,
the system menu and the Windows 11 snap-layouts flyout.

- **`mouseMoveEvent` appears nowhere in that module and must not.** A manual drag takes
  precedence over the hit test and silently removes every behaviour above. The suite walks
  the AST for it.
- **It declines on a platform that cannot hit-test** — Linux, macOS, and the offscreen plugin
  the suite runs on — and the native title bar is kept. A degraded custom title bar is worse
  than the real one.
- The hit test reads `QCursor.pos()` rather than unpacking lParam: those are screen
  coordinates in two words, and getting them right across monitors with negative coordinates
  is a well-known off-by-a-monitor bug. Qt has already done that arithmetic.
- Measured on this machine: Qt's frameless `showMaximized()` already lands on the work area
  rather than covering the taskbar, so there is no `WM_GETMINMAXINFO` handling.

### Component vocabulary

Views compose components; they never style a widget themselves. The shared set is
`Card`, `GlassPanel`, `StatusPill`, `CardAction`, `SegmentedControl`, `Disclosure`,
`IconButton`, `ListRow`, `Meter`, `StatRow`, `Toggle`, `EmptyState`, `Metric`, `GroupLabel`,
`Divider`, `RowRule` plus the text helpers, and the shell components `FloatingDock`,
`NavigationDrawer`, `AmbientBackdrop`, `AppWindowChrome`, `Sidebar`.

**`GlassPanel` and `Card` are different objects, not two styles of one.** A `Card` is OPAQUE
and sits IN a page — the five utility screens are built from them, where content is dense and
a solid ground is what makes a table readable. A `GlassPanel` is TRANSLUCENT and floats OVER
the backdrop, which is the point on Home: the bloom behind the orb bleeds through the panels
around it instead of stopping at their edges. Restyling `Card` to be translucent would have
put a moving backdrop behind every settings form. **If a row appears on two screens it must be the same component** — Activity,
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
phrasings only the classifier catches. **"exit"** is the only one that ends the process — and since 2026-09-10 it ASKS FIRST. See
"Dangerous voice control is confirmation-gated": a single recognition of a shutdown or sleep
phrase raises a confirmation and nothing else, and only an affirmative answer inside the window
executes it.

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
- **Home has NO fixed pixel geometry.** Columns are stretch ratios (3 : 4 : 3), the orb is
  sized from the centre column's own share of the width (`_resize_orb`, clamped 168-320px),
  and every caption is elided to the width it is GIVEN. The suite renders the page at 1040,
  1440 and 1920 and asserts nothing overflows and the orb stays inside its bounds.
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
