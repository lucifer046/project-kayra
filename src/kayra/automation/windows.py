# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation_windows.py                          │
# │                  System Control & Keystroke Core                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
The "hands" of the assistant: everything that actually touches the machine.

PIPELINE
--------
A DMM token never reaches a handler directly any more. It goes through:

    token -> normalize_command()  -> Action          (structured; parsed ONCE)
          -> classify_action()    -> ALLOW/CONFIRM/DENY   (automation_policy)
          -> resolve_*()          -> Resolution      (automation_targets)
          -> plan_actions()       -> ordered groups
          -> execute_action()     -> ActionResult    (this file)
          -> verify              -> spoken sentence

Each stage has exactly one job, which is what makes the whole thing testable: the policy can
be tested without a desktop, the resolver without an executor, and the executor with a
resolved target it did not have to guess at.

NO LLM ON THIS PATH. Once the DMM has said "browser.close_tab", pressing Ctrl+W is arithmetic,
not inference. `Content()` is the single exception in this file, and it is a text-GENERATION
feature, not an execution decision.

BACKWARD COMPATIBILITY
----------------------
Every handler that existed before is still here with its original name and signature
(`OpenApp`, `CloseApp`, `WindowManage`, `MediaControl`, `HotkeyShortcut`, `SystemInfo`,
`SetTimer`, `TakeScreenshot`, `ClipboardCopy/Paste/CopyText`, `ExecuteCommand`, `ToggleWifi`,
`WebSearch`, `Content`, `YoutubeSearch`, `PlayYoutube`, `global_desktop_type`). The new layer
wraps them; it did not replace them. Several now delegate to a resolver-backed implementation
internally, but their public behaviour and return type are unchanged.

WHAT CHANGED, AND WHY IT HAD TO
-------------------------------
* `CloseApp` used to end at `taskkill /f /im <name>.exe`. That force-kills EVERY process of
  that name — the user's whole browser session, and Kayra's own STT Chrome with it. It is gone.
  Closing is now WM_CLOSE to a RESOLVED window, which is what clicking the X does.
* `CloseApp` also matched the requested name as a substring of every visible window title and
  closed every match. Matches are now scored and ranked, and a tie asks the user.
* `SystemInfo` spawned a PowerShell process per query (~300-900ms each). psutil was already a
  dependency; the same answers are now microseconds away, with the PowerShell path kept as a
  fallback for the two things psutil cannot see.
* Timers each spawned their own thread and could not be cancelled or shut down. There is now
  one bounded timer service.
* Shutdown, restart and Wi-Fi-off executed immediately on a voice command. They are now
  CONFIRM actions.
"""

import io
import os
import re
import sys
import time
import ctypes
import contextlib
import platform
import tempfile
import asyncio
import threading
import shlex
import shutil
import subprocess
import requests
import webbrowser
import keyboard
import pyautogui
from pynput.keyboard import Controller

# Suppress noisy startup output of third-party loaders (e.g. AppOpener, pywhatkit)
try:
    _old_stdout = sys.stdout
    _old_stderr = sys.stderr
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')
    
    from AppOpener import close, open as appopen
    from pywhatkit import search, playonyt
finally:
    sys.stdout.close()
    sys.stderr.close()
    sys.stdout = _old_stdout
    sys.stderr = _old_stderr

from bs4 import BeautifulSoup

try:
    import psutil
except Exception:
    psutil = None

try:
    import pyperclip
except Exception:
    pyperclip = None

# Robust relative path import vectors for unified ecosystem execution
from kayra.intelligence.llm_engine import CentralizedLLMEngine

from kayra.core.config import env
from kayra.core.config import env
from kayra.utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console

# Policy / structured-action layer and the target resolver. Both are pure-logic modules with
# no hardware dependency, which is what lets the automation test suite run headless.
# The whole module, not only the names below: the power-target gate reads
# `policy.POWER_VERBS` / `policy.resolve_power_target` / `policy.TARGET_*`, and importing
# them individually would mean a set added there is silently not enforced here.
from kayra.automation import policy
from kayra.automation.policy import (Action, ActionResult, Status, Risk, classify_action,
                                classify_shell, ConfirmationManager, AutomationContext,
                                audit, AUDIT_STARTED, AUDIT_SUCCESS, AUDIT_FAILED,
                                AUDIT_BLOCKED, AUDIT_CONFIRM, AUDIT_AMBIGUOUS,
                                is_protected_path, read_confirmation_reply)
from kayra.automation import targets as targets

# The platform-neutral half of the pipeline: the normalizer, the planner, the timer service,
# the filesystem handlers and the safe terminal. They lived in this file until the Linux port,
# and are re-exported here under their original names so every caller and test keeps working.
from kayra.automation.common import (
    TimerService, parse_duration, _humanize_duration, _writable_base, CreateFolder,
    CreateFile, RenamePath, CopyPath, MovePath, DeletePath, SearchFiles, RunShellCommand,
    _env_int, SCREENSHOT_KEEP, SHELL_TIMEOUT_SECONDS, prune_screenshots, _EXACT_TOKENS,
    _PREFIX_TOKENS,
    HOTKEY_MAP, _power_refusal_sentence, _assistant_alias, ASSISTANT_LABEL, _SYSTEM_VERBS,
    normalize_command, _build_action, _CONCURRENT_SAFE, plan_actions, _CONFIRM_PROMPTS,
    _confirm_prompt)
from kayra.automation import common as _common

# Runtime state is optional here: the automation layer works standalone (the diagnostic block
# at the bottom runs without main.py), but when the assistant is live it must publish
# AUTOMATING so the proactive agent knows not to speak.
from kayra.core.runtime_state import get_runtime_state, AssistantState

# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

username = env("USERNAME", "User")

# Share the centralized engine instance for content generation pipelines
engine = CentralizedLLMEngine()
virtual_keyboard = Controller()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36"
messages_cache = []

# ┌────────────────────────────────────────────────────────────────────────┐
# │                GLOBAL DESKTOP TYPE HARDWARE INJECTION                  │
# └────────────────────────────────────────────────────────────────────────┘

def global_desktop_type(spoken_text):
    """
    Simulates hardware keyboard keystrokes instantly at the current active mouse cursor.
    Uses clipboard fallback logic to ensure complex strings or Hinglish phonetics transfer safely.
    """
    cleaned_text = spoken_text.strip()
    if cleaned_text.lower().startswith("write "):
        payload = spoken_text[6:]
    elif cleaned_text.lower().startswith("type "):
        payload = spoken_text[5:]
    else:
        payload = spoken_text

    print_info(f"Injecting simulated desktop keystrokes: '{payload[:30]}...'")
    
    # 0.5s safety window to allow focus stabilization
    time.sleep(0.5)
    try:
        # Utilizing fast write sequence intervals
        pyautogui.write(payload, interval=0.005)
        return True
    except Exception as e:
        print_error(f"Hardware-level text injection failure: {e}")
        return False

# ┌────────────────────────────────────────────────────────────────────────┐
# │                       AUTOMATION CORE ENGINE                           │
# └────────────────────────────────────────────────────────────────────────┘

def WebSearch(query):
    """Executes default search engine tracking."""
    search(query)
    return True

def Content(topic):
    """
    Generates programming logic or scripts via the Centralized Engine, 
    persists it into a system temp cache, and targets notepad to display it.
    """
    global messages_cache
    topic_clean = topic.replace("content", "").strip()
    print_info(f"Synthesizing dedicated script asset for: '{topic_clean[:20]}...'")

    system_prompt = f"You are a professional content writer and expert software programmer. Write high-quality code, emails, or text for {username}. Do not include markdown wraps or conversational fluff."
    messages_cache.append({"role": "user", "content": topic_clean})

    api_payload = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": topic_clean}
    ]

    # Leverage unified streaming pipeline directly
    generated_buffer = ""
    for chunk in engine.generate_chat_stream(api_payload):
        generated_buffer += chunk

    messages_cache.append({"role": "assistant", "content": generated_buffer})

    # Save to disk securely
    temp_dir = tempfile.gettempdir()
    file_name = f"MYSTERY_Output_{int(time.time())}.txt"
    filepath = os.path.join(temp_dir, file_name)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(generated_buffer)

    print_success(f"Content generation committed to temporary cache file.")
    subprocess.Popen(["notepad.exe", filepath])
    return True

def YoutubeSearch(topic):
    """Opens target query matrices within YouTube index parameters."""
    webbrowser.open(f"https://www.youtube.com/results?search_query={topic}")
    return True

def PlayYoutube(query):
    """Dispatches instant video streaming links via pywhatkit."""
    playonyt(query)
    return True

def OpenApp(app):
    """
    Opens an application, a website or a URL — after deciding WHICH of those it is.

    Backward compatible: same name, same argument, still returns a bool. The body is new,
    because the old one had no notion of target TYPE and therefore no way to be right.

    What it used to do, and why "open YouTube" opened File Explorer
    --------------------------------------------------------------
    Anything that was not literally a URL went to `AppOpener.open(..., match_closest=True)`.
    That launcher is `os.system("explorer shell:appsFolder\\" + id)` over a CACHED Start-Menu
    index, with a `difflib` fuzzy match (cutoff 0.6) when the exact name is absent. Two
    distinct ways it launched the wrong thing, both reproduced on this machine:

      * a STALE index entry — `youtube` was cached as a Brave PWA AppsFolder id that no longer
        exists. `explorer shell:appsFolder\\<dead id>` does not fail; it opens a plain FILE
        EXPLORER WINDOW. That is the reported bug, exactly.
      * a FUZZY hit — `github` is absent from the index, difflib matched "git gui", and Git
        GUI launched.

    Neither raised, so the assistant reported success. When AppOpener did raise, the fallback
    was a DuckDuckGo `!ducky` redirect — routing a plain "open YouTube" through a search
    engine. Both behaviours are gone.

    Now: `targets.resolve_open_target` decides the type, and each type has exactly one
    execution path. An unresolvable name is reported, never guessed at.
    """
    return open_target(app).ok


def _split_open_targets(raw):
    """
    Splits "chrome and youtube" into two targets — but only when both halves really resolve.

    The old code split on whitespace in its fallback path, which turns "visual studio code"
    into three lookups and three wrong launches. Splitting only on an explicit separator, and
    only when every part is independently resolvable, keeps the multi-open capability without
    that failure: "open my documents and settings" stays one target because "my documents"
    does not resolve on its own.
    """
    lowered = (raw or "")
    if "," not in lowered and not re.search(r"\band\b", lowered, re.I):
        return [raw]
    parts = [p.strip() for p in re.split(r",|\band\b", raw, flags=re.I) if p.strip()]
    if len(parts) < 2:
        return [raw]
    if all(targets.resolve_open_target(part).ok for part in parts):
        return parts
    return [raw]


def _launch_from_index(name):
    """
    Launches an EXACT Start-Menu entry through AppOpener. Never `match_closest`.

    `match_closest=True` is what turned "open github" into Git GUI. The exact-name path is
    still useful — it is how a normal installed application that Kayra does not curate gets
    launched — so it is kept, with the guessing removed.
    """
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            appopen(name, match_closest=False, output=False, throw_error=True)
        return True
    except Exception:
        return False


def _launch_application(canonical, spoken):
    """
    Starts a desktop application and VERIFIES that its window appeared.

    Launch order is cheapest-and-most-exact first: `os.startfile` on the registered executable
    (the same mechanism the Windows Run box uses), then the exact Start-Menu entry. There is
    no third, fuzzy attempt — that is the whole bug.
    """
    spec = targets.APP_REGISTRY.get(canonical)
    started = False

    if spec:
        try:
            os.startfile(spec["exe"])
            started = True
        except Exception:
            started = False

    if not started:
        candidates = [canonical]
        if spec:
            candidates.extend(spec["aliases"])
        for candidate in candidates:
            if targets.app_index_entry(candidate) and _launch_from_index(candidate):
                started = True
                break

    if not started:
        return ActionResult.failure(f"I couldn't start {spoken}.", target=canonical)

    targets.invalidate_cache()
    if spec:
        window = targets.wait_for_app_window(canonical, timeout=8.0)
        if window is None:
            return ActionResult.failure(f"I started {spoken}, but no window appeared.")
        return ActionResult.success(f"{spoken.title()} is open.", hwnd=window.hwnd)

    # Not a curated application, so there is no canonical window class to wait for. Verify
    # that SOMETHING new appeared instead of claiming a success we did not observe.
    if _wait_for_new_window(timeout=8.0):
        return ActionResult.success(f"{spoken.title()} is open.")
    return ActionResult.failure(f"I started {spoken}, but no window appeared.")


def _wait_for_new_window(timeout=8.0, before=None):
    """Bounded poll for any new non-Kayra window. Verification for an uncurated launch."""
    baseline = before if before is not None else {w.hwnd for w in targets.list_windows(force=True)}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in targets.list_windows(force=True):
            if window.hwnd not in baseline and not window.kayra_owned:
                return window
        time.sleep(0.15)
    return None


def open_target(name, context=None):
    """
    The open pipeline. Resolver-backed, typed, and verified — returns the spoken sentence.

        normalize -> resolve_open_target -> {URL | WEBSITE | APPLICATION} -> execute -> verify

    A website opens in the user's existing default browser through `webbrowser`, which reuses
    a running browser rather than starting another one. An application is focused if it is
    already running and launched otherwise, and either way a window has to be there before
    this says it worked.
    """
    raw = (name or "").strip()
    if not raw:
        return ActionResult.failure("Open what?")

    spoken = []
    last = None
    for part in _split_open_targets(raw):
        last = _open_one(part)
        if last.message:
            spoken.append(last.message)
        if not last.ok:
            break
    if len(spoken) > 1:
        merged = ActionResult(last.status, " ".join(spoken))
        return merged
    return last


def _open_one(name):
    resolution = targets.resolve_open_target(name)
    print_info(f"Open target '{name}' -> {resolution.kind} ({resolution.reason})")

    if resolution.status == targets.Resolution.NOT_FOUND:
        # Deliberately NOT a filesystem sweep or a web search. Saying "I couldn't find it" is
        # the correct answer; launching the nearest-looking executable is how this broke.
        return ActionResult.not_found(f"I couldn't find {name}.")

    if resolution.kind in (targets.TargetType.URL, targets.TargetType.WEBSITE):
        url = resolution.target
        result = OpenUrl(url)
        if not result.ok:
            return result
        label = targets.canonical_website(name) or name
        return ActionResult.success(f"Opening {label.title()}.", url=url,
                                    kind=resolution.kind)

    if resolution.kind == targets.TargetType.APPLICATION:
        canonical = resolution.target
        running = targets.resolve_application(canonical)
        if running.ok:
            # Already open: "open Chrome" means "show me Chrome", not "start a second copy".
            window = targets.pick_single(running).target or running.matches[0]
            if targets.focus_window(window):
                return ActionResult.success(f"{name.title()} is open.", focused=True,
                                            hwnd=window.hwnd)
        result = _launch_application(canonical, name)
        if result.ok:
            return result
        # A curated application that would not start, but which also has a web version
        # (Spotify, WhatsApp): the web player is what the user can actually use right now.
        url = targets.website_url(name) or targets.website_url(f"{canonical} web")
        if url:
            web = OpenUrl(url)
            if web.ok:
                return ActionResult.success(f"Opening {name.title()} on the web.", url=url)
        return result

    if resolution.kind in (targets.TargetType.FILE, targets.TargetType.FOLDER):
        return OpenPath(resolution.target)

    return ActionResult.not_found(f"I couldn't find {name}.")



def CloseApp(app):
    """
    Closes an application, a browser site, or a window — resolving the target first.

    Backward compatible: same name, same argument, still returns a bool. Everything inside is
    new, because the old implementation had two ways to destroy the wrong thing:

      * it posted WM_CLOSE to EVERY visible window whose title contained the requested string,
        so "close code" closed every window with "code" anywhere in its title; and
      * when nothing matched it ran `taskkill /f /im <name>.exe`, force-killing every process
        of that name. For "close chrome" that is the user's entire browsing session — and
        Kayra's own STT Chrome, which would take the microphone down with it.

    Now: resolve -> verify the target is not Kayra-owned -> WM_CLOSE the ONE best match ->
    confirm it is gone. No force kill, ever, on this path.
    """
    return close_target(app).ok


def close_target(app, context=None, all_instances=False):
    """
    The resolver-backed close. Returns an `ActionResult` carrying the spoken sentence.

    ONE INTENT, ONE TARGET. That is the contract, and it is enforced by `targets.pick_single`
    rather than by care at each call site. The previous version resolved an application to
    every one of its windows and then looped `close_window` over all of them: one sentence,
    four windows gone, no question asked. It also fell through from a missed site lookup into
    a LOOSE application lookup, so "close YouTube" with no YouTube window in front matched
    every window with "youtube" anywhere in its title and closed those too.

    Order matters: a site is checked before an application, because "close YouTube" is a tab,
    not a process, and asking AppOpener for "youtube.exe" is how the old code got lost.

    `all_instances=True` is the explicit broad form ("close all Chrome windows"). It is never
    inferred — it arrives only from a token the user actually said.
    """
    name = (app or "").strip()
    if not name:
        return ActionResult.failure("I didn't catch what to close.")
    lowered = name.lower()

    # ── 1. Website target ("close YouTube") ──
    if targets.looks_like_site(lowered):
        resolution = targets.resolve_site(lowered)
        if resolution.status == targets.Resolution.AMBIGUOUS:
            names = targets.describe_windows(resolution.matches)
            return ActionResult.ambiguous(
                f"I found {len(resolution.matches)} windows showing {name}. "
                f"Which one should I close?", candidates=names)
        if resolution.ok:
            window = resolution.target
            if not targets.focus_window(window):
                return ActionResult.failure(
                    f"I couldn't bring that {window.app} window forward, so I left it alone.")
            # focus_window has settled the input queue; send_keys guarantees no modifier is
            # still held, so this Ctrl+W reaches the browser rather than the task switcher.
            send_keys("ctrl+w", settle=0.3)
            targets.invalidate_cache()
            return ActionResult.success(f"{name.title()} is closed.", hwnd=window.hwnd)
        # A pure site name that is not on screen is a MISS, not an invitation to look for a
        # process of that name. Only a name that is ALSO a real application falls through.
        if not (targets.canonical_app(lowered) or targets.app_index_entry(lowered)):
            return ActionResult.not_found(
                f"I don't see {name} open. If it's in a background tab I can't see it "
                f"from here.")

    # ── 2. Application target ("close Spotify", "close Chrome") ──
    # strict=True: for a name Kayra does not curate, only an exact process-identity match
    # counts. A title substring is enough to FOCUS the wrong window and far too little to
    # close it.
    resolution = targets.resolve_application(lowered, strict=True)
    if resolution.ok:
        if all_instances:
            windows = [w for w in resolution.matches if not w.kayra_owned]
            handles = [w.hwnd for w in windows]
            for window in windows:
                targets.close_window(window)
            gone = sum(1 for h in handles if targets.wait_until_gone(h, timeout=2.0))
            if gone:
                return ActionResult.success(
                    f"Closed {gone} {name.title()} window{'s' if gone != 1 else ''}.",
                    closed=gone)
            return ActionResult.failure(f"{name.title()} didn't respond, so it's still open.")

        single = targets.pick_single(resolution)
        if single.status == targets.Resolution.AMBIGUOUS:
            return ActionResult.ambiguous(
                f"You have {len(single.matches)} {name.title()} windows open. "
                f"Which one should I close?",
                candidates=targets.describe_windows(single.matches))
        if not single.ok:
            return ActionResult.not_found(f"I couldn't find {name}.")
        window = single.target
        targets.close_window(window)
        if targets.wait_until_gone(window.hwnd, timeout=2.0):
            return ActionResult.success(f"{name.title()} is closed.", hwnd=window.hwnd)
        return ActionResult.failure(f"{name.title()} didn't respond, so it's still open.")

    # ── 3. Not running as a window. AppOpener can close a background/tray app — but only on
    #      an EXACT name. `match_closest` here would close whatever it liked the look of. ──
    if targets.app_index_entry(lowered):
        from AppOpener import close as appclose
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                appclose(lowered, match_closest=False, output=False, throw_error=True)
            targets.invalidate_cache()
            return ActionResult.success(f"{name.title()} is closed.")
        except Exception:
            pass

    # ── 4. Give up honestly. The old code force-killed here; that is the bug, not the fix. ──
    if targets.looks_like_site(lowered):
        return ActionResult.not_found(
            f"I don't see {name} open. If it's in a background tab I can't see it from here.")
    return ActionResult.not_found(f"I couldn't find {name} open.")


def close_all_windows():
    """
    Closes every visible user window. The explicit broad form of "close everything".

    Reachable ONLY from a token the user actually said, and CONFIRM-gated by the policy layer,
    because it is the one close in this module whose blast radius is the whole desktop.
    Kayra's own windows and the shell are excluded by the same rules as everything else.
    """
    own_pid = os.getpid()
    windows = [w for w in targets.list_windows(force=True)
               if not w.kayra_owned and w.pid != own_pid]
    if not windows:
        return ActionResult.not_found("There are no windows open.")
    handles = [w.hwnd for w in windows]
    for window in windows:
        targets.close_window(window)
    gone = sum(1 for h in handles if targets.wait_until_gone(h, timeout=2.5))
    targets.invalidate_cache()
    if gone:
        return ActionResult.success(f"Closed {gone} window{'s' if gone != 1 else ''}.",
                                    closed=gone)
    return ActionResult.failure("Nothing closed.")


def force_close_app(app):
    """
    Force-terminates an application by PID. CONFIRM-gated; never reached by a plain "close X".

    Even here the sweep is scoped: it targets the PIDs behind that application's actual
    windows, skips anything Kayra owns, and refuses critical system processes. There is
    deliberately no code path in this module that terminates by process NAME.
    """
    name = (app or "").strip().lower()
    if not name:
        return ActionResult.failure("I didn't catch what to close.")
    if psutil is None:
        return ActionResult.failure("I can't manage processes without psutil.")

    protected = {"explorer.exe", "winlogon.exe", "services.exe", "svchost.exe", "csrss.exe",
                 "lsass.exe", "smss.exe", "wininit.exe", "python.exe", "pythonw.exe"}
    resolution = targets.resolve_application(name)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {app} running.")

    owned = targets.kayra_owned_pids()
    killed = 0
    for pid in {w.pid for w in resolution.matches}:
        if pid in owned or pid == os.getpid():
            continue
        try:
            process = psutil.Process(pid)
            if process.name().lower() in protected:
                continue
            process.terminate()
            killed += 1
        except Exception:
            continue
    targets.invalidate_cache()
    if killed:
        return ActionResult.success(f"Force-closed {app}.", killed=killed)
    return ActionResult.failure(f"I couldn't close {app}.")


def _set_brightness(target):
    """Directly sets monitor brightness to a specific percentage level."""
    try:
        target = max(0, min(100, target))
        # shell=False with an argument vector. `target` is an int clamped just above, so
        # there is nothing user-controlled reaching the command line either way — but a shell
        # that is never invoked cannot be tricked, which is the cheaper guarantee to hold.
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
             f".WmiSetBrightness(1, {int(target)})"],
            shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        print_success(f"System monitor panel luminance set directly to {target}%")
    except Exception as e:
        print_error(f"Direct monitor brightness adjustment failed: {e}")

def _adjust_volume(delta):
    """Adjusts system volume by a relative percentage by simulating hardware key presses."""
    try:
        steps = abs(delta) // 2
        key = "volume up" if delta > 0 else "volume down"
        for _ in range(steps):
            keyboard.press_and_release(key)
            time.sleep(0.01)
        print_success(f"System volume {'increased' if delta > 0 else 'decreased'} by {abs(delta)}%")
    except Exception as e:
        print_error(f"Hardware volume adjustment failed: {e}")

def _set_volume(target):
    """
    Sets system volume to an absolute percentage.

    Windows media keys move volume in 2% steps and expose no "set to N" key, so an absolute
    target means zeroing first and stepping up. The old version pressed volume-down 50 times
    with no delay and then stepped up with a 10ms sleep each — up to 75 synthetic key events
    and about a second of wall time for "set volume to 50".

    This keeps the same approach (there is no API alternative without adding pycaw) but
    bounds it: the zeroing burst is the exact number of steps the scale needs, and the sleeps
    are gone from the descent, where they bought nothing.
    """
    try:
        target = max(0, min(100, int(target)))
        for _ in range(50):                       # 50 * 2% = the full scale, exactly
            keyboard.press_and_release("volume down")
        for _ in range(target // 2):
            keyboard.press_and_release("volume up")
        print_success(f"System volume set to {target}%")
        return True
    except Exception as e:
        print_error(f"Absolute hardware volume configuration failed: {e}")
        return False


def ExecuteCommand(command):
    """Hardware command registry processing matrix."""
    cmd = command.lower().strip()
    if "mute" in cmd:
        keyboard.press_and_release("volume mute")
    elif "volume" in cmd:
        match = re.search(r'(\d+)', cmd)
        if match:
            target_val = int(match.group(1))
            if "by" in cmd:
                if "decrease" in cmd or "down" in cmd or "lower" in cmd:
                    _adjust_volume(-target_val)
                else:
                    _adjust_volume(target_val)
            else:
                _set_volume(target_val)
        elif "up" in cmd or "increase" in cmd or "raise" in cmd:
            _adjust_volume(10)
        elif "down" in cmd or "decrease" in cmd or "lower" in cmd:
            _adjust_volume(-10)
        else:
            print_warning(f"Failed to match volume operation parameters: {cmd}")
    elif "lock" in cmd:
        ctypes.windll.user32.LockWorkStation()
    elif "sleep" in cmd or "turn off screen" in cmd:
        # Inert for the SAME reason shutdown is, below. Suspending the machine is a power
        # action and every power action must pass the target gate in `normalize_command`;
        # a route to it from here would be a way around that gate. The normalizer already
        # sends every payload containing "sleep" through the gate, so this branch is
        # unreachable from it — it stays as a refusal rather than a hole.
        print_warning("Sleep goes through the power-target policy, not here.")
        return False
    elif "shutdown" in cmd or "restart" in cmd:
        # Deliberately inert here. `os.system("shutdown /s /t 0")` used to fire the instant
        # this branch matched, with no confirmation of any kind — a misheard word could power
        # the machine off mid-sentence. Shutdown and restart are CONFIRM actions now and run
        # through `execute_action`, which asks first; routing them from here would be a way
        # around that gate.
        print_warning("Shutdown and restart go through the confirmation policy, not here.")
        return False
    elif "brightness" in cmd:
        match = re.search(r'(\d+)', cmd)
        if match:
            target_val = int(match.group(1))
            if "by" in cmd:
                if "decrease" in cmd or "down" in cmd or "lower" in cmd:
                    _adjust_brightness(-target_val)
                else:
                    _adjust_brightness(target_val)
            else:
                _set_brightness(target_val)
        elif "up" in cmd or "increase" in cmd or "raise" in cmd:
            _adjust_brightness(15)
        elif "down" in cmd or "decrease" in cmd or "lower" in cmd:
            _adjust_brightness(-15)
        else:
            print_warning(f"Failed to match brightness operation parameters: {cmd}")
    else:
        print_warning(f"System execution command route failed to match target pattern: {cmd}")
    return True

def _adjust_brightness(delta):
    """Executes high-privilege WMI monitor configuration arrays."""
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness"],
            capture_output=True, text=True, shell=False, timeout=8)
        current = int(res.stdout.strip()) if res.stdout.strip() else 50
        target = max(0, min(100, current + delta))
        _set_brightness(target)
    except Exception as e:
        print_error(f"Monitor instrumentation brightness adjustments failed: {e}")

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SCREENSHOT CAPTURE ENGINE                           │
# └────────────────────────────────────────────────────────────────────────┘

def TakeScreenshot(name=None):
    """Captures the entire screen and saves it to the user's Desktop folder."""
    try:
        # Dynamically resolve the real Desktop path (supports OneDrive-synced desktops)
        home = os.path.expanduser("~")
        desktop_candidates = [
            os.path.join(home, "OneDrive", "Desktop"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "Pictures"),  # Final fallback
        ]
        desktop = next((p for p in desktop_candidates if os.path.exists(p)), home)

        filename = name or f"KAYRA_Screenshot_{int(time.time())}.png"
        if not filename.endswith(".png"):
            filename += ".png"
        filepath = os.path.join(desktop, filename)
        screenshot = pyautogui.screenshot()
        screenshot.save(filepath)
        print_success(f"Screen capture saved: '{filepath}'")
        return True
    except Exception as e:
        print_error(f"Screenshot capture pipeline failed: {e}")
        return False

# ┌────────────────────────────────────────────────────────────────────────┐
# │                      CLIPBOARD OPERATIONS                               │
# └────────────────────────────────────────────────────────────────────────┘

def ClipboardCopy():
    """Simulates a Ctrl+C hardware keystroke to copy the current selection."""
    keyboard.press_and_release("ctrl+c")
    print_success("Clipboard copy operation dispatched.")
    return True

def ClipboardPaste():
    """Simulates a Ctrl+V hardware keystroke to paste clipboard contents."""
    keyboard.press_and_release("ctrl+v")
    print_success("Clipboard paste operation dispatched.")
    return True

def ClipboardCopyText(text):
    """
    Puts arbitrary text on the clipboard without typing it.

    Was `subprocess.Popen('clip', shell=True)` — a process spawn plus a shell, per call, for
    something pyperclip does in-process. pyperclip is already installed; the shell path stays
    as a fallback with shell=False so text can never become shell syntax.
    """
    if not text:
        return False
    try:
        if pyperclip is not None:
            pyperclip.copy(text)
        else:
            process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=False)
            process.communicate(text.encode("utf-16le"))
        print_success(f"Text copied to clipboard: '{text[:30]}...'")
        return True
    except Exception as e:
        print_error(f"Clipboard text injection failed: {e}")
        return False


def ClipboardRead():
    """Returns the current clipboard text, or None. Never logged — it may hold a password."""
    try:
        if pyperclip is not None:
            return pyperclip.paste()
    except Exception as e:
        print_error(f"Clipboard read failed: {e}")
    return None


def ClipboardClear():
    """Empties the clipboard. Useful right after pasting something sensitive."""
    try:
        if pyperclip is not None:
            pyperclip.copy("")
            print_success("Clipboard cleared.")
            return True
    except Exception as e:
        print_error(f"Clipboard clear failed: {e}")
    return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    WINDOW MANAGEMENT CONTROLS                           │
# └────────────────────────────────────────────────────────────────────────┘

def WindowManage(action):
    """Executes advanced window management operations via hardware hotkey injection."""
    cmd = action.lower().strip()

    if "minimize all" in cmd or "show desktop" in cmd or "desktop" in cmd:
        keyboard.press_and_release("win+d")
        print_success("All windows minimized. Desktop exposed.")

    elif "snap left" in cmd:
        keyboard.press_and_release("win+left")
        print_success("Active window snapped to left half.")

    elif "snap right" in cmd:
        keyboard.press_and_release("win+right")
        print_success("Active window snapped to right half.")

    elif "switch window" in cmd or "alt tab" in cmd:
        keyboard.press_and_release("alt+tab")
        print_success("Window focus switched via Alt+Tab.")

    elif "task view" in cmd:
        keyboard.press_and_release("win+tab")
        print_success("Task view panel activated.")

    elif "maximize" in cmd:
        keyboard.press_and_release("win+up")
        print_success("Active window maximized.")

    elif "minimize" in cmd:
        keyboard.press_and_release("win+down")
        print_success("Active window minimized.")

    elif "close window" in cmd:
        keyboard.press_and_release("alt+F4")
        print_success("Active window closed via Alt+F4.")

    elif "notification" in cmd or "action center" in cmd:
        keyboard.press_and_release("win+a")
        print_success("Windows Action Center panel toggled.")

    elif "emoji" in cmd:
        keyboard.press_and_release("win+.")
        print_success("Emoji picker panel activated.")

    else:
        print_warning(f"Unrecognized window management command: '{cmd}'")
    return True

# ┌────────────────────────────────────────────────────────────────────────┐
# │                      MEDIA PLAYBACK CONTROLS                            │
# └────────────────────────────────────────────────────────────────────────┘

def MediaControl(action):
    """Dispatches global media playback control signals via hardware media keys."""
    cmd = action.lower().strip()

    if "pause" in cmd or "play" in cmd or "resume" in cmd:
        keyboard.press_and_release("play/pause media")
        print_success("Media play/pause toggled.")

    elif "next" in cmd or "skip" in cmd:
        keyboard.press_and_release("next track")
        print_success("Skipped to next track.")

    elif "previous" in cmd or "prev" in cmd or "back" in cmd:
        keyboard.press_and_release("previous track")
        print_success("Returned to previous track.")

    elif "stop" in cmd:
        keyboard.press_and_release("stop media")
        print_success("Media playback stopped.")

    else:
        print_warning(f"Unrecognized media control command: '{cmd}'")
    return True

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SYSTEM INFORMATION QUERIES                          │
# └────────────────────────────────────────────────────────────────────────┘

def _powershell(command, timeout=6.0):
    """
    Runs a FIXED, first-party PowerShell query. shell=False, argument vector, bounded timeout.

    Only reached for the handful of readings psutil cannot give. No user text ever enters this
    function — the callers pass literal strings — so there is nothing here for a spoken command
    to inject into. The shell policy in `automation_policy` governs commands that came from
    the user; this is trusted internal code with a fixed argument list.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, shell=False, timeout=timeout,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def SystemInfo(query):
    """
    Reports local machine telemetry.

    Rewritten onto psutil. Every one of these readings previously spawned its own PowerShell
    process — measured at roughly 300-900ms apiece, for numbers psutil returns from memory in
    microseconds. psutil was already a project dependency for the STT process ownership work,
    so this cost nothing to remove.

    PowerShell survives only for monitor brightness and as a battery fallback, which psutil
    cannot always see on desktops.
    """
    cmd = (query or "").lower().strip()

    if "battery" in cmd:
        battery = None
        try:
            battery = psutil.sensors_battery() if psutil else None
        except Exception:
            battery = None
        if battery is not None:
            state = ("Charging" if battery.power_plugged else "Discharging")
            print_success(f"Battery Level: {int(battery.percent)}%")
            print_info(f"Power State: {state}")
            return f"Battery is at {int(battery.percent)} percent, {state.lower()}."
        output = _powershell("(Get-CimInstance Win32_Battery).EstimatedChargeRemaining")
        if output.strip().isdigit():
            print_success(f"Battery Level: {output.strip()}%")
            return f"Battery is at {output.strip()} percent."
        print_warning("No battery detected. This may be a desktop system.")
        return "I don't see a battery on this machine."

    if "ip" in cmd or "wifi" in cmd or "network" in cmd:
        addresses = []
        try:
            import socket
            families = psutil.net_if_addrs() if psutil else {}
            stats = psutil.net_if_stats() if psutil else {}
            for interface, entries in families.items():
                if "loopback" in interface.lower():
                    continue
                if interface in stats and not stats[interface].isup:
                    continue
                for entry in entries:
                    if entry.family == socket.AF_INET and entry.address:
                        addresses.append((interface, entry.address))
        except Exception as e:
            print_error(f"Network telemetry query failed: {e}")
        if addresses:
            for interface, address in addresses:
                print_success(f"{interface}: {address}")
            primary = addresses[0][1]
            return f"Your I P address is {primary}."
        print_warning("No active network interfaces detected.")
        return "I couldn't find an active network connection."

    if "disk" in cmd or "storage" in cmd:
        try:
            lines, spoken = [], []
            for part in (psutil.disk_partitions(all=False) if psutil else []):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except Exception:
                    continue
                lines.append(f"{part.device} {usage.used / 1e9:.1f} GB used / "
                             f"{usage.total / 1e9:.1f} GB ({usage.free / 1e9:.1f} GB free)")
                spoken.append(f"{part.device.rstrip(chr(92) + ':')} has "
                              f"{usage.free / 1e9:.0f} gigabytes free")
            if lines:
                print_success("Disk Usage Report:\n" + "\n".join(lines))
                return ". ".join(spoken[:2]) + "."
        except Exception as e:
            print_error(f"Disk telemetry query failed: {e}")
        return "I couldn't read the disk usage."

    if "ram" in cmd or "memory" in cmd:
        try:
            memory = psutil.virtual_memory() if psutil else None
            if memory is not None:
                used = (memory.total - memory.available) / 1e9
                total = memory.total / 1e9
                print_success(f"RAM: {used:.2f} GB used / {total:.2f} GB total "
                              f"({memory.available / 1e9:.2f} GB free)")
                return (f"Memory is at {memory.percent:.0f} percent, "
                        f"{used:.1f} of {total:.1f} gigabytes used.")
        except Exception as e:
            print_error(f"Memory telemetry query failed: {e}")
        return "I couldn't read the memory usage."

    if "cpu" in cmd or "processor" in cmd:
        try:
            if psutil:
                # interval=0.1 is a real sample; interval=None would return 0.0 on first call.
                load = psutil.cpu_percent(interval=0.1)
                cores = psutil.cpu_count(logical=False) or 0
                threads = psutil.cpu_count(logical=True) or 0
                freq = None
                try:
                    freq = psutil.cpu_freq()
                except Exception:
                    freq = None
                detail = f"CPU: {load:.0f}% load, {cores} cores / {threads} threads"
                if freq and freq.current:
                    detail += f", {freq.current / 1000:.1f} GHz"
                print_success(detail)
                return f"C P U is at {load:.0f} percent across {cores} cores."
        except Exception as e:
            print_error(f"CPU telemetry query failed: {e}")
        return "I couldn't read the processor usage."

    if "uptime" in cmd:
        try:
            if psutil:
                seconds = time.time() - psutil.boot_time()
                days = int(seconds // 86400)
                hours = int((seconds % 86400) // 3600)
                minutes = int((seconds % 3600) // 60)
                print_success(f"Uptime: {days}d {hours}h {minutes}m")
                if days:
                    return f"Up for {days} days, {hours} hours."
                return f"Up for {hours} hours and {minutes} minutes."
        except Exception as e:
            print_error(f"Uptime query failed: {e}")
        return "I couldn't read the uptime."

    if "os" in cmd or "windows" in cmd or "version" in cmd or "system info" in cmd:
        try:
            detail = f"{platform.system()} {platform.release()} (build {platform.version()})"
            print_success(detail)
            return f"This is {platform.system()} {platform.release()}."
        except Exception:
            pass

    print_warning(f"Unrecognized system info query: '{cmd}'")
    return "I'm not sure what to check."


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     TIMER & REMINDER ENGINE                             │
# └────────────────────────────────────────────────────────────────────────┘



TIMERS = TimerService(notify=lambda label: _timer_toast(label))


def _timer_toast(label):
    """Native notification when a timer fires. shell=False, fixed arguments."""
    safe = str(label).replace("'", "").replace('"', "")[:60]
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Windows.Forms; "
             f"[System.Windows.Forms.MessageBox]::Show('Timer complete: {safe}',"
             "'KAYRA Timer','OK','Information')"],
            shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass




def SetTimer(command):
    """
    Arms a countdown timer. Same name and signature as before; now cancellable and
    shutdown-safe via the shared `TIMERS` service.
    """
    seconds = parse_duration(command)
    if not seconds:
        print_warning(f"Could not parse timer duration from: '{command}'")
        return False
    label = _humanize_duration(seconds)
    timer_id, error = TIMERS.add(seconds, label)
    if timer_id is None:
        print_warning(error)
        return False
    print_success(f"Timer armed for {label}. Countdown initiated.")
    return True




# ┌────────────────────────────────────────────────────────────────────────┐
# │                   KEYBOARD SHORTCUT INJECTION                           │
# └────────────────────────────────────────────────────────────────────────┘

def HotkeyShortcut(action):
    """Injects common keyboard shortcuts as hardware-level key events."""
    cmd = action.lower().strip()

    shortcut_map = {
        "undo":         "ctrl+z",
        "redo":         "ctrl+y",
        "select all":   "ctrl+a",
        "save":         "ctrl+s",
        "save file":    "ctrl+s",
        "find":         "ctrl+f",
        "search":       "ctrl+f",
        "new tab":      "ctrl+t",
        "close tab":    "ctrl+w",
        "refresh":      "ctrl+r",
        "reload":       "ctrl+r",
        "fullscreen":   "f11",
        "print":        "ctrl+p",
        "zoom in":      "ctrl+plus",
        "zoom out":     "ctrl+minus",
        "reset zoom":   "ctrl+0",
        "task manager":  "ctrl+shift+escape",
        "run dialog":   "win+r",
    }

    matched = False
    for keyword, keys in shortcut_map.items():
        if keyword in cmd:
            keyboard.press_and_release(keys)
            print_success(f"Keyboard shortcut dispatched: {keyword.title()} ({keys})")
            matched = True
            break

    if not matched:
        print_warning(f"Unrecognized hotkey shortcut command: '{cmd}'")
    return True

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     WI-FI ADAPTER CONTROL                               │
# └────────────────────────────────────────────────────────────────────────┘

def ToggleWifi(action):
    """Toggles the system Wi-Fi adapter on or off using PowerShell netsh commands."""
    cmd = action.lower().strip()
    try:
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                 "shell": False, "timeout": 15}
        if "off" in cmd or "disable" in cmd or "disconnect" in cmd:
            subprocess.run(["netsh", "wlan", "disconnect"], **quiet)
            subprocess.run(["netsh", "interface", "set", "interface", "Wi-Fi", "disable"], **quiet)
            print_success("Wi-Fi adapter disabled. Network disconnected.")
        elif "on" in cmd or "enable" in cmd or "connect" in cmd:
            subprocess.run(["netsh", "interface", "set", "interface", "Wi-Fi", "enable"], **quiet)
            print_success("Wi-Fi adapter enabled. Reconnecting to network...")
        else:
            print_warning(f"Unrecognized Wi-Fi command: '{cmd}'")
    except Exception as e:
        print_error(f"Wi-Fi adapter control failed: {e}")
    return True


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     NEW CAPABILITY HANDLERS                            │
# └────────────────────────────────────────────────────────────────────────┘
# Everything below is additive. No existing handler was removed to make room for it.

def send_keys(combo, settle=0.12):
    """
    Injects a key combination, with the modifier state normalised first.

    THIS IS NOT DEFENSIVE PADDING. Bringing a window forward requires a synthetic ALT tap to
    defeat Windows' foreground lock, and if ALT is still logically held when the next
    accelerator arrives, "Ctrl+T" is delivered as "Ctrl+Alt+T" — Windows opens the task
    switcher and the browser never sees it. Observed end to end: the new tab silently failed,
    and the following "close this tab" then landed on a single-tab window and closed the whole
    window.

    Every injected keystroke in this module goes through here so no future call site can
    reintroduce it by forgetting.
    """
    targets.release_modifiers()
    keyboard.press_and_release(combo)
    if settle:
        time.sleep(settle)


def FocusApp(name):
    """
    Brings an application to the front. "switch to VS Code", "bring Chrome up".

    This capability did not exist: "switch to X" used to reach `WindowManage`, whose only
    switching primitive is Alt+Tab — so it toggled to whatever happened to be second in the
    z-order rather than to X. That is not a subtle bug; it is a different action.
    """
    if not name:
        return ActionResult.failure("Switch to what?")
    resolution = targets.resolve_window(name)
    if resolution.status == targets.Resolution.AMBIGUOUS:
        names = targets.describe_windows(resolution.matches)
        return ActionResult.ambiguous(
            f"I found {len(resolution.matches)} windows matching {name}. Which one?",
            candidates=names)
    if not resolution.ok:
        return ActionResult.not_found(f"I don't see {name} open.")
    window = resolution.target
    if targets.focus_window(window):
        return ActionResult.success(f"Switched to {name}.", hwnd=window.hwnd)
    return ActionResult.failure(f"Windows wouldn't let me bring {name} forward.")


def RestartApp(name):
    """Closes an application and launches it again, verifying each half."""
    if not name:
        return ActionResult.failure("Restart what?")
    closing = close_target(name)
    if closing.status == Status.AMBIGUOUS:
        return closing
    time.sleep(0.6)
    targets.invalidate_cache()
    OpenApp(name)
    canonical = targets.canonical_app(name)
    if canonical and targets.wait_for_app_window(canonical, timeout=10.0):
        return ActionResult.success(f"{name.title()} restarted.")
    return ActionResult.success(f"Restarting {name}.")


# Browser navigation. Deterministic keystrokes against the focused browser — there is nothing
# here for a model to decide, so nothing here calls one.
_BROWSER_KEYS = {
    "new_tab":       "ctrl+t",
    "close_tab":     "ctrl+w",
    "next_tab":      "ctrl+tab",
    "previous_tab":  "ctrl+shift+tab",
    "reopen_tab":    "ctrl+shift+t",
    "duplicate_tab": None,          # no native shortcut; handled below
    "refresh":       "ctrl+r",
    "back":          "alt+left",
    "forward":       "alt+right",
    "fullscreen":    "f11",
}


# Tab actions that DESTROY something. For these the browser must already be in front: if the
# user says "close this tab" while looking at their editor, the honest answer is "no browser
# is in front", not "I found a browser somewhere and closed a tab in it".
#
# This is not hypothetical. During end-to-end testing an earlier version did exactly that —
# the foreground browser had gone away, the fallback picked the only remaining browser, and
# Ctrl+W closed a tab in a window full of the user's own work.
_DESTRUCTIVE_BROWSER_ACTIONS = frozenset({"close_tab"})


def BrowserNav(action, require_browser=True):
    """
    Sends a browser navigation keystroke to the browser the user is looking at.

    `require_browser` verifies a browser really is in front first. Without that check,
    "close this tab" spoken over a text editor sends Ctrl+W to the editor and closes the
    user's file — the keystroke is harmless in isolation and destructive in the wrong window.
    """
    key = _BROWSER_KEYS.get(action)
    if action not in _BROWSER_KEYS:
        return ActionResult.unsupported(f"I don't know how to {action.replace('_', ' ')}.")

    if require_browser:
        front = targets.foreground_window()
        if front is None:
            return ActionResult.failure("I couldn't tell which window is in front.")

        if not front.is_browser:
            if action in _DESTRUCTIVE_BROWSER_ACTIONS:
                return ActionResult.not_found(
                    "No browser is in front, so there's no tab to close.")
            # Non-destructive: opening or navigating in the one unambiguous browser is a
            # reasonable reading of the request.
            resolution = targets.resolve_browser()
            if not resolution.ok:
                if resolution.status == targets.Resolution.AMBIGUOUS:
                    return ActionResult.ambiguous(
                        "You have more than one browser open. Which one?",
                        candidates=targets.describe_windows(resolution.matches))
                return ActionResult.not_found("No browser window is open.")
            if not targets.focus_window(resolution.matches[0]):
                return ActionResult.failure("I couldn't bring the browser forward.")
        elif front.kayra_owned:
            # Should be unreachable (the STT session is headless), but a keystroke aimed at
            # Kayra's own browser is never what the user meant.
            return ActionResult.not_found("No browser is in front.")

    if action == "duplicate_tab":
        # Alt+D focuses the address bar, then Alt+Enter opens its contents in a new tab.
        send_keys("alt+d")
        send_keys("alt+enter")
    else:
        send_keys(key, settle=0.2)

    targets.invalidate_cache()
    spoken = {
        "new_tab": "New tab.", "close_tab": "Tab closed.", "next_tab": "Done.",
        "previous_tab": "Done.", "reopen_tab": "Reopened.", "duplicate_tab": "Duplicated.",
        "refresh": "Refreshed.", "back": "Went back.", "forward": "Went forward.",
        "fullscreen": "Done.",
    }.get(action, "Done.")
    return ActionResult.success(spoken)


def OpenUrl(url, new_tab=True):
    """Opens a URL in the default browser. Encodes properly; never builds a shell command."""
    if not url:
        return ActionResult.failure("Open what?")
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    try:
        webbrowser.open(target, new=2 if new_tab else 0)
        return ActionResult.success("Done.", url=target)
    except Exception as e:
        return ActionResult.failure("I couldn't open that link.", error=str(e))


# ── Mouse ─────────────────────────────────────────────────────────────────
def MouseControl(action, parameters=None):
    """Direct pointer control. pyautogui only — no shell, no subprocess."""
    parameters = parameters or {}
    try:
        if action == "move":
            pyautogui.moveTo(int(parameters.get("x", 0)), int(parameters.get("y", 0)),
                             duration=0.15)
        elif action == "click":
            pyautogui.click()
        elif action == "double_click":
            pyautogui.doubleClick()
        elif action == "right_click":
            pyautogui.rightClick()
        elif action == "scroll_up":
            pyautogui.scroll(int(parameters.get("amount", 400)))
        elif action == "scroll_down":
            pyautogui.scroll(-int(parameters.get("amount", 400)))
        elif action == "drag":
            pyautogui.dragTo(int(parameters.get("x", 0)), int(parameters.get("y", 0)),
                             duration=0.25)
        else:
            return ActionResult.unsupported("I don't know that mouse action.")
        return ActionResult.success("Done.")
    except Exception as e:
        return ActionResult.failure("The mouse action didn't go through.", error=str(e))


# ── Filesystem ────────────────────────────────────────────────────────────
# pathlib / shutil / os.startfile throughout. No `cmd /c`, no PowerShell: a direct API has no
# quoting to get wrong and nothing for a filename to inject into.

def OpenPath(target, kind="any"):
    """Opens a file or folder in its default handler."""
    resolution = targets.resolve_path(target, kind=kind)
    if not resolution.ok:
        return ActionResult.not_found(resolution.reason or f"I couldn't find {target}.")
    path = resolution.target
    try:
        os.startfile(path)
        return ActionResult.success("Done.", path=path)
    except Exception as e:
        return ActionResult.failure(f"I couldn't open {target}.", error=str(e))


























def _screenshot_folder():
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, "OneDrive", "Desktop"),
                      os.path.join(home, "Desktop"),
                      os.path.join(home, "Pictures")):
        if os.path.exists(candidate):
            return candidate
    return home


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        ACTION NORMALIZER                               │
# └────────────────────────────────────────────────────────────────────────┘
# One DMM token in, one structured `Action` out. This is the ONLY place raw command text is
# interpreted; every stage after it reads named fields.
#
# Pure string/dict work: no window enumeration, no process lookup, no model call. Measured at
# a few microseconds, which is what lets it sit on the hot path of every command.
















# ┌────────────────────────────────────────────────────────────────────────┐
# │                          ACTION PLANNER                                │
# └────────────────────────────────────────────────────────────────────────┘





# ┌────────────────────────────────────────────────────────────────────────┐
# │                             EXECUTOR                                   │
# └────────────────────────────────────────────────────────────────────────┘

CONFIRMATIONS = ConfirmationManager()
CONTEXT = AutomationContext()





def execute_action(action, skip_policy=False):
    """
    Runs ONE structured action, with policy first and verification after.

    `skip_policy` is used exactly once: when a confirmation has already been given for this
    action, so the CONFIRM verdict is not re-raised in an infinite loop. It is never exposed
    to anything the user can say.
    """
    if action is None:
        return ActionResult.unsupported("I didn't understand that one.")

    audit(AUDIT_STARTED, action)

    # ── 1. POLICY ──
    if not skip_policy:
        verdict, reason = classify_action(action)
        if verdict == Risk.DENY:
            audit(AUDIT_BLOCKED, action, reason=reason)
            # A POWER-TARGET REFUSAL IS A QUESTION, NOT A REFUSAL SENTENCE. "I won't do that —
            # a power action needs the computer named explicitly" is accurate and useless: the
            # user asked for something and the honest response is to find out which thing they
            # meant. Every other DENY keeps the plain refusal, because every other DENY is
            # something Kayra will not do however it is phrased.
            spoken = _power_refusal_sentence(action)
            return ActionResult.blocked(spoken or f"I won't do that — {reason}.")
        if verdict == Risk.CONFIRM:
            prompt = _confirm_prompt(action, reason)
            CONFIRMATIONS.request(action, prompt)
            audit(AUDIT_CONFIRM, action, reason=reason)
            return ActionResult(Status.NEEDS_CONFIRMATION, prompt, {"reason": reason}, action)

    # ── 2. EXECUTE ──
    try:
        result = _dispatch(action)
    except Exception as e:
        audit(AUDIT_FAILED, action, error=str(e))
        return ActionResult.failure("That didn't work.", error=str(e))

    if result is None:
        result = ActionResult.unsupported("I don't know how to do that yet.")
    result.action = action

    # ── 3. RECORD ──
    if result.status == Status.AMBIGUOUS:
        audit(AUDIT_AMBIGUOUS, action, candidates=result.detail.get("candidates"))
    elif result.ok:
        audit(AUDIT_SUCCESS, action)
        CONTEXT.note_action(action, True)
    else:
        audit(AUDIT_FAILED, action, status=result.status, reason=result.message)
    return result


def _dispatch(action):
    """
    Maps a structured action to a handler.

    A flat dictionary of (domain, action) -> callable would read more tidily, but the handlers
    have genuinely different signatures and several need the parameters dict, so the explicit
    branching here is the honest shape. What matters is that this is the ONLY branching left:
    it dispatches on already-parsed fields, never on the user's sentence.
    """
    domain, verb = action.domain, action.action
    target = action.target
    params = action.parameters

    # ── window ──
    if domain == "window":
        if verb == "focus":
            return FocusApp(target)
        if verb == "close":
            resolution = targets.resolve_window(target or "current")
            if resolution.status == targets.Resolution.AMBIGUOUS:
                return ActionResult.ambiguous(
                    "I found more than one window like that. Which one?",
                    candidates=targets.describe_windows(resolution.matches))
            if not resolution.ok:
                return ActionResult.not_found("I couldn't find that window.")
            window = resolution.target
            targets.close_window(window)
            if targets.wait_until_gone(window.hwnd, timeout=2.0):
                return ActionResult.success("Closed.")
            return ActionResult.failure("That window didn't close.")
        if verb == "close_all":
            return close_all_windows()
        keymap = {"minimize": "win+down", "minimize_all": "win+d", "maximize": "win+up",
                  "restore": "win+down", "snap_left": "win+left", "snap_right": "win+right",
                  "alt_tab": "alt+tab", "task_view": "win+tab", "action_center": "win+a",
                  "emoji": "win+."}
        if verb in keymap:
            send_keys(keymap[verb])
            targets.invalidate_cache()
            return ActionResult.success("Done.")
        return None

    # ── application ──
    if domain == "app":
        if verb == "open":
            return _open_app_verified(target)
        if verb == "close":
            return close_target(target, CONTEXT)
        if verb == "close_all":
            # Explicit and scoped to one named application. The user said "all".
            return close_target(target, CONTEXT, all_instances=True)
        if verb == "kill":
            return force_close_app(target)
        if verb == "restart":
            return RestartApp(target)
        if verb == "content":
            Content(target)
            return ActionResult.success("Written and opened.")
        return None

    # ── browser ──
    if domain == "browser":
        if verb == "open_url":
            result = OpenUrl(params.get("url") or target)
            if result.ok:
                # Name what was opened. "Done." is technically true and tells the user
                # nothing about WHICH of the several readings of their sentence was taken —
                # which is exactly the information that was missing when this opened the
                # wrong thing.
                site = params.get("site") or targets.canonical_website(target)
                result.message = f"Opening {site.title()}." if site else "Opening that link."
            return result
        if verb == "search_web":
            WebSearch(target)
            return ActionResult.success("Searching.")
        if verb == "search_youtube":
            YoutubeSearch(target)
            return ActionResult.success("Searching YouTube.")
        return BrowserNav(verb)

    # ── media ──
    if domain == "media":
        if verb == "play" and target:
            PlayYoutube(target)
            return ActionResult.success("Playing.")
        keymap = {"pause": "play/pause media", "resume": "play/pause media",
                  "next": "next track", "previous": "previous track", "stop": "stop media"}
        if verb in keymap:
            send_keys(keymap[verb], settle=0.05)
            return ActionResult.success("Done.")
        return None

    # ── keyboard ──
    if domain == "keyboard":
        if verb == "type":
            text = params.get("text", "")
            if not text:
                return ActionResult.failure("Type what?")
            return (ActionResult.success("Typed.") if global_desktop_type(text)
                    else ActionResult.failure("I couldn't type that."))
        if verb == "hotkey":
            keys = params.get("keys") or target
            if not keys:
                return ActionResult.unsupported("I don't know that shortcut.")
            send_keys(keys)
            return ActionResult.success("Done.")
        return None

    # ── mouse ──
    if domain == "mouse":
        return MouseControl(verb, params)

    # ── clipboard ──
    if domain == "clipboard":
        if verb == "copy":
            return ActionResult.success("Copied.") if ClipboardCopy() else ActionResult.failure("Copy failed.")
        if verb == "paste":
            return ActionResult.success("Pasted.") if ClipboardPaste() else ActionResult.failure("Paste failed.")
        if verb == "cut":
            send_keys("ctrl+x")
            return ActionResult.success("Cut.")
        if verb == "set":
            return (ActionResult.success("Copied to the clipboard.")
                    if ClipboardCopyText(target) else ActionResult.failure("I couldn't copy that."))
        if verb == "clear":
            return ActionResult.success("Clipboard cleared.") if ClipboardClear() else ActionResult.failure("I couldn't clear it.")
        if verb == "read":
            content = ClipboardRead()
            if not content:
                return ActionResult.not_found("The clipboard is empty.")
            preview = content.strip().replace("\n", " ")[:120]
            return ActionResult.success(f"The clipboard has: {preview}")
        return None

    # ── information ──
    if domain == "info":
        spoken = SystemInfo(verb if verb != "network" else "ip")
        return ActionResult.success(spoken if isinstance(spoken, str) else "Done.")

    # ── screen ──
    if domain == "screen":
        if verb == "screenshot":
            folder = _screenshot_folder()
            before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
            TakeScreenshot()
            after = set(os.listdir(folder)) if os.path.isdir(folder) else set()
            created = [f for f in (after - before) if f.endswith(".png")]
            prune_screenshots(folder)
            if created:
                return ActionResult.success("Screenshot saved.", path=os.path.join(folder, created[0]))
            return ActionResult.failure("The screenshot didn't save.")
        return None

    # ── files ──
    if domain == "file":
        if verb == "open_folder":
            return OpenPath(target, kind="folder")
        if verb == "open_file":
            return OpenPath(target, kind="file")
        if verb == "create_folder":
            return CreateFolder(target)
        if verb == "create_file":
            return CreateFile(target, params.get("content", ""))
        if verb == "rename":
            return RenamePath(target, params.get("new_name"))
        if verb == "copy":
            return CopyPath(target, params.get("destination"))
        if verb == "move":
            return MovePath(target, params.get("destination"))
        if verb == "delete":
            return DeletePath(target)
        if verb == "search":
            return SearchFiles(target)
        return None

    # ── timers ──
    if domain == "timer":
        if verb in ("add", "reminder"):
            seconds = params.get("seconds")
            if not seconds:
                return ActionResult.failure("How long should I set it for?")
            label = _humanize_duration(seconds)
            timer_id, error = TIMERS.add(seconds, label)
            if timer_id is None:
                return ActionResult.failure(error)
            return ActionResult.success(f"Timer set for {label}.")
        if verb == "cancel":
            label = TIMERS.cancel()
            return (ActionResult.success(f"Cancelled the {label} timer.") if label
                    else ActionResult.not_found("You don't have any timers running."))
        if verb == "list":
            active = TIMERS.list()
            if not active:
                return ActionResult.success("No timers running.")
            first = active[0]
            return ActionResult.success(
                f"{len(active)} running. The next one has "
                f"{int(first['remaining'] // 60)} minutes left.")
        return None

    # ── system ──
    if domain == "system":
        # NEITHER OF THESE REACHES HERE IN PRACTICE — the policy DENIES them first and
        # `_power_refusal_sentence` supplies what the user hears. They stay as a second wall
        # so that a caller passing `skip_policy=True` still cannot touch the machine.
        if verb in ("power_ambiguous", "power_kayra"):
            return ActionResult.blocked(_power_refusal_sentence(action))
        if verb == "lock":
            ctypes.windll.user32.LockWorkStation()
            return ActionResult.success("Locking.")
        if verb == "shutdown":
            subprocess.run(["shutdown", "/s", "/t", "5"], shell=False)
            return ActionResult.success("Shutting down in five seconds.")
        if verb == "restart":
            subprocess.run(["shutdown", "/r", "/t", "5"], shell=False)
            return ActionResult.success("Restarting in five seconds.")
        if verb == "sign_out":
            subprocess.run(["shutdown", "/l"], shell=False)
            return ActionResult.success("Signing out.")
        if verb == "sleep":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], shell=False)
            return ActionResult.success("Putting the computer to sleep.")
        if verb == "screen_off":
            # WM_SYSCOMMAND / SC_MONITORPOWER, "off". Blanks the display and leaves everything
            # running — which is what the user asked for and what "sleep" was doing wrong.
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF = 0xFFFF, 0x0112, 0xF170, 2
            ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND,
                                              SC_MONITORPOWER, MONITOR_OFF)
            return ActionResult.success("Screen off.")
        if verb in ("volume", "brightness", "raw"):
            ExecuteCommand(params.get("command") or target or "")
            return ActionResult.success("Done.")
        if verb in ("wifi_on", "wifi_off", "wifi_toggle"):
            ToggleWifi("on" if verb == "wifi_on" else target or "off")
            return ActionResult.success("Done.")
        return None

    # ── shell ──
    if domain == "shell":
        return RunShellCommand(params.get("command") or target)

    return None


def _open_app_verified(name):
    """
    The executor's entry point for `app.open`. Delegates to the one open pipeline.

    Kept as a separate name because it is referenced by the dispatcher and by the test suite,
    and because the guarantee it advertises — that nothing reports success without a verified
    window or a verified navigation — is now `open_target`'s, in one place instead of two.
    """
    if not name:
        return ActionResult.failure("Open what?")
    return open_target(name, CONTEXT)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 ASYNCHRONOUS ORCHESTRATOR CHANNELS                     │
# └────────────────────────────────────────────────────────────────────────┘

async def translate_and_execute(commands):
    """
    Runs a list of DMM tokens through the full pipeline and returns what to SAY.

    Backward compatible in the way that matters: it still takes the same list of raw token
    strings `main.py` has always passed, and every legacy token still routes. What changed is
    what comes back — the old version returned None and printed to the console, so a failed
    automation was completely silent to a voice user. It now returns a short spoken sentence.

    Execution is ordered by `plan_actions`. The old version wrapped every command in
    `asyncio.to_thread` and ran the lot through one `asyncio.gather`, which raced any
    multi-step request that had a dependency — "open chrome and maximize" maximized whatever
    was in front while Chrome was still starting.
    """
    if not commands:
        return ""

    actions, unmapped = [], []
    for command in commands:
        action = normalize_command(command, CONTEXT)
        if action is None:
            unmapped.append(command)
            print_warning(f"Skipping unmapped system loop command token: '{command}'")
            continue
        actions.append(action)

    if not actions:
        return "I'm not sure how to do that." if unmapped else ""

    _set_runtime(AssistantState.AUTOMATING if AssistantState else None)
    spoken = []
    outcomes = {"ok": 0, "failed": 0, "tokens": []}
    try:
        for group in plan_actions(actions):
            if len(group) == 1:
                results = [await asyncio.to_thread(execute_action, group[0])]
            else:
                # Only pure reads reach this branch; see `plan_actions`.
                results = await asyncio.gather(
                    *(asyncio.to_thread(execute_action, action) for action in group))

            for result in results:
                if result.message:
                    spoken.append(result.message)
                # Tallied for the runtime event below. The proactive presence layer uses
                # repeated failures of the SAME action as its evidence for offering another
                # approach, and a guess about what failed would be worse than no offer.
                if result.status == Status.OK:
                    outcomes["ok"] += 1
                elif result.status == Status.FAILED:
                    outcomes["failed"] += 1
                    outcomes["tokens"].append(f"{result.action.domain}.{result.action.action}"
                                              if getattr(result, "action", None) else "")
                # A question stops the batch. Continuing past "which tab did you mean?" would
                # execute the rest of a plan whose earlier step never happened.
                if result.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS):
                    return " ".join(spoken)
    finally:
        _set_runtime(AssistantState.PROCESSING if AssistantState else None)
        _emit_outcome(outcomes)

    return " ".join(spoken)


def _emit_outcome(outcomes):
    """
    Publishes the batch's outcome on the runtime bus.

    Optional in exactly the same way `_set_runtime` is: this module runs standalone from its
    own diagnostic block, where there is no runtime to talk to. It carries counts and action
    identities only — never the user's words.
    """
    if get_runtime_state is None or not (outcomes["ok"] or outcomes["failed"]):
        return
    try:
        get_runtime_state().emit("automation_result", ok=outcomes["ok"],
                                 failed=outcomes["failed"],
                                 tokens=[t for t in outcomes["tokens"] if t])
    except Exception:
        pass


def _set_runtime(state):
    """
    Publishes automation state so the proactive agent knows Kayra is busy.

    Optional by design: `automation_windows` is runnable standalone (see the diagnostic block
    below), and there it has no runtime to talk to.
    """
    if state is None or get_runtime_state is None:
        return
    try:
        get_runtime_state().set_state(state)
    except Exception:
        pass


async def Automation(commands):
    """
    Public entry point. Returns the sentence to speak.

    The historical contract was "returns True". A non-empty string is still truthy, so any
    caller that only tested truthiness keeps working; `main.py` now speaks the value.
    """
    return await translate_and_execute(commands)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      CONFIRMATION ENTRY POINTS                         │
# └────────────────────────────────────────────────────────────────────────┘

def pending_confirmation():
    """The question awaiting an answer, or None. Used by main.py before it calls the DMM."""
    return CONFIRMATIONS.prompt if CONFIRMATIONS.peek() is not None else None


def resolve_confirmation(reply):
    """
    Applies the user's answer to the pending action.

    Returns (handled, spoken). `handled` is False when the utterance was not an answer at all,
    in which case the caller must treat it as a brand-new command — a pending confirmation
    must never swallow an unrelated instruction.

    The action executed here is the one that was stored WITH the prompt, so a "yes" cannot be
    redirected onto anything else, and it is already expired if too much time has passed.
    """
    verdict = read_confirmation_reply(reply)
    if verdict is None:
        return False, ""
    action = CONFIRMATIONS.peek()
    if action is None:
        return False, ""
    if verdict is False:
        CONFIRMATIONS.cancel()
        audit(AUDIT_BLOCKED, action, reason="declined by user")
        return True, "Alright, I won't."
    approved = CONFIRMATIONS.consume()
    if approved is None:
        return True, "That request expired. Ask me again if you still want it."
    result = execute_action(approved, skip_policy=True)
    return True, result.message or "Done."


def shutdown_automation():
    """
    Releases everything this module owns. Called from main.py's shutdown handler.

    Timers are the only long-lived resource here — no threads of our own, no processes we keep
    open, no browser sessions. Cancelling them is the whole teardown.
    """
    cancelled = TIMERS.shutdown()
    CONFIRMATIONS.cancel()
    targets.invalidate_cache()
    return cancelled


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         DIAGNOSTIC TEST NODE                           │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_banner("KAYRA WINDOWS MACRO CORE", "High-Privilege System Automation Node")
    print_success("Asynchronous execution pools initialized successfully. Automation layer active.")

    async def test_loop():
        while True:
            try:
                cmd_input = await asyncio.to_thread(input, "\nAutomation Command > ")
                if not cmd_input.strip(): continue
                if cmd_input.lower() in ["exit", "quit", "bye"]: break
                
                await Automation([cmd_input])
            except (KeyboardInterrupt, asyncio.CancelledError):
                print_system("Manual interrupt. Exiting diagnostic processor.")
                break
            except Exception as e:
                print_error(f"Diagnostic processor halted: {e}")

    try:
        asyncio.run(test_loop())
    except KeyboardInterrupt:
        pass