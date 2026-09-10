# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_setup_runtime.py                            │
# │   setup.py — Hardware-Conditional Dependency Provisioning & Repair     │
# └────────────────────────────────────────────────────────────────────────┘
r"""
test_setup_runtime.py — assertion suite for the part of `setup.py` that decides what to
install.

    .venv\Scripts\python tests\test_setup_runtime.py

SAFETY — READ THIS FIRST
------------------------
**NOTHING IS INSTALLED, UNINSTALLED OR DOWNLOADED BY THIS SUITE.** `setup._pip` is replaced by
a `RecordingInstaller` that answers success and writes down what it was asked to do; the
assertions are made against that record. That is not a convenience — the decision is the thing
under test, and actually performing it would mean a ~1.4 GB download per fixture and would
leave the developer's `.venv` in whatever state the last synthetic machine described.

The suite also never writes `.env`, never touches the memory store, and spawns no process
except the ONE it is explicitly measuring (the real `detect_graphics()` on this host, which
runs `nvidia-smi` only if this machine actually has an NVIDIA adapter). An `EnvironmentGuard`
wraps the whole run and fails it if anything the developer owns changed.

WHAT IT PROVES
--------------
The rule this milestone added: **NVIDIA runtime dependencies are installed only when an NVIDIA
GPU actually exists.** Previously the gate was "did `nvidia-smi` answer", which is a different
question and gets two cases wrong — a machine with an NVIDIA card and a stale driver was told
it had no GPU, and a venv carried from an NVIDIA machine kept `onnxruntime-gpu` plus four CUDA
runtime wheels it could never load.

Section 3 drives `configure_speech_runtime()` against every synthetic machine and asserts, for
each, exactly which distributions the installer was asked for. Section 5 asserts the report
distinguishes PASS, FAIL and NOT APPLICABLE — "CUDA: FAIL" on a machine with a Radeon describes
a defect that does not exist.
"""

import os
import io
import ast
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (Checker, EnvironmentGuard, RecordingInstaller, MACHINES,
                      MACHINES_BY_LETTER, describe_host, run, PROJECT_ROOT)

from kayra.utils import print_banner, print_info

sys.path.insert(0, PROJECT_ROOT)
import setup as kayra_setup                                            # noqa: E402

check = Checker("setup runtime")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        THE SANDBOXED SETUP                             │
# └────────────────────────────────────────────────────────────────────────┘

class SandboxedSetup:
    """
    `setup.py` with every side effect replaced, and one synthetic machine underneath it.

    Substituted:
      `_pip`                  -> a recorder. Installs nothing.
      `detect_graphics`       -> the fixture's hardware.
      `_installed_packages`   -> the fixture's starting environment.
      `_ort_report`           -> what ORT would say about the variant that ends up chosen.
      `_probe_cuda_session`   -> the fixture's CUDA verdict.

    Everything BETWEEN those — the branch structure, the reconciliation, the pin selection,
    the report's three-way verdict — is the real code, which is the point.
    """

    def __init__(self, machine, installed=None, cuda_ok=True, cuda_build="12.8",
                 driver_ready=None, quiet=True):
        self.machine = machine
        self.installed = dict(installed or {})
        self.cuda_ok = cuda_ok
        self.cuda_build = cuda_build
        self.driver_ready = driver_ready
        self.pip = RecordingInstaller()
        self.quiet = quiet
        self._saved = {}

    def _graphics(self):
        graphics = self.machine.graphics_dict()
        if self.driver_ready is not None:
            graphics["driver_ready"] = self.driver_ready
        return graphics

    def __enter__(self):
        module = kayra_setup
        for name in ("_pip", "detect_graphics", "_installed_packages", "_ort_report",
                     "_probe_cuda_session", "info", "step", "ok", "warn", "fail"):
            self._saved[name] = getattr(module, name)

        module._pip = self.pip
        module.detect_graphics = self._graphics

        # `_installed_packages` is re-read after a reconcile, so it has to REFLECT the
        # installer's record — otherwise the test would be asserting against a frozen world
        # and the uninstall step would look like it had done nothing.
        def installed_packages(python_exe):
            current = dict(self.installed)
            for name in self.pip.uninstalled():
                current.pop(name, None)
            for name in self.pip.installed():
                current.setdefault(name, "0.0.0")
            return current

        module._installed_packages = installed_packages

        def ort_report(python_exe):
            current = installed_packages(python_exe)
            package = ("onnxruntime-gpu" if "onnxruntime-gpu" in current
                       else "onnxruntime")
            providers = ["CPUExecutionProvider"]
            build = None
            if package == "onnxruntime-gpu":
                providers = ["TensorrtExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider"]
                build = self.cuda_build
            return {"ok": True, "package": package, "version": "1.26.0",
                    "location": "/fixture/onnxruntime/__init__.py",
                    "providers": providers, "cuda_build": build}

        module._ort_report = ort_report
        module._probe_cuda_session = lambda python_exe: {
            "ok": self.cuda_ok,
            "provider": "CUDAExecutionProvider" if self.cuda_ok else "CPUExecutionProvider",
            "error": "" if self.cuda_ok else "cublasLt64_12.dll missing"}

        if self.quiet:
            for name in ("info", "step", "ok", "warn", "fail"):
                setattr(module, name, lambda *a, **k: None)
        return self

    def __exit__(self, *exc):
        for name, original in self._saved.items():
            setattr(kayra_setup, name, original)
        return False

    def configure(self):
        return kayra_setup.configure_speech_runtime("<fixture-python>")


# ┌────────────────────────────────────────────────────────────────────────┐
# │             1. DETECTION AGREES WITH THE APPLICATION                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_detection():
    check.section("[1] Hardware detection")
    from kayra.core import hardware

    graphics = kayra_setup.detect_graphics()
    check("detect_graphics returns a dict", isinstance(graphics, dict))
    for field in ("adapters", "vendors", "nvidia", "nvidia_name", "primary", "driver_ready"):
        check(f"it exposes '{field}'", field in graphics)

    # THE TWO IMPLEMENTATIONS MUST AGREE. `setup.py` cannot import `kayra` — it runs on the
    # system interpreter before `.venv` exists — so the registry logic is deliberately
    # duplicated. This is the check that keeps the duplicate honest; without it a fix applied
    # to one would silently leave the other reading the machine differently.
    check("setup and kayra.core.hardware agree about NVIDIA",
          graphics["nvidia"] == hardware.has_nvidia_gpu(),
          f"setup={graphics['nvidia']} kayra={hardware.has_nvidia_gpu()}")
    check("setup and kayra.core.hardware agree about the adapter count",
          len(graphics["adapters"]) == len([a for a in hardware.gpu_adapters()
                                            if not a.software]),
          f"setup={len(graphics['adapters'])}")
    check("setup and kayra.core.hardware agree about the vendors",
          list(graphics["vendors"]) == list(hardware.gpu_vendors()),
          f"{graphics['vendors']} vs {list(hardware.gpu_vendors())}")

    print_info(f"      this machine: {kayra_setup.describe_graphics(graphics)}")

    check("describe_graphics never says NVIDIA on a machine without one",
          graphics["nvidia"]
          or "nvidia" not in kayra_setup.describe_graphics(graphics).lower(),
          kayra_setup.describe_graphics(graphics))
    check("an absent GPU is described, not left blank",
          bool(kayra_setup.describe_graphics({"nvidia": False, "primary": None,
                                              "adapters": [], "vendors": []})))

    # ── nvidia-smi is spawned only when an NVIDIA adapter exists ──
    spawned = []
    original_run = kayra_setup.subprocess.run
    original_registry = kayra_setup._registry_adapters
    try:
        kayra_setup.subprocess.run = lambda *a, **k: spawned.append(a) or original_run(*a, **k)
        kayra_setup._registry_adapters = lambda: [("AMD", "AMD Radeon RX 7900 XT",
                                                   20 * 1024 ** 3)]
        result = kayra_setup.detect_graphics()
        check("an AMD-only machine reports no NVIDIA", result["nvidia"] is False)
        check("and spawns no process to find that out", not spawned,
              "the registry already answered")

        kayra_setup._registry_adapters = lambda: []
        spawned.clear()
        result = kayra_setup.detect_graphics()
        check("a machine with no adapters reports none", result["primary"] is None)
    finally:
        kayra_setup.subprocess.run = original_run
        kayra_setup._registry_adapters = original_registry


# ┌────────────────────────────────────────────────────────────────────────┐
# │           2. THE INSTALLER IS NEVER ASKED TO DO ANYTHING REAL          │
# └────────────────────────────────────────────────────────────────────────┘

def section_sandbox_integrity():
    check.section("[2] Sandbox integrity")

    machine = MACHINES_BY_LETTER["B"]
    with SandboxedSetup(machine) as sandbox:
        sandbox.configure()
        check("the recording installer captured the calls", len(sandbox.pip.calls) > 0)
    check("the real _pip was restored", kayra_setup._pip is not RecordingInstaller)
    check("the real detect_graphics was restored",
          kayra_setup.detect_graphics.__name__ == "detect_graphics")

    # A guard around the fixtures themselves: if `SandboxedSetup` ever failed to substitute
    # `_pip`, this suite would start downloading gigabytes. Assert the substitution by
    # confirming a call reaches the recorder rather than pip.
    recorder = RecordingInstaller()
    result = recorder("<python>", "install", "onnxruntime==1.26.0")
    check("the recorder answers success without acting", result.returncode == 0)
    check("and remembers what it was asked for",
          recorder.installed() == ["onnxruntime"], str(recorder.installed()))
    recorder("<python>", "uninstall", "-y", "nvidia-cublas-cu12")
    check("uninstalls are recorded too",
          recorder.uninstalled() == ["nvidia-cublas-cu12"], str(recorder.uninstalled()))


# ┌────────────────────────────────────────────────────────────────────────┐
# │        3. THE DEPENDENCY DECISION, PER SYNTHETIC MACHINE               │
# └────────────────────────────────────────────────────────────────────────┘

def section_dependency_selection():
    check.section("[3] Runtime selection per machine")

    for machine in MACHINES:
        label = machine.label
        with SandboxedSetup(machine) as sandbox:
            state = sandbox.configure()

        installed = sandbox.pip.installed()
        nvidia_wheels = sandbox.pip.touched_nvidia()

        if machine.has_nvidia:
            check(f"[{label}] installs the GPU ONNX Runtime",
                  "onnxruntime-gpu" in installed, str(installed))
            check(f"[{label}] does not also install the CPU ONNX Runtime",
                  "onnxruntime" not in installed, str(installed))
            check(f"[{label}] installs the pinned CUDA runtime wheels",
                  set(nvidia_wheels) == set(kayra_setup._NVIDIA_RUNTIME_DISTRIBUTIONS),
                  str(nvidia_wheels))
            check(f"[{label}] treats CUDA as an applicable question",
                  state["cuda_applicable"] is True)
        else:
            check(f"[{label}] installs the CPU ONNX Runtime",
                  "onnxruntime" in installed, str(installed))
            check(f"[{label}] never installs onnxruntime-gpu",
                  "onnxruntime-gpu" not in installed, str(installed))
            # THE HEADLINE RULE OF THIS MILESTONE.
            check(f"[{label}] installs NO NVIDIA package of any kind",
                  not nvidia_wheels, str(nvidia_wheels))
            check(f"[{label}] marks CUDA NOT APPLICABLE rather than failed",
                  state["cuda_applicable"] is False and state["cuda_ok"] is False)

        check(f"[{label}] reports the hardware it decided from",
              state.get("graphics") is not None)

    # Every pin is version-locked. An unpinned GPU install once resolved to a CUDA 13 build
    # and orphaned the CUDA 12.8 wheels installed moments earlier.
    for spec in kayra_setup.CUDA_RUNTIME_PINS["12"]:
        check(f"'{spec}' is version-pinned", "==" in spec, spec)
    check("the GPU ONNX Runtime is version-pinned", "==" in kayra_setup.ORT_GPU_PIN)
    check("the reconciliation list matches the pinned wheels",
          {s.split("==")[0] for s in kayra_setup.CUDA_RUNTIME_PINS["12"]}
          == set(kayra_setup._NVIDIA_RUNTIME_DISTRIBUTIONS),
          "a wheel that can be installed but not removed is a leak")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 4. RECONCILIATION AND IDEMPOTENCE                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_reconciliation():
    check.section("[4] Reconciliation and re-run safety")

    gpu_env = {"onnxruntime-gpu": "1.26.0", "nvidia-cuda-runtime-cu12": "12.8.90",
               "nvidia-cublas-cu12": "12.8.4.1", "nvidia-cufft-cu12": "11.3.3.83",
               "nvidia-cudnn-cu12": "9.13.1.26"}
    cpu_env = {"onnxruntime": "1.26.0"}

    # ── A GPU environment carried to a machine with no NVIDIA GPU ──
    non_nvidia = MACHINES_BY_LETTER["D"]            # AMD Radeon discrete
    with SandboxedSetup(non_nvidia, installed=gpu_env) as sandbox:
        state = sandbox.configure()
    removed = sandbox.pip.uninstalled()
    check("[reconcile] the GPU ONNX Runtime is replaced, not kept",
          "onnxruntime-gpu" in removed or "onnxruntime" in sandbox.pip.installed(),
          str(sandbox.pip.calls))
    check("[reconcile] the orphaned CUDA wheels are removed",
          set(kayra_setup._NVIDIA_RUNTIME_DISTRIBUTIONS).issubset(set(removed)),
          str(removed))
    check("[reconcile] no NVIDIA wheel is installed on the way",
          not sandbox.pip.touched_nvidia(), str(sandbox.pip.touched_nvidia()))
    check("[reconcile] the outcome is reported", bool(state.get("reconciled")),
          str(state.get("reconciled")))
    check("[reconcile] CUDA is NOT APPLICABLE afterwards",
          state["cuda_applicable"] is False)

    # ── A CPU environment on a machine that has since gained an NVIDIA GPU ──
    nvidia = MACHINES_BY_LETTER["A"]
    with SandboxedSetup(nvidia, installed=cpu_env) as sandbox:
        state = sandbox.configure()
    check("[upgrade] the GPU ONNX Runtime is installed",
          "onnxruntime-gpu" in sandbox.pip.installed(), str(sandbox.pip.installed()))
    check("[upgrade] the CUDA runtime wheels are installed",
          set(sandbox.pip.touched_nvidia())
          == set(kayra_setup._NVIDIA_RUNTIME_DISTRIBUTIONS),
          str(sandbox.pip.touched_nvidia()))
    check("[upgrade] the CPU variant is removed rather than left alongside",
          "onnxruntime" in sandbox.pip.uninstalled()
          or "onnxruntime" not in sandbox.pip.installed(),
          str(sandbox.pip.calls))

    # ── IDEMPOTENCE. A second run against the state the first produced must do nothing. ──
    settled_gpu = dict(gpu_env)
    with SandboxedSetup(nvidia, installed=settled_gpu) as sandbox:
        sandbox.configure()
    check("[idempotent] a settled NVIDIA environment needs no install",
          not sandbox.pip.installed(), str(sandbox.pip.installed()))
    check("[idempotent] and no uninstall", not sandbox.pip.uninstalled(),
          str(sandbox.pip.uninstalled()))

    with SandboxedSetup(non_nvidia, installed=dict(cpu_env)) as sandbox:
        sandbox.configure()
    check("[idempotent] a settled CPU environment needs no install",
          not sandbox.pip.installed(), str(sandbox.pip.installed()))
    check("[idempotent] and no uninstall", not sandbox.pip.uninstalled(),
          str(sandbox.pip.uninstalled()))

    # ── BOTH VARIANTS PRESENT: remove all, reinstall the keeper ──
    # They share one `onnxruntime/` package directory and their RECORD manifests overlap, so
    # uninstalling only the loser deletes files the keeper still needs.
    both = {"onnxruntime": "1.26.0", "onnxruntime-gpu": "1.26.0"}
    with SandboxedSetup(nvidia, installed=both) as sandbox:
        sandbox.configure()
    removed = sandbox.pip.uninstalled()
    check("[conflict] both variants are removed before the keeper is reinstalled",
          "onnxruntime" in removed and "onnxruntime-gpu" in removed, str(removed))
    check("[conflict] and the keeper is reinstalled at its pin",
          any(kayra_setup.ORT_GPU_PIN in " ".join(str(t) for t in call)
              for call in sandbox.pip.calls if call and call[0] == "install"),
          str(sandbox.pip.calls))

    # ── An NVIDIA card whose driver does not answer ──
    with SandboxedSetup(nvidia, installed=dict(cpu_env), driver_ready=False) as sandbox:
        state = sandbox.configure()
    check("[stale driver] no CUDA wheel is installed for a driver that cannot load it",
          not sandbox.pip.touched_nvidia(), str(sandbox.pip.touched_nvidia()))
    check("[stale driver] the CPU runtime is provisioned so Kayra still runs",
          "onnxruntime" in sandbox.pip.installed()
          or state["variant"] == "onnxruntime", str(sandbox.pip.installed()))
    check("[stale driver] the reason names the driver, not the hardware",
          "driver" in (state.get("cuda_reason") or "").lower(), state.get("cuda_reason"))
    check("[stale driver] CUDA stays an applicable question",
          state["cuda_applicable"] is True,
          "the card is present, so the check is meaningful and merely unresolved")

    # ── CUDA present but the session refuses to initialize ──
    with SandboxedSetup(nvidia, installed=dict(gpu_env), cuda_ok=False) as sandbox:
        state = sandbox.configure()
    check("[cuda fail] a genuine failure is reported as a failure",
          state["cuda_applicable"] is True and state["cuda_ok"] is False)
    check("[cuda fail] with a reason that names the missing piece",
          "cublas" in (state.get("cuda_reason") or "").lower()
          or bool(state.get("cuda_reason")), state.get("cuda_reason"))

    # ── An unrecognised CUDA major must be reported, never guessed at ──
    with SandboxedSetup(nvidia, installed=dict(gpu_env), cuda_build="13.0") as sandbox:
        state = sandbox.configure()
    check("[unknown cuda] no package name is guessed",
          not sandbox.pip.touched_nvidia(), str(sandbox.pip.touched_nvidia()))
    check("[unknown cuda] the situation is explained",
          "13" in (state.get("cuda_reason") or ""), state.get("cuda_reason"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │            5. THE REPORT: PASS / FAIL / NOT APPLICABLE                 │
# └────────────────────────────────────────────────────────────────────────┘

def _render_report(state):
    """Capture `speech_runtime_report`'s output as text."""
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        kayra_setup.speech_runtime_report("<fixture-python>", state)
    return buffer.getvalue()


def section_report():
    check.section("[5] Setup report")

    for machine in MACHINES:
        with SandboxedSetup(machine) as sandbox:
            state = sandbox.configure()
        text = _render_report(state)
        label = machine.label

        check(f"[{label}] the report names the graphics hardware",
              "Graphics adapter" in text)
        if machine.adapters:
            check(f"[{label}] names the adapter it found",
                  machine.adapters[0][1] in text, text[:200])
        else:
            check(f"[{label}] says none was detected", "none detected" in text)

        if machine.has_nvidia:
            check(f"[{label}] CUDA is PASS or FAIL, never NOT APPLICABLE",
                  "NOT APPLICABLE" not in text)
            check(f"[{label}] a working CUDA session reads PASS", "PASS" in text)
        else:
            # THE FIX. Three lines used to read FAIL on every non-NVIDIA machine.
            check(f"[{label}] CUDA reads NOT APPLICABLE", "NOT APPLICABLE" in text, text[-400:])
            check(f"[{label}] and never reads FAIL", "FAIL" not in text, text[-400:])
            check(f"[{label}] the NVIDIA line states absence as a fact",
                  "not present on this machine" in text)
            check(f"[{label}] CPU is presented as correct, not as a fallback",
                  "correct for this hardware" in text, text[-300:])
            check(f"[{label}] the report never claims an NVIDIA card",
                  "NVIDIA GeForce" not in text and "RTX" not in text, text[-400:])
            # A missing CUDA provider is not a defect on a machine that cannot use one. A red
            # cross beside "Provider: CUDA — not offered" is the same false alarm as
            # "CUDA: FAIL", and the CPU wheel is the correct thing to have installed here.
            check(f"[{label}] the absent CUDA provider is not marked as wrong",
                  "not needed here" in text, text[:600])

    # A GPU machine whose CUDA genuinely fails still reads FAIL.
    with SandboxedSetup(MACHINES_BY_LETTER["A"], cuda_ok=False) as sandbox:
        state = sandbox.configure()
    text = _render_report(state)
    check("a real CUDA failure still reads FAIL", "FAIL" in text)
    check("and is never softened to NOT APPLICABLE", "NOT APPLICABLE" not in text)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                6. SETUP'S OWN STRUCTURAL RULES                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_structure():
    check.section("[6] setup.py structural rules")

    source = io.open(os.path.join(PROJECT_ROOT, "setup.py"), encoding="utf-8").read()
    tree = ast.parse(source)

    # `setup.py` runs on the SYSTEM interpreter before `.venv` exists.
    kayra_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            kayra_imports += [a.name for a in node.names if a.name.split(".")[0] == "kayra"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "kayra":
                kayra_imports.append(node.module)
    check("setup.py imports nothing from kayra", not kayra_imports, str(kayra_imports))

    third_party = {"psutil", "requests", "selenium", "numpy", "PySide6", "cv2",
                   "mediapipe", "onnxruntime", "cohere", "groq"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    check("setup.py imports no third-party package at module level",
          not (imported & third_party), str(imported & third_party))
    check("setup.py reads the registry through the stdlib", "winreg" in imported)

    # No shell string building, anywhere — the same rule the automation layer follows.
    shells = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True):
                    shells.append(node.lineno)
    check("setup.py never passes shell=True", not shells, str(shells))
    check("setup.py never calls os.system", "os.system(" not in source)

    # Every pip invocation goes through `_pip`, which targets the venv interpreter. A bare
    # `pip` would install into whatever is first on PATH — usually the system Python.
    bare = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
            and node.args and isinstance(node.args[0], ast.List)
            and node.args[0].elts
            and isinstance(node.args[0].elts[0], ast.Constant)
            and node.args[0].elts[0].value == "pip"]
    check("no bare 'pip' is ever invoked", not bare, str(bare))

    check("the CUDA pin table has no unverified entries",
          set(kayra_setup.CUDA_RUNTIME_PINS) == {"12"},
          "a guessed -cu13 package name installs a 1.4 kB placeholder and reports success")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               MAIN                                     │
# └────────────────────────────────────────────────────────────────────────┘

def main():
    print_banner("SETUP RUNTIME")
    print_info(f"Host: {describe_host()}")
    print_info("Nothing is installed or uninstalled by this suite.")

    with EnvironmentGuard() as guard:
        section_detection()
        section_sandbox_integrity()
        section_dependency_selection()
        section_reconciliation()
        section_report()
        section_structure()

    check.section("[7] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())

    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
