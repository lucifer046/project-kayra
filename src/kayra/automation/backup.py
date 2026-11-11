# ┌────────────────────────────────────────────────────────────────────────┐
# │                          automation/backup.py                          │
# │    "Back up my Kayra data" / "restore my last backup" — keys excluded   │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §13 N5.1 + N2.6 (Part 16). One zip in `backups/` (beside `data/`, never
committed) holding:

* every file in `data/` — the conversation memory, the form profile, what Kayra learned, page
  recipes, saved tabs, the reading list, triggers, scenes, habits (not the run lock);
* `settings.env` — `.env` WITHOUT a single secret: every key `config.is_secret` calls one (API
  keys, tokens, passwords) and every `@keyring` value is left out, and the manifest lists which
  names were left out so a restore can say so;
* `manifest.json` — when, what, and the names omitted.

The zip is 0600 (it holds the user's memory and details). The last ten are kept.

**Restore** always asks first (it overwrites memory), then saves the CURRENT state as a backup
before touching anything, extracts only plain `data/` files (no absolute paths, no `..`), and
merges `settings.env` into `.env` without touching any secret already there. Restart to load the
restored memory.
"""

import os
import io
import json
import time
import zipfile
import datetime

KEEP = 10
ROOT = None                        # the test suite points this at a temporary project
_SKIP = {"kayra.lock"}


def _paths():
    from kayra.core import paths
    if ROOT is not None:
        data = os.path.join(ROOT, "data")
        backups = os.path.join(ROOT, "backups")
        env = os.path.join(ROOT, ".env")
        os.makedirs(backups, exist_ok=True)
        return data, backups, env
    from kayra.core.config import env_file
    return paths.data_dir(), paths.backups_dir(), env_file()


def _result(kind, message, **detail):
    from kayra.automation.policy import ActionResult, Status
    return ActionResult({"ok": Status.OK, "not_found": Status.NOT_FOUND, "failed": Status.FAILED}[kind], message, detail)


def _clean_env(path):
    """(the .env text with every secret line removed, [names removed])."""
    from kayra.core.config import is_secret
    kept, omitted = [], []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return "", []
    for line in lines:
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and name and not name.startswith("#") and (is_secret(name) or value.strip() == "@keyring"):
            omitted.append(name)
            continue
        kept.append(line)
    return "\n".join(kept) + "\n", omitted


def _backups(folder):
    names = [n for n in os.listdir(folder) if n.startswith("kayra-backup-") and n.endswith(".zip")]
    return sorted((os.path.join(folder, n) for n in names), key=os.path.getmtime)


def create(label=""):
    """'back up my Kayra data' — a zip in backups/, keys excluded."""
    data, folder, env = _paths()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(folder, f"kayra-backup-{stamp}{('-' + label) if label else ''}.zip")
    files = []
    settings, omitted = _clean_env(env)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if os.path.isdir(data):
            for base, _dirs, names in os.walk(data):
                for name in sorted(names):
                    if name in _SKIP or name.endswith(".tmp"):
                        continue
                    full = os.path.join(base, name)
                    rel = os.path.relpath(full, data).replace(os.sep, "/")
                    archive.write(full, "data/" + rel)
                    files.append(rel)
        if settings.strip():
            archive.writestr("settings.env", settings)
        archive.writestr("manifest.json", json.dumps({"created": stamp, "files": files, "omitted_secrets": omitted,
                                                      "kind": "kayra-backup", "version": 1}, indent=1))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(buffer.getvalue())
    for old in _backups(folder)[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    keys = f" Your {len(omitted)} key{'s' if len(omitted) != 1 else ''} stayed out of it." if omitted else ""
    return _result("ok", f"Backed up {len(files)} file{'s' if len(files) != 1 else ''} and your settings to the "
                   f"backups folder.{keys}", path=path, files=len(files), omitted=omitted)


def list_backups():
    _data, folder, _env = _paths()
    found = _backups(folder)
    if not found:
        return _result("ok", "You don't have any backups yet. Say \"back up my Kayra data\".")
    newest = datetime.datetime.fromtimestamp(os.path.getmtime(found[-1]))
    return _result("ok", f"{len(found)} backup{'s' if len(found) != 1 else ''}; the newest is from "
                   f"{newest:%d %B at %H:%M}.")


def _safe(name):
    parts = name.split("/")
    return name.startswith("data/") and ".." not in parts and not name.startswith("/") and "\\" not in name \
        and len(parts) >= 2 and parts[-1] and parts[-1] not in _SKIP


def restore(path=None):
    """'restore my last backup' (the policy has already asked)."""
    data, folder, env = _paths()
    found = _backups(folder)
    if path is None:
        if not found:
            return _result("not_found", "There's no backup to restore.")
        path = found[-1]
    try:
        archive = zipfile.ZipFile(path)
        manifest = json.loads(archive.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return _result("failed", "That backup can't be read, so nothing was changed.")
    if manifest.get("kind") != "kayra-backup":
        return _result("failed", "That isn't a Kayra backup, so nothing was changed.")
    safety = create("before-restore")                 # the state being replaced is never lost
    restored = 0
    with archive:
        for name in archive.namelist():
            if not _safe(name):
                continue
            target = os.path.join(data, *name.split("/")[1:])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            temp = target + ".tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(archive.read(name))
            os.replace(temp, target)
            restored += 1
        settings = {}
        if "settings.env" in archive.namelist():
            from kayra.core.config import is_secret
            for line in archive.read("settings.env").decode("utf-8", "replace").splitlines():
                key, sep, value = line.partition("=")
                key = key.strip()
                if sep and key and not key.startswith("#") and not is_secret(key) and value.strip() != "@keyring":
                    settings[key] = value.strip()
    if settings:
        from kayra.core import config
        if ROOT is not None:
            _merge_env(env, settings)
        else:
            config.write_env_values(settings)
            config.reset_cache()
    when = manifest.get("created", "")
    return _result("ok", f"Restored {restored} file{'s' if restored != 1 else ''} and {len(settings)} settings from {when}. "
                   "What you had before is saved as a backup too. Restart me to load the restored memory.",
                   safety=(safety.detail or {}).get("path"))


def _merge_env(path, settings):
    """Test-root restore: the same in-place merge `config.write_env_values` does, on `path`."""
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in settings:
            lines[i] = f"{key}={settings[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in settings.items() if k not in seen]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return time.time()
