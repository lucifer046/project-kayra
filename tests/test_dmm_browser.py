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
YT = {"app": "google-chrome", "tab_url": "https://www.youtube.com/watch?v=x"}
SHOP = {"app": "google-chrome", "tab_url": "https://www.amazon.in/dp/B0"}

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
    # the V11 fix round: whatever is in front
    ("scroll the mail to bottom", WEB, "browser.page_scroll", "bottom"),
    ("scroll the chat up a bit", WA, "browser.page_scroll", "up"),
    ("open graphics team group in whatsapp", WEB, "message.open", "graphics team"),
    ("read the recent chat", WEB, "message.read", "recent"),
    ("click on the search button and search for council details", WEB, "browser.search_here", "council details"),
    ("close the search bar", WA, "browser.search_close", None),
    ("open council details 2026-27 sheet", WA, "browser.ws_open", "council details 2026-27"),
    ("open youtube", WA, "browser.open_url", None),
    # …and from anywhere
    ("open the PR group on WhatsApp", WEB, "message.open", None),
    ("who messaged me on WhatsApp", WEB, "message.unread", None),
    ("what did they say in the core team today", WEB, "message.read", None),
    ("tell Rahul I'm running late", WEB, "message.send", "rahul"),
    # Part 6: forms, the video, site search and shopping
    ("fill this form with my details", WEB, "browser.form_fill", None),
    ("select India in the country dropdown", WEB, "browser.form_choose", "India"),
    ("tick I agree", WEB, "browser.form_choose", None),
    ("submit this form", WEB, "browser.form_submit", None),
    ("upload my resume here", WEB, "browser.file_upload", "resume"),
    ("remember my phone number is 98765 43210", WEB, "browser.profile_set", "phone"),
    ("skip the ad", YT, "browser.video", "skipAd"),
    ("skip 10 seconds", YT, "browser.video", "seek"),
    ("play at 1.5x", YT, "browser.video", "rate"),
    ("turn on captions", YT, "browser.video", "captions"),
    ("search amazon for earbuds under 2000", WEB, "browser.site_search", "earbuds"),
    ("images of red pandas", WEB, "browser.site_search", "red pandas"),
    ("how much is this", SHOP, "browser.product_info", None),
    ("sort by price low to high", SHOP, "browser.shop_refine", None),
    ("add this to cart", SHOP, "browser.page_click", "add to cart"),
    # Part 6b: AI sites, WhatsApp replies and files, Reddit / LinkedIn / GitHub, several steps
    ("ask chatgpt what a monad is", WEB, "browser.ai_ask", "what a monad is"),
    ("ask Claude to write a haiku about rain", WEB, "browser.ai_ask", "write a haiku about rain"),
    ("what does gemini think of the new pixel phone", WEB, "browser.ai_ask", None),
    ("read the whole answer", WEB, "browser.ai_read", None),
    ("reply saying I'll be there in ten minutes", WA, "message.send", "current"),
    ("send my resume to Ravi on whatsapp", WEB, "message.send_file", "ravi"),
    ("send this pdf here", WA, "message.send_file", "current"),
    ("read my linkedin notifications", WEB, "browser.social", "notifications"),
    ("what's the top post on r/python this week", WEB, "browser.social", "top"),
    ("open r/india", WEB, "browser.social", "subreddit"),
    ("open the github repo pallets/flask", WEB, "browser.social", "repo"),
    ("search linkedin jobs for data analyst", WEB, "browser.site_search", "data analyst"),
    ("open the budget sheet and total column c", WEB, "browser.ws_open", "budget"),
    # Part 5P: Chrome profiles
    ("open my work chrome", WEB, "browser.chrome_profile", "work"),
    ("open chrome as kiran", WEB, "browser.chrome_profile", "kiran"),
    ("open gmail in my study chrome", WEB, "browser.chrome_profile", "study"),
    ("which chrome profiles do I have", WEB, "browser.chrome_profiles", None),
    ("open chrome", WEB, "app.open", "chrome"),
    ("what is chatgpt", WEB, "chat", None),
    ("is claude better than gemini", WEB, "chat", None),
    ("what's the price of gold today", WEB, "chat", None),
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
