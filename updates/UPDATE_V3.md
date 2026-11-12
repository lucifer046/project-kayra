# Update V3 — Tuning pass: latency, idle cost, and a 250 KB agent file

| | |
|---|---|
| **Version** | V3 |
| **Based on** | commit `1559df7` on `feature/linux-desktop-automation` (Linux automation rounds 1-5 part 1, understanding layer) |
| **Date** | 2026-11-10 |
| **Type** | Performance and maintenance; one behaviour addition (the classifier fast path) |
| **Scope** | intelligence (DMM), automation model calls, browser macros, the Chrome extension, bridge, watcher, app index, speech model choice, `setup.py`, agent docs |
| **Not in scope** | Windows automation; the Hyprland event socket; extension active-tab push (listed as not done) |

## 1. Summary

A review of the updates so far, followed by measurement. Two of the review's assumptions were
wrong and are recorded here because the corrections matter more than the fixes:

- **The speech model believed to be "full precision" was the quantized one, and the quantized
  one is the slow one.** `models/kokoro.onnx` is byte-for-byte `kokoro-v1.0.int8.onnx`.
- **The local intent classifier was not paying for hidden thinking.** Its few-shot prompt
  already suppresses it (3-9 output tokens, ~0.4 s warm).

| Area | Before | After (measured, reference laptop) |
|---|---|---|
| Speech synthesis, 6.5 s sentence | 6.2-7.0 s (RTF ~1.0, CPU or GPU) | 1.4 s on the processor, 0.24 s on the GPU (fp16) |
| "Chrome is open." to audio | ~1.6 s | 0.36 s processor / 0.07-0.4 s GPU |
| Page / email / calendar summary | 10.1 s | 2.1 s |
| Local screen description | 7.8 s | 1.8 s |
| WhatsApp / Meet / calendar flows | 1.5-5.5 s of fixed pauses | readiness waits |
| Exact commands ("pause", "volume 40") | one model call | none |
| `CLAUDE.md` loaded into every agent session | 256 KB | 35 KB (+ 227 KB in `docs/reference/`, verbatim) |

## 2. Changes by area

**Speech.** `output/text_to_speech.py` picks the model from `VOICE_MODELS` (fp16, fp32, int8,
`kokoro.onnx`) and warns when only the quantized graph is installed. `setup.py::check_models`
offers the 177 MB fp16 file (asked, SHA-256 pinned, written as `.part` first).
`core/system_profile.py` reports the model by what it is. Why fp16 first: same speed as fp32,
half the download, half the VRAM (+637 MiB vs +1151 MiB), which matters beside a 9B local LLM
on an 8 GB card.

**Classifier.** `llm_engine.fast_intent`: an utterance that is itself a complete command is
returned as its token without a model. Whole-utterance exact match, like
`core.voice_control`; conversational words ("search", "save", "print"), broad closes, power
and lifecycle are excluded. `_dmm_local` sends `reasoning_effort: none` (protects a heavier
thinking model from returning empty content inside the 128-token budget; a server that
rejects the field is asked once more without it).

**Automation model calls, browser macros, extension, idle costs.** See the 2026-11-10 tuning
entry in `docs/platforms/LINUX_AUTOMATION.md` (table and file list).

**Agent docs.** `CLAUDE.md` keeps the data flow, cross-platform rules, import/path rules,
module map, style conventions and an index; each subsystem's long notes moved verbatim to
`docs/reference/*.md` (nine files).

## 3. Bugs fixed

| Bug | Effect | Fix |
|---|---|---|
| `ai._local_text_model` cached "no local model" forever | LM Studio started after Kayra was never used for quick decisions | misses expire after 30 s |
| Local vision with thinking on returned empty text inside a small token budget | "what's on my screen" fell through to slower backends or OCR | thinking off for the local backend |
| `evaluate_triggers` rewrote `triggers.json` every 20 s | constant disk writes while any automation existed | written only on change |
| `setup.py` and the System screen called the int8 graph "fastest" | users were steered to the slow model | corrected, with the measurement |

## 4. Dependencies and configuration

No dependency changes. New `.env` key `DMM_FAST_PATH` (default on). New optional model file
`models/kokoro-v1.0.fp16.onnx` (git-ignored; 177 MB). New data file
`data/learned_sites.json` (bounded at 200 names). Extension manifest 1.2.0 -> 1.2.1.

## 5. Verification

Omarchy, Hyprland, Python 3.14 project venv, LM Studio with `google/gemma-4-e4b`.

- Unit tier: **4572 passed, 0 failed, 4 skipped/n-a, 58 s** (`tests/run_all.py`).
- `tests/test_browser_extension.py` (real headless Chrome): 27/27.
- `tests/test_dmm_matrix.py` against the local model: 50/53, the same three documented flaky
  boundaries as before the change; none involves the fast path.
- Timings in the tables: measured with throwaway scripts against the live local model and the
  real ONNX sessions.

## 6. Known limitations / not verified

- **Not tested with Qwen3.5-9B** (not installed). Whether LM Studio honours
  `reasoning_effort: none` for it is unverified; if it does not, quick decisions will be slow
  or empty and `tests/test_dmm_matrix.py` will show it.
- The new readiness waits were verified against the fake extension and fixture pages, **not
  in the user's real Gmail / WhatsApp / Meet / Calendar**.
- GPU speech takes ~640 MiB of VRAM. With a 9B model loaded on an 8 GB card that may not
  fit; `TTS_DEVICE_MODE=CPU` still gives RTF ~0.22 with no VRAM.
- Windows was not run.

## 7. Upgrade notes

1. Run `python3 setup.py` and accept the speech-model download (or copy
   `kokoro-v1.0.fp16.onnx` into `models/`).
2. Chrome picks up extension 1.2.1 by itself the next time it connects (1.1.0+), or say
   "browser update".
