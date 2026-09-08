#!/usr/bin/env python
# ┌────────────────────────────────────────────────────────────────────────┐
# │                              run.py                                    │
# │                   Kayra Launcher (use this every time)                 │
# └────────────────────────────────────────────────────────────────────────┘
"""
Starts Kayra. The user never has to activate the virtual environment by hand.

    python setup.py          # once, to prepare the machine
    python run.py            # every time after that

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is a LAUNCHER. It contains no application logic: it locates the project virtual
environment, re-executes itself with that interpreter, runs a handful of cheap checks and
then calls `kayra.app.main()`. Every decision about how the assistant actually behaves —
model routing, boot ordering, shutdown — belongs to the application and stays there.

The split matters because the two jobs fail differently. `setup.py` failing means the machine
is not prepared, and the fix is to install something. `run.py` failing means the prepared
machine could not be started, and the fix is almost always "run setup again". Keeping them in
one script would make it impossible to say which of the two the user is looking at.

TWO PHASES, ONE FILE
--------------------
    phase 1 (system interpreter) : find .venv, validate it, re-exec -> phase 2
    phase 2 (.venv interpreter)  : preflight, take the single-instance lock, run the app

Phase 1 imports nothing but the standard library, because at that point the project's
dependencies are — by definition — not importable. The two phases are told apart by
`sys.prefix`, not by an environment variable a stale shell could have left behind.

WHY RE-EXEC RATHER THAN JUST ADDING PATHS
-----------------------------------------
Kayra depends on compiled extensions (onnxruntime, sounddevice, numpy, psutil, mediapipe).
Those are bound to the interpreter that installed them; pointing `sys.path` at another
environment's `site-packages` from a different Python loads the wrong ABI and fails deep
inside a native import with a message that has nothing to do with the real cause. Running the
right interpreter is the only reliable answer.

WHY THERE IS A SINGLE-INSTANCE LOCK
-----------------------------------
A second Kayra is not merely wasteful, it is broken: two processes both start a headless
Chrome that claims the microphone, both open an audio output stream, and both answer the same
spoken sentence. The lock is advisory, PID-checked (a stale lock from a crashed run is
reclaimed automatically rather than requiring the user to delete a file), and can be
overridden with `--force` for the rare case of deliberately running two configurations.
"""

import os
import sys
import json
import signal
import subprocess

# ── Layout ────────────────────────────────────────────────────────────────
# Computed from this file's own location so the launcher works from any working directory.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(PROJECT_ROOT, ".venv")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
LOCK_FILE = os.path.join(PROJECT_ROOT, "data", "kayra.lock")

IS_WINDOWS = sys.platform.startswith("win")


# ── Console encoding ──────────────────────────────────────────────────────
# This script runs on the SYSTEM interpreter, which on Windows still means a console using a
# legacy code page (cp1252) more often than not. Printing a "✗" there raises
# UnicodeEncodeError — and it did, in the single most important path this file has: a user
# with no virtual environment got a traceback instead of the "run setup.py" instructions.
#
# Two defences, because either alone is insufficient. Reconfiguring to UTF-8 fixes modern
# consoles but is unavailable on some redirected streams; probing what the stream can actually
# encode then picks ASCII substitutes for the rest. A launcher must never fail while trying to
# report a failure.
def _enable_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_enable_utf8()


def _can_encode(sample):
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError, TypeError):
        return False


_UNICODE = _can_encode("·✗")
MARK_INFO = "·" if _UNICODE else "-"
MARK_FAIL = "✗" if _UNICODE else "x"

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(text, code):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg):
    print(_paint(f"  {MARK_INFO}  ", "36") + msg)


def fail(msg):
    print(_paint(f"  {MARK_FAIL}  ", "1;31") + msg)


def hint(msg):
    print(_paint("     " + msg, "33"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    PHASE 1 — LOCATE THE ENVIRONMENT                    │
# └────────────────────────────────────────────────────────────────────────┘

def venv_python(venv_dir=VENV_DIR):
    """The interpreter inside the project virtual environment, per platform."""
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def running_inside_venv():
    """
    True when the CURRENT interpreter is the project virtual environment's.

    `sys.prefix` is the authoritative answer: it is what the interpreter itself resolved at
    startup. An environment variable such as VIRTUAL_ENV only reflects what some shell exported
    and is routinely stale or absent (the user does not activate anything — that is the point
    of this launcher).
    """
    return _same_path(sys.prefix, VENV_DIR)


def setup_required(reason):
    """Explains that the environment is not ready, and how to fix it. Never fixes it itself."""
    fail(reason)
    hint("Kayra's environment is not ready. Prepare it with:")
    hint("")
    hint("    python setup.py")
    hint("")
    hint("setup.py creates .venv, installs dependencies and configures .env.")
    return 1


def relaunch_in_venv(argv):
    """
    Re-executes this script with the virtual environment's interpreter and returns its exit code.

    A child process is used rather than `os.execv` because `execv` on Windows does not replace
    the process the way it does on POSIX — it spawns a new one and lets the original exit, so
    the console returns to the prompt while Kayra is still running and Ctrl+C no longer reaches
    it. Keeping the parent alive as a thin wrapper preserves both the exit code and the console
    signal path.
    """
    python = venv_python()

    if not os.path.isdir(VENV_DIR):
        return setup_required("No virtual environment found at .venv")

    if not os.path.isfile(python):
        return setup_required(f"The virtual environment is incomplete — {python} is missing")

    # Loop guard. If the child re-executes and STILL does not recognise itself as running
    # inside the environment, re-launching again would fork Kayra forever — the worst possible
    # version of the duplicate-instance problem this file exists to prevent. One relaunch is
    # legitimate; a second means the detection disagrees with reality, and the honest response
    # is to stop and say so rather than to keep trying.
    if os.environ.get("KAYRA_RELAUNCHED") == "1":
        fail("Relaunched into .venv but the interpreter still does not report that environment.")
        hint(f"Expected prefix: {VENV_DIR}")
        hint(f"Interpreter reports: {sys.prefix}")
        hint("The virtual environment is probably damaged. Recreate it with: python setup.py")
        return 1

    info(f"Starting Kayra with {os.path.relpath(python, PROJECT_ROOT)}")

    child_env = dict(os.environ, KAYRA_RELAUNCHED="1")

    # Ctrl+C goes to the whole console process group, so BOTH this wrapper and the real
    # assistant receive it. The assistant installs handlers for SIGINT/SIGTERM/SIGBREAK and
    # runs a real shutdown (releasing the microphone, killing the Chrome processes it owns).
    # This wrapper must therefore ignore the signal and keep waiting: if it dies first, the
    # console returns to the prompt while Kayra is still tearing down, and the wrapper's own
    # exit status replaces the assistant's. Measured before this: Ctrl+C reported
    # 0xC000013A (STATUS_CONTROL_C_EXIT) regardless of how cleanly the child had exited.
    previous = {}
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

    try:
        completed = subprocess.run([python, os.path.abspath(__file__)] + argv,
                                   cwd=PROJECT_ROOT, env=child_env)
    except OSError as e:
        return setup_required(f"Could not start the virtual environment interpreter: {e}")
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    return completed.returncode


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      PHASE 2 — PREFLIGHT CHECKS                        │
# └────────────────────────────────────────────────────────────────────────┘
# Cheap and structural only. Nothing here loads a model, opens the microphone, starts a
# browser or constructs an API client — that is `bootstrap()`'s job, it costs seconds, and
# duplicating any part of it here would mean paying for it twice on every start.

def preflight():
    """Verifies the things whose absence would produce a confusing failure later."""
    problems = []

    if not os.path.isdir(SRC_DIR):
        problems.append(f"The source tree is missing: {SRC_DIR}")

    # Make the package importable when running from a source checkout. An installed copy
    # (`pip install -e .`) already resolves without this, and inserting a path that is already
    # effective is harmless.
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)

    try:
        import kayra
    except Exception as e:
        problems.append(f"The kayra package could not be imported: {e}")
    else:
        # WHERE DID `kayra` ACTUALLY COME FROM?
        #
        # `running_inside_venv()` proves the INTERPRETER is right; it does not prove the
        # MODULES are. A global `pip install kayra`, a stale `PYTHONPATH`, or a sibling
        # checkout earlier on `sys.path` all produce an interpreter from .venv importing code
        # from somewhere else entirely — and the symptom is edits that appear to do nothing.
        # Half a millisecond to check beats an afternoon of that.
        origin = os.path.dirname(os.path.dirname(os.path.abspath(kayra.__file__)))
        if not _same_path(origin, SRC_DIR):
            problems.append(
                f"The kayra package is being imported from {origin}, not this project's "
                f"{SRC_DIR}. Remove the other copy from PYTHONPATH or uninstall it.")

    if not os.path.isfile(ENV_FILE):
        # Not fatal: every setting has a default. But an assistant with no API keys and no
        # local model will fail at the first question, and that is worth saying up front.
        info("No .env found — running with defaults. Run setup.py to configure API keys.")

    # Directories Kayra writes to. Created here rather than left to fail at the first write:
    # a fresh clone has none of them, and `data/` in particular is needed for the lock below.
    for name in ("data", "logs", "Reports"):
        try:
            os.makedirs(os.path.join(PROJECT_ROOT, name), exist_ok=True)
        except OSError as e:
            problems.append(f"Could not create {name}/: {e}")

    if problems:
        for p in problems:
            fail(p)
        hint("")
        hint("Try: python setup.py")
        return False

    report_environment()
    return True


def _distribution_version(name):
    """A package version WITHOUT importing the package. Costs a metadata read, not a load."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version(name)
        except PackageNotFoundError:
            return None
    except Exception:
        return None


def report_environment():
    """
    One line saying which interpreter and which ONNX Runtime this run is using.

    Deliberately cheap: the ORT variant and version come from installed-package METADATA, not
    from `import onnxruntime`, which costs ~200 ms and would land on the cold-start path for
    information the TTS engine is about to print properly anyway. What this catches is the
    class of problem that is invisible later — the wrong interpreter, or a CPU `onnxruntime`
    sitting on top of `onnxruntime-gpu`.
    """
    info(f"Python  {sys.version.split()[0]}  {sys.executable}")

    variants = [name for name in ("onnxruntime-gpu", "onnxruntime-directml", "onnxruntime")
                if _distribution_version(name)]
    if not variants:
        info("ONNX Runtime is not installed — speech output will be unavailable.")
        hint("Run: python setup.py")
    elif len(variants) > 1:
        # They share one package directory, so whichever was installed last wins and the other
        # is a half-deleted ghost. setup.py repairs this; say so rather than letting it surface
        # as a mystifying provider list.
        fail(f"Conflicting ONNX Runtime packages installed: {', '.join(variants)}")
        hint("Run `python setup.py` to repair the environment (it keeps exactly one).")
    else:
        info(f"ONNX Runtime  {variants[0]} {_distribution_version(variants[0])}")


def print_doctor():
    """
    `python run.py --doctor` — the full runtime picture, then exit.

    This is the thing to ask for when speech output is on the wrong device. It reports the
    interpreter, the environment, the ORT build, every provider, whether CUDA has been VERIFIED
    (a real session, not a provider name), and the GPU telemetry, all from the single authority
    in `kayra.output.tts_device`.
    """
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
    try:
        from kayra.output import tts_device
    except Exception as exc:
        fail(f"Could not load the speech runtime layer: {exc}")
        return 1

    # Prime the GPU sampler and give it one sample. `runtime_diagnostics` deliberately does
    # NOT start it (it is called from paint paths), but --doctor is an explicit request for the
    # numbers, so waiting a moment for them here is the right trade.
    if tts_device.gpu_metrics() is None:
        import time as _time
        deadline = _time.time() + 6
        while tts_device.gpu_metrics() is None and _time.time() < deadline:
            _time.sleep(0.25)

    diag = tts_device.runtime_diagnostics()
    rows = [
        ("Python", f"{sys.version.split()[0]}"),
        ("Executable", diag.python_executable),
        ("Environment", diag.environment_path),
        ("", ""),
        ("ONNX Runtime package", diag.ort_package),
        ("ONNX Runtime version", diag.ort_version),
        ("Package location", diag.ort_location),
        ("Built for CUDA", diag.ort_cuda_build or "not a CUDA build"),
        ("Available providers", ", ".join(diag.available_providers) or "none"),
        ("", ""),
        ("CUDA offered", "yes" if diag.cuda_available else "no"),
        ("CUDA VERIFIED usable", "yes" if diag.cuda_usable else "no"),
        ("CUDA runtime status", diag.cuda_runtime_status),
        ("cuDNN status", diag.cudnn_status),
        ("TensorRT offered", ("yes (not used for TTS)" if diag.tensorrt_available else "no")),
        ("", ""),
        ("TTS mode", diag.mode),
        ("Active device", diag.active_device),
        ("Active provider", diag.provider),
    ]
    if diag.gpu_name:
        rows += [("", ""), ("GPU", diag.gpu_name)]
        if diag.gpu_memory_total:
            rows.append(("VRAM", f"{(diag.gpu_memory_used or 0) / 1024:.1f} / "
                                 f"{diag.gpu_memory_total / 1024:.1f} GiB"))
        if diag.gpu_utilization is not None:
            rows.append(("Utilization", f"{diag.gpu_utilization:.0f}%"))
        if diag.temperature is not None:
            rows.append(("Temperature", f"{diag.temperature:.0f} C"))

    print()
    for label, value in rows:
        print("" if not label else f"  {label:<24} {value}")
    if diag.failure_reason:
        print()
        print(f"  Reason: {diag.failure_reason}")
    if diag.missing_packages:
        print(f"  Missing: {', '.join(diag.missing_packages)}")
        hint("Run: python setup.py")
    print()
    return 0


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      SINGLE-INSTANCE LOCK                              │
# └────────────────────────────────────────────────────────────────────────┘

def _process_identity(pid=None):
    """
    A (pid, create_time) pair identifying a process, or None when it cannot be determined.

    The creation timestamp is what makes the lock correct rather than merely usual. A PID alone
    is ambiguous: Windows recycles PIDs aggressively, and Kayra's shutdown path ends in
    `os._exit(0)` — which by design skips every `finally` in the process, so the lock file is
    ALWAYS left behind after a normal exit. A pure-PID check would therefore rely on the stale
    file naming a number that no live process happens to have reused; pairing it with the
    creation time makes "is this the same process?" an exact question.

    psutil is available in phase 2 (it is a project dependency and this runs inside the
    prepared environment). Without it, identity is unknowable and the caller falls back to
    treating the lock as held — refusing to start costs the user one deleted file, whereas
    wrongly reclaiming it starts a second Kayra that fights the first for the microphone.
    """
    try:
        import psutil
        proc = psutil.Process(os.getpid() if pid is None else pid)
        return (proc.pid, round(proc.create_time(), 3))
    except Exception:
        return None


def _read_lock():
    """The (pid, create_time) recorded in the lock file, or None if absent or unreadable."""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return (int(data["pid"]), float(data["created"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def acquire_lock(force=False):
    """
    Claims the single-instance lock. Returns a release callable, or None if already held.

    A stale lock is reclaimed silently. That is the common case rather than the exceptional
    one: Kayra's shutdown ends in `os._exit(0)`, so the file outlives every run. Requiring the
    user to delete it by hand after each session would be a far worse failure than the
    duplicate this exists to prevent.
    """
    mine = _process_identity()

    if force:
        info("--force: skipping the single-instance check.")
    else:
        held = _read_lock()
        if held is not None and (mine is None or held != mine):
            # Same identity as a LIVE process means a genuine second instance. Anything else —
            # a dead PID, or a live PID whose creation time differs because the number was
            # recycled — is a leftover file.
            if _process_identity(held[0]) == held:
                fail(f"Kayra is already running (process {held[0]}).")
                hint("Stop it first, or start this one anyway with:  python run.py --force")
                return None

    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        with open(LOCK_FILE, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(),
                       "created": mine[1] if mine else 0.0}, fh)
    except OSError:
        # An unwritable lock file must not stop the assistant from starting. The lock is a
        # convenience, not a safety property.
        return lambda: None

    def release():
        # Best-effort: the assistant's shutdown path calls `os._exit(0)`, which skips every
        # `finally` in the process, so this usually does NOT run. That is deliberate on the
        # application's side and is why staleness detection above has to be exact rather than
        # relying on the file being cleaned up.
        #
        # The file is read and CLOSED before removal. Windows refuses to unlink a file still
        # open in this process, so deleting inside the `with` block fails with a PermissionError
        # the except clause would swallow.
        if _read_lock() == mine:
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass

    return release


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             ENTRY POINT                                │
# └────────────────────────────────────────────────────────────────────────┘

USAGE = """Kayra launcher

  python run.py            start Kayra with the desktop interface
  python run.py --console  start without the interface (voice + terminal only)
  python run.py --force    start even if another instance holds the lock
  python run.py --doctor   report the Python, ONNX Runtime and GPU state, then exit
  python run.py --help     this message

First-time setup:  python setup.py
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0

    force = "--force" in argv
    if force:
        argv.remove("--force")

    console = "--console" in argv
    if console:
        argv.remove("--console")

    doctor = "--doctor" in argv
    if doctor:
        argv.remove("--doctor")

    # ── Phase 1: not in the venv yet ──
    if not running_inside_venv():
        passthrough = list(argv)
        if force:
            passthrough.append("--force")
        if console:
            passthrough.append("--console")
        if doctor:
            passthrough.append("--doctor")
        return relaunch_in_venv(passthrough)

    # ── Phase 2: running the right interpreter ──
    if not preflight():
        return 1

    # `--doctor` reports and exits. Placed AFTER preflight so it is guaranteed to describe the
    # venv, and BEFORE the lock so it can be run while Kayra is already up.
    if doctor:
        return print_doctor()

    release = acquire_lock(force=force)
    if release is None:
        return 1

    try:
        # Imported here, not at module scope: phase 1 runs on an interpreter where this import
        # cannot succeed, and a module-level import would crash the launcher before it had a
        # chance to explain that setup has not been run.
        if console:
            from kayra.app import main as app_main
            return app_main()

        # The desktop interface is the default front end. If Qt is not installed, Kayra is
        # still perfectly usable — it simply falls back to the console loop and says why,
        # rather than refusing to start over a presentation dependency.
        try:
            from kayra.ui.application import main as ui_main
        except ImportError as exc:
            info(f"Desktop interface unavailable ({exc}). Starting in console mode.")
            hint("Install it with:  .venv\\Scripts\\python.exe -m pip install PySide6-Essentials")
            from kayra.app import main as app_main
            return app_main()
        return ui_main(argv)
    except KeyboardInterrupt:
        # The application installs its own SIGINT handler and normally exits inside it. This is
        # the narrow window before that handler is registered.
        print()
        info("Interrupted before startup completed.")
        return 130
    finally:
        release()


if __name__ == "__main__":
    sys.exit(main())
