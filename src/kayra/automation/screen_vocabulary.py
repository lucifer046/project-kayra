"""
The names that are on screen right now, for the transcript repair stage.

The recognizer has never heard of "Megha Sharma IITM" or a button called "Join now", so it
writes down the nearest ordinary words ("make her sharma", "joint"). The repair stage
(`input.transcript_repair`) can only choose a better reading when it knows which names are
actually in front of the user. This module answers that, and nothing else:

  * the chat names in an open WhatsApp tab (any tab, not only the active one),
  * the file titles in a Google Docs / Sheets / Drive list in the active tab,
  * the buttons and links on the active tab.

Cheap reads only: three extension calls with short timeouts, no screenshot, no OCR, no model.
Bridge not connected -> []. Any failure -> what was gathered so far. It never raises: the
caller sits on the speech path, and a missing vocabulary must cost a repair, never a command.

The names never leave the machine; they are compared locally and then forgotten.
"""

from urllib.parse import urlsplit

from kayra.automation import browser_bridge

BRIDGE = browser_bridge          # tests replace this with a fake extension

# Per extension call. The whole read is three calls, so the worst case is ~1 s on a stuck
# page; normally it is a few tens of milliseconds over the loopback socket.
CALL_TIMEOUT_S = 0.35
MAX_NAMES = 300
MAX_NAME_CHARS = 60

_FILE_LIST_HOSTS = ("docs.google.com", "drive.google.com")


def _call(cmd, **args):
    try:
        return BRIDGE.call(cmd, timeout=CALL_TIMEOUT_S, **args)
    except Exception:
        return None


def _connected():
    try:
        return bool(BRIDGE.connected())
    except Exception:
        return False


def names():
    """Every distinct name on screen, chats first, then files, then controls. [] when unknown."""
    if not _connected():
        return []
    out, seen = [], set()

    def add(name):
        text = " ".join(str(name or "").split())
        key = text.lower()
        if 2 <= len(text) <= MAX_NAME_CHARS and key not in seen and len(out) < MAX_NAMES:
            seen.add(key)
            out.append(text)

    listing = _call("chat.list", match="web.whatsapp.com", max=60)
    if isinstance(listing, dict):
        for chat in listing.get("chats") or ():
            add((chat or {}).get("name"))

    tab = _call("tabs.active")
    url = str((tab or {}).get("url") or "") if isinstance(tab, dict) else ""
    host = (urlsplit(url).hostname or "").lower() if url.startswith("http") else ""
    if host in _FILE_LIST_HOSTS and "/d/" not in url:
        for item in _call("page.docList", max=40) or ():
            if isinstance(item, dict):
                add(item.get("title"))
    if host:
        for control in _call("page.controls", limit=60) or ():
            if isinstance(control, dict):
                add(control.get("name"))
    return out
