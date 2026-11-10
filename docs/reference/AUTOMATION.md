# The automation layer

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

## The automation layer (rewired 2026-09-07)

`src/kayra/automation/windows.py` used to be one ordered `if/elif` chain matching on
`cmd_lower.startswith(...)`, with every layer re-reading the user's raw sentence. It is now a
pipeline, and the old chain is gone:

```
DMM token
  -> normalize_command()   Action{domain, action, target, parameters, confidence}
  -> classify_action()     ALLOW | CONFIRM | DENY
  -> resolve_*()           RESOLVED | AMBIGUOUS | NOT_FOUND | UNAVAILABLE
  -> plan_actions()        dependency-ordered groups
  -> execute_action()      handler + verification
  -> ActionResult.message  the sentence main.py speaks
```

**Backward compatibility is total.** Every handler that existed before is still exported with
its original name and signature (`OpenApp`, `CloseApp`, `WindowManage`, `MediaControl`,
`HotkeyShortcut`, `SystemInfo`, `SetTimer`, `TakeScreenshot`, `Clipboard*`, `ExecuteCommand`,
`ToggleWifi`, `WebSearch`, `Content`, `YoutubeSearch`, `PlayYoutube`, `global_desktop_type`,
`translate_and_execute`, `Automation`). `tests/test_automation.py` asserts the list.
`Automation()` now RETURNS the spoken sentence instead of `True`; a non-empty string is still
truthy, so truthiness checks are unaffected.

### Prefix collisions are now structural, not positional

The old chain depended on `elif` ORDER: `close window` and `close tab` had to be tested before
the generic `close ` prefix or they were routed to `CloseApp("window")`. That constraint is
gone. `_EXACT_TOKENS` is a dict consulted before `_PREFIX_TOKENS`, and the prefix list is
sorted longest-first at import. A dict cannot be reordered by accident and a longest-first list
cannot be shadowed, so the class of bug is eliminated rather than documented.

Still true: if you add a literal token, add it to `_EXACT_TOKENS`, and confirm
`tests/test_automation.py`'s normalizer table covers it. Every token in `llm_engine.funcs` must
normalize to something — the suite asserts zero dead tokens.

### Opening a target: application vs website (fixed 2026-09-08)

**"Open YouTube" used to open File Explorer, and "open GitHub" used to launch Git GUI.** Both
came from the same place: every non-URL target went to `AppOpener.open(..., match_closest=True)`,
whose launcher is `os.system("explorer shell:appsFolder\\<id>")` over a CACHED Start-Menu index
with a `difflib` fuzzy fallback at cutoff 0.6. Reproduced on this machine — the index held a
STALE `youtube` entry pointing at a Brave PWA AppsFolder id that no longer exists, and
**explorer answers a dead id by opening a plain File Explorer window** rather than failing;
`github` was absent entirely, so difflib matched "git gui". Neither raised, so the assistant
said it had opened YouTube. The `except` fallback was a DuckDuckGo `!ducky` redirect, which
routed a plain website open through a search engine.

`targets.resolve_open_target()` now decides the target's TYPE first, and each type has exactly
one execution path: explicit URL -> installed application -> canonical website -> curated
app (unconfirmed install) -> exact Start-Menu entry -> named user folder or literal path ->
NOT_FOUND. The folder step is last on purpose ("open Chrome" must never be answered by a
folder) and uses `resolve_path`, which checks the well-known folders by NAME and the path as
given — it never walks the disk.

- **There is no step that searches the machine for a name resembling X and runs it.** A miss is
  reported. `match_closest=True` appears at no call site (AST-asserted by
  `tests/test_target_resolution.py`), and the `!ducky` fallback is gone.
- **`WEBSITE_REGISTRY`** in `targets.py` is the canonical service table (URL + aliases + the
  browser-title fragments used for closing). One line per service. `website_url()` is one hash
  probe — measured **1.7us** — so nothing calls an LLM or the network to learn that YouTube is
  a website. `SITE_HINTS` is now derived from it and keeps its old shape; the legacy contract
  that "spotify" is NOT a site key (the desktop app owns that name; the web player is "spotify
  web") is pinned by `tests/test_automation.py`.
- **`application_available()`** answers "is X installed?" from metadata that already exists: a
  running window, the Windows *App Paths* registry key, then the Start-Menu index **on exact
  names only**. Cached 300s — 0.12ms cold, 1.2us warm. It never walks the disk, and index
  entries with a falsy id are dropped (that empty id is what produced the File Explorer
  window).
- **Application beats website** for a curated app name, so "open Chrome" is never a web page. A
  curated app that will not launch but has a web version (Spotify, WhatsApp) falls back to the
  web player and says so out loud.
- **The type reaches the structured action.** `normalize_command("open youtube")` now yields
  `browser.open_url` with the URL already in `parameters`, not `app.open`. Two dict lookups,
  ~8us total.
- **"Open YouTube" is not "search for YouTube".** Opening is `browser.open_url`; `google
  search X` / `youtube search X` / `realtime X` are unchanged and still distinct.

### Closing a target: one intent, one target (fixed 2026-09-08)

**"Close X" could close several windows.** `resolve_application` returns EVERY window of an
application and the executor looped `WM_CLOSE` over all of them — one sentence, four windows
gone, no question asked. A missed site lookup also fell through into a LOOSE application lookup
matching the name anywhere in a window title, so "close YouTube" could reach a VS Code window
editing `youtube-dl` notes.

- **`targets.pick_single()` is the single-target guarantee.** It narrows a multi-candidate
  resolution to exactly one, tie-broken by the foreground window and only when the foreground
  really is one of the candidates; otherwise AMBIGUOUS and the assistant asks. Nothing on the
  close path iterates a match set any more. It also re-filters Kayra-owned windows, so a match
  set that somehow contained one still cannot be acted on.
- **Strictness scales with risk.** `resolve_application(name, strict=True)` is used for every
  close: for a name Kayra does not curate, only exact process identity counts, never a title
  substring. The loose reading is kept for FOCUS, where a near-miss only brings up the wrong
  window. Do not use the loose reading for anything destructive.
- **A pure site name that is not on screen is a MISS.** It is not an invitation to look for a
  process of that name. Only a name that is ALSO a real application falls through to the app
  branch.
- **Breadth is explicit, never inferred.** `close all <app>` -> `app.close_all`;
  `close everything` / `close all windows` -> `window.close_all`, which is CONFIRM-gated. A
  plain "close chrome" cannot widen into either. Both are DMM tokens the user has to actually
  say.
- **The semantics stay distinct:** "close this"/"close this window" -> the exact foreground
  HWND; "close this tab" -> Ctrl+W to a browser that is ALREADY in front; "close YouTube" ->
  the one browser window showing it; "close Chrome" -> one window; "close that" -> the bounded
  context referent, or the foreground window when nothing fresh applies.
- **Last-tab behaviour is the browser's consequence, not a second close.** Ctrl+W on a
  single-tab window makes the browser close that window. The guarantee Kayra owes is one
  keystroke to one focused window, and that is what the test suite pins.

### Three real dangers that were removed

* **`taskkill /f /im <name>.exe`.** `CloseApp`'s final fallback force-killed every process of a
  name. For "close chrome" that is the user's entire browsing session *and* Kayra's own STT
  Chrome, which would take the microphone down with it. There is now no path in the automation
  stack that terminates a process by name; `tests/test_automation.py` walks the AST to prove it.
  Closing is `WM_CLOSE` to a resolved window — what clicking the X does. `force_close_app()`
  exists for the genuine force case: PID-scoped, protected-process filtered, CONFIRM-gated.
* **The substring sweep.** `CloseApp` posted `WM_CLOSE` to *every* visible window whose title
  contained the requested string. Matches are now scored (`_score_window`) and ranked, and a
  near-tie returns AMBIGUOUS so the assistant asks.
* **Instant shutdown/restart.** `ExecuteCommand` ran `os.system("shutdown /s /t 0")` the moment
  a substring matched — a misheard word could power the machine off mid-sentence. Both are
  CONFIRM actions now, and `ExecuteCommand`'s branch is deliberately inert so the gate cannot
  be routed around.

### Safety policy (`src/kayra/automation/policy.py`)

One place decides whether anything may run. `classify_action` for structured actions,
`classify_shell` for terminal commands; both return ALLOW / CONFIRM / DENY plus a reason that
becomes the spoken refusal.

`classify_shell` PARSES the command rather than string-matching it: shell metacharacters
(chaining, piping, redirection, substitution) are refused outright because every later check
reasons about one command; then the executable stem is checked (path- and extension-insensitive,
so `C:\Windows\System32\format.exe` is `format`); then destructive verbs are judged by SCOPE
against `_is_protected_path`. Nested interpreters (`powershell`, `cmd`, `wscript`) are denied as
shell targets because a nested shell defeats every check above it. Unrecognised executables are
CONFIRM, not ALLOW.

`_is_protected_path` distinguishes `_PROTECTED_TREES` (the Windows directory and everything
under it) from `_PROTECTED_EXACT` (a drive root, the Users folder — protected as a TARGET only).
Conflating them is not a safe default in the direction it looks: an earlier version protected
everything on C:, which blocked every legitimate file operation the user has. Caught by the test
suite, not by reading it.

**Kayra's own first-party PowerShell calls are not governed by this policy** — they are fixed
argument vectors with no user text in them (`_powershell`, the brightness WMI calls, the timer
toast). The policy governs commands that ORIGINATE from something the user said.

### Confirmations

`ConfirmationManager` holds at most one pending action, bound to `Action.fingerprint()` (domain,
action, target and parameters) and expiring after `AUTOMATION_CONFIRM_TTL_SECONDS`. `app.py`
answers it BEFORE the DMM: a bare "yes" sent to the classifier comes back as `general yes` and
gets answered by the chatbot, so the confirmation would never resolve. `read_confirmation_reply`
matches the whole cleaned utterance exactly — "yes and open chrome" is a new instruction, not an
authorisation — and returns None for anything that is not an answer, so an unrelated command
spoken while a prompt is pending still runs normally.

### Target resolution (`src/kayra/automation/targets.py`)

`resolve_open_target`, `resolve_window`, `resolve_application`, `resolve_site`,
`resolve_browser`, `resolve_path`, plus `pick_single`. Every one returns a `Resolution`, never
an action — and a `Resolution` now carries a `kind` (`TargetType`: APPLICATION, WEBSITE, URL,
WINDOW, TAB, FILE, FOLDER, PROCESS, UNKNOWN) as well as a status. Knowing WHAT was resolved is
load-bearing: the "open YouTube" bug was a type confusion, an untyped name walking into an
application launcher.

* **Kayra-owned PID exclusion is the critical safety property.** `kayra_owned_pids()` reads the
  live STT engine's `owned_pids` out of `sys.modules` — it must NEVER import or construct
  `speech_to_text`, because doing so would boot a headless Chrome as a side effect of the user
  asking to close a window.
  **Because the lookup is by STRING, a module RENAME turns this safety property off silently
  instead of raising.** It did exactly that: after the move to `kayra.input.speech_to_text` the
  function still listed only the two pre-reorganisation names, returned an empty set, and
  Kayra's own Chrome stopped being excluded from window enumeration. Fixed 2026-09-07, with two
  regression checks in `tests/test_automation.py` — one pins the current module path, one proves
  the lookup actually finds a live engine's PIDs. If you move the STT module again, update the
  name tuple. Every enumerated window is flagged once, in `list_windows`, so no
  caller can forget to check.
* **"Close YouTube" is a site, not a process.** `looks_like_site` + `resolve_site` match the
  site's title fragments against browser window titles.
* **Known limitation, stated in the module:** Win32 exposes one handle per browser window and
  its title reflects only the ACTIVE tab. A site in a background tab is invisible here.
  `resolve_site` returns NOT_FOUND with that reason rather than closing something else. Real tab
  enumeration needs UI Automation or a debugging port; neither is installed and neither earns
  its cost yet. `_TAB_ENUMERATION_NOTE` marks the extension point.
* `list_windows()` caches for 250ms — a multi-step command asks three or four times, and a
  quarter-second is far shorter than any user-visible window change. `invalidate_cache()` after
  anything that changes the window set.

### The stuck-modifier bug (do not reintroduce)

Bringing a window forward needs a synthetic ALT tap to defeat Windows' `ForegroundLockTimeout`.
If ALT is still logically held when the next accelerator arrives, **Ctrl+T is delivered as
Ctrl+Alt+T and Windows opens the task switcher** — the browser never sees it. Observed end to
end: the new tab silently failed, and the following "close this tab" then landed on a
single-tab window and closed the whole window.

Two things prevent it, and both are load-bearing:
* `focus_window` tries `SetForegroundWindow` FIRST and only taps ALT as a fallback, then
  releases ALT/CTRL/SHIFT and settles for `FOCUS_SETTLE_SECONDS` — **including on the fast path
  where the window is already in front**, which is where the bug actually lived.
* `send_keys()` in `automation/windows.py` normalises the modifier state immediately before every
  injected keystroke. Every injection site goes through it so no future call site can forget.

### Destructive tab actions never hunt for a browser

`close_tab` requires a browser to ALREADY be in front. If the user says "close this tab" while
looking at their editor, the honest answer is "no browser is in front". An earlier version fell
back to "find any browser and close a tab in it", and during testing that closed a tab in a
window full of the user's own work. Non-destructive actions (new tab, refresh, back) may still
focus the one unambiguous browser.

### Planner

`plan_actions` groups actions for execution. Only `_CONCURRENT_SAFE` domains (`info` — pure
reads) share a group; everything else is sequential. This is a correctness fix, not tuning: the
old router built every command as an `asyncio.to_thread` task and fired the lot through one
`asyncio.gather`, so "open chrome and maximize the window" raced the maximize against the
launch, and two keystroke sequences went to whatever had focus at that instant.

### Verification

Actions verify rather than assume. `_open_app_verified` focuses an already-running app instead
of launching a second copy, then waits for a window; `close` polls until the handle is gone;
screenshot diffs the folder; filesystem operations re-`stat` the result. Nothing says "Done."
because a keystroke was sent.

### Efficiency

* `normalize_command` 1.4us, `classify_shell`/`classify_action` 4.3us — no allocation, no I/O.
* `list_windows` 4.9ms cold, 0.0004ms cached.
* `SystemInfo` moved off PowerShell onto psutil: ~0.1ms instead of a 300-900ms process spawn
  per query. Three info queries answer together in ~100ms.
* **Zero LLM calls on the execution path.** The DMM classifies; everything after it is
  arithmetic. `Content()` is the one text-GENERATION feature and is not an execution decision.
* No new threads. Timers use `threading.Timer` via the bounded `TimerService`, cancelled at
  shutdown by `shutdown_automation()`.
* No `shell=True` and no `os.system` anywhere in the stack (AST-asserted).

### Bounded collections

Audit ring 200 entries; automation context is six fixed slots with a 300s referent TTL;
`TimerService.MAX_ACTIVE` 16; screenshots pruned to `AUTOMATION_SCREENSHOT_KEEP` (30); the
window cache is one list; the pid→exe cache clears at 256 entries; `SearchFiles` is depth- and
result-capped and never walks the whole disk.

### Automation responses

`ActionResult.message` is SPOKEN, so it is one short sentence: "Chrome is open.",
"YouTube is closed.", "I don't see netflix open.", "You have more than one browser open. Which
one?". No markdown, no paths, no status codes — those go to `detail` and the audit log.

## The power-target boundary: Kayra is not the computer (2026-09-10)

**Observed live.** The user said something about Kayra's engine, the recognizer produced
`"Shutdown the engine car."`, and Kayra offered to **shut down Windows**:

```
[DMM] ...
[INFO] Dispatching hardware automation tasks: ['system shutdown']
"This will shut down your computer. Should I go ahead?"
```

One reflexive "yes" from ending the user's session, from a sentence that never mentioned a
computer.

### The root cause was a substring test

`automation/windows.py::normalize_command` routed `system <payload>` through `_SYSTEM_VERBS`:

```python
for needle, verb in _SYSTEM_VERBS:
    if needle in lowered_payload:            # <- SUBSTRING. No target requirement at all.
        return Action("system", verb, ...)
```

`"shutdown" in "shutdown the engine car"` is True, so the payload became `system.shutdown`,
which is in `_CONFIRM_ACTIONS`, which produced the computer-shutdown prompt. **The substring
test was the whole of the safety logic.**

### Explicit targeting, resolved before the action is built

`policy.resolve_power_target()` returns `COMPUTER`, `KAYRA` or `AMBIGUOUS` from **whole-word**
matching, and **defaults to AMBIGUOUS**.

| Payload | Target |
|---|---|
| `shut down the computer` / `turn off my pc` / `shutdown windows` | COMPUTER |
| `shutdown the engine car` / `shut down the engine` / `turn off yourself` | KAYRA |
| `shutdown` / `shut down` / `""` / `shut down kayra and the computer` | AMBIGUOUS |

* **`POWER_VERBS` = shutdown, restart, sign_out, sleep.** `lock` is deliberately not one — a
  one-keystroke undo is not a session-wide blast radius.
* **`"system"` is deliberately NOT a computer-target word.** It is the DMM's own token prefix,
  so it appears in every payload and would match all of them — which is the bug.
* **Two new actions, both DENIED by policy**: `system.power_ambiguous` and
  `system.power_kayra`. Neither can reach the machine, and `classify_action` re-checks the
  target from the action itself — a verdict that depends on one function having been called
  correctly is not a policy.
* **The refusal is a QUESTION, not a flat no.** `_power_refusal_sentence()` supplies it:
  *"Do you mean shut down Kayra, or shut down the computer? They are different things, so
  please say which."* Every other DENY keeps the plain refusal, because every other DENY is
  something Kayra will not do however it is phrased.
* **The genuine computer prompt now names its target**: "This will shut down your COMPUTER,
  not Kayra."
* **`"turn off screen"` was mapped to `sleep`**, which calls `SetSuspendState` — so it
  suspended the whole machine. It is `system.screen_off` now, blanks the display via
  `SC_MONITORPOWER`, and needs no target because nothing is lost by getting it wrong.
* **`ExecuteCommand`'s sleep branch is inert**, for the same reason its shutdown branch already
  was: a route to a power action from there would be a way around the gate.

---
