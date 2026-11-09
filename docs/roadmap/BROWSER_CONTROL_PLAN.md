# Browser Control — Implementation Plan (round 5)

> **Status: PLANNED, NOT STARTED.** Written 2026-11-10 at the user's request so a later session
> can implement it when told "implement the browser control plan" (or a phase of it).
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
- **Wait for readiness, not fixed sleeps:** add an extension command
  `page.waitFor {text|selector|urlIncludes, timeoutMs}` and use it instead of `SLEEP(1.5)`.
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

- **D1** Shopping: stop at "add to cart"? (recommended: yes)
- **D2** Form profile fields to store in `data/profile.json` (name, email, phone, address,
  college, roll number?) — never passwords/cards.
- **D3** May Kayra read Drive/Docs **file names** (in Chrome only, never stored) to search them?
- **D4** URLs of the user's IITM portals for the site registry.
- **D5** Extra permissions: `downloads` (C11), `tabGroups` (B4). `debugger` is NOT to be added.

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

- [ ] A1 [ ] A2 [ ] A3 [ ] A4 [ ] A5 [ ] A6
- [ ] B1 [ ] B2 [ ] B3 [ ] B4 [ ] B5 [ ] B6 [ ] B7 [ ] B8
- [ ] C1 [ ] C2 [ ] C3 [ ] C4 [ ] C5 [ ] C6 [ ] C7 [ ] C8 [ ] C9 [ ] C10 [ ] C11
- [ ] D1 [ ] D2 [ ] D3 [ ] D4 [ ] D5
- [ ] E1 [ ] E2 [ ] E3 [ ] E4 [ ] E5 [ ] E6 [ ] E7 [ ] E8
- [ ] F1 [ ] F2 [ ] F3 [ ] F4 [ ] F5 [ ] F6
- [ ] G1 [ ] G2 [ ] G3 [ ] G4 [ ] G5 [ ] G6
- [ ] H0.1 [ ] H0.2 [ ] H0.3 [ ] H0.4 [ ] H1 [ ] H1.1 [ ] H2 [ ] H3 [ ] H4 [ ] §7a model benchmark

## 9. Known state at the time of writing

- Extension in the repo is **1.1.0**; the user's Chrome was still running **1.0.0** (needs one
  reload on chrome://extensions — after that, use `runtime.reload` for updates).
- Unit tier: 4398 checks green; extension integration suite: 24 checks green.
- Screen (non-browser) clicking needs `/dev/uinput` and is deliberately deferred by the user.
- Nothing from rounds 1–4 has been committed yet.
