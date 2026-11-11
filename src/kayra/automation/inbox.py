# ┌────────────────────────────────────────────────────────────────────────┐
# │                          automation/inbox.py                           │
# │   One inbox across apps (H4), and what to say when something arrives   │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §4 H4 (Part 10) and §12 M4.1 (Part 9).

* **"Who messaged me anywhere?"** — WhatsApp's unread chats (only when WhatsApp is ALREADY open:
  reading an inbox never opens an app), Gmail's unread count (from its open tab's title), and the
  messaging apps in Kayra's notification log (Telegram, Signal, Slack, Discord, Teams, Instagram,
  LinkedIn, …) from the last few hours, grouped by app and named by sender.
* **"Reply to Rahul saying …"** — goes to the app his LAST message came from: the newest
  notification naming him decides, else his WhatsApp chat. WhatsApp -> the normal send (it asks,
  naming the chat). Email -> the newest email from him is opened and the reply written; Kayra
  never sends it from here. Any other app -> said honestly.
* **When something arrives** (`on_notification`, called by the notification log): a message ->
  "Ana messaged on Telegram." (sender and app only, never the text — someone else may be in the
  room); a calendar reminder -> "Standup is starting. Say join my next meeting." Both go through
  `common.announce`, which holds them during a turn, a call, a screen share or standby, and both
  are rate-limited. "Stop telling me about new messages" turns the message offers off.

Nothing here reaches a model; message text is never logged.
"""

import re
import time

CLOCK = time.time
RECENT_S = 6 * 3600                 # "anywhere" looks at the notification log's last six hours
OFFER_GAP_S = 600                   # the same sender in the same app is offered at most every 10 min
OFFERS_PER_HOUR = 6
STATE = {"messages": None, "offered": {}, "times": []}     # None = the .env default

_MESSAGING = (("whatsapp", "WhatsApp"), ("telegram", "Telegram"), ("signal", "Signal"), ("slack", "Slack"),
              ("discord", "Discord"), ("teams", "Teams"), ("instagram", "Instagram"), ("messenger", "Messenger"),
              ("linkedin", "LinkedIn"), ("thunderbird", "email"), ("geary", "email"), ("evolution", "email"),
              ("gmail", "Gmail"), ("mail.google.com", "Gmail"), ("web.whatsapp.com", "WhatsApp"),
              ("web.telegram.org", "Telegram"))
_CALENDAR = re.compile(r"calendar\.google\.com|google calendar|\bstarts? (?:in|at|now)\b|\bstarting (?:now|in)\b|"
                       r"\bin \d{1,2} min(?:ute)?s?\b|meet\.google\.com|\bevent reminder\b", re.I)


def _W():
    from kayra.automation import web_macros
    return web_macros


def _notes():
    from kayra.automation import linux_system
    with linux_system._NOTES["lock"]:
        return list(linux_system._NOTES["items"])


def app_of(note):
    """The messaging app a notification is from ('WhatsApp', 'Telegram', …), or None."""
    text = f"{note.get('app', '')} {note.get('body', '')}".lower()
    return next((label for needle, label in _MESSAGING if needle in text), None)


def _setting(name, default):
    try:
        from kayra.core.config import env
        value = env(name, default)
    except Exception:
        value = default
    return str(value or default).strip().lower() not in ("0", "off", "false", "no", "disabled")


def inbox_all():
    """'who messaged me anywhere' — one sentence across the open apps and the notification log."""
    from kayra.automation.policy import ActionResult
    W = _W()
    parts = []
    unread = W.whatsapp_unread()
    if unread:
        parts.append("WhatsApp: " + W._join([f"{n} ({c})" for n, c in unread[:5]]) +
                     (f", and {len(unread) - 5} more" if len(unread) > 5 else ""))
    mails = W.gmail_unread()
    if mails:
        parts.append(f"Gmail: {mails} unread")
    since = CLOCK() - RECENT_S
    by_app = {}
    for note in reversed(_notes()):
        app = app_of(note)
        if not app or note.get("at", 0) < since or app == "WhatsApp" and unread or app == "Gmail" and mails is not None:
            continue
        who = str(note.get("summary") or "").strip()[:40]
        if who and who not in by_app.setdefault(app, []):
            by_app[app].append(who)
    for app, people in by_app.items():
        parts.append(f"{app}: " + W._join(people[:4]) + (f", and {len(people) - 4} more" if len(people) > 4 else ""))
    if not parts:
        opened = [label for host, label in (("web.whatsapp.com", "WhatsApp"), ("mail.google.com", "Gmail"))
                  if W.open_tab_on(host) is not None]
        where = f" in {W._join(opened)} or" if opened else ""
        return ActionResult.success(f"Nothing new{where} in your notifications.")
    return ActionResult.success(". ".join(parts) + ".")


def last_source(who):
    """('WhatsApp' | 'Gmail' | 'email' | <app>, the name as that app shows it) for where `who` last
    wrote from, or (None, None)."""
    from kayra.automation import semantic_match as S
    for note in reversed(_notes()):
        app = app_of(note)
        if app and S.lexical(who, str(note.get("summary") or "")) >= 0.5:
            return app, str(note.get("summary")).strip()
    rows = _W().whatsapp_rows()
    if rows:
        decision = S.match(who, [S.Candidate(r["name"], recent=i) for i, r in enumerate(rows)])
        if decision.decision == S.PICK:
            return "WhatsApp", decision.best.name
    return None, None


def reply_anywhere(who, text, action=None):
    """H4: 'reply to Rahul saying I'm on my way' — in the app his last message came from."""
    from kayra.automation.policy import Action, ActionResult
    W = _W()
    if not who or not text:
        return ActionResult.failure("Who should I reply to, and what should I say?")
    app, name = last_source(who)
    if app is None:
        return ActionResult.not_found(f"I haven't seen a message from {who} lately. Say which app — for example "
                                      f"\"reply to {who} on WhatsApp saying …\".")
    if app == "WhatsApp":
        send = Action("message", "send", target=name, parameters={"text": text, "app": "whatsapp"},
                      raw=action.raw if action else "")
        return W.message_send(name, text, None, send)
    if app in ("Gmail", "email"):
        return W.reply_by_email(who, text)
    return ActionResult.unsupported(f"{name}'s last message came from {app}. I can reply on WhatsApp and Gmail "
                                    "so far — it's open for you there.")


def set_message_offers(on):
    from kayra.automation.policy import ActionResult
    STATE["messages"] = bool(on)
    return ActionResult.success("I'll tell you when a message arrives." if on else
                                "Okay, I won't tell you about new messages.")


def _offers_on():
    return STATE["messages"] if STATE["messages"] is not None else _setting("INBOX_ANNOUNCE", "on")


def on_notification(note):
    """Called for every desktop notification the log records. Speaks at most one short line."""
    try:
        from kayra.automation.common import announce
    except Exception:
        return None
    text = f"{note.get('summary', '')} {note.get('body', '')}"
    now = CLOCK()
    STATE["times"] = [t for t in STATE["times"] if now - t < 3600]
    if len(STATE["times"]) >= OFFERS_PER_HOUR:
        return None
    app = app_of(note)
    if app is None and _CALENDAR.search(text) and _setting("MEETING_REMINDERS", "on"):
        title = re.sub(r"\s+", " ", str(note.get("summary") or "Your meeting")).strip()[:60]
        key = ("calendar", title)
        if now - STATE["offered"].get(key, 0) < OFFER_GAP_S:
            return None
        STATE["offered"][key] = now
        STATE["times"].append(now)
        line = f"{title} is starting. Say \"join my next meeting\"."
        announce(line)
        return line
    if app is None or not _offers_on():
        return None
    who = re.sub(r"\s+", " ", str(note.get("summary") or "")).strip()[:40]
    if not who:
        return None
    key = (app, who.lower())
    if now - STATE["offered"].get(key, 0) < OFFER_GAP_S:
        return None
    STATE["offered"][key] = now
    STATE["times"].append(now)
    line = f"{who} messaged on {app}."
    announce(line)
    return line
