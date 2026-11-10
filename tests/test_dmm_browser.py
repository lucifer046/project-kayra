# ┌────────────────────────────────────────────────────────────────────────┐
# │                         test_dmm_browser.py                            │
# │   Browser / Meet / Workspace phrasings through the REAL routing path    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_dmm_browser.py — integration tier, needs a LOCAL model (LM Studio). Added in the V9
tuning pass because the 53-case DMM matrix has none of the ~150 browser phrasings.

    .venv/bin/python tests/test_dmm_browser.py [--verbose]

Each case goes through exactly what the app does with an utterance:

    rules first (`common.direct_tokens`, with a synthetic screen)  ->  else the classifier
    (`CentralizedLLMEngine.classify_intent`)  ->  `normalize_command`  ->  the action

and is scored on the ACTION it ends as (domain.action + target), not on the token's wording.
Conversational sentences must NOT become automation. Reports accuracy, how many went
rules-first (no model), and latency. Refuses to run on cloud models: it would spend money
and measure a different model.

SAFE: nothing is executed. No browser, no extension, no desktop.
"""

import os
import sys
import time
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, run  # noqa: E402

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.core.config import load_environment  # noqa: E402

check = Checker("browser intent")
VERBOSE = "--verbose" in sys.argv

SHEET = {"tab_url": "https://docs.google.com/spreadsheets/d/x/edit"}
SLIDES = {"tab_url": "https://docs.google.com/presentation/d/x/edit"}
WEB = {"tab_url": "https://example.com/article"}
WA = {"site": "web.whatsapp.com", "app": "chrome-web.whatsapp.com__-default"}

# (said, screen, expected action key, expected target or None = any)
CASES = (
    # tabs and pages (Parts 1–2)
    ("open the third result", WEB, "browser.page_result", "3"),
    ("play the second video", WEB, "browser.page_result", "2"),
    ("open the last result in a new tab", WEB, "browser.page_result", "-1"),
    ("close all my YouTube tabs", WEB, "browser.tab_close", "youtube"),
    ("close everything except the pinned tabs", WEB, "browser.tabs_close_unpinned", None),
    ("close duplicate tabs", WEB, "browser.tabs_close_duplicates", None),
    ("mute everything except this tab", WEB, "browser.tabs_mute_others", None),
    ("what did I just close", WEB, "browser.tabs_recently_closed", None),
    ("free up memory in chrome", WEB, "browser.tabs_discard", None),
    ("find the tab where I was reading about rust", WEB, "browser.tab_find", "rust"),
    ("group my college tabs", WEB, "browser.tabs_group", "college"),
    ("make the research group blue", WEB, "browser.group_color", "research"),
    ("collapse the research group", WEB, "browser.group_collapse", "research"),
    ("move this tab to a new window", WEB, "browser.tab_detach", None),
    ("merge my chrome windows", WEB, "browser.windows_merge", None),
    ("find pricing on this page", WEB, "browser.page_find", "pricing"),
    ("next match", WEB, "browser.page_find_step", None),
    # about the page (Part 3)
    ("what does this page say about refunds", WEB, "browser.page_ask", None),
    ("does this page mention refunds", WEB, "browser.page_ask", None),
    ("read the comments", WEB, "browser.page_comments", None),
    ("summarize the reviews", WEB, "browser.page_comments", None),
    ("summarize this video", WEB, "browser.video_summary", None),
    ("translate this page to Hindi", WEB, "browser.page_translate", "hindi"),
    ("explain the selected text", WEB, "browser.selection_explain", None),
    ("bookmark this page", WEB, "browser.bookmark_add", None),
    ("clear today's history", WEB, "browser.history_clear", "today"),
    ("what did I download", WEB, "browser.downloads_list", None),
    ("open my last download", WEB, "browser.download_open", None),
    ("pause the download", WEB, "browser.download_control", "pause"),
    ("what can I do here", WEB, "browser.page_help", None),
    # Workspace (Part 4)
    ("open my budget sheet", WEB, "browser.ws_open", "budget"),
    ("open the doc about the internship", WEB, "browser.ws_open", "internship"),
    ("search my docs for offer letter", WEB, "browser.ws_search", "offer letter"),
    ("find the PDF Ravi sent me", WEB, "browser.ws_search", None),
    ("create a new doc", WEB, "browser.ws_create", "doc"),
    ("create a new sheet called Expenses", WEB, "browser.ws_create", "sheet"),
    ("go to cell B12", SHEET, "browser.sheet_cell", "B12"),
    ("what's the total of column C", SHEET, "browser.sheet_calc", "c"),
    ("read row 5", SHEET, "browser.sheet_row", "5"),
    ("share this doc with ana@x.com", WEB, "browser.ws_share", "ana@x.com"),
    ("start the slideshow", SLIDES, "browser.slides_present", None),
    ("next slide", SLIDES, "browser.slides_nav", "next"),
    ("add a note saying buy milk", WEB, "browser.keep_note", None),
    ("what assignments are due", WEB, "browser.classroom_due", None),
    ("open my recent files", WEB, "browser.ws_recent", None),
    # Gmail / Calendar / Meet
    ("search my mail for the fee receipt", WEB, "email.search", "fee receipt"),
    ("show my unread emails from this week", WEB, "email.search", None),
    ("compose a new email", WEB, "email.compose", None),
    ("what's my next meeting", WEB, "calendar.next", None),
    ("join my next meeting", WEB, "calendar.next", None),
    ("let them in", WEB, "meet.admit", None),
    ("share my screen", WEB, "meet.share", "monitor"),
    ("start screen sharing", WEB, "meet.share", "monitor"),
    ("share the LM Studio window", WEB, "meet.share", "window"),
    ("stop sharing", WEB, "meet.stop_share", None),
    ("mute me", WEB, "meet.mic", None),
    # WhatsApp (Part 5) — the bug report's sentences, with WhatsApp in front
    ("open XYZ chat", WA, "message.open", "xyz"),
    ("open PR and outreach core team chat", WA, "message.open", None),
    ("click on design team 2026", WA, "message.open", "design team 2026"),
    ("who messaged me", WA, "message.unread", None),
    ("read it", WA, "message.read", None),
    ("open youtube", WA, "browser.open_url", None),
    # …and from anywhere
    ("open the PR group on WhatsApp", WEB, "message.open", None),
    ("who messaged me on WhatsApp", WEB, "message.unread", None),
    ("what did they say in the core team today", WEB, "message.read", None),
    ("tell Rahul I'm running late", WEB, "message.send", "rahul"),
    # must stay conversation / web questions (never automation)
    ("who invented the telephone", WEB, "chat", None),
    ("what's the total of 25 and 30", WEB, "chat", None),
    ("how do I share my screen on zoom", WEB, "chat", None),
    ("tell me about the history of Rome", WEB, "chat", None),
    ("what are the results of the election", WEB, "chat", None),
    ("explain how transformers work", WEB, "chat", None),
)

CHAT_HEADS = ("general ", "realtime ", "deep research ")


def outcome(tokens, normalize):
    """The action key a token list ends as: 'chat' for conversation, else domain.action."""
    if not tokens:
        return "none", None
    if all(t.lower().startswith(CHAT_HEADS) for t in tokens):
        return "chat", None
    for token in tokens:
        if token.lower().startswith(CHAT_HEADS):
            continue
        action = normalize(token)
        if action is not None:
            return action.key, action.target
    return "unparsed", None


def main():
    print_banner("BROWSER INTENT (rules first, then the local classifier)")
    load_environment()
    os.environ["CLOUD_FIRST"] = "False"
    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    from kayra.automation import common
    engine = CentralizedLLMEngine()
    if getattr(engine, "is_online", True):
        check.skip("all cases", "no local model is running (this suite never uses cloud models)")
        return check.finish()
    engine.classify_intent("hello")                         # warm the prompt cache
    timings, ruled = [], 0
    for said, screen, expected, target in CASES:
        started = time.perf_counter()
        tokens = common.direct_tokens(said, lambda screen=screen: screen)
        if tokens:
            ruled += 1
        else:
            tokens = engine.classify_intent(said)
        timings.append(time.perf_counter() - started)
        key, got_target = outcome(tokens, common.normalize_command)
        ok = key == expected and (target is None or str(got_target).lower() == target.lower())
        check(f"{said!r} -> {expected}" + (f" {target!r}" if target else ""), ok,
              f"{tokens} -> {key} {got_target!r}")
        if VERBOSE:
            print_info(f"    {timings[-1] * 1000:5.0f} ms  {tokens}")
    print_info(f"Rules first (no model): {ruled}/{len(CASES)}; median {statistics.median(timings) * 1000:.0f} ms, "
               f"worst {max(timings) * 1000:.0f} ms per utterance")
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
