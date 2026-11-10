# Update V2 — Cross-platform architecture, Linux/Omarchy support, dependency modernization

| | |
|---|---|
| **Version** | V2 |
| **Based on** | V1 baseline, commit `929371b` ([`UPDATE_V1.md`](UPDATE_V1.md)) |
| **Date** | 2026-11-09 |
| **Type** | Major feature + reliability + dependency + documentation update |
| **Scope** | 57 files modified, 1 deleted, 6 new source/test files, 16 new docs (~1,350 lines added / ~540 removed in existing files; ~2,700 lines of new code, tests and docs) |
| **Not in scope** | Desktop automation (`src/kayra/automation/`) — unchanged byte for byte |
| **Committed** | No (working tree only at the time of writing) |

---

## 1. Summary

Kayra was a Windows-only assistant. V2 makes it run on **Linux**, with **Omarchy (Arch +
Hyprland)** as the first-class target, while keeping Windows behaviour the same. It does this
by moving every operating-system question behind one new layer (`kayra.platforms`), instead of
adding `if linux:` branches throughout the code. Along the way it found and fixed four
problems that affected **every** platform (MediaPipe, PySide6, signal handling, stale docs),
modernised the dependency list for Python 3.10–3.14, and rewrote the documentation for AI
agents and developers.

| Area | Before (V1) | After (V2) |
|---|---|---|
| Platforms | Windows 10/11 | Windows 10/11 + Linux x86_64 (Omarchy first) |
| Python | 3.10–3.12 (verified to 3.12) | 3.10–3.14, unit tier run on each |
| Install on Linux | `pip` failed (pywin32) | `python3 setup.py` completes |
| OS-specific code | spread across 10 modules | `src/kayra/platforms/` (+ documented exceptions) |
| Gestures on fresh install | broken (MediaPipe API removed / 1.x crash) | works (both MediaPipe APIs) |
| UI on Python 3.10/3.11 | crashed with PySide6 6.12 | capped, passes |
| Speech on CUDA (Linux) | n/a | verified |
| Unit tests (Linux) | 18/23 suites passing | all passing, 0 failures on every version |

## 2. New platform abstraction layer — `src/kayra/platforms/`

| File | Contents |
|---|---|
| `__init__.py` | Façade. Picks a backend lazily from the detected OS and exposes `current()`, `active_window()`, `active_window_support()`, `reveal_command()`, `file_manager_label()`, `prefers_reduced_motion()`, `displays()`, `default_browser_hint()`, `pointer_injector()` |
| `detect.py` | `PlatformInfo`: OS family, distribution from `/etc/os-release`, Arch-based, **Omarchy** (os-release `ID=omarchy`, kernel name, or the Omarchy tree), session type (Wayland/X11), desktop, **Hyprland**, WSL |
| `windows.py` | Windows backend — code moved **verbatim** from the modules that had it: pygetwindow active window, Explorer `/select`, `SystemParametersInfoW` animations, `EnumDisplaySettingsW` displays, UserChoice default browser, `Win32Injector` |
| `linux.py` | Linux backend: Hyprland active window/monitors/animations, FileManager1 D-Bus or `xdg-open` reveal, `/sys/class/drm` displays, `mimeapps.list` default browser, `LinuxInjector` (Hyprland `movecursor` + uinput clicks/scroll) |
| `hyprland.py` | Hyprland IPC client over its Unix socket (~0.1 ms, no `hyprctl` process spawn) |
| `fallback.py` | Any other OS (future macOS): every capability "unavailable" |

**Rules:** stdlib only, imports nothing else from `kayra`, sits *below* `core`; no import-time
side effects; unknown is reported as unknown, never guessed. Old names were kept as aliases
(`pointer._Win32Injector`, `store.explorer_command`) so nothing that used them broke.

**Modules rewired to the layer:** `core/hardware.py` (displays), `core/system_profile.py`,
`input/browsers.py`, `input/gesture/pointer.py`, `memory/store.py`,
`services/proactive_agent.py`, `ui/components/backdrop.py`.

## 3. Linux and Omarchy support

| Capability | Implementation |
|---|---|
| OS identity | os-release → "Omarchy · Version 4.5.0" |
| CPU | `/proc/cpuinfo` brand string |
| GPUs | sysfs PCI display devices; NVIDIA name from the driver, others from `pci.ids`; AMD VRAM from amdgpu; an AMD APU's graphics recognised and named from the CPU brand ("AMD Radeon 780M Graphics"); best-first ordering matches Windows |
| Displays | Hyprland monitors (real resolution and scale), else `/sys/class/drm` |
| Proactive active-window signal | Hyprland IPC (title + app class) |
| Reduced motion | Hyprland `animations:enabled` |
| Reveal memory file | Nautilus via FileManager1 D-Bus, else `xdg-open` (argument vectors, no shell) |
| Speech input | Google Chrome, with the matching ChromeDriver (see §5) |
| Speech output on GPU | `setup.py` installs `onnxruntime-gpu` + CUDA 12.8 wheels and proves a CUDA session |
| Gesture pointer | Hyprland `dispatch movecursor`; clicks/scroll through `/dev/uinput` (python-evdev) when permitted; told once, clearly, when not |
| Camera | OpenCV AUTO (V4L2); `V4L2`/`GSTREAMER` accepted on Linux |
| Desktop UI | native window decorations; Wayland app-id `kayra`; ambient panel titled `Kayra Ambient` for Hyprland window rules |
| Shutdown on signals | new POSIX signal bridge in the UI (see §5) |
| Setup | distro/session report, PortAudio and xcb-cursor checks with `pacman` commands, uinput check, Linux browser table, NVIDIA detection via sysfs then `nvidia-smi` |

## 4. Desktop automation — intentionally untouched, now isolated

`src/kayra/automation/` and its two test files were **not modified** (verified with
`git diff`). It remains Windows-only because it depends on pywin32, AppOpener, keyboard,
pyautogui, pynput, pywhatkit, `ctypes.windll` and PowerShell. Isolation was done **outside**
the package:

- Its Windows-only libraries carry `sys_platform == "win32"` markers and are not installed on Linux.
- `app.bootstrap()` catches any exception from importing it (on Linux pyautogui/pynput fail with
  Xlib errors, not `ImportError`) and substitutes a stub that **says** "Desktop control isn't
  available on this system yet." instead of staying silent.
- The System screen shows "Desktop automation — Windows-only for now"; `setup.py` shows a
  neutral row instead of a red cross.
- The test runner reports `test_automation.py` and `test_target_resolution.py` as `n/a:win32`.
- A porting plan for a future phase is in `docs/platforms/CROSS_PLATFORM.md`. Two docstrings
  inside the package emit `SyntaxWarning` on Python 3.12+ and were left for that phase.

## 5. Bug fixes

| # | Bug | Effect | Fix |
|---|---|---|---|
| 1 | Arch's `chromedriver` (for Chromium 155) on PATH was used for Google Chrome 157 | Chrome never started; Kayra fell back to Chromium | Linux resolves the matching driver with Selenium Manager `--skip-driver-in-path`; Chrome's binary is set explicitly |
| 2 | Omarchy's default browser Chromium starts a speech session but never transcribes | Kayra would pick it, cache it as "verified", and stay deaf | On Linux an unverified default no longer outranks an installed Google Chrome (Windows order unchanged) |
| 3 | In the desktop UI, `bootstrap()` runs on a worker thread, so signal handlers were never installed | SIGTERM (logout, `kill`) killed Python and **orphaned ~120 browser processes** — observed live | `ui/application.py` bridges SIGTERM/SIGINT/SIGHUP into the Qt loop with a wakeup fd + `QSocketNotifier` (no polling); measured 0.03 ms to reach the normal shutdown |
| 4 | MediaPipe ≥ 0.10.30 removed `mp.solutions`; they are the only wheels for Python 3.13+ | Gestures failed on fresh installs | Detector uses `solutions` when present, else a Tasks `HandLandmarker` adapter with the same interface; `setup.py` downloads `models/hand_landmarker.task` (verified as a zip) |
| 5 | Every MediaPipe 1.x (1.0.0–1.2.2) is SIGKILLed while building the hand landmarker on the reference host | Process dies with no traceback | `mediapipe>=0.10.14,<1.0` (0.10.33/0.10.35 measured at ~9 ms/frame) |
| 6 | PySide6 6.12.0 aborts CPython 3.10/3.11 (`Fatal Python error: none_dealloc`) | UI crash on the **recommended** Python 3.11, including fresh Windows installs | `PySide6-Essentials<6.12` for `python_version < "3.12"`; 6.11.3/6.10.3 verified on 3.11 |
| 7 | Windows-only packages had no markers | `pip install` failed on Linux | PEP 508 markers |
| 8 | Camera backend names MSMF/DSHOW used on Linux | camera unopenable | names for the other OS fall back to AUTO |
| 9 | System-drive detection compared drive letters | wrong/empty on Linux | POSIX: the drive is `/` |

## 6. Dependency and Python modernization

- **Supported Python: 3.10–3.14.** Each version resolved for both Windows and Linux with
  `uv pip compile`, installed into its own venv, and the unit tier run on Linux. Python 3.15
  is unreleased and **not** claimed. `setup.py` now warns only above 3.14.
- **Markers:** `win32` for pywin32, pygetwindow, AppOpener, pyautogui, pynput, pywhatkit,
  keyboard; `linux` for evdev (new, optional).
- **Ceilings, each with a measured reason in a comment:** `mediapipe<1.0`,
  `PySide6-Essentials<6.12` on Python < 3.12. Existing `cohere<8` re-verified with 7.3.1.
- **Removed:** `duckduckgo-search` (duplicate of `ddgs`; the code's import fallback remains).
- **No second packaging system** was added: `requirements.txt` stays the single manifest
  because `setup.py` is a bootstrap script, not setuptools.
- Resolved versions on Linux: numpy 2.2.6 (3.10) / 2.4.6 (3.11) / 2.5.4 (3.12+), onnxruntime
  1.23.2–1.31.1 (CPU) or onnxruntime-gpu 1.26.0, mediapipe 0.10.35, PySide6 6.11.3 (3.10/3.11)
  / 6.12.0 (3.12+), openai 3.46.0, cohere 7.3.1, selenium 4.52.0.

## 7. Configuration and tooling changes

| File | Change |
|---|---|
| `setup.py` | Linux GPU detection (sysfs), Linux system checks, Linux browser table, hand-model download step, Python ceiling 3.14, neutral automation row on Linux, OS-neutral wording, dead `google.generativeai` probe removed |
| `run.py` | PySide6 install hint uses the real venv path on each OS |
| `.env.example` | Linux notes for `STT_BROWSER`, camera backend, MediaPipe |
| `tests/run_all.py` | per-suite `platforms`, `--python PATH` for version matrices |

## 8. Cleanup

| Item | Action | Reason |
|---|---|---|
| `blueprint.md` | **deleted** | described a non-existent `modules/` layout; replaced by `docs/architecture/BLUEPRINT.md` |
| unused imports in 30 non-automation files (17 source, 13 tests) | removed (autoflake, pyflakes-verified) | dead code |
| 3 invalid escape sequences | fixed | `SyntaxWarning` on 3.12+ |
| `__init__.py` files | all **kept** | each carries a docstring, façade or API; the two empty ones keep `ui/components` and `ui/views` regular packages |
| Runtime folders created during testing (`data/`, `logs/`, `Reports/`) | removed | did not exist in the clone |

## 9. Security-relevant notes

- No new shell usage: every new subprocess call uses an argument vector with `shell=False`;
  the platform layer spawns no processes at all (asserted by its tests).
- The FileManager1 reveal percent-encodes the path so a quote in a filename cannot break out of
  the D-Bus argument.
- The downloaded hand model is checked to be a model bundle before it is installed (a captive
  portal page cannot be saved as a model).
- `/dev/uinput` access is documented with a `uaccess` udev rule (active-session only) rather
  than adding the user to the `input` group, and the trade-off is stated.
- Processes are still terminated only by PID tree, never by name — important on Linux where the
  user's own browser is also a Chrome.

## 10. Documentation

| File | Purpose |
|---|---|
| `AGENTS.md` (rewritten) | central guide for AI agents: overview, stack, layout, flow, 12 hard rules, conventions, dependency policy, platform workflow, testing |
| `CLAUDE.md` (updated) | points to AGENTS.md; new "Cross-platform" section with every trap found |
| `docs/README.md` | documentation index |
| `docs/architecture/` | OVERVIEW, SYSTEM_DESIGN, EXECUTION_FLOW, MODULES, DEPENDENCIES, BLUEPRINT |
| `docs/platforms/` | CROSS_PLATFORM, WINDOWS, LINUX, OMARCHY |
| `docs/development/` | INSTALLATION, CONFIGURATION, DEVELOPMENT, TROUBLESHOOTING, MIGRATION_CROSS_PLATFORM (file-by-file record) |
| `README.md`, `tests/TESTING.md` | Linux sections |
| `updates/` | this version history |

## 11. Verification

Host: Omarchy 4.5.0, Hyprland 0.56.2 (Wayland), kernel 7.2.15-1-omarchy, AMD Ryzen 9 8945HS,
NVIDIA RTX 4060 Laptop (driver 615.78.08) + Radeon 780M, Google Chrome 157.

| Python | Passed | Failed | Skipped / n/a |
|---|---|---|---|
| 3.10.22 | 3,736 | 0 | 7 |
| 3.11.18 | 3,736 | 0 | 7 |
| 3.12.16 | 3,736 | 0 | 7 |
| 3.13.18 | 3,736 | 0 | 7 |
| 3.14.8 (project venv, CUDA) | 3,746 | 0 | 5 |

Also verified: new `test_platforms.py` 100/100; STT lifecycle with real headless Chrome
(session, echo cancellation applied, recovery 1.3 s, zero leaked processes); `setup.py` end to
end on a clean copy; `run.py --doctor`; UI boot to "Kayra is ready"; real CUDA session for
speech synthesis; SIGTERM bridge; MediaPipe Tasks inference; dependency resolution for 3.10–3.14
on Windows and Linux.

## 12. Known limitations / not verified

- **Windows was not re-tested** (no Windows host). Windows code was moved verbatim and the
  synthetic-Windows suites pass on Linux, but run `.venv\Scripts\python tests\run_all.py` on
  Windows before relying on it.
- Desktop automation is Windows-only.
- Gesture control with a real hand and real clicks on Linux was not exercised.
- GNOME/KDE/X11 sessions and non-Arch distributions were not run.
- A full voice conversation on Linux was not run (no model keys or local server configured).
- NVIDIA VRAM shows as "unknown" in the hardware profile on Linux until GPU telemetry fills it.
- `CLAUDE.md` is ~250 KB and is loaded into every Claude Code session; splitting it is a
  worthwhile follow-up.

## 13. Upgrade notes for an existing install

1. `git pull`, then re-run `python setup.py` (Windows) / `python3 setup.py` (Linux). It
   repairs the environment: drops `duckduckgo-search`, applies the new version ceilings, and
   downloads the hand model if your MediaPipe needs it.
2. If your Python is 3.10/3.11 and the venv already holds PySide6 6.12:
   `python -m pip install "PySide6-Essentials<6.12"`.
3. On Omarchy: `yay -S google-chrome`; optionally the uinput udev rule and the Hyprland window
   rule from `docs/platforms/`.
