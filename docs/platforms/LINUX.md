# Linux

Supported: x86_64 Linux with glibc ≥ 2.28, Python 3.10–3.14. First-class target: **Omarchy**
(Arch + Hyprland, see [`OMARCHY.md`](OMARCHY.md)). Other Arch-based distributions and
mainstream distributions (Debian/Ubuntu, Fedora) follow the same path with different package
names; they have not been run end to end.

## What works

| Feature | Status on Linux |
|---|---|
| Conversation, live search, deep research, memory, emotion, proactive presence | ✅ identical |
| Speech output (Kokoro, CPU) | ✅ |
| Speech output on NVIDIA CUDA | ✅ setup installs `onnxruntime-gpu` + CUDA wheels; same verified-session rule |
| Speech input | ✅ via **Google Chrome** (headless). Distribution Chromium is not used by preference |
| Barge-in, endpointing, echo rejection | ✅ identical (browser-side) |
| Desktop UI | ✅ native window decorations (the Windows frameless chrome declines) |
| Proactive active-window signal | ✅ on Hyprland; elsewhere time/habit signals only |
| Hand gestures: tracking + pointer move | ✅ Hyprland IPC; uinput absolute pointer elsewhere |
| Hand gestures: click/scroll | ✅ with `/dev/uinput` access (see below) |
| System screen: OS, CPU, GPUs, displays | ✅ os-release, `/proc/cpuinfo`, sysfs, Hyprland |
| Desktop automation (apps, windows, workspaces, keys, media, volume, power) | ✅ Hyprland IPC + Wayland tools — see [`LINUX_AUTOMATION.md`](LINUX_AUTOMATION.md) |
| Voice control inside Chrome (tabs, clicks, Gmail, Meet) | ✅ with the Kayra Bridge extension — see [`CHROME_BRIDGE.md`](CHROME_BRIDGE.md) |
| Diagnostics, screen understanding, modes, plans, triggers, briefing | ✅ — see [`LINUX_AUTOMATION.md`](LINUX_AUTOMATION.md) |

## System packages

pip cannot install these; `setup.py` checks for them and prints the command.

| Purpose | Arch / Omarchy | Debian / Ubuntu | Fedora |
|---|---|---|---|
| Python 3.10–3.14 + venv | `python` | `python3 python3-venv` | `python3` |
| Audio (sounddevice) | `portaudio` | `libportaudio2` | `portaudio` |
| Speech input browser | `google-chrome` (AUR: `yay -S google-chrome`) | Google's `.deb` | Google's `.rpm` |
| Qt under X11/XWayland only | `xcb-util-cursor` | `libxcb-cursor0` | `xcb-util-cursor` |
| Reveal memory file | `xdg-utils` (+ a FileManager1 provider such as Nautilus) | same | same |

ChromeDriver is **not** needed from the distribution: Selenium Manager downloads the driver
that matches Google Chrome. (Arch's `chromedriver` package belongs to Chromium; Kayra
deliberately skips a PATH driver on Linux so it cannot be paired with the wrong browser.)

## Install and run

```bash
python3 setup.py          # creates .venv, installs, provisions ONNX Runtime/CUDA, checks the system
python3 run.py            # desktop UI
python3 run.py --console  # terminal only
python3 run.py --doctor   # interpreter, ONNX Runtime, CUDA truth, GPU
```

The venv interpreter is `.venv/bin/python`. Nothing needs to be activated.

## Gesture control and voice clicks: uinput access

Spoken clicks ("click on Save", "click at 400 300", "right click") use the same device, so
the rule below enables both.

Pointer *movement* on Hyprland uses the compositor's IPC and needs nothing. *Clicks and
scrolling* are injected through a virtual input device, which requires write access to
`/dev/uinput` (root-only by default). Grant it to the logged-in user with one udev rule:

```bash
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-kayra-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=uinput
```

`TAG+="uaccess"` gives the *active local session* access through systemd-logind ACLs, which is
narrower than adding yourself to the `input` group (that would also expose every keyboard's raw
events to your user permanently). Note the security trade-off either way: any process running
as you can then synthesise input. Without the rule Kayra logs
`Pointer: hyprland-ipc (clicks unavailable: no write access to /dev/uinput …)` and keeps
working without clicks.

## Linux-specific implementation notes

- **Active window** — Wayland has no portable query by design. Hyprland exposes one over its
  IPC socket (`kayra.platforms.hyprland`, ~0.1 ms, no subprocess); other compositors report
  the signal as unavailable.
- **Displays** — Hyprland reports each monitor's current mode and scale; otherwise the
  connected-connector count and preferred mode come from `/sys/class/drm` (scale unknown → 1.0).
- **GPU identity** — `/sys/bus/pci/devices/*` (class 0x03), names from the NVIDIA driver's
  `/proc/driver/nvidia/gpus/*/information` or `pci.ids`, AMD VRAM from `mem_info_vram_total`.
  NVIDIA VRAM is not in sysfs and shows as unknown until `nvidia-smi` telemetry overlays it.
  An AMD APU's graphics are recognised (and named) from the CPU brand string.
- **Signals** — in the desktop UI, SIGTERM/SIGINT/SIGHUP (logout, `kill`, `systemctl --user
  stop`) run the normal shutdown, which reaps the speech browser by PID.
- **Camera** — OpenCV's AUTO backend (V4L2); `GESTURE_CAMERA_BACKEND=V4L2|GSTREAMER` are
  accepted on Linux, Windows names fall back to AUTO.

## Known limitations

- Window management (focus, close, workspaces, layout) needs Hyprland; on other sessions apps,
  media and system control still work. See [`LINUX_AUTOMATION.md`](LINUX_AUTOMATION.md).
- Non-Hyprland Wayland sessions (GNOME, KDE) have no active-window signal and move the
  pointer through uinput only (absolute device; not verified on those compositors).
- `mediapipe` 1.x is excluded; see [`../architecture/DEPENDENCIES.md`](../architecture/DEPENDENCIES.md).
- The ambient floating panel is a frameless `Qt.Tool` window; tiling compositors may tile it
  unless a float rule is added (Omarchy example in [`OMARCHY.md`](OMARCHY.md)).
- Only x86_64 was tested. aarch64 Linux has wheels for most packages but was not run.
