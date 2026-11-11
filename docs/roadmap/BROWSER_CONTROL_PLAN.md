# Browser Control — Implementation Plan (round 5)

> **Status: IN PROGRESS — Parts 1, 1b, 2 (2a, 2b, 2c), 2M, 3, 4, 5, 5P, 6 (6a + 6b), 7 (7a/7b/7c), 8, 9, 10 and the V9 / V11 fix rounds done (2026-11-11).**
> See §10 for the parts and what is next (left: H2 phrasings + tone, H3 other apps, H4 multi-app sends, M4.2–M4.5, 5P's P4; 2M and WhatsApp still need live tries). Written 2026-11-10 at the user's request; the user asked for it to be built one part
> per session because of usage quota.
> Implement ONLY what the user asks for (all, a phase, or numbered items). Tick items off in
> the checklist at the bottom and add a changelog entry to
> `docs/platforms/LINUX_AUTOMATION.md` + `docs/platforms/CHROME_BRIDGE.md` as you go.

## 0. Read this first

**What the user wants, in their words:** "proper browser control — literally each and
everything a browser control can give me — done with my natural tone and natural voice."
Examples they gave: *"open new tab and open Google Sheet in that tab"*, *"I need to check the
mails I got"*, *"I need to send an email"* (→ compose box), *"I need to check Google Sheets"*
(→ open Sheets), *"search this sheet"*, *"open this document"*, *"I need this type of
document"*, *"search for this document"*.

**Standing instruction from the user (applies to every round):** do not wait to be told about
each feature. When building something, also add the obvious neighbouring features a user would
expect, and list them in the final reply so the user can veto any.

**Before coding, read:** `AGENTS.md`, `CLAUDE.md` (sections "Cross-platform", round 2–4
bullets), `docs/platforms/LINUX_AUTOMATION.md`, `docs/platforms/CHROME_BRIDGE.md`.

## 1. The bug that started this (fix first, it is item A1–A3)

"open new tab and open Google Sheet in that tab" → *"I couldn't find an app called google
sheet."* Root causes, verified by running `normalize_command` / `website_url`:

1. `targets.WEBSITE_REGISTRY` has only **23** sites — no Sheets, Slides, Forms, Keep, Notion,
   Canva, Figma, etc.
2. Aliases are exact: `"google docs"` works, `"google doc"` (singular) does not.
3. `normalize_command("open X")` becomes `browser.open_url` only when X is in the registry;
   otherwise `app.open`. In `linux._dispatch` the `app.open` NOT_FOUND fallback tries a path,
   then `website_url()` — the same tiny registry — and then gives up. **There is no
   "if there is no app, open the website / search the web" fallback.**
4. "…in that tab": `linux.open_url` always uses `xdg-open` (new tab); there is no
   "open in the current tab" path, and "new tab" + "open X" run as two unrelated tokens.

## 2. Architecture you must keep (do not re-invent)

```
DMM token ─▶ automation/common.py  normalize_command / _web_action / _extended_action ─▶ Action
         ─▶ automation/policy.py   classify_action (ALLOW / CONFIRM / DENY)
         ─▶ automation/linux.py    _dispatch ─▶ _dispatch_extended
         ─▶ automation/web_macros.py  (Kayra side: one function per user intent)
         ─▶ automation/browser_bridge.py  ws://127.0.0.1:17893/kayra  (Origin-checked)
         ─▶ extension/kayra-bridge/background.js  run(cmd, args) + page functions
```

Rules (each has bitten before):
- **Models never decide whether something runs.** The DMM classifies; `ai.summarize` only
  rewords; `ai.see` only looks. Plans are validated by `normalize_command` + `classify_action`.
- **New extension command ⇒ bump `extension/kayra-bridge/manifest.json` version AND
  `web_macros.REQUIRED_VERSION`.** An older extension answers `unknown command`; `_call`
  turns that into the spoken "reload the extension" instruction. Since 1.1.0 the extension
  has `runtime.reload` — use it (`browser update`) so the user never has to open
  chrome://extensions again.
- **Page functions are serialised alone** (`chrome.scripting.executeScript({func})`): every
  helper must be defined INSIDE the function. Test each one in
  `tests/test_browser_extension.py` against a page shaped like the real site.
- **Match by what a screen reader announces** (aria-label, tooltip, title, text), never by
  CSS class names — except where a site's structure is the only handle (Gmail rows `tr.zA`,
  Sheets' Name Box); then isolate it in ONE site function with a fallback and a test fixture.
- **DMM tokens are split on commas.** No command may need a comma inside it.
- **Every new `system …` needle goes BEFORE the power needles** in `_SYSTEM_VERBS`.
- **Chrome in front + bridge connected ⇒ clicks/scrolls go into the page**
  (`linux._browser_in_front`). Keep it.
- **Spoken results are one or two plain sentences** and say what actually happened
  ("Opened Budget 2026, the newest of 3 matches"), never "Done." for a navigation.
- **Wait for readiness, not fixed sleeps:** `web_macros._wait_for` (the extension's
  `page.waitFor`) or `web_macros._until` (try the action, short poll, try again). Never a
  fixed `SLEEP` in front of a page action — a test in `test_browser_control.py` counts them.
- Logs/audit carry counts and ids, never email bodies, sheet contents or document text.

## 3. Safety (unchanged rules, extended)

CONFIRM before: sending, posting, submitting a form, buying/adding payment, deleting, sharing
(Docs/Drive/Sheets), changing account settings, clearing history, closing many tabs, uploading
a file, accepting calendar changes, recording a meeting, subscribing.
NEVER: typing passwords, OTPs, card numbers, CVV, UPI PIN; logging in for the user; completing
a checkout. The extension refuses those fields on its side too (`pageType`) — keep both walls.
Shopping stops at "add to cart" unless the user decides otherwise (open decision D1).

---

## 4. The features

Each item: **what the user says → what happens → where → done when**.

### A. Opening anything, the way you say it  *(Phase 1)*

**A1. App → website → search fallback.**
"open Google Sheets" / "open notion" / "open swiggy".
Order: installed app (desktop entry or Omarchy web app) → known website → a web search for
"<name>" opening the first result (DuckDuckGo `!` redirect is NOT allowed — use the bridge:
open `https://www.google.com/search?q=<name>` and `page.click` the first result, or
`ddgs` from Python to get the top URL, then open it). Never answer "no app found" for
something that is plainly a website.
Where: `linux._dispatch` app.open branch; new `web_macros.open_site(name, where)`.
Done when: "open google sheet", "open google doc", "open notion", "open canva", "open some
site nobody registered" all open a page; an installed app still wins over a website.

**A2. Big website registry (~150), natural names.** Move it to a data module
`automation/sites.py` (one line per site: url, aliases, title fragments, category, "search URL
template" where the site has one). Normalise singular/plural, "the … website/site/app",
"my …". Include all Google (Sheets, Slides, Forms, Keep, Photos, Meet, Calendar, Maps,
Translate, Classroom, Gemini, Drive, Docs, Contacts, News, Play), Microsoft 365 (Outlook,
Word, Excel, PowerPoint, OneDrive, Teams), Notion, Canva, Figma, Miro, Trello, Slack web,
Discord web, ChatGPT, Claude, Perplexity, Gemini, GitHub, GitLab, Stack Overflow, LeetCode,
Kaggle, Colab, HuggingFace, LinkedIn, X, Instagram, Facebook, Reddit, Quora, Medium, YouTube
(Studio, Music), Spotify web, Netflix, Prime Video, Hotstar, Amazon (.in), Flipkart, Myntra,
Swiggy, Zomato, Blinkit, Zepto, BookMyShow, MakeMyTrip, IRCTC, Google Pay web, banks (open
only), Wikipedia, Google Scholar, arXiv, the user's IITM portals (ask the user for URLs —
decision D4), weather, news sites.
Keep `targets.WEBSITE_REGISTRY` as a thin view over it (Windows uses it) and keep
`SITE_HINTS`' shape (pinned by tests).
Done when: a table test proves every alias resolves and singular/plural both work.

**A3. Where to open it.** "in this tab" / "in that tab" / "here" → navigate the active tab;
"in a new tab" (default); "in a new window"; "in incognito"; "next to this tab".
New extension commands: `tabs.open {url, where: current|tab|window|incognito, index}`
(`chrome.windows.create({incognito:true})` needs "Allow in Incognito" — say so if refused).
Parser: strip the location phrase in `_web_action` / app.open into `parameters["where"]`.

**A4. One utterance, one flow.** "open new tab and open Google Sheets in that tab" must not
race: when a `new tab` is immediately followed by an open in "that tab", merge into one
`tabs.open {where:"tab"}`. Do the merge in `plan_actions` (common) or by teaching the DMM
preamble to emit `open google sheets in a new tab`.

**A5. Omarchy web apps first.** If a desktop entry exists for the site
(`omarchy-launch-webapp` entries: WhatsApp, YouTube, Google Maps, Photos, Messages, HEY,
Basecamp, X, Discord, Zoom), "open X" launches that app; "open X in the browser" opens a tab.

**A6. Straight to your own lists.** "open my sheets" → `docs.google.com/spreadsheets/`,
"my docs" → `/document/`, "my slides", "my forms", "my drive", "my YouTube history /
watch later / subscriptions", "my GitHub repos", "my orders" (Amazon/Flipkart).

### B. Tabs and windows  *(Phase 1)*

B1 list / switch / close / duplicate by name **or position** ("the third tab", "last tab",
"the tab on the right"). B2 bulk close: duplicates, "all YouTube tabs", "tabs to the right",
"all but pinned" (CONFIRM when > 3). B3 pin/unpin, mute/unmute, "mute everything except
this". B4 tab groups: create from matching tabs ("group my college tabs"), rename, colour,
collapse, ungroup (`chrome.tabGroups` + `tabs.group`; add "tabGroups" permission). B5 move tab
to new window / merge windows / move to other monitor (Hyprland move of the new window). B6
reopen closed tab/window; "what did I just close?" (`chrome.sessions.getRecentlyClosed`). B7
discard (sleep) tabs: "free up memory in Chrome" (`chrome.tabs.discard` on background tabs);
"which tab is using the most memory" — not available to MV3 without `processes` (dev-channel
only) → answer from `ps` of Chrome renderers + tab titles is NOT reliable; say the honest
limit. B8 "find the tab where I was reading about X" (search `page.text` of every tab, bounded
to 30 tabs, 20 KB each).

### C. Navigating and reading a page  *(Phase 1)*

C1 back, forward, reload, hard reload (`tabs.reload {bypassCache}`), stop, top, bottom, page
up/down. C2 zoom in/out/reset (`chrome.tabs.setZoom`), full screen (window state), print,
**"save this page as PDF"** (`chrome.tabs.printToPDF` is not in MV3 → use the print dialog via
`window.print()` + Enter, or `debugger` permission — do NOT add debugger; say what happens).
C3 "open the link to Pricing", **"open the third result"** — site-aware result lists: Google
(`#search a h3`), YouTube (`ytd-video-renderer a#video-title`), Amazon, Flipkart, GitHub,
Reddit; generic fallback = the Nth visible main-content link. C4 find on page with highlight
+ "next match" (`window.find` or a mark-and-scroll helper). C5 read aloud / summarize / "what
does this page say about X" (exists — add the question form). C6 "read the comments", "top
reviews", **"summarize this video"** (YouTube transcript: click "Show transcript", read the
panel text). C7 copy link / title / selection / "copy this table as CSV" → clipboard
(`linux.clipboard_set`). C8 translate page (open `translate.google.com/translate?u=`), explain
the selected text (`window.getSelection()` → ai.summarize). C9 bookmarks: add (folder), open,
search, delete (CONFIRM). C10 history: search by meaning ("that site yesterday about X" —
exists), "clear today's history" (CONFIRM, `chrome.history.deleteRange`). C11 downloads (add
"downloads" permission — justify in CHROME_BRIDGE.md security section): last download, open
it, show in folder, pause/resume/cancel.

### D. Typing and forms  *(Phase 3)*

D1 type / fill by label (exists) + select dropdown option by text, tick checkbox/radio by
label, set date fields. D2 press keys in page (`page.key` via `wtype` with Chrome focused —
real key events beat synthetic ones). D3 "fill this form with my details" from a saved
profile in `data/profile.json` (decision D2 — fields), preview spoken before filling, never
submits by itself. D4 "submit this" (CONFIRM). D5 "upload my resume here": find the file
(`linux_system.find_files`), then set it on `input[type=file]` — needs the `debugger`
permission or a native file dialog drive; prefer: open the file chooser by clicking the input,
then type the path into the GTK chooser with `wtype` (Ctrl+L, path, Enter). CONFIRM.

### E. Google Workspace by voice  *(Phase 2)*

E1 **Sheets.** "open my budget sheet" (Drive search `https://docs.google.com/spreadsheets/u/0/?q=<name>`
then open the best title match); "create a new sheet called X" (`sheets.new` then rename via
the title box); **"search this sheet for Ravi"** (Ctrl+F in the grid via wtype, type, Enter);
"go to cell B12" (Name Box: `#t-name-box`, type B12, Enter); "what's the total of column C" /
"read row 5" (export the visible sheet: `…/export?format=csv&gid=` fetched IN THE PAGE with
the user's session, parse in Python, answer); "add a row: …" (go to first empty row, type
tab-separated); "sort by column B", "make a chart" (menu clicks by name). Never write formulas
the user did not dictate.
E2 **Docs.** "open the document about the internship" / "search my docs for offer letter"
(`docs.google.com/document/u/0/?q=`); "create a new doc called X" (`docs.new`); "read / summarize
this document" (page text of `.kix-appview-editor` or export `?format=txt` in-page); "find
'deadline' in this doc"; "type this paragraph" (focus editor, wtype); "share it with
ana@x.com" (CONFIRM; Share dialog by names).
E3 **Slides.** open deck by name, "start presenting" (Ctrl+F5 / "Slideshow"), next/previous,
end.
E4 **Drive.** "find the PDF Ravi sent me" (`drive.google.com/drive/search?q=`), open, recent,
share (CONFIRM), download.
E5 **Gmail.** "compose a new email" (opens compose: `?view=cm&fs=1` — exists as draft),
"write to Ana saying …", **"search my mail for the fee receipt"** (`#search/<query>`),
"show unread from this week" (`is:unread newer_than:7d`), "mark all as read" (CONFIRM),
labels, "attach my resume" (D5), send (CONFIRM — exists).
E6 **Calendar.** next meeting, a named day, "move my 3 pm to 4" (CONFIRM), **"join my next
meeting"** (open the event, click its Meet link).
E7 **Meet** extras: admit waiting people, noise cancellation, layout, record (CONFIRM).
E8 **Keep / Forms / Classroom:** add a note, open a form's responses, assignments due.

### F. Big sites  *(Phase 3)*

F1 **YouTube:** play/pause, skip ad ("Skip"), speed ("speed 1.5" via settings menu or
`video.playbackRate`), captions, full screen, ±10/30 s, next, like, subscribe (CONFIRM), watch
later, summarize (C6). F2 search any site with a search template ("search Amazon for
earbuds under 2000", "search GitHub for …"), "open the first result" (C3), "images of".
F3 shopping: search, filter/sort by name, open product, **add to cart — never checkout**
(decision D1). F4 ChatGPT/Claude/Gemini web: "ask ChatGPT …" (type into the prompt box,
submit, wait for the answer to stop growing, read it back). F5 WhatsApp Web: read unread
chats, reply to the open chat, send a file (CONFIRM). F6 LinkedIn/GitHub/Reddit:
notifications, profile/repo, top post.

### G. Making it feel like a real assistant  *(Phase 1, threads through everything)*

G1 **Conversation memory for the web:** remember the last opened tab id / document / sheet /
email in `AutomationContext` (add slots `tab`, `doc`) so "search it", "share it", "close it",
"read it" refer to it. G2 **Context-aware search:** "search for Ravi" searches inside the open
Sheet / Doc / Gmail / YouTube / Drive / the current site's search box; a Google search only
when nothing searchable is in front (decide from the active tab URL). G3 smart waiting
(`page.waitFor`). G4 **"what can I do here?"** — per-site command hints. G5 multi-step ("open
the sheet Ravi sent last week and total column C") through the existing planner with the new
tokens in its CATALOG. G6 honest outcomes (see §2).

---

### H. Messaging web apps, the page agent and learning  *(added 2026-11-10)*

**The bug report that triggered this section.** The user opened WhatsApp (an Omarchy *web
app*: a Chrome `--app` window, Hyprland class `chrome-web.whatsapp.com__-Default`), then said:
1. "open XYZ chat" → *"no app called XYZ chat"* — routed to `app.open`; Kayra keeps no
   "which app am I in" context, and has no "open a chat" intent at all (only `message.send`).
2. "open XYZ chat in WhatsApp" → same.
3. "click on XYZ chat" → nothing. `linux._browser_in_front` only accepts
   `linux_apps.BROWSER_CLASSES`, so a web-app window was treated as non-browser and the click
   went to the OCR/uinput clicker. (The extension DOES run in `--app` windows; their tabs are in
   `chrome.tabs.query({})`.)
4. Even if routed, matching is lexical: "PR and outreach core team" vs "PR and Outreach Core
   Team 2026-27" scores weakly, and WhatsApp's chat list is not made of buttons.

**The user's explicit requirement:** this must NOT be hard-coded string matching; it must
behave like an AI model — understand loose phrasing, pick the nearest group/contact (tie →
most recent message), learn from how the user talks, and reuse what it learned.

#### H0. Foundations (Phase 1 of this section — fixes the report)
- **H0.1 Web-app windows are browser surfaces.** Any client whose class starts with
  `chrome-`/`brave-`/`msedge-` app prefixes, or whose desktop entry Exec is
  `omarchy-launch-webapp`, counts as Chrome for `_browser_in_front`, page clicks, typing and
  scrolling. Map class → site (`chrome-web.whatsapp.com__-Default` → `web.whatsapp.com`).
- **H0.2 App context.** `AutomationContext` gets `app_context` (site/app in front + time). A
  command arriving within ~2 min while a known site is in front is interpreted INSIDE it:
  "open X" / "open X chat" / "click X" / "reply" / "read it" target the site, not an app
  launch. Implement as a pre-normalizer step in `linux._dispatch` (app.open NOT_FOUND +
  site context → site intent) AND as a DMM hint ("the user is in WhatsApp").
- **H0.3 Semantic matcher** (`automation/semantic_match.py`), used for chats, contacts,
  groups, tabs, files, buttons. Score = blend of: token overlap order-free (stemmed), prefix /
  initials ("PR group", "core team"), learned aliases (H2), embedding cosine via the local
  `text-embedding-nomic-embed-text-v1.5` already loaded in LM Studio (`/v1/embeddings`;
  cache vectors), recency (latest message time — tie-breaker the user asked for) and usage
  frequency. Returns ranked candidates + confidence; below a margin → ASK ("PR Outreach Core
  Team or PR Outreach 2025?"). Pure Python, unit-testable with a fake embedder.
- **H0.4 WhatsApp core:** "open <chat>" (search box → ranked results → open best),
  "send '<text>' to <chat>" / "tell <chat> <text>" (type, read back, CONFIRM, Enter),
  "who messaged me" / "read unread chats", "what did they say in <group> today" (read the open
  chat's recent messages → ai.summarize). Extension commands: `chat.list` (visible chats:
  name, last message time, unread count, preview), `chat.open {index}`, `chat.messages
  {limit}`; selectors by role/aria first (`[role=listitem]`, `[role=gridcell]`, span[title]),
  isolated in ONE site function with a fixture in `test_browser_extension.py`.

#### H1. The page agent (Phase 2 — the "AI, not a script" core)
- `automation/page_agent.py`: loop of **observe → decide → act → verify**, max ~8 steps.
  - observe: extension `page.snapshot` → compact list of interactive/landmark elements with
    stable ids, role, accessible name, value, a few lines of visible text per region
    (≤ ~6 KB; redact password/OTP-like strings).
  - decide: the model returns ONE JSON action from a closed set
    `{click id | type id text | press key | scroll dir | wait | done summary | ask question}`.
  - act: execute through the extension; verify with a fresh snapshot.
- **Safety is not delegated:** every proposed action passes `policy` — a click/type whose
  target or text matches the risky set (send/post/delete/pay/submit/share/follow/like)
  becomes a CONFIRM to the user with a spoken description; DENY list unchanged; the agent can
  never type into password/OTP/card fields (extension refuses too).
- Used when no recipe/site function covers the request, and for unknown sites.
- **H1.1 Recipes:** a successful agent run is saved as a parameterised recipe
  (`data/recipes.json`: site, intent signature, steps keyed by accessible names, not ids) and
  replayed next time WITHOUT the model; if a step's target is missing, fall back to the agent
  and overwrite the recipe (self-healing). `recipes list/forget`.

#### H2. Learning from the user (Phase 2)
- **Aliases:** `data/aliases.json` — "PR group" → "PR and Outreach Core Team 2026-27", "mom" →
  "Maa ❤️", per app; written after a confirmed or uncorrected success; corrections ("no, the
  other one") demote. Feeds H0.3.
- **Phrasings:** successful utterance → token pairs stored locally (`data/phrasings.jsonl`,
  capped) and retrieved by embedding similarity as extra DMM hints for the NEXT
  classification. Never edit `dmm_chat_history`; append retrieved hints in a separate
  message so the measured contract is untouched. Re-run `test_dmm_matrix.py` after.
- **Tone:** for "reply politely saying …", drafts use a short style note learned from the
  user's own sent messages that Kayra already typed (never scraped from chats).
- **Transparency:** "what have you learned about me", "forget that", "forget everything about
  WhatsApp". All local, never logged with message content.

#### H3. Per-app features (Phase 3 — mostly the agent + recipes, plus thin site helpers)
- **WhatsApp:** reply / quote-reply to last message, react, search messages, send a file or
  the last screenshot (CONFIRM), mute/pin/archive/mark read, star, forward (CONFIRM), view
  status, start a voice/video call (CONFIRM), per-contact "send without asking" opt-in.
- **Instagram:** DMs read/reply (CONFIRM), open a profile, latest post, stories,
  notifications, like/save; follow/comment/post CONFIRM.
- **Facebook / Messenger:** notifications, messages, group feed summary, events, Marketplace
  search.
- **Telegram / Discord / Slack (web):** open channel, unread, mentions, reply (CONFIRM),
  summarize a channel.
- **LinkedIn / X / Reddit:** messages, notifications, accept requests (CONFIRM), job search,
  feed summary, post (CONFIRM).

#### H4. Cross-app assistant layer (Phase 4)
- Unified inbox: "who messaged me anywhere?" across WhatsApp, Instagram, Gmail, Telegram,
  LinkedIn, Slack, ranked by importance.
- "Reply to Rahul" → the app his last message came from.
- Notification-driven offers from the existing notification log ("Ana messaged on WhatsApp —
  read it?"), respecting calls/standby via `speak_unprompted`.
- One request, several apps ("message the core team and email the same to Prof X"), one
  CONFIRM per send.
- Reminders to reply (`trigger`), drafts in the user's tone (approve before send).

#### H — safety and limits
- Nothing sent/posted/reacted without a yes unless the user opted a contact in; OTPs/codes
  never read aloud; no logins; message text never logged; learned data visible and erasable.
- The agent needs a capable model (see §7a). First run of a new task takes seconds; recipes
  make repeats near-instant. Voice notes / in-call controls are limited by what the page
  exposes.

#### H — tests
- `semantic_match` unit table (the user's exact example: "PR and outreach core team" →
  "PR and Outreach Core Team 2026-27"; ties broken by recency; ambiguity asks).
- Page agent against a FAKE model (scripted JSON actions) + fake extension: completes, stops
  at max steps, every risky step becomes CONFIRM, password fields refused, recipe saved and
  replayed without the model, broken recipe self-heals.
- Extension fixtures for a WhatsApp-shaped chat list / composer.
- Learning: alias written after success, demoted after "no, the other one", forgotten on
  request; nothing written for a failed/declined action.

---

## 5. Parsing and the DMM

- Add the new verbs to `common._web_action` (browser/email) and new kinds (`sheets`, `docs`,
  `slides`, `drive`, `youtube`, `site`) or fold them under `browser …` with sub-parsing —
  prefer **one token family per product** (`sheets …`, `docs …`) for DMM clarity, each added to
  `llm_engine.funcs` and to the preamble section D3 with 2–3 examples. Never edit
  `dmm_chat_history`.
- Natural phrasings to support (examples): "I need to check my mails", "I need to send an
  email", "I need to check Google Sheets", "search this sheet for X", "open this document",
  "I need the document about X", "find my resume in Drive", "play lo-fi on YouTube and make it
  full screen", "close all these YouTube tabs".
- After changes run `tests/test_dmm_matrix.py` against the configured model and add new rows
  for these phrasings.

## 6. Tests (required for every item)

- `tests/test_assistant_automation.py` (or a new `tests/test_browser_control.py` registered in
  `tests/run_all.py`, unit): parsing table for every phrasing; policy (every CONFIRM item
  runs nothing before yes); web_macros against `FakeBridge` (extend it with the new commands).
- `tests/test_browser_extension.py` (integration, real headless Chrome): one fixture page per
  site shape (Sheets name box + grid, Docs editor, Drive list, YouTube player + results,
  Google results, Amazon results, a form with select/checkbox/date/file). Each page function
  proven there.
- Live check on the user's Chrome ONLY with consent, read-only first (page.controls), and say
  that opening an email marks it read.
- Run the whole unit tier (`tests/run_all.py`) and `graphify update .` at the end.

## 7. Open decisions (ask the user when you reach them)

- **D1** Shopping: stop at "add to cart"? — **answered 2026-11-11: search and open ONLY** (no
  add-to-cart, no cart page, no checkout; the policy refuses those clicks).
- **D2** Form profile fields to store in `data/profile.json` — **answered 2026-11-11: name,
  email, phone, postal address, college and roll number, date of birth and gender** — never
  passwords/cards.
- **D3** May Kayra read Drive/Docs **file names** (in Chrome only, never stored) to search them? — **approved 2026-11-10**: matched locally, never sent to a model.
- **D4** URLs of the user's IITM portals for the site registry.
- **D5** Extra permissions: `downloads` (C11) — **approved 2026-11-10**, in 1.6.0; `tabGroups` (B4) — **approved 2026-11-10**, in 1.5.0. `debugger` is NOT to be added.

## 7a. Which local model runs the page agent  *(decided 2026-11-10)*

Reference machine: RTX 4060 Laptop **8 GB VRAM** (~1.5 GB already used by the desktop and
Kayra's TTS), Ryzen 9 8945HS, **14 GB RAM**. LM Studio at the time held only
`google/gemma-4-e4b` (Q4_K_M, vision) and `text-embedding-nomic-embed-text-v1.5`.

**Primary: `Qwen3.5-9B` (Instruct), GGUF Q4_K_M (~5.3 GB), fully on GPU.** Reasons: the most
consistently recommended model for 8 GB cards, stable tool/JSON calling (BFCL-V4 ~66 %), native
vision (one model can serve the page agent, screen understanding and chat), long native
context (262K; run it at 16–32K here). Apache 2.0.
LM Studio settings: GPU offload = all layers, context 16384 (32768 if it still fits), Flash
Attention ON, KV cache quantization Q8_0, **thinking OFF for the page agent** (send `/no_think`
or the chat-template switch — thinking roughly triples latency for a one-action JSON reply);
thinking may stay on for diagnosis summaries.
Set in `.env`: `LOCAL_CHAT_MODEL` / `LOCAL_DECISION_MODEL` / `VISION_LOCAL_MODEL` to its LM
Studio id, and add `PAGE_AGENT_MODEL` (new) defaulting to the same.

**Fallback / fast lane: keep `gemma-4-e4b`** for quick single-step decisions if Qwen is
unloaded, and **`nomic-embed-text`** for the semantic matcher (H0.3).
**Do not** load two 8–9B models at once on this GPU.

**Measured 2026-11-11** (`tests/bench_page_agent.py`: 30 one-step decisions on Gmail / WhatsApp /
Sheets / shop / form / settings / GitHub-shaped pages through the real `pageSnapshot`, prompt
and parser; local only; one model on the GPU at a time — loading both fails on 8 GB):

| Model | Valid JSON | Correct action | Median / worst s per step | Whole runs (`--loops`) |
|---|---|---|---|---|
| Qwen3.5-9B (before prompt tuning) | 28/30 | 21/30 | 2.26 / 3.36 | — |
| gemma-4-e4b (before prompt tuning) | 22/30 | 17/30 | 0.69 / 1.79 | — |
| **Qwen3.5-9B** (tuned, `/no_think`) | 30/30 | **30/30** | 1.40 / 2.38 | 2/2 |
| **gemma-4-e4b** (tuned) | 30/30 | **27/30** | 0.70 / 0.95 | 2/2 |

What the tuning was: rules + four worked examples in the agent prompt ("type, don't click a
text box", "Enter after a search", "answer a question from the page"), a question hint,
"already done — never repeat", the agent telling the model when a click / key changed nothing,
Qwen's `/no_think` (latency halved), and choosing an already-chosen option = done. Both models
clear the 90 % bar. **Default: the decision model (gemma) — no model swap on an 8 GB GPU;**
set `PAGE_AGENT_MODEL=qwen/qwen3.5-9b` for accuracy when LM Studio runs Qwen for everything.
Gemma's three misses: re-typing instead of pressing Enter / Send twice, and one question
answered by acting.

**Required before trusting it (part of H1):** a small benchmark — 30 scripted page-agent
tasks against the fixture pages (WhatsApp/Gmail/Sheets shapes) measuring valid-JSON rate,
correct-action rate and seconds per step, run for Qwen3.5-9B and gemma-4-e4b; record results
here. Public sources compare general tool calling, not browser agents, and one argues 8 GB
is marginal for agent loops — so measure. If correct-action rate is < 90 %, route the agent
to a cloud model (Groq/Gemini) when a key exists and keep local for recipes and matching.

Sources consulted: localllm.in "Best Local LLMs for 8GB VRAM (2026)", vdf.ai "Best Local LLM
for Agents and Tool Calling (2026)", promptquorum.com tool-calling tests, llmconfigurator.com
agents ranking, Qwen3.5-9B listings (Together, DeepInfra, ApX).

## 8. Phases and checklist

| Phase | Items |
|---|---|
| 1 — Open anything, tabs, pages | A1–A6, B1–B8, C1–C11, G1–G4, G6 |
| 2 — Workspace | E1–E8 |
| 3 — Forms and big sites | D1–D5, F1–F6, G5 |
| H1 — Messaging foundations (fixes the WhatsApp report) | H0.1–H0.4 |
| H2 — Page agent, recipes, learning (needs §7a model) | H1, H1.1, H2 |
| H3 — Other messaging/social apps | H3 |
| H4 — Cross-app layer | H4 |

- [x] A1 [x] A2 [x] A3 [x] A4 [x] A5 [x] A6  *(A6: GitHub "my repos" has no user-free URL — not added)*
- [x] B1 [x] B2 [x] B3 [x] B4 [x] B5 [x] B6 [x] B7 [x] B8
- [x] C1 [x] C2 [x] C3 [x] C4 [x] C5 [x] C6 [x] C7 [x] C8 [x] C9 [x] C10 [x] C11
- [x] D1 [x] D2 [x] D3 [x] D4 [x] D5  *(D5 not yet tried against the real portal file chooser)*
- [x] E1 [x] E2 [x] E3 [x] E4 [~] E5 [~] E6 [~] E7 [x] E8  *(E5: no mark-all-read / attach; E6: no "move my 3 pm"; E7: admit only — see §10 Part 4)*
- [x] F1 [x] F2 [~] F3 [x] F4 [x] F5 [x] F6  *(F3: search, sort, filter, open, read — no cart, by decision D1; F4–F6 = slice 6b, not tried on the live sites)*
- [x] G1 [x] G2 [x] G3 [x] G4 [x] G5 [ ] G6  *(G3: `page.waitFor` exists and email check uses it; the other fixed sleeps move over part by part)*
- §12 meetings: [x] M0.1 [x] M0.2 [x] M0.3 [x] M0.4 [x] M0.5 [x] M0.6 [ ] M0.7 (live call) [x] M1.1 [~] M1.2 [x] M1.3 [x] M1.4 [x] M1.5 [x] M2.1 [x] M2.2 [x] M2.3 [~] M3 [~] M4  *(M3: no device switch; M4: only M4.1, by Calendar's own notifications)*  *(M1.2: Chrome's tab list opens, the user picks the tab)*
- [x] H0.1 [x] H0.2 [x] H0.3 [x] H0.4 [x] H1 [x] H1.1 [~] H2 [~] H3 [~] H4 [x] §7a model benchmark  *(H3: WhatsApp done; Instagram / Telegram / Discord / Slack / LinkedIn go through the page agent, no site helpers yet. H4: inbox, reply-where-they-wrote, offers done; one request to several apps and reply reminders not)*  *(H2: aliases + transparency done; learned phrasings and tone not — they would touch the cached classifier prompt)*

## 9. Known state at the time of writing

- Rounds 1–4 were committed in `1261170` (feature/linux-desktop-automation); Part 1 in
  `2a8e51b`; the understanding layer in `677eba3`, made fast in `c0ee678`.
- Extension in the repo is **1.2.1** (Part 1 + the 2026-11-10 tuning pass; see the changelog in
  `docs/platforms/LINUX_AUTOMATION.md`). From 1.1.0 on it updates itself: when Kayra
  needs a newer command it sends `runtime.reload` and says so; only a 1.0.x extension still
  needs the manual reload on chrome://extensions.
- Unit tier: 4529 checks green (`tests/test_browser_control.py`: 130); extension integration
  suite: 27 checks green.
- Local model in LM Studio: `google/gemma-4-e4b` (thinking model) + `nomic-embed-text`. The
  understanding layer calls it with thinking OFF: median 0.51 s, worst 0.78 s, 12/12 correct
  on the live check. §7a's Qwen3.5-9B is still only a recommendation, not installed.
- Not yet verified end to end by voice in the user's real Chrome (the user's Chrome was on
  extension 1.0.0, which needs one manual reload before self-updating works).
- Screen (non-browser) clicking needs `/dev/uinput` and is deliberately deferred by the user.

## 10. Implementation parts (one per session)

The user asked for this to be built in parts so a quota limit never leaves half a feature.
Each part ends green on `tests/run_all.py` + `tests/test_browser_extension.py`, with the
checklist above ticked and a changelog entry in LINUX_AUTOMATION.md / CHROME_BRIDGE.md.

| Part | Items | Status |
|---|---|---|
| 1 | Tuning pass; A1–A6, G3 (`page.waitFor`), H0.1, `browser update` + self-updating extension | **done 2026-11-10** |
| 1b | The understanding layer (`automation/interpreter.py`), requested by the user | **done 2026-11-10** |
| 2 | B1–B8 tabs & windows (by position, bulk close, groups, reopen, discard, find-the-tab), C1–C4, C7 | **done 2026-11-10** — 2c done (B2 by site + all but pinned with CONFIRM > 3, B4 groups, B5 windows, C3 the Nth result, C4 find with highlight; extension 1.5.0 + tabGroups); 2a done (B1 by position, B2 duplicates / right / left); 2b done (B3 mute the rest, B6 recently closed + reopen by name, B7 sleep tabs, B8 find the tab, C1 hard reload, C2 zoom to N %, C7 copy link/title) — 2026-11-10. Next (2c): B2 "all YouTube tabs" + CONFIRM > 3, B4 groups, B5 windows, C3 "open the third result", C4 find with highlight |
| 2M | §12 Meetings and screen sharing. M0 bugs (misroutes, fake meet code, fixed waits, confirm, test log hygiene), M1 share a named window / tab / monitor (xdph picker helper), M2.1 DND | **done 2026-11-10** except M0.7 (try it in a live call — needs the user) and M2.2/M2.3, M3, M4 (next meeting slice, after 2c or when asked) |
| N | §13 neighbouring features (browser N1, models N2, interface N3, voice N4, housekeeping N5) — pick up beside the part each neighbours | |
| 3 | C5, C6, C8–C11 (questions about a page, YouTube transcript, translate, bookmarks, history, downloads — D5 permission), G1, G2, G4 | **done 2026-11-10** (extension 1.6.0 + `downloads`) |
| 4 | E1–E8 Google Workspace (needs D3 answered) | **done 2026-11-10** (D3 approved; extension 1.7.0). Left for later: Sheets add-row/sort/chart, Gmail mark-all-read/attach, Calendar move, Meet layout/recording, Drive share |
| 5 | H0.2–H0.4: app context, `semantic_match.py`, WhatsApp core | **done 2026-11-10** (extension 1.9.0; not yet tried on a live, logged-in WhatsApp) |
| 5F | Fix round V11 after live use of Parts 4–5: scrolling inside web apps, one search action + close search, WhatsApp chats by loose name from anywhere, "recent chat", year ranges and recency for file names | **done 2026-11-11** (extension 1.11.0 after the second pass from the audit log; `updates/UPDATE_V11.md`; not yet re-tried on live Gmail / WhatsApp / Sheets) |
| 5P | **§14 Chrome profiles** — open Chrome as a profile / with an account, discovered from Chrome itself (requested 2026-11-10) | **done 2026-11-11** (P1, P2, P3, P5; `updates/UPDATE_V14.md`). Open: P4 (the extension in several profiles), "always open Chrome as X" (a Settings key), spoken aliases (H2) |
| 6 | D1–D5 forms (needs D2), F1–F6 big sites (needs D1), G5 | **done 2026-11-11** — 6a (D1–D5, F1, F2, F3 within D1; extension 1.12.0; `updates/UPDATE_V12.md`); 6b (F4 ask ChatGPT/Claude/Gemini, F5 WhatsApp reply + send a file, F6 Reddit/LinkedIn/GitHub, G5 several steps; extension 1.13.0; `updates/UPDATE_V13.md`) |
| 8 | H3 WhatsApp extras: mute / pin / archive / read, star, react, quote-reply, forward, calls; refused: block / delete / clear / leave | **done 2026-11-11** (extension 1.15.0; `updates/UPDATE_V16.md`) |
| 9 | §12 M2.2 sensitive windows on a shared screen, M2.3 quiet while sharing, M3 in-call (people, reactions, pin, meeting chat, the link), M4.1 meeting reminders | **done 2026-11-11** (M3 device switch, M4.2–M4.5 open) |
| 10 | H4 one inbox: "who messaged me anywhere", "reply to X" where X last wrote, spoken offers from notifications | **done 2026-11-11** (one request to several apps, reply reminders open) |
| 7 | H1, H1.1, H2 (page agent, recipes, learning; §7a benchmark first), then H3, H4 | **7a/7b/7c done 2026-11-11** (§7a benchmark, H1 page agent, H1.1 recipes, H2 aliases + transparency; extension 1.14.0; `updates/UPDATE_V15.md`). Open: H2 phrasings / tone, H3, H4 |

**Parts 8–10 — WhatsApp extras, the call, one inbox** (done 2026-11-11, extension 1.15.0). Detail:
`updates/UPDATE_V16.md`.
- 8 (H3) `pageWa`: the chat row's / newest message's / header's own menus by name, a dialog's
  choice + button (mute 8 h / 1 week / always), reactions, the forward picker, call buttons.
  `wa_manage` (mute, pin, archive, read / unread — just happen, "already pinned" said),
  `wa_message` (star happens; react, quote-reply, forward, call ASK naming chat + message; on yes
  the chat must still be open and the message still the newest). Block / report / clear / delete
  / leave are refused by voice.
- 9 (§12) `pageMeet`: people, reactions, pin, the meeting chat (asks), copy / send the link (send =
  WhatsApp, asks). M2.2 `sensitive_on_screen` adds "WhatsApp and Bitwarden are open on that
  screen." to a whole-screen share question (`common.SHARE_WARNING`); M2.3 `workflows.in_call` is
  true while Kayra's share is on; M4.1 from Calendar's own desktop notifications.
- 10 (H4) `automation/inbox.py`: `inbox_all` (open WhatsApp, Gmail's tab title, the notification
  log — never opens an app to look), `reply_anywhere` (newest notification naming them, else their
  WhatsApp chat; email = opened + reply written, never sent), `on_notification` (sender + app only,
  10 min per sender, 6 an hour, "stop telling me about new messages", `INBOX_ANNOUNCE`,
  `MEETING_REMINDERS`).
- **Not tried live** (WhatsApp's menu labels and Meet's reaction / pin names are from their 2026
  accessible names; each falls back to "I couldn't find …").

**Part 7 — the page agent, recipes, learning** (done 2026-11-11, extension 1.14.0; 7a §7a + H1,
7b H1.1, 7c H2 aliases). Detail: `updates/UPDATE_V15.md`.
- 7a `page.snapshot` (stable `data-kayra-id`, role, accessible name, value, secret flag, masked
  codes) and `page.act` (click / type / select / check / press / scroll; secret fields refused in
  the page). `automation/page_agent.py`: observe -> decide (local model, one JSON action from a
  closed set) -> act -> verify, ≤ 8 steps, loop and "nothing changed" detection. Clicks judged by
  `policy.classify_action` + the agent's social verbs (CONFIRM, late, naming the control; cart /
  pay DENY); Enter after typing into a message box CONFIRMs; it may TYPE only the user's words.
  Triggers: "on this page, …", "figure out how to …", "browser agent …"; the understanding layer
  may propose it. `PAGE_AGENT=off` disables it.
- 7b recipes: a run whose every step worked is saved (`data/recipes.json`, 0600): host, the
  request as a template with typed words as slots, steps by role + NAME. Replayed with no model;
  a missing control hands over to the model from that step and the recipe is rewritten.
  "list my page shortcuts", "forget the shortcut for …".
- 7c learning (`automation/learning.py`, `data/aliases.json`, 0600): Kayra's "which one?" is
  remembered for 2 min; the answer ("the second one", "the core team one") runs the action and,
  for WhatsApp names, teaches the alias; "no, the other one" opens the runner-up and swaps the
  alias; "what have you learned about me", "forget that", "forget everything about WhatsApp",
  "forget everything you learned" (asks). The model never changes what was learned.
- Not done: H2 learned phrasings as classifier hints and a tone note (both would touch the
  cached DMM prompt), H3, H4. **Not tried on live sites** — whole runs were real Chrome + real
  local model on fixture pages.

**Part 5P — Chrome profiles** (done 2026-11-11; §14 P1, P2, P3, P5). Detail:
`updates/UPDATE_V14.md`.
- P1 `automation/chrome_profiles.py` reads `Local State` (`profile.info_cache`) of Chrome,
  Chromium, Brave or Edge — folder from `platforms.browser_user_data_dirs()` (all three backends)
  — and re-parses only when its mtime / size changes; an unreadable file is said so, plain "open
  Chrome" still works. Nothing about any profile is in code or config.
- P2 `common.profile_action` (shapes only; the parser never reads the file): "open my X Chrome",
  "open Chrome as / with / on X", "open X's Chrome", "open <site> in my X Chrome", "switch to my X
  profile", "which Chrome profiles do I have", "open Chrome as a guest". `resolve()` tiers:
  profile name > email / its prefix > account name > domain (whole or any label) > position >
  loose match; ties ask ONE question naming name + account.
- P3 `<binary> --profile-directory=<folder> [url]` through `linux.RUNNER`, the new window
  focused, "Opened Chrome as Work (account name)".
- P5 `tests/test_chrome_profiles.py` (unit, made-up Local State) and
  `tests/test_profile_privacy_guard.py` (integration: the real profiles' emails, prefixes,
  domains and account names must not be in any tracked file; prints only file + kind). Its
  first run flagged a public portal URL in the site table that shares a hosted domain — web
  addresses are now allowed, everything else still fails.
- **Open**: P4 (one bridge connection per profile; "Chrome is open as X but the extension isn't
  installed there"), "always open Chrome as X" (needs a Settings control), spoken aliases (H2).
  Not tried by voice on the live machine (the parser and launcher were checked with recorded
  argv; the resolver was checked against this machine's real profiles, counts only).

**Part 6b — AI sites, WhatsApp files, Reddit / LinkedIn / GitHub, several steps** (done
2026-11-11, extension 1.13.0, no new permission). Detail: `updates/UPDATE_V13.md` and the
changelog in `docs/platforms/LINUX_AUTOMATION.md`.
- F4 `ai_ask` / `ai_read` (`page.aiChat`: state / ask / wait-in-page / read; the site's tab
  reused; bounded 90 s wait announced once; never routed there by a model).
- F5 `message.send` to the open chat ("reply saying …"), `message.send_file` (`chat.attach`:
  arm with the trusted-Space hook, read the preview, press the preview's Send); late
  confirmation naming file AND chat; never from a model.
- F6 `social` (`page.social`): notifications / messages / top post / subreddit / repo, LinkedIn
  jobs search through the site table.
- G5 `direct_tokens` splits complete halves with the later half judged against the predicted
  screen; batches park the rest behind a yes; a failed open stops the dependent step;
  `ws_open` waits for the page; the planner's catalog grew.
- Tried and dropped: a DMM preamble paragraph for 6b — it made the local model misread "mute
  me"; all 6b phrasings are rules-first instead.
- Tests: `test_browser_control.py` [22], `test_browser_extension.py` [11], `test_dmm_browser.py`
  110/110 on the local model (91 rules-first).
- **Not verified live**: real ChatGPT / Claude / Gemini, a real WhatsApp file send, live
  LinkedIn / Reddit / GitHub pages.

**Part 6a — forms, the video, site search and shopping** (done 2026-11-11, extension 1.12.0;
D1 and D2 answered by the user that day). Detail: `updates/UPDATE_V12.md` and the changelog
in `docs/platforms/LINUX_AUTOMATION.md`.
- D1 `form_choose` (drop-down incl. Google Forms, radio, tick/untick, date); D2 `page_key`;
  D3 `form_profile.py` + `form_fill` (late confirmation naming fields; never submits; values
  never in the pending action or the audit log); D4 `form_submit` (CONFIRM; invalid form and
  pay buttons refused); D5 `file_upload` (late confirmation naming the file; trusted-key hook;
  chooser driven with Ctrl+L).
- F1 `video` (speed, seek, captions, skip ad, next, like, watch later, full screen, state) and
  `video_subscribe` (CONFIRM). F2 `site_search` (any site with a search template, Amazon /
  Flipkart price and sort, images). F3 `shop_refine`, `product_info`; cart / buy / checkout /
  pay refused by `policy._SHOPPING_STOP` (decision D1).
- Tests: `test_browser_control.py` [21], `test_browser_extension.py` [10], `test_dmm_browser.py`
  95/95 on the local model.
- **Not verified live**: real forms, the real portal file chooser, live YouTube / Amazon /
  Flipkart pages.

**Part 5F — fix round V11** (done 2026-11-11, extension 1.10.0). Detail: the changelog in
`docs/platforms/LINUX_AUTOMATION.md` and `updates/UPDATE_V11.md`.
- `page.scroll` scrolls the box in view and reports `moved`; `common.scroll_request`.
- `page.search` / `page.searchClose`; `common.search_request`, `browser.search_close`.
- WhatsApp from anywhere ("… in whatsapp", "X chat", "X group" with a tab-group check), `recent`,
  unread-only reads, editions (`semantic_match.edition`), `year_ranges`.
- `rank_titles` by recency + prefix tier, `_find_files` fallbacks.
- Second pass (audit log, extension 1.11.0): `chat.search` waits for WhatsApp's results (the
  list was read before it filtered), search by words, search always closed, `search_close`
  verified with a real-key fallback, `message search` / `open result <n>` / `close this chat`,
  `_rethink` never re-runs the failed command.
- Changed on purpose: the same name with different YEARS no longer asks (chats: latest message;
  files: first listed). Everything else that asked still asks.

**Part 5 — WhatsApp, app context, the semantic matcher** (done 2026-11-10, extension 1.9.0).
- H0.2 app context: `common.front_site(situation)` — a web-app window's site, or Chrome's tab
  when CHROME is in front (Chrome's active tab is not "in front" while another app is). With
  WhatsApp in front, `direct_tokens` turns "open X" / "click on X" / "go to X chat" into
  `message open X` (a known site or app stays itself unless "chat"/"group" is said), "read it"
  into `message read this`, "who messaged me" into `message who messaged me`.
  `linux.situation` remembers the app the user was in for 2 min while Kayra's window is in front.
  Not added: a DMM context hint (the rules-first route covers it without touching the cached prompt).
- H0.3 `automation/semantic_match.py` — `match(query, candidates, embed, aliases)` -> pick / ask /
  none. Measured on the user's example and 9 more names; local nomic embeddings were tried as a
  blend and made "PR group" a confident wrong pick, so meaning breaks near-ties only and can by
  itself only ASK. Aliases are an input; learning them is H2 (Part 7). `ws_open` (Part 4) still
  uses `rank_titles`.
- H0.4 WhatsApp: `message open <chat>` (visible list, then WhatsApp's search; the search box is
  emptied when nothing opens), `message who messaged me`, `message read <chat> [today]` / `message
  read this` (short -> read out; long -> LOCAL model only; no local model -> the latest 3),
  `message <chat>: <text>` (opens the chat, then asks "Send '<text>' to <real chat name>?"; on yes
  the open chat is re-checked, typed with clear, read back, Send (else Enter), arrival checked).
  `message.send` moved from the static CONFIRM set to this late confirmation (asserted).
- Tests: [19] (matcher table, app context, situation memory, open / ambiguous / search / unread /
  read / privacy / send / no / chat switched / old extension), `test_browser_extension.py` [7]
  (WhatsApp-shaped page), `test_dmm_browser.py` 72/72 on the local model (55 rules-first).
- **Not verified on a live, logged-in WhatsApp** (it needs the user's phone to link; Claude in
  Chrome was not connected). The selectors are by role / title / aria-label with the old class
  names as fallback.

**Tuning pass V9** (2026-11-10, extension 1.8.0, local model only) — see `updates/UPDATE_V9.md`.
Rules before the classifier (466 ms -> < 1 ms for browser commands; 62/62 on the new
`tests/test_dmm_browser.py`), boot warm-up, waits inside the browser, a per-command active-tab
cache, relevant text for page questions, spoken numbers / near spellings / spoken emails, Sheets
header + `where` filter, and site lookups fixed after live checks (Google bot page, YouTube Up
next + ads + transcript button, Amazon sponsored + `reviewText`, Bing organic, Wikipedia,
generic heading links). Open from the pass: streaming spoken answers (needs the turn pipeline),
live checks of the signed-in Google apps (Claude in Chrome was not connected).

**Part 4 — Google Workspace** (done 2026-11-10, extension 1.7.0, no new permission; D3
approved by the user: names read in Chrome, matched locally, never stored or sent to a model).
- Names: `page.docList` (Docs/Sheets/Slides/Forms links + `data-id` rows; Drive rows open via
  `drive.google.com/open?id=`) and `web_macros.rank_titles` (exact 100 / phrase 80 / all words
  60 / partial 40×share, shorter first). Two near-equal results under 80 → asks which.
- E1 `open my <name> sheet`, `create a new sheet called <Title>` (Rename + real keyboard; the
  user's capitals kept), `go to cell B12` (`page.focus` on the Name Box), `what's the total |
  average | highest | lowest | count of column C | the <header> column`, `how many rows`,
  `read row N`, `what are the columns` — from `gviz/tq?tqx=out:csv&headers=1&gid=<the tab>`
  (export as fallback) through `page.fetchText`, parsed with `csv`, currency / thousands
  separators / (negatives) understood, text cells skipped and said so. Sheets values never
  reach a model.
- E2 Docs `open the doc about X`, `search my docs for X`, `create a new doc`; read / summarize /
  ask use `export?format=txt` (`_page_of` → `doc_text`); `find X` in any Google editor = its
  Ctrl+F; `share this doc with <email>` opens Share and types the address — Kayra NEVER
  presses Send.
- E3 `start the slideshow` (Ctrl+F5); a bare "start presenting" with a Slides tab in front is
  the slideshow (`AutomationRuntime._slides_in_front`, decided before the share CONFIRM);
  `next slide` / `previous slide` (fast path — the local model read them as chat), `end the
  slideshow`.
- E4 `find the PDF Ravi sent me` (`type:pdf`; opens the one clear match), `search my drive
  for`, `open my recent files`, `download this file` (xlsx / docx / pptx / uc?export=download).
- E5 `email search for X`, `email show unread from this week|today`, `email compose`, `email
  open my <name> label`. E6 `calendar next meeting`, `calendar join my next meeting` (the event's
  own visible Meet link → `meet_join`), `calendar friday`. E7 `meet admit everyone`. E8 `add a
  note saying …` (Keep), `open the responses of my <name> form`, `what assignments are due`
  (Classroom to-do page, summarized).
- Live local DMM: 20 Workspace phrasings, 19 right first time; "next slide" now on the fast
  path. DMM matrix 50/53 (baseline).
- Tests: `test_browser_control.py` [17], `test_browser_extension.py` [5].
- **Not done** (later): Sheets add-row / sort / chart, Gmail mark-all-read and attachments,
  Calendar "move my 3 pm to 4", Meet noise cancellation / layout / recording, Drive share.
- **Not verified** against the live Google apps with the extension connected; the Docs home
  list markup and Calendar's schedule view are the most likely to need tuning.

**Part 3 — about the page, bookmarks, history, downloads, "it"** (done 2026-11-10,
extension 1.6.0 + `downloads`, approved by the user for D5).
- C5 `browser what does this page say about X` / `does this page mention X` / `ask this
  page: …` -> `page_ask` (model reads the page text; "The page doesn't say." otherwise).
- C6 `read the comments`, `summarize the reviews`, `what are people saying about this` ->
  `page.comments` (YouTube scrolled to + polled, Reddit, Amazon, Flipkart, HN, GitHub, any
  `itemprop=reviewBody`); `summarize this video` -> `page.transcript` (opens the panel, polled,
  description fallback said so); not YouTube -> the page summary.
- C8 `translate this page to <language>` (Google Translate, this tab, 28 languages),
  `translate the selected text to …`, `explain the selected text` (`page.selection`).
- C9 `bookmark this page [in <folder>]` (folder created if missing; an existing bookmark is
  said so), `search my bookmarks for …`, `delete the bookmark for …` (ALWAYS asks, exact ids).
- C10 `clear today's history` / `the last hour` / `15 minutes` (ALWAYS asks; never all time;
  "delete my browsing history" = today), `what did I visit today|yesterday|this week`.
- C11 `what did I download` (progress for active ones), `open my last download` (ALWAYS asks;
  never a program or a Chrome-flagged file; Kayra opens it with xdg-open — the
  `downloads.open` permission is NOT requested), `show it in folder`, `pause|resume|cancel
  the download`.
- G1 `LAST_TAB`: "close it" / "go back to it" / "read it" / "summarize it" = the tab Kayra last
  opened or switched to BY NAME (not by position), 10 minutes, only while still open.
- G2 `search_here`: site table, Docs/Sheets Ctrl+F, the page's search box, else Google.
- G4 `what can I do here` -> per-site hints (Gmail, Calendar, Meet, YouTube, Docs, WhatsApp,
  Google results, shops, GitHub; a general list elsewhere).
- Late confirmations (from 2c) carry the exact ids / id / `confirmed` flag.
- Live local DMM: 12 phrasings; the two misreads (a page question read as a word find,
  "what did I download" read as closed tabs) fixed in the preamble and re-checked; bare
  "close it" is still read as chat by the DMM, which cannot see that Kayra just opened a tab
  (a G1 follow-up: give the DMM the last opened tab as context). DMM matrix 51/53.
- Tests: `test_browser_control.py` [16], `test_browser_extension.py` [4] (real Chrome:
  reviews, dedupe, max, transcript off YouTube).
- **Not verified** against the live Chrome with the extension connected.

**Part 2c — the rest of tabs and windows** (done 2026-11-10, extension 1.5.0 + `tabGroups`,
approved by the user for D5).
- B2 `browser close all youtube tabs`, `browser close all but pinned`: > 3 tabs asks first; the
  approved action carries the exact tab ids (`confirmed_ids`), so "yes" never closes a tab
  opened in between. Mechanism: a handler returns NEEDS_CONFIRMATION with `detail["confirm"]`
  and `AutomationRuntime.execute_action` registers it ("larger than it sounded").
- B4 `browser group my <topic> tabs [as <name>]` (title/address match, else page text ≥ 2
  mentions, one window), `rename`, `make … <colour>`, `collapse/expand` (one / all), `add this
  tab to …`, `ungroup` (one / all), `close the … group` (> 3 asks), `list my tab groups`.
  "this group" = the active tab's.
- B5 `browser move this tab to a new window` / `… to the other screen` (Hyprland moves the new
  window), `browser merge my chrome windows` (same incognito-ness only).
- C3 `open the third result` / `play the second video` / `… in a new tab` / `what are the
  results` — `page.results` (site table + generic content links; nav/header/footer/aside
  excluded, deduplicated by address). Caught before `app.open` without the `browser` prefix.
- C4 `find X on this page` highlights (`page.highlight`, `<mark data-kayra-find>`, never inside
  inputs/textareas/contenteditable), `next match`, `previous match` (wraps), `clear highlights`.
- Live local DMM: 11/11 new phrasings produced the right token; DMM matrix 50/53 (baseline).
- Tests: `test_browser_control.py` [15] (36 + flows), `test_browser_extension.py` [3] (real
  headless Chrome: result list, highlight, step, wrap, clear restores the text exactly).
- **Not verified** against the live Chrome with the extension connected (as for 2a/2b).

**Part 2M — meetings and screen sharing** (done 2026-11-10, extension 1.4.0). §12 in full.
- M0.1 `common.share_request()` reads every phrasing ("share screen", "start screen sharing",
  "present now", "share the LM Studio window", "share this tab", "share my second screen",
  "… with sound", "… instead") into ONE `meet.share {surface, name, audio, instead}`; a bare
  token ("click on share screen") is caught before the click/screen prefixes; "meet am i
  sharing" is `meet.share_status`. The DMM preamble and `interpreter.CATALOG` (+ a worked
  example) carry the shapes. Live check against the local gemma model: all nine test
  phrasings produced the right token.
- M0.2 `llm_engine._guard_meeting_code`: a code the user never said is dropped (or, when the
  user said share, re-read as the share).
- M0.3 `common.merge_share_steps`: `focus X` + `meet share window` -> one share of X; one share
  per utterance.
- M0.4 no fixed sleeps in `web_macros` (asserted): `_drive_pickers` acts on the windows that
  actually appear and reads success from Meet's "Stop presenting".
- M0.5 the question names what goes out; "yes, don't ask again" trusts `meet.share` only, until
  `meet leave` (≤ 3 h).
- M0.6 `KAYRA_AUDIT_LOG` (path | off); `run_all.py` -> temp file, `_harness.py` -> off. The
  unit tier no longer writes `logs/automation.log` (checked: 14,258 lines before and after).
- M1.1 `extension/kayra-share-picker/kayra-share-picker` (stdlib-only, wraps the configured
  picker) + `automation/share_picker.py` (one-shot request in `$XDG_RUNTIME_DIR/kayra`, 0600;
  `install`/`uninstall`/`status` of the `xdph.conf` line; `setup.py` offers it). Without it, a
  named window is left for the user to pick — never Enter on the first entry.
- M1.2 tab: Chrome's tab list opens; the user picks (no API selects a tab in Chrome's picker).
- M1.3 monitor by position left to right, or the focused one; M1.4 switch while sharing, "am I
  sharing?"; M1.5 `systemAudio: "include"` (extension 1.4.0, `__kayraShareAudio`).
- M2.1 mako Do Not Disturb for the length of a share (`MEET_SHARE_DND`).
- Tests: `tests/test_browser_control.py` [12] (sharing), [13] (the picker helper, run as the
  portal runs it), [14] (audit log).
- **Not done:** M0.7 — nothing here has been tried in a live Meet call. Next time the user is
  in a call: install the helper (`python setup.py`), say "share the LM Studio window", and
  record what each picker did in CHROME_BRIDGE.md.

**Part 2a — tabs by position and bulk close** (done 2026-11-10, extension 1.2.2).
- `common.tab_position()` turns "the third tab" / "tab 4" / "the last tab" / "the second last
  tab" / "the tab on the right|left" into `#3`, `#4`, `#-1`, `#-2`, `+1`, `-1`; `tab_switch` and
  `tab_close` accept those targets. Positions count in the FOCUSED window's tab strip.
- `browser close duplicate tabs` (same page, ignoring `#fragment` and a trailing `/`; the active
  copy and pinned tabs are kept) and `browser close tabs to the right|left` (pinned tabs kept;
  CONFIRM-gated as `browser.tabs_close_side`).
- Extension 1.2.2: `tabs.close {tabIds}`; `REQUIRED_VERSION` bumped, so 1.1.0+ extensions reload
  themselves; an older one is never sent a tab-id close.
- New shapes are in `interpreter.CATALOG` and the DMM preamble. Tests:
  `tests/test_browser_control.py` section [10] (27 checks).

**Part 2b — the rest of the tab and page basics** (done 2026-11-10, extension 1.3.0).
- B3 `browser mute everything except this tab` / `unmute all other tabs` (`tabs.muteOthers`).
- B6 `browser what did i just close` (`tabs.recentlyClosed`, newest first) and `browser reopen
  the <site> tab i closed` (`tabs.restore {sessionId}` — a NAMED closed tab, not just the last).
- B7 `browser free up memory in chrome` / `put background tabs to sleep` (`tabs.discard`: never
  an active tab, never one playing sound, pinned kept). The honest limit from B7 stands: MV3
  cannot say which tab uses the most memory, and Kayra does not guess.
- B8 `browser find the tab about <topic>`: title/address match first, then the text of each tab
  (≤ 30 tabs, 20 KB each, sleeping tabs by title only), switches to the best.
- C1 `browser hard reload` (`tabs.reload {hard}`); C2 `browser zoom to 150 percent`
  (`tabs.zoom`; zoom in/out/reset stay Ctrl+plus/minus/0); C7 `browser copy the link | page
  title` (to the clipboard).
- Every new command checks `_outdated((1, 3, 0))` first. Shapes are in `interpreter.CATALOG`
  and the DMM preamble. Tests: `tests/test_browser_control.py` [11] (25 checks).
- NOT verified against a live Chrome with the extension loaded (see `updates/UPDATE_V4.md` §7).

**Part 1b — the understanding layer** (`automation/interpreter.py`). The user asked that
Kayra not be only hard-coded rules: "it should ask AI whether this is automation or chat, like
proper human-alike intelligence."
- **How it works.** The DMM (a model) still makes the first call: chat, web question or task.
  The rule parser then handles everything it knows in microseconds. Only when the rules cannot
  parse a token, are only guessing (`browser <anything>` read as a click), or ran and found
  NOTHING, `AutomationRuntime` asks a model what the user meant — with what was said, what the
  rules tried, the screen context (`linux.situation()`: app / web app in front, Chrome's active
  tab) and `interpreter.CATALOG`. It answers commands / chat (answered by the Chatbot via
  `take_chat_handoff()`) / one clarifying question / none. Unknown names go rules → model →
  web result.
- **Safety.** The model proposes, never authorises: every command is parsed and judged by the
  same policy; power, shell, plans, triggers and file delete/move/rename are never accepted
  from it; its own proposals are never re-interpreted (one call per request).
- **Speed.** First version ~4 s warm / 7.2 s cold. ~85 % of gemma-4-e4b's output was hidden
  thinking (prompt size barely mattered). `ai.fast()` calls the local model with
  `reasoning_effort: none`, temperature 0, 200-token cap, plus worked examples in the prompt:
  12/12 correct, median 0.51 s, worst 0.78 s. Requests the rules handle never call a model.
- **Covers part of H0.2** ("open X" inside the app in front) without hard-coding. Part 5 still
  adds WhatsApp site functions and the semantic matcher (loose names like "core team").
- **Rule for every later part:** add new command shapes to `interpreter.CATALOG` and, where a
  phrasing is easy to get wrong, a worked example to `_SYSTEM` (the tests assert both parse).
- `AUTOMATION_AI_INTERPRETER=off` in `.env` disables it.

**Tuning applied in Part 1** (found while reading rounds 1–4):
- `REQUIRED_VERSION` was declared but never checked, so an old extension silently ignored new
  arguments. It is now checked before any feature that needs it, and an out-of-date 1.1.0+
  extension is reloaded automatically instead of the user being sent to chrome://extensions.
- `open_url` answered "Done." for a navigation (against §2's honest-outcome rule): it now says
  what it opened.
- A curated app that is not installed (Spotify) said "not installed" on Linux but opened the
  web player on Windows; Linux now does what Windows (and CLAUDE.md) describe.
- "open the article I read yesterday" reached the app launcher; it is a history lookup now.
- This section's stale "nothing committed yet" and the extension version.

---

## 11. UI: smooth, GPU-composited scrolling  *(planned 2026-11-10, not started)*

Requested by the user after the GPU rendering pass ("the UI is very smooth now; scrolling
still feels like CPU"). Measured before planning (offscreen, 1280x800 @ 2.25x): one scroll
frame repaints the whole visible page — **27 ms median on Settings, 33 ms on System** — and a
wheel notch jumps 60 px in a single frame. Two causes:

1. **No interpolation.** Qt applies each wheel notch as one 60 px jump.
2. **No blit scrolling.** The pages are translucent over the animated backdrop, so Qt cannot
   move the already-drawn pixels; every scroll step repaints every visible card on the CPU,
   capping scrolling near 30 fps (a 120 Hz display needs ≤ 8 ms a frame).

### The plan

**S1. Smooth scrolling everywhere** (one helper installed on every scroll area: the `View`
base, the Chat transcript, any other `QScrollArea`):
- mouse wheel: each notch glides ~200 ms with ease-out; rapid notches ACCUMULATE into one
  continuous glide instead of restarting;
- touchpad: pixel deltas follow the fingers 1:1; a flick coasts and decays (kinetic momentum);
- keyboard: PageUp/PageDown/Home/End/arrows glide too;
- frames paced from the window's own refresh (`QWindow.requestUpdate`), so 60 / 120 Hz
  displays both get one step per refresh, not a fixed 16 ms timer;
- off under reduced motion (`prefers_reduced_motion()`).

**S2. A GPU scroll layer** (how browsers do it): while a scroll is in motion the page is shown
as pre-rendered tiles held as GPU textures and each frame only moves them (~1 ms); at rest the
live widgets swap back in.
- tiles (viewport-sized strips above and below) are rendered while the page is idle, so the
  first frame of a scroll does not stall (`QWidget.grab()` costs ~12 ms — see the page-fade
  note in `docs/reference/UI.md`; never pay it on the input path);
- any change to the page (a child repaint, a new chat message, a live meter) invalidates the
  tiles; a page that changes while scrolling (System's live meters, a streaming reply) falls
  back to live painting for that gesture — smooth motion, without the 1 ms frames;
- Chat uses the layer only while nothing is streaming.

**S3. Measure.** Extend `KAYRA_UI_PROFILE=1` with scroll frame time and frames per second;
target < 8 ms per frame. Report before/after numbers measured on the reference laptop.

**Tests:** the glide lands exactly on the target; rapid notches accumulate; reduced motion
disables it; the layer never shows stale content and always returns to the live page; no
scroll area is left without the helper.

**The honest limit:** Qt Widgets still paint on the CPU; only a Qt Quick rewrite would put
every frame of a live-changing page on the GPU. Not proposed.

---

## 12. Meetings and screen sharing  *(M0, M1, M2.1 done 2026-11-10 — see §10 "Part 2M"; M0.7 live test, M2.2–M4 open)*

The user tried "start screen sharing of this meet and share the LM Studio window" in a live
Google Meet call and nothing useful happened. What exists today: `web_macros.meet_share()` —
entire screen only, built against Chrome's and the portal's pickers but **never tried in a live
call**. Sharing a named window does not exist. None of this was in the plan before.

### M0. Bugs found in the audit log (fix first)

The real attempt, `logs/automation.log` 16:25–16:26 on 2026-11-10:

| Said / did | What Kayra ran | Bug |
|---|---|---|
| (start sharing) | `meet.join abc-defg-hij` | the DMM preamble's EXAMPLE meeting code was emitted as a real one |
| "share screen" | `mouse.click_text "share screen"` | a plain click, not `meet.share`; Chrome's picker opened and nobody chose |
| "share the LM Studio window" | `window.focus lm studio` + `meet.share monitor` → CONFIRM | focused away from Meet, shared the whole MONITOR, then waited on an unanswered confirmation |
| "meet start screen sharing" | `meet.join new` | "start …" is read as start a meeting |

- M0.1 Parsing: "start screen sharing", "present (now)", "share my screen", "share the <X>
  window", "share this tab", "share my second screen" → ONE `meet.share` with
  `surface` (monitor | window | browser) and an optional `target` name; never a click, never
  `meet.join`. Add the shapes to `interpreter.CATALOG` and the DMM preamble with the right
  examples, and assert the old ones no longer misroute.
- M0.2 Placeholder guard: any token argument that appears verbatim as an example in the DMM
  preamble (`abc-defg-hij`, …) is rejected like the `general query` placeholder; a real Meet
  code must match `[a-z]{3}-[a-z]{4}-[a-z]{3}` AND come from the user's words.
- M0.3 Compound requests: "share the X window" must NOT run `window.focus X` first (it takes
  Meet out of focus); the name belongs to the share.
- M0.4 Readiness, not clocks: replace the fixed `SLEEP(1.5)` steps in `meet_share` with
  `_until` waits for Chrome's picker, the portal picker and Meet's "Stop presenting".
- M0.5 Confirmation: speak "Share your entire screen / the LM Studio window with everyone in
  the call?" and accept a spoken yes (`resolve_confirmation`); offer a per-call "don't ask
  again for sharing" opt-in.
- M0.6 Test hygiene: the unit tier writes `AUTOMATION_*` lines into the developer's real
  `logs/automation.log` (every run adds six `meet.*` entries). Route the audit logger to a temp
  file under `EnvironmentGuard`, and assert the real log is untouched.
- M0.7 Try it live in a real call on the reference laptop and tune; record what each picker
  did in `docs/platforms/CHROME_BRIDGE.md` ("How share my entire screen works").

### M1. Share exactly what was named

- M1.1 **A named window** ("share the LM Studio window"). On Hyprland: ship a small picker
  helper and set it as xdg-desktop-portal-hyprland's `screencopy:custom_picker_binary`. Kayra
  writes the wanted selection (window handle by exact Hyprland identity, or monitor) to a
  one-shot file; the helper prints it as `[SELECTION]window:<handle>` and deletes it; with no
  request pending it execs the normal `hyprland-share-picker`, so manual sharing is unchanged.
  `setup.py` offers the config change (asked, reversible). On X11 / other compositors: the
  Chrome picker's window list, selected by title through the extension where possible, else say
  "press Share on <title>".
- M1.2 A specific Chrome tab ("share the YouTube tab"): `displaySurface: "browser"` plus
  activating that tab first / `preferCurrentTab` where Meet allows.
- M1.3 A specific monitor ("share my second screen"): monitor selection through the same helper.
- M1.4 Switch mid-call ("share LM Studio instead"), "am I sharing?", "stop sharing" (exists).
- M1.5 Share with sound ("share with audio"): `systemAudio: "include"` in the page hook.

### M2. Privacy while presenting

- M2.1 Do Not Disturb for the length of a share (Omarchy/mako `makoctl mode -a do-not-disturb`,
  restored on stop) so notifications never appear on a shared screen.
- M2.2 Before sharing a whole screen, warn when a sensitive window is visible on it (password
  manager, WhatsApp, email, banking) — "WhatsApp is open on that screen; share anyway?".
- M2.3 Kayra's own spoken lines stay quiet during a call (call detection exists; confirm it
  covers a sharing session).

### M3. In-call controls (Meet; Zoom/Teams/Discord web where the same labels exist)

Raise / lower hand, send a reaction, captions on/off, pin or spotlight a person, "who's in
the call?", send a message in the meeting chat, copy the meeting link and send it to someone
(CONFIRM), switch microphone or speaker ("use my headphones in this call"). Extends E7.

### M4. Around the call

- M4.1 "Your meeting starts in 2 minutes — join?" (calendar + `speak_unprompted`), joining
  from the calendar (E6).
- M4.2 Present a Slides deck straight into the call (with E3).
- M4.3 Save live captions during the call; afterwards "summarize this meeting" with action
  items offered to Calendar / Docs (local model first).
- M4.4 Local recording (wf-recorder / OBS), CONFIRM, with a visible indicator.
- M4.5 The same share commands for Zoom, Teams and Discord in the browser (they use the same
  `getDisplayMedia` path the page hook already wraps).

**Tests:** parsing of every phrasing above (and the four misroutes), the placeholder guard,
the picker helper (selection file written, consumed once, falls back to the normal picker),
DND on/off around a share, readiness waits (no `SLEEP` in `meet_share`), and the audit log
staying out of the developer's `logs/`.

---

## 13. Neighbouring features not covered elsewhere  *(planned 2026-11-10, not started)*

Ideas that sit next to what was built in V4 (cloud models, key storage, the redesigned and
GPU-composited UI, tab Parts 2a/2b) and that no section above covers. Each is small on its own;
pick them up alongside the part they neighbour. Ask before any that spends money or changes
the user's system.

### N1. Browser, next to Parts 2a/2b

- N1.1 **Save and restore a tab session** — "save these tabs as research", "open my research
  tabs" (stored locally as titles + URLs; restore into a new window).
- N1.2 **Reading list** — "read this later", "what's on my reading list", "open the next one"
  (Chrome's `readingList` API, MV3, Chrome 120+).
- N1.3 **Picture-in-picture** — "pop this video out" (`video.requestPictureInPicture()` in the
  page; YouTube and any `<video>`).
- N1.4 **Media on the page** — "skip ad", "play at 1.5x", "skip 30 seconds", "loop this video"
  (page `<video>` element; the active YouTube tab first).
- N1.5 **Screenshot of a tab / full page** — "screenshot this page" (`tabs.captureVisibleTab`;
  full page by scroll-and-stitch), saved to Pictures and copied to the clipboard.
- N1.6 **Tab hygiene suggestions** — "you have 14 tabs open, 5 are duplicates — close them?"
  through `speak_unprompted` (proactive service, respects calls and standby).
- N1.7 **Open the same page in another browser / profile** — "open this in Chromium",
  "open this in my work profile".

### N2. Models and keys, next to V4 §2

- N2.1 **Usage and cost** — tokens and estimated spend per provider today / this month,
  shown in Settings → AI models; an optional monthly cap that falls back to Groq/Gemini/local
  when reached.
- N2.2 **Model speed test** — Settings → "Compare": one fixed classification and one short
  reply on every configured provider and the local model, with first-token and total times.
- N2.3 **LM Studio management** — show which model is loaded, "load Qwen 3.5" / "unload the
  model" (LM Studio's REST API), and warn when the loaded model has no vision.
- N2.4 **Key health on boot** — test each stored key once in the background and say "your
  Claude key has no credit" / "your Groq key was revoked" before the first request fails.
- N2.5 **Re-run the DMM matrix from Settings** ("check how well Kayra understands commands on
  this model"), with the 53-case score shown per provider.
- N2.6 **Export / import settings** (without keys) to move a setup to another machine.

### N3. Interface, next to the redesign and GPU work

- N3.1 **Command palette** (Ctrl+K style): type any Kayra command or setting name, run it.
- N3.2 **Keyboard shortcut list** — a "?" overlay listing every shortcut; editable shortcuts.
- N3.3 **Text size and density** — "larger text" / "compact" in Settings, applied live through
  the theme tokens (also helps the 2.25x display).
- N3.4 **Light theme / follow the Omarchy theme** — the tokens are already centralised.
- N3.5 **Voice preview** — a Play button next to the Voice dropdown that speaks a sample line.
- N3.6 **Settings search** — filter all five sections by a word.
- N3.7 **Performance panel** — the `KAYRA_UI_PROFILE` numbers and the GPU status shown in
  System, instead of only in the log.
- N3.8 **Notifications inbox** — what Kayra said while you were away (proactive lines, failed
  actions) in one list on Home.

### N4. Voice, next to the voice dropdown

- N4.1 **Speaking speed and pitch** in Settings (Kokoro `speed`), applied live.
- N4.2 **Per-language voice** — answer in Hindi with a Hindi voice when asked in Hindi.
- N4.3 **Wake word** ("Kayra, …") as an option to the always-listening microphone, with a
  local detector so nothing leaves the machine.

### N5. Safety and housekeeping

- N5.1 **Backup of memory and settings** (local zip, keys excluded) and restore.
- N5.2 **Audit log viewer** in Automation: search, filter by risky actions, export.
- N5.3 **Test hygiene sweep** — find every other place a unit suite writes to the developer's
  `logs/`, `data/` or `Reports/` (M0.6 found one) and route it to a temp dir.

---

## 14. Chrome profiles  *(requested 2026-11-10; P1, P2, P3, P5 done 2026-11-11 — part 5P; P4 and the default-profile setting open)*

**What the user asked.** "Open Google Chrome" shows Chrome's *Who's using Chrome?* picker. The
user has five profiles that they renamed themselves (for example Personal, Work, Study, Study 2
and one named after a person); three are signed in to accounts on the same school
domain. They want "open my Work Chrome" / "open Chrome on <email>" to open straight into that
profile. (No real names, emails or domains are recorded here or anywhere in the repository.)

**Hard requirement: fully dynamic.** Nothing about anyone's profiles is ever written in the
code or the repository, and no user is asked to edit code, `.env` or a config file. Kayra
reads the profiles from Chrome itself every time, so it works for any user, any number of
profiles, renamed or never named.

### P1. Discover profiles from Chrome (read-only, local)
- Source: Chrome's own `Local State` JSON, `profile.info_cache` — one entry per profile folder
  (`Default`, `Profile 1`, …) with the profile `name` (what the picker shows above the avatar:
  "Personal", "Work"), the account's `gaia_name` / `gaia_given_name` (the account's display name),
  `user_name` (the email), `hosted_domain` (e.g. a school domain), `is_using_default_name`
  (true when the user never renamed it — "Person 1"), plus `profile.last_used` and
  `profile.last_active_profiles`.
- Locations (through `kayra.platforms`, never guessed in a shared module): Linux
  `~/.config/google-chrome/Local State` (also `chromium`, `BraveSoftware/Brave-Browser`,
  `microsoft-edge` — same format); Windows `%LOCALAPPDATA%\Google\Chrome\User Data\Local
  State`. The browser that "open Chrome" means is the one Kayra already resolves.
- Re-read when the file's modification time changes (a rename in Chrome shows up at the next
  request; no restart, no cache to clear). Parse failures -> "I can't read Chrome's profiles"
  and plain "open Chrome" still works.
- Privacy: read on this computer only; emails and names are matched locally
  (`semantic_match`), never logged in full (counts only), never sent to any model. The
  classifier sees only the user's words.

### P2. Say it any way
- Shapes: "open Chrome as / with / for / on / in <X>", "open my <X> Chrome", "open <X>'s
  Chrome", "open Chrome with my <domain or org> account", "open Chrome on <email>" (spoken
  emails via `common.spoken_email`), "switch to my <X> profile", "which Chrome profiles do I
  have?", and a site inside a profile: "open Gmail in my Work Chrome" (= the profile + the URL).
- Matching (`semantic_match.match`) against EVERY field of every profile: profile name, account
  name and given name, email and its local part, domain and its first labels ("my school
  account"), and
  ordinal words for unnamed profiles ("the first profile", "profile 2"). The profile name
  wins a tie over an account name ("Study" -> the profile named Study, not every Study-domain
  account); spoken numbers make "study two" -> "Study 2".
- Ambiguity asks, naming what tells them apart, from the data — e.g. "my school Chrome" with
  three profiles on one domain -> "Work, Study or Study 2?"; for never-renamed profiles it
  describes them by account: "Person 1 (<account name>) or Person 2 (<account name>)?"
- Plain "open Chrome" is unchanged (Chrome's picker) unless the user says "always open Chrome
  as Work" — stored as a USER SETTING (Settings + `.env` key written by Settings, never by hand),
  with "use the last profile I used" as the other option (`profile.last_used`).
- Optional, spoken: "call the Study 2 profile my exam prep" -> a local alias
  (`data/aliases.json`, H2) — again data, never code. Renaming in Chrome works just as well.

### P3. Open it
- `<browser> --profile-directory="<folder>" [url]` through the existing launcher (argv, no
  shell). If that browser is already running, Chrome opens a window in that profile; then the
  new window is focused (Hyprland: the newest window of the browser's class).
- Spoken result names the profile and the account: "Opened Chrome as Work (<account name>)."

### P4. The Kayra Bridge in more than one profile (follow-up)
- The extension is installed per profile. After opening a profile Kayra checks whether the
  bridge connected FROM it; if not: "Chrome is open as Study 2, but the Kayra extension isn't
  installed there — say 'browser set up' to add it."
- The bridge must accept one connection per profile and route each command to the profile in
  front (the extension reports its profile with `chrome.identity`-free means: the hello carries
  a per-install id kept in `chrome.storage.local`). Until then, browser commands go to the
  profile that connected first, and Kayra says which.
- "switch to my Work Chrome window" needs the window <-> profile link (Hyprland sees one class
  for all profiles): from the bridge per profile (P4), else the window title when Chrome puts
  the profile name in it.

### P5. Tests
- A temporary `Local State` with: renamed profiles (the user's five shapes, with made-up
  emails), never-renamed ones ("Person 1"), two on one domain, one without an account, a
  corrupt file, and a rename between two requests (mtime).
- Phrasings: every shape in P2, spoken emails, "study two", ties (profile name beats account
  name), ambiguity questions built from the data, plain "open Chrome" untouched.
- Launch argv recorded by the fake runner (no real browser); the extension-missing sentence.
- A guard test: no profile name, email or domain from the user's machine appears anywhere in
  `src/` or `tests/` (the feature must stay data-driven).
- Add the phrasings to `tests/test_dmm_browser.py` (rules-first where the shape is unambiguous).

