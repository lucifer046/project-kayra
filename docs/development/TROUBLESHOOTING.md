# Troubleshooting

The README's *Debugging guide* covers the general cases (setup errors, edits that do nothing,
GPU vs CPU speech, stuck "stop", self-listening, duplicate instances, leftover browsers). This
page adds the cross-platform and Linux-specific ones.

## Linux / Omarchy

**Voice input never transcribes, but Kayra says it is listening.**
Check the startup line `[STT] Backend …`. If it says *Chromium*, install Google Chrome
(`yay -S google-chrome`) or set `STT_BROWSER=chrome`. Distribution Chromium starts a speech
session and never returns text. Delete `data/browser_support.json` if an old run cached
Chromium.

**`[STT] Using Chromium rather than your default (Google Chrome), which could not start…`**
The ChromeDriver did not match Chrome. Kayra resolves the matching driver with Selenium Manager
(cached in `~/.cache/selenium`); this needs network access the first time. If it persists, run
`.venv/bin/python -c "from selenium.webdriver.common.selenium_manager import SeleniumManager as S; print(S().binary_paths(['--browser','chrome','--skip-driver-in-path']))"`.

**`OSError: PortAudio library not found`** — `sudo pacman -S portaudio`
(`libportaudio2` on Debian/Ubuntu).

**`qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`** (X11/XWayland only) —
`sudo pacman -S xcb-util-cursor` (`libxcb-cursor0`). On Wayland Qt uses its Wayland plugin.

**Gestures move the pointer but never click** — `/dev/uinput` is not writable; apply the udev
rule in [`../platforms/LINUX.md`](../platforms/LINUX.md#gesture-control-and-voice-clicks-uinput-access). The
startup log says `clicks unavailable: …` with the exact reason.

**Gestures: `hand_landmarker.task is missing`** — MediaPipe ≥ 0.10.30 needs the model file;
re-run `python3 setup.py`, or download it to `models/` from the URL in
`src/kayra/input/gesture/detector.py`.

**Gesture start crashes the process (killed, no traceback)** — a `mediapipe` 1.x got
installed. `requirements.txt` caps it below 1.0; run setup again or
`.venv/bin/python -m pip install "mediapipe<1.0"`.

**"Desktop control isn't available on this system yet."** — expected on Linux: the automation
layer is Windows-only for now. Conversation, search, research, speech and gestures work.

**The ambient panel tiles instead of floating** — add the window rule from
[`../platforms/OMARCHY.md`](../platforms/OMARCHY.md#window-rules).

**Kayra left browser processes after being killed** — `kill -9` cannot run any cleanup. Use
`kill <pid>` (SIGTERM), the tray's Quit, or say "turn off Kayra". To clean up by hand, walk
the PID tree from Kayra's ChromeDriver (`pstree -p <kayra pid>`); never `pkill chrome`, which
would close your own browser.

**Speech on CUDA fails after setup on Linux** — run `python3 run.py --doctor`. `Missing:
nvidia-…` means the CUDA wheels did not install (often a network interruption on the ~600 MB
cuBLAS wheel) — re-run `python3 setup.py`. A driver that `nvidia-smi` cannot talk to is
reported separately from a missing card.

## Python versions

**`Fatal Python error: none_dealloc` / the UI aborts on Python 3.10 or 3.11** — PySide6 6.12
has a refcount bug on those interpreters. `requirements.txt` caps it below 6.12 there; an older
venv may still hold 6.12: `.venv/bin/python -m pip install "PySide6-Essentials<6.12"` (or re-run
setup), or use Python 3.12+.

**`SyntaxWarning: invalid escape sequence` on 3.12+** from `automation/windows.py` or
`automation/targets.py` — known, harmless today, left for the automation phase (that package is
frozen). Everything else compiles clean with `-W error::SyntaxWarning`.

**Python 3.15** — not supported yet: the binary dependencies have no 3.15 wheels.
