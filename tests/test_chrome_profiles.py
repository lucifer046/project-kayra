# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_chrome_profiles.py                          │
# │   "Open my Work Chrome" — profiles read from Chrome, said any way      │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_chrome_profiles.py — unit tier, hardware-free, no network.

    .venv/bin/python tests/test_chrome_profiles.py

BROWSER_CONTROL_PLAN §14 (Part 5P). Every profile here is MADE UP and lives in a temporary
`Local State`; the real one on this computer is never read (that is the separate privacy
guard, tests/test_profile_privacy_guard.py, in the integration tier). Launches are recorded by
the fake process runner; no browser starts.
"""

import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, describe_host, run  # noqa: E402
from test_linux_automation import (FakeHyprland, RecordingRunner, Rig, client,  # noqa: E402
                                   ALL_TOOLS)

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.automation import chrome_profiles as P, common  # noqa: E402
from kayra.automation.policy import Status  # noqa: E402

check = Checker("chrome profiles")
n = common.normalize_command

# The user's shapes from §14, with invented names, accounts and domains.
CACHE = {
    "Default": {"name": "Personal", "gaia_name": "Asha Verma", "gaia_given_name": "Asha",
                "user_name": "asha.verma@example.com", "hosted_domain": "NO_HOSTED_DOMAIN",
                "is_using_default_name": False},
    "Profile 1": {"name": "Work", "gaia_name": "Asha Verma", "gaia_given_name": "Asha",
                  "user_name": "averma@ds.campus.test", "hosted_domain": "ds.campus.test",
                  "is_using_default_name": False},
    "Profile 2": {"name": "Study", "gaia_name": "Asha V", "gaia_given_name": "Asha",
                  "user_name": "21f100@ds.campus.test", "hosted_domain": "ds.campus.test",
                  "is_using_default_name": False},
    "Profile 3": {"name": "Study 2", "gaia_name": "A Verma", "gaia_given_name": "A",
                  "user_name": "21f200@ds.campus.test", "hosted_domain": "ds.campus.test",
                  "is_using_default_name": False},
    "Profile 5": {"name": "Kiran", "gaia_name": "Kiran Rao", "gaia_given_name": "Kiran",
                  "user_name": "kiranrao@example.org", "is_using_default_name": False},
}
UNNAMED = {
    "Default": {"name": "Person 1", "gaia_name": "Meera Iyer", "user_name": "meera@example.net",
                "is_using_default_name": True},
    "Profile 1": {"name": "Person 2", "gaia_name": "Dev Shah", "user_name": "dev@example.net",
                  "is_using_default_name": True},
    "Profile 2": {"name": "Person 3", "is_using_default_name": True},       # no account
}


class Profiles:
    """A temporary user-data folder per browser, holding the Local State we write."""

    def __init__(self, cache=None, browsers=("google-chrome",)):
        self.cache, self.browsers = cache, browsers

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="kayra-profiles-")
        self.paths = {}
        dirs = []
        for b in ("google-chrome", "chromium", "brave", "microsoft-edge"):
            path = os.path.join(self.dir, b)
            dirs.append((b, path))
            self.paths[b] = path
            if b in self.browsers:
                os.makedirs(path)
                self.write(self.cache if self.cache is not None else CACHE, b)
        self._saved, P.USER_DATA_DIRS = P.USER_DATA_DIRS, dirs
        self._sleep, P.SLEEP = P.SLEEP, (lambda s: None)
        P._CACHE.clear()
        return self

    def write(self, cache, browser="google-chrome", raw=None):
        path = os.path.join(self.paths[browser], "Local State")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps({"profile": {"info_cache": cache}}))
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))   # a new mtime every write

    def __exit__(self, *exc):
        P.USER_DATA_DIRS, P.SLEEP = self._saved, self._sleep
        P._CACHE.clear()
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class NewWindowRunner(RecordingRunner):
    """A browser launch makes a new browser window appear in the fake compositor."""

    def __init__(self, hypr, opens=True):
        super().__init__(ALL_TOOLS + ("google-chrome-stable", "chromium"))
        self.hypr, self.opens = hypr, opens

    def spawn(self, argv):
        super().spawn(argv)
        if self.opens and argv and argv[0] in ("google-chrome-stable", "chromium"):
            self.hypr.clients_list.append(client(f"0xc{len(self.spawned)}", "google-chrome", "New Tab - Google Chrome", pid=777))
        return True


def section_reading():
    check.section("[1] P1 — profiles read from Chrome's own Local State")
    with Profiles() as rig:
        profiles = P.load("google-chrome")
        check("every profile is read, in Chrome's order (Default, Profile 1, …)",
              [p.folder for p in profiles] == ["Default", "Profile 1", "Profile 2", "Profile 3", "Profile 5"],
              [p.folder for p in profiles])
        work = profiles[1]
        check("name, account, first name, email and domain", (work.name, work.account, work.given, work.email, work.domain)
              == ("Work", "Asha Verma", "Asha", "averma@ds.campus.test", "ds.campus.test"), work.to_dict())
        check("'NO_HOSTED_DOMAIN' is no domain; a consumer account's domain comes from the email",
              profiles[0].domain == "example.com" and profiles[4].domain == "example.org", (profiles[0].domain, profiles[4].domain))
        again = P.load("google-chrome")
        check("an unchanged file is not parsed again (the same objects come back)", again[1] is work)
        changed = json.loads(json.dumps(CACHE))
        changed["Profile 1"]["name"] = "Office"
        rig.write(changed)
        check("a profile renamed in Chrome is understood at the very next request (mtime)",
              [p.name for p in P.load("google-chrome")][1] == "Office")
        rig.write(None, raw="{ not json")
        try:
            P.load("google-chrome")
            raised = False
        except P.ProfilesUnreadable:
            raised = True
        check("a corrupt Local State is reported as unreadable, never as 'no profiles'", raised)
    with Profiles(browsers=()) as rig:
        check("no Local State at all = no profiles (not an error)", P.load("google-chrome") == [])
        check("...and the browser said is still known", P.browser_for("chrome")[0] == "google-chrome")
    with Profiles(cache=UNNAMED) as rig:
        profiles = P.load("google-chrome")
        check("never-renamed profiles are marked so", all(p.default_name for p in profiles))
        check("a profile without an account has no account fields", (profiles[2].account, profiles[2].email) == ("", ""))


def section_resolving():
    check.section("[2] P2 — said any way: name, number, email, account, domain, position")
    with Profiles():
        ps = P.load("google-chrome")
        by = {p.folder: p for p in ps}
        for said, email, folder in (
                ("work", "", "Profile 1"), ("Work", "", "Profile 1"), ("my work", "", "Profile 1"),
                ("study", "", "Profile 2"),             # the PROFILE named Study, not every study-domain account
                ("study two", "", "Profile 3"), ("study 2", "", "Profile 3"),
                ("personal", "", "Default"), ("kiran", "", "Profile 5"),
                ("averma at ds dot campus dot test", "averma@ds.campus.test", "Profile 1"),
                ("21f200", "", "Profile 3"),              # the part of the email before the @
                ("kiran rao", "", "Profile 5"),           # the account's name
                ("the second", "", "Profile 1"), ("second", "", "Profile 1"), ("profile 3", "", "Profile 2"),
                ("last", "", "Profile 5"),
                ("example dot org", "", "Profile 5"),     # the only account on that domain
                ("wrok", "", "Profile 1")):               # a near spelling
            got, question = P.resolve(said, ps, email=email)
            check(f"'{said}' -> {by[folder].name}", got is by[folder] and question is None, (got, question))
        got, question = P.resolve("campus", ps)
        check("three profiles on one domain: ONE question naming what tells them apart",
              got is None and question == "Which one: Work (Asha Verma), Study (Asha V) or Study 2 (A Verma)?", question)
        got, question = P.resolve("example", ps)
        check("a domain label two accounts share asks between them",
              question == "Which one: Personal (Asha Verma) or Kiran (Kiran Rao)?", question)
        got, question = P.resolve("asha", ps)
        check("a first name shared by several profiles asks", got is None and question and question.startswith("Which one:"),
              question)
        check("nothing that fits = no pick and no question", P.resolve("zebra crossing", ps) == (None, None))
    with Profiles(cache=UNNAMED):
        ps = P.load("google-chrome")
        got, question = P.resolve("dev", ps)
        check("a never-renamed profile is found by its account", got is ps[1], got)
        check("...and is described by its account when spoken", P.label(ps[0]) == "Person 1 (Meera Iyer)")
        check("'person 3' is that profile", P.resolve("person 3", ps)[0] is ps[2])


def section_parsing():
    check.section("[3] P2 — the shapes, and what must stay as it was")
    for said, key, target, params in (
            ("open my work chrome", "browser.chrome_profile", "work", {"browser": "chrome", "url": ""}),
            ("open my Work Chrome", "browser.chrome_profile", "Work", {}),
            ("open chrome as Kiran", "browser.chrome_profile", "Kiran", {}),
            ("open Kiran's chrome", "browser.chrome_profile", "Kiran", {}),
            ("open google chrome with my campus account", "browser.chrome_profile", "campus", {}),
            ("open chrome on averma at ds dot campus dot test", "browser.chrome_profile",
             "averma at ds dot campus dot test", {"email": "averma@ds.campus.test"}),
            ("open gmail in my study chrome", "browser.chrome_profile", "study", {"url": "https://mail.google.com/"}),
            ("open youtube in chrome as work", "browser.chrome_profile", "work", {"url": "https://www.youtube.com/"}),
            ("switch to my study two profile", "browser.chrome_profile", "study two", {}),
            ("open the second chrome profile", "browser.chrome_profile", "second", {}),
            ("open brave as personal", "browser.chrome_profile", "personal", {"browser": "brave"}),
            ("which chrome profiles do i have", "browser.chrome_profiles", "chrome", {}),
            ("list my chrome profiles", "browser.chrome_profiles", "chrome", {}),
            ("open chrome as a guest", "browser.chrome_guest", "guest", {})):
        a = n(said)
        ok = a is not None and a.key == key and a.target == target and all(a.parameters.get(k) == v for k, v in params.items())
        check(f"'{said}' -> {key} {target!r}", ok, a and (a.key, a.target, a.parameters))
        check(f"...rules first: '{said}'", common.direct_tokens(said, lambda: {}) == [f"browser {said}"])
        b = n("browser " + said)
        check("...and with the browser prefix", b is not None and b.key == key and b.target == target, b)
    for said in ("open chrome", "open google chrome", "open youtube in chrome", "open chrome with youtube",
                 "open chrome in incognito", "open youtube in a new chrome window", "open my chrome",
                 "open gmail in chrome"):
        a = n(said)
        check(f"unchanged: '{said}' is not a profile", a is not None and not a.action.startswith("chrome_"),
              a and (a.key, a.target))


def section_opening():
    check.section("[4] P3 — opening straight into the profile")
    with Profiles():
        hypr = FakeHyprland()
        with Rig(hypr=hypr, runner=NewWindowRunner(hypr)) as rig:
            r = rig.do("open my study two chrome")
            check("the browser starts with --profile-directory of THAT profile (argv, no shell)",
                  rig.runner.spawned == [["google-chrome-stable", "--profile-directory=Profile 3"]], rig.runner.spawned)
            check("...the new window is focused, and the profile is named with its account",
                  r.ok and r.message == "Opened Chrome as Study 2 (A Verma)." and hypr.active == "0xc1",
                  (r.message, hypr.active))
            r = rig.do("open gmail in my work chrome")
            check("a site inside a profile: the address goes with it",
                  rig.runner.spawned[-1] == ["google-chrome-stable", "--profile-directory=Profile 1",
                                             "https://mail.google.com/"] and "with https://mail.google.com/" in r.message,
                  rig.runner.spawned[-1])
            count = len(rig.runner.spawned)
            r = rig.do("open my campus chrome")
            check("an ambiguous name asks and starts nothing", r.status == Status.AMBIGUOUS
                  and "Study 2 (A Verma)" in r.message and len(rig.runner.spawned) == count, r.message)
            r = rig.do("open my zebra chrome")
            check("no such profile: says which ones exist, starts nothing", r.status == Status.NOT_FOUND
                  and "Personal, Work, Study, Study 2 and Kiran" in r.message and len(rig.runner.spawned) == count, r.message)
            r = rig.do("which chrome profiles do i have")
            check("'which Chrome profiles do I have' lists them by name and account", r.ok and r.message.startswith(
                "You have 5 Chrome profiles: Personal (Asha Verma), Work (Asha Verma), Study (Asha V), Study 2 (A Verma) and "
                "Kiran (Kiran Rao)."), r.message)
            r = rig.do("open chrome as a guest")
            check("guest: Chrome's own --guest", rig.runner.spawned[-1] == ["google-chrome-stable", "--guest"] and r.ok)
        with Rig(runner=RecordingRunner(ALL_TOOLS)) as rig:
            r = rig.do("open my work chrome")
            check("Chrome not installed: says so, starts nothing", r.status == Status.NOT_FOUND and not rig.runner.spawned,
                  r.message)
    with Profiles(browsers=("google-chrome", "chromium")) as profiles:
        hypr = FakeHyprland()
        with Rig(hypr=hypr, runner=NewWindowRunner(hypr)) as rig:
            rig.do("open chromium as personal")
            check("Chromium has its own profiles and binary", rig.runner.spawned == [["chromium", "--profile-directory=Default"]],
                  rig.runner.spawned)
    with Profiles() as profiles:
        profiles.write(None, raw="garbage")
        with Rig(runner=NewWindowRunner(FakeHyprland())) as rig:
            r = rig.do("open my work chrome")
            check("an unreadable Local State says so and offers the plain picker", not r.ok and "can't read" in r.message
                  and "open Chrome" in r.message and not rig.runner.spawned, r.message)


def section_platform():
    check.section("[5] Where each browser keeps its profiles comes from the platform layer")
    from kayra import platforms
    from kayra.platforms import linux as plinux, windows as pwindows, fallback
    saved = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-test-config"
    try:
        dirs = dict(plinux.browser_user_data_dirs())
    finally:
        if saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = saved
    check("Linux: under XDG_CONFIG_HOME, Brave under BraveSoftware", dirs == {
        "google-chrome": "/tmp/xdg-test-config/google-chrome", "chromium": "/tmp/xdg-test-config/chromium",
        "brave": "/tmp/xdg-test-config/BraveSoftware/Brave-Browser",
        "microsoft-edge": "/tmp/xdg-test-config/microsoft-edge"}, dirs)
    saved = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = "C:\\Users\\x\\AppData\\Local"
    try:
        win = dict(pwindows.browser_user_data_dirs())
    finally:
        if saved is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = saved
    check("Windows: %LOCALAPPDATA%\\Google\\Chrome\\User Data", win["google-chrome"].endswith(
        os.path.join("Google", "Chrome", "User Data")), win)
    check("fallback: none", fallback.browser_user_data_dirs() == [])
    check("the façade exposes it", "browser_user_data_dirs" in platforms.__all__)
    source = open(P.__file__, encoding="utf-8").read()
    check("chrome_profiles never names a home folder itself", ".config" not in source and "AppData" not in source)


def main():
    print_banner("CHROME PROFILES (Part 5P)")
    print_info(f"Host: {describe_host()}")
    with EnvironmentGuard() as guard:
        section_reading()
        section_resolving()
        section_parsing()
        section_opening()
        section_platform()
    check.section("[6] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
