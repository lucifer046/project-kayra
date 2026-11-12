# Update V12 — forms, the video, site search and shopping (Part 6a)

| | |
|---|---|
| **Version** | V12 |
| **Based on** | V11 (uncommitted, on top of commit `10d56ae` on `feature/linux-desktop-automation`) |
| **Date** | 2026-11-11 |
| **Type** | Feature, privacy |
| **Scope** | BROWSER_CONTROL_PLAN Part 6, slice 6a: D1–D5 (forms), F1 (video), F2 (search any site), F3 (shopping, within decision D1); Chrome extension 1.12.0 |
| **Not in scope** | Slice 6b: F4 ask ChatGPT / Claude / Gemini, F5 WhatsApp reply-to-open-chat and send a file, F6 LinkedIn / Reddit pages, G5 multi-step requests |
| **Decisions** | **D1** (asked 2026-11-11): shopping is search and open only — no add-to-cart, no cart page, no checkout. **D2**: the form profile may hold name, email, phone, postal address, college, roll number, date of birth and gender |
| **Models** | none added. Everything is rules plus page functions; no form value ever reaches a model |

## 1. Summary

| Said | Before (V11) | After (V12) |
|---|---|---|
| "fill this form with my details" | typed the words "my details" into a field called "this form" | asks "I can fill 5 fields here: name, email, phone number, date of birth and gender. Fill them?", then fills only those; never submits |
| "remember my phone number is 98765 43210" | not understood | saved for forms (on this computer, private file) |
| "select India in the country dropdown", "tick I agree" | a click on a button named "select india in the country dropdown" | the option is chosen / the box is ticked; a missing option names the real ones |
| "set the date of birth to 4 May 2003" | not understood | the date field is set |
| "submit this form" | a click on "submit this form" | asks first; a form with an empty required field is not sent and the field is named; a "Place order" button is never pressed |
| "upload my resume here" | a click on "upload my resume here" | asks "Upload Ana-Resume-2026.pdf from home/Documents to this page?", then opens the page's own chooser and puts the file in |
| "skip the ad", "speed 1.5", "skip 10 seconds", "turn on captions", "like this video" | a click on those words, or nothing | the video in front does it, and says where it is now |
| "search Amazon for earbuds under 2000" | searched the current site for "amazon for earbuds" | Amazon's results, priced up to ₹2,000 |
| "sort by price low to high" (Amazon / Flipkart results) | not understood | the same search, cheapest first |
| "how much is this" (a product page) | not understood | "boAt Airdopes 141 costs 1,299 rupees. Rated 4.1 out of 5 from 2,345 ratings. In stock." |
| "add this to cart", "buy it now", "checkout" | a click (asked first for buy / checkout) | refused: "I only search and open products. Adding to a cart, checking out and paying are for you to do." |

## 2. Changes by area

- **Extension 1.11.0 → 1.12.0** (`extension/kayra-bridge/background.js`, no new permission)
  - `pageForm(op, args)` — ONE self-contained function: `list` (every field with its label from
    `aria-labelledby`, `aria-label`, `<label for>`, a wrapping label, the placeholder or the
    question heading; type, options, autocomplete, required, filled; tagged
    `data-kayra-field`), `fill`, `choose`, `submit`, `file`, `fileState`. Password, card, CVV,
    OTP, PIN and id fields are marked `sensitive` and refused. *Why one function:* page
    functions are serialised alone, so the helpers must live inside, and "fill" must act on
    exactly the fields "list" previewed.
  - `submit` checks `form.checkValidity()` first: a form the browser would refuse is reported
    with its missing fields, never as sent. A button that pays, buys or orders is never pressed.
  - `file` with `arm` installs a one-shot listener for a TRUSTED Space key; inside it,
    `input.click()` opens the chooser (a page may only open it during a user gesture, and
    Kayra's real key press is one). The Space itself is swallowed. Expires after 8 s.
  - `pageVideo(op, value)` — the largest visible `<video>` (speed, seek, mute on any site),
    YouTube's own buttons for skip ad, captions, next, like, subscribe, watch later; `blur`
    before a key shortcut.
  - `pageProduct()` — schema.org Product (also inside `@graph`), then Amazon / Flipkart
    markup, then Open Graph.
- **`automation/form_profile.py`** (new) — the profile: load / save (atomic, 0600), spoken
  emails, numbers and dates ("the fourth of May two thousand three"), card-shaped values
  refused, field → key by autocomplete token, input type, then label words; fields about
  someone else (father, guardian, emergency contact, nominee, company, …) never filled; the
  date in the field's own format (ISO for date inputs, dd/mm/yyyy otherwise, mm/dd when the
  field says so).
- **Parser** (`automation/common.py`) — `part_6_action`, `part_6_direct`, `_bare_part_6`,
  `price_filter`, `sort_request`. Complete shapes go rules-first; "skip 10 seconds", "how much
  is this", "sort by price" only when a video / a shop is in front (elsewhere the classifier
  decides). "google search …" and "youtube search …" are unchanged.
- **Handlers** (`automation/web_macros.py`) — `form_fill`, `form_choose`, `form_submit`,
  `page_key`, `file_upload` + `find_upload`, `video_control`, `site_search` + `shop_url`,
  `shop_refine`, `product_info`, `profile_set` / `profile_show` / `profile_forget`.
  `page_type` now also takes "my email" from the profile and falls back to a drop-down or a
  date field when there is no text box by that name.
- **Policy** (`automation/policy.py`) — `_SHOPPING_STOP` DENIES (not confirms) clicks on add to
  cart, buy now, checkout, place order, pay, the cart; also OCR clicks. CONFIRM:
  `browser.form_submit`, `browser.profile_clear`, `browser.video_subscribe`. Filling and
  uploading are late confirmations naming the fields / the file.
- **Understanding layer** (`automation/interpreter.py`) — the catalog has the new shapes;
  `_USER_ONLY`: a model's proposal can never set, forget or submit the user's details or pick a
  file to upload.
- **DMM preamble** (`intelligence/llm_engine.py`) — one short "FORMS AND VIDEOS" paragraph (the
  prompt stays one constant string, so LM Studio's prompt cache is kept).

## 3. Privacy and safety

| Concern | What happens |
|---|---|
| Profile values on disk | `data/profile.json`, mode 0600, written atomically; "forget my form profile" asks, then deletes the file |
| Profile values in logs | the set command carries the value as `text`, which the audit log reduces to its length |
| Profile values while waiting for "yes" | the pending action holds field numbers, keys and labels only; values are read again on yes (asserted) |
| Profile values to a model | never: matching is local rules; the understanding model cannot write or submit them |
| Secret fields | refused in the page and skipped by the matcher (password, card, CVV, OTP, PIN, Aadhaar, PAN) |
| Someone else's fields | father's / mother's name, guardian, emergency contact, nominee, company, … never filled |
| Submitting | always asks; an invalid form is not sent; a pay / order button is never pressed |
| Shopping | search and open only (D1); cart / buy / checkout / pay clicks are refused |

## 4. Bug fixes found on the way

| Symptom | Fix |
|---|---|
| "search Amazon for earbuds" (with the browser prefix) searched the current site for "amazon for earbuds" | site names are read before the generic page search |
| A form with an empty required field would have been reported as submitted (found by the real-Chrome test) | `checkValidity()` first; the missing field is named |
| `RunShellCommand` had an f-string without placeholders (pyflakes) | plain string |

## 5. Dependencies and configuration

None. Extension permissions unchanged. 1.1.0+ extensions reload themselves to 1.12.0.
New data file `data/profile.json`, created only when the user says "remember my …".

## 6. Files

Created: `src/kayra/automation/form_profile.py`, this file.
Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.12.0),
`src/kayra/automation/{common,web_macros,linux,policy,interpreter}.py`,
`src/kayra/intelligence/llm_engine.py`, `tests/test_browser_control.py`,
`tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`,
`CLAUDE.md`, `updates/README.md`.

## 7. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,452 passed, 0 failed (V11: 5,267).
- `tests/test_browser_control.py`: 918 passed (185 new, section [21]).
- `tests/test_browser_extension.py` (real headless Chrome): 114 passed (37 new, section [10]) —
  including a scripted Space being ignored and a real Space opening the file chooser once.
- `tests/test_dmm_browser.py` on the local model (LM Studio): 95/95 (16 new), 78 rules-first.
- pyflakes clean on every changed file.

## 8. Not verified

- No real form was filled or submitted, and no file went through the real xdg-desktop-portal
  chooser (its window title and Ctrl+L behaviour are assumed from GTK's file chooser).
- No live YouTube, Amazon or Flipkart page was used; the YouTube button selectors and the
  Amazon / Flipkart price markup were taken from earlier live checks (V9) and may drift.
- Google Forms' drop-down was checked against a fixture with the same roles, not the live site.

## 9. Upgrade notes

Nothing to do: the extension updates itself on the next connection. To use "fill this form",
tell Kayra your details once ("remember my email is …", "remember my phone number is …").
