# The Kayra Bridge — voice control inside Chrome

Kayra can drive **the Chrome you actually use**, with your sign-ins: switch and close tabs
(including background ones), click buttons by their name, type into fields, read and
summarize pages, use Gmail, Google Calendar, WhatsApp Web and Google Meet.

It does that through a small Chrome extension in this repository,
[`extension/kayra-bridge`](../../extension/kayra-bridge), that talks to Kayra over a local
WebSocket.

```
Chrome  ── Kayra Bridge extension ──ws://127.0.0.1:17893/kayra──▶  Kayra (automation/browser_bridge.py)
        tabs · clicks · typing · page text · history · bookmarks · Meet
```

## One-time setup (about a minute)

1. Start Kayra, then say **"browser set up"**. Kayra opens `chrome://extensions`, opens the
   extension folder, and copies its path to your clipboard.
2. In Chrome, turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `extension/kayra-bridge` folder (paste the path).
4. That's it. The extension connects on its own within 30 seconds. Say **"browser status"**
   to check.

The extension has a fixed ID (`gdllbbphfpgfcgmpjjklkkkpebimeeel`), derived from the public key
in its manifest, so it is the same on every machine. No private key exists in the repo or
anywhere else; one is never needed for an unpacked extension.

## What you can say

| Say | What happens |
|---|---|
| "open google sheet", "open notion", "open swiggy" | the site (an installed app or Omarchy web app wins; an unknown name opens the top web result) |
| "open google sheets in this tab / in a new window / in incognito / next to this tab" | exactly there |
| "open new tab and open Google Sheets in that tab" | one new tab with Sheets — no race |
| "open my sheets / my docs / watch later / my orders" | your own lists |
| "browser update" | reloads the extension from this folder |
| "list my tabs" | titles of every open tab |
| "go to my Gmail tab", "switch to the YouTube tab" | activates it and brings Chrome forward |
| "close the YouTube tab" | closes it; two YouTube tabs → asks which (or "close all YouTube tabs") |
| "close all other tabs" | **asks first**, then keeps only the current one |
| "go to the third tab", "switch to tab 4", "the last tab", "the tab on the right" | by position in the window you are in |
| "close the second tab", "close the last tab" | exactly that tab |
| "close duplicate tabs" | closes extra copies of the same page (keeps the one you are on, and pinned tabs) |
| "close the tabs to the right / left" | **asks first**; pinned tabs stay |
| "mute everything except this tab", "unmute all other tabs" | every other tab |
| "what did I just close?" | the last few closed tabs and windows |
| "reopen the YouTube tab I closed" | that tab, not just the last one closed |
| "free up memory in Chrome", "put background tabs to sleep" | background tabs sleep (never one playing sound); they reload when opened |
| "find the tab where I was reading about transformers" | reads your open tabs and switches to the best match |
| "hard reload", "zoom to 150 percent" | as said (zoom in / out / reset are Ctrl + / − / 0) |
| "copy this page's link", "copy the page title" | onto the clipboard |
| "mute this tab", "pin this tab" | as said |
| "click the sign in button", "click the second reply", "click on more", "click on load more" | clicks by the name a screen reader would announce (see **How clicks are matched**) |
| *any* "click on …" / "scroll down" while Chrome is in front | goes **into the page** through the extension — no screenshot, no OCR, no `/dev/uinput` |
| "type hello into the search box", "fill email with me@x.com", "search this page for pricing" | types into the field with that label/placeholder |
| "scroll until you see pricing", "scroll to the bottom" | scrolls the page |
| "read this page", "summarize this page" | reads it out / summarizes it with the AI |
| "what can I click here?" | lists the buttons on the page |
| "open the article I read yesterday about transformers" | searches your history and opens the best match |
| "open my bookmark for the Rust book" | searches bookmarks |
| "check my email" | the unread count, from Gmail's own title |
| "open my latest email" | opens the **newest by date** (not the top row — a priority inbox puts "Important" above newer mail) and says who it's from and the subject |
| "open the first / second / last email", "open my unread email", "open the email from Amazon" | opens that one; first/second count from the top of the list |
| "read my latest email", "read the email from Ana", "read this email" | opens it if needed and reads **that** email: sender, what it says, any date or action |
| "read my emails" | a summary of the inbox list |
| "go to Sent / All Mail / Starred / Drafts / Spam" | navigates Gmail directly (no clicking through "More") |
| "reply", "forward", "archive", "go back to inbox", "mark it unread"; "delete it" / "report spam" (ask first) | Gmail's own buttons on the open email |
| "draft an email to ana@x.com about lunch saying see you at 1" | opens a prefilled Gmail draft |
| "send it" | **asks first**, then clicks Send |
| "what's on my calendar today/tomorrow" | reads your Google Calendar day view aloud |
| "add standup tomorrow at 10am to my calendar" | **asks first**, then fills and saves the event |
| "tell Rahul I'm running late" | **asks first**, then sends it on WhatsApp Web |
| "join my meeting abc-defg-hij", "start a new meeting" | opens Meet and clicks Join now |
| "mute me", "turn off my camera", "turn on captions", "raise my hand", "leave the call" | clicks Meet's own buttons |
| "share my screen", "start screen sharing", "present now" | **asks first** ("Share your entire screen with everyone in the call?"), then shares the screen you're on (see below) |
| "share the LM Studio window", "share LM Studio instead" | **asks first**, then shares exactly that window (needs Kayra's share picker on Hyprland, see below) |
| "share my second screen", "share my screen with sound" | a specific monitor (counted left to right); system audio included |
| "share this tab", "share the YouTube tab" | opens Chrome's picker on its tab list; you pick the tab |
| "yes, don't ask again" (to a share question) | shares, and stops asking before sharing until you leave the call |
| "am I sharing?" | says whether you are, and what |
| "stop sharing" | clicks Stop presenting and brings notifications back |

Without the extension, Gmail drafts, calendar events and navigation still open by URL; the
rest says how to connect it.

## When Kayra doesn't understand the words

If a request matches none of Kayra's rules ("open PR and outreach core team chat" with
WhatsApp in front, "make this tab quiet", "hit the like button"), a local model is asked what
you meant, given what's on your screen. It answers in about half a second and turns the request
into one of the commands above. It can also decide it was a question (answered as chat) or ask
you one short question. It never sends, deletes or shuts down anything without the usual
confirmation, and it can't propose power, terminal or file-deletion commands at all.

## How clicks are matched

The extension looks at what a screen reader would announce for every visible control (its
`aria-label`, tooltip, title or text) and takes the first tier that has any match:

| Tier | Example (from a real Gmail) |
|---|---|
| exact name | "sent" → **Sent** |
| same words ignoring plurals and a few synonyms | "all mails" → **All Mail**; "load more" ≈ "show more" |
| name starts with the words | "more" → **More labels** (before "More email options", by page order) |
| every spoken word is in a short name | "load more" → **Show more messages** |
| the words appear in the name | "reply" → **Reply all** |
| visible-text fallback | plain text inside a clickable element (Gmail's spans) |

Hidden elements are never clicked, the innermost match wins (the button, not the panel around
it), and "the second Save" picks the second match. Say **"what can I click here?"** to hear the
real names on the page.

Gmail's message rows are not buttons, so opening an email has its own command
(`gmail.open`): it reads each row's sender, subject, unread mark and full timestamp, and opens
the right one.

## Updating the extension

From 1.1.0 on the extension updates itself: when Kayra needs a command your Chrome does not
have yet, it sends `runtime.reload` (Chrome re-reads this folder) and says *"I've just updated
the Kayra Bridge extension…"* — ask again a few seconds later. You can also say **"browser
update"**. Only a 1.0.x extension needs the manual **reload arrow** on `chrome://extensions`.

Current version: **1.4.0** (1.4.0: the Meet page hook can include system audio in a share; 1.2.0 added `tabs.open` with `where` and `page.waitFor`; 1.2.1 makes
click matching and `page.waitFor` cheaper; 1.2.2 adds `tabs.close {tabIds}`; 1.3.0 adds
`tabs.recentlyClosed`, `tabs.restore {sessionId}`, `tabs.muteOthers`, `tabs.discard`,
`tabs.zoom` and `tabs.reload {hard}` — no new permissions). An older 1.1.0+ extension is
reloaded automatically, once, when it connects.

## How screen sharing works (2026-11-10, BROWSER_CONTROL_PLAN §12 part 2M)

Every way of asking — "share screen", "start screen sharing", "present now", "click on share
screen", "share the LM Studio window" — becomes ONE `meet.share` action with a surface
(monitor / window / browser tab), an optional name, and optional audio. Before 2M a live attempt
misrouted four ways (the DMM preamble's example code `abc-defg-hij` was joined as a real
meeting; "share screen" became a plain click; "share the LM Studio window" focused LM Studio
away from Meet and then shared the whole monitor; "meet start screen sharing" started a new
meeting). All four are fixed and tested.

1. **It asks first, naming what goes out**: "Share the LM Studio window with everyone in the
   call?" Answer "yes", or "yes, don't ask again" to stop being asked until you leave the call.
2. Kayra tells the Meet page hook (`meet_main.js`, running in Meet's own page) to add Chrome's
   standard `displaySurface` hint (and `systemAudio: "include"` when you said "with sound"),
   so Chrome's picker opens on the right list.
3. **On Hyprland with Kayra's share picker installed**, it writes a one-shot request
   (`$XDG_RUNTIME_DIR/kayra/share-request.json`, 0600) naming the window (by its Hyprland
   address) or the monitor. When Chrome asks the desktop portal for a source, the portal runs
   `extension/kayra-share-picker/kayra-share-picker`, which answers with exactly that window or
   monitor and deletes the request. With no request pending it runs your normal picker
   (Omarchy's `hyprland-preview-share-picker`), so sharing by hand is unchanged.
4. It clicks Meet's **Share screen** / **Present now**, then **waits for what actually appears**
   (no fixed pauses): a new Chrome picker window gets **Enter** (pressed again once if Chrome
   only enables Share after the portal answered); a portal picker window gets **Enter** only
   for a plain "share my screen" without the helper.
5. As soon as Meet shows **Stop presenting** it says what you're sharing and turns on Do Not
   Disturb (mako) for the length of the share, unless you already had it on
   (`MEET_SHARE_DND=off` disables this). "stop sharing" or leaving the call turns it back off.

**Without the helper**, a whole screen is still shared for you, but a NAMED window or tab is
left for you to pick in the picker — pressing Enter there would share whatever is listed first.
Install the helper with `python setup.py` (it asks), or:

    PYTHONPATH=src .venv/bin/python -m kayra.automation.share_picker install    # or uninstall / status

It adds one line, `custom_picker_binary = <repo>/extension/kayra-share-picker/kayra-share-picker`,
to `~/.config/hypr/xdph.conf` (backed up to `xdph.conf.kayra-backup`), records the old picker in
`~/.config/kayra/share-picker-fallback`, and restarts `xdg-desktop-portal-hyprland`.
`uninstall` puts the old line back exactly.

**Not yet verified in a live call** (plan item M0.7): the order in which Chrome on Wayland
shows its own picker and the portal's, and the portal's window list format with Kayra's helper
(parsed from xdph 1.4.1's `XDPH_WINDOW_SHARING_LIST`). The unit suite drives every step against
fakes. If a picker stays open, press Share and tell me what you saw.

## Security

- **Local only.** Kayra listens on `127.0.0.1`. The extension only ever connects there and
  makes no other network requests.
- **Only this extension can connect.** The WebSocket handshake is accepted only when its
  `Origin` is `chrome-extension://gdllbbphfpgfcgmpjjklkkkpebimeeel`. Browsers set `Origin`
  themselves; a website you visit cannot fake it, so no page can drive your browser through
  Kayra. Tested with real sockets in `tests/test_assistant_automation.py`.
- **Never your passwords or card numbers.** The extension refuses to type into password,
  PIN, CVV and card-number fields, and Kayra refuses on its side too.
- **Risky clicks ask first.** "Click Delete / Send / Pay / Confirm / Submit / Publish /
  Unsubscribe …" all need your yes, as do sending email or messages, adding events, sharing
  your screen and closing other tabs.
- **Minimal permissions.** tabs, scripting, history, bookmarks, sessions, alarms, storage.
  No cookies, downloads, debugger or native messaging.

## Why an extension, not remote debugging

Chrome 136+ refuses `--remote-debugging-port` on your normal profile, and a separate debug
Chrome is not signed in as you. An extension runs inside your own Chrome, sees your real tabs
(including background ones, which window titles never show), and stays alive in MV3 because
a WebSocket that exchanges a ping every 20 seconds keeps its service worker running.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Chrome isn't connected to me yet" | Is the extension enabled on `chrome://extensions`? Click its toolbar icon to force a reconnect. |
| Port 17893 busy | Set `KAYRA_BRIDGE_PORT` in `.env` **and** `PORTS` in `background.js`, reload the extension. |
| A button "can't be found" | Say "what can I click here?" to hear the exact names, then use one. |
| Meet picker stays open | Press Share; report what you saw so the key presses can be tuned. For a named window, check `PYTHONPATH=src .venv/bin/python -m kayra.automation.share_picker status` says `installed`. |

## Tests

- `tests/test_assistant_automation.py` [3]: real-socket handshake (rejects a web page's and another
  extension's Origin), real frame round-trip, error propagation, port release, the manifest
  key really producing the ID, and the extension's permissions.
- `tests/test_assistant_automation.py` [4b]: every Gmail phrasing, open/read/folder/actions against a fake
  extension, the out-of-date-extension message, and clicks/scrolls routed into Chrome only
  when Chrome is in front.
- `tests/test_browser_extension.py` (integration, needs Chrome): runs the extension's page
  functions inside a throwaway headless Chrome. They click Meet-style buttons by accessible
  name, pick the second "Save", never click hidden buttons, find fields by label or
  placeholder, refuse password and card fields, type into a chat box, scroll to text and
  list the controls. A Gmail-shaped page (priority-inbox order, a collapsed "More", "Show
  more messages", rows that are not buttons) proves "click on more", "all mails", "sent",
  "load more", and opening the first / latest / second / from-Ana / second-unread email, and
  the `page.waitFor` probe. 27 checks.
- `tests/test_browser_control.py` (unit): the site registry, every "where", the new-tab merge,
  app/web-app/site/web-result order, the no-extension fallbacks, self-update, and the AI
  understanding layer against a scripted model. 130 checks.
