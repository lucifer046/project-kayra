# Windows

The original and fully-featured platform: Windows 10 and 11, x64.

## Install and run

```powershell
python setup.py      # once: venv, dependencies, ONNX Runtime variant (+ CUDA on NVIDIA), .env
python run.py        # every time; no venv activation needed
python run.py --console
python run.py --doctor
```

Python 3.10–3.14 from python.org (3.11 recommended). The venv interpreter is
`.venv\Scripts\python.exe`.

## What is Windows-specific

| Area | Implementation |
|---|---|
| Hardware identity | `core/hardware.py`: registry (`CurrentVersion` with the stale-`ProductName` correction, `CentralProcessor\0`, display class GUID with 64-bit `qwMemorySize`) |
| Displays, reduced motion, active window, default browser, reveal-in-Explorer, pointer | `kayra.platforms.windows` |
| Speech-input browser | Chrome or Edge (Edge is preinstalled and carries Microsoft's backend); paths under `%PROGRAMFILES%` / `%LOCALAPPDATA%` |
| Window chrome | `ui/components/chrome.py`: frameless window answering `WM_NCHITTEST` so Aero Snap etc. still work |
| Desktop automation | `src/kayra/automation/` (pywin32, AppOpener, keyboard, pyautogui, pynput, pywhatkit, PowerShell for brightness/toasts) |
| GPU speech | `onnxruntime-gpu` + `nvidia-*-cu12` wheels; `ort.preload_dlls()` puts their DLLs on the search path |
| Signals | SIGINT/SIGTERM/SIGBREAK in console mode; `run.py`'s wrapper ignores SIGINT/SIGBREAK while waiting for its child |
| Console | UTF-8 reconfiguration + ASCII fallback for cp1252 consoles (`utils/console.py`, `run.py`, `setup.py`) |

## What changed for Windows in the cross-platform work

Behaviour is intended to be identical. Code moved, it was not rewritten:

- `Win32Injector`, the `EnumDisplaySettingsW` display query, the `SystemParametersInfoW`
  reduced-motion read, the Explorer vector and the UserChoice read moved verbatim into
  `kayra/platforms/windows.py`; the old call sites delegate to it
  (`pointer._Win32Injector` and `store.explorer_command` remain as aliases).
- `hardware.py` gates its registry paths on `winreg` being importable (always true on Windows).
- `requirements.txt` gained `sys_platform == "win32"` markers on the Windows-only packages —
  on Windows they install exactly as before.
- **mediapipe**: a fresh install now resolves to 0.10.35 (the newest release below the new
  `<1.0` ceiling), which uses the Tasks API and needs `models/hand_landmarker.task` (setup
  downloads it). An existing 0.10.14 install keeps using `mp.solutions` unchanged.
- `duckduckgo-search` is no longer installed (`ddgs` is the same client under its new name).

**Not re-verified on Windows** in this change (no Windows host was available): the unit tier,
the live UI, and `setup.py`. Dependency resolution for Windows was verified for Python
3.10–3.14 with `uv pip compile --python-platform x86_64-pc-windows-msvc`. Run
`.venv\Scripts\python tests\run_all.py` on Windows before relying on it.
