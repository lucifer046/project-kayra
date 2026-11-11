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

MAILAPP = """<html><body style="margin:0;overflow:hidden;height:100vh">
<div style="display:flex;height:100vh">
 <div id="side" style="width:25%;overflow-y:auto"><div style="height:3000px">labels</div></div>
 <div id="mail" style="flex:1;overflow-y:auto"><div style="height:5000px">a long mail</div></div>
</div></body></html>"""

SEARCHAPP = """<html><head><title>shop</title></head><body>
<header><button aria-label="Search" onclick="document.getElementById('box').style.display='block'">o</button>
<form role="search" onsubmit="document.title='submitted:'+this.q.value;return false">
 <input id="box" name="q" style="display:none"><button type="button" aria-label="Clear search" onclick="document.title='cleared'">x</button></form></header>
<input aria-label="Email address"><input type="password" aria-label="Search password">
</body></html>"""

WA_SEARCH = """<html><head><title>WhatsApp</title></head><body>
<div id="side"><div><div><button aria-label="Cancel search" onclick="document.title='cancelled'">x</button>
 <div id="s" contenteditable="true" role="textbox" aria-label="Search input textbox" style="min-height:20px;width:200px"></div></div></div></div>
<div id="main"><header><span title="Rahul Sharma">Rahul Sharma</span></header>
 <footer><div id="c" contenteditable="true" role="textbox" aria-label="Type a message" style="min-height:20px;width:200px"></div></footer></div>
</body></html>"""


WA_LIVE = """<html><head><title>WhatsApp</title></head><body>
<div id="side"><div><div id="s" contenteditable="true" role="textbox" aria-label="Search input textbox"
  style="min-height:20px;width:200px">old text</div></div>
 <div id="pane-side" aria-label="Chat list"><div role="grid" id="grid"></div></div></div>
<div id="main"><header><span title="Family">Family</span></header>
 <footer><div id="c" contenteditable="true" role="textbox" aria-label="Type a message" style="min-height:20px;width:200px"></div></footer></div>
<script>
 const all = ["Family", "Rahul Sharma", "General Panchayat", "Safe House"];
 const draw = (q) => {
   const rows = q ? all.filter((n) => n.toLowerCase().includes(q)) : all.slice(0, 2);   // only two are on screen
   grid.innerHTML = rows.length ? rows.map((n) => '<div role="row"><span title="' + n + '">' + n + '</span></div>').join("")
     : "<div>No chats, contacts or messages found</div>";
 };
 draw(""); let timer;
 s.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => draw(s.innerText.trim().toLowerCase()), 500); });
 document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const m = document.getElementById("main"); if (m) m.remove(); } });
</script></body></html>"""

CAPTCHA = """<html><body><form action="/sorry/validateCaptcha" id="captcha-form"><a href="https://g.example/why">Why did this happen?</a></form></body></html>"""


FORM = """<html><head><title>Application</title></head><body><main><form id=f onsubmit="document.title='sent';return false">
<label for=n>Full name *</label><input id=n name=fullname required>
<label>Email address <input type=email name=email autocomplete=email required></label>
<input type=tel aria-label="Mobile number" name=mobile>
<label for=fa>Father's name</label><input id=fa required>
<label for=dob>Date of birth</label><input id=dob type=date>
<label for=g>Gender</label><select id=g><option value="">Choose</option><option value=m>Male</option><option value=f>Female</option><option>Other</option></select>
<label for=c>Country</label><select id=c><option value="">Select</option><option>India</option><option>Nepal</option></select>
<fieldset><legend>Year of study</legend>
 <label><input type=radio name=year value=1> First year</label><label><input type=radio name=year value=2> Second year</label></fieldset>
<label for=p>Password</label><input id=p type=password>
<label for=cc>Card number</label><input id=cc>
<label for=city>City</label><input id=city value="Chennai">
<label><input type=checkbox id=agree> I agree to the terms</label>
<div role=listitem><div role=heading id=qh>Hostel</div>
 <div role=listbox aria-labelledby=qh onclick="document.getElementById('opts').style.display='block'">
  <div role=option aria-selected=true data-value="">Choose</div>
  <div id=opts style="display:none"><div role=option data-value="Sundarbans" onclick="this.parentNode.parentNode.setAttribute('data-picked','Sundarbans');document.getElementById('opts').style.display='none';event.stopPropagation()">Sundarbans</div>
  <div role=option data-value="Godavari" onclick="this.parentNode.parentNode.setAttribute('data-picked','Godavari');event.stopPropagation()">Godavari</div></div>
 </div></div>
<div><button type=button onclick="document.getElementById('cv').click()">Upload resume</button><input type=file id=cv style="display:none" accept=".pdf"></div>
<button type=submit>Submit</button>
</form></main></body></html>"""

SHOP_FORM = """<html><body><form onsubmit="document.title='ordered';return false"><input aria-label="Address" value="x">
<button type=submit>Place order</button></form></body></html>"""

VIDEO = """<html><head><title>Attention explained - YouTube</title></head><body>
<ytd-watch-metadata><h1>Attention explained</h1></ytd-watch-metadata>
<video id=v width=640 height=360></video>
<div class="ad-showing"></div>
<button class="ytp-skip-ad-button" onclick="document.title='skipped'">Skip</button>
<button class="ytp-subtitles-button" aria-pressed="false" onclick="this.setAttribute('aria-pressed', this.getAttribute('aria-pressed')==='true'?'false':'true')">CC</button>
<button class="ytp-next-button" onclick="document.title='next'">Next</button>
<like-button-view-model><button aria-pressed="false" aria-label="like this video along with 10 other people" onclick="this.setAttribute('aria-pressed','true')">Like</button></like-button-view-model>
<div id="subscribe-button"><button aria-label="Subscribe to Channel" onclick="this.setAttribute('aria-label','Unsubscribe from Channel');this.textContent='Subscribed'">Subscribe</button></div>
<input id=box>
</body></html>"""

PRODUCT_LD = """<html><head><title>Earbuds</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"BreadcrumbList"},
{"@type":"Product","name":"boAt Airdopes 141","brand":{"name":"boAt"},
 "offers":{"@type":"Offer","price":"1299.00","priceCurrency":"INR","availability":"https://schema.org/InStock"},
 "aggregateRating":{"ratingValue":"4.1","reviewCount":"2345"}}]}</script></head><body><h1>boAt Airdopes 141</h1></body></html>"""

AMAZON_PRODUCT = """<html><head><meta charset=utf-8></head><body><span id=productTitle> Sony WH-1000XM5 Wireless Headphones </span>
<div id=corePriceDisplay_desktop_feature_div><span class=a-price><span class=a-offscreen>₹26,990</span></span></div>
<span id=acrPopover title="4.4 out of 5 stars"></span><span id=acrCustomerReviewText>1,812 ratings</span>
<div id=availability><span> In stock </span></div></body></html>"""


CHATGPT = """<html><head><title>ChatGPT</title></head><body><main>
<div data-message-author-role="user">earlier question</div>
<div data-message-author-role="assistant"><p>Old answer.</p><button aria-label="Copy">copy</button></div>
<form><div id="prompt-textarea" contenteditable="true" aria-label="Message ChatGPT"></div>
<button data-testid="send-button" aria-label="Send prompt" disabled>up</button></form></main>
<script>
const box = document.getElementById('prompt-textarea'), btn = document.querySelector('[data-testid=send-button]');
box.addEventListener('input', () => { btn.disabled = !box.innerText.trim(); });
btn.addEventListener('click', () => {
  const q = box.innerText.trim(); box.innerHTML = ''; btn.disabled = true;
  const stop = document.createElement('button'); stop.setAttribute('aria-label', 'Stop streaming'); stop.textContent = 'stop';
  document.body.appendChild(stop);
  const a = document.createElement('div'); a.setAttribute('data-message-author-role', 'assistant');
  document.querySelector('main').appendChild(a);
  const words = ('The answer to ' + q + ' is forty two. <pre>print(42)</pre> Done.').split(' ');
  let i = 0;
  const t = setInterval(() => { a.innerHTML += (i ? ' ' : '') + words[i]; i++;
    if (i >= words.length) { clearInterval(t); setTimeout(() => stop.remove(), 300); } }, 60);
});
</script></body></html>"""

CHATGPT_OUT = """<html><head><title>ChatGPT</title></head><body><main><h1>Welcome back</h1>
<button>Log in</button><button>Sign up</button></main></body></html>"""

CLAUDE = """<html><head><title>Claude</title></head><body><main>
<div data-is-streaming="false"><div class="font-claude-response"><p>First reply.</p></div></div>
<div data-is-streaming="true"><div class="font-claude-response"><p>Still writ</p></div></div>
<fieldset><div class="ProseMirror" contenteditable="true" aria-label="Write your prompt to Claude"></div>
<button aria-label="Send message">send</button><button aria-label="Stop response">stop</button></fieldset>
</main></body></html>"""

WA_ATTACH = """<html><head><title>WhatsApp</title></head><body><div id=app><div id=side></div>
<div id=main><header><span title="Rahul Sharma">Rahul Sharma</span></header>
<footer><div role=button aria-label="Attach" onclick="setTimeout(openMenu, 200)"><span data-icon="plus">+</span></div>
<div contenteditable=true role=textbox aria-label="Type a message"></div>
<div role=button aria-label="Send" onclick="document.title='composer-send'"><span data-icon="send">&gt;</span></div></footer></div></div>
<script>
function openMenu() {
  if (document.getElementById('menu')) return;
  const m = document.createElement('ul'); m.id = 'menu';
  m.innerHTML = '<li><div role=button>Document<input type=file accept="*" multiple style="display:none" id=docin></div></li>' +
                '<li><div role=button>Photos &amp; videos<input type=file accept="image/*,video/mp4" style="display:none" id=medin></div></li>';
  document.getElementById('app').appendChild(m);
  document.getElementById('docin').addEventListener('click', () => { document.title = 'doc-chooser'; });
  document.getElementById('medin').addEventListener('click', () => { document.title = 'media-chooser'; });
}
function preview(name) {
  const p = document.createElement('div'); p.id = 'preview';
  p.innerHTML = '<span title="' + name + '">' + name + '</span><div role=button aria-label="Send" onclick="this.parentNode.remove(); document.title=\\'sent\\'"><span data-icon="send">&gt;</span></div>';
  document.getElementById('app').appendChild(p);
}
</script></body></html>"""

REDDIT = """<html><head><title>r/Python top</title></head><body><main>
<shreddit-post promoted post-title="Buy our course" score="1"></shreddit-post>
<shreddit-post post-title="Python 3.15 is out" score="4100" comment-count="380" subreddit-prefixed-name="r/Python"
  permalink="/r/Python/comments/1/python_315/"></shreddit-post>
<shreddit-post post-title="My first package" score="900" comment-count="12" subreddit-prefixed-name="r/Python"
  permalink="/r/Python/comments/2/first/"></shreddit-post>
</main></body></html>"""

GITHUB_NOTES = """<html><head><title>Notifications</title></head><body><main><ul>
<li class="notifications-list-item"><a class="markdown-title" href="/o/r/pull/7">Fix the parser</a><span class="f6">o/r #7</span></li>
<li class="notifications-list-item"><a class="markdown-title" href="/o/r/issues/9">Crash on start</a><span class="f6">o/r #9</span></li>
</ul></main></body></html>"""

LINKEDIN_NOTES = """<html><head><title>Notifications | LinkedIn</title></head><body><main>
<article class="nt-card"><div>Promoted</div><div>Try Premium for free this month and see more</div></article>
<article class="nt-card"><div>2h</div><div>Ana Rao viewed your profile and 3 others recently</div></article>
<article class="nt-card"><div>1d</div><div>Ravi posted: we are hiring interns for the summer</div></article>
</main></body></html>"""


AGENT_PAGE = """<html><head><title>Flights</title></head><body>
<nav><a href="#home">Home</a></nav>
<main><h1>Find flights</h1>
<label for=from>From</label><input id=from value="Chennai">
<label for=to>To</label><input id=to>
<select id=cls aria-label="Class"><option>Economy</option><option>Business</option></select>
<label><input type=checkbox id=direct> Direct flights only</label>
<input type=password aria-label="Password">
<button id=go onclick="document.title='searched:' + document.getElementById('to').value">Search flights</button>
<button aria-label="Close banner"><svg></svg></button>
<div style="display:none"><button>Hidden thing</button></div>
<p>Your login code is 482913. Do not share it.</p>
<div id=list style="height:200px;overflow:auto"><div style="height:2000px">long list</div></div>
</main></body></html>"""


WA_MENUS = """<html><head><meta charset=utf-8><title>WhatsApp</title></head><body><div id=app>
<div id=pane-side aria-label="Chat list">
 <div role=listitem><span title="Rahul Sharma">Rahul Sharma</span>
  <div role=button aria-label="Open chat context menu" style="width:20px;height:20px" onclick="menu(['Archive chat','Mute notifications','Pin chat','Mark as unread'])">v</div></div>
</div>
<div id=main><header><span title="Rahul Sharma">Rahul Sharma</span>
 <div role=button aria-label="Video call" onclick="document.title='video-call'">V</div>
 <div role=button aria-label="Voice call" onclick="document.title='voice-call'">A</div>
 <div role=button aria-label="Menu" onclick="menu(['Contact info','Mute notifications','Close chat'])">:</div></header>
 <div class="message-in" role=row><div data-pre-plain-text="[10:00, 11/11/2026] Rahul Sharma: "><span class="selectable-text">are you coming tonight?</span></div>
  <div role=button aria-label="Context menu" style="width:20px;height:20px" onclick="menu(['Reply','React','Forward','Star'])">v</div>
  <div role=button aria-label="React" style="width:20px;height:20px" onclick="reactions()">:)</div></div>
 <div class="message-out" role=row><div data-pre-plain-text="[10:01, 11/11/2026] You: "><span class="selectable-text">mine</span></div></div>
 <footer><div contenteditable=true role=textbox aria-label="Type a message"></div></footer></div></div>
<script>
function menu(items) {
  const old = document.getElementById('menu'); if (old) old.remove();
  const m = document.createElement('div'); m.id = 'menu'; m.setAttribute('role', 'application');
  m.innerHTML = '<ul>' + items.map((i) => '<li><div aria-label="' + i + '">' + i + '</div></li>').join('') + '</ul>';
  document.body.appendChild(m);
  m.querySelectorAll('li').forEach((li) => li.addEventListener('click', () => {
    document.title = 'picked:' + li.innerText; m.remove();
    if (li.innerText === 'Mute notifications') dialog();
    if (li.innerText === 'Forward') { const b = document.createElement('div'); b.setAttribute('role', 'button');
      b.setAttribute('aria-label', 'Forward'); b.textContent = 'fwd'; b.onclick = forwardDialog; document.getElementById('main').appendChild(b); }
  }));
}
function dialog() {
  const d = document.createElement('div'); d.setAttribute('role', 'dialog');
  d.innerHTML = '<label><input type=radio name=t> 8 hours</label><label><input type=radio name=t id=wk> 1 week</label>' +
                '<label><input type=radio name=t> Always</label><button>Cancel</button><button>Mute</button>';
  d.querySelectorAll('button')[1].onclick = () => { document.title = 'muted:' + (document.getElementById('wk').checked ? 'week' : 'other'); d.remove(); };
  document.body.appendChild(d);
}
function reactions() {
  const d = document.createElement('div'); d.setAttribute('role', 'dialog');
  d.innerHTML = '<div role=button aria-label="👍">👍</div><div role=button aria-label="❤️">❤️</div>';
  d.querySelectorAll('[role=button]').forEach((b) => b.onclick = () => { document.title = 'reacted:' + b.textContent; d.remove(); });
  document.body.appendChild(d);
}
function forwardDialog() {
  const d = document.createElement('div'); d.setAttribute('role', 'dialog');
  d.innerHTML = '<input aria-label="Search"><div role=listitem><span title="Mom">Mom</span></div>' +
                '<div role=listitem><span title="Rahul Verma">Rahul Verma</span></div><div role=button aria-label="Send">send</div>';
  d.querySelectorAll('[role=listitem]').forEach((r) => r.onclick = () => { document.title = 'to:' + r.innerText; });
  d.querySelector('[aria-label=Send]').onclick = () => { document.title += '|sent'; d.remove(); };
  document.body.appendChild(d);
}
</script></body></html>"""

MEET_CALL = """<html><head><meta charset=utf-8><title>Meet - abc-defg-hij</title></head><body>
<div role=list aria-label="Participants"><div role=listitem aria-label="Ana Rao">Ana Rao</div><div role=listitem aria-label="Ravi Kumar">Ravi Kumar</div></div>
<button aria-label="Send a reaction" onclick="document.getElementById('emo').style.display='block'">R</button>
<div id=emo style="display:none"><button aria-label="💖" onclick="document.title='reaction:heart'">h</button><button aria-label="👍">t</button></div>
<button aria-label="More options for Ana Rao" onclick="document.getElementById('pinmenu').style.display='block'">...</button>
<div id=pinmenu style="display:none"><div role=menuitem onclick="document.title='pinned'">Pin to screen</div></div>
<button aria-label="Chat with everyone" onclick="document.getElementById('chat').style.display='block'">C</button>
<div id=chat style="display:none"><textarea aria-label="Send a message"></textarea>
<button aria-label="Send a message" onclick="document.title='said:' + document.querySelector('textarea').value">S</button></div>
</body></html>"""


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

        check.section("[8] V11 — scrolling the box the user looks at, one search action, closing it")

        def aat(html, host, name, *args):
            # An async page function, run as if served from `host`; the promise is awaited.
            d.get("data:text/html;base64," + base64.b64encode(html.encode()).decode())
            return d.execute_script(
                f"return (function(location){{ return (async {function(name)}).apply(null, arguments[1]); }})"
                f"(arguments[0], arguments[1])", {"hostname": host, "pathname": "/"}, list(args))

        top = lambda el: d.execute_script(f"return document.getElementById('{el}').scrollTop")
        r = at(MAILAPP, "mail.google.com", "pageScroll", "down", "", "", 1)
        check("a web app whose window never scrolls: the open MAIL scrolls, and it is reported as moved",
              r["moved"] and r["inner"] and top("mail") > 100 and top("side") == 0, (r, top("mail")))
        r = d.execute_script(f"return ({function('pageScroll')})('bottom', '', '', 1)")
        check("'to the bottom' reaches the end of that box", r["moved"] and r["atEnd"] and top("mail") > 4000, r)
        r = d.execute_script(f"return ({function('pageScroll')})('bottom', '', '', 1)")
        check("already at the end: NOT moved (so Kayra never says 'Done' for nothing)",
              r["moved"] is False and r["canScroll"] is True, r)
        r = d.execute_script(f"return ({function('pageScroll')})('down', '', 'side', 0.4)")
        check("where = side scrolls the list on the left, by the smaller amount",
              r["moved"] and 0 < top("side") < 400, (r, top("side")))
        r = d.execute_script(f"return ({function('pageScroll')})('top', '', '', 1)")
        check("'to the top' goes back", r["moved"] and top("mail") == 0, r)
        r = at(PAGE, "x.example", "pageScroll", "down", "", "", 1)
        check("an ordinary page still scrolls the window", r["moved"] and r["inner"] is False
              and d.execute_script("return window.scrollY") > 100, r)
        r = at("<html><body><p>short</p></body></html>", "x.example", "pageScroll", "down", "", "", 1)
        check("nothing to scroll is said so (Kayra then uses the real keys)", r["moved"] is False and r["canScroll"] is False, r)

        r = aat(SEARCHAPP, "shop.example", "pageSearch", "council details", True)
        check("search: a box hidden behind a Search button is opened, typed into and submitted",
              r["searched"] and r["opened"] and d.title == "submitted:council details", (r, d.title))
        r = d.execute_script(f"return (async {function('pageSearchClose')})()")
        check("close search: the box is emptied and its own Clear button pressed",
              r["closed"] and r["had"] == "council details" and d.title == "cleared"
              and d.execute_script("return document.getElementById('box').value") == "", (r, d.title))
        r = aat("<html><body><input type=password aria-label='Search password'><input aria-label='Email'></body></html>",
                "x.example", "pageSearch", "secret", True)
        check("search: a password field is never the search box", r["searched"] is False, r)
        r = aat(WA_SEARCH, "web.whatsapp.com", "pageSearch", "rahul", False)
        check("WhatsApp: its search box (a contenteditable) takes the text, without Enter",
              r["searched"] and d.execute_script("return document.getElementById('s').innerText").strip() == "rahul", r)
        r = d.execute_script("return (function(location){ return (async " + function("pageSearchClose") +
                             ")(); })({hostname: 'web.whatsapp.com'})")
        check("WhatsApp: closing the search empties it, presses Cancel, and hands the focus back to the open chat's message box",
              r["closed"] and r["had"] == "rahul" and d.title == "cancelled"
              and d.execute_script("return document.getElementById('s').innerText").strip() == ""
              and d.execute_script("return document.activeElement.id") == "c", (r, d.title))
        r = d.execute_script(f"return ({function('pageType')})('search', '', false, true)")
        check("typing an EMPTY text with clear really empties a contenteditable box", r["typed"], r)

        check.section("[9] V12 — WhatsApp's search is WAITED for; the old text is replaced; a chat can be closed")
        r = aat(WA_LIVE, "web.whatsapp.com", "pageChatSearch", "general panchayat", 60, 4000)
        check("a chat that is NOT on screen: the search is typed and its results waited for (they arrive 0.5 s later)",
              r["searched"] and r["typed"] and [c["name"] for c in r["chats"]] == ["General Panchayat"], r)
        check("...the text that was in the search box was REPLACED, not added to", r["text"] == "general panchayat", r["text"])
        r = d.execute_script(f"return (async {function('pageChatSearch')})('zzzz', 60, 4000)")
        check("a search with no hit is reported as empty (not as the old list)", r["empty"] and r["chats"] == [] and r["text"] == "zzzz", r)
        r = d.execute_script(f"return (async {function('pageSearch')})('safe', false)")
        check("the generic search replaces the old text too and reads it back", r["searched"] and r["value"] == "safe" and r["exact"], r)
        r = d.execute_script(f"return (async {function('pageChatSearch')})('', 60, 4000)")
        check("an empty search empties the box and waits for the normal list to come back",
              r["text"] == "" and [c["name"] for c in r["chats"]] == ["Family", "Rahul Sharma"], r)
        r = d.execute_script(f"return (async {function('pageChatClose')})()")
        check("'close the chat': Escape, and the conversation is gone", r["closed"] and not r["open"], r)
        r = d.execute_script(f"return (async {function('pageChatClose')})()")
        check("...with no chat open it says so", r == {"closed": False, "open": False}, r)

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

        check.section("[10] Part 6 — forms (D1–D5), the video (F1), the product (F3)")
        form = lambda op, args=None: d.execute_script(f"return (async {function('pageForm')})(arguments[0], arguments[1])", op, args or {})
        d.get("data:text/html;base64," + base64.b64encode(FORM.encode()).decode())
        listed = form("list")["fields"]
        by = {f["label"]: f for f in listed}
        check("every field is listed with its own label (label, wrapping label, aria-label, heading)",
              {"Full name *", "Email address", "Mobile number", "Father's name", "Date of birth", "Gender", "Country",
               "Year of study", "City", "I agree to the terms", "Hostel"} <= set(by), sorted(by))
        check("password and card fields are listed as sensitive, nothing else is",
              {f["label"] for f in listed if f["sensitive"]} == {"Password", "Card number"},
              [(f["label"], f["sensitive"]) for f in listed])
        check("types, options, autocomplete, required and already-filled are read",
              by["Email address"]["type"] == "email" and by["Email address"]["autocomplete"] == "email"
              and by["Gender"]["options"] == ["Male", "Female", "Other"] and by["Year of study"]["type"] == "radio"
              and by["Year of study"]["options"] == ["First year", "Second year"] and by["City"]["filled"]
              and by["Full name *"]["required"] and by["Hostel"]["type"] == "listbox"
              and by["Hostel"]["options"] == ["Sundarbans", "Godavari"], by.get("Hostel"))
        check("the hidden file input is NOT a fillable field", all(f["type"] != "file" for f in listed))
        items = [{"i": by["Full name *"]["i"], "value": "Ana Rao"}, {"i": by["Email address"]["i"], "value": "ana@x.com"},
                 {"i": by["Date of birth"]["i"], "value": "2003-05-04"}, {"i": by["Gender"]["i"], "value": "Female"},
                 {"i": by["Year of study"]["i"], "value": "second year"}, {"i": by["Hostel"]["i"], "value": "Sundarbans"},
                 {"i": by["Password"]["i"], "value": "hunter2"}]
        r = form("fill", {"items": items})
        values = d.execute_script("return [document.getElementById('n').value, document.querySelector('[name=email]').value,"
                                  "document.getElementById('dob').value, document.getElementById('g').value,"
                                  "document.querySelector('[name=year]:checked') && document.querySelector('[name=year]:checked').value,"
                                  "document.querySelector('[role=listbox]').getAttribute('data-picked'), document.getElementById('p').value]")
        check("fill: text, email, date, select, radio and a Google-Forms drop-down all take their values",
              values == ["Ana Rao", "ana@x.com", "2003-05-04", "f", "2", "Sundarbans", ""], (values, r))
        check("...and the password field is refused in the page itself",
              [f["reason"] for f in r["failed"]] == ["sensitive"] and len(r["filled"]) == 6, r)
        r = form("choose", {"field": "country", "option": "india", "kind": "select"})
        check("choose: 'India' in the country drop-down", r["done"] and d.execute_script("return document.getElementById('c').value") == "India", r)
        r = form("choose", {"field": "country", "option": "mars", "kind": "select"})
        check("choose: a missing option lists the real ones", not r["done"] and r["options"] == ["India", "Nepal"], r)
        r = form("choose", {"field": "", "option": "Nepal", "kind": "select"})
        check("choose: no field said — the drop-down that HAS that option", r["done"] and r["field"] == "Country"
              and d.execute_script("return document.getElementById('c').value") == "Nepal", r)
        r = form("choose", {"field": "i agree", "kind": "check", "state": True})
        check("tick: by its label", r["done"] and r["now"] and d.execute_script("return document.getElementById('agree').checked"), r)
        r = form("choose", {"field": "i agree", "kind": "check", "state": True})
        check("...ticking a ticked box leaves it ticked", r["was"] and r["now"] and d.execute_script("return document.getElementById('agree').checked"), r)
        r = form("choose", {"field": "i agree", "kind": "check", "state": False})
        check("untick", not r["now"] and not d.execute_script("return document.getElementById('agree').checked"), r)
        r = form("choose", {"field": "first year", "kind": "check", "state": True})
        check("'tick first year' picks that radio option", r["done"] and r["group"] == "Year of study"
              and d.execute_script("return document.querySelector('[name=year]:checked').value") == "1", r)
        r = form("choose", {"field": "date of birth", "option": "2001-01-31", "kind": "date"})
        check("a date field", r["done"] and d.execute_script("return document.getElementById('dob').value") == "2001-01-31", r)
        r = form("file", {"field": "resume"})
        check("the hidden file input behind 'Upload resume' is found", r["found"] and "resume" in r["label"].lower()
              and r["accept"] == ".pdf", r)
        r = form("file", {"arm": True})
        check("arming installs the key hook without opening anything", r["armed"] and d.title == "Application", r)
        d.execute_script("document.getElementById('cv').addEventListener('click', () => { document.title = 'chooser'; });"
                         "window.__typed = 0; document.addEventListener('keypress', () => window.__typed++);")
        d.execute_script("document.getElementById('box') || document.body.focus()")
        from selenium.webdriver.common.action_chains import ActionChains
        d.execute_script("window.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true}))")
        check("...a SCRIPTED Space does nothing (the hook wants a real key)", d.title == "Application")
        ActionChains(d).send_keys(" ").perform()
        check("...a REAL Space opens the file input's chooser, and the Space itself is swallowed",
              d.title == "chooser" and d.execute_script("return window.__typed") == 0, d.title)
        d.execute_script("document.title = 'Application'")
        ActionChains(d).send_keys(" ").perform()
        check("...the hook fires once only", d.title == "Application", d.title)
        r = form("submit", {"dry": True})
        check("submit (dry): the form's own Submit button", r["label"] == "Submit" and d.title == "Application", r)
        r = form("submit")
        check("submit with a required field empty: NOT pressed, and the field is named",
              not r["submitted"] and r["reason"] == "invalid" and r["missing"] == ["Father's name"] and d.title != "sent", r)
        d.execute_script("document.getElementById('fa').value = 'R. Rao'")
        r = form("submit")
        check("...once it is filled, submit presses it", r["submitted"] and d.title == "sent", r)
        d.get("data:text/html;base64," + base64.b64encode(SHOP_FORM.encode()).decode())
        r = form("submit")
        check("D1: a form whose button is 'Place order' is never submitted", not r["submitted"] and r["reason"] == "payment"
              and d.title != "ordered", r)

        video = lambda op, value=None: d.execute_script(
            f"return (function(location){{ return (async {function('pageVideo')})(arguments[1], arguments[2]); }})"
            f"(arguments[0], arguments[1], arguments[2])", {"hostname": "www.youtube.com"}, op, value)
        d.get("data:text/html;base64," + base64.b64encode(VIDEO.encode()).decode())
        r = video("state")
        check("F1: the video, its title and the ad are seen", r["video"] and r["title"] == "Attention explained" and r["ad"], r)
        check("F1: speed is set on the <video> (clamped to 0.25–4)", video("rate", 1.5)["rate"] == 1.5 and video("rate", 9)["rate"] == 4)
        check("F1: mute", video("mute", True)["muted"])
        r = video("skipAd")
        check("F1: Skip ad presses YouTube's skip button", r["skipped"] and d.title == "skipped", r)
        r = video("captions", True)
        check("F1: captions on through the CC button, and asking again leaves them on",
              r["captions"] and video("captions", True)["captions"], r)
        check("F1: captions off", video("captions", False)["captions"] is False)
        check("F1: next video", video("next")["next"] and d.title == "next")
        r = video("like", True)
        check("F1: like presses the like button once", r["liked"] and not r["was"] and video("like", True)["was"], r)
        r = video("subscribe")
        check("F1: subscribe, and 'already subscribed' after", r["subscribed"] and not r.get("already")
              and video("subscribe").get("already"), r)
        d.execute_script("document.getElementById('box').focus()")
        video("blur")
        check("F1: before a key shortcut the page lets go of the text box", d.execute_script("return document.activeElement.id") != "box")
        d.get("data:text/html;base64," + base64.b64encode(PAGE.encode()).decode())
        check("F1: a page without a video says so", video("rate", 2) == {"video": False})

        d.get("data:text/html;base64," + base64.b64encode(PRODUCT_LD.encode()).decode())
        r = call("pageProduct")
        check("F3: the product from schema.org data (inside @graph)", r["found"] and r["name"] == "boAt Airdopes 141"
              and r["price"] == "1299.00" and r["currency"] == "INR" and r["rating"] == "4.1" and r["ratings"] == "2345"
              and r["availability"] == "InStock", r)
        r = at(AMAZON_PRODUCT, "www.amazon.in", "pageProduct", path="/dp/B0")
        check("F3: Amazon's own markup when there is no structured data", r["found"]
              and r["name"] == "Sony WH-1000XM5 Wireless Headphones" and r["price"] == "₹26,990"
              and r["rating"] == "4.4" and r["ratings"] == "1,812" and r["availability"] == "In stock", r)
        d.get("data:text/html;base64," + base64.b64encode(PAGE.encode()).decode())
        check("F3: a page that is not a product says so", call("pageProduct")["found"] is False)

        check.section("[11] Part 6b — ChatGPT / Claude pages, WhatsApp's attach menu, Reddit / GitHub / LinkedIn lists")
        ai = lambda html, host, op, args=None: (d.get("data:text/html;base64," + base64.b64encode(html.encode()).decode()),
                                                d.execute_script(
            f"return (function(location){{ return (async {function('pageAiChat')})(arguments[1], arguments[2]); }})"
            f"(arguments[0], arguments[1], arguments[2])", {"hostname": host}, op, args or {}))[1]
        r = ai(CHATGPT, "chatgpt.com", "state")
        check("F4: ChatGPT's prompt box and its earlier answer are found", r["ready"] and r["site"] == "chatgpt"
              and r["count"] == 1 and r["last"] == "Old answer." and not r["busy"], r)
        run_ai = lambda op, args=None: d.execute_script(
            f"return (function(location){{ return (async {function('pageAiChat')})(arguments[1], arguments[2]); }})"
            f"(arguments[0], arguments[1], arguments[2])", {"hostname": "chatgpt.com"}, op, args or {})
        r = run_ai("ask", {"text": "what is life"})
        check("F4: the question is typed, the send button waited for until it is enabled, and pressed",
              r["sent"] and r["pressed"] == "button" and r["before"] == 1, r)
        r = run_ai("wait", {"after": 1, "waitMs": 10000, "settleMs": 500})
        check("F4: the answer is waited for until it stops growing AND the Stop button is gone",
              r["done"] and not r["busy"] and r["count"] == 2 and
              r["last"].startswith("The answer to what is life is forty two.") and r["last"].endswith("Done."), r)
        check("F4: code in an answer is said to be on screen, never read out", "[code on screen]" in r["last"]
              and "print(42)" not in r["last"], r["last"])
        r = ai(CHATGPT_OUT, "chatgpt.com", "ask", {"text": "hi"})
        check("F4: a signed-out page is reported, nothing typed", not r["sent"] and r["reason"] == "signed out"
              and r["signedOut"], r)
        r = ai(CLAUDE, "claude.ai", "state")
        check("F4: Claude's editor and answers are found, and a streaming answer counts as busy",
              r["ready"] and r["site"] == "claude" and r["count"] == 2 and r["busy"] and r["last"] == "Still writ", r)

        attach = lambda op, args=None: d.execute_script(
            f"return (async {function('pageChatAttach')}).apply(null, arguments)", op, args or {})
        d.get("data:text/html;base64," + base64.b64encode(WA_ATTACH.encode()).decode())
        r = attach("state")
        check("F5: the composer's own Send is NOT a file preview", r["chat"] == "Rahul Sharma" and not r["preview"], r)
        r = attach("arm", {"kind": "document"})
        check("F5: arming opens the Attach menu, WAITS for its inputs, and picks the document input",
              r["found"] and r["armed"] and r["kind"] == "document" and r["chat"] == "Rahul Sharma", r)
        from selenium.webdriver.common.action_chains import ActionChains
        d.execute_script("window.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true}))")
        check("F5: a scripted Space opens nothing", d.title == "WhatsApp", d.title)
        ActionChains(d).send_keys(" ").perform()
        check("F5: a REAL Space opens the document chooser", d.title == "doc-chooser", d.title)
        d.execute_script("document.title = 'WhatsApp'")
        r = attach("arm", {"kind": "media"})
        ActionChains(d).send_keys(" ").perform()
        check("F5: a photo goes through the Photos & videos input", r["kind"] == "media" and d.title == "media-chooser", (r, d.title))
        d.execute_script("document.title = 'WhatsApp'; preview('Ana-Resume-2026.pdf')")
        r = attach("state")
        check("F5: the preview and its file name are read", r["preview"] and r["files"] == ["Ana-Resume-2026.pdf"], r)
        r = attach("send")
        check("F5: send presses the PREVIEW's Send (not the composer's) and sees the preview close",
              r["sent"] and d.title == "sent", (r, d.title))

        r = at(REDDIT, "www.reddit.com", "pageSocial", "posts", 5, path="/r/Python/top/")
        check("F6: Reddit posts from the post elements' own facts; promoted posts skipped",
              [i["title"] for i in r["items"]] == ["Python 3.15 is out", "My first package"]
              and r["items"][0]["meta"] == "r/Python, 4100 points, 380 comments"
              and r["items"][0]["url"] == "https://www.reddit.com/r/Python/comments/1/python_315/", r["items"][:1])
        r = at(GITHUB_NOTES, "github.com", "pageSocial", "notifications", 5, path="/notifications")
        check("F6: GitHub notifications with their repository", [(i["title"], i["meta"]) for i in r["items"]] ==
              [("Fix the parser", "o/r #7"), ("Crash on start", "o/r #9")], r["items"])
        r = at(LINKEDIN_NOTES, "www.linkedin.com", "pageSocial", "notifications", 5, path="/notifications/")
        check("F6: LinkedIn notifications, the promoted card skipped", [i["title"] for i in r["items"]] ==
              ["Ana Rao viewed your profile and 3 others recently", "Ravi posted: we are hiring interns for the summer"],
              r["items"])
        r = at(RESULTS, "example.com", "pageSocial", "posts", 5)
        check("F6: any other page falls back to the articles / list items in <main>", isinstance(r["items"], list), r)

        check.section("[12] Part 7 — the page agent's eyes (page.snapshot) and hands (page.act)")
        d.get("data:text/html;base64," + base64.b64encode(AGENT_PAGE.encode()).decode())
        snap = call("pageSnapshot", 120, 1500)
        by = {i["name"]: i for i in snap["items"]}
        check("H1: every visible control with its role and accessible name; hidden ones left out",
              {"From", "To", "Class", "Direct flights only", "Search flights", "Close banner", "Home"} <= set(by)
              and "Hidden thing" not in by and by["Search flights"]["role"] == "button" and by["To"]["role"] == "textbox"
              and by["Class"]["options"] == ["Economy", "Business"], sorted(by))
        check("H1: values are read; a password field is flagged and its value never returned",
              by["From"]["value"] == "Chennai" and by["Password"]["sensitive"] and by["Password"].get("value") in (None, "(hidden)"),
              (by["From"], by["Password"]))
        check("H1: a code in the page text is masked", "482913" not in snap["text"] and "••••" in snap["text"], snap["text"][:200])
        again = call("pageSnapshot", 120, 1500)
        check("H1: ids are STABLE across snapshots", {i["name"]: i["id"] for i in again["items"]} ==
              {i["name"]: i["id"] for i in snap["items"]})
        act = lambda op, args: d.execute_script(f"return (async {function('pageAct')}).apply(null, arguments)", op, args)
        r = act("type", {"id": by["To"]["id"], "text": "Goa"})
        check("H1: type into a field by id", r["ok"] and d.execute_script("return document.getElementById('to').value") == "Goa", r)
        r = act("type", {"id": by["Password"]["id"], "text": "x"})
        check("H1: the page refuses a password field too", not r["ok"] and r["reason"] == "secret field", r)
        r = act("select", {"id": by["Class"]["id"], "text": "business"})
        check("H1: choose a drop-down option", r["ok"] and d.execute_script("return document.getElementById('cls').value") == "Business", r)
        r = act("check", {"id": by["Direct flights only"]["id"], "state": True})
        check("H1: tick a box", r["checked"] and d.execute_script("return document.getElementById('direct').checked"), r)
        r = act("click", {"id": by["Search flights"]["id"]})
        check("H1: click by id", r["ok"] and d.title == "searched:Goa", d.title)
        check("H1: an id that does not exist is reported", act("click", {"id": 9999})["reason"] == "no such element")
        check("H1: scroll moves the page or the long box in it", act("scroll", {"direction": "down"})["ok"] is True)

        check.section("[13] Parts 8–9 — WhatsApp menus, reactions, forwarding, calls; Meet in-call")
        wa = lambda op, args=None: d.execute_script(f"return (async {function('pageWa')}).apply(null, arguments)", op, args or {})
        d.get("data:text/html;base64," + base64.b64encode(WA_MENUS.encode()).decode())
        r = wa("last")
        check("H3: the newest message FROM THEM (not mine)", r["found"] and r["text"] == "are you coming tonight?"
              and r["from"] == "Rahul Sharma" and not r["out"], r)
        r = wa("menu", {"scope": "chat", "name": "Rahul Sharma", "labels": ["Mute notifications"], "choose": "1 week", "press": ["Mute"]})
        check("H3: the chat row's menu -> Mute notifications -> 1 week -> Mute", r["ok"] and d.title == "muted:week", (r, d.title))
        r = wa("menu", {"scope": "chat", "name": "Rahul Sharma", "labels": ["Unpin chat"]})
        check("H3: an item the menu doesn't offer lists what it does (so 'already pinned' can be said)",
              not r["ok"] and "Pin chat" in r["items"], r)
        d.execute_script("const m = document.getElementById('menu'); if (m) m.remove();")
        r = wa("menu", {"scope": "message", "labels": ["Star", "Star message"]})
        check("H3: the newest message's own menu -> Star", r["ok"] and d.title == "picked:Star", (r, d.title))
        r = wa("react", {"emoji": "❤️"})
        check("H3: a reaction on the newest message", r["ok"] and d.title == "reacted:❤️", (r, d.title))
        wa("menu", {"scope": "message", "labels": ["Forward"]})
        r = wa("forward", {"name": "Rahul Verma"})
        check("H3: forward: the Forward bar, the picker, the RIGHT chat", r["ok"] and d.title == "to:Rahul Verma", (r, d.title))
        r = wa("forwardSend")
        check("H3: ...and the picker's own Send", r["ok"] and d.title.endswith("|sent"), d.title)
        r = wa("call", {"kind": "video"})
        check("H3: the header's Video call button", r["ok"] and d.title == "video-call", d.title)
        mt = lambda op, args=None: d.execute_script(f"return (async {function('pageMeet')}).apply(null, arguments)", op, args or {})
        d.get("data:text/html;base64," + base64.b64encode(MEET_CALL.encode()).decode())
        r = mt("people")
        check("M3: who's in the call, from the People list", r["people"] == ["Ana Rao", "Ravi Kumar"], r)
        r = mt("react", {"emoji": "💖"})
        check("M3: Send a reaction -> the emoji", r["ok"] and d.title == "reaction:heart", (r, d.title))
        r = mt("pin", {"name": "Ana Rao"})
        check("M3: More options for <name> -> Pin", r["ok"] and d.title == "pinned", (r, d.title))
        r = mt("chat", {"text": "running late"})
        check("M3: the in-call chat: opened, typed, sent", r["ok"] and d.title == "said:running late", (r, d.title))
    finally:
        d.quit()
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
