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
import shutil
import platform
import threading
import functools
import subprocess

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

def _powershell_once(script, timeout=6.0):
    """
    One short PowerShell read. Used ONLY for facts psutil cannot see, and only at first use.

    A fixed argument vector with `shell=False` - there is no user text anywhere in these
    commands, which is what keeps them outside the automation shell policy.
    """
    if not sys.platform.startswith("win"):
        return ""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, shell=False, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (completed.stdout or "").strip()
    except Exception:
        return ""


# `AdapterRAM` is a 32-bit WMI field and cannot express the VRAM of any modern card. It does
# not fail cleanly: drivers CLAMP it, and the observed value on this host's 8GB RTX 4060 is
# 4293918720 (4095 MiB) rather than the full-scale 0xFFFFFFFF. So a naive ceiling test does not
# work - two earlier bounds (4 * 1024**3, then 0xFFFFFFFF - 1024) both let the clamped value
# through, and the screen confidently reported "4.0 GB" for an 8GB card.
#
# Anything at or above ~4000MB is therefore treated as clamped and reported as UNKNOWN. This
# deliberately gives up on genuine 4GB cards: saying "unknown" about a card that has 4GB is a
# far smaller error than telling someone with 8GB, 12GB or 16GB that they have 4GB, and this
# module's whole contract is that it never invents a number.
_ADAPTER_RAM_CEILING = 4000 * 1024 ** 2


@functools.lru_cache(maxsize=1)
def _windows_facts():
    """
    CPU name, OS edition, GPU name and VRAM - in ONE PowerShell call.

    These are the only facts psutil cannot see. Collecting them separately measured 4.0s at
    startup because each call pays a fresh PowerShell process launch (~1.3s apiece); batching
    them into a single script pays that once. Called exactly once per process, and off the UI
    thread - the profile is warmed by a worker, never by a paint.
    """
    raw = _powershell_once(
        "$c = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name; "
        "$o = (Get-CimInstance Win32_OperatingSystem).Caption; "
        "$g = Get-CimInstance Win32_VideoController | "
        "Sort-Object -Property AdapterRAM -Descending | Select-Object -First 1; "
        "\"$c`n$o`n$($g.Name)`n$($g.AdapterRAM)\"")
    lines = (raw or "").splitlines()
    while len(lines) < 4:
        lines.append("")
    cpu, os_edition, gpu_name, vram = (line.strip() for line in lines[:4])
    try:
        vram_bytes = int(vram)
    except (TypeError, ValueError):
        vram_bytes = 0
    if vram_bytes >= _ADAPTER_RAM_CEILING:
        vram_bytes = 0                  # saturated, not measured
    return {"cpu_name": cpu, "os_edition": os_edition,
            "gpu_name": gpu_name or None, "vram_total": max(0, vram_bytes)}


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


def _collect_profile():
    profile = {
        "os_name": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "os_edition": "",
        "architecture": platform.machine(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "cpu_name": platform.processor() or "Unknown CPU",
        "cpu_cores": 0,
        "cpu_threads": 0,
        "cpu_max_mhz": 0.0,
        "ram_total": 0,
        "gpu_name": None,
        "vram_total": 0,
        "disks": [],
        "audio_outputs": 0,
        "audio_inputs": 0,
        "collected_at": time.time(),
    }

    if psutil is not None:
        try:
            profile["cpu_cores"] = psutil.cpu_count(logical=False) or 0
            profile["cpu_threads"] = psutil.cpu_count(logical=True) or 0
        except Exception:
            pass
        try:
            freq = psutil.cpu_freq()
            profile["cpu_max_mhz"] = float(getattr(freq, "max", 0) or 0)
        except Exception:
            pass
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
                })
        except Exception:
            pass

    profile["audio_outputs"], profile["audio_inputs"] = _audio_devices()

    # One batched WMI read for everything psutil cannot see. platform.processor() returns a
    # family/model/stepping string on Windows, which is accurate and unreadable, so the WMI
    # name is preferred when it is available.
    if sys.platform.startswith("win"):
        facts = _windows_facts()
        profile["cpu_name"] = (facts["cpu_name"]
                               or os.environ.get("PROCESSOR_IDENTIFIER", "")
                               or profile["cpu_name"])
        profile["os_edition"] = facts["os_edition"]
        profile["gpu_name"] = facts["gpu_name"]
        profile["vram_total"] = facts["vram_total"]

    return profile


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
        usage = psutil.disk_usage(os.path.abspath(os.sep))
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
    out.append(Finding(
        "Operating system",
        READY if is_windows else LIMITED,
        # The edition string already names the version ("Microsoft Windows 11 Home"), so
        # appending platform.release() produced "…Home Single Language 10".
        (profile.get("os_edition")
         or f"{profile['os_name']} {profile['os_release']}").strip(),
        "Kayra's automation layer is built on Win32 APIs.",
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

    gpu = profile["gpu_name"]
    out.append(Finding(
        "Graphics", GOOD if gpu else LIMITED, gpu or "Not detected",
        "Kayra does not currently use the GPU; every model it runs locally is CPU-based.",
        "" if gpu else "Not a problem - nothing in Kayra requires a GPU today."))

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
