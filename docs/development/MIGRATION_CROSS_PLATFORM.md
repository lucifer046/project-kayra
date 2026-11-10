# Cross-platform migration record — 2026-11-09

What changed when Kayra gained Linux (Omarchy-first) support and a Python 3.10–3.14 range,
why, and exactly what was verified. Desktop automation (`src/kayra/automation/`) was
deliberately **not** modified — `git diff --stat src/kayra/automation tests/test_automation.py
tests/test_target_resolution.py` is empty.

## Created

| File | Purpose |
|---|---|
| `src/kayra/platforms/__init__.py` | Platform façade: lazy backend selection + capability functions |
| `src/kayra/platforms/detect.py` | OS family, distro, Arch-based/Omarchy, session, Hyprland, WSL |
| `src/kayra/platforms/windows.py` | Windows backend (code moved verbatim from 5 modules) |
| `src/kayra/platforms/linux.py` | Linux backend: Hyprland, FileManager1/xdg-open, drm, mimeapps, uinput injector |
| `src/kayra/platforms/hyprland.py` | Hyprland IPC socket client |
| `src/kayra/platforms/fallback.py` | Unsupported-OS backend (all capabilities unavailable) |
| `tests/test_platforms.py` | 100 hardware-free checks for the layer |
| `docs/README.md` | Documentation index |
| `docs/architecture/{OVERVIEW,SYSTEM_DESIGN,EXECUTION_FLOW,MODULES,DEPENDENCIES,BLUEPRINT}.md` | Architecture set |
| `docs/platforms/{CROSS_PLATFORM,WINDOWS,LINUX,OMARCHY}.md` | Platform set |
| `docs/development/{INSTALLATION,CONFIGURATION,DEVELOPMENT,TROUBLESHOOTING,MIGRATION_CROSS_PLATFORM}.md` | Development set |

## Deleted

| File | Reason |
|---|---|
| `blueprint.md` | Described a `modules/` layout, `air_cursor_engine.py` and `automation_windows.py` that no longer exist; actively misleading to agents. Replaced by `docs/architecture/BLUEPRINT.md`. References in `CLAUDE.md` and `docs/KAYRA_SYSTEM_ARCHITECTURE.md` updated. |

No `__init__.py` was deleted: every one carries a package docstring, a façade or an API, and
the two empty ones (`ui/components`, `ui/views`) keep those directories regular packages (see
`docs/architecture/MODULES.md`). No other unreferenced files were found: the three legacy
smoke scripts (`tests/test_DMM.py`, `test_engine.py`, `test_voice.py`) are still listed in the
runner inventory as integration suites; `main.py` is the documented compatibility shim;
`graphify-out/` and `.agents/` are tool directories (gitignored).

## Modified — source

| File | Change | Why |
|---|---|---|
| `core/hardware.py` | Linux OS (os-release), GPUs (sysfs + `/proc/driver/nvidia` + pci.ids + amdgpu VRAM, APU naming from CPU brand), displays via platform layer; registry gates on `winreg` presence | System screen showed nothing on Linux; test suite can serve Windows fixtures on Linux |
| `core/system_profile.py` | Proactive finding via `platforms.active_window_support()`; automation finding "Windows-only for now" off Windows; POSIX system-drive; escape fix | Honest findings on Linux |
| `input/browsers.py` | Linux discovery via PATH, default via `mimeapps.list`; on Linux an unknown-backend default no longer outranks Google Chrome | Omarchy's default Chromium would be cached as "verified" and transcribe nothing |
| `input/speech_to_text.py` | Linux: matching ChromeDriver via Selenium Manager `--skip-driver-in-path`; Chrome `binary_location` set on Linux | Arch's PATH chromedriver (Chromium 155) broke Chrome 157 sessions |
| `input/gesture/detector.py` | Tasks `HandLandmarker` adapter when `mp.solutions` is absent | MediaPipe ≥ 0.10.30 removed `solutions`; only those wheels exist for 3.13+ |
| `input/gesture/pointer.py` | Injector from `platforms.pointer_injector()`; `_Win32Injector` alias kept | Linux pointer; OS code isolated |
| `input/gesture/camera.py`, `config.py` | OS-aware backend names (V4L2/GSTREAMER on Linux) | A Windows-only name made the camera unopenable on Linux |
| `memory/store.py` | Reveal vector from `platforms.reveal_command()`; `explorer_command` alias kept | Reveal works on Linux |
| `services/proactive_agent.py` | Active window via platform layer instead of pygetwindow | Context signal on Hyprland |
| `ui/components/backdrop.py` | Reduced motion via platform layer | Hyprland animation setting honoured |
| `ui/application.py` | POSIX signal bridge (wakeup fd + QSocketNotifier); Wayland app-id `kayra`; ambient panel title | SIGTERM in UI mode orphaned the speech browser; window rules on Hyprland |
| `app.py` | Automation import isolated with `except Exception`; spoken stub on non-Windows | Linux boot must not fail on an X11/Win32 import |
| `utils/text.py`, `utils/timing.py` | Invalid escape sequences fixed | `SyntaxWarning` on 3.12+, error in a future Python |
| 18 modules under `src/kayra` (ui, services, memory, output, input) | Unused imports removed (autoflake, pyflakes-verified) | Cleanup |
| `__init__.py` (package) | Docstring now names both platforms | Accuracy |

## Modified — tooling, config, docs

| File | Change |
|---|---|
| `requirements.txt` | Platform markers (`win32`: pywin32, pygetwindow, AppOpener, pyautogui, pynput, pywhatkit, keyboard; `linux`: evdev); `mediapipe>=0.10.14,<1.0`; `PySide6-Essentials<6.12` on Python < 3.12; dropped redundant `duckduckgo-search`; header documents the policy |
| `setup.py` | Linux GPU detection via sysfs; Linux system checks (distro, session, PortAudio, xcb-cursor, uinput); Linux speech-browser table; hand-model download; Python ceiling 3.14; automation row neutral off Windows; Windows-only wording fixed; removed dead `google.generativeai` probe |
| `run.py` | PySide6 install hint uses the real venv interpreter path |
| `.env.example` | Linux notes for `STT_BROWSER`, camera backend, MediaPipe |
| `tests/run_all.py` | `platforms=` per suite (automation suites `n/a:win32` off Windows), `--python PATH` for the version matrix, `test_platforms.py` registered |
| `tests/test_hardware_profile.py` | Stub `winreg` so synthetic Windows machines run on Linux; Linux real-machine checks; leaf rule allows `kayra.platforms` |
| `tests/test_browser_selection.py` | Platform-specific binary-location check; Omarchy default-Chromium ordering checks |
| `tests/test_gesture_control.py` | Hand-graph ownership rule expressed for both MediaPipe APIs |
| `tests/test_voice_turn.py` | Power-boundary section skipped where automation cannot import |
| `tests/test_environment.py` | Venv-identity check skipped during `--python` matrix runs |
| 11 other test files | Unused imports removed |
| `AGENTS.md` | Rewritten as the central agent guide (graphify section kept) |
| `CLAUDE.md` | Points to AGENTS.md; new "Cross-platform" section; layout updated |
| `README.md` | Requirements table and a "Linux and Omarchy" section |
| `tests/TESTING.md` | Linux and version-matrix section |
| `docs/KAYRA_SYSTEM_ARCHITECTURE.md` | `blueprint.md` reference removed |

## Verification

Host: Omarchy 4.5.0, Hyprland 0.56.2 (Wayland), kernel 7.2.15-1-omarchy, Ryzen 9 8945HS,
RTX 4060 Laptop (driver 615.78.08) + Radeon 780M, Google Chrome 157, Chromium 155.

| Check | Result |
|---|---|
| Dependency resolution, Python 3.10–3.14 × Windows/Linux (`uv pip compile`) | all 10 resolve |
| Unit tier, Linux, per Python version | see table below |
| `test_platforms.py` | 100/100 |
| STT lifecycle integration (`test_stt_lifecycle.py`, real headless Chrome, Linux) | all pass — Chrome session, AEC applied, recovery 1.3 s, 0 leaked processes |
| `setup.py` end to end in a clean copy (Python 3.14, non-interactive) | completed; every step correct; the CUDA wheel download was cut by a full scratch tmpfs (environmental) — CUDA then verified separately on the project `.venv` |
| `run.py --doctor` on Linux | correct interpreter, ORT, GPU telemetry |
| UI boot on Linux (offscreen) | boots to "Kayra is ready"; startup report correct |
| POSIX signal bridge | SIGTERM → `bridge.shutdown(hard=True)` in 0.03 ms under a live Qt loop |
| MediaPipe Tasks adapter | constructs and infers ~9 ms/frame (0.10.33/0.10.35 on 3.12–3.14); 1.0.0–1.2.2 SIGKILL |
| PySide6 bisection on 3.11 | 6.12.0 aborts; 6.11.3 and 6.10.3 pass 634/634 |

### Unit tier by Python version (Linux)

| Python | Key versions installed | Passed | Failed | Skipped / n/a |
|---|---|---|---|---|
| 3.10.22 | numpy 2.2.6, ORT 1.23.2 (CPU), PySide6 6.11.3, mediapipe 0.10.35 | 3736 | 0 | 7 |
| 3.11.18 | numpy 2.4.6, ORT 1.31.1 (CPU), PySide6 6.11.3, mediapipe 0.10.35 | 3736 | 0 | 7 |
| 3.12.16 | numpy 2.5.4, ORT 1.31.1 (CPU), PySide6 6.12.0, mediapipe 0.10.35 | 3736 | 0 | 7 |
| 3.13.18 | numpy 2.5.4, ORT 1.31.1 (CPU), PySide6 6.12.0, mediapipe 0.10.35 | 3736 | 0 | 7 |
| 3.14.8 (project `.venv`) | onnxruntime-gpu 1.26.0 + CUDA 12.8 wheels, PySide6 6.12.0 | 3746 | 0 | 5 |

Skips: the two automation suites (`n/a:win32`), the automation power-boundary section of
`test_voice_turn.py`, the broken-CUDA simulation in `test_tts_device.py` (nothing to simulate
when CUDA works), and, on 3.10–3.13 (run with `--python`), the "I am the project `.venv`" check
plus the CUDA-only checks. `setup.py`'s CUDA provisioning was re-run on the project `.venv`
(on disk): **real CUDA EP session verified** (`CUDAExecutionProvider`, cuBLAS/cuFFT/cudart/cuDNN 9
PASS).

### Not verified

- **Windows**: no Windows host was available. The Windows code paths were moved verbatim and
  the synthetic-Windows hardware/setup suites pass on Linux, but the unit tier, the UI and
  `setup.py` have not been re-run on Windows after this change.
- **Python 3.15**: unreleased; unsupported.
- **Gesture input on real hardware on Linux** (camera + hand, real clicks): needs a person,
  a camera session and `/dev/uinput` access; only the synthetic suites and a no-hand inference
  run were done. The Hyprland `movecursor` path was not exercised against the live desktop.
- **Non-Hyprland Linux sessions** (GNOME/KDE/X11) and non-Arch distributions: not run.
- **Live voice turn on Linux** with a model provider: not run (no API keys / local server were
  configured); the provider router and DMM suites ran against fakes.
