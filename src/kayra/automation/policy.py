# ┌────────────────────────────────────────────────────────────────────────┐
# │                        automation_policy.py                            │
# │      Structured Actions, Safety Policy, Confirmations & Audit Log      │
# └────────────────────────────────────────────────────────────────────────┘
"""
The safety boundary of Kayra's computer-control layer.

Every automation request passes through this module before anything touches the machine, so
that the answer to "is this allowed?" lives in ONE auditable place instead of being scattered
through thirty handler functions where a single missed check is a formatted disk.

What is here
------------
* `Action`      — the structured representation the whole automation stack speaks. Produced
                  once by the normalizer; never re-parsed downstream from raw English.
* `ActionResult`— what an executor returns: a machine status plus the SPOKEN sentence.
* `classify_action` / `classify_shell` — the policy. Returns ALLOW, CONFIRM or DENY.
* `ConfirmationManager` — a pending high-risk action bound to a fingerprint, with a TTL.
* `AutomationContext`   — bounded memory of the last app / window / site / file, so "close it"
                  can resolve without hallucinating a referent.
* `audit()`     — structured logging of every automation decision.

DESIGN RULES (load-bearing)
---------------------------
* **The LLM never reaches the operating system.** It produces intent; deterministic Python in
  this file decides whether that intent may execute, and `automation_windows.py` decides how.
  Nothing in this module calls a model.
* **Deny is not string matching.** `classify_shell` parses the command, identifies the
  executable, and inspects arguments, target paths, destructive flags and scope. A blocklist of
  literal strings is trivially defeated by whitespace, quoting or an equivalent flag spelling.
* **A confirmation authorises exactly one action.** "Yes" resolves the fingerprint that was
  pending; it can never execute something else, and it expires.
* **Everything bounded.** Context, audit ring, and the confirmation slot are all fixed size.
"""

import os
import re
import time
import shlex
import threading
from collections import deque

from kayra.utils import setup_logger, print_warning


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          STRUCTURED ACTION                             │
# └────────────────────────────────────────────────────────────────────────┘

class Risk:
    """Policy verdict. There is no fourth value — every action gets exactly one of these."""
    ALLOW = "ALLOW"
    CONFIRM = "CONFIRM"
    DENY = "DENY"


class Status:
    """Outcome of an execution attempt."""
    OK = "OK"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"                    # policy said DENY
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    AMBIGUOUS = "AMBIGUOUS"                # several valid targets; we must ask
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"


class Action:
    """
    One structured automation intent.

    The whole point of this object is that everything downstream — policy, resolver, planner,
    executor, verifier — reads named fields instead of re-interpreting the user's sentence.
    The old design re-parsed the raw string at every layer, which is how "close window" ended
    up being handled by the generic "close <app>" branch.

    Fields:
        domain: "app" | "window" | "browser" | "media" | "keyboard" | "mouse" | "clipboard" |
                "system" | "file" | "shell" | "screen" | "timer" | "info" | "agent" | "chat"
        action: the verb within the domain ("open", "close_tab", "hotkey", ...)
        target: what it acts on. "current" means the foreground window / active tab.
        parameters: dict of extras (text to type, duration, key combo, ...)
        confidence: 0..1 from the normalizer. A low value must never be silently executed.
        raw: the original DMM token, kept for logging and for handlers that still take a string.
    """

    __slots__ = ("domain", "action", "target", "parameters", "confidence", "raw")

    def __init__(self, domain, action, target=None, parameters=None, confidence=1.0, raw=""):
        self.domain = domain
        self.action = action
        self.target = target
        self.parameters = parameters or {}
        self.confidence = confidence
        self.raw = raw

    @property
    def key(self) -> str:
        """`domain.action` — the identity used for policy lookups."""
        return f"{self.domain}.{self.action}"

    def fingerprint(self) -> str:
        """
        Stable identity of this exact action, used to bind a confirmation to it.

        Includes the target and the parameters that change what gets destroyed, so answering
        "yes" to a pending "delete the build folder" cannot execute "delete Documents".
        """
        params = ",".join(f"{k}={self.parameters[k]}" for k in sorted(self.parameters))
        return f"{self.domain}.{self.action}|{self.target}|{params}"

    def to_dict(self) -> dict:
        return {"domain": self.domain, "action": self.action, "target": self.target,
                "parameters": dict(self.parameters), "confidence": self.confidence,
                "raw": self.raw}

    def __repr__(self):
        return f"<Action {self.key} target={self.target!r} params={self.parameters}>"


class ActionResult:
    """
    What an executor hands back.

    `message` is SPOKEN, so it is a short natural sentence — never a status code, never a path,
    never markdown. `detail` carries the machine-readable extras for the log and for the caller.
    """

    __slots__ = ("status", "message", "detail", "action")

    def __init__(self, status, message="", detail=None, action=None):
        self.status = status
        self.message = message
        self.detail = detail or {}
        self.action = action

    @property
    def ok(self) -> bool:
        return self.status == Status.OK

    def __repr__(self):
        return f"<ActionResult {self.status} {self.message!r}>"

    # Convenience constructors — they make executor code read as prose.
    @staticmethod
    def success(message="Done.", **detail):
        return ActionResult(Status.OK, message, detail)

    @staticmethod
    def failure(message, **detail):
        return ActionResult(Status.FAILED, message, detail)

    @staticmethod
    def not_found(message, **detail):
        return ActionResult(Status.NOT_FOUND, message, detail)

    @staticmethod
    def ambiguous(message, **detail):
        return ActionResult(Status.AMBIGUOUS, message, detail)

    @staticmethod
    def blocked(message, **detail):
        return ActionResult(Status.BLOCKED, message, detail)

    @staticmethod
    def unsupported(message, **detail):
        return ActionResult(Status.UNSUPPORTED, message, detail)


# ┌────────────────────────────────────────────────────────────────────────┐
# │            THE POWER-TARGET BOUNDARY: KAYRA vs THE COMPUTER            │
# └────────────────────────────────────────────────────────────────────────┘
# "SHUT DOWN" NAMES TWO COMPLETELY DIFFERENT OPERATIONS, and confusing them is the worst
# mistake this codebase can make.
#
#   A. shut down KAYRA        -> end one process. Reversible in five seconds.
#   B. shut down the COMPUTER -> end the user's session, close everything they had open,
#                                and lose anything unsaved. Reversible in two minutes if
#                                they are lucky.
#
# THE FAILURE THIS EXISTS TO PREVENT, observed live:
#
#     transcript: "Shutdown the engine car."          <- a MALFORMED transcript. The user
#                                                        meant Kayra's engine.
#     DMM token : "system shutdown the engine car"
#     normalizer: _SYSTEM_VERBS did `if "shutdown" in payload` — a SUBSTRING test with no
#                 requirement that anything in the sentence names a computer
#     result    : Action("system", "shutdown") -> "This will shut down your computer.
#                 Should I go ahead?"
#
# One "yes" away from ending the user's session, from a sentence that never mentioned a
# computer. The substring test was the whole of the safety logic.
#
# So the destructive power verbs now require an EXPLICIT TARGET, resolved here, before the
# action is even constructed. Nothing infers a computer shutdown; the user has to name one.

# Words that unambiguously name THE MACHINE. Matched as whole words, never as substrings —
# "pc" must not match inside "pcap", and "system" is deliberately absent because it is the
# DMM's own token prefix ("system shutdown") and would therefore match every single one of
# these payloads, which is precisely the bug.
COMPUTER_TARGET_WORDS = frozenset({
    "computer", "pc", "laptop", "desktop", "machine", "windows", "notebook",
    "workstation", "device",
})

# Words that unambiguously name KAYRA. A payload naming one of these is a LIFECYCLE request
# that reached the automation layer by mistake — the local control interpreter should have
# caught it before the DMM ever saw it — and the correct response is to hand it back, never
# to touch the machine.
KAYRA_TARGET_WORDS = frozenset({
    "kayra", "engine", "assistant", "yourself", "you", "program", "app", "application",
    "jarvis", "bot",
})

# Verbs whose blast radius is the whole session. These are the ones that need a target.
# `lock`, `volume` and `brightness` are deliberately NOT here: locking is a one-keystroke
# undo and the other two are not destructive at all.
POWER_VERBS = frozenset({"shutdown", "restart", "sign_out", "sleep"})

# The three answers, named so the caller reads as prose.
TARGET_COMPUTER = "COMPUTER"
TARGET_KAYRA = "KAYRA"
TARGET_AMBIGUOUS = "AMBIGUOUS"

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def resolve_power_target(payload, assistant_alias="kayra"):
    """
    Whose power is this request about? Returns COMPUTER, KAYRA or AMBIGUOUS.

    WHOLE-WORD MATCHING, and the default is AMBIGUOUS. A payload that names neither is not
    quietly assumed to mean the computer — that assumption is exactly what turned "shutdown
    the engine car" into a Windows shutdown prompt. Naming BOTH is also ambiguous: "shut down
    Kayra and the computer" is two requests and the user must say which they meant.

    `assistant_alias` folds a renamed assistant into the Kayra set, so a user who called it
    "Vega" gets the same protection as one who did not.
    """
    words = {w for w in _WORD_SPLIT.split(str(payload or "").lower()) if w}
    if not words:
        return TARGET_AMBIGUOUS

    kayra_words = set(KAYRA_TARGET_WORDS)
    alias = str(assistant_alias or "").strip().lower()
    if alias:
        kayra_words.add(alias)

    names_computer = bool(words & COMPUTER_TARGET_WORDS)
    names_kayra = bool(words & kayra_words)

    if names_computer and not names_kayra:
        return TARGET_COMPUTER
    if names_kayra and not names_computer:
        return TARGET_KAYRA
    return TARGET_AMBIGUOUS


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        ACTION RISK CLASSIFICATION                      │
# └────────────────────────────────────────────────────────────────────────┘
# Ordinary computer control is ALLOW. Nothing here asks the user to confirm opening a tab —
# a confirmation prompt on a harmless action trains people to say "yes" reflexively, which is
# exactly what makes the prompt on a dangerous action worthless.

# Actions that are irreversible, affect the whole session, or destroy data.
_CONFIRM_ACTIONS = {
    "system.shutdown",
    "system.restart",
    "system.sign_out",
    "system.sleep",
    "system.wifi_off",
    "file.delete",
    "file.move",                 # a move can silently overwrite
    "app.kill",                  # terminating a process by force, not asking it to close
    "window.close_all",          # the whole desktop at once — the one close with a blast
                                 # radius wider than the thing the user named
    "screen.brightness_set",     # can render the display unreadable at 0
}

# Actions that must never run from a voice command, whatever the phrasing.
_DENY_ACTIONS = {
    "file.delete_recursive_system",
    "system.disable_security",
    "system.registry_write",
    # A power request whose target was never established. DENIED rather than confirmed:
    # a confirmation would be asking "shall I shut down the computer?" about a sentence that
    # may not have been about the computer at all, and a reflexive "yes" would end the
    # session. The executor answers with a question naming BOTH options instead.
    "system.power_ambiguous",
    # A power request that named KAYRA. It reached the automation layer only because the
    # transcript was malformed enough to miss the local control vocabulary; the machine must
    # not be touched for it under any circumstances.
    "system.power_kayra",
}


def classify_action(action: Action):
    """
    Policy verdict for a structured action.

    Returns (Risk, reason). The reason is for the audit log and, on DENY, for the sentence the
    user hears — a refusal the user does not understand is indistinguishable from a bug.
    """
    key = action.key

    if key in _DENY_ACTIONS:
        return Risk.DENY, f"{key} is never permitted from a voice command"

    # Shell is special: the verdict depends on the command, not on the fact that it is shell.
    if action.domain == "shell":
        return classify_shell(action.parameters.get("command") or action.target or "")

    # ── THE POWER-TARGET GATE ──
    # Belt and braces. The normalizer already refuses to build `system.shutdown` without an
    # explicit computer target, and this re-checks it from the action itself, because a
    # verdict that depends on one function having been called correctly is not a policy.
    if action.domain == "system" and action.action in POWER_VERBS:
        target_kind = action.parameters.get("power_target")
        if target_kind != TARGET_COMPUTER:
            return (Risk.DENY,
                    "a power action needs the computer named explicitly; "
                    f"this one resolved to {target_kind or 'nothing'}")

    if key in _CONFIRM_ACTIONS:
        return Risk.CONFIRM, f"{key} is irreversible or affects the whole session"

    # A bulk filesystem operation is confirmed even though the single-item version is not.
    if action.domain == "file" and action.parameters.get("bulk"):
        return Risk.CONFIRM, "affects many files at once"

    # The normalizer sets a low confidence when it had to guess. Guessing is fine for opening
    # a tab and unacceptable for anything that closes or deletes.
    if action.confidence < 0.5 and action.action in ("close", "delete", "kill", "quit"):
        return Risk.CONFIRM, "the target was not confidently identified"

    return Risk.ALLOW, ""


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     SHELL COMMAND SAFETY POLICY                        │
# └────────────────────────────────────────────────────────────────────────┘
# Kayra can run a terminal command, but the LLM never gets to hand a string to a shell. A
# request arrives as text, is PARSED here, and is judged on what it would actually do.
#
# The checks below deliberately look at the executable, the verbs, the flags and the target
# PATH separately. A blocklist of literal command strings is defeated by extra whitespace,
# different quoting, an equivalent flag, or a different order of arguments; asking "which
# program, doing what, to where, how broadly" is not.

# Executables with no legitimate voice-driven use. Matched on the stem, so "format.com",
# "C:\\Windows\\System32\\format.exe" and "FORMAT" are all the same entry.
_DENY_EXECUTABLES = {
    "format", "diskpart", "fdisk", "mkfs", "dd",
    "cipher",             # /w wipes free space
    "vssadmin",           # deleting shadow copies is the ransomware playbook
    "wbadmin",
    "bcdedit",            # boot configuration
    "reg",                # registry writes; reads are not worth the risk surface either
    "regedit",
    "sc",                 # service control — disabling defender/firewall services
    "netsh",              # firewall manipulation (Wi-Fi toggling has its own safe handler)
    "bcdboot",
    "attrib",             # used to unhide/strip system protection
    "takeown", "icacls", "cacls",   # permission stripping precedes destruction
    "schtasks", "at",     # persistence
    "wmic",               # process/product deletion in one line
    "rundll32",           # arbitrary DLL entry point execution
    "mshta", "regsvr32", "certutil",   # classic download-and-execute proxies
    "powershell", "pwsh", "cmd", "wscript", "cscript",
    # ^ a nested shell defeats every check below, so the *interpreters* themselves are denied
    #   as a shell target. Kayra's own internal handlers still call PowerShell directly where
    #   there is no API alternative; that is trusted, first-party, fixed-argument code — this
    #   list governs commands that originate from something the user said.
}

# Process-termination utilities. Denied outright as shell commands: closing an application has
# a first-class, target-resolved handler that operates on a specific window or PID, and a
# name-wide sweep is precisely the failure mode that would kill the user's browser session.
_DENY_PROCESS_KILLERS = {"taskkill", "tskill", "pskill", "killall", "pkill"}

# Destructive verbs paired with a scope test below.
_DELETE_EXECUTABLES = {"del", "erase", "rd", "rmdir", "rm", "remove-item", "ri"}

# Flags that turn a delete into a recursive, unstoppable delete.
_RECURSIVE_FLAGS = {"/s", "/q", "-r", "-rf", "-fr", "-recurse", "--recursive", "-force"}

# Locations that must never be a destructive target.
#
# The distinction between these two sets is the whole correctness of this check, and getting
# it wrong is expensive in BOTH directions. `_PROTECTED_TREES` protects a directory and
# everything under it: nothing inside the Windows directory is ever a legitimate delete.
# `_PROTECTED_EXACT` protects a location as a TARGET only: deleting the Users folder is
# catastrophic, deleting a file in the user's own home directory is an ordinary request, and
# an earlier version of this function conflated the two and refused every delete on the C:
# drive — including the user's own files, which made the feature useless rather than safe.
_PROTECTED_TREES = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\system volume information", "c:\\boot", "c:\\recovery",
    "/bin", "/sbin", "/etc", "/usr", "/boot", "/system", "/var", "/lib",
)

_PROTECTED_EXACT = (
    "c:", "c:\\", "c:\\users", "/", "/home", "/root",
)

# Wildcards that make a delete unbounded.
_WILDCARD = re.compile(r"[*?]")

# Environment variables that expand to a protected location.
_PROTECTED_ENV = ("%systemroot%", "%windir%", "%programfiles%", "%programdata%",
                  "%userprofile%", "$env:systemroot", "$env:windir")

# Shell metacharacters. Their presence means the text is trying to be shell SYNTAX rather than
# a program plus arguments, which is the one thing this layer exists to prevent.
_SHELL_METACHARS = ("&", "|", ";", "`", "$(", "&&", "||", ">", "<", "\n", "\r")

# Commands safe enough to run without asking. Read-only, bounded, no side effects.
_ALLOW_EXECUTABLES = {
    "echo", "whoami", "hostname", "date", "time", "ver", "cd", "pwd",
    "ipconfig", "ping", "tracert", "nslookup", "systeminfo", "tasklist",
    "git", "python", "pip", "node", "npm", "where", "which", "dir", "ls", "type", "cat",
}


def _stem(executable: str) -> str:
    """'C:\\Windows\\System32\\format.exe' -> 'format'. Path- and extension-insensitive."""
    name = os.path.basename((executable or "").strip().strip('"').replace("/", "\\"))
    for ext in (".exe", ".com", ".bat", ".cmd", ".ps1", ".msc"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name.lower()


def _is_protected_path(token: str) -> bool:
    """
    True when a token names a location that must never be a destructive target.

    Three separate questions, because they have three different answers:
      1. Is it inside a protected TREE (the Windows directory and below)? -> always protected
      2. Is it exactly a protected ROOT (a drive root, the Users folder, /)? -> protected as a target
      3. Is it an unbounded wildcard anchored at one of those roots? -> protected
    A path merely being on the C: drive is none of those, and treating it as protected would
    block every legitimate file operation the user has.
    """
    raw = (token or "").strip().strip('"').strip("'")
    if not raw:
        return False
    lowered = raw.lower()

    if any(env in lowered for env in _PROTECTED_ENV):
        return True

    # A bare drive root, with or without a trailing separator or wildcard.
    if re.fullmatch(r"[a-z]:[\\/]?\*{0,2}", lowered):
        return True

    has_wildcard = bool(_WILDCARD.search(lowered))
    try:
        normalized = os.path.normpath(raw).replace("/", "\\").lower().rstrip("\\")
    except (ValueError, TypeError):
        normalized = lowered.replace("/", "\\").rstrip("\\")
    if not normalized:
        return True                      # normpath collapsed it to the root

    # Unix-style absolute paths keep forward slashes for comparison.
    unix_form = os.path.normpath(raw).replace("\\", "/").lower().rstrip("/") if raw else ""

    # 1. Inside a protected tree.
    for tree in _PROTECTED_TREES:
        tree_win = tree.replace("/", "\\").rstrip("\\")
        if normalized == tree_win or normalized.startswith(tree_win + "\\"):
            return True
        if unix_form and (unix_form == tree.rstrip("/") or
                          unix_form.startswith(tree.rstrip("/") + "/")):
            return True

    # 2. Exactly a protected root.
    for exact in _PROTECTED_EXACT:
        exact_win = exact.replace("/", "\\").rstrip("\\")
        if normalized == exact_win:
            return True
        if unix_form and unix_form == exact.rstrip("/"):
            return True
    if raw.strip() in ("/", "\\"):
        return True

    # 3. A wildcard anchored directly at a protected root.
    if has_wildcard:
        parent = os.path.dirname(normalized).rstrip("\\")
        for exact in _PROTECTED_EXACT:
            if parent == exact.replace("/", "\\").rstrip("\\"):
                return True

    return False


def is_protected_path(path) -> bool:
    """
    Public form of the protected-location test, so executors share ONE definition of
    "somewhere that must never be deleted" with the shell policy. Two copies of this rule
    would eventually disagree, and the disagreement would be discovered destructively.
    """
    return _is_protected_path(path)


def classify_shell(command):
    """
    Policy verdict for a terminal command.

    Accepts either a string or an already-split argument vector. Returns (Risk, reason).

    The order of the checks matters: syntax that would escape the parser is rejected before
    anything is interpreted, then the executable, then the scope of what it acts on.
    """
    if isinstance(command, (list, tuple)):
        argv = [str(a) for a in command]
        text = " ".join(argv)
    else:
        text = str(command or "").strip()
        if not text:
            return Risk.DENY, "empty command"
        try:
            # posix=False keeps Windows backslashes intact instead of eating them as escapes.
            argv = shlex.split(text, posix=False)
        except ValueError:
            return Risk.DENY, "the command could not be parsed safely"

    if not argv:
        return Risk.DENY, "empty command"

    lowered_text = text.lower()

    # 1. Shell metacharacters — chaining, piping, redirection, substitution. Anything that
    #    wants to be shell syntax rather than a program with arguments is refused outright:
    #    every later check reasons about ONE command, and a chain defeats all of them.
    if any(ch in text for ch in _SHELL_METACHARS):
        return Risk.DENY, "chained or redirected shell syntax is not permitted"

    # 2. Fork bombs and their relatives — recognised by shape, not by the exact famous string.
    if re.search(r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:", text) or "%0|%0" in lowered_text:
        return Risk.DENY, "this looks like a fork bomb"

    stem = _stem(argv[0])
    args = argv[1:]
    lowered_args = [a.lower().strip('"') for a in args]

    # 3. Executables with no legitimate voice-driven use, including nested interpreters.
    if stem in _DENY_EXECUTABLES:
        return Risk.DENY, f"'{stem}' can damage the system and is not allowed this way"

    # 4. Process termination never goes through the shell — it has a resolved-target handler.
    if stem in _DENY_PROCESS_KILLERS:
        return Risk.DENY, ("closing programs by name can kill unrelated windows; "
                           "ask me to close the specific app instead")

    # 5. Destructive filesystem operations, judged by SCOPE.
    if stem in _DELETE_EXECUTABLES:
        recursive = any(a in _RECURSIVE_FLAGS for a in lowered_args)
        # A leading "/" is a Windows flag, but on a unix-style path it is the root itself.
        candidates = [a for a in args
                      if not a.startswith("-") and not re.fullmatch(r"/[a-z]{1,4}", a.lower())]
        for candidate in candidates:
            if _is_protected_path(candidate):
                return Risk.DENY, "that would delete a protected system location"
            if recursive and _WILDCARD.search(candidate):
                return Risk.DENY, "a recursive wildcard delete is not permitted"
        if not candidates:
            return Risk.DENY, "a delete with no explicit target is not permitted"
        if recursive:
            return Risk.CONFIRM, "this deletes a folder and everything inside it"
        return Risk.CONFIRM, "this deletes files"

    # 6. Shutdown / restart through the shell still needs the same confirmation as the
    #    first-class action, otherwise the policy is trivially bypassed by phrasing.
    if stem == "shutdown":
        return Risk.CONFIRM, "this shuts down or restarts the computer"

    # 7. Anything that writes to a raw device.
    if re.search(r"\\\\\.\\physicaldrive|of=/dev/|\\\\\.\\[a-z]:", lowered_text):
        return Risk.DENY, "raw disk access is not permitted"

    # 8. Known-safe read-only utilities run directly.
    if stem in _ALLOW_EXECUTABLES:
        return Risk.ALLOW, ""

    # 9. Everything else is plausible but unrecognised. Confirm rather than guess — an unknown
    #    executable is exactly the case where being wrong is expensive.
    return Risk.CONFIRM, f"I don't recognise '{stem}', so I'd rather check first"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        CONFIRMATION MANAGEMENT                         │
# └────────────────────────────────────────────────────────────────────────┘

class ConfirmationManager:
    """
    Holds AT MOST ONE pending high-risk action, bound to its fingerprint and expiring on a TTL.

    Two properties make this safe, and both are easy to lose:

    * A confirmation authorises the exact action that raised it. "Yes" resolves the stored
      fingerprint; there is no path by which a stale "yes" executes a different action.
    * It expires. A pending shutdown from four minutes ago must not be triggered by an
      unrelated "sure" later in the conversation.
    """

    # Long enough for the user to think, short enough that a forgotten prompt cannot be
    # answered by an unrelated "sure" later in the conversation.
    DEFAULT_TTL = float(os.environ.get("AUTOMATION_CONFIRM_TTL_SECONDS") or 60.0)

    def __init__(self, ttl=None, clock=None):
        self.ttl = ttl if ttl is not None else self.DEFAULT_TTL
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._pending = None          # {"action", "fingerprint", "expires", "prompt"}

    def request(self, action: Action, prompt: str):
        """Stores `action` as pending and returns the question to ask. Replaces any previous one."""
        with self._lock:
            self._pending = {
                "action": action,
                "fingerprint": action.fingerprint(),
                "expires": self._clock() + self.ttl,
                "prompt": prompt,
            }
        return prompt

    def peek(self):
        """The pending action if one is live, else None. Expiry is evaluated here."""
        with self._lock:
            if self._pending is None:
                return None
            if self._clock() >= self._pending["expires"]:
                self._pending = None
                return None
            return self._pending["action"]

    def consume(self):
        """Atomically takes the pending action, clearing it. Returns None if there is none."""
        with self._lock:
            action = self.peek()
            self._pending = None
            return action

    def cancel(self):
        with self._lock:
            had = self._pending is not None
            self._pending = None
            return had

    @property
    def prompt(self):
        with self._lock:
            return self._pending["prompt"] if self._pending else None


# Words that answer a pending confirmation. Matched only while something is actually pending,
# so a bare "yes" in ordinary conversation is never treated as authorisation.
_AFFIRMATIVE = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead",
                "proceed", "confirm", "confirmed", "affirmative", "please do", "yes please"}
_NEGATIVE = {"no", "nope", "nah", "cancel", "stop that", "don't", "do not", "never mind",
             "nevermind", "forget it", "abort", "negative"}


def read_confirmation_reply(text: str):
    """
    Classifies an utterance as an answer to a pending confirmation.

    Returns True (proceed), False (cancel) or None (not an answer — treat as a new command).
    Exact match on the cleaned utterance, never a substring test: "yes, and also open chrome"
    is a new instruction, and "no thanks, close the tab" must not read as a bare refusal.
    """
    if not text:
        return None
    cleaned = re.sub(r"[.,!?;:]+", " ", text.strip().lower())
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if cleaned in _AFFIRMATIVE:
        return True
    if cleaned in _NEGATIVE:
        return False
    return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     BOUNDED AUTOMATION CONTEXT                         │
# └────────────────────────────────────────────────────────────────────────┘

class AutomationContext:
    """
    What "it" refers to.

    Deliberately a handful of named slots rather than a history log: the assistant needs to
    resolve "close it" against the thing it just opened, not to keep a record of everything the
    user has ever done. Fixed size, no growth, nothing persisted to disk.
    """

    # A referent older than this is not what the user means by "it" any more.
    REFERENT_TTL = 300.0

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self.last_app = None
        self.last_window = None
        self.last_site = None
        self.last_url = None
        self.last_file = None
        self.last_action = None
        self._touched = {}
        # Bounded trail, for diagnostics only — never used to resolve a referent.
        self.recent = deque(maxlen=10)

    def note(self, **fields):
        """Records referents. Only known slots are accepted, so this cannot grow new keys."""
        with self._lock:
            now = self._clock()
            for name, value in fields.items():
                slot = f"last_{name}" if not name.startswith("last_") else name
                if value and hasattr(self, slot):
                    setattr(self, slot, value)
                    self._touched[slot] = now

    def note_action(self, action: Action, result_ok: bool):
        """Updates the referents an executed action established."""
        if not result_ok:
            return
        with self._lock:
            self.last_action = action.key
            self._touched["last_action"] = self._clock()
        updates = {}
        if action.domain == "app" and action.target:
            updates["app"] = action.target
            updates["window"] = action.target
        elif action.domain == "browser" and action.target and action.target != "current":
            updates["site"] = action.target
        elif action.domain == "file" and action.target:
            updates["file"] = action.target
        if action.parameters.get("url"):
            updates["url"] = action.parameters["url"]
        if updates:
            self.note(**updates)

    def resolve_referent(self, kind=None):
        """
        Best current referent for "it" / "this" / "that", or None.

        Returns None rather than a guess when nothing recent applies — a wrong referent on a
        `close` is worse than asking. Preference order reflects what a person means by "it":
        the most recently established concrete thing.
        """
        with self._lock:
            now = self._clock()

            def fresh(slot):
                value = getattr(self, slot, None)
                if not value:
                    return None
                if now - self._touched.get(slot, 0.0) > self.REFERENT_TTL:
                    return None
                return value

            if kind == "site":
                return fresh("last_site")
            if kind == "file":
                return fresh("last_file")
            if kind == "app":
                return fresh("last_app")

            # Untyped "it": whichever concrete referent was touched most recently.
            candidates = [(self._touched.get(s, 0.0), fresh(s))
                          for s in ("last_site", "last_app", "last_file", "last_window")]
            candidates = [(t, v) for t, v in candidates if v]
            if not candidates:
                return None
            return max(candidates, key=lambda item: item[0])[1]

    def snapshot(self):
        with self._lock:
            return {"app": self.last_app, "window": self.last_window, "site": self.last_site,
                    "url": self.last_url, "file": self.last_file, "action": self.last_action}

    def clear(self):
        with self._lock:
            self.last_app = self.last_window = self.last_site = None
            self.last_url = self.last_file = self.last_action = None
            self._touched.clear()
            self.recent.clear()


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             AUDIT LOG                                  │
# └────────────────────────────────────────────────────────────────────────┘

AUDIT_STARTED = "AUTOMATION_STARTED"
AUDIT_SUCCESS = "AUTOMATION_SUCCESS"
AUDIT_FAILED = "AUTOMATION_FAILED"
AUDIT_BLOCKED = "AUTOMATION_BLOCKED"
AUDIT_CONFIRM = "AUTOMATION_CONFIRM_REQUIRED"
AUDIT_AMBIGUOUS = "AUTOMATION_AMBIGUOUS"

_logger = None
_audit_ring = deque(maxlen=200)     # bounded: diagnostics, not history
_audit_lock = threading.Lock()

# Parameters whose VALUE is content rather than a control decision. Logged as a length, not a
# value — the audit trail should say that text was typed, not what the text was.
_SENSITIVE_PARAMS = {"text", "content", "clipboard", "password", "command_output"}


def audit(event: str, action=None, **fields):
    """
    Records one automation decision.

    Writes to `logs/kayra.log` through the project's logger and keeps a bounded in-memory ring
    for tests and diagnostics. Sensitive parameter values are reduced to their length.
    """
    global _logger
    entry = {"event": event, "ts": time.time()}
    if action is not None:
        entry["action"] = action.key
        entry["target"] = action.target
        safe_params = {}
        for name, value in (action.parameters or {}).items():
            if name in _SENSITIVE_PARAMS:
                safe_params[name] = f"<{len(str(value))} chars>"
            else:
                safe_params[name] = value
        entry["parameters"] = safe_params
    entry.update(fields)

    with _audit_lock:
        _audit_ring.append(entry)

    try:
        if _logger is None:
            _logger = setup_logger("kayra.automation", "automation.log")
        payload = " ".join(f"{k}={v!r}" for k, v in entry.items() if k not in ("event", "ts"))
        _logger.info("%s %s", event, payload)
    except Exception:
        # Logging must never be able to break an automation the user asked for.
        pass
    return entry


def recent_audit(limit=20):
    with _audit_lock:
        return list(_audit_ring)[-limit:]


def clear_audit():
    with _audit_lock:
        _audit_ring.clear()
