# Dependencies

`requirements.txt` is the single manifest. `setup.py` installs it into `.venv` and then owns
the ONNX Runtime variant and the NVIDIA runtime wheels. There is deliberately no
`pyproject.toml`/Poetry/Pipenv/uv-lock: `setup.py` is a stdlib bootstrap *script* (not
setuptools), and a second packaging system would compete with it — `pip install .` would even
try to execute it as a build script.

## Python support

| Version | Status | Evidence |
|---|---|---|
| 3.10 | Supported (floor) | resolves on Windows + Linux; unit tier run on Linux |
| 3.11 | Supported (recommended; historical measurements) | resolves on Windows + Linux; unit tier run on Linux |
| 3.12 | Supported | resolves on Windows + Linux; unit tier run on Linux |
| 3.13 | Supported | resolves on Windows + Linux; unit tier run on Linux |
| 3.14 | Supported | resolves on Windows + Linux; full install + unit tier + live STT on Linux (Omarchy's system Python) |
| 3.15 | **Not supported yet** | not released at the time of writing (Nov 2026); no wheels exist for onnxruntime, PySide6, numpy, mediapipe |

"Resolves on Windows" was verified with `uv pip compile --python-platform x86_64-pc-windows-msvc`
for each version; the Windows unit tier was **not** re-run in this change (no Windows host
available). The per-version Linux results are recorded in
[`../development/MIGRATION_CROSS_PLATFORM.md`](../development/MIGRATION_CROSS_PLATFORM.md).

`setup.py` enforces `MIN_PYTHON = 3.10`, recommends 3.11, and warns above
`MAX_VERIFIED_PYTHON = 3.14`.

## Packages

| Package | Constraint | Platforms | Used by | Why this constraint |
|---|---|---|---|---|
| selenium | >=4.20 | all | `input/speech_to_text` | Selenium Manager (driver resolution, `--skip-driver-in-path`) |
| mtranslate | >=1.8 | all | `input/speech_to_text` | sdist only; pure Python |
| python-dotenv | >=1.0 | all | `core/config` | |
| rich | >=13.7 | all | `utils/console` | |
| requests | >=2.31 | all | llm_engine, deep_research | |
| psutil | >=5.9 | all | STT ownership, metrics, lock | |
| pygetwindow | >=0.0.9 | **win32** | `platforms/windows` | Windows/macOS only upstream |
| pywin32 | >=306 | **win32** | `automation/targets` | no Linux wheels |
| pyperclip | >=1.8 | all | automation clipboard | |
| send2trash | >=1.8 | all | automation file delete (optional) | |
| openai | >=1.30 | all | OpenAI-compatible clients (Groq, Gemini, local) | 3.x verified by the provider-router suite |
| anthropic | >=2.0 | all | Claude: first cloud provider (DMM and chat) | verified with 2.7.0; imported lazily by `llm_engine` |
| kokoro-onnx | >=0.4 | all | `output/text_to_speech` | pulls CPU `onnxruntime` transitively; setup repairs |
| sounddevice | >=0.4 | all | audio out | Linux needs system PortAudio |
| numpy | >=1.26 | all | audio, gestures | |
| soundfile | >=0.12 | all | audio | |
| ddgs | >=9.0 | all | search, research | `duckduckgo-search` (old name) removed — redundant |
| beautifulsoup4 | >=4.12 | all | deep_research, automation | |
| AppOpener, pyautogui, pynput, pywhatkit, keyboard | see file | **win32** | `automation/windows` only | do not install or import on Linux (Xlib/root/Windows-only) |
| opencv-python | >=4.9 | all | gesture camera | |
| mediapipe | >=0.10.14,**<1.0** | all | gesture detector | every 1.x release SIGKILLs during `HandLandmarker` construction on the reference host |
| evdev | >=1.7 | **linux** | `platforms/linux` uinput clicks/scroll | optional at runtime |
| PySide6-Essentials | >=6.6 (**<6.12 on Python < 3.12**) | all | `ui/` | essentials only: no QtWebEngine. 6.12.0 aborts CPython 3.10/3.11 with `Fatal Python error: none_dealloc` (shiboken refcount bug hidden by 3.12+'s immortal `None`); 6.11.3/6.10.3 pass the UI suite on 3.11 |

**Not listed on purpose:** `onnxruntime` / `onnxruntime-gpu` / `onnxruntime-directml` (one
`onnxruntime/` directory, mutually exclusive; `setup.py::configure_speech_runtime` picks and
verifies), and the `nvidia-*-cu12` runtime wheels (pinned in `setup.py::CUDA_RUNTIME_PINS`,
installed only when an NVIDIA GPU is present — on Windows and Linux alike).

## Resolved versions (Linux, 2026-11-09)

| | 3.10 | 3.11–3.14 |
|---|---|---|
| numpy | 2.2.6 | 2.4.6 (3.11) / 2.5.4 |
| onnxruntime (CPU, transitive) | 1.23.2 | 1.31.1 |
| mediapipe | 0.10.35 | 0.10.35 |
| PySide6-Essentials (installed on Arch's glibc) | 6.11.3 (ceiling) | 6.11.3 (3.11) / 6.12.0 (3.12+) |
| evdev | 1.9.3 | 2.0.0 |
| openai / anthropic / selenium | 3.46.0 / 2.7.0 / 4.52.0 | same |

## MediaPipe: two APIs, one detector

| mediapipe | API | Model | Python |
|---|---|---|---|
| 0.10.14 – 0.10.21 | `mp.solutions.hands` | bundled in the wheel | ≤ 3.12 |
| 0.10.30 – 0.10.35 | Tasks `HandLandmarker` | `models/hand_landmarker.task` (setup downloads, ~7.8 MB) | 3.10 – 3.14 (py3-none wheels) |
| 1.0.0 – 1.2.2 | Tasks | — | excluded: crashes on construction |

`input/gesture/detector.py` uses `solutions` when it exists and the Tasks adapter otherwise, so
an existing Windows install on 0.10.14 keeps working unchanged.

## Checking a change

```bash
for v in 3.10 3.11 3.12 3.13 3.14; do
  for p in x86_64-pc-windows-msvc x86_64-unknown-linux-gnu; do
    uv pip compile requirements.txt --python-version $v --python-platform $p -q -o /dev/null \
      && echo "ok $v $p"
  done
done
```

Resolution is necessary, not sufficient: install into a venv and run
`tests/run_all.py --python <that venv's python>` before claiming a version works.
