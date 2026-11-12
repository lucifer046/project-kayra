# Update V21 — The small fixes from the audit-log review (Part 17, L9)

| | |
|---|---|
| **Version** | V21 |
| **Based on** | V20 (uncommitted), on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Fix |
| **Scope** | BROWSER_CONTROL_PLAN §15 L9 |
| **Models** | none; no extension change (1.16.0) |

## 1. Summary

| Said / happened | Before (V20) | After (V21) |
|---|---|---|
| "open the images folder in Google Drive" | searched your computer: "I couldn't find images in google drive" | Drive is searched for a folder called images, and it is opened |
| "open images in Google Drive" | the same local search | any Drive item called images |
| "open folder sundarbans house general panchayat group" | a local folder by that name | the group (tab group, else the WhatsApp group) |
| "open folder" | "I couldn't find an app called folder" | your home folder in the file manager |
| "open Google Chat" | "I couldn't find an app called google chat" | chat.google.com |
| "share screen" arriving with "click share" | share, then "I can't find share on this page" ×2 | the share only |
| "delete this email?" — "yes, delete it" / "yes yes" / "delete it" | taken as a new delete: asked again, never deleted | taken as yes: deleted |
| "open the latest email" with Gmail not open | waited for Gmail's whole page (9 s in the log) | opens the email as soon as the list is there |

## 2. Changes

- `automation/common.py`: `folder_action` (Drive folders, "folder … group", bare "open folder");
  `_is_share_click` in `merge_share_steps`; `AutomationRuntime._restated`.
- `automation/policy.py`: `read_confirmation_reply` accepts a reply made only of agreeing words
  with a real yes among them (`_CORE_YES`, `_AGREEING`); any other word still makes it a new command.
- `automation/web_macros.py`: `_WS_HOME["folder"]`; `email_open` opens a missing Gmail tab
  without waiting and polls the list (BridgeError while navigating = not ready yet).
- `automation/sites.py`: "google chat".

## 3. Tests

`tests/test_browser_control.py` [30] +25 checks. Unit tier 6037, 0 failed; extension suite 158/0.

## 4. Known limits

- Not tried live. The email speed-up is from reading the code path, not a measurement.
- A restated request counts as yes only for the SAME action (`email.delete` for "delete it");
  "yes, and also open chrome" is still a new command.
