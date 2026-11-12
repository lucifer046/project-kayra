# Update V14 — open Chrome straight into a profile (Part 5P)

| | |
|---|---|
| **Version** | V14 |
| **Based on** | V13, commit `b3c6c3e` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, privacy |
| **Scope** | BROWSER_CONTROL_PLAN §14: P1 (read profiles from Chrome), P2 (say it any way), P3 (open it), P5 (tests) |
| **Not in scope** | P4 (the Kayra Bridge in more than one profile), "always open Chrome as X" (a Settings control), spoken aliases (H2) |
| **Models** | none; the DMM prompt is unchanged |

## 1. Summary

| Said | Before (V13) | After (V14) |
|---|---|---|
| "open my Work Chrome" | "I couldn't find an app called my work chrome" | Chrome opens straight into the profile named Work: "Opened Chrome as Work (account name)." |
| "open Chrome as Kiran", "open Kiran's Chrome" | Chrome's *Who's using Chrome?* picker, or an app lookup | that profile, found by its name or its account's name |
| "open Chrome on ana at school dot edu" | not understood | the profile signed in to that address |
| "open Chrome with my school account" (three profiles on that domain) | not understood | "Which one: Work (…), Study (…) or Study 2 (…)?" |
| "open Gmail in my Study Chrome" | Gmail in whatever Chrome was open | Gmail inside the Study profile |
| "switch to my study two profile", "open the second Chrome profile" | not understood | "Study 2"; the second profile in Chrome's order |
| "which Chrome profiles do I have" | not understood | "You have 5 Chrome profiles: …" |
| "open Chrome as a guest" | not understood | a guest window (`--guest`) |
| "open chrome", "open youtube in chrome", "open chrome in incognito" | — | unchanged |

## 2. Changes by area

- **`kayra.platforms`** — `browser_user_data_dirs()` in `linux` (XDG config: google-chrome,
  chromium, BraveSoftware/Brave-Browser, microsoft-edge), `windows` (%LOCALAPPDATA% … User
  Data) and `fallback` ([]), exposed by the façade. *Why there:* where an OS keeps a browser's
  data is an OS question; shared modules never guess paths.
- **`automation/chrome_profiles.py`** (new) — `load()` parses `profile.info_cache` (folder,
  name, account name, first name, email, hosted domain, default-name flag) in Chrome's order,
  cached on (mtime, size), so a rename in Chrome is seen at the next request; `resolve()` decides
  in tiers — profile name (spoken numbers: "study two" = "Study 2") > email or its prefix >
  account name > domain (whole, or any label but the TLD) > position ("the second", "profile
  3", "person 2") > a loose match (`semantic_match`). Several equal answers -> ONE question that
  names each profile with its account. `open_profile`, `list_profiles`, `open_guest`.
- **Parser** (`automation/common.py`) — `profile_action`: the shapes only. Which profile a name
  means is decided when the command RUNS, so no parse (and no test of another feature) ever
  reads the host's profiles. A site's name after "with" / "on" stays the site ("open chrome with
  youtube"); after "my" / "as" / "'s" it is a profile name.
- **Linux backend** (`automation/linux.py`) — `<binary> --profile-directory=<folder> [url]`
  through `RUNNER` (argv, no shell); the new browser window is focused.
- **Understanding layer** — the catalog lists the profile shapes.

## 3. Privacy

| Concern | What happens |
|---|---|
| Profile names, emails, domains | read from Chrome's file on this computer at the moment they are needed; never stored by Kayra, never sent to a model, never logged in full |
| The repository | `tests/test_profile_privacy_guard.py` (integration) fails if any email, email prefix, hosted domain or account name of this computer's profiles appears in a tracked file; it prints only the file and the kind of value |
| What the classifier sees | only the user's words |

## 4. Found on the way

The guard's first run flagged `automation/sites.py`: a public university portal address in the
site table (Part 1) shares a hosted domain with the user's accounts. A public web address is not
account data, so domains inside `https://…` are allowed; anywhere else they still fail.

## 5. Dependencies and configuration

None. No extension change.

## 6. Files

Created: `src/kayra/automation/chrome_profiles.py`, `tests/test_chrome_profiles.py`,
`tests/test_profile_privacy_guard.py`, this file.
Modified: `src/kayra/platforms/{__init__,linux,windows,fallback}.py`,
`src/kayra/automation/{common,linux,interpreter}.py`, `tests/run_all.py`,
`tests/test_dmm_browser.py`, `docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`,
`docs/roadmap/BROWSER_CONTROL_PLAN.md`, `CLAUDE.md`, `updates/README.md`.

## 7. Verification

Linux (Omarchy), the project venv.

- `tests/run_all.py` (unit tier): 5,659 passed, 0 failed (V13: 5,559).
- `tests/test_chrome_profiles.py`: 100 passed — made-up Local State with renamed, never-renamed,
  account-less and same-domain profiles; a rename between requests; a corrupt file; argv recorded.
- `tests/test_profile_privacy_guard.py` (reads this computer's profiles in memory): passed.
- `tests/test_dmm_browser.py` on the local model: 115/115, 95 rules-first.
- On this computer's real `Local State` (5 profiles; only counts and booleans printed): every
  profile resolves by its name, its email and its email prefix; a domain shared by 2 and by 3
  profiles asks between exactly those.
- pyflakes clean on every changed file.

## 8. Not verified

- No profile was opened by voice on the live machine; the launch was checked with the recorded
  argv. Chrome's behaviour when already running (a new window in that profile) is Chrome's own.
- Windows: the platform function exists, but the Windows automation backend does not dispatch
  these actions yet.

## 9. Upgrade notes

Nothing to do. Rename your profiles in Chrome if you want to call them something else — Kayra
reads the new names next time.
