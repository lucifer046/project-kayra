# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_environment.py                             │
# │      Launcher, Interpreter Ownership and Runtime Provisioning          │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_environment.py — standalone diagnostic for `run.py` and `setup.py`.

    .venv\\Scripts\\python tests\\test_environment.py

The subject is the ENVIRONMENT, not the assistant: which interpreter runs Kayra, where its
modules come from, and whether `setup.py` can provision and verify the ONNX Runtime it needs.

WHY THIS SUITE EXISTS. Two failures motivated it, and neither is visible from inside the
application:

  * A `.venv` interpreter importing `kayra` from somewhere else — a global install or a stale
    `PYTHONPATH` — so edits appear to do nothing.
  * `pip install -r requirements.txt` quietly replacing `onnxruntime-gpu` with the CPU
    `onnxruntime` (kokoro-onnx depends on it), after which speech synthesis silently stops
    using the GPU and nothing in the app can tell.

Everything here is hardware-free and read-only: no package is installed, no process is
launched that outlives the check, and `setup.py` is loaded with `run_name` set so its `main()`
never executes.
"""

import io
import os
import runpy
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from kayra.utils import (print_banner, print_info, print_success, print_error, print_system,
                         print_warning)

FAILURES = []
SKIPPED = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def skip(label, why):
    SKIPPED.append(f"{label} ({why})")
    print_warning(f"SKIP  {label} — {why}")


def load(module_name):
    """Loads run.py / setup.py WITHOUT running main(). Both guard on `__name__`."""
    return runpy.run_path(os.path.join(PROJECT_ROOT, module_name), run_name="not_main")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    1. run.py OWNS THE INTERPRETER                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_launcher():
    print_system("\n[1] run.py — interpreter ownership")

    run = load("run.py")

    venv_dir = run["VENV_DIR"]
    src_dir = run["SRC_DIR"]
    check("run.py locates the project root", os.path.isdir(run["PROJECT_ROOT"]))
    check("run.py locates the source tree", os.path.isdir(src_dir))
    check("run.py knows where the venv is", venv_dir.endswith(".venv"))
    check("the venv exists", os.path.isdir(venv_dir), venv_dir)

    venv_python = run["venv_python"]()
    check("run.py can name the venv interpreter", bool(venv_python))
    check("the venv interpreter exists on disk", os.path.isfile(venv_python), venv_python)

    # THE AUTHORITATIVE TEST. `sys.prefix` is what the interpreter itself resolved; an
    # environment variable a stale shell left behind is not evidence of anything.
    check("running_inside_venv is decided by sys.prefix, not an env var",
          "sys.prefix" in io.open(os.path.join(PROJECT_ROOT, "run.py"),
                                  encoding="utf-8").read())
    check("this suite is itself running in the venv",
          run["running_inside_venv"]() is True,
          f"(sys.prefix={sys.prefix})")

    # A relaunch loop guard must exist: one relaunch is legitimate, forever is the worst form
    # of the duplicate instance the lock exists to prevent.
    source = io.open(os.path.join(PROJECT_ROOT, "run.py"), encoding="utf-8").read()
    check("a relaunch loop guard exists", "KAYRA_RELAUNCHED" in source)
    # Re-exec, never sys.path surgery. Native extensions (onnxruntime, sounddevice, numpy)
    # are ABI-bound to the interpreter that installed them, so pointing sys.path at another
    # environment's site-packages fails deep inside a native import with an unrelated message.
    # The only sys.path insertion run.py may perform is the project's own src/.
    import ast as _ast
    inserts = [n for n in _ast.walk(_ast.parse(source))
               if isinstance(n, _ast.Call)
               and isinstance(n.func, _ast.Attribute) and n.func.attr == "insert"
               and isinstance(n.func.value, _ast.Attribute)
               and n.func.value.attr == "path"]
    check("run.py re-execs rather than editing sys.path", "relaunch_in_venv" in source)
    check("the only sys.path insertion is this project's src/",
          all(isinstance(call.args[1], _ast.Name) and call.args[1].id == "SRC_DIR"
              for call in inserts if len(call.args) > 1),
          f"({len(inserts)} insertion(s))")
    check("--doctor is offered", "--doctor" in source and "print_doctor" in source)
    check("the environment is reported at startup", "report_environment" in source)


def section_system_python():
    """
    A system Python must not end up RUNNING the assistant.

    This launches the real launcher on the SYSTEM interpreter with a relaunch guard already
    set, which is the state that would otherwise loop. It must refuse and explain, not proceed
    on the wrong interpreter.
    """
    print_system("\n[2] The system interpreter cannot run the application")

    system_python = None
    for candidate in (getattr(sys, "_base_executable", None),
                      os.path.join(sys.base_prefix, "python.exe"),
                      os.path.join(sys.base_prefix, "bin", "python")):
        if candidate and os.path.isfile(candidate) and not _same(candidate, sys.executable):
            system_python = candidate
            break

    if system_python is None:
        skip("system-Python refusal", "no separate base interpreter found")
        return

    print_info(f"system interpreter: {system_python}")

    env = dict(os.environ, KAYRA_RELAUNCHED="1")
    result = subprocess.run([system_python, os.path.join(PROJECT_ROOT, "run.py"), "--doctor"],
                            capture_output=True, text=True, timeout=120, env=env,
                            cwd=PROJECT_ROOT)
    output = (result.stdout or "") + (result.stderr or "")

    check("the launcher refuses to proceed on the system interpreter",
          result.returncode != 0, f"(exit {result.returncode})")
    check("it explains rather than crashing",
          any(word in output.lower() for word in ("venv", "environment", "setup")),
          output.strip().splitlines()[-1][:90] if output.strip() else "(no output)")
    check("it does not silently import the application",
          "Active provider" not in output)


def _same(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 3. MODULES COME FROM THIS PROJECT                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_import_origin():
    print_system("\n[3] Import origin")

    import kayra
    origin = os.path.dirname(os.path.dirname(os.path.abspath(kayra.__file__)))
    expected = os.path.join(PROJECT_ROOT, "src")
    print_info(f"kayra imported from: {origin}")
    check("kayra is imported from this project's src/", _same(origin, expected),
          f"({origin})")

    # The guard that catches the opposite case at launch.
    source = io.open(os.path.join(PROJECT_ROOT, "run.py"), encoding="utf-8").read()
    check("run.py verifies where kayra was imported from",
          "kayra.__file__" in source and "not this project" in source)

    # ORT is imported in exactly ONE place in the application. Centralising the runtime layer
    # is meaningless if another module can reach around it.
    import subprocess as sp
    result = sp.run(["git", "grep", "-l", "-E", r"^\s*import onnxruntime|^\s*from onnxruntime",
                     "--", "src/"], capture_output=True, text=True, cwd=PROJECT_ROOT)
    files = [f for f in (result.stdout or "").split() if f.strip()]
    print_info(f"modules importing onnxruntime: {files or 'none found via git'}")
    check("onnxruntime is imported in exactly ONE module",
          len(files) <= 1, str(files))
    if files:
        check("that module is the device manager",
              files[0].endswith("tts_device.py"), files[0])


# ┌────────────────────────────────────────────────────────────────────────┐
# │              4. setup.py PROVISIONS AND VERIFIES                       │
# └────────────────────────────────────────────────────────────────────────┘

def section_setup():
    print_system("\n[4] setup.py — provisioning and verification")

    setup = load("setup.py")

    for name in ("configure_speech_runtime", "speech_runtime_report", "_probe_cuda_session",
                 "_ensure_ort_variant", "_ort_requirement", "_ort_report", "_nvidia_gpu_present",
                 "_installed_packages", "_pip"):
        check(f"setup.py provides {name}()", callable(setup.get(name)))

    check("exactly three ORT variants are known",
          setup["ORT_VARIANTS"] == ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"))

    # THE PACKAGE NAME / IMPORT NAME DISTINCTION.
    check("the GPU distribution is named onnxruntime-gpu",
          setup["ORT_GPU_PACKAGE"] == "onnxruntime-gpu")
    source = io.open(os.path.join(PROJECT_ROOT, "setup.py"), encoding="utf-8").read()
    check("setup.py never tries to import a module called onnxruntime_gpu",
          "import onnxruntime_gpu" not in source)
    check("setup.py imports onnxruntime by its real module name",
          "import onnxruntime as ort" in source)

    # Pins, not unbounded latest: five NVIDIA wheels at "latest" is not a combination anyone
    # has tested together, and a mismatched cuBLAS is the failure being fixed.
    pins = setup["CUDA_RUNTIME_PINS"]
    check("a known-good CUDA 12 pin set exists", "12" in pins)
    cuda12 = pins["12"]
    check("the pin set covers cuBLAS (cublasLt64_12.dll)",
          any("cublas" in p for p in cuda12), str(cuda12))
    check("the pin set covers the CUDA runtime (cudart64_12.dll)",
          any("cuda-runtime" in p for p in cuda12))
    check("the pin set covers cuFFT", any("cufft" in p for p in cuda12))
    check("the pin set covers cuDNN 9", any("cudnn" in p for p in cuda12))
    check("CUDA 12 pins are exact versions, not floating",
          all("==" in p for p in cuda12), str(cuda12))

    # pip must always be invoked as `<venv python> -m pip`, never a bare `pip`.
    check("pip is always run through the target interpreter",
          '"-m", "pip"' in source and "\n    subprocess.run([\"pip\"" not in source)

    # requirements.txt must NOT pin an ORT variant, or every install clobbers the GPU one.
    requirements = io.open(os.path.join(PROJECT_ROOT, "requirements.txt"),
                           encoding="utf-8").read()
    active = [line.split("#")[0].strip() for line in requirements.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    check("requirements.txt does not pin any onnxruntime variant",
          not any(line.lower().startswith("onnxruntime") for line in active),
          str([l for l in active if "onnx" in l.lower()]))
    check("requirements.txt explains that setup.py owns ONNX Runtime",
          "setup.py" in requirements and "onnxruntime-gpu" in requirements)

    # The repair must reinstall rather than merely uninstalling the loser: the variants share
    # one package directory, so uninstalling one deletes files the other still needs.
    check("variant repair reinstalls the keeper", "--force-reinstall" in source)

    # AND IT MUST REINSTALL AT THE PIN. An unpinned reinstall resolved to the newest
    # onnxruntime-gpu, which is built for a different CUDA major, silently orphaning the CUDA
    # runtime wheels installed moments earlier. Caught by running setup for real.
    check("the GPU variant has a version pin", "==" in setup["ORT_GPU_PIN"],
          setup["ORT_GPU_PIN"])
    check("every ORT install goes through the pinned requirement",
          setup["_ort_requirement"](setup["ORT_GPU_PACKAGE"]) == setup["ORT_GPU_PIN"])
    check("the CPU variant is not pinned to the GPU version",
          setup["_ort_requirement"](setup["ORT_CPU_PACKAGE"]) == setup["ORT_CPU_PACKAGE"])
    check("installs avoid dragging shared dependencies forward",
          "only-if-needed" in source)

    # NO SPECULATIVE PACKAGE NAMES. The obvious `-cu13` names are 1.4 kB PyPI placeholders at
    # version 0.0.1; installing them achieves nothing while looking like success.
    check("only VERIFIED CUDA pin sets are shipped", set(pins) == {"12"}, str(set(pins)))
    check("an unrecognised CUDA major is reported, never guessed at",
          "not guessing at package names" in source.lower())


def section_setup_probe():
    """The CUDA probe inside setup.py must agree with the application's own verdict."""
    print_system("\n[5] setup.py CUDA probe agrees with the runtime layer")

    setup = load("setup.py")
    from kayra.output import tts_device as td

    result = setup["_probe_cuda_session"](sys.executable)
    print_info(f"setup probe: ok={result.get('ok')} provider={result.get('provider')} "
               f"preload={result.get('preload')}")

    check("the setup probe returns a structured result",
          isinstance(result, dict) and "ok" in result and "provider" in result)
    check("the setup probe agrees with tts_device.cuda_usable()",
          bool(result.get("ok")) == td.cuda_usable(),
          f"(setup={result.get('ok')} app={td.cuda_usable()})")
    check("the probe reports the provider the SESSION got, not the one requested",
          result.get("provider") in (td.CUDA_PROVIDER, td.CPU_PROVIDER, None))

    if result.get("ok"):
        check("a successful probe names CUDA", result["provider"] == td.CUDA_PROVIDER)


# ┌────────────────────────────────────────────────────────────────────────┐
# │            6. CPU-ONLY AND GPU-CAPABLE ENVIRONMENTS                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_environment_shapes():
    """
    Both environment shapes must produce a coherent, truthful answer.

    The GPU-capable case is measured live when this machine has one. The CPU-only case is
    simulated by substituting the provider list, which is the exact input the selection logic
    reasons about — no hardware is pretended into existence.
    """
    print_system("\n[6] CPU-only and GPU-capable environments")

    from kayra.output import tts_device as td

    original = td._PROVIDERS
    td._PROVIDERS = (td.CPU_PROVIDER,)
    td.reset_probe_cache()
    try:
        check("a CPU-only environment plans CPU for AUTO",
              td.plan_providers("AUTO") == [td.CPU_PROVIDER])
        check("a CPU-only environment plans CPU for GPU",
              td.plan_providers("GPU") == [td.CPU_PROVIDER])
        auto = td.describe("AUTO")
        gpu = td.describe("GPU")
        check("AUTO on a CPU-only machine is not a fallback", auto.fallback is False)
        check("GPU on a CPU-only machine IS a fallback", gpu.fallback is True)
        check("neither claims a GPU device",
              auto.status != td.DEVICE_GPU and gpu.status != td.DEVICE_GPU)
        check("the runtime diagnostic reports CUDA as unavailable",
              td.runtime_diagnostics().cuda_available is False)
    finally:
        td._PROVIDERS = original
        td.reset_probe_cache()

    diag = td.runtime_diagnostics()
    if diag.cuda_usable:
        check("GPU-capable: CUDA is offered", diag.cuda_available is True)
        check("GPU-capable: the runtime status is OK",
              diag.cuda_runtime_status == td.RUNTIME_OK)
        check("GPU-capable: cuDNN is reported OK", diag.cudnn_status == td.RUNTIME_OK)
        check("GPU-capable: no failure reason", not diag.failure_reason)
        check("GPU-capable: AUTO plans CUDA first",
              td.plan_providers("AUTO")[0] == td.CUDA_PROVIDER)
        print_info(f"live GPU: {diag.gpu_name} / {diag.ort_package} {diag.ort_version} "
                   f"/ CUDA build {diag.ort_cuda_build}")
    else:
        skip("GPU-capable environment checks", "CUDA is not usable on this machine")


def main():
    print_banner("KAYRA ENVIRONMENT", "Launcher, interpreter ownership, runtime provisioning")
    section_launcher()
    section_system_python()
    section_import_origin()
    section_setup()
    section_setup_probe()
    section_environment_shapes()

    print_system("\n" + "=" * 60)
    if SKIPPED:
        print_warning(f"{len(SKIPPED)} check group(s) skipped: {SKIPPED}")
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All environment checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
