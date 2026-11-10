# Omarchy

[Omarchy](https://omarchy.org) is an opinionated Arch Linux setup built around Hyprland. It is
Kayra's primary Linux target. Verified on Omarchy 4.5.0 (Hyprland 0.56.2, kernel
7.2.15-1-omarchy, Python 3.14.8, RTX 4060 Laptop + Radeon 780M).

## How Kayra recognises Omarchy

`kayra.platforms.detect` reports `is_omarchy=True` when any of these holds:
`/etc/os-release` has `ID=omarchy` (current releases), the kernel release contains `omarchy`,
or os-release says Arch and the Omarchy tree exists (`~/.local/share/omarchy` or
`/usr/share/omarchy`, older installs). `is_arch_based` is true either way, so `setup.py` prints
`pacman` commands. Hyprland is recognised by `HYPRLAND_INSTANCE_SIGNATURE`.

## Setup on Omarchy

```bash
sudo pacman -S --needed portaudio          # usually already present
yay -S google-chrome                       # speech input (Omarchy's default Chromium cannot transcribe)
python3 setup.py                           # system Python 3.14 is fine
python3 run.py
```

`setup.py` reports the distribution, the Wayland/Hyprland session, PortAudio, `/dev/uinput`
access, the speech browser, the NVIDIA GPU (via sysfs, then `nvidia-smi`) and provisions CUDA
for speech output when an NVIDIA card is present.

**Why Google Chrome:** Omarchy sets Chromium as the default browser. A distribution Chromium
starts a Web Speech session without error but never returns a transcript (it is not entitled
to Google's speech service). Kayra therefore does not prefer an unverified default over an
installed Google Chrome on Linux, and pins the ChromeDriver that matches Chrome (Arch's
`chromedriver` package is Chromium's).

## Hyprland integration

Everything goes through Hyprland's request socket (`kayra/platforms/hyprland.py`), never
`hyprctl` processes:

| Kayra feature | Hyprland request |
|---|---|
| Proactive "what app are you in" signal | `j/activewindow` (title + class) |
| System screen display line, gesture screen size | `j/monitors` (mode, scale, position, transform) |
| Reduced motion in the UI backdrop | `j/getoption animations:enabled` |
| Gesture pointer movement | `dispatch movecursor X Y` (logical layout coordinates) |

## Window rules

Both Kayra windows carry the app-id **`kayra`**. The dashboard is titled `Kayra`; the small
always-on-top panel is titled `Kayra Ambient`. Omarchy tiles new windows, so float the panel
(and optionally the dashboard) in `~/.config/hypr/hyprland.lua`, using Omarchy's own helper:

```lua
-- Kayra: keep the ambient orb panel floating and pinned above other windows.
o.window({ class = "^kayra$", title = "^Kayra Ambient$" }, { float = true, pin = true })
-- Optional: float the dashboard too instead of tiling it.
-- o.window({ class = "^kayra$", title = "^Kayra$" }, { float = true, center = true })
```

(Matchers and keys follow the style of Omarchy's `default/hypr/apps/*.lua`; check that file
set for your Omarchy version if Hyprland reports a config error. On Wayland the panel's own
`move()` placement is ignored by design — the compositor places windows.)

## Hand-gesture clicks

Movement works out of the box via IPC. For clicks and scrolling, grant `/dev/uinput` to your
session with the udev rule in [`LINUX.md`](LINUX.md#gesture-control-and-voice-clicks-uinput-access).

## Launching from the app launcher (optional)

Kayra does not install a desktop entry. To add one for Walker/the Omarchy menu, create
`~/.local/share/applications/kayra.desktop` (the `StartupWMClass` matches the app-id):

```ini
[Desktop Entry]
Type=Application
Name=Kayra
Exec=python3 /path/to/project-kayra/run.py
Path=/path/to/project-kayra
Terminal=false
StartupWMClass=kayra
Categories=Utility;
```

## Verified on the reference host

- `platforms.current()` → Omarchy (Arch-based), Wayland, Hyprland.
- Active window, monitor scale (2880×1800 @ 2.25), animation setting, default browser
  (`google-chrome.desktop`) all read over IPC/files.
- GPUs: RTX 4060 Laptop (NVIDIA driver 615.78.08) ranked above the Radeon 780M APU graphics.
- STT lifecycle suite: Google Chrome session, AEC applied, recovery 1.3 s, all owned processes
  reaped, nothing else touched.
- MediaPipe 0.10.35 Tasks `HandLandmarker`: ~9 ms/frame on CPU.
- `setup.py` end to end in a clean copy (Python 3.14).
