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

import io
import os
import re
import csv
import time
import difflib
import datetime
from urllib.parse import quote, quote_plus, urlsplit

from kayra.automation import ai
from kayra.automation import browser_bridge
# Kayra's lane into the Hyprland screen-share portal. Replaced by a fake in the test suite:
# the real one reads ~/.config/hypr/xdph.conf and writes under $XDG_RUNTIME_DIR.
from kayra.automation import share_picker as SHARE_HELPER
from kayra.automation.policy import Action, ActionResult, Status

BRIDGE = browser_bridge            # the test suite replaces this with a fake extension
SLEEP = time.sleep

SETUP_HINT = ("Chrome isn't connected to me yet. Say \"browser set up\" and I'll walk you "
              "through adding the Kayra Bridge extension — it takes a minute, once.")


def _L():
    from kayra.automation import linux
    return linux


REQUIRED_VERSION = (1, 9, 0)
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


# Reads that change nothing. Within ONE command, "which tab is active?" is asked once and
# reused until something that can change the answer (open, navigate, activate, close…) runs.
# Cleared at the start of every command by `new_command()` (tuning pass V9).
_READ_ONLY = frozenset({"tabs.active", "tabs.list", "page.text", "page.controls", "groups.list",
                        "downloads.list", "page.docList", "page.results", "page.comments",
                        "page.selection", "page.find", "history.search", "bookmarks.search",
                        "bookmarks.find", "calendar.next", "page.waitFor", "page.fetchText", "ping"})
_ACTIVE = {"tab": None}


def new_command():
    _ACTIVE["tab"] = None


def _call(cmd, timeout=10.0, **args):
    if cmd == "tabs.active" and not args and _ACTIVE["tab"] is not None:
        return dict(_ACTIVE["tab"])
    if cmd not in _READ_ONLY:
        _ACTIVE["tab"] = None
    try:
        result = BRIDGE.call(cmd, timeout=timeout, **args)
        if cmd == "tabs.active" and not args and isinstance(result, dict):
            _ACTIVE["tab"] = dict(result)
        return result
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


def _ready(cmd, ready, wait_s, **args):
    """
    `cmd` until `ready(result)`: from extension 1.8.0 ONE call that waits inside the browser
    (no 250 ms polling round trips); older extensions keep the polling `_until`.
    """
    if _version() >= (1, 8, 0):
        try:
            result = _call(cmd, timeout=wait_s + 5.0, waitMs=int(wait_s * 1000), **args)
        except browser_bridge.BridgeError:
            return None
        return result
    return _until(lambda: (lambda r: r if ready(r) else None)(_call(cmd, **args)), wait_s)


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


# ── Tabs by POSITION (B1) and in bulk (B2) ──────────────────────────────
# Targets written by `common.tab_position`: "#3" (third), "#-1" (last), "#-2" (second last),
# "+1" (the tab on the right), "-1" (the tab on the left). Positions count within the window
# Kayra is acting in — the focused Chrome window — the way the tab strip reads.

def is_position(target):
    return bool(re.fullmatch(r"#-?\d+|[+-]\d+", str(target or "").strip()))


def _strip_tabs():
    """(tabs of the focused window in strip order, the active one) — or (None, None)."""
    active = _call("tabs.active") or {}
    tabs = [t for t in (_call("tabs.list") or []) if t.get("windowId") == active.get("windowId")]
    return (tabs, active) if tabs else (None, None)


def _tab_at(target):
    tabs, active = _strip_tabs()
    if not tabs:
        return None, "I don't see any tabs."
    spec = str(target).strip()
    if spec.startswith("#"):
        nth = int(spec[1:])
        index = nth - 1 if nth > 0 else len(tabs) + nth
        if not 0 <= index < len(tabs):
            return None, f"You only have {len(tabs)} tab{'s' if len(tabs) != 1 else ''} open."
        return tabs[index], None
    here = next((i for i, t in enumerate(tabs) if t.get("id") == active.get("id")), 0)
    index = here + int(spec)
    if not 0 <= index < len(tabs):
        return None, "There is no tab on that side."
    return tabs[index], None


def _close_ids(ids):
    stale = _outdated((1, 2, 2))
    if stale:
        return stale
    result = _call("tabs.close", tabIds=list(ids)) or {}
    closed = result.get("closed", 0)
    return ActionResult.success(f"Closed {closed} tab{'s' if closed != 1 else ''}.")


def _url_key(url):
    url = str(url or "").split("#", 1)[0]
    return url[:-1] if url.endswith("/") else url


def tabs_close_duplicates():
    """Closes every tab whose page is already open in another tab, keeping the active one."""
    missing = _need_bridge()
    if missing:
        return missing
    tabs = _call("tabs.list") or []
    seen, doomed = {}, []
    for tab in sorted(tabs, key=lambda t: not t.get("active")):     # active ones are kept
        key = _url_key(tab.get("url"))
        if not key or tab.get("pinned"):
            continue
        if key in seen:
            doomed.append(tab["id"])
        else:
            seen[key] = tab["id"]
    if not doomed:
        return ActionResult.success("No duplicate tabs — every tab is a different page.")
    return _close_ids(doomed)


def tabs_close_side(side):
    """Closes the unpinned tabs to the right (or left) of the active one in its window."""
    missing = _need_bridge()
    if missing:
        return missing
    tabs, active = _strip_tabs()
    if not tabs:
        return ActionResult.not_found("I don't see any tabs.")
    here = next((i for i, t in enumerate(tabs) if t.get("id") == active.get("id")), 0)
    chosen = tabs[here + 1:] if side == "right" else tabs[:here]
    ids = [t["id"] for t in chosen if not t.get("pinned")]
    if not ids:
        return ActionResult.success(f"There are no tabs to the {side}.")
    return _close_ids(ids)


# ── Part 2b: mute the rest, recently closed, sleep, find, zoom, hard reload, copy ──

def _bridge_at(minimum):
    """The ActionResult to return instead of acting, or None when the bridge can do it."""
    return _need_bridge() or _outdated(minimum)


def tabs_mute_others(on=True):
    """B3: "mute everything except this" — every tab but the active one."""
    stop = _bridge_at((1, 3, 0))
    if stop:
        return stop
    changed = (_call("tabs.muteOthers", on=bool(on)) or {}).get("changed", 0)
    if not changed:
        return ActionResult.success("Nothing else was " + ("playing." if on else "muted."))
    return ActionResult.success(f"{'Muted' if on else 'Unmuted'} {changed} other "
                                f"tab{'s' if changed != 1 else ''}.")


def tabs_recently_closed():
    """B6: "what did I just close?" — the last few closed tabs and windows, newest first."""
    stop = _bridge_at((1, 3, 0))
    if stop:
        return stop
    items = _call("tabs.recentlyClosed", max=5) or []
    if not items:
        return ActionResult.success("You haven't closed anything recently.")
    names = [_short(i.get("title") or i.get("url", "")) for i in items]
    return ActionResult.success("You recently closed " + ", ".join(names[:3])
                                + (f", and {len(names) - 3} more." if len(names) > 3 else ".")
                                + " Say reopen and the name to bring one back.", closed=items)


def tab_reopen(name=""):
    """B6: reopen the last closed tab, or the closed tab whose title or address matches."""
    stop = _bridge_at((1, 3, 0))
    if stop:
        return stop
    key = _site_key(name) if name and name not in ("last", "tab", "last closed") else ""
    session = None
    if key:
        for item in _call("tabs.recentlyClosed", max=25) or []:
            if key in f"{item.get('title', '')} {item.get('url', '')}".lower():
                session = item.get("sessionId")
                break
        if not session:
            return ActionResult.not_found(f"I don't see a recently closed {name} tab.")
    result = _call("tabs.restore", **({"sessionId": session} if session else {})) or {}
    if not result.get("restored"):
        return ActionResult.not_found("There's nothing to reopen.")
    _focus_chrome()
    return ActionResult.success(f"Reopened {_short(result.get('title') or 'it')}.")


def tabs_discard():
    """B7: put background tabs to sleep. They reload when clicked; nothing playing is touched."""
    stop = _bridge_at((1, 3, 0))
    if stop:
        return stop
    count = (_call("tabs.discard") or {}).get("discarded", 0)
    if not count:
        return ActionResult.success("Every background tab is already asleep.")
    return ActionResult.success(f"Put {count} background tab{'s' if count != 1 else ''} to "
                                "sleep. They reload when you open them.")


_STOP_WORDS = frozenset("the a an i was am is about on of for and to in my me where with that "
                        "reading read tab page looking at".split())


def tab_find(topic, max_tabs=30, max_chars=20000):
    """
    B8: "find the tab where I was reading about X" — titles and addresses first, then the
    text of each tab (bounded: 30 tabs, 20 KB each), and switches to the best match.
    """
    missing = _need_bridge()
    if missing:
        return missing
    words = [w for w in re.findall(r"[a-z0-9]+", str(topic).lower()) if w not in _STOP_WORDS]
    if not words:
        return ActionResult.failure("What was the tab about?")
    best, best_score = None, 0.0
    for tab in (_call("tabs.list") or [])[:max_tabs]:
        head = f"{tab.get('title', '')} {tab.get('url', '')}".lower()
        score = 3.0 * sum(w in head for w in words)
        if score < 3.0 * len(words):
            try:
                text = str((_call("page.text", tabId=tab["id"], max=max_chars) or {}).get("text", ""))
            except Exception:
                text = ""                     # a sleeping or restricted tab: title only
            low = text.lower()
            score += sum(min(low.count(w), 5) for w in words) / 5.0
        if score > best_score:
            best, best_score = tab, score
    if best is None:
        return ActionResult.not_found(f"No open tab mentions {topic}.")
    _call("tabs.activate", tabId=best["id"])
    _focus_chrome()
    return ActionResult.success(f"Here's {_short(best.get('title') or 'that tab')}.")


def page_reload(hard=False):
    """C1: reload; "hard reload" bypasses the cache."""
    stop = _bridge_at((1, 3, 0) if hard else (1, 0, 0))
    if stop:
        return stop
    _call("tabs.reload", hard=bool(hard))
    return ActionResult.success("Reloaded without the cache." if hard else "Reloaded.")


def page_zoom(percent):
    """C2: "zoom to 150 percent" (Ctrl+plus / minus / 0 already cover in, out and reset)."""
    stop = _bridge_at((1, 3, 0))
    if stop:
        return stop
    try:
        factor = max(25, min(500, int(float(percent)))) / 100.0
    except (TypeError, ValueError):
        return ActionResult.failure("What zoom level?")
    result = _call("tabs.zoom", factor=factor) or {}
    return ActionResult.success(f"Zoom is {round(float(result.get('factor', factor)) * 100)} percent.")


def page_copy(what="link"):
    """C7: the active tab's address or title onto the clipboard."""
    missing = _need_bridge()
    if missing:
        return missing
    tab = _call("tabs.active") or {}
    value = tab.get("title") if what == "title" else tab.get("url")
    if not value:
        return ActionResult.not_found("I can't read this tab.")
    if not COPY(str(value)):
        return ActionResult.failure("I couldn't use the clipboard.")
    return ActionResult.success("Copied the page title." if what == "title" else "Copied the link.")


def _copy_default(text):
    from kayra.automation import linux
    return linux.clipboard_set(text)


COPY = _copy_default        # replaced by the test rigs


def tab_switch(name):
    missing = _need_bridge()
    if missing:
        return missing
    if name == "it":
        tab = _referent_tab()
        if tab is None:
            return _no_referent()
        _call("tabs.activate", tabId=tab["id"])
        _focus_chrome()
        return ActionResult.success(f"Here's {_short(tab.get('title') or 'that tab')}.")
    if is_position(name):
        tab, problem = _tab_at(name)
        if tab is None:
            return ActionResult.not_found(problem)
        _call("tabs.activate", tabId=tab["id"])
        _focus_chrome()
        return ActionResult.success(f"Here's {_short(tab.get('title') or 'that tab')}.")
    try:
        tab = _call("tabs.activate", match=_site_key(name))
    except browser_bridge.BridgeError:
        return ActionResult.not_found(f"I don't see a {name} tab.")
    remember_tab(tab)
    _focus_chrome()
    return ActionResult.success(f"Here's {_short(tab.get('title') or name)}.")


def tab_close(name, all_tabs=False, confirmed_ids=None, action=None):
    missing = _need_bridge()
    if missing:
        return missing
    if name == "it":
        tab = _referent_tab()
        if tab is None:
            return _no_referent()
        LAST_TAB["id"] = None
        result = _close_ids([tab["id"]])
        return ActionResult.success(f"Closed {_short(tab.get('title') or 'that tab')}.") if result.ok else result
    if is_position(name):
        tab, problem = _tab_at(name)
        if tab is None:
            return ActionResult.not_found(problem)
        return _close_ids([tab["id"]])
    if all_tabs:
        return _close_many(_matching_tabs(name), f"{name} tab", f"{name} tabs", action, confirmed_ids)
    result = _call("tabs.close", match=_site_key(name), all=all_tabs) or {}
    if result.get("ambiguous"):
        titles = [_short(t.get("title", "")) for t in result["ambiguous"]]
        return ActionResult.ambiguous(f"I found {len(titles)} {name} tabs: {', '.join(titles[:3])}. "
                                      "Which one? Or say close all of them.", candidates=titles)
    closed = result.get("closed", 0)
    if not closed:
        return ActionResult.not_found(f"I don't see a {name} tab.")
    return ActionResult.success(f"Closed {closed} tab{'s' if closed != 1 else ''}.")


# ── Part 2c: B2 by site / all but pinned, B4 groups, B5 windows, C3 results, C4 highlight ──

CONFIRM_ABOVE = 3          # B2: closing more tabs than this asks first


def _matching_tabs(name):
    """Tabs whose title or address contains the name (a site name is its domain first)."""
    key, plain = _site_key(name).lower(), str(name or "").lower().strip()
    tabs = _call("tabs.list") or []
    hits = [t for t in tabs if key and key in f"{t.get('title', '')} {t.get('url', '')}".lower()]
    if not hits and plain != key:
        hits = [t for t in tabs if plain in f"{t.get('title', '')} {t.get('url', '')}".lower()]
    return hits


def _close_many(tabs, noun, plural, action=None, confirmed_ids=None):
    """
    Closes `tabs`; more than CONFIRM_ABOVE asks first (nothing closes until "yes"), and the
    approved action carries the exact tab ids found, so "yes" never closes a tab that opened
    in the meantime.
    """
    if confirmed_ids is not None:
        return _close_ids(confirmed_ids)
    ids = [t["id"] for t in tabs]
    if not ids:
        return ActionResult.not_found(f"I don't see a {noun}.")
    if len(ids) > CONFIRM_ABOVE and action is not None:
        approved = Action(action.domain, action.action, target=action.target,
                          parameters={**action.parameters, "confirmed_ids": ids}, raw=action.raw)
        titles = ", ".join(_short(t.get("title", ""), 30) for t in tabs[:3])
        return ActionResult(Status.NEEDS_CONFIRMATION,
                            f"That's {len(ids)} {plural}, like {titles}. Close all of them?",
                            {"confirm": approved})
    return _close_ids(ids)


def tabs_close_unpinned(confirmed_ids=None, action=None):
    """B2: "close all but pinned" — every unpinned tab in the focused window."""
    missing = _need_bridge()
    if missing:
        return missing
    tabs, _active = _strip_tabs()
    tabs = [t for t in (tabs or []) if not t.get("pinned")]
    if not tabs and confirmed_ids is None:
        return ActionResult.success("Every tab here is pinned.")
    return _close_many(tabs, "unpinned tab", "unpinned tabs", action, confirmed_ids)


def _groups():
    return _call("groups.list") or []


def _find_group(name):
    """A group by its title (exact, then contains), or the active tab's for "current"."""
    groups = _groups()
    if not groups:
        return None, "You don't have any tab groups."
    if name in ("current", "", None):
        active = _call("tabs.active") or {}
        gid = active.get("groupId", -1)
        group = next((g for g in groups if g.get("id") == gid), None)
        return (group, None) if group else (None, "This tab isn't in a group.")
    want = str(name).lower().strip()
    exact = [g for g in groups if g.get("title", "").lower() == want]
    hits = exact or [g for g in groups if want and want in g.get("title", "").lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"I don't see a {name} group."
    return None, f"You have {len(hits)} groups called {name}."


def _group_label(group):
    return f"the {group.get('title')} group" if group.get("title") else "that group"


def tabs_group(topic, title=""):
    """B4: "group my college tabs (as studies)" — matching tabs into a new, named group."""
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    words = [w for w in re.findall(r"[a-z0-9]+", str(topic).lower()) if w not in _STOP_WORDS]
    if not words:
        return ActionResult.failure("Which tabs should I group?")
    tabs = _matching_tabs(topic)
    if not tabs:
        # Not in a title or address: read the tabs, the way "find the tab about X" does.
        for tab in (_call("tabs.list") or [])[:30]:
            try:
                text = str((_call("page.text", tabId=tab["id"], max=20000) or {}).get("text", "")).lower()
            except Exception:
                text = ""
            if sum(text.count(w) for w in words) >= 2:
                tabs.append(tab)
    tabs = [t for t in tabs if not t.get("pinned")]
    if not tabs:
        return ActionResult.not_found(f"I don't see any {topic} tabs.")
    name = (title or topic).strip().title()
    windows = {t.get("windowId") for t in tabs}
    if len(windows) > 1:
        # A group lives in one window: use the window holding most of them.
        home = max(windows, key=lambda w: sum(t.get("windowId") == w for t in tabs))
        tabs = [t for t in tabs if t.get("windowId") == home]
    _call("groups.create", tabIds=[t["id"] for t in tabs], title=name)
    return ActionResult.success(f"Grouped {len(tabs)} tab{'s' if len(tabs) != 1 else ''} as {name}.")


def groups_list():
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    groups = _groups()
    if not groups:
        return ActionResult.success("You don't have any tab groups.")
    parts = [f"{g.get('title') or 'an unnamed group'} ({g.get('tabs', 0)} tabs"
             + (", collapsed)" if g.get("collapsed") else ")") for g in groups[:6]]
    return ActionResult.success(f"You have {len(groups)} tab group{'s' if len(groups) != 1 else ''}: "
                                + ", ".join(parts) + ".")


def group_add(name):
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    group, problem = _find_group(name)
    if group is None:
        return ActionResult.not_found(problem)
    active = _call("tabs.active") or {}
    if active.get("windowId") != group.get("windowId"):
        return ActionResult.failure(f"{_group_label(group).capitalize()} is in another window.")
    _call("groups.create", tabIds=[active["id"]], groupId=group["id"])
    return ActionResult.success(f"Added this tab to {_group_label(group)}.")


def group_update(name, **props):
    """Rename, recolour, collapse or expand one group — or every group for "all"."""
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    if name == "all":
        groups = _groups()
        if not groups:
            return ActionResult.success("You don't have any tab groups.")
        for g in groups:
            _call("groups.update", groupId=g["id"], **props)
        verb = "Collapsed" if props.get("collapsed") else "Expanded"
        return ActionResult.success(f"{verb} {len(groups)} group{'s' if len(groups) != 1 else ''}.")
    group, problem = _find_group(name)
    if group is None:
        return ActionResult.not_found(problem)
    _call("groups.update", groupId=group["id"], **props)
    if "title" in props:
        return ActionResult.success(f"Renamed {_group_label(group)} to {props['title']}.")
    if "color" in props:
        return ActionResult.success(f"{_group_label(group).capitalize()} is {props['color']} now.")
    return ActionResult.success(f"{'Collapsed' if props.get('collapsed') else 'Expanded'} "
                                f"{_group_label(group)}.")


def group_ungroup(name):
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    if name == "all":
        groups = _groups()
        total = sum((_call("groups.ungroup", groupId=g["id"]) or {}).get("ungrouped", 0) for g in groups)
        return ActionResult.success(f"Ungrouped {total} tabs." if groups else "You don't have any tab groups.")
    group, problem = _find_group(name)
    if group is None:
        return ActionResult.not_found(problem)
    count = (_call("groups.ungroup", groupId=group["id"]) or {}).get("ungrouped", 0)
    return ActionResult.success(f"Ungrouped {_group_label(group)} ({count} tabs).")


def group_close(name, confirmed_ids=None, action=None):
    """Closes the tabs of one group; more than CONFIRM_ABOVE asks first."""
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    if confirmed_ids is not None:
        return _close_ids(confirmed_ids)
    group, problem = _find_group(name)
    if group is None:
        return ActionResult.not_found(problem)
    tabs = [t for t in (_call("tabs.list") or []) if t.get("groupId") == group["id"]]
    return _close_many(tabs, f"tab in {_group_label(group)}", f"tabs in {_group_label(group)}", action)


def tab_detach(to_other_monitor=False):
    """B5: "move this tab to a new window" (and "… to the other screen")."""
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    result = _call("windows.detach") or {}
    title = _short(result.get("title") or "This tab", 40)
    if not to_other_monitor:
        return ActionResult.success(f"{title} has its own window now.")
    L = _L()
    try:
        many = len(L.HYPR.monitors()) >= 2
    except Exception:
        many = False
    if not many:
        return ActionResult.success(f"{title} has its own window now; there's only one screen.")
    window = _until(lambda: (lambda w: w if w is not None and L._is_browser_window(w) else None)(
        L.active_window()), 2.0)
    if window is None:
        return ActionResult.success(f"{title} has its own window; I couldn't move it to the other screen.")
    moved = L._dispatch_window("move", "movewindow mon:+1", window, monitor="+1")
    return ActionResult.success(f"{title} is on the other screen now." if moved else
                                f"{title} has its own window; I couldn't move it to the other screen.")


def windows_merge():
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    result = _call("windows.merge") or {}
    moved, windows = result.get("moved", 0), result.get("windows", 0)
    if not moved:
        return ActionResult.success("All your tabs are already in one window.")
    return ActionResult.success(f"Moved {moved} tab{'s' if moved != 1 else ''} from {windows} "
                                f"other window{'s' if windows != 1 else ''} into this one.")


_ORDINAL_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
                  7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", -1: "last"}


def page_result(nth, new_tab=False):
    """C3: "open the third result" — on Google, YouTube, Amazon, Flipkart, GitHub, Reddit,
    DuckDuckGo, Bing, else the Nth content link of the page."""
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    # Up to 6 s: the wait ends the moment results appear (a slow YouTube load took ~7 s live).
    found = _ready("page.results", lambda r: bool((r or {}).get("count") or (r or {}).get("blocked")),
                   6.0, nth=int(nth)) or {}
    if found.get("blocked"):
        return ActionResult.not_found("This page is a bot check, not a list of results. Solve it "
                                      "in Chrome, then ask me again.")
    result = found.get("result")
    which = _ORDINAL_WORDS.get(int(nth), f"number {nth}")
    if not found.get("count"):
        return ActionResult.not_found("I don't see a list of results on this page.")
    if not result:
        return ActionResult.not_found(f"There are only {found['count']} results here.")
    if new_tab:
        _call("tabs.open", url=result["url"], where="next")
    else:
        _call("tabs.navigate", url=result["url"])
    title = _short(result.get("title") or result["url"])
    return ActionResult.success(f"Opened the {which} result{' in a new tab' if new_tab else ''}: {title}.")


def page_results():
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    found = _call("page.results", nth=0) or {}
    if found.get("blocked"):
        return ActionResult.not_found("This page is a bot check, not a list of results.")
    results = found.get("results") or []
    if not results:
        return ActionResult.not_found("I don't see a list of results on this page.")
    spoken = "; ".join(f"{i}. {_short(r.get('title') or r.get('url'), 60)}"
                       for i, r in enumerate(results[:5], 1))
    return ActionResult.success(f"The top results: {spoken}. Say \"open the second result\" "
                                "to open one.", results=results)


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
    if _version() >= (1, 7, 0) and _active_doc()[0] in ("doc", "sheet", "slides"):
        # The Google editors draw on a canvas: their own Find (Ctrl+F) is the only way in.
        L = _L()
        _focus_chrome()
        L.send_keys("ctrl+f")
        L.type_text(text)
        return ActionResult.success(f"Searching this {_WS_NOUN[_active_doc()[0]]} for {text}.")
    if _version() >= (1, 5, 0):
        # C4: highlighted on the page, the first one scrolled to; "next match" steps on.
        found = _call("page.highlight", text=text) or {}
        count = found.get("count", 0)
        if not count:
            return ActionResult.success(f"\"{text}\" isn't on this page.")
        more = " Say next match to go on." if count > 1 else ""
        return ActionResult.success(f"Found {count} match{'es' if count != 1 else ''} for "
                                    f"\"{text}\" and highlighted them; here's the first.{more}")
    count = (_call("page.find", text=text) or {}).get("count", 0)
    return ActionResult.success(f"\"{text}\" appears {count} time{'s' if count != 1 else ''} on "
                                "this page." if count else f"\"{text}\" isn't on this page.")


def page_find_step(step):
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    found = _call("page.highlight", step=int(step)) or {}
    if not found.get("count"):
        return ActionResult.not_found("There's nothing highlighted. Say find, then the words.")
    return ActionResult.success(f"Match {found['index']} of {found['count']}.")


def page_find_clear():
    stop = _bridge_at((1, 5, 0))
    if stop:
        return stop
    _call("page.highlight", clear=True)
    return ActionResult.success("Cleared the highlights.")


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


def _page_of(target):
    """page.text of the active tab, or of the remembered tab for "it" (G1). A Google Doc is
    read through its text export (Part 4, E2): its page draws on a canvas."""
    if target == "it":
        tab = _referent_tab()
        if tab is not None:
            return _call("page.text", tabId=tab["id"], max=15000, timeout=15.0) or {}
    if _version() >= (1, 7, 0):
        doc = doc_text()
        if doc and doc.get("text", "").strip():
            return doc
    return _page_text()


def page_read(target="current"):
    missing = _need_bridge()
    if missing:
        return missing
    page = _page_of(target)
    text = " ".join((page.get("text") or "").split())
    if not text:
        return ActionResult.not_found("This page has no readable text.")
    cut = text[:900]
    cut = cut[: cut.rfind(".") + 1] or cut
    return ActionResult.success(cut)


def page_summarize(target="current"):
    missing = _need_bridge()
    if missing:
        return missing
    page = _page_of(target)
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
# │   PART 3 — questions, comments, video, translate, bookmarks, history,  │
# │            downloads, "it", search here, what can I do here            │
# └────────────────────────────────────────────────────────────────────────┘

# G1: the tab Kayra last opened or switched to, so "close it" / "go back to it" / "read it"
# mean THAT tab even after the user moved on. Bounded: one tab, ten minutes.
LAST_TAB = {"id": None, "title": "", "at": 0.0}
LAST_TAB_TTL_S = 600.0
CLOCK = time.monotonic          # replaced by the test rigs


def remember_tab(tab):
    if isinstance(tab, dict) and tab.get("id") is not None:
        LAST_TAB.update(id=tab["id"], title=tab.get("title") or "", at=CLOCK())


def _referent_tab():
    """The remembered tab if it is fresh and still open, else None."""
    if LAST_TAB["id"] is None or CLOCK() - LAST_TAB["at"] > LAST_TAB_TTL_S:
        return None
    return next((t for t in (_call("tabs.list") or []) if t.get("id") == LAST_TAB["id"]), None)


def _no_referent():
    return ActionResult.not_found("I'm not sure which tab you mean. Say its name, like close the "
                                  "YouTube tab.")


def page_ask(question):
    """C5: "what does this page say about pricing" — answered from the page's own text."""
    missing = _need_bridge()
    if missing:
        return missing
    page = _page_of("current")
    text = page.get("text") or ""
    if len(text.strip()) < 40:
        return ActionResult.not_found("This page has no readable text to answer from.")
    answer = ai.summarize(
        "You are Kayra. Answer the user's question using ONLY the web page below, in one to "
        "three spoken sentences. If the page does not say, answer exactly: The page doesn't say.",
        f"Question: {question}\n\nTitle: {page.get('title')}\nURL: {page.get('url')}\n\n"
        f"{ai.relevant(text, question, budget=4000)}",
        fallback="")
    if not answer:
        return ActionResult.failure("I couldn't reach a model to answer that. Say \"find <word> on "
                                    "this page\" to look for it yourself.")
    return ActionResult.success(answer)


def page_comments(summarize=False):
    max_comments = 30 if summarize else 12
    """C6: "read the comments" / "summarize the reviews" — YouTube, Reddit, Amazon, Flipkart,
    Hacker News, GitHub, or any page that marks up its reviews."""
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    # YouTube loads comments only once scrolled to: the first call scrolls, then poll.
    found = _ready("page.comments", lambda r: bool((r or {}).get("comments")), 6.0, max=max_comments) or {}
    comments, kind = found.get("comments") or [], found.get("kind", "comments")
    if not comments:
        return ActionResult.not_found(f"I don't see any {kind} on this page.")
    if summarize and len(comments) >= 2:
        spoken = ai.summarize(
            f"You are Kayra. Summarize what these {kind} say, in two to four spoken sentences: "
            "the overall feeling, what people agree on, and any common complaint. No quotes.",
            "\n".join(f"- {c}" for c in comments),
            fallback="")
        if spoken:
            return ActionResult.success(spoken)
    top = " ... ".join(_short(c, 160) for c in comments[:3])
    return ActionResult.success(f"The top {kind}: {top}")


def video_summary():
    """C6: "summarize this video" — the YouTube transcript, else the title and description."""
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    if _version() >= (1, 8, 0):
        data = _call("page.transcript", open=True, waitMs=5000, timeout=10.0) or {}
        if not data.get("supported"):
            return page_summarize()              # not YouTube: the page itself
    else:
        first = _call("page.transcript", open=True) or {}
        if not first.get("supported"):
            return page_summarize()
        data = _until(lambda: (lambda r: r if r.get("text") else None)(
            _call("page.transcript") or {}), 5.0) or (_call("page.transcript") or {})
    text, title = data.get("text") or "", data.get("title") or "this video"
    source = f"Transcript:\n{text[:14000]}" if text else \
        f"(No transcript is available.) Description:\n{data.get('description', '')}"
    spoken = ai.summarize(
        "You are Kayra. Summarize this YouTube video for the user in three to five spoken "
        "sentences: what it covers and the main takeaways. If you only have the description, "
        "say that you're going by the description. No markdown.",
        f"Title: {title}\n{source}",
        fallback=f"This is {title}. I couldn't reach a model to summarize it.")
    return ActionResult.success(spoken)


def page_translate(language="english"):
    """C8: the whole page through Google Translate, in this tab."""
    missing = _need_bridge()
    if missing:
        return missing
    from kayra.automation.common import LANGUAGES
    code = LANGUAGES.get(str(language).lower().strip())
    if code is None:
        return ActionResult.not_found(f"I don't know the language {language}.")
    tab = _call("tabs.active") or {}
    url = tab.get("url") or ""
    if not url.startswith("http"):
        return ActionResult.not_found("This tab isn't a web page I can translate.")
    _call("tabs.navigate", url=f"https://translate.google.com/translate?sl=auto&tl={code}&u={quote(url, safe='')}")
    return ActionResult.success(f"Here's the page in {language.capitalize()}.")


def _selection():
    return str(_call("page.selection") or "").strip()


def selection_explain():
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    picked = _selection()
    if not picked:
        return ActionResult.not_found("Nothing is selected. Highlight some text first.")
    spoken = ai.summarize("You are Kayra. Explain the selected text simply, in two or three spoken "
                          "sentences, as if to a curious friend. No markdown.", picked[:4000],
                          fallback="")
    return ActionResult.success(spoken) if spoken else \
        ActionResult.failure("I couldn't reach a model to explain it.")


def selection_translate(language="english"):
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    picked = _selection()
    if not picked:
        return ActionResult.not_found("Nothing is selected. Highlight some text first.")
    spoken = ai.summarize(f"Translate the user's text into {language}. Reply with the translation "
                          "only.", picked[:3000], fallback="", max_chars=1200)
    return ActionResult.success(spoken) if spoken else \
        ActionResult.failure("I couldn't reach a model to translate it.")


def bookmark_add(folder=""):
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    result = _call("bookmarks.add", folder=folder or "") or {}
    title = _short(result.get("title") or "this page")
    if result.get("already"):
        return ActionResult.success(f"{title} is already bookmarked.")
    return ActionResult.success(f"Bookmarked {title}" + (f" in {folder}." if folder else "."))


def bookmarks_search(query):
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    items = _call("bookmarks.find", text=query, max=5) or []
    if not items:
        return ActionResult.not_found(f"No bookmarks match {query}.")
    names = ", ".join(_short(b.get("title") or b.get("url"), 50) for b in items)
    return ActionResult.success(f"I found {len(items)}: {names}. Say open my bookmark for one of them.")


def bookmark_delete(query, confirmed_ids=None, action=None):
    """C9: always asks, naming what will go; "yes" removes exactly the bookmarks it named."""
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    if confirmed_ids is not None:
        removed = (_call("bookmarks.remove", ids=list(confirmed_ids)) or {}).get("removed", 0)
        return ActionResult.success(f"Removed {removed} bookmark{'s' if removed != 1 else ''}.")
    items = _call("bookmarks.find", text=query, max=5) or []
    if not items:
        return ActionResult.not_found(f"No bookmarks match {query}.")
    if action is None:
        return ActionResult.failure("I need to ask before removing bookmarks.")
    approved = Action(action.domain, action.action, target=action.target,
                      parameters={**action.parameters, "confirmed_ids": [b["id"] for b in items]},
                      raw=action.raw)
    names = ", ".join(_short(b.get("title") or b.get("url"), 40) for b in items)
    return ActionResult(Status.NEEDS_CONFIRMATION,
                        f"Remove {len(items)} bookmark{'s' if len(items) != 1 else ''}: {names}?",
                        {"confirm": approved})


_SPANS = {"today": "today's history", "hour": "the last hour of history",
          "15m": "the last 15 minutes of history"}


def _span_start(span):
    now = datetime.datetime.now()
    if span == "hour":
        return now - datetime.timedelta(hours=1)
    if span == "15m":
        return now - datetime.timedelta(minutes=15)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def history_clear(span="today", confirmed=False, action=None):
    """C10: always asks first. Only today / the last hour / 15 minutes — never all time."""
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    what = _SPANS.get(span, _SPANS["today"])
    if not confirmed:
        if action is None:
            return ActionResult.failure("I need to ask before clearing history.")
        approved = Action(action.domain, action.action, target=action.target,
                          parameters={**action.parameters, "confirmed": True}, raw=action.raw)
        return ActionResult(Status.NEEDS_CONFIRMATION,
                            f"Clear {what} in Chrome? That can't be undone.", {"confirm": approved})
    start = int(_span_start(span).timestamp() * 1000)
    _call("history.deleteRange", startTime=start)
    return ActionResult.success(f"Cleared {what}.")


def history_list(days=1):
    missing = _need_bridge()
    if missing:
        return missing
    items = _call("history.search", text="", days=days, max=8) or []
    if not items:
        return ActionResult.success("Nothing in your history for that time.")
    names = ", ".join(_short(h.get("title") or h.get("url"), 40) for h in items[:6])
    return ActionResult.success(f"Recently: {names}.")


def _downloads(n=5):
    return _call("downloads.list", max=n) or []


def _file_name(path):
    return os.path.basename(str(path or "")) or "the file"


def downloads_list():
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    items = _downloads(5)
    if not items:
        return ActionResult.success("Chrome has no downloads.")
    parts = []
    for d in items[:4]:
        name = _file_name(d.get("filename"))
        if d.get("state") == "in_progress":
            total, got = d.get("total") or 0, d.get("bytes") or 0
            pct = f", {round(100 * got / total)} percent" if total else ""
            parts.append(f"{name} ({'paused' if d.get('paused') else 'downloading'}{pct})")
        elif d.get("state") == "interrupted":
            parts.append(f"{name} (failed)")
        else:
            parts.append(name)
    return ActionResult.success("Your downloads: " + ", ".join(parts) + ".")


# A downloaded file Kayra will not open for you: it would RUN something.
_EXECUTABLE = re.compile(r"\.(?:sh|run|bin|appimage|exe|msi|bat|cmd|com|deb|rpm|desktop|jar|py|pl|"
                         r"apk|pkg(?:\.tar\.\w+)?|zst)$", re.I)


def download_open(confirmed_id=None, action=None):
    """C11: opens the newest finished download — always asks, never an executable."""
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    done = [d for d in _downloads(10) if d.get("state") == "complete" and d.get("exists") is not False]
    if confirmed_id is not None:
        done = [d for d in done if d.get("id") == confirmed_id]
    if not done:
        return ActionResult.not_found("I don't see a finished download to open.")
    item, name = done[0], _file_name(done[0].get("filename"))
    if _EXECUTABLE.search(name) or item.get("danger") not in (None, "safe", "accepted"):
        return ActionResult.blocked(f"{name} is a program or was flagged by Chrome, so I won't open "
                                    "it. Open it yourself if you trust it.")
    if confirmed_id is None:
        if action is None:
            return ActionResult.failure("I need to ask before opening a downloaded file.")
        approved = Action(action.domain, action.action, target=action.target,
                          parameters={**action.parameters, "confirmed_id": item["id"]}, raw=action.raw)
        return ActionResult(Status.NEEDS_CONFIRMATION, f"Open {name}?", {"confirm": approved})
    L = _L()
    if L._have("xdg-open") and L.RUNNER.spawn(["xdg-open", item["filename"]]):
        return ActionResult.success(f"Opening {name}.")
    return ActionResult.failure(f"I couldn't open {name}.")


def download_show():
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    items = _downloads(1)
    if not items:
        return ActionResult.not_found("Chrome has no downloads.")
    _call("downloads.show", id=items[0]["id"])
    return ActionResult.success(f"Here's {_file_name(items[0].get('filename'))} in its folder.")


def download_control(verb):
    stop = _bridge_at((1, 6, 0))
    if stop:
        return stop
    active = [d for d in _downloads(10) if d.get("state") == "in_progress"]
    if verb == "resume":
        active = [d for d in active if d.get("paused")] or active
    elif verb == "pause":
        active = [d for d in active if not d.get("paused")]
    if not active:
        return ActionResult.not_found("Nothing is downloading." if verb != "resume" else
                                      "No download is paused.")
    item = active[0]
    _call(f"downloads.{verb}", id=item["id"])
    name = _file_name(item.get("filename"))
    return ActionResult.success({"pause": f"Paused {name}.", "resume": f"Resumed {name}.",
                                 "cancel": f"Cancelled {name}."}[verb])


# G2: search INSIDE what is in front. Each site's own search address; the editors use their
# own find (Ctrl+F); any page with a search box gets typed into; otherwise Google.
_SITE_SEARCH = (
    (r"(^|\.)youtube\.com$", "YouTube", lambda host, path, q: f"https://www.youtube.com/results?search_query={quote_plus(q)}"),
    (r"^mail\.google\.com$", "your mail", lambda host, path, q: f"https://mail.google.com/mail/u/0/#search/{quote(q)}"),
    (r"^drive\.google\.com$", "Drive", lambda host, path, q: f"https://drive.google.com/drive/search?q={quote_plus(q)}"),
    (r"^github\.com$", "GitHub", lambda host, path, q: (
        f"https://github.com/{'/'.join(path.strip('/').split('/')[:2])}/search?q={quote_plus(q)}"
        if len(path.strip("/").split("/")) >= 2 and path.strip("/").split("/")[0] not in ("search", "settings")
        else f"https://github.com/search?q={quote_plus(q)}")),
    (r"(^|\.)amazon\.[a-z.]+$", "Amazon", lambda host, path, q: f"https://{host}/s?k={quote_plus(q)}"),
    (r"(^|\.)flipkart\.com$", "Flipkart", lambda host, path, q: f"https://www.flipkart.com/search?q={quote_plus(q)}"),
    (r"(^|\.)wikipedia\.org$", "Wikipedia", lambda host, path, q: f"https://{host}/w/index.php?search={quote_plus(q)}"),
    (r"(^|\.)reddit\.com$", "Reddit", lambda host, path, q: f"https://www.reddit.com/search/?q={quote_plus(q)}"),
    (r"(^|\.)stackoverflow\.com$", "Stack Overflow", lambda host, path, q: f"https://stackoverflow.com/search?q={quote_plus(q)}"),
    (r"(^|\.)google\.[a-z.]+$", "Google", lambda host, path, q: f"https://www.google.com/search?q={quote_plus(q)}"
     if path.startswith("/search") else None),
)
_EDITORS = re.compile(r"^docs\.google\.com$")


def search_here(query):
    """G2: "search for Ravi" — in the sheet, doc, inbox, video site or shop in front."""
    missing = _need_bridge()
    if missing:
        return missing
    tab = _call("tabs.active") or {}
    url = tab.get("url") or ""
    host, path = _host(url), urlsplit(url).path or "/"
    for pattern, label, build in _SITE_SEARCH:
        if re.search(pattern, host):
            target = build(host, path, query)
            if target:
                _call("tabs.navigate", url=target)
                where = "this repo" if label == "GitHub" and "/search?q=" in target and \
                    not target.startswith("https://github.com/search") else label
                return ActionResult.success(f"Searching {where} for {query}.")
    if _EDITORS.search(host):
        L = _L()
        _focus_chrome()
        L.send_keys("ctrl+f")
        L.type_text(query)
        kind = "sheet" if "/spreadsheets/" in url else "document" if "/document/" in url else "file"
        return ActionResult.success(f"Searching this {kind} for {query}.")
    typed = (_call("page.type", field="search", text=query, submit=True) or {}) if url.startswith("http") else {}
    if typed.get("typed"):
        return ActionResult.success(f"Searching this site for {query}.")
    return open_in(f"https://www.google.com/search?q={quote_plus(query)}", "tab",
                   f"a Google search for {query}")


# G4: "what can I do here?" — a few things worth saying on the site in front.
_HELP = (
    (r"^mail\.google\.com$", "In Gmail you can say: open my latest email, read this email, reply, "
                             "archive, go to Sent, search for invoices, or draft an email to someone."),
    (r"^calendar\.google\.com$", "In Calendar you can say: what's on my calendar today, or add "
                                 "standup tomorrow at 10am."),
    (r"^meet\.google\.com$", "In a call you can say: mute me, turn off my camera, share the LM "
                             "Studio window, stop sharing, raise my hand, or leave the call."),
    (r"(^|\.)youtube\.com$", "On YouTube you can say: play the second video, summarize this video, "
                             "read the comments, search for lofi, pause, or skip forward."),
    (r"^docs\.google\.com$", "In Docs and Sheets you can say: search this sheet for a name, read "
                             "this page, summarize this page, or copy the link."),
    (r"^web\.whatsapp\.com$", "In WhatsApp you can say: message Rahul saying running late, or "
                              "open a chat by its name."),
    (r"(^|\.)google\.[a-z.]+$", "On search results you can say: open the third result, what are the "
                                "results, or open the first result in a new tab."),
    (r"(^|\.)(amazon\.[a-z.]+|flipkart\.com)$", "Here you can say: search for headphones, open the "
                                                "second product, or summarize the reviews."),
    (r"^github\.com$", "On GitHub you can say: search this repo for a word, read this page, or "
                       "open the first result."),
)


def page_help():
    missing = _need_bridge()
    if missing:
        return missing
    host = _host((_call("tabs.active") or {}).get("url"))
    for pattern, hint in _HELP:
        if re.search(pattern, host):
            return ActionResult.success(hint)
    return ActionResult.success("On any page you can say: summarize this page, what does this page "
                                "say about something, find a word on this page, open the third "
                                "result, bookmark this page, translate this page, or what can I "
                                "click.")


# ┌────────────────────────────────────────────────────────────────────────┐
# │             PART 4 — GOOGLE WORKSPACE (Sheets, Docs, Slides,           │
# │                 Drive, Gmail, Calendar, Meet, Keep, Forms)             │
# └────────────────────────────────────────────────────────────────────────┘
# File NAMES are read from the page in Chrome while acting and matched HERE, locally, with no
# model (decision D3, 2026-11-10): never stored, never sent to a cloud model. Contents are read
# only when the user asks a question about them.

_WS_HOME = {"sheet": "https://docs.google.com/spreadsheets/u/0/?q={q}",
            "doc": "https://docs.google.com/document/u/0/?q={q}",
            "slides": "https://docs.google.com/presentation/u/0/?q={q}",
            "form": "https://docs.google.com/forms/u/0/?q={q}",
            "file": "https://drive.google.com/drive/search?q={q}",
            "pdf": "https://drive.google.com/drive/search?q=type:pdf%20{q}"}
_WS_NEW = {"sheet": "https://docs.google.com/spreadsheets/create",
           "doc": "https://docs.google.com/document/create",
           "slides": "https://docs.google.com/presentation/create",
           "form": "https://docs.google.com/forms/create"}
_WS_NOUN = {"sheet": "sheet", "doc": "document", "slides": "presentation", "form": "form",
            "file": "file", "pdf": "PDF"}
_NAME_STOP = frozenset("the a an my our of for and to in on about with called named file sheet "
                       "spreadsheet doc document deck presentation slides form pdf".split())


_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}


def spoken_numbers(text):
    """'budget twenty twenty six' -> 'budget 2026'; 'q three' -> 'q 3'; 'two thousand twenty
    five' -> '2025'. Speech recognisers write numbers in words often enough to matter here."""
    words, out, i = str(text or "").lower().split(), [], 0

    def pair(j):                                   # a number below 100 at j -> (value, used)
        if j < len(words) and words[j] in _TENS:
            if j + 1 < len(words) and words[j + 1] in _UNITS and _UNITS[words[j + 1]] < 10:
                return _TENS[words[j]] + _UNITS[words[j + 1]], 2
            return _TENS[words[j]], 1
        if j < len(words) and words[j] in _UNITS:
            return _UNITS[words[j]], 1
        return None, 0

    while i < len(words):
        if words[i] in ("two", "one") and i + 1 < len(words) and words[i + 1] == "thousand":
            base, j = (2000 if words[i] == "two" else 1000), i + 2
            if j < len(words) and words[j] == "and":
                j += 1
            rest, used = pair(j)
            out.append(str(base + (rest or 0)))
            i = j + used
            continue
        first, used = pair(i)
        if first is not None and 10 <= first <= 99:
            j = i + used
            if j + 1 < len(words) and words[j] == "oh" and words[j + 1] in _UNITS and _UNITS[words[j + 1]] < 10:
                out.append(f"{first}{_UNITS[words[j + 1]]:02d}")     # "nineteen oh five" -> 1905
                i = j + 2
                continue
            second, used2 = pair(j)
            if second is not None and second >= 10:
                out.append(f"{first}{second:02d}")   # "twenty twenty six" -> 2026, "nineteen oh five"
                i += used + used2
                continue
        if first is not None:
            out.append(str(first))
            i += used
            continue
        out.append(words[i])
        i += 1
    return " ".join(out)


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", spoken_numbers(text)) if w not in _NAME_STOP]


def _same_word(a, b):
    """Exact, or a near spelling the recogniser produced ("budjet" / "budget") for words of 4+."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or a.isdigit() or b.isdigit():
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def rank_titles(name, items):
    """
    [(score, item)] best first. Exact title 100; the whole phrase inside the title 80; every
    word present 60 (+ shorter titles first); some words: 40 × the share present. Local and
    deterministic — names never leave the machine.
    """
    want = " ".join(_words(name))
    words = want.split()
    ranked = []
    for item in items:
        title = " ".join(_words(item.get("title", "")))
        if not title or not words:
            continue
        title_words = title.split()
        near = [any(_same_word(w, t) or (len(w) >= 3 and t.startswith(w)) for t in title_words)
                for w in words]
        if title == want:
            score = 100.0
        elif want in title:
            score = 80.0
        elif all(w in title_words or w in title for w in words):
            score = 60.0
        elif all(near):
            score = 55.0                           # every word, allowing a misheard spelling
        else:
            score = 40.0 * sum(near) / len(words)
        if score:
            ranked.append((score - min(len(title), 200) / 1000.0, item))
    ranked.sort(key=lambda pair: -pair[0])
    return ranked


def _file_list(url):
    # Reuse a tab that is already on a Google file list / Drive, instead of a new tab each time.
    host = _host((_call("tabs.active") or {}).get("url"))
    if host in ("docs.google.com", "drive.google.com") and not _doc_ids((_call("tabs.active") or {}).get("url"))[0]:
        _call("tabs.navigate", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    else:
        _call("tabs.open", url=url, where="tab", wait=True, timeoutMs=20000, timeout=25.0)
    return _ready("page.docList", bool, 6.0, max=40) or []


def ws_open(name, kind="sheet"):
    """E1–E4: "open my budget sheet" — the best title match in Sheets/Docs/Slides/Forms/Drive."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    noun = _WS_NOUN.get(kind, "file")
    items = _file_list(_WS_HOME.get(kind, _WS_HOME["file"]).format(q=quote(name)))
    ranked = rank_titles(name, items)
    if not ranked:
        return ActionResult.not_found(f"I don't see a {noun} called {name}. The search is open.")
    best_score, best = ranked[0]
    close = [item for score, item in ranked[1:4] if best_score - score < 1.0 and best_score < 80]
    if close:
        names = ", ".join(_short(i["title"], 40) for i in [best] + close)
        return ActionResult.ambiguous(f"I found {len(close) + 1} {noun}s: {names}. Which one?",
                                      candidates=[best["title"]] + [i["title"] for i in close])
    _call("tabs.navigate", url=best["url"])
    remember_tab({"id": (_call("tabs.active") or {}).get("id"), "title": best["title"]})
    more = f", the best of {len(ranked)} matches" if len(ranked) > 1 else ""
    return ActionResult.success(f"Opened {_short(best['title'])}{more}.")


def ws_search(query, kind="file", open_best=False):
    """'search my docs for offer letter' lists the matches; 'find the PDF Ravi sent me' opens
    the one clear match."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    noun = _WS_NOUN.get(kind, "file")
    items = _file_list(_WS_HOME.get(kind, _WS_HOME["file"]).format(q=quote(query)))
    if not items:
        return ActionResult.not_found(f"No {noun}s match {query}. The search is open.")
    ranked = rank_titles(query, items) or [(0.0, i) for i in items]
    if open_best and (len(items) == 1 or ranked[0][0] >= 60):
        _call("tabs.navigate", url=ranked[0][1]["url"])
        return ActionResult.success(f"Opened {_short(ranked[0][1]['title'])}.")
    names = ", ".join(_short(i["title"], 40) for _s, i in ranked[:4])
    return ActionResult.success(f"I found {len(items)} {noun}{'s' if len(items) != 1 else ''}: {names}. "
                                f"Say open the {noun} called one of them.")


def ws_create(kind="sheet", title=""):
    """E1/E2/E3/E8: a new Sheet / Doc / presentation / form, named when a name was said."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    url = _WS_NEW.get(kind)
    if url is None:
        return ActionResult.failure("I can create a sheet, a document, a presentation or a form.")
    tab = _call("tabs.open", url=url, where="tab", wait=True, timeoutMs=25000, timeout=30.0) or {}
    remember_tab(tab)
    noun = _WS_NOUN[kind]
    if not title:
        return ActionResult.success(f"Here's a new {noun}.")
    # The title box is Docs' own "Rename" control; typed with the real keyboard so the editor
    # sees genuine key events.
    if not _until(lambda: (_call("page.click", text="Rename", nth=1) or {}).get("clicked"), 6.0):
        return ActionResult.success(f"Here's a new {noun}. I couldn't reach its title to name it {title}.")
    L = _L()
    _focus_chrome()
    L.send_keys("ctrl+a")
    L.type_text(title)
    L.send_keys("enter")
    return ActionResult.success(f"Created a new {noun} called {title}.")


def _doc_ids(url):
    """('sheet'|'doc'|'slides'|None, file id, gid) from a docs.google.com address."""
    m = re.search(r"docs\.google\.com/(spreadsheets|document|presentation)/d/([\w-]{20,})", str(url or ""))
    if not m:
        return None, None, None
    gid = re.search(r"[#&?]gid=(\d+)", str(url))
    kind = {"spreadsheets": "sheet", "document": "doc", "presentation": "slides"}[m.group(1)]
    return kind, m.group(2), gid.group(1) if gid else "0"


def _active_doc():
    tab = _call("tabs.active") or {}
    return (*_doc_ids(tab.get("url")), tab)


def sheet_rows():
    """The visible sheet as rows (list of lists), fetched in the page with the user's session."""
    kind, file_id, gid, _tab = _active_doc()
    if kind != "sheet":
        return None, "Open a Google Sheet first."
    for url in (f"https://docs.google.com/spreadsheets/d/{file_id}/gviz/tq?tqx=out:csv&headers=1&gid={gid}",
                f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv&gid={gid}"):
        try:
            got = _call("page.fetchText", url=url, timeout=20.0) or {}
        except browser_bridge.BridgeError:
            got = {}
        if got.get("ok") and got.get("text", "").strip():
            return list(csv.reader(io.StringIO(got["text"]))), None
    return None, "I couldn't read this sheet."


def _number(value):
    text = re.sub(r"[₹$€£,%\s]", "", str(value or ""))
    text = text[1:-1] if text.startswith("(") and text.endswith(")") else text
    try:
        return float(text)
    except ValueError:
        return None


def _column_index(spec, header):
    spec = str(spec or "").strip().lower()
    if re.fullmatch(r"[a-z]{1,3}", spec) and not any(spec == h.strip().lower() for h in header):
        index = 0
        for ch in spec:
            index = index * 26 + (ord(ch) - 96)
        return index - 1
    for i, h in enumerate(header):
        if h.strip().lower() == spec:
            return i
    for i, h in enumerate(header):
        if spec and spec in h.strip().lower():
            return i
    return None


def _fmt(n):
    return f"{n:,.2f}".rstrip("0").rstrip(".") if n != int(n) else f"{int(n):,}"


def _split_header(rows):
    """(header, body): the header is the first row with two or more filled cells that are
    mostly NOT numbers — a title line or blank rows above the table are skipped."""
    for index, row in enumerate(rows[:6]):
        filled = [c.strip() for c in row if c.strip()]
        if len(filled) >= 2 and sum(_number(c) is None for c in filled) > len(filled) / 2:
            return row, rows[index + 1:]
    return rows[0], rows[1:]


def sheet_calc(column, op="sum", where=None):
    """E1: 'what's the total of column C' / 'average of the amount column' / 'how many rows'.
    Computed here, from the sheet's own values — never by a model."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    rows, problem = sheet_rows()
    if rows is None:
        return ActionResult.not_found(problem)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ActionResult.success("This sheet is empty.")
    header, body = _split_header(rows)
    if where:
        col = _column_index(where[0], header)
        if col is None:
            return ActionResult.not_found(f"I don't see a column {where[0]}.")
        want = str(where[1]).strip().lower()
        body = [r for r in body if col < len(r) and r[col].strip().lower() == want]
        if not body:
            return ActionResult.not_found(f"No rows where {where[0]} is {where[1]}.")
    if op == "rows":
        return ActionResult.success(f"This sheet has {len(body)} rows below the header.")
    index = _column_index(column, header)
    if index is None or index >= max(len(r) for r in rows):
        return ActionResult.not_found(f"I don't see a column {column}.")
    label = header[index].strip() if index < len(header) and header[index].strip() else f"column {column.upper()}"
    values = [_number(r[index]) for r in body if index < len(r)]
    numbers = [v for v in values if v is not None]
    if op == "count":
        filled = sum(1 for r in body if index < len(r) and r[index].strip())
        return ActionResult.success(f"{label} has {filled} filled cells.")
    if not numbers:
        return ActionResult.not_found(f"{label} has no numbers.")
    value = {"sum": sum(numbers), "avg": sum(numbers) / len(numbers), "max": max(numbers),
             "min": min(numbers)}[op]
    word = {"sum": "total", "avg": "average", "max": "highest", "min": "lowest"}[op]
    skipped = len(values) - len(numbers)
    note = f" I skipped {skipped} cell{'s' if skipped != 1 else ''} that weren't numbers." if skipped else ""
    scope = f" where {where[0]} is {where[1]}" if where else ""
    return ActionResult.success(f"The {word} of {label}{scope} is {_fmt(value)}, over {len(numbers)} rows.{note}")


def sheet_row(number, headers=False):
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    rows, problem = sheet_rows()
    if rows is None:
        return ActionResult.not_found(problem)
    if not rows:
        return ActionResult.success("This sheet is empty.")
    header = rows[0]
    if headers:
        names = [h for h in header if h.strip()]
        return ActionResult.success("The columns are: " + ", ".join(names) + "." if names else
                                    "The first row is empty.")
    index = int(number) - 1
    if not 0 <= index < len(rows):
        return ActionResult.not_found(f"There's no row {number}; the sheet has {len(rows)} rows.")
    row = rows[index]
    if index == 0:
        return ActionResult.success("Row 1 is the header: " + ", ".join(c for c in row if c.strip()) + ".")
    parts = [f"{(header[i].strip() if i < len(header) and header[i].strip() else chr(65 + i) if i < 26 else str(i + 1))} {c.strip()}"
             for i, c in enumerate(row) if c.strip()]
    return ActionResult.success(f"Row {number}: " + ", ".join(parts) + "." if parts else f"Row {number} is empty.")


def sheet_cell(cell):
    """E1: 'go to cell B12' — Sheets' own Name Box, typed with the real keyboard."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    kind, _id, _gid, _tab = _active_doc()
    if kind != "sheet":
        return ActionResult.not_found("Open a Google Sheet first.")
    if not (_call("page.focus", selector="#t-name-box, input[aria-label='Name Box'], input[aria-label='Name box']") or {}).get("focused"):
        return ActionResult.not_found("I can't find the sheet's Name Box.")
    L = _L()
    _focus_chrome()
    L.type_text(cell)
    L.send_keys("enter")
    return ActionResult.success(f"Here's {cell}.")


def doc_text(max_chars=20000):
    """The open Google Doc's text, exported in the page (Docs draws on a canvas, so the page's
    own text is nearly empty). None when the active tab is not a Doc."""
    kind, file_id, _gid, tab = _active_doc()
    if kind != "doc":
        return None
    try:
        got = _call("page.fetchText", url=f"https://docs.google.com/document/d/{file_id}/export?format=txt",
                    max=max_chars, timeout=20.0) or {}
    except browser_bridge.BridgeError:
        got = {}
    if not got.get("ok"):
        return None
    return {"title": re.sub(r"\s+-\s+Google Docs$", "", tab.get("title") or ""), "url": tab.get("url"),
            "text": got.get("text", "")}


def ws_share(email):
    """E2/E4: opens Share with the address typed in. Kayra stops there; you press Send."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    kind, _id, _gid, _tab = _active_doc()
    if kind is None:
        return ActionResult.not_found("Open the document, sheet or presentation first.")
    if not _until(lambda: (_call("page.click", text="Share", nth=1) or {}).get("clicked"), 4.0):
        return ActionResult.not_found("I can't find the Share button.")
    # The Share dialog opens with "Add people" focused: wait for the dialog, then type.
    if not _wait_for(selector="div[role=dialog] iframe, div[role=dialog]", timeout_ms=5000):
        return ActionResult.not_found("The Share dialog didn't open.")
    L = _L()
    _focus_chrome()
    L.type_text(email)
    return ActionResult.success(f"I've opened Share with {email} filled in. Check who and what "
                                "access, then press Send yourself.")


def slides_present():
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    kind, _id, _gid, _tab = _active_doc()
    if kind != "slides":
        return ActionResult.not_found("Open a Google Slides presentation first.")
    _focus_chrome()
    _L().send_keys("ctrl+f5")
    return ActionResult.success("Presenting. Say next slide, previous slide, or end the slideshow.")


def slides_nav(verb):
    missing = _need_bridge()
    if missing:
        return missing
    kind, _id, _gid, _tab = _active_doc()
    if kind != "slides":
        return ActionResult.not_found("No presentation is open.")
    _focus_chrome()
    _L().send_keys({"next": "right", "previous": "left", "end": "escape"}[verb])
    return ActionResult.success({"next": "Next slide.", "previous": "Previous slide.",
                                 "end": "Ended the slideshow."}[verb])


def ws_recent():
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    items = _file_list("https://drive.google.com/drive/recent")
    if not items:
        return ActionResult.success("Here are your recent files in Drive.")
    names = ", ".join(_short(i["title"], 40) for i in items[:5])
    return ActionResult.success(f"Your recent files: {names}.")


def ws_download():
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    tab = _call("tabs.active") or {}
    url = tab.get("url") or ""
    kind, file_id, _gid = _doc_ids(url)
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=)([\w-]{20,})", url)
    if kind == "sheet":
        target = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
    elif kind == "doc":
        target = f"https://docs.google.com/document/d/{file_id}/export?format=docx"
    elif kind == "slides":
        target = f"https://docs.google.com/presentation/d/{file_id}/export/pptx"
    elif m:
        target = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    else:
        return ActionResult.not_found("Open the Drive file, document or sheet first.")
    _call("tabs.open", url=target, where="next", active=False)
    return ActionResult.success("Downloading it. Say what did I download to check on it.")


def keep_note(text):
    """E8: a note in Google Keep — "Take a note…", typed with the real keyboard, closed."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    try:
        _call("tabs.activate", match="keep.google.com")
    except browser_bridge.BridgeError:
        _call("tabs.open", url="https://keep.google.com/", where="tab", wait=True, timeoutMs=20000, timeout=25.0)
    if not _until(lambda: (_call("page.click", text="Take a note", nth=1) or {}).get("clicked"), 6.0):
        return ActionResult.not_found("Keep is open, but I can't find Take a note.")
    L = _L()
    _focus_chrome()
    L.type_text(text)
    L.send_keys("escape")                 # Keep saves and closes the note on Escape
    return ActionResult.success("Saved to Keep.")


def form_responses(name):
    """E8: a form's responses — the form by name, then its Responses tab."""
    result = ws_open(name, "form")
    if not result.ok:
        return result
    tab = _call("tabs.active") or {}
    url = str(tab.get("url") or "").split("#")[0]
    if "/forms/d/" in url:
        _call("tabs.navigate", url=re.sub(r"/(edit|viewform)?$", "", url.rstrip("/")) + "/edit#responses")
    return ActionResult.success(result.message.replace("Opened", "Here are the responses for", 1))


def classroom_due():
    missing = _need_bridge()
    if missing:
        return missing
    _call("tabs.open", url="https://classroom.google.com/a/not-turned-in/all", where="tab", wait=True,
          timeoutMs=20000, timeout=25.0)
    _wait_for(text="Assigned", timeout_ms=4000)
    page = _page_text(8000)
    spoken = ai.summarize("You are Kayra. From this Google Classroom 'to-do' page, list the assignments "
                          "that are due, nearest first, with their class and due date, in at most four "
                          "spoken sentences. If there are none, say so.", page.get("text", "")[:8000],
                          fallback="I've opened your Classroom to-do list.")
    return ActionResult.success(spoken)


# E5 Gmail extras.
def email_search(query):
    missing = _need_bridge()
    if missing:
        return missing
    url = f"https://mail.google.com/mail/u/0/#search/{quote(query)}"
    try:
        _call("tabs.activate", match="mail.google.com")
        _call("tabs.navigate", url=url)
    except browser_bridge.BridgeError:
        _call("tabs.open", url=url, where="tab")
    return ActionResult.success(f"Here's your mail for {query}." if not query.startswith(("is:", "in:", "label:"))
                                else "Here they are.")


def email_compose():
    return open_in("https://mail.google.com/mail/?view=cm&fs=1", "tab", "a new email")


def email_label(name):
    missing = _need_bridge()
    if missing:
        return missing
    url = f"https://mail.google.com/mail/u/0/#label/{quote(name.strip().title())}"
    try:
        _call("tabs.activate", match="mail.google.com")
        _call("tabs.navigate", url=url)
    except browser_bridge.BridgeError:
        _call("tabs.open", url=url, where="tab")
    return ActionResult.success(f"Here's your {name} label.")


# E6 Calendar extras.
def calendar_next(join=False):
    """'what's my next meeting' / 'join my next meeting' — Calendar's schedule view."""
    stop = _bridge_at((1, 7, 0))
    if stop:
        return stop
    url = "https://calendar.google.com/calendar/u/0/r/agenda"
    try:
        _call("tabs.activate", match="calendar.google.com")
        _call("tabs.navigate", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    except browser_bridge.BridgeError:
        _call("tabs.open", url=url, wait=True, timeoutMs=20000, timeout=25.0)
    first = _ready("calendar.next", lambda r: bool((r or {}).get("events")), 5.0) or {}
    if not first.get("events"):
        return ActionResult.success("You have nothing coming up on your calendar.")
    title = _short(first.get("title") or "your next event", 90)
    if not join:
        return ActionResult.success(f"Next up: {title}.")
    found = _ready("calendar.next", lambda r: bool((r or {}).get("meet")), 4.0, open=True) or {}
    link = found.get("meet")
    if not link:
        return ActionResult.not_found(f"Your next event, {title}, has no Google Meet link.")
    return meet_join(link)


# E7 Meet extras.
def meet_admit(everyone=True):
    """E7: "admit everyone" — Meet's own Admit all (or Admit), and its second "Admit all"."""
    missing = _need_bridge()
    if missing:
        return missing
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("You're not in a Google Meet call.")
    labels = ["Admit all", "Admit"] if everyone else ["Admit"]
    clicked = _click_first(labels)
    if not clicked and _click_first(["View all"]):          # the waiting list is collapsed
        clicked = _until(lambda: _click_first(labels), 2.0)
    if not clicked:
        return ActionResult.not_found("Nobody is waiting to join.")
    if clicked == "Admit all":
        _until(lambda: _click_first(["Admit all"]), 1.5)    # Meet's own "Admit all?" check
    return ActionResult.success("Let them in.")


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
    remember_tab(tab)
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
    when = datetime.date.today()
    if day == "tomorrow":
        when += datetime.timedelta(days=1)
    elif day == "day after tomorrow":
        when += datetime.timedelta(days=2)
    elif day in _WEEKDAYS:                       # the next one; today counts if it IS that day
        when += datetime.timedelta(days=(_WEEKDAYS.index(day) - when.weekday()) % 7)
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

# ── WhatsApp Web (Part 5, H0.4) ──
# Chats are matched by `semantic_match` (loose names, recency for ties, ask when two are
# close). Message text is read only when asked, summarized by the LOCAL model only, and never
# logged. Sending ALWAYS asks first, naming the exact chat and the text.
WHATSAPP = "https://web.whatsapp.com/"
EMBEDDER = None                  # set to semantic_match.local_embedder() lazily; tests replace it


def _embedder():
    global EMBEDDER
    if EMBEDDER is None:
        from kayra.automation import semantic_match
        EMBEDDER = semantic_match.local_embedder() or False
    return EMBEDDER or None


def _wa_ready():
    """WhatsApp open and its chat list loaded: the list, or an ActionResult to return."""
    stop = _bridge_at((1, 9, 0))
    if stop:
        return None, stop
    _ensure_tab("web.whatsapp.com", WHATSAPP)
    listing = _ready("chat.list", lambda r: bool((r or {}).get("ready")), 15.0, max=60) or {}
    if not listing.get("ready"):
        return None, ActionResult.failure("WhatsApp isn't ready. Is it open and logged in?")
    return listing.get("chats") or [], None


def _wa_find(name):
    """(chat dict, None) | (None, ActionResult). Visible list first, then WhatsApp's own search."""
    from kayra.automation import semantic_match as S
    chats, problem = _wa_ready()
    if problem:
        return None, problem

    def decide(rows):
        cands = [S.Candidate(c["name"], recent=i, data=c) for i, c in enumerate(rows)]
        return S.match(name, cands, embed=_embedder())

    found = decide(chats)
    searched = False
    if found.decision != S.PICK:
        # Not clearly in the visible list: WhatsApp's own search, then match its results.
        if (_call("page.type", field="search", text=name, clear=True) or {}).get("typed"):
            searched = True
            listing = _ready("chat.list", lambda r: bool((r or {}).get("chats")), 4.0, max=30) or {}
            again = decide(listing.get("chats") or [])
            if again.decision != S.NONE or found.decision == S.NONE:
                found = again
    if found.decision == S.PICK:
        return found.best.data, None
    if searched:
        # Nothing will be opened: empty WhatsApp's search box again, or every later chat list
        # (who messaged me, the next open) would only see this search's results.
        _call("page.type", field="search", text="", clear=True)
    if found.decision == S.ASK:
        a, b = [c.name for c in found.top(2)]
        return None, ActionResult(Status.AMBIGUOUS, f"Do you mean {a} or {b}?", {"candidates": [a, b]})
    return None, ActionResult.not_found(f"I don't see a chat called {name} in WhatsApp.")


def _wa_open(chat):
    """Opens `chat` (a chat.list row) and waits until its conversation is the one shown."""
    _call("chat.open", name=chat["name"])
    shown = _ready("chat.messages", lambda r: (r or {}).get("chat") == chat["name"], 4.0, limit=1) or {}
    return shown.get("chat") == chat["name"]


def wa_open(name):
    """H0.4: "open the PR group" / "open XYZ chat" inside WhatsApp."""
    chat, problem = _wa_find(name)
    if problem:
        return problem
    if not _wa_open(chat):
        return ActionResult.failure(f"I found {chat['name']} but couldn't open it.")
    unread = f" {chat['unread']} unread." if chat.get("unread") else ""
    return ActionResult.success(f"Opened {chat['name']}.{unread}")


def wa_unread():
    """H0.4: "who messaged me" — the chats with unread messages, by name and count."""
    chats, problem = _wa_ready()
    if problem:
        return problem
    waiting = [c for c in chats if c.get("unread")]
    if not waiting:
        return ActionResult.success("No unread WhatsApp messages.")
    parts = [f"{c['name']} ({c['unread']})" for c in waiting[:6]]
    more = f", and {len(waiting) - 6} more chats" if len(waiting) > 6 else ""
    return ActionResult.success(f"Unread on WhatsApp: {', '.join(parts)}{more}. Say read and a name "
                                "to hear one.")


def _today_marks():
    now = datetime.date.today()
    return {f"{now.day}/{now.month}/{now.year}", f"{now.month}/{now.day}/{now.year}",
            f"{now.day:02d}/{now.month:02d}/{now.year}", f"{now.month:02d}/{now.day:02d}/{now.year}"}


def wa_read(name, today=False):
    """H0.4: "read the PR group" / "what did they say in the core team today"."""
    if name in ("", "current", "this", "it"):
        stop = _bridge_at((1, 9, 0))
        if stop:
            return stop
        data = _call("chat.messages", limit=30) or {}
        if not data.get("chat"):
            return ActionResult.not_found("No chat is open in WhatsApp.")
    else:
        chat, problem = _wa_find(name)
        if problem:
            return problem
        if not _wa_open(chat):
            return ActionResult.failure(f"I found {chat['name']} but couldn't open it.")
        data = _call("chat.messages", limit=30) or {}
    messages = data.get("messages") or []
    if today:
        marks = _today_marks()
        messages = [m for m in messages if m.get("date") in marks]
    title = data.get("chat") or name
    if not messages:
        return ActionResult.success(f"No messages in {title}{' today' if today else ''}.")
    lines = [f"{'You' if m.get('out') else (m.get('from') or 'They')}: {m.get('text')}" for m in messages]
    if len(lines) <= 4:
        return ActionResult.success(f"In {title}: " + " ... ".join(_short(l, 200) for l in lines))
    spoken = ai.summarize(
        "You are Kayra. Summarize this WhatsApp conversation for the user in two to four spoken "
        "sentences: who said what that matters, any question for the user, any plan or time. "
        "Never read out codes, OTPs or passwords.",
        f"Chat: {title}\n" + "\n".join(lines[-30:]), fallback="", local_only=True)
    if not spoken:                       # no local model: the latest few, not a cloud summary
        return ActionResult.success(f"The latest in {title}: " + " ... ".join(_short(l, 160) for l in lines[-3:]))
    return ActionResult.success(spoken)


def message_send(contact, text, confirmed_chat=None, action=None):
    """
    H0.4: "tell Rahul I'm running late". Finds and OPENS the chat first, then ALWAYS asks —
    "Send 'I'm running late' to Rahul Sharma?" — naming the chat it resolved, not the words
    said. On yes: the same chat is checked to be the open one, the text typed and read back,
    then sent, and the sent message is looked for.
    """
    if not contact or not text:
        return ActionResult.failure("Who should I message, and what should I say?")
    if confirmed_chat is None:
        chat, problem = _wa_find(contact)
        if problem:
            return problem
        if not _wa_open(chat):
            return ActionResult.failure(f"I found {chat['name']} but couldn't open it.")
        if action is None:
            return ActionResult.failure("I need to ask before sending a message.")
        approved = Action(action.domain, action.action, target=action.target,
                          parameters={**action.parameters, "confirmed_chat": chat["name"]}, raw=action.raw)
        return ActionResult(Status.NEEDS_CONFIRMATION, f"Send \"{_short(text, 120)}\" to {chat['name']}?",
                            {"confirm": approved})
    stop = _bridge_at((1, 9, 0))
    if stop:
        return stop
    state = _call("chat.composer") or {}
    if state.get("chat") != confirmed_chat:
        return ActionResult.failure(f"{confirmed_chat} is no longer the open chat, so I didn't send it.")
    typed = (_call("page.type", field="type a message", text=text, clear=True) or {}).get("typed")
    readback = (_call("chat.composer") or {}).get("text", "")
    if not typed or " ".join(readback.split()) != " ".join(str(text).split()):
        return ActionResult.failure("I couldn't type the message exactly, so I didn't send it.")
    if not _click_first(["Send"]):
        _call("page.type", field="type a message", text="", submit=True)    # Enter in the composer
    sent = _ready("chat.messages", lambda r: any(m.get("out") and " ".join(m.get("text", "").split())
                                                 == " ".join(str(text).split())
                                                 for m in ((r or {}).get("messages") or [])[-3:]), 4.0, limit=5) or {}
    ok = any(m.get("out") and " ".join(m.get("text", "").split()) == " ".join(str(text).split())
             for m in (sent.get("messages") or [])[-3:])
    return ActionResult.success(f"Sent to {confirmed_chat}." if ok else
                                f"I pressed send in {confirmed_chat}; I couldn't see it arrive yet.")


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
    if clicked == "Leave call":
        _share_dnd(False)                      # leaving ends any share Kayra silenced for
        _SHARE_STATE["what"] = ""
    if not clicked:
        already = {True: "on", False: "off"}.get(on)
        return ActionResult.success(f"Your {control} is already {already}.") if already else \
            ActionResult.not_found(f"I can't find the {control} button.")
    return ActionResult.success({"Turn off microphone": "You're muted.",
                                 "Turn on microphone": "You're unmuted.",
                                 "Turn off camera": "Camera off.", "Turn on camera": "Camera on.",
                                 "Leave call": "You've left the call."}.get(clicked, "Done."))


_PRESENTING = re.compile(r"stop presenting|you are presenting|stop sharing", re.I)
_SHARE_INDICATOR = re.compile(r"\bis sharing\b|sharing (?:your|a|this)|stop sharing", re.I)
_SHARE_STATE = {"dnd": False, "what": ""}
SHARE_WAIT_S = 12.0


def _presenting():
    controls = [c.get("name", "") for c in (_call("page.controls", limit=80) or [])]
    return any(_PRESENTING.search(c) for c in controls)


def _share_window(name):
    """The window the user named: (window, None) or (None, sentence)."""
    L = _L()
    if not L.hyprland_ready():
        return None, "Sharing a named window needs the Hyprland session."
    app = L.linux_apps.resolve(name)
    if app is not None:
        matches = L._windows_for(app, strict=False)
    else:
        needle = (name or "").lower().strip()
        matches = [w for w in L.list_windows() if needle and needle in w.title.lower()]
    if not matches:
        return None, f"I don't see {name} open, so I can't share it."
    window = L._pick_single(matches, prefer_recent=True)
    return (window, None) if window is not None else (None, f"Which {name} window?")


def _share_monitor(name):
    """'#2' / '<' / '>' / '' -> a Hyprland monitor dict (focused one for ''), or None."""
    L = _L()
    try:
        monitors = list(L.HYPR.monitors())
    except Exception:
        monitors = []
    if not monitors:
        return None
    ordered = sorted(monitors, key=lambda m: (m.get("x", 0), m.get("y", 0)))
    if not name:
        return next((m for m in monitors if m.get("focused")), ordered[0])
    if name == "<":
        return ordered[0]
    if name == ">":
        return ordered[-1]
    if name.startswith("#") and name[1:].isdigit():
        index = int(name[1:]) - 1
        return ordered[index] if 0 <= index < len(ordered) else None
    return next((m for m in monitors if str(m.get("name", "")).lower() == name.lower()), None)


def _share_dnd(on):
    """
    Do Not Disturb for the length of a share (M2.1): notifications never pop up on a shared
    screen. Only undone by Kayra when Kayra turned it on. MEET_SHARE_DND=off disables it.
    """
    if str(os.environ.get("MEET_SHARE_DND", "on")).strip().lower() in ("0", "off", "false", "no"):
        return False
    L = _L()
    if not L._have("makoctl"):
        return False
    if on:
        _, modes = L._run("makoctl", "mode")
        if "do-not-disturb" in (modes or ""):
            return False                          # the user already had it on: leave it alone
        _SHARE_STATE["dnd"] = L._ok("makoctl", "mode", "-a", "do-not-disturb")
        return _SHARE_STATE["dnd"]
    if _SHARE_STATE["dnd"]:
        _SHARE_STATE["dnd"] = False
        return L._ok("makoctl", "mode", "-r", "do-not-disturb")
    return False


def _drive_pickers(before, auto_portal, press_chrome):
    """
    Answers the pickers that open after Meet's share button, by READINESS (M0.4): each poll
    looks at what is actually on screen — Meet already presenting, a new portal picker
    window, a new Chrome picker window — and acts on that, instead of pressing Enter after a
    fixed pause. Returns "sharing", "portal" (the portal picker is up and the user must
    choose) or None (nothing happened in time).
    """
    L = _L()
    state = {"portal": False, "chrome": 0, "polls": 0}

    def attempt():
        if _presenting():
            return "sharing"
        state["polls"] += 1
        try:
            windows = L.list_windows(include_kayra=True)
        except Exception:
            windows = []
        new = [w for w in windows if w.address not in before]
        portal = [w for w in new if any("share-picker" in c for c in w.classes)]
        if portal:
            if not auto_portal:
                return "portal"
            if not state["portal"]:
                L.focus_window(portal[0])
                L.send_keys("enter")             # the portal picker: its first monitor
                state["portal"] = True
            return None
        if not press_chrome or state["chrome"] >= 2:
            return None
        chrome = [w for w in new if L._is_browser_window(w) and not _SHARE_INDICATOR.search(w.title)]
        if chrome and (state["chrome"] == 0 or state["polls"] % 4 == 0):
            # Chrome's picker: "Share" on the preselected source. A second press covers
            # Chrome on Wayland, where Share is enabled only once the portal has answered.
            L.focus_window(chrome[0])
            L.send_keys("enter")
            state["chrome"] += 1
        elif not chrome and state["chrome"] == 0 and state["polls"] == 6:
            L.send_keys("enter")                 # no separate picker window (X11): it has focus
            state["chrome"] += 1
        return None

    return _until(attempt, SHARE_WAIT_S)


def meet_share(surface="monitor", name="", audio=False, instead=False):
    """
    Shares into the Meet call exactly what was named (§12 M1): the entire screen (the one in
    front, or "my second screen"), a named window ("the LM Studio window"), or a Chrome tab.

    Meet's button opens Chrome's picker; the extension's page hook preselects the surface
    (and system audio when asked). On Hyprland with Kayra's share picker installed, the
    portal is answered with the named window or monitor itself — no picker window appears.
    Without it, a whole screen is still chosen for the user, but a NAMED window or tab is
    left for the user to pick: pressing Enter there would share whatever is listed first.
    """
    missing = _need_bridge()
    if missing:
        return missing
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("You're not in a Google Meet call.")
    L = _L()
    what, request = "your entire screen", None
    if surface == "window" and name:
        window, problem = _share_window(name)
        if window is None:
            return ActionResult.not_found(problem) if "open" in problem or "needs" in problem \
                else ActionResult(Status.AMBIGUOUS, problem)
        what, request = f"the {name.strip()} window", ("window", window)
    elif surface == "window":
        what = "a window"
    elif surface == "browser":
        what = f"the {name} tab" if name else "a Chrome tab"
    else:
        monitor = _share_monitor(name)
        if name and monitor is None:
            return ActionResult.not_found("I don't see that screen.")
        if name and monitor is not None:
            what = f"your {_SCREEN_NAMES.get(name, 'screen')} ({monitor.get('name')})"
        request = ("monitor", monitor) if monitor is not None else None

    if _presenting():
        # The same thing again is a no-op; anything else (M1.4, "share LM Studio instead")
        # stops the current share and starts the new one.
        if not instead and not audio and what == (_SHARE_STATE.get("what") or "your entire screen"):
            return ActionResult.success(f"You're already sharing {what}.")
        _click_first(_MEET_LABELS["stop_share"])
        _until(lambda: not _presenting(), 3.0)

    helper = SHARE_HELPER.installed()
    if helper and request is not None:
        if request[0] == "window":
            SHARE_HELPER.request_window(request[1])
        else:
            SHARE_HELPER.request_monitor(request[1].get("name"))
    # Without the helper the portal picker would share its FIRST entry on Enter: right for
    # "my screen" on a one-monitor desk, wrong for anything that was named.
    auto_portal = not helper and surface == "monitor" and not name
    press_chrome = surface != "browser"            # a tab is the user's pick in Chrome's list

    _focus_chrome()
    _call("page.setMain", key="__kayraShareSurface", value=surface)
    if audio and _version() >= (1, 4, 0):
        _call("page.setMain", key="__kayraShareAudio", value=True)
    try:
        before = {w.address for w in L.list_windows(include_kayra=True)}
    except Exception:
        before = set()
    if not _click_first(_MEET_LABELS["share"]):
        SHARE_HELPER.clear()
        return ActionResult.not_found("I can't find Meet's share button.")
    outcome = _drive_pickers(before, auto_portal, press_chrome)
    SHARE_HELPER.clear()                            # answered or not, never a second time
    if outcome == "sharing":
        _SHARE_STATE["what"] = what
        quiet = _share_dnd(True)
        return ActionResult.success(f"You're sharing {what}"
                                    + (" with sound" if audio else "")
                                    + (", and I've silenced notifications until you stop."
                                       if quiet else "."))
    if surface == "browser":
        return ActionResult.success(f"Chrome's share picker is open on tabs. Pick {what} and "
                                    "press Share.")
    if request is not None and request[0] == "window" and not helper:
        return ActionResult.success(f"The share picker is open. Choose {what} and press Share. "
                                    "Run python setup.py once and I'll pick it for you next "
                                    "time.")
    return ActionResult.success(f"I've opened the share picker for {what}. If it's still open, "
                                "press Share.")


_SCREEN_NAMES = {"#1": "first screen", "#2": "second screen", "#3": "third screen",
                 "<": "left screen", ">": "right screen"}


def meet_stop_share():
    if not BRIDGE.connected():
        return ActionResult.not_found("You're not sharing.")
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.not_found("You're not in a Google Meet call.")
    clicked = _click_first(_MEET_LABELS["stop_share"])
    restored = _share_dnd(False)
    if not clicked:
        return ActionResult.not_found("You're not sharing.")
    _SHARE_STATE["what"] = ""
    return ActionResult.success("Stopped sharing" + (", and notifications are back." if restored
                                                      else "."))


def meet_share_status():
    if not BRIDGE.connected():
        return ActionResult.success("No — Chrome isn't connected, so you're not sharing through me.")
    try:
        _call("tabs.activate", match="meet.google.com")
    except browser_bridge.BridgeError:
        return ActionResult.success("You're not in a Google Meet call.")
    if _presenting():
        what = _SHARE_STATE.get("what")
        return ActionResult.success(f"Yes, you're sharing {what}." if what else "Yes, you're sharing.")
    return ActionResult.success("No, you're not sharing anything.")


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
