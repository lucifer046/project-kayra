# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/local_models.py                       │
# │   "Which model is loaded?", "load Qwen", "check my API keys"            │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §13 N2 (Part 15).

* **N2.3 LM Studio by voice.** What is loaded (and whether it can see images — screen questions
  need that), "load Qwen" / "switch the local model to gemma", "unload the local model".
  State is read from LM Studio's own REST API (`/api/v0/models`, which says loaded / not-loaded
  and llm / vlm / embeddings); loading and unloading go through its `lms` command (argv only).
  An 8 GB GPU cannot hold two 8–9B models (measured in Part 7: LM Studio refused to load Qwen
  while gemma was resident), so loading UNLOADS the others first, and says so.
* **N2.2 How fast is the local model** — two tiny local requests with thinking off; the second
  is the warm number. Local only: nothing costs money.
* **N2.4 Key health.** "Check my API keys" makes ONE tiny request per stored key
  (`llm_engine.probe_provider`) and says which work, which have no credit, which are revoked.
  That spends a fraction of a cent per key, so it never runs on its own unless
  `KEY_HEALTH_ON_BOOT=on` (default off) — then once, in the background, after boot, and only a
  problem is spoken.
"""

import os
import re
import time
import shutil

HTTP_GET = None                    # the test suite injects a fake: (url, timeout) -> parsed JSON
LOAD_TIMEOUT_S = 240.0
PROVIDERS = (("Claude", ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"), "CLAUDE_MODEL", "claude-haiku-5-5"),
             ("Groq", ("GROQ_API_KEY",), "GROQ_DECISION_MODEL", "openai/gpt-oss-20b"),
             ("Gemini", ("GEMINI_API_KEY",), "GEMINI_CHAT_MODEL", "gemini-flash-latest"))
PROBE = None                       # the test suite injects a fake probe_provider


def _L():
    from kayra.automation import linux
    return linux


def _result(kind, message, **detail):
    from kayra.automation.policy import ActionResult, Status
    return ActionResult({"ok": Status.OK, "not_found": Status.NOT_FOUND, "failed": Status.FAILED,
                         "unsupported": Status.UNSUPPORTED, "ambiguous": Status.AMBIGUOUS}[kind], message, detail)


def _base():
    from kayra.automation import ai
    return re.sub(r"/v1/?$", "", ai._local_base())


def _get(path, timeout=2.0):
    url = _base() + path
    if HTTP_GET is not None:
        return HTTP_GET(url, timeout)
    from kayra.automation import ai
    return ai._http().get(url, timeout=timeout).json()


def models():
    """[{id, state, type}] the local server knows (downloaded), or None when it is not running."""
    try:
        data = _get("/api/v0/models")
        return [{"id": m.get("id", ""), "state": m.get("state", ""), "type": m.get("type", "")}
                for m in data.get("data", [])]
    except Exception:
        pass
    try:
        data = _get("/v1/models")                      # older servers: no state, everything listed is usable
        return [{"id": m.get("id", ""), "state": "loaded", "type": ""} for m in data.get("data", [])]
    except Exception:
        return None


def _short(model_id):
    return model_id.split("/")[-1]


def loaded_report():
    """N2.3 'which model is loaded'."""
    found = models()
    if found is None:
        return _result("not_found", "The local model server isn't running. Start LM Studio and I'll use it.")
    chat = [m for m in found if m["state"] == "loaded" and m["type"] != "embeddings"]
    if not chat:
        downloaded = [_short(m["id"]) for m in found if m["type"] != "embeddings"]
        return _result("ok", "No local model is loaded" + (f". You have {', '.join(downloaded[:4])} — say load and a name." if downloaded else "."))
    parts = []
    for m in chat:
        sees = " It can see images." if m["type"] == "vlm" else \
            " It can't see images, so questions about your screen need a vision model." if m["type"] == "llm" else ""
        parts.append(f"{_short(m['id'])} is loaded.{sees}")
    return _result("ok", " ".join(parts))


def _lms():
    found = shutil.which("lms")
    if found:
        return found
    candidate = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms")
    return candidate if os.path.isfile(candidate) else None


def _resolve(name, found):
    from kayra.automation import semantic_match as S
    cands = [S.Candidate(_short(m["id"]).replace("-", " "), data=m) for m in found if m["type"] != "embeddings"]
    decision = S.match(name, cands)
    if decision.decision == S.PICK:
        return decision.best.data, None
    if decision.decision == S.ASK:
        a, b = [c.data["id"] for c in decision.top(2)]
        return None, _result("ambiguous", f"{_short(a)} or {_short(b)}?", candidates=[a, b])
    return None, _result("not_found", f"I don't see a local model called {name}. You have "
                         + ", ".join(_short(m["id"]) for m in found if m["type"] != "embeddings")[:200] + ".")


def _forget_cached_model():
    from kayra.automation import ai
    ai._LOCAL_MODEL.clear()            # the next quick decision asks LM Studio which model is loaded


def load(name):
    """N2.3 'load qwen' — unload the others (one model fits), then load it."""
    found = models()
    if found is None:
        return _result("not_found", "The local model server isn't running.")
    model, problem = _resolve(name, found)
    if problem:
        return problem
    if model["state"] == "loaded":
        return _result("ok", f"{_short(model['id'])} is already loaded.")
    lms = _lms()
    if not lms:
        return _result("unsupported", "LM Studio's lms command isn't installed, so load it in LM Studio itself.")
    from kayra.automation.common import announce
    announce(f"Loading {_short(model['id'])}. This takes a little while.", urgent=True)
    others = [m["id"] for m in found if m["state"] == "loaded" and m["type"] != "embeddings"]
    L = _L()
    if others:
        L.RUNNER.run([lms, "unload", "--all"], timeout=60.0)
    code, _out = L.RUNNER.run([lms, "load", model["id"], "-y"], timeout=LOAD_TIMEOUT_S)
    _forget_cached_model()
    if code != 0:
        return _result("failed", f"LM Studio couldn't load {_short(model['id'])}" + (" — I'd unloaded "
                       + ", ".join(_short(o) for o in others) + ", so it loads on demand again." if others else "."))
    swapped = f" (unloaded {', '.join(_short(o) for o in others)} to make room)" if others else ""
    return _result("ok", f"{_short(model['id'])} is loaded{swapped}.")


def unload():
    """N2.3 'unload the local model'."""
    found = models()
    if found is None:
        return _result("not_found", "The local model server isn't running.")
    loaded = [m["id"] for m in found if m["state"] == "loaded" and m["type"] != "embeddings"]
    if not loaded:
        return _result("ok", "No local model is loaded.")
    lms = _lms()
    if not lms:
        return _result("unsupported", "LM Studio's lms command isn't installed, so unload it in LM Studio itself.")
    code, _out = _L().RUNNER.run([lms, "unload", "--all"], timeout=60.0)
    _forget_cached_model()
    if code != 0:
        return _result("failed", "LM Studio didn't unload it.")
    return _result("ok", f"Unloaded {', '.join(_short(m) for m in loaded)}. LM Studio loads a model again when one is "
                   "needed; until then I use your cloud models if you have keys.")


def speed_test():
    """N2.2 'how fast is my local model' — two tiny local requests; the second is the warm one."""
    from kayra.automation import ai
    if not ai._local_text_model(ai._local_base()):
        return _result("not_found", "No local model is running.")
    times = []
    for _ in range(2):
        started = time.perf_counter()
        reply = ai._local_chat("Reply with exactly: OK", "Say OK.", 30.0, 8)
        times.append(time.perf_counter() - started)
        if not reply:
            return _result("failed", "The local model didn't answer.")
    name = _short(ai._local_text_model(ai._local_base()) or "the local model")
    return _result("ok", f"{name} answers a short question in {times[1]:.1f} seconds when warm "
                   f"({times[0]:.1f} the first time).", seconds=times)


def key_health(speak_only_problems=False):
    """N2.4 'check my API keys' — one tiny request per stored key."""
    from kayra.core.config import env
    probe = PROBE
    if probe is None:
        from kayra.intelligence.llm_engine import probe_provider as probe
    lines, problems = [], []
    for provider, names, model_key, default_model in PROVIDERS:
        key = next((env(n) for n in names if env(n)), "")
        if not key:
            continue
        outcome = probe(provider, key, env(model_key) or default_model) or {}
        message = str(outcome.get("message") or "").rstrip(".")
        if outcome.get("ok"):
            lines.append(f"{provider} works")
        else:
            lines.append(f"{provider}: {message}")
            problems.append(f"{provider}: {message}")
    if not lines:
        return _result("ok", "You haven't stored any cloud keys. I use your local model.")
    if speak_only_problems:
        return _result("ok", ("Key check: " + ". ".join(problems) + ".") if problems else "")
    return _result("ok", ". ".join(lines) + ".")


def boot_check(announce):
    """KEY_HEALTH_ON_BOOT=on: once, in the background, and only a problem is spoken."""
    from kayra.core.config import env
    if str(env("KEY_HEALTH_ON_BOOT", "off") or "off").strip().lower() not in ("1", "on", "true", "yes"):
        return False
    result = key_health(speak_only_problems=True)
    if result.message:
        announce(result.message)
    return True
