# ┌────────────────────────────────────────────────────────────────────────┐
# │                     test_browser_extension.py                          │
# │   The Kayra Bridge extension's page functions, run in a REAL Chrome    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_browser_extension.py — integration tier: needs Google Chrome (or Chromium).

    .venv/bin/python tests/test_browser_extension.py

The extension's in-page functions (click by accessible name, type into a labelled field,
refuse a password field, scroll to text, find, list controls, read text) are pulled out of
`extension/kayra-bridge/background.js` and executed in a headless Chrome against a page that
imitates Google Meet's and Gmail's accessible names. This is what proves "click on Share
screen" lands on Meet's real button shape, not on a fake.

SAFE: a throwaway headless Chrome with its own temporary profile, a data: URL page, no
network. It never touches your Chrome or your profile.
"""

import os
import sys
import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, run  # noqa: E402
from kayra.utils import print_banner  # noqa: E402

check = Checker("browser extension")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = open(os.path.join(ROOT, "extension", "kayra-bridge", "background.js"), encoding="utf-8").read()

PAGE = """<html><head><title>Meet - abc</title></head><body><main>
<button aria-label="Turn off microphone (ctrl + d)" onclick="document.title='mic'">X</button>
<div role=button data-tooltip="Share screen" onclick="document.title='share'"><span>icon</span></div>
<button onclick="document.title='save1'">Save</button><button onclick="document.title='save2'">Save</button>
<div style="display:none"><button onclick="document.title='hidden'">Leave call</button></div>
<label for=e>Email address</label><input id=e>
<input placeholder="Search mail">
<input type=password aria-label="Password">
<input aria-label="Card number">
<div contenteditable=true aria-label="Type a message"></div>
<p style='margin-top:3000px'>Pricing section</p></main></body></html>"""


GMAIL = """<html><head><title>Inbox (1119) - me@gmail.com - Gmail</title></head><body>
<div role=navigation>
 <a href="#inbox" aria-label="Inbox 1119 unread" onclick="document.title='inbox';return false">Inbox</a>
 <a href="#sent" onclick="document.title='sent';return false">Sent</a>
 <span role=button aria-label="More labels" onclick="document.getElementById('all').style.display='block'">More</span>
 <a id=all href="#all" style="display:none" onclick="document.title='all mail';return false">All Mail</a>
</div>
<main role=main>
 <div role=button aria-label="More email options" onclick="document.title='wrong-more'">⋮</div>
 <table>
  <tr class="zA zE" onclick="document.title='row1'"><td><span email="news@x.com" name="Newsletter">Newsletter</span></td>
   <td class="xT"><span class="bog">Weekly digest</span></td><td class="xW"><span title="Sun, Nov 8, 2026, 12:00 AM">Nov 8</span></td></tr>
  <tr class="zA" onclick="document.title='row2'"><td><span email="ana@x.com" name="Ana">Ana</span></td>
   <td class="xT"><span class="bog">Lunch on Friday</span></td><td class="xW"><span title="Sat, Nov 7, 2026, 8:50 AM">Nov 7</span></td></tr>
  <tr class="zA zE" onclick="document.title='row3'"><td><span email="bank@y.com" name="Bank">Bank</span></td>
   <td class="xT"><span class="bog">Your statement</span></td><td class="xW"><span title="Mon, Nov 9, 2026, 11:01 PM">11:01 PM</span></td></tr>
 </table>
 <div jsaction="click:xyz" onclick="document.title='showmore'"><span>Show more messages</span></div>
</main></body></html>"""


RESULTS = """<html><head><title>Search</title></head><body>
<nav><a href="https://nav.example/home">Home of the navigation menu</a></nav>
<main>
 <div><a href="https://a.example/1"><h3>First result about transformers</h3></a></div>
 <div><a href="https://a.example/2"><h3>Second result about attention</h3></a></div>
 <div><a href="https://a.example/2#again"><h3>Second result about attention</h3></a></div>
 <div><a href="https://a.example/3"><h3>Third result about long context</h3></a></div>
 <a href="https://a.example/x">short</a>
 <p>Pricing is simple. The pricing page lists PRICING for teams.</p>
 <textarea>pricing in a textarea is never marked</textarea>
</main>
<footer><a href="https://f.example/legal">Legal information and the footer links</a></footer>
</body></html>"""


REVIEWS = """<html><head><title>Product</title></head><body><main>
<div itemprop=review><p itemprop=reviewBody>Battery lasts two days.</p></div>
<div itemprop=review><p itemprop=reviewBody>Battery lasts two days.</p></div>
<div itemprop=review><p itemprop=reviewBody>Too heavy for travel.</p></div>
<div class=comment><p>A plain comment.</p></div>
</main></body></html>"""


FILES = """<html><head><title>Sheets</title></head><body><main>
<a href="https://docs.google.com/spreadsheets/d/1AAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit#gid=0" aria-label="Budget 2026">x</a>
<a href="https://docs.google.com/document/d/1BBBBBBBBBBBBBBBBBBBBBBBBBBBB/edit">Offer letter</a>
<a href="https://docs.google.com/spreadsheets/d/1AAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit">Budget 2026 again</a>
<div data-id="1CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"><span class="docs-homescreen-list-item-title-value">Team roster</span></div>
<div data-id="short">not a file</div>
<div data-id="1DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD" style="display:none">Hidden</div>
</main></body></html>"""

CALENDAR = """<html><body><main>
<div data-eventid="e1" style="width:300px;height:20px">10:00 Standup</div>
<div data-eventid="e2" style="width:300px;height:20px" onclick="document.getElementById('d').style.display='block'">15:00 Review</div>
<div id=d style="display:none"><a href="https://meet.google.com/abc-defg-hij">Join with Google Meet</a></div>
</main></body></html>"""


# Markup shapes seen LIVE on 2026-11-10 (tuning pass V9), reduced to what the functions read.
AMAZON = """<html><body><main>
<div data-component-type="s-search-result"><span class="puis-sponsored-label-text">Sponsored</span>
 <a href="/sspa/click?ad=1"><h2>Sponsored hub</h2></a></div>
<div data-component-type="s-search-result"><a href="/sspa/click?ad=2"><h2>Another ad</h2></a></div>
<div data-component-type="s-search-result"><a href="https://www.amazon.in/Portronics-Mport/dp/B09"><h2>Portronics Mport 31C USB C Hub</h2></a></div>
<div data-component-type="s-search-result"><a href="https://www.amazon.in/Anker-Hub/dp/B0A"><h2>Anker 7-in-1 USB C Hub</h2></a></div>
<div id="customerReviews"><li data-hook="review"><span data-hook="reviewText">Compact and reliable, works with my laptop.</span></li>
<li data-hook="review"><span data-hook="reviewText">Gets warm after an hour.</span></li></div>
</main></body></html>"""

YOUTUBE_WATCH = """<html><body>
<ytd-ad-slot-renderer><a id="video-title" href="https://www.youtube.com/watch?v=ad">An ad</a></ytd-ad-slot-renderer>
<ytd-ad-slot-renderer><yt-lockup-view-model><a href="https://www.youtube.com/watch?v=AD"><h3>App review, sponsored</h3></a></yt-lockup-view-model></ytd-ad-slot-renderer>
<div id="related">
 <yt-lockup-view-model><a href="https://www.youtube.com/watch?v=A1">0:59</a><h3><a href="https://www.youtube.com/watch?v=A1">How might LLMs store facts</a></h3></yt-lockup-view-model>
 <yt-lockup-view-model><a href="https://www.youtube.com/watch?v=B2">12:01</a><h3><a href="https://www.youtube.com/watch?v=B2">Quantum mechanics, seen</a></h3></yt-lockup-view-model>
</div>
<ytd-button-renderer style="display:none">Show transcript</ytd-button-renderer>
<button style="display:none" onclick="document.title='hidden-transcript'">Show transcript</button>
<button onclick="document.title='transcript'">Show transcript</button>
</body></html>"""

BING = """<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://github.com/x/kokoro-onnx">GitHub - kokoro-onnx</a></h2></li>
<li class="b_ans"><h2><a href="https://www.bing.com/news">News about kokoro</a></h2></li>
<li class="b_algo"><h2><a href="https://huggingface.co/kokoro">Kokoro on Hugging Face</a></h2></li>
</ol></body></html>"""

WIKI = """<html><body><div id="content">
<p>There is a page named "<a href="https://en.wikipedia.org/wiki/T">Transformer deep learning</a>". You can
<a href="https://en.wikipedia.org/wiki/Help:Draft">create a draft and submit it for review</a>.</p>
<ul><li><div class="mw-search-result-heading"><a href="https://en.wikipedia.org/wiki/Transformer_(deep_learning)">Transformer (deep learning)</a></div></li>
<li><div class="mw-search-result-heading"><a href="https://en.wikipedia.org/wiki/GPT">Generative pre-trained transformer</a></div></li></ul>
</div></body></html>"""

BLOG = """<html><body><main>
<p><a href="https://b.example/help">Read our very long help page first</a></p>
<h2><a href="https://b.example/one">First article about attention</a></h2>
<p><a href="https://b.example/footnote">a footnote link with long text</a></p>
<h2><a href="https://b.example/two">Second article about memory</a></h2>
<h3><a href="https://b.example/three">Third article about speed</a></h3>
</main></body></html>"""

WHATSAPP = """<html><head><title>WhatsApp</title></head><body>
<div id="pane-side" aria-label="Chat list"><div role="grid">
 <div role="row"><div role="gridcell"><span dir="auto" title="PR and Outreach Core Team 2026-27">PR and Outreach Core Team 2026-27</span>
   <div>10:21</div><span title="Ana: meeting at 6">Ana: meeting at 6</span>
   <span aria-label="5 unread messages">5</span></div></div>
 <div role="row"><div role="gridcell" onmousedown="document.title='open-rahul'"><span dir="auto" title="Rahul Sharma">Rahul Sharma</span>
   <div>Yesterday</div><span title="ok see you">ok see you</span></div></div>
</div></div>
<div id="main"><header><span dir="auto" title="PR and Outreach Core Team 2026-27">PR and Outreach Core Team 2026-27</span></header>
 <div role="application">
  <div role="row"><div class="message-in"><div class="copyable-text" data-pre-plain-text="[10:00, 11/10/2026] Ana: ">
    <span class="selectable-text"><span>Meeting at 6 in room 204</span></span></div></div></div>
  <div role="row"><div class="message-out"><div class="copyable-text" data-pre-plain-text="[10:02, 11/10/2026] Me: ">
    <span class="selectable-text"><span>I'll be there</span></span></div></div></div>
 </div>
 <footer><div contenteditable="true" role="textbox" aria-label="Type a message">draft text</div></footer>
</div></body></html>"""

CAPTCHA = """<html><body><form action="/sorry/validateCaptcha" id="captcha-form"><a href="https://g.example/why">Why did this happen?</a></form></body></html>"""


def function(name):
    start = SOURCE.index(f"function {name}(")
    depth, opened = 0, SOURCE.index("{", start)
    for index in range(opened, len(SOURCE)):
        depth += {"{": 1, "}": -1}.get(SOURCE[index], 0)
        if depth == 0:
            return SOURCE[start:index + 1]
    raise ValueError(name)


def driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.selenium_manager import SeleniumManager
    import shutil
    binary = next((p for p in ("/opt/google/chrome/chrome", shutil.which("google-chrome-stable") or "",
                               shutil.which("chromium") or "") if p and os.path.exists(p)), None)
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    args = ["--browser", "chrome", "--skip-driver-in-path"]
    if binary:
        options.binary_location = binary
        args += ["--browser-path", binary]
    paths = SeleniumManager().binary_paths(args)
    return webdriver.Chrome(options=options, service=Service(paths["driver_path"]))


def main():
    print_banner("BROWSER EXTENSION (real Chrome)")
    try:
        d = driver()
    except Exception as exc:
        check.skip("all checks", f"no usable Chrome: {type(exc).__name__}")
        return check.finish()
    try:
        d.get("data:text/html;base64," + base64.b64encode(PAGE.encode()).decode())
        call = lambda name, *a: d.execute_script(f"return ({function(name)}).apply(null, arguments)", *a)
        check.section("[1] Clicking by what a screen reader announces")
        r = call("pageClick", "turn off microphone", 1)
        check("Meet's 'Turn off microphone (ctrl + d)' matches 'turn off microphone'",
              r["clicked"] and d.title == "mic", r)
        r = call("pageClick", "share screen", 1)
        check("a div with role=button and a tooltip is clickable", r["clicked"] and d.title == "share", r)
        r = call("pageClick", "save", 2)
        check("'the second Save' clicks the second one", r["clicked"] and d.title == "save2", r)
        r = call("pageClick", "leave call", 1)
        check("an invisible button is never clicked", not r["clicked"] and d.title != "hidden", r)
        check("a missing label reports not found", not call("pageClick", "nonexistent", 1)["clicked"])
        check.section("[2] Typing")
        r = call("pageType", "email address", "a@b.com", False, False)
        check("a field is found by its <label>", r["typed"] and
              d.execute_script("return document.getElementById('e').value") == "a@b.com", r)
        check("a field is found by its placeholder", call("pageType", "search", "shoes", True, False)["typed"])
        check("a password field is refused", not call("pageType", "password", "x", False, False)["typed"])
        check("a card-number field is refused", not call("pageType", "card number", "4111", False, False)["typed"])
        r = call("pageType", "type a message", "hello", False, False)
        check("a contenteditable chat box takes text", r["typed"] and
              d.execute_script("return document.querySelector('[contenteditable]').innerText") == "hello", r)
        check.section("[3] Reading the page")
        check("scroll until text is found", call("pageScroll", "down", "pricing")["found"])
        check("find counts matches", call("pageFind", "save")["count"] == 2)
        names = [c["name"] for c in call("pageControls", 20)]
        check("controls are listed by name", "Share screen" in names and "Save" in names, names)
        check("page text has the title", call("pageText", 500)["title"])
        check("page.waitFor's probe: text present", call("pageHas", "pricing section", ""))
        check("page.waitFor's probe: text absent", not call("pageHas", "no such words here", ""))
        check("page.waitFor's probe: selector", call("pageHas", "", "input[placeholder]")
              and not call("pageHas", "", "#nope"))

        d.get("data:text/html;base64," + base64.b64encode(GMAIL.encode()).decode())
        check.section("[4] Gmail, as it really is")
        r = call("pageClick", "more", 1)
        check("'click on more' opens 'More labels' (the nav), not 'More email options'",
              r["clicked"] and d.execute_script("return getComputedStyle(document.getElementById('all')).display") == "block", r)
        r = call("pageClick", "all mails", 1)
        check("'all mails' (plural) clicks 'All Mail'", r["clicked"] and d.title == "all mail", r)
        r = call("pageClick", "sent", 1)
        check("'sent' clicks the Sent folder", r["clicked"] and d.title == "sent", r)
        r = call("pageClick", "load more", 1)
        check("'load more' finds 'Show more messages'", r["clicked"] and d.title == "showmore", r)
        r = call("gmailOpen", "first", 1, "")
        check("'open the first email' opens the top row", r["opened"] and d.title == "row1" and r["subject"] == "Weekly digest", r)
        r = call("gmailOpen", "latest", 1, "")
        check("'open the latest email' opens the NEWEST by date, not the top row (priority inbox)",
              r["opened"] and d.title == "row3" and r["from"] == "Bank", r)
        r = call("gmailOpen", "nth", 2, "")
        check("'open the second email'", r["opened"] and d.title == "row2", r)
        r = call("gmailOpen", "first", 1, "ana")
        check("'open the email from Ana'", r["opened"] and d.title == "row2", r)
        r = call("gmailOpen", "unread", 2, "")
        check("'open the second unread email'", r["opened"] and d.title == "row3", r)
        r = call("gmailOpen", "first", 1, "nobody")
        check("no match is reported, nothing opened", not r["opened"], r)

        check.section("[3] Part 2c — the Nth result (C3) and find with highlight (C4)")
        d.get("data:text/html;base64," + base64.b64encode(RESULTS.encode()).decode())
        r = call("pageResults", 0, "")
        titles = [x["title"] for x in r["results"]]
        check("the result list is the content links, not nav/footer, deduplicated by address",
              r["count"] == 3 and titles[0].startswith("First result") and
              not any("navigation" in t or "footer" in t for t in titles), r)
        r = call("pageResults", 3, "")
        check("'the third result' is the third", r["result"]["url"] == "https://a.example/3", r)
        r = call("pageResults", -1, "")
        check("'the last result' is the last", r["result"]["url"] == "https://a.example/3", r)
        check("past the end is null", call("pageResults", 9, "")["result"] is None)
        r = call("pageHighlight", "pricing", 0, False)
        marks = d.execute_script("return document.querySelectorAll('mark[data-kayra-find]').length")
        check("find highlights every match, case-insensitively, never inside a textarea",
              r["count"] == 3 and marks == 3 and r["index"] == 1, (r, marks))
        r = call("pageHighlight", "", 1, False)
        check("next match steps to 2 of 3", r["index"] == 2 and r["count"] == 3, r)
        r = call("pageHighlight", "", 2, False)
        check("...and wraps around", r["index"] == 1, r)
        call("pageHighlight", "", 0, True)
        text = d.execute_script("return document.querySelector('p').textContent")
        check("clear removes the marks and leaves the text exactly as it was",
              d.execute_script("return document.querySelectorAll('mark').length") == 0
              and text == "Pricing is simple. The pricing page lists PRICING for teams.", text)
        check("finding nothing marks nothing", call("pageHighlight", "zebra", 0, False)["count"] == 0)

        check.section("[6] Tuning pass V9 — markup seen live on Amazon, YouTube, Bing, Wikipedia")

        def at(html, host, name, *args, path="/"):
            # The page functions read `location`; run them as if served from `host`.
            d.get("data:text/html;base64," + base64.b64encode(html.encode()).decode())
            fake = {"hostname": host, "pathname": path, "search": "", "href": f"https://{host}{path}"}
            return d.execute_script(
                f"return (function(location){{ return ({function(name)}).apply(null, arguments[1]); }})"
                f"(arguments[0], arguments[1])", fake, list(args))

        r = at(AMAZON, "www.amazon.in", "pageResults", 1, "", path="/s")
        check("Amazon: sponsored listings are skipped — 'the first result' is the first REAL one",
              r["result"]["url"].endswith("/Portronics-Mport/dp/B09") and r["count"] == 2, r)
        r = at(AMAZON, "www.amazon.in", "pageComments", 12)
        check("Amazon: reviews are read from data-hook=reviewText (review-body is gone)",
              r["kind"] == "reviews" and r["comments"] == ["Compact and reliable, works with my laptop.",
                                                           "Gets warm after an hour."], r)
        r = at(YOUTUBE_WATCH, "www.youtube.com", "pageResults", 2, "", path="/watch")
        check("YouTube watch page: Up next lockups are the results, titled from <h3>, never the ad",
              r["count"] == 2 and r["result"]["title"] == "Quantum mechanics, seen", r)
        r = at(YOUTUBE_WATCH, "www.youtube.com", "pageTranscript", True, path="/watch")
        check("YouTube: 'Show transcript' presses the VISIBLE button, not a hidden duplicate",
              r["opened"] and d.title == "transcript", (r, d.title))
        r = at(BING, "www.bing.com", "pageResults", 2, "", path="/search")
        check("Bing: only organic results count (the news block is not 'the second result')",
              r["count"] == 2 and r["result"]["url"] == "https://huggingface.co/kokoro", r)
        r = at(WIKI, "en.wikipedia.org", "pageResults", 1, "", path="/w/index.php")
        check("Wikipedia search: the result headings, not 'create a draft'",
              r["site"] == "wikipedia" and r["result"]["title"] == "Transformer (deep learning)", r)
        r = at(BLOG, "b.example", "pageResults", 0, "")
        check("generic page: heading links win over help and footnote links",
              [x["url"] for x in r["results"]] == ["https://b.example/one", "https://b.example/two",
                                                   "https://b.example/three"], r)
        check.section("[7] Part 5 — WhatsApp Web (H0.4), by roles, titles and aria-labels")
        r = at(WHATSAPP, "web.whatsapp.com", "pageChatList", 60)
        check("the chat list: names in WhatsApp's order, preview, time and unread count",
              r["ready"] and [c["name"] for c in r["chats"]] == ["PR and Outreach Core Team 2026-27", "Rahul Sharma"]
              and r["chats"][0]["unread"] == 5 and r["chats"][0]["when"] == "10:21"
              and r["chats"][0]["preview"] == "Ana: meeting at 6" and r["chats"][1]["unread"] == 0, r)
        r = at(WHATSAPP, "web.whatsapp.com", "pageChatOpen", "Rahul Sharma")
        check("a chat is opened by its EXACT name with real mouse events", r["opened"] and d.title == "open-rahul", (r, d.title))
        check("a name not in the list is not clicked", not at(WHATSAPP, "web.whatsapp.com", "pageChatOpen", "Rahul")["opened"])
        r = at(WHATSAPP, "web.whatsapp.com", "pageChatMessages", 30)
        check("messages: the open chat, sender, time, date, text, and which are mine",
              r["chat"] == "PR and Outreach Core Team 2026-27" and r["messages"][0] ==
              {"time": "10:00", "date": "11/10/2026", "from": "Ana", "text": "Meeting at 6 in room 204", "out": False}
              and r["messages"][1]["out"] is True, r)
        r = at(WHATSAPP, "web.whatsapp.com", "pageChatComposer")
        check("the message box: its text (for the read-back before sending) and the open chat",
              r == {"found": True, "text": "draft text", "chat": "PR and Outreach Core Team 2026-27"}, r)
        r = at("<html><body>loading…</body></html>", "web.whatsapp.com", "pageChatList", 60)
        check("WhatsApp still loading is 'not ready', not an empty chat list", r == {"ready": False, "chats": []}, r)

        r = at(CAPTCHA, "www.google.com", "pageResults", 1, "", path="/sorry/index")
        check("a bot-check page is reported as blocked, never as results", r.get("blocked") and r["count"] == 0, r)

        check.section("[4] Part 3 — comments and reviews (C6), transcript off YouTube")
        d.get("data:text/html;base64," + base64.b64encode(REVIEWS.encode()).decode())
        r = call("pageComments", 12)
        check("marked-up reviews are read as REVIEWS, duplicates dropped, in page order",
              r["kind"] == "reviews" and r["comments"][:2] == ["Battery lasts two days.", "Too heavy for travel."], r)
        check("the max is honoured", len(call("pageComments", 1)["comments"]) == 1)
        check("the transcript reader says 'not YouTube' off YouTube",
              call("pageTranscript", True) == {"supported": False})

        check.section("[5] Part 4 — the file list (D3: names read in the page only) and Calendar")
        d.get("data:text/html;base64," + base64.b64encode(FILES.encode()).decode())
        r = call("pageDocList", 40)
        titles = [x["title"] for x in r]
        check("Docs/Sheets links and data-id rows are listed once each, hidden ones skipped",
              titles == ["Budget 2026", "Offer letter", "Team roster"], r)
        check("a link keeps its own address (no #gid fragment), a data-id row opens through Drive",
              r[0]["url"] == "https://docs.google.com/spreadsheets/d/1AAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit"
              and r[0]["kind"] == "sheet" and r[1]["kind"] == "doc"
              and r[2]["url"] == "https://drive.google.com/open?id=1CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC", r)
        d.get("data:text/html;base64," + base64.b64encode(CALENDAR.encode()).decode())
        r = call("pageCalendarNext", False)
        check("the next event is the first one listed; a HIDDEN Meet link is never taken",
              r["title"] == "10:00 Standup" and r["events"] == 2 and r["meet"] == "", r)
        d.execute_script("document.getElementById('d').style.display='block'")
        r = call("pageCalendarNext", False)
        check("...a visible one is", r["meet"] == "https://meet.google.com/abc-defg-hij", r)
    finally:
        d.quit()
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
