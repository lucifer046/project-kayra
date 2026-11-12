# Update V20 — Fixes from the audit-log review (Part 17, L1–L8)

| | |
|---|---|
| **Version** | V20 |
| **Based on** | V19 (uncommitted), on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Fix |
| **Scope** | BROWSER_CONTROL_PLAN §15 L1–L8 |
| **Not in scope** | L9 (Drive folder, Google Chat, email delete "yes", slow email open) |
| **Models** | none; no extension change (1.16.0) |

## 1. Summary

| Said / happened | Before (V19) | After (V20) |
|---|---|---|
| "read my latest email" with Chrome closed | "Chrome isn't connected… say browser set up" | Chrome is started, the extension waited for, the email read |
| "send it" / "press the send button" after typing in WhatsApp | looked for a chat called "(contact)" or "sand button", twice | 'Send "…" to Rahul Sharma?' → yes → sent (re-checked) |
| "open PR chat" when WhatsApp's search box isn't ready | "I don't see a chat called pr" in 50 ms, then the same run again | search closed and retried in the same request; honest message if it still can't search |
| "read core team" | "Outreach team 2026 or Namita Graphics Team?" | no chat offered on "team" alone |
| "P.R." / "p r" | no match | the PR chat |
| model token "open megha sharma chat and read the last three messages" | one chat with that whole name | open the chat, then read the last 3 |
| "close Google Drive" | "I couldn't find google drive open" | the Drive tab is closed |
| "close the WhatsApp group" (no tab groups) | "You don't have any tab groups" | the WhatsApp tab is closed |
| "open council details 2026-27" with Sheets results on screen | Docs searched 3×, 24 s, user's results lost, wrong file opened by a follow-up | the list on screen is read first; ≤ 2 loads in a new tab; "202627" = 2026-27 |
| "open Google Sheets and search council details" | the search ran 0.4 s after the open, on the old page | the search waits for Sheets to load |

## 2. Changes by file

- `automation/web_macros.py`: `_wake_chrome` (L1); `message_send_draft`, `is_placeholder`,
  placeholder handling in `message_send` (L2); search retry + honest failure in
  `_wa_find_named`, cleaned search text (L3/L4); `site_tabs`, `close_site_tabs`, `group_close`
  fallback (L6); `_find_files` rewrite, `_file_list_tab`, `_split_year_run`, `_singular`,
  `_NAME_STOP` + google/sheets/docs/drive (L7); `wait_page` (L8).
- `automation/common.py`: `_SEND_DRAFT` + routing (L2); repeat-miss guard and batch
  de-duplication in `AutomationRuntime` (L3); `split_compound`, `_WA_LAST_HERE` without a noun
  (L5); `_opens_page` includes `app.open` (L7); `_settle_page` (L8).
- `automation/semantic_match.py`: `GENERIC`, `_join_letters` (L4).
- `automation/interpreter.py`: `_PLACEHOLDER` filter; `send_draft` is user-only (L2).
- `automation/linux.py`: `message.send_draft` dispatch; `app.close` falls back to the site's tabs (L6).

## 3. Tests

`tests/test_browser_control.py` section [30] (+48 checks); two V9/V11 checks rewritten for the
new search rule (two loads, Drive second). Unit tier 6012 checks, 0 failed;
`tests/test_browser_extension.py` 158/0.

## 4. Known limits

- Not tried live. The WhatsApp search retry is from the log's timing; whether its first failure
  was a search left open or a slow box is not recorded (set `KAYRA_LOG_FILE=True`).
- Chrome is started with the default profile; P4 (several profiles) is still open.
- Within one request a NOT_FOUND is not re-run for 3 s; a later request always runs.
