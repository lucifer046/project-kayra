# Update V11 — fix round: scrolling, search, WhatsApp names, file names

| | |
|---|---|
| **Version** | V11 |
| **Based on** | V10, commit `10d56ae` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Bug fixes, tuning |
| **Scope** | Scrolling inside web apps; one search action and "close the search"; WhatsApp chats by a loose name from anywhere; "the recent chat"; Workspace file names said the short way; Chrome extension 1.11.0 |
| **Not in scope** | Reading a chat from a screenshot (the page text is faster and exact — see §3); learning aliases (H2); a live test on logged-in Gmail / WhatsApp / Sheets |
| **Models** | none added. Every fix is rules plus a script in the page |

## 1. Summary

Reported after using Parts 4 and 5. Each sentence was run through the parser to find where it
went wrong before anything was changed.

| Said | Before (V10) | After (V11) |
|---|---|---|
| "scroll the mail", "scroll the mail to bottom" | not understood; "scroll down" said "Done" and nothing moved | the open mail scrolls; "At the bottom." / "It's already at the bottom." |
| "open graphics team group in whatsapp" | tried to launch an app with that name | opens WhatsApp and "Graphics Team (2026-27)" |
| "open graphics team group" | expanded a Chrome tab group (or "I don't see that group") | the tab group when one has that name, else the WhatsApp group |
| two groups "Graphics Team (2026-27)" and "(2025-26)" | asked which | the one with the latest message |
| "read the recent chat" | looked for a chat called "recent" | the open chat, else the newest; only unread messages when there are any |
| "click on the search button and search for X" | one click on a button called "search button and search for X" | finds the search box, clicks, clears, types X, submits |
| "close the search bar" | tried to close an app called "search bar" | search emptied and closed; WhatsApp back on the chat list, the open chat untouched |
| "open council details 2026-27 sheet" (title "… \| Sundarbans House") | worked only when the year was heard as "2026-27" | also "2026 2027", "2026 to 27"; a failed Google search is retried without numbers, then in recent files |
| "open council details sheet" with two such files | the shorter title | the one opened most recently (asks when they are different files, not different years) |

## 2. Changes by area

- **Extension 1.10.0 → 1.11.0** (`extension/kayra-bridge/background.js`)
  - `pageScroll(direction, until, where, amount)` — the scrollable ancestor of what is in the
    middle of the view, else the largest scrollable box, else the window; returns `moved`,
    `atEnd`, `atStart`, `canScroll`. *Why:* Gmail, WhatsApp and Docs never scroll the window.
  - `pageSearch(text, submit)` — the search box by type / role / `role=search` / name; presses a
    "Search" button first when the box is hidden; refuses password and payment fields.
  - `pageSearchClose()` — empties the box, presses its Cancel / Clear, sends Escape, and on
    WhatsApp gives the focus back to the open chat.
  - `pageChatList` rows carry `pinned`; `pageType` with empty text + clear empties a
    contenteditable box.
- **Parser** (`automation/common.py`) — `scroll_request`, `search_request`,
  `_SEARCH_CLOSE_RE`, `_bare_fix`, `_named_chat`; "… in whatsapp" and the scroll / search shapes
  are rules-first (no classifier call).
- **Handlers** (`automation/web_macros.py`) — `page_scroll` (says what happened), `search_here`
  (one action; the Sheets/Docs home list uses Google's search), `search_close`, `group_open`,
  `wa_open` / `wa_read` with `recent` and unread-only, `_find_files`, `rank_titles`.
- **Matching** (`automation/semantic_match.py`) — `year_ranges`, `edition`.
- **`linux._page_scroll`** — another app in front is scrolled with real keys, not Chrome.

## 2a. Second pass, from the audit log (extension 1.11.0)

`logs/automation.log`, 2026-11-11 01:00–01:05, after the first pass was tried live:

| In the log | Cause | Fix |
|---|---|---|
| `message.open 'general panchayat'` NOT_FOUND in 31 ms, again 0.6 s later, then SUCCESS 20 s later | Kayra typed into WhatsApp's search and read the list at once — before WhatsApp had filtered it | `chat.search` types and waits in the page until the list has changed and is still |
| every failed command appears twice | the understanding layer proposed the same command and it was run again | an identical proposal is not run |
| the search box stayed filled | closing depended on a script the page may ignore; nothing checked | closed after every lookup; `search_close` reads the box back, uses the real keys if text is left, and says so if it still cannot |
| `message.open 'first search result not the second one'` | no way to pick a row | "open the first one / second chat" = that row of the list as shown |
| `browser.group_close 'current' / 'the' / 'whatsapp'` → "You don't have any tab groups" | "close the group" was only ever a tab group | with WhatsApp in front it closes the open chat |

Also new: "search for X" in WhatsApp (results said, search left open for "open the first
one"), "search X and open it", a second search by the name's words when a word was misheard,
and a new search always replaces the old text.

## 2b. Third pass, from the audit log (01:19–01:23)

| In the log | Cause | Fix |
|---|---|---|
| `app.close 'whatsapp'` → "WhatsApp isn't open", then `message.close` SUCCESS (the chat was closed) | the web app runs in Chrome's "Profile 1", so its window class is not the one in its desktop entry; the fallback model then guessed "close the chat" | a web app's window is recognised by its site in any profile; with no window its tab is closed; a failed "close X" is never reinterpreted |
| `app.open 'whatsapp'` takes 8.0 s every time | the same class mismatch: Kayra waited for a window it could not recognise | fixed by the same change |
| "read the last three … chats of mota panda" → `message.read 'core team' today` | the classifier echoed its own example | a rule reads the name and the number; exactly that many messages are read out |
| `message.open 'public relation and outreach coding'` NOT_FOUND after 6 s | the name was heard wrong; search results existed but none matched well | the two closest chats from the search are offered |
| `message.send target='open'` | "message open" with no name was parsed as a message to "open" | not parsed any more |

## 3. Screenshot or page text for "read the recent chat"?

Page text. It is already what `chat.messages` reads: exact sender, text and date in about
50 ms, summarized by the LOCAL model only when long. A screenshot needs the vision model
(~2 s, extra VRAM), sees only what is on screen and can misread names. Nothing was added
for the screenshot route.

## 4. Bug fixes found on the way

| Symptom | Fix |
|---|---|
| With WhatsApp in front, "open the budget sheet" opened a chat search | Workspace shapes are checked first |
| "search for X" on the Sheets home list pressed Ctrl+F | Google's own list search (`?q=`) |
| After a failed chat lookup WhatsApp's search could stay filled | `pageType` empties a contenteditable; a search that opened a chat is closed afterwards |

## 5. Dependencies and configuration

None. Extension permissions unchanged. 1.1.0+ extensions reload themselves to 1.11.0.

## 6. Files

Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.10.0),
`src/kayra/automation/{common,web_macros,linux,semantic_match,interpreter}.py`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`,
`CLAUDE.md`, `updates/README.md`. Created: this file.

## 7. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,267 passed, 0 failed.
- `tests/test_browser_control.py`: 733 passed (122 new, section [20]).
- `tests/test_browser_extension.py` (real headless Chrome): 77 passed (20 new, sections [8] and [9]).
- `tests/test_dmm_browser.py`: the 7 new cases pass; 10 older cases fail in this run exactly as they did before this change (11 at the base commit) — they need the local classifier model.

## 8. Not verified

- Live, logged-in Gmail, WhatsApp Web and Google Sheets. The page functions were checked
  against fixtures in real Chrome, not against the real sites' current markup.
- Sheets' grid scrolling relies on the Page Down / Ctrl+End key fallback.

## 9. Upgrade notes

Nothing to do: the extension updates itself on the next connection. If Kayra says the
extension is out of date, reload it once in `chrome://extensions`.
