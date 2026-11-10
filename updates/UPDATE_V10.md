# Update V10 — WhatsApp by voice, the app in front, a semantic name matcher (browser plan Part 5)

| | |
|---|---|
| **Version** | V10 |
| **Based on** | V9 (uncommitted, on top of commit `d1d48af`) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Feature, privacy, bug fixes |
| **Scope** | H0.2 app context, H0.3 `semantic_match.py`, H0.4 WhatsApp core (open, who messaged me, read, send); Chrome extension 1.9.0 |
| **Not in scope** | H1 page agent, H1.1 recipes, H2 learning aliases (Part 7); H3 per-app extras (react, reply-quote, files, calls); a live test on a logged-in WhatsApp |
| **Models** | local only (LM Studio `google/gemma-4-e4b`, `text-embedding-nomic-embed-text-v1.5`) |

## 1. Summary

The bug report this section was written for: with WhatsApp open, "open XYZ chat" answered
*"I couldn't find an app called XYZ chat"*, and "click on XYZ chat" did nothing.

| Said (WhatsApp in front) | Before (V9) | After (V10) |
|---|---|---|
| "open XYZ chat", "click on design team 2026" | an app launch / a page click on the words | the chat whose name best matches |
| "open PR and outreach core team" | — | "PR and Outreach Core Team 2026-27" (the user's example) |
| "open the PR group" with two PR chats | — | "Do you mean PR Outreach 2025 or PR and Outreach Core Team 2026-27?" |
| "who messaged me" | — | "Unread on WhatsApp: PR and Outreach Core Team 2026-27 (5), Rahul Verma (2)…" |
| "what did they say in the core team today", "read it" | — | read out, or summarized by the LOCAL model |
| "tell Rahul I'm running late" | asked "you want me to send rahul?", then searched and typed | opens Rahul's chat, asks "Send 'I'm running late' to Rahul Sharma?", types, reads back, sends, checks it arrived |
| "open youtube" with WhatsApp in front | YouTube | YouTube (a known site stays itself; "open youtube chat" is a chat) |

## 2. Changes by area

- **H0.3 `automation/semantic_match.py`** — `match()` returns pick / ask / none from: order-free
  word overlap with stems, prefixes and near spellings ("rahool" → Rahul); spoken numbers;
  initials ("ptc" → Pune Tech Club); taught aliases; LOCAL embeddings; recency (ties → the most
  recent chat, the user's rule) and usage. Measured: a 70/30 blend with nomic embeddings turned
  the honest question for "PR group" into a confident wrong pick, so meaning only breaks
  near-ties and, on its own, can only lead to a question. Embeddings are cached in memory, never
  written, never from a cloud model.
- **H0.2 app context** — `common.front_site()`; WhatsApp-in-front shapes in
  `common.direct_tokens`; `linux.situation()` remembers the app the user was in for 2 minutes
  while Kayra's own window is in front (`_LAST_FRONT`, `FRONT_MEMORY_S`). No classifier prompt
  change (it would break the prompt cache; the rules-first route covers it).
- **H0.4 WhatsApp** — extension 1.9.0 `pageChatList` / `pageChatOpen` / `pageChatMessages` /
  `pageChatComposer` (roles, `span[title]`, aria-labels, `data-pre-plain-text`; WhatsApp's own
  row order = recency); `web_macros.wa_open`, `wa_unread`, `wa_read`, `message_send`, `_wa_find`
  (visible list → WhatsApp's search → match), `ai.summarize(local_only=True)`. Parsing:
  `message open | read | who messaged me`, and "tell / text / message X …" no longer addresses
  someone called "tell". Classifier preamble and the understanding layer's catalog and worked
  examples now use `message open <chat>`.
- **Policy** — `message.send` left the static CONFIRM set: the handler opens the chat first and
  then always asks, naming the chat it resolved (one question, about a chat, not about words).
  On yes it re-checks that chat is still open; if the user switched chats, nothing is sent.

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| "open XYZ chat" in WhatsApp → app launcher | "no app called XYZ chat" | app context (H0.2) |
| A failed WhatsApp search left its text in the search box | every later chat list (who messaged me, the next open) saw only that search's results | the box is emptied when nothing opens (found by the new tests) |
| "message tell rahul saying hi" | addressed to a contact called "tell" | leading "tell / text / message" stripped |
| WhatsApp summaries could fall back to a cloud model | chat text sent off the machine when no local model ran | `local_only=True`; without a local model the latest 3 messages are read instead |

## 4. Dependencies and configuration

No new packages or `.env` keys. Chrome extension **1.9.0** (no new permission). The embedding
model is optional: without it the matcher is lexical only.

## 5. Files

Created: `src/kayra/automation/semantic_match.py`, this file. Modified: `automation/common.py`,
`automation/web_macros.py`, `automation/ai.py`, `automation/linux.py`, `automation/policy.py`,
`automation/interpreter.py`, `intelligence/llm_engine.py`, `extension/kayra-bridge/background.js`,
`extension/kayra-bridge/manifest.json`, `tests/test_browser_control.py`,
`tests/test_browser_extension.py`, `tests/test_assistant_automation.py`, `tests/test_dmm_browser.py`,
docs. Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **5,144 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_control.py` [19]: the matcher table (the user's example, exact names,
  misspellings, ties by recency, initials, ambiguity asks, meaning-only never picks), app context
  and its 2-minute memory, open / ambiguous / found through search / not found, the search box
  emptied, who messaged me, read today, long chats summarized, PRIVACY (no cloud summary), send
  asks naming the real chat, nothing typed before yes, yes sends and verifies, no sends nothing,
  a switched chat sends nothing, an ambiguous chat asks which, not logged in, old extension.
- `tests/test_browser_extension.py` (real headless Chrome): **57 passed**, new [7] on a
  WhatsApp-shaped page.
- `tests/test_dmm_browser.py` (local model): **72/72**, 55 rules-first.
- `tests/test_dmm_matrix.py` (local model): 52/53.

## 7. Known limitations / not verified

- **Not tried on a live, logged-in WhatsApp.** Linking needs the user's phone, and Claude in
  Chrome was not connected. The page functions follow WhatsApp Web's accessible structure (chat
  list grid rows, `span[title]`, unread aria-labels, `data-pre-plain-text`, the `Type a message`
  box) with older class names as fallback; they are the first thing to tune in a live run.
- "mom" → "Maa" needs a taught alias (H2, Part 7); meaning alone will only ask.
- A true tie ("rahul" with Rahul Sharma and Rahul Verma) picks the most recent chat, as asked;
  for sending, the question names the chat, so a "no" catches a wrong pick.
- Sending relies on WhatsApp's Send button, falling back to Enter in the message box.

## 8. Upgrade notes

1. Restart Kayra; the extension reloads itself to 1.9.0.
2. Optional: keep `text-embedding-nomic-embed-text-v1.5` loaded in LM Studio for meaning-aware
   matching.
3. Try, with WhatsApp open: "who messaged me", "open the PR group", "tell Rahul I'm on my way".
