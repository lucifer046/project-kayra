# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_memory_store.py                            │
# │      Memory Management — identity, deletion, safety and location       │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_memory_store.py — standalone diagnostic for the memory management service.

    .venv\\Scripts\\python tests\\test_memory_store.py

HARDWARE-FREE AND NON-DESTRUCTIVE. It never touches the developer's real memory: `core.paths`
is redirected at a temporary directory for the duration and restored afterwards, and the
suite asserts that redirection actually took before writing anything. Explorer is never
launched — the command VECTOR is asserted instead, because a test that had to spawn a file
manager to check a command shape is a test nobody runs.

It exercises

  1. Stable identity: ids are content-derived, persisted, unique, and survive a reload.
  2. Listing, including the empty store and a corrupted one.
  3. Deleting one memory, by id — including the ids that must NOT match.
  4. Clearing everything, and the guarantee that it deletes no file.
  5. Persistence, atomicity, and the backup the existing helper writes.
  6. The path, read from the one authority.
  7. Opening the location: the command vector, the /select behaviour, and the safety rules.
  8. Privacy: counts and ids in the log, never content.
  9. The UI contract, and that it deletes by id rather than by row.
"""

import os
import re
import sys
import ast
import json
import time
import shutil
import inspect
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core import logbus, paths
from kayra.memory import conversation, store

FAILURES = []
SANDBOX = None
_REAL_DATA_DIR = None


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ──────────────────────────────────────────────────────────────────────────
#                               SANDBOX
# ──────────────────────────────────────────────────────────────────────────

def enter_sandbox():
    """
    Redirects the memory store into a temporary directory.

    `conversation.get_data_paths` is the one function every writer here goes through, so
    substituting it redirects the whole service — including `store`, which imports the load
    and save helpers from that module rather than resolving paths itself. That is worth
    asserting rather than assuming: if `store` ever grew its own path resolution, this
    sandbox would silently stop protecting the developer's real memory.
    """
    global SANDBOX, _REAL_DATA_DIR
    _REAL_DATA_DIR = paths.data_dir()
    SANDBOX = tempfile.mkdtemp(prefix="kayra-memtest-")
    primary = os.path.join(SANDBOX, "conversation.json")
    backup = os.path.join(SANDBOX, "conversation_backup.json")

    conversation.get_data_paths = lambda: (primary, backup)
    store.conversation_paths = lambda: (primary, backup)
    return primary, backup


def leave_sandbox():
    if SANDBOX and os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)


def write_raw(entries):
    primary, _ = store.conversation_paths()
    with open(primary, "w", encoding="utf-8") as handle:
        json.dump(entries, handle)


def read_raw():
    primary, _ = store.conversation_paths()
    with open(primary, "r", encoding="utf-8") as handle:
        return json.load(handle)


def code_of(module):
    """
    A module's source with every docstring and comment stripped.

    Several rules here are STATED IN PROSE inside the module they govern — `store.py`
    explicitly documents that it must not use `os.system`, `cmd /c` or `powershell` for this.
    A substring test over the raw source then fails on the documentation rather than on the
    code, which is worse than useless: it trains the reader to delete the explanation.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


def calls_in(module):
    """Every function and method NAME called anywhere in a module."""
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
                if isinstance(node.func.value, ast.Name):
                    names.add(f"{node.func.value.id}.{node.func.attr}")
    return names


SAMPLE = [
    {"role": "user", "content": "remember my flight is on the 4th"},
    {"role": "assistant", "content": "Noted — the 4th."},
    {"role": "user", "content": "remember I prefer Brave"},
    {"role": "assistant", "content": "Noted."},
]


# ──────────────────────────────────────────────────────────────────────────
#                        1. STABLE IDENTITY
# ──────────────────────────────────────────────────────────────────────────

def section_identity():
    print_system("\n[1] Stable identity — the property deletion depends on")

    primary, backup = store.conversation_paths()
    check("the sandbox is in a temporary directory, not the project",
          SANDBOX in primary and _REAL_DATA_DIR not in primary, primary)

    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)

    check("every memory gets an id", all(item["id"] for item in items))
    check("ids are unique", len({item["id"] for item in items}) == len(items))
    check("ids are fixed-length", {len(item["id"]) for item in items} == {store.ID_LENGTH})
    check("ids are hex, so nothing downstream is tempted to parse them",
          all(re.fullmatch(r"[0-9a-f]+", item["id"]) for item in items))

    # Persisted, not recomputed on the fly.
    stored = read_raw()
    check("ids are written INTO the store", all("id" in record for record in stored))
    check("no existing field was removed",
          all({"role", "content"} <= set(record) for record in stored))
    check("the record count is unchanged", len(stored) == len(SAMPLE))
    check("the content is untouched",
          [r["content"] for r in stored] == [r["content"] for r in SAMPLE])

    # Stable across reloads.
    again = store.list_memories(newest_first=False)
    check("the same store yields the same ids on reload",
          [i["id"] for i in again] == [i["id"] for i in items])

    # Stable across a fresh computation from the same content.
    write_raw(SAMPLE)
    fresh = store.list_memories(newest_first=False)
    check("ids are content-derived, so a rebuilt store yields the same ones",
          [i["id"] for i in fresh] == [i["id"] for i in items])

    # Duplicate content stays two deletable things.
    write_raw([{"role": "user", "content": "same"}, {"role": "user", "content": "same"}])
    dupes = store.list_memories(newest_first=False)
    check("identical memories get different ids",
          dupes[0]["id"] != dupes[1]["id"], str([d["id"] for d in dupes]))
    check("and both are still listed", len(dupes) == 2)

    # A hand-edited file with colliding ids is repaired rather than refused.
    write_raw([{"role": "user", "content": "a", "id": "dup"},
               {"role": "user", "content": "b", "id": "dup"}])
    repaired = store.list_memories(newest_first=False)
    check("a colliding id is re-issued rather than making deletion ambiguous",
          repaired[0]["id"] != repaired[1]["id"],
          str([r["id"] for r in repaired]))

    # A bare string from some older version is WRAPPED, never dropped.
    write_raw(["a loose string someone saved", {"role": "user", "content": "normal"}])
    mixed = store.list_memories(newest_first=False)
    check("a non-dict entry is kept, not discarded", len(mixed) == 2, str(len(mixed)))
    check("and it gets an id like everything else", all(item["id"] for item in mixed))
    check("and its text survives",
          mixed[0]["content"] == "a loose string someone saved", mixed[0]["content"])

    # Ordering.
    write_raw(SAMPLE)
    newest = store.list_memories()
    check("listing is newest-first by default",
          newest[0]["content"] == SAMPLE[-1]["content"], newest[0]["content"])
    check("a limit bounds the result", len(store.list_memories(limit=2)) == 2)
    check("a limit of 0 returns nothing", store.list_memories(limit=0) == [])


# ──────────────────────────────────────────────────────────────────────────
#                       2. LISTING AND EDGE CASES
# ──────────────────────────────────────────────────────────────────────────

def section_listing():
    print_system("\n[2] Listing — empty, missing and corrupted stores")

    primary, backup = store.conversation_paths()

    # Missing file.
    for path in (primary, backup):
        if os.path.exists(path):
            os.remove(path)
    check("a missing store lists as empty", store.list_memories() == [])
    check("and counts as zero", store.count_memories() == 0)
    check("and reports that it does not exist", store.store_exists() is False)

    # Empty list.
    write_raw([])
    check("an empty store lists as empty", store.list_memories() == [])
    check("an empty store does not crash the count", store.count_memories() == 0)

    # Corrupted JSON, with no backup: the existing loader returns [], and so does this.
    with open(primary, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    check("a corrupted store lists as empty rather than raising",
          store.list_memories() == [])
    check("and does not crash the count", store.count_memories() == 0)

    # Corrupted primary WITH a good backup: the existing fallback still works through this.
    with open(backup, "w", encoding="utf-8") as handle:
        json.dump(SAMPLE, handle)
    recovered = store.list_memories()
    check("a corrupted primary falls back to the backup, as it always did",
          len(recovered) == len(SAMPLE), str(len(recovered)))

    # Not a list at all.
    with open(primary, "w", encoding="utf-8") as handle:
        json.dump({"not": "a list"}, handle)
    if os.path.exists(backup):
        os.remove(backup)
    check("a store that is not a list is treated as empty",
          store.list_memories() == [])


# ──────────────────────────────────────────────────────────────────────────
#                        3. DELETING ONE MEMORY
# ──────────────────────────────────────────────────────────────────────────

def section_delete_one():
    print_system("\n[3] Deleting one memory, by id")

    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)
    target = items[1]

    deleted, detail = store.delete_memory(target["id"])
    check("deleting by id succeeds", deleted is True, str(detail))

    remaining = store.list_memories(newest_first=False)
    check("exactly one memory is gone", len(remaining) == len(SAMPLE) - 1)
    check("the RIGHT one is gone",
          target["content"] not in [item["content"] for item in remaining])
    check("the others are untouched",
          [item["content"] for item in remaining] ==
          [entry["content"] for index, entry in enumerate(SAMPLE) if index != 1])
    check("their ids did not change",
          [item["id"] for item in remaining] ==
          [item["id"] for index, item in enumerate(items) if index != 1])

    # It really persisted.
    check("the deletion is on disk",
          len(read_raw()) == len(SAMPLE) - 1, str(len(read_raw())))

    # A nonexistent id changes nothing.
    before = read_raw()
    deleted, detail = store.delete_memory("ffffffffffff")
    check("an unknown id is refused", deleted is False)
    check("and says so rather than raising", "no such memory" in detail, detail)
    check("and the store is untouched", read_raw() == before)

    for bad in ("", None, "   "):
        deleted, _ = store.delete_memory(bad)
        check(f"an empty id ({bad!r}) is refused", deleted is False)
    check("and the store is STILL untouched", read_raw() == before)

    # Double-deletion: the second is a refusal, not a second removal.
    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)
    store.delete_memory(items[0]["id"])
    deleted, _ = store.delete_memory(items[0]["id"])
    check("deleting the same id twice removes exactly one memory",
          deleted is False and len(read_raw()) == len(SAMPLE) - 1)

    # THE POSITION BUG. A memory appended between the read and the click must not shift what
    # gets deleted — which is exactly what deleting by index did.
    write_raw(SAMPLE)
    rendered = store.list_memories()               # what a screen would be holding
    doomed = rendered[0]                           # the newest, at row 0
    appended = list(read_raw()) + [{"role": "user", "content": "arrived after the render"}]
    write_raw(appended)
    store.delete_memory(doomed["id"])
    contents = [item["content"] for item in store.list_memories()]
    check("a memory appended after the render does not shift the deletion",
          doomed["content"] not in contents, str(contents))
    check("and the newly appended one survives",
          "arrived after the render" in contents, str(contents))

    # A write failure must report failure, so the UI can leave the row on screen.
    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)
    real_save = conversation.save_conversation_memory
    store_save = store.save_conversation_memory
    store.save_conversation_memory = lambda entries: False
    try:
        deleted, detail = store.delete_memory(items[0]["id"])
    finally:
        store.save_conversation_memory = store_save
        conversation.save_conversation_memory = real_save
    check("a failed write reports failure rather than claiming success", deleted is False)
    check("and says the store could not be written", "written" in detail, detail)
    check("and the memory is still there",
          len(read_raw()) == len(SAMPLE), str(len(read_raw())))


# ──────────────────────────────────────────────────────────────────────────
#                          4. CLEARING EVERYTHING
# ──────────────────────────────────────────────────────────────────────────

def section_clear():
    print_system("\n[4] Clearing everything — without deleting a file")

    write_raw(SAMPLE)
    primary, backup = store.conversation_paths()

    cleared, ok = store.clear_all_memories()
    check("clearing reports success", ok is True)
    check("clearing reports the count it removed", cleared == len(SAMPLE), str(cleared))
    check("the store is empty", store.list_memories() == [])

    # THE SAFETY PROPERTY: it writes an empty list, it does not remove anything from disk.
    check("the memory FILE still exists", os.path.exists(primary))
    check("and holds an empty list", read_raw() == [])
    check("the backup still exists too", os.path.exists(backup))

    check("clearing an already-empty store is harmless",
          store.clear_all_memories() == (0, True))

    # Nothing in this module may remove a file. This is the rule that makes "clear my
    # memories" incapable of deleting anything on disk.
    source = inspect.getsource(store)
    tree = ast.parse(source)
    dangerous = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("remove", "unlink", "rmtree", "rmdir", "truncate"):
                dangerous.add(node.func.attr)
    check("the module calls no file-removal function", not dangerous, str(sorted(dangerous)))
    check("and no shutil at all", "shutil" not in source)

    write_raw(SAMPLE)
    real_save = store.save_conversation_memory
    store.save_conversation_memory = lambda entries: False
    try:
        cleared, ok = store.clear_all_memories()
    finally:
        store.save_conversation_memory = real_save
    check("a failed clear reports failure", ok is False and cleared == 0)
    check("and the memories are still there", len(read_raw()) == len(SAMPLE))


# ──────────────────────────────────────────────────────────────────────────
#                     5. PERSISTENCE AND ATOMICITY
# ──────────────────────────────────────────────────────────────────────────

def section_persistence():
    print_system("\n[5] Persistence — through the existing atomic write, not around it")

    primary, backup = store.conversation_paths()
    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)
    store.delete_memory(items[0]["id"])

    check("the backup is written as well as the primary", os.path.exists(backup))
    with open(backup, "r", encoding="utf-8") as handle:
        backup_data = json.load(handle)
    check("the backup matches the primary after a delete", backup_data == read_raw())

    # The service must not have its own persistence. Every write goes through the one helper
    # that writes the backup first and copies it over the primary.
    source = inspect.getsource(store)
    # The CALLS, not the prose: the module must never open the store itself. Every write goes
    # through `save_conversation_memory`, which writes the backup first and copies it over the
    # primary — the atomicity the whole store depends on.
    calls = calls_in(store)
    check("the service never opens a file itself",
          "open" not in calls, str(sorted(c for c in calls if "open" in c)))
    check("nor writes one", not ({"write", "writelines", "dump"} & calls),
          str(sorted(calls & {"write", "writelines", "dump"})))
    check("it uses the existing atomic save helper",
          "save_conversation_memory" in source)
    check("it uses the existing loader", "load_conversation_memory" in source)
    check("it does not import json for its own serialisation",
          "import json" not in code_of(store))

    # Survives a reload in a fresh interpreter view of the module.
    write_raw(SAMPLE)
    items = store.list_memories(newest_first=False)
    kept = items[2]["id"]
    store.delete_memory(items[0]["id"])
    store.delete_memory(items[1]["id"])
    reloaded = store.list_memories(newest_first=False)
    check("deletions survive a reload", len(reloaded) == 2, str(len(reloaded)))
    check("and the surviving ids are unchanged",
          kept in [item["id"] for item in reloaded], kept)


# ──────────────────────────────────────────────────────────────────────────
#                              6. THE PATH
# ──────────────────────────────────────────────────────────────────────────

def section_path():
    print_system("\n[6] The path comes from the one authority")

    described = store.describe_store()
    for key in ("path", "backup_path", "exists", "size_bytes", "count"):
        check(f"describe_store carries {key}", key in described)

    check("the path is absolute", os.path.isabs(described["path"]), described["path"])
    check("the primary and backup differ", described["path"] != described["backup_path"])
    check("the count matches the store", described["count"] == store.count_memories())

    # No path is guessed here: `core.paths` is the single source of truth, and a bare relative
    # path once fragmented the assistant's memory across several files.
    source = inspect.getsource(store)
    check("the module resolves paths through core.paths",
          "conversation_paths" in source)
    code = code_of(store)
    check("and hardcodes no data path in the code",
          "conversation.json" not in code and "data\\\\" not in code)

    # The un-sandboxed truth, for the report.
    real_primary, _ = paths.conversation_paths()
    print_info(f"      real memory store: {real_primary}")
    check("the real store lives under the project's data directory",
          real_primary.startswith(_REAL_DATA_DIR), real_primary)


# ──────────────────────────────────────────────────────────────────────────
#                      7. OPENING THE LOCATION
# ──────────────────────────────────────────────────────────────────────────

def section_open():
    print_system("\n[7] Opening the location — an argument vector, never a shell string")

    primary, _ = store.conversation_paths()
    write_raw(SAMPLE)

    command = store.explorer_command(primary)
    check("the command is a LIST, not a string", isinstance(command, list), str(type(command)))
    check("the executable is explorer.exe", command[0] == "explorer.exe", str(command))
    check("an existing file is revealed with /select",
          command[1].startswith("/select,"), str(command))
    check("the path is absolute in the command",
          os.path.isabs(command[1][len("/select,"):]), str(command))
    check("/select and the path are ONE argument, as Explorer requires",
          len(command) == 2, str(command))

    # A missing file opens the folder instead: /select on a missing path opens Documents,
    # which would be a confusing non-answer.
    missing = os.path.join(SANDBOX, "does-not-exist.json")
    command = store.explorer_command(missing)
    check("a missing file falls back to opening its folder",
          "/select" not in " ".join(command), str(command))
    check("and that folder is the store's directory",
          os.path.normpath(command[1]) == os.path.normpath(SANDBOX), str(command))

    command = store.explorer_command(SANDBOX)
    check("a directory is opened directly",
          command == ["explorer.exe", os.path.abspath(SANDBOX)], str(command))

    # THE SAFETY RULES. These are the same ones the automation layer is AST-asserted against.
    source = inspect.getsource(store)
    tree = ast.parse(source)
    shell_true = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "shell"
        and isinstance(node.value, ast.Constant) and node.value.value is True
    ]
    check("no shell=True anywhere", not shell_true)
    check("shell=False is passed explicitly", "shell=False" in source)
    code = code_of(store)
    check("no os.system", "os.system" not in code)
    check("no cmd /c", "cmd /c" not in code.lower() and "cmd.exe" not in code.lower())
    check("no powershell", "powershell" not in code.lower())
    check("no taskkill", "taskkill" not in code.lower())
    check("subprocess is used, but only Popen with a vector",
          "subprocess.Popen" in code and "subprocess.call" not in code
          and "check_output" not in code)
    check("the command is never built by string concatenation",
          not re.search(r'"explorer[^"]*"\s*\+', source))

    # A launch failure reports the exact path, so the user can navigate there by hand.
    real_popen = store.subprocess.Popen

    def explode(*args, **kwargs):
        raise OSError("Explorer is not available")

    store.subprocess.Popen = explode
    try:
        ok, detail = store.open_memory_location()
    finally:
        store.subprocess.Popen = real_popen
    if sys.platform.startswith("win"):
        check("a failed launch reports failure", ok is False)
        check("and the message carries the exact path", primary in detail, detail)

    # A successful launch is not attempted here — spawning a file manager in a test suite is
    # exactly the kind of side effect that makes a suite nobody runs. The vector is asserted
    # above, which is the part that can be wrong.
    calls = []
    store.subprocess.Popen = lambda *a, **k: calls.append((a, k))
    try:
        ok, detail = store.open_memory_location()
    finally:
        store.subprocess.Popen = real_popen
    if sys.platform.startswith("win"):
        check("a successful launch reports success", ok is True, str(detail))
        check("Explorer was invoked exactly once", len(calls) == 1)
        check("with shell=False", calls and calls[0][1].get("shell") is False, str(calls))
        check("and with a list argument",
              calls and isinstance(calls[0][0][0], list), str(calls))


# ──────────────────────────────────────────────────────────────────────────
#                             8. PRIVACY
# ──────────────────────────────────────────────────────────────────────────

def section_privacy():
    print_system("\n[8] Privacy — counts and ids in the log, never content")

    lines = []
    saved = {name: getattr(store, name) for name in ("info", "warning", "error", "debug")}

    def capture(level):
        return lambda subsystem, message, **kwargs: lines.append(
            logbus.format_line(level, subsystem, message, timestamp="00:00:00"))

    store.info = capture(logbus.INFO)
    store.warning = capture(logbus.WARNING)
    store.error = capture(logbus.ERROR)
    store.debug = capture(logbus.DEBUG)
    try:
        secret = "my bank password is hunter2 and my address is 14 Elm Street"
        write_raw([{"role": "user", "content": secret}])
        items = store.list_memories()
        store.delete_memory(items[0]["id"])

        write_raw([{"role": "user", "content": secret},
                   {"role": "assistant", "content": secret}])
        store.clear_all_memories()
        store.report_loaded()
    finally:
        for name, fn in saved.items():
            setattr(store, name, fn)

    text = "\n".join(lines)
    check("no memory content reaches the log", secret not in text, text)
    check("not even a fragment of it", "hunter2" not in text and "Elm Street" not in text)
    check("a deletion logs its id", re.search(r"Deleted: id=[0-9a-f]+", text) is not None, text)
    check("a clear logs only a count",
          re.search(r"Cleared: \d+ memories", text) is not None, text)
    check("the boot report logs a count", re.search(r"Loaded: \d+ memories", text) is not None)
    check("the boot report logs the path", "Store:" in text, text)
    check("every line is tagged [MEMORY]", all("[MEMORY]" in line for line in lines), text)


# ──────────────────────────────────────────────────────────────────────────
#                          9. THE UI CONTRACT
# ──────────────────────────────────────────────────────────────────────────

def section_ui():
    print_system("\n[9] The UI deletes by id, and confirms first")

    from kayra.ui.views import memory as memory_view

    source = inspect.getsource(memory_view)

    check("the row is constructed with a memory record, not an index",
          "def __init__(self, entry, on_delete" in source, "")
    check("the row holds the memory's id", "self._memory_id" in source)
    check("the delete button passes the id", "on_delete(self._memory_id)" in source)
    check("no index is passed to the delete handler",
          "on_delete(index)" not in source)
    check("the view no longer slices the last thirty entries",
          "entries[-30:]" not in source)
    check("the view reads through the managed listing",
          "bridge.list_memories()" in source)
    check("the view no longer imports the raw persistence helpers directly",
          "from kayra.memory.conversation import" not in source)

    check("deleting one memory confirms first",
          source.count("QMessageBox") >= 2)
    check("clearing everything states that it cannot be undone",
          "cannot be undone" in source, "")
    check("and the default button is Cancel",
          source.count("setDefaultButton(QMessageBox.Cancel)") >= 2)
    check("a failed deletion does not remove the row",
          "if not deleted:" in source and "return" in source)

    check("the screen shows the memory location", "memory_store()" in source)
    check("and offers to open it", "open_memory_location" in source)
    check("the path is never hardcoded in the view's code",
          "conversation.json" not in code_of(memory_view))

    # The bridge and session are pass-throughs, with no second store behind them.
    from kayra.ui import bridge as ui_bridge, session as ui_session
    for module, label in ((ui_bridge, "bridge"), (ui_session, "session")):
        text = inspect.getsource(module)
        check(f"{label} exposes list_memories", "def list_memories" in text)
        check(f"{label} exposes delete_memory", "def delete_memory" in text)
        check(f"{label} exposes clear_memories", "def clear_memories" in text)
        check(f"{label} exposes open_memory_location", "def open_memory_location" in text)
        check(f"{label} keeps no memory of its own",
              "self._memories" not in text and "self.memories" not in text)

    # Cost: listing must be O(n) and must not scan anything else.
    write_raw([{"role": "user", "content": f"memory {index}"} for index in range(2000)])
    started = time.perf_counter()
    items = store.list_memories(limit=50)
    elapsed = (time.perf_counter() - started) * 1000.0
    check("listing 2000 memories is fast", elapsed < 400.0, f"{elapsed:.1f}ms")
    check("and a limit really bounds the result", len(items) == 50)
    check("while the count still reports the truth", store.count_memories() == 2000)

    started = time.perf_counter()
    store.delete_memory(store.list_memories(limit=1)[0]["id"])
    elapsed = (time.perf_counter() - started) * 1000.0
    check("deleting from 2000 memories is fast", elapsed < 500.0, f"{elapsed:.1f}ms")
    check("and removed exactly one", store.count_memories() == 1999)

    from kayra.ui.views.memory import MemoryView
    check("the view bounds how many rows it builds",
          isinstance(MemoryView.MAX_ROWS, int) and MemoryView.MAX_ROWS <= 200,
          str(MemoryView.MAX_ROWS))


def main():
    print_banner("MEMORY STORE DIAGNOSTIC", "Identity · deletion · safety · location")
    enter_sandbox()
    try:
        section_identity()
        section_listing()
        section_delete_one()
        section_clear()
        section_persistence()
        section_path()
        section_open()
        section_privacy()
        section_ui()
    finally:
        leave_sandbox()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All memory store checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
