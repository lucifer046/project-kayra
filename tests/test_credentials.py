# ┌────────────────────────────────────────────────────────────────────────┐
# │                         test_credentials.py                            │
# │        API Keys: System Keyring First, Owner-Only .env as Fallback     │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_credentials.py — where an API key typed into Settings ends up.

    .venv/bin/python tests/test_credentials.py

HOST-FREE. `.env` is a file in a temporary directory and the keyring is an in-memory fake, so
the developer's real configuration and real keyring are never read or written.

  1. With a keyring: the key goes there and `.env` holds only the `@keyring` marker.
  2. The marker is resolved for every reader (`env_values`, `env`, `os.environ`) and never leaks.
  3. Without a keyring: the key goes into `.env`, which is made owner-only (0600).
  4. Remove deletes the key from both places.
  5. A configuration WITHOUT the marker never touches the keyring at all.
"""

import os
import sys
import stat
import shutil
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_success, print_error, print_system
from kayra.core import config, credentials

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}")
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


class FakeKeyring:
    """The three calls `credentials` makes, kept in a dict, with a call log."""

    def __init__(self):
        self.store, self.calls = {}, []

    def get_password(self, service, name):
        self.calls.append(("get", name))
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.calls.append(("set", name))
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.calls.append(("delete", name))
        self.store.pop((service, name), None)

    def get_keyring(self):
        return self


def use_keyring(fake):
    credentials._STATE["checked"] = True
    credentials._STATE["backend"] = fake


def raw_env(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main():
    print_banner("CREDENTIAL STORAGE DIAGNOSTIC", "keyring · owner-only file · markers")
    temp = tempfile.mkdtemp(prefix="kayra-cred-")
    env_path = os.path.join(temp, ".env")
    saved_env_file = config.env_file
    saved_environ = dict(os.environ)
    config.env_file = lambda: env_path
    config.reset_cache()
    try:
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("# test\nASSISTANT_NAME=Kayra\nGROQ_API_KEY=\n")

        print_system("\n[1] With a system keyring")
        fake = FakeKeyring()
        use_keyring(fake)
        where = config.save_secret("CLAUDE_API_KEY", "sk-ant-secret-1")
        text = raw_env(env_path)
        check("the key goes to the keyring", where == "keyring", where)
        check(".env holds only the marker", "CLAUDE_API_KEY=@keyring" in text, text)
        check("the key itself is nowhere in .env", "sk-ant-secret-1" not in text)
        check("readers get the real key", config.env("CLAUDE_API_KEY") == "sk-ant-secret-1")
        check("os.environ gets the key, never the marker",
              os.environ.get("CLAUDE_API_KEY") == "sk-ant-secret-1")
        check("stored_raw reveals WHERE (the marker), not the key",
              config.stored_raw("CLAUDE_API_KEY") == "@keyring")
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(env_path).st_mode)
            check(".env is owner-only after a save", mode == 0o600, oct(mode))

        os.environ["CLAUDE_API_KEY"] = "@keyring"
        config.load_environment(override=True)
        check("load_environment replaces an exported marker with the key",
              os.environ.get("CLAUDE_API_KEY") == "sk-ant-secret-1")

        print_system("\n[2] Remove")
        where = config.save_secret("CLAUDE_API_KEY", "")
        check("an empty value removes the key", where == "removed", where)
        check("...from the keyring", ("kayra", "CLAUDE_API_KEY") not in fake.store)
        check("...and from .env", config.env("CLAUDE_API_KEY") in (None, ""))

        print_system("\n[3] A keyring that cannot be reached")
        config.save_secret("GROQ_API_KEY", "gsk-secret-2")
        use_keyring(None)
        os.environ.pop("GROQ_API_KEY", None)        # a fresh process: nothing exported yet
        config.reset_cache()
        check("a marker the keyring cannot answer reads as NO key, not as the marker",
              config.env("GROQ_API_KEY") in (None, ""), str(config.env("GROQ_API_KEY")))

        print_system("\n[4] Without a keyring: the owner-only file")
        if os.name == "posix":
            os.chmod(env_path, 0o644)
        where = config.save_secret("GEMINI_API_KEY", "gm-secret-3")
        check("the key falls back to .env", where == "file", where)
        check("and is readable from it", config.env("GEMINI_API_KEY") == "gm-secret-3")
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(env_path).st_mode)
            check("a world-readable .env is tightened to 0600 on save", mode == 0o600, oct(mode))
        check("credentials.available() is False without a keyring", not credentials.available())
        check("the screen is told where keys will go",
              "file" in credentials.backend_label())

        print_system("\n[5] No marker, no keyring traffic")
        quiet = FakeKeyring()
        use_keyring(quiet)
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("CLAUDE_API_KEY=plain-key\n")
        config.reset_cache()
        check("a plain key is read from the file", config.env("CLAUDE_API_KEY") == "plain-key")
        check("and the keyring was never asked", quiet.calls == [], str(quiet.calls))
        check("only the three provider keys may go to the keyring",
              not credentials.store("ASSISTANT_NAME", "x") and quiet.calls == [])

        credentials._reset_for_tests()
        os.environ["KAYRA_KEYRING"] = "off"
        check("KAYRA_KEYRING=off disables the keyring", not credentials.available())
    finally:
        config.env_file = saved_env_file
        config.reset_cache()
        credentials._reset_for_tests()
        os.environ.clear()
        os.environ.update(saved_environ)
        shutil.rmtree(temp, ignore_errors=True)

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All credential storage checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
