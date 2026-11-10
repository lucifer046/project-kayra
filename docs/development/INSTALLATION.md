# Installation

Two commands on every platform. The user never activates the virtual environment.

| | Windows | Linux / Omarchy |
|---|---|---|
| Once | `python setup.py` | `python3 setup.py` |
| Every time | `python run.py` | `python3 run.py` |
| Venv interpreter | `.venv\Scripts\python.exe` | `.venv/bin/python` |

## 1. Prerequisites

- **Python 3.10–3.14** (3.11 recommended; 3.14 is Omarchy's system Python and works).
- **Kokoro voice model** in `models/`: `kokoro.onnx` + `voices.bin` are tracked in git; the
  faster quantized pair (`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`) is recommended — see the
  README.
- **A speech-input browser:** Windows — Edge (preinstalled) or Chrome. Linux — Google Chrome.
- **Linux system packages:** see the table in [`../platforms/LINUX.md`](../platforms/LINUX.md#system-packages).
- **Optional:** an NVIDIA GPU + driver (speech on CUDA), a webcam (gestures), a local LLM
  server (LM Studio / Ollama) or cloud keys (Claude, Groq or Gemini — any one serves intent and chat).

## 2. What `setup.py` does

1. Checks the interpreter (≥ 3.10; warns above 3.14).
2. Creates/validates `.venv` (asks before deleting a broken one; default **no**).
3. `pip install -r requirements.txt` (platform markers pick the right packages).
4. Validates the imports Kayra performs at boot, per subsystem, and that `kayra` imports from
   `src/`.
5. Creates `.env` from `.env.example` (never overwrites), probes for a local LLM, asks for cloud
   keys (blank to skip; nothing is echoed).
6. Checks the Kokoro model; downloads `models/hand_landmarker.task` (~8 MB, asks first) when the
   installed MediaPipe needs it.
7. Configures speech synthesis: keeps exactly one ONNX Runtime variant; on an NVIDIA machine
   installs `onnxruntime-gpu` + pinned CUDA 12 runtime wheels (~1.4 GB) and **proves** a CUDA
   session starts; elsewhere reconciles to the CPU build.
8. Platform checks: Windows version / Linux distribution, session, PortAudio, `/dev/uinput`,
   audio devices, speech browser.
9. Prints a summary; exit code 0 only when Kayra is ready to run.

Re-run it any time: it is idempotent and repairs drift (e.g. a stray CPU `onnxruntime`).

## 3. First run

```bash
python3 run.py --doctor     # optional: confirm interpreter, ORT variant, CUDA, GPU
python3 run.py              # desktop UI
python3 run.py --console    # terminal only
```

Startup prints a report read from the live subsystems (models, speech device, speech browser,
microphone processing, memory, presence). Desktop automation is reported as unavailable on
Linux — that is expected.

## Uninstall / reset

Delete `.venv/` (re-run setup to recreate). User data lives in `data/`, `Reports/` and `logs/`
and `.env`; delete those only if you want to lose memory, habits, reports and settings.
