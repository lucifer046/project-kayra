# ┌────────────────────────────────────────────────────────────────────────┐
# │                          test_dmm_matrix.py                            │
# │            Decision-Making Model Intent Boundary Test Matrix           │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_dmm_matrix.py — accuracy harness for `CentralizedLLMEngine.classify_intent()`.

    .venv\\Scripts\\python tests\\test_dmm_matrix.py [--verbose]

Every case is a real classification call against whichever DMM the engine routes to
(Cohere online, local model offline), so this measures the shipped behaviour rather than a
mock. Cases are grouped by the intent BOUNDARY they probe — the pairs that are semantically
close and easy to confuse are the point of the matrix:

    open app / open URL          close app / close window / close tab
    minimize / minimize all      general / realtime / deep research
    media control / play         automation keyword inside a conversational sentence

The `forbidden` field is what keeps the classifier honest: "how do I take a screenshot on a
Mac?" must NOT fire the screenshot automation just because the word appears in it.

Reports per-category accuracy, duplicate-token rate, invalid-token rate and unexecutable
tokens (tokens the automation router has no branch for, which would be silently dropped).
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules.llm_engine import CentralizedLLMEngine
from modules.utils import print_banner, print_info, print_success, print_error, print_system, console

VERBOSE = "--verbose" in sys.argv

# Cohere trial keys rate-limit aggressively. Without pacing, roughly a fifth of the matrix
# comes back as the rate-limit fallback ('general <query>') and scores as a misclassification,
# which makes the numbers meaningless. Pace requests instead of measuring the API's quota.
DELAY = 3.5
for _arg in sys.argv[1:]:
    if _arg.startswith("--delay="):
        DELAY = float(_arg.split("=", 1)[1])

# Each case: (category, query, expected, forbidden)
#   expected  — list of predicates; every one must be satisfied by some emitted token.
#               a str means "some token starts with this"; a tuple means "some token starts
#               with ANY of these" (used where more than one routing is legitimately correct).
#   forbidden — token prefixes that must NOT appear.
CASES = [
    # ── conversation vs realtime vs deep research ──────────────────────────
    ("knowledge",   "Explain quantum computing to me.",              ["general "],   ["realtime ", "open ", "content "]),
    ("knowledge",   "Who was Akbar?",                                ["general "],   ["realtime "]),
    ("knowledge",   "What do you think about remote work?",          ["general "],   ["realtime "]),
    ("knowledge",   "Tell me more about him.",                       ["general "],   ["realtime "]),
    ("realtime",    "What's the weather right now?",                 ["realtime "],  ["general ", "open "]),
    ("realtime",    "What is today's news?",                         ["realtime "],  ["general "]),
    ("realtime",    "Who is the current prime minister of India?",   ["realtime "],  ["general "]),
    ("realtime",    "Search the web for the latest iPhone price.",   [("realtime ", "google search ")], ["general "]),
    ("research",    "Do deep research on fusion energy developments.", ["deep research "], ["general ", "realtime "]),
    ("research",    "Do a deep dive on solid state battery scaling.",  ["deep research "], ["general ", "realtime "]),

    # ── application control ────────────────────────────────────────────────
    ("app",         "Open Chrome.",                                  ["open chrome"], ["general ", "new tab"]),
    ("app",         "Open github.com",                               ["open github"], ["google search "]),
    ("app",         "Close Chrome.",                                 ["close chrome"], ["close window", "close tab"]),
    ("app",         "Open Chrome and Telegram.",                     ["open chrome", "open telegram"], []),

    # ── window control ─────────────────────────────────────────────────────
    ("window",      "Close this window.",                            ["close window"], ["close tab"]),
    ("window",      "Minimize everything.",                          ["minimize all"], []),
    ("window",      "Maximize this window.",                         ["maximize"],    ["minimize"]),
    ("window",      "Snap this to the left.",                        ["snap left"],   []),
    ("window",      "Show me the desktop.",                          [("minimize all", "show desktop")], ["open "]),

    # ── tab control ────────────────────────────────────────────────────────
    ("tab",         "Close this tab.",                               ["close tab"],   ["close window"]),
    ("tab",         "Open a new tab.",                               ["new tab"],     ["open "]),
    ("tab",         "Refresh the page.",                             [("refresh", "reload")], []),

    # ── media ──────────────────────────────────────────────────────────────
    ("media",       "Pause the music.",                              ["pause"],       ["play "]),
    ("media",       "Skip to the next song.",                        ["next track"],  []),
    ("media",       "Stop the music.",                               ["stop media"],  ["general ", "close "]),
    ("media",       "Play some jazz.",                               ["play "],       ["pause"]),

    # ── system control ─────────────────────────────────────────────────────
    ("system",      "Mute the sound.",                               ["system mute"], []),
    ("system",      "Set brightness to 40 percent.",                 ["system "],     []),
    ("system",      "Lock my pc.",                                   ["system lock"], []),
    ("system",      "Turn off wifi.",                                ["wifi"],        []),

    # ── system information ─────────────────────────────────────────────────
    ("sysinfo",     "Check my battery status.",                      ["battery"],     ["general ", "realtime "]),
    ("sysinfo",     "How much RAM am I using?",                      ["ram"],         ["general ", "realtime "]),
    ("sysinfo",     "What's my IP address?",                         ["ip address"],  ["realtime "]),

    # ── input / clipboard / hotkeys ────────────────────────────────────────
    ("input",       "Copy that.",                                    ["copy"],        ["copy text"]),
    ("input",       "Type hello world.",                             [("write ", "type ")], []),
    ("input",       "Undo that.",                                    ["undo"],        []),
    ("input",       "Select all.",                                   ["select all"],  []),

    # ── utilities ──────────────────────────────────────────────────────────
    ("utility",     "Set a timer for 5 minutes.",                    [("set timer", "timer ")], []),
    ("utility",     "Take a screenshot.",                            [("take screenshot", "screenshot")], []),
    ("utility",     "Write me an email to my boss about the delay.", ["content "],    ["write ", "general "]),

    # ── multi-intent ───────────────────────────────────────────────────────
    ("multi",       "Open Chrome and search for today's weather.",   ["open chrome", ("realtime ", "google search ")], []),
    ("multi",       "Minimize all windows and take a screenshot.",   ["minimize all", ("take screenshot", "screenshot")], []),
    ("multi",       "Mute the sound and lock the computer.",         ["system mute", "system lock"], []),
    ("multi",       "Who is Akshay Kumar and what's his net worth?", ["realtime "],   []),

    # ── negative / ambiguity: automation words inside conversation ─────────
    ("negative",    "What's the best way to close a business deal?", ["general "],    ["close ", "close window", "close tab"]),
    ("negative",    "How do I take a screenshot on a Mac?",          [("general ", "realtime ")], ["take screenshot", "screenshot"]),
    ("negative",    "Tell me about the play Hamlet.",                [("general ", "realtime ")], ["play "]),
    ("negative",    "I need to find a good restaurant nearby.",      [("general ", "realtime ")], ["find"]),
    ("negative",    "Can you open up about how you work?",           ["general "],    ["open "]),
    ("negative",    "Explain how to minimize latency in a web app.", ["general "],    ["minimize", "minimize all"]),

    # ── long / detailed natural language ───────────────────────────────────
    ("long",        "I've been trying to understand how transformer models handle long context "
                    "windows, especially the attention mechanism and why it scales quadratically, "
                    "so could you walk me through it in detail?", ["general "], ["realtime ", "content "]),
    ("long",        "Open Spotify for me and then set a timer for twenty five minutes because "
                    "I want to do a focused work session.", ["open spotify", ("set timer", "timer ")], []),

    # ── exit ───────────────────────────────────────────────────────────────
    ("exit",        "Goodbye Kayra.",                                ["exit"],        ["general "]),
]

# Tokens the automation router in modules/automation_windows.py can actually execute.
# A token outside this set is either a conversational route or would be silently dropped.
ROUTABLE_PREFIXES = (
    "general ", "realtime ", "deep research ", "exit",
    "write ", "type ", "open ", "close ", "play ", "content ",
    "youtube search ", "google search ", "web search ", "system ",
    "screenshot", "take screenshot", "copy", "paste", "copy text ",
    "minimize all", "show desktop", "snap left", "snap right", "switch window",
    "alt tab", "task view", "maximize", "minimize", "action center",
    "notification", "emoji",
    "pause", "resume", "next track", "previous track", "skip track", "stop media",
    "play media", "play pause",
    "battery", "ip address", "disk", "storage", "ram", "memory", "cpu",
    "processor", "uptime", "network info", "wifi status",
    "timer ", "set timer ", "remind",
    "undo", "redo", "select all", "save file", "find", "new tab", "refresh",
    "reload", "fullscreen", "zoom in", "zoom out", "reset zoom", "task manager",
    "run dialog", "wifi",
    "volume", "brightness", "mute", "lock", "shutdown", "restart", "sleep",
)


def satisfied(expectation, tokens):
    options = expectation if isinstance(expectation, tuple) else (expectation,)
    return any(t.lower().startswith(o.lower()) for t in tokens for o in options)


def run_matrix():
    engine = CentralizedLLMEngine()
    mode = "Cohere (online)" if engine.is_online else "Local model (offline)"
    print_info(f"DMM route: {mode}")

    per_cat = {}
    failures = []
    dup_cases = 0
    unroutable_cases = 0
    total_latency = 0.0

    for index, (category, query, expected, forbidden) in enumerate(CASES):
        if index and DELAY:
            time.sleep(DELAY)
        t0 = time.perf_counter()
        try:
            tokens = engine.classify_intent(query)
        except Exception as e:
            tokens = [f"<exception {e}>"]
        total_latency += time.perf_counter() - t0

        lowered = [t.lower().strip() for t in tokens]

        missing = [e for e in expected if not satisfied(e, lowered)]
        violated = [f for f in forbidden if any(t.startswith(f.lower()) for t in lowered)]
        duplicated = len(lowered) != len(set(lowered))
        unroutable = [t for t in lowered
                      if not any(t.startswith(p.lower()) for p in ROUTABLE_PREFIXES)]

        ok = not missing and not violated and not duplicated and not unroutable
        stats = per_cat.setdefault(category, [0, 0])
        stats[1] += 1
        if ok:
            stats[0] += 1
        else:
            failures.append((category, query, tokens, missing, violated, duplicated, unroutable))
        if duplicated:
            dup_cases += 1
        if unroutable:
            unroutable_cases += 1

        if VERBOSE or not ok:
            mark = "[success]OK  [/success]" if ok else "[error]BAD [/error]"
            console.print(f"{mark} [dim]{category:<9}[/dim] {query[:58]!r} -> {tokens}")
            if missing:
                console.print(f"       [warning]missing[/warning]: {missing}")
            if violated:
                console.print(f"       [error]forbidden token emitted[/error]: {violated}")
            if duplicated:
                console.print(f"       [error]duplicate tokens[/error]")
            if unroutable:
                console.print(f"       [error]token has no automation branch[/error]: {unroutable}")

    total_ok = sum(v[0] for v in per_cat.values())
    total = sum(v[1] for v in per_cat.values())

    print_system("\n" + "=" * 62)
    for cat in sorted(per_cat):
        ok, n = per_cat[cat]
        bar = "#" * int(round(12 * ok / n))
        console.print(f"  {cat:<10} {ok:>2}/{n:<3} {100*ok/n:5.1f}%  [dim]{bar}[/dim]")
    print_system("=" * 62)
    print_info(f"duplicate-token cases:   {dup_cases}/{total}")
    print_info(f"unexecutable-token cases:{unroutable_cases}/{total}")
    print_info(f"mean classification time: {total_latency/total:.2f}s")

    if total_ok == total:
        print_success(f"DMM matrix: {total_ok}/{total} (100.0%)")
    else:
        print_error(f"DMM matrix: {total_ok}/{total} ({100*total_ok/total:.1f}%)")
    return total_ok, total, failures


if __name__ == "__main__":
    print_banner("KAYRA DMM TEST MATRIX", "Intent boundary accuracy & output-contract validation")
    ok, total, _ = run_matrix()
    sys.exit(0 if ok == total else 1)
