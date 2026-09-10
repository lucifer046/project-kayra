# ┌────────────────────────────────────────────────────────────────────────┐
# │                      test_hardware_profile.py                          │
# │     OS, Processor and Graphics Identification — Portability Suite      │
# └────────────────────────────────────────────────────────────────────────┘
r"""
test_hardware_profile.py — assertion suite for `kayra.core.hardware` and the structured half
of `kayra.core.system_profile`.

    .venv\Scripts\python tests\test_hardware_profile.py

Tier 1: hardware-free, deterministic, exits non-zero on any failure. It reads the REAL machine
too — that is unavoidable for a hardware module — but every check about the real machine is a
CONSISTENCY check ("the vendor matches the PCI id", "the build is a build number"), never an
equality check against a value only this developer's laptop has. The value assertions are all
driven through synthetic machines, so this suite reaches the same verdict on an AMD desktop, an
Intel ultrabook and a machine with no GPU at all.

WHAT THIS EXISTS TO PREVENT
---------------------------
Two defects, and they are the same defect wearing different clothes:

  1. **The System screen said Windows 10 on a Windows 11 machine.** Every cheap source lies in
     the same direction — registry `ProductName`, `platform.release()` and
     `sys.getwindowsversion().major` all say 10 — so a detector that consults any single one of
     them is confidently wrong. Section 2 drives the correction across seven builds.
  2. **Video memory read 4095 MiB on an 8 GiB card, or "not reported by Windows".** The 32-bit
     `AdapterRAM` field saturates. Section 3 asserts the clamp value can never be reported as a
     measurement, on this machine and on the synthetic ones.

And one thing it exists to guarantee going forward: **no production path may name a piece of
hardware.** Section 6 walks the source of every module that renders a device fact and fails on
a literal model name, capacity or resolution.

SAFETY
------
SAFE. Reads only. It opens no camera, no microphone and no browser, spawns no process, writes
no file, and touches neither `.env` nor the memory store — asserted by an `EnvironmentGuard`
around the whole run.
"""

import os
import io
import re
import ast
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (Checker, EnvironmentGuard, MACHINES, MACHINES_BY_LETTER,
                      describe_host, run, PROJECT_ROOT)

from kayra.utils import print_banner, print_info
from kayra.core import hardware
from kayra.core import system_profile as sp

check = Checker("hardware profile")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SUBSTITUTING A WHOLE MACHINE                       │
# └────────────────────────────────────────────────────────────────────────┘
# `core.hardware` reads the registry through exactly two doors: `_reg_values` for a named key
# and a `winreg.EnumKey` walk for the adapter list. Replacing those two is enough to serve a
# machine that does not exist — and it is the RIGHT seam, because it exercises the parsing,
# the correction, the vendor resolution and the ordering, rather than stubbing the answers.

class FakeRegistry:
    """A registry containing exactly one synthetic machine."""

    def __init__(self, machine):
        self.machine = machine
        self._values = None
        self._enum = None

    def __enter__(self):
        machine = self.machine
        self._values = hardware._reg_values

        adapters = {}
        for index, (vendor, name, vram) in enumerate(machine.adapters):
            pci = {"NVIDIA": "10de", "AMD": "1002", "Intel": "8086"}.get(vendor, "abcd")
            adapters[f"{index:04d}"] = {
                "DriverDesc": name,
                "HardwareInformation.qwMemorySize": vram,
                "DriverVersion": "1.2.3.4",
                "ProviderName": vendor,
                "MatchingDeviceId": rf"pci\ven_{pci}&dev_1234&subsys_0000",
            }

        def fake_values(root, path, names):
            if path == hardware._OS_KEY:
                source = {
                    "ProductName": machine.product_name,
                    "DisplayVersion": machine.display_version,
                    "CurrentBuildNumber": str(machine.build),
                    "UBR": machine.revision,
                    "EditionID": "Core",
                    "InstallationType": "Server" if machine.is_server else "Client",
                }
            elif path == hardware._CPU_KEY:
                source = {"ProcessorNameString": machine.cpu_model,
                          "VendorIdentifier": {"Intel": "GenuineIntel",
                                               "AMD": "AuthenticAMD"}.get(
                                                   machine.cpu_vendor, machine.cpu_vendor)}
            elif path.startswith(hardware._GPU_CLASS_KEY + "\\"):
                source = adapters.get(path.rsplit("\\", 1)[-1], {})
            else:
                source = {}
            return {name: source[name] for name in names if name in source}

        hardware._reg_values = fake_values

        # The adapter walk uses winreg directly, so it needs its own stand-in.
        class _Key:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        self._enum = (hardware.winreg.OpenKey, hardware.winreg.EnumKey)

        def fake_open(root, path, *a, **k):
            if path == hardware._GPU_CLASS_KEY:
                return _Key()
            return self._enum[0](root, path, *a, **k)

        def fake_enum(key, index):
            keys = sorted(adapters)
            if index >= len(keys):
                raise OSError("no more items")
            return keys[index]

        hardware.winreg.OpenKey = fake_open
        hardware.winreg.EnumKey = fake_enum
        hardware.reset_cache()
        return self

    def __exit__(self, *exc):
        hardware._reg_values = self._values
        hardware.winreg.OpenKey, hardware.winreg.EnumKey = self._enum
        hardware.reset_cache()
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │            1. THE REAL MACHINE — CONSISTENCY, NOT EQUALITY             │
# └────────────────────────────────────────────────────────────────────────┘

def section_real_machine():
    check.section("[1] This machine (consistency checks only)")

    facts = hardware.os_info()
    check("os_info returns an OSInfo", isinstance(facts, hardware.OSInfo))
    check("the OS has a product name", bool(facts.display_name))
    check("the source is declared", facts.source in ("registry", "platform"))
    print_info(f"      OS   {facts.display_name}  |  {facts.version_text or 'no version'}")

    if sys.platform.startswith("win"):
        check("Windows is read from the registry, not from platform",
              facts.source == "registry")
        check("the build is a plausible Windows build",
              isinstance(facts.build, int) and 7000 < facts.build < 1_000_000,
              str(facts.build))
        # THE BUG. `platform.release()` is "10" here on Windows 11.
        import platform as _platform
        check("the reported product does not come from platform.release()",
              not facts.display_name.strip().endswith(f" {_platform.release()}")
              or facts.build is None, facts.display_name)
        if facts.build >= hardware.WINDOWS_11_MIN_BUILD:
            check("a build at or above 22000 is never reported as Windows 10",
                  "windows 10" not in facts.display_name.lower(), facts.display_name)
        check("the build text carries the revision when there is one",
              facts.revision is None or "." in facts.build_text, facts.build_text)

    cpu = hardware.cpu_info()
    check("cpu_info returns a CPUInfo", isinstance(cpu, hardware.CPUInfo))
    check("the processor has a model string", bool(cpu.model))
    check("the processor model is not the CPUID family string",
          not re.match(r"^\w+ Family \d+ Model \d+", cpu.model), cpu.model)
    check("thread count is positive", cpu.threads > 0, str(cpu.threads))
    check("cores never exceed threads", not cpu.cores or cpu.cores <= cpu.threads)
    print_info(f"      CPU  {cpu.vendor or '?'} | {cpu.model} | {cpu.topology_text}")

    adapters = hardware.gpu_adapters()
    check("gpu_adapters is a tuple (hashable, so it can be cached)",
          isinstance(adapters, tuple))
    for adapter in adapters:
        print_info(f"      GPU  {adapter.vendor or '?'} | {adapter.name} "
                   f"| {adapter.memory_text} | drv {adapter.driver_version or '?'}")
        check(f"'{adapter.name}' never reports the 32-bit clamp",
              adapter.vram_total != 4293918720)
        check(f"'{adapter.name}' vendor agrees with its PCI id",
              adapter.pci_vendor_id is None
              or hardware.PCI_VENDORS.get(adapter.pci_vendor_id, adapter.vendor)
              == adapter.vendor, f"{adapter.pci_vendor_id:#06x}"
              if adapter.pci_vendor_id else "")
        check(f"'{adapter.name}' integrated verdict is tri-state",
              adapter.integrated in (True, False, None))
    check("adapters are ordered best-first",
          list(adapters) == sorted(adapters, key=lambda a: (not a.software,
                                                            not a.integrated,
                                                            a.vram_total), reverse=True))
    check("has_nvidia_gpu agrees with the list",
          hardware.has_nvidia_gpu() == any(a.is_nvidia and not a.software for a in adapters))
    check("primary_gpu is never a software shim",
          hardware.primary_gpu() is None or not hardware.primary_gpu().software)

    monitors, width, height, scale = hardware.displays()
    print_info(f"      SCR  {width}x{height} at {scale}x  ·  {monitors} monitor(s)")
    check("display size is measured or reported as unknown",
          (width and height) or (width == 0 and height == 0))
    check("the display is not the 1920x1080 default",
          (width, height) != (1920, 1080) or width == 0,
          "1920x1080 is wrong on every high-DPI laptop, which is most of them")
    check("the scale is positive", scale > 0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │              2. THE WINDOWS PRODUCT-NAME CORRECTION                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_os_detection():
    check.section("[2] Windows product detection")

    correct = hardware._windows_product_name

    # ── The correction, stated as a table ──
    cases = [
        # (recorded name,                     build,  server, expected)
        ("Windows 10 Home Single Language",   26200,  False,  "Windows 11 Home Single Language"),
        ("Windows 10 Pro",                    22000,  False,  "Windows 11 Pro"),
        ("Windows 10 Pro",                    21999,  False,  "Windows 10 Pro"),
        ("Windows 10 Enterprise",             19045,  False,  "Windows 10 Enterprise"),
        ("Windows 11 Pro",                    26100,  False,  "Windows 11 Pro"),
        ("Windows Server 2025 Standard",      26100,  True,   "Windows Server 2025 Standard"),
        ("Windows Server 2022 Datacenter",    20348,  True,   "Windows Server 2022 Datacenter"),
        # A future product that names itself honestly keeps its own name: the correction is
        # narrow BY DESIGN and fires only for the values Microsoft is known to leave stale.
        ("Windows 12 Home",                   30000,  False,  "Windows 12 Home"),
        ("Windows 10 Pro",                    None,   False,  "Windows 10 Pro"),
        ("",                                  26100,  False,  ""),
    ]
    for recorded, build, server, expected in cases:
        got = correct(recorded, build, server)
        check(f"'{recorded or '(blank)'}' @ build {build} -> '{expected}'",
              got == expected, got)

    check("22000 is the documented Windows 11 boundary",
          hardware.WINDOWS_11_MIN_BUILD == 22000)

    # ── Splitting product from SKU ──
    for recorded, product, edition in (
            ("Windows 11 Home Single Language", "Windows 11", "Home Single Language"),
            ("Windows 10 Pro", "Windows 10", "Pro"),
            ("Windows Server 2025 Standard", "Windows Server 2025", "Standard"),
            ("Windows", "Windows", ""),
    ):
        got = hardware._split_product(recorded)
        check(f"'{recorded}' splits into product and SKU", got == (product, edition), str(got))

    # ── Every synthetic machine, end to end through the real detector ──
    for machine in MACHINES:
        with FakeRegistry(machine):
            facts = hardware.os_info()
            expected_product = correct(machine.product_name, machine.build, machine.is_server)
            check(f"[{machine.label}] OS reads '{expected_product}'",
                  facts.display_name == expected_product, facts.display_name)
            check(f"[{machine.label}] build is {machine.build}",
                  facts.build == machine.build, str(facts.build))
            check(f"[{machine.label}] version line names the feature update",
                  machine.display_version in facts.version_text, facts.version_text)
            check(f"[{machine.label}] build text includes the revision",
                  facts.build_text == f"{machine.build}.{machine.revision}",
                  facts.build_text)
            if not machine.is_server and machine.build >= 22000:
                check(f"[{machine.label}] is never labelled Windows 10",
                      "windows 10" not in facts.display_name.lower())

    # ── Failure and fallback ──
    class _Blank:
        def __enter__(self_inner):
            self_inner.original = hardware._reg_values
            hardware._reg_values = lambda *a, **k: {}
            hardware.reset_cache()
            return self_inner

        def __exit__(self_inner, *exc):
            hardware._reg_values = self_inner.original
            hardware.reset_cache()
            return False

    with _Blank():
        facts = hardware.os_info()
        # An unreadable registry must yield "Windows" plus whatever build the OS API knows —
        # never an invented version. Showing less is the correct answer; showing a plausible
        # default is the failure this whole module exists to end.
        check("an unreadable registry reports the family, not a version",
              facts.name == "Windows" and facts.edition == "", facts.display_name)
        check("no feature-update version is invented", facts.display_version == "",
              facts.display_version)
        check("the build still comes from the OS API when the registry is empty",
              facts.build is None or isinstance(facts.build, int), str(facts.build))
    hardware.reset_cache()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    3. GRAPHICS, EVERY VENDOR                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_graphics():
    check.section("[3] Graphics detection")

    # ── PCI vendor parsing ──
    for device_id, vendor_id in ((r"pci\ven_10de&dev_28e0&subsys_33a81043", 0x10DE),
                                 (r"PCI\VEN_1002&DEV_1900&SUBSYS_33A81043", 0x1002),
                                 (r"pci\ven_8086&dev_46a6", 0x8086),
                                 ("root\\basicdisplay", None)):
        parsed, _device = hardware._parse_pci_ids(device_id)
        check(f"'{device_id[:28]}' -> vendor {vendor_id}", parsed == vendor_id, str(parsed))

    check("the PCI table names the three desktop vendors",
          {hardware.PCI_VENDORS[0x10DE], hardware.PCI_VENDORS[0x1002],
           hardware.PCI_VENDORS[0x8086]} == {"NVIDIA", "AMD", "Intel"})

    # ── Integrated classification: a positive verdict needs the NAME, never the size alone ──
    classify = hardware._classify_integrated
    check("Intel Iris is integrated",
          classify("Intel(R) Iris(R) Xe Graphics", 0, "Intel") is True)
    check("Intel UHD is integrated",
          classify("Intel(R) UHD Graphics 770", 0, "Intel") is True)
    check("a Radeon iGPU is integrated",
          classify("AMD Radeon 780M Graphics", 1024 ** 3, "AMD") is True)
    check("a discrete Radeon is not integrated",
          classify("AMD Radeon RX 7900 XT", 20 * 1024 ** 3, "AMD") is False)
    check("every NVIDIA adapter is discrete",
          classify("NVIDIA GeForce RTX 3050 Laptop GPU", 4 * 1024 ** 3, "NVIDIA") is False)
    check("a small unnamed adapter is UNKNOWN, not guessed",
          classify("Some Unknown Adapter", 256 * 1024 ** 2, "") is None,
          "a carve-out is not proof of an iGPU")

    # ── Software adapters are never 'the GPU' ──
    check("the Basic Display Adapter is recognised as a shim",
          hardware._is_software_adapter("Microsoft Basic Display Adapter"))
    check("a real card is not", not hardware._is_software_adapter("AMD Radeon RX 7900 XT"))

    # ── Every synthetic machine, through the real adapter walk ──
    for machine in MACHINES:
        with FakeRegistry(machine):
            adapters = hardware.gpu_adapters()
            check(f"[{machine.label}] sees {len(machine.adapters)} adapter(s)",
                  len(adapters) == len(machine.adapters), str(len(adapters)))
            check(f"[{machine.label}] NVIDIA presence is {machine.has_nvidia}",
                  hardware.has_nvidia_gpu() == machine.has_nvidia)
            for adapter in adapters:
                expected = next(a for a in machine.adapters if a[1] == adapter.name)
                check(f"[{machine.label}] '{adapter.name}' vendor",
                      adapter.vendor == expected[0], adapter.vendor)
                check(f"[{machine.label}] '{adapter.name}' VRAM is the 64-bit value",
                      adapter.vram_total == expected[2], str(adapter.vram_total))
            if machine.adapters:
                primary = hardware.primary_gpu()
                # Ranked, not first-found: a discrete card outranks the iGPU beside it.
                best = max(machine.adapters, key=lambda a: a[2])
                check(f"[{machine.label}] primary is '{best[1]}'",
                      primary is not None and primary.name == best[1],
                      primary.name if primary else "None")
            else:
                check(f"[{machine.label}] reports no GPU rather than inventing one",
                      hardware.primary_gpu() is None)

    # ── The clamp, on a machine that only exposes the 32-bit field ──
    class _LegacyOnly:
        """A driver too old to publish `qwMemorySize` — the only path that can still clamp."""

        def __enter__(self_inner):
            self_inner.original = hardware._reg_values

            def values(root, path, names):
                if path.startswith(hardware._GPU_CLASS_KEY + "\\"):
                    return {"DriverDesc": "Legacy Adapter",
                            "HardwareInformation.MemorySize": 4293918720,
                            "MatchingDeviceId": r"pci\ven_10de&dev_0001"}
                return {}

            hardware._reg_values = values
            hardware.reset_cache()
            return self_inner

        def __exit__(self_inner, *exc):
            hardware._reg_values = self_inner.original
            hardware.reset_cache()
            return False

    machine = MACHINES_BY_LETTER["A"]
    with FakeRegistry(machine):
        with _LegacyOnly():
            adapters = hardware.gpu_adapters()
            if adapters:
                check("a saturated 32-bit read is reported as unknown, never as 4 GB",
                      adapters[0].vram_total == 0, str(adapters[0].vram_total))
                check("and the caption says so rather than showing a number",
                      adapters[0].memory_text in ("unknown", "shared memory"),
                      adapters[0].memory_text)
    hardware.reset_cache()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  4. THE STRUCTURED PROFILE SNAPSHOT                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_profile():
    check.section("[4] system_profile snapshot")

    profile = sp.device_profile()
    required = ("os_name", "os_product", "os_edition", "os_display_version", "os_build",
                "os_build_text", "architecture", "cpu_name", "cpu_vendor", "cpu_cores",
                "cpu_threads", "ram_total", "gpu_name", "gpu_vendor", "gpu_integrated",
                "gpu_driver", "vram_total", "gpus", "gpu_vendors", "has_nvidia",
                "monitor_count", "screen_width", "screen_height", "screen_scale",
                "disks", "audio_outputs", "audio_inputs")
    for field in required:
        check(f"the snapshot exposes '{field}'", field in profile)

    check("the snapshot is collected once", sp.device_profile() is profile)
    check("the primary GPU matches the adapter list",
          (profile["gpu_name"] is None and not profile["gpus"])
          or profile["gpu_name"] == next(g["name"] for g in profile["gpus"]
                                         if not g["software"]))
    check("has_nvidia agrees with the vendor list",
          profile["has_nvidia"] == ("NVIDIA" in profile["gpu_vendors"]))

    # ── The system drive is derived, never assumed ──
    drive = sp.system_drive()
    check("system_drive returns a mount point", bool(drive))
    check("system_drive comes from the environment, not a literal",
          drive.rstrip("\\/").upper() ==
          (os.environ.get("SystemDrive") or drive).rstrip("\\/").upper(), drive)
    system_disks = [d for d in profile["disks"] if d.get("system")]
    check("at most one disk is flagged as the system drive", len(system_disks) <= 1)
    if profile["disks"]:
        check("every disk carries a system flag",
              all("system" in d for d in profile["disks"]))

    # ── Live metrics stay psutil-only and bounded ──
    metrics = sp.live_metrics()
    for field in ("cpu_percent", "ram_percent", "ram_used", "ram_total", "disk_percent",
                  "battery_percent", "battery_plugged", "uptime_seconds"):
        check(f"live metrics expose '{field}'", field in metrics)
    check("cpu_percent is a percentage", 0.0 <= metrics["cpu_percent"] <= 100.0)
    check("battery is None or a percentage",
          metrics["battery_percent"] is None or 0 <= metrics["battery_percent"] <= 100)
    check("a machine with no battery reports None rather than 0",
          metrics["battery_percent"] is None or metrics["battery_percent"] > 0
          or metrics["battery_plugged"] is not None)

    product, version = sp.os_summary()
    check("os_summary product is non-empty", bool(product))
    check("os_summary never returns the compatibility release as the build",
          f"Build {profile['os_release']}" not in version or profile["os_build"] is None,
          version)

    # ── Cost. This is the headline optimization of the pass and it is measured, not claimed ──
    hardware.reset_cache()
    started = time.perf_counter()
    hardware.os_info()
    hardware.cpu_info()
    hardware.gpu_adapters()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print_info(f"      hardware collection: {elapsed_ms:.3f} ms")
    check("hardware collection costs under 100ms", elapsed_ms < 100.0,
          f"{elapsed_ms:.3f} ms — the PowerShell/CIM call it replaced measured 4410 ms")

    started = time.perf_counter()
    for _ in range(1000):
        sp.device_profile()
    per_call_us = (time.perf_counter() - started) / 1000 * 1_000_000
    check("a cached profile read is effectively free", per_call_us < 50.0,
          f"{per_call_us:.2f} us per call")


# ┌────────────────────────────────────────────────────────────────────────┐
# │              5. NVIDIA-SPECIFIC WORK IS GATED ON NVIDIA                │
# └────────────────────────────────────────────────────────────────────────┘

def section_nvidia_gating():
    check.section("[5] NVIDIA gating")
    from kayra.output import tts_device

    check("tts_device exposes a presence gate",
          hasattr(tts_device, "nvidia_telemetry_supported"))
    check("the gate agrees with the hardware",
          tts_device.nvidia_telemetry_supported() or not hardware.has_nvidia_gpu()
          or tts_device._NVIDIA_SMI_MISSING)

    # On a machine with no NVIDIA adapter the gate must answer False WITHOUT a spawn.
    original = hardware.has_nvidia_gpu
    spawned = []
    original_run = tts_device.subprocess.run

    def counting_run(*a, **k):
        spawned.append(a[0] if a else None)
        return original_run(*a, **k)

    try:
        hardware.has_nvidia_gpu = lambda: False
        tts_device._NVIDIA_SMI_MISSING = False
        tts_device.subprocess.run = counting_run
        supported = tts_device.nvidia_telemetry_supported()
        check("a non-NVIDIA machine reports no telemetry support", supported is False)
        check("and reaches that answer without spawning anything", not spawned,
              "learning it from a FileNotFoundError is the expensive route")
        check("_run_nvidia_smi returns None immediately",
              tts_device._run_nvidia_smi() is None)
        check("still no process was spawned", not spawned)
    finally:
        hardware.has_nvidia_gpu = original
        tts_device.subprocess.run = original_run
        tts_device._NVIDIA_SMI_MISSING = False

    # `run.py --doctor` and the diagnostics must never claim a device the session is not on.
    diagnostics = tts_device.runtime_diagnostics()
    check("diagnostics report cuda_available and cuda_usable separately",
          "cuda_available" in diagnostics.to_dict() and "cuda_usable" in diagnostics.to_dict())
    if not hardware.has_nvidia_gpu():
        check("a non-NVIDIA machine never reports CUDA as usable",
              not diagnostics.to_dict().get("cuda_usable"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │           6. NO PRODUCTION PATH MAY NAME A PIECE OF HARDWARE           │
# └────────────────────────────────────────────────────────────────────────┘

def section_no_hardcoding():
    check.section("[6] No machine-specific values in production code")

    # Model names, capacities and resolutions that could only be right by luck. Checked
    # against STRING LITERALS in the parsed source, not against the file text — every one of
    # these modules legitimately DOCUMENTS the measurements behind its design decisions, and a
    # grep would flag the explanation as the defect. This is the same distinction the
    # transcript-repair suite draws when it proves there is no word-replacement dictionary.
    forbidden = ("rtx 4060", "rtx 3050", "gtx 1080", "radeon 780m", "iris xe",
                 "core i5", "core i7", "ryzen 9", "ryzen 7",
                 "1920x1080", "windows 10 home", "windows 11 home")

    scanned = 0
    offenders = []
    for folder, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "src", "kayra")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            scanned += 1
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    first = node.body[0] if node.body else None
                    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                            and isinstance(first.value.value, str)):
                        docstrings.add(id(first.value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                lowered = node.value.lower()
                for token in forbidden:
                    if token in lowered:
                        offenders.append(
                            f"{os.path.relpath(path, PROJECT_ROOT)}:{node.lineno} '{token}'")

    check(f"no runnable string in {scanned} modules names a specific device",
          not offenders, "; ".join(offenders[:5]))

    # The UI must read the profile rather than composing hardware facts itself.
    for relative in ("src/kayra/ui/views/home.py", "src/kayra/ui/views/system.py"):
        # Comment lines stripped: both views EXPLAIN the `platform.release()` trap they were
        # fixed for, and the explanation is the reason the fix will not be undone.
        raw = io.open(os.path.join(PROJECT_ROOT, *relative.split("/")),
                      encoding="utf-8").read()
        source = chr(10).join(line for line in raw.splitlines()
                              if not line.lstrip().startswith("#"))
        check(f"{os.path.basename(relative)} reads the profile rather than the platform",
              "platform.release()" not in source and "platform.processor()" not in source,
              "hardware detection belongs in core, not in a view")

    # And `system_profile` must not have grown a subprocess back.
    tree = ast.parse(io.open(sp.__file__, encoding="utf-8").read())
    imports = {alias.name.split(".")[0] for node in ast.walk(tree)
               if isinstance(node, ast.Import) for alias in node.names}
    check("system_profile imports no subprocess module", "subprocess" not in imports)
    tree = ast.parse(io.open(hardware.__file__, encoding="utf-8").read())
    imports = {alias.name.split(".")[0] for node in ast.walk(tree)
               if isinstance(node, ast.Import) for alias in node.names}
    check("hardware imports no subprocess module", "subprocess" not in imports)
    check("hardware is a leaf: it imports nothing from kayra except lazily",
          all(not (node.module or "").startswith("kayra")
              for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               MAIN                                     │
# └────────────────────────────────────────────────────────────────────────┘

def main():
    print_banner("HARDWARE PROFILE")
    print_info(f"Host: {describe_host()}")

    with EnvironmentGuard() as guard:
        section_real_machine()
        section_os_detection()
        section_graphics()
        section_profile()
        section_nvidia_gating()
        section_no_hardcoding()

    check.section("[7] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())

    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
