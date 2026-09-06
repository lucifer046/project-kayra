# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation_windows.py                          │
# │                  System Control & Keystroke Core                       │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module acts as the "Hands" of the AI, executing high-priority system automation scripts.

Core Frameworks & Architectures:
1. Hardware-Level Input Injection: Simulates low-level keyboard inputs globally across any application.
2. Asynchronous Task Concurrency: Leverages asyncio thread pooling to run multiple automations in parallel.
3. Win32 Windows Subsystem Tracing: Safely targets, focuses, and destroys specific tabs or standalone windows.
4. Centralized Supply Linkage: Drops local redundant clients, routing text generation tasks back to the engine.
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
from dotenv import dotenv_values

# Robust relative path import vectors for unified ecosystem execution
try:
    from .llm_engine import CentralizedLLMEngine
except ImportError:
    try:
        from modules.llm_engine import CentralizedLLMEngine
    except ImportError:
        from llm_engine import CentralizedLLMEngine

try:
    from .utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
except ImportError:
    try:
        from modules.utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
    except ImportError:
        from utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console

# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if __file__ else "."
env_vars = dotenv_values(os.path.join(root, ".env")) or {}
username = env_vars.get("USERNAME", "User")

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
    Advanced target window trace mechanism.
    Intercepts window title attributes via low-level Win32 tracking callbacks.
    """
    app_target = app.lower().strip()
    
    if "file explorer" in app_target or app_target == "explorer":
        cmd = "powershell -Command \"(New-Object -ComObject Shell.Application).Windows() | ForEach-Object { $_.Quit() }\""
        subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL)
        return True

    if "settings" in app_target:
        subprocess.run("taskkill /f /im SystemSettings.exe", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        return True

    # 1. First Priority: Search open windows by title to catch browser tabs or active visible apps
    try:
        from ctypes import windll, create_unicode_buffer
        WM_CLOSE = 0x0010
        target_hwnds = []

        def enum_windows_proc(hwnd, _):
            if windll.user32.IsWindowVisible(hwnd):
                length = windll.user32.GetWindowTextLengthW(hwnd)
                buf = create_unicode_buffer(length + 1)
                windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                if app_target in buf.value.lower():
                    target_hwnds.append((hwnd, buf.value))
            return True

        enum_cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(enum_windows_proc)
        windll.user32.EnumWindows(enum_cb, 0)

        if target_hwnds:
            for hwnd, title in target_hwnds:
                is_browser = any(b in title.lower() for b in ["chrome", "edge", "firefox", "brave", "opera"])
                # Safe close tabs if browser window context maps directly
                if is_browser and not any(ext in app_target for ext in ["chrome", "edge", "browser", "firefox", "brave"]):
                    print_warning(f"Isolating active browser window tab focus hook: '{title[:20]}'")
                    
                    # Bypass Windows ForegroundLockTimeout restriction by injecting a dummy ALT keystroke
                    windll.user32.keybd_event(0x12, 0, 0, 0) # ALT down
                    windll.user32.keybd_event(0x12, 0, 2, 0) # ALT up
                    
                    # Only restore the window if it is currently minimized to the taskbar
                    if windll.user32.IsIconic(hwnd):
                        windll.user32.ShowWindow(hwnd, 9) # Restore window state
                        
                    windll.user32.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
                    keyboard.press_and_release("ctrl+w")
                    print_success(f"Closed browser tab targeting: '{app_target}'")
                else:
                    print_info(f"Sending hardware-level close interrupt signals to: '{title[:20]}'")
                    windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True
    except Exception as e:
        print_error(f"Advanced Win32 title callback tracing crashed: {e}")

    # 2. Try AppOpener to close known local apps cleanly if no active windows matched
    from AppOpener import close as appclose
    try:
        _old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            appclose(app_target, match_closest=True, output=True, throw_error=True)
            success = True
        except:
            success = False
        finally:
            sys.stdout.close()
            sys.stdout = _old_stdout
        
        if success:
            print_success(f"Successfully closed application: '{app_target}'")
            return True
    except:
        pass

    # 3. Aggressive taskkill fallback logic matrix for non-critical targets
    critical_blocks = ["explorer", "winlogon", "services", "svchost", "python", "py"]
    if app_target in critical_blocks:
        return False
    
    if app_target.endswith(".exe"):
        subprocess.run(f"taskkill /f /im {app_target}", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        subprocess.run(f"taskkill /f /im {app_target}.exe", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return True

def _set_brightness(target):
    """Directly sets monitor brightness to a specific percentage level."""
    try:
        target = max(0, min(100, target))
        set_cmd = f'powershell -Command "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {target})"'
        subprocess.run(set_cmd, shell=True)
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
    """Sets system volume to an absolute percentage using a zero-out calibration approach."""
    try:
        target = max(0, min(100, target))
        for _ in range(50):
            keyboard.press_and_release("volume down")
        steps = target // 2
        for _ in range(steps):
            keyboard.press_and_release("volume up")
            time.sleep(0.01)
        print_success(f"System volume calibrated and set directly to {target}%")
    except Exception as e:
        print_error(f"Absolute hardware volume configuration failed: {e}")

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
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    elif "shutdown" in cmd:
        os.system("shutdown /s /t 0")
    elif "restart" in cmd:
        os.system("shutdown /r /t 0")
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
        get_cmd = 'powershell -Command "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness"'
        res = subprocess.run(get_cmd, capture_output=True, text=True, shell=True)
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
    """Copies arbitrary text directly to the system clipboard without typing it."""
    try:
        import subprocess
        process = subprocess.Popen('clip', stdin=subprocess.PIPE, shell=True)
        process.communicate(text.encode('utf-8'))
        print_success(f"Text copied to clipboard: '{text[:30]}...'")
        return True
    except Exception as e:
        print_error(f"Clipboard text injection failed: {e}")
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

def SystemInfo(query):
    """Retrieves real-time system telemetry data from the Windows kernel."""
    cmd = query.lower().strip()

    if "battery" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "(Get-WmiObject Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus | Format-List)"',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                # Parse the output for clean display
                lines = [l.strip() for l in output.split('\n') if l.strip()]
                for line in lines:
                    if "EstimatedChargeRemaining" in line:
                        percent = line.split(":")[-1].strip()
                        print_success(f"Battery Level: {percent}%")
                    elif "BatteryStatus" in line:
                        status_code = line.split(":")[-1].strip()
                        status = "Charging" if status_code == "2" else "Discharging" if status_code == "1" else "Unknown"
                        print_info(f"Power State: {status}")
            else:
                print_warning("No battery detected. This may be a desktop system.")
        except Exception as e:
            print_error(f"Battery telemetry query failed: {e}")

    elif "ip" in cmd or "wifi" in cmd or "network" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike \'*Loopback*\' } | Select-Object IPAddress, InterfaceAlias | Format-Table -AutoSize"',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                print_success(f"Active Network Interfaces:\n{output}")
            else:
                print_warning("No active network interfaces detected.")
        except Exception as e:
            print_error(f"Network telemetry query failed: {e}")

    elif "disk" in cmd or "storage" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{N=\'Used(GB)\';E={[math]::Round($_.Used/1GB,2)}}, @{N=\'Free(GB)\';E={[math]::Round($_.Free/1GB,2)}} | Format-Table -AutoSize"',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                print_success(f"Disk Usage Report:\n{output}")
        except Exception as e:
            print_error(f"Disk telemetry query failed: {e}")

    elif "ram" in cmd or "memory" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "$os = Get-WmiObject Win32_OperatingSystem; $total = [math]::Round($os.TotalVisibleMemorySize/1MB,2); $free = [math]::Round($os.FreePhysicalMemory/1MB,2); $used = [math]::Round($total - $free, 2); Write-Output \\"RAM: $used GB used / $total GB total ($free GB free)\\""',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                print_success(output)
        except Exception as e:
            print_error(f"Memory telemetry query failed: {e}")

    elif "cpu" in cmd or "processor" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "Get-WmiObject Win32_Processor | Select-Object Name, NumberOfCores, LoadPercentage | Format-List"',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                print_success(f"CPU Telemetry:\n{output}")
        except Exception as e:
            print_error(f"CPU telemetry query failed: {e}")

    elif "uptime" in cmd:
        try:
            res = subprocess.run(
                'powershell -Command "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; $uptime = (Get-Date) - $boot; Write-Output \\"Uptime: $($uptime.Days)d $($uptime.Hours)h $($uptime.Minutes)m\\""',
                capture_output=True, text=True, shell=True
            )
            output = res.stdout.strip()
            if output:
                print_success(output)
        except Exception as e:
            print_error(f"Uptime query failed: {e}")

    else:
        print_warning(f"Unrecognized system info query: '{cmd}'")
    return True

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     TIMER & REMINDER ENGINE                             │
# └────────────────────────────────────────────────────────────────────────┘

def SetTimer(command):
    """Sets a countdown timer that triggers a Windows notification toast when complete."""
    cmd = command.lower().strip()

    # Parse duration from natural language (e.g. "5 minutes", "30 seconds", "1 hour")
    match = re.search(r'(\d+)\s*(second|sec|minute|min|hour|hr)s?', cmd)
    if not match:
        print_warning(f"Could not parse timer duration from: '{cmd}'")
        return False

    value = int(match.group(1))
    unit = match.group(2)

    if unit in ["second", "sec"]:
        total_seconds = value
    elif unit in ["minute", "min"]:
        total_seconds = value * 60
    elif unit in ["hour", "hr"]:
        total_seconds = value * 3600
    else:
        total_seconds = value

    print_success(f"Timer armed for {value} {unit}(s). Countdown initiated.")

    def _timer_worker(seconds, label):
        time.sleep(seconds)
        # Trigger Windows 10/11 native toast notification via PowerShell
        toast_cmd = f'''powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Timer Complete: {label}', 'KAYRA Timer', 'OK', 'Information')"'''
        subprocess.Popen(toast_cmd, shell=True)
        print_success(f"Timer complete: {label}")

    label = f"{value} {unit}(s)"
    timer_thread = threading.Thread(target=_timer_worker, args=(total_seconds, label), daemon=True)
    timer_thread.start()
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
        if "off" in cmd or "disable" in cmd or "disconnect" in cmd:
            subprocess.run("netsh wlan disconnect", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run('netsh interface set interface "Wi-Fi" disable', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print_success("Wi-Fi adapter disabled. Network disconnected.")
        elif "on" in cmd or "enable" in cmd or "connect" in cmd:
            subprocess.run('netsh interface set interface "Wi-Fi" enable', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print_success("Wi-Fi adapter enabled. Reconnecting to network...")
        else:
            print_warning(f"Unrecognized Wi-Fi command: '{cmd}'")
    except Exception as e:
        print_error(f"Wi-Fi adapter control failed: {e}")
    return True

# ┌────────────────────────────────────────────────────────────────────────┐
# │                 ASYNCHRONOUS ORCHESTRATOR CHANNELS                     │
# └────────────────────────────────────────────────────────────────────────┘

async def translate_and_execute(commands: list[str]):
    """Iterates through dynamic array strings scheduling concurrent automation execution targets."""
    tasks = []

    for command in commands:
        cmd_str = command.strip()
        cmd_lower = cmd_str.lower()
        
        # 1. Catch Global Type Injections First
        if cmd_lower.startswith("write ") or cmd_lower.startswith("type "):
            tasks.append(asyncio.to_thread(global_desktop_type, cmd_str))

        # 2. Match standard processing automation headers
        elif cmd_lower.startswith("open "):
            if "open it" in cmd_lower or "open file " in cmd_lower:
                pass # Skip explicit file/it opens as per routing rules
            else:
                tasks.append(asyncio.to_thread(OpenApp, cmd_str.removeprefix("open ").strip()))
        elif cmd_lower.startswith("general "):
            pass # Skip general command flag

        # NOTE: These two exact-match literal tokens MUST be checked BEFORE the generic
        # "close " prefix branch below — otherwise "close window"/"close tab" (which both
        # start with "close ") get shadowed and routed to CloseApp("window")/CloseApp("tab"),
        # which tries to close an app literally named "window"/"tab" instead of sending the
        # intended Alt+F4 / Ctrl+W hotkey.
        elif cmd_lower == "close window":
            tasks.append(asyncio.to_thread(WindowManage, cmd_str))
        elif cmd_lower == "close tab":
            tasks.append(asyncio.to_thread(HotkeyShortcut, cmd_str))
        elif cmd_lower.startswith("close "):
            tasks.append(asyncio.to_thread(CloseApp, cmd_str.removeprefix("close ").strip()))
        elif cmd_lower.startswith("play "):
            tasks.append(asyncio.to_thread(PlayYoutube, cmd_str.removeprefix("play ").strip()))
        elif cmd_lower.startswith("content "):
            tasks.append(asyncio.to_thread(Content, cmd_str.removeprefix("content ").strip()))
        elif cmd_lower.startswith("youtube search "):
            tasks.append(asyncio.to_thread(YoutubeSearch, cmd_str.removeprefix("youtube search ").strip()))
        elif cmd_lower.startswith("google search ") or cmd_lower.startswith("web search "):
            prefix = "google search " if cmd_lower.startswith("google search ") else "web search "
            tasks.append(asyncio.to_thread(WebSearch, cmd_str.removeprefix(prefix).strip()))
        elif cmd_lower.startswith("system "):
            tasks.append(asyncio.to_thread(ExecuteCommand, cmd_str.removeprefix("system ").strip()))

        # 3. Screenshot capture
        elif "screenshot" in cmd_lower or "screen capture" in cmd_lower or "take screenshot" in cmd_lower:
            tasks.append(asyncio.to_thread(TakeScreenshot))

        # 4. Clipboard operations
        elif cmd_lower in ["copy", "copy that"]:
            tasks.append(asyncio.to_thread(ClipboardCopy))
        elif cmd_lower in ["paste", "paste that"]:
            tasks.append(asyncio.to_thread(ClipboardPaste))
        elif cmd_lower.startswith("copy text "):
            tasks.append(asyncio.to_thread(ClipboardCopyText, cmd_str.removeprefix("copy text ").strip()))

        # 5. Window management ("close window" is handled by the exact-match branch above)
        elif any(k in cmd_lower for k in ["minimize all", "show desktop", "snap left", "snap right",
                "switch window", "alt tab", "task view", "maximize", "minimize",
                "action center", "notification", "emoji"]):
            tasks.append(asyncio.to_thread(WindowManage, cmd_str))

        # 6. Media playback controls
        elif any(k in cmd_lower for k in ["pause", "resume", "next track", "previous track",
                "skip track", "stop media", "play media", "play pause"]):
            tasks.append(asyncio.to_thread(MediaControl, cmd_str))

        # 7. System information queries
        elif any(k in cmd_lower for k in ["battery", "ip address", "disk", "storage",
                "ram", "memory", "cpu", "processor", "uptime", "network info", "wifi status"]):
            tasks.append(asyncio.to_thread(SystemInfo, cmd_str))

        # 8. Timer and reminder
        elif cmd_lower.startswith("timer ") or cmd_lower.startswith("set timer ") or cmd_lower.startswith("remind"):
            tasks.append(asyncio.to_thread(SetTimer, cmd_str))

        # 9. Hotkey shortcut injection ("close tab" is handled by the exact-match branch above)
        #
        # The exact-match tuple carries the tokens HotkeyShortcut's map supports but the
        # keyword test below cannot safely look for as substrings: "save" is a substring of
        # nothing useful but "search" appears inside "google search ..." and "print" inside
        # ordinary prose. Matching them only as whole commands dispatches them (they used to
        # fall through to the unmapped-token warning and silently do nothing) without
        # shadowing the google/youtube search branches above.
        elif cmd_lower in ("save", "search", "print") or any(k in cmd_lower for k in [
                "undo", "redo", "select all", "save file",
                "find", "new tab", "refresh", "reload", "fullscreen",
                "zoom in", "zoom out", "reset zoom", "task manager", "run dialog"]):
            tasks.append(asyncio.to_thread(HotkeyShortcut, cmd_str))

        # 10. Wi-Fi adapter control
        elif "wifi" in cmd_lower or "wi-fi" in cmd_lower:
            tasks.append(asyncio.to_thread(ToggleWifi, cmd_str))
            
        # 11. Direct matching pass-through handling for system commands
        elif any(k in cmd_lower for k in ["volume", "brightness", "mute", "lock", "shutdown", "restart", "sleep"]):
            tasks.append(asyncio.to_thread(ExecuteCommand, cmd_str))
        else:
            print_warning(f"Skipping unmapped system loop command token: '{cmd_str}'")

    if tasks:
        # Launch concurrent worker channels instantly across system layers
        await asyncio.gather(*tasks)

async def Automation(commands: list[str]):
    """Public wrapper to access asynchronous macro-processing tasks."""
    await translate_and_execute(commands)
    return True

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