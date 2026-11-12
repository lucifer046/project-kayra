# Update V19 — Hearing the names on screen

| | |
|---|---|
| **Version** | V19 |
| **Based on** | V18, commit `36d97f5` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Fix (speech recognition) |
| **Scope** | §13 N4.4 |
| **Not in scope** | N4.5 local Whisper with hot words; the other issues from the 2026-11-11 audit log review |
| **Models** | none; no extension change |

## 1. Summary

The audit log of 2026-11-11 showed many commands failing because a name was misheard:
"make her sharma" (Megha Sharma IITM), "sand button" (Send), "punchayat", "joint" (Join).
The recognizer was set to US English and never knew which names were on screen.

| Said | Heard | Before (V18) | After (V19) |
|---|---|---|---|
| "click the send button" | "click sand button" | chat "sand button" not found | `click Send button` — and the Send click still asks first |
| "open Megha Sharma IITM chat" | "open make her sharma iitm chat" | "which one?" between two wrong chats | `open Megha Sharma IITM chat` |
| "open General Panchayat" | "open general punchayat" | not found | `open General Panchayat` |
| "open Safe House" | top "open sapphire house", 2nd "open safe house" | not found | the recognizer's own 2nd reading is used |
| "tell me about sand dunes", "search for sand", "send hello to raju" | — | unchanged | unchanged (no name slot, or nothing on screen sounds like it) |

## 2. Changes

- **`.env`: `INPUT_LANGUAGE=en-IN`** (was `en-US`) on the reference machine. Settings → Voice
  → Recognition language already offers "English (India)".
- **`automation/screen_vocabulary.py`** (new): `names()` — WhatsApp chat names, the file
  titles of a Docs/Sheets/Drive list, the active tab's buttons. 0.35 s per call, `[]`
  without the bridge, never raises, names never leave the machine.
- **`input/transcript_repair.py`**: `set_screen_source()`; rule 1 prefers an alternative that
  names something on screen; new rule 2a replaces a misheard NAME SLOT of a command with the
  single on-screen name it sounds like (`sound_skeleton`, `sounds_like`). Every guard is a
  named constant at the top of the module; see `docs/reference/VOICE_LOOP.md` → Names on
  screen. Still no word-replacement table, no model, no automation import.
- **`app.py`**: `_repair_stage()` injects `screen_vocabulary.names`.

Every change still prints `[TRANSCRIPT] 'x' -> 'y' (screen:0.90)` on the console.

## 3. Tests

`tests/test_capture_pipeline.py::section_screen_names` (+25 checks): the five log
mishearings, eleven sentences that must be left alone, confidence gate, alternative
preference, one screen read per TTL, a failing source, the reader against a fake extension,
the app wiring. Unit tier: 5964 checks, 0 failed.

## 4. Known limits

- WhatsApp only lists the chats in view. A misheard name whose real chat is further down is
  not corrected (the downstream WhatsApp search still runs); a different first letter is
  never corrected, so "Central" is not turned into a visible "General".
- A name said with only half its sounds ("make her sharma" with both Megha Sharma and
  Himanshu Sharma on screen) is left alone: two names fit equally.
- Not tried live yet. After a week, count `[TRANSCRIPT]` lines and remaining NOT_FOUND names
  in the audit log to decide on N4.5 (local Whisper with hot words).
