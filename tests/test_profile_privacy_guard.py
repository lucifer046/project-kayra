# ┌────────────────────────────────────────────────────────────────────────┐
# │                    test_profile_privacy_guard.py                       │
# │   Nothing about THIS computer's Chrome profiles is in the repository   │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_profile_privacy_guard.py — integration tier: reads this computer's Chrome `Local State`.

    .venv/bin/python tests/test_profile_privacy_guard.py

BROWSER_CONTROL_PLAN §14 (Part 5P) requires the profiles feature to stay data-driven: no
account name, email, email prefix or domain of the user's own profiles may appear anywhere in
the repository's text files (code, tests, docs, update notes). This reads the real profiles
IN MEMORY, searches the tracked files, and reports only WHICH file matched and what KIND of
value it was — never the value. Profile names are not checked: people name profiles "Work"
or "Personal", words the code legitimately uses. A domain inside a web address
(`https://<domain>/`) is a public site in the site table and is allowed.

SAFE: reads only; writes nothing; prints no name, email or domain.
"""

import os
import re
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, run  # noqa: E402

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.automation import chrome_profiles  # noqa: E402

check = Checker("profile privacy guard")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GENERIC_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
                    "example.com", "example.org", "example.net"}


def secrets():
    """{value: kind} from every Chromium-family browser's profiles on this computer."""
    found = {}
    for browser, folder in chrome_profiles._dirs():
        try:
            profiles = chrome_profiles.load(browser, folder)
        except chrome_profiles.ProfilesUnreadable:
            continue
        for p in profiles:
            if p.email:
                found[p.email.lower()] = "an email"
                local = p.email.split("@")[0].lower()
                if len(local) >= 5:
                    found[local] = "an email prefix"
            if p.domain and p.domain not in _GENERIC_DOMAINS:
                found[p.domain.lower()] = "a domain"
            if p.account and len(p.account) >= 5 and " " in p.account:
                found[p.account.lower()] = "an account name"
    return found


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=20, check=True).stdout
        files = out.splitlines()
    except (OSError, subprocess.SubprocessError):
        files = []
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "data", "logs", "models", "graphify-out")]
            files += [os.path.relpath(os.path.join(base, f), ROOT) for f in names]
    return [f for f in files if f.endswith((".py", ".md", ".js", ".json", ".txt", ".example", ".toml", ".cfg"))]


def main():
    print_banner("PROFILE PRIVACY GUARD (Part 5P)")
    values = secrets()
    if not values:
        check.skip("all checks", "no Chromium-family profiles with accounts on this computer")
        return check.finish()
    print_info(f"Checking {len(values)} private values (not shown) against the repository.")
    hits = []
    for rel in tracked_files():
        try:
            text = open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read().lower()
        except OSError:
            continue
        for value, kind in values.items():
            if kind == "a domain":
                # A domain inside a web address ("https://<domain>/") is a public site in the site
                # table, not account data; anywhere else (an email, prose) it is flagged.
                if any(not re.search(r"https?://(?:[\w-]+\.)*$", text[max(0, i - 40):i])
                       for i in (m.start() for m in re.finditer(re.escape(value), text))):
                    hits.append(f"{rel}: {kind}")
            elif value in text:
                hits.append(f"{rel}: {kind}")
    check("no account name, email, email prefix or domain from this computer's Chrome is in the repository",
          not hits, "; ".join(sorted(set(hits))[:20]))
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
