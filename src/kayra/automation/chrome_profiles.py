# ┌────────────────────────────────────────────────────────────────────────┐
# │                     automation/chrome_profiles.py                      │
# │     "Open my Work Chrome" — Chrome's own profiles, read every time      │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §14 (Part 5P). "Open Google Chrome" shows Chrome's *Who's using Chrome?*
picker; people with several profiles want "open my Work Chrome", "open Chrome as Ravi",
"open Chrome on ana at school dot edu" or "open Gmail in my Study Chrome" to go straight in.

**Fully dynamic, by requirement.** Nothing about anyone's profiles is written in code, in the
repository or in a config file the user must edit. The profiles are read from the browser's
own `Local State` (`profile.info_cache`: the folder, the name the picker shows, the signed-in
account's name, email and hosted domain, whether the name is still Chrome's default) every
time it is asked, re-parsed only when the file's modification time changes — so a profile
renamed in Chrome is understood at the next request, with nothing to restart.

**Private.** Read on this computer only. Names and emails are matched here (`semantic_match`
and exact comparisons), never logged in full, never sent to a model. The classifier only ever
sees the user's words. Where the folder lives is the platform layer's question
(`platforms.browser_user_data_dirs()`), never guessed here.

Matching, in order — the first tier with an answer decides, so a profile NAME beats an
account name ("Study" is the profile called Study, not every account on a study domain):
  1. the profile name exactly ("study two" -> "Study 2": spoken numbers are digits)
  2. the email, or its part before the @ (spoken emails understood)
  3. the account's name or first name
  4. the account's domain or its first label ("my iitm account")
  5. "the second profile" / "profile 2" / "person 2" — position in Chrome's order
  6. a loose match on the names (`semantic_match`: pick / ask / none)
Several equally good answers -> ONE question naming what tells them apart.
"""

import os
import re
import json

from kayra.automation.semantic_match import match as _loose_match, tokens as _tokens, PICK, ASK

USER_DATA_DIRS = None              # the test suite points this at temporary folders
_CACHE = {}                        # Local State path -> (mtime, size, [Profile])

_DISPLAY = {"google-chrome": "Chrome", "chromium": "Chromium", "brave": "Brave", "microsoft-edge": "Edge"}
_BINARIES = {"google-chrome": ("google-chrome-stable", "google-chrome"), "chromium": ("chromium", "chromium-browser"),
             "brave": ("brave", "brave-browser"), "microsoft-edge": ("microsoft-edge-stable", "microsoft-edge")}
_SAID_BROWSER = {"chrome": "google-chrome", "google chrome": "google-chrome", "chromium": "chromium",
                 "brave": "brave", "edge": "microsoft-edge", "microsoft edge": "microsoft-edge"}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}
_FILLER = re.compile(r"\b(?:my|the|our|a|an|profile|account|chrome|google|browser|window|one|user|"
                     r"logged in as|signed in as|signed into|logged into)\b|'s\b|’s\b")


class Profile:
    __slots__ = ("browser", "folder", "name", "account", "given", "email", "domain", "default_name", "order")

    def __init__(self, browser, folder, name="", account="", given="", email="", domain="",
                 default_name=False, order=0):
        self.browser, self.folder, self.name = browser, folder, name
        self.account, self.given, self.email, self.domain = account, given, email, domain
        self.default_name, self.order = default_name, order

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self):
        return f"<Profile {self.folder} {self.name!r}>"


def _dirs():
    if USER_DATA_DIRS is not None:
        return list(USER_DATA_DIRS)
    from kayra import platforms
    return platforms.browser_user_data_dirs()


def browser_for(said=""):
    """The browser key "open <said> as X" means: the one named, else the first installed one."""
    said = " ".join(str(said or "").lower().split())
    want = _SAID_BROWSER.get(said) or next((key for word, key in _SAID_BROWSER.items() if word in said), None)
    found = [(key, path) for key, path in _dirs() if os.path.isfile(os.path.join(path, "Local State"))]
    if want:
        return next(((k, p) for k, p in found if k == want), (want, None))
    if said and said not in _SAID_BROWSER:
        return (None, None)
    return found[0] if found else ("google-chrome", None)


class ProfilesUnreadable(Exception):
    pass


def load(browser="google-chrome", folder=None):
    """[Profile] for `browser`, in Chrome's own order (Default, Profile 1, Profile 2, …).
    Raises ProfilesUnreadable when the file exists but cannot be read; [] when there is none."""
    if folder is None:
        folder = dict(_dirs()).get(browser)
    if not folder:
        return []
    path = os.path.join(folder, "Local State")
    try:
        stat = os.stat(path)
    except OSError:
        return []
    cached = _CACHE.get(path)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return list(cached[2])
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        cache = ((data.get("profile") or {}).get("info_cache") or {})
        if not isinstance(cache, dict):
            raise ValueError("info_cache")
    except (OSError, ValueError, AttributeError) as exc:
        raise ProfilesUnreadable(str(exc)) from None

    def order_key(item):
        folder_name = item[0]
        if folder_name == "Default":
            return (0, 0)
        m = re.fullmatch(r"Profile (\d+)", folder_name)
        return (1, int(m.group(1))) if m else (2, folder_name)

    profiles = []
    for index, (key, info) in enumerate(sorted(cache.items(), key=order_key)):
        info = info if isinstance(info, dict) else {}
        email = str(info.get("user_name") or "").strip()
        domain = str(info.get("hosted_domain") or "").strip().lower()
        if domain in ("no_hosted_domain", "none"):
            domain = ""
        if not domain and "@" in email:
            domain = email.split("@", 1)[1].lower()
        profiles.append(Profile(browser, key, str(info.get("name") or key).strip(),
                                str(info.get("gaia_name") or "").strip(), str(info.get("gaia_given_name") or "").strip(),
                                email, domain, bool(info.get("is_using_default_name")), index + 1))
    _CACHE[path] = (stat.st_mtime_ns, stat.st_size, list(profiles))
    return profiles


def with_extension(extension_id, browser="google-chrome"):
    """
    The profiles that have the extension `extension_id` installed and not disabled, read from
    each profile's own Preferences at run time (the audit log of 2026-11-11: the Kayra Bridge
    was in ONE profile while Chrome was open in another, so every browser command said "set up
    the extension"). [] when unknown. Never raises.
    """
    folder = dict(_dirs()).get(browser)
    if not folder:
        return []
    try:
        profiles = load(browser, folder)
    except ProfilesUnreadable:
        return []
    out = []
    for profile in profiles:
        for name in ("Secure Preferences", "Preferences"):
            try:
                with open(os.path.join(folder, profile.folder, name), "r", encoding="utf-8") as handle:
                    settings = ((json.load(handle).get("extensions") or {}).get("settings") or {})
            except (OSError, ValueError, AttributeError):
                continue
            entry = settings.get(extension_id) if isinstance(settings, dict) else None
            if isinstance(entry, dict) and not entry.get("disable_reasons"):
                out.append(profile)
                break
    return out


def last_used(browser="google-chrome"):
    """The profile folder Chrome opens by default (Local State's last_used), or ''."""
    folder = dict(_dirs()).get(browser)
    try:
        with open(os.path.join(folder or "", "Local State"), "r", encoding="utf-8") as handle:
            return str(((json.load(handle).get("profile") or {}).get("last_used")) or "")
    except (OSError, ValueError, AttributeError, TypeError):
        return ""


def label(profile):
    """How Kayra SAYS a profile: its name, plus the account when that tells it apart."""
    who = profile.account or profile.given
    if who and _norm(who) != _norm(profile.name):
        return f"{profile.name} ({who})"
    return profile.name


def _norm(text):
    from kayra.automation.semantic_match import _numbers
    return " ".join(re.findall(r"[a-z0-9]+", _numbers(str(text or "").lower())))


def _clean(said):
    return " ".join(_FILLER.sub(" ", _norm(said)).split())


def _ask(candidates):
    names = [label(p) for p in candidates[:4]]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return f"Which one: {listed}?"


def resolve(said, profiles, email=""):
    """(Profile, None) | (None, question) | (None, None) for nothing that fits."""
    if not profiles:
        return None, None
    raw = _norm(said)
    want = _clean(said)
    # 1. the profile's own name
    hits = [p for p in profiles if _norm(p.name) in (raw, want)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, _ask(hits)
    # 2. the email (spoken or written), or the part before the @
    email = (email or "").lower()
    if email:
        hits = [p for p in profiles if p.email.lower() == email]
        if hits:
            return hits[0], None
    local = re.sub(r"[^a-z0-9]", "", want.replace(" ", ""))
    hits = [p for p in profiles if p.email and re.sub(r"[^a-z0-9]", "", p.email.split("@")[0].lower()) == local and local]
    if len(hits) == 1:
        return hits[0], None
    # 3. the account's name / first name
    hits = [p for p in profiles if want and want in (_norm(p.account), _norm(p.given))]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, _ask(hits)
    # 4. the domain or its first label ("my iitm account", "school chrome" when that IS the label)
    said_domain = re.sub(r"\s*\bdot\b\s*", ".", want).replace(" ", "")
    hits = [p for p in profiles if p.domain and p.domain == said_domain]      # "example dot org": the whole domain
    if len(hits) == 1:
        return hits[0], None
    words = set(want.split())
    hits = hits or [p for p in profiles if p.domain and (want == p.domain.replace(".", " ") or
                                                  any(part in words for part in p.domain.split(".")[:-1]) or
                                                  " ".join(p.domain.split(".")[:2]) == want)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, _ask(hits)
    # 5. by position: "the second profile", "profile 2", "person 2"
    m = re.fullmatch(r"(?:(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)|"
                     r"(?:profile|person|number) (\d{1,2}))", " ".join(_norm(said).replace("the ", "").replace(" profile", "")
                                                                  .replace(" chrome", "").split()))
    if m:
        n = _ORDINALS[m.group(1)] if m.group(1) else int(m.group(2))
        if n == -1:
            return profiles[-1], None
        if 1 <= n <= len(profiles):
            return profiles[n - 1], None
    # 6. a loose match over every name a profile is known by
    if not _tokens(want):
        return None, None
    from kayra.automation.semantic_match import Candidate
    cands = []
    for p in profiles:
        for text in {p.name, p.account, p.given}:
            if text:
                cands.append(Candidate(text, recent=p.order, data=p))
    decision = _loose_match(want, cands)
    if decision.decision == PICK:
        return decision.best.data, None
    if decision.decision == ASK:
        seen = []
        for c in decision.top(3):
            if c.data not in seen:
                seen.append(c.data)
        if len(seen) == 1:
            return seen[0], None
        return None, _ask(seen)
    return None, None


def summary(profiles, display="Chrome"):
    if not profiles:
        return f"I don't see any {display} profiles on this computer."
    names = [label(p) for p in profiles]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return (f"You have {len(profiles)} {display} profile{'s' if len(profiles) != 1 else ''}: {listed}. "
            f"Say, for example, open my {profiles[0].name} {display}.")


def binary_for(browser, have):
    return next((b for b in _BINARIES.get(browser, ()) if have(b)), None)


def display(browser):
    return _DISPLAY.get(browser, "Chrome")


# ── Opening (Linux backend; Windows answers "not yet") ──
import time as _time

SLEEP = _time.sleep                # the test suite replaces this
WINDOW_WAIT_S = 4.0


def _L():
    from kayra.automation import linux
    return linux


def _result(kind, message, **detail):
    from kayra.automation.policy import ActionResult, Status
    status = {"ok": Status.OK, "not_found": Status.NOT_FOUND, "ambiguous": Status.AMBIGUOUS,
              "failed": Status.FAILED, "unsupported": Status.UNSUPPORTED}[kind]
    return ActionResult(status, message, detail)


def list_profiles(browser_said=""):
    """"which Chrome profiles do I have?" """
    browser, folder = browser_for(browser_said)
    if browser is None:
        return _result("unsupported", "I can list profiles for Chrome, Chromium, Brave and Edge.")
    try:
        profiles = load(browser, folder) if folder else []
    except ProfilesUnreadable:
        return _result("failed", f"I can't read {display(browser)}'s profiles right now.")
    return _result("ok", summary(profiles, display(browser)), count=len(profiles))


def open_profile(said, browser_said="", url="", email=""):
    """P3: "open my Work Chrome" -> `<browser> --profile-directory=<folder> [url]`, the new window
    focused, and the profile named back ("Opened Chrome as Work (Ana Rao)")."""
    browser, folder = browser_for(browser_said)
    if browser is None:
        return _result("unsupported", "I can open profiles of Chrome, Chromium, Brave and Edge.")
    name = display(browser)
    try:
        profiles = load(browser, folder) if folder else []
    except ProfilesUnreadable:
        return _result("failed", f"I can't read {name}'s profiles right now. Say open {name} to get its picker.")
    if not profiles:
        return _result("not_found", f"I don't see any {name} profiles on this computer.")
    profile, question = resolve(said, profiles, email=email)
    if question:
        return _result("ambiguous", question, candidates=[p.name for p in profiles if label(p) in question])
    if profile is None:
        names = [p.name for p in profiles]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return _result("not_found", f"I don't see a {name} profile called {said}. You have {listed}.")
    L = _L()
    binary = binary_for(browser, L._have)
    if not binary:
        return _result("not_found", f"{name} isn't installed.")
    before = {w.address for w in L.list_windows()}
    argv = [binary, f"--profile-directory={profile.folder}"] + ([url] if url else [])
    L.RUNNER.spawn(argv)
    window = None
    for attempt in range(max(1, int(WINDOW_WAIT_S / 0.25))):
        if attempt:
            SLEEP(0.25)
        fresh = [w for w in L.list_windows() if w.address not in before and L._is_browser_window(w)]
        if fresh:
            window = fresh[-1]
            break
    if window is not None:
        try:
            L.focus_window(window)
        except Exception:
            pass
    where = f" with {url}" if url else ""
    return _result("ok", f"Opened {name} as {label(profile)}{where}.", profile=profile.folder, browser=browser)


def open_guest(browser_said=""):
    """"open Chrome as a guest": a window that keeps nothing (Chrome's own --guest)."""
    browser, _folder = browser_for(browser_said)
    if browser is None:
        browser = "google-chrome"
    L = _L()
    binary = binary_for(browser, L._have)
    if not binary:
        return _result("not_found", f"{display(browser)} isn't installed.")
    L.RUNNER.spawn([binary, "--guest"])
    return _result("ok", f"Opened a {display(browser)} guest window. Nothing in it is kept after you close it.")
