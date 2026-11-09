# Cross-Platform Architecture

Kayra was written for Windows. Linux support (Arch/Omarchy first) was added in November 2026
by **isolating OS-specific behaviour behind one layer** rather than branching throughout the
code. This page is the strategy; [`WINDOWS.md`](WINDOWS.md), [`LINUX.md`](LINUX.md) and
[`OMARCHY.md`](OMARCHY.md) are the per-platform details.

## The strategy in four rules

1. **Shared logic is OS-agnostic.** Conversation, intent, search, research, memory, the voice
   state machine, the endpointer, the provider router, the UI — none of them know which OS
   they are on.
2. **OS questions go through `kayra.platforms`.** One façade, three backends with identical
   function names (`windows.py`, `linux.py`, `fallback.py`), selected at runtime from
   `platforms.current().family`. Stdlib only, imported by `core` and above, imports nothing
   from `kayra` itself.
3. **Platform-specific dependencies carry markers** in `requirements.txt`
   (`sys_platform == "win32"` / `"linux"`), so one manifest installs cleanly everywhere and no
   Windows-only wheel is ever attempted on Linux.
4. **Absent is reported, never faked.** A capability the platform cannot provide answers
   `None` / `(False, reason)`, and the UI and logs say "unavailable" with the reason.

## The contract

| Function | Windows | Linux | fallback (macOS, other) |
|---|---|---|---|
| `current()` | `PlatformInfo(family="windows")` | + distro, Arch-based, Omarchy, session, Hyprland, WSL | family only |
| `active_window()` | pygetwindow → `(title, "")` | Hyprland IPC → `(title, class)` | None |
| `active_window_support()` | pygetwindow installed? | Hyprland present? | False |
| `reveal_command(path)` | `explorer.exe /select,<path>` | `gdbus … FileManager1.ShowItems` or `xdg-open <dir>` | None |
| `prefers_reduced_motion()` | `SPI_GETCLIENTAREAANIMATION` | Hyprland `animations:enabled` | None |
| `displays()` | `EnumDisplaySettingsW` (DPI-proof) | Hyprland monitors (real scale) or `/sys/class/drm` | zeros |
| `default_browser_hint()` | UserChoice ProgId | `mimeapps.list` https handler | None |
| `pointer_injector()` | `Win32Injector` (user32) | `LinuxInjector` (Hyprland movecursor + uinput) | None |

Other OS-dependent code that is **not** in the layer, and why:

| Where | What | Why it stays |
|---|---|---|
| `core/hardware.py` | registry readers (Windows), os-release + sysfs readers (Linux) | the hardware model is core's job; it delegates displays to the layer |
| `input/browsers.py` | per-OS browser locations and default-id → family tables | domain knowledge about browsers; the OS lookup is the layer's |
| `input/speech_to_text.py` | Linux: driver pinned with Selenium Manager `--skip-driver-in-path` | specific to how the STT engine launches its browser |
| `output/tts_device.py` | `preload_dlls` (DLLs on Windows, `.so` on Linux), `CREATE_NO_WINDOW` | ONNX Runtime's own API already abstracts it |
| `ui/components/chrome.py` | WM_NCHITTEST frameless chrome (Windows only; native decorations elsewhere) | Qt-native-event code, already guarded |
| `ui/application.py` | POSIX signal bridge (wakeup fd + QSocketNotifier) | Qt event-loop integration |
| `run.py`, `setup.py` | venv layout, OS checks, GPU detection | must run before `kayra` is importable (stdlib only) |
| `automation/` | apps, windows, keys, media, power | `common.py` shared; `windows.py` / `linux.py` per OS — see below |

## Automation

Ported 2026-11-09. One pipeline, two sets of hands:

| Layer | Windows | Linux |
|---|---|---|
| normalizer, power gate, planner, executor, timers, files | `automation/common.py` (shared) | same |
| policy | `automation/policy.py` (shared) | same |
| apps | Start-Menu index, AppOpener (exact names) | XDG desktop entries, `uwsm-app` (exact names) |
| windows | pywin32 (`WM_CLOSE`, focus) | Hyprland IPC (`hl.dsp.*`, legacy fallback) |
| keys / typing | `keyboard`, pynput | `wtype` |
| media / volume / brightness | media keys, WMI | MPRIS (`playerctl`/`gdbus`), `wpctl`, `brightnessctl` |
| power | `shutdown.exe` | `systemctl`, `loginctl`, `uwsm` |

| Assistant features (diagnostics, Chrome bridge, screen AI, scenes, plans, triggers, briefing) | not yet | `diagnostics`, `web_macros`, `screen_ai`, `workflows`, `linux_system` |

`automation.load_backend()` chooses; `app.bootstrap()` calls it. Details, the full command
table and the roadmap: [`LINUX_AUTOMATION.md`](LINUX_AUTOMATION.md).

## Paths, environment, temp files, permissions

- **Paths:** `core/paths.py` anchors everything on the repository root on both OSes. It does
  not move user data into XDG directories: doing so would orphan existing `data/` stores, and
  the repo-local layout is what every other doc and tool assumes. All joins use `os.path` so
  separators are native.
- **Venv interpreter:** `.venv\Scripts\python.exe` vs `.venv/bin/python`, chosen in
  `run.py`, `setup.py`, `core/paths.venv_python()` and `tests/run_all.py`.
- **Environment:** `.env` is parsed once by `core/config`; Linux needs no extra variables.
  Platform detection reads `XDG_SESSION_TYPE`, `WAYLAND_DISPLAY`, `DISPLAY`,
  `XDG_CURRENT_DESKTOP`, `HYPRLAND_INSTANCE_SIGNATURE`, `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`.
- **Temp files:** `tempfile` everywhere (no hard-coded `/tmp` or `%TEMP%`).
- **Executable bits:** `run.py` and `setup.py` are launched as `python3 run.py`, so no `+x` is
  required; they carry no shebang dependence.
- **Processes:** ownership is by PID tree on both OSes (`psutil`); nothing is killed by name.
- **Signals:** console mode installs SIGINT/SIGTERM (+SIGBREAK on Windows) on the main thread;
  UI mode on POSIX bridges SIGTERM/SIGINT/SIGHUP into the Qt loop.

## Verification status

See [`../development/MIGRATION_CROSS_PLATFORM.md`](../development/MIGRATION_CROSS_PLATFORM.md)
for exactly what was run, on which OS and Python, and what was not.
