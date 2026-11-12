# Update V5 — Meetings and screen sharing (browser plan Part 2M)

| | |
|---|---|
| **Version** | V5 |
| **Based on** | V4 (uncommitted, on top of commit `283611b`) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Bug fixes, feature, test hygiene |
| **Scope** | Google Meet screen sharing: parsing, the DMM, confirmations, the share flow, a Hyprland portal picker helper, Do Not Disturb while presenting, the automation audit log in tests. Chrome extension 1.4.0 |
| **Not in scope** | Trying it in a live call (M0.7); a sensitive-window warning (M2.2); in-call controls (M3); calendar and recording around calls (M4); browser Part 2c |

## 1. Summary

The user said "start screen sharing of this meet and share the LM Studio window" in a live
Google Meet call and nothing useful happened. The audit log showed four separate misroutes.
All four are fixed, and Kayra can now share the exact window, monitor or tab that was named.

| Said | Before (V4) | After (V5) |
|---|---|---|
| "start sharing" | `meet.join abc-defg-hij`: joined the preamble's EXAMPLE meeting code | one `meet.share`; a code the user never said is dropped |
| "share screen" | `mouse.click_text "share screen"`: Chrome's picker opened, nobody chose | `meet.share monitor` |
| "share the LM Studio window" | `window.focus lm studio` (Meet left the front) + whole MONITOR shared, behind a "share monitor?" question | one share of THAT window, asked as "Share the lm studio window with everyone in the call?" |
| "meet start screen sharing" | `meet.join new`: started a new meeting | `meet.share monitor` |
| "share my second screen", "share with sound", "share LM Studio instead", "am I sharing?" | not available | available |
| Waiting for the pickers | four fixed pauses (5 s) | acts on what appears; success read from Meet's "Stop presenting" |
| Notifications during a share | could pop up on the shared screen | Do Not Disturb until you stop (mako) |
| Unit tier | appended `AUTOMATION_*` lines to `logs/automation.log` every run | writes a temp file; the real log is untouched |

## 2. Changes by area

**Parsing** (`automation/common.py`). `share_request()` reads every phrasing into
`{surface: monitor|window|browser, name, audio, instead}`. `normalize_command` catches a bare
share token ("share screen", "click on share screen", "present now", "share the X window")
before the click and `screen` prefixes — only when a screen surface or presenting is named, so
"click on share" on a video page is still a click. `merge_share_steps` folds `focus X` +
`meet share window` into one share of X and keeps a single share per utterance.
`meet am i sharing` → `meet.share_status`.

**The DMM** (`intelligence/llm_engine.py`). `_guard_meeting_code` drops a `meet` token whose
meeting code is not in the user's words (spoken "abc defg hij" counts), and re-reads `meet
join/start` as the user's own words when they asked to share and did not ask for a meeting. The
preamble now says sharing is always ONE `meet share` token with examples for each surface.
The understanding layer's catalog and a worked example carry the same shapes.

**The share flow** (`automation/web_macros.py`). `meet_share(surface, name, audio, instead)`
resolves the named window (desktop entry, then title) or monitor (left to right; the focused
one by default), stops a different share in progress, writes the portal request when the helper
is installed, sets the page hook, clicks Meet's button, and `_drive_pickers` answers what
appears: Enter on a new Chrome picker window (twice at most), Enter on the portal picker only
for an unnamed whole screen without the helper. A named window or tab is never auto-picked
without the helper. `meet_stop_share`, `meet_share_status`; leaving the call ends the share
state.

**The portal picker helper** (`extension/kayra-share-picker/kayra-share-picker`,
`automation/share_picker.py`). xdg-desktop-portal-hyprland runs a configurable picker program
and reads `[SELECTION]/window:<handle>` or `[SELECTION]/screen:<name>` from it. Kayra's helper
answers a one-shot request (`$XDG_RUNTIME_DIR/kayra/share-request.json`, 0600, 30 s, deleted
when read) by matching the window's Hyprland address in the portal's
`XDPH_WINDOW_SHARING_LIST` (then class + title, then a unique class or title; never a guess),
and otherwise runs the picker configured before it. `share_picker install|uninstall|status`
edits `~/.config/hypr/xdph.conf` (backup kept, old picker recorded, restored exactly);
`setup.py` offers it on Hyprland.

**Confirmations** (`automation/common.py`). The question names what goes out. "yes, don't ask
again" approves and trusts `meet.share` — the only action that can be trusted by voice — until
`meet leave`, three hours at most.

**Extension 1.4.0** (`extension/kayra-bridge/meet_main.js`). `window.__kayraShareAudio` adds
`audio` and `systemAudio: "include"` to the next `getDisplayMedia` call. `REQUIRED_VERSION` is
1.4.0, so 1.1.0+ extensions reload themselves.

**Audit log in tests** (`automation/policy.py`, `tests/run_all.py`, `tests/_harness.py`).
`KAYRA_AUDIT_LOG` = a path or `off`.

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| The DMM emitted the preamble's example code `abc-defg-hij` | Kayra opened a stranger's meeting | `_guard_meeting_code` |
| "share screen" read as `click share screen` | Chrome's picker opened, nothing chosen | bare share tokens are caught first |
| "share the X window" split into focus + share | Meet lost focus; the whole monitor was shared | `merge_share_steps`; window shares by name |
| "meet start …" always meant a new meeting | "start screen sharing" started a meeting | share is checked before join |
| Generic confirmation "you want me to share monitor?" | unclear what would be shared | names the window / screen / tab and the audience |
| Fixed 1.0–1.5 s pauses in `meet_share` | slow, and wrong on a slow machine | readiness loop |
| Unit tier wrote `logs/automation.log` | the real audit trail filled with test lines | `KAYRA_AUDIT_LOG` |

## 4. Dependencies and configuration

- No new Python packages. The helper uses the system `python3` (stdlib only).
- New `.env`: `MEET_SHARE_DND` (on). New environment: `KAYRA_AUDIT_LOG`.
- Chrome extension **1.4.0**.
- Optional, asked by `setup.py`: one line in `~/.config/hypr/xdph.conf`.

## 5. Files

Created: `extension/kayra-share-picker/kayra-share-picker`, `src/kayra/automation/share_picker.py`,
this file. Modified: `automation/common.py`, `automation/web_macros.py`, `automation/linux.py`,
`automation/policy.py`, `automation/interpreter.py`, `intelligence/llm_engine.py`,
`extension/kayra-bridge/meet_main.js`, `extension/kayra-bridge/manifest.json`, `setup.py`,
`.env.example`, `tests/test_browser_control.py`, `tests/test_assistant_automation.py`,
`tests/run_all.py`, `tests/_harness.py`, docs (`BROWSER_CONTROL_PLAN.md`, `CHROME_BRIDGE.md`,
`LINUX_AUTOMATION.md`, `CLAUDE.md`, `tests/TESTING.md`). Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **4,842 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
  `logs/automation.log` had 14,258 lines before and after the run.
- `tests/test_browser_control.py`: 305 checks, including the new [12] sharing (parsing of every
  phrasing and the four misroutes, the DMM guard, confirmation wording, trust and its end, the
  flow with and without the helper, monitors, tabs, audio, switching, DND), [13] the helper run
  as a subprocess the way the portal runs it, and install/uninstall on a temporary config,
  [14] the audit log.
- `tests/test_browser_extension.py` (real headless Chrome): 27 passed.
- `tests/test_dmm_matrix.py` against the local model: 50/53, the documented baseline.
- Live local DMM (gemma in LM Studio) on nine share phrasings, including the original
  sentence: every one produced the right `meet` token.

## 7. Known limitations / not verified

- **Not tried in a live Meet call** (M0.7). In particular: the order in which Chrome on Wayland
  shows its own picker and the portal's, and whether its picker is a separate Hyprland window
  (if not, Kayra presses Enter once after a short wait, as before).
- The window-list format is read from xdph 1.4.1 (`<handle>[HC>]<class>[HT>]<title>[HE>]<address>[HA>]`);
  the address field is matched first, class and title after. The helper is not installed on
  this machine yet — it changes `xdph.conf`, so it is the user's call (`python setup.py`).
- A specific Chrome TAB cannot be selected programmatically; Kayra opens the tab list and asks.
- M2.2 (warn about a sensitive window on a shared screen), M2.3, M3 and M4 are still open.

## 8. Upgrade notes

1. `python setup.py` and answer yes to "Set it up now?" for sharing by name (Hyprland).
2. The extension reloads itself to 1.4.0 when Kayra connects.
3. In a call: "share the LM Studio window", "yes" (or "yes, don't ask again"), "stop sharing".
