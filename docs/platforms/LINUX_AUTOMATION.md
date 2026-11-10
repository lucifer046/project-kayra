# Desktop automation on Linux (Hyprland / Omarchy)

Kayra's "hands" now run on Linux. Same voice commands, same safety rules, same confirmations
as Windows — different mechanism underneath.

```
"close chrome"
   │  DMM token
   ▼
automation/common.py      normalize_command() → Action{app.close, target=chrome}
automation/policy.py      classify_action()   → ALLOW / CONFIRM / DENY
automation/linux.py       _dispatch()         → close_target()
   │                         linux_apps.resolve("chrome") → classes {google-chrome}
   │                         Hyprland clients → 1 match (or the focused one, or ASK)
   ▼
Hyprland IPC              hl.dsp.window.close({ window = "address:0x…" })  → verified gone
```

## Layout

| Module | Role |
|---|---|
| `automation/common.py` | **Shared by both OSes.** Normalizer, power-target gate, planner, `AutomationRuntime` (policy → dispatch → record, confirmations, context, timers), filesystem handlers, safe terminal. |
| `automation/policy.py` | Unchanged contract; the shell policy now also denies `sudo`/`su`/`doas`/`pkexec`, POSIX shells, `shred`/`wipefs`/`parted`, and confirms `systemctl poweroff`/`reboot`. |
| `automation/linux_apps.py` | XDG desktop-entry index (incl. Flatpak exports) + curated alias table. Exact identities only — **no fuzzy matching**, the rule that ended "open GitHub → Git GUI" on Windows. |
| `automation/linux.py` | The Linux handlers and `_dispatch`. Every process start goes through one injectable `Runner`; every compositor call through `platforms.hyprland`. |
| `automation/windows.py` | The Win32 hands. Now imports the shared pipeline from `common.py` under the original names; behaviour unchanged. |
| `automation/__init__.py` | `load_backend()` — the one place that picks the hands for this OS. `app.bootstrap()` calls it. |
| `platforms/hyprland.py` | IPC client. `dispatch(lua, legacy)` speaks **both** syntaxes (see below). |
| `platforms/linux.py` | `automation_support()` — the readiness probe the System screen and setup use; `battery_status()` from sysfs. |
| `automation/ai.py` | The two model calls automation makes *after* deciding: `summarize()` (bounded, with a deterministic fallback) and `see()` (vision: local LM Studio first, then Gemini, then Groq). |
| `automation/diagnostics.py` | "Diagnose my computer": read-only probes → findings → AI summary → Markdown report → offered fixes; the background health watch. |
| `automation/browser_bridge.py` + `extension/kayra-bridge/` | The Chrome extension and its local WebSocket server — see [CHROME_BRIDGE.md](CHROME_BRIDGE.md). |
| `automation/web_macros.py` | Tabs, page clicks/typing/reading, Gmail, Calendar, WhatsApp Web, Google Meet. |
| `automation/screen_ai.py` | "What's on my screen?", explain an error, read aloud, copy screen text, screen recording, vision-located clicks. |
| `automation/workflows.py` | Scenes (modes), saved layouts, the AI planner, triggers, the watcher thread, the briefing, "what did you just do?", call detection. |
| `automation/linux_system.py` | Audio/mic devices, per-app volume, Bluetooth devices, Wi-Fi, updates and installs, display/input/theme, processes, Docker, git, notifications, window layouts, files. |
| `automation/status_file.py` + `extension/omarchy-kayra-widget/` | Kayra's state for the Omarchy bar pill. |

## What works, and how

| Say | Mechanism |
|---|---|
| "open Chrome / files / terminal / calculator / btop" | desktop entry via `uwsm-app -- id.desktop` (`gtk-launch` fallback); focuses an already-running window instead of starting a second copy; waits for the window to appear |
| "close Spotify", "close this window", "close all Chrome windows" | Hyprland `window.close` on an exact address, verified gone. Several matches and none focused → asks |
| "close YouTube" | focuses the browser window showing it, Ctrl+W (the tab, not the window) |
| "switch to VS Code" | Hyprland focus, most recently used window on a tie |
| "open control panel", "open settings" | Omarchy's Setup menu (`omarchy-menu toggle setup`); GNOME/KDE settings elsewhere |
| "open wifi / bluetooth / sound / power / display settings" | the matching Omarchy bar panel (`omarchy-shell shell toggle omarchy.network` …) |
| "open the system menu", "keyboard shortcuts", "theme settings" | `omarchy-menu toggle root` / `omarchy-menu-keybindings` / `omarchy-menu toggle style` |
| "maximize", "fullscreen", "snap left", "minimize", "show desktop" | fullscreen-maximized / fullscreen / swap / special:minimized / the first empty workspace |
| "go to workspace 3", "move this window to workspace 2" | `hl.dsp.focus` / `hl.dsp.window.move` |
| "open an empty workspace", "move this window to an empty workspace" | the **lowest-numbered** workspace with no window on it — windows on 1, 2, 4, 6 → **3**, never 5 |
| "make this window float", "pin window", "center window" | float toggle / pin / center |
| "type hello world", "undo", "new tab", "refresh" | `wtype` (text over **stdin**, never as an argument) |
| "pause", "next track", "what's playing?" | `playerctl`, else MPRIS over `gdbus` (Kayra's own speech browser excluded by PID) |
| "volume 40", "increase volume by 20", "mute" | `wpctl` (PipeWire), capped at 100 %; `pamixer` fallback |
| "brightness 60", "dim the screen" | `brightnessctl` (never below 5 % by voice) |
| "wifi off" (confirms), "bluetooth on" | `nmcli`, `bluetoothctl` |
| "do not disturb on", "night light on" | `omarchy-toggle-notification-silencing` / `makoctl`; `omarchy-toggle-nightlight` / `hyprsunset` |
| "take a screenshot", "screenshot a region" | `grim` (+ `slurp`) → `~/Pictures/Screenshots`, also copied to the clipboard; Kayra keeps its own last 30 |
| "copy text …", "read clipboard" | `wl-copy` / `wl-paste` |
| "lock", "turn off the screen" | `omarchy-system-lock` / `loginctl`; DPMS only when input is configured to wake it, otherwise lock (a blank screen with no way back is worse than a lock) |
| "shut down the computer" (confirms), "restart the computer" (confirms) | `systemctl poweroff` / `reboot` — only after the power-target gate named the COMPUTER and the user said yes |
| "set a timer for 5 minutes" | shared timer service → `notify-send` + chime |
| "play lo-fi beats", "google search …" | first YouTube result / search page via `xdg-open` |
| "click", "double click", "right click", "scroll down" | uinput (needs `/dev/uinput` access, see below) |
| "click on Save", "double click on report.pdf", "right click on Downloads" | screenshot of the focused monitor → `tesseract` OCR → exact words → pointer moved there → click (see **Clicking**) |
| "click at 400 300", "move mouse to center / top right / Save" | coordinates on the focused monitor (logical pixels); `move` never clicks |
| "how's my battery?", "is it charging?" | kernel sysfs: percent, **charging / discharging / plugged in but idle / full**, watts, time to full or empty, health vs design capacity, cycles |
| "how hot is my CPU?" | `psutil` sensors (+ `nvidia-smi` for the GPU); warns when running hot |
| "which Wi-Fi am I on?", "what Bluetooth devices are connected?" | `nmcli` (no rescan), `bluetoothctl devices Connected` |
| "screen resolution", "device info", "CPU / RAM / disk / uptime / IP" | Hyprland monitors; `core.hardware` + `psutil` |
| "switch to performance / balanced / power saver mode" | `powerprofilesctl` (undoable) |
| "undo what you just did", "put it back" | Kayra's own **undo stack** (see **Undo**) — a bare "undo" is still Ctrl+Z |

## Everything you can say (round 3)

Grouped like the brainstorm. Phrasings are examples — the classifier maps natural wording to
these. **🔒 = asks for your yes first.**

**A. Diagnostics and health**
| Say | What happens |
|---|---|
| "diagnose my computer" | CPU, memory, temperatures/throttling, every disk, battery wear, failed services, crashes this week, actionable kernel errors, network quality, uptime, pending updates → spoken in 2–4 sentences by the AI → full report in `Reports/diagnosis_*.md` |
| "why is my laptop slow / fan loud / battery draining / internet slow", "is my computer secure", "what crashed" | the matching slice of the diagnosis |
| "fix it" 🔒 | applies every fix the diagnosis offered: close a runaway app, balanced/power-saver profile, restart a failed *user* service, clear the thumbnail cache, empty Trash, trim the package cache in a terminal (you type the password) |
| *(background)* | full disk, CPU ≥ 97 °C for 3 minutes, lost network, a user service failing — spoken once per 2 h. `KAYRA_HEALTH_WATCH=0` turns it off |

**B. Apps, windows, workspaces**
| Say | What happens |
|---|---|
| "Chrome on the left and VS Code on the right" | both opened if needed, placed side by side on the first empty workspace |
| "what's open?", "where is Chrome?" | windows grouped by workspace |
| "move all browsers to workspace 2", "group my windows", "move this to the other screen", "make it bigger/smaller" | as said (resize is undoable) |
| "close everything except VS Code" 🔒 | closes the rest, never Kayra |
| "work mode", "study / focus / gaming / movie / presentation / night / battery saver mode" | built-in scenes (apps on workspaces + DND, volume, brightness, power profile) |
| "save this layout as study", "what modes do I have", "delete study mode" | your own scenes, in `data/scenes.json` |
| "remember my setup", "restore my setup", "restore my last session" | the layout is also saved automatically as *last session* when Kayra shuts down |

**C. Browser and web** — see [CHROME_BRIDGE.md](CHROME_BRIDGE.md): tabs (incl. background),
click by name, type, scroll, read, summarize, history, bookmarks, form fill (never passwords).

**D. Screen understanding**
| Say | What happens |
|---|---|
| "what's on my screen?", "explain this error" | screenshot of the focused monitor → local vision model (`google/gemma-4-e4b` here, ~12 s) → spoken answer. No vision model → OCR text to the text model → OCR text |
| "read this to me", "copy the text on screen" | OCR, Kayra's own window text removed |
| "click the second Save", "click the blue button" | OCR with ordinals; falls back to the vision model to locate a button without readable text |
| "record my screen", "stop recording" | Omarchy's recorder, saved to Videos |

**E. Files**
| Say | What happens |
|---|---|
| "find the PDF I downloaded yesterday", "find my resume" | name/type/date search of home (bounded), opens the folder |
| "what did I work on today?" | the desktop's recently-used list |
| "organize my downloads" 🔒 | sorts loose files into Images/Documents/Videos/…; never touches unfinished downloads, never deletes |
| "find duplicate files", "what's taking up space?" | content-hash duplicates (nothing deleted); biggest home folders |
| "summarize resume.pdf", "what does the contract say about notice period" | PDF/DOCX/text → AI summary |
| "convert report.docx to pdf", "compress this video", "resize photo.jpg by 50%" | LibreOffice / ffmpeg (in a terminal) / ImageMagick |
| "back up my documents" 🔒 | rsync to a plugged-in drive, or `~/Backups` |

**F. Communication and assistant**
| Say | What happens |
|---|---|
| "read my notifications", "what did I miss?", "clear notifications" | Kayra keeps the last 50 desktop notifications **in memory only** (never on disk) |
| "check / read my email", "draft…", "send it" 🔒, "what's on my calendar", "add … to my calendar" 🔒, "tell Rahul I'm late" 🔒 | through Chrome — see CHROME_BRIDGE.md |
| "remind me at 9 pm to call mom", "wake me at 7", "set a timer for 20 minutes" | **spoken** when due, plus a notification and a chime |
| "good morning / brief me" | time, weather, calendar and unread mail (when Chrome is connected), battery, crashes in the last day, disk warnings, scheduled automations — phrased by the AI in Kayra's own calm, precise voice |

**G. Media and audio**
| Say | What happens |
|---|---|
| "switch to my headphones / the speakers", "what audio devices do I have" | default output switched (undoable) |
| "mute my mic", "mic volume 70" | default input |
| "lower Chrome's volume", "mute Discord" | per-app stream volume |
| "connect my earbuds", "disconnect the mouse", "earbuds battery" | paired Bluetooth devices by name |
| "set a sleep timer for 30 minutes" | pauses the music when it fires |

**H. System**
| Say | What happens |
|---|---|
| "check for updates" | repo + AUR counts |
| "update my system" 🔒 | `omarchy-update` in a terminal; **refused on battery below 40 %** |
| "install VLC" 🔒, "uninstall VLC" 🔒 | `yay` in a terminal (you confirm with your password); core packages can never be removed |
| "connect to Wi-Fi Home", "forget the Cafe network" 🔒, "share the Wi-Fi password" | `nmcli` / Omarchy's Wi-Fi QR |
| "set refresh rate to 60", "scale the screen up", "rotate the screen" | `hyprctl keyword monitor` (undoable) |
| "toggle the touchpad", "switch keyboard layout", "keyboard backlight on" | Omarchy tools |
| "next theme", "switch to Catppuccin", "dark mode", "next wallpaper" | Omarchy themes (undoable) |
| "what's using my CPU / memory?", "close the app using the most memory" 🔒 | grouped per app; closing goes through the window |
| "shut down the computer in 30 minutes" 🔒, "hibernate the computer" 🔒 | a cancellable timer ("cancel timer") |

**I. Developer**
| Say | What happens |
|---|---|
| "git status", "what branch am I on", "commit with message fix login" 🔒 | the repo of the focused terminal, else `KAYRA_DEV_PROJECT`, else Kayra's own |
| "run my tests", "start the dev server" | detected command (run_all.py / npm / cargo / pytest / manage.py) in a terminal |
| "explain the error in my terminal" | screen explain |
| "docker status", "restart the db container" 🔒 | Docker |

**J. Plans and automations**
| Say | What happens |
|---|---|
| "get me ready for my presentation" | the AI writes a plan from a fixed catalog; each step is validated (no shell, no power, nothing denied), spoken, then run through the policy. A step that needs a yes pauses the plan; "no" drops the rest |
| "when the download finishes, shut down the computer" | 🔒 confirmed when created; when it fires Kayra warns and waits one minute ("cancel timer" stops it) |
| "when my battery hits 20%, switch to power saver", "when it's charged, remind me to unplug", "when I plug in…", "when I'm back online…", "every day at 9, work mode", "at 6 pm remind me…", "in 10 minutes…" | stored in `data/triggers.json`, checked every 20 s |
| "list my automations", "cancel the download automation", "cancel all automations" | as said |
| "what did you just do?", "why didn't you do that?" | from the audit log |

**K. Presence**
| | |
|---|---|
| Speaking unprompted | reminders and warnings speak at once; everything else waits (up to a minute) until Kayra is idle, **you're not on a call** (another app recording the mic; Kayra's own speech browser excluded) and not in standby — otherwise it's dropped |
| Wake word | `KAYRA_WAKE_WORD=on`: only speech naming Kayra is acted on, except follow-ups within 20 s of a reply |
| Bar widget | `python3 scripts/install_omarchy_widget.py` adds a Kayra pill (listening / thinking / speaking / paused / standby / off) to the Omarchy bar; click to bring Kayra up or start it. `--remove` takes it out |
| Spoken progress | "Running a full diagnosis…", "Setting up work mode.", "Here's the plan: …" |

## Hyprland 0.55+: Lua dispatch

Hyprland now configures itself in Lua, and `dispatch` on the socket evaluates a Lua
expression (`dispatch hl.dsp.window.close({ window = "address:0x…" })`). The classic form
(`dispatch closewindow address:0x…`) is answered with an **error string**, not a refused
connection — so a client that treats "got a reply" as success silently does nothing. That had
already broken gesture pointer movement (`movecursor`) on this machine.

`hyprland.dispatch(lua, legacy)` tries the Lua form, falls back to the legacy one, counts only
a literal `ok` as success, and remembers which syntax the compositor speaks so a 30 Hz caller
pays for one attempt, not two.

## Clicking on what you can see

"click on Save" works like a person would: look at the screen, find the word, click it.

1. `grim -o <focused monitor>` takes a screenshot (a temp file, deleted right after).
2. `tesseract --psm 11 tsv` reads every word with its box and confidence.
3. The **whole phrase** must appear on one line, word for word (no partial or fuzzy matches),
   with OCR confidence ≥ 50.
4. Matches **inside Kayra's own window are ignored** — its chat shows the very words you said.
5. One match → click it. Several → the one in the focused window; still several → Kayra asks.
6. Image pixels are converted to layout coordinates (`monitor.x + px / scale`), the pointer is
   moved with Hyprland, and the button is pressed through uinput.

**Risky buttons ask first.** If the words sound like they delete, send, pay, publish, sign out
or shut down (`Delete`, `Send`, `Pay`, `Confirm`, `Submit`, `Uninstall`…), the click is
CONFIRM-gated like any other destructive action.

**Clicks need `/dev/uinput`.** Wayland lets no ordinary program press mouse buttons; the kernel's
virtual-input device is the honest way, and it is root-only by default. Without it Kayra still
**moves the pointer to the target** and tells you clicking needs access. One-time fix (the same
rule hand-gesture clicks use):

```bash
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/60-kayra-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=uinput
```

## Undo

Every **reversible** action records how to put things back, newest first: volume (exact
previous level and mute state), brightness, Wi-Fi, Bluetooth, do-not-disturb, night light,
power profile, workspace switches, moving a window to a workspace, minimize, maximize,
fullscreen, float and pin. "undo what you just did" / "revert that" / "put it back" pops the
newest one.

- Bounded to the last **10** actions and **5 minutes** — an undo for something long ago is a
  surprise.
- Irreversible actions (closing, deleting, typing, clicking) record **nothing**, so Kayra never
  pretends it can bring them back; it says "There's nothing recent of mine to undo."
- A bare "undo" is still Ctrl+Z in the app you're using; only "undo *what you did*" phrasing
  reaches Kayra's own undo.

## Device questions

Read-only, always allowed, answered from the machine itself:

| Ask | Example answer |
|---|---|
| battery | "Battery is at 73 percent and charging at 48 watts, about 23 minutes until full. Its health is 85 percent of the original capacity." |
| battery, unplugged | "Battery is at 40 percent and running on battery, drawing 10 watts, about 2 hours left." |
| battery, plugged in but idle | "Battery is at 80 percent, plugged in but idle, not charging." |
| temperature | "Right now the processor is at 93 degrees and the graphics card is at 52 degrees. That's running hot." |
| Wi-Fi | "You're connected to Home_5G, signal 58 percent." |
| Bluetooth | "Connected: realme Buds T310." |
| displays | "1 display: eDP-2 at 2880 by 1800, 120 hertz, scale 2.25." |
| device info | "This computer is running Omarchy, with an AMD Ryzen 9 8945HS…, a NVIDIA GeForce RTX 4060 Laptop GPU, 15 gigabytes of memory. …" |

Battery details come from `platforms.linux.battery_status()` (kernel `power_supply` class,
both energy- and charge-reporting batteries; peripheral batteries such as a mouse are skipped).

## Safety

- **Every gate is shared code.** Power-target gate, confirmations, shell policy and the
  "one intent, one target" close rule live in `common.py` / `policy.py`. The test suite
  asserts a denied or unconfirmed action leaves the process log **empty**.
- **Kayra is never a target.** The UI window (class `kayra`, our PID) and the speech browser's
  PIDs are excluded from every window and media resolution.
- **No shell, no name-kill.** Argument vectors, `shell=False`, no `os.system`, no
  `pkill`/`killall`. Closing is `window.close` — what the X button does.

### Commands Kayra refuses (spoken "terminal …" / "run command …")

The shell policy (`automation/policy.py::classify_shell`) parses the command and judges what it
would actually do. On top of the original rules (no chaining/redirection, no nested shells,
protected system paths, no fork bombs, no raw disk writes), this round added:

| Refused outright (DENY) | Why |
|---|---|
| `systemctl`/`service` **stop, disable, mask, kill, isolate**, rescue/emergency targets — any service | switching off services is how a voice command takes the network, sound, login or display down |
| `loginctl terminate-*/kill-*` | ends your session |
| `kill`, `pkill`, `killall`, `xkill`, `skill` | ending processes by number or name can crash the system; closing goes through the window |
| removing a **critical package** (`linux*`, `systemd`, `glibc`, `pacman`, `hyprland`, `mesa`, `nvidia*`, `pipewire`, `networkmanager`, `omarchy*`, …) or any forced removal (`-Rdd`, `--nodeps`) | leaves the machine unbootable |
| `chmod`/`chown -R` on a system path or your home folder | locks you out |
| `rm -rf ~`, `$HOME`, `~/`, `~/../../etc`, `/usr`, … | your home folder is protected as a target; `~` paths are judged by where they really point |
| `sudo`, `su`, `doas`, `pkexec`, `run0`; `bash`/`sh`/`zsh`/`fish -c` | privilege escalation and nested shells defeat every other check |
| `mount`, `umount`, `swapoff`, `modprobe`, `rmmod`, `sysctl`, `init`/`telinit`, `kexec`, `fsck`, `mkfs.*`, `dd`, `wipefs`, `parted`, bootloader tools, `iptables`/`nft`/`ufw`, `passwd`/`useradd`/`usermod`, `chroot`, `stress` | can crash, hang, brick or weaken the system in one line |

| Asks first (CONFIRM) | |
|---|---|
| starting/restarting any service (critical ones say so) | `systemctl restart NetworkManager` |
| installing or removing ordinary software | `pacman -Syu`, `yay -R vlc`, `pip install …` |
| inline code | `python -c …`, `node -e …` (running your own `python train.py` is allowed) |
| git commands that change or discard work | `git clean -fdx`, `git reset --hard` |
| anything that can cut the network | `nmcli networking off`, `ip link set … down`, `rfkill block all` |
| power | `systemctl poweroff`, `reboot`, `shutdown` |

Allowed without asking: read-only queries — `systemctl status`, `df`, `free`, `uname`,
`nmcli device status`, `ip addr`, `pip list`, `pacman -Qi`, `git status/log/diff`, …
- **Honest failure.** A missing tool returns *"I can't … here. Install X."*, never a fake
  "Done."

## Tests

`tests/test_assistant_automation.py` — 326 checks, ~1 s: every new phrasing, every new confirmation (and that
nothing runs before the yes), the Chrome bridge over real sockets, every web flow against a
fake extension, diagnostics with fake probes and models, screen AI, scenes/plans/triggers
(including the one-minute shutdown grace), every system control at the argv level, files in a
temp folder, the wake word and the status file. `tests/test_browser_extension.py`
(integration) runs the extension's page code in a real headless Chrome.

`tests/test_linux_automation.py` — 273 checks, under 1 s, hardware-free: a fake compositor and a
recording runner, so assertions are about the exact argv / dispatch that would have reached
the machine. `test_voice_turn.py`'s power-boundary section now runs on Linux too.

## Status of the 60 brainstormed items

Implemented, with these gaps stated plainly:

| Item | Not done yet |
|---|---|
| 29 "write a summary of my week" | no weekly activity report; `content …` and the briefing exist |
| 37 smarter music | "play something calm", sleep timer and now-playing work; "like this song" and "what was the last song" do not |
| 54 follow-ups | "make it full screen / move it to workspace 3" act on the focused window (which is the one just opened), not on a remembered window address |
| 56 learns routines | uses the proactive agent's existing habit suggestions; nothing new was added |

And the ones that depend on something outside Kayra:

| Item | Status |
|---|---|
| 14–18, 30–32, 34 (Chrome, Gmail, Calendar, WhatsApp, Meet) | Built and unit-tested; the extension's page code passes in real Chrome. **Needs the one-time extension load** ("browser set up"). Meet screen-share key presses are not yet verified in a live call. |
| 19, 22 (vision) | Verified live with the local `google/gemma-4-e4b` (~12 s). Vision-located clicks need a model good at coordinates — treat as experimental. |
| Real mouse clicks (22 and every "click on …") | Need `/dev/uinput` access (one-time udev rule above). Browser clicks go through the extension and do **not** need it. |
| 59 (bar widget) | Shipped in `extension/omarchy-kayra-widget`; installed by **you** with `scripts/install_omarchy_widget.py` (it edits `~/.config/omarchy/shell.json`, with a backup). Not yet seen running in the bar. |
| 31 email | Through Gmail in your Chrome, not an email API — no account tokens are stored anywhere. |
| Windows | The new families are Linux-only for now; on Windows they answer "I don't know how to do that yet." |

## Changelog

### 2026-11-11 — round 5, part 6 (slice 6a): forms, the video, site search, shopping (extension 1.12.0)

Decisions asked first and answered by the user: **D1** shopping = search and open only (no
add-to-cart, no cart page, no checkout); **D2** the form profile may hold name, email, phone,
postal address, college, roll number, date of birth and gender.

- **D1 fields**: `form_choose` — drop-downs (native and Google Forms' `role=listbox`), radio
  options, check boxes, date fields; "set the country to India" falls back from the text box to
  the drop-down. **D2**: `page_key` (real keys via wtype). **D3**: `automation/form_profile.py`
  (0600 file, validation, spoken emails / numbers / dates, local field matching by autocomplete,
  type and label; someone-else fields and secret fields never) and `form_fill` (a late
  confirmation naming the fields; the pending action holds field numbers and keys, not values;
  never submits; says what is still empty). **D4** `form_submit` (CONFIRM; an invalid form is
  not reported as sent; a pay/order button is never pressed). **D5** `file_upload` (newest
  matching file, late confirmation naming it, a one-shot trusted-key hook in the page so a real
  Space opens the chooser, Ctrl+L + path + Enter, the name read back).
- **F1** `video` / `video_subscribe` — speed (absolute and a quarter step), seek and jump,
  captions, skip ad, next, like / unlike, watch later, full screen / theatre / mini player
  (YouTube's own keys after the page releases focus), play / pause / mute, "how much is left".
- **F2** `site_search` — "search <site> for X", "X on <site>", "images of X"; Amazon and
  Flipkart price ranges ("under 2000", "between 500 and 1000", "2k", "two thousand") and sort.
  "google search …" and "youtube search …" are unchanged. **F3** `shop_refine` (re-sort /
  filter the results in front) and `product_info` (schema.org Product, Amazon and Flipkart
  markup, Open Graph).
- **Policy**: `policy._SHOPPING_STOP` DENIES cart / buy / checkout / order / pay clicks (also
  OCR clicks); `browser.form_submit`, `browser.profile_clear`, `browser.video_subscribe` are
  CONFIRM. `interpreter._USER_ONLY`: the model can never set / forget / submit details or
  choose an upload.
- **Rules first**: `common.part_6_direct` — complete shapes skip the classifier; the
  context-dependent ones (seek, product, sort) only with a video or a shop in front.
- Tests: `test_browser_control.py` [21] (185 new), `test_browser_extension.py` [10] (37 new),
  `test_dmm_browser.py` +16 cases (95/95 on the local model, 78 rules-first).
- **Not done (slice 6b)**: F4 ask ChatGPT / Claude / Gemini, F5 WhatsApp extras (reply to the
  open chat, send a file), F6 LinkedIn / Reddit pages, G5 multi-step requests.
- **Not verified live**: no real form was filled or submitted, no file uploaded through the
  real portal chooser, no live YouTube / Amazon / Flipkart page used.

### 2026-11-11 — fix round V11, third pass from the audit log (01:19–01:23)

- **"close WhatsApp" closed the chat.** `app.close whatsapp` answered "WhatsApp isn't open"
  (its web-app window runs in Chrome's "Profile 1", so its class is not the entry's
  `StartupWMClass` `…__-Default`); the understanding layer then turned that into `message
  close`. Fixes: `linux._shows_site` — a web app's windows are matched by SITE, any profile
  (this also removes the 8 s wait on every "open whatsapp", which never saw its window);
  with no window, the site's TAB is closed through the bridge; `app.close` / `message.close`
  are never rethought by a model; `wa_close` never opens WhatsApp.
- **"open and read the last three latest recent chats of mota panda"** reached the classifier,
  which answered with its own example (`message read core team today`).
  `common.last_messages_request` is rules-first: (name, count) from "the last N messages /
  chats of|from|in X", "X's last N messages"; `wa_read(name, count=N)` reads exactly N, as
  they are. "read the last three messages" with WhatsApp in front is the open chat. Mail words
  ("emails from Ravi") are excluded.
- **A name heard wrong** ("public relation and outreach coding"): when WhatsApp's own search
  showed chats for one of the words but none matched well, the closest two are offered
  instead of "not found".
- `message open` with no name became a message to someone called "open" (the log's
  `message.send target='open'`): now not parsed.
- Tests: `test_browser_control.py` [20], the cases marked LOG (733 checks in the suite).

### 2026-11-11 — fix round V11, second pass from the audit log (extension 1.11.0)

`logs/automation.log` 01:00–01:05: every `message.open` for a chat that was not on screen
failed in ~40 ms, ran a SECOND time ~1 s later, and sometimes succeeded then.

- **Cause.** `_wa_find` typed into WhatsApp's search and read the chat list in the same
  instant, before WhatsApp had filtered it (`_ready` returned at once: the stale list was
  "non-empty"). The retry sometimes saw the first attempt's results. Fix: `chat.search`
  (`pageChatSearch`) types and waits INSIDE the page until the list has changed and held still
  for 350 ms, or says "nothing found" and holds still, or 1.6 s pass with the same list.
  A stale "nothing found" from the previous search is not trusted (found by the Chrome test).
- **Search again by words.** The whole name, then its two longest words: "general punchayat"
  finds "General Panchayat" through "general".
- **The search is always closed** after a lookup — found (`_wa_open`) or not — through
  `_wa_search_close` (`page.searchClose`, then `chat.search ""` when text is left).
- **Replace, never append.** `page.search` / `chat.search` select the box's own content with a
  Range (no dependence on the window having focus), delete, insert, and read the value back
  (`exact`); a page that refused gets the real keyboard (Ctrl+A, type).
- **`search_close` is verified.** `page.searchClose` is async and returns `left`; with text
  left the real keys are sent (Ctrl+A, Backspace, Escape) and the page asked again; still text
  → a FAILURE is said, never "closed". Wording widened: "close the whatsapp search", "delete
  the text in the search bar", "clear the search text".
- **Ran twice.** `_rethink` no longer executes a model proposal identical to the action that
  just failed.
- **New**: `message search <name> [and open it]` (`wa_search`: results said, search left open),
  `message open result <n>` ("open the first one" — the log's "first search result not the
  second one"), `message close this chat` (`chat.close`; "close the group" with WhatsApp in
  front and no tab groups — the log's three `group_close` failures).
- Tests: `test_browser_control.py` [20] now 107 checks (the log's cases are marked LOG),
  `test_browser_extension.py` [9] (a list that filters 0.5 s after typing).

### 2026-11-11 — fix round V11: scroll, search, close search, names (extension 1.10.0)

Reported by the user after using Parts 4 and 5. Each was traced through the parser first.

- **Scrolling did nothing in web apps.** `page.scroll` called `window.scrollBy`; Gmail, WhatsApp,
  Docs scroll a box inside the page. The extension now scrolls the scrollable ancestor of what
  is in the middle of the view (`where="side"`: the left list; WhatsApp: `#main` / `#pane-side`),
  else the largest scrollable box, else the window, and returns `moved` / `atEnd` / `canScroll`.
  `web_macros.page_scroll` says "Done" only when it moved, names an end as an end, and sends
  the real Page Down / Ctrl+End keys when nothing scrolls by script (Sheets' canvas).
  `common.scroll_request` parses "scroll the mail to bottom", "scroll up a bit", "scroll the
  chat list down", "go to the top of the page" (direction, where, amount); "scroll to <words>"
  is still a scroll TO those words. `linux._page_scroll`: another app in front gets real keys /
  the wheel, never a hidden Chrome tab.
- **"open graphics team group [in whatsapp]"** was a Chrome tab-group expand / an app launch.
  `common._bare_fix` + `_WA_NAMED_RE`: "… in/on/from whatsapp" is a chat from anywhere (rules
  first, and in `normalize_command` for a DMM token); "open X chat" is a chat unless an app or
  site is called that; "open X group" is `group_collapse` with `or_chat` → `web_macros.group_open`
  (a tab group when one has the name, else `wa_open`). With WhatsApp in front, "open the budget
  sheet" is a Sheet again (it was sent to a chat).
- **Editions.** `semantic_match.edition`: names that are what was said plus only numbers
  ("Graphics Team (2026-27)", "(2025-26)") are editions of one chat — the one with the latest
  message is picked, no question. One shared word ("graphics") still asks. `year_ranges`:
  "2026 2027" = "2026-27" = "2026 to 27" on both sides, for chats and files.
- **"read the recent chat"** looked for a chat called "recent". Now `recent` = the open chat,
  else the first non-pinned (or unread) row. Reading a chat with N unread reads those N
  ("2 new in …"). A chat found through WhatsApp's search leaves the search closed.
- **"click on the search button and search for X"** was one click on a button of that name.
  `common.search_request` → `search_here`; from 1.10.0 it is `page.search` (box found by
  type/role/name, magnifier pressed first when the box is hidden, cleared, typed, Enter — no
  Enter on live-filter apps, `_LIVE_SEARCH`). On the Sheets/Docs/Slides HOME list it navigates
  to Google's own search (it used to press Ctrl+F there).
- **"close the search bar"** (was: close an app called "search bar"): `browser.search_close` →
  `page.searchClose` (empty, the box's own Cancel/Clear, Escape, focus back to the open chat's
  message box on WhatsApp), highlights cleared, Gmail `#search/…` → `#inbox`, an editor's find
  bar gets Escape. Rules-first (`_DIRECT_BROWSER`).
- **File names.** `rank_titles`: a title that STARTS with the phrase 85; equal scores keep the
  page's order (most recently opened first) instead of "shortest title first". `_find_files`:
  the query is sent with numbers as digits, then without numbers, then the recent list is
  matched locally (≥ 40 only). Same-name-different-year files open the first listed; other
  near-ties still ask.
- `pageType` with an empty text and `clear` now really empties a contenteditable box (WhatsApp's
  search was left filtered after a failed lookup).
- Tests: `test_browser_control.py` [20] (78 checks), `test_browser_extension.py` [8] (real
  Chrome: inner-box scrolling, hidden search box, close search), 7 cases in `test_dmm_browser.py`.
  Not tried on live, logged-in Gmail / WhatsApp / Sheets.

### 2026-11-10 — round 5, part 5: WhatsApp, app context, the semantic matcher

- **H0.2** `common.front_site` + the rules-first route: with WhatsApp in front (web-app window or
  Chrome on web.whatsapp.com), "open X", "click on X", "go to X chat" are a chat; a known site or
  app ("open youtube") stays itself unless "chat"/"group" is said. `linux.situation` remembers
  the app the user was in for 2 minutes while Kayra's own window is in front.
- **H0.3** `automation/semantic_match.py`: order-free stemmed / prefix / near-spelling word
  overlap, spoken numbers, initials, taught aliases, local embeddings (nomic, LM Studio; meaning
  alone can only ASK), recency for ties, usage; decisions pick / ask / none.
- **H0.4** extension 1.9.0 `chat.list/open/messages/composer`; `web_macros.wa_open`, `wa_unread`,
  `wa_read` (today filter; local-only summaries), `message_send` (opens the chat, then a late
  confirmation naming it; on yes: same chat re-checked, typed, read back, Send, arrival checked).
  `message.send` left the static CONFIRM set for that reason. Fixed: a failed WhatsApp search
  left its text in the search box and filtered every later chat list; "tell rahul saying hi"
  was addressed to "tell".
- Tests: `test_browser_control.py` [19], `test_browser_extension.py` [7], `test_dmm_browser.py`
  (72/72). Not verified on a live, logged-in WhatsApp.

### 2026-11-10 — tuning pass V9: rules first, fewer round trips, live-checked lookups

- `common.direct_tokens` + `llm_engine.set_rule_router`: utterances the browser parser fully
  understands (allow-listed actions) skip the classifier — 466 ms -> < 1 ms; screen-aware for
  Sheets / Slides / "close it". `tests/test_dmm_browser.py`: 62/62 on the local model, 50 rules-first.
- Boot warm-up of the classifier and understanding prompts; per-site catalog trimming rejected
  (it would break LM Studio's prompt cache).
- Extension 1.8.0 `untilReady` (`waitMs`) for lists / comments / results / Calendar / transcript;
  `web_macros._ready`; per-command `tabs.active` cache (`new_command`); file lists reuse a tab.
- `ai.relevant` for page questions (1.1 s -> 0.74 s, answer kept); spoken numbers + near
  spellings in `rank_titles`; `spoken_email`; Sheets header row detection and `where` filters.
- Live site findings and fixes: `updates/UPDATE_V9.md` §2.

### 2026-11-10 — round 5, part 4: Google Workspace

- **E1 Sheets**: open by name (`page.docList` + local `rank_titles`; ties ask), create + name
  (Rename + real keys), Name Box (`page.focus`), total / average / highest / lowest / count of a
  column by letter or header, rows, a row read with its headers, the headers — from
  `gviz/tq?tqx=out:csv` (export as fallback) fetched in the page (`page.fetchText`,
  docs.google.com only), computed in Python. **E2 Docs**: open / search / create; read,
  summarize, ask from `export?format=txt` (the editor is a canvas); Find = the editor's Ctrl+F;
  share = Share + the address typed, the user presses Send. **E3 Slides**: present (also an
  unqualified "start presenting" when Slides is in front — decided before the share question),
  next / previous (also on the DMM fast path) / end. **E4 Drive**: search (`type:pdf`), recent,
  download (xlsx / docx / pptx / the file). **E5 Gmail**: search, unread this week/today,
  compose, labels. **E6 Calendar**: next meeting, join it (a VISIBLE Meet link only), weekday
  agendas. **E7 Meet**: admit everyone. **E8** Keep note, a form's responses, Classroom due.
- Not done from E: Sheets "add a row" / sort / chart, Docs typing beyond the existing `type`,
  Gmail "mark all as read" and attachments, Calendar "move my 3 pm to 4", Meet noise
  cancellation / layout / recording, Drive share.
- Extension 1.7.0 (no new permission). Fixed while testing: `ws_download` unpacked four values
  from a three-value helper; a hidden Meet link could have been taken as the next meeting's.
- Tests: `test_browser_control.py` [17], `test_browser_extension.py` [5].

### 2026-11-10 — round 5, part 3: about the page, bookmarks, history, downloads, "it"

- **C5** `page_ask`: a question answered from the page text by the model ("the page doesn't
  say" otherwise). **C6** `page.comments` (site table + marked-up reviews; YouTube is scrolled
  to and polled), `page.transcript` (presses "...more" and "Show transcript", polled with
  `_until`, falls back to the description). **C8** Google Translate for the page, the model for a
  selection (`page.selection`). **C9** bookmark add (folder by name, created if missing), search,
  delete (always asks; exact ids). **C10** history clear (today / hour / 15 min; always asks),
  "what did I visit". **C11** downloads: list with progress, open (asks; Kayra runs xdg-open,
  never a program or a flagged file), show in folder, pause / resume / cancel.
- **G1** `web_macros.LAST_TAB`: the tab Kayra last opened or switched to by name — "close it",
  "go back to it", "read it", "summarize it"; 10 minutes, and only while it is still open.
- **G2** `search_here`: the site in front's own search (Gmail, YouTube, Drive, GitHub repo,
  Amazon, Flipkart, Wikipedia, Reddit, Stack Overflow, Google results), Ctrl+F in Docs/Sheets,
  the page's search box, else Google. "browser search for X" now goes here.
- **G4** `page_help`: per-site hints.
- Extension 1.6.0 (+ `downloads`). Fixed: "bookmark this page" tried to OPEN a bookmark
  called "this page". DMM preamble: a question about the page is not a word find; downloads
  are not closed tabs (both misread by the live local model before the fix).
- Tests: `test_browser_control.py` [16], `test_browser_extension.py` [4].

### 2026-11-10 — round 5, part 2c: the rest of tabs and windows, results, find

- **B2** "close all YouTube tabs", "close all but pinned": more than 3 tabs asks first. New in
  the runtime: a handler can return NEEDS_CONFIRMATION with `detail["confirm"]` = the exact
  action to run on "yes" (here with the tab ids it found), and `execute_action` registers it.
- **B4** tab groups (`groups.list/create/update/ungroup`, extension 1.5.0 + `tabGroups`):
  group by topic (title/address, else page text), rename, colour, collapse/expand (one or all),
  add this tab, ungroup, close (asks above 3), list.
- **B5** `windows.detach` ("move this tab to a new window", "… to the other screen" = then a
  Hyprland move of the new window), `windows.merge`.
- **C3** `page.results`: site-aware result lists (Google, YouTube, Amazon, Flipkart, GitHub,
  Reddit, DuckDuckGo, Bing), else the Nth content link; "open the third result" with no
  `browser` prefix is caught before `app.open`.
- **C4** `page.highlight`: marks every match, scrolls to the current one, next / previous /
  clear; "find X on this page" uses it from 1.5.0.
- Tests: `tests/test_browser_control.py` [15], `tests/test_browser_extension.py` [3].
- Not tried against a live Chrome with the extension loaded.

### 2026-11-10 — round 5, part 2M: meetings and screen sharing

- **Four misroutes fixed** (the audit log of a live attempt): `common.share_request()` reads
  every way of asking to share into ONE `meet.share` (surface monitor/window/browser, a name,
  audio, "instead"); a bare token ("share screen", "click on share screen", "present now") is
  caught before the click and screen prefixes. `merge_share_steps` folds `focus X` + `meet
  share window` into one share of X, and keeps one share per utterance.
  `llm_engine._guard_meeting_code` drops a meeting code the user never said (the preamble's
  `abc-defg-hij`) and re-reads "meet join/start" as a share when the user said share.
- **Share what was named** (`web_macros.meet_share`, `automation/share_picker.py`,
  `extension/kayra-share-picker/kayra-share-picker`): a window by its Hyprland address, a
  monitor by position (left to right) or the focused one, a tab via Chrome's tab list (your
  pick), system audio (extension 1.4.0). Switching while sharing stops the old share first.
- **Readiness, not clocks**: the four fixed `SLEEP`s in `meet_share` are gone; the pickers are
  answered when they appear and the result is read from Meet's own "Stop presenting".
- **Confirmation names what goes out**; "yes, don't ask again" trusts `meet.share` (and ONLY
  it) until `meet leave`, three hours at most.
- **Do Not Disturb while presenting** (mako), undone only if Kayra turned it on.
- **Test hygiene**: `KAYRA_AUDIT_LOG` (a path, or `off`) redirects the audit log; `run_all.py`
  points it at a temp file and `_harness.py` turns it off for suites run alone. The unit tier
  no longer appends to `logs/automation.log`.
- Tests: `tests/test_browser_control.py` sections [12]-[14].
- Not verified in a live call (M0.7).

### 2026-11-10 — tuning pass: less waiting, fewer model calls, no idle churn

Measured on the reference laptop (gemma-4-e4b in LM Studio, RTX 4060 Laptop).

| What | Before | After |
|---|---|---|
| Page / email / calendar summary (`ai.summarize`) | 10.1 s | 2.1 s (direct local call, thinking off) |
| "What's on my screen" (local vision) | 7.8 s | 1.8 s (thinking off) |
| WhatsApp send, fixed pauses | 5.5 s always | as soon as each step is ready |
| Meet join / calendar add / email read, fixed pauses | 1.5-3 s each | readiness waits |
| "pause", "next tab", "volume 40" | one model call (~0.4 s local) | no model call |
| OCR screen capture | 151 ms (PNG) | 35 ms (PPM); the OCR itself is still ~1.2 s |
| Bridge frame unmasking, 600 KB | ~100 ms | 2 ms |
| `triggers.json` | rewritten every 20 s | written only on change; re-read only when its mtime moves |
| App index | re-parsed every 5 min | re-parsed when an applications folder changes |
| Bridge accept loop | one wake-up a second | none while idle |

**What changed**
- `automation/ai.py`: `_local_chat` (one keep-alive session, thinking off, the HTTP timeout is
  the bound); `summarize` uses it when a local server is up and the cloud is not forced;
  local vision sends `reasoning_effort: none`; "no local model" is cached for 30 s, not
  forever (a server started after Kayra used to be ignored for the whole run); Qwen 3.5 is
  recognised as a vision model.
- `automation/web_macros.py`: `_until` (try, short poll, try again) replaces the fixed pauses in
  WhatsApp, Meet join, calendar add and Gmail; the only fixed pauses left are the two OS
  share-picker waits in `meet_share`, which are untested live and were left alone. Unknown
  site names found by the web lookup are remembered in `data/learned_sites.json` (bounded,
  only when the host carries the spoken name). `refresh_stale_extension` reloads an
  out-of-date extension once when it connects.
- Extension 1.2.1: `pageClick` scores names first and checks visibility only for matches;
  `page.waitFor` waits on a `MutationObserver` in the page instead of re-reading it every
  250 ms.
- `automation/diagnostics.py`: probes run side by side (`PARALLEL`). `workflows.briefing`
  fetches the weather while Chrome answers for calendar and mail.
- `automation/browser_bridge.py`, `workflows.py`, `linux_apps.py`: the three idle-cost fixes
  in the table.
- `intelligence/llm_engine.py`: `fast_intent` (exact whole-utterance commands skip the model);
  the local DMM call sends `reasoning_effort: none`.

**Not done, on purpose:** the Hyprland event socket (replacing the 0.1-0.2 s window polls),
the extension pushing the active tab, capturing only the focused window for OCR (changes
which matches are found), backing off the extension's 30 s reconnect alarm (slower
auto-connect), caching `_have()` (microseconds, and tests swap the runner).

Tests: `tests/test_browser_control.py` section 8b (173 checks in the suite).

### 2026-11-10 — the understanding layer: a model works out what you meant

**Why:** everything after the DMM was rules (tables and patterns). Fast and exact for what
they know, but "open PR and outreach core team chat" with WhatsApp in front, or "make this tab
quiet", fell through to *"I couldn't find an app…"*.

**What changed**
- `automation/interpreter.py` (new): one bounded model call, given what the user said, what
  the rules tried, what is on screen and the catalog of commands. It answers automation
  commands, *chat*, *ask one question*, or *none*. Its commands are parsed and judged by the
  policy like spoken ones; power, shell, plans, triggers and file delete/move/rename are never
  accepted from it.
- `automation/common.py`: `AutomationRuntime` asks it only when a token is unparseable, only a
  guess, or ran and found nothing; `take_chat_handoff()`; unknown names go rules → model →
  web result.
- `automation/linux.py`: `situation()` (app / web app in front, Chrome's active tab).
- `app.py`: a request the model judged to be conversation is answered by the Chatbot.
- `automation/ai.py`: `complete()` returns the reply verbatim; the goal planner uses it too
  (a ```json reply used to be stripped to nothing).
- Verified live with the local model: WhatsApp chat → `browser click on PR and outreach core
  team` (7.2 s), "make this tab quiet" → `browser mute this tab` (3.8 s), "what's a good name
  for my cat" → chat (1.9 s).
- **Speed (same day):** the first version took ~4 s warm (7.2 s cold). Measured: prompt size
  barely mattered (865 vs 59 tokens: 4.0 vs 3.7 s); ~140 of ~160 output tokens were the
  model's hidden THINKING. `ai.fast()` calls the local model with thinking off, temperature 0
  and a 200-token cap, plus worked examples in the prompt: **12/12 correct, median 0.51 s,
  worst 0.78 s**. Unknown reply shapes are read as chat.
- Tests: `tests/test_browser_control.py` section 8 (scripted fake model): 130 checks.

### 2026-11-10 — round 5, part 1: open anything, anywhere, the way you say it

Plan: `docs/roadmap/BROWSER_CONTROL_PLAN.md` (items A1–A6, G3, H0.1).

**What was wrong:** "open new tab and open Google Sheet in that tab" answered *"I couldn't find
an app called google sheet"*: 23 known sites, exact aliases only, no web fallback, no "this
tab", and the two tokens raced.

**What changed**
- `automation/sites.py` (new): ~190 sites with aliases, title fragments, a category and a
  search template. `targets.WEBSITE_REGISTRY` is a view over it. Lookups now accept grammar
  (singular/plural, "the … website/app", "my …") but never misspellings; folder names
  (documents, photos) are never sites.
- `automation/common.py`: `_split_where` ("in this tab", "in a new window", "in incognito",
  "next to this tab", "in the browser"); `merge_tab_steps` turns `new tab` + `open X` into one
  open; `browser open …`, `browser update`; "open the article I read …" goes to history.
- `automation/linux.py`: "open X" → installed app → Omarchy web app → known site → top web
  result (DuckDuckGo text API, 6 s cap) → web search, said honestly; a known app that is not
  installed opens its web version (as on Windows); web-app windows (`chrome-…__-Default`) are
  browser surfaces, so clicks there go into the page; incognito/new window without the
  extension starts Chrome with `--incognito` / `--new-window`.
- `automation/linux_apps.py`: `webapp_for_url`, `webapp_url`, `is_app_window_class`,
  `site_of_window_class`.
- `automation/web_macros.py`: `open_in`, `open_site`, `web_worthy`, `bridge_update`,
  `_outdated` / self-update, `_wait_for`.
- Extension 1.2.0: `tabs.open {where: tab|window|incognito|next}` (never into an `--app`
  window), `page.waitFor {text|selector|urlIncludes|titleIncludes, timeoutMs}`.
- `intelligence/llm_engine.py`: preamble lines for sites with a location (history untouched).
- Tests: `tests/test_browser_control.py` (new, 107 at Part 1; 130 with the understanding layer), `test_browser_extension.py` +3,
  `test_linux_automation.py` "open spotify" now expects the web player.

### 2026-11-10 — round 4: browser control that actually works in Gmail

**What was wrong** (from `logs/automation.log` and your real Gmail's control names, read
through the extension without clicking anything):
1. "open the first / latest email" was classified as *read the inbox aloud*; there was no way
   to open one email, and Gmail's rows are not buttons, so no generic click could open them.
2. "click on more / load more / all mails / sent" went to the **screen** clicker (OCR + mouse),
   which cannot click without `/dev/uinput` — not to the extension inside Chrome.
3. The extension's matching missed "More" → **More labels**, "all mails" → **All Mail** (plural),
   and "load more" → **Show more messages**.

**What changed**
- Extension 1.1.0: tiered click matching (exact, plurals/synonyms, prefix, all-words, contains,
  visible-text fallback), more clickable roles, `gmail.open` (first / Nth / latest-by-date /
  oldest / last / unread / from / about), `runtime.reload`.
- `automation/common.py`: `email open …`, `email read <one email>` (opens and reads that
  message), `email read my emails` (inbox), Gmail folders, reply/forward/archive/delete/back/
  unread/star/spam; "open the first email" as a plain `open …` is caught and routed to email.
- `automation/linux.py`: any `click on …` / `scroll` with Chrome in front and the extension
  connected goes into the page; the screen clicker is only used outside the browser.
- `automation/web_macros.py`: `email_open`, `email_folder` (by URL), `email_action`, single-email
  reading, and a clear "reload the extension" message when Chrome runs an older version.
- `automation/policy.py`: deleting an email and reporting spam ask first.
- `intelligence/llm_engine.py`: preamble lines for the Gmail phrasings.

**Screen (non-browser) clicking** is unchanged and, as agreed, left for later: it needs the
`/dev/uinput` rule to press the button at all.


### 2026-11-10 — round 3: assistant features

**Added** `automation/ai.py`, `diagnostics.py`, `browser_bridge.py`, `web_macros.py`,
`screen_ai.py`, `workflows.py`, `linux_system.py`, `status_file.py`; the Chrome extension
(`extension/kayra-bridge`) and the Omarchy bar widget (`extension/omarchy-kayra-widget`,
`scripts/install_omarchy_widget.py`); `tests/test_assistant_automation.py`, `tests/test_browser_extension.py`.

**Changed**
- `automation/common.py`: new token families (`browser`, `meet`, `email`, `calendar`,
  `message`, `diagnose`, `screen`, `scene`, `plan`, `trigger`, `files`, `git`, `click on …`)
  parsed into structured actions; ~120 new "system …" phrasings, all matched before the power
  needles; clock-time reminders (`parse_clock_reminder`); `AutomationRuntime.run_steps` with
  plan pause/resume across a confirmation; the announcer (`set_announcer` / `announce`).
- `automation/policy.py`: confirmations for update/install/uninstall, forgetting Wi-Fi,
  closing many windows, closing the biggest app, other tabs, sending email/messages, calendar
  events, screen sharing, diagnosis fixes, git commits, Docker stop/restart, bulk file moves;
  the risky-click rule now covers browser clicks.
- `automation/linux.py`: dispatch for all of the above; scheduled/hibernate power; per-app
  volume; spoken timers; `start_services()` (bridge, watcher, notification log, status file)
  and their teardown, which also saves the *last session* layout.
- `intelligence/llm_engine.py`: the new tokens in `funcs` and a D3 preamble section (the
  few-shot history is untouched).
- `app.py`: `speak_unprompted()` (holds lines during turns, calls and standby) wired into the
  automation services; the optional wake word.
- `core/voice_control.py`: `passes_wake_word()`.

**Verified live on the reference laptop:** full diagnosis in 16.5 s with an AI summary and
report; morning briefing with real weather; local vision describing the screen; earbuds
battery (90 %), audio devices, mic level, git status, workspaces, a real desktop notification
captured and read back; bridge port opening and closing cleanly.


### 2026-11-10 — round 2: safety hardening, device awareness, clicking, undo

**What changed**
- `automation/policy.py`: critical-service, package, permission, kernel/boot/firewall and
  process-kill rules (tables above); home-folder protection; inline-code confirmation;
  risky-button rule for text clicks (`_RISKY_CLICK`).
- `platforms/linux.py`: `battery_status()` reads the kernel's `power_supply` class.
- `automation/common.py`: device questions phrased as "system …" become read-only `info`
  actions; new verbs `undo`, `power_profile`; "move/send … to an empty workspace" moves;
  new prefixes `click on`, `click the`, `click at`, `double click on`, `right click on`,
  `move mouse to`.
- `automation/linux.py`: battery/temperature/Wi-Fi/Bluetooth/display/summary answers,
  `powerprofilesctl`, `first_empty_workspace()`, settings screens, OCR clicking
  (`ocr_words`, `find_text`, `click_on_text`, `click_at`), the undo stack
  (`remember_undo`, `undo_last`).
- `automation/linux_apps.py`: `SETTINGS_PANELS` / `settings_panel()`.
- `intelligence/llm_engine.py`: preamble examples for the new phrasings (preamble only; the
  few-shot history is untouched).

**How it works now** — see the sections above. Verified live on the reference laptop: battery
"73 percent and charging at 48 watts, about 23 minutes until full, health 85 percent"; first
empty workspace 5 with windows on 1–4 and 6; Wi-Fi name and signal; volume 40 → 35 → undo → 40.

**Not yet verified** — real mouse clicks on this machine (`/dev/uinput` is root-only until
the udev rule above is installed); the DMM matrix with the new preamble lines.

### 2026-11-09 — round 1: the Linux port

Shared pipeline extracted to `automation/common.py`; Linux hands (`automation/linux.py`,
`automation/linux_apps.py`); Hyprland Lua dispatch; System screen findings; 143-check suite.
