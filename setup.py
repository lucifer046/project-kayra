#!/usr/bin/env python
# ┌────────────────────────────────────────────────────────────────────────┐
# │                             setup.py                                   │
# │                Kayra Environment Preparation (run once)                │
# └────────────────────────────────────────────────────────────────────────┘
"""
Prepares the machine to run Kayra. It does NOT run Kayra.

    python setup.py          # first time, or after changing requirements
    python run.py            # every time after that

WHAT IT DOES
------------
  1. Verifies the Python interpreter is a version this project supports.
  2. Creates (or reuses) `.venv`, never deleting an existing one without asking.
  3. Installs `requirements.txt` into that environment.
  4. Validates that the imports Kayra actually needs really work.
  5. Detects whether a local LLM server is running, and asks for cloud API keys ONLY for
     the things that are genuinely missing.
  6. Creates `.env` from `.env.example` if absent — never overwriting an existing one.
  7. Checks for the speech model assets and reports honestly what is missing.

DESIGN RULES
------------
* **This script runs on the SYSTEM interpreter**, before the virtual environment exists. It
  therefore imports nothing outside the standard library, and nothing from `kayra`. A setup
  script that needs the project's dependencies in order to install the project's dependencies
  is not a setup script.
* **Nothing is destroyed silently.** The one destructive operation (recreating an incompatible
  `.venv`) requires an explicit "y", and the answer defaults to no.
* **No secret is ever printed.** Configuration status is reported as SET or MISSING.
* **Failures are reported, not hidden.** A dependency that will not install is a loud error,
  because the alternative is Kayra failing three subsystems deep at runtime with a confusing
  message.
"""

import os
import re
import sys
import json
import shutil
import socket
import platform
import subprocess

# ── Supported interpreters ────────────────────────────────────────────────
# 3.10 is the floor: the codebase uses `str.removeprefix` and builtin generic annotations.
# 3.11 is what every measurement and test run in this repository was produced on, so it is
# what gets recommended. Newer versions are allowed but flagged as unverified rather than
# blocked — refusing to run on a version nobody has tried is not the same as knowing it fails.
MIN_PYTHON = (3, 10)
RECOMMENDED_PYTHON = (3, 11)
MAX_VERIFIED_PYTHON = (3, 12)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(PROJECT_ROOT, ".venv")
REQUIREMENTS = os.path.join(PROJECT_ROOT, "requirements.txt")
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
ENV_EXAMPLE = os.path.join(PROJECT_ROOT, ".env.example")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")

IS_WINDOWS = sys.platform.startswith("win")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          CONSOLE HELPERS                               │
# └────────────────────────────────────────────────────────────────────────┘
# `rich` is a project dependency and therefore may not exist yet. ANSI is used directly, and
# disabled when the stream is not a terminal so piped output stays readable.
#
# ENCODING. This script runs on the SYSTEM interpreter, where a Windows console is still
# commonly on a legacy code page (cp1252). Box-drawing and check-mark glyphs raise
# UnicodeEncodeError there, which would crash setup while it was printing its own banner. So:
# reconfigure to UTF-8 where the stream supports it, then probe what can actually be encoded
# and fall back to ASCII for the rest. A setup script must run on the console the user has,
# not the one it would prefer.
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


_UNICODE = _can_encode("═╔╗╚╝║▸✓✗─")

_G = {
    "h":     "═" if _UNICODE else "=",
    "tl":    "╔" if _UNICODE else "+",
    "tr":    "╗" if _UNICODE else "+",
    "bl":    "╚" if _UNICODE else "+",
    "br":    "╝" if _UNICODE else "+",
    "v":     "║" if _UNICODE else "|",
    "step":  "▸" if _UNICODE else ">",
    "yes":   "✓" if _UNICODE else "[ok]",
    "no":    "✗" if _UNICODE else "[!!]",
    "rule":  "─" if _UNICODE else "-",
    "dash":  "—" if _UNICODE else "-",
}

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(text, code):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg):
    print(f"  {msg}")


def step(msg):
    print(_paint(f"\n{_G['step']} {msg}", "1;36"))


def ok(msg):
    print(f"  {_paint('OK', '1;32')}   {msg}")


def warn(msg):
    print(f"  {_paint('WARN', '1;33')} {msg}")


def fail(msg):
    print(f"  {_paint('FAIL', '1;31')} {msg}")


def banner():
    line = _G["h"] * 62
    title = f"KAYRA {_G['dash']} ENVIRONMENT SETUP".center(62)
    print(_paint(f"\n{_G['tl']}{line}{_G['tr']}", "1;36"))
    print(_paint(f"{_G['v']}{title}{_G['v']}", "1;36"))
    print(_paint(f"{_G['bl']}{line}{_G['br']}", "1;36"))


def ask_yes_no(question, default=False):
    """Blocking y/n prompt. Defaults to NO for anything destructive."""
    suffix = "[y/N]" if not default else "[Y/n]"
    while True:
        try:
            answer = input(f"  {question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_secret(label):
    """
    Prompts for an API key.

    Deliberately NOT `getpass`: the user is pasting a key they already have on their
    clipboard, and a prompt that shows nothing at all leads people to paste twice. The value
    is never echoed back, stored anywhere but `.env`, or printed again.
    """
    try:
        return input(f"  {label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     1. PYTHON VERSION VALIDATION                       │
# └────────────────────────────────────────────────────────────────────────┘

def version_text(info_tuple):
    return ".".join(str(part) for part in info_tuple[:3])


def check_python():
    """Refuses to continue on an interpreter the project cannot run on."""
    step("Checking Python version")
    current = sys.version_info
    info(f"Detected Python {version_text(current)} ({sys.executable})")

    if current[:2] < MIN_PYTHON:
        fail(f"Kayra requires Python {version_text(MIN_PYTHON)} or newer.")
        info("")
        info("Install a supported Python and run setup again with it, for example:")
        info(r"    C:\Python311\python.exe setup.py")
        info("")
        info("Downloads: https://www.python.org/downloads/")
        return False

    if current[:2] > MAX_VERIFIED_PYTHON:
        warn(f"Python {version_text(current)} is newer than the versions this project has "
             f"been verified on ({version_text(RECOMMENDED_PYTHON)}). Continuing, but if a "
             "dependency fails to build, try Python "
             f"{version_text(RECOMMENDED_PYTHON)}.")
    elif current[:2] != RECOMMENDED_PYTHON:
        warn(f"Python {version_text(RECOMMENDED_PYTHON)} is the recommended version; "
             f"{version_text(current[:2])} should work.")
    else:
        ok(f"Python {version_text(current)} is the recommended version.")
    return True


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     2. VIRTUAL ENVIRONMENT                             │
# └────────────────────────────────────────────────────────────────────────┘

def venv_python(venv_dir=VENV_DIR):
    """Path to the interpreter inside a virtual environment, per platform."""
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def interpreter_version(python_exe):
    """(major, minor, micro) of another interpreter, or None if it cannot be asked."""
    try:
        result = subprocess.run(
            [python_exe, "-c", "import sys;print('%d %d %d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return tuple(int(part) for part in result.stdout.split())
    except Exception:
        pass
    return None


def ensure_venv():
    """
    Creates or validates `.venv`.

    An existing environment on a compatible interpreter is REUSED — recreating it would throw
    away a working multi-hundred-megabyte install for no reason. An incompatible one is only
    removed after an explicit yes, because it may not even belong to this project.
    """
    step("Preparing the virtual environment")
    executable = venv_python()

    if os.path.isdir(VENV_DIR):
        existing = interpreter_version(executable)
        if existing is None:
            warn(f".venv exists at {VENV_DIR} but its interpreter does not run.")
            if not ask_yes_no("Recreate it?", default=False):
                fail("Cannot continue with a broken virtual environment.")
                return None
            if not remove_venv():
                return None
        elif existing[:2] < MIN_PYTHON:
            warn(f"The existing .venv uses Python {version_text(existing)}. "
                 f"Kayra requires {version_text(MIN_PYTHON)} or newer.")
            if not ask_yes_no("Recreate the environment?", default=False):
                fail("Setup stopped. Kayra cannot run in this environment.")
                info("Either recreate it later, or point setup at a supported interpreter.")
                return None
            if not remove_venv():
                return None
        else:
            ok(f"Reusing the existing .venv (Python {version_text(existing)}).")
            return executable

    info(f"Creating {VENV_DIR} with Python {version_text(sys.version_info)} ...")
    try:
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    except subprocess.CalledProcessError as e:
        fail(f"Could not create the virtual environment: {e}")
        info("On Debian/Ubuntu you may need: sudo apt install python3-venv")
        return None

    if not os.path.exists(executable):
        fail(f"The environment was created but {executable} is missing.")
        return None
    ok("Virtual environment created.")
    return executable


def remove_venv():
    """Deletes `.venv`. Only ever called after an explicit confirmation."""
    info("Removing the old environment ...")
    try:
        shutil.rmtree(VENV_DIR)
        return True
    except OSError as e:
        fail(f"Could not remove {VENV_DIR}: {e}")
        info("Close anything using that environment (an editor, a running Kayra) and retry.")
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. DEPENDENCY INSTALLATION                         │
# └────────────────────────────────────────────────────────────────────────┘

def install_dependencies(python_exe):
    """
    Installs `requirements.txt` into the environment.

    Invoked as `<venv python> -m pip`, never as a bare `pip`: the shell this script runs in
    has NOT activated the environment, and a bare `pip` would install into whatever happens to
    be first on PATH — frequently the system Python, which is how "I installed it but it says
    the module is missing" happens.
    """
    step("Installing dependencies")
    if not os.path.exists(REQUIREMENTS):
        fail(f"{REQUIREMENTS} is missing.")
        return False

    info("Upgrading pip ...")
    subprocess.run([python_exe, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
                   check=False)

    info("Installing from requirements.txt (this can take a few minutes) ...")
    result = subprocess.run([python_exe, "-m", "pip", "install", "-r", REQUIREMENTS])
    if result.returncode != 0:
        fail("Dependency installation failed. The pip output above says why.")
        info("Common causes: no network, a missing C toolchain, or a package with no wheel "
             "for this Python version.")
        return False

    ok("Dependencies installed.")
    return True


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     4. IMPORT VALIDATION                               │
# └────────────────────────────────────────────────────────────────────────┘
# "pip said it worked" and "the import works" are different claims. These are the imports
# Kayra actually performs at boot, grouped by the subsystem that breaks without them.

IMPORT_GROUPS = [
    ("Core",       ["dotenv", "rich", "requests", "psutil"]),
    ("LLM",        ["openai", "cohere", "google.generativeai"]),
    ("Speech-out", ["kokoro_onnx", "sounddevice", "numpy"]),
    ("Speech-in",  ["selenium", "mtranslate"]),
    ("Automation", ["pyautogui", "keyboard", "pyperclip", "AppOpener", "pywhatkit", "bs4"]),
    ("Search",     ["ddgs"]),
]

if IS_WINDOWS:
    IMPORT_GROUPS.append(("Windows", ["win32gui", "win32process", "pygetwindow"]))

# Missing these degrades a feature; missing anything else stops the assistant.
OPTIONAL_IMPORTS = {"google.generativeai", "ddgs", "send2trash", "mediapipe", "cv2"}


def validate_imports(python_exe):
    """Imports each dependency inside the environment and reports exactly what failed."""
    step("Validating imports")
    probe = (
        "import json,sys\n"
        "names=json.loads(sys.argv[1])\n"
        "bad={}\n"
        "for n in names:\n"
        "    try:\n"
        "        __import__(n)\n"
        "    except Exception as e:\n"
        "        bad[n]=type(e).__name__+': '+str(e)[:90]\n"
        "print(json.dumps(bad))\n"
    )

    all_ok = True
    broken = {}
    for group, names in IMPORT_GROUPS:
        result = subprocess.run([python_exe, "-c", probe, json.dumps(names)],
                                capture_output=True, text=True)
        try:
            failures = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            failures = {name: "probe failed" for name in names}

        required_failures = {n: r for n, r in failures.items() if n not in OPTIONAL_IMPORTS}
        optional_failures = {n: r for n, r in failures.items() if n in OPTIONAL_IMPORTS}

        if not failures:
            ok(f"{group}: all {len(names)} imports fine")
        else:
            if required_failures:
                all_ok = False
                fail(f"{group}: {', '.join(required_failures)}")
                broken.update(required_failures)
            if optional_failures:
                warn(f"{group}: optional missing — {', '.join(optional_failures)}")

    if broken:
        info("")
        for name, reason in broken.items():
            info(f"    {name}: {reason}")
    return all_ok


def validate_package(python_exe):
    """The project itself must import from `src/` without being installed."""
    step("Validating the Kayra package")
    probe = (
        "import sys, os\n"
        "sys.path.insert(0, os.path.join(%r, 'src'))\n"
        "import kayra\n"
        "from kayra.core.paths import project_root, models_dir\n"
        "from kayra.core.config import env_values\n"
        "import kayra.app\n"
        "print('OK', kayra.__version__, project_root())\n"
    ) % PROJECT_ROOT
    result = subprocess.run([python_exe, "-c", probe], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.startswith("OK"):
        ok(f"kayra package imports cleanly ({result.stdout.split()[1]})")
        return True
    fail("The kayra package could not be imported.")
    for line in (result.stderr or "").strip().splitlines()[-6:]:
        info(f"    {line}")
    return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     5. CONFIGURATION (.env)                            │
# └────────────────────────────────────────────────────────────────────────┘

def read_env(path):
    """Minimal KEY=VALUE parser. Avoids needing python-dotenv before it is installed."""
    values = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def ensure_env_file():
    """
    Creates `.env` from `.env.example` when it is absent.

    NEVER overwrites an existing `.env`. That file holds the user's keys and their tuning, and
    a setup script that clobbers it is a setup script people learn to fear.
    """
    step("Checking configuration")
    if os.path.exists(ENV_FILE):
        ok(".env already exists (left untouched).")
        return True
    if not os.path.exists(ENV_EXAMPLE):
        fail(".env.example is missing; cannot create a starter .env.")
        return False
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    ok("Created .env from .env.example.")
    return True


def set_env_values(updates):
    """
    Writes keys into `.env`, preserving every existing line, comment and ordering.

    Rewriting the file from a dict would silently discard the annotated comments in
    `.env.example` that explain what each setting does.
    """
    if not updates:
        return
    try:
        with open(ENV_FILE, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []

    remaining = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        output.append("")
        output.append("# Added by setup.py")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    with open(ENV_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(output).rstrip() + "\n")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     6. LOCAL LLM DETECTION                             │
# └────────────────────────────────────────────────────────────────────────┘

def probe_local_llm(base_url, timeout=0.6):
    """
    Is a local OpenAI-compatible server listening?

    A TCP connect first, exactly as `llm_engine._check_local_server` does at runtime, and for
    the same reason: Windows Firewall DROPS rather than refuses connections to closed loopback
    ports, so a bare HTTP request to a dead port waits out the full timeout. Connecting first
    turns "no server" into a sub-millisecond answer.
    """
    match = re.match(r"https?://([^:/]+):?(\d+)?", base_url or "")
    if not match:
        return False
    host = match.group(1)
    port = int(match.group(2) or (443 if base_url.startswith("https") else 80))

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False

    # The port is open; confirm it actually speaks the models API.
    try:
        import urllib.request
        url = base_url.rstrip("/") + "/models"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer local"})
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status < 500
    except Exception:
        # Something is listening but did not answer the models endpoint. Treat it as present:
        # the runtime probe makes the same judgement, and disagreeing here would produce a
        # setup that asks for cloud keys the running assistant then never uses.
        return True


def configure_models():
    """
    Works out what Kayra needs in order to think, and asks only for that.

    LOCAL FIRST. If a local server is up and `FORCE_ONLINE` is not set, the local model serves
    both chat and intent classification and NO cloud key is required — so none is requested.
    Asking for "all your API keys" up front, when the configuration cannot use them, is how
    setup scripts train people to paste secrets they did not need to.
    """
    step("Model routing")
    env = read_env(ENV_FILE)
    base_url = env.get("LOCAL_BASE_URL", "http://localhost:1234/v1")
    force_online = env.get("FORCE_ONLINE", "False").strip().lower() == "true"

    local_available = False
    if force_online:
        info("FORCE_ONLINE=True in .env — skipping the local server check.")
    else:
        info(f"Probing local LLM at {base_url} ...")
        local_available = probe_local_llm(base_url)

    if local_available:
        ok("Local LLM server detected. Kayra will route chat AND intent classification there.")
        info("No cloud API keys are required for this configuration.")
        cloud_state = {"required": False, "cohere": bool(env.get("CohereAPIKey", "").strip()
                                                         and "your_" not in env.get("CohereAPIKey", "")),
                       "chat": _has_chat_key(env)}
        return local_available, cloud_state

    if force_online:
        info("Cloud routing is forced on.")
    else:
        warn("No local LLM server is running.")
    info("Kayra will use the cloud path: Cohere for intent, Groq (or Gemini) for chat.")

    updates = {}

    # Intent classification: Cohere only, no fallback. Without it every query degrades to
    # plain conversation and no automation can ever be triggered.
    if not _key_present(env, "CohereAPIKey"):
        info("")
        info("Cohere powers intent classification. Without it, Kayra can still chat but")
        info("cannot run any commands. Free key: https://dashboard.cohere.com/api-keys")
        value = ask_secret("Cohere API key (blank to skip)")
        if value:
            updates["CohereAPIKey"] = value
    else:
        ok("Cohere API key: SET")

    # Chat generation: Groq primary, Gemini fallback. At least one is required.
    if not _has_chat_key(env):
        info("")
        info("A chat model is required. Groq is the primary; Gemini is the fallback.")
        info("Free keys: https://console.groq.com/keys  |  https://aistudio.google.com/apikey")
        groq = ask_secret("Groq API key (blank to skip)")
        if groq:
            updates["GROQ_API_KEY"] = groq
        gemini = ask_secret("Gemini API key (blank to skip)")
        if gemini:
            updates["GEMINI_API_KEY"] = gemini
    else:
        ok("Chat API key: SET")

    # Real-time search and deep research use DuckDuckGo, which needs no credentials — so
    # nothing is asked for them.
    info("Web search uses DuckDuckGo and needs no API key.")

    if updates:
        set_env_values(updates)
        ok(f"Saved {len(updates)} value(s) to .env.")

    env = read_env(ENV_FILE)
    return False, {"required": True,
                   "cohere": _key_present(env, "CohereAPIKey"),
                   "chat": _has_chat_key(env)}


def _key_present(env, name):
    """True when a key is set to something that is not the placeholder from .env.example."""
    value = (env.get(name) or "").strip()
    return bool(value) and "your_" not in value.lower() and value.lower() != "none"


def _has_chat_key(env):
    return _key_present(env, "GROQ_API_KEY") or _key_present(env, "GEMINI_API_KEY")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     7. MODEL ASSETS                                    │
# └────────────────────────────────────────────────────────────────────────┘

def check_models():
    """
    Reports which speech model files are present.

    Nothing is downloaded automatically: these are hundreds of megabytes, and a setup script
    that silently pulls that much data over someone's connection is not being helpful.
    """
    step("Checking speech model assets")
    if not os.path.isdir(MODELS_DIR):
        warn(f"models/ does not exist ({MODELS_DIR}).")
        _print_model_help()
        return "missing"

    quantized = (os.path.join(MODELS_DIR, "kokoro-v1.0.int8.onnx"),
                 os.path.join(MODELS_DIR, "voices-v1.0.bin"))
    full = (os.path.join(MODELS_DIR, "kokoro.onnx"),
            os.path.join(MODELS_DIR, "voices.bin"))

    if all(os.path.exists(p) for p in quantized):
        ok("Quantized Kokoro model present (fastest speech synthesis).")
        return "quantized"
    if all(os.path.exists(p) for p in full):
        ok("Full-precision Kokoro model present.")
        warn("The quantized model (kokoro-v1.0.int8.onnx + voices-v1.0.bin) synthesizes "
             "several times faster at effectively identical quality.")
        _print_model_help()
        return "full"

    fail("No usable Kokoro voice model found in models/.")
    _print_model_help()
    return "missing"


def _print_model_help():
    info("    Download from: https://github.com/thewh1teagle/kokoro-onnx/releases")
    info("    Place kokoro-v1.0.int8.onnx and voices-v1.0.bin into models/")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     8. PLATFORM VALIDATION                             │
# └────────────────────────────────────────────────────────────────────────┘

def check_platform(python_exe):
    """Windows version, audio output, and the browser the STT layer drives."""
    step("Checking the platform")
    results = {}

    if IS_WINDOWS:
        ok(f"Windows {platform.release()} (build {platform.version()})")
    else:
        warn(f"{platform.system()} detected. Kayra's automation layer is Windows-only; "
             "conversation and speech will work, computer control will not.")
    results["os"] = IS_WINDOWS

    # Audio OUTPUT is checkable without recording anything. Microphone access deliberately is
    # not probed: doing so would open the device and pop a permission prompt during setup, and
    # The browser asks for it properly at first run anyway.
    probe = ("import sounddevice as sd\n"
             "out=[d for d in sd.query_devices() if d['max_output_channels']>0]\n"
             "inp=[d for d in sd.query_devices() if d['max_input_channels']>0]\n"
             "print(len(out), len(inp))\n")
    result = subprocess.run([python_exe, "-c", probe], capture_output=True, text=True)
    if result.returncode == 0:
        try:
            outputs, inputs = (int(x) for x in result.stdout.split())
            if outputs:
                ok(f"Audio output devices: {outputs}")
            else:
                fail("No audio output device found. Kayra will have nothing to speak through.")
            if inputs:
                ok(f"Audio input devices: {inputs} (not opened; the browser requests the mic at run time)")
            else:
                warn("No microphone detected. Kayra will fall back to keyboard input.")
            results["audio"] = bool(outputs)
        except ValueError:
            warn("Could not enumerate audio devices.")
            results["audio"] = None
    else:
        warn("sounddevice could not query the audio devices.")
        results["audio"] = None

    # Selenium Manager (bundled with Selenium 4.6+) downloads the matching driver on first
    # run, so no driver needs installing here. A usable browser does have to exist.
    usable, unusable = _find_speech_browsers()
    if usable:
        ok(f"Speech-input browser: {', '.join(usable)}")
        for label in unusable:
            info(f"    {label} is installed but ships no speech backend, so it cannot "
                 f"transcribe; Kayra will not use it for voice input.")
        results["chrome"] = True
    else:
        warn("No browser that can run speech recognition was found.")
        if unusable:
            info(f"    Found {', '.join(unusable)}, but that browser ships without a speech "
                 f"backend and cannot transcribe.")
        info("    Voice input needs Microsoft Edge (preinstalled on Windows 11) or Chrome.")
        info("    Kayra still runs without one - it falls back to keyboard input.")
        results["chrome"] = False

    return results


# Browsers that can run speech recognition, and whether they carry a speech BACKEND.
#
# This is the single most misunderstood requirement in the project, so it is spelled out here
# too: `webkitSpeechRecognition` existing is not enough. Chrome streams to Google's service,
# Edge to Microsoft's, and Brave ships neither (a deliberate privacy decision) - in Brave the
# API is present, start() succeeds, and recognition then fails with 'network' forever.
#
# Kayra verifies this at runtime and falls back; setup only needs to tell the user whether a
# USABLE browser exists at all. Edge is preinstalled on Windows 11, so this is almost never a
# blocker - which is why Chrome is not a hard requirement.
_SPEECH_BROWSERS = (
    ("Google Chrome", "usable", (
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    )),
    ("Microsoft Edge", "usable", (
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    )),
    ("Brave", "no-backend", (
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
    )),
    ("Chromium", "unknown", (
        r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    )),
)


def _find_speech_browsers():
    """
    (usable_names, unusable_names) for browsers that could run speech recognition.

    Only browsers with a known or possible backend count as usable; a browser known to ship
    without one is reported separately so the summary can explain rather than just warn.
    """
    usable, unusable = [], []
    for label, capability, paths in _SPEECH_BROWSERS:
        found = False
        for raw in paths:
            expanded = os.path.expandvars(raw)
            if "%" in expanded:
                continue                     # unset variable on this host
            if os.path.isfile(expanded):
                found = True
                break
        if not found and not IS_WINDOWS:
            found = bool(shutil.which(label.split()[-1].lower()))
        if found:
            (usable if capability != "no-backend" else unusable).append(label)
    return usable, unusable


def _find_chrome():
    """Kept for compatibility; speech input no longer requires Chrome specifically."""
    usable, _ = _find_speech_browsers()
    return usable[0] if usable else None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          9. SUMMARY                                    │
# └────────────────────────────────────────────────────────────────────────┘

def _speech_browser_text():
    """One line naming what speech input can actually use on this machine."""
    usable, unusable = _find_speech_browsers()
    if usable:
        text = ", ".join(usable)
        if unusable:
            text += f" ({', '.join(unusable)} has no speech backend)"
        return text
    if unusable:
        return f"only {', '.join(unusable)}, which cannot transcribe - install Edge or Chrome"
    return "none found - install Microsoft Edge or Google Chrome"


def summary(state):
    print(_paint("\n" + _G["rule"] * 64, "1;36"))
    print(_paint("  KAYRA SETUP SUMMARY", "1;36"))
    print(_paint(_G["rule"] * 64, "1;36"))

    def row(label, value, good=True):
        mark = _paint(_G["yes"], "1;32") if good else _paint(_G["no"], "1;31")
        print(f"  {mark} {label:<28} {value}")

    row("Python", state["python"], True)
    row("Virtual environment", state["venv"], state["venv_ok"])
    row("Dependencies", state["deps"], state["deps_ok"])
    row("Kayra package", state["package"], state["package_ok"])
    row("Local LLM", state["local_llm"], True)
    row("Cloud keys", state["cloud"], state["cloud_ok"])
    row("Speech model", state["models"], state["models_ok"])
    row("Audio output", state["audio"], state["audio_ok"])
    row("Speech-input browser", state["chrome"], state["chrome_ok"])
    row("Automation", state["automation"], state["automation_ok"])

    print(_paint(_G["rule"] * 64, "1;36"))
    if state["ready"]:
        print(_paint("\n  Setup complete. Start Kayra with:\n", "1;32"))
        print(_paint("      python run.py\n", "1;37"))
    else:
        print(_paint(f"\n  Setup finished with problems. Fix the items marked {_G['no']} above, "
                     "then run setup again.\n", "1;33"))
    print("  Nothing above prints your API keys; only whether they are set.\n")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             MAIN                                       │
# └────────────────────────────────────────────────────────────────────────┘

def main():
    banner()

    if not check_python():
        return 1

    python_exe = ensure_venv()
    if python_exe is None:
        return 1
    venv_version = interpreter_version(python_exe)

    deps_ok = install_dependencies(python_exe)
    imports_ok = validate_imports(python_exe) if deps_ok else False
    package_ok = validate_package(python_exe) if deps_ok else False

    env_ok = ensure_env_file()
    local_available, cloud = configure_models() if env_ok else (False, {"required": True,
                                                                        "cohere": False,
                                                                        "chat": False})
    model_state = check_models()
    platform_state = check_platform(python_exe)

    cloud_ok = (not cloud["required"]) or (cloud["cohere"] and cloud["chat"])
    if cloud["required"]:
        parts = []
        parts.append("Cohere SET" if cloud["cohere"] else "Cohere MISSING")
        parts.append("chat SET" if cloud["chat"] else "chat MISSING")
        cloud_text = ", ".join(parts)
    else:
        cloud_text = "not required (local model in use)"

    ready = bool(deps_ok and imports_ok and package_ok and env_ok
                 and model_state != "missing" and cloud_ok)

    summary({
        "python": f"{version_text(sys.version_info)} (venv: "
                  f"{version_text(venv_version) if venv_version else 'unknown'})",
        "venv": "ready" if python_exe else "missing",
        "venv_ok": bool(python_exe),
        "deps": "installed" if deps_ok else "FAILED",
        "deps_ok": deps_ok and imports_ok,
        "package": "importable" if package_ok else "NOT IMPORTABLE",
        "package_ok": package_ok,
        "local_llm": "available" if local_available else "not running (cloud path)",
        "cloud": cloud_text,
        "cloud_ok": cloud_ok,
        "models": {"quantized": "quantized (fast)", "full": "full precision",
                   "missing": "MISSING"}[model_state],
        "models_ok": model_state != "missing",
        "audio": "ready" if platform_state.get("audio") else "not verified",
        "audio_ok": bool(platform_state.get("audio")),
        "chrome": _speech_browser_text(),
        "chrome_ok": bool(_find_speech_browsers()[0]),
        "automation": "ready" if (imports_ok and IS_WINDOWS) else "unavailable on this OS",
        "automation_ok": bool(imports_ok and IS_WINDOWS),
        "ready": ready,
    })
    return 0 if ready else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled. Nothing was left half-installed.\n")
        sys.exit(130)
