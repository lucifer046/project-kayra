# ┌────────────────────────────────────────────────────────────────────────┐
# │                        automation/learning.py                          │
# │   What Kayra learns from how YOU talk — kept here, shown, forgettable  │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §4 H2 (Part 7c). Two things are learned, both on this computer only:

* **Aliases** — "PR group" means "PR and Outreach Core Team 2026-27" in WhatsApp. Taught when
  the user ANSWERS one of Kayra's "which one?" questions, or corrects a pick ("no, the other
  one": the wrong alias is dropped and the right one taught). Never guessed from silence:
  an alias the user never confirmed is not an alias. `data/aliases.json`, 0600.
* **Page recipes** — kept by `automation.page_agent` in `data/recipes.json` through the same
  private store; listed and forgotten from here.

Transparency is part of the feature: "what have you learned about me", "forget that",
"forget everything about WhatsApp", "forget everything you learned" (asks first). Nothing
learned is ever logged with its content or sent to a model.

Not here (yet): learned phrasings as classifier hints and a tone note for drafted replies
(H2's other two items) — both would touch the classifier prompt, which is measured and cached.
"""

import os
import re
import json
import time

DATA_DIR = None                    # the test suite points this at a temporary folder
ALIASES = "aliases.json"
RECIPES = "recipes.json"
MAX_ALIASES = 200
FORGET_WINDOW_S = 600              # "forget that" means something learned in the last 10 minutes
CLOCK = time.time

LAST = {"kind": None, "app": None, "said": None, "at": 0.0}
_APPS = {"whatsapp": "WhatsApp", "chrome": "Chrome", "gmail": "Gmail"}


def _path(name):
    if DATA_DIR is not None:
        return os.path.join(DATA_DIR, name)
    from kayra.core.paths import data_path
    return data_path(name)


def load_json(name, default):
    try:
        with open(_path(name), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def save_json(name, data):
    """Atomic write, 0600: what Kayra learned about the user is theirs."""
    path = _path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key(said):
    return " ".join(re.findall(r"[\w']+", str(said or "").lower()))


# ── aliases ──
def aliases(app):
    """{said: name} for `app`."""
    table = load_json(ALIASES, {}).get(app) or {}
    return {k: v["name"] for k, v in table.items() if isinstance(v, dict) and v.get("name")}


def alias_for(app, said):
    return aliases(app).get(_key(said))


def teach(app, said, name):
    """`said` means `name` in `app` from now on. Ignored when they are already the same words."""
    key = _key(said)
    if not key or not name or key == _key(name) or len(key) > 80:
        return False
    data = load_json(ALIASES, {})
    table = data.setdefault(app, {})
    entry = table.get(key) or {"uses": 0}
    entry.update(name=str(name), at=CLOCK(), uses=int(entry.get("uses", 0)) + 1)
    table[key] = entry
    if len(table) > MAX_ALIASES:                      # the least recently used go first
        for old in sorted(table, key=lambda k: table[k].get("at", 0))[:len(table) - MAX_ALIASES]:
            table.pop(old, None)
    save_json(ALIASES, data)
    LAST.update(kind="alias", app=app, said=key, at=CLOCK())
    return True


def demote(app, said, name):
    """A correction: `said` does NOT mean `name`."""
    data = load_json(ALIASES, {})
    table = data.get(app) or {}
    key = _key(said)
    if key in table and _key(table[key].get("name")) == _key(name):
        table.pop(key)
        save_json(ALIASES, data)
        return True
    return False


def forget_last():
    """'forget that': the alias or recipe learned most recently, within ten minutes."""
    if not LAST["kind"] or CLOCK() - LAST["at"] > FORGET_WINDOW_S:
        return None
    if LAST["kind"] == "alias":
        data = load_json(ALIASES, {})
        entry = (data.get(LAST["app"]) or {}).pop(LAST["said"], None)
        if entry is None:
            return None
        save_json(ALIASES, data)
        said = f"Forgot that \"{LAST['said']}\" means {entry.get('name')}."
    else:
        from kayra.automation import page_agent
        if not page_agent.forget_recipe(LAST["said"], host=LAST["app"]):
            return None
        said = f"Forgot how to \"{LAST['said']}\" on {LAST['app']}."
    LAST.update(kind=None, app=None, said=None, at=0.0)
    return said


def note_recipe(host, template):
    LAST.update(kind="recipe", app=host, said=template, at=CLOCK())


def forget_app(app):
    """'forget everything about WhatsApp': its aliases, or a site's recipes."""
    data = load_json(ALIASES, {})
    count = len(data.pop(app, {}) or {})
    if count:
        save_json(ALIASES, data)
    from kayra.automation import page_agent
    count += page_agent.forget_site(app)
    return count


def forget_all():
    count = sum(len(t) for t in load_json(ALIASES, {}).values() if isinstance(t, dict))
    from kayra.automation import page_agent
    count += len(page_agent.recipes())
    for name in (ALIASES, RECIPES):
        try:
            os.remove(_path(name))
        except OSError:
            pass
    LAST.update(kind=None, app=None, said=None, at=0.0)
    return count


def describe():
    """The spoken answer to 'what have you learned about me'."""
    parts = []
    for app, table in sorted(load_json(ALIASES, {}).items()):
        if not isinstance(table, dict) or not table:
            continue
        recent = sorted(table.items(), key=lambda kv: -kv[1].get("at", 0))
        shown = "; ".join(f"\"{k}\" means {v.get('name')}" for k, v in recent[:3])
        more = f", and {len(table) - 3} more" if len(table) > 3 else ""
        parts.append(f"In {_APPS.get(app, app)}: {shown}{more}.")
    from kayra.automation import page_agent
    recipes = page_agent.recipes()
    if recipes:
        sites = sorted({r["host"] for r in recipes})
        parts.append(f"I also remember {len(recipes)} way{'s' if len(recipes) != 1 else ''} of doing things on "
                     f"{', '.join(sites[:4])}{' and more' if len(sites) > 4 else ''}.")
    if not parts:
        return "I haven't learned anything about you yet. When I ask which one you mean, I remember your answer."
    return " ".join(parts) + " Say \"forget that\" or \"forget everything about WhatsApp\" to remove any of it."
