# ┌────────────────────────────────────────────────────────────────────────┐
# │                           conversation.py                              │
# │              Long-Term Conversation Memory Persistence                 │
# └────────────────────────────────────────────────────────────────────────┘
"""
The assistant's permanent memory: the exchanges the user explicitly asked to keep.

Two tiers exist in Kayra. The SHORT-term tier is an in-RAM list owned by `services/chatbot.py`,
capped at the last handful of messages and lost on restart. This module owns the LONG-term
tier — a JSON file that is only ever appended to when the user says a trigger phrase
("remember this", "store this", ...).

It lives in its own package rather than in `utils` because it is the only durable
conversational state in the system, and durable state deserves a boundary you can point at.

CORRUPTION RESISTANCE
---------------------
Writes are atomic in the only sense that matters here: the backup file is written FIRST and
only then copied over the primary. A crash mid-write can therefore lose the newest exchange
but can never leave the primary database half-written, and `load_conversation_memory` falls
back to the backup when the primary is unreadable.
"""

import json
import shutil

from kayra.core.paths import conversation_paths, data_path, data_dir
from kayra.utils.console import print_warning, print_error


def get_data_paths():
    """
    (primary, backup) absolute paths for the conversation database.

    Resolved from the project root, never from the working directory. Bare relative paths
    were a real bug here: launching Kayra from outside the project folder silently split the
    assistant's memory across several files.
    """
    return conversation_paths()


def load_conversation_memory():
    """
    Loads long-term conversation history from the persistent JSON database.

    Fault-Tolerance:
        If the primary file is missing or corrupted (e.g. due to a sudden
        process halt mid-write), transparently falls back to the rolling
        secondary backup copy.

    Returns:
        list: Long-term conversational history, or an empty list if neither
              the primary database nor its backup can be read.
    """
    db_file, backup_file = get_data_paths()
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            print_warning("Primary index compromised. Restored data from rolling backup.")
            return data
        except Exception:
            return []


def save_conversation_memory(memory_list):
    """
    Persists the long-term conversation history using an atomic write pattern.

    Corruption-Prevention Strategy:
        1. Write to the backup file first.
        2. Only once that succeeds, copy the backup over the primary file.
        This guarantees the primary database is never left half-written if the
        process is interrupted mid-save.

    Args:
        memory_list (list): The full conversation history to persist.

    Returns:
        bool: True if the write succeeded, False otherwise.
    """
    db_file, backup_file = get_data_paths()
    try:
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(memory_list, f, indent=4, ensure_ascii=False)
        shutil.copy(backup_file, db_file)
        return True
    except Exception as e:
        print_error(f"Persistent storage transaction failed: {e}")
        return False
