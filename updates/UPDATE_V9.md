# Update V9 — Tuning pass: speed and accuracy, checked against live sites

| | |
|---|---|
| **Version** | V9 |
| **Based on** | V8 (uncommitted, on top of commit `d1d48af`) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Performance, accuracy, bug fixes found on live sites |
| **Scope** | routing (rules before the classifier), boot warm-up, extension round trips (waits inside the browser, active-tab cache), page questions (relevant text), name matching, spoken emails, Sheets maths, site lookups re-checked on live YouTube / Amazon / GitHub / DuckDuckGo / Bing / Wikipedia / Google; extension 1.8.0; a new browser-intent suite |
| **Not in scope** | streaming spoken answers (needs the turn pipeline to accept a stream from automation — planned); signed-in sites (Gmail, Docs, Calendar) — Claude in Chrome was not connected, so only public sites were checked live; the 2M live-call test |
| **Models** | the local model only (LM Studio, `google/gemma-4-e4b`); `CLOUD_FIRST=False`; no cloud call was made |

## 1. Summary (measured on the reference laptop, local model)

| What | Before | After |
|---|---|---|
| Classifying a browser / Workspace / Meet command (20-phrase benchmark) | median 466 ms, worst 580 ms (a model call each) | **< 1 ms** for 17 of 20 (rules first); median 0 ms, mean 65 ms |
| Browser-intent accuracy (new 62-case suite, full routing path) | — (live misreads: "create a new doc" → essay writer; "mute everything except this tab" → *system mute + close all but pinned*) | **62/62**, 50 of them without a model |
| "what does this page say about refunds" (long page) | 1.11 s (12 K characters sent) | 0.74 s, ~1 K characters, answer kept (a blind 3 K cut answered wrongly) |
| First understanding-layer / classifier call after boot | 1.3 s | warmed in the background at boot (both prompts) |
| File list, comments, results, next event | one extension round trip per 250 ms poll | ONE call that waits inside the browser (extension 1.8.0) |
| "Which tab is active?" per command | asked up to 3–4 times | once per command (cached until something changes it) |
| 53-case DMM matrix | 50/53 | 51/53 (same model; the classifier is unchanged) |

## 2. Changes by area

**Rules before the classifier** (`common.direct_tokens`, `llm_engine.RULE_ROUTER` /
`set_rule_router`, wired in `app.bootstrap` on Linux). A whole utterance that the browser
parser fully understands AND whose action is on an allow-list (`_DIRECT_BROWSER`) skips the
model; so do bare Meet share / admit, Gmail search / compose, and next-meeting phrasings.
Context-sensitive shapes look at the screen only when needed: Sheets maths only with a Sheet in
front ("what's the total of 25 and 30" stays a question), slide steps only with Slides in
front, "close it" only while Kayra remembers a tab. Open-ended shapes ("search for X", a bare
"highlight X", "open the report file") still go to the model. A failing router never costs the
turn. The intelligence layer still imports nothing from automation: it calls what it is handed.

**Boot warm-up** (`ai.warm_up`, `interpreter.system_prompt`, `app.bootstrap`). Two daemon
threads load the classifier's and the understanding layer's prompts into LM Studio's cache.
Measured first: alternating the two prompts costs nothing (LM Studio keeps both), so per-site
catalog trimming was REJECTED — a prompt that changes with the site would break that cache and
be slower.

**Extension round trips** (`web_macros._ready`, `new_command`, the `_READ_ONLY` cache;
extension `untilReady`). From 1.8.0 `page.docList`, `page.comments`, `page.results`,
`calendar.next` and `page.transcript` take `waitMs` and wait inside the browser; older
extensions keep the polling. `tabs.active` is answered from a per-command cache, cleared by any
call that can change it and at the start of every command. The Sheets/Docs file list reuses a
tab already on a file list instead of opening a new tab per search (never navigating away from
an open file).

**Relevant text** (`ai.relevant`): sentence-aligned chunks scored by the question's 5-letter
stems, original order kept, the opening chunk always kept; used by page / doc questions.

**Names, emails, Sheets** (`web_macros.spoken_numbers`, `_same_word`, `rank_titles`;
`common.spoken_email`; `web_macros._split_header`, the `where` filter). "budjet twenty twenty
six" finds Budget 2026; "team rooster" finds Team roster; "ana at x dot com" becomes
`ana@x.com`; a title row above a sheet's table is skipped; "total of amount where status is
paid" filters.

**Site lookups, re-checked live** (extension 1.8.0, `pageResults` / `pageComments` /
`pageTranscript`), in Kayra's built-in browser (not signed in):

| Site | Found | Fix |
|---|---|---|
| Google search | the automated browser got Google's "unusual traffic" page; the generic fallback offered **"Why did this happen?" as the first result** | bot-check pages are reported as `blocked` and said so; nothing is opened |
| YouTube search | 19 results, but the page took ~7 s to fill | the results wait is 6 s (ends the moment they appear) |
| YouTube video page | Up next is now `yt-lockup-view-model` cards (20), and the old selector instead caught a 4-link shelf whose first title was "1:03"; card 1 was an **ad** | on /watch the cards come first, titled from `<h3>`, bare durations and `ytd-ad-slot-renderer` excluded |
| YouTube transcript | four "Show transcript" matches, two hidden; the code clicked a wrapper | the visible `<button>` is pressed. The panel itself showed no text in the unsigned browser, and the caption files need a token only YouTube's player makes — not worked around; the description fallback stays |
| YouTube comments | 10 found once scrolled to | unchanged (works) |
| Amazon search | the first two results were **sponsored** (`/sspa/`) | sponsored listings skipped |
| Amazon reviews | review text is now `data-hook="reviewText"` (`review-body` is gone); loads only when scrolled to | new selector + scroll — "summarize the reviews" would otherwise have said there were none |
| Bing | `#b_results h2 a` counted 12, only 10 organic | `li.b_algo` only |
| Wikipedia search | the generic fallback took "create a draft and submit it for review" as result 2 | `.mw-search-result-heading`; generic pages prefer heading links when there are 3+ |
| GitHub, DuckDuckGo | correct | — |
| Reddit | not allowed in the built-in browser | not checked |

## 3. Bugs fixed

All in the "Site lookups" table above, plus: the live classifier read "create a new doc" as the
essay writer and "mute everything except this tab" as system mute + close all but pinned — both
now go rules-first.

## 4. Dependencies and configuration

No new packages or `.env` keys. Chrome extension **1.8.0** (no new permission). New suite
`tests/test_dmm_browser.py` (integration; local model only — it skips itself otherwise).

## 5. Files

Created: `tests/test_dmm_browser.py`, this file. Modified: `automation/common.py`,
`automation/web_macros.py`, `automation/ai.py`, `automation/interpreter.py`,
`automation/linux.py`, `intelligence/llm_engine.py`, `app.py`,
`extension/kayra-bridge/background.js`, `extension/kayra-bridge/manifest.json`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/run_all.py`, docs.
Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **5,101 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_control.py`: 564 checks, new section [18].
- `tests/test_browser_extension.py` (real headless Chrome): **51 passed**, new section [6] with the
  markup seen live (Amazon sponsored + reviewText, YouTube lockups + ad + hidden buttons, Bing
  organic, Wikipedia headings, generic heading links, bot-check page).
- `tests/test_dmm_browser.py` (local model): **62/62**, 50 rules-first.
- `tests/test_dmm_matrix.py` (local model): 51/53.
- Live checks: Kayra's page functions run by hand in the built-in browser on YouTube (search,
  a video page), Amazon (search, a product page), GitHub, DuckDuckGo, Bing, Wikipedia, Google
  (blocked). Read-only: nothing was sent, bought, posted, signed into or deleted; a playing
  video was muted and paused.

## 7. Known limitations / not verified

- **Signed-in sites were not checked live** (Gmail, Docs/Sheets lists, Drive, Calendar, Keep,
  Classroom): Claude in Chrome was not connected, and the built-in browser is not signed in.
  Those selectors remain verified against fixtures only.
- **Streaming spoken answers** (summaries spoken as they are written) was not done: the
  automation layer returns one finished sentence to the turn, and changing that touches the
  voice loop. Planned as the next tuning item.
- YouTube transcripts could not be confirmed in an unsigned browser (see the table).
- A bare "close it" with nothing remembered still goes to the classifier, which answers
  inconsistently ("close tab" / "close it"); an unresolved "close it" asks before acting.

## 8. Upgrade notes

1. Restart Kayra; the extension reloads itself to 1.8.0.
2. Nothing else: the rule route and warm-up start with Kayra. `DMM_FAST_PATH=off` in `.env`
   still turns off both the exact-command fast path and the rules-first route.
