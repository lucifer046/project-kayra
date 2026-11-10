# Development

Read [`../../AGENTS.md`](../../AGENTS.md) first — it holds the rules. This page is the workflow.

## Environment

```bash
python3 setup.py                         # or: python setup.py on Windows
.venv/bin/python -m pip install pyflakes # the only dev tool the checks below need
```

Run any module with the venv interpreter; `run.py` and the test suites add `src/` to
`sys.path` themselves, so there is no editable install.

## The loop

```bash
# 1. change code
# 2. static checks (pyflakes catches undefined names that py_compile does not)
.venv/bin/python -m pyflakes src/kayra tests run.py setup.py main.py
.venv/bin/python -W error::SyntaxWarning -m py_compile $(git ls-files 'src/*.py' 'tests/*.py' run.py setup.py main.py | grep -v automation)
# 3. the suite that owns what you changed, then the whole unit tier
.venv/bin/python tests/test_platforms.py
.venv/bin/python tests/run_all.py
# 4. run it
python3 run.py --console
# 5. keep the knowledge graph current (optional, if you use graphify)
graphify update .
```

On Windows replace `.venv/bin/python` with `.venv\Scripts\python`.

## Working on platform code

- Add/modify a capability in **all three** backends in `src/kayra/platforms/`.
- Test it with synthetic inputs (env dicts, os-release text, temp XDG trees, a fake Hyprland
  socket, recording transports) in `tests/test_platforms.py` — never against the real desktop.
- Keep the layer stdlib-only; import optional libraries (evdev, pygetwindow) inside functions.
- If a Windows behaviour moves into the layer, move it verbatim and leave an alias at the old
  name (see `pointer._Win32Injector`, `store.explorer_command`).

## Working near automation

`src/kayra/automation/` is frozen. If a change elsewhere needs something from it, add an
adapter outside it, or stop and ask. Do not "clean up" lint inside it.

## Python-version matrix

```bash
for v in 3.10 3.11 3.12 3.13 3.14; do
  uv venv -p $v /tmp/kv && uv pip install --python /tmp/kv/bin/python -r requirements.txt
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/run_all.py --python /tmp/kv/bin/python
  rm -rf /tmp/kv
done
```

Each full venv is ~1.5 GB (ONNX Runtime, PySide6, MediaPipe, OpenCV); on a tmpfs `/tmp`, run
the versions one at a time.

## Debugging aids

- `python3 run.py --doctor` — interpreter, ORT variant/location, providers, verified CUDA, GPU.
- `KAYRA_LOG_LEVEL=DEBUG python3 run.py --console` — DMM tokens, VAD, interim transcripts,
  provider exceptions, pointer backend choice.
- `python -m kayra.input.gesture --doctor` — camera, detector, accelerator.
- `python -c "from kayra import platforms; print(platforms.current().to_dict())"` (with
  `src` on the path) — what the platform layer sees.

## Commits

Do not commit or push unless asked. Keep docs in the same change as the code they describe.
