# Update V13 — ask ChatGPT / Claude / Gemini, WhatsApp files and replies, Reddit / LinkedIn / GitHub, several steps at once (Part 6b)

| | |
|---|---|
| **Version** | V13 |
| **Based on** | V12, commit `e18f0a4` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, safety |
| **Scope** | BROWSER_CONTROL_PLAN Part 6, slice 6b: F4 (AI sites), F5 (WhatsApp reply to the open chat, send a file), F6 (Reddit / LinkedIn / GitHub), G5 (several steps in one sentence); Chrome extension 1.13.0 |
| **Not in scope** | Part 5P (Chrome profiles), Part 7 (page agent, recipes, learning), H3 per-app extras (reactions, forwarding, Instagram) |
| **Models** | none added; the DMM prompt is unchanged (see §4) |

## 1. Summary

| Said | Before (V12) | After (V13) |
|---|---|---|
| "ask ChatGPT what a monad is" | read as a click on the words, or answered by Kayra's own model | ChatGPT's tab (reused when open), the question typed as said, the answer waited for and read: "ChatGPT says: …" |
| "ask Claude in a new chat …", "what does Gemini think of X" | not understood | a new conversation / the question to Gemini |
| "read the whole answer" | not understood | the full latest answer on the AI tab |
| "reply saying I'll be there" (WhatsApp in front) | sent "I'll be there" to a contact called "reply" | asks "Send "I'll be there" to Rahul Sharma?" (the OPEN chat), then sends |
| "send my resume to Ravi on WhatsApp" | not understood | asks "Send Ana-Resume-2026.pdf (the newest of 2) to Ravi Sharma?", then attaches it through WhatsApp's Attach menu and presses Send only when the preview shows that file |
| "send the last screenshot here" | not understood | the newest screenshot, as a photo, to the open chat — after a yes |
| "read my LinkedIn notifications", "any new GitHub notifications" | opened nothing / an app search | opens the page and reads the latest five |
| "what's the top post on r/python this week", "what's trending on Reddit" | not understood | "The top post on r/python this week: … (4100 points, 380 comments)" |
| "open r/python", "open the github repo pallets/flask", "search LinkedIn jobs for data analyst" | app lookups | the subreddit, the repository (or GitHub's repository search), LinkedIn's job search |
| "open the budget sheet and total column C" | one command "open the budget sheet and total column c" | two steps in order; the total is read from the sheet that was just opened |
| "tick I agree and then submit this form" | the submit question dropped the rest | the question holds the rest; yes runs it, no drops it and says so |

## 2. Changes by area

- **Extension 1.12.0 → 1.13.0** (`extension/kayra-bridge/background.js`, no new permission)
  - `pageAiChat(op, args)` — ChatGPT, Claude, Gemini (and a generic chat page): the prompt box by
    site selector then by name ("prompt", "message"); answers by role (`data-message-author-role`,
    `data-is-streaming`, `model-response`); busy = a visible Stop button or a streaming answer.
    `ask` types with `insertText`, waits for the send button to turn on (never presses a disabled
    one), falls back to Enter, and confirms the box emptied. `wait` waits INSIDE the page for a
    new answer that has stopped growing (one call, up to 25 s). Code blocks become "[code on
    screen]".
  - `pageChatAttach(op, args)` — WhatsApp: `arm` opens Attach when its inputs are not there,
    waits for them, picks Photos & videos or Document, installs the one-shot trusted-Space hook
    (as `page.form file`); `state` reads the preview (a Send outside the composer + file names);
    `send` presses the preview's Send and waits for it to close.
  - `pageSocial(kind, max)` — Reddit posts from `shreddit-post` attributes (promoted skipped),
    GitHub notification rows, LinkedIn notification / message / feed cards (promoted skipped),
    a generic `<main>` article / list fallback.
- **Kayra side** (`automation/web_macros.py`): `ai_ask`, `ai_read`, `spoken_answer`,
  `wa_send_file`, `find_send_file` (+ the newest screenshot), `open_chat_name`, `message_send`
  to "current", `social`, `subreddit_url`; `ws_open` / `ws_search` wait for the page.
- **Parser** (`automation/common.py`): `part_6b_action`, `message_6b_action`, `part_6b_direct`,
  `_bare_part_6b`; G5: `_split_steps`, `_predicted`, `_direct_one`, "total column C", "the sheet
  Ravi sent last week"; batches park the rest behind a yes (`pending_plan`) and stop a step that
  needed a page that failed to open (`_opens_page`, `_needs_page`); Amazon "… and sort by price"
  no longer leaves "and" in the query.
- **Site table** (`automation/sites.py`): `linkedin jobs` (with a search template),
  `linkedin notifications`, `linkedin messages`.
- **Safety** (`automation/interpreter.py`, `automation/workflows.py`): `from_model_ok` — neither
  the understanding layer nor the planner may propose `ai_ask` or `message.send_file` (nor the
  Part 6 user-only actions); the planner now applies the same rule as the understanding layer.

## 3. Privacy and safety

| Concern | What happens |
|---|---|
| What reaches ChatGPT / Claude / Gemini | only the words the user said after "ask <site>", routed by the rules; a model never forwards a request there |
| Reading the answer | read from the page as written; never reworded by another model |
| Sending a message or a file | always a yes first, naming the RESOLVED chat (and the file); on yes the open chat is checked again; the preview must show that file |
| Social lists | read from the page and spoken; not logged, not sent to a model |

## 4. Tried and reverted

A short "AI SITES AND SOCIAL" paragraph in the DMM preamble: with it the local model read "mute
me" as `system mute my mic` (109/110, twice); without it 110/110. Every 6b phrasing in the
suite is rules-first, so the paragraph bought nothing. The prompt is byte-for-byte V12's.

## 5. Bug fixes found on the way

| Symptom | Fix |
|---|---|
| "message reply saying ok" sent "ok" to a contact called "reply" | replies go to the open chat |
| "search amazon for earbuds and sort by price low to high" searched for "earbuds and" | "and" before the sort clause is dropped |
| A batch step that asked a question dropped every later step | the rest is parked; yes runs it |
| `linux.py` had an f-string without placeholders (pyflakes) | plain string |

## 6. Dependencies and configuration

None. Extension permissions unchanged; 1.1.0+ extensions reload themselves to 1.13.0.

## 7. Files

Created: this file.
Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.13.0),
`src/kayra/automation/{common,web_macros,linux,interpreter,workflows,sites}.py`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`,
`CLAUDE.md`, `AGENTS.md`, `updates/README.md`.

## 8. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,559 passed, 0 failed (V12: 5,452).
- `tests/test_browser_control.py`: 1,025 passed (107 new, section [22]).
- `tests/test_browser_extension.py` (real headless Chrome): 131 passed (17 new, section [11]) —
  a streaming ChatGPT-shaped answer waited for until it stopped, a disabled send button waited
  for, a signed-out page, Claude's streaming flag, WhatsApp's Attach menu opening late, a real
  Space opening the chooser, the preview's Send (not the composer's), Reddit / GitHub /
  LinkedIn lists.
- `tests/test_dmm_browser.py` on the local model (LM Studio, gemma-4-e4b): 110/110, 91 rules-first.
- pyflakes clean on every changed file.

## 9. Not verified

- No question was sent to the live ChatGPT, Claude or Gemini; their prompt box / answer / Stop
  markup is what those sites used in 2026 and may change (each has a by-name fallback).
- No file was sent in a real WhatsApp chat, and the real portal file chooser was not driven.
- No live LinkedIn, Reddit or GitHub page was read (LinkedIn's classes change often; the
  generic fallback reads the cards' text).

## 10. Upgrade notes

Nothing to do: the extension updates itself on the next connection. "Ask ChatGPT …" needs you
to be signed in to that site in Chrome.
