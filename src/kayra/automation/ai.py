# ┌────────────────────────────────────────────────────────────────────────┐
# │                           automation/ai.py                             │
# │      The two ways automation asks a model: reformat facts, see a screen │
# └────────────────────────────────────────────────────────────────────────┘
"""
Model calls the automation layer makes AFTER it has already decided what to do.

The rule from the rest of the pipeline holds: no model decides whether anything runs. Models
here only (a) turn facts Kayra already collected into a spoken sentence — a diagnosis, a
page, a document — and (b) look at a screenshot the user asked about. Both have a
deterministic fallback, so a dead provider costs wording, never the feature.

* `summarize(...)`  — the shared CentralizedLLMEngine (local first, then the cloud chain),
  bounded by a wall-clock deadline. Returns the fallback on any failure or timeout.
* `see(...)`        — a vision request. Local LM Studio first (private: the screenshot never
  leaves the machine), then Gemini, then Groq, each only when configured. None when nothing
  can see, and the caller falls back to OCR text.
"""

import os
import re
import time
import base64
import threading

DEFAULT_TIMEOUT_S = 25.0
VISION_TIMEOUT_S = 45.0

# Injected by the test suite so no model is ever called from a tier-1 run.
TEXT_MODEL = None       # callable(messages) -> str
VISION_MODEL = None     # callable(question, image_bytes, mime) -> str | None


def _clean(text):
    text = re.sub(r"```.*?```", "", str(text or ""), flags=re.S)
    text = re.sub(r"[*_#>`]+", "", text)
    return " ".join(text.split()).strip()


def _engine_text(messages):
    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    return "".join(CentralizedLLMEngine().generate_chat_stream(messages) or [])


def complete(system, user, timeout=DEFAULT_TIMEOUT_S):
    """
    The model's reply VERBATIM (code fences and all), or "" on failure/timeout. For callers
    that parse structure (JSON); `summarize` is for speech and strips fenced blocks, which
    used to swallow a ```json reply whole.
    """
    model = TEXT_MODEL or _engine_text
    box = {}

    def work():
        try:
            box["text"] = model([{"role": "system", "content": system},
                                 {"role": "user", "content": user}])
        except Exception as e:                       # provider down, no key, rate limited
            box["error"] = e

    thread = threading.Thread(target=work, name="kayra-ai-call", daemon=True)
    thread.start()
    thread.join(timeout)
    return str(box.get("text") or "")


_LOCAL_MODEL = {}                 # base_url -> (model id or None, monotonic time it was learned)
_LOCAL_MISS_TTL_S = 30.0          # "no local model" is re-checked: LM Studio may start after Kayra
_HTTP = {"session": None}


def _http():
    """One keep-alive session for the local server: no TCP handshake per decision."""
    if _HTTP["session"] is None:
        import requests
        _HTTP["session"] = requests.Session()
    return _HTTP["session"]


def _local_base():
    return _env("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1").replace("localhost", "127.0.0.1")


def _local_text_model(base_url):
    """The local model to use for quick decisions: LOCAL_DECISION_MODEL, else the first loaded
    chat model in LM Studio (never an embedding model). A hit is cached for the process; a MISS
    only for `_LOCAL_MISS_TTL_S`, so a server started after Kayra is picked up (it used to be
    cached as absent forever, which silently sent every quick decision down the slow path)."""
    configured = _env("LOCAL_DECISION_MODEL") or _env("LOCAL_CHAT_MODEL")
    if configured and configured != "local-model":
        return configured
    cached = _LOCAL_MODEL.get(base_url)
    if cached and (cached[0] or time.monotonic() - cached[1] < _LOCAL_MISS_TTL_S):
        return cached[0]
    try:
        data = _http().get(base_url.rstrip("/") + "/models", timeout=1.0).json()
        name = next((m["id"] for m in data.get("data", []) if "embed" not in m["id"].lower()), None)
    except Exception:
        name = None
    _LOCAL_MODEL[base_url] = (name, time.monotonic())
    return name


def _local_chat(system, user, timeout, max_tokens, temperature=0.0):
    """
    One non-streamed reply from the local server with THINKING OFF, or "" when there is no
    local model or it failed. The HTTP timeout is the bound: when it expires the connection is
    closed and the server stops generating, so a slow answer does not keep the GPU busy behind
    the user's back (a thread that is merely abandoned does).
    """
    base = _local_base()
    model = _local_text_model(base)
    if not model:
        return ""
    try:
        reply = _http().post(base.rstrip("/") + "/chat/completions", timeout=timeout, json={
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "reasoning_effort": "none",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}).json()
        return reply["choices"][0]["message"].get("content") or ""
    except Exception:
        _LOCAL_MODEL.pop(base, None)          # re-learn the model next time (it may have changed)
        return ""


def _cloud_forced():
    return str(_env("FORCE_ONLINE", "")).strip().lower() in ("1", "true", "yes", "on")


def fast(system, user, timeout=8.0, max_tokens=120):
    """
    A quick structured decision: the local model with THINKING OFF, temperature 0, a small
    output budget. Measured on the reference laptop (gemma-4-e4b): ~0.5 s instead of ~4 s,
    because ~85 % of a thinking model's output on these one-line answers is hidden reasoning.
    Falls back to `complete` (the engine's local/cloud chain) when no local server answers.
    Returns the reply verbatim, or "".
    """
    if TEXT_MODEL is not None:                       # the test suite's scripted model
        return complete(system, user, timeout)
    text = _local_chat(system, user, timeout, max_tokens)
    return text if text.strip() else complete(system, user, timeout)


def summarize(system, user, fallback="", timeout=DEFAULT_TIMEOUT_S, max_chars=700):
    """
    A short spoken answer from a model, or `fallback`. Never raises, never hangs.

    With a local server up (and the cloud not forced) this is a direct thinking-off call: a
    few spoken sentences do not need a reasoning pass, and on a thinking model that pass is
    most of the wait. Otherwise the engine's chain answers.
    """
    text = ""
    if TEXT_MODEL is None and not _cloud_forced():
        text = _clean(_local_chat(system, user, timeout, max_tokens=max(160, max_chars // 2),
                                  temperature=0.3))
    if not text:
        text = _clean(complete(system, user, timeout))
    if not text:
        return fallback
    if len(text) > max_chars:
        cut = text[:max_chars]
        text = cut[: cut.rfind(".") + 1] or cut
    return text


# ── Vision ────────────────────────────────────────────────────────────────

def _env(name, default=""):
    try:
        from kayra.core.config import env
        return env(name, default) or default
    except Exception:
        return os.environ.get(name, default)


def _vision_providers():
    """(label, base_url, api_key, model) for every configured vision backend, local first."""
    providers = []
    local = _env("VISION_LOCAL_BASE_URL") or _env("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
    local_model = _env("VISION_LOCAL_MODEL") or _local_vision_model(local)
    if local_model:
        providers.append(("local", local, _env("LOCAL_API_KEY", "lm-studio"), local_model))
    if _env("GEMINI_API_KEY"):
        providers.append(("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
                          _env("GEMINI_API_KEY"), _env("VISION_GEMINI_MODEL", "gemini-2.5-flash")))
    if _env("GROQ_API_KEY"):
        providers.append(("groq", "https://api.groq.com/openai/v1", _env("GROQ_API_KEY"),
                          _env("VISION_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")))
    return providers


_VISION_HINTS = ("gemma-3", "gemma-4", "gemma3", "gemma4", "llava", "vision", "-vl", "qwen2.5-vl",
                 "qwen3-vl", "qwen3.5", "qwen-3.5", "qwen3_5", "minicpm-v", "moondream", "pixtral", "llama-4", "smolvlm", "idefics")


def _local_vision_model(base_url):
    """A loaded local model that can take images, picked by name. None when none is loaded."""
    try:
        data = _http().get(base_url.rstrip("/") + "/models", timeout=1.5).json()
    except Exception:
        return None
    for model in data.get("data", []):
        name = str(model.get("id", ""))
        if any(hint in name.lower() for hint in _VISION_HINTS):
            return name
    return None


def _openai_vision(base_url, api_key, model, question, image_bytes, mime, local=False):
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=VISION_TIMEOUT_S)
    data_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    reply = client.chat.completions.create(
        model=model, max_tokens=300, temperature=0.2,
        # Describing a screenshot in five sentences needs no reasoning pass; on a local
        # thinking model that pass is most of the wait. Cloud backends keep their defaults.
        **({"extra_body": {"reasoning_effort": "none"}} if local else {}),
        messages=[{"role": "system", "content":
                   "You are Kayra, a desktop assistant looking at the user's screen. Answer in "
                   "plain spoken sentences, no markdown, no lists, at most five sentences."},
                  {"role": "user", "content": [
                      {"type": "text", "text": question},
                      {"type": "image_url", "image_url": {"url": data_uri}}]}])
    return reply.choices[0].message.content


def see(question, image_bytes, mime="image/jpeg"):
    """(answer, provider) from the first vision backend that answers, else (None, None)."""
    if VISION_MODEL is not None:
        answer = VISION_MODEL(question, image_bytes, mime)
        return (_clean(answer), "test") if answer else (None, None)
    for label, base_url, key, model in _vision_providers():
        try:
            answer = _openai_vision(base_url, key, model, question, image_bytes, mime,
                                    local=(label == "local"))
        except Exception:
            continue
        if answer and answer.strip():
            return _clean(answer), label
    return None, None
