# Update V16 — WhatsApp extras, in-call controls, one inbox across apps (Parts 8–10)

| | |
|---|---|
| **Version** | V16 |
| **Based on** | V15, commit `79cd0b5` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, safety, privacy |
| **Scope** | Part 8 = H3 (WhatsApp), Part 9 = §12 M2.2, M2.3, M3, M4.1 (meetings), Part 10 = H4 (one inbox); Chrome extension 1.15.0 |
| **Not in scope** | H3 site helpers for Instagram / Telegram / Discord / Slack / LinkedIn (the page agent covers them); H4 one request to several apps and reply reminders; M3 switching microphone / speaker; M4.2–M4.5 |
| **Models** | none; nothing in these parts reaches a model |

## 1. Summary

| Said | Before (V15) | After (V16) |
|---|---|---|
| "mute the PR group for a week", "pin Mom", "archive this chat", "mark Rahul as read" | not understood | done through WhatsApp's own menu; "Mom is already pinned." when it is |
| "star the last message" | not understood | the newest message from them is starred and said back |
| "react with a heart to the last message", "reply to the last message saying …", "forward the last message to Rahul", "video call Mom on WhatsApp" | not understood | **asks** ("React ❤️ to Rahul's "are you coming tonight?" in Rahul Sharma?"); on yes the chat and the message are checked again |
| "block Rahul", "delete this chat", "leave the group" | not understood | refused: "That one is yours to do in WhatsApp." |
| "who's in the call", "send a heart", "pin Ana in the call" | not understood | Meet does it |
| "send in the meeting chat: running late", "send the meeting link to Rahul" | not understood | **asks**, then sends |
| "copy the meeting link" | not understood | the link, without `?authuser`, on the clipboard |
| "share my screen" with WhatsApp open on it | "Share your entire screen …?" | "… WhatsApp and Bitwarden are open on that screen. …" |
| Kayra wants to say something while you share | it could speak on the shared screen's call | it waits until the share ends |
| Calendar's reminder notification | nothing | "Standup is starting. Say "join my next meeting"." |
| "who messaged me anywhere" | not understood | "WhatsApp: PR group (5), Rahul Verma (2). Gmail: 3 unread. Telegram: Ana Rao." |
| "reply to Rahul saying I'm on my way" | the classifier guessed | in the app his last message came from (WhatsApp asks; email is written, not sent; other apps said honestly) |
| a Telegram message arrives | nothing | "Ana Rao messaged on Telegram." — never the text; "stop telling me about new messages" |

## 2. Changes by area

- **Extension 1.14.0 → 1.15.0** (no new permission): `pageWa` (WhatsApp's chat-row, message and
  header menus by name; a dialog's option and button; reactions; the forward picker and its Send;
  call buttons) and `pageMeet` (people, reactions, pin, the in-call chat).
- **`automation/web_macros.py`**: `wa_manage`, `wa_message` (late confirmations re-checked on yes),
  `sensitive_on_screen`, `share_warning`, `meet_people`, `meet_react`, `meet_pin`, `meet_chat`,
  `meet_link`, `open_tab_on`, `whatsapp_unread`, `gmail_unread`, `reply_by_email`.
- **`automation/inbox.py`** (new): `inbox_all`, `last_source`, `reply_anywhere`, `on_notification`,
  `set_message_offers`.
- **`automation/linux_system.py`**: the notification log calls a listener for each new notification.
- **`automation/workflows.py`**: `in_call()` is also true while Kayra's screen share is on.
- **`automation/common.py`**: `message_8_action`, `part_9_10_action`; `SHARE_WARNING` hook in the
  share question; "meet open the chat" opens the chat (it used to join a new meeting); a WhatsApp
  "reply to X saying …" with WhatsApp in front is rules-first.

## 3. Safety and privacy

| Concern | What happens |
|---|---|
| Things other people see | reaction, quote-reply, forward, call, the meeting chat, sending the link: always a yes first, naming the chat / message; checked again on yes |
| Irreversible chat actions | block, report, clear, delete, leave: refused by voice |
| Reading an inbox | never opens WhatsApp or Gmail to look; reads only what is already open plus the notification log (RAM only) |
| Spoken offers | sender and app only, never the text; 10 minutes per sender, six an hour; held during a call, a share, a turn or standby |
| Sharing a screen | private apps on it are named in the question |

## 4. Bugs found on the way

| Symptom | Fix |
|---|---|
| "message reply to Rahul on whatsapp saying hi" searched for a chat called "Rahul on whatsapp" | the app name is dropped from the name |
| "meet open the chat" joined a new meeting | opens the meeting chat |
| (found by the new tests) an already-muted chat was muted "again" through the header menu | "already muted" is read from the row menu before any fallback |

## 5. Files

Created: `src/kayra/automation/inbox.py`, this file.
Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.15.0),
`src/kayra/automation/{common,linux,linux_system,workflows,web_macros,interpreter}.py`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`.env.example` (`INBOX_ANNOUNCE`, `MEETING_REMINDERS`), `docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`,
`docs/roadmap/BROWSER_CONTROL_PLAN.md`, `CLAUDE.md`, `AGENTS.md`, `updates/README.md`.

## 6. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,789 passed, 0 failed (V15: 5,716).
- `tests/test_browser_control.py`: 1,155 passed ([24]: every phrasing, housekeeping, every
  confirmation and its re-check, refusals, in-call controls, the share warning on a two-monitor
  fake, quiet while sharing, the inbox, reply routing, offers and their limits).
- `tests/test_browser_extension.py` (real headless Chrome): 154 passed ([13]: WhatsApp row menu +
  mute dialog, message menu, reactions, the forward picker, call; Meet people, reactions, pin, chat).
- `tests/test_dmm_browser.py` on the local model: 129/129, 107 rules-first.
- pyflakes clean on every changed file.

## 7. Not verified

- None of it has been tried on the live WhatsApp Web or a live Meet call; the menu labels and
  Meet's reaction and pin names are their 2026 accessible names and may change.
- Which notification apps name their sender in the summary varies; Telegram, Signal and Slack do,
  Chrome web notifications mostly do.

## 8. Upgrade notes

Nothing to do; the extension updates itself to 1.15.0. `INBOX_ANNOUNCE=off` / `MEETING_REMINDERS=off`
in `.env` silence the offers.
