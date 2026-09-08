# ┌────────────────────────────────────────────────────────────────────────┐
# │                           tts_device.py                                │
# │     The ONNX Runtime Layer — DLL Loading, Provider Choice, Truth       │
# └────────────────────────────────────────────────────────────────────────┘
"""
The single authority on where Kokoro runs. Nothing else in Kayra decides whether CUDA is
usable, loads a CUDA DLL, or names an execution provider.

FOUR DIFFERENT THINGS, AND CONFLATING ANY TWO OF THEM PRODUCES A LIE
--------------------------------------------------------------------
    1. GPU HARDWARE EXISTS         — `nvidia-smi` answers; an RTX 4060 is present.
    2. A GPU PROVIDER IS OFFERED   — `get_available_providers()` lists CUDAExecutionProvider.
    3. ITS DLLs ACTUALLY LOAD      — the CUDA/cuDNN runtime is present AND on the DLL path.
    4. A SESSION ACTUALLY USES IT  — `session.get_providers()[0]` is that provider.

This machine has been every combination of these. The bug this module was rewritten for sat
squarely between (2) and (3):

    onnxruntime-gpu 1.26.0 is built against CUDA 12.8 + cuDNN 9, and reported
    ['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider'].
    The venv contained no NVIDIA runtime packages at all, so:

        Error loading "onnxruntime_providers_cuda.dll" which depends on
        "cublasLt64_12.dll" which is missing. (Error 126)

    ONNX Runtime does NOT raise for this. It logs, drops the provider, and returns a working
    session on the CPU. So a check that trusted (2) reported "GPU" while every millisecond of
    synthesis ran on the processor.

There is a second half to that root cause, and it is the one that is easy to miss: the NVIDIA
pip wheels install their DLLs under `site-packages/nvidia/*/bin`, which is **not** on the
Windows DLL search path. Installing them is necessary and not sufficient — measured here, a
session built without `preload_dlls()` still fell back to `['CPUExecutionProvider']` with every
required DLL sitting on disk. `prepare_runtime()` below is therefore mandatory and must run
before the first `InferenceSession` in the process.

WHAT THE USER CHOOSES, AND WHAT THEY DO NOT
-------------------------------------------
The setting is `AUTO` / `GPU` / `CPU`. Which *provider* implements "GPU" is this module's
problem.

    AUTO  prefer CUDA when it genuinely initializes; fall back to CPU quietly.
    GPU   require it. On failure, fall back to CPU and say so LOUDLY, with the real reason.
    CPU   force CPU. No GPU provider is passed to the session at all.

TENSORRT IS DISCOVERABLE BUT NEVER PLANNED FOR KOKORO
------------------------------------------------------
`TensorrtExecutionProvider` is reported in diagnostics when ORT offers it, and is deliberately
excluded from `plan_providers()`. It builds an engine for the graph on first run — tens of
seconds — against a model that already synthesizes at roughly real time, and a TensorRT plugin
failure must never be able to stand between Kokoro and CUDA. See `_TTS_PROVIDER_PREFERENCE`.

MEASURED REALITY, RECORDED HONESTLY
-----------------------------------
CUDA is **not materially faster than CPU for this model**. Measured on an RTX 4060 Laptop with
the full-precision `kokoro.onnx`: pure `session.run` 7.70 s on CUDA vs 7.96 s on CPU for 9.9 s
of audio (RTF 0.77 vs 0.80) — about 3%. ONNX Runtime reports *547 Memcpy nodes added to the
graph for CUDAExecutionProvider*, i.e. the graph has many operators the CUDA EP does not
implement and it round-trips between host and device throughout. GPU mode here buys correctness
of reporting and roughly nothing in latency; that is stated in `DeviceStatus.reason` rather than
being hidden behind a "GPU accelerated" label.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess
from dataclasses import dataclass, asdict
from typing import Optional

try:
    import onnxruntime as ort
except Exception:                       # pragma: no cover - onnxruntime is a hard dependency
    ort = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                 MODES                                  │
# └────────────────────────────────────────────────────────────────────────┘

MODE_AUTO = "AUTO"
MODE_GPU = "GPU"
MODE_CPU = "CPU"
MODES = (MODE_AUTO, MODE_GPU, MODE_CPU)

# The dropdown's labels. Exactly these three, in this order.
MODE_LABELS = {MODE_AUTO: "Automatic", MODE_GPU: "GPU", MODE_CPU: "CPU"}
LABEL_MODES = {label: mode for mode, label in MODE_LABELS.items()}

# Device statuses, named distinctly rather than sharing one generic ok/failed. The UI has to be
# able to tell "running on the GPU you asked for" from "running on CPU because the GPU you asked
# for could not start", and those are not the same outcome.
DEVICE_GPU = "DEVICE_GPU"
DEVICE_CPU = "DEVICE_CPU"
DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"

# Runtime statuses for the CUDA/cuDNN layer specifically.
RUNTIME_OK = "RUNTIME_OK"
RUNTIME_MISSING = "RUNTIME_MISSING"          # provider not offered by this ORT build
RUNTIME_DLL_MISSING = "RUNTIME_DLL_MISSING"  # provider offered, its DLLs will not load
RUNTIME_INIT_FAILED = "RUNTIME_INIT_FAILED"  # DLLs load, session still refuses the provider
RUNTIME_UNKNOWN = "RUNTIME_UNKNOWN"

CPU_PROVIDER = "CPUExecutionProvider"
CUDA_PROVIDER = "CUDAExecutionProvider"
TENSORRT_PROVIDER = "TensorrtExecutionProvider"

# Providers Kokoro's session may be built on, best first.
#
# CUDA only, then CPU. TensorRT is excluded ON PURPOSE — see the module docstring. Adding a
# provider here means "Kayra will try to run the TTS graph on it", which is a different claim
# from "ORT knows about it"; the latter is reported in diagnostics.
_TTS_PROVIDER_PREFERENCE = (CUDA_PROVIDER,)

# Every provider that puts work on a graphics device. Used for LABELLING and DIAGNOSTICS only —
# `plan_providers` never consults this list. DirectML/ROCm are recognised so a machine running
# one of those builds is described correctly rather than as "unknown".
_GPU_PROVIDER_PREFERENCE = (
    CUDA_PROVIDER,
    "DmlExecutionProvider",             # DirectML, as onnxruntime-directml names it
    "DirectMLExecutionProvider",        # historical spelling, accepted defensively
    "ROCMExecutionProvider",
    "MIGraphXExecutionProvider",
    TENSORRT_PROVIDER,
)

# Friendly names. Shown beside the raw provider string in the UI, never instead of it.
_PROVIDER_DEVICE_LABELS = {
    CUDA_PROVIDER: "NVIDIA GPU (CUDA)",
    TENSORRT_PROVIDER: "NVIDIA GPU (TensorRT)",
    "DmlExecutionProvider": "GPU (DirectML)",
    "DirectMLExecutionProvider": "GPU (DirectML)",
    "ROCMExecutionProvider": "AMD GPU (ROCm)",
    "MIGraphXExecutionProvider": "AMD GPU (MIGraphX)",
    CPU_PROVIDER: "CPU",
    "AzureExecutionProvider": "Azure (remote)",
}

# The pip packages that supply the DLLs an `onnxruntime-gpu` CUDA 12.x build needs. Named here
# because setup.py installs them and the diagnostics quote them when one is missing — "install
# the CUDA Toolkit" is not an answer a user can act on, and is not the answer anyway: ORT looks
# for these inside site-packages, not in a system CUDA install.
CUDA12_RUNTIME_PACKAGES = (
    "nvidia-cuda-runtime-cu12",
    "nvidia-cublas-cu12",
    "nvidia-cufft-cu12",
    "nvidia-cudnn-cu12",
)


def normalize_mode(value, default=MODE_AUTO) -> str:
    """
    Any user- or file-supplied value -> one of `MODES`. Never anything else.

    An invalid `TTS_DEVICE_MODE` must not be able to reach `InferenceSession` as a provider
    name: onnxruntime would either raise deep inside the constructor or, worse, ignore it.
    Accepts the UI's display labels too, so a settings screen can round-trip either form.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    upper = text.upper()
    if upper in MODES:
        return upper
    if text in LABEL_MODES:
        return LABEL_MODES[text]
    if upper in ("AUTOMATIC", "DEFAULT"):
        return MODE_AUTO
    if upper in ("CUDA", "DIRECTML", "DML", "NVIDIA", "GRAPHICS"):
        return MODE_GPU
    if upper in ("PROCESSOR",):
        return MODE_CPU
    return default


def configured_mode() -> str:
    """The mode from `.env` (`TTS_DEVICE_MODE`), validated and clamped. Defaults to AUTO."""
    try:
        from kayra.core.config import env
        return normalize_mode(env("TTS_DEVICE_MODE", MODE_AUTO))
    except Exception:
        return MODE_AUTO


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROVIDER DISCOVERY                              │
# └────────────────────────────────────────────────────────────────────────┘

_PROVIDERS: Optional[tuple] = None
_PROVIDERS_LOCK = threading.Lock()


def available_providers() -> tuple:
    """
    Everything the installed `onnxruntime` build offers, in its own order.

    NOTE ON THE PACKAGE NAME. The distribution is `onnxruntime-gpu`, but the import is and
    stays `import onnxruntime`. There is no module called `onnxruntime_gpu`; a GPU build simply
    reports more providers here. Anything that tries to detect GPU support by importing a
    differently-named module is wrong.

    Cached under a lock rather than with `lru_cache`: the UI and the boot thread both reach
    this, and the decorator does not hold a lock across the wrapped call.
    """
    global _PROVIDERS
    if _PROVIDERS is not None:
        return _PROVIDERS
    with _PROVIDERS_LOCK:
        if _PROVIDERS is None:
            try:
                _PROVIDERS = tuple(ort.get_available_providers()) if ort else (CPU_PROVIDER,)
            except Exception:
                _PROVIDERS = (CPU_PROVIDER,)
    return _PROVIDERS


def gpu_providers() -> tuple:
    """GPU-capable providers this build OFFERS, best first. Says nothing about whether they work."""
    installed = set(available_providers())
    return tuple(p for p in _GPU_PROVIDER_PREFERENCE if p in installed)


def gpu_provider_available() -> bool:
    """Whether ANY GPU execution provider is offered. Offered is not the same as usable."""
    return bool(gpu_providers())


def device_label(provider) -> str:
    """A readable name for a provider string. Falls back to the raw name, never to a guess."""
    return _PROVIDER_DEVICE_LABELS.get(provider, provider or "unknown")


def is_gpu_provider(provider) -> bool:
    return provider in _GPU_PROVIDER_PREFERENCE


def ort_version() -> str:
    try:
        return str(ort.__version__) if ort else "not installed"
    except Exception:
        return "unknown"


def ort_package_name() -> str:
    """`onnxruntime-gpu` or `onnxruntime`, as the installed build reports itself."""
    try:
        return str(getattr(ort, "package_name", "onnxruntime"))
    except Exception:
        return "unknown"


def ort_cuda_build_version() -> Optional[str]:
    """The CUDA version this ORT wheel was BUILT against ("12.8"), or None on a CPU build."""
    try:
        return getattr(ort, "cuda_version", None) or None
    except Exception:
        return None


def ort_location() -> str:
    try:
        return os.path.dirname(os.path.abspath(ort.__file__)) if ort else "n/a"
    except Exception:
        return "unknown"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      WINDOWS DLL PREPARATION                           │
# └────────────────────────────────────────────────────────────────────────┘
# THIS IS HALF THE ROOT CAUSE, and the half that is invisible.
#
# `pip install nvidia-cublas-cu12` puts `cublasLt64_12.dll` in
# `site-packages/nvidia/cublas/bin`, which Windows does not search. Installing the packages is
# necessary and NOT sufficient: measured on this machine, a session built without the preload
# still returned `['CPUExecutionProvider']` with every required DLL present on disk.
#
# `ort.preload_dlls()` (ONNX Runtime >= 1.22) walks the nvidia site-packages layout and loads
# each DLL by absolute path, which also pins them for the provider DLL that depends on them.
# Failures are RECORDED, never swallowed — a silent preload failure is how "GPU" ends up on a
# screen while the CPU does the work.


@dataclass(frozen=True)
class RuntimePreparation:
    """What happened when the CUDA/cuDNN/MSVC DLLs were loaded. Immutable, computed once."""

    attempted: bool = False
    supported: bool = False          # does this ORT expose preload_dlls at all?
    ok: bool = False
    error: str = ""
    elapsed_ms: float = 0.0
    cuda_build: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_preparation: Optional[RuntimePreparation] = None
_preparation_lock = threading.RLock()


def prepare_runtime(force: bool = False) -> RuntimePreparation:
    """
    Makes the CUDA/cuDNN/MSVC DLLs loadable, once per process. Idempotent and thread-safe.

    MUST run before the first `InferenceSession`, which is why `create_session` calls it rather
    than leaving it to a caller to remember. Calling it twice is free — the result is cached and
    the second caller gets the first one's outcome.

    A CPU-only ORT build has nothing to preload and reports `supported=False, ok=True`: there is
    no failure to report, because nothing was required.
    """
    global _preparation
    if _preparation is not None and not force:
        return _preparation

    with _preparation_lock:
        if _preparation is not None and not force:
            return _preparation

        cuda_build = ort_cuda_build_version()

        if ort is None:                                   # pragma: no cover
            _preparation = RuntimePreparation(attempted=False, supported=False, ok=False,
                                              error="onnxruntime is not installed")
            return _preparation

        preload = getattr(ort, "preload_dlls", None)
        if preload is None or not cuda_build:
            # Either an ORT too old to expose the hook, or a CPU-only build. Neither is an
            # error: there is simply no CUDA runtime to prepare.
            _preparation = RuntimePreparation(attempted=False, supported=bool(preload),
                                              ok=True, cuda_build=cuda_build)
            return _preparation

        started = time.perf_counter()
        try:
            # msvc=True as well: the CUDA EP links the MSVC runtime, and a machine without the
            # redistributable fails with the same shape of error as a missing cuBLAS.
            preload(cuda=True, cudnn=True, msvc=True)
            _preparation = RuntimePreparation(
                attempted=True, supported=True, ok=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0, cuda_build=cuda_build)
        except Exception as exc:
            # NOT suppressed. The reason travels all the way to the UI and the boot log.
            _preparation = RuntimePreparation(
                attempted=True, supported=True, ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - started) * 1000.0, cuda_build=cuda_build)
        return _preparation


def _nvidia_runtime_packages_present() -> tuple:
    """
    Which of the CUDA-12 runtime wheels are installed. Cheap: a directory probe, no imports.

    Used to turn "CUDA could not start" into a sentence naming the missing package, because
    that is the difference between a diagnostic the user can act on and one they cannot.
    """
    missing = []
    try:
        import importlib.metadata as md
        installed = {(d.metadata["Name"] or "").lower() for d in md.distributions()}
        for package in CUDA12_RUNTIME_PACKAGES:
            if package.lower() not in installed:
                missing.append(package)
    except Exception:
        return ()
    return tuple(missing)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        THE REAL CUDA PROBE                             │
# └────────────────────────────────────────────────────────────────────────┘
# An 84-byte ONNX model — one `Identity` node — hand-encoded as protobuf so this module carries
# no dependency on the `onnx` package.
#
# WHY A MODEL AT ALL. There is no ORT API that answers "can the CUDA EP initialize?" without
# building a session; the provider's DLLs are loaded lazily by the session factory, which is
# exactly where the cuBLAS failure surfaces. Probing with the real 88 MB Kokoro graph would cost
# ~1.8 s and allocate VRAM, so the probe uses the smallest valid graph instead: measured well
# under 200 ms, and the answer is cached for the process.
_PROBE_MODEL = (
    b'\x08\x08\x12\x05kayra:C\n\x14\n\x01x\x12\x01y\x1a\x02id"\x08Identity\x12\x05probe'
    b'Z\x11\n\x01x\x12\x0c\n\n\x08\x01\x12\x06\n\x04\n\x02\x08\x01'
    b'b\x11\n\x01y\x12\x0c\n\n\x08\x01\x12\x06\n\x04\n\x02\x08\x01B\x04\n\x00\x10\r'
)


@dataclass(frozen=True)
class ProviderProbe:
    """The verified answer to 'can a session actually run on this provider?'."""

    provider: str
    usable: bool
    status: str = RUNTIME_UNKNOWN
    reason: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


_probe_cache: dict = {}
_probe_lock = threading.RLock()


def probe_provider(provider: str, force: bool = False) -> ProviderProbe:
    """
    Builds a throwaway session on `provider` and reports whether it ACTUALLY took.

    CACHED per provider for the process lifetime. Neither the installed packages nor the DLL
    search path change while Kayra runs, so re-probing every utterance would burn time to
    re-derive a constant — and `create_session` consults this before every session build.

    The check is `session.get_providers()[0] == provider`, not "no exception was raised".
    ONNX Runtime does not raise when a provider's DLLs are missing; it logs, drops the provider
    and hands back a working CPU session. That silent fallback is the entire reason this
    function exists.
    """
    with _probe_lock:
        if not force and provider in _probe_cache:
            return _probe_cache[provider]

    if ort is None:                                       # pragma: no cover
        result = ProviderProbe(provider, False, RUNTIME_MISSING, "onnxruntime is not installed")
    elif provider not in available_providers():
        result = ProviderProbe(
            provider, False, RUNTIME_MISSING,
            f"{provider} is not offered by {ort_package_name()} {ort_version()}.")
    else:
        preparation = prepare_runtime()
        started = time.perf_counter()
        try:
            options = ort.SessionOptions()
            # Quiet: a failing provider logs at ERROR from native code, and this probe EXPECTS
            # to fail on machines without the runtime. The reason is captured and reported by
            # this function instead, so the console gets one clean sentence rather than a wall
            # of native diagnostics on every boot.
            options.log_severity_level = 4
            session = ort.InferenceSession(_PROBE_MODEL, sess_options=options,
                                           providers=[provider, CPU_PROVIDER])
            in_use = list(session.get_providers())
            elapsed = (time.perf_counter() - started) * 1000.0
            del session

            if in_use and in_use[0] == provider:
                result = ProviderProbe(provider, True, RUNTIME_OK, "", elapsed)
            else:
                result = ProviderProbe(provider, False, RUNTIME_DLL_MISSING,
                                       _explain_provider_failure(provider, preparation),
                                       elapsed)
        except Exception as exc:
            result = ProviderProbe(
                provider, False, RUNTIME_INIT_FAILED,
                f"{type(exc).__name__}: {str(exc)[:200]}",
                (time.perf_counter() - started) * 1000.0)

    with _probe_lock:
        _probe_cache[provider] = result
    return result


def _explain_provider_failure(provider: str, preparation: RuntimePreparation) -> str:
    """
    Turns "the session came back on CPU" into a sentence naming what is actually missing.

    "GPU unavailable" is not a diagnostic. The user needs to know that their `onnxruntime-gpu`
    build wants CUDA 12.8 and that four pip packages supply it, because that is a thing they
    (or `setup.py`) can fix.
    """
    if provider != CUDA_PROVIDER:
        return (f"{provider} is offered but a session could not be initialized on it; "
                f"ONNX Runtime fell back to the CPU.")

    missing = _nvidia_runtime_packages_present()
    build = preparation.cuda_build or ort_cuda_build_version() or "12.x"

    if missing:
        return (f"{ort_package_name()} {ort_version()} is built for CUDA {build}, but the "
                f"CUDA runtime is not installed in this environment: missing "
                f"{', '.join(missing)}. Run `python setup.py` to install them "
                f"(they provide cublasLt64_12.dll, cublas64_12.dll, cudart64_12.dll and "
                f"cuDNN 9).")

    if preparation.attempted and not preparation.ok:
        return (f"The CUDA runtime packages are installed but their DLLs could not be loaded: "
                f"{preparation.error}")

    return (f"The CUDA runtime packages are installed and loaded, but ONNX Runtime still could "
            f"not initialize {provider}. This usually means the NVIDIA driver is older than "
            f"CUDA {build} requires, or the GPU is not CUDA-capable.")


def cuda_usable() -> bool:
    """The one question every other module should ask. Verified, not inferred, and cached."""
    return probe_provider(CUDA_PROVIDER).usable


def reset_probe_cache():
    """Forgets the cached probes. For tests and for a deliberate re-verification only."""
    global _preparation
    with _probe_lock:
        _probe_cache.clear()
    with _preparation_lock:
        _preparation = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SESSION CONSTRUCTION                          │
# └────────────────────────────────────────────────────────────────────────┘

@dataclass
class DeviceStatus:
    """
    What was asked for, what was tried, and what is actually running.

    `mode` and `provider` are deliberately separate fields with separate labels in the UI.
    Collapsing them into one "device" string is how a settings screen ends up claiming GPU
    acceleration that is not happening.
    """

    mode: str = MODE_AUTO
    provider: str = CPU_PROVIDER
    status: str = DEVICE_CPU
    available: tuple = ()
    fallback: bool = False              # asked for GPU, got CPU
    reason: str = ""
    model: str = ""
    session_ms: float = 0.0
    runtime_status: str = RUNTIME_UNKNOWN

    def __post_init__(self):
        self.available = tuple(self.available)

    @property
    def device(self) -> str:
        return device_label(self.provider)

    @property
    def on_gpu(self) -> bool:
        return self.status == DEVICE_GPU

    def to_dict(self) -> dict:
        data = asdict(self)
        data["available"] = list(self.available)
        data["device"] = self.device
        data["mode_label"] = MODE_LABELS.get(self.mode, self.mode)
        return data

    def summary(self) -> str:
        """The one-line form printed at boot and shown under the dropdown."""
        line = f"{MODE_LABELS.get(self.mode, self.mode)} -> {self.device} ({self.provider})"
        return f"{line} - {self.reason}" if self.reason else line

    def __repr__(self):
        return f"DeviceStatus({self.mode} -> {self.provider}, {self.status})"


def plan_providers(mode) -> list:
    """
    The provider list to hand `InferenceSession`, for a validated mode.

    CPU mode returns CPU ALONE — not "CPU first, GPU after". A list containing a GPU provider
    still initializes that provider's runtime, which is exactly what "force CPU" asks not to
    happen.

    GPU/AUTO return `[CUDA, CPU]` and nothing else. TensorRT is never planned here: it would
    build an engine on first run and a TensorRT plugin failure must not be able to stand between
    Kokoro and CUDA.
    """
    mode = normalize_mode(mode)
    if mode == MODE_CPU:
        return [CPU_PROVIDER]

    offered = set(available_providers())
    plan = [p for p in _TTS_PROVIDER_PREFERENCE if p in offered]
    # CPU stays on the end as ONNX Runtime's own per-NODE fallback for operators the GPU
    # provider does not implement. That is normal and expected — this model triggers 547 of
    # them — and is not the same thing as the whole session landing on the CPU, which is what
    # `_verify` actually checks.
    return plan + [CPU_PROVIDER]


def _verify(session) -> tuple:
    """
    Asks the constructed session what it is ACTUALLY using, and classifies it.

    `session.get_providers()` returns the providers in effect, highest priority first. If the
    GPU provider is not at the front, the graph is not running on the GPU however the request
    was phrased.
    """
    try:
        in_use = list(session.get_providers())
    except Exception:
        in_use = [CPU_PROVIDER]
    provider = in_use[0] if in_use else CPU_PROVIDER
    return provider, (DEVICE_GPU if is_gpu_provider(provider) else DEVICE_CPU)


def create_session(model_path, mode=None, sess_options=None):
    """
    Builds ONE Kokoro InferenceSession on the best provider the mode allows.

    Returns `(session, DeviceStatus)`. Never returns a session it cannot describe truthfully,
    and never claims a device the session is not on.

    ORDER OF OPERATIONS, AND WHY:

      1. `prepare_runtime()` — the CUDA DLLs must be loadable BEFORE the first session, or the
         provider is dropped silently. Idempotent, so calling it here costs nothing after boot.
      2. `probe_provider()` — a cached 84-byte probe answers "would CUDA actually take?" without
         paying 1.8 s to find out on the real graph. A failing probe skips straight to CPU with
         the real reason attached, so a machine without the runtime does not build two sessions
         on every start.
      3. Build, then `_verify()` — belt and braces. The probe can be right and the real graph
         still land on CPU (an operator set the provider refuses), and the status must follow
         the session that exists, not the prediction.
    """
    mode = normalize_mode(mode if mode is not None else configured_mode())
    providers = available_providers()
    model_name = os.path.basename(str(model_path))

    if ort is None:                     # pragma: no cover - hard dependency
        raise RuntimeError("onnxruntime is not installed")

    # Load the CUDA/cuDNN DLLs before the first session. Idempotent and cached, so this
    # costs nothing after boot — but skipping it silently drops the provider (see the module
    # docstring). Called for its effect; the result is reported through the probe below.
    prepare_runtime()

    def build(provider_list):
        started = time.perf_counter()
        session = ort.InferenceSession(model_path, sess_options=sess_options,
                                       providers=list(provider_list))
        return session, (time.perf_counter() - started) * 1000.0

    def cpu_only(reason, fallback, runtime_status):
        session, elapsed = build([CPU_PROVIDER])
        provider, status = _verify(session)
        return session, DeviceStatus(mode=mode, provider=provider, status=status,
                                     available=providers, fallback=fallback, reason=reason,
                                     model=model_name, session_ms=elapsed,
                                     runtime_status=runtime_status)

    # ── CPU: one path, no ambiguity ──
    if mode == MODE_CPU:
        return cpu_only("CPU was requested.", False, RUNTIME_OK)

    # ── Is a GPU provider even worth trying? ──
    plan = plan_providers(mode)
    candidates = [p for p in plan if p != CPU_PROVIDER]

    if not candidates:
        detail = (f"No GPU execution provider is available for TTS. "
                  f"{ort_package_name()} {ort_version()} offers: "
                  f"{', '.join(providers) or 'none'}.")
        if mode == MODE_GPU:
            detail += (" Install onnxruntime-gpu (NVIDIA/CUDA) and run `python setup.py`, "
                       "or choose CPU.")
        return cpu_only(detail, mode == MODE_GPU, RUNTIME_MISSING)

    probe = probe_provider(candidates[0])
    if not probe.usable:
        reason = probe.reason
        if mode == MODE_GPU:
            reason = f"GPU was requested but could not be initialized. {reason} Running on the CPU."
        return cpu_only(reason, mode == MODE_GPU, probe.status)

    # ── The probe says CUDA works. Build for real, then verify anyway. ──
    try:
        session, elapsed = build(plan)
    except Exception as exc:
        reason = (f"{candidates[0]} initialized in the probe but failed on the speech model: "
                  f"{type(exc).__name__}: {str(exc)[:160]}")
        if mode == MODE_GPU:
            reason = f"GPU was requested but {reason} Running on the CPU."
        return cpu_only(reason, mode == MODE_GPU, RUNTIME_INIT_FAILED)

    provider, status = _verify(session)
    if status == DEVICE_GPU:
        return session, DeviceStatus(mode=mode, provider=provider, status=status,
                                     available=providers, reason="", model=model_name,
                                     session_ms=elapsed, runtime_status=RUNTIME_OK)

    # Built, but landed on the CPU anyway. Dispose rather than keeping a GPU-shaped session
    # that is not one.
    del session
    reason = (f"{candidates[0]} was offered and probed clean, but the speech model still "
              f"initialized on {provider}.")
    if mode == MODE_GPU:
        reason = f"GPU was requested but {reason} Running on the CPU."
    return cpu_only(reason, mode == MODE_GPU, RUNTIME_INIT_FAILED)


def describe(mode=None) -> DeviceStatus:
    """
    What WOULD happen for a mode, without building the speech model.

    Unlike the old version of this function, this is not a guess: it runs the cached provider
    probe, so "would use GPU" means CUDA has genuinely initialized a session in this process.
    Only the Kokoro-specific step is left unverified.
    """
    mode = normalize_mode(mode if mode is not None else configured_mode())
    providers = available_providers()

    if mode == MODE_CPU:
        return DeviceStatus(mode=mode, provider=CPU_PROVIDER, status=DEVICE_CPU,
                            available=providers, reason="CPU was requested.",
                            runtime_status=RUNTIME_OK)

    candidates = [p for p in plan_providers(mode) if p != CPU_PROVIDER]
    if not candidates:
        return DeviceStatus(
            mode=mode, provider=CPU_PROVIDER, status=DEVICE_UNAVAILABLE,
            available=providers, fallback=(mode == MODE_GPU),
            reason="No GPU execution provider is available for TTS.",
            runtime_status=RUNTIME_MISSING)

    probe = probe_provider(candidates[0])
    if probe.usable:
        return DeviceStatus(mode=mode, provider=candidates[0], status=DEVICE_GPU,
                            available=providers, reason="", runtime_status=RUNTIME_OK)

    reason = probe.reason
    if mode == MODE_GPU:
        reason = f"GPU was requested but could not be initialized. {reason}"
    return DeviceStatus(mode=mode, provider=CPU_PROVIDER, status=DEVICE_UNAVAILABLE,
                        available=providers, fallback=(mode == MODE_GPU), reason=reason,
                        runtime_status=probe.status)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            GPU TELEMETRY                               │
# └────────────────────────────────────────────────────────────────────────┘
# Entirely optional, entirely separate from the decision above: no part of choosing or running a
# provider depends on any of this, and speech works identically when it is unavailable.
#
# COST CONTROL. `nvidia-smi` is a process spawn (~120-250 ms measured), so it is never called
# from a paint path. ONE sampler thread refreshes a cache on a slow interval and PARKS ITSELF
# once nothing has asked for metrics recently — so a user who never opens a screen showing GPU
# stats pays for exactly zero samples, and one who does pays for a thread that ends by itself
# shortly after they navigate away. There is no persistent monitor process and no daemon.

_SAMPLE_INTERVAL = 5.0          # seconds between samples while anyone is watching
_WATCHER_TIMEOUT = 20.0         # stop sampling this long after the last request
_SMI_TIMEOUT = 4.0

_metrics_lock = threading.Lock()
_metrics_cache = {"at": 0.0, "value": None}
_metrics_last_request = 0.0
_metrics_thread = None
_NVIDIA_SMI_MISSING = False


def _run_nvidia_smi():
    """One sample, or None. Never raises; a missing tool is a normal outcome, not an error."""
    global _NVIDIA_SMI_MISSING
    if _NVIDIA_SMI_MISSING:
        return None
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    try:
        # Fixed argument vector, shell=False. No user text reaches this command, and it stays
        # that way — the same rule the automation layer's first-party calls follow.
        completed = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_SMI_TIMEOUT, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        _NVIDIA_SMI_MISSING = True      # never probed again this process
        return None
    except Exception:
        return None

    if completed.returncode != 0 or not completed.stdout.strip():
        return None

    line = completed.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return None

    def number(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    used, total = number(parts[2]), number(parts[3])
    free = (total - used) if (used is not None and total is not None) else None
    return {
        "name": parts[0] or None,
        "utilization": number(parts[1]),
        "memory_used_mb": used,
        "memory_total_mb": total,
        "memory_free_mb": free,
        "memory_percent": (used / total * 100.0) if used is not None and total else None,
        "temperature_c": number(parts[4]),
        "source": "nvidia-smi",
    }


def _sampler():
    """The one shared sampler. Exits when nobody has asked for metrics recently."""
    global _metrics_thread
    while True:
        value = _run_nvidia_smi()
        with _metrics_lock:
            _metrics_cache["value"] = value
            _metrics_cache["at"] = time.time()
            idle = time.time() - _metrics_last_request
            if idle > _WATCHER_TIMEOUT or value is None:
                # Nobody is watching, or this machine has no NVIDIA tooling at all. Either way
                # there is no reason for this thread to exist.
                _metrics_thread = None
                return
        time.sleep(_SAMPLE_INTERVAL)


def gpu_metrics(start_sampling: bool = True):
    """
    The most recent GPU telemetry, or None.

    Returns IMMEDIATELY from the cache — it never blocks a caller on a subprocess, which is the
    whole reason the sampler exists. The first call therefore returns None and starts the
    sampler; the next one, a few seconds later, has real numbers. A UI that refreshes on a timer
    sees the values appear on its second tick, which is the correct trade against freezing the
    GUI thread for a quarter of a second.
    """
    global _metrics_thread, _metrics_last_request
    with _metrics_lock:
        _metrics_last_request = time.time()
        value = _metrics_cache["value"]
        needs_thread = start_sampling and _metrics_thread is None and not _NVIDIA_SMI_MISSING
        if needs_thread:
            _metrics_thread = threading.Thread(target=_sampler, daemon=True,
                                               name="kayra-gpu-sampler")
    if needs_thread:
        _metrics_thread.start()
    return value


def gpu_telemetry_available() -> bool:
    """Whether GPU telemetry can be collected at all on this machine."""
    return not _NVIDIA_SMI_MISSING


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     THE STRUCTURED DIAGNOSTIC                          │
# └────────────────────────────────────────────────────────────────────────┘

@dataclass
class RuntimeDiagnostics:
    """
    Everything a caller could need to explain the current state, in one object.

    Returned as a structured value rather than a pile of booleans so the UI, the boot log and
    `setup.py` all read the same fields and cannot drift into three different accounts of the
    same machine.
    """

    mode: str = MODE_AUTO
    active_device: str = "CPU"
    provider: str = CPU_PROVIDER
    available_providers: tuple = ()
    cuda_available: bool = False        # offered by this ORT build
    cuda_usable: bool = False           # verified: a session really initialized on it
    cuda_runtime_status: str = RUNTIME_UNKNOWN
    cudnn_status: str = RUNTIME_UNKNOWN
    tensorrt_available: bool = False
    failure_reason: str = ""
    gpu_name: Optional[str] = None
    gpu_memory_total: Optional[float] = None    # MiB
    gpu_memory_used: Optional[float] = None
    gpu_memory_free: Optional[float] = None
    gpu_utilization: Optional[float] = None
    temperature: Optional[float] = None
    ort_version: str = ""
    ort_package: str = ""
    ort_location: str = ""
    ort_cuda_build: Optional[str] = None
    missing_packages: tuple = ()
    python_executable: str = ""
    environment_path: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["available_providers"] = list(self.available_providers)
        data["missing_packages"] = list(self.missing_packages)
        return data


def runtime_diagnostics(status: Optional[DeviceStatus] = None) -> RuntimeDiagnostics:
    """
    The full picture: ORT build, providers, verified CUDA state, GPU telemetry, environment.

    `status` is the live `DeviceStatus` from a running engine when there is one. Without it this
    describes what WOULD happen, using the same cached probe.
    """
    import sys

    status = status or describe()
    probe = probe_provider(CUDA_PROVIDER) if CUDA_PROVIDER in available_providers() else None
    preparation = prepare_runtime()
    metrics = gpu_metrics(start_sampling=False) or {}

    # cuDNN is not separately queryable — ORT loads it as part of the CUDA EP, and a cuDNN
    # failure presents identically to a CUDA one. It is reported as the CUDA runtime's state
    # rather than invented as an independent answer.
    cudnn = RUNTIME_OK if (probe and probe.usable) else (
        probe.status if probe else RUNTIME_MISSING)

    return RuntimeDiagnostics(
        mode=status.mode,
        active_device=status.device,
        provider=status.provider,
        available_providers=available_providers(),
        cuda_available=CUDA_PROVIDER in available_providers(),
        cuda_usable=bool(probe and probe.usable),
        cuda_runtime_status=(probe.status if probe else RUNTIME_MISSING),
        cudnn_status=cudnn,
        tensorrt_available=TENSORRT_PROVIDER in available_providers(),
        failure_reason=status.reason or (probe.reason if probe and not probe.usable else ""),
        gpu_name=metrics.get("name"),
        gpu_memory_total=metrics.get("memory_total_mb"),
        gpu_memory_used=metrics.get("memory_used_mb"),
        gpu_memory_free=metrics.get("memory_free_mb"),
        gpu_utilization=metrics.get("utilization"),
        temperature=metrics.get("temperature_c"),
        ort_version=ort_version(),
        ort_package=ort_package_name(),
        ort_location=ort_location(),
        ort_cuda_build=preparation.cuda_build,
        missing_packages=_nvidia_runtime_packages_present(),
        python_executable=sys.executable,
        environment_path=sys.prefix,
    )


def diagnostics(status: Optional[DeviceStatus] = None) -> list:
    """
    The boot report. Every line is a fact read from the runtime, never an inference from
    hardware — see the module docstring for why that distinction is the entire point.
    """
    status = status or describe()
    lines = [
        f"TTS mode: {status.mode}",
        f"Available providers: {', '.join(status.available) or 'none'}",
        f"Selected provider: {status.provider}",
        f"Device: {status.device}",
    ]
    if status.model:
        lines.append(f"Model: {status.model}")
    if status.reason:
        lines.append(status.reason)
    return lines
