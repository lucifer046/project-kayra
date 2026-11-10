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


REQUIRED_VERSION = (1, 2, 1)
UPDATE_HINT = ("Your Kayra Bridge extension is out of date. Open chrome://extensions and press "
               "the reload arrow on Kayra Bridge, then ask me again.")
RELOADED_HINT = ("I've just updated the Kayra Bridge extension in Chrome. Give it a few seconds "
                 "to reconnect, then ask me again.")


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
            raise browser_bridge.BridgeError(_self_update() or UPDATE_HINT)
        raise


def _self_update():
    """
    Since 1.1.0 the extension accepts `runtime.reload`, which makes Chrome re-read the
    unpacked extension from this repository. So an out-of-date extension updates ITSELF and
    the user never opens chrome://extensions. Returns the sentence to say, or None when the
    extension is too old to reload itself (1.0.x).
    """
    if _version() < (1, 1, 0):
        return None
    try:
        BRIDGE.call("runtime.reload", timeout=3.0)
    except Exception:
        return None
    return RELOADED_HINT


_REFRESHED = {"done": False}


def refresh_stale_extension():
    """
    Called when the extension says hello. An extension older than the code in this repository
    (but new enough to reload itself) is told to reload ONCE per Kayra run, so fixes to the
    page functions reach the user's Chrome without anyone saying "browser update".
    """
    if _REFRESHED["done"] or not ((1, 1, 0) <= _version() < REQUIRED_VERSION):
        return False
    _REFRESHED["done"] = True
    try:
        BRIDGE.call("runtime.reload", timeout=3.0)
        return True
    except Exception:
        return False


def _outdated(minimum=REQUIRED_VERSION):
    """An ActionResult when the connected extension is older than `minimum`, else None."""
    if _version() >= tuple(minimum):
        return None
    return ActionResult.failure(_self_update() or UPDATE_HINT)


def _wait_for(timeout_ms=8000, **condition):
    """
    Waits until the active tab shows `text`, matches `selector`, has `urlIncludes` in its URL
    or `titleIncludes` in its title (G3). Returns True when it did. Older extensions without
    `page.waitFor` get the old fixed pause instead, so nothing breaks while one updates.
    """
    if _version() < (1, 2, 0):
        SLEEP(min(timeout_ms, 1500) / 1000.0)
        return True
    try:
        result = _call("page.waitFor", timeout=timeout_ms / 1000.0 + 2.0, timeoutMs=timeout_ms,
                       **condition) or {}
    except browser_bridge.BridgeError:
        return False
    return bool(result.get("ok"))


POLL_S = 0.25


def _until(attempt, timeout_s=2.5):
    """
    Readiness by TRYING: runs `attempt()` every POLL_S until it returns something truthy or
    `timeout_s` has been spent, and returns that value (or the last falsy one). This replaces
    the fixed pauses that used to sit in front of every click and keystroke: a page that is
    ready is acted on at once, and a slow one gets longer than the old pause allowed. Counted
    in attempts, not wall-clock, so a test rig with a no-op SLEEP finishes instantly.
    """
    result = None
    for index in range(max(1, int(timeout_s / POLL_S) + 1)):
        if index:
            SLEEP(POLL_S)
        try:
            result = attempt()
        except browser_bridge.BridgeError:
            result = None                    # the page is navigating; try again
        if result:
            return result
    return result


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


def bridge_update():
    if not BRIDGE.connected():
        return ActionResult.not_found(SETUP_HINT)
    if _version() < (1, 1, 0):
        return ActionResult.failure(UPDATE_HINT)
    try:
        BRIDGE.call("runtime.reload", timeout=3.0)
    except Exception:
        return ActionResult.failure(UPDATE_HINT)
    return ActionResult.success("Updating the Kayra Bridge extension. It reconnects on its own "
                                "in a few seconds.")


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
# │                    OPENING ANYTHING (round 5, A1–A6)                   │
# └────────────────────────────────────────────────────────────────────────┘

_WHERE_SAID = {"current": "in this tab", "tab": "in a new tab", "window": "in a new window",
               "incognito": "in an incognito window", "next": "next to this tab"}


def display_name(site):
    """'sheets' -> 'Google Sheets', 'youtube' -> 'YouTube'. What Kayra SAYS it opened."""
    from kayra.automation import sites
    return sites.display_name(site)


def _host(url):
    host = re.sub(r"^[a-z]+://", "", str(url or "").lower()).split("/")[0]
    return host[4:] if host.startswith("www.") else host


def open_in(url, where=None, name=None):
    """
    Opens `url` where the user said (A3): None/'tab' a new tab, 'current' this tab, 'window',
    'incognito', 'next' (beside this tab). Through the extension when it is connected — the
    user's own Chrome, and the tab is then known for "search it" / "close it" — otherwise by
    `xdg-open` or a browser argv, saying honestly what could not be honoured.
    """
    if not url:
        return ActionResult.failure("Open what?")
    label = name if name else _host(url)
    said = _WHERE_SAID.get(where, "")
    if not BRIDGE.connected():
        return _open_without_bridge(url, where, label)
    if where in ("window", "incognito", "next"):
        stale = _outdated((1, 2, 0))
        if stale:
            return stale
    try:
        if where == "current":
            tab = _call("tabs.navigate", url=url, wait=False) or {}
        else:
            tab = _call("tabs.open", url=url, where=where or "tab", wait=False) or {}
    except browser_bridge.BridgeError as e:
        if where == "incognito":
            return _open_without_bridge(url, where, label)
        return ActionResult.failure(f"I couldn't open {label}: {e}")
    _focus_chrome()
    spoken = f"Opened {label} {said}." if where in _WHERE_SAID and where != "tab" else \
        f"Opening {label}."
    return ActionResult.success(spoken, url=url, tab=tab.get("id"))


def _open_without_bridge(url, where, label):
    L = _L()
    if where in ("window", "incognito"):
        binary = L._browser_binary()
        if binary:
            result = L.open_browser_window(binary, where, url)
            if result.ok:
                result.message = f"Opened {label} {_WHERE_SAID[where]}."
            return result
    result = L.open_url(url)
    if not result.ok:
        return result
    if where in ("current", "next"):
        result.message = (f"Opened {label} in a new tab. To use this tab I need the Kayra Bridge "
                          "extension; say \"browser set up\".")
    else:
        result.message = f"Opening {label}."
    return result


# Words that say the thing is NOT a website: "open the PR group chat", "open my resume file".
_NOT_WEB = re.compile(r"\b(chat|chats|group|conversation|folder|file|files|email|mail|message|"
                      r"messages|tab|tabs|window|workspace|terminal|setting|settings|"
                      r"document|sheet|spreadsheet|pdf)\s*$", re.I)


def web_worthy(name):
    """May an unknown 'open X' fall back to the web? Short names only, never paths or chats."""
    text = " ".join(str(name or "").split())
    return bool(text) and len(text.split()) <= 5 and "/" not in text and "~" not in text \
        and not _NOT_WEB.search(text) and not re.match(r"(this|that|it)$", text, re.I)


def _ddgs_top(query, limit=5):
    """Top web results from DuckDuckGo's text API (the same library real-time search uses)."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return []
    import threading
    box = []

    def work():
        try:
            with DDGS() as client:
                box.extend(client.text(query, max_results=limit) or [])
        except Exception:
            pass
    worker = threading.Thread(target=work, name="kayra-web-lookup", daemon=True)
    worker.start()
    worker.join(6.0)                       # a lookup never holds the turn longer than this
    return [{"url": r.get("href") or r.get("url"), "title": r.get("title", "")}
            for r in list(box) if (r.get("href") or r.get("url"))]


TOP_RESULTS = _ddgs_top                    # the test suite replaces this; no network in tests

# What the web lookup found last time, so "open swiggy" pays the (up to 6 s) lookup ONCE.
# Only names that resolved to a host carrying the spoken name are remembered — a first-result
# guess is not a fact worth keeping. Bounded; used only with the real lookup, so a test rig
# that substitutes TOP_RESULTS neither reads nor writes the user's file.
LEARNED_SITES_FILE = "learned_sites.json"
LEARNED_SITES_MAX = 200


def _learned_path():
    from kayra.core.paths import data_path
    return data_path(LEARNED_SITES_FILE)


def _learned_sites():
    import json
    try:
        with open(_learned_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _learned_key(name):
    return " ".join(str(name or "").lower().split())


def recall_site(name):
    if TOP_RESULTS is not _ddgs_top:
        return None
    url = _learned_sites().get(_learned_key(name))
    return url if isinstance(url, str) and url.startswith("http") else None


def remember_site(name, url):
    if TOP_RESULTS is not _ddgs_top:
        return False
    squashed = re.sub(r"[^a-z0-9]", "", _learned_key(name))
    if not squashed or squashed not in re.sub(r"[^a-z0-9]", "", _host(url)):
        return False
    import json
    import os
    sites = _learned_sites()
    sites.pop(_learned_key(name), None)
    sites[_learned_key(name)] = "https://" + _host(url) + "/"
    while len(sites) > LEARNED_SITES_MAX:
        sites.pop(next(iter(sites)))
    path = _learned_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(sites, handle, indent=1)
        os.replace(path + ".tmp", path)
        return True
    except OSError:
        return False


def best_result(name, results):
    """The result whose HOST carries the spoken name ('swiggy' -> swiggy.com), else the first."""
    squashed = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    for result in results:
        if squashed and squashed in re.sub(r"[^a-z0-9]", "", _host(result.get("url"))):
            return result
    return results[0] if results else None


def open_site(name, where=None):
    """
    "open X" for something that is not an installed app (A1): a known website, else the top
    web result for X, else a web search. Never "I couldn't find an app called X" for a site.
    """
    from kayra.automation import targets
    url = targets.website_url(name)
    if url:
        return open_in(url, where, display_name(targets.canonical_website(name)))
    if targets.is_explicit_url(name):
        return open_in(targets.normalize_url(name), where, _host(name))
    known = recall_site(name)
    if known:
        return open_in(known, where, _host(known))
    hit = best_result(name, TOP_RESULTS(name) or [])
    if hit:
        result = open_in(hit["url"], where, _host(hit["url"]))
        if result.ok:
            remember_site(name, hit["url"])
            result.message = (f"I don't have an app called {name}, so I opened "
                              f"{_host(hit['url'])}, the top web result.")
        return result
    result = open_in(f"https://www.google.com/search?q={quote_plus(name)}", where, "")
    if result.ok:
        result.message = (f"I couldn't find an app or a site called {name}, so I searched the "
                          "web for it.")
    return result


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
    _wait_for(titleIncludes="Inbox", timeout_ms=6000)
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
    for _attempt in range(12):             # the list may still be rendering after a switch
        try:
            result = _call("gmail.open", which=which, nth=nth, query=query) or {}
        except browser_bridge.BridgeError as e:
            return ActionResult.failure(str(e))
        if result.get("opened") or "visible" not in str(result.get("reason", "")):
            break
        SLEEP(POLL_S)
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
            # The opened message, not a fixed pause. Gmail marks every rendered message with
            # `data-message-id`; if that ever changes this costs what the old pause did.
            _wait_for(selector="[data-message-id]", timeout_ms=2500)
        page = _page_text(12000)
        text = page.get("text", "")
        spoken = ai.summarize(
            "You are Kayra reading ONE email the user has open in Gmail, from the page text. Say "
            "who it is from and what it says in at most 5 spoken sentences, including any date, "
            "deadline or action it asks for. Never read out codes, passwords or links.",
            text[:12000], fallback=" ".join(text.split())[:700] or "The email is open.")
        return ActionResult.success(spoken)
    _ensure_tab("mail.google.com", GMAIL)
    _wait_for(selector="tr.zA", timeout_ms=2500)
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
    _wait_for(selector="[role=main] [role=grid], [role=main] [role=gridcell]", timeout_ms=2500)
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
        saved = _until(lambda: (_call("page.click", text="Save", nth=1) or {}).get("clicked"), 4.0)
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
    # Each step is tried until the page is ready for it (WhatsApp can take ten seconds to load
    # and under one when it is already open) instead of pausing 3 + 1.5 + 1 seconds every time.
    if not _until(lambda: (_call("page.type", field="search", text=contact, clear=True)
                           or {}).get("typed"), 10.0):
        return ActionResult.failure("WhatsApp isn't ready. Is it logged in?")
    if not _until(lambda: (_call("page.click", text=contact, nth=1) or {}).get("clicked"), 4.0):
        return ActionResult.not_found(f"I couldn't find {contact} in WhatsApp.")
    typed = _until(lambda: (lambda r: r if r.get("typed") else None)(
        _call("page.type", field="type a message", text=text, submit=True) or {}), 3.0) or {}
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
    if code in ("new", "", None):
        return ActionResult.success("I've started a new meeting.")
    if _until(lambda: _click_first(_MEET_LABELS["join"]), 6.0):
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
