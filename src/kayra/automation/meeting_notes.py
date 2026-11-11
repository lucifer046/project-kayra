# ┌────────────────────────────────────────────────────────────────────────┐
# │                     automation/meeting_notes.py                        │
# │   "Take notes of this meeting" — captions kept, summarized locally     │
# └────────────────────────────────────────────────────────────────────────┘
"""
BROWSER_CONTROL_PLAN §12 M4.3 (Part 13). "Take notes of this meeting" turns Meet's captions on and
reads them every few seconds into memory; "stop taking notes" (or leaving the call) writes them
to `Reports/meetings/<date> <time> <title>.txt` (0600). "Summarize this meeting" sends the notes
to the LOCAL model only (`ai.summarize(local_only=True)`) for a short summary and the action
items, spoken and saved beside the notes. Without a local model, the last lines are read instead
— a meeting's words never go to a cloud model.

One daemon thread (`kayra-meet-notes`) exists only while notes are being taken; it waits on an
Event, so stopping is immediate, and `stop()` is part of shutdown.

Captions grow while someone speaks ("We should" -> "We should ship on Friday"): a line that
extends the previous one replaces it, so the notes hold each sentence once.
"""

import os
import re
import time
import threading
import datetime

POLL_S = 3.0
MAX_LINES = 4000
STATE = {"thread": None, "stop": None, "lines": [], "title": "", "started": 0.0, "path": "", "last": ""}
_LOCK = threading.Lock()
CLOCK = time.time


def _W():
    from kayra.automation import web_macros
    return web_macros


def active():
    return STATE["thread"] is not None


def add_lines(lines):
    """Merge one captions reading into the notes (also the test seam)."""
    with _LOCK:
        notes = STATE["lines"]
        for line in lines:
            line = " ".join(str(line).split())
            if not line:
                continue
            if notes and (line.startswith(notes[-1]) or notes[-1].startswith(line)):
                notes[-1] = line if len(line) >= len(notes[-1]) else notes[-1]
            elif line not in notes[-12:]:
                notes.append(line)
        del notes[:-MAX_LINES]


def _read_once():
    W = _W()
    try:
        result = W.BRIDGE.call("meet.do", timeout=8.0, op="captions", match="meet.google.com") or {}
    except Exception:
        return False
    add_lines(result.get("lines") or [])
    return bool(result.get("on"))


def _loop(stop):
    while not stop.wait(POLL_S):
        _read_once()


def start():
    """'take notes of this meeting'."""
    from kayra.automation.policy import ActionResult
    W = _W()
    stop = W._bridge_at((1, 16, 0))
    if stop:
        return stop
    if not W._activate_call() or W._CALL["site"] != "meet":
        return ActionResult.not_found("I can take notes in a Google Meet call — you're not in one.")
    if active():
        return ActionResult.success("I'm already taking notes.")
    W.meet_toggle("captions", True)
    title = ""
    try:
        title = re.sub(r"^Meet\s*[-–]\s*", "", str((W._call("tabs.active") or {}).get("title") or "")).strip()
    except Exception:
        pass
    with _LOCK:
        STATE.update(lines=[], title=title[:60] or "Meeting", started=CLOCK(), path="", last="")
    event = threading.Event()
    thread = threading.Thread(target=_loop, args=(event,), name="kayra-meet-notes", daemon=True)
    STATE.update(thread=thread, stop=event)
    thread.start()
    return ActionResult.success("Taking notes from the captions. Say \"stop taking notes\" when you're done.")


def _folder():
    from kayra.core.paths import reports_dir
    return os.path.join(reports_dir(), "meetings")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def stop(save=True):
    """'stop taking notes' (also on leaving the call and at shutdown)."""
    from kayra.automation.policy import ActionResult
    event, thread = STATE.get("stop"), STATE.get("thread")
    if thread is None:
        return ActionResult.success("I wasn't taking notes.")
    event.set()
    thread.join(timeout=2.0)
    STATE.update(thread=None, stop=None)
    if save:
        _read_once()                              # the last captions before the call ends
    with _LOCK:
        lines = list(STATE["lines"])
    if not save or not lines:
        return ActionResult.success("Stopped taking notes. The captions had nothing to keep.")
    stamp = datetime.datetime.fromtimestamp(STATE["started"] or CLOCK()).strftime("%Y-%m-%d %H%M")
    name = re.sub(r"[^\w .()-]", "", STATE["title"])[:50].strip() or "Meeting"
    path = os.path.join(_folder(), f"{stamp} {name}.txt")
    _write(path, "\n".join(lines) + "\n")
    STATE.update(path=path, last="\n".join(lines))
    return ActionResult.success(f"Saved {len(lines)} lines of notes to Reports, meetings. Say \"summarize this "
                                "meeting\" for the summary and the action items.", path=path)


SUMMARY_PROMPT = ("You are Kayra. These are the live captions of a meeting. In at most five spoken sentences: "
                  "what was decided, then the action items as 'Name will …' with any dates. No lists, no markdown. "
                  "Never read out codes or passwords.")


def summarize():
    """'summarize this meeting' — the notes in progress, else the last saved ones."""
    from kayra.automation import ai
    from kayra.automation.policy import ActionResult
    with _LOCK:
        text = "\n".join(STATE["lines"]) if STATE["lines"] else STATE["last"]
    if not text.strip():
        return ActionResult.not_found("I haven't taken notes of a meeting yet. Say \"take notes of this meeting\" during the call.")
    spoken = ai.summarize(SUMMARY_PROMPT, text[-12000:], fallback="", local_only=True)
    if not spoken:
        tail = text.strip().splitlines()[-3:]
        return ActionResult.success("I need the local model for a summary. The last thing said: " + " ... ".join(tail))
    if STATE.get("path"):
        _write(STATE["path"][:-4] + " summary.txt", spoken + "\n")
    return ActionResult.success(spoken)
