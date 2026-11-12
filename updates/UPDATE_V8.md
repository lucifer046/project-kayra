# Update V8 — Google Workspace by voice (browser plan Part 4)

| | |
|---|---|
| **Version** | V8 |
| **Based on** | commit `d1d48af` (V7) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Feature, two bug fixes found while testing |
| **Scope** | E1 Sheets, E2 Docs, E3 Slides, E4 Drive, E5 Gmail, E6 Calendar, E7 Meet, E8 Keep / Forms / Classroom. Chrome extension 1.7.0 (no new permission). Decision D3 approved: file names read in Chrome, matched locally |
| **Not in scope** | Sheets add-row / sort / chart; Gmail mark-all-read, attachments; Calendar "move my 3 pm to 4"; Meet noise cancellation / layout / recording; Drive share; Part 5 |

## 1. Summary

| Said | Before (V7) | After (V8) |
|---|---|---|
| "open my budget sheet" | "I couldn't find an app called my budget sheet" | searches Sheets, opens the best title match, asks when two are equally good |
| "create a new sheet called Expenses 2026" | not available | a new Sheet, named |
| "what's the total of column C", "average of the amount column", "read row 5" | not available | computed by Kayra from the sheet's own values |
| "go to cell B12" | not available | the Name Box, B12, Enter |
| "summarize this document", "find deadline in this doc" | "not enough text on this page" (Docs draws on a canvas); find highlighted nothing | read from the Doc's text export; Find uses the editor's own Ctrl+F |
| "share this doc with ana@x.com" | not available | Share opened with the address typed; you press Send |
| "start the slideshow", "next slide", "start presenting" with Slides in front | "next slide" went to chat; "start presenting" asked to share your screen | the slideshow, step by step |
| "find the PDF Ravi sent me", "open my recent files", "download this file" | not available | Drive search / recent / download (xlsx, docx, pptx) |
| "search my mail for the fee receipt", "unread from this week", "compose a new email", "open my Work label" | partly (draft only) | Gmail search, unread, compose, labels |
| "what's my next meeting", "join my next meeting", "what's on Friday" | today / tomorrow only | the next event; joins through its own Meet link; any weekday |
| "let them in" (in a call) | not available | Meet's Admit all |
| "add a note saying buy milk", "open the responses of my feedback form", "what assignments are due" | not available | Keep, Forms, Classroom |

## 2. Changes by area

- **Parsing** (`automation/common.py`): `part_4_action` (browser), `_bare_workspace` (the same
  without the prefix — Google file kinds only, so "open the report file" stays a local file),
  Gmail / Calendar / Meet additions in `_web_action`. `AutomationRuntime` turns a bare "start
  presenting" into the slideshow when a Slides tab is in front, before the share confirmation.
- **Macros** (`automation/web_macros.py`): `rank_titles` (local name matching), `ws_open`,
  `ws_search`, `ws_create`, `sheet_rows` / `sheet_calc` / `sheet_row` / `sheet_cell`, `doc_text`
  (used by read / summarize / ask), `ws_share`, `slides_present` / `slides_nav`, `ws_recent`,
  `ws_download`, `keep_note`, `form_responses`, `classroom_due`, `email_search` /
  `email_compose` / `email_label`, `calendar_next`, `meet_admit`; `calendar_agenda` takes a
  weekday; `page_find` uses Ctrl+F inside the Google editors.
- **DMM** (`intelligence/llm_engine.py`): Workspace examples in the preamble; "next slide" /
  "previous slide" on the exact-match fast path.
- **Extension 1.7.0**: `page.docList` (`pageDocList`), `page.fetchText` (`pageFetchText`,
  docs.google.com only), `page.focus`, `calendar.next` (`pageCalendarNext`).

## 3. Bugs fixed (found by the new tests)

| Bug | Effect | Fix |
|---|---|---|
| `ws_download` unpacked four values from a three-value helper | "download this file" always failed | correct unpacking |
| `pageCalendarNext` accepted a hidden Meet link | "join my next meeting" could join another event's call (Calendar keeps other events' details hidden in the page) | visible links only, tested in real Chrome |

## 4. Privacy (decision D3)

File names are read from the page while acting, ranked by `rank_titles` on this computer,
never stored and never sent to any model (a test asserts no model call during matching). A
Sheet's values are added up by Kayra itself. A Doc's text goes to a model only when you ask
about it ("summarize this document"), the same as any page. `page.fetchText` is the
extension's only `fetch`, runs inside the Google page with your own session, and refuses any
address outside docs.google.com.

## 5. Files

Created: this file. Modified: `automation/common.py`, `automation/web_macros.py`,
`automation/linux.py`, `automation/interpreter.py`, `intelligence/llm_engine.py`,
`extension/kayra-bridge/background.js`, `extension/kayra-bridge/manifest.json`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`,
`tests/test_assistant_automation.py`, docs (`BROWSER_CONTROL_PLAN.md`, `CHROME_BRIDGE.md`,
`LINUX_AUTOMATION.md`, `CLAUDE.md`, `updates/README.md`). Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **5,051 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_control.py`: 514 checks, new section [17].
- `tests/test_browser_extension.py` (real headless Chrome): 43 passed, new section [5] (file
  list, dedupe, hidden rows skipped, Drive open link; Calendar next event, hidden vs visible
  Meet link).
- Live local DMM (gemma in LM Studio): 20 Workspace phrasings, 19 right; the 20th ("next
  slide") is now on the fast path. `tests/test_dmm_matrix.py` 50/53 (baseline).

## 7. Known limitations / not verified

- **Not run against the live Google apps with the extension connected.** The Docs/Sheets home
  list markup and Calendar's schedule view are the most likely to need tuning.
- Sheets values come from Sheets' own CSV of the open tab; merged header rows or a header that
  isn't in row 1 count differently.
- `share this doc` types the address but cannot see into the Share dialog (it is a separate
  frame); you check and press Send.
- Not done: see the header's "Not in scope".

## 8. Upgrade notes

1. Restart Kayra; the extension reloads itself to 1.7.0.
2. Try: "open my budget sheet", "what's the total of column C", "summarize this document",
   "join my next meeting".
