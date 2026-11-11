# Update V17 — smooth scrolling, browser neighbours, around the call (Parts 11–13)

| | |
|---|---|
| **Version** | V17 |
| **Based on** | V16, commit `e9628b1` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, UI, privacy |
| **Scope** | Part 11 = §11 S1 + S3 (smooth scrolling, measuring); Part 12 = §13 N1 (browser neighbours); Part 13 = §12 M4.2–M4.5 (around the call); Chrome extension 1.16.0 |
| **Not in scope** | §11 S2 (GPU tile layer for scrolling), §13 N2–N5, M3 microphone / speaker switching |
| **Models** | none added; meeting summaries use the local model only |

## 1. Summary

| Said / done | Before (V16) | After (V17) |
|---|---|---|
| A mouse-wheel notch in any page | one 60 px jump in one frame | a ~170 ms ease-out glide; fast notches add up into one glide |
| A touchpad flick | jumped with the events, stopped dead | follows the fingers 1:1, then coasts and slows down |
| PageDown / Home / End | jumped | glide |
| "save these tabs as research" / "open my research tabs" | not understood | the window's tabs saved by name and reopened in a new window |
| "read this later" / "open the next one on my reading list" | not understood | Kayra's own reading list |
| "pop this video out" | not understood | picture-in-picture |
| "screenshot this page" / "take a full page screenshot" | a screenshot of the whole desktop | just the page — or the whole page, stitched — saved and copied |
| "tidy my tabs"; 18 tabs with 6 duplicates | not understood; nothing | how many and what to say; once every two hours, unprompted |
| "open this in Chromium" / "open this page in my Work profile" | not understood | there |
| "mute me", "share my screen", "leave" on Zoom / Teams / Discord in Chrome | "You're not in a Google Meet call." | each site's own buttons |
| "present my launch deck in the meeting" | not understood | the deck is opened, shared (that asks) and started |
| "take notes of this meeting" … "summarize this meeting" | not understood | captions kept, saved to Reports/meetings, summarized by your local model with the action items |
| "record this meeting" | not understood | asks, then records the screen and the call's sound |

## 2. Part 11 — smooth scrolling (§11 S1, S3)

`src/kayra/ui/smooth_scroll.py`, installed on every scroll area (`View` base, Chat transcript; a
test fails for any new `QScrollArea` without it). Ease-out glide toward an accumulating target;
touchpad 1:1 with momentum after a real flick (two samples or more above 120 px/s); keys that reach
the area; a timer at the screen's refresh rate; instant under reduced motion; anything else moving
the bar (Chat following a reply, a drag) stops the glide and is adopted.

Measured offscreen at 1280×800, 2.25×: a notch moves in ~20 steps of at most 21 px (was one jump).
Offscreen frames cost ~4 ms, which does NOT reproduce the real window (the GPU backdrop is off
offscreen; §11's 27–33 ms was measured in the live window). With `KAYRA_UI_PROFILE=1` the live
window now logs "scroll" frames — that is where the laptop's real number comes from. The motion is
continuous now; the frame cost is unchanged until S2 (GPU tiles).

## 3. Part 12 — browser neighbours (§13 N1)

- Sessions and the reading list: titles + addresses in Kayra's private store (`data/`, 0600). The
  reading list is Kayra's own: Chrome's `readingList` API would need a new extension permission.
- Extension: `windows.openUrls`; `tabs.capture` (the visible part, or the whole page as slices —
  the ~2-per-second capture limit is paced inside the extension, Kayra never sleeps); `pageVideo`
  `pipArm` / `pipState` / `pipExit` (picture-in-picture behind the same trusted-Space hook as uploads).
- Full pages are stitched with Pillow when present (it is, as a dependency of another package);
  without it the visible part is saved and that is said.
- The tidy suggestion runs in the existing watcher thread, checks tabs at most every 10 minutes,
  speaks at most every two hours, only at 15+ tabs with 3+ duplicates (`TAB_TIDY_SUGGESTIONS`).
- "Open this page in my Work profile" goes through Part 5P's profile resolution.

## 4. Part 13 — around the call (§12 M4)

- M4.5: one table of button names per call site (Zoom, Teams, Discord), Meet's as the fallback;
  the share hook content script now also runs there (the extension already had `<all_urls>`).
  Spoken results follow the control ("You're muted."), not one site's label.
- M4.2: "present … in the meeting" runs as a plan: open the deck, share this tab (the share
  question is asked as always; "no" stops the rest), start the slideshow.
- M4.3: `automation/meeting_notes.py` — captions read every 3 s on one thread that exists only
  while notes are taken; growing captions merged; saved 0600 to `Reports/meetings` on "stop taking
  notes", on leaving the call and at shutdown; summary from the LOCAL model only (without one, the
  last lines are read).
- M4.4: "record this meeting" is CONFIRM ("Tell the others you're recording."); stopping is not.

## 5. Files

Created: `src/kayra/ui/smooth_scroll.py`, `src/kayra/automation/meeting_notes.py`,
`tests/test_smooth_scroll.py`, this file.
Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.16.0, call-site matches),
`meet_main.js` (comment), `src/kayra/ui/views/{base,chat}.py`,
`src/kayra/automation/{common,linux,web_macros,workflows,policy}.py`, `tests/run_all.py`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`.env.example` (`TAB_TIDY_SUGGESTIONS`), `docs/reference/UI.md`,
`docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`,
`CLAUDE.md`, `AGENTS.md`, `updates/README.md`.

## 6. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,867 passed, 0 failed (V16: 5,789).
- `tests/test_smooth_scroll.py` (new): 22 — glides land exactly, accumulate, never pass an end,
  ease out; keys; touchpad 1:1, flick coasting, no coast on a nudge; programmatic moves win;
  reduced motion; every scroll area covered.
- `tests/test_ui.py`: 701, unchanged.
- `tests/test_browser_control.py`: 1,211 ([25]).
- `tests/test_browser_extension.py` (real headless Chrome): 158 ([14]: Meet captions as lines, the
  PiP hook ignores a scripted Space and fires once on a real one).
- `tests/test_dmm_browser.py` on the local model: 138/138, 116 rules-first. One new negative case
  ("what's a reading list in chrome") was read by the model as a browser command and was dropped;
  in the app that guessed click goes to the understanding layer.
- Privacy guard passed; pyflakes clean on every changed file.

## 7. Not verified

- Scrolling has not been felt or profiled in the live window on the laptop; run with
  `KAYRA_UI_PROFILE=1` to see the frame times.
- `tabs.capture` and `windows.openUrls` use Chrome tab APIs the headless page harness cannot host;
  they are covered by the fake extension only.
- No live Zoom / Teams / Discord call, live Meet captions or real recording was tried; the button
  names are the sites' 2026 accessible names.

## 8. Upgrade notes

Nothing to do; the extension updates itself to 1.16.0. `TAB_TIDY_SUGGESTIONS=off` silences the tab
suggestion. Meeting notes are in `Reports/meetings`.
