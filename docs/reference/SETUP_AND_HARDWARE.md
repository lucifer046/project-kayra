# Launcher, setup and hardware detection

> Moved verbatim out of `CLAUDE.md` on 2026-11-10 so that file stays small enough to load
> into every agent session. Nothing was rewritten; read this before changing what it describes.
> Index of all references: [`CLAUDE.md`](../../CLAUDE.md).

## Entry points, setup and run

```
python setup.py     # ONCE: validates Python, creates .venv, installs deps, writes .env
python run.py       # EVERY TIME: finds .venv, re-execs into it, starts the assistant
```

The user never activates the virtual environment by hand.

| File | Role |
|---|---|
| `run.py` | Launcher ONLY. Two phases in one file, told apart by `sys.prefix`: phase 1 (system Python, stdlib only) locates and validates `.venv` and re-execs; phase 2 (venv Python) runs preflight, takes the single-instance lock and calls `kayra.app.main()`. No application logic lives here. |
| `setup.py` | Environment preparation ONLY, and the single source of truth for it. Runs on the SYSTEM interpreter before `.venv` exists, so it imports nothing outside the stdlib and nothing from `kayra`. Creates the venv, installs `requirements.txt`, then PROVISIONS AND VERIFIES the speech runtime: one ONNX Runtime variant, `onnxruntime-gpu` on NVIDIA machines, the pinned CUDA/cuDNN wheels that build needs, and a real CUDA session probe before claiming GPU readiness. Never overwrites `.env`, never deletes a `.venv` without an explicit "y" that defaults to no, never prints a secret. |
| `main.py` | Backward-compatibility shim → `kayra.app.main()`. Kept because `python main.py` has always worked. |
| `src/kayra/__main__.py` | `python -m kayra` → `kayra.app.main()`. |
| `src/kayra/app.py` | THE application: bootstrap, listen/route loop, lifecycle, shutdown. Still the CONSOLE front end. |
| `src/kayra/ui/` | The desktop interface (PySide6). A presentation layer over the same backend — `python run.py` starts it by default, `--console` skips it. |

Things about these that are load-bearing:

- **`run.py` re-execs rather than manipulating `sys.path`.** Native extensions (onnxruntime,
  sounddevice, numpy, psutil, mediapipe) are ABI-bound to the interpreter that installed them;
  pointing `sys.path` at another environment's `site-packages` fails deep inside a native
  import with a message unrelated to the cause.
- **A child process, not `os.execv`.** On Windows `execv` does not replace the process — it
  spawns a new one and lets the original exit, so the console returns to the prompt while Kayra
  is still running and Ctrl+C no longer reaches it.
- **The phase-1 wrapper ignores SIGINT/SIGBREAK while waiting.** Console signals go to the whole
  process group, so both processes get them; the assistant runs a real shutdown and the wrapper
  must survive to report its exit code. Before this, Ctrl+C reported `0xC000013A` no matter how
  cleanly the child exited.
- **The interpreter is proved by `sys.prefix`, and the MODULES are checked too.** Being on the
  venv interpreter does not prove `kayra` came from this project — a global install or a stale
  `PYTHONPATH` produces a `.venv` Python importing someone else's code, and the symptom is
  edits that appear to do nothing. `preflight()` compares `kayra.__file__` against `src/` and
  refuses when they differ.
- **`python run.py --doctor`** prints the interpreter, environment, ORT package/version/
  location, every provider, whether CUDA is VERIFIED usable (a real session, not a provider
  name), and the GPU telemetry — then exits. This is the thing to ask for when speech output is
  on the wrong device.
- **Startup reports the interpreter and the ORT variant**, read from package METADATA rather
  than by importing `onnxruntime` (which costs ~200ms on the cold-start path). Two ORT variants
  installed at once is reported as an error, because they share one package directory and
  whichever was installed last silently wins.
- **A relaunch loop guard** (`KAYRA_RELAUNCHED`). One relaunch is legitimate; a second means
  detection disagrees with reality, and forking forever is the worst form of the duplicate
  instance the lock exists to prevent.
- **The single-instance lock is `(pid, create_time)`, not a bare PID.** Shutdown ends in
  `os._exit(0)`, which skips every `finally`, so the lock file is ALWAYS left behind — staleness
  detection has to be exact rather than relying on cleanup. Windows recycles PIDs.
- **Both scripts degrade to ASCII on a legacy console.** They print box-drawing and check-mark
  glyphs, which raise `UnicodeEncodeError` on cp1252 — and did, in the single most important
  path `run.py` has: a user with no `.venv` got a traceback instead of the "run setup.py"
  instructions. Each reconfigures its streams to UTF-8 where possible AND probes what the stream
  can actually encode. `utils/console.py` does the same for every other entry point, which is
  what keeps the test suites runnable on a legacy console.

### NVIDIA components are installed only when an NVIDIA GPU exists (2026-09-09)

`detect_graphics()` reads the SAME registry keys `core.hardware` reads, and everything
downstream follows from it: which ONNX Runtime variant is installed, whether four CUDA runtime
wheels are downloaded, whether a CUDA session is probed, and whether the report says PASS or
NOT APPLICABLE.

**The logic is deliberately duplicated rather than imported.** `setup.py` runs on the SYSTEM
interpreter before `.venv` exists and imports nothing outside the stdlib and nothing from
`kayra`; importing the application to decide how to install the application would invert that.
`winreg` is stdlib, so the same keys are read directly. `tests/test_setup_runtime.py` asserts
setup's verdict for this machine matches `kayra.core.hardware.has_nvidia_gpu()`, which is what
keeps the duplicate honest.

- **TWO SOURCES, TWO QUESTIONS.** The registry says whether the CARD is present; `nvidia-smi`
  says whether the DRIVER STACK works. `nvidia-smi` alone was the previous gate, and a machine
  with an NVIDIA card and a stale driver was told it had no GPU — an accurate outcome reached
  by an inaccurate route, reporting a cause the user could not act on. It now says the card is
  present and the driver did not answer, and provisions CPU meanwhile.
- **`nvidia-smi` is spawned only after the registry has already found an NVIDIA adapter**, so
  an AMD or Intel machine launches no process at all to learn it has no NVIDIA GPU.
- **Non-NVIDIA machines are RECONCILED, not merely tolerated.** The old behaviour KEPT an
  inherited `onnxruntime-gpu` because it "runs on the CPU perfectly well" — true, and not the
  whole story: it leaves CUDA libraries nothing on that machine can load, offers a GPU mode
  that cannot work, and makes `--doctor` report a CUDA build on a machine with no CUDA. The GPU
  variant is replaced with the CPU one and the four `nvidia-*` runtime wheels are removed.
  That removal is safe in a way the ORT variant swap is not: the `nvidia-*` distributions own
  their own files under `site-packages/nvidia/`, share no RECORD entries, and are pure runtime
  payload.
- **The report distinguishes PASS / FAIL / NOT APPLICABLE.** Three lines used to read FAIL on
  every non-NVIDIA machine, describing a defect that does not exist and sending the user
  looking for a fix with nothing to fix. `state["cuda_applicable"]` is what separates "the CUDA
  check failed" from "there was never a CUDA check to run", and the CPU outcome is presented as
  "correct for this hardware" rather than as a fallback.
- **The report names whatever graphics hardware is present, in its own terms** — one line per
  adapter, on every machine. It never says NVIDIA on a machine without one.
- **Idempotent in both directions**, asserted: a settled NVIDIA environment on an NVIDIA
  machine performs no install and no uninstall, and neither does a settled CPU environment on a
  non-NVIDIA one. A machine that GAINS an NVIDIA GPU is upgraded on the next run; one that
  loses it is reconciled down.

## Universal hardware and OS detection (`src/kayra/core/hardware.py`, added 2026-09-09)

**The System screen said "Windows 10" on a Windows 11 machine, and reported "not reported by
Windows" for the video memory of an 8 GiB card.** Both were the same defect: the cheapest
source was consulted and it lied.

`core.hardware` is the single source of truth for the OS product/version/build, the
processor's real marketing name, and every graphics adapter with its vendor, its true VRAM and
its driver version. It is a LEAF — stdlib only plus an optional psutil — so anything may
import it, and it spawns no process, opens no socket and imports nothing heavy.

### The `ProductName` trap — this is the Windows 10/11 bug

Every cheap source lies in the SAME direction on Windows 11:

| Source | Reports on this Windows 11 machine |
|---|---|
| registry `ProductName` | `Windows 10 Home Single Language` |
| `platform.release()` | `10` |
| `sys.getwindowsversion().major` | `10` |

Microsoft froze `ProductName` for application compatibility and has never updated it. The
BUILD number does not lie: Windows 11 is documented as build **22000 and above**.

- **The correction is narrow BY DESIGN.** `_windows_product_name()` rewrites the product only
  when the recorded name is one of the values Microsoft is known to leave stale AND the build
  proves a newer product. `Windows 11 Pro` is left alone; `Windows 10 Pro` on build 19045 is
  left alone; a future `Windows 12 Home` that names itself honestly is left alone. It is a
  correction, not a blanket "build ≥ X means 11 forever".
- **Server SKUs are excluded**, because the client thresholds do not describe them — Server
  2022 is build 20348 and Server 2025 is 26100, so a client rule would mislabel both.
- **When the build cannot be read the answer is `Windows` with no version.** Showing less is
  the correct outcome; a plausible default is the failure this module exists to end.
- The System screen previously printed `(build 10)` from `platform.release()`. `os_release` is
  RETAINED in the profile because callers exist, and is never shown as a build again.

### VRAM, and why the number was blank

`Win32_VideoController.AdapterRAM` is a 32-bit field. Drivers CLAMP it, so this host's 8 GiB
RTX 4060 reported `4293918720` (4095 MiB) — not the full-scale `0xFFFFFFFF`, so a naive
ceiling test does not catch it. `system_profile` correctly refused to trust anything at or
above ~4000 MiB and therefore showed a blank for every modern card.

The registry publishes `HardwareInformation.qwMemorySize`, which is 64-bit and **not clamped**:
`8585740288` for the same card. The 32-bit field is now read only when the 64-bit one is
absent (very old drivers), and a saturated value from it is still refused.

### Everything is read from the registry, and the cost is the headline

| | Before (batched PowerShell/CIM) | After (registry) |
|---|---|---|
| CPU name, OS product, GPU | **4410 ms** | **0.4 ms** |
| VRAM on an 8 GiB card | "not reported" | 8585740288 bytes |
| Adapters seen | 1 (highest clamped `AdapterRAM` — a coin toss on a switchable-graphics laptop) | all of them |

The keys: `Windows NT\CurrentVersion` (build, UBR, DisplayVersion, EditionID, ProductName),
`HARDWARE\DESCRIPTION\System\CentralProcessor\0` (branded name, CPUID vendor), and the
display class GUID `{4d36e968-…}` (DriverDesc, qwMemorySize, DriverVersion, MatchingDeviceId).

- **The vendor comes from the PCI id, not the name.** `pci\ven_10de` is NVIDIA whatever the
  driver calls the card; a marketing string can be rebranded or localised.
- **`integrated` is TRI-STATE.** True, False, or **None** when neither the name nor the vendor
  settles it. A small VRAM figure is not sufficient — plenty of discrete cards report a
  carve-out — so an unrecognised adapter reports "unknown" rather than a guess.
- **Software adapters are never "the GPU".** A machine whose only adapter is the Microsoft
  Basic Display Adapter has no usable GPU, and saying so beats naming the shim.
- **`displays()` uses `EnumDisplaySettingsW`, not `GetSystemMetrics`.** The metric returns the
  DPI-virtualised size unless the process declared per-monitor awareness — measured here as
  1440x900 on a 2880x1800 panel at 200%. That is not a resolution, it is what a scaled window
  thinks the desktop is. **1920x1080 is never a default**; an unmeasurable display reports 0.
- **The system drive comes from `%SystemDrive%`.** `live_metrics` used
  `os.path.abspath(os.sep)`, which resolves against the CURRENT WORKING DIRECTORY's drive — so
  running Kayra from `D:\` reported D:'s usage as the system disk.
- Non-Windows degrades to `platform`/`psutil` and SAYS SO in `source`, which is what lets a
  caller (and the suite) tell a measurement from a fallback. There is no branch that assumes
  Windows.

### The profile is additive, and the UI reads it

`system_profile.device_profile()` keeps every key it had — `os_name`, `os_release`,
`cpu_name`, `gpu_name`, `vram_total` all still mean what they meant — and adds `os_product`,
`os_build`, `os_display_version`, `cpu_vendor`, `gpu_vendor`, `gpu_integrated`, `gpu_driver`,
`gpus` (the full list), `has_nvidia`, `monitor_count`, `screen_*` and a `system` flag per disk.
Nothing that read the profile had to change.

- **`os_summary()` returns `(product, version)` already formatted**, so no screen composes an
  OS line itself. Two writers to one fact is how the voice caption went wrong once already.
- **`profile_if_ready()` never blocks, and `warm_profile()` collects on a one-shot daemon
  thread.** Home reads the machine identity on its 1.5s tick, on the GUI thread; collection is
  now dominated by `sounddevice` device enumeration (**122 ms** measured) and 122 ms on the
  GUI thread is a visible hitch. Home asks for the profile only once it is ready and simply
  does not paint the line until then — there is nothing true to put in it yet.

### Home's Graphics card is vendor-neutral

THREE sources, answering three different questions, and none substitutes for another:

| | Question | Availability |
|---|---|---|
| `graphics_profile()` | what hardware IS this | every vendor, registry, cached |
| `gpu_metrics()` | what is it DOING | NVIDIA only (`nvidia-smi`) |
| `tts_provider()` | what is SPEECH on | the live engine |

The card previously had only the second, so **an AMD or Intel machine was told it had no GPU.**
It now names the adapter from the profile, overlays live telemetry when there is any, and says
"<vendor> telemetry unavailable" when there is not. The empty state is reached only when there
is genuinely no adapter at all.

- **A missing measurement is never drawn as a zero.** An unreported utilization is captioned
  "not reported", because a 0% bar reads as an idle GPU rather than as one whose vendor does
  not tell us. Unmeasured VRAM shows the installed capacity, or "shared with system memory"
  for an integrated part.
- **`nvidia-smi` is gated on `hardware.has_nvidia_gpu()` before the first spawn.** The absence
  used to be learned from a `FileNotFoundError` — a correct answer reached the expensive way,
  and the WRONG answer on a machine that has the tooling but not the card.

### Kayra's only GPU acceleration path is CUDA, and that is stated rather than implied

An AMD or Intel machine runs speech on the processor. That is a supported, correct outcome —
Kokoro synthesizes at roughly real time on a modern CPU — and the Graphics finding says so in
its advice rather than leaving the user to infer a fault.
