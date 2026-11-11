# Update V18 — Kayra's voice, local models and keys, backups (Parts 14–16)

| | |
|---|---|
| **Version** | V18 |
| **Based on** | V17, commit `a9478a7` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, privacy, test hygiene |
| **Scope** | §13 N4.1, N4.2, N3.5 (Part 14); N2.2, N2.3, N2.4 (Part 15); N5.1, N2.6, N5.3 (Part 16) |
| **Not in scope** | N2.1 usage and cost, N2.5 the DMM matrix from Settings, the rest of N3, N4.3 wake word, N5.2 audit log viewer; §11 S2 |
| **Models** | none added; no extension change |

## 1. Summary

| Said / done | Before (V17) | After (V18) |
|---|---|---|
| "speak a bit faster", "speak at normal speed", "set your speaking speed to 1.3" | not understood; speed fixed at 1.1 | applied from the next sentence and kept for the next start |
| Settings → Voice | voice and recognition language | + speaking speed, "speak other languages in their own voice", a Play button to hear a voice before saving |
| Kayra says a Hindi (or Japanese / Mandarin) sentence | read by the English voice | read by a Hindi voice of the same gender (hf_alpha / hm_omega) |
| "which model is loaded" | not understood | "gemma-4-e4b is loaded. It can see images." (checked on this machine) |
| "load Qwen", "unload the local model" | not understood | done through LM Studio's `lms`; the other model is unloaded first so it fits, and that is said |
| "how fast is my local model" | not understood | "gemma-4-e4b answers a short question in 0.1 seconds when warm" (measured on this machine) |
| "check my API keys" | only Settings → Test, one at a time | one tiny request per stored key: "Claude: no credit. Groq works." |
| "back up my Kayra data" | not understood | memory, details, learned names, recipes, saved tabs, settings — never keys — in `backups/` |
| "restore my last backup" | not understood | asks, saves what is there now, then restores |
| A test suite writes into `data/` or `logs/` | caught only for a few named files | the whole run fails and names the file |

## 2. Changes by area

- **Speech** (`output/text_to_speech.py`): `clamp_speed` (0.7–1.6), `script_language` (Devanagari,
  kana, Han — Latin-script languages are not guessed), `voice_for_language`; the engine's `speed`
  (`ASSISTANT_SPEECH_RATE`), `follow_language` (`VOICE_FOLLOWS_LANGUAGE`), `voice_and_language`
  per sentence, `set_speed`, `preview` (one sentence in another voice, queued). British voices
  now read English with `en-gb`. Measured before building: Kokoro speaks hi / ja / cmn here.
- **Voice commands** (`automation/common.py`): `voice_action`, `voice_rate` — through
  `common.SPEECH["engine"]`, set by `app.bootstrap`; saved with `config.write_env_values` and
  announced by `settings_log.record`.
- **Settings / bridge / session**: the two new rows, the Play button, `apply_speech_rate`,
  `apply_follow_language`, `preview_voice`.
- **`automation/local_models.py`** (new): `models`, `loaded_report`, `load`, `unload`,
  `speed_test`, `key_health`, `boot_check`.
- **`automation/backup.py`** (new) + `core.paths.backups_dir()` (+ `.gitignore`).
- **Policy**: `backup.restore` CONFIRM; the understanding layer may not propose `models`, `voice`
  or `backup` actions.
- **`tests/run_all.py`**: `snapshot_tree` / `diff_tree` around the whole run.

## 3. Privacy and cost

| Concern | What happens |
|---|---|
| Keys in a backup | never: every name `config.is_secret` matches and every `@keyring` line is left out, and the manifest lists which |
| A malicious or damaged backup | only plain `data/` files are written; `..` and absolute paths are skipped; a non-Kayra zip is refused |
| Restoring over your current memory | asks first, and the current state becomes a backup before anything changes |
| Key checks cost money | only when you ask, or once at start with `KEY_HEALTH_ON_BOOT=on` (default off) |

## 4. Files

Created: `src/kayra/automation/local_models.py`, `src/kayra/automation/backup.py`,
`tests/test_voice_models_backup.py`, this file.
Modified: `src/kayra/output/text_to_speech.py`, `src/kayra/automation/{common,linux,interpreter,policy}.py`,
`src/kayra/ui/{bridge,session}.py`, `src/kayra/ui/views/settings.py`, `src/kayra/core/{paths,settings_log}.py`,
`src/kayra/app.py`, `tests/run_all.py`, `tests/test_ui.py` (stub methods), `tests/test_dmm_browser.py`,
`.env.example` (`KEY_HEALTH_ON_BOOT`), `.gitignore` (`backups/`), `docs/reference/DEV_WORKFLOW.md`,
`docs/platforms/LINUX_AUTOMATION.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`, `CLAUDE.md`, `AGENTS.md`,
`updates/README.md`.

## 5. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,939 passed, 0 failed (V17: 5,867); the new sweep reported no
  file changed under `data/`, `logs/`, `Reports/` or `backups/`. The same with the browser
  integration suites added (`--integration --only browser`): 1,575 passed, nothing touched.
- `tests/test_voice_models_backup.py` (new): 72.
- `tests/test_ui.py`: 701 (the Settings screen builds with the new rows).
- `tests/test_dmm_browser.py` on the local model: 145/145, 122 rules-first.
- Live, read-only, on this machine: "which model is loaded" and "how fast is my local model"
  against the running LM Studio (results in §1). Kokoro synthesized hi / ja / cmn test sentences.
- pyflakes clean on every changed file.

## 6. Not verified

- The new speed and the Hindi voice have not been heard through the app's own speaker path; the
  synthesis was checked directly with Kokoro.
- "load Qwen" was not run by voice here (it was exercised against a fake LM Studio and runner;
  the same `lms` commands were used by hand in Part 7).
- No real key was probed by these tests (a fake probe stands in).

## 7. Upgrade notes

Nothing to do. `ASSISTANT_SPEECH_RATE`, `VOICE_FOLLOWS_LANGUAGE` and `KEY_HEALTH_ON_BOOT` appear in
Settings / `.env.example`; backups go to `backups/` beside `data/`.
