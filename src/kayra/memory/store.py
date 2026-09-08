# ┌────────────────────────────────────────────────────────────────────────┐
# │                              store.py                                  │
# │       Managing What Kayra Has Kept — list, delete, locate, open        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The management surface over the long-term conversation memory.

THERE IS NO SECOND STORE
------------------------
Everything here reads and writes through `memory.conversation`, which stays the only owner of
the file and of the atomic write (backup first, then copy over the primary). A management
layer that kept its own copy would be a second source of truth for the one piece of durable
conversational state in the system, and the two would disagree the first time a chat turn
appended something while the screen was open.

STABLE IDENTITY, AND WHY IT HAD TO BE ADDED
-------------------------------------------
The screen previously deleted by POSITION — the row's index into the last thirty entries. That
is wrong in a way that is easy to miss and impossible to recover from: the store is appended to
by the running assistant, so the entry at index 4 when the screen rendered is not necessarily
the entry at index 4 when the button is clicked. Delete the wrong memory and there is no undo.

Every record therefore carries an `id`: a short, stable, content-derived hash, WRITTEN INTO
the record the first time it is seen and persisted with it. Content-derived rather than
sequential so the same store always yields the same ids regardless of how it is loaded, and
persisted rather than recomputed so a later edit to the text cannot orphan an id the UI is
holding. Duplicate content (the user saving the same sentence twice) is disambiguated by an
occurrence ordinal, so two identical memories remain two deletable things.

Migration is additive and lazy: `load()` adds ids to records that lack them and saves once,
through the existing atomic helper. No field is removed, no field is renamed, and
`chatbot.py`'s `role`/`content` reads are untouched — an older Kayra could still read the file.

PRIVACY IN THE LOG
------------------
`[MEMORY] Deleted: id=a1b2c3d4e5f6`. Never the content. This is the store of the things the
user explicitly asked to keep, so it is by construction the most sensitive text in the
process, and a terminal log is the least private place it could end up.
"""

import os

import hashlib
import subprocess
import sys

from kayra.core.logbus import Subsystem, info, warning, error, debug
from kayra.core.paths import conversation_paths
from kayra.memory.conversation import load_conversation_memory, save_conversation_memory


ID_LENGTH = 12
ID_FIELD = "id"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              IDENTITY                                  │
# └────────────────────────────────────────────────────────────────────────┘

def _compute_id(entry, ordinal):
    """
    A stable id for one record: sha1 over its role, content and duplicate ordinal.

    The ordinal is part of the digest rather than a suffix on it so that ids remain uniform
    fixed-length strings — a UI that renders or truncates them does not have to cope with two
    shapes, and nothing downstream can be tempted to parse one.
    """
    if isinstance(entry, dict):
        role = str(entry.get("role", ""))
        content = str(entry.get("content", ""))
    else:
        role, content = "", str(entry)
    digest = hashlib.sha1(f"{ordinal}\x00{role}\x00{content}".encode("utf-8", "replace"))
    return digest.hexdigest()[:ID_LENGTH]


def _normalize(entries):
    """
    Ensures every record is a dict carrying an `id`. Returns (records, changed).

    Non-dict entries — a bare string written by some older version — are WRAPPED rather than
    dropped. Losing something the user asked to keep because it is in an unexpected shape
    would be the worst outcome available to this function.
    """
    records = []
    changed = False
    seen = {}
    for entry in entries or []:
        if isinstance(entry, dict):
            record = dict(entry)
        else:
            record = {"role": "", "content": str(entry)}
            changed = True

        key = (str(record.get("role", "")), str(record.get("content", "")))
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1

        existing = str(record.get(ID_FIELD, "") or "")
        if not existing:
            record[ID_FIELD] = _compute_id(record, ordinal)
            changed = True
        records.append(record)

    # A duplicated id (two records that were given the same one by an older build, or a
    # hand-edited file) would make deletion ambiguous, which is the one thing an id must never
    # be. Re-issue the later of the pair rather than refusing to load.
    used = set()
    for index, record in enumerate(records):
        while record[ID_FIELD] in used:
            record[ID_FIELD] = _compute_id(record, index + len(used) + 1)
            changed = True
        used.add(record[ID_FIELD])

    return records, changed


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE SERVICE                               │
# └────────────────────────────────────────────────────────────────────────┘

def store_path():
    """The primary memory file. Always from `core.paths`; never a guessed relative path."""
    primary, _backup = conversation_paths()
    return primary


def backup_path():
    _primary, backup = conversation_paths()
    return backup


def store_exists():
    return os.path.exists(store_path())


def list_memories(limit=None, newest_first=True):
    """
    Every saved memory, each with a stable `id`, `role`, `content` and a display `preview`.

    Migrates ids in on first read and persists them ONCE. A store that cannot be read at all
    returns an empty list rather than raising: this is called from a paint path, and a
    corrupted file must show as "nothing saved" plus a warning, never as a broken screen.
    """
    try:
        raw = load_conversation_memory() or []
    except Exception as exc:
        error(Subsystem.MEMORY, f"Could not read the memory store: {type(exc).__name__}")
        debug(Subsystem.MEMORY, str(exc))
        return []

    if not isinstance(raw, list):
        warning(Subsystem.MEMORY, "Memory store is not a list; treating it as empty")
        return []

    records, changed = _normalize(raw)
    if changed and records:
        # One migration write, through the SAME atomic helper the assistant uses. If it fails,
        # the ids are still correct in memory for this session — they are deterministic — so
        # listing and deleting keep working and the next attempt will try again.
        if save_conversation_memory(records):
            debug(Subsystem.MEMORY, f"Assigned identifiers to {len(records)} memories")
        else:
            warning(Subsystem.MEMORY, "Could not persist memory identifiers")

    items = [
        {
            "id": record[ID_FIELD],
            "role": str(record.get("role", "")),
            "content": str(record.get("content", "")),
            "preview": " ".join(str(record.get("content", "")).split()),
        }
        for record in records
    ]
    if newest_first:
        items.reverse()
    if limit is not None:
        items = items[:max(0, int(limit))]
    return items


def count_memories():
    try:
        raw = load_conversation_memory() or []
        return len(raw) if isinstance(raw, list) else 0
    except Exception:
        return 0


def delete_memory(memory_id):
    """
    Removes exactly one memory, by id. Returns (deleted, detail).

    THE STORE IS RE-READ HERE, not taken from whatever the screen is holding. Between the
    render and the click, a chat turn may have appended to the file; writing back a list built
    from a stale read would silently discard whatever arrived in between.

    Persistence is verified before anything is reported as gone (11.9). A failed write returns
    False, and the caller must leave the row on screen — a UI that removes a row on click and
    finds it back after a restart is worse than one that says the delete failed.
    """
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        return False, "no identifier given"

    try:
        raw = load_conversation_memory() or []
    except Exception as exc:
        error(Subsystem.MEMORY, f"Could not read the memory store: {type(exc).__name__}")
        return False, str(exc)

    records, _changed = _normalize(raw)
    remaining = [record for record in records if record.get(ID_FIELD) != memory_id]

    if len(remaining) == len(records):
        # Not an error — a stale screen, or a double click. Say so and change nothing.
        warning(Subsystem.MEMORY, f"No memory with id={memory_id}")
        return False, "no such memory"

    if not save_conversation_memory(remaining):
        error(Subsystem.MEMORY, f"Could not delete id={memory_id}: the write failed")
        return False, "the memory store could not be written"

    info(Subsystem.MEMORY, f"Deleted: id={memory_id}", correlate=False)
    return True, ""


def clear_all_memories():
    """
    Empties the store. Returns (cleared_count, ok).

    It writes an EMPTY LIST through the atomic helper; it never deletes, moves or truncates a
    file. That distinction is the whole safety property: "clear my memories" must not be a
    path that can remove anything on disk, and the backup written by the atomic helper is
    still there afterwards.
    """
    total = count_memories()
    if not save_conversation_memory([]):
        error(Subsystem.MEMORY, "Could not clear the memory store: the write failed")
        return 0, False
    # The COUNT, never the content. This is the most sensitive text in the process.
    info(Subsystem.MEMORY, f"Cleared: {total} memories", correlate=False)
    return total, True


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        OPENING THE LOCATION                            │
# └────────────────────────────────────────────────────────────────────────┘

def open_memory_location():
    """
    Reveals the memory file in Windows File Explorer. Returns (ok, detail).

    THE COMMAND IS AN ARGUMENT VECTOR, NEVER A STRING. `explorer.exe /select,<path>` is
    launched with `shell=False` and a list, so the path — which is data, even though it comes
    from `core.paths` rather than from the user — cannot be interpreted as shell syntax. The
    same rule the automation layer is AST-asserted against applies here: no `shell=True`, no
    `os.system`, no `cmd /c`, no `powershell -Command`. `tests/test_memory_store.py` asserts it.

    `/select` reveals the FILE with it highlighted rather than opening the folder blind, which
    is the difference between answering "where is my memory kept?" and answering "here is a
    folder, good luck". When the file does not exist yet, the parent folder is opened instead —
    `/select` on a missing path opens the user's Documents folder, which would be a confusing
    non-answer.

    Explorer's exit code is deliberately NOT treated as failure: `explorer.exe` routinely
    returns 1 having opened the window perfectly well. What IS reported is a failure to launch
    at all, and then the exact path and error are given so the user can navigate there by hand
    (11.10).
    """
    path = store_path()
    parent = os.path.dirname(path)

    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        error(Subsystem.MEMORY, f"Memory folder unavailable: {parent} ({exc})")
        return False, f"{parent}: {exc}"

    if not sys.platform.startswith("win"):
        # Kayra is a Windows assistant; this is here so the function is testable and honest
        # rather than silently doing nothing on another platform.
        warning(Subsystem.MEMORY, f"File Explorer is Windows-only. Memory store: {path}")
        return False, "not supported on this platform"

    command = explorer_command(path)
    try:
        subprocess.Popen(command, shell=False)
    except Exception as exc:
        error(Subsystem.MEMORY, f"Could not open File Explorer: {type(exc).__name__}: {exc}")
        info(Subsystem.MEMORY, f"Memory store: {path}", correlate=False)
        return False, f"{path}: {exc}"

    info(Subsystem.MEMORY, f"Opened location: {path}", correlate=False)
    return True, path


def explorer_command(path):
    """
    The exact argument vector used to reveal `path`. Separated so it can be asserted on
    without launching anything — a test that had to spawn Explorer to check the command shape
    would be a test nobody runs.
    """
    path = os.path.abspath(path)
    if os.path.exists(path) and os.path.isfile(path):
        # One argument, exactly: Explorer's /select takes the path joined to the switch by a
        # comma, and passing them as two arguments opens the wrong thing.
        return ["explorer.exe", f"/select,{path}"]
    target = path if os.path.isdir(path) else os.path.dirname(path)
    return ["explorer.exe", target]


def describe_store():
    """One dict describing where memory lives and how much of it there is, for the UI."""
    path = store_path()
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {
        "path": path,
        "backup_path": backup_path(),
        "exists": os.path.exists(path),
        "size_bytes": size,
        "count": count_memories(),
    }


def report_loaded():
    """
    Announces the store at boot: `[MEMORY] Store: …` and `[MEMORY] Loaded: 37 memories`.

    The count only. Called once from `bootstrap`, so the startup report says where memory
    lives and how much there is without any of it appearing on the terminal.
    """
    described = describe_store()
    info(Subsystem.MEMORY, f"Store: {described['path']}", correlate=False)
    info(Subsystem.MEMORY, f"Loaded: {described['count']} memories", correlate=False)
    return described
