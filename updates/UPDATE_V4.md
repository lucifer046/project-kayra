# Update V4 — Claude-first cloud models, keys in the keyring, a redesigned and GPU-composited UI, browser tabs (Part 2a/2b)

| | |
|---|---|
| **Version** | V4 |
| **Based on** | commit `283611b` (V3) on `feature/linux-desktop-automation` |
| **Date** | 2026-11-10 |
| **Type** | Major feature, bug fixes, security (key storage), performance (UI rendering) |
| **Scope** | cloud model routing, provider failure handling, API key storage, Settings / System / dialogs, responsive layout, GPU rendering, Chrome extension 1.3.0, browser tab and page commands |
| **Not in scope** | Windows automation; smooth/GPU scrolling (planned as `BROWSER_CONTROL_PLAN.md` §11); browser Part 2 items B4 tab groups, B5 windows, C3 "open the third result", C4 find-with-highlight |

## 1. Summary

| Area | Before (V3) | After (V4) |
|---|---|---|
| Cloud models | Cohere (DMM) → Groq → Gemini; Groq → Gemini (chat) | **Claude Haiku 5.5 → Groq → Gemini** for both; Cohere removed; any one key serves everything |
| A retired model / plan with no access | reported as "every model is rate-limited" | HTTP status decides: "cannot serve the configured model", "has no API credit" |
| One provider's 400 | stopped the chain ("no model could answer") | falls through to the next provider |
| LM Studio running | ignored when Settings had saved a blank endpoint; decided once at boot | used whenever it runs (re-checked live); only "Use cloud models even when LM Studio is running" overrides |
| API keys | typed into `.env` (world-readable) | Settings → Cloud models: system keyring (`.env` holds `@keyring`), Test / Remove, `.env` 0600 |
| Settings | 11 cards in one scroll, free-text model ids, Voice field ignored | 5 sections, pinned Save bar, model and voice dropdowns, live status tiles |
| Window | refused to be narrower than 1040 px | 480×520 minimum: compact rail, Home 3/2/1 columns, stacking rows |
| Confirmations | stock `QMessageBox` (separate floating window) | in-window `ConfirmDialog`, fitted to the window |
| UI paint work (Home, 6 s, live) | ~190 ms/s, backdrop 8 fps | ~82 ms/s, backdrop 30 fps on the GPU |
| Dock repaint | 6.98 ms | 2.16 ms |
| Browser tabs | by name only | by position, duplicates, left/right, mute the rest, recently closed, sleep, find by topic, zoom, hard reload, copy link/title |

## 2. Changes by area

**Cloud models** (`intelligence/provider_router.py`, `intelligence/llm_engine.py`). Claude via the
official `anthropic` SDK (lazy import); `claude_speed_args` picks thinking/effort per model and no
sampling parameter is sent; the DMM's few-shot prefix is prompt-cached on Claude. Groq uses
`gpt-oss-20b` for commands and `gpt-oss-120b` for chat; Gemini uses `gemini-flash-latest`.
`classify_failure` reads the HTTP status first; `is_no_credit()` turns Anthropic's 400 "credit
balance is too low" into an account problem; `INVALID_REQUEST` falls through with a 60 s
cooldown. `reload_providers()` applies saved keys and models without a restart and clears only
the changed provider's cooldown. `_sync_mode()` follows LM Studio live (≤ every 10 s);
`_setting()` treats blank `.env` values as unset. Vision: local → Claude → Gemini → Groq.

**Key storage** (`core/credentials.py`, `core/config.py`). `save_secret()` stores keys in the
Secret Service / Credential Manager and writes `NAME=@keyring`, resolved inside `env_values()`;
without a keyring the key goes to `.env`. Every writer (`write_env_values`, `setup.py`) sets
`.env` to 0600.

**Settings, System, dialogs** (`ui/views/settings.py`, `ui/views/system.py`,
`ui/components/confirm.py`, `primitives.FactGrid`). Sections (General · Voice · AI models ·
Gestures · Proactive); provider panels with Test (one tiny request, fills the model list with
what the key can use) and Remove; voice dropdown from `voices.bin`, applied on Save (the setting
was never read before); languages by name; defaults as placeholders; one local/cloud switch
plus "Answering now". System → Device is a tile grid; same-filesystem mounts share one Storage
tile.

**Responsive layout** (`ui/application.py`, `navigation.Sidebar.set_compact`, `views/home.py`,
`SettingRow`, `views/chat.py`, `views/base.py`). Breakpoints by window width (rail) and by each
page's own width (Home, settings rows, System tiles, page margins).

**GPU rendering** (`ui/gpu.py`, `components/backdrop.py`, `components/shadow.py`,
`ui/profile.py`). The backdrop is an OpenGL widget (MSAA, vsync, 30 fps), which also moves the
window's composition to the GPU; cached `ShadowLayer` replaces `QGraphicsDropShadowEffect` on
the dock and drawer; `KAYRA_UI_GPU=off`, automatic CPU fallback (headless, no context, software
GL); `KAYRA_UI_PROFILE=1` logs paint times. X11 and Wayland alike.

**Browser control, Part 2a + 2b** (`automation/web_macros.py`, `automation/common.py`,
`automation/linux.py`, `extension/kayra-bridge` 1.3.0). See `docs/platforms/CHROME_BRIDGE.md`
"What you can say" and `BROWSER_CONTROL_PLAN.md` §10.

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| Text classifier matched "429"/"quota" first | retired models and no-access plans read as rate limits | status code first, no bare numbers |
| A 400 stopped the provider chain | "No model could answer" with fallbacks unasked | INVALID_REQUEST falls through |
| Anthropic no-credit 400 | "Claude refused the request" | "Claude has no API credit on its account" + a fix |
| `LOCAL_BASE_URL=` (blank, written by Settings) | LM Studio ignored, everything went to the cloud | blank = default |
| Local/cloud decided once at boot | LM Studio started later was never used | `_sync_mode` |
| Saved keys needed a restart; old key's 900 s cooldown persisted | a corrected key looked broken | `reload_providers` |
| `ASSISTANT_VOICE` never read | choosing a voice did nothing | read by TTS, applied live |
| Window minimum 1040 px | cut off in tiled/half-screen windows | 480×520, responsive |
| Chat column pinned with `setFixedWidth` in `resizeEvent` | overflowed while shrinking | capped widget between stretches |
| A UI test drove Save outside `NoEnvWrites` | created a `.env` of stub values (deleted) | guarded; the suite now fails if it creates a `.env` |

## 4. Dependencies and configuration

- Added: `anthropic>=2.0` (verified 2.7.0), `keyring>=25` (verified 25.7.0). Removed: `cohere`.
- New `.env`: `CLAUDE_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_DECISION_MODEL`, `CLAUDE_EFFORT`,
  `CLOUD_FIRST`, `GROQ_DECISION_MODEL`, `GEMINI_DECISION_MODEL`, `VISION_CLAUDE_MODEL`;
  environment `KAYRA_UI_GPU`, `KAYRA_UI_PROFILE`, `KAYRA_KEYRING`. `CohereAPIKey` is no longer read.
- Chrome extension **1.3.0** (1.2.2: `tabs.close {tabIds}`; 1.3.0: `tabs.recentlyClosed`,
  `tabs.restore {sessionId}`, `tabs.muteOthers`, `tabs.discard`, `tabs.zoom`,
  `tabs.reload {hard}`). 1.1.0+ extensions reload themselves.

## 5. Files

Created: `src/kayra/core/credentials.py`, `src/kayra/ui/gpu.py`, `src/kayra/ui/profile.py`,
`src/kayra/ui/components/confirm.py`, `src/kayra/ui/components/shadow.py`,
`tests/test_credentials.py`, this file. Modified: 58 files (intelligence, core config/logging,
UI views/components/theme, automation web/common/linux/policy/interpreter, the extension,
`setup.py`, `requirements.txt`, `.env.example`, docs, tests). Deleted: none.

## 6. Verification

- `tests/run_all.py` (unit tier): **4,760 passed, 0 failed, 4 skipped** (Linux, Python 3.14.8).
- `tests/test_browser_extension.py` (real headless Chrome): 27 passed.
- Live on the reference laptop (Omarchy, Wayland, AMD Radeon 780M + RTX 4060): GPU probe chose
  the Radeon; paint profile CPU vs GPU as in §1; Claude key probed (reported "no API credit");
  engine picked LM Studio with the real `.env`.
- Screenshots of Settings, System, Home and the dialog at 1400, 760, 600 and 480–520 px.

## 7. Known limitations / not verified

- **The new extension commands (1.2.2/1.3.0) were not run against a live Chrome** — the real-
  Chrome suite exercises in-page functions only, and Kayra was not running with the extension
  connected. Their Python side is unit-tested against a fake bridge.
- No real Claude reply was seen (the account has no API credit); the DMM accuracy matrix was not
  re-run against Haiku.
- Scrolling still repaints on the CPU (plan §11).
- **Google Meet screen sharing does not work yet**: only "entire screen" exists, untested live;
  a live attempt misrouted four ways (fake meeting code, plain click, focus-away + whole
  monitor, "start sharing" read as a new meeting). Diagnosis and the fix plan, plus sharing a
  named window and the meeting features around it, are `BROWSER_CONTROL_PLAN.md` §12 (part 2M). Other neighbouring features found during V4
  (tab sessions, reading list, usage/cost, model speed test, command palette, …) are §13.
- Nothing in V4 is committed yet.

## 8. Upgrade notes

1. `python setup.py` (installs `anthropic` and `keyring`; `.env` becomes 0600).
2. Settings → AI models: paste keys (they move to the keyring), press Test, pick models.
3. Restart Kayra once; the Chrome extension reloads itself to 1.3.0 when it connects.
