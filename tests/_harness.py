# ┌────────────────────────────────────────────────────────────────────────┐
# │                            _harness.py                                 │
# │      Shared Test Scaffolding — Isolation, Doubles, Capability Gates    │
# └────────────────────────────────────────────────────────────────────────┘
"""
The pieces every Kayra suite needs, in one place.

Kayra's tests are STANDALONE SCRIPTS, not a pytest suite — each is run directly and exits
non-zero on failure. That convention predates this module and is deliberate: the suites boot
real subsystems, take real locks and own real processes, and a collector that imports all of
them into one interpreter would have them fighting over the microphone. This module does not
change that. It gives the scripts a common `check()`, a common summary, and — the part that
matters most — a structural guarantee that running them cannot damage the machine they run on.

WHY THE ISOLATION IS STRUCTURAL RATHER THAN A CONVENTION
--------------------------------------------------------
It has already failed as a convention, twice, on this project:

  * `test_ui.py` drove the real `SettingsView._on_backend`, which PERSISTS a committed switch
    — so a suite with a stubbed bridge reporting success rewrote the developer's own `.env`.
  * `test_proactive_agent.py` read the machine's real battery through `pressure_sample()`, was
    green all afternoon, and then failed nine checks because the laptop dropped to 12% and
    unplugged. The presence layer was right; the suite was wrong.

Both were found by a run going red on unchanged code, which is the expensive way to find them.
`EnvironmentGuard` and `HostPin` below make each class of mistake fail LOUDLY AT THE MOMENT IT
HAPPENS, naming the file that was written or the host fact that was read, instead of surfacing
as an unrelated failure hours later.

WHAT IS PROTECTED
-----------------
`.env`, the conversation/memory store, `data/habits.json`, the browser-support cache, API keys
in `os.environ`, and the real mouse. Anything a suite genuinely needs to write goes to a
temporary directory that is removed on exit.
"""

import os
import io
import sys
import time
import shutil
import tempfile
import platform
import traceback

# The package lives under src/; put it on the path so suites run without installing.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(PROJECT_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE CHECK PROTOCOL                            │
# └────────────────────────────────────────────────────────────────────────┘
# Matches what every existing suite already does by hand, so a suite can adopt this without
# its output changing shape. `Checker` is an object rather than module globals because two
# suites imported into one interpreter (the runner does this for nothing today, but the door
# is left open) must not share a failure list.

class Checker:
    """Counts checks, prints them in the project's format, and exits non-zero on failure."""

    def __init__(self, title):
        self.title = title
        self.failures = []
        self.passed = 0
        self.started = time.time()

    def __call__(self, label, condition, detail=""):
        from kayra.utils import print_success, print_error
        if condition:
            self.passed += 1
            print_success(f"PASS  {label}")
        else:
            self.failures.append(label)
            print_error(f"FAIL  {label}" + (f"  ({detail})" if detail else ""))
        return bool(condition)

    def section(self, label):
        from kayra.utils import print_system
        print_system(f"\n{label}")

    def skip(self, label, reason):
        """
        A capability that is not present is NOT a failure.

        "This machine has no NVIDIA GPU" and "the NVIDIA path is broken" are different facts,
        and a suite that reports the first as the second teaches its readers to ignore red.
        """
        from kayra.utils import print_info
        print_info(f"SKIP  {label}  ({reason})")

    def finish(self):
        from kayra.utils import print_system, print_success, print_error
        elapsed = time.time() - self.started
        print_system("\n" + "=" * 60)
        total = self.passed + len(self.failures)
        if self.failures:
            print_error(f"{len(self.failures)} {self.title} check(s) FAILED "
                        f"({self.passed} passed of {total}, {elapsed:.1f}s).")
            for name in self.failures:
                print_error(f"    - {name}")
            return 1
        print_success(f"All {self.title} checks passed ({self.passed}, {elapsed:.1f}s).")
        return 0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        ENVIRONMENT ISOLATION                           │
# └────────────────────────────────────────────────────────────────────────┘

# The files a suite must never write. Absolute paths, resolved once.
def _protected_paths():
    paths = [os.path.join(PROJECT_ROOT, ".env")]
    for relative in ("data/conversation.json", "data/conversation_backup.json",
                     "data/habits.json", "data/browser_support.json"):
        paths.append(os.path.join(PROJECT_ROOT, *relative.split("/")))
    return paths


class EnvironmentGuard:
    """
    Snapshots everything a suite could damage, and reports anything that changed.

    Used as a context manager around a whole suite:

        with EnvironmentGuard() as guard:
            ...run the suite...
        check("the suite left the environment alone", not guard.modified(), guard.report())

    It RESTORES rather than merely reporting, so a suite that does write is not left having
    damaged the machine — but it also records what it restored, so the write is still a
    failure the run has to answer for. Silently repairing would hide the defect; refusing to
    repair would punish the developer for a test's mistake.
    """

    def __init__(self, extra_paths=()):
        self.paths = _protected_paths() + list(extra_paths)
        self._before = {}
        self._env_before = {}
        self.changed = []

    def __enter__(self):
        for path in self.paths:
            try:
                with open(path, "rb") as handle:
                    self._before[path] = handle.read()
            except OSError:
                self._before[path] = None       # absent is a state worth restoring too
        self._env_before = dict(os.environ)
        return self

    def __exit__(self, *exc):
        for path, original in self._before.items():
            try:
                current = None
                if os.path.exists(path):
                    with open(path, "rb") as handle:
                        current = handle.read()
                if current == original:
                    continue
                self.changed.append(os.path.relpath(path, PROJECT_ROOT))
                if original is None:
                    os.remove(path)
                else:
                    with open(path, "wb") as handle:
                        handle.write(original)
            except OSError:
                self.changed.append(os.path.relpath(path, PROJECT_ROOT) + " (unrestorable)")
        # Environment variables are restored quietly: suites set them on purpose all the time,
        # and a leaked variable damages nothing outside this process.
        os.environ.clear()
        os.environ.update(self._env_before)
        return False

    def modified(self):
        return bool(self.changed)

    def report(self):
        return ", ".join(self.changed) or "nothing"


class TemporaryProject:
    """
    A throwaway project root: `data/`, `models/`, `logs/`, `Reports/`, and a `.env`.

    Point `kayra.core.paths` at this and a suite can exercise the real persistence code
    without touching the developer's conversation history. Removed on exit, always.
    """

    def __init__(self, env=None):
        self.root = tempfile.mkdtemp(prefix="kayra-test-project-")
        self.env = dict(env or {})
        self._patched = []
        for name in ("data", "models", "logs", "Reports"):
            os.makedirs(os.path.join(self.root, name), exist_ok=True)
        with io.open(os.path.join(self.root, ".env"), "w", encoding="utf-8") as handle:
            for key, value in self.env.items():
                handle.write(f"{key}={value}\n")

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def __enter__(self):
        from kayra.core import paths
        # Redirect the ONE authority rather than each caller. Every module already routes
        # through `core.paths`, so patching it here is what makes the isolation total instead
        # of covering whichever writers a suite happened to think of.
        self._patched = [(paths, "_ROOT", getattr(paths, "_ROOT", None))]
        for attribute in ("_ROOT", "PROJECT_ROOT", "ROOT"):
            if hasattr(paths, attribute):
                self._patched.append((paths, attribute, getattr(paths, attribute)))
                setattr(paths, attribute, self.root)
        return self

    def __exit__(self, *exc):
        for module, attribute, original in self._patched:
            if original is None and attribute == "_ROOT":
                continue
            try:
                setattr(module, attribute, original)
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)
        return False


class HostPin:
    """
    Pins the host facts a tier-1 suite must not read: battery, CPU load, memory pressure.

    `test_proactive_agent.py` learned this the hard way — see the module docstring. A suite
    whose verdict depends on the developer's charge level is not a suite anybody can trust,
    and the fix is not "remember not to", it is "make the real value unreachable".

    Tests that genuinely care about a threshold drive `pressure_sample` themselves with their
    own numbers; this only stops the AMBIENT reads from leaking a real machine in.
    """

    def __init__(self, cpu_percent=12.0, ram_percent=40.0,
                 battery_percent=88.0, battery_plugged=True):
        self.sample = {"cpu_percent": cpu_percent, "ram_percent": ram_percent,
                       "battery_percent": battery_percent,
                       "battery_plugged": battery_plugged}
        self._original = None

    def __enter__(self):
        from kayra.core import system_profile
        self._original = system_profile.pressure_sample
        system_profile.pressure_sample = lambda *a, **k: dict(self.sample)
        return self

    def __exit__(self, *exc):
        from kayra.core import system_profile
        if self._original is not None:
            system_profile.pressure_sample = self._original
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SYNTHETIC MACHINES                            │
# └────────────────────────────────────────────────────────────────────────┘
# Machines nobody owns, so that any real value appearing in a rendered screen or a setup
# decision can only have come from the host — which is the defect being hunted.
#
# Every field is what `core.hardware` and `setup.detect_graphics` actually produce, so a
# fixture can be substituted for either without an adapter in between.

class SyntheticMachine:
    """One hypothetical machine: OS, processor, and graphics adapters."""

    __slots__ = ("label", "product_name", "build", "revision", "display_version",
                 "is_server", "cpu_vendor", "cpu_model", "cores", "threads",
                 "adapters", "ram_total")

    def __init__(self, label, product_name, build, display_version, cpu_vendor, cpu_model,
                 cores, threads, adapters, ram_total, revision=1000, is_server=False):
        self.label = label
        self.product_name = product_name
        self.build = build
        self.revision = revision
        self.display_version = display_version
        self.is_server = is_server
        self.cpu_vendor = cpu_vendor
        self.cpu_model = cpu_model
        self.cores = cores
        self.threads = threads
        self.adapters = adapters            # [(vendor, name, vram_bytes)]
        self.ram_total = ram_total

    @property
    def has_nvidia(self):
        return any(vendor == "NVIDIA" for vendor, _name, _vram in self.adapters)

    def graphics_dict(self):
        """The shape `setup.detect_graphics()` returns, for the setup-logic tests."""
        vendors = []
        for vendor, _n, _v in self.adapters:
            if vendor and vendor not in vendors:
                vendors.append(vendor)
        nvidia = next((a for a in self.adapters if a[0] == "NVIDIA"), None)
        return {
            "adapters": list(self.adapters),
            "vendors": vendors,
            "nvidia": nvidia is not None,
            "nvidia_name": nvidia[1] if nvidia else None,
            "nvidia_vram_mib": (nvidia[2] / 1024 ** 2) if (nvidia and nvidia[2]) else None,
            "primary": self.adapters[0] if self.adapters else None,
            "driver_ready": nvidia is not None,
        }


# The five machines Part 9 of this milestone names, plus two that exercise the OS rules.
MACHINES = [
    SyntheticMachine(
        "A  Windows 11 / Intel i5 / RTX 3050",
        # The STALE registry name. Windows 11 machines record "Windows 10" here; a fixture
        # that recorded the truth would never exercise the correction that matters.
        product_name="Windows 10 Home", build=22631, display_version="23H2",
        cpu_vendor="Intel", cpu_model="13th Gen Intel(R) Core(TM) i5-13420H",
        cores=8, threads=12, ram_total=8 * 1024 ** 3,
        adapters=[("NVIDIA", "NVIDIA GeForce RTX 3050 Laptop GPU", 4 * 1024 ** 3)]),
    SyntheticMachine(
        "B  Windows 11 / Ryzen 7 / RTX 4060",
        product_name="Windows 10 Pro", build=26100, display_version="24H2",
        cpu_vendor="AMD", cpu_model="AMD Ryzen 7 7840HS w/ Radeon 780M Graphics",
        cores=8, threads=16, ram_total=16 * 1024 ** 3,
        adapters=[("NVIDIA", "NVIDIA GeForce RTX 4060 Laptop GPU", 8 * 1024 ** 3),
                  ("AMD", "AMD Radeon 780M Graphics", 1024 ** 3)]),
    SyntheticMachine(
        "C  Windows 11 / Intel i7 / integrated only",
        product_name="Windows 11 Home", build=26100, display_version="24H2",
        cpu_vendor="Intel", cpu_model="12th Gen Intel(R) Core(TM) i7-1255U",
        cores=10, threads=12, ram_total=16 * 1024 ** 3,
        adapters=[("Intel", "Intel(R) Iris(R) Xe Graphics", 0)]),
    SyntheticMachine(
        "D  Windows 11 / Ryzen / Radeon discrete",
        product_name="Windows 10 Pro", build=22621, display_version="22H2",
        cpu_vendor="AMD", cpu_model="AMD Ryzen 9 7950X 16-Core Processor",
        cores=16, threads=32, ram_total=32 * 1024 ** 3,
        adapters=[("AMD", "AMD Radeon RX 7900 XT", 20 * 1024 ** 3)]),
    SyntheticMachine(
        "E  no adapter and no telemetry",
        product_name="Windows 10 Pro", build=26100, display_version="24H2",
        cpu_vendor="Intel", cpu_model="Intel(R) Xeon(R) W-2225 CPU @ 4.10GHz",
        cores=4, threads=8, ram_total=64 * 1024 ** 3, adapters=[]),
    SyntheticMachine(
        "F  genuine Windows 10",
        product_name="Windows 10 Pro", build=19045, display_version="22H2",
        cpu_vendor="Intel", cpu_model="Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz",
        cores=6, threads=12, ram_total=16 * 1024 ** 3,
        adapters=[("NVIDIA", "NVIDIA GeForce GTX 1080", 8 * 1024 ** 3)]),
    SyntheticMachine(
        "G  Windows Server",
        product_name="Windows Server 2025 Standard", build=26100, display_version="24H2",
        cpu_vendor="AMD", cpu_model="AMD EPYC 9354P 32-Core Processor",
        cores=32, threads=64, ram_total=256 * 1024 ** 3, adapters=[], is_server=True),
]

MACHINES_BY_LETTER = {m.label[0]: m for m in MACHINES}


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            TEST DOUBLES                                │
# └────────────────────────────────────────────────────────────────────────┘

class RecordingInstaller:
    """
    A pip that installs nothing and remembers everything it was asked to do.

    THE POINT IS THAT A UNIT TEST MUST NOT CHANGE THE ENVIRONMENT IT RUNS IN. The setup logic
    tests exercise real decision code — which ORT variant, which CUDA wheels, what to
    reconcile — and the decision is the thing under test; actually performing it would make
    running the suite a ~2GB download and would leave the developer's venv in whatever state
    the last fixture described.
    """

    class Result:
        def __init__(self, returncode=0):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = tuple(fail_on)

    def __call__(self, python_exe, *args, **kwargs):
        self.calls.append(list(args))
        if any(marker in " ".join(str(a) for a in args) for marker in self.fail_on):
            return self.Result(1)
        return self.Result(0)

    # ── Questions the assertions ask ──

    def installed(self):
        """Every distribution this installer was asked to install, unpinned."""
        names = []
        for call in self.calls:
            if not call or call[0] != "install":
                continue
            for token in call[1:]:
                if str(token).startswith("-"):
                    continue
                names.append(str(token).split("==")[0])
        return names

    def uninstalled(self):
        names = []
        for call in self.calls:
            if not call or call[0] != "uninstall":
                continue
            names.extend(str(t) for t in call[1:] if not str(t).startswith("-"))
        return names

    def touched_nvidia(self):
        return [n for n in self.installed() if n.startswith("nvidia-")]


class FakeMouse:
    """
    A pointer that records instead of acting.

    The gesture suites must be runnable while the developer is reading their output, so the
    default everywhere is that nothing reaches the desktop. `tests/test_gesture_live.py`
    already applies this rule and requires a typed confirmation for `--real-mouse`; this is
    the same double, available to any suite that needs one.
    """

    def __init__(self):
        self.moves = []
        self.clicks = []
        self.scrolls = []
        self.buttons_down = set()

    def move_to(self, x, y):
        self.moves.append((x, y))

    def click(self, button="left"):
        self.clicks.append(button)

    def press(self, button="left"):
        self.buttons_down.add(button)

    def release(self, button="left"):
        self.buttons_down.discard(button)

    def scroll(self, delta):
        self.scrolls.append(delta)


class FakeClock:
    """A monotonic clock a test drives, so policy can be checked at exact offsets."""

    def __init__(self, start_ms=0):
        self.now = start_ms

    def __call__(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds
        return self.now


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          CAPABILITY GATES                              │
# └────────────────────────────────────────────────────────────────────────┘
# "The hardware is not here" is never a failure. Each of these answers cheaply and without
# side effects, so a gate never costs more than the test it guards.

def has_nvidia():
    try:
        from kayra.core import hardware
        return hardware.has_nvidia_gpu()
    except Exception:
        return False


def has_camera():
    """True when a capture device can be OPENED — not merely when OpenCV is importable."""
    try:
        import cv2
    except Exception:
        return False
    capture = None
    try:
        capture = cv2.VideoCapture(0, cv2.CAP_ANY)
        return bool(capture.isOpened())
    except Exception:
        return False
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def has_microphone():
    try:
        import sounddevice as sd
        return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
    except Exception:
        return False


def has_speech_browser():
    try:
        from kayra.input import browsers
        return any(b.recognition != "none" for b in browsers.discover_browsers())
    except Exception:
        return False


def has_network():
    """
    A cheap reachability probe, with a hard budget.

    A TCP connect, not an HTTP request: the question is "is there a route out", and paying a
    TLS handshake plus a response body to answer it is the sort of avoidable cost the boot
    path already removed once.
    """
    import socket
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.5):
            return True
    except OSError:
        return False


def has_tts_model():
    try:
        from kayra.core.paths import model_path
        return any(os.path.isfile(model_path(name))
                   for name in ("kokoro-v1.0.int8.onnx", "kokoro.onnx"))
    except Exception:
        return False


def can_reach_input_desktop():
    """
    Can this process actually inject input?

    Windows silently returns FALSE from `SetCursorPos` when the process cannot reach the
    interactive input desktop — so every injection LOOKS like it worked. A real-mouse test
    that ran there would report success having done nothing.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        desktop = user32.OpenInputDesktop(0, False, 0x0100)     # DESKTOP_READOBJECTS
        if not desktop:
            return False
        user32.CloseDesktop(desktop)
        return True
    except Exception:
        return False


def describe_host():
    """One line naming what this run can and cannot exercise. Printed by every suite."""
    bits = [f"{platform.python_version()} on {platform.system()}"]
    bits.append("nvidia" if has_nvidia() else "no-nvidia")
    bits.append("camera" if has_camera() else "no-camera")
    bits.append("mic" if has_microphone() else "no-mic")
    return "  ·  ".join(bits)


def run(main_callable):
    """
    Standard entry point: run `main_callable`, translate an exception into a failure exit.

    An uncaught exception in a suite is a FAILING RUN, not a crash to be read as noise — and
    the traceback is printed rather than swallowed, because a suite that dies silently is
    indistinguishable from one that passed.
    """
    try:
        return int(main_callable() or 0)
    except Exception:
        traceback.print_exc()
        return 1
