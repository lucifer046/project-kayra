# Configuration

All runtime configuration lives in **`.env`** at the repository root, created from
**`.env.example`** by `setup.py`. `.env.example` is the authoritative, annotated reference —
every key, its default, its clamp range and the measurement behind it. This page is the map.

`core/config.py` parses `.env` once and exports it into `os.environ`; values already in `.env`
take precedence over the process environment (a few diagnostics, such as `KAYRA_LOG_LEVEL`,
are read from the process environment first so one run can be made verbose without editing).

## Groups

| Prefix / key | Controls | Live? |
|---|---|---|
| `ASSISTANT_NAME`, `ASSISTANT_GENDER`, `USERNAME`, `USER_GENDER`, `LANGUAGE` | identity in prompts and speech | next start |
| `INPUT_LANGUAGE` | recognizer language (e.g. `hi-IN`, `en-US`) | next start |
| `ASSISTANT_VOICE` | Kokoro voice | next start |
| `TTS_DEVICE_MODE` | `AUTO` / `GPU` / `CPU` for speech synthesis | **live** (Settings) |
| `STT_BROWSER` | `auto` / `chrome` / `edge` / … | **live** (Settings) |
| `FORCE_ONLINE`, `LOCAL_*` | local LLM server vs cloud | next start |
| `CLAUDE_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY` | cloud providers (Claude → Groq → Gemini, intent and chat). Set them in Settings → Cloud models: stored in the system keyring (`.env` then says `@keyring`), Test and Remove per provider | immediately |
| `CLOUD_FIRST` | prefer the cloud keys while a local server runs | next start |
| `CLAUDE_MODEL`, `GROQ_CHAT_MODEL`, `GROQ_DECISION_MODEL`, `GEMINI_CHAT_MODEL` | model ids | immediately |
| `PROVIDER_COOLDOWN_*`, `PROVIDER_TIMEOUT_SECONDS` | router stand-down and request timeout (clamped) | next start |
| `PROACTIVE_*` | proactive agent and presence: switch, tick, cooldowns, threshold, late-night window, habit caps, phrasing | switch is live |
| `AUTOMATION_*` | confirmation TTL, shell timeout, screenshot retention, timer cap (Windows) | next start |
| `MAX_SUB_QUESTIONS`, `MAX_FOLLOWUP_QUERIES`, `MAX_DEEP_PAGES`, `SEARCH_RESULTS_PER_QUERY` | deep research | next start |
| `GESTURE_*` | gesture autostart, camera format/backend, detector, accelerator, dials, thresholds | switches are live |
| `KAYRA_LOG_LEVEL`, `KAYRA_LOG_FILE` | log threshold, optional rotating debug log | next start |
| `KAYRA_REDUCED_MOTION` | force reduced motion on/off (else the OS setting via `kayra.platforms`) | next start |

There is deliberately **no** key that disables the safety policy, confirmations, provider
cooldowns or the fallback order — only bounds.

## Platform notes

- No Linux-only keys are required. Session detection uses the standard XDG/Wayland variables.
- `GESTURE_CAMERA_BACKEND`: `AUTO` everywhere; `MSMF`/`DSHOW` are Windows names,
  `V4L2`/`GSTREAMER` Linux names; a name for the other OS falls back to `AUTO`.
- `STT_BROWSER=chrome` is the explicit choice on Linux; `auto` already prefers Google Chrome
  over Omarchy's default Chromium.
- `.env` is copied between machines safely: no key holds an OS-specific path.

## Files written at runtime

`data/conversation.json` (+ `_backup`), `data/habits.json`, `data/browser_support.json`,
`data/kayra.lock`, `Reports/*.md`, `logs/` (only with `KAYRA_LOG_FILE`). All gitignored.
