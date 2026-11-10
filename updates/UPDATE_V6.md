# Update V6 — Tabs and windows, finished (browser plan Part 2c)

| | |
|---|---|
| **Version** | V6 |
| **Based on** | commit `5d1d8a5` (V4 + V5) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Feature |
| **Scope** | B2 bulk close by site / all but pinned, B4 tab groups, B5 windows, C3 "open the third result", C4 find with highlight. Chrome extension 1.5.0 with the `tabGroups` permission |
| **Not in scope** | Part 3 (C5, C6, C8–C11, G1, G2, G4); the 2M live-call test |

## 1. Summary

| Said | Before (V5) | After (V6) |
|---|---|---|
| "close all YouTube tabs" (7 open) | closed all 7 at once | "That's 7 youtube tabs, like … Close all of them?"; "yes" closes exactly those 7 |
| "close all but pinned" | not available | every unpinned tab in the window; more than 3 asks first |
| "group my college tabs", "make the research group blue", "collapse it", "rename this group to thesis", "ungroup", "close the research group" | not available | Chrome tab groups by voice |
| "move this tab to a new window / the other screen", "merge my Chrome windows" | not available | done (Hyprland moves the new window to the other monitor) |
| "open the third result", "play the second video" | "I couldn't find an app called the third result" | opens that result on Google, YouTube, Amazon, Flipkart, GitHub, Reddit, DuckDuckGo, Bing, or the Nth content link elsewhere |
| "find pricing on this page" | a count only | every match highlighted, the first scrolled to; "next match", "previous match", "clear highlights" |

## 2. Changes by area

- **Runtime** (`automation/common.py`): a handler may now find that a request is bigger than it
  sounded and return `NEEDS_CONFIRMATION` with `detail["confirm"]`, the exact action to run on
  "yes". `execute_action` registers it like a policy confirmation. Nothing has run at that point.
- **Parsing** (`common._part_2c_action`, `result_request`): the new shapes, checked before the
  older tab rules; a bare "open the third result" is caught before `app.open`.
- **Macros** (`automation/web_macros.py`): `_matching_tabs`, `_close_many` (CONFIRM_ABOVE = 3),
  `tabs_close_unpinned`, `tabs_group`, `groups_list`, `group_add`, `group_update`,
  `group_ungroup`, `group_close`, `tab_detach`, `windows_merge`, `page_result`, `page_results`,
  `page_find` (highlights from 1.5.0), `page_find_step`, `page_find_clear`. Each checks the
  extension is 1.5.0+ first.
- **Extension 1.5.0** (`extension/kayra-bridge/background.js`, `manifest.json`):
  `groups.list/create/update/ungroup`, `windows.detach/merge/count`, `page.results`
  (`pageResults`), `page.highlight` (`pageHighlight`); `tabInfo` carries `groupId` and `index`;
  `tabGroups` permission (approved by the user, plan decision D5).
- **DMM preamble and the understanding catalog** carry every new shape.

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| "open the third result" fell through to the app launcher | "I couldn't find an app called the third result" | result requests are matched first |
| "close all X tabs" closed any number without asking | one sentence could close a dozen tabs | more than 3 asks first, with the exact tab list |

## 4. Dependencies and configuration

- No new packages or `.env` keys. Chrome extension **1.5.0**, new permission `tabGroups`
  (Chrome shows no prompt for it; 1.1.0+ extensions reload themselves).

## 5. Files

Created: this file. Modified: `automation/common.py`, `automation/web_macros.py`,
`automation/linux.py`, `automation/interpreter.py`, `intelligence/llm_engine.py`,
`extension/kayra-bridge/background.js`, `extension/kayra-bridge/manifest.json`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, docs
(`BROWSER_CONTROL_PLAN.md`, `CHROME_BRIDGE.md`, `LINUX_AUTOMATION.md`, `updates/README.md`).
Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **4,904 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_control.py`: 367 checks, new section [15].
- `tests/test_browser_extension.py` (real headless Chrome): 36 passed, new section [3] — the
  result list skips nav/footer links and duplicates; highlight marks 3 of 3 case-insensitively
  and never inside a textarea; next/previous wrap; clear restores the text exactly.
- Live local DMM (gemma in LM Studio): 11 of 11 new phrasings produced the right token.
  `tests/test_dmm_matrix.py`: 50/53, the documented baseline.

## 7. Known limitations / not verified

- **Not run against the live Chrome with the extension connected** (as for Parts 2a/2b).
- Site result selectors follow each site's current markup; when a site changes it, the generic
  "Nth content link" fallback takes over, which may count differently. Each site's selector
  lives in one place (`pageResults`).
- A tab group lives in one window: tabs matching a topic across windows are grouped in the
  window holding most of them.
- Merging windows never mixes incognito and normal windows.

## 8. Upgrade notes

1. Restart Kayra; the extension reloads itself to 1.5.0 when it connects.
2. Try: "group my YouTube tabs", "make the YouTube group red", "open the second result".
