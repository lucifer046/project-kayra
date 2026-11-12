# Update V7 — About the page, bookmarks, history, downloads (browser plan Part 3)

| | |
|---|---|
| **Version** | V7 |
| **Based on** | commit `3efbacd` (V6) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Feature, one bug fix |
| **Scope** | C5 questions about a page, C6 comments/reviews and YouTube summaries, C8 translation and the selection, C9 bookmarks, C10 history, C11 downloads, G1 "it", G2 search where you are, G4 "what can I do here". Chrome extension 1.6.0 with the `downloads` permission |
| **Not in scope** | Part 4 (Google Workspace, needs decision D3); G5 multi-step; the 2M live-call test |

## 1. Summary

| Said | Before (V6) | After (V7) |
|---|---|---|
| "what does this page say about pricing" | a click on that sentence | answered from the page's own text |
| "read the comments", "summarize the reviews" | a click | the top comments, or a summary of them |
| "summarize this video" | a summary of the YouTube page chrome | the video's transcript, summarized (the description when there is none) |
| "translate this page to Hindi", "explain the selected text" | a click | Google Translate in this tab; the model explains / translates the selection |
| "bookmark this page" | tried to OPEN a bookmark called "this page" | bookmarks it (in a named folder if said) |
| "delete the bookmark for X", "clear today's history" | not available | asks first, naming exactly what goes |
| "what did I download", "open my last download", "pause the download" | the app launcher | the download list with progress; opening asks and never runs a program |
| "close it" / "go back to it" after "switch to the GitHub tab" | a click on "close it" | that GitHub tab |
| "search this sheet for Ravi" | a click | the sheet's own Find; on Gmail, YouTube, Drive, GitHub, shops… their own search |
| "what can I do here?" | a click | a few things worth saying on that site |

## 2. Changes by area

- **Parsing** (`automation/common.py`: `part_3_action`, `LANGUAGES`, `_BARE_PAGE`): the new
  shapes, checked before Part 2c's and the older rules; a few page phrasings are also taken
  without the `browser` prefix.
- **Macros** (`automation/web_macros.py`): `page_ask`, `page_comments`, `video_summary`,
  `page_translate`, `selection_explain`, `selection_translate`, `bookmark_add`,
  `bookmarks_search`, `bookmark_delete`, `history_clear`, `history_list`, `downloads_list`,
  `download_open`, `download_show`, `download_control`, `search_here`, `page_help`, and
  `LAST_TAB` / `remember_tab` / `_referent_tab` for "it". `page_read` / `page_summarize` take
  "it". Every 1.6.0 command checks the extension version first.
- **Extension 1.6.0**: `page.comments` (`pageComments`), `page.transcript`
  (`pageTranscript`), `page.selection`, `bookmarks.add/remove/find`, `history.deleteRange`,
  `downloads.list/show/pause/resume/cancel`; `downloads` permission (approved, D5).
- **DMM preamble / catalog**: every shape; two contrasts the live local model needed ("a
  question about the page is not a word find", "downloads are not closed tabs").

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| "browser bookmark this page" parsed as `bookmark_open "this page"` | opened a search for a bookmark instead of adding one | `bookmark_add` is matched first |

## 4. Dependencies and configuration

- No new packages or `.env` keys. Chrome extension **1.6.0**, new permission `downloads`.
  `downloads.open` is deliberately not requested: Kayra opens a finished file with `xdg-open`
  after a yes.

## 5. Files

Created: this file. Modified: `automation/common.py`, `automation/web_macros.py`,
`automation/linux.py`, `automation/interpreter.py`, `intelligence/llm_engine.py`,
`extension/kayra-bridge/background.js`, `extension/kayra-bridge/manifest.json`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`,
`tests/test_assistant_automation.py`, docs (`BROWSER_CONTROL_PLAN.md`, `CHROME_BRIDGE.md`,
`LINUX_AUTOMATION.md`, `CLAUDE.md`, `updates/README.md`). Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **4,974 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_control.py`: 437 checks, new section [16]: parsing of every shape; the
  page question goes to the model with the page text; comments read and summarized; translate
  URL and unknown language; bookmark folder; delete / clear history / open download each ask
  and act on exactly what was named; history from midnight only; a `.sh` download is refused
  even after a yes; download progress and pause; the YouTube transcript is polled (not slept)
  and the description fallback; site searches on Gmail, GitHub repo, Amazon, Drive, YouTube;
  Ctrl+F then typing in a Sheet; a generic search box; "it" remembered by name only, expiring
  after 10 minutes; an extension older than 1.6.0 is never sent a 1.6.0 command.
- `tests/test_browser_extension.py` (real headless Chrome): 39 passed, new section [4].
- Live local DMM (gemma in LM Studio): 12 phrasings; 2 misreads fixed in the preamble and
  re-checked; `tests/test_dmm_matrix.py` 51/53 (baseline 50–51).

## 7. Known limitations / not verified

- **Not run against the live Chrome with the extension connected.**
- Bare "close it" is read as chat by the DMM, which cannot see that Kayra just opened a tab;
  "browser close it" (or the understanding layer) works.
- Comment and transcript selectors follow each site's current markup; YouTube's transcript
  button label is matched in English.
- History can be cleared only for today, the last hour or 15 minutes — never all time.

## 8. Upgrade notes

1. Restart Kayra; the extension reloads itself to 1.6.0. Chrome may note the new downloads
   permission once.
2. Try: "summarize this video", "what does this page say about refunds", "bookmark this page".
