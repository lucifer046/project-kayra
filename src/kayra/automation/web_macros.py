# ┌────────────────────────────────────────────────────────────────────────┐
# │                       automation/web_macros.py                         │
# │     Voice control INSIDE Chrome: tabs, pages, Gmail, Calendar, Meet    │
# └────────────────────────────────────────────────────────────────────────┘
"""
Everything Kayra does inside the user's own Chrome, through the Kayra Bridge extension
(`automation.browser_bridge`). Every function takes an already-parsed `Action` field set and
returns an `ActionResult` with ONE spoken sentence.

Sites are driven by what a person sees — button labels and field names — not by CSS classes
that change every release. "Click Join now" means the button whose accessible name is "Join
now". When a site renames a button, the generic "browser click on <label>" still works.

Without the extension: tab and page actions say how to connect it ("say: browser set up");
plain navigation (open Gmail, compose by URL) still works through `xdg-open`.

Safety: the policy confirms closing other tabs, sending email, sending messages, adding
calendar events, sharing the screen, and clicking buttons whose label sounds destructive. The
extension refuses password and payment fields on its own side as well.
"""

import re
import time
import datetime
from urllib.parse import quote, quote_plus

from kayra.automation import ai
from kayra.automation import browser_bridge
from kayra.automation.policy import ActionResult

BRIDGE = browser_bridge            # the test suite replaces this with a fake extension
SLEEP = time.sleep

SETUP_HINT = ("Chrome isn't connected to me yet. Say \"browser set up\" and I'll walk you "
              "through adding the Kayra Bridge extension — it takes a minute, once.")


def _L():
    from kayra.automation import linux
    return linux


REQUIRED_VERSION = (1, 1, 0)
UPDATE_HINT = ("Your Kayra Bridge extension is out of date. Open chrome://extensions and press "
               "the reload arrow on Kayra Bridge, then ask me again.")


def _version():
    raw = getattr(getattr(BRIDGE, "BRIDGE", BRIDGE), "version", None) or "0"
    try:
        return tuple(int(x) for x in str(raw).split(".")[:3])
    except ValueError:
        return (0,)


def _call(cmd, timeout=10.0, **args):
    try:
        return BRIDGE.call(cmd, timeout=timeout, **args)
    except browser_bridge.BridgeError as e:
        if "unknown command" in str(e):
            raise browser_bridge.BridgeError(UPDATE_HINT)
        raise


def _need_bridge():
    return None if BRIDGE.connected() else ActionResult.unsupported(SETUP_HINT)


def _focus_chrome():
    """Bring the browser window forward so keystrokes and the share picker land there."""
    L = _L()
    try:
        windows = [w for w in L.list_windows() if L._is_browser_window(w)]
        window = L._pick_single(windows, prefer_recent=True) if windows else None
        if window is not None:
            L.focus_window(window)
    except Exception:
        pass


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         SETUP AND STATUS                               │
# └────────────────────────────────────────────────────────────────────────┘

def bridge_setup():
    L = _L()
    folder = browser_bridge.extension_dir()
    for binary in ("google-chrome-stable", "google-chrome", "chromium", "brave"):
        if L._have(binary):
            L.RUNNER.spawn([binary, "chrome://extensions/"])
            break
    if L._have("xdg-open"):
        L.RUNNER.spawn(["xdg-open", folder])
    if L._have("wl-copy"):
        L._ok("wl-copy", "--", folder)
    return ActionResult.success(
        "I've opened Chrome's extensions page and the extension folder, and copied its path. "
        "Turn on Developer mode at the top right, click Load unpacked, and choose that "
        "folder. I'll connect on my own within half a minute.", folder=folder)


def bridge_status():
    if BRIDGE.connected():
        return ActionResult.success("Chrome is connected to me.")
    return ActionResult.not_found(SETUP_HINT)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               TABS                                     │
# └────────────────────────────────────────────────────────────────────────┘

def tabs_list():
    missing = _need_bridge()
    if missing:
        return missing
    tabs = _call("tabs.list") or []
    if not tabs:
        return ActionResult.success("You have no tabs open.")
    titles = [_short(t.get("title") or t.get("url", "")) for t in tabs]
    head = ", ".join(titles[:6])
    more = f", and {len(titles) - 6} more" if len(titles) > 6 else ""
    return ActionResult.success(f"You have {len(tabs)} tab{'s' if len(tabs) != 1 else ''}: "
                                f"{head}{more}.", tabs=tabs)


def tab_switch(name):
    missing = _need_bridge()
    if missing:
        return missing
    try:
        tab = _call("tabs.activate", match=_site_key(name))
    except browser_bridge.BridgeError:
        return ActionResult.not_found(f"I don't see a {name} tab.")
    _focus_chrome()
    return ActionResult.success(f"Here's {_short(tab.get('title') or name)}.")


def tab_close(name, all_tabs=False):
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("tabs.close", match=_site_key(name), all=all_tabs) or {}
    if result.get("ambiguous"):
        titles = [_short(t.get("title", "")) for t in result["ambiguous"]]
        return ActionResult.ambiguous(f"I found {len(titles)} {name} tabs: {', '.join(titles[:3])}. "
                                      "Which one? Or say close all of them.", candidates=titles)
    closed = result.get("closed", 0)
    if not closed:
        return ActionResult.not_found(f"I don't see a {name} tab.")
    return ActionResult.success(f"Closed {closed} tab{'s' if closed != 1 else ''}.")


def tabs_close_others():
    missing = _need_bridge()
    if missing:
        return missing
    closed = (_call("tabs.close", exceptActive=True) or {}).get("closed", 0)
    return ActionResult.success(f"Closed {closed} other tab{'s' if closed != 1 else ''}.")


def tab_mute(name, on):
    missing = _need_bridge()
    if missing:
        return missing
    args = {} if name in ("current", "", None) else {"match": _site_key(name)}
    result = _call("tabs.mute", on=on, **args) or {}
    return ActionResult.success("Tab muted." if result.get("muted") else "Tab unmuted.")


def tab_pin():
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("tabs.pin") or {}
    return ActionResult.success("Pinned." if result.get("pinned") else "Unpinned.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              PAGES                                     │
# └────────────────────────────────────────────────────────────────────────┘

def page_click(label, nth=1):
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("page.click", text=label, nth=nth) or {}
    if not result.get("clicked"):
        return ActionResult.not_found(f"I can't find \"{label}\" on this page.")
    extra = f" There were {result['matches']} like it; I used number {nth}." \
        if result.get("matches", 1) > 1 and nth == 1 else ""
    return ActionResult.success(f"Clicked {result.get('label') or label}.{extra}")


def page_type(field, text, submit=False):
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("page.type", field=field, text=text, submit=submit) or {}
    if not result.get("typed"):
        reason = result.get("reason", "")
        if "password" in reason:
            return ActionResult.blocked("That's a password or payment field. I never type into "
                                        "those; please do it yourself.")
        return ActionResult.not_found(f"I can't find a {field} field here.")
    return ActionResult.success("Searching." if submit else "Typed.")


def page_scroll(direction="down", until=None):
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("page.scroll", direction=direction, until=until or "") or {}
    if until:
        return ActionResult.success(f"Here's {until}.") if result.get("found") else \
            ActionResult.not_found(f"I couldn't find {until} on this page.")
    return ActionResult.success("Done.")


def page_find(text):
    missing = _need_bridge()
    if missing:
        return missing
    count = (_call("page.find", text=text) or {}).get("count", 0)
    return ActionResult.success(f"\"{text}\" appears {count} time{'s' if count != 1 else ''} on "
                                "this page." if count else f"\"{text}\" isn't on this page.")


def page_controls():
    missing = _need_bridge()
    if missing:
        return missing
    controls = _call("page.controls", limit=40) or []
    names = [c["name"] for c in controls][:12]
    if not names:
        return ActionResult.not_found("I can't see any buttons on this page.")
    return ActionResult.success("You can say click on: " + ", ".join(names) + ".",
                                controls=controls)


def _page_text(max_chars=15000):
    return _call("page.text", max=max_chars, timeout=15.0) or {}


def page_read():
    missing = _need_bridge()
    if missing:
        return missing
    page = _page_text(4000)
    text = " ".join((page.get("text") or "").split())
    if not text:
        return ActionResult.not_found("This page has no readable text.")
    cut = text[:900]
    cut = cut[: cut.rfind(".") + 1] or cut
    return ActionResult.success(cut)


def page_summarize():
    missing = _need_bridge()
    if missing:
        return missing
    page = _page_text()
    text = page.get("text") or ""
    if len(text.strip()) < 80:
        return ActionResult.not_found("There isn't enough text on this page to summarize.")
    spoken = ai.summarize(
        "You are Kayra. Summarize this web page for the user in 3 to 5 spoken sentences: what "
        "it is, the key points, and anything they should act on. No markdown.",
        f"Title: {page.get('title')}\nURL: {page.get('url')}\n\n{text[:12000]}",
        fallback=f"This page is {page.get('title') or 'untitled'}. I couldn't reach a model to "
                 "summarize it, so say \"read this page\" if you want it read out.")
    return ActionResult.success(spoken)


def history_open(query, days=7):
    words = re.sub(r"\b(yesterday|today|last week|this week|earlier|about|on|the|a|an)\b", " ",
                   query or "", flags=re.I)
    words = " ".join(words.split())
    if BRIDGE.connected():
        items = _call("history.search", text=words, days=days, max=5) or []
        if items:
            best = items[0]
            _call("tabs.open", url=best["url"])
            return ActionResult.success(f"Opening {_short(best.get('title') or best['url'])}.")
        return ActionResult.not_found(f"I don't see {words or 'that'} in your recent history.")
    return ActionResult.unsupported(SETUP_HINT)


def bookmark_open(query):
    missing = _need_bridge()
    if missing:
        return missing
    items = _call("bookmarks.search", text=query, max=3) or []
    if not items:
        return ActionResult.not_found(f"I don't have a bookmark for {query}.")
    _call("tabs.open", url=items[0]["url"])
    return ActionResult.success(f"Opening {_short(items[0].get('title') or query)}.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               GMAIL                                    │
# └────────────────────────────────────────────────────────────────────────┘

GMAIL = "https://mail.google.com/mail/u/0/"


def _ensure_tab(match, url, wait=True):
    """Activate a tab whose URL contains `match`, or open `url`. Returns the tab dict."""
    try:
        return _call("tabs.activate", match=match)
    except browser_bridge.BridgeError:
        return _call("tabs.open", url=url, wait=wait, timeoutMs=20000, timeout=25.0)


def email_check():
    if not BRIDGE.connected():
        return _L().open_url(GMAIL)
    tab = _ensure_tab("mail.google.com", GMAIL)
    SLEEP(1.5)
    title = (_call("tabs.active") or tab or {}).get("title", "")
    match = re.search(r"\((\d[\d,]*)\)", title)
    if match:
        count = int(match.group(1).replace(",", ""))
        return ActionResult.success(f"You have {count} unread email{'s' if count != 1 else ''}. "
                                    "Say \"read my latest emails\" to hear them.")
    return ActionResult.success("No unread email." if "inbox" in title.lower() else
                                "Gmail is open.")


_FOLDERS = {"inbox": "inbox", "sent": "sent", "sent mail": "sent", "all mail": "all",
            "all": "all", "starred": "starred", "drafts": "drafts", "draft": "drafts",
            "spam": "spam", "trash": "trash", "bin": "trash", "important": "imp",
            "snoozed": "snoozed", "scheduled": "scheduled"}


def email_folder(name):
    key = re.sub(r"\b(mails?|emails?|folder|box|my|the|open|go to|show)\b", " ", str(name or "").lower())
    key = " ".join(key.split()) or "inbox"
    folder = _FOLDERS.get(key) or _FOLDERS.get(key.rstrip("s"))
    if folder is None:
        return ActionResult.not_found(f"Gmail has no {name} folder I know of.")
    url = f"{GMAIL}#{folder}"
    if not BRIDGE.connected():
        return _L().open_url(url)
    try:
        _call("tabs.activate", match="mail.google.com")
        _call("tabs.navigate", match="mail.google.com", url=url, wait=False)
    except browser_bridge.BridgeError:
        _call("tabs.open", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    _focus_chrome()
    return ActionResult.success(f"Here's {key.title()}.")


def email_open(which="first", nth=1, query=""):
    """Opens ONE email in Gmail: first / Nth / latest (by date) / oldest / unread / from X."""
    missing = _need_bridge()
    if missing:
        return missing
    _ensure_tab("mail.google.com", GMAIL)
    _focus_chrome()
    result = {}
    for _attempt in range(3):              # the list may still be rendering after a switch
        try:
            result = _call("gmail.open", which=which, nth=nth, query=query) or {}
        except browser_bridge.BridgeError as e:
            return ActionResult.failure(str(e))
        if result.get("opened") or "visible" not in str(result.get("reason", "")):
            break
        SLEEP(1.0)
    if not result.get("opened"):
        if query:
            return ActionResult.not_found(f"I don't see an email from or about {query} here.")
        return ActionResult.not_found("I can't see the email list. Is your inbox open?")
    who = result.get("from") or "someone"
    subject = result.get("subject") or "no subject"
    return ActionResult.success(f"Opened the email from {who}: {subject}.", **result)


def email_action(verb):
    """Reply / forward / archive / delete / back, on the email that is open."""
    missing = _need_bridge()
    if missing:
        return missing
    labels = {"reply": ["Reply"], "reply_all": ["Reply all"], "forward": ["Forward"],
              "archive": ["Archive"], "delete": ["Delete"], "back": ["Back to Inbox", "Back to"],
              "unread": ["Mark as unread"], "star": ["Not starred", "Starred"],
              "spam": ["Report spam"]}[verb]
    try:
        _call("tabs.activate", match="mail.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("Gmail isn't open.")
    clicked = _click_first(labels)
    if not clicked:
        return ActionResult.not_found(f"I can't see a {labels[0]} button. Is an email open?")
    return ActionResult.success({"reply": "Replying.", "reply_all": "Replying to everyone.",
                                 "forward": "Forwarding.", "archive": "Archived.",
                                 "delete": "Deleted.", "back": "Back to your inbox.",
                                 "unread": "Marked unread.", "star": "Starred.",
                                 "spam": "Reported as spam."}[verb])


def email_read(sender="latest", which=None, nth=1, query=""):
    """
    One email ("read my latest email", "read the email from Ana", "read this email"): open it
    and summarize THAT message. Several ("read my emails", "read my inbox"): summarize the list.
    """
    missing = _need_bridge()
    if missing:
        return missing
    if which:
        if which != "this":
            opened = email_open(which, nth, query)
            if not opened.ok:
                return opened
            SLEEP(2.0)
        page = _page_text(12000)
        text = page.get("text", "")
        spoken = ai.summarize(
            "You are Kayra reading ONE email the user has open in Gmail, from the page text. Say "
            "who it is from and what it says in at most 5 spoken sentences, including any date, "
            "deadline or action it asks for. Never read out codes, passwords or links.",
            text[:12000], fallback=" ".join(text.split())[:700] or "The email is open.")
        return ActionResult.success(spoken)
    _ensure_tab("mail.google.com", GMAIL)
    SLEEP(1.5)
    page = _page_text(8000)
    focus = "" if sender in ("latest", "inbox", "", None) else f" Only mention emails from {sender}."
    spoken = ai.summarize(
        "You are Kayra reading the user's Gmail inbox from its page text. In at most 5 spoken "
        "sentences, list the newest unread emails: who it's from and what it's about, most "
        "recent first. Never read out codes, passwords or links." + focus,
        page.get("text", "")[:8000],
        fallback="I've opened your inbox, but I couldn't reach a model to read it out.")
    return ActionResult.success(spoken)


def email_draft(to="", subject="", body=""):
    url = (f"{GMAIL}?view=cm&fs=1&to={quote(to)}&su={quote(subject)}&body={quote(body)}")
    if BRIDGE.connected():
        _call("tabs.open", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    else:
        _L().open_url(url)
    who = f" to {to}" if to else ""
    return ActionResult.success(f"Your draft{who} is ready. Check it, then say \"email send\" "
                                "or press send yourself.")


def email_send():
    missing = _need_bridge()
    if missing:
        return missing
    result = _call("page.click", text="Send", nth=1) or {}
    return ActionResult.success("Sent.") if result.get("clicked") else \
        ActionResult.not_found("I can't see a Send button. Is the draft open?")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         GOOGLE CALENDAR                                │
# └────────────────────────────────────────────────────────────────────────┘

CALENDAR = "https://calendar.google.com/calendar/u/0/r/day"


def calendar_agenda(day="today"):
    if not BRIDGE.connected():
        return _L().open_url(CALENDAR)
    when = datetime.date.today() + datetime.timedelta(days=1 if day == "tomorrow" else 0)
    url = f"https://calendar.google.com/calendar/u/0/r/day/{when:%Y/%m/%d}"
    try:
        _call("tabs.activate", match="calendar.google.com")
        _call("tabs.navigate", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    except browser_bridge.BridgeError:
        _call("tabs.open", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    SLEEP(1.5)
    page = _page_text(6000)
    spoken = ai.summarize(
        f"You are Kayra reading the user's Google Calendar day view for {day}. In at most 4 "
        "spoken sentences, list the events with their times in order. If there are none, say "
        "the day is free.", page.get("text", "")[:6000],
        fallback=f"I've opened your calendar for {day}.")
    return ActionResult.success(spoken)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text, now=None):
    """'tomorrow at 3pm', 'friday 10:30 am', 'at 6' -> datetime, or None. Local time."""
    now = now or datetime.datetime.now()
    low = (text or "").lower()
    day = now.date()
    if "tomorrow" in low:
        day += datetime.timedelta(days=1)
    else:
        for index, name in enumerate(_WEEKDAYS):
            if re.search(rf"\b{name}\b", low):
                ahead = (index - now.weekday()) % 7 or 7
                day += datetime.timedelta(days=ahead)
                break
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", low.split(" at ", 1)[-1]
                      if " at " in low else low)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "")
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not meridiem and hour < 8:          # "at 3" means the afternoon
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def calendar_add(text):
    when = parse_when(text)
    if when is None:
        return ActionResult.failure("When should I put it? Say a day and a time.")
    title = re.sub(r"\b(add|create|schedule|book|new|an?|event|on|at|tomorrow|today|"
                   + "|".join(_WEEKDAYS) + r")\b|\d{1,2}(:\d{2})?\s*(am|pm)?", " ", text,
                   flags=re.I)
    title = " ".join(title.split()).strip(" ,.") or "Event"
    end = when + datetime.timedelta(hours=1)
    url = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
           f"&text={quote_plus(title)}&dates={when:%Y%m%dT%H%M%S}/{end:%Y%m%dT%H%M%S}")
    if BRIDGE.connected():
        _call("tabs.open", url=url, wait=True, timeoutMs=20000, timeout=25.0)
        SLEEP(2.0)
        saved = (_call("page.click", text="Save", nth=1) or {}).get("clicked")
        if saved:
            return ActionResult.success(f"Added {title} on {when:%A} at {when:%-I:%M %p}.")
    else:
        _L().open_url(url)
    return ActionResult.success(f"I've filled in {title} for {when:%A} at {when:%-I:%M %p}. "
                                "Press Save to add it.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                         WHATSAPP WEB                                   │
# └────────────────────────────────────────────────────────────────────────┘

def message_send(contact, text):
    missing = _need_bridge()
    if missing:
        return missing
    if not contact or not text:
        return ActionResult.failure("Who should I message, and what should I say?")
    _ensure_tab("web.whatsapp.com", "https://web.whatsapp.com/")
    SLEEP(3.0)
    if not (_call("page.type", field="search", text=contact, clear=True) or {}).get("typed"):
        return ActionResult.failure("WhatsApp isn't ready. Is it logged in?")
    SLEEP(1.5)
    if not (_call("page.click", text=contact, nth=1) or {}).get("clicked"):
        return ActionResult.not_found(f"I couldn't find {contact} in WhatsApp.")
    SLEEP(1.0)
    typed = _call("page.type", field="type a message", text=text, submit=True) or {}
    if not typed.get("typed"):
        return ActionResult.failure("I opened the chat but couldn't type the message.")
    return ActionResult.success(f"Sent to {contact}.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            GOOGLE MEET                                 │
# └────────────────────────────────────────────────────────────────────────┘
# Meet's accessible names, as Meet itself announces them to a screen reader. The first label
# that exists wins, so English renamings ("Present now" -> "Share screen") keep working.
_MEET_LABELS = {
    ("mic", False): ["Turn off microphone"], ("mic", True): ["Turn on microphone"],
    ("camera", False): ["Turn off camera"], ("camera", True): ["Turn on camera"],
    ("captions", True): ["Turn on captions"], ("captions", False): ["Turn off captions"],
    ("hand", True): ["Raise hand"], ("hand", False): ["Lower hand"],
    "leave": ["Leave call"], "share": ["Share screen", "Present now"],
    "stop_share": ["Stop presenting", "Stop sharing", "You are presenting"],
    "join": ["Join now", "Ask to join", "Join"], "chat": ["Chat with everyone"],
}


def _click_first(labels):
    for label in labels:
        result = _call("page.click", text=label, nth=1) or {}
        if result.get("clicked"):
            return label
    return None


def meet_join(code="new"):
    missing = _need_bridge()
    if missing:
        return missing
    if code in ("new", "", None):
        url = "https://meet.google.com/new"
    else:
        url = code if code.startswith("http") else f"https://meet.google.com/{code}"
    _call("tabs.open", url=url, wait=True, timeoutMs=25000, timeout=30.0)
    SLEEP(3.0)
    if code in ("new", "", None):
        return ActionResult.success("I've started a new meeting.")
    if _click_first(_MEET_LABELS["join"]):
        return ActionResult.success("Joining the meeting.")
    return ActionResult.success("The meeting is open. Say \"meet join\" again when the Join "
                                "button appears.")


def meet_toggle(control, on):
    missing = _need_bridge()
    if missing:
        return missing
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("You're not in a Google Meet call.")
    if control in ("leave", "chat"):
        labels = _MEET_LABELS[control]
    elif on is None:
        labels = _MEET_LABELS[(control, False)] + _MEET_LABELS[(control, True)]
    else:
        labels = _MEET_LABELS[(control, on)]
    clicked = _click_first(labels)
    if not clicked:
        already = {True: "on", False: "off"}.get(on)
        return ActionResult.success(f"Your {control} is already {already}.") if already else \
            ActionResult.not_found(f"I can't find the {control} button.")
    return ActionResult.success({"Turn off microphone": "You're muted.",
                                 "Turn on microphone": "You're unmuted.",
                                 "Turn off camera": "Camera off.", "Turn on camera": "Camera on.",
                                 "Leave call": "You've left the call."}.get(clicked, "Done."))


def meet_share(surface="monitor"):
    """
    Starts screen sharing. Meet's button opens Chrome's picker; the extension's page hook
    preselects the requested surface ("Entire screen" for "monitor"). Kayra then confirms the
    picker with Enter, and on Wayland accepts the desktop portal's monitor picker the same way.
    """
    missing = _need_bridge()
    if missing:
        return missing
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("You're not in a Google Meet call.")
    _focus_chrome()
    _call("page.setMain", key="__kayraShareSurface", value=surface)
    if not _click_first(_MEET_LABELS["share"]):
        return ActionResult.not_found("I can't find Meet's share button.")
    L = _L()
    SLEEP(1.5)
    L.send_keys("enter")                       # Chrome's picker: "Share" on the preselected screen
    SLEEP(1.5)
    picker = [w for w in L.list_windows(include_kayra=True)
              if "share-picker" in w.cls or "share-picker" in w.initial_class]
    if picker:
        L.focus_window(picker[0])
        L.send_keys("enter")                   # the Hyprland portal picker: the first monitor
        SLEEP(1.0)
    SLEEP(1.0)
    controls = [c.get("name", "") for c in (_call("page.controls", limit=80) or [])]
    if any(re.search(r"stop presenting|you are presenting|stop sharing", c, re.I) for c in controls):
        return ActionResult.success("You're sharing your entire screen." if surface == "monitor"
                                    else "You're sharing.")
    return ActionResult.success("I've opened the share picker with your entire screen "
                                "selected. If it's still open, press Share.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              HELPERS                                   │
# └────────────────────────────────────────────────────────────────────────┘

_SITE_KEYS = {"gmail": "mail.google.com", "mail": "mail.google.com", "email": "mail.google.com",
              "calendar": "calendar.google.com", "meet": "meet.google.com",
              "google meet": "meet.google.com", "youtube": "youtube.com",
              "whatsapp": "web.whatsapp.com", "github": "github.com", "drive": "drive.google.com",
              "docs": "docs.google.com", "chatgpt": "chatgpt.com", "claude": "claude.ai"}


def _site_key(name):
    cleaned = " ".join(str(name or "").lower().replace(" tab", "").split())
    return _SITE_KEYS.get(cleaned, cleaned)


def _short(title, limit=48):
    title = re.sub(r"\s+[-–|]\s+(Google Chrome|Gmail|YouTube)$", "", str(title or "")).strip()
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"
