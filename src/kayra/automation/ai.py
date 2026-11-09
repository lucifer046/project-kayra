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


def summarize(system, user, fallback="", timeout=DEFAULT_TIMEOUT_S, max_chars=700):
    """A short spoken answer from a model, or `fallback`. Never raises, never hangs."""
    model = TEXT_MODEL or _engine_text
    box = {}

    def work():
        try:
            box["text"] = model([{"role": "system", "content": system},
                                 {"role": "user", "content": user}])
        except Exception as e:                       # provider down, no key, rate limited
            box["error"] = e

    thread = threading.Thread(target=work, name="kayra-ai-summary", daemon=True)
    thread.start()
    thread.join(timeout)
    text = _clean(box.get("text", ""))
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
                 "qwen3-vl", "minicpm-v", "moondream", "pixtral", "llama-4", "smolvlm", "idefics")


def _local_vision_model(base_url):
    """A loaded local model that can take images, picked by name. None when none is loaded."""
    try:
        import requests
        data = requests.get(base_url.rstrip("/") + "/models", timeout=1.5).json()
    except Exception:
        return None
    for model in data.get("data", []):
        name = str(model.get("id", ""))
        if any(hint in name.lower() for hint in _VISION_HINTS):
            return name
    return None


def _openai_vision(base_url, api_key, model, question, image_bytes, mime):
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=VISION_TIMEOUT_S)
    data_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    reply = client.chat.completions.create(
        model=model, max_tokens=500, temperature=0.2,
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
            answer = _openai_vision(base_url, key, model, question, image_bytes, mime)
        except Exception:
            continue
        if answer and answer.strip():
            return _clean(answer), label
    return None, None
