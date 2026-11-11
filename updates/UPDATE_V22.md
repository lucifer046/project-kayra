# Update V22 — "Open email" means the inbox; the extension's profile; close all tabs

| | |
|---|---|
| **Version** | V22 |
| **Based on** | V21 (uncommitted), on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Fix (user report + audit log 15:11–15:46) |
| **Scope** | BROWSER_CONTROL_PLAN §15 L10 |
| **Extension** | 1.16.1 (reloads itself on the next hello) |

## 1. Summary

| Said / happened | Before (V21) | After (V22) |
|---|---|---|
| "open email" / "open Gmail" / "open my inbox" | the model opened the LATEST email and read it out | Gmail opens on the inbox: "Gmail is open. You have 3 unread emails." |
| "open the latest email" | (same as above, sometimes read out) | opens it, says who it's from — no reading |
| "open the latest email and read it" / "read my latest email" | — | opens it and reads it |
| any browser command with Chrome open as Personal | "Chrome isn't connected… browser set up" (the extension is only in Work) | "Chrome is open, but the Kayra extension is only in your Work profile… say 'open Chrome as Work'" |
| a browser command with Chrome closed | started as Personal (no extension), then failed | started as Work, where the extension is |
| "close all tabs" | closed the tabs with "all" in their name | closes every tab in the window (asks above 3; pinned stay) |
| "close (app name)" (model template) | looked for an app called "(app name)" | "I didn't catch which one." |
| "close Google Docs" with a Sheet open too | would close both (same site) | the Doc only |
| "search for sundarbans house" with Sheets in front | searched Google Docs | searches Sheets |
| "open the email from IITM BS meet up team" | not found ("Meetup" ≠ "meet up") | found; if not on screen, Gmail's search finds it |

## 2. Tests

`tests/test_browser_control.py` [30] +25; `tests/test_browser_extension.py` +2 (real Chrome);
unit tier 6062, 0 failed; extension suite 160, 0 failed.

## 3. Known limits / for you to do

- The Kayra extension is only in your **Work** Chrome profile. To use Kayra with **Personal**,
  add it there too (chrome://extensions → Developer mode → Load unpacked → `extension/kayra-bridge`).
  Making the bridge work with several profiles at once is plan item P4.
- "open the email from X" with X misheard ("itmbs metre team") still finds nothing.
- Not tried live.
