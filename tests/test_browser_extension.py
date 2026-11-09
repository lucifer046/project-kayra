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
    finally:
        d.quit()
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
