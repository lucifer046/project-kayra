# Kayra — Errors and Bugs

> **This file is for FIXING what is broken.** New features live in
> [`BROWSER_CONTROL_PLAN.md`](BROWSER_CONTROL_PLAN.md) (the implementation plan).
>
> **How to use it**
> - **Open bugs are at the top**, most important first. Each has an ID (`B<n>`), where it
>   was seen (the audit log, `logs/automation.log`, or a user report), what happened, the
>   cause when known, and the fix idea.
> - When a bug is fixed: move it to **Fixed** at the bottom, add the version
>   (`updates/UPDATE_Vx.md`) and the tests that pin it. Never delete an entry.
> - New bugs found in a log review are added to **Open** with the next free ID.
> - Most log entries record what Kayra DID, not what you SAID. Set `KAYRA_LOG_FILE=True` in
>   `.env` so the console log (what was heard, what the model produced) is kept too — several
>   bugs below cannot be diagnosed without it.

Last log review: **2026-11-11, run 16:13–16:22** (after V22).

---

## Open

| ID | Priority | Area | Short |
|---|---|---|---|
| B1 | high | Gmail / Chrome profiles | "open email" opens Gmail in the wrong Chrome profile when the extension is not connected |
| B2 | high | parsing / speech | "work space 5" / "park space 5" read as a Chrome profile (and maybe ran the "work" scene) |
| B3 | high | Chrome bridge | the extension only works in ONE Chrome profile (Work) — plan item P4 |
| B4 | medium | closing windows | "close Chrome" with two windows asks which one forever |
| B5 | medium | Chrome bridge | the extension disconnected mid-session (~16:20); cause unknown |
| B6 | medium | speech / targets | misheard short words become targets: chat "on", profile "well", web search "general search" |
| B7 | low | Gmail | a badly misheard sender ("itmbs metre team") finds nothing |
| B8 | low | WhatsApp | "lunch open lunch" — a garbled open run four times; cause unknown |
| B9 | low | diagnostics | what the user SAID is not logged (`KAYRA_LOG_FILE` off) |
| V1 | — | verification | the V19–V22 fixes have not been tried live |

### B1 — "open email" opens Gmail in the wrong Chrome profile  *(high)*

- **Seen:** log 16:20:22–16:20:33. "open email" reported success; "go back" and "reply" then
  failed with "the Kayra extension is only in your Work profile, and that one isn't open".
- **Cause:** `web_macros.email_check()` does `if not BRIDGE.connected(): return _L().open_url(GMAIL)`
  — `xdg-open` opens Gmail in the DEFAULT profile (Personal), which Kayra cannot control. It
  never goes through `_need_bridge()` / `_wake_chrome()`, so it neither uses the profile that
  has the extension nor says why it can't follow up.
- **Fix idea:** `email_check` (and every other "open X without the bridge" fallback:
  `open_in` → `_open_without_bridge`) should, when `chrome_profiles.with_extension()` names a
  profile that is not the default, open the page IN that profile
  (`google-chrome-stable --profile-directory=<folder> <url>`) and wait for the bridge;
  otherwise open by `xdg-open` and say "I can open it, but I can't control it in this profile."
- **Files:** `automation/web_macros.py` (`email_check`, `_open_without_bridge`, `_wake_chrome`).

### B2 — "work space 5" read as a Chrome profile  *(high)*

- **Seen:** log 16:13:55 `browser.chrome_profile 'park space 5'`, 16:14:11 `'work space 5'`
  (both "no Chrome profile called …"); 16:14:42 the **"work" scene ran** (opened terminal,
  VS Code, Chrome, moved windows, Do Not Disturb ON, power profile) — probably another
  misheard "workspace". "workspace 5" worked at 16:14:52.
- **Cause:** the recognizer splits "workspace" into "work space"; Part 5P's profile shapes
  (`common.profile_action`) match "work" (a profile name) before the workspace rules, and a
  bare "work" can reach `scene.run work`.
- **Fix idea:** before profiles and scenes: `(?:go to |switch to |open )?(?:work|park|walk) ?space (\d+|one…ten)`
  = `system workspace N`. A scene ("start work mode") only on its own words, never on a
  sentence that also holds a workspace number. Add the phrasings to `tests/test_dmm_browser.py`.
- **Files:** `automation/common.py` (`profile_action`, scene parsing, `_SYSTEM_VERBS`).

### B3 — the extension works in ONE Chrome profile only  *(high — plan P4)*

- **Seen:** every run today; the Kayra Bridge is installed in **Work** only. Chrome open as
  Personal / Study → every browser command fails (now with an honest message, V22).
- **Workaround (user):** add the extension to the other profiles (chrome://extensions →
  Developer mode → Load unpacked → `extension/kayra-bridge`).
- **Real fix:** BROWSER_CONTROL_PLAN §14 **P4** — the bridge accepts one connection per
  profile and sends each command to the profile in front.

### B4 — "close Chrome" with two windows asks forever  *(medium)*

- **Seen:** log 16:16:50 and 16:16:59 — `app.close chrome` AMBIGUOUS twice
  (candidates: the Work Gmail window, the Personal Meet window); nothing closed.
- **Cause:** the question lists window titles; the answer was not one of them, and saying
  "close chrome" again starts the same question.
- **Fix idea:** name Chrome windows by PROFILE ("Work or Personal?", from the title suffix or
  the bridge); accept "both" / "all" / "the Gmail one" / "the first one"; a repeated
  "close chrome" while the question is pending = close both (CONFIRM if a call is in one).
- **Files:** `automation/linux.py` (`close_target`, `_describe`), `common.resolve_choice`.

### B5 — the extension disconnected mid-session  *(medium, needs evidence)*

- **Seen:** Work Chrome opened 16:14:59 / 16:15:00 and Gmail worked at 16:15:56; by
  16:20:22 the bridge was not connected (B1 followed).
- **Unknown:** was the Work window closed (by "close whatsapp" at 16:20:17, or by hand), or
  did the MV3 worker drop the socket? The extension pings every 20 s and reconnects every 30 s.
- **To do:** log bridge connect/disconnect with time and profile (`browser_bridge` →
  `logbus`, `Subsystem.AUTO`), then re-check on the next run. Fix once the cause is known.

### B6 — misheard short words become targets  *(medium)*

- **Seen:** 16:17:43 `message.search 'on'` ("I don't see a chat called on"); 16:20:53
  `browser.chrome_profile 'well'` (meant "Work"); 16:21:57 `search_web 'general search'`.
- **Fix idea:** a target of one or two letters / a stop word ("on", "it", "the") is never a
  name — ask what was meant; profile names use the same sound-alike repair as on-screen names
  (V19), so "well" → "Work" when Work is a profile; the profile list joins
  `screen_vocabulary.names()`.
- **Files:** `input/transcript_repair.py`, `automation/screen_vocabulary.py`, `automation/common.py`.

### B7 — a badly misheard Gmail sender finds nothing  *(low)*

- **Seen:** 15:11:58 `email.open 'itmbs metre team'`. ("iitm bs meet up team" is fixed in V22.)
- **Fix idea:** sender names on screen into `screen_vocabulary.names()` (Gmail rows), so the
  transcript repair can correct the name before parsing.

### B8 — "lunch open lunch" ran four times  *(low, needs evidence)*

- **Seen:** 15:13:43–15:13:55: `message.open 'lunch open lunch'`, `'lunch'`, again, again —
  all not found. Possibly before V20's double-run fix was loaded.
- **To do:** re-check after a run with `KAYRA_LOG_FILE=True`; close if it does not recur.

### B9 — what was said is not logged  *(low, diagnostics)*

- `KAYRA_LOG_FILE=False` in `.env`, so only actions reach `logs/automation.log`. Every log
  review guesses the words. **Fix:** turn it on (user), or log the heard text + DMM tokens at
  INFO into the audit log (redacted the same way).

### V1 — verify V19–V22 live  *(verification, not a bug)*

Try each fixed item below once with the voice and tick it here: en-IN + on-screen names (V19),
L1–L8 (V20), L9 (V21), L10 (V22).

---

## Fixed

### V22 — 2026-11-11 (log 15:11–15:46 + user report) — `updates/UPDATE_V22.md`

- **L10.1** "open email" opened AND read the latest email → `common.email_words`: inbox only;
  "open the latest email" opens it; "… and read it" / "read my latest email" reads it.
- **L10.2** extension only in Work, Chrome open as Personal → `chrome_profiles.with_extension`;
  a closed Chrome starts in that profile; otherwise the profile is named. (Root cause: B3.)
- **L10.3** "close all tabs" closed tabs whose NAME contained "all" → every tab in the window.
- **L10.4** "close (app name)" (a model template) → refused (`common.is_placeholder`).
- **L10.5** "close Google Docs" also closed Sheets → matched by path.
- **L10.6** "search for X" with Sheets in front searched Docs → the page's own search.
- **L10.7** "the email from IITM BS meet up team" not found → spacing/order-free match
  (extension 1.16.1) + Gmail's search.
- Tests: `test_browser_control.py` [30], `test_browser_extension.py` (+2).

### V21 — 2026-11-11 — `updates/UPDATE_V21.md`

- **L9** "open images in Google Drive" searched the computer → Drive (folders: `type:folder`);
  "open folder … group" → the group; bare "open folder" → home folder; "Google Chat" added;
  stray "click share" after a share dropped; "yes, delete it" / "yes yes" / "delete it" now
  answer a pending question (the email was never deleted); "open the latest email" no longer
  waits for all of Gmail to load.

### V20 — 2026-11-11 (logs 00:08–02:24, 14:23–14:52) — `updates/UPDATE_V20.md`

- **L1** "Chrome isn't connected" when Chrome was only closed → Chrome is started.
- **L2** "send it" became a chat called "(contact)" → `message.send_draft` (asks first).
- **L3** every failed WhatsApp open ran twice → search retried inside the request; identical
  commands run once.
- **L4** "core team" offered chats sharing only "team"; "P.R." not matched → fixed.
- **L5** "open X chat and read the last three messages" became one chat name → split.
- **L6** "close Google Drive" / WhatsApp as a tab / "close the WhatsApp group" → the tab.
- **L7** Sheets/Drive file search (wrong kind, 24 s, lost results, wrong file) → fixed.
- **L8** "open Sheets and search X" searched before the page loaded → waits for the page.

### V19 — 2026-11-11 — `updates/UPDATE_V19.md`

- **L0** mishearing → `INPUT_LANGUAGE=en-IN`; misheard chat / file / button names corrected
  to the one on screen (`automation/screen_vocabulary.py`, `input/transcript_repair.py`).
