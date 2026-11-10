# ┌────────────────────────────────────────────────────────────────────────┐
# │                        automation/workflows.py                         │
# │   Scenes, sessions, plans, triggers, the watcher, briefings, recall    │
# └────────────────────────────────────────────────────────────────────────┘
"""
The layer that makes Kayra feel like one assistant instead of a command list.

* **Scenes** — "work mode", "movie mode", "save this layout as study". A scene is a list of
  ordinary automation tokens ("open vscode", "system move window to workspace 2",
  "system do not disturb on"), run in order through the FULL policy. Built-ins exist; saved
  ones live in `data/scenes.json`. "remember my setup" / "restore my setup" are scenes too,
  and the layout is saved automatically as "last session" when Kayra shuts down.
* **Plans** — "get me ready for my presentation". A model turns the goal into tokens from a
  fixed catalog; each token is validated by the normalizer and executed through the policy,
  so a plan can never do anything the user could not have asked for directly. A step that
  needs confirmation pauses the plan; "yes" resumes it.
* **Triggers** — "when the download finishes, shut down the computer", "when my battery hits
  20 percent, switch to power saver", "every day at 9 am, start work mode". Stored in
  `data/triggers.json`. A trigger whose action needs confirmation is confirmed when it is
  CREATED, and when it fires Kayra announces it and waits a one-minute grace period that
  "cancel timer" stops.
* **The watcher** — ONE daemon thread (`kayra-watch`) that evaluates triggers every 20 s and
  the health watch every 60 s. It reads cheap state (sysfs, /proc, a folder listing) and
  speaks through `common.announce`.
* **Briefing** — "good morning" / "brief me": time, weather, calendar, unread mail, battery,
  health, updates, overnight crashes — phrased by the model, deterministic without one.
* **Recall** — "what did you just do?" from the audit log the policy already keeps.
"""

import os
import re
import json
import time
import datetime
import threading

from kayra.automation import ai
from kayra.automation.common import announce, normalize_command
from kayra.automation.policy import ActionResult, Risk, classify_action, recent_audit, Status


def _L():
    from kayra.automation import linux
    return linux


def _data(name):
    try:
        from kayra.core.paths import data_path
        return data_path(name)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".kayra-" + name)


def _load(name, default):
    try:
        with open(_data(name), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(name, data):
    path = _data(name)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                SCENES                                  │
# └────────────────────────────────────────────────────────────────────────┘

BUILTIN_SCENES = {
    "work": ["open terminal", "system move window to workspace 1", "open vs code",
             "system move window to workspace 2", "open chrome", "system move window to workspace 3",
             "system do not disturb on", "system power profile balanced", "system workspace 2"],
    "study": ["open chrome", "system move window to workspace 1", "open obsidian",
              "system move window to workspace 2", "system do not disturb on", "system volume 30",
              "system workspace 1"],
    "focus": ["system do not disturb on", "system volume 20", "system workspace empty"],
    "gaming": ["system power profile performance", "system do not disturb on", "open steam"],
    "movie": ["system do not disturb on", "system volume 70", "system brightness 80",
              "system night light off"],
    "presentation": ["system do not disturb on", "system brightness 100",
                     "system power profile performance", "system volume 50"],
    "night": ["system night light on", "system brightness 40", "system volume 25"],
    "battery saver": ["system power profile power saver", "system brightness 40",
                      "system bluetooth off"],
}
SCENE_FILE = "scenes.json"


def scenes():
    saved = _load(SCENE_FILE, {})
    merged = dict(BUILTIN_SCENES)
    merged.update({k: v for k, v in saved.items() if isinstance(v, list)})
    return merged


def _scene_key(name):
    name = " ".join(re.sub(r"\b(mode|scene|setup|layout)\b", "", str(name or "").lower()).split())
    return name or "work"


def run_scene(name):
    all_scenes = scenes()
    key = _scene_key(name)
    if key not in all_scenes:
        close = [k for k in all_scenes if key in k or k in key]
        if not close:
            return ActionResult.not_found(f"I don't have a {key} mode. I have: "
                                          + ", ".join(sorted(all_scenes)) + ".")
        key = close[0]
    announce(f"Setting up {key} mode.", urgent=True)
    spoken = _L().RUNTIME.run_steps(all_scenes[key])
    if _L().RUNTIME.pending_plan:
        return ActionResult(Status.NEEDS_CONFIRMATION, " ".join(spoken[-1:]))
    return ActionResult.success(f"{key.title()} mode is ready.", steps=spoken)


def capture_layout():
    """The open apps and their workspaces, as scene tokens. Kayra and hidden windows excluded."""
    L = _L()
    tokens, seen = [], set()
    for window in sorted(L.list_windows(), key=lambda w: (str(w.workspace).zfill(3), w.focus_order)):
        if not str(window.workspace).isdigit():
            continue
        name = _app_name_for(window)
        if not name or (name, window.workspace) in seen:
            continue
        seen.add((name, window.workspace))
        tokens += [f"open {name}", f"system move window to workspace {window.workspace}"]
    return tokens


def _app_name_for(window):
    L = _L()
    for canonical, spec in L.linux_apps.LINUX_APPS.items():
        if window.classes & {c.lower() for c in spec["classes"]}:
            return spec["aliases"][0]
    for entry in L.linux_apps.INDEX.entries():
        if window.classes & entry.classes:
            return entry.name.lower()
    return None


def save_scene(name):
    tokens = capture_layout()
    if not tokens:
        return ActionResult.not_found("There's nothing open to remember.")
    key = _scene_key(name)
    saved = _load(SCENE_FILE, {})
    saved[key] = tokens
    if not _save(SCENE_FILE, saved):
        return ActionResult.failure("I couldn't save it.")
    return ActionResult.success(f"Saved {len(tokens) // 2} apps as {key}. Say \"{key} mode\" "
                                "to bring it back.")


def delete_scene(name):
    key = _scene_key(name)
    saved = _load(SCENE_FILE, {})
    if key not in saved:
        return ActionResult.not_found(f"I don't have a saved {key} mode." +
                                      (" Built-in modes can't be deleted." if key in BUILTIN_SCENES else ""))
    del saved[key]
    _save(SCENE_FILE, saved)
    return ActionResult.success(f"Forgot {key} mode.")


def list_scenes():
    names = sorted(scenes())
    return ActionResult.success("Modes: " + ", ".join(names) + ".")


def save_last_session():
    """Called at shutdown so "restore my last session" works tomorrow."""
    tokens = capture_layout()
    if tokens:
        saved = _load(SCENE_FILE, {})
        saved["last session"] = tokens
        _save(SCENE_FILE, saved)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                PLANS                                   │
# └────────────────────────────────────────────────────────────────────────┘

CATALOG = """open <app> | close <app> | focus <app> | open <website or url> | maximize | minimize |
fullscreen | snap left | snap right | system workspace <n|empty> | system move window to workspace <n|empty> |
system volume <0-100> | system mute | system brightness <0-100> | system do not disturb on|off |
system night light on|off | system power profile performance|balanced|power saver |
system bluetooth on|off | wifi on | system lock | pause | resume | next track | play <song> |
browser <switch to X tab | close X tab | click on <label> | summarize this page> |
meet <join CODE | mic off | camera off | share screen | leave> | email check | calendar today |
scene <name> | set timer <duration> | reminder <time> <text> | diagnose <scope> |
screen describe | take screenshot | google search <query> | battery | close tab | new tab"""

MAX_PLAN_STEPS = 10


def plan_goal(goal):
    """A model turns a goal into catalog tokens. Every token is validated before it runs."""
    names = ", ".join(sorted(scenes()))
    system = ("You are the planning module of Kayra, a desktop assistant. Turn the user's goal "
              "into a short sequence of commands from this catalog, in order:\n" + CATALOG +
              f"\nSaved modes you may use with 'scene <name>': {names}.\n"
              "Reply with ONLY a JSON array of command strings, at most 10. Never include "
              "shutdown, restart, delete, install or terminal commands. If nothing fits, reply [].")
    raw = ai.complete(system, goal, timeout=30.0) or "[]"   # verbatim: fences must survive
    match = re.search(r"\[.*\]", raw, re.S)
    try:
        tokens = json.loads(match.group(0)) if match else []
    except ValueError:
        tokens = []
    clean = []
    for token in tokens if isinstance(tokens, list) else []:
        token = " ".join(str(token).split())
        action = normalize_command(token)
        if action is None or action.domain in ("plan", "shell", "trigger"):
            continue
        if classify_action(action)[0] == Risk.DENY:
            continue
        clean.append(token)
        if len(clean) >= MAX_PLAN_STEPS:
            break
    return clean


def run_plan(goal):
    goal = (goal or "").strip()
    if not goal:
        return ActionResult.failure("What should I plan?")
    key = next((k for k in scenes() if re.search(rf"\b{re.escape(k)}\b", goal.lower())), None)
    announce("Let me work out the steps.", urgent=True)
    tokens = plan_goal(goal)
    if not tokens and key:
        tokens = [f"scene {key}"]
    if not tokens:
        return ActionResult.not_found("I couldn't turn that into steps I can do. Try asking for "
                                      "the steps one at a time.")
    announce("Here's the plan: " + "; then ".join(tokens) + ".", urgent=True)
    spoken = _L().RUNTIME.run_steps(tokens)
    if _L().RUNTIME.pending_plan:
        return ActionResult(Status.NEEDS_CONFIRMATION, " ".join(spoken[-1:]), {"steps": tokens})
    return ActionResult.success(f"Done — {len(tokens)} steps.", steps=tokens)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               TRIGGERS                                 │
# └────────────────────────────────────────────────────────────────────────┘

TRIGGER_FILE = "triggers.json"
GRACE_S = 60
_TIME = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)"


def parse_trigger(text):
    """'when X, Y' -> ({condition}, action_text) or (None, reason)."""
    low = (text or "").lower().strip(" .")
    rules = (
        (r"(?:when|once|after) (?:the |my |this )?downloads? (?:is |are )?(?:finish(?:es|ed)?|complete[sd]?|done)",
         lambda m: {"type": "download"}),
        (r"(?:when|if|once) (?:my )?battery (?:is |gets |drops |hits |goes |falls )?(?:below |under |to |at )?(\d{1,3})",
         lambda m: {"type": "battery_below", "value": int(m.group(1))}),
        (r"(?:when|once) (?:my )?(?:battery is |it'?s )?(?:fully )?charged(?: to (\d{1,3}))?",
         lambda m: {"type": "battery_above", "value": int(m.group(1) or 100)}),
        (r"(?:when|once) i unplug|(?:when|once) (?:the )?charger is (?:unplugged|removed)",
         lambda m: {"type": "unplugged"}),
        (r"(?:when|once) i plug in|(?:when|once) (?:it'?s |i'?m )?(?:plugged in|charging)",
         lambda m: {"type": "plugged"}),
        (r"(?:when|once) (?:i'?m |i am |we'?re )?(?:back )?online|(?:when|once) (?:the )?(?:internet|wifi) (?:is back|connects)",
         lambda m: {"type": "online"}),
        (r"every (?:day|morning|evening|night|weekday)? ?at " + _TIME,
         lambda m: {"type": "daily", "at": m.group(1), "weekdays": "weekday" in m.group(0)}),
        (r"^(?:at|by) " + _TIME, lambda m: {"type": "once_at", "at": m.group(1)}),
        (r"^in (\d+) (minutes?|hours?)", lambda m: {"type": "after", "seconds": int(m.group(1)) * (
            3600 if m.group(2).startswith("hour") else 60)}),
    )
    for pattern, build in rules:
        match = re.search(pattern, low)
        if match:
            condition = build(match)
            action = (low[:match.start()] + " " + low[match.end():]).strip(" ,.")
            action = re.sub(r"^(then|please|,)\s*", "", action).strip()
            return condition, action
    return None, "I can trigger on downloads finishing, battery level, plugging in or "\
                 "unplugging, coming online, a time of day, or a delay."


_ACTION_REWRITES = (
    (r"^(shut ?down|turn off|power off)( the| my)? (computer|pc|laptop)?$", "system shut down the computer"),
    (r"^(restart|reboot)( the| my)? (computer|pc|laptop)?$", "system restart the computer"),
    (r"^(go to sleep|sleep|suspend)( the computer)?$", "system sleep the computer"),
    (r"^lock( the screen| my computer)?$", "system lock"),
)


def action_tokens(action_text):
    """Turn the action half of a trigger into tokens, without a model when possible."""
    text = " ".join((action_text or "").lower().split())
    if not text:
        return []
    say = re.match(r"^(?:remind me to|tell me to|remind me|say|tell me|let me know)\s*(.*)$", text)
    if say:
        return [f"say {say.group(1) or 'it is time'}"]
    for pattern, token in _ACTION_REWRITES:
        if re.match(pattern, text):
            return [token]
    # "system <text>" first: "switch to power saver" is a setting, and read bare it would be
    # the window command "switch to <app>".
    system = normalize_command(f"system {text}")
    if system is not None and not (system.domain == "system" and system.action == "raw"):
        return [f"system {text}"]
    for candidate in (text, f"scene {text}" if "mode" in text else ""):
        if not candidate:
            continue
        action = normalize_command(candidate)
        if action is not None and not (action.domain == "system" and action.action == "raw") \
                and not (action.domain == "app" and action.action == "open" and " " in text
                         and not candidate.startswith("open")):
            return [candidate]
    return plan_goal(text)


def add_trigger(text, approved=False):
    condition, action_text = parse_trigger(text)
    if condition is None:
        return ActionResult.failure(action_text)
    tokens = action_tokens(action_text)
    if not tokens:
        return ActionResult.failure("What should I do when that happens?")
    needs_yes = False
    for token in tokens:
        if token.startswith("say "):
            continue
        action = normalize_command(token)
        verdict = classify_action(action)[0] if action else Risk.DENY
        if verdict == Risk.DENY:
            return ActionResult.blocked(f"I can't schedule \"{token}\" — it isn't allowed.")
        needs_yes |= verdict == Risk.CONFIRM
    if needs_yes and not approved:
        from kayra.automation.policy import Action
        pending = Action("trigger", "add", target=text, parameters={"approved": True})
        prompt = (f"When that happens I'll {_describe_tokens(tokens)}, with a one-minute warning. "
                  "Should I set that up?")
        _L().RUNTIME.confirmations.request(pending, prompt)
        return ActionResult(Status.NEEDS_CONFIRMATION, prompt)
    if condition["type"] in ("once_at", "daily"):
        when = _clock(condition["at"])
        if when is None:
            return ActionResult.failure("I didn't catch the time.")
        condition["at"] = when.strftime("%H:%M")
    if condition["type"] == "after":
        condition["due"] = time.time() + condition.pop("seconds")
    triggers = _load(TRIGGER_FILE, [])
    triggers.append({"id": int(time.time() * 1000) % 10 ** 9, "text": text, "condition": condition,
                     "tokens": tokens, "approved": bool(needs_yes), "created": time.time(),
                     "last_fired": None, "state": {}})
    _save(TRIGGER_FILE, triggers)
    ensure_watcher()
    return ActionResult.success(f"Okay. {_describe_condition(condition)}, I'll "
                                f"{_describe_tokens(tokens)}.")


def list_triggers():
    triggers = _load(TRIGGER_FILE, [])
    if not triggers:
        return ActionResult.success("You have no automations set.")
    parts = [f"{_describe_condition(t['condition'])}, {_describe_tokens(t['tokens'])}"
             for t in triggers[:5]]
    return ActionResult.success(f"{len(triggers)} automation{'s' if len(triggers) != 1 else ''}: "
                                + "; ".join(parts) + ".")


def cancel_trigger(text):
    triggers = _load(TRIGGER_FILE, [])
    low = (text or "").lower()
    if not triggers:
        return ActionResult.not_found("There are no automations to cancel.")
    if re.search(r"\ball\b|everything", low):
        _save(TRIGGER_FILE, [])
        return ActionResult.success(f"Cancelled all {len(triggers)} automations.")
    words = [w for w in re.findall(r"[a-z0-9]{3,}", low) if w not in ("the", "trigger", "automation", "one")]
    keep, dropped = [], []
    for trigger in triggers:
        (dropped if words and any(w in trigger["text"].lower() for w in words) else keep).append(trigger)
    if not dropped:
        dropped, keep = [triggers[-1]], triggers[:-1]
    _save(TRIGGER_FILE, keep)
    return ActionResult.success(f"Cancelled: {dropped[0]['text']}.")


def _clock(text):
    from kayra.automation.web_macros import parse_when
    return parse_when(f"at {text}")


def _describe_condition(c):
    kind = c.get("type")
    return {"download": "When your download finishes",
            "battery_below": f"When the battery drops to {c.get('value')} percent",
            "battery_above": f"When the battery reaches {c.get('value')} percent",
            "unplugged": "When you unplug", "plugged": "When you plug in",
            "online": "When you're back online",
            "daily": f"Every {'weekday' if c.get('weekdays') else 'day'} at {c.get('at')}",
            "once_at": f"At {c.get('at')}",
            "after": "When the time is up"}.get(kind, "When that happens")


def _describe_tokens(tokens):
    words = []
    for token in tokens:
        token = re.sub(r"^system ", "", token)
        token = re.sub(r"^say ", "remind you: ", token)
        words.append(token)
    return ", then ".join(words)


def _condition_met(trigger, now, snapshot):
    c, state = trigger["condition"], trigger.setdefault("state", {})
    kind = c["type"]
    battery = snapshot.get("battery") or {}
    if kind == "download":
        active = snapshot.get("downloading", False)
        if active:
            state["seen_active"] = True
            return False
        return bool(state.get("seen_active"))
    if kind == "battery_below":
        return battery.get("percent") is not None and battery["percent"] <= c["value"] \
            and battery.get("state") != "charging"
    if kind == "battery_above":
        return battery.get("percent") is not None and battery["percent"] >= c["value"]
    if kind in ("unplugged", "plugged"):
        plugged = bool(battery.get("ac_online"))
        previous = state.get("plugged")
        state["plugged"] = plugged
        return previous is not None and previous != plugged and plugged == (kind == "plugged")
    if kind == "online":
        online = snapshot.get("online", True)
        previous = state.get("online")
        state["online"] = online
        return previous is False and online
    if kind == "after":
        return now >= c.get("due", now + 1)
    if kind in ("once_at", "daily"):
        hour, minute = (int(x) for x in c["at"].split(":"))
        today = datetime.datetime.fromtimestamp(now)
        if kind == "daily" and c.get("weekdays") and today.weekday() >= 5:
            return False
        due = today.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
        last = trigger.get("last_fired") or trigger.get("created", 0)
        return due <= now < due + 300 and last < due
    return False


def _snapshot():
    from kayra import platforms
    downloads = os.path.expanduser("~/Downloads")
    try:
        downloading = any(n.endswith((".crdownload", ".part", ".download", ".partial"))
                          for n in os.listdir(downloads))
    except OSError:
        downloading = False
    from kayra.automation import diagnostics
    return {"battery": platforms.battery_status() or {}, "downloading": downloading,
            "online": diagnostics.default_gateway() is not None}


def evaluate_triggers(now=None, snapshot=None):
    """Fires every trigger whose condition is met. Returns the spoken lines. Watcher-called."""
    now = now or time.time()
    triggers = _load(TRIGGER_FILE, [])
    if not triggers:
        return []
    snapshot = snapshot if snapshot is not None else _snapshot()
    fired, keep = [], []
    for trigger in triggers:
        met = _condition_met(trigger, now, snapshot)
        if met:
            fired.append(trigger)
            trigger["last_fired"] = now
            trigger["state"] = {}
            if trigger["condition"]["type"] == "daily":
                keep.append(trigger)
        else:
            keep.append(trigger)
    _save(TRIGGER_FILE, keep)
    lines = []
    for trigger in fired:
        lines.append(_fire(trigger))
    return lines


def _fire(trigger):
    L = _L()
    tokens = trigger["tokens"]
    says = [t[4:] for t in tokens if t.startswith("say ")]
    actions = [t for t in tokens if not t.startswith("say ")]
    line = ""
    if says:
        line = "Reminder: " + "; ".join(says) + "."
        announce(line)
    if not actions:
        return line
    risky = any(classify_action(normalize_command(t))[0] == Risk.CONFIRM for t in actions
                if normalize_command(t))
    if risky and trigger.get("approved"):
        warning = (f"{_describe_condition(trigger['condition'])} — in one minute I'll "
                   f"{_describe_tokens(actions)}. Say cancel timer to stop me.")
        announce(warning, urgent=True)

        def go(_label):
            for token in actions:
                action = normalize_command(token)
                if action is not None:
                    L.RUNTIME.execute_action(action, skip_policy=True)
        L.RUNTIME.timers.add(GRACE_S, "scheduled " + _describe_tokens(actions), on_fire=go)
        return warning
    spoken = L.RUNTIME.run_steps(actions)
    line = " ".join(spoken)
    if line:
        announce(f"{_describe_condition(trigger['condition'])}: {line}")
    return line


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE WATCHER                               │
# └────────────────────────────────────────────────────────────────────────┘

TICK_S = 20
HEALTH_EVERY_S = 60
_WATCHER = {"thread": None, "stop": threading.Event(), "health_at": 0.0}


def health_watch_enabled():
    return os.environ.get("KAYRA_HEALTH_WATCH", "1").strip().lower() not in ("0", "false", "off", "no")


def ensure_watcher():
    if _WATCHER["thread"] is not None and _WATCHER["thread"].is_alive():
        return True
    _WATCHER["stop"].clear()
    thread = threading.Thread(target=_watch_loop, name="kayra-watch", daemon=True)
    _WATCHER["thread"] = thread
    thread.start()
    return True


def stop_watcher():
    _WATCHER["stop"].set()
    _WATCHER["thread"] = None


def _watch_loop():
    stop = _WATCHER["stop"]
    while not stop.wait(TICK_S):
        try:
            evaluate_triggers()
        except Exception:
            pass
        now = time.time()
        if health_watch_enabled() and now - _WATCHER["health_at"] >= HEALTH_EVERY_S:
            _WATCHER["health_at"] = now
            try:
                from kayra.automation import diagnostics
                for sentence in diagnostics.health_alerts(now):
                    announce(sentence)
            except Exception:
                pass


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         BRIEFING AND RECALL                            │
# └────────────────────────────────────────────────────────────────────────┘

def weather():
    try:
        import requests
        place = os.environ.get("KAYRA_WEATHER_LOCATION", "").replace(" ", "+")
        reply = requests.get(f"https://wttr.in/{place}?format=%C,+%t,+feels+like+%f",
                             timeout=4, headers={"User-Agent": "curl/8"})
        text = reply.text.strip()
        return text if reply.ok and len(text) < 80 and "unknown" not in text.lower() else ""
    except Exception:
        return ""


def briefing():
    from kayra.automation import diagnostics, web_macros
    L = _L()
    now = datetime.datetime.now()
    facts = [f"Time: {now:%A %d %B, %-I:%M %p}"]
    sky = weather()
    if sky:
        facts.append(f"Weather: {sky}")
    if web_macros.BRIDGE.connected():
        try:
            agenda = web_macros.calendar_agenda("today")
            facts.append(f"Calendar: {agenda.message}")
            mail = web_macros.email_check()
            facts.append(f"Email: {mail.message}")
        except Exception:
            pass
    facts.append(L.battery_sentence())
    findings = diagnostics.probe_crashes(days=1) + diagnostics.probe_disk()[:2]
    issues = [f.text for f in findings if f.level != diagnostics.OK]
    if issues:
        facts.append("Issues: " + "; ".join(issues))
    triggers = _load(TRIGGER_FILE, [])
    if triggers:
        facts.append(f"{len(triggers)} automations are scheduled")
    greeting = ("Good evening" if now.hour < 5 or now.hour >= 17 else
                "Good morning" if now.hour < 12 else "Good afternoon")
    fallback = f"{greeting}. " + " ".join(f if f.endswith(".") else f + "." for f in facts)
    spoken = ai.summarize(
        "You are Kayra, a composed, precise and quietly confident assistant. Give the user their "
        f"briefing, opening with '{greeting}'. Use only these facts, in 4 to 6 spoken sentences, "
        "most useful first. No markdown.", "\n".join(facts), fallback=fallback, max_chars=900)
    return ActionResult.success(spoken)


def explain_last(text=""):
    from kayra.automation.policy import AUDIT_SUCCESS, AUDIT_BLOCKED, AUDIT_CONFIRM
    entries = [e for e in recent_audit(30) if e.get("action") and e["action"].split(".")[0]
               not in ("info",) and e["action"] != "system.recall"]
    if not entries:
        return ActionResult.success("I haven't done anything on the computer yet.")
    if re.search(r"why", (text or "").lower()):
        blocked = next((e for e in reversed(entries) if e["event"] in (AUDIT_BLOCKED, AUDIT_CONFIRM)), None)
        if blocked:
            return ActionResult.success(f"I didn't just do {blocked['action'].replace('.', ' ')} "
                                        f"because {blocked.get('reason', 'it needed your say-so')}.")
    done = [e for e in entries if e["event"] == AUDIT_SUCCESS][-3:]
    if not done:
        last = entries[-1]
        outcome = last["event"].replace("AUTOMATION_", "").replace("_", " ").lower()
        return ActionResult.success(f"The last thing I tried was {last['action'].replace('.', ' ')}"
                                    f" on {last.get('target')}: {outcome}.")
    parts = [f"{e['action'].split('.', 1)[1].replace('_', ' ')}"
             + (f" {e['target']}" if e.get("target") not in (None, "", "current") else "")
             for e in done]
    return ActionResult.success("Most recently I did: " + ", then ".join(parts) + ".")


def in_call():
    """True when a call is likely running: a non-Kayra app is recording from the microphone."""
    L = _L()
    code, text = L._run("pactl", "-f", "json", "list", "source-outputs", timeout=2.0)
    if code != 0:
        return False
    try:
        streams = json.loads(text)
    except ValueError:
        return False
    try:
        from kayra.automation import targets
        owned = set(targets.kayra_owned_pids()) | {os.getpid()}
    except Exception:
        owned = {os.getpid()}
    for stream in streams:
        props = stream.get("properties") or {}
        try:
            pid = int(props.get("application.process.id", 0))
        except (TypeError, ValueError):
            pid = 0
        name = (props.get("application.name") or "").lower()
        if pid in owned or "kayra" in name or "peak detect" in name:
            continue
        return True
    return False
