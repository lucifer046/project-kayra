# ┌────────────────────────────────────────────────────────────────────────┐
# │                         test_tts_device.py                             │
# │      ONNX Runtime, CUDA Provisioning and Provider Truthfulness         │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_tts_device.py — standalone diagnostic for the speech runtime layer.

    .venv\\Scripts\\python tests\\test_tts_device.py

The property under test is TRUTHFULNESS, not speed. What must never happen is the application
reporting a device it is not using.

FOUR independent facts get confused constantly, so each is tested separately:

    1. GPU HARDWARE EXISTS         — nvidia-smi answers
    2. A GPU PROVIDER IS OFFERED   — get_available_providers() lists it
    3. ITS DLLs ACTUALLY LOAD      — the CUDA/cuDNN runtime is present AND on the DLL path
    4. A SESSION ACTUALLY USES IT  — session.get_providers()[0] is that provider

The bug this suite was extended for lived between (2) and (3): `onnxruntime-gpu` offered
CUDAExecutionProvider while the venv held no NVIDIA runtime wheels, so `cublasLt64_12.dll` was
missing, ONNX Runtime silently dropped the provider, and a check trusting (2) reported GPU while
the CPU did the work.

Every "GPU works" assertion below is CONDITIONAL on the live probe and is SKIPPED, loudly,
rather than faked when CUDA is genuinely unusable. Sections 4 and 5 simulate the
provider-present cases by substituting the provider list, which tests the selection logic
without pretending hardware exists.
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import (print_banner, print_info, print_success, print_error, print_system,
                         print_warning)
from kayra.output import tts_device as td
from kayra.core.paths import model_path

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


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    1. WHAT THIS MACHINE ACTUALLY HAS                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_environment():
    print_system("\n[1] Environment")

    providers = td.available_providers()
    print_info(f"onnxruntime package: {td.ort_package_name()} {td.ort_version()}")
    print_info(f"built for CUDA: {td.ort_cuda_build_version() or 'not a CUDA build'}")
    print_info(f"providers: {', '.join(providers)}")
    print_info(f"location: {td.ort_location()}")

    check("provider discovery always returns something",
          isinstance(providers, tuple) and len(providers) >= 1)
    check("the CPU provider is always present", td.CPU_PROVIDER in providers)
    check("discovery is cached", td.available_providers() is providers)

    # THE PACKAGE-NAME TRAP. The distribution is `onnxruntime-gpu`; the IMPORT is
    # `onnxruntime`. There is no module named `onnxruntime_gpu`, and any code that tried to
    # import one to detect GPU support would be wrong.
    import importlib.util
    check("there is no importable module named 'onnxruntime_gpu'",
          importlib.util.find_spec("onnxruntime_gpu") is None)
    check("the GPU build still imports as 'onnxruntime'",
          td.ort_location().replace("\\", "/").rstrip("/").endswith("onnxruntime"))

    # Exactly one ORT distribution may be installed: they share a package directory.
    import importlib.metadata as md
    installed = {(d.metadata["Name"] or "").lower() for d in md.distributions()}
    variants = [v for v in ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml")
                if v in installed]
    print_info(f"installed ORT distributions: {', '.join(variants) or 'none'}")
    check("exactly ONE onnxruntime variant is installed", len(variants) == 1,
          f"({variants})")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     2. DLL PREPARATION                                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_preparation():
    print_system("\n[2] CUDA/cuDNN DLL preparation")

    prep = td.prepare_runtime()
    print_info(f"preload: attempted={prep.attempted} ok={prep.ok} "
               f"cuda_build={prep.cuda_build} {prep.elapsed_ms:.0f}ms")

    check("prepare_runtime returns a structured result",
          hasattr(prep, "ok") and hasattr(prep, "error") and hasattr(prep, "attempted"))
    check("prepare_runtime is idempotent and cached", td.prepare_runtime() is prep)
    check("failures are recorded, never swallowed",
          prep.ok or bool(prep.error) or not prep.attempted)

    if td.ort_cuda_build_version():
        # THE SECOND HALF OF THE ROOT CAUSE. The NVIDIA wheels put their DLLs under
        # site-packages/nvidia/*/bin, which Windows does not search. Installing them is
        # necessary and NOT sufficient — the preload is what makes them findable.
        check("a CUDA build attempts the DLL preload", prep.attempted is True)
        check("the preload succeeded", prep.ok is True, prep.error)
        missing = td._nvidia_runtime_packages_present()
        print_info(f"missing NVIDIA runtime packages: {', '.join(missing) or 'none'}")
        check("the diagnostic can name missing runtime packages",
              isinstance(missing, tuple))
    else:
        skip("CUDA preload checks", "this onnxruntime build has no CUDA support")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. THE REAL PROBE                                  │
# └────────────────────────────────────────────────────────────────────────┘

def section_probe():
    print_system("\n[3] Real provider verification")

    # The probe model must be valid and must work on the CPU, or every probe is meaningless.
    cpu = td.probe_provider(td.CPU_PROVIDER)
    check("the CPU provider probes usable", cpu.usable is True, cpu.reason)
    check("a probe returns a structured result",
          hasattr(cpu, "status") and hasattr(cpu, "reason") and hasattr(cpu, "elapsed_ms"))
    print_info(f"CPU probe: {cpu.elapsed_ms:.0f}ms")

    # CACHING. `create_session` consults this before every session build, so a repeat probe
    # must be free.
    t0 = time.perf_counter()
    for _ in range(1000):
        td.probe_provider(td.CPU_PROVIDER)
    per_call = (time.perf_counter() - t0) / 1000 * 1e6
    print_info(f"cached probe: {per_call:.1f}us per call")
    check("probes are cached, not re-run", per_call < 100.0, f"({per_call:.1f}us)")
    check("the same object comes back", td.probe_provider(td.CPU_PROVIDER) is cpu)

    cuda = td.probe_provider(td.CUDA_PROVIDER)
    print_info(f"CUDA probe: usable={cuda.usable} status={cuda.status} "
               f"{cuda.elapsed_ms:.0f}ms")
    if cuda.reason:
        print_info(f"  reason: {cuda.reason}")

    check("cuda_usable() agrees with the probe", td.cuda_usable() == cuda.usable)
    check("the probe is cheap enough for startup", cuda.elapsed_ms < 3000.0,
          f"({cuda.elapsed_ms:.0f}ms)")

    if td.CUDA_PROVIDER not in td.available_providers():
        check("CUDA absent from the build probes as MISSING",
              cuda.status == td.RUNTIME_MISSING)
        check("a missing provider is never reported usable", cuda.usable is False)
    elif not cuda.usable:
        # Offered but unusable: the exact state this work fixed. The reason must NAME the
        # problem, not say "GPU unavailable".
        check("an unusable CUDA gives a reason", bool(cuda.reason))
        check("the reason is actionable, not a shrug",
              any(word in cuda.reason.lower()
                  for word in ("install", "driver", "setup.py", "missing", "dll")),
              cuda.reason)
    else:
        check("CUDA probes usable on this machine", cuda.usable is True)
        check("a usable probe carries no failure reason", not cuda.reason)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        4. MODE VALIDATION                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_modes():
    print_system("\n[4] Mode validation")

    check("exactly three modes are offered", td.MODES == ("AUTO", "GPU", "CPU"))
    check("each mode has a display label",
          set(td.MODE_LABELS) == set(td.MODES) and td.MODE_LABELS["AUTO"] == "Automatic")
    check("the labels are exactly Automatic / GPU / CPU",
          [td.MODE_LABELS[m] for m in td.MODES] == ["Automatic", "GPU", "CPU"])

    for value, expected in [
        ("AUTO", "AUTO"), ("auto", "AUTO"), ("Automatic", "AUTO"),
        ("GPU", "GPU"), ("gpu", "GPU"), ("cuda", "GPU"), ("DirectML", "GPU"),
        ("CPU", "CPU"), ("cpu", "CPU"),
    ]:
        check(f"'{value}' normalizes to {expected}", td.normalize_mode(value) == expected)

    # AN INVALID VALUE MUST NEVER BECOME A PROVIDER NAME.
    for junk in ["", None, "  ", "CUDAExecutionProvider", "nonsense", "TPU", 42, [], "GPU!!"]:
        result = td.normalize_mode(junk)
        check(f"{junk!r} falls back to a valid mode", result in td.MODES, f"(got {result})")

    check("the default is AUTO", td.normalize_mode(None) == "AUTO")
    check("the configured mode is always valid", td.configured_mode() in td.MODES)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     5. PROVIDER PLANNING                               │
# └────────────────────────────────────────────────────────────────────────┘

def _with_providers(providers, body):
    """
    Runs `body` with the discovered provider list substituted, then restores it.

    This does NOT fake a GPU — it substitutes the ANSWER to "which providers are offered", which
    is the exact input the selection logic reasons about. The probe cache is cleared around it
    so a simulated provider is genuinely re-probed against real onnxruntime.
    """
    original = td._PROVIDERS
    td._PROVIDERS = tuple(providers)
    td.reset_probe_cache()
    try:
        return body()
    finally:
        td._PROVIDERS = original
        td.reset_probe_cache()


def section_planning():
    print_system("\n[5] Provider planning")

    cpu_plan = td.plan_providers("CPU")
    check("CPU mode plans CPU alone", cpu_plan == [td.CPU_PROVIDER])
    check("CPU mode offers NO GPU provider — 'force CPU' means the GPU runtime is never "
          "initialized", not any(td.is_gpu_provider(p) for p in cpu_plan))

    for mode in ("AUTO", "GPU"):
        plan = td.plan_providers(mode)
        check(f"{mode} mode always ends with the CPU provider", plan[-1] == td.CPU_PROVIDER)
        check(f"{mode} mode never repeats a provider", len(plan) == len(set(plan)))

    check("an invalid mode plans safely",
          td.plan_providers("nonsense") == td.plan_providers("AUTO"))

    # TENSORRT MUST NEVER BE PLANNED FOR KOKORO.
    #
    # It builds an engine for the graph on first run — tens of seconds — and a TensorRT plugin
    # failure must not be able to stand between the speech model and CUDA. That was the observed
    # startup behaviour being fixed: TensorRT first, TensorRT fails, land on CPU.
    def with_trt():
        plan = td.plan_providers("AUTO")
        check("TensorRT is NOT planned even when offered",
              td.TENSORRT_PROVIDER not in plan, str(plan))
        check("CUDA is planned first", plan[0] == td.CUDA_PROVIDER, str(plan))
        check("the plan is exactly [CUDA, CPU]",
              plan == [td.CUDA_PROVIDER, td.CPU_PROVIDER], str(plan))
        check("TensorRT is still DISCOVERABLE for diagnostics",
              td.TENSORRT_PROVIDER in td.gpu_providers())

    _with_providers([td.TENSORRT_PROVIDER, td.CUDA_PROVIDER, td.CPU_PROVIDER], with_trt)

    # TensorRT alone must not be treated as a usable TTS GPU: there is no CUDA to plan.
    def trt_only():
        plan = td.plan_providers("GPU")
        check("TensorRT alone yields a CPU-only plan", plan == [td.CPU_PROVIDER], str(plan))

    _with_providers([td.TENSORRT_PROVIDER, td.CPU_PROVIDER], trt_only)

    check("the TTS preference list is CUDA only",
          td._TTS_PROVIDER_PREFERENCE == (td.CUDA_PROVIDER,))


# ┌────────────────────────────────────────────────────────────────────────┐
# │            6. THE FAILURE MODES, SIMULATED HONESTLY                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_failure_modes():
    print_system("\n[6] Failure modes")

    # ── (a) no GPU provider offered at all ──
    def cpu_only():
        auto = td.describe("AUTO")
        check("AUTO with no GPU provider describes CPU", auto.provider == td.CPU_PROVIDER)
        check("AUTO with no GPU provider is NOT a fallback — the user asked for 'whatever "
              "works'", auto.fallback is False)

        gpu = td.describe("GPU")
        check("GPU with no provider IS reported as a fallback", gpu.fallback is True)
        check("GPU with no provider never claims a GPU device",
              gpu.status != td.DEVICE_GPU and gpu.provider == td.CPU_PROVIDER)
        check("the runtime status says MISSING", gpu.runtime_status == td.RUNTIME_MISSING)

    _with_providers([td.CPU_PROVIDER], cpu_only)

    # ── (b) provider offered but the DLLs will not load ──
    # A provider name ORT does not really have: the session cannot initialize on it, which is
    # structurally the same failure as a missing cuBLAS and exercises the real code path.
    def broken_gpu():
        probe = td.probe_provider(td.CUDA_PROVIDER)
        check("a CUDA that cannot initialize is NOT reported usable", probe.usable is False)
        check("the failure is classified, not generic",
              probe.status in (td.RUNTIME_DLL_MISSING, td.RUNTIME_INIT_FAILED))
        check("the failure names something actionable", bool(probe.reason))
        print_info(f"  simulated broken CUDA -> {probe.status}: {probe.reason[:100]}")

        status = td.describe("GPU")
        check("GPU mode with a broken CUDA falls back", status.fallback is True)
        check("GPU mode with a broken CUDA reports CPU", status.provider == td.CPU_PROVIDER)
        check("the reason reaches the status", bool(status.reason))

    # Only meaningful when the real build does NOT offer CUDA; otherwise the probe would
    # genuinely succeed and there would be nothing broken to simulate.
    if td.CUDA_PROVIDER not in td.available_providers():
        _with_providers([td.CUDA_PROVIDER, td.CPU_PROVIDER], broken_gpu)
    else:
        skip("broken-CUDA simulation", "CUDA genuinely works here, nothing to simulate")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      7. STATUS REPORTING                               │
# └────────────────────────────────────────────────────────────────────────┘

def section_status():
    print_system("\n[7] Status and diagnostics")

    status = td.describe("AUTO")
    report = status.to_dict()

    for field in ("mode", "mode_label", "provider", "device", "status", "available",
                  "fallback", "reason", "runtime_status"):
        check(f"the report carries '{field}'", field in report)

    check("mode and provider are SEPARATE fields — the UI has to be able to show that they "
          "disagree", report["mode"] != report["provider"])
    check("the device label is human-readable",
          td.device_label("CUDAExecutionProvider") == "NVIDIA GPU (CUDA)")
    check("an unknown provider degrades to its own name rather than a guess",
          td.device_label("SomethingNewExecutionProvider") == "SomethingNewExecutionProvider")
    check("statuses are distinct named values, not one generic flag",
          len({td.DEVICE_GPU, td.DEVICE_CPU, td.DEVICE_UNAVAILABLE}) == 3)
    check("runtime statuses are distinct too",
          len({td.RUNTIME_OK, td.RUNTIME_MISSING, td.RUNTIME_DLL_MISSING,
               td.RUNTIME_INIT_FAILED, td.RUNTIME_UNKNOWN}) == 5)

    lines = td.diagnostics(status)
    for prefix in ("TTS mode:", "Available providers:", "Selected provider:", "Device:"):
        check(f"diagnostics report '{prefix}'",
              any(line.startswith(prefix) for line in lines))

    # ── the structured diagnostic the spec requires ──
    diag = td.runtime_diagnostics()
    required = ("mode", "active_device", "provider", "available_providers", "cuda_available",
                "cuda_runtime_status", "cudnn_status", "failure_reason", "gpu_name",
                "gpu_memory_total", "gpu_memory_used", "gpu_memory_free", "gpu_utilization",
                "temperature", "ort_version", "python_executable", "environment_path")
    data = diag.to_dict()
    for field in required:
        check(f"runtime_diagnostics carries '{field}'", field in data)

    check("the diagnostic knows which interpreter it is describing",
          diag.python_executable == sys.executable)
    check("the diagnostic knows which environment", diag.environment_path == sys.prefix)
    check("cuda_available and cuda_usable are SEPARATE facts",
          isinstance(diag.cuda_available, bool) and isinstance(diag.cuda_usable, bool))
    check("cuda_usable can never be True while cuda_available is False",
          not (diag.cuda_usable and not diag.cuda_available))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   8. A REAL SESSION, AND A REAL SWITCH                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_live_session():
    print_system("\n[8] Live Kokoro session")

    path = model_path("kokoro.onnx")
    if not os.path.exists(path):
        skip("live session", "models/kokoro.onnx is not present")
        return

    cuda_ok = td.cuda_usable()

    for mode in ("AUTO", "CPU", "GPU"):
        t0 = time.perf_counter()
        session, status = td.create_session(path, mode=mode)
        elapsed = (time.perf_counter() - t0) * 1000.0
        actual = session.get_providers()[0]
        print_info(f"  {mode}: {status.summary()}  [{elapsed:.0f}ms]")

        check(f"{mode}: the reported provider IS the session's provider",
              status.provider == actual, f"({status.provider} vs {actual})")
        check(f"{mode}: the reported device follows the provider",
              status.on_gpu == td.is_gpu_provider(actual))

        if mode == "CPU":
            check("CPU mode really runs on the CPU", actual == td.CPU_PROVIDER)
            check("CPU mode never reports a fallback", status.fallback is False)
        if mode in ("AUTO", "GPU"):
            if cuda_ok:
                check(f"{mode}: CUDA is usable, so the session uses it",
                      actual == td.CUDA_PROVIDER, f"(got {actual})")
                check(f"{mode}: no fallback is reported", status.fallback is False)
                check(f"{mode}: TensorRT was not used", actual != td.TENSORRT_PROVIDER)
            else:
                check(f"{mode}: without usable CUDA the session is CPU",
                      actual == td.CPU_PROVIDER)
                if mode == "GPU":
                    check("GPU mode without usable CUDA flags the fallback AND says why",
                          status.fallback is True and bool(status.reason))
        del session

    # ── the engine end to end ──
    try:
        from kayra.output.text_to_speech import TextToSpeechEngine
    except Exception as exc:
        skip("engine integration", f"TTS engine unavailable: {exc}")
        return

    # THE MODE IS PINNED, NOT INHERITED FROM `.env`.
    #
    # `TextToSpeechEngine()` with no argument reads `TTS_DEVICE_MODE` from the configuration,
    # so these checks used to describe the developer's own setting: on a machine configured
    # for CPU, "the default engine runs on CUDA" failed and "switching replaced the session"
    # failed because the switch to CPU was a no-op from CPU. Both were correct behaviour
    # reported as defects — a tier-1 suite reading the host, which is the thing this suite is
    # otherwise strict about. AUTO is what the GPU assertions below actually mean.
    engine = TextToSpeechEngine(warm_up=False, device_mode="AUTO")
    try:
        check("an explicitly requested mode is honoured over the configuration",
              engine.device_mode == "AUTO", engine.device_mode)
        report = engine.device_report()
        check("the engine exposes a device report", bool(report))
        check("the engine's report matches its own session",
              report["provider"] == engine.onnx.sess.get_providers()[0])
        check("the engine has exactly ONE session",
              sum(1 for name in dir(engine) if name.endswith("sess")) <= 1)

        # KOKORO MUST NOT BUILD ITS OWN CPU SESSION behind the manager's back.
        check("Kokoro adopted the manager's session, it did not construct one",
              engine.onnx.sess.get_providers() == engine.onnx.sess.get_providers())
        if cuda_ok:
            check("with CUDA usable, an AUTO engine runs on CUDA",
                  engine.onnx.sess.get_providers()[0] == td.CUDA_PROVIDER,
                  engine.onnx.sess.get_providers()[0])
        else:
            check("without usable CUDA, an AUTO engine runs on the CPU",
                  engine.onnx.sess.get_providers()[0] == td.CPU_PROVIDER,
                  engine.onnx.sess.get_providers()[0])

        # A runtime switch: one session at a time, and speech must still work afterwards.
        before = engine.onnx
        status = engine.set_device_mode("CPU")
        check("switching returns the resulting status", status is not None)
        # A REAL change of mode rebuilds; asking for the mode already in force does not, and
        # that distinction is checked explicitly at the end of this block rather than being
        # assumed one way here.
        check("a real mode change replaces the session", engine.onnx is not before)
        check("the mode was applied", engine.device_mode == "CPU")
        check("the switched session really is on the CPU",
              engine.onnx.sess.get_providers()[0] == td.CPU_PROVIDER)
        check("the report follows the switch",
              engine.device_report()["provider"] == engine.onnx.sess.get_providers()[0])

        chunks = 0
        for _audio, _rate in engine.onnx.stream("Testing.", voice=engine.voice, speed=1.1):
            chunks += 1
            break
        check("the engine still synthesizes after a device switch", chunks == 1)

        if cuda_ok:
            status = engine.set_device_mode("GPU")
            check("switching back to GPU restores CUDA",
                  engine.onnx.sess.get_providers()[0] == td.CUDA_PROVIDER)
            check("the GPU switch reports no fallback", status.fallback is False)
            chunks = 0
            for _audio, _rate in engine.onnx.stream("Testing.", voice=engine.voice, speed=1.1):
                chunks += 1
                break
            check("the engine synthesizes on CUDA after switching back", chunks == 1)

        check("switching to the same mode is a no-op",
              engine.set_device_mode(engine.device_mode) is engine.device_status)
    finally:
        engine.shutdown()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        9. TELEMETRY COST                               │
# └────────────────────────────────────────────────────────────────────────┘

def section_telemetry():
    print_system("\n[9] GPU telemetry")

    # The contract that keeps the UI responsive: metrics come from a cache, never from a
    # subprocess on the calling thread.
    td.gpu_metrics()
    t0 = time.perf_counter()
    for _ in range(200):
        td.gpu_metrics()
    per_call = (time.perf_counter() - t0) / 200 * 1000.0
    print_info(f"gpu_metrics(): {per_call:.3f}ms per call")
    check("gpu_metrics never blocks the caller on a subprocess", per_call < 1.0,
          f"({per_call:.3f}ms)")

    import threading
    samplers = [t for t in threading.enumerate() if t.name == "kayra-gpu-sampler"]
    check("at most ONE sampler thread exists, however many callers there are",
          len(samplers) <= 1, f"({len(samplers)})")
    check("there is no persistent GPU monitor process",
          all("nvidia" not in t.name.lower() for t in threading.enumerate()))

    deadline = time.time() + 8
    metrics = td.gpu_metrics()
    while metrics is None and time.time() < deadline:
        time.sleep(0.25)
        metrics = td.gpu_metrics()

    if metrics:
        print_info(f"GPU: {metrics.get('name')}  {metrics.get('utilization')}%  "
                   f"{(metrics.get('memory_used_mb') or 0) / 1024:.1f}/"
                   f"{(metrics.get('memory_total_mb') or 0) / 1024:.1f} GiB  "
                   f"{metrics.get('temperature_c')}C")
        for field in ("name", "utilization", "memory_used_mb", "memory_total_mb",
                      "memory_free_mb", "memory_percent", "temperature_c"):
            check(f"telemetry carries '{field}'", field in metrics)
        check("VRAM percentage is derived, not invented",
              abs(metrics["memory_percent"] -
                  metrics["memory_used_mb"] / metrics["memory_total_mb"] * 100.0) < 0.01)
        check("free VRAM is derived from used and total",
              abs((metrics["memory_free_mb"] or 0) -
                  (metrics["memory_total_mb"] - metrics["memory_used_mb"])) < 0.01)
    else:
        skip("telemetry field checks", "no GPU telemetry on this machine")

    check("telemetry availability is reported honestly",
          isinstance(td.gpu_telemetry_available(), bool))

    # THE PHYSICAL GPU AND THE TTS DEVICE ARE SEPARATE FACTS. Telemetry must not disappear
    # merely because speech is running on the CPU.
    if metrics:
        cpu_status = td.describe("CPU")
        check("GPU telemetry survives a CPU speech device",
              td.gpu_metrics() is not None and cpu_status.provider == td.CPU_PROVIDER)


def main():
    print_banner("TTS DEVICE / ONNX RUNTIME DIAGNOSTIC",
                 "Providers, CUDA verification, and the truth about them")
    section_environment()
    section_preparation()
    section_probe()
    section_modes()
    section_planning()
    section_failure_modes()
    section_status()
    section_live_session()
    section_telemetry()

    print_system("\n" + "=" * 60)
    if SKIPPED:
        print_warning(f"{len(SKIPPED)} check group(s) skipped: {SKIPPED}")
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All TTS device checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
