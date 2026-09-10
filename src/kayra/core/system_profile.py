# ┌────────────────────────────────────────────────────────────────────────┐
# │                          system_profile.py                             │
# │      Structured Device Facts, Live Metrics & Capability Analysis       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything the System screen needs, as DATA rather than as sentences.

RELATIONSHIP TO `automation.windows.SystemInfo`
-----------------------------------------------
Kayra already reads the machine in one place: `SystemInfo(query)` in the automation layer.
This module does not replace it and does not duplicate its purpose, because the two have
different output contracts and only one of them can be right for each caller:

  * `SystemInfo` answers a SPOKEN question. It returns one short sentence ("Battery is at 84
    percent, discharging.") and prints a formatted block. That is exactly right for voice and
    useless for a dashboard, which needs numbers it can lay out, compare and threshold.
  * This module returns structured values with units, and never formats or prints anything.

The shared substrate is psutil, which both call directly. Refactoring `SystemInfo` onto this
module was considered and rejected: it is a stable, heavily-tested path (263 automation checks
depend on its behaviour), the overlap is a handful of psutil calls rather than real logic, and
a UI feature is not a good reason to disturb the voice path.

COST DISCIPLINE
---------------
Static hardware facts are collected ONCE and cached for the life of the process. The CPU model,
the amount of installed RAM and the GPU do not change while Kayra is running, and the one
genuinely expensive probe here - a single PowerShell/WMI call for the display adapter - is
therefore paid once at first use rather than on every refresh.

Live metrics use psutil ONLY. There is no subprocess anywhere on the refresh path, because the
System screen updates on a timer and a PowerShell spawn per refresh is precisely the 300-900ms
mistake the automation layer already removed once.
"""

import os
import sys
import time

import platform
import threading
import functools

try:
    import psutil
except Exception:                       # pragma: no cover - psutil is a hard dependency
    psutil = None


# Capability verdicts, ordered worst to best so a UI can sort or colour by severity.
NOT_AVAILABLE = "NOT_AVAILABLE"
REQUIRES_CONFIGURATION = "REQUIRES_CONFIGURATION"
LIMITED = "LIMITED"
GOOD = "GOOD"
READY = "READY"

_VERDICT_RANK = {NOT_AVAILABLE: 0, REQUIRES_CONFIGURATION: 1, LIMITED: 2, GOOD: 3, READY: 4}

# Collected once, under a lock. See `device_profile`.
_PROFILE = None
_PROFILE_LOCK = threading.Lock()
_CPU_PRIMED = False


class Finding:
    """One checked fact, with the reason it reached its verdict."""

    __slots__ = ("subsystem", "verdict", "summary", "detail", "advice")

    def __init__(self, subsystem, verdict, summary, detail="", advice=""):
        self.subsystem = subsystem
        self.verdict = verdict
        self.summary = summary
        self.detail = detail
        self.advice = advice

    @property
    def rank(self):
        return _VERDICT_RANK.get(self.verdict, 0)

    def to_dict(self):
        return {"subsystem": self.subsystem, "verdict": self.verdict,
                "summary": self.summary, "detail": self.detail, "advice": self.advice}

    def __repr__(self):
        return f"<Finding {self.subsystem}={self.verdict}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        STATIC DEVICE FACTS                             │
# └────────────────────────────────────────────────────────────────────────┘

# The GPU, the CPU name and the OS product used to be read here with one batched PowerShell
# CIM call. That call measured **4.41 s** on this host and was wrong about the two facts users
# actually look at: `Win32_VideoController.AdapterRAM` is a 32-bit field that drivers clamp
# (an 8 GiB card reports 4095 MiB), and selecting the single highest-AdapterRAM adapter is a
# coin toss on a switchable-graphics laptop where both are clamped.
#
# `core.hardware` reads all of it from the registry instead: **0.4 ms**, the true 64-bit VRAM,
# every adapter with its PCI vendor, and an OS product name corrected for the `ProductName`
# staleness that made this screen say "Windows 10" on Windows 11. There is no PowerShell on
# this path any more — see `core/hardware.py` for the full account.


@functools.lru_cache(maxsize=1)
def _audio_devices():
    """(output_count, input_count). sounddevice is already a dependency for TTS playback."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        outputs = sum(1 for d in devices if d.get("max_output_channels", 0) > 0)
        inputs = sum(1 for d in devices if d.get("max_input_channels", 0) > 0)
        return outputs, inputs
    except Exception:
        return 0, 0


def device_profile():
    """
    Static facts about this machine. Collected once, cached for the process lifetime.

    Every field is optional in practice: a missing value is reported as None or 0 and the UI
    renders "unknown" rather than a fabricated default.

    THREAD SAFETY. This is deliberately NOT `functools.lru_cache`. That decorator does not hold
    a lock across the wrapped call, so two threads that miss simultaneously BOTH execute the
    body and the loser's result silently replaces the winner's. For a pure function that is
    merely wasteful; here the body makes a ~3s WMI call, so it meant paying the most expensive
    probe in the codebase twice and handing two callers different dict objects.

    That is not hypothetical: the UI reaches this from two directions at once — the System
    screen's background loader and any other caller — and the test suite caught it immediately.
    The double-checked lock below makes the collection happen exactly once per process.
    """
    global _PROFILE
    if _PROFILE is not None:
        return _PROFILE
    with _PROFILE_LOCK:
        if _PROFILE is not None:        # another thread finished while we waited
            return _PROFILE
        _PROFILE = _collect_profile()
    return _PROFILE


_WARMING = None


def profile_if_ready():
    """
    The static profile IF it has already been collected, else None. NEVER blocks.

    Home reads the machine's identity on its 1.5s tick, and that tick runs on the GUI thread.
    Collection is now dominated by one thing — enumerating audio devices through
    `sounddevice`, measured at ~150 ms — and 150 ms on the GUI thread is a visible hitch on
    the screen's first paint. So the paint path asks this, gets None on the first tick, and
    gets the real answer a moment later. Nothing is displayed wrongly in the meantime; the
    line is simply not painted until there is something true to put in it.
    """
    return _PROFILE


def warm_profile():
    """
    Collect the static profile on a background thread, once.

    A ONE-SHOT DAEMON THREAD, NOT A NEW PERSISTENT ONE. It runs the same collection any
    caller would have run, exits, and is never started again — `device_profile()` is idempotent
    behind its lock, so a caller that arrives first simply does the work itself and the warmer
    finds it already done.
    """
    global _WARMING
    if _PROFILE is not None or _WARMING is not None:
        return
    def work():
        global _WARMING
        try:
            device_profile()
        except Exception:
            pass                        # a warmer must never take a screen down
        finally:
            _WARMING = None
    _WARMING = threading.Thread(target=work, name="kayra-profile-warm", daemon=True)
    _WARMING.start()


def _collect_profile():
    """
    One dict describing this machine. Every value is measured or absent; none is assumed.

    THE DICT IS THE CONTRACT and it is additive. The keys that existed before this pass
    (`os_name`, `os_release`, `cpu_name`, `gpu_name`, `vram_total`, ...) all still exist and
    still mean what they meant, so nothing that reads the profile had to change; the new keys
    beside them carry what the old shape could not express — the OS build, the feature-update
    version, the GPU's vendor, and the full adapter list on a machine with more than one.
    """
    from kayra.core import hardware

    os_facts = hardware.os_info()
    cpu_facts = hardware.cpu_info()
    adapters = hardware.gpu_adapters()
    gpu = hardware.primary_gpu()
    monitors, screen_w, screen_h, screen_scale = hardware.displays()

    profile = {
        # ── Operating system ──
        # `os_name` stays the bare platform family ("Windows") because callers use it for
        # branching. `os_product` is the corrected, user-facing product ("Windows 11").
        "os_name": platform.system(),
        "os_product": os_facts.name,
        "os_edition": os_facts.edition,
        "os_display_version": os_facts.display_version,
        "os_build": os_facts.build,
        "os_build_revision": os_facts.revision,
        "os_build_text": os_facts.build_text,
        "os_version": platform.version(),
        # RETAINED FOR COMPATIBILITY, AND NO LONGER SHOWN AS A BUILD. `platform.release()` is
        # "10" on Windows 11 — the exact value that made the System screen read
        # "(build 10)". It is kept because callers exist; `os_build` is what a screen shows.
        "os_release": platform.release(),
        "os_source": os_facts.source,
        "is_server": os_facts.is_server,

        "architecture": platform.machine(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),

        # ── Processor ──
        "cpu_name": cpu_facts.model or "Unknown processor",
        "cpu_vendor": cpu_facts.vendor,
        "cpu_cores": cpu_facts.cores,
        "cpu_threads": cpu_facts.threads,
        "cpu_max_mhz": cpu_facts.max_mhz,
        "cpu_source": cpu_facts.source,

        # ── Memory ──
        "ram_total": 0,

        # ── Graphics ──
        # `gpu_name` / `vram_total` describe the PRIMARY adapter, so the existing single-GPU
        # readers keep working unchanged. `gpus` is the whole list for anything that wants it.
        "gpu_name": gpu.name if gpu else None,
        "gpu_vendor": gpu.vendor if gpu else "",
        "gpu_integrated": gpu.integrated if gpu else None,
        "gpu_driver": gpu.driver_version if gpu else "",
        "vram_total": gpu.vram_total if gpu else 0,
        "gpus": [a.to_dict() for a in adapters],
        "gpu_vendors": list(hardware.gpu_vendors()),
        "has_nvidia": hardware.has_nvidia_gpu(),

        # ── Displays ──
        "monitor_count": monitors,
        "screen_width": screen_w,
        "screen_height": screen_h,
        "screen_scale": screen_scale,

        "disks": [],
        "audio_outputs": 0,
        "audio_inputs": 0,
        "collected_at": time.time(),
    }

    if psutil is not None:
        try:
            profile["ram_total"] = int(psutil.virtual_memory().total)
        except Exception:
            pass
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except (PermissionError, OSError):
                    continue            # empty optical drive / unreadable mount
                profile["disks"].append({
                    "mount": part.mountpoint,
                    "filesystem": part.fstype,
                    "total": int(usage.total),
                    "used": int(usage.used),
                    "free": int(usage.free),
                    "system": _is_system_drive(part.mountpoint),
                })
        except Exception:
            pass

    profile["audio_outputs"], profile["audio_inputs"] = _audio_devices()
    return profile


def _is_system_drive(mountpoint):
    """
    Is this the drive the operating system is installed on?

    Read from the environment rather than compared against a literal `C:`. Windows can be
    installed anywhere, and on this developer's own machine the project lives on `D:` while
    the system is on `C:` — a hardcoded drive letter would be right here by luck and wrong
    for anyone who moved theirs.
    """
    try:
        system_root = os.environ.get("SystemDrive") or os.path.splitdrive(sys.executable)[0]
        if not system_root:
            return False
        return os.path.splitdrive(os.path.abspath(mountpoint))[0].upper() ==             os.path.splitdrive(system_root + os.sep)[0].upper()
    except Exception:
        return False


def system_drive():
    """
    The mount point of the drive the OS is on, for anything that needs "the" disk.

    `live_metrics` used `os.path.abspath(os.sep)`, which resolves against the CURRENT WORKING
    DIRECTORY's drive — so running Kayra from `D:\` reported D:'s usage as the system disk.
    """
    root = os.environ.get("SystemDrive")
    if root:
        return root + os.sep
    return os.path.abspath(os.sep)


def os_summary():
    """
    The two lines a screen shows for the operating system, already formatted and never wrong.

    Returns `(product, version)` — for example `("Windows 11 Home Single Language",
    "Version 25H2  ·  Build 26200.9445")`. When the version could not be measured the second
    string is empty and the caller shows only the product: an OS line with no build is
    truthful, and "Windows 10" on a Windows 11 machine is not.
    """
    facts = _os_facts()
    return facts.display_name, facts.version_text


def _os_facts():
    from kayra.core import hardware
    return hardware.os_info()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            LIVE METRICS                                │
# └────────────────────────────────────────────────────────────────────────┘

_PROC_CACHE = {"at": 0.0, "value": (0, 0)}


def kayra_footprint(max_age=4.0):
    """
    (process_count, resident_bytes) for this Kayra process and everything it owns.

    Walking the process tree is the expensive part, so the result is cached briefly: the System
    screen refreshes faster than the footprint meaningfully changes, and the browser session
    alone is nine processes to stat.
    """
    now = time.time()
    if now - _PROC_CACHE["at"] < max_age:
        return _PROC_CACHE["value"]
    if psutil is None:
        return 0, 0
    try:
        me = psutil.Process()
        family = [me] + me.children(recursive=True)
        total = 0
        alive = 0
        for proc in family:
            try:
                total += proc.memory_info().rss
                alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        value = (alive, total)
    except Exception:
        value = (0, 0)
    _PROC_CACHE["at"] = now
    _PROC_CACHE["value"] = value
    return value


def live_metrics():
    """
    Values that change while Kayra runs. psutil only - no subprocess on this path, ever.

    `cpu_percent()` is called WITHOUT an interval, so it returns the load since the previous
    call and never blocks. A blocking `interval=0.1` here would stall the UI thread on every
    refresh for no extra accuracy.
    """
    global _CPU_PRIMED
    if not _CPU_PRIMED and psutil is not None:
        # `cpu_percent(interval=None)` reports load since the PREVIOUS call. The very first
        # call has no previous, so psutil measures since process start and typically returns
        # 0.0 or a meaningless 100.0 — the dashboard opened showing a red 100% processor bar
        # that had nothing to do with the machine. Priming once discards that first reading.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        _CPU_PRIMED = True

    metrics = {
        "cpu_percent": 0.0, "ram_percent": 0.0, "ram_used": 0, "ram_total": 0,
        "disk_percent": 0.0, "disk_used": 0, "disk_total": 0,
        "kayra_processes": 0, "kayra_memory": 0,
        "battery_percent": None, "battery_plugged": None,
        "uptime_seconds": 0.0, "gpu_percent": None,   # None = not reliably measurable
    }
    if psutil is None:
        return metrics
    try:
        metrics["cpu_percent"] = float(psutil.cpu_percent(interval=None))
    except Exception:
        pass
    try:
        memory = psutil.virtual_memory()
        metrics.update(ram_percent=float(memory.percent),
                       ram_used=int(memory.total - memory.available),
                       ram_total=int(memory.total))
    except Exception:
        pass
    try:
        usage = psutil.disk_usage(system_drive())
        metrics.update(disk_percent=float(usage.percent),
                       disk_used=int(usage.used), disk_total=int(usage.total))
    except Exception:
        pass
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            metrics["battery_percent"] = float(battery.percent)
            metrics["battery_plugged"] = bool(battery.power_plugged)
    except Exception:
        pass
    try:
        metrics["uptime_seconds"] = time.time() - psutil.boot_time()
    except Exception:
        pass

    metrics["kayra_processes"], metrics["kayra_memory"] = kayra_footprint()
    return metrics


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       CAPABILITY ANALYSIS                              │
# └────────────────────────────────────────────────────────────────────────┘
# Three questions are answered SEPARATELY, because collapsing them into one number is what
# makes this kind of screen useless:
#
#   COMPATIBILITY  - can Kayra operate correctly on this device at all?
#   PERFORMANCE    - how comfortably can this device run it?
#   READINESS      - is THIS installation configured and ready right now?
#
# A powerful machine with no API keys is highly capable and not ready. A modest laptop that is
# fully configured is completely ready and merely slow. One blended percentage would describe
# neither, so each is scored from its own findings and every score can be traced to the checks
# that produced it.

# Thresholds. Named and gathered here so the scoring is auditable rather than buried in
# conditionals, and so the System Guide can quote the same numbers the analysis used.
MIN_RAM = 4 * 1024 ** 3
COMFORTABLE_RAM = 8 * 1024 ** 3
GENEROUS_RAM = 16 * 1024 ** 3
MIN_THREADS = 4
COMFORTABLE_THREADS = 8
MIN_FREE_DISK = 2 * 1024 ** 3


def _compatibility_findings(profile):
    """Can Kayra run correctly here? Hard requirements only."""
    out = []

    is_windows = profile["os_name"] == "Windows"
    product, version = os_summary()
    out.append(Finding(
        "Operating system",
        READY if is_windows else LIMITED,
        # The CORRECTED product name, never `platform.release()`. That value is "10" on a
        # Windows 11 machine, and this finding used to fall back to it whenever the edition
        # string was unavailable — which is how a Windows 11 laptop was told it ran Windows 10.
        product,
        version or "Kayra's automation layer is built on Win32 APIs.",
        "" if is_windows else
        "Voice and conversation work, but window and application control are Windows-only."))

    arch = (profile["architecture"] or "").lower()
    known_64 = arch in ("amd64", "x86_64", "arm64")
    out.append(Finding(
        "Architecture", READY if known_64 else LIMITED, profile["architecture"] or "unknown",
        "64-bit is required by the ONNX speech model runtime.",
        "" if known_64 else "A 32-bit interpreter cannot load the speech model."))

    ram = profile["ram_total"]
    if ram >= COMFORTABLE_RAM:
        verdict, advice = READY, ""
    elif ram >= MIN_RAM:
        verdict, advice = LIMITED, ("Kayra runs, but the browser speech session and the voice "
                                    "model together want about 1GB.")
    else:
        verdict, advice = NOT_AVAILABLE, "Below the practical minimum for the speech subsystems."
    out.append(Finding("Memory", verdict, _gib(ram),
                       f"Minimum {_gib(MIN_RAM)}, comfortable {_gib(COMFORTABLE_RAM)}.", advice))

    outputs, inputs = profile["audio_outputs"], profile["audio_inputs"]
    if outputs and inputs:
        verdict, advice = READY, ""
    elif outputs:
        verdict, advice = LIMITED, "No microphone detected - voice input will fall back to typing."
    elif inputs:
        verdict, advice = LIMITED, "No audio output detected - Kayra cannot speak responses."
    else:
        verdict, advice = NOT_AVAILABLE, "Neither input nor output audio devices were detected."
    out.append(Finding("Audio devices", verdict, f"{outputs} output / {inputs} input",
                       "Speech input needs a microphone; spoken replies need an output device.",
                       advice))

    free = max((d["free"] for d in profile["disks"]), default=0)
    out.append(Finding(
        "Storage", READY if free >= MIN_FREE_DISK else LIMITED, f"{_gib(free)} free",
        f"Research reports, logs and the model cache need room ({_gib(MIN_FREE_DISK)} minimum).",
        "" if free >= MIN_FREE_DISK else "Free some space to avoid failed report writes."))

    return out


def _performance_findings(profile):
    """How comfortably does this device run Kayra? No pass/fail - this is headroom."""
    out = []

    threads = profile["cpu_threads"]
    if threads >= COMFORTABLE_THREADS:
        verdict, advice = READY, ""
    elif threads >= MIN_THREADS:
        verdict, advice = GOOD, ("Speech synthesis shares cores with the browser session; "
                                 "replies may start slightly later under load.")
    elif threads:
        verdict, advice = LIMITED, "Expect noticeably slower speech synthesis."
    else:
        verdict, advice = LIMITED, ""
    out.append(Finding(
        "Processor", verdict,
        f"{profile['cpu_cores'] or '?'} cores / {threads or '?'} threads",
        (profile["cpu_name"] or "").strip(), advice))

    ram = profile["ram_total"]
    if ram >= GENEROUS_RAM:
        verdict, advice = READY, ""
    elif ram >= COMFORTABLE_RAM:
        verdict, advice = GOOD, ""
    else:
        verdict, advice = LIMITED, "Close other applications before long research runs."
    out.append(Finding("Memory headroom", verdict, _gib(ram),
                       "Kayra's own footprint is roughly 470MB with the speech session up.",
                       advice))

    # Speech synthesis is the single biggest latency item, and it is CPU-bound. The quantized
    # model is the one change that measurably improves it, so it is reported as a performance
    # fact rather than buried in configuration.
    from kayra.core.paths import model_path
    quantized = os.path.isfile(model_path("kokoro-v1.0.int8.onnx"))
    full = os.path.isfile(model_path("kokoro.onnx"))
    if quantized:
        verdict, summary, advice = READY, "Quantized model installed", ""
    elif full:
        verdict, summary = GOOD, "Full-precision model"
        advice = ("Installing the quantized 'kokoro-v1.0.int8.onnx' and 'voices-v1.0.bin' pair "
                  "into models/ measurably reduces the delay before Kayra starts speaking.")
    else:
        verdict, summary, advice = NOT_AVAILABLE, "No speech model found", \
            "Place a Kokoro ONNX model in models/ to enable spoken replies."
    out.append(Finding("Speech synthesis model", verdict, summary,
                       "Synthesis runs on the CPU at roughly real time.", advice))

    # VENDOR-NEUTRAL. The detail line states what was measured about whatever adapter is
    # actually present — an AMD or Intel machine is described in its own terms rather than
    # being compared against an NVIDIA card it does not have.
    gpu = profile["gpu_name"]
    if gpu:
        detail_bits = []
        if profile["vram_total"]:
            detail_bits.append(f"{_gib(profile['vram_total'])} video memory")
        elif profile["gpu_integrated"]:
            detail_bits.append("shared system memory")
        if profile["gpu_driver"]:
            detail_bits.append(f"driver {profile['gpu_driver']}")
        others = [g["name"] for g in profile["gpus"][1:] if not g["software"]]
        if others:
            detail_bits.append("also present: " + ", ".join(others))
        advice = ""
        if not profile["has_nvidia"]:
            # Speech synthesis is only GPU-accelerated through CUDA here, so a non-NVIDIA
            # machine is told the truth about that rather than being shown a failure.
            advice = ("Speech synthesis runs on the processor: GPU acceleration in Kayra is "
                      "CUDA-only, and this machine has no NVIDIA adapter.")
        out.append(Finding("Graphics", GOOD, gpu,
                           "  ·  ".join(detail_bits) or "Detected.", advice))
    else:
        out.append(Finding(
            "Graphics", LIMITED, "Not detected",
            "No graphics adapter could be identified.",
            "Not a problem - Kayra runs entirely on the processor without one."))

    return out


def _readiness_findings():
    """Is THIS installation configured and ready? Reads configuration, never hardware."""
    from kayra.core.config import env, env_bool
    out = []

    # Speech input: a browser that can actually transcribe.
    try:
        from kayra.input import browsers
        installed = browsers.discover_browsers()
        usable = [b for b in installed if b.recognition != "none"]
        if usable:
            verdict, summary = READY, ", ".join(b.label for b in usable)
            advice = ""
        elif installed:
            verdict, summary = NOT_AVAILABLE, "No browser with a speech backend"
            advice = ("Installed browsers cannot transcribe. Microsoft Edge is preinstalled on "
                      "Windows 11 and works.")
        else:
            verdict, summary, advice = NOT_AVAILABLE, "No supported browser found", \
                "Install Microsoft Edge or Google Chrome for voice input."
    except Exception:
        verdict, summary, advice = LIMITED, "Could not be determined", ""
    out.append(Finding("Speech recognition", verdict, summary,
                       "Recognition runs in a headless browser session Kayra owns.", advice))

    # Language model routing.
    force_online = env_bool("FORCE_ONLINE", False)
    has_cohere = bool(env("CohereAPIKey"))
    has_chat = bool(env("GROQ_API_KEY") or env("GEMINI_API_KEY"))
    local_url = env("LOCAL_BASE_URL")

    if has_cohere and has_chat:
        verdict, summary, advice = READY, "Cloud routing configured", ""
    elif has_chat and not has_cohere:
        verdict, summary = REQUIRES_CONFIGURATION, "Intent classification key missing"
        advice = ("Without a Cohere key every request is treated as conversation, so automation "
                  "commands will not run. Add CohereAPIKey in Settings.")
    elif local_url and not force_online:
        verdict, summary, advice = GOOD, "Local model endpoint configured", \
            "Cloud keys are not required while the local server is reachable."
    else:
        verdict, summary, advice = REQUIRES_CONFIGURATION, "No model configured", \
            "Add API keys in Settings, or run a local model server."
    out.append(Finding("Language model", verdict, summary,
                       "Kayra prefers a local server and falls back to the cloud.", advice))

    # Proactive service.
    enabled = env_bool("PROACTIVE_AGENT_ENABLED", True)
    try:
        import pygetwindow                              # noqa: F401
        has_context = True
    except Exception:
        has_context = False
    if not enabled:
        verdict, summary, advice = LIMITED, "Disabled", "Enable it in Settings if you want it."
    elif has_context:
        verdict, summary, advice = READY, "Enabled with full signals", ""
    else:
        verdict, summary = GOOD, "Enabled, reduced signals"
        advice = "pygetwindow is unavailable, so suggestions use time and habits only."
    out.append(Finding("Proactive agent", verdict, summary,
                       "Suggests things unprompted, using local scoring only.", advice))

    # Automation dependencies.
    missing = []
    for module, label in (("win32gui", "pywin32"), ("pyautogui", "pyautogui"),
                          ("keyboard", "keyboard"), ("AppOpener", "AppOpener")):
        try:
            __import__(module)
        except Exception:
            missing.append(label)
    if not missing:
        verdict, summary, advice = READY, "All components available", ""
    elif "pywin32" in missing:
        verdict, summary = NOT_AVAILABLE, "Window control unavailable"
        advice = f"Install: {', '.join(missing)}"
    else:
        verdict, summary, advice = LIMITED, "Some components missing", f"Install: {', '.join(missing)}"
    out.append(Finding("Desktop automation", verdict, summary,
                       "Window, application, media and keyboard control.", advice))

    # Memory store.
    try:
        from kayra.core.paths import data_dir
        writable = os.access(data_dir(), os.W_OK)
    except Exception:
        writable = False
    out.append(Finding(
        "Memory", READY if writable else NOT_AVAILABLE,
        "Writable" if writable else "Not writable",
        "Conversation memory and learned habits are stored under data/.",
        "" if writable else "Kayra cannot save what you ask it to remember."))

    return out


def analysis():
    """
    The complete System-screen analysis: three independent scores plus the findings behind them.

    Scores are the mean of their findings' verdict ranks, expressed out of 100. That is a
    deliberately boring formula: every point is traceable to a named check, and the UI shows
    the checks alongside the number so the score never has to be taken on faith.
    """
    profile = device_profile()
    groups = {
        "compatibility": _compatibility_findings(profile),
        "performance": _performance_findings(profile),
        "readiness": _readiness_findings(),
    }

    result = {"profile": profile, "groups": {}, "scores": {}, "grades": {}}
    for name, findings in groups.items():
        result["groups"][name] = findings
        if findings:
            score = round(sum(f.rank for f in findings) / (len(findings) * 4) * 100)
        else:
            score = 0
        result["scores"][name] = score
        result["grades"][name] = grade(score)
    result["blockers"] = [f for findings in groups.values() for f in findings
                          if f.verdict == NOT_AVAILABLE]
    result["attention"] = [f for findings in groups.values() for f in findings
                           if f.verdict in (REQUIRES_CONFIGURATION, LIMITED)]
    return result


def grade(score):
    if score >= 90:
        return "EXCELLENT"
    if score >= 75:
        return "VERY GOOD"
    if score >= 60:
        return "GOOD"
    if score >= 40:
        return "LIMITED"
    return "POOR"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             FORMATTING                                 │
# └────────────────────────────────────────────────────────────────────────┘

def _gib(value):
    if not value:
        return "unknown"
    return f"{value / 1024 ** 3:.1f} GB"


def human_bytes(value):
    """Human-readable size. Kept here so every screen formats sizes identically."""
    if not value:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def human_duration(seconds):
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     LIGHTWEIGHT PRESSURE SAMPLE                        │
# └────────────────────────────────────────────────────────────────────────┘

_PRESSURE_CACHE = {"at": 0.0, "value": {}}


def pressure_sample(max_age=20.0):
    """
    CPU / RAM / battery only, cached. For anything that samples on a slow repeating tick.

    This exists as a SEPARATE entry point from `live_metrics()` for one reason: that function
    also calls `kayra_footprint()`, which walks Kayra's whole process tree (nine browser
    processes to stat) for the System screen's footprint line. The proactive presence engine
    asks about memory pressure once a minute, forever, and paying a process-tree walk each
    time to answer a question about a percentage is exactly the kind of background cost this
    codebase does not accept. Same psutil reads, same priming rule, none of the walk.
    """
    global _CPU_PRIMED
    now = time.time()
    if _PRESSURE_CACHE["value"] and (now - _PRESSURE_CACHE["at"]) < max_age:
        return dict(_PRESSURE_CACHE["value"])

    sample = {"cpu_percent": None, "ram_percent": None,
              "battery_percent": None, "battery_plugged": None}
    if psutil is None:
        return sample

    primed = _CPU_PRIMED
    if not primed:
        # Same first-reading problem `live_metrics` documents: `cpu_percent(interval=None)`
        # measures since the previous call, and the very first one measures since process
        # start. Priming discards that reading rather than reporting it as load — but only
        # the CPU figure is affected, so memory and battery are still read and returned.
        # Withholding all three would mean a caller sampling once a minute learns nothing
        # about memory for a full minute after it starts.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        _CPU_PRIMED = True

    if primed:
        try:
            sample["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        except Exception:
            pass
    try:
        sample["ram_percent"] = float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            sample["battery_percent"] = float(battery.percent)
            sample["battery_plugged"] = bool(battery.power_plugged)
    except Exception:
        pass

    _PRESSURE_CACHE["at"] = now
    _PRESSURE_CACHE["value"] = dict(sample)
    return sample
