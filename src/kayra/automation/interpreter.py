# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/interpreter.py                        │
# │   When the rules don't understand a request, ask a model what you meant │
# └────────────────────────────────────────────────────────────────────────┘
"""
Kayra understands requests in two layers.

1. THE FAST LAYER (rules). The DMM — a model — already decides whether an utterance is chat,
   a web question or an automation task, and the automation parser turns its tokens into
   structured actions with tables and patterns. That layer is instant, works offline and is
   exact for everything it knows.

2. THE UNDERSTANDING LAYER (this module). When the fast layer cannot make sense of a
   request — no parser matched, the target was not found, or the parser was only guessing —
   a model is asked to UNDERSTAND it like a person would: given what the user said, what is
   on the screen (the app in front, the open tab), and the full list of things Kayra can do,
   it answers with one of:

       {"kind": "automation", "commands": ["open google sheets in this tab", ...]}
       {"kind": "chat"}                         it is a question or conversation, not a task
       {"kind": "ask", "question": "..."}       genuinely unclear: ask the user one question
       {"kind": "none"}                         Kayra cannot do it

   "open XYZ chat" while WhatsApp is in front becomes a click on that chat; "can you make the
   tab quiet" becomes `browser mute this tab`; "what's a good name for my cat" becomes chat.

The safety rule does not move: the model PROPOSES, it never authorises. Every command it
returns is parsed by `normalize_command` like a spoken one, judged by `classify_action`
(DENY is dropped here, CONFIRM still asks the user), and runs through the same executor.
Commands from this module are never re-interpreted, so there is no loop, and at most
`MAX_COMMANDS` run. Shell, plans and triggers are never accepted from it.

Cost: one bounded model call (local first, then the cloud chain), ONLY on the failure path —
everything the rules already handle never pays it. `AUTOMATION_AI_INTERPRETER=off` in `.env`
turns it off.
"""

import os
import re
import json

from kayra.automation import ai

MAX_COMMANDS = 4
TIMEOUT_S = 8.0
NEVER_FROM_MODEL = frozenset({"shell", "plan", "trigger"})
_POWER = frozenset({"shutdown", "restart", "sign_out", "sleep", "hibernate", "logout",
                    "reboot", "suspend"})
ACTIVE = True       # the unit-test rigs switch this off so no suite ever reaches a model

# What Kayra can do, as the commands the parser accepts. Kept in sync with the parser by the
# test suite: every example here must normalize to an action.
CATALOG = """\
APPS & WINDOWS: open <app> | close <app> | focus <app> | maximize | minimize | fullscreen |
  snap left | snap right | system workspace <n> | system move window to workspace <n>
WEBSITES: open <site or url> [in this tab | in a new tab | in a new window | in incognito |
  next to this tab | in the browser] | google search <query> | youtube search <query> |
  new tab | close tab | next tab | previous tab | reopen tab | go back | go forward | refresh
INSIDE THE BROWSER PAGE (also inside web apps like WhatsApp, YouTube, X):
  browser click on <visible label or name> | browser click on the second <label> |
  browser type <text> into <field> | browser search this page for <text> |
  browser scroll down | browser scroll up | browser scroll to <text> |
  browser scroll the <mail | chat | page> to the <top | bottom> | browser scroll the chat list down |
  browser search for <text> in the search bar | browser close the search |
  browser read this page | browser summarize this page | browser find <text> on this page |
  browser what can i click | browser list tabs | browser switch to <site> tab |
  browser close <site> tab | browser mute this tab | browser pin this tab |
  browser switch to the third tab | browser switch to the last tab |
  browser switch to the tab on the right | browser close the second tab |
  browser close duplicate tabs | browser close tabs to the right |
  browser mute everything except this tab | browser what did i just close |
  browser reopen the <site> tab i closed | browser free up memory in chrome |
  browser find the tab about <topic> | browser hard reload | browser zoom to <n> percent |
  browser copy the link | browser copy the page title |
  browser open the third result | browser play the second video |
  browser open the first result in a new tab | browser what are the results |
  browser find <text> on this page | browser next match | browser previous match |
  browser clear highlights | browser close all <site> tabs | browser close all but pinned |
  browser group my <topic> tabs | browser group the <site> tabs as <name> |
  browser list my tab groups | browser add this tab to the <name> group |
  browser rename the <name> group to <new name> | browser make the <name> group <colour> |
  browser collapse the <name> group | browser expand all groups |
  browser ungroup the <name> group | browser close the <name> group |
  browser move this tab to a new window | browser move this tab to the other screen |
  browser merge my chrome windows |
  browser what does this page say about <topic> | browser read the comments |
  browser summarize the reviews | browser summarize this video |
  browser translate this page to <language> | browser explain the selected text |
  browser translate the selected text to <language> | browser bookmark this page |
  browser bookmark this page in <folder> | browser search my bookmarks for <text> |
  browser delete the bookmark for <name> | browser clear today's history |
  browser clear the last hour of history | browser what did i visit today |
  browser what did i download | browser open my last download | browser show it in folder |
  browser pause the download | browser resume the download | browser cancel the download |
  browser close it | browser go back to it | browser search for <text> here |
  browser what can i do here |
  browser open my <name> sheet | browser open the doc about <topic> | browser open my <name> deck |
  browser search my docs for <text> | browser search my drive for <text> |
  browser find the pdf <name> sent me | browser create a new sheet called <title> |
  browser create a new doc | browser go to cell <B12> | browser what's the total of column <C> |
  browser what is the average of the <header> column | browser read row <n> |
  browser what are the columns | browser share this doc with <email> |
  browser start the slideshow | browser next slide | browser previous slide |
  browser end the slideshow | browser open my recent files | browser download this file |
  browser add a note saying <text> | browser open the responses of my <name> form |
  browser what assignments are due |
  browser open the page i read yesterday about <topic> | browser bookmark <name>
FORMS (decision D2: the user's saved details; Kayra never presses submit unless asked):
  browser fill this form with my details | browser select <option> in the <field> dropdown |
  browser choose <option> for <field> | browser tick <label> | browser untick <label> |
  browser set the <field> date to <date> | browser fill the <field> field with my <email | phone> |
  browser submit this form | browser press <enter | tab | escape | down arrow> |
  browser upload my <resume | file name> here | browser remember my <field> is <value> |
  browser what details do you have for forms | browser forget my <field>
VIDEO (YouTube and any page with a video): browser skip the ad | browser speed <1.5> |
  browser normal speed | browser skip <10> seconds | browser go back <30> seconds |
  browser go to <2:30> | browser turn on captions | browser next video | browser like this video |
  browser subscribe to this channel | browser save this to watch later |
  browser make the video full screen | browser theater mode | browser pause the video |
  browser how much is left in this video
SITES & SHOPPING (search and open only — never add to a cart, buy or pay):
  browser search <amazon | flipkart | github | wikipedia | reddit | site> for <query> |
  browser search amazon for <product> under <price> | browser images of <thing> |
  browser sort by price low to high | browser show only under <price> | browser how much is this |
  browser what's the rating
EMAIL (Gmail): email search for <text> | email show unread from this week | email compose |
  email open my <name> label | email check | email open latest | email open the email from <name> |
  email read latest | email read this | email go to sent | email reply | email archive |
  email draft to <address> about <subject> saying <text> | email send
CALENDAR / MEET: calendar next meeting | calendar join my next meeting | calendar friday |
  meet admit everyone | calendar today | calendar tomorrow | calendar add <event> <day> at <time> |
  meet join <code the user said> | meet join new | meet mic off | meet camera off |
  meet share screen | meet share the <app> window | meet share this tab |
  meet share the <site> tab | meet share my second screen | meet share screen with audio |
  meet share <app> instead | meet am i sharing | meet stop sharing | meet leave
MESSAGES (WhatsApp): message <contact>: <text> | message open <chat or group> | message search for <name> | message open result <n> |
  message close this chat |
  message read <chat> | message read <chat> today | message read this | message who messaged me
MEDIA & SYSTEM: play <song> | pause | resume | next track | previous track |
  system volume <0-100> | system mute | system brightness <0-100> | wifi on | wifi off |
  system bluetooth on | system do not disturb on | system night light on | battery |
  take screenshot | screen describe | set timer <duration> | reminder <time> <text>
TYPING & KEYS: type <text> | copy | paste | undo | redo | select all | save | enter | escape
"""

_SYSTEM = """You are the understanding module of Kayra, a voice assistant that controls the \
user's Linux desktop and their Chrome browser. A request reached you because Kayra's fast rules \
could not handle it. Work out what the person MEANT, like a thoughtful human assistant would, \
using what is on their screen.

Kayra can run these commands (the exact command shapes, <...> is filled in):
{catalog}
Reply with ONLY one JSON object, no prose:
  {{"kind": "automation", "commands": ["<command>", ...]}}  -- at most {limit}, in order
  {{"kind": "chat"}}       -- a question, opinion or conversation, not a task on the computer
  {{"kind": "ask", "question": "<one short question>"}}  -- truly ambiguous; ask ONE thing
  {{"kind": "none"}}       -- a task Kayra has no command for

Rules:
- Use the screen context. If a web app or site is in front (WhatsApp, Gmail, YouTube...), \
"open X", "click X", "go to X" usually mean something INSIDE that page: in WhatsApp use \
"message open X", elsewhere "browser click on X", with the name as the person said it.
- Keep names and text exactly as the person said them. Never invent email addresses, \
contacts, file names or URLs.
- Never output shutdown, restart, delete, install, terminal or shell commands.
- Prefer one command when one is enough.
- A factual question or anything to just answer is {{"kind": "chat"}}.

Examples:
Said: "open PR and outreach core team chat" | in front: web.whatsapp.com
-> {{"kind": "automation", "commands": ["message open PR and outreach core team"]}}
Said: "open the design team 2026 group" | in front: web.whatsapp.com
-> {{"kind": "automation", "commands": ["message open design team 2026"]}}
Said: "can you make the tab quiet" | active tab: Lo-fi - YouTube
-> {{"kind": "automation", "commands": ["browser mute this tab"]}}
Said: "put google sheets in this tab" | active tab: New Tab
-> {{"kind": "automation", "commands": ["open google sheets in this tab"]}}
Said: "start screen sharing of this meet and share the LM Studio window" | active tab: Meet
-> {{"kind": "automation", "commands": ["meet share the lm studio window"]}}
Said: "who invented the telephone"
-> {{"kind": "chat"}}
Said: "open the budget thing"
-> {{"kind": "ask", "question": "Do you mean a Google Sheet, a Doc or a file called budget?"}}
Copy names EXACTLY as said, every word ("PR", "and", numbers), minus the words chat/group."""


def system_prompt():
    """The understanding layer's system prompt — one stable string, so it stays cached."""
    return _SYSTEM.format(catalog=CATALOG, limit=MAX_COMMANDS)


def enabled():
    try:
        from kayra.core.config import env
        value = env("AUTOMATION_AI_INTERPRETER", "on")
    except Exception:
        value = os.environ.get("AUTOMATION_AI_INTERPRETER", "on")
    return str(value or "on").strip().lower() not in ("0", "off", "false", "no", "disabled")


class Interpretation:
    __slots__ = ("kind", "commands", "question", "raw")

    def __init__(self, kind, commands=(), question="", raw=""):
        self.kind, self.commands, self.question, self.raw = kind, list(commands), question, raw

    def __repr__(self):
        return f"<Interpretation {self.kind} {self.commands} {self.question!r}>"


def describe_situation(situation):
    """The screen context as two or three plain lines for the model."""
    if not situation:
        return "Screen: unknown."
    lines = []
    if situation.get("app"):
        lines.append(f"App in front: {situation['app']}" +
                     (f" (window title: {situation['window_title']})" if situation.get("window_title") else ""))
    if situation.get("site"):
        lines.append(f"Web app / site in front: {situation['site']}")
    if situation.get("tab_title") or situation.get("tab_url"):
        lines.append(f"Active Chrome tab: {situation.get('tab_title', '')} <{situation.get('tab_url', '')}>")
    lines.append("Chrome extension connected: " + ("yes" if situation.get("bridge") else "no"))
    return "\n".join(lines)


def _parse(raw):
    match = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# Part 6: a model may never write, forget or submit the user's details, or pick a file to upload
# — those come from the user's own words only.
_USER_ONLY = frozenset({"profile_set", "profile_forget", "profile_clear", "form_submit", "file_upload"})


def validate(commands, normalize, classify):
    """Only commands the parser understands and the policy does not forbid. Order kept."""
    from kayra.automation.policy import Risk
    clean = []
    for command in commands if isinstance(commands, list) else []:
        command = " ".join(str(command or "").split()).strip(" .")
        if not command or "," in command:              # DMM tokens never carry commas
            continue
        action = normalize(command)
        if action is None or action.domain in NEVER_FROM_MODEL:
            continue
        if action.domain == "system" and (action.action in _POWER or
                                          action.action.startswith("power")):
            continue                                   # power is only ever the user's words
        if action.domain in ("file", "files") and action.action in ("delete", "move", "rename"):
            continue
        if action.domain == "browser" and action.action == "page_click" and action.confidence < 1.0:
            continue                                   # the parser's own guess, not a command
        if action.domain == "browser" and action.action in _USER_ONLY:
            continue                                   # the user's own details and decisions
        if classify(action)[0] == Risk.DENY:
            continue
        clean.append(command)
        if len(clean) >= MAX_COMMANDS:
            break
    return clean


def interpret(request, situation=None, reason="", normalize=None, classify=None):
    """
    What did the user mean? Returns an Interpretation; kind "none" when the model is
    unreachable, disabled, or proposed nothing valid. Never raises.
    """
    if not enabled() or not str(request or "").strip():
        return Interpretation("none")
    if normalize is None or classify is None:
        from kayra.automation.common import normalize_command
        from kayra.automation.policy import classify_action
        normalize, classify = normalize or normalize_command, classify or classify_action
    user = f"The person said: \"{request.strip()}\"\n{describe_situation(situation)}"
    if reason:
        user += f"\nKayra's rules tried and got: {reason}"
    if not ACTIVE:
        return Interpretation("none")
    # Thinking OFF: this is a one-line decision, and a thinking model spends ~85 % of its
    # time reasoning before it — measured 4 s -> 0.5 s on the reference laptop.
    raw = ai.fast(system_prompt(), user, timeout=TIMEOUT_S,
                  max_tokens=160)
    data = _parse(raw)
    if not data:
        return Interpretation("none", raw=raw)
    kind = str(data.get("kind", "")).lower()
    if kind == "chat":
        return Interpretation("chat", raw=raw)
    if kind == "ask" and str(data.get("question", "")).strip():
        return Interpretation("ask", question=" ".join(str(data["question"]).split())[:200], raw=raw)
    if kind == "automation":
        commands = validate(data.get("commands"), normalize, classify)
        if commands:
            return Interpretation("automation", commands, raw=raw)
        return Interpretation("none", raw=raw)
    if kind not in ("none", "ask"):
        # A shape outside the contract ("google search", "answer"…) means the model wanted to
        # ANSWER something, not run a command: the safe reading is conversation.
        return Interpretation("chat", raw=raw)
    return Interpretation("none", raw=raw)
