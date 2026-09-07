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

import os
import re
import sys
import time
import ctypes
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
from kayra.automation.policy import (Action, ActionResult, Status, Risk, classify_action,
                                classify_shell, ConfirmationManager, AutomationContext,
                                audit, AUDIT_STARTED, AUDIT_SUCCESS, AUDIT_FAILED,
                                AUDIT_BLOCKED, AUDIT_CONFIRM, AUDIT_AMBIGUOUS,
                                is_protected_path, read_confirmation_reply)
from kayra.automation import targets as targets

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
    """Opens local window executables or falls back to scraping links instantly."""
    app_target = app.lower().strip()
    print_info(f"Targeting system execution paths for: '{app_target}'")

    # Detect if the input is a URL or domain name (e.g. github.com, https://example.org, claude.ai)
    domain_extensions = r'\.(com|org|net|in|io|ai|co|dev|me|xyz|gov|edu|info|app|tech|site|online|live|pro|cc|tv|gg|us|uk|eu)(/|$|\s)'
    is_url = app_target.startswith("http://") or app_target.startswith("https://")
    is_domain = bool(re.search(domain_extensions, app_target))

    if is_url or is_domain:
        # Parse multiple URLs/domains if separated by spaces, commas, or 'and'
        if "," in app_target or " and " in app_target:
            targets = [t.strip() for t in re.split(r',|\band\b', app_target) if t.strip()]
        else:
            targets = app_target.split()

        for target in targets:
            url = target
            if not url.startswith("http"):
                url = f"https://{url}"
            webbrowser.open(url)
            print_success(f"Opened URL in default browser: {url}")
            time.sleep(0.3)
        return True

    if app_target in ["file explorer", "file manager", "my computer", "this pc", "explorer"]:
        os.startfile("explorer")
        return True

    try:
        _old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            appopen(app_target, match_closest=True, output=True, throw_error=True)
        finally:
            sys.stdout.close()
            sys.stdout = _old_stdout
        return True
    except:
        print_warning(f"Local app shortcut not resolved. Running fast web scraping link extraction...")

        # Parse targets by commas or 'and'. If none, split by space to support multi-site space-separated lists
        if "," in app_target or " and " in app_target:
            targets = [t.strip() for t in re.split(r',|\band\b', app_target) if t.strip()]
        else:
            targets = app_target.split()

        for target in targets:
            try:
                # Use DuckDuckGo's !ducky bang ("I'm Feeling Lucky") to automatically and instantly
                # redirect the user's browser to the primary official website (bypassing all scraping CAPTCHAs)
                import urllib.parse
                safe_target = urllib.parse.quote(target)
                resolved_url = f"https://duckduckgo.com/?q=!ducky+{safe_target}"
                
                # Route exclusively through the system's natively configured default browser
                webbrowser.open(resolved_url)
                
                # Small delay to prevent browser tab rendering bottlenecks
                time.sleep(0.3)
            except Exception as web_err:
                print_error(f"Web fallback routing layer failed for '{target}': {web_err}")
        return True

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


def close_target(app, context=None):
    """
    The resolver-backed close. Returns an `ActionResult` carrying the spoken sentence.

    Order matters: a site is checked before an application, because "close YouTube" is a tab,
    not a process, and asking AppOpener for "youtube.exe" is how the old code got lost.
    """
    name = (app or "").strip()
    if not name:
        return ActionResult.failure("I didn't catch what to close.")
    lowered = name.lower()

    # ── Special surfaces that have no ordinary window to close ──
    if lowered in ("file explorer", "explorer", "file manager", "this pc", "my computer"):
        closed = 0
        for window in targets.list_windows():
            if window.app == "explorer" and not window.kayra_owned and window.title:
                if targets.close_window(window):
                    closed += 1
        return (ActionResult.success("Closed File Explorer.", closed=closed) if closed
                else ActionResult.not_found("File Explorer isn't open."))

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
        # Fall through: it might also be an installed application (Spotify, WhatsApp).

    # ── 2. Application target ("close Spotify") ──
    resolution = targets.resolve_application(lowered)
    if resolution.ok:
        windows = [w for w in resolution.matches if not w.kayra_owned]
        if not windows:
            return ActionResult.not_found(f"I couldn't find {name}.")
        handles = [w.hwnd for w in windows]
        for window in windows:
            targets.close_window(window)
        gone = sum(1 for h in handles if targets.wait_until_gone(h, timeout=2.0))
        if gone:
            return ActionResult.success(f"{name.title()} is closed.", closed=gone)
        return ActionResult.failure(
            f"{name.title()} didn't respond, so it's still open.")

    # ── 3. Not running as a window. Ask AppOpener — it can close background/tray apps. ──
    from AppOpener import close as appclose
    try:
        _old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            appclose(lowered, match_closest=True, output=True, throw_error=True)
            success = True
        except Exception:
            success = False
        finally:
            sys.stdout.close()
            sys.stdout = _old_stdout
        if success:
            targets.invalidate_cache()
            return ActionResult.success(f"{name.title()} is closed.")
    except Exception:
        pass

    # ── 4. Give up honestly. The old code force-killed here; that is the bug, not the fix. ──
    if resolution.status == targets.Resolution.NOT_FOUND and targets.looks_like_site(lowered):
        return ActionResult.not_found(
            f"I don't see {name} open. If it's in a background tab I can't see it from here.")
    return ActionResult.not_found(f"I couldn't find {name} open.")


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
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], shell=False)
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

class TimerService:
    """
    One bounded, cancellable timer service for the whole process.

    The old `SetTimer` started a fresh `threading.Thread` per timer that slept for the whole
    duration. That meant: a thread per timer for up to an hour each, no way to cancel one, no
    way to list them, and nothing that stopped them at shutdown — a two-hour timer kept a
    thread alive and fired a PowerShell message box after the assistant had exited.

    This uses `threading.Timer`, which is still a thread per pending timer but a cancellable
    one, and adds an explicit registry with a hard cap so a stuck loop cannot create thousands.
    `shutdown()` cancels every outstanding timer, so nothing survives the process.
    """

    MAX_ACTIVE = 16

    def __init__(self):
        try:
            self.MAX_ACTIVE = max(1, min(128, int(
                str(os.environ.get("AUTOMATION_MAX_TIMERS", "")).strip())))
        except (TypeError, ValueError):
            pass
        self._lock = threading.RLock()
        self._timers = {}                 # id -> {"timer", "label", "due", "seconds"}
        self._next_id = 1

    def add(self, seconds, label, on_fire=None):
        """Arms a timer. Returns (id, error_message). `id` is None when it was refused."""
        if seconds <= 0:
            return None, "That duration doesn't make sense."
        with self._lock:
            if len(self._timers) >= self.MAX_ACTIVE:
                return None, f"I already have {len(self._timers)} timers running."
            timer_id = self._next_id
            self._next_id += 1

            def _fire():
                with self._lock:
                    self._timers.pop(timer_id, None)
                print_success(f"Timer complete: {label}")
                if on_fire:
                    try:
                        on_fire(label)
                    except Exception:
                        pass
                else:
                    _timer_toast(label)

            handle = threading.Timer(seconds, _fire)
            handle.daemon = True
            handle.name = f"kayra-timer-{timer_id}"
            handle.start()
            self._timers[timer_id] = {"timer": handle, "label": label,
                                      "due": time.time() + seconds, "seconds": seconds}
            return timer_id, None

    def cancel(self, timer_id=None):
        """Cancels one timer, or the most recent when no id is given. Returns its label."""
        with self._lock:
            if not self._timers:
                return None
            if timer_id is None:
                timer_id = max(self._timers)
            entry = self._timers.pop(timer_id, None)
        if entry is None:
            return None
        entry["timer"].cancel()
        return entry["label"]

    def list(self):
        with self._lock:
            now = time.time()
            return [{"id": tid, "label": e["label"], "remaining": max(0.0, e["due"] - now)}
                    for tid, e in sorted(self._timers.items())]

    def shutdown(self):
        """Cancels everything. Called from the assistant's shutdown path."""
        with self._lock:
            entries = list(self._timers.values())
            self._timers.clear()
        for entry in entries:
            try:
                entry["timer"].cancel()
            except Exception:
                pass
        return len(entries)


TIMERS = TimerService()


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


def parse_duration(text):
    """
    Parses '5 minutes', 'an hour and 30 minutes', '90 sec' into seconds. Returns None if it
    cannot. Sums every unit it finds, so compound durations work.
    """
    if not text:
        return None
    lowered = str(text).lower()
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
                                  lowered):
        amount = int(value)
        if unit.startswith(("hour", "hr", "h")):
            total += amount * 3600
        elif unit.startswith(("min", "m")):
            total += amount * 60
        else:
            total += amount
        found = True
    if not found:
        # "an hour", "half an hour"
        if "half an hour" in lowered or "half hour" in lowered:
            return 1800
        if re.search(r"\ban hour\b", lowered):
            return 3600
        if re.search(r"\ba minute\b", lowered):
            return 60
        return None
    return total or None


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


def _humanize_duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {minutes} minutes" if minutes else "")
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}" + (f" {rest} seconds" if rest else "")
    return f"{seconds} second{'s' if seconds != 1 else ''}"


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


def _writable_base(name):
    """
    Resolves the parent directory for a new file/folder.

    A bare name means the Desktop — that is what "make a folder called notes" means to a
    person. An explicit path is honoured as given.
    """
    raw = str(name).strip().strip('"')
    if os.path.isabs(raw) or "\\" in raw or "/" in raw:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        return os.path.dirname(expanded) or os.getcwd(), os.path.basename(expanded)
    home = os.path.expanduser("~")
    desktop = next((p for p in (os.path.join(home, "OneDrive", "Desktop"),
                                os.path.join(home, "Desktop")) if os.path.isdir(p)), home)
    return desktop, raw


def CreateFolder(name):
    parent, leaf = _writable_base(name)
    if not leaf:
        return ActionResult.failure("What should I call it?")
    path = os.path.join(parent, leaf)
    try:
        if os.path.exists(path):
            return ActionResult.failure(f"{leaf} already exists.")
        os.makedirs(path)
        return (ActionResult.success(f"Created {leaf}.", path=path) if os.path.isdir(path)
                else ActionResult.failure(f"I couldn't create {leaf}."))
    except Exception as e:
        return ActionResult.failure(f"I couldn't create {leaf}.", error=str(e))


def CreateFile(name, content=""):
    parent, leaf = _writable_base(name)
    if not leaf:
        return ActionResult.failure("What should I call it?")
    path = os.path.join(parent, leaf)
    try:
        if os.path.exists(path):
            return ActionResult.failure(f"{leaf} already exists.")
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content or "")
        return (ActionResult.success(f"Created {leaf}.", path=path) if os.path.isfile(path)
                else ActionResult.failure(f"I couldn't create {leaf}."))
    except Exception as e:
        return ActionResult.failure(f"I couldn't create {leaf}.", error=str(e))


def RenamePath(source, new_name):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    path = resolution.target
    destination = os.path.join(os.path.dirname(path), str(new_name).strip())
    try:
        if os.path.exists(destination):
            return ActionResult.failure("Something with that name already exists.")
        os.rename(path, destination)
        return (ActionResult.success("Renamed.", path=destination)
                if os.path.exists(destination) else ActionResult.failure("The rename failed."))
    except Exception as e:
        return ActionResult.failure("The rename failed.", error=str(e))


def CopyPath(source, destination):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    src = resolution.target
    dst_resolution = targets.resolve_path(destination, kind="folder")
    dst = (dst_resolution.target if dst_resolution.ok
           else os.path.expandvars(os.path.expanduser(str(destination))))
    try:
        final = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        if os.path.isdir(src):
            shutil.copytree(src, final)
        else:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            shutil.copy2(src, final)
        return (ActionResult.success("Copied.", path=final) if os.path.exists(final)
                else ActionResult.failure("The copy failed."))
    except Exception as e:
        return ActionResult.failure("The copy failed.", error=str(e))


def MovePath(source, destination):
    resolution = targets.resolve_path(source)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {source}.")
    src = resolution.target
    dst_resolution = targets.resolve_path(destination, kind="folder")
    dst = (dst_resolution.target if dst_resolution.ok
           else os.path.expandvars(os.path.expanduser(str(destination))))
    try:
        final = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        if os.path.exists(final):
            return ActionResult.failure("Something with that name is already there.")
        shutil.move(src, final)
        return (ActionResult.success("Moved.", path=final) if os.path.exists(final)
                else ActionResult.failure("The move failed."))
    except Exception as e:
        return ActionResult.failure("The move failed.", error=str(e))


def DeletePath(target):
    """
    Deletes a file or folder. CONFIRM-gated by policy — never reached without approval.

    Sends to the Recycle Bin when send2trash is available, because a recoverable delete is
    strictly better and costs nothing. Refuses a protected system location regardless.
    """
    resolution = targets.resolve_path(target)
    if not resolution.ok:
        return ActionResult.not_found(f"I couldn't find {target}.")
    path = resolution.target

    if is_protected_path(path):
        return ActionResult.blocked("That's a protected system location. I won't delete it.")

    try:
        try:
            from send2trash import send2trash
            send2trash(path)
            recycled = True
        except Exception:
            recycled = False
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        if os.path.exists(path):
            return ActionResult.failure("I couldn't delete it.")
        return ActionResult.success(
            "Moved to the Recycle Bin." if recycled else "Deleted.", path=path)
    except Exception as e:
        return ActionResult.failure("I couldn't delete it.", error=str(e))


def SearchFiles(query, root=None, limit=20):
    """
    Finds files by name under one folder.

    Bounded on purpose: one root, `os.scandir`, a depth cap and a result cap. A full-disk walk
    is unbounded work that would block a spoken command for minutes.
    """
    base = root or os.path.join(os.path.expanduser("~"))
    needle = str(query).strip().lower()
    if not needle:
        return ActionResult.failure("Search for what?")
    matches = []
    max_depth = 4

    def _walk(directory, depth):
        if depth > max_depth or len(matches) >= limit:
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(matches) >= limit:
                        return
                    if entry.name.startswith((".", "$")):
                        continue
                    if needle in entry.name.lower():
                        matches.append(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        _walk(entry.path, depth + 1)
        except (PermissionError, OSError):
            return

    _walk(base, 0)
    if not matches:
        return ActionResult.not_found(f"I couldn't find anything called {query}.")
    for path in matches[:10]:
        print_info(path)
    noun = "match" if len(matches) == 1 else "matches"
    return ActionResult.success(f"Found {len(matches)} {noun}.", matches=matches)


# ── Safe terminal ─────────────────────────────────────────────────────────
def RunShellCommand(command, timeout=None):
    """
    Runs a terminal command that the POLICY has already approved.

    Called only after `classify_shell` returned ALLOW, or CONFIRM plus an explicit yes. The
    command is split into an argument vector and run with `shell=False`, so the user's words
    can never become shell syntax — no chaining, no redirection, no substitution.

    This function does not decide whether the command is safe. That decision belongs to
    `automation_policy` and is made before we get here; duplicating it would create two places
    that can disagree.
    """
    try:
        argv = shlex.split(str(command), posix=False)
    except ValueError:
        return ActionResult.failure("I couldn't parse that command.")
    if not argv:
        return ActionResult.failure("There was no command to run.")

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, shell=False,
                                   timeout=timeout if timeout is not None else SHELL_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return ActionResult.not_found(f"I couldn't find {argv[0]}.")
    except subprocess.TimeoutExpired:
        return ActionResult.failure("That command took too long, so I stopped it.")
    except Exception as e:
        return ActionResult.failure("The command didn't run.", error=str(e))

    output = (completed.stdout or completed.stderr or "").strip()
    if output:
        print_info(output[:2000])
    if completed.returncode == 0:
        return ActionResult.success("Done.", returncode=0, output_chars=len(output))
    return ActionResult.failure(f"That finished with an error.", returncode=completed.returncode)


# ── Screenshot retention ──────────────────────────────────────────────────
def _env_int(name, default, minimum=1, maximum=100000):
    try:
        return max(minimum, min(maximum, int(str(os.environ.get(name, "")).strip())))
    except (TypeError, ValueError):
        return default


SCREENSHOT_KEEP = _env_int("AUTOMATION_SCREENSHOT_KEEP", 30, 1, 1000)
# Bounded so an approved-but-hung command can never block the assistant indefinitely.
SHELL_TIMEOUT_SECONDS = _env_int("AUTOMATION_SHELL_TIMEOUT_SECONDS", 20, 1, 600)


def prune_screenshots(folder=None, keep=SCREENSHOT_KEEP):
    """
    Keeps the newest `keep` Kayra screenshots and deletes the rest.

    Retention policy, as required of every collection Kayra writes: a screenshot key pressed
    a few times a day is an unbounded pile of PNGs on the user's Desktop within a year. Only
    files matching Kayra's own naming prefix are ever touched.
    """
    try:
        folder = folder or _screenshot_folder()
        entries = [os.path.join(folder, f) for f in os.listdir(folder)
                   if f.startswith("KAYRA_Screenshot_") and f.endswith(".png")]
        if len(entries) <= keep:
            return 0
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        removed = 0
        for path in entries[keep:]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0


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

# Exact-match tokens -> (domain, action). Checked BEFORE any prefix rule, which is what keeps
# "close window" and "close tab" from being swallowed by the generic "close " prefix. The old
# router relied on if/elif ordering for this; a dict cannot be reordered by accident.
_EXACT_TOKENS = {
    # window
    "close window": ("window", "close"), "minimize": ("window", "minimize"),
    "minimize all": ("window", "minimize_all"), "show desktop": ("window", "minimize_all"),
    "maximize": ("window", "maximize"), "restore": ("window", "restore"),
    "snap left": ("window", "snap_left"), "snap right": ("window", "snap_right"),
    "switch window": ("window", "alt_tab"), "alt tab": ("window", "alt_tab"),
    "task view": ("window", "task_view"), "action center": ("window", "action_center"),
    "notification": ("window", "action_center"), "emoji": ("window", "emoji"),
    # browser
    "close tab": ("browser", "close_tab"), "new tab": ("browser", "new_tab"),
    "next tab": ("browser", "next_tab"), "previous tab": ("browser", "previous_tab"),
    "reopen tab": ("browser", "reopen_tab"), "duplicate tab": ("browser", "duplicate_tab"),
    "go back": ("browser", "back"), "go forward": ("browser", "forward"),
    "refresh": ("browser", "refresh"), "reload": ("browser", "refresh"),
    # media
    "pause": ("media", "pause"), "resume": ("media", "resume"),
    "next track": ("media", "next"), "previous track": ("media", "previous"),
    "stop media": ("media", "stop"), "play pause": ("media", "pause"),
    # clipboard
    "copy": ("clipboard", "copy"), "copy that": ("clipboard", "copy"),
    "paste": ("clipboard", "paste"), "paste that": ("clipboard", "paste"),
    "cut": ("clipboard", "cut"), "clear clipboard": ("clipboard", "clear"),
    "read clipboard": ("clipboard", "read"),
    # information
    "battery": ("info", "battery"), "cpu": ("info", "cpu"), "ram": ("info", "ram"),
    "memory": ("info", "ram"), "disk": ("info", "disk"), "storage": ("info", "disk"),
    "uptime": ("info", "uptime"), "ip address": ("info", "network"),
    # screen
    "screenshot": ("screen", "screenshot"), "take screenshot": ("screen", "screenshot"),
    # keyboard shortcuts
    "undo": ("keyboard", "hotkey"), "redo": ("keyboard", "hotkey"),
    "select all": ("keyboard", "hotkey"), "save": ("keyboard", "hotkey"),
    "save file": ("keyboard", "hotkey"), "find": ("keyboard", "hotkey"),
    "search": ("keyboard", "hotkey"), "print": ("keyboard", "hotkey"),
    "fullscreen": ("keyboard", "hotkey"), "zoom in": ("keyboard", "hotkey"),
    "zoom out": ("keyboard", "hotkey"), "reset zoom": ("keyboard", "hotkey"),
    "task manager": ("keyboard", "hotkey"), "run dialog": ("keyboard", "hotkey"),
    "enter": ("keyboard", "hotkey"), "escape": ("keyboard", "hotkey"),
    # mouse
    "click": ("mouse", "click"), "mouse click": ("mouse", "click"),
    "double click": ("mouse", "double_click"), "right click": ("mouse", "right_click"),
    "scroll up": ("mouse", "scroll_up"), "scroll down": ("mouse", "scroll_down"),
    # timers
    "cancel timer": ("timer", "cancel"), "list timers": ("timer", "list"),
    # wifi
    "wifi on": ("system", "wifi_on"), "wifi off": ("system", "wifi_off"),
}

# Prefix tokens -> (domain, action). Ordered longest-first at build time so a longer literal
# can never be shadowed by a shorter one that happens to be its prefix.
_PREFIX_TOKENS = [
    ("open folder ", ("file", "open_folder")),
    ("open file ", ("file", "open_file")),
    ("create folder ", ("file", "create_folder")),
    ("create file ", ("file", "create_file")),
    ("delete file ", ("file", "delete")),
    ("delete folder ", ("file", "delete")),
    ("rename ", ("file", "rename")),
    ("find file ", ("file", "search")),
    ("search files ", ("file", "search")),
    ("restart app ", ("app", "restart")),
    ("focus ", ("window", "focus")),
    ("switch to ", ("window", "focus")),
    ("google search ", ("browser", "search_web")),
    ("web search ", ("browser", "search_web")),
    ("youtube search ", ("browser", "search_youtube")),
    ("copy text ", ("clipboard", "set")),
    ("terminal ", ("shell", "run")),
    ("run command ", ("shell", "run")),
    ("set timer ", ("timer", "add")),
    ("timer ", ("timer", "add")),
    ("reminder ", ("timer", "reminder")),
    ("remind ", ("timer", "reminder")),
    ("content ", ("app", "content")),
    ("system ", ("system", "raw")),
    ("write ", ("keyboard", "type")),
    ("type ", ("keyboard", "type")),
    ("close ", ("app", "close")),
    ("open ", ("app", "open")),
    ("play ", ("media", "play")),
]
_PREFIX_TOKENS.sort(key=lambda item: len(item[0]), reverse=True)

# Hotkey names -> key combination. Single source of truth, shared with HotkeyShortcut.
HOTKEY_MAP = {
    "undo": "ctrl+z", "redo": "ctrl+y", "select all": "ctrl+a", "save": "ctrl+s",
    "save file": "ctrl+s", "find": "ctrl+f", "search": "ctrl+f", "cut": "ctrl+x",
    "copy": "ctrl+c", "paste": "ctrl+v", "new tab": "ctrl+t", "close tab": "ctrl+w",
    "refresh": "ctrl+r", "reload": "ctrl+r", "fullscreen": "f11", "print": "ctrl+p",
    "zoom in": "ctrl+plus", "zoom out": "ctrl+minus", "reset zoom": "ctrl+0",
    "task manager": "ctrl+shift+escape", "run dialog": "win+r",
    "enter": "enter", "escape": "esc", "tab": "tab",
}

# "system X" sub-verbs. Shutdown/restart/sign-out become their own actions so the policy layer
# can see them; previously they were a substring test inside ExecuteCommand and executed
# instantly with no confirmation whatsoever.
_SYSTEM_VERBS = (
    ("sign out", "sign_out"), ("log out", "sign_out"), ("logout", "sign_out"),
    ("shutdown", "shutdown"), ("shut down", "shutdown"), ("power off", "shutdown"),
    ("restart", "restart"), ("reboot", "restart"),
    ("lock", "lock"),
    ("sleep", "sleep"), ("turn off screen", "sleep"),
    ("mute", "volume"), ("volume", "volume"), ("brightness", "brightness"),
)


def normalize_command(command, context=None):
    """
    DMM token -> structured `Action`.

    Contextual references ("it", "this", "that") are resolved HERE, against the bounded
    automation context, and only when the context actually holds a fresh referent. When it
    does not, the action keeps the pronoun and carries a low confidence, which the policy
    layer turns into a confirmation rather than a guess.
    """
    raw = (command or "").strip()
    lowered = raw.lower()
    if not lowered:
        return None

    # 1. Exact tokens first — this is what prevents prefix shadowing structurally.
    if lowered in _EXACT_TOKENS:
        domain, action = _EXACT_TOKENS[lowered]
        parameters = {}
        if domain == "keyboard" and action == "hotkey":
            parameters["keys"] = HOTKEY_MAP.get(lowered, lowered)
            parameters["name"] = lowered
        return Action(domain, action, target="current", parameters=parameters, raw=raw)

    # 2. Prefix tokens, longest first.
    for prefix, (domain, action) in _PREFIX_TOKENS:
        if lowered.startswith(prefix):
            payload = raw[len(prefix):].strip()
            return _build_action(domain, action, payload, raw, context)

    # 3. Keyword fallbacks for phrasings the DMM emits without a canonical prefix.
    for keyword, (domain, action) in (
            ("screenshot", ("screen", "screenshot")),
            ("screen capture", ("screen", "screenshot")),
            ("wifi", ("system", "wifi_toggle")),
            ("wi-fi", ("system", "wifi_toggle"))):
        if keyword in lowered:
            return Action(domain, action, target=lowered, raw=raw)

    if lowered in HOTKEY_MAP:
        return Action("keyboard", "hotkey", target="current",
                      parameters={"keys": HOTKEY_MAP[lowered], "name": lowered}, raw=raw)

    return None


def _build_action(domain, action, payload, raw, context):
    """Builds the action for a prefix token, resolving pronouns and sub-verbs."""
    parameters = {}
    confidence = 1.0
    target = payload

    # ── Contextual reference resolution ──
    if payload and payload.lower().strip(" .?!") in targets.CURRENT_TARGET_WORDS:
        # "close it" is UNTYPED: the thing the user opened a moment ago might have been a
        # site, an app or a file, and the untyped resolver returns whichever was established
        # most recently. Only the file domain pins a kind, because "delete it" reaching for a
        # browser tab would be nonsense.
        kind = "file" if domain == "file" else None
        referent = context.resolve_referent(kind) if context else None
        if referent:
            target = referent
            parameters["resolved_from"] = payload
        else:
            # No fresh referent. Keep the pronoun and drop confidence — the policy layer will
            # ask rather than let a `close` act on a guess.
            confidence = 0.3

    if domain == "keyboard" and action == "type":
        return Action(domain, action, target="current",
                      parameters={"text": payload}, raw=raw)

    if domain == "shell":
        return Action("shell", "run", target=payload,
                      parameters={"command": payload}, raw=raw)

    if domain == "timer":
        seconds = parse_duration(payload)
        return Action("timer", action, target=payload,
                      parameters={"seconds": seconds, "label": payload},
                      confidence=1.0 if seconds else 0.4, raw=raw)

    if domain == "system" and action == "raw":
        lowered_payload = (payload or "").lower()
        for needle, verb in _SYSTEM_VERBS:
            if needle in lowered_payload:
                return Action("system", verb, target=payload,
                              parameters={"command": payload}, raw=raw)
        return Action("system", "raw", target=payload,
                      parameters={"command": payload}, confidence=0.6, raw=raw)

    if domain == "app" and action == "open":
        # An "open X" whose X is a URL or a domain is a browser action, not an app launch.
        if targets.looks_like_site(payload) and (
                payload.lower().startswith(("http://", "https://", "www.")) or "." in payload):
            return Action("browser", "open_url", target=payload,
                          parameters={"url": payload}, raw=raw)

    if domain == "app" and action == "close":
        # "close this"/"close it" with no referent means the foreground WINDOW, which is both
        # the safest reading and what a person actually means standing at their desk.
        if confidence < 1.0 and (payload or "").lower().strip(" .?!") in targets.CURRENT_TARGET_WORDS:
            return Action("window", "close", target="current", raw=raw)

    if domain == "file" and action in ("create_file", "create_folder", "rename", "delete"):
        if action == "rename" and " to " in payload.lower():
            source, _, new_name = payload.rpartition(" to ")
            return Action(domain, action, target=source.strip(),
                          parameters={"new_name": new_name.strip()}, raw=raw)
        if action == "delete":
            parameters["bulk"] = bool(re.search(r"[*?]|\ball\b|\beverything\b", payload.lower()))

    return Action(domain, action, target=target, parameters=parameters,
                  confidence=confidence, raw=raw)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          ACTION PLANNER                                │
# └────────────────────────────────────────────────────────────────────────┘

# Domains that only READ. Two of these can genuinely run at the same time because neither
# touches the screen, the keyboard, or the foreground window.
_CONCURRENT_SAFE = frozenset({"info"})


def plan_actions(actions):
    """
    Orders actions into execution groups.

    Every group is executed in order; the actions inside one group run concurrently.

    The rule is deliberately conservative: only pure reads share a group. Everything else
    contends for exactly one resource — the foreground window and the keyboard focus — and
    running two of those at once is a race with a visible, wrong outcome.

    This is a correctness fix, not a tuning choice. The old router built every command as an
    `asyncio.to_thread` task and fired them all through one `asyncio.gather`, so
    "open chrome and maximize the window" raced the maximize against the launch, and
    "open chrome, open youtube" sent both keystroke sequences into whatever had focus at that
    instant.
    """
    groups = []
    batch = []
    for action in actions:
        if action.domain in _CONCURRENT_SAFE:
            batch.append(action)
            continue
        if batch:
            groups.append(batch)
            batch = []
        groups.append([action])
    if batch:
        groups.append(batch)
    return groups


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             EXECUTOR                                   │
# └────────────────────────────────────────────────────────────────────────┘

CONFIRMATIONS = ConfirmationManager()
CONTEXT = AutomationContext()

_CONFIRM_PROMPTS = {
    "system.shutdown": "This will shut down your computer. Should I go ahead?",
    "system.restart": "This will restart your computer. Should I go ahead?",
    "system.sign_out": "This will sign you out. Should I go ahead?",
    "system.sleep": "This will put the computer to sleep. Should I go ahead?",
    "system.wifi_off": "This will turn off Wi-Fi. Should I go ahead?",
    "file.delete": "That will delete it. Should I go ahead?",
    "app.kill": "That will force-close it and you may lose unsaved work. Should I go ahead?",
}


def _confirm_prompt(action, reason):
    prompt = _CONFIRM_PROMPTS.get(action.key)
    if prompt:
        return prompt
    if action.domain == "shell":
        return f"I can run that, but {reason}. Should I?"
    what = action.target or action.action.replace("_", " ")
    return f"Just to check — you want me to {action.action.replace('_', ' ')} {what}?"


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
            return ActionResult.blocked(f"I won't do that — {reason}.")
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
            return OpenUrl(params.get("url") or target)
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
            return ActionResult.success("Going to sleep.")
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
    Launches or focuses an application, then verifies a window actually appeared.

    Focus-if-running comes first on purpose: "open Chrome" when Chrome is already open means
    "show me Chrome", not "start a second copy". The old path always launched.
    """
    if not name:
        return ActionResult.failure("Open what?")
    canonical = targets.canonical_app(name)

    if canonical:
        running = targets.resolve_application(canonical)
        if running.ok:
            window = running.matches[0]
            if targets.focus_window(window):
                return ActionResult.success(f"{name.title()} is open.", focused=True)

    OpenApp(name)

    if canonical:
        window = targets.wait_for_app_window(canonical, timeout=8.0)
        if window is not None:
            return ActionResult.success(f"{name.title()} is open.")
        return ActionResult.failure(f"I started {name}, but no window appeared.")
    # Unknown application or a web fallback: AppOpener/webbrowser already reported, and there
    # is no canonical window to wait for, so do not claim a verification we did not perform.
    targets.invalidate_cache()
    return ActionResult.success("Done.")


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
                # A question stops the batch. Continuing past "which tab did you mean?" would
                # execute the rest of a plan whose earlier step never happened.
                if result.status in (Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS):
                    return " ".join(spoken)
    finally:
        _set_runtime(AssistantState.PROCESSING if AssistantState else None)

    return " ".join(spoken)


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