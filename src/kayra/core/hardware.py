# ┌────────────────────────────────────────────────────────────────────────┐
# │                            hardware.py                                 │
# │        Vendor-Neutral OS, Processor and Graphics Identification        │
# └────────────────────────────────────────────────────────────────────────┘
"""
What machine is this, actually?

This module answers that question for ANY machine, not for the machine Kayra was written on.
It is the single source of truth for the OS product/version/build, the processor's real
marketing name, and every graphics adapter present — with its vendor, its true VRAM and its
driver version. `core.system_profile` composes it into the snapshot the UI renders; nothing
else may guess at these facts.

WHY THIS EXISTS AS A SEPARATE MODULE
------------------------------------
`system_profile` used to collect all of this in ONE batched PowerShell/CIM call. That call was
correct about the CPU name and the OS caption and WRONG about two things that matter, and it
cost 4.4 seconds measured on this host:

  * `Win32_VideoController.AdapterRAM` is a 32-bit field. Drivers clamp it, so an 8 GiB card
    reports 4293918720 (4095 MiB). `system_profile` correctly refused to trust anything at or
    above ~4000 MiB and therefore reported "not reported by Windows" for every modern GPU —
    honest, and still a blank where a number belongs.
  * It selected ONE adapter (highest AdapterRAM). On a laptop with switchable graphics that is
    a coin toss between the discrete card and the integrated one, because both report clamped
    or carve-out values.

Every fact it was reading is in the registry, where it costs **nothing** to read:

    HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-…}\\NNNN
        DriverDesc                          the adapter's real name
        HardwareInformation.qwMemorySize    64-bit VRAM — NOT clamped
        DriverVersion, ProviderName         driver identity
        MatchingDeviceId                    PCI\\VEN_xxxx — the authoritative vendor

    HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0
        ProcessorNameString, VendorIdentifier

    HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion
        CurrentBuildNumber, UBR, DisplayVersion, EditionID, ProductName

Measured on this host: the whole collection is **under 1 ms** against 4.4 s, it returns the
true 8585740288-byte VRAM instead of "unknown", and it sees BOTH adapters instead of one.

THE `ProductName` TRAP — this is the Windows 10/11 bug
------------------------------------------------------
`ProductName` under `Windows NT\\CurrentVersion` reads **"Windows 10 Home Single Language"** on
a Windows 11 machine. Microsoft froze that value for application-compatibility reasons and it
has never been updated. `platform.release()` returns `"10"` for the same reason, and
`sys.getwindowsversion().major` is `10`. Every cheap source lies in the same direction.

The build number does not. Windows 11 is documented as build **22000 and above**, and that is
the relationship this module uses — as a CORRECTION applied only when the recorded product
name is one of the known-stale ones, never as a blanket rule. A future Windows whose registry
reports its own name correctly is displayed under that name, because the correction does not
fire. When the build cannot be read at all the answer is `"Windows"` with no version, never a
guessed one: this module's contract is that it reports what it measured or says it does not
know, and it never invents a value.

NON-WINDOWS
-----------
Kayra's automation layer is Windows-only, but this module is not: it degrades to
`platform`/`psutil` facts everywhere else so that the profile, the UI and the tests all still
have something truthful to render. There is no branch anywhere that assumes Windows.

COST
----
Everything here is cached for the life of the process behind `functools.lru_cache`. Nothing in
this module spawns a subprocess, opens a network socket, or imports a heavy dependency. It is
a LEAF: stdlib only, plus an optional `psutil` for core counts.
"""

import os
import re
import sys
import platform
import functools

try:
    import psutil
except Exception:                       # pragma: no cover - psutil is a hard dependency
    psutil = None

try:
    import winreg                       # Windows only; absent everywhere else by design
except Exception:                       # pragma: no cover - non-Windows
    winreg = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              CONSTANTS                                 │
# └────────────────────────────────────────────────────────────────────────┘

# PCI vendor ids. The authoritative vendor answer: a name string can be rebranded or localised,
# a PCI vendor id cannot. Read from `MatchingDeviceId`, e.g. `pci\ven_10de&dev_28e0&...`.
PCI_VENDORS = {
    0x10DE: "NVIDIA",
    0x1002: "AMD",
    0x1022: "AMD",                      # AMD's second id, used by some integrated parts
    0x8086: "Intel",
    0x5143: "Qualcomm",
    0x1414: "Microsoft",                # Basic Display Adapter / virtual adapters
    0x15AD: "VMware",
    0x1AF4: "Red Hat",                  # virtio-gpu
    0x1013: "Cirrus",
}

VENDOR_NVIDIA = "NVIDIA"
VENDOR_AMD = "AMD"
VENDOR_INTEL = "Intel"

# Windows 11's first build. This is a DOCUMENTED relationship (Windows 11 shipped as 22000 and
# every client build since is 22000+), which is why it is safe to use as a correction. It is
# NOT used as "anything above this is Windows 11 forever" — see `_windows_product_name`.
WINDOWS_11_MIN_BUILD = 22000

# The product names Microsoft is known to leave stale on a newer OS. The correction below only
# fires for one of these, so a future Windows that reports its own name honestly keeps it.
_STALE_PRODUCT_NAMES = ("windows 10",)

# Names that identify an adapter as integrated/shared rather than a discrete card. Matched as
# whole words against the lower-cased adapter name. This is a HEURISTIC and is reported as
# such — `GPUAdapter.integrated` is None when neither the name nor the vendor settles it,
# rather than defaulting to a guess.
_INTEGRATED_PATTERNS = (
    r"\buhd graphics\b", r"\bhd graphics\b", r"\biris\b", r"\bintel\(r\) graphics\b",
    r"\barc\b.*\bgraphics\b",
    r"\bvega\b.*\bgraphics\b", r"\bradeon\b.*\bgraphics\b", r"\b\d{3}m graphics\b",
    r"\bapu\b", r"\bintegrated\b",
)

# Adapters that are not real graphics hardware. A machine with only these has no usable GPU and
# should be told so, rather than being shown "Microsoft Basic Display Adapter" as its GPU.
_SOFTWARE_ADAPTERS = (
    "microsoft basic display adapter", "microsoft remote display adapter",
    "remotefx", "citrix indirect display", "parsec virtual display",
    "idd hdr", "virtual display", "meta virtual monitor",
)

_GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_OS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          STRUCTURED RESULTS                            │
# └────────────────────────────────────────────────────────────────────────┘
# Plain classes with `__slots__` and a `to_dict()`, matching `system_profile.Finding` rather
# than introducing dataclasses into a package that does not use them.

class OSInfo:
    """
    The operating system, as separate FIELDS rather than one pre-formatted sentence.

    `name` is the product ("Windows 11"), `edition` the SKU ("Home Single Language"),
    `display_version` the feature update ("25H2"), `build` the build number (26200) and
    `revision` the UBR. Any of them may be empty or None when it could not be read; a caller
    renders what it has and says "unknown" for the rest. Nothing here is ever guessed.
    """

    __slots__ = ("name", "edition", "display_version", "build", "revision",
                 "architecture", "kernel_version", "is_server", "source")

    def __init__(self, name="", edition="", display_version="", build=None, revision=None,
                 architecture="", kernel_version="", is_server=False, source="platform"):
        self.name = name
        self.edition = edition
        self.display_version = display_version
        self.build = build
        self.revision = revision
        self.architecture = architecture
        self.kernel_version = kernel_version
        self.is_server = is_server
        self.source = source

    @property
    def build_text(self):
        """`26200.9445`, or `26200`, or `""` — never a fabricated number."""
        if self.build is None:
            return ""
        if self.revision:
            return f"{self.build}.{self.revision}"
        return str(self.build)

    @property
    def display_name(self):
        """
        The one-line product name a screen shows: `Windows 11 Home Single Language`.

        When the product could not be identified this is bare `Windows` (or the platform's
        own name), and the caller is expected to show the build alongside it. Showing
        `Windows` + a real build is truthful; showing `Windows 10` on a Windows 11 machine is
        the bug this module exists to fix.
        """
        parts = [p for p in (self.name, self.edition) if p]
        return " ".join(parts) or self.name or "Unknown"

    @property
    def version_text(self):
        """`Version 25H2 (Build 26200.9445)` — whichever halves are actually known."""
        chunks = []
        if self.display_version:
            chunks.append(f"Version {self.display_version}")
        if self.build_text:
            chunks.append(f"Build {self.build_text}")
        return "  ·  ".join(chunks)

    def to_dict(self):
        return {"name": self.name, "edition": self.edition,
                "display_version": self.display_version, "build": self.build,
                "revision": self.revision, "architecture": self.architecture,
                "kernel_version": self.kernel_version, "is_server": self.is_server,
                "source": self.source, "display_name": self.display_name,
                "build_text": self.build_text, "version_text": self.version_text}

    def __repr__(self):
        return f"<OSInfo {self.display_name!r} build={self.build_text or '?'}>"


class CPUInfo:
    """The processor. `model` is the marketing name; `vendor` comes from the CPUID string."""

    __slots__ = ("vendor", "model", "architecture", "cores", "threads", "max_mhz", "source")

    def __init__(self, vendor="", model="", architecture="", cores=0, threads=0,
                 max_mhz=0.0, source="platform"):
        self.vendor = vendor
        self.model = model
        self.architecture = architecture
        self.cores = cores
        self.threads = threads
        self.max_mhz = max_mhz
        self.source = source

    @property
    def topology_text(self):
        if self.cores and self.threads:
            return f"{self.cores} cores / {self.threads} threads"
        if self.threads:
            return f"{self.threads} threads"
        return "unknown"

    def to_dict(self):
        return {"vendor": self.vendor, "model": self.model,
                "architecture": self.architecture, "cores": self.cores,
                "threads": self.threads, "max_mhz": self.max_mhz, "source": self.source,
                "topology_text": self.topology_text}

    def __repr__(self):
        return f"<CPUInfo {self.model!r} {self.topology_text}>"


class GPUAdapter:
    """
    One graphics adapter.

    `vram_total` is in BYTES and comes from the 64-bit `qwMemorySize`, so it is the real figure
    rather than the clamped 32-bit `AdapterRAM`. `integrated` is True, False, or **None** when
    neither the name nor the vendor settles it — a tri-state, because "we could not tell" is a
    different answer from "it is discrete" and the UI phrases them differently.
    """

    __slots__ = ("vendor", "name", "vram_total", "driver_version", "integrated",
                 "pci_vendor_id", "pci_device_id", "software", "source")

    def __init__(self, vendor="", name="", vram_total=0, driver_version="",
                 integrated=None, pci_vendor_id=None, pci_device_id=None,
                 software=False, source="registry"):
        self.vendor = vendor
        self.name = name
        self.vram_total = vram_total
        self.driver_version = driver_version
        self.integrated = integrated
        self.pci_vendor_id = pci_vendor_id
        self.pci_device_id = pci_device_id
        self.software = software
        self.source = source

    @property
    def is_nvidia(self):
        return self.vendor == VENDOR_NVIDIA

    @property
    def memory_text(self):
        """`8.0 GB`, or `shared memory` for an integrated part, or `unknown`."""
        if self.vram_total:
            return f"{self.vram_total / 1024 ** 3:.1f} GB"
        if self.integrated:
            return "shared memory"
        return "unknown"

    def to_dict(self):
        return {"vendor": self.vendor, "name": self.name, "vram_total": self.vram_total,
                "driver_version": self.driver_version, "integrated": self.integrated,
                "pci_vendor_id": self.pci_vendor_id, "pci_device_id": self.pci_device_id,
                "software": self.software, "source": self.source,
                "memory_text": self.memory_text}

    def __repr__(self):
        return f"<GPUAdapter {self.vendor} {self.name!r} {self.memory_text}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         REGISTRY PRIMITIVES                            │
# └────────────────────────────────────────────────────────────────────────┘

def _reg_values(root, path, names):
    """
    Read named values from one registry key. Returns {} on any failure.

    Registry reads are the cheapest source of hardware truth on Windows and the only one that
    is free of a process spawn, so this is deliberately forgiving: a machine missing a value
    yields a missing key rather than an exception, and the caller reports "unknown".
    """
    if winreg is None:
        return {}
    out = {}
    try:
        with winreg.OpenKey(root, path) as key:
            for name in names:
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    continue
    except OSError:
        return {}
    return out


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          OPERATING SYSTEM                              │
# └────────────────────────────────────────────────────────────────────────┘

def _windows_product_name(product_name, build, is_server):
    """
    Resolve the real product name from the recorded one plus the build.

    THE CORRECTION IS NARROW ON PURPOSE. It fires only when the recorded name is one of the
    values Microsoft is known to leave stale AND the build proves a newer product. Anything
    else is returned unchanged, so:

      * `Windows 11 Pro` on build 26200            -> `Windows 11 Pro`   (already correct)
      * `Windows 10 Home` on build 26200           -> `Windows 11 Home`  (corrected)
      * `Windows 10 Pro` on build 19045            -> `Windows 10 Pro`   (genuinely 10)
      * `Windows Server 2022 Standard`             -> unchanged          (server builds differ)
      * anything unrecognised                      -> unchanged

    Server SKUs are excluded because the client build thresholds do not describe them: Windows
    Server 2022 is build 20348 and Server 2025 is 26100, so a client rule would mislabel both.
    """
    name = (product_name or "").strip()
    if not name:
        return ""
    if is_server:
        return name
    if build is None:
        return name
    lowered = name.lower()
    if not any(lowered.startswith(stale) for stale in _STALE_PRODUCT_NAMES):
        return name
    if build >= WINDOWS_11_MIN_BUILD:
        # Replace only the leading product token, keeping whatever SKU words follow it.
        return re.sub(r"^Windows\s+10", "Windows 11", name, count=1, flags=re.IGNORECASE)
    return name


def _split_product(product_name):
    """
    `Windows 11 Home Single Language` -> (`Windows 11`, `Home Single Language`).

    The split is on the version token so a screen can show the product large and the SKU small,
    and so `Windows Server 2022 Standard` splits at `Server 2022` rather than at `Server`.
    """
    name = (product_name or "").strip()
    match = re.match(r"^(Windows(?:\s+Server)?(?:\s+[\d.]+)?)\s*(.*)$", name, re.IGNORECASE)
    if not match:
        return name, ""
    return match.group(1).strip(), match.group(2).strip()


@functools.lru_cache(maxsize=1)
def os_info():
    """
    The operating system, measured. Cached for the process lifetime — it cannot change.

    On Windows every field comes from the registry in well under a millisecond. Everywhere
    else the `platform` module is the honest best answer and `source` says so, which is what
    lets a caller (and the test suite) tell a measured value from a fallback.
    """
    architecture = platform.machine() or ""
    kernel = platform.version() or ""

    if not sys.platform.startswith("win") or winreg is None:
        # macOS / Linux / anything else. `platform.system()` and `.release()` are accurate off
        # Windows — the staleness problem is specific to Windows' compatibility shims.
        return OSInfo(name=platform.system() or "Unknown",
                      edition="", display_version=platform.release() or "",
                      build=None, revision=None, architecture=architecture,
                      kernel_version=kernel, is_server=False, source="platform")

    values = _reg_values(
        winreg.HKEY_LOCAL_MACHINE, _OS_KEY,
        ("ProductName", "DisplayVersion", "ReleaseId", "CurrentBuildNumber", "UBR",
         "EditionID", "InstallationType"))

    build = _as_int(values.get("CurrentBuildNumber"))
    if build is None:
        # `sys.getwindowsversion()` is subject to the manifest compatibility shim for the
        # MAJOR version but reports the true build, so it is a sound second source for the one
        # number the correction depends on.
        try:
            build = int(sys.getwindowsversion().build)
        except Exception:
            build = None

    revision = _as_int(values.get("UBR"))
    installation = str(values.get("InstallationType") or "")
    is_server = installation.lower() == "server"

    corrected = _windows_product_name(values.get("ProductName"), build, is_server)
    product, edition = _split_product(corrected)
    if not product:
        # Nothing readable. Say "Windows" and let the build carry the identity — never a
        # version number that was not measured.
        product = "Windows"
        edition = ""

    display_version = (str(values.get("DisplayVersion") or "").strip()
                       or str(values.get("ReleaseId") or "").strip())
    # `ReleaseId` was frozen at "2009" the same way ProductName was frozen at Windows 10. It is
    # a valid feature-update name for 20H2 and earlier and meaningless after, so it is only
    # accepted as a fallback when it is not that specific stale value.
    if display_version == "2009" and not values.get("DisplayVersion"):
        display_version = ""

    return OSInfo(name=product, edition=edition, display_version=display_version,
                  build=build, revision=revision, architecture=architecture,
                  kernel_version=kernel, is_server=is_server, source="registry")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             PROCESSOR                                  │
# └────────────────────────────────────────────────────────────────────────┘

_CPU_VENDOR_IDS = {
    "genuineintel": VENDOR_INTEL,
    "authenticamd": VENDOR_AMD,
    "qualcomm technologies inc": "Qualcomm",
    "arm limited": "ARM",
}


def _cpu_counts():
    cores = threads = 0
    max_mhz = 0.0
    if psutil is not None:
        try:
            cores = psutil.cpu_count(logical=False) or 0
            threads = psutil.cpu_count(logical=True) or 0
        except Exception:
            pass
        try:
            freq = psutil.cpu_freq()
            max_mhz = float(getattr(freq, "max", 0) or 0)
        except Exception:
            pass
    if not threads:
        threads = os.cpu_count() or 0
    return cores, threads, max_mhz


@functools.lru_cache(maxsize=1)
def cpu_info():
    """
    The processor's real marketing name, vendor and topology.

    `platform.processor()` returns `AMD64 Family 25 Model 117 Stepping 2, AuthenticAMD` on
    Windows — accurate, and not something anyone recognises as their own CPU. The registry
    holds the string the vendor actually branded the part with, and reading it costs nothing.
    """
    cores, threads, max_mhz = _cpu_counts()
    architecture = platform.machine() or ""

    if sys.platform.startswith("win") and winreg is not None:
        values = _reg_values(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY,
                             ("ProcessorNameString", "VendorIdentifier"))
        model = str(values.get("ProcessorNameString") or "").strip()
        raw_vendor = str(values.get("VendorIdentifier") or "").strip()
        vendor = _CPU_VENDOR_IDS.get(raw_vendor.lower(), raw_vendor)
        if model:
            # Vendors pad the branded string to a fixed width and some include a trademark
            # suffix that reads badly in a fixed-height card.
            model = re.sub(r"\s+", " ", model).strip()
            return CPUInfo(vendor=vendor, model=model, architecture=architecture,
                           cores=cores, threads=threads, max_mhz=max_mhz, source="registry")
        # Registry unreadable — fall through to the platform answer rather than returning
        # an empty model, and say where it came from.
        fallback = (os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor()
                    or "Unknown processor")
        return CPUInfo(vendor=vendor, model=fallback.strip(), architecture=architecture,
                       cores=cores, threads=threads, max_mhz=max_mhz, source="platform")

    model = (platform.processor() or "").strip()
    if not model:
        # Linux exposes the branded name here; macOS answers the same question through
        # sysctl, which is not worth a subprocess for a field the UI labels honestly.
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    if raw.lower().startswith("model name"):
                        model = raw.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    vendor = ""
    lowered = model.lower()
    if "intel" in lowered:
        vendor = VENDOR_INTEL
    elif "amd" in lowered or "ryzen" in lowered:
        vendor = VENDOR_AMD
    elif "apple" in lowered:
        vendor = "Apple"
    return CPUInfo(vendor=vendor, model=model or "Unknown processor",
                   architecture=architecture, cores=cores, threads=threads,
                   max_mhz=max_mhz, source="platform")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              GRAPHICS                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _parse_pci_ids(matching_device_id):
    """`pci\\ven_10de&dev_28e0&subsys_…` -> (0x10DE, 0x28E0). (None, None) when not PCI."""
    text = str(matching_device_id or "").lower()
    vendor = re.search(r"ven_([0-9a-f]{4})", text)
    device = re.search(r"dev_([0-9a-f]{4})", text)
    return (int(vendor.group(1), 16) if vendor else None,
            int(device.group(1), 16) if device else None)


def _classify_integrated(name, vram_total, vendor):
    """
    Integrated, discrete, or unknown.

    The name is the strong signal and the only one used for a positive "integrated" verdict.
    A small VRAM figure is NOT sufficient — plenty of discrete cards report a carve-out — so a
    discrete-looking name with a small figure stays discrete, and anything the patterns do not
    recognise returns None rather than a guess.
    """
    lowered = (name or "").lower()
    if not lowered:
        return None
    for pattern in _INTEGRATED_PATTERNS:
        if re.search(pattern, lowered):
            return True
    # NVIDIA has shipped no integrated PC graphics; an NVIDIA adapter is discrete.
    if vendor == VENDOR_NVIDIA:
        return False
    # A named adapter carrying a real, sizeable dedicated pool is discrete.
    if vram_total and vram_total >= 2 * 1024 ** 3:
        return False
    return None


def _is_software_adapter(name):
    lowered = (name or "").lower()
    return any(marker in lowered for marker in _SOFTWARE_ADAPTERS)


@functools.lru_cache(maxsize=1)
def gpu_adapters():
    """
    Every graphics adapter on this machine, best-first. Returns a tuple (hashable, cached).

    ORDERING IS THE CONTRACT. Callers that want "the GPU" take the first entry, so the sort
    puts real hardware ahead of virtual adapters, discrete ahead of integrated, and larger
    VRAM ahead of smaller. On this host that yields the RTX 4060 first and the Radeon 780M
    second, which is the order a user would name them.

    Off Windows this returns an empty tuple rather than a fabricated entry. There is no
    portable registry-equivalent, and inventing one adapter from an OpenGL string would be a
    guess presented as a measurement.
    """
    if not sys.platform.startswith("win") or winreg is None:
        return ()

    adapters = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS_KEY) as root:
            index = 0
            while True:
                try:
                    subkey = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                # Only the numbered instance keys describe adapters; `Configuration`,
                # `Properties` and friends are siblings that must not be parsed as one.
                if not subkey.isdigit():
                    continue
                values = _reg_values(
                    winreg.HKEY_LOCAL_MACHINE, f"{_GPU_CLASS_KEY}\\{subkey}",
                    ("DriverDesc", "HardwareInformation.qwMemorySize",
                     "HardwareInformation.MemorySize", "DriverVersion", "ProviderName",
                     "MatchingDeviceId"))
                name = str(values.get("DriverDesc") or "").strip()
                if not name:
                    continue

                # 64-bit first. The 32-bit `MemorySize` is the clamped field and is used only
                # when the 64-bit one is absent (very old drivers), where its value is at
                # least not saturated because the card is genuinely small.
                vram = _as_int(values.get("HardwareInformation.qwMemorySize")) or 0
                if not vram:
                    legacy = _as_int(values.get("HardwareInformation.MemorySize")) or 0
                    # 4293918720 is the observed clamp. Anything at or above 4000 MiB from the
                    # 32-bit field is saturated and is reported as unknown, exactly as
                    # `system_profile` used to — the 64-bit read above is what normally
                    # makes this branch unnecessary.
                    vram = legacy if 0 < legacy < 4000 * 1024 ** 2 else 0

                vendor_id, device_id = _parse_pci_ids(values.get("MatchingDeviceId"))
                vendor = PCI_VENDORS.get(vendor_id or -1, "")
                if not vendor:
                    provider = str(values.get("ProviderName") or "")
                    lowered = provider.lower()
                    if "nvidia" in lowered:
                        vendor = VENDOR_NVIDIA
                    elif "advanced micro" in lowered or "amd" in lowered:
                        vendor = VENDOR_AMD
                    elif "intel" in lowered:
                        vendor = VENDOR_INTEL
                    else:
                        vendor = provider.strip()

                software = _is_software_adapter(name) or vendor_id is None
                adapters.append(GPUAdapter(
                    vendor=vendor, name=name, vram_total=max(0, vram),
                    driver_version=str(values.get("DriverVersion") or "").strip(),
                    integrated=_classify_integrated(name, vram, vendor),
                    pci_vendor_id=vendor_id, pci_device_id=device_id,
                    software=software, source="registry"))
    except OSError:
        return ()

    def rank(adapter):
        return (0 if adapter.software else 1,
                0 if adapter.integrated else 1,
                adapter.vram_total)

    adapters.sort(key=rank, reverse=True)
    return tuple(adapters)


def primary_gpu():
    """
    The adapter a user would call "my graphics card", or None when there is no real one.

    Virtual/software adapters are never returned: a machine whose only adapter is the Microsoft
    Basic Display Adapter has no usable GPU, and saying so is more useful than naming the shim.
    """
    for adapter in gpu_adapters():
        if not adapter.software:
            return adapter
    return None


def nvidia_gpu():
    """The first NVIDIA adapter, or None. This is the ONLY gate for NVIDIA-specific work."""
    for adapter in gpu_adapters():
        if adapter.is_nvidia and not adapter.software:
            return adapter
    return None


def has_nvidia_gpu():
    """
    True when an NVIDIA adapter is physically present.

    This is what NVIDIA-only code paths must ask before doing anything NVIDIA-specific —
    spawning `nvidia-smi`, planning a CUDA provider, or installing a CUDA runtime wheel. It
    reads the registry and never launches a process, so it is safe to call on any machine and
    costs nothing on the machines where the answer is no.
    """
    return nvidia_gpu() is not None


def gpu_vendors():
    """The distinct vendors of the real adapters present, best-first."""
    seen = []
    for adapter in gpu_adapters():
        if adapter.software or not adapter.vendor:
            continue
        if adapter.vendor not in seen:
            seen.append(adapter.vendor)
    return tuple(seen)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              DISPLAYS                                  │
# └────────────────────────────────────────────────────────────────────────┘

@functools.lru_cache(maxsize=1)
def displays():
    """
    (count, width, height, scale) for the primary display, measured — never assumed.

    `1920x1080` is not a safe default: it is wrong on every high-DPI laptop, which is most of
    them. When nothing can be measured the answer is `(0, 0, 0, 1.0)` and the caller renders
    "unknown".

    `user32` is used directly rather than Qt because this is a `core` module and `core` is a
    leaf — it may not import the UI, and the System screen is not the only caller.
    """
    if not sys.platform.startswith("win"):
        return 0, 0, 0, 1.0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        count = int(user32.GetSystemMetrics(80))          # SM_CMONITORS

        # `GetSystemMetrics(SM_CXSCREEN)` returns the DPI-*virtualised* size unless the process
        # declared per-monitor awareness — measured here as 1440x900 on a 2880x1800 panel at
        # 200%. That is not the display's resolution, it is what a scaled window thinks the
        # desktop is, so reporting it would be exactly the fabricated-value failure this module
        # exists to avoid. `EnumDisplaySettingsW(ENUM_CURRENT_SETTINGS)` reports the adapter's
        # ACTUAL current mode and is unaffected by the calling process's awareness.
        class _DEVMODE(ctypes.Structure):
            _fields_ = [("dmDeviceName", wintypes.WCHAR * 32),
                        ("dmSpecVersion", wintypes.WORD),
                        ("dmDriverVersion", wintypes.WORD),
                        ("dmSize", wintypes.WORD),
                        ("dmDriverExtra", wintypes.WORD),
                        ("dmFields", wintypes.DWORD),
                        ("dmPositionX", ctypes.c_long),
                        ("dmPositionY", ctypes.c_long),
                        ("dmDisplayOrientation", wintypes.DWORD),
                        ("dmDisplayFixedOutput", wintypes.DWORD),
                        ("dmColor", ctypes.c_short),
                        ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short),
                        ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short),
                        ("dmFormName", wintypes.WCHAR * 32),
                        ("dmLogPixels", wintypes.WORD),
                        ("dmBitsPerPel", wintypes.DWORD),
                        ("dmPelsWidth", wintypes.DWORD),
                        ("dmPelsHeight", wintypes.DWORD),
                        ("dmDisplayFlags", wintypes.DWORD),
                        ("dmDisplayFrequency", wintypes.DWORD),
                        ("dmICMMethod", wintypes.DWORD),
                        ("dmICMIntent", wintypes.DWORD),
                        ("dmMediaType", wintypes.DWORD),
                        ("dmDitherType", wintypes.DWORD),
                        ("dmReserved1", wintypes.DWORD),
                        ("dmReserved2", wintypes.DWORD),
                        ("dmPanningWidth", wintypes.DWORD),
                        ("dmPanningHeight", wintypes.DWORD)]

        mode = _DEVMODE()
        mode.dmSize = ctypes.sizeof(_DEVMODE)
        width = height = 0
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):   # ENUM_CURRENT_SETTINGS
            width, height = int(mode.dmPelsWidth), int(mode.dmPelsHeight)
        if not width or not height:
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))

        # The scale is then the ratio between the real mode and the virtualised metric, which
        # is correct whether or not this process is DPI-aware — unlike `GetDpiForSystem`, which
        # answers 96 for an unaware process on a 200% display.
        scale = 1.0
        virtual_width = int(user32.GetSystemMetrics(0))
        if virtual_width and width:
            scale = round(width / float(virtual_width), 2) or 1.0
        return max(0, count), max(0, width), max(0, height), scale
    except Exception:
        return 0, 0, 0, 1.0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CACHE CONTROL                               │
# └────────────────────────────────────────────────────────────────────────┘

def reset_cache():
    """
    Drop every cached answer.

    Hardware does not change while a process runs, so nothing in production calls this. It
    exists for the test suite, which substitutes synthetic machines and must not leak one
    fixture's answers into the next check.
    """
    for cached in (os_info, cpu_info, gpu_adapters, displays):
        cached.cache_clear()
