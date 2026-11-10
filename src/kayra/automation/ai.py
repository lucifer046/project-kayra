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
  leaves the machine), then Claude, Gemini and Groq, each only when configured; with
  FORCE_ONLINE / CLOUD_FIRST the cloud backends go first. None when nothing can see, and the
  caller falls back to OCR text.
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


def relevant(text, question, budget=4000, chunk=420):
    """
    The parts of `text` that bear on `question`, in their original order, within `budget`
    characters — so a page question sends ~4 K characters instead of 12 K (measured on
    gemma-4-e4b: 1.1 s -> ~0.45 s) WITHOUT the blind cut that dropped the answer. Chunks are
    scored by the question's content words (5-letter stems, so "refunds" finds "refund"); the
    page's opening chunk is always kept for context. Short text is returned unchanged.
    """
    text = str(text or "")
    if len(text) <= budget:
        return text
    stop = {"what", "does", "this", "page", "say", "about", "the", "and", "for", "with", "that",
            "from", "have", "tell", "mention", "there", "which", "when", "where", "who", "how",
            "document", "doc", "site", "article"}
    stems = {w[:5] for w in re.findall(r"[a-z0-9]{3,}", str(question).lower()) if w not in stop}
    pieces, start = [], 0
    while start < len(text):                       # sentence-aligned chunks of ~`chunk` chars
        end = min(len(text), start + chunk)
        if end < len(text):
            cut = text.rfind(". ", start + chunk // 2, end + 120)
            end = cut + 1 if cut != -1 else end
        pieces.append(text[start:end])
        start = end
    scored = []
    for index, piece in enumerate(pieces):
        low = piece.lower()
        score = sum(low.count(stem) for stem in stems)
        scored.append((score, index))
    keep, used = {0}, len(pieces[0])
    for score, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 or index in keep:
            continue
        if used + len(pieces[index]) > budget:
            continue
        keep.add(index)
        used += len(pieces[index])
    if len(keep) == 1:                              # nothing matched: the opening, as before
        return text[:budget]
    return " … ".join(pieces[i].strip() for i in sorted(keep))


def warm_up(systems, timeout=20.0):
    """
    Loads each system prompt into the local server's prompt cache with a one-token request,
    so the FIRST real call after boot is as fast as the rest (measured: the understanding
    layer's first call 1.3 s, later ones ~0.5 s). Local only; silent; never raises.
    """
    if TEXT_MODEL is not None or _cloud_forced():
        return 0
    warmed = 0
    for system in systems:
        if _local_chat(system, "ok", timeout, max_tokens=1):
            warmed += 1
    return warmed


def _truthy(name):
    return str(_env(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _cloud_forced():
    """FORCE_ONLINE, or CLOUD_FIRST with a cloud key: the cloud answers before a local model."""
    if _truthy("FORCE_ONLINE"):
        return True
    return _truthy("CLOUD_FIRST") and any(
        _cloud_key(name) for name in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY",
                                      "GROQ_API_KEY", "GEMINI_API_KEY"))


def _cloud_key(name):
    value = str(_env(name, "")).strip()
    return "" if value.lower().startswith(("your_", "gsk_your", "sk-ant-your")) else value


def fast(system, user, timeout=8.0, max_tokens=120):
    """
    A quick structured decision: the local model with THINKING OFF, temperature 0, a small
    output budget. Measured on the reference laptop (gemma-4-e4b): ~0.5 s instead of ~4 s,
    because ~85 % of a thinking model's output on these one-line answers is hidden reasoning.
    Falls back to `complete` (the engine's local/cloud chain) when no local server answers.
    Returns the reply verbatim, or "".
    """
    if TEXT_MODEL is not None or _cloud_forced():    # scripted model / cloud preferred
        return complete(system, user, timeout)
    text = _local_chat(system, user, timeout, max_tokens)
    return text if text.strip() else complete(system, user, timeout)


def summarize(system, user, fallback="", timeout=DEFAULT_TIMEOUT_S, max_chars=700, local_only=False):
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
    # Private content (chats) never goes to a cloud model; a scripted TEXT_MODEL (tests) is fine.
    if not text and (not local_only or TEXT_MODEL is not None):
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
    """
    (label, base_url, api_key, model) for every configured vision backend.

    Local first — private, the screenshot never leaves the machine — unless the cloud is
    preferred (FORCE_ONLINE / CLOUD_FIRST). Then Claude, Gemini, Groq: Claude Haiku is the
    fastest of the three on a screenshot, Gemini Flash the strongest reader of dense text.
    """
    cloud = []
    claude_key = _cloud_key("CLAUDE_API_KEY") or _cloud_key("ANTHROPIC_API_KEY")
    if claude_key:
        cloud.append(("claude", None, claude_key,
                      _env("VISION_CLAUDE_MODEL") or _env("CLAUDE_MODEL") or "claude-haiku-5-5"))
    if _cloud_key("GEMINI_API_KEY"):
        cloud.append(("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
                      _cloud_key("GEMINI_API_KEY"),
                      _env("VISION_GEMINI_MODEL") or _env("GEMINI_CHAT_MODEL") or "gemini-flash-latest"))
    if _cloud_key("GROQ_API_KEY"):
        cloud.append(("groq", "https://api.groq.com/openai/v1", _cloud_key("GROQ_API_KEY"),
                      _env("VISION_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")))
    local = []
    local_url = _env("VISION_LOCAL_BASE_URL") or _env("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
    local_model = _env("VISION_LOCAL_MODEL") or _local_vision_model(local_url)
    if local_model:
        local.append(("local", local_url, _env("LOCAL_API_KEY", "lm-studio"), local_model))
    return cloud + local if _cloud_forced() else local + cloud


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
        messages=[{"role": "system", "content": _VISION_SYSTEM},
                  {"role": "user", "content": [
                      {"type": "text", "text": question},
                      {"type": "image_url", "image_url": {"url": data_uri}}]}])
    return reply.choices[0].message.content


_VISION_SYSTEM = ("You are Kayra, a desktop assistant looking at the user's screen. Answer in "
                  "plain spoken sentences, no markdown, no lists, at most five sentences.")


def _claude_vision(api_key, model, question, image_bytes, mime):
    import anthropic
    from kayra.intelligence.llm_engine import claude_speed_args, claude_text
    client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=VISION_TIMEOUT_S)
    reply = client.messages.create(
        model=model, max_tokens=400, system=_VISION_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime,
                                         "data": base64.standard_b64encode(image_bytes).decode()}},
            {"type": "text", "text": question}]}],
        **claude_speed_args(model, "low"))
    return claude_text(reply)


def see(question, image_bytes, mime="image/jpeg"):
    """(answer, provider) from the first vision backend that answers, else (None, None)."""
    if VISION_MODEL is not None:
        answer = VISION_MODEL(question, image_bytes, mime)
        return (_clean(answer), "test") if answer else (None, None)
    for label, base_url, key, model in _vision_providers():
        try:
            if label == "claude":
                answer = _claude_vision(key, model, question, image_bytes, mime)
            else:
                answer = _openai_vision(base_url, key, model, question, image_bytes, mime,
                                        local=(label == "local"))
        except Exception:
            continue
        if answer and answer.strip():
            return _clean(answer), label
    return None, None
