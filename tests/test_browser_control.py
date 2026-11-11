# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_browser_control.py                          │
# │   Round 5 browser control: open anything, anywhere, the way you say it │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_browser_control.py — unit tier, hardware-free, no network.

    .venv/bin/python tests/test_browser_control.py

Covers docs/roadmap/BROWSER_CONTROL_PLAN.md as it is built, part by part:

  Part 1  A1–A6 (open anything: the site registry, app → web app → website → web result,
          where to open it, one flow for "new tab and open X in that tab", your own lists),
          G3 (page.waitFor), H0.1 (web-app windows are browser surfaces), the extension's
          self-update.

Every web action runs against a FAKE extension and a recording process runner; the web
lookup used for unknown names is replaced, so nothing here touches the network, the real
Chrome or the screen.
"""

import os
import re
import sys
import json
import asyncio
from time import time as time_now

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Checker, EnvironmentGuard, describe_host, run  # noqa: E402
from test_linux_automation import (FakeHyprland, RecordingRunner, client,  # noqa: E402
                                   ALL_TOOLS, with_desktop_tree)
from test_assistant_automation import FakeBridge, Models, WebRig  # noqa: E402

from kayra.utils import print_banner, print_info  # noqa: E402
from kayra.automation import (ai, common, linux, linux_apps, sites, targets,  # noqa: E402
                              web_macros, interpreter)
from kayra.automation.policy import Status  # noqa: E402

check = Checker("browser control")
n = common.normalize_command
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Bridge(FakeBridge):
    """The fake extension at a given version, answering the round-5 commands too."""

    def __init__(self, version="1.2.0", **kw):
        super().__init__(**kw)
        self.version = version

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "page.waitFor":
            self.calls.append((cmd, args))
            return {"ok": True}
        if cmd == "runtime.reload":
            self.calls.append((cmd, args))
            return {"reloading": True}
        if cmd == "tabs.open" and args.get("where") == "incognito" and self.version < "1.2":
            raise AssertionError("an old extension must never be asked for incognito")
        return super().call(cmd, timeout=timeout, **args)

    def opened(self):
        return [(c, a) for c, a in self.calls if c in ("tabs.open", "tabs.navigate")]


class Results:
    """Replaces the web lookup for unknown names."""

    def __init__(self, results):
        self.results, self.asked = results, []

    def __enter__(self):
        self._saved = web_macros.TOP_RESULTS
        web_macros.TOP_RESULTS = lambda q: (self.asked.append(q), self.results)[1]
        return self

    def __exit__(self, *exc):
        web_macros.TOP_RESULTS = self._saved
        return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     A2. THE SITE REGISTRY                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_registry():
    check.section("[1] A2 — the site registry")
    check("the registry is big (>= 150 services)", len(sites.SITES) >= 150, len(sites.SITES))
    bad = [k for k, v in sites.SITES.items() if not v["url"].startswith("https://")]
    check("every URL is https", not bad, bad)
    misses = []
    for name, spec in sites.SITES.items():
        for alias in (name,) + spec["aliases"]:
            if targets.canonical_website(alias) != name:
                misses.append((alias, targets.canonical_website(alias)))
    check("every name and alias resolves to its own site", not misses, misses[:8])
    check("targets.WEBSITE_REGISTRY is a view with the three historical fields",
          set(targets.WEBSITE_REGISTRY) == set(sites.SITES) and all(
              set(v) == {"url", "aliases", "titles"} for v in targets.WEBSITE_REGISTRY.values()))
    check("SITE_HINTS keeps its shape", all(isinstance(v, tuple)
                                            for v in targets.SITE_HINTS.values()))
    for said, site in (("google sheet", "sheets"), ("google sheets", "sheets"),
                       ("google doc", "docs"), ("google docs", "docs"),
                       ("the google sheets website", "sheets"), ("my sheets", "sheets"),
                       ("my docs", "docs"), ("google slide", "slides"), ("google form", "forms"),
                       ("the notion app", "notion"), ("canva website", "canva"),
                       ("gmails", "gmail"), ("my drive", "drive"), ("swiggy", "swiggy"),
                       ("watch later", "watch later"), ("my orders", "amazon orders"),
                       ("my youtube subscriptions", "youtube subscriptions"),
                       ("hugging face", "huggingface"), ("youtube.com", "youtube")):
        check(f"'{said}' -> {site}", targets.canonical_website(said) == site,
              targets.canonical_website(said))
    for said in ("spotify", "whatsapp", "documents", "my photos", "photos", "music",
                 "downloads", "chrome", "youtub", "gothub", "notionn"):
        check(f"'{said}' is NOT a website (app, folder or a misspelling)",
              targets.canonical_website(said) is None, targets.canonical_website(said))
    check("display names are what a person says",
          [sites.display_name(k) for k in ("sheets", "youtube", "docs", "notion", "chatgpt")]
          == ["Google Sheets", "YouTube", "Google Docs", "Notion", "ChatGPT"])
    check("search templates fill in the query",
          sites.search_url("amazon", "usb c hub") == "https://www.amazon.in/s?k=usb+c+hub")
    check("a site with no search template has none", sites.search_url("canva", "x") is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     A3/A4. PARSING                                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_parsing():
    check.section("[2] A3/A4 — where to open it, and one flow per utterance")
    table = (
        ("open google sheet", "browser", "open_url", "google sheet", None),
        ("open google sheets in that tab", "browser", "open_url", "google sheets", "current"),
        ("open google docs in this tab", "browser", "open_url", "google docs", "current"),
        ("open youtube here", "browser", "open_url", "youtube", "current"),
        ("open notion in a new tab", "browser", "open_url", "notion", "tab"),
        ("open notion in a new window", "browser", "open_url", "notion", "window"),
        ("open youtube in incognito", "browser", "open_url", "youtube", "incognito"),
        ("open gmail in a private window", "browser", "open_url", "gmail", "incognito"),
        ("open github next to this tab", "browser", "open_url", "github", "next"),
        ("open youtube in the browser", "browser", "open_url", "youtube", "browser"),
        ("open github.com in this tab", "browser", "open_url", "github.com", "current"),
        ("google search cats in a new tab", "browser", "search_web", "cats", "tab"),
        ("open some new site", "app", "open", "some new site", None),
        ("open chrome", "app", "open", "chrome", None),
        ("browser open google docs here", "browser", "open_url", "google docs", "current"),
    )
    for token, domain, action, target, where in table:
        a = n(token)
        check(f"'{token}' -> {domain}.{action} {target!r} where={where}",
              a and a.domain == domain and a.action == action and a.target == target
              and a.parameters.get("where") == where,
              a and (a.domain, a.action, a.target, a.parameters))
    a = n("open the article i read yesterday about transformers")
    check("'open the article I read yesterday …' is a HISTORY lookup, not a web search",
          a.domain == "browser" and a.action == "history_open", (a.domain, a.action))
    check("'a tab' inside a site name is not a location ('open tab for a cause')",
          n("open tab for a cause").parameters.get("where") is None)
    for token in ("browser update", "browser reload the extension", "browser update extension"):
        a = n(token)
        check(f"'{token}' updates the extension", a.action == "bridge_update", a.action)

    merged = common.merge_tab_steps([n("new tab"), n("open google sheets in that tab")])
    check("'new tab' + 'open X in that tab' becomes ONE open in a new tab",
          len(merged) == 1 and merged[0].action == "open_url"
          and merged[0].parameters["where"] == "tab", [(m.action, m.parameters) for m in merged])
    merged = common.merge_tab_steps([n("new tab"), n("open some odd thing")])
    check("'new tab' + 'open <unknown>' opens it as a WEB page in that tab",
          len(merged) == 1 and merged[0].parameters == {"where": "tab", "web": True},
          merged[0].parameters)
    merged = common.merge_tab_steps([n("new tab"), n("open youtube in incognito")])
    check("an explicit incognito survives the merge", merged[0].parameters["where"] == "incognito")
    merged = common.merge_tab_steps([n("new tab"), n("volume up")] if n("volume up") else [n("new tab")])
    check("'new tab' followed by something unrelated is left alone",
          merged[0].action == "new_tab")
    merged = common.merge_tab_steps([n("open youtube"), n("new tab")])
    check("order matters: an open BEFORE 'new tab' is not merged", len(merged) == 2)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     A1/A3/A5. RUNNING IT                               │
# └────────────────────────────────────────────────────────────────────────┘

def section_opening():
    check.section("[3] A1/A3/A5 — opening, with the extension connected")
    with WebRig(Bridge()) as rig:
        r = rig.do("open google sheet")
        opened = rig.bridge.opened()
        check("'open google sheet' opens Google Sheets in a new tab through the extension",
              r.ok and opened and opened[-1][0] == "tabs.open"
              and opened[-1][1]["url"] == "https://docs.google.com/spreadsheets/"
              and opened[-1][1]["where"] == "tab", (r.message, opened))
        check("…and says what it opened", r.message == "Opening Google Sheets.", r.message)
        check("nothing went to xdg-open while Chrome is connected",
              not any(a[:1] == ["xdg-open"] for a in rig.runner.spawned), rig.runner.spawned)

        r = rig.do("open google sheets in that tab")
        check("'… in that tab' NAVIGATES the current tab",
              rig.bridge.opened()[-1][0] == "tabs.navigate" and "in this tab" in r.message,
              (rig.bridge.opened()[-1], r.message))
        r = rig.do("open notion in a new window")
        check("'… in a new window' asks for a window",
              rig.bridge.opened()[-1][1].get("where") == "window" and "new window" in r.message)
        r = rig.do("open youtube in incognito")
        check("'… in incognito' asks for an incognito window",
              rig.bridge.opened()[-1][1].get("where") == "incognito" and "incognito" in r.message)
        r = rig.do("open github next to this tab")
        check("'… next to this tab'", rig.bridge.opened()[-1][1].get("where") == "next")

        before = len(rig.bridge.opened())
        spoken = asyncio.run(linux.translate_and_execute(["new tab", "open google sheets in that tab"]))
        after = rig.bridge.opened()[before:]
        check("'open new tab and open Google Sheets in that tab' is ONE tabs.open, no Ctrl+T",
              len(after) == 1 and after[0][1]["where"] == "tab"
              and not any("ctrl+t" in " ".join(a) for a, _ in rig.runner.ran), (after, spoken))

        with Results([{"url": "https://en.wikipedia.org/wiki/Zzz", "title": "Zzz"},
                      {"url": "https://www.zzzfoods.in/", "title": "Zzz Foods"}]) as web:
            r = rig.do("open zzz foods")
            check("an unknown name opens the top web result whose host carries the name",
                  r.ok and rig.bridge.opened()[-1][1]["url"] == "https://www.zzzfoods.in/"
                  and "top web result" in r.message and web.asked == ["zzz foods"], r.message)
        with Results([]):
            r = rig.do("open qwxz portal thing")
            check("no result at all -> a web search, said honestly",
                  "google.com/search?q=qwxz" in rig.bridge.opened()[-1][1]["url"]
                  and "searched the web" in r.message, r.message)
        with Results([{"url": "https://x.example/", "title": "x"}]) as web:
            r = rig.do("open the pr group chat")
            check("'open … chat' is never sent to a web search",
                  not web.asked and not r.ok, (web.asked, r.message))
            r = rig.do("open my resume file")
            check("'open … file' is never sent to a web search", not web.asked)

        check("web_worthy: short names yes, paths and chats no",
              web_macros.web_worthy("swiggy") and not web_macros.web_worthy("~/notes")
              and not web_macros.web_worthy("pr outreach group")
              and not web_macros.web_worthy("one two three four five six"))

    check.section("[4] A1/A5 — an installed app or web app wins over a tab")

    def apps():
        hypr = FakeHyprland()
        with WebRig(Bridge(), hypr=hypr) as rig:
            rig.do("open whatsapp web")
            check("'open whatsapp web' launches the WhatsApp web app, not a tab",
                  any("WhatsApp.desktop" in " ".join(a) or "omarchy-launch-webapp" in " ".join(a)
                      for a in rig.runner.spawned)
                  and not rig.bridge.opened(), (rig.runner.spawned, rig.bridge.opened()))
            rig.runner.spawned.clear()
            rig.do("open whatsapp web in the browser")
            check("'… in the browser' opens a tab instead",
                  rig.bridge.opened() and not rig.runner.spawned
                  and rig.bridge.opened()[-1][1]["url"] == "https://web.whatsapp.com/",
                  (rig.runner.spawned, rig.bridge.opened()))
            entries = linux_apps.INDEX.entries()
            check("webapp_for_url finds the web app by host",
                  linux_apps.webapp_for_url("https://web.whatsapp.com/", entries) is not None)
            check("a plain app is never a web app",
                  linux_apps.webapp_for_url("https://github.com/", entries) is None)
    with_desktop_tree(apps)

    class E:
        def __init__(self, exec):
            self.exec, self.name = exec, exec
    maps = E("omarchy-launch-webapp https://maps.google.com")
    gmaps = E("omarchy-launch-webapp https://www.google.com/maps")
    check("a shared host needs the same product path ('open google' is never Maps)",
          linux_apps.webapp_for_url("https://www.google.com/", [gmaps]) is None
          and linux_apps.webapp_for_url("https://www.google.com/maps", [gmaps]) is gmaps)
    check("a quoted Exec URL with extra flags is read",
          linux_apps.webapp_url(E('omarchy-launch-webapp "https://web.whatsapp.com/" --profile-directory="Profile 1"'))
          == "https://web.whatsapp.com/")
    check("an ordinary Exec is not a web app", linux_apps.webapp_url(E("nautilus %U")) is None)
    check("the YouTube web app's host matches the registry's www URL",
          linux_apps.webapp_for_url("https://www.youtube.com/", [E("omarchy-launch-webapp https://youtube.com/")]) is not None)
    del maps


def section_without_bridge():
    check.section("[5] A3 — without the extension, honest about what it could not do")
    tools = ALL_TOOLS + ("google-chrome-stable",)
    with WebRig(Bridge(connected=False), runner=RecordingRunner(tools)) as rig:
        r = rig.do("open google sheets in this tab")
        check("'in this tab' falls back to xdg-open and says why",
              any(a[:1] == ["xdg-open"] and "spreadsheets" in a[1] for a in rig.runner.spawned)
              and "browser set up" in r.message, r.message)
        rig.runner.spawned.clear()
        r = rig.do("open youtube in incognito")
        check("incognito without the extension starts Chrome with --incognito",
              ["google-chrome-stable", "--incognito", "https://www.youtube.com/"] in rig.runner.spawned
              and "incognito" in r.message, (rig.runner.spawned, r.message))
        rig.runner.spawned.clear()
        r = rig.do("open chrome in a new window")
        check("'open chrome in a new window' opens a new browser window",
              any("--new-window" in a for a in rig.runner.spawned), rig.runner.spawned)
        r = rig.do("open github")
        check("a plain open still uses xdg-open", any(a[:1] == ["xdg-open"] for a in rig.runner.spawned))
        check("…and never says a bare 'Done.'", r.message != "Done.", r.message)


def section_extension_versions():
    check.section("[6] The extension updates itself (1.1.0+ has runtime.reload)")
    with WebRig(Bridge(version="1.1.0")) as rig:
        r = rig.do("open youtube in incognito")
        check("a 1.1.0 extension asked for a new feature is RELOADED, not told to the user",
              ("runtime.reload", {}) in rig.bridge.calls and "updated" in r.message, r.message)
        r = rig.do("open google sheets")
        check("…while a plain open still works on 1.1.0", r.ok)
        rig.bridge.calls.clear()
        check("email check on 1.1.0 waits the old way (no page.waitFor)",
              rig.do("email check").ok and "page.waitFor" not in rig.bridge.cmds())
    with WebRig(Bridge(version="1.0.0")) as rig:
        r = rig.do("open youtube in incognito")
        check("a 1.0.0 extension cannot reload itself: the manual instruction",
              ("runtime.reload", {}) not in rig.bridge.calls and "chrome://extensions" in r.message,
              r.message)
    with WebRig(Bridge()) as rig:
        r = rig.do("browser update")
        check("'browser update' reloads the extension", r.ok and "runtime.reload" in rig.bridge.cmds())
        rig.do("email check")
        waited = [a for c, a in rig.bridge.calls if c == "page.waitFor"]
        check("email check waits for readiness (page.waitFor), not a fixed sleep",
              waited and waited[0].get("titleIncludes") == "Inbox", waited)
    manifest = json.load(open(os.path.join(ROOT, "extension", "kayra-bridge", "manifest.json")))
    check("manifest version == web_macros.REQUIRED_VERSION",
          tuple(int(x) for x in manifest["version"].split(".")) == web_macros.REQUIRED_VERSION,
          manifest["version"])
    source = open(os.path.join(ROOT, "extension", "kayra-bridge", "background.js")).read()
    for needle in ('case "page.waitFor"', 'where === "incognito"', 'where === "next"',
                   'windowTypes: ["normal"]'):
        check(f"background.js handles {needle}", needle in source)
    check("never debugger or native messaging (downloads was approved for 1.6.0, D5)",
          not set(manifest["permissions"]) & {"debugger", "nativeMessaging", "cookies"})


def section_app_windows():
    check.section("[7] H0.1 — a web-app window is a browser surface")
    check("chrome-web.whatsapp.com__-Default is an app window",
          linux_apps.is_app_window_class("chrome-web.whatsapp.com__-Default"))
    check("…for web.whatsapp.com",
          linux_apps.site_of_window_class("chrome-web.whatsapp.com__-Default") == "web.whatsapp.com")
    check("google-chrome itself is not an app window",
          not linux_apps.is_app_window_class("google-chrome"))
    hypr = FakeHyprland([client("0xw1", "chrome-web.whatsapp.com__-Default", "WhatsApp")],
                        active="0xw1")
    with WebRig(Bridge(), hypr=hypr) as rig:
        front = linux.active_window()
        check("the WhatsApp web-app window counts as a browser window",
              front is not None and linux._is_browser_window(front))
        r = rig.do("click on rahul")
        check("'click on X' in a web-app window goes INTO the page, not to OCR",
              "page.click" in rig.bridge.cmds() and r.ok, (rig.bridge.cmds(), r.message))


class Model:
    """A scripted model for the understanding layer. Records every prompt it was shown."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __enter__(self):
        self._saved = (ai.TEXT_MODEL, interpreter.ACTIVE)
        interpreter.ACTIVE = True

        def answer(messages):
            self.prompts.append(messages[-1]["content"])
            return self.replies.pop(0) if self.replies else ""
        ai.TEXT_MODEL = answer
        return self

    def __exit__(self, *exc):
        ai.TEXT_MODEL, interpreter.ACTIVE = self._saved
        return False


def run_tokens(tokens):
    return asyncio.run(linux.translate_and_execute(tokens))


def section_understanding():
    check.section("[8] The understanding layer — a model works out what you MEANT")
    whatsapp = [client("0xw1", "chrome-web.whatsapp.com__-Default", "WhatsApp")]
    with WebRig(Bridge(), hypr=FakeHyprland(whatsapp, active="0xw1")) as rig:
        with Model('{"kind": "automation", "commands": ["browser click on PR and outreach core team"]}') as m:
            spoken = run_tokens(["open PR and outreach core team"])
            check("'open XYZ' with WhatsApp in front: the rules miss, the model clicks the chat",
                  ("page.click", {"text": "pr and outreach core team", "nth": 1}) in rig.bridge.calls
                  or any(c == "page.click" and "outreach" in a["text"].lower()
                         for c, a in rig.bridge.calls), (rig.bridge.calls[-3:], spoken))
            check("…the model was told what is on screen (WhatsApp in front)",
                  m.prompts and "web.whatsapp.com" in m.prompts[0], m.prompts[:1])
            check("…and what the rules tried", m.prompts and "couldn't find" in m.prompts[0].lower())
            check("exactly one model call (its own proposals are never re-interpreted)",
                  len(m.prompts) == 1, len(m.prompts))

    with WebRig(Bridge()) as rig:
        with Model('```json\n{"kind": "automation", "commands": ["browser mute this tab"]}\n```') as m:
            spoken = run_tokens(["make this tab quiet please"])
            check("an unparseable request becomes the command it meant (a fenced reply is fine)",
                  "tabs.mute" in rig.bridge.cmds(), (rig.bridge.cmds(), spoken))
        with Model('{"kind": "chat"}'):
            spoken = run_tokens(["whats a good name for my cat"])
            check("a question that reached automation is handed to CHAT, nothing runs",
                  spoken == "" and linux.take_chat_handoff() and not linux.take_chat_handoff(),
                  spoken)
        with Model('{"kind": "ask", "question": "Do you mean the Budget sheet or the Budget doc?"}'):
            spoken = run_tokens(["open the budget thing"])
            check("genuinely ambiguous: ONE question back, nothing opened",
                  spoken.startswith("Do you mean") and not rig.bridge.opened(), spoken)
        with Model('{"kind": "automation", "commands": ["system shutdown the computer", '
                   '"terminal rm -rf ~", "delete file notes.txt", "plan take over the world", '
                   '"browser click on settings"]}'):
            calls = len(rig.bridge.calls)
            run_tokens(["do the thing with the stuff"])
            check("power, shell, file deletion and plans are NEVER accepted from a model",
                  not any("--incognito" in a or "systemctl" in " ".join(a) for a in rig.runner.spawned)
                  and not any("rm" in " ".join(a[0]) for a in rig.runner.ran)
                  and linux.CONFIRMATIONS.peek() is None, linux.CONFIRMATIONS.peek())
            check("…while the safe proposal in the same reply still runs",
                  any(c == "page.click" for c, _ in rig.bridge.calls[calls:]))
        with Model('{"kind": "automation", "commands": ["email send"]}'):
            calls = len(rig.bridge.calls)
            spoken = run_tokens(["fire off that mail i wrote"])
            check("a model proposal that needs a yes STILL asks first; nothing clicked",
                  linux.CONFIRMATIONS.peek() is not None
                  and not any(c == "page.click" for c, _ in rig.bridge.calls[calls:]), spoken)
            linux.CONFIRMATIONS.cancel()
        with Model(""):
            with Results([]):
                spoken = run_tokens(["open qwerty zxcv"])
            check("model unreachable: the honest original answer stands",
                  "couldn't find" in spoken.lower() or "can't" in spoken.lower(), spoken)
        with Model('{"kind": "none"}') as m:
            with Results([{"url": "https://www.zzzfoods.in/", "title": "Zzz"}]):
                before = len(rig.bridge.opened())
                spoken = run_tokens(["open zzz foods"])
            check("unknown name: the model is asked FIRST, the web result is the last resort",
                  len(m.prompts) == 1 and rig.bridge.opened()[before:]
                  and rig.bridge.opened()[-1][1]["url"] == "https://www.zzzfoods.in/", spoken)
        with Model('{"kind": "google search", "queries": ["who invented the telephone"]}'):
            spoken = run_tokens(["who invented the telephone"])
            check("a reply outside the contract ('google search') is read as CHAT, nothing opens",
                  spoken == "" and linux.take_chat_handoff(), spoken)
        with Model("I think you want to open sheets!"):
            spoken = run_tokens(["blorp the glorp"])
            check("a reply that is not JSON proposes nothing", "not sure" in spoken.lower(), spoken)
        with Model('{"kind": "automation", "commands": ["open google sheets"]}') as m:
            interpreter.ACTIVE = False
            run_tokens(["blorp the glorp"])
            check("switched off (ACTIVE False / AUTOMATION_AI_INTERPRETER=off): no model call",
                  not m.prompts)
        with Model() as m:
            run_tokens(["open google sheets", "volume up" if n("volume up") else "pause"])
            check("everything the rules handle never pays for a model call", not m.prompts, m.prompts)

    check("the fast call asks for NO thinking and a deterministic, short answer",
          '"reasoning_effort": "none"' in open(ai.__file__).read()
          and "ai.fast(" in open(interpreter.__file__).read())
    calls = []
    saved = (ai.TEXT_MODEL, ai.requests if hasattr(ai, "requests") else None)
    ai.TEXT_MODEL = lambda messages: (calls.append(1), '{"kind": "chat"}')[1]
    check("with a scripted model the fast path never touches the network",
          ai.fast("s", "u") == '{"kind": "chat"}' and calls == [1])
    ai.TEXT_MODEL = saved[0]
    for line in re.findall(r'-> \{\{"kind": "automation", "commands": \["(.+?)"\]', interpreter._SYSTEM):
        a = n(line)
        check(f"the worked example '{line}' is a real command", a is not None and not common._is_guess(a))
    examples = ["open notion in this tab", "google search cats", "browser click on the second reply",
                "browser type hello into search", "browser search this page for pricing",
                "browser scroll to pricing", "browser summarize this page", "browser what can i click",
                "browser switch to gmail tab", "browser mute this tab", "email open latest",
                "email draft to a@b.com about lunch saying hi", "calendar add standup tomorrow at 10am",
                "meet mic off", "message rahul: running late", "system volume 40", "take screenshot",
                "set timer 5 minutes", "type hello", "new tab", "close tab", "go back"]
    unknown = [e for e in examples if n(e) is None or common._is_guess(n(e))]
    check("every catalog example the model is shown is a real command", not unknown, unknown)


def section_tuning():
    check.section("[8b] Tuning pass (2026-11-10): no model, no clock, no churn where none is needed")
    import tempfile
    import time as _time
    from kayra.intelligence import llm_engine
    from kayra.automation import browser_bridge, workflows

    # ── the classifier's exact-command fast path ──
    fast = llm_engine.fast_intent
    check("every fast-path literal is a token the classifier may emit AND the parser understands",
          all(n(c) is not None for c in llm_engine._FAST_COMMANDS))
    for said, token in (("Pause.", "pause"), ("Kayra, next tab please", "next tab"),
                        ("volume 40", "system volume 40"), ("Set the volume to 70%", "system volume 70"),
                        ("brightness 30", "system brightness 30"), ("mute", "system mute")):
        check(f"fast path: {said!r} -> {token!r}", fast(said, "Kayra") == token and n(token) is not None,
              str(fast(said, "Kayra")))
    for said in ("stop the music", "pause for a second and tell me a joke", "copy that", "search",
                 "stop", "undo that", "exit", "close everything", "close all", "save", "print",
                 "what is a tab", "open chrome", "volume", "go to sleep", ""):
        check(f"fast path leaves {said!r} to the model", fast(said, "Kayra") is None)
    check("no power, lifecycle or broad-close word can come from the fast path",
          not any(re.search(r"shut|restart|sleep|exit|close all|close everything|delete|power", c)
                  for c in llm_engine._FAST_COMMANDS))

    class Engine:                                   # classify_intent on a stub: no model exists
        env_vars = {}
        is_online = False
    check("classify_intent answers a complete command without touching a model",
          llm_engine.CentralizedLLMEngine.classify_intent(Engine(), "Next tab.") == ["next tab"])
    Engine.env_vars = {"DMM_FAST_PATH": "off"}
    check("DMM_FAST_PATH=off switches it off", not llm_engine._fast_path_enabled(Engine()))

    # ── readiness instead of fixed pauses ──
    tries = []
    check("_until returns the first truthy attempt at once",
          web_macros._until(lambda: (tries.append(1), "ok")[1], 2.5) == "ok" and len(tries) == 1)
    saved_sleep, naps, tries = web_macros.SLEEP, [], []
    web_macros.SLEEP = naps.append
    try:
        got = web_macros._until(lambda: (tries.append(1), len(tries) >= 3)[1], 2.5)
        check("_until retries in short polls until ready", got is True and naps == [0.25, 0.25], str(naps))
        naps.clear()
        check("_until gives up after its budget, counted in attempts",
              not web_macros._until(lambda: None, 1.0) and len(naps) == 4, str(naps))

        def navigating():
            raise browser_bridge.BridgeError("frame was detached")
        check("a page that is mid-navigation is retried, not fatal",
              web_macros._until(navigating, 0.5) is None)
    finally:
        web_macros.SLEEP = saved_sleep
    source = open(web_macros.__file__).read()
    pauses = re.findall(r"SLEEP\((\d+(?:\.\d+)?)\)", source)
    check("no fixed pause is left in web_macros (the share pickers are waited for too, M0.4)",
          pauses == [], str(pauses))
    with WebRig(Bridge(version="1.2.1")) as rig, Models("Two new emails."):
        rig.do("email read my emails")
        waited = [a for c, a in rig.bridge.calls if c == "page.waitFor"]
        check("reading the inbox waits for the message list", any(a.get("selector") == "tr.zA" for a in waited),
              str(waited))

    # ── learned sites are never read or written under a substituted lookup ──
    with Results([{"url": "https://www.swiggy.com/", "title": "Swiggy"}]):
        check("a test rig's lookup neither recalls nor remembers",
              web_macros.recall_site("swiggy") is None
              and web_macros.remember_site("swiggy", "https://www.swiggy.com/") is False)
    saved_path, saved_top = web_macros._learned_path, web_macros.TOP_RESULTS
    with tempfile.TemporaryDirectory() as folder:
        web_macros._learned_path = lambda: os.path.join(folder, "learned_sites.json")
        web_macros.TOP_RESULTS = web_macros._ddgs_top
        try:
            check("a result whose host carries the name is remembered",
                  web_macros.remember_site("Swiggy", "https://www.swiggy.com/restaurants")
                  and web_macros.recall_site("swiggy") == "https://swiggy.com/")
            check("a first-result guess on some other host is not",
                  web_macros.remember_site("swiggy", "https://en.wikipedia.org/wiki/Swiggy") is False)
        finally:
            web_macros._learned_path, web_macros.TOP_RESULTS = saved_path, saved_top

    # ── the bridge ──
    mask, payload = b"\x11\x22\x33\x44", bytes(range(256)) * 3 + b"xyz"
    slow = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    check("frame unmasking matches the per-byte definition (and handles empty)",
          browser_bridge._unmask(payload, mask) == slow and browser_bridge._unmask(b"", mask) == b"")
    check("the accept loop has no polling timeout",
          "settimeout(1.0)" not in open(browser_bridge.__file__).read())
    saved_bridge, saved_flag = web_macros.BRIDGE, web_macros._REFRESHED["done"]
    try:
        web_macros._REFRESHED["done"] = False
        web_macros.BRIDGE = old = Bridge(version="1.2.0")
        check("an out-of-date extension is reloaded once when it says hello",
              web_macros.refresh_stale_extension() and ("runtime.reload", {}) in old.calls
              and not web_macros.refresh_stale_extension())
        web_macros._REFRESHED["done"] = False
        web_macros.BRIDGE = current = Bridge(version="1.13.0")
        check("a current one is left alone",
              not web_macros.refresh_stale_extension() and not current.calls)
    finally:
        web_macros.BRIDGE, web_macros._REFRESHED["done"] = saved_bridge, saved_flag

    # ── the watcher writes the trigger file only when something changed ──
    saved_data, saved_cache = workflows._data, dict(workflows._TRIGGER_CACHE)
    with tempfile.TemporaryDirectory() as folder:
        workflows._data = lambda name: os.path.join(folder, name)
        workflows._TRIGGER_CACHE.update(stamp=None, data=[])
        try:
            far = _time.time() + 3600
            workflows._save(workflows.TRIGGER_FILE, [{"condition": {"type": "after", "due": far},
                                                      "tokens": ["say stretch"], "created": 0}])
            path = os.path.join(folder, workflows.TRIGGER_FILE)
            before = os.stat(path).st_mtime_ns
            for _ in range(3):
                workflows.evaluate_triggers(snapshot={})
            check("three idle ticks do not rewrite triggers.json", os.stat(path).st_mtime_ns == before)
            lines = workflows.evaluate_triggers(now=far + 1, snapshot={})
            check("a trigger that fires is removed from the file",
                  json.load(open(path)) == [] and lines, str(lines))
        finally:
            workflows._data = saved_data
            workflows._TRIGGER_CACHE.update(saved_cache)

    # ── the app index re-parses only when a folder changed ──
    with tempfile.TemporaryDirectory() as folder:
        index, loads = linux_apps._Index(), []
        real_load, real_dirs = index._load, linux_apps.application_dirs
        index._load = lambda dirs=None: (loads.append(1), real_load(dirs))[1]
        linux_apps.application_dirs = lambda environ=None: [folder]
        try:
            index.ensure(); index.ensure()
            index._checked_at = 0.0
            index.ensure()
            check("an unchanged folder is stat-ed, not re-parsed", len(loads) == 1, str(loads))
            with open(os.path.join(folder, "new.desktop"), "w") as handle:
                handle.write("[Desktop Entry]\nType=Application\nName=New App\nExec=newapp\n")
            os.utime(folder, ns=(_time.time_ns() + 10**9, _time.time_ns() + 10**9))
            index._checked_at = 0.0
            index.ensure()
            check("installing an app is picked up on the next lookup",
                  len(loads) == 2 and index.by_name("new app") is not None, str(loads))
        finally:
            linux_apps.application_dirs = real_dirs


# ┌────────────────────────────────────────────────────────────────────────┐
# │              PART 2 (first slice) — B1 by position, B2 bulk close      │
# └────────────────────────────────────────────────────────────────────────┘

class TabBridge(Bridge):
    """Two Chrome windows; the focused one has five tabs in strip order, the third active."""

    def __init__(self, version="1.3.0"):
        super().__init__(version=version)
        self.closed = [{"sessionId": "s1", "kind": "tab", "title": "Rust lifetimes - YouTube",
                        "url": "https://www.youtube.com/watch?v=9"},
                       {"sessionId": "s2", "kind": "tab", "title": "Hacker News",
                        "url": "https://news.ycombinator.com/"}]
        self.texts = {12: "lofi beats to study to", 13: "Quarterly plan: the transformer "
                      "architecture section, attention and transformers", 15: "pull requests"}
        self.zoom = 1.0
        self.tabs = [
            {"id": 11, "title": "Inbox - Gmail", "url": "https://mail.google.com/mail/", "windowId": 1, "active": False, "pinned": True},
            {"id": 12, "title": "Lo-fi - YouTube", "url": "https://www.youtube.com/watch?v=1", "windowId": 1, "active": False},
            {"id": 13, "title": "Docs", "url": "https://docs.google.com/", "windowId": 1, "active": True},
            {"id": 14, "title": "Lo-fi - YouTube", "url": "https://www.youtube.com/watch?v=1#t=30", "windowId": 1, "active": False},
            {"id": 15, "title": "GitHub", "url": "https://github.com/", "windowId": 1, "active": False},
            {"id": 21, "title": "Other window", "url": "https://example.com/", "windowId": 2, "active": True},
        ]

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "ping":
            return {"version": self.version}
        if cmd == "tabs.list":
            return list(self.tabs)
        if cmd == "tabs.active":
            return next(t for t in self.tabs if t["windowId"] == 1 and t["active"])
        if cmd == "tabs.activate" and "tabId" in args:
            self.calls.append((cmd, args))
            return next(t for t in self.tabs if t["id"] == args["tabId"])
        if cmd in ("tabs.muteOthers", "tabs.discard", "tabs.reload", "tabs.zoom",
                   "tabs.recentlyClosed", "tabs.restore", "page.text"):
            self.calls.append((cmd, args))
            if cmd == "tabs.muteOthers":
                return {"changed": 2}
            if cmd == "tabs.discard":
                return {"discarded": 3}
            if cmd == "tabs.zoom":
                self.zoom = args.get("factor", self.zoom)
                return {"factor": self.zoom}
            if cmd == "tabs.recentlyClosed":
                return list(self.closed)
            if cmd == "tabs.restore":
                hit = next((c for c in self.closed if c["sessionId"] == args.get("sessionId")),
                           self.closed[0])
                return {"restored": True, "title": hit["title"]}
            if cmd == "page.text":
                return {"text": self.texts.get(args.get("tabId"), "")}
            return {}
        if cmd == "tabs.close" and "tabIds" in args:
            self.calls.append((cmd, args))
            before = len(self.tabs)
            self.tabs = [t for t in self.tabs if t["id"] not in args["tabIds"]]
            return {"closed": before - len(self.tabs)}
        return super().call(cmd, timeout=timeout, **args)

    def last(self, cmd):
        return next((a for c, a in reversed(self.calls) if c == cmd), None)


def section_tabs_by_position():
    check.section("[10] Part 2 — tabs by position, duplicates, to the right")
    table = (
        ("browser switch to the third tab", "tab_switch", "#3"),
        ("browser go to the last tab", "tab_switch", "#-1"),
        ("browser go to the 2nd tab", "tab_switch", "#2"),
        ("browser switch to tab 4", "tab_switch", "#4"),
        ("browser go to the tab on the right", "tab_switch", "+1"),
        ("browser close the second last tab", "tab_close", "#-2"),
        ("browser close the first tab", "tab_close", "#1"),
        ("browser close duplicate tabs", "tabs_close_duplicates", "duplicates"),
        ("browser close tabs to the right", "tabs_close_side", "right"),
        ("browser close the tabs on the left", "tabs_close_side", "left"),
        # names are still names
        ("browser switch to youtube", "tab_switch", "youtube"),
        ("browser close the youtube tab", "tab_close", "youtube"),
    )
    for token, action, target in table:
        a = n(token)
        check(f"'{token}' -> browser.{action} {target!r}",
              a.domain == "browser" and a.action == action and a.target == target,
              f"{a.domain}.{a.action} {a.target!r}")
    check("'go to the third tab' and 'close duplicate tabs' are in the understanding catalog",
          "browser switch to the third tab" in interpreter.CATALOG
          and "browser close duplicate tabs" in interpreter.CATALOG)

    with WebRig(TabBridge()) as rig:
        r = rig.do("browser switch to the third tab")
        check("'the third tab' counts in the focused window's strip (Docs)",
              r.ok and rig.bridge.last("tabs.activate") == {"tabId": 13}, (r.message,))
        r = rig.do("browser go to the last tab")
        check("'the last tab' is GitHub, not the other window's tab",
              rig.bridge.last("tabs.activate") == {"tabId": 15}, r.message)
        r = rig.do("browser go to the tab on the right")
        check("'the tab on the right' is next to the ACTIVE one",
              rig.bridge.last("tabs.activate") == {"tabId": 14}, r.message)
        r = rig.do("browser switch to the ninth tab")
        check("a position past the end says how many tabs there are",
              not r.ok and "5 tabs" in r.message, r.message)
        r = rig.do("browser close duplicate tabs")
        check("duplicates: the second YouTube copy (same page, different #fragment) closes",
              rig.bridge.last("tabs.close") == {"tabIds": [14]} and "Closed 1 tab" in r.message,
              (rig.bridge.last("tabs.close"), r.message))
        r = rig.do("browser close duplicate tabs")
        check("...and running it again says there are none", "No duplicate" in r.message, r.message)
        r = rig.do("browser close the first tab")
        check("closing a tab by position closes exactly that tab",
              rig.bridge.last("tabs.close") == {"tabIds": [11]}, rig.bridge.last("tabs.close"))
        r = rig.do("browser close tabs to the right")
        check("closing tabs to the right asks first (it closes several at once)",
              r.status == Status.NEEDS_CONFIRMATION, r.status)

    with WebRig(TabBridge(version="1.2.1")) as rig:
        r = rig.do("browser close duplicate tabs")
        check("an extension older than 1.2.2 is never sent a tab-id close",
              rig.bridge.last("tabs.close") is None and not r.ok, r.message)


def section_part_2b():
    check.section("[11] Part 2b — mute the rest, recently closed, sleep, find, zoom, copy")
    table = (
        ("browser mute everything except this tab", "tabs_mute_others", "others"),
        ("browser unmute all other tabs", "tabs_mute_others", "others"),
        ("browser what did i just close", "tabs_recently_closed", "closed"),
        ("browser reopen the youtube tab i closed", "tab_reopen", "youtube"),
        ("browser free up memory in chrome", "tabs_discard", "background"),
        ("browser put background tabs to sleep", "tabs_discard", "background"),
        ("browser find the tab where i was reading about transformers", "tab_find", "transformers"),
        ("browser hard reload", "page_reload", "current"),
        ("browser zoom to 150 percent", "page_zoom", "150"),
        ("browser copy the link", "page_copy", "link"),
        ("browser copy the page title", "page_copy", "title"),
        ("browser mute this tab", "tab_mute", "current"),          # unchanged
    )
    for token, action, target in table:
        a = n(token)
        check(f"'{token}' -> browser.{action} {target!r}",
              a.domain == "browser" and a.action == action and a.target == target,
              f"{a.domain}.{a.action} {a.target!r}")
    check("the new shapes are in the understanding catalog",
          all(phrase in interpreter.CATALOG for phrase in (
              "browser what did i just close", "browser free up memory in chrome",
              "browser find the tab about <topic>", "browser copy the link")))

    copied = []
    saved_copy = web_macros.COPY
    web_macros.COPY = lambda text: (copied.append(text), True)[1]
    try:
        with WebRig(TabBridge()) as rig:
            r = rig.do("browser mute everything except this tab")
            check("muting the rest asks the extension to mute OTHER tabs",
                  rig.bridge.last("tabs.muteOthers") == {"on": True} and "2 other tabs" in r.message,
                  r.message)
            r = rig.do("browser what did i just close")
            check("'what did I just close' names the closed tabs",
                  "Rust lifetimes" in r.message and "Hacker News" in r.message, r.message)
            r = rig.do("browser reopen the hacker news tab i closed")
            check("reopening a NAMED closed tab restores that session, not just the last one",
                  rig.bridge.last("tabs.restore") == {"sessionId": "s2"}
                  and "Hacker News" in r.message, (rig.bridge.last("tabs.restore"), r.message))
            r = rig.do("browser reopen the netflix tab i closed")
            check("...and says so when nothing closed matches", not r.ok, r.message)
            r = rig.do("browser free up memory in chrome")
            check("freeing memory puts background tabs to sleep and says they come back",
                  "3 background tabs" in r.message and "reload" in r.message, r.message)
            r = rig.do("browser find the tab where i was reading about transformers")
            check("finding a tab reads the pages and switches to the best match (Docs)",
                  rig.bridge.last("tabs.activate") == {"tabId": 13}, (rig.bridge.last("tabs.activate"), r.message))
            r = rig.do("browser find the tab about github")
            check("...a title or address match wins without reading every page",
                  rig.bridge.last("tabs.activate") == {"tabId": 15}, r.message)
            r = rig.do("browser hard reload")
            check("hard reload bypasses the cache", rig.bridge.last("tabs.reload") == {"hard": True})
            r = rig.do("browser zoom to 150 percent")
            check("zoom to 150 percent sets factor 1.5",
                  rig.bridge.last("tabs.zoom") == {"factor": 1.5} and "150 percent" in r.message,
                  r.message)
            r = rig.do("browser copy the link")
            check("copy the link puts the active tab's address on the clipboard",
                  copied[-1:] == ["https://docs.google.com/"] and r.ok, (copied, r.message))
            r = rig.do("browser copy the page title")
            check("copy the title copies the title", copied[-1:] == ["Docs"], copied)
        with WebRig(TabBridge(version="1.2.2")) as rig:
            r = rig.do("browser free up memory in chrome")
            check("an extension older than 1.3.0 is never sent a 1.3.0 command",
                  rig.bridge.last("tabs.discard") is None and not r.ok, r.message)
    finally:
        web_macros.COPY = saved_copy


# ┌────────────────────────────────────────────────────────────────────────┐
# │          PART 2M — MEETINGS AND SCREEN SHARING (§12, M0/M1/M2.1)        │
# └────────────────────────────────────────────────────────────────────────┘

ENTER = ["wtype", "-k", "Return"]


class ShareHelper:
    """Stands in for share_picker: never reads ~/.config, never writes $XDG_RUNTIME_DIR."""

    def __init__(self, installed=True):
        self._installed, self.requests, self.cleared = installed, [], 0

    def installed(self):
        return self._installed

    def request_window(self, window):
        self.requests.append(("window", window.address))

    def request_monitor(self, name):
        self.requests.append(("monitor", name))

    def clear(self):
        self.cleared += 1


class MeetBridge(Bridge):
    """
    A Meet call. Pressing Meet's share button makes a picker window appear in the fake
    compositor (`on_share`); Meet shows "Stop presenting" once `presses_needed` Enters were
    sent (`presses`), so the readiness loop has to keep looking until the share is real.
    """

    def __init__(self, presses, on_share=None, presses_needed=1, presenting=False, **kw):
        super().__init__(version="1.4.0", tabs=[{"id": 3, "title": "Meet - abc",
                                                 "url": "https://meet.google.com/abc-defg-hij"}], **kw)
        self.presses, self.on_share = presses, on_share
        self.presses_needed, self.presenting = presses_needed, presenting
        self.base, self.armed = 0, False

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "page.controls":
            self.calls.append((cmd, args))
            if not self.presenting and self.armed and \
                    self.presses() - self.base >= self.presses_needed:
                self.presenting, self.armed = True, False
            return [{"name": "Stop presenting" if self.presenting else "Share screen"}]
        if cmd == "page.click":
            self.calls.append((cmd, args))
            if args["text"] == "Share screen":
                self.base, self.armed = self.presses(), True
                if self.on_share:
                    self.on_share()
                return {"clicked": True}
            if args["text"] in ("Stop presenting", "Leave call"):
                was, self.presenting = self.presenting, False
                return {"clicked": was or args["text"] == "Leave call"}
            return {"clicked": False}
        return super().call(cmd, timeout=timeout, **args)


def _share_rig(helper, picker=("google-chrome", "Choose what to share"), monitors=None,
               presses_needed=1):
    hypr = FakeHyprland([client("0xc", "google-chrome", "Meet - abc - Google Chrome", focus=0),
                         client("0xl", "lm-studio", "LM Studio", focus=2)],
                        active="0xc", monitors=monitors)
    from test_linux_automation import RecordingRunner as Runner
    runner = Runner(ALL_TOOLS + ("makoctl",))
    bridge = MeetBridge(lambda: runner.calls.count(ENTER),
                        on_share=(lambda: hypr.add(client("0xpick", picker[0], picker[1], focus=0)))
                        if picker else None, presses_needed=presses_needed)
    saved = web_macros.SHARE_HELPER
    web_macros.SHARE_HELPER = helper
    rig = WebRig(bridge, hypr=hypr, runner=runner)
    rig._restore_helper = lambda: setattr(web_macros, "SHARE_HELPER", saved)
    return rig


def section_meetings():
    check.section("[12] Part 2M — meetings and screen sharing (M0 bugs, M1 share what was named)")
    table = (
        # the four misroutes from logs/automation.log, 2026-11-10 16:25-16:26
        ("click on share screen", "monitor", ""),
        ("meet start screen sharing", "monitor", ""),
        ("meet start screen sharing of this meet and share the lm studio window", "window", "lm studio"),
        ("share the lm studio window", "window", "lm studio"),
        # every phrasing in M0.1
        ("share screen", "monitor", ""), ("start screen sharing", "monitor", ""),
        ("present now", "monitor", ""), ("browser click on present now", "monitor", ""),
        ("screen share", "monitor", ""), ("share my entire screen", "monitor", ""),
        ("meet share entire screen", "monitor", ""), ("meet present", "monitor", ""),
        ("meet share the lm studio window", "window", "lm studio"),
        ("meet share lm studio", "window", "lm studio"),
        ("meet share this tab", "browser", ""), ("meet share the youtube tab", "browser", "youtube"),
        ("meet share my second screen", "monitor", "#2"),
        ("meet share window", "window", ""),
    )
    for token, surface, name in table:
        a = n(token)
        check(f"'{token}' -> meet.share {surface} {name!r}",
              a is not None and a.key == "meet.share" and a.target == surface
              and a.parameters.get("name") == name,
              f"{a.key if a else None} {a.target if a else ''} {a.parameters if a else ''}")
    a = n("meet share my screen with audio")
    check("'with audio' asks for system sound", a.parameters.get("audio") is True, a.parameters)
    a = n("meet share lm studio instead")
    check("'... instead' switches what is shared", a.parameters.get("instead") is True, a.parameters)
    check("'meet am i sharing' is a status question", n("meet am i sharing").key == "meet.share_status")
    check("'meet stop sharing' still stops", n("meet stop sharing").key == "meet.stop_share")
    check("'meet join abc-defg-hij' still joins by code", n("meet join abc-defg-hij").key == "meet.join")
    check("'click on share' on a page stays a click (no screen word)",
          n("click on share").key == "mouse.click_text")
    check("'present tense of go' is not a share", n("present tense of go") is None)
    check("the share shapes are in the understanding catalog",
          all(p in interpreter.CATALOG for p in ("meet share the <app> window", "meet share this tab",
                                                 "meet am i sharing", "meet join new")))

    # M0.3 — the name belongs to the share, never a focus that takes Meet out of the front.
    merged = common.merge_share_steps([n("focus lm studio"), n("meet share window")])
    check("'focus X' + 'meet share window' -> ONE share of the X window",
          len(merged) == 1 and merged[0].key == "meet.share" and merged[0].target == "window"
          and merged[0].parameters.get("name") == "lm studio", merged)
    merged = common.merge_share_steps([n("meet share screen"), n("meet share the lm studio window")])
    check("two shares in one utterance -> the one that named something",
          len(merged) == 1 and merged[0].parameters.get("name") == "lm studio", merged)

    # M0.2 — the preamble's example code is never a real meeting.
    from kayra.intelligence import llm_engine as E
    g = E._guard_meeting_code
    check("a code the user never said, during a share request, becomes the share",
          g("meet join abc-defg-hij", "start screen sharing") == "meet start screen sharing")
    check("a code the user never said is dropped otherwise",
          g("meet join abc-defg-hij", "open the meeting from my calendar") is None)
    check("a code the user DID say (spoken with spaces) is kept",
          g("meet join abc-defg-hij", "join meeting abc defg hij") == "meet join abc-defg-hij")
    check("'meet join new' for 'start screen sharing' is re-read as the share",
          g("meet join new", "start screen sharing") == "meet start screen sharing")
    check("'meet join new' for 'start a new meeting and share my screen' is kept",
          g("meet join new", "start a new meeting and share my screen") == "meet join new")
    check("every other token passes untouched", g("meet mic off", "mute me") == "meet mic off"
          and g("browser list tabs", "abc") == "browser list tabs")
    preamble = open(E.__file__, encoding="utf-8").read()
    check("the DMM preamble says sharing is ONE meet share token, never a click or a join",
          "SCREEN SHARING is always ONE 'meet share' token" in preamble
          and "'meet share the lm studio window'" in preamble)

    # M0.5 — the confirmation says WHAT goes to everyone.
    helper = ShareHelper(installed=True)
    rig = _share_rig(helper)
    try:
        with rig:
            r = rig.do("meet share the lm studio window")
            check("the confirmation names the window and the audience",
                  r.status == Status.NEEDS_CONFIRMATION and "the lm studio window" in r.message
                  and "everyone in the call" in r.message, r.message)
            ok, said = linux.resolve_confirmation("yes")
            check("M1.1 with the helper: the portal is asked for THAT window (by address)",
                  helper.requests == [("window", "0xl")], helper.requests)
            check("the page hook is told to prefer a window",
                  ("page.setMain", {"key": "__kayraShareSurface", "value": "window"}) in rig.bridge.calls)
            check("LM Studio was NOT focused (Meet stays in front)",
                  not any('"address:0xl"' in d and "focus" in d for d in rig.hypr.dispatched),
                  rig.hypr.dispatched)
            check("Chrome's picker got Enter once it appeared (readiness, not a clock)",
                  rig.runner.calls.count(ENTER) == 1, rig.runner.calls)
            check("Kayra says what is being shared", "sharing the lm studio window" in said, said)
            check("M2.1 notifications are silenced for the share",
                  ["makoctl", "mode", "-a", "do-not-disturb"] in rig.runner.calls, rig.runner.calls)
            check("the one-shot request is withdrawn after the share", helper.cleared >= 1)
            r = rig.do("meet am i sharing")
            check("'am I sharing?' answers with what", "lm studio window" in r.message, r.message)
            r = rig.do("meet stop sharing")
            check("stopping clicks Meet's Stop presenting and brings notifications back",
                  ("page.click", {"text": "Stop presenting", "nth": 1}) in rig.bridge.calls
                  and ["makoctl", "mode", "-r", "do-not-disturb"] in rig.runner.calls, r.message)
            r = rig.do("meet am i sharing")
            check("...and then 'am I sharing?' says no", r.message.startswith("No"), r.message)
    finally:
        rig._restore_helper()

    # "yes, don't ask again" — for this call only.
    helper = ShareHelper(installed=True)
    rig = _share_rig(helper, picker=None)
    try:
        with rig:
            rig.do("meet share my screen")
            ok, said = linux.resolve_confirmation("yes, don't ask again")
            check("'yes, don't ask again' approves and says it will not ask again",
                  ok and "won't ask again" in said, said)
            rig.do("meet stop sharing")
            r = rig.do("meet share my screen")
            check("...so the next share in the call runs without asking",
                  r.status != Status.NEEDS_CONFIRMATION, r.message)
            rig.do("meet leave")
            r = rig.do("meet share my screen")
            check("leaving the call ends that trust", r.status == Status.NEEDS_CONFIRMATION, r.message)
            linux.resolve_confirmation("no")
            ok, said = linux.resolve_confirmation("don't ask again")
            check("'don't ask again' with nothing pending is not an answer", not ok)
    finally:
        rig._restore_helper()
    check("no other confirmation can be switched off by voice", common._TRUSTABLE == {"meet.share"})

    # Without the helper: a NAMED window is the user's pick, never Enter on the first entry.
    helper = ShareHelper(installed=False)
    rig = _share_rig(helper, picker=("hyprland-preview-share-picker", "Share"))
    try:
        with rig:
            rig.do("meet share the lm studio window")
            ok, said = linux.resolve_confirmation("yes")
            check("without the helper, a named window is never auto-picked in the portal",
                  rig.runner.calls.count(ENTER) == 0 and "Choose the lm studio window" in said, said)
            check("...and no request file is written", helper.requests == [])
    finally:
        rig._restore_helper()
    # Without the helper, "my screen" keeps the old behaviour: the portal's first monitor.
    rig = _share_rig(ShareHelper(installed=False), picker=("hyprland-preview-share-picker", "Share"))
    try:
        with rig:
            rig.do("meet share my screen")
            ok, said = linux.resolve_confirmation("yes")
            check("without the helper, 'my screen' focuses the portal picker and presses Enter",
                  any('"address:0xpick"' in d for d in rig.hypr.dispatched)
                  and rig.runner.calls.count(ENTER) >= 1 and "entire screen" in said, said)
    finally:
        rig._restore_helper()

    # M1.3 — a specific monitor, by position left to right.
    monitors = [{"name": "HDMI-A-1", "x": 2000, "y": 0, "focused": False},
                {"name": "eDP-1", "x": 0, "y": 0, "focused": True}]
    helper = ShareHelper(installed=True)
    rig = _share_rig(helper, monitors=monitors)
    try:
        with rig:
            rig.do("meet share my second screen")
            ok, said = linux.resolve_confirmation("yes")
            check("'my second screen' asks the portal for the second monitor from the left",
                  helper.requests == [("monitor", "HDMI-A-1")] and "HDMI-A-1" in said,
                  (helper.requests, said))
            helper.requests.clear()
            rig.bridge.calls.clear()
            rig.do("meet share my screen with audio")
            linux.resolve_confirmation("yes")
            check("M1.4 already presenting + a new share: the old one is stopped first",
                  ("page.click", {"text": "Stop presenting", "nth": 1}) in rig.bridge.calls)
            check("'my screen' is the FOCUSED monitor", helper.requests == [("monitor", "eDP-1")],
                  helper.requests)
            check("M1.5 'with audio' turns on system sound in the page hook (1.4.0)",
                  ("page.setMain", {"key": "__kayraShareAudio", "value": True}) in rig.bridge.calls)
            rig.do("meet share my third screen")
            ok, said = linux.resolve_confirmation("yes")
            check("a screen that does not exist is said so", "I don't see that screen" in said, said)
    finally:
        rig._restore_helper()

    # M1.2 — a tab is chosen in Chrome's own list; Kayra opens it there and says which.
    helper = ShareHelper(installed=True)
    rig = _share_rig(helper)
    try:
        with rig:
            rig.do("meet share the youtube tab")
            ok, said = linux.resolve_confirmation("yes")
            check("a tab share preselects Chrome's tab list and never presses Enter on it",
                  ("page.setMain", {"key": "__kayraShareSurface", "value": "browser"}) in rig.bridge.calls
                  and rig.runner.calls.count(ENTER) == 0 and "youtube tab" in said, said)
    finally:
        rig._restore_helper()

    # Not in a call.
    with WebRig(Bridge(version="1.4.0", tabs=[{"id": 1, "title": "x", "url": "https://x"}])) as rig:
        rig.do("meet share my screen")
        ok, said = linux.resolve_confirmation("yes")
        check("no Meet tab -> 'You're not in a Google Meet call.'", "not in a Google Meet call" in said, said)

    js = open(os.path.join(ROOT, "extension", "kayra-bridge", "meet_main.js"), encoding="utf-8").read()
    check("the page hook adds system audio only when Kayra asked, once",
          "__kayraShareAudio" in js and 'systemAudio = "include"' in js
          and "window.__kayraShareAudio = null" in js)
    manifest = json.load(open(os.path.join(ROOT, "extension", "kayra-bridge", "manifest.json")))
    check("the extension is at least 1.4.0 (system audio in a share)",
          tuple(int(x) for x in manifest["version"].split(".")) >= (1, 4, 0))


def section_share_picker_helper():
    check.section("[13] M1.1 — the portal picker helper (kayra-share-picker)")
    import time
    import tempfile
    import subprocess
    from kayra.automation import share_picker
    h = share_picker.helper()
    listing = ("101[HC>]google-chrome[HT>]Meet - abc[HE>]94006417367040[HA>]"
               "202[HC>]lm-studio[HT>]LM Studio[HE>]94006417368000[HA>]"
               "303[HC>]kitty[HT>]~[HE>]94006417369000[HA>]"
               "404[HC>]kitty[HT>]htop[HE>]94006417370000[HA>]")
    windows = h.parse_sharing_list(listing)
    check("the portal's window list is parsed (handle, class, title, address)",
          len(windows) == 4 and windows[1] == {"handle": "202", "class": "lm-studio",
                                               "title": "LM Studio", "address": 94006417368000},
          windows)
    now = time.time()
    req = {"kind": "window", "address": hex(94006417368000), "class": "x", "title": "y", "created": now}
    check("a window is matched by its Hyprland address", h.select(req, windows, now) == "[SELECTION]/window:202")
    req = {"kind": "window", "address": "0x1", "class": "lm-studio", "title": "LM Studio", "created": now}
    check("...then by class and title", h.select(req, windows, now) == "[SELECTION]/window:202")
    req = {"kind": "window", "address": "0x1", "class": "kitty", "title": "nvim", "created": now}
    check("two windows of one class and no exact title -> the normal picker, never a guess",
          h.select(req, windows, now) is None)
    req = {"kind": "window", "address": hex(94006417368000), "created": now - 120}
    check("a stale request is ignored", h.select(req, windows, now) is None)
    check("a monitor request names the screen",
          h.select({"kind": "monitor", "name": "HDMI-A-1", "created": now}, windows, now)
          == "[SELECTION]/screen:HDMI-A-1")
    check("an older portal without addresses still parses",
          h.parse_sharing_list("7[HC>]a[HT>]b[HE>][HA>]")[0]["address"] is None)

    with tempfile.TemporaryDirectory() as root:
        env = {"XDG_RUNTIME_DIR": os.path.join(root, "run"), "XDG_CONFIG_HOME": os.path.join(root, "cfg")}
        os.makedirs(env["XDG_RUNTIME_DIR"])

        class W:
            address, cls, initial_class, title = "0x" + format(94006417368000, "x"), "lm-studio", "lm-studio", "LM Studio"
        path = share_picker.request_window(W, env)
        check("the request file is private (0600, in a 0700 dir)",
              oct(os.stat(path).st_mode & 0o777) == "0o600"
              and oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700")
        fake = os.path.join(root, "normal-picker")
        with open(fake, "w") as handle:
            handle.write("#!/bin/sh\necho normal-picker-ran\n")
        os.chmod(fake, 0o755)
        os.makedirs(os.path.join(env["XDG_CONFIG_HOME"], "kayra"))
        with open(h.fallback_record(env), "w") as handle:
            handle.write(fake + "\n")
        run_env = dict(os.environ, **env, XDPH_WINDOW_SHARING_LIST=listing)
        out = subprocess.run([sys.executable, share_picker.helper_file()], env=run_env,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        check("run by the portal with a request pending, it prints the selection", out == "[SELECTION]/window:202", out)
        check("...and the request is consumed (answered once)", not os.path.exists(path))
        out = subprocess.run([sys.executable, share_picker.helper_file()], env=run_env,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        check("with no request it runs the picker that was configured before", out == "normal-picker-ran", out)

        conf = share_picker.config_path(env)
        os.makedirs(os.path.dirname(conf))
        original = ("screencopy {\n    allow_token_by_default = true\n"
                    "    custom_picker_binary = hyprland-preview-share-picker\n}\n")
        with open(conf, "w") as handle:
            handle.write(original)
        os.remove(h.fallback_record(env))
        check("status before install", not share_picker.installed(env))
        said = share_picker.install(env)
        text = open(conf).read()
        check("install points xdph at the helper and keeps the other settings",
              share_picker.installed(env) and "allow_token_by_default = true" in text
              and text.count("custom_picker_binary") == 1, text)
        check("...remembers the old picker as the fallback, and backs up xdph.conf",
              open(h.fallback_record(env)).read().strip() == "hyprland-preview-share-picker"
              and os.path.exists(conf + ".kayra-backup") and "hyprland-preview-share-picker" in said, said)
        share_picker.uninstall(env)
        check("uninstall puts the old picker back exactly", open(conf).read() == original, open(conf).read())
        os.remove(conf)
        share_picker.install(env)
        check("with no xdph.conf, install writes a screencopy block",
              share_picker.installed(env) and open(conf).read().startswith("screencopy {"))
    check("the helper is executable", os.access(share_picker.helper_file(), os.X_OK))
    source = open(share_picker.helper_file(), encoding="utf-8").read()
    check("the helper imports nothing from Kayra (the portal runs it outside the venv)",
          "kayra" not in "".join(l for l in source.splitlines() if l.startswith(("import", "from"))))


def section_audit_log():
    check.section("[14] M0.6 — the suites never write the developer's audit log")
    from kayra.automation import policy
    real = os.path.join(ROOT, "logs", "automation.log")
    check("KAYRA_AUDIT_LOG is set for this suite", bool(os.environ.get("KAYRA_AUDIT_LOG")))
    logger = policy._audit_logger()
    target = getattr(logger.handlers[0], "baseFilename", "") if logger else ""
    check("the audit logger is off or points outside the project's logs/",
          logger is False or os.path.abspath(target) != os.path.abspath(real), target)
    return os.path.getsize(real) if os.path.exists(real) else None

# ┌────────────────────────────────────────────────────────────────────────┐
# │   PART 2c — B2 by site / but pinned, B4 groups, B5 windows, C3, C4      │
# └────────────────────────────────────────────────────────────────────────┘

class GroupBridge(TabBridge):
    """TabBridge at 1.5.0, with five YouTube tabs, tab groups, windows and a result list."""

    def __init__(self, version="1.6.0"):
        super().__init__(version=version)
        for i, title in enumerate(("Lecture 4 - YouTube", "Lecture 5 - YouTube", "Lecture 6 - YouTube")):
            self.tabs.append({"id": 30 + i, "title": title, "url": f"https://www.youtube.com/watch?v=l{i}",
                              "windowId": 1, "active": False})
        for t in self.tabs:
            t.setdefault("groupId", -1)
        self.groups = [{"id": 7, "title": "Research", "color": "grey", "collapsed": False,
                        "windowId": 1, "tabs": 0}]
        self.tabs[2]["groupId"] = 7          # Docs (active) is in Research
        self.results = [{"title": f"Result {i}", "url": f"https://r.example/{i}"} for i in range(1, 6)]
        self.marks = 0

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "groups.list":
            for g in self.groups:
                g["tabs"] = sum(t.get("groupId") == g["id"] for t in self.tabs)
            return [dict(g) for g in self.groups]
        if cmd.startswith(("groups.", "windows.", "page.results", "page.highlight")) or \
                cmd in ("tabs.navigate", "tabs.open"):
            self.calls.append((cmd, args))
            if cmd == "groups.create":
                gid = args.get("groupId") or 99
                if gid == 99:
                    self.groups.append({"id": 99, "title": args.get("title", ""), "color": "grey",
                                        "collapsed": False, "windowId": 1})
                for t in self.tabs:
                    if t["id"] in args["tabIds"]:
                        t["groupId"] = gid
                return {"groupId": gid, "tabs": len(args["tabIds"])}
            if cmd == "groups.update":
                g = next(g for g in self.groups if g["id"] == args["groupId"])
                g.update({k: v for k, v in args.items() if k in ("title", "color", "collapsed")})
                return dict(g)
            if cmd == "groups.ungroup":
                hit = [t for t in self.tabs if t.get("groupId") == args.get("groupId")]
                for t in hit:
                    t["groupId"] = -1
                return {"ungrouped": len(hit)}
            if cmd == "windows.detach":
                return {"windowId": 5, "title": "Docs"}
            if cmd == "windows.merge":
                return {"moved": 1, "windows": 1}
            if cmd == "page.results":
                nth = args.get("nth", 0)
                if not nth:
                    return {"site": "google", "count": len(self.results), "results": self.results}
                index = nth - 1 if nth > 0 else len(self.results) + nth
                ok = 0 <= index < len(self.results)
                return {"site": "google", "count": len(self.results),
                        "result": self.results[index] if ok else None}
            if cmd == "page.highlight":
                if args.get("clear"):
                    self.marks = 0
                    return {"cleared": True}
                if args.get("text"):
                    self.marks = 3 if args["text"] == "pricing" else 0
                    return {"count": self.marks, "index": 1 if self.marks else 0}
                return {"count": self.marks, "index": 2 if self.marks else 0}
            return {"id": 9, "url": args.get("url")}
        return super().call(cmd, timeout=timeout, **args)


def section_part_2c():
    check.section("[15] Part 2c — close by site, groups, windows, the Nth result, find with highlight")
    table = (
        ("browser open the third result", "page_result", "3"),
        ("open the third result", "page_result", "3"),
        ("browser play the second video", "page_result", "2"),
        ("browser open the last result in a new tab", "page_result", "-1"),
        ("browser open result 4", "page_result", "4"),
        ("browser click the second result", "page_result", "2"),
        ("browser what are the results", "page_results", "current"),
        ("browser find pricing on this page", "page_find", "pricing"),
        ("browser highlight transformer", "page_find", "transformer"),
        ("browser next match", "page_find_step", "next"),
        ("browser previous match", "page_find_step", "previous"),
        ("browser clear highlights", "page_find_clear", "current"),
        ("browser close all youtube tabs", "tab_close", "youtube"),
        ("browser close all but pinned", "tabs_close_unpinned", "unpinned"),
        ("browser close all tabs except pinned", "tabs_close_unpinned", "unpinned"),
        ("browser group my college tabs", "tabs_group", "college"),
        ("browser group the youtube tabs as music", "tabs_group", "youtube"),
        ("browser list my tab groups", "groups_list", "all"),
        ("browser add this tab to the research group", "group_add", "research"),
        ("browser rename the research group to thesis", "group_rename", "research"),
        ("browser rename this group to thesis", "group_rename", "current"),
        ("browser make the research group blue", "group_color", "research"),
        ("browser collapse the research group", "group_collapse", "research"),
        ("browser expand all groups", "group_collapse", "all"),
        ("browser ungroup the research group", "group_ungroup", "research"),
        ("browser ungroup all", "group_ungroup", "all"),
        ("browser close the research group", "group_close", "research"),
        ("browser move this tab to a new window", "tab_detach", "current"),
        ("browser move this tab to the other screen", "tab_detach", "current"),
        ("browser merge my chrome windows", "windows_merge", "all"),
        # unchanged neighbours
        ("browser close the third tab", "tab_close", "#3"),
        ("browser find the tab about rust", "tab_find", "rust"),
        ("browser click on sign in", "page_click", "sign in"),
    )
    for token, action, target in table:
        a = n(token)
        check(f"'{token}' -> browser.{action} {target!r}",
              a is not None and a.domain == "browser" and a.action == action and a.target == target,
              f"{a.key if a else None} {a.target if a else ''}")
    check("'open the first email' is still Gmail", n("open the first email").key == "email.open")
    check("the new shapes are in the understanding catalog",
          all(p in interpreter.CATALOG for p in ("browser open the third result", "browser next match",
                                                 "browser group my <topic> tabs",
                                                 "browser merge my chrome windows")))

    with WebRig(GroupBridge()) as rig:
        r = rig.do("browser close all youtube tabs")
        check("B2: five YouTube tabs ASK first, naming some of them",
              r.status == Status.NEEDS_CONFIRMATION and "5 youtube tabs" in r.message
              and rig.bridge.last("tabs.close") is None, r.message)
        rig.bridge.tabs.append({"id": 40, "title": "New - YouTube", "url": "https://youtube.com/x",
                                "windowId": 1, "active": False, "groupId": -1})
        ok, said = linux.resolve_confirmation("yes")
        check("...'yes' closes exactly the five it found (not one opened since)",
              sorted(rig.bridge.last("tabs.close")["tabIds"]) == [12, 14, 30, 31, 32]
              and "Closed 5 tabs" in said, (rig.bridge.last("tabs.close"), said))
    with WebRig(GroupBridge()) as rig:
        rig.bridge.tabs = [t for t in rig.bridge.tabs if t["id"] not in (30, 31, 32)]
        r = rig.do("browser close all youtube tabs")
        check("B2: three or fewer close at once", r.ok and rig.bridge.last("tabs.close") == {"tabIds": [12, 14]},
              r.message)
        r = rig.do("browser close all but pinned")
        check("B2: 'all but pinned' keeps the pinned Gmail tab (4 tabs: closed at once)",
              r.ok and 11 not in rig.bridge.last("tabs.close")["tabIds"]
              and 21 not in rig.bridge.last("tabs.close")["tabIds"], rig.bridge.last("tabs.close"))
        linux.resolve_confirmation("no")

    with WebRig(GroupBridge()) as rig:
        r = rig.do("browser group the youtube tabs as music")
        created = rig.bridge.last("groups.create")
        check("B4: matching tabs become one named group",
              created and sorted(created["tabIds"]) == [12, 14, 30, 31, 32] and created["title"] == "Music"
              and "Grouped 5 tabs as Music" in r.message, (created, r.message))
        r = rig.do("browser make the research group blue")
        check("B4: colour", rig.bridge.last("groups.update") == {"groupId": 7, "color": "blue"}
              and "blue" in r.message, r.message)
        r = rig.do("browser rename this group to thesis")
        check("B4: 'this group' is the active tab's group",
              rig.bridge.last("groups.update") == {"groupId": 7, "title": "thesis"}, r.message)
        r = rig.do("browser collapse the thesis group")
        check("B4: collapse", rig.bridge.last("groups.update") == {"groupId": 7, "collapsed": True}, r.message)
        r = rig.do("browser expand all groups")
        check("B4: expand all", rig.bridge.last("groups.update")["collapsed"] is False and "Expanded" in r.message,
              r.message)
        r = rig.do("browser list my tab groups")
        check("B4: list names the groups and their tab counts", "Thesis" in r.message or "thesis" in r.message,
              r.message)
        r = rig.do("browser ungroup the thesis group")
        check("B4: ungroup", rig.bridge.last("groups.ungroup") == {"groupId": 7}, r.message)
        r = rig.do("browser collapse the cooking group")
        check("B4: an unknown group is said so", not r.ok and "cooking" in r.message, r.message)
    with WebRig(GroupBridge()) as rig:
        rig.do("browser group my transformer tabs")
        check("B4: a topic not in any title is found in the page text",
              rig.bridge.last("groups.create")["tabIds"] == [13], rig.bridge.last("groups.create"))
        r = rig.do("browser group my netflix tabs")
        check("...and none is said so", not r.ok, r.message)
    with WebRig(GroupBridge(version="1.4.0")) as rig:
        r = rig.do("browser group the youtube tabs")
        check("an extension older than 1.5.0 is never sent a 1.5.0 command",
              rig.bridge.last("groups.create") is None and not r.ok, r.message)

    hypr = FakeHyprland([client("0xa", "google-chrome", "Docs - Google Chrome", focus=0)], active="0xa",
                        monitors=[{"name": "eDP-1", "x": 0, "y": 0, "focused": True},
                                  {"name": "HDMI-A-1", "x": 2000, "y": 0, "focused": False}])
    with WebRig(GroupBridge(), hypr=hypr) as rig:
        r = rig.do("browser move this tab to a new window")
        check("B5: a tab gets its own window", rig.bridge.last("windows.detach") == {} and "own window" in r.message,
              r.message)
        r = rig.do("browser move this tab to the other screen")
        check("B5: '… to the other screen' moves the new window with Hyprland",
              any("move" in d and "0xa" in d for d in rig.hypr.dispatched) and "other screen" in r.message,
              (rig.hypr.dispatched, r.message))
        r = rig.do("browser merge my chrome windows")
        check("B5: merge windows", "Moved 1 tab from 1 other window" in r.message, r.message)

        r = rig.do("open the third result")
        check("C3: the third result is opened in this tab",
              rig.bridge.last("tabs.navigate") == {"url": "https://r.example/3"} and "third result" in r.message,
              r.message)
        r = rig.do("browser open the last result in a new tab")
        check("C3: '… in a new tab' opens next to this one",
              rig.bridge.last("tabs.open") == {"url": "https://r.example/5", "where": "next"}, r.message)
        r = rig.do("browser open the ninth result")
        check("C3: past the end is said so", not r.ok and "only 5" in r.message, r.message)
        r = rig.do("browser what are the results")
        check("C3: the results are read out, numbered", "1. Result 1" in r.message, r.message)

        r = rig.do("browser find pricing on this page")
        check("C4: find highlights and says how many", rig.bridge.last("page.highlight") == {"text": "pricing"}
              and "3 matches" in r.message, r.message)
        r = rig.do("browser next match")
        check("C4: next match steps", rig.bridge.last("page.highlight") == {"step": 1} and "2 of 3" in r.message,
              r.message)
        r = rig.do("browser clear highlights")
        check("C4: clear", rig.bridge.last("page.highlight") == {"clear": True}, r.message)
        r = rig.do("browser next match")
        check("C4: next with nothing highlighted is said so", not r.ok, r.message)

    manifest = json.load(open(os.path.join(ROOT, "extension", "kayra-bridge", "manifest.json")))
    check("the extension has tabGroups (1.5.0+) and never debugger",
          "tabGroups" in manifest["permissions"] and "debugger" not in manifest["permissions"])

# ┌────────────────────────────────────────────────────────────────────────┐
# │  PART 3 — questions, comments, video, translate, bookmarks, history,   │
# │           downloads, "it", search here, what can I do here (§10)       │
# └────────────────────────────────────────────────────────────────────────┘

class PageBridge(GroupBridge):
    """GroupBridge at 1.6.0 with an active page of a chosen address, bookmarks, history and
    downloads."""

    def __init__(self, version="1.7.0", url="https://docs.google.com/", comments=None,
                 transcript="", selection="", downloads=None):
        super().__init__(version=version)
        self.url, self.selection = url, selection
        next(t for t in self.tabs if t["id"] == 13)["url"] = url
        self.comment_list = ["Great explanation of attention.", "The audio is a bit low.",
                             "Helped me pass my exam."] if comments is None else comments
        self.transcript, self.transcript_calls = transcript, 0
        self.bookmarks = [{"id": "b1", "title": "The Rust Book", "url": "https://doc.rust-lang.org/book/"},
                          {"id": "b2", "title": "Rust by Example", "url": "https://doc.rust-lang.org/rbe/"}]
        self.download_list = downloads if downloads is not None else [
            {"id": 1, "filename": "/home/u/Downloads/report.pdf", "state": "complete", "exists": True,
             "danger": "safe"},
            {"id": 2, "filename": "/home/u/Downloads/old.zip", "state": "complete", "exists": True,
             "danger": "safe"}]

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "tabs.active":
            tab = dict(next(t for t in self.tabs if t["id"] == 13))
            tab["url"] = self.url
            return tab
        if cmd == "page.text" and "tabId" not in args:
            self.calls.append((cmd, args))
            return {"title": "Pricing - Acme", "url": self.url,
                    "text": "Acme costs 10 dollars a month for teams. " * 5}
        if cmd.startswith(("page.comments", "page.transcript", "page.selection", "bookmarks.",
                           "history.", "downloads.", "page.type")):
            self.calls.append((cmd, args))
            if cmd == "page.comments":
                return {"kind": "comments", "comments": list(self.comment_list)}
            if cmd == "page.transcript":
                if "youtube" not in self.url:
                    return {"supported": False}
                self.transcript_calls += 1
                ready = self.transcript_calls > 2         # the panel loads a moment later
                return {"supported": True, "title": "Attention explained",
                        "description": "A talk.", "text": self.transcript if ready else ""}
            if cmd == "page.selection":
                return self.selection
            if cmd == "bookmarks.add":
                return {"added": True, "title": "Pricing - Acme", "folder": args.get("folder", "")}
            if cmd == "bookmarks.find":
                want = args.get("text", "").lower()
                return [b for b in self.bookmarks if want in b["title"].lower()]
            if cmd == "bookmarks.remove":
                self.bookmarks = [b for b in self.bookmarks if b["id"] not in args["ids"]]
                return {"removed": len(args["ids"])}
            if cmd == "history.deleteRange":
                return {"deleted": True}
            if cmd == "history.search":
                return [{"title": "Rust lifetimes", "url": "https://x"}, {"title": "Docs", "url": "https://y"}]
            if cmd == "downloads.list":
                return [dict(d) for d in self.download_list]
            if cmd == "page.type":
                return {"typed": True}
            return {"ok": True}
        return super().call(cmd, timeout=timeout, **args)


def section_part_3():
    check.section("[16] Part 3 — ask the page, comments, video, translate, bookmarks, history, "
                  "downloads, 'it', search here, what can I do here")
    table = (
        ("browser what does this page say about pricing", "page_ask", "current"),
        ("browser read the comments", "page_comments", "current"),
        ("browser summarize the reviews", "page_comments", "current"),
        ("browser what do people say about this", "page_comments", "current"),
        ("browser summarize this video", "video_summary", "current"),
        ("summarize this video", "video_summary", "current"),
        ("browser translate this page to hindi", "page_translate", "hindi"),
        ("translate this page", "page_translate", "english"),
        ("browser explain the selected text", "selection_explain", "current"),
        ("browser translate the selected text to french", "selection_translate", "french"),
        ("browser bookmark this page", "bookmark_add", "current"),
        ("bookmark this page", "bookmark_add", "current"),
        ("browser bookmark this page in the work folder", "bookmark_add", "current"),
        ("browser search my bookmarks for rust", "bookmarks_search", "rust"),
        ("browser delete the bookmark for rust", "bookmark_delete", "rust"),
        ("browser clear today's history", "history_clear", "today"),
        ("browser clear the last hour of history", "history_clear", "hour"),
        ("browser what did i visit today", "history_list", "today"),
        ("browser what did i download", "downloads_list", "recent"),
        ("browser open my last download", "download_open", "latest"),
        ("browser show it in folder", "download_show", "latest"),
        ("browser pause the download", "download_control", "pause"),
        ("browser cancel the download", "download_control", "cancel"),
        ("browser close it", "tab_close", "it"),
        ("browser go back to it", "tab_switch", "it"),
        ("browser search for ravi here", "search_here", "ravi"),
        ("browser search this sheet for ravi", "search_here", "ravi"),
        ("browser what can i do here", "page_help", "current"),
        ("what can i do here", "page_help", "current"),
        # unchanged neighbours
        ("browser open my bookmark for rust", "bookmark_open", "rust"),
        ("browser summarize this page", "page_summarize", "current"),
        ("browser find the tab about rust", "tab_find", "rust"),
    )
    for token, action, target in table:
        a = n(token)
        check(f"'{token}' -> browser.{action} {target!r}",
              a is not None and a.domain == "browser" and a.action == action and a.target == target,
              f"{a.key if a else None} {a.target if a else ''}")
    check("'bookmark this page' no longer tries to OPEN a bookmark called 'this page'",
          n("browser bookmark this page").action == "bookmark_add")
    check("'browser delete my browsing history' clears TODAY, never all time",
          n("browser delete my browsing history").target == "today")
    check("the new shapes are in the understanding catalog",
          all(p in interpreter.CATALOG for p in ("browser summarize this video", "browser close it",
                                                 "browser open my last download",
                                                 "browser what can i do here")))

    with WebRig(PageBridge()) as rig, Models("Acme costs 10 dollars a month for teams.") as models:
        r = rig.do("browser what does this page say about pricing")
        check("C5: the answer comes from the page's text",
              r.ok and "10 dollars" in r.message and "Acme costs" in str(models.asked[-1]), r.message)
        r = rig.do("browser read the comments")
        check("C6: the top comments are read out", "Great explanation" in r.message, r.message)
        rig.do("browser summarize the reviews")
        check("C6: 'summarize' asks a model about the comments", "audio is a bit low" in str(models.asked[-1]))
        r = rig.do("browser translate this page to hindi")
        nav = rig.bridge.last("tabs.navigate") or {}
        check("C8: the page opens through Google Translate in Hindi, this tab",
              "translate.google.com/translate?sl=auto&tl=hi&u=https%3A%2F%2Fdocs.google.com%2F" in nav.get("url", "")
              and "Hindi" in r.message, (nav, r.message))
        r = rig.do("browser translate this page to klingon")
        check("...an unknown language is said so", not r.ok and "klingon" in r.message, r.message)
        r = rig.do("browser explain the selected text")
        check("C8: no selection is said so", not r.ok and "selected" in r.message, r.message)
        r = rig.do("browser bookmark this page in the work folder")
        check("C9: bookmark into a folder", rig.bridge.last("bookmarks.add") == {"folder": "work"}
              and "in work" in r.message, r.message)
        r = rig.do("browser delete the bookmark for rust")
        check("C9: deleting ALWAYS asks, naming the bookmarks", r.status == Status.NEEDS_CONFIRMATION
              and "The Rust Book" in r.message and rig.bridge.last("bookmarks.remove") is None, r.message)
        ok, said = linux.resolve_confirmation("yes")
        check("...'yes' removes exactly those", rig.bridge.last("bookmarks.remove") == {"ids": ["b1", "b2"]}
              and "Removed 2" in said, said)
        r = rig.do("browser clear today's history")
        check("C10: clearing history ALWAYS asks", r.status == Status.NEEDS_CONFIRMATION
              and "can't be undone" in r.message and rig.bridge.last("history.deleteRange") is None, r.message)
        ok, said = linux.resolve_confirmation("yes")
        start = (rig.bridge.last("history.deleteRange") or {}).get("startTime", 0)
        import datetime as _dt
        midnight = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        check("...from midnight today, nothing older", start == int(midnight.timestamp() * 1000), start)
        r = rig.do("browser what did i download")
        check("C11: downloads are listed by file name", "report.pdf" in r.message, r.message)
        r = rig.do("browser open my last download")
        check("C11: opening a download ALWAYS asks, naming it", r.status == Status.NEEDS_CONFIRMATION
              and "report.pdf" in r.message, r.message)
        ok, said = linux.resolve_confirmation("yes")
        check("...then opens that file", ["xdg-open", "/home/u/Downloads/report.pdf"] in rig.runner.spawned,
              rig.runner.spawned)
        r = rig.do("browser show it in folder")
        check("C11: show in folder", rig.bridge.last("downloads.show") == {"id": 1}, r.message)
        r = rig.do("browser pause the download")
        check("C11: nothing downloading is said so", not r.ok and "Nothing" in r.message, r.message)
        r = rig.do("browser what can i do here")
        check("G4: Docs gets Docs hints", "Docs and Sheets" in r.message, r.message)

    exe = [{"id": 5, "filename": "/home/u/Downloads/install.sh", "state": "complete", "exists": True,
            "danger": "safe"}]
    with WebRig(PageBridge(downloads=exe)) as rig:
        r = rig.do("browser open my last download")
        check("C11: a program is never opened, not even after a yes", r.status == Status.BLOCKED
              and not rig.runner.spawned, r.message)
    busy = [{"id": 7, "filename": "/d/big.iso", "state": "in_progress", "paused": False, "bytes": 50, "total": 200}]
    with WebRig(PageBridge(downloads=busy)) as rig:
        r = rig.do("browser what did i download")
        check("C11: an active download says its progress", "25 percent" in r.message, r.message)
        r = rig.do("browser pause the download")
        check("C11: pause the one downloading", rig.bridge.last("downloads.pause") == {"id": 7}
              and "Paused big.iso" in r.message, r.message)

    with WebRig(PageBridge(url="https://www.youtube.com/watch?v=1", transcript="Attention lets a model "
                           "look at every word.")) as rig, Models("A short talk on attention.") as models:
        r = rig.do("browser summarize this video")
        check("C6: the YouTube transcript is opened, waited for (not slept) and summarized",
              r.message == "A short talk on attention." and "look at every word" in str(models.asked[-1])
              and rig.bridge.transcript_calls >= 3, (r.message, rig.bridge.transcript_calls))
        r = rig.do("browser search for lofi here")
        check("G2: on YouTube, search YouTube", rig.bridge.last("tabs.navigate") ==
              {"url": "https://www.youtube.com/results?search_query=lofi"} and "YouTube" in r.message, r.message)
    with WebRig(PageBridge(url="https://www.youtube.com/watch?v=1", transcript="")) as rig, \
            Models("Going by the description, it's a talk.") as models:
        r = rig.do("browser summarize this video")
        check("C6: no transcript -> the description, said so by the model prompt",
              "No transcript" in str(models.asked[-1]), str(models.asked[-1])[:200])

    for url, expected in (
            ("https://mail.google.com/mail/u/0/#inbox", "https://mail.google.com/mail/u/0/#search/ravi%20kumar"),
            ("https://github.com/rust-lang/rust/issues", "https://github.com/rust-lang/rust/search?q=ravi+kumar"),
            ("https://www.amazon.in/dp/B0", "https://amazon.in/s?k=ravi+kumar"),
            ("https://drive.google.com/drive/my-drive", "https://drive.google.com/drive/search?q=ravi+kumar")):
        with WebRig(PageBridge(url=url)) as rig:
            rig.do("browser search for ravi kumar here")
            check(f"G2: search on {url.split('/')[2]} uses its own search",
                  rig.bridge.last("tabs.navigate") == {"url": expected}, rig.bridge.last("tabs.navigate"))
    with WebRig(PageBridge(url="https://docs.google.com/spreadsheets/d/x/edit")) as rig:
        r = rig.do("browser search this sheet for ravi")
        keys = [(argv, given) for argv, given in rig.runner.ran if argv[:1] == ["wtype"]]
        check("G2: in a Sheet, the sheet's own find (Ctrl+F), then the name typed",
              len(keys) == 2 and keys[0][0] == linux.wtype_argv("ctrl+f")
              and keys[1] == (["wtype", "-"], "ravi") and "this sheet" in r.message, (keys, r.message))
    with WebRig(PageBridge(url="https://example.com/blog")) as rig:
        r = rig.do("browser search for ravi here")
        check("G2: any other site: its own search box", rig.bridge.last("page.type") ==
              {"field": "search", "text": "ravi", "submit": True}, r.message)

    # G1 — "it" is the tab Kayra last opened or switched to.
    with WebRig(PageBridge()) as rig:
        saved_clock = web_macros.CLOCK
        now = [1000.0]
        web_macros.CLOCK = lambda: now[0]
        try:
            web_macros.LAST_TAB.update(id=None, title="", at=0.0)
            r = rig.do("browser close it")
            check("G1: 'close it' with nothing remembered asks for a name", not r.ok and "which tab" in r.message,
                  r.message)
            rig.do("browser switch to the github tab")
            rig.do("browser switch to the third tab")      # by position: not remembered as "it"
            r = rig.do("browser go back to it")
            check("G1: 'go back to it' returns to the tab Kayra switched to by name",
                  rig.bridge.last("tabs.activate") == {"tabId": 15}, (rig.bridge.last("tabs.activate"), r.message))
            r = rig.do("browser close it")
            check("G1: 'close it' closes THAT tab", rig.bridge.last("tabs.close") == {"tabIds": [15]}
                  and "GitHub" in r.message, r.message)
            rig.do("browser switch to the github tab")
            now[0] += 3600
            r = rig.do("browser close it")
            check("G1: an hour later 'it' has expired", not r.ok, r.message)
        finally:
            web_macros.CLOCK = saved_clock
            web_macros.LAST_TAB.update(id=None, title="", at=0.0)

    with WebRig(PageBridge(version="1.5.0")) as rig:
        r = rig.do("browser read the comments")
        check("an extension older than 1.6.0 is never sent a 1.6.0 command",
              rig.bridge.last("page.comments") is None and not r.ok, r.message)
    manifest = json.load(open(os.path.join(ROOT, "extension", "kayra-bridge", "manifest.json")))
    check("the extension has downloads (1.6.0+) and still no debugger",
          "downloads" in manifest["permissions"] and "debugger" not in manifest["permissions"])

# ┌────────────────────────────────────────────────────────────────────────┐
# │       PART 4 — Google Workspace (E1–E8), names matched locally (D3)     │
# └────────────────────────────────────────────────────────────────────────┘

SHEET = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=42"
DOC = "https://docs.google.com/document/d/1DoCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit"
CSV = ("Name,Amount,Status\nRavi,500,paid\nAna,\"1,250.50\",due\nMeera,₹300,paid\nTotal row,n/a,\n")


class WorkspaceBridge(PageBridge):
    """PageBridge at 1.7.0: file lists, in-page fetches, the Name Box, Calendar, Keep, Meet."""

    def __init__(self, version="1.8.0", url=SHEET, files=None, csv=CSV, doc="Offer letter. Start date "
                 "is 1 December. The stipend is 40,000 a month. Report to the Bengaluru office.", calendar=None, clickable=()):
        super().__init__(version=version, url=url)
        self.files = files if files is not None else [
            {"id": "a" * 30, "title": "Budget 2026", "url": "https://docs.google.com/spreadsheets/d/" + "a" * 30 + "/edit"},
            {"id": "b" * 30, "title": "Budget 2025 (old)", "url": "https://docs.google.com/spreadsheets/d/" + "b" * 30 + "/edit"},
            {"id": "c" * 30, "title": "Team roster", "url": "https://docs.google.com/spreadsheets/d/" + "c" * 30 + "/edit"}]
        self.csv, self.doc = csv, doc
        self.calendar = calendar or {"title": "Standup 10:00", "events": 3, "meet": ""}
        self.extra = set(clickable)
        self.fetched = []

    def call(self, cmd, timeout=10.0, **args):
        if cmd in ("tabs.navigate", "tabs.open") and args.get("url"):
            self.calls.append((cmd, args))
            if args.get("where") != "next":
                self.url = args["url"]
            return {"id": 13, "url": args["url"], "title": "x"}
        if cmd == "page.docList":
            self.calls.append((cmd, args))
            self.doclist_calls = getattr(self, "doclist_calls", 0) + 1
            return list(self.files)
        if cmd == "page.fetchText":
            self.calls.append((cmd, args))
            self.fetched.append(args["url"])
            if "/spreadsheets/" in args["url"]:
                return {"ok": True, "text": self.csv}
            return {"ok": True, "text": self.doc}
        if cmd == "page.focus":
            self.calls.append((cmd, args))
            return {"focused": True}
        if cmd == "calendar.next":
            self.calls.append((cmd, args))
            out = dict(self.calendar)
            if args.get("open"):
                out["meet"] = self.calendar.get("link", "")
            return out
        if cmd == "page.click" and args.get("text") in {"Rename", "Share", "Take a note"} | self.extra:
            self.calls.append((cmd, args))
            return {"clicked": True, "label": args["text"]}
        return super().call(cmd, timeout=timeout, **args)


def section_part_4():
    check.section("[17] Part 4 — Google Workspace: Sheets, Docs, Slides, Drive, Gmail, Calendar, Meet, "
                  "Keep, Forms, Classroom")
    table = (
        ("open my budget sheet", "browser.ws_open", "budget"),
        ("browser open my budget sheet", "browser.ws_open", "budget"),
        ("browser open the doc about the internship", "browser.ws_open", "internship"),
        ("browser open my thesis deck", "browser.ws_open", "thesis"),
        ("browser search my docs for offer letter", "browser.ws_search", "offer letter"),
        ("browser find the pdf ravi sent me", "browser.ws_search", "ravi"),
        ("browser create a new sheet called Expenses 2026", "browser.ws_create", "sheet"),
        ("create a new doc", "browser.ws_create", "doc"),
        ("browser go to cell B12", "browser.sheet_cell", "B12"),
        ("browser what's the total of column C", "browser.sheet_calc", "c"),
        ("browser what is the average of the amount column", "browser.sheet_calc", "amount"),
        ("browser how many rows are there", "browser.sheet_calc", "rows"),
        ("browser read row 5", "browser.sheet_row", "5"),
        ("browser what are the columns", "browser.sheet_row", "1"),
        ("browser share this doc with ana@x.com", "browser.ws_share", "ana@x.com"),
        ("browser start the slideshow", "browser.slides_present", "current"),
        ("next slide", "browser.slides_nav", "next"),
        ("browser end the slideshow", "browser.slides_nav", "end"),
        ("browser open my recent files", "browser.ws_recent", "recent"),
        ("browser download this file", "browser.ws_download", "current"),
        ("take a note buy milk", "browser.keep_note", "keep"),
        ("browser open the responses of my feedback form", "browser.form_responses", "feedback"),
        ("browser what assignments are due", "browser.classroom_due", "due"),
        ("email search for fee receipt", "email.search", "fee receipt"),
        ("email show unread from this week", "email.search", "is:unread newer_than:7d"),
        ("email compose", "email.compose", "new"),
        ("email open my work label", "email.label", "work"),
        ("calendar what's my next meeting", "calendar.next", "next"),
        ("calendar join my next meeting", "calendar.next", "next"),
        ("meet join my next meeting", "calendar.next", "next"),
        ("calendar friday", "calendar.agenda", "friday"),
        ("meet admit everyone", "meet.admit", "everyone"),
        # neighbours that must not move
        ("open google sheets", "browser.open_url", "google sheets"),
        ("open the report file", "app.open", "the report file"),
        ("email read latest", "email.read", "latest"),
        ("meet join abc-defg-hij", "meet.join", "abc-defg-hij"),
    )
    for token, key, target in table:
        a = n(token)
        check(f"'{token}' -> {key} {target!r}", a is not None and a.key == key and a.target == target,
              f"{a.key if a else None} {a.target if a else ''}")
    from kayra.intelligence import llm_engine as E
    check("'next slide' / 'previous slide' are exact commands (no model): the local model read them as chat",
          E.fast_intent("Next slide.") == "browser next slide" and E.fast_intent("previous slide") ==
          "browser previous slide" and E.fast_intent("what is the next slide about") is None)
    check("Workspace shapes are in the understanding catalog",
          all(p in interpreter.CATALOG for p in ("browser open my <name> sheet", "browser read row <n>",
                                                 "email search for <text>", "calendar join my next meeting")))

    # Local, deterministic name matching (D3): nothing goes to a model.
    rank = web_macros.rank_titles
    items = [{"title": "Budget 2026"}, {"title": "Budget 2025 (old)"}, {"title": "Team roster"}]
    check("ranking: 'budget 2026' -> exactly that one first", rank("budget 2026", items)[0][1]["title"] == "Budget 2026")
    check("ranking: an unrelated name ranks nothing", rank("holiday photos", items) == [])

    with WebRig(WorkspaceBridge()) as rig, Models("SHOULD NOT BE ASKED") as models:
        r = rig.do("open my budget sheet")
        check("E1: two equally good 'Budget' sheets -> asks which (no guess)",
              r.status == Status.AMBIGUOUS and "Budget 2026" in r.message and "Budget 2025" in r.message, r.message)
        check("...and the names were never sent to a model (D3)", models.asked == [], models.asked)
        opened = [a for c, a in rig.bridge.calls if c == "tabs.open"]
        check("...searched Sheets' own list for the name",
              opened and opened[0]["url"] == "https://docs.google.com/spreadsheets/u/0/?q=budget", opened)
        r = rig.do("browser open my budget 2026 sheet")
        check("E1: an exact name opens it, and the page is waited for (G5)", rig.bridge.last("tabs.navigate") ==
              {"url": "https://docs.google.com/spreadsheets/d/" + "a" * 30 + "/edit", "wait": True, "timeoutMs": 20000} and "Budget 2026" in r.message,
              r.message)
        r = rig.do("browser open my holiday sheet")
        check("E1: no match is said so", not r.ok and "holiday" in r.message, r.message)
        waits = [a for c, a in rig.bridge.calls if c == "page.docList"]
        check("V9: from 1.8.0 the file list is ONE call that waits inside the browser (waitMs)",
              all(a.get("waitMs") == 6000 for a in waits) and rig.bridge.doclist_calls == len(waits) == 4, waits)
        check("V11: a name Google's search does not find is looked for once more in the recent list",
              [a["url"] for c, a in rig.bridge.calls if c in ("tabs.open", "tabs.navigate")][-2:] ==
              ["https://docs.google.com/spreadsheets/u/0/?q=holiday", "https://docs.google.com/spreadsheets/u/0/?q="],
              [a["url"] for c, a in rig.bridge.calls if c in ("tabs.open", "tabs.navigate")])
        tabs_open = [a for c, a in rig.bridge.calls if c == "tabs.open"]
        check("V9: a tab already on the Sheets list is reused (search 2); an OPEN sheet is never "
              "navigated away from (search 3 gets a new tab)", len(tabs_open) == 2, tabs_open)

    with WebRig(WorkspaceBridge(url=SHEET)) as rig, Models("SHOULD NOT BE ASKED") as models:
        r = rig.do("browser what's the total of column B")
        check("E1: total of column B = 500 + 1,250.50 + ₹300 (text cell skipped, said so)",
              "2,050.5" in r.message and "Amount" in r.message and "skipped 1" in r.message, r.message)
        check("...read through Sheets' own export on docs.google.com, this sheet's tab (gid 42)",
              rig.bridge.fetched and rig.bridge.fetched[0].startswith(
                  "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/gviz/tq?tqx=out:csv")
              and "gid=42" in rig.bridge.fetched[0], rig.bridge.fetched)
        r = rig.do("browser what is the average of the amount column")
        check("E1: a column by its header name", "683.5" in r.message, r.message)
        r = rig.do("browser what's the highest of column amount")
        check("E1: highest", "1,250.5" in r.message, r.message)
        r = rig.do("browser how many rows are there")
        check("E1: rows below the header", "4 rows" in r.message, r.message)
        r = rig.do("browser read row 3")
        check("E1: a row is read with its column names", r.message == "Row 3: Name Ana, Amount 1,250.50, Status due.",
              r.message)
        r = rig.do("browser what are the columns")
        check("E1: the headers", "Name, Amount, Status" in r.message, r.message)
        r = rig.do("browser what's the total of column Q")
        check("E1: a column that doesn't exist is said so", not r.ok, r.message)
        check("...no sheet value ever went to a model", models.asked == [], models.asked)
        r = rig.do("browser go to cell B12")
        typed = [(a, given) for a, given in rig.runner.ran if a[:1] == ["wtype"]]
        check("E1: 'go to cell B12' focuses the Name Box, types B12, Enter",
              rig.bridge.last("page.focus") and typed[0] == (["wtype", "-"], "B12")
              and typed[1][0] == linux.wtype_argv("enter"), typed)
        r = rig.do("browser find ravi on this page")
        typed = [(a, given) for a, given in rig.runner.ran if a[:1] == ["wtype"]]
        check("E2: 'find X' inside a Google editor uses its own Ctrl+F (the page is a canvas)",
              typed[-2][0] == linux.wtype_argv("ctrl+f") and typed[-1] == (["wtype", "-"], "ravi")
              and rig.bridge.last("page.highlight") is None, (typed[-2:], r.message))
        r = rig.do("browser download this file")
        check("E4: download a Sheet as .xlsx, in the background",
              rig.bridge.last("tabs.open") == {"url": "https://docs.google.com/spreadsheets/d/"
                                               "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/export?format=xlsx",
                                               "where": "next", "active": False}, rig.bridge.last("tabs.open"))

    with WebRig(WorkspaceBridge(url=DOC)) as rig, Models("A 40,000 a month internship from 1 December.") as models:
        r = rig.do("browser summarize this document")
        check("E2: a Doc is summarized from its TEXT EXPORT (not the empty canvas page)",
              r.message.startswith("A 40,000") and "stipend is 40,000" in str(models.asked[-1])
              and rig.bridge.fetched[-1].endswith("/export?format=txt"), (r.message, rig.bridge.fetched))
        r = rig.do("browser share this doc with ana@x.com")
        typed = [(a, given) for a, given in rig.runner.ran if a[:1] == ["wtype"]]
        check("E2: share opens Share and types the address — and never presses Send",
              rig.bridge.last("page.click") == {"text": "Share", "nth": 1} and typed[-1] == (["wtype", "-"], "ana@x.com")
              and "press Send yourself" in r.message
              and not any(a == linux.wtype_argv("enter") for a, _ in typed), (typed, r.message))

    with WebRig(WorkspaceBridge(url="https://docs.google.com/spreadsheets/u/0/")) as rig:
        r = rig.do("browser create a new sheet called Expenses 2026")
        typed = [(a, given) for a, given in rig.runner.ran if a[:1] == ["wtype"]]
        check("E1: a new sheet is created and named through Rename, with the user's capitals",
              rig.bridge.calls[0][1].get("url") == "https://docs.google.com/spreadsheets/create"
              and (["wtype", "-"], "Expenses 2026") in typed and "Expenses 2026" in r.message, (typed, r.message))
        r = rig.do("browser what's the total of column B")
        check("E1: sums need a Sheet in front", not r.ok, r.message)
        r = rig.do("take a note buy milk")
        typed = [(a, given) for a, given in rig.runner.ran if a[:1] == ["wtype"]]
        check("E8: a Keep note: Take a note, the text, Escape to save",
              typed[-2] == (["wtype", "-"], "buy milk") and typed[-1][0] == linux.wtype_argv("escape")
              and "Keep" in r.message, typed[-2:])

    pres = "https://docs.google.com/presentation/d/1PrCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit"
    with WebRig(WorkspaceBridge(url=pres)) as rig:
        linux.RUNTIME.situation_fn, saved = (lambda: {"tab_url": pres}), linux.RUNTIME.situation_fn
        try:
            r = rig.do("start presenting")
            check("E3: 'start presenting' with Slides in front starts the SLIDESHOW, without the share question",
                  r.status != Status.NEEDS_CONFIRMATION and any(a == linux.wtype_argv("ctrl+f5")
                                                                for a, _ in rig.runner.ran), r.message)
            rig.do("next slide")
            check("E3: next slide = Right", rig.runner.ran[-1][0] == linux.wtype_argv("right"))
            rig.do("browser end the slideshow")
            check("E3: end = Escape", rig.runner.ran[-1][0] == linux.wtype_argv("escape"))
        finally:
            linux.RUNTIME.situation_fn = saved
    with WebRig(WorkspaceBridge(url="https://meet.google.com/abc-defg-hij")) as rig:
        linux.RUNTIME.situation_fn, saved = (lambda: {"tab_url": "https://meet.google.com/abc"}), linux.RUNTIME.situation_fn
        try:
            r = rig.do("start presenting")
            check("...in a Meet call it is still a screen share, which asks first",
                  r.status == Status.NEEDS_CONFIRMATION, r.message)
            linux.resolve_confirmation("no")
        finally:
            linux.RUNTIME.situation_fn = saved

    with WebRig(WorkspaceBridge(url="https://mail.google.com/mail/u/0/#inbox")) as rig:
        rig.do("email search for fee receipt")
        check("E5: mail search", rig.bridge.last("tabs.navigate") ==
              {"url": "https://mail.google.com/mail/u/0/#search/fee%20receipt"}, rig.bridge.last("tabs.navigate"))
        rig.do("email show unread from this week")
        check("E5: unread from this week", rig.bridge.last("tabs.navigate") ==
              {"url": "https://mail.google.com/mail/u/0/#search/is%3Aunread%20newer_than%3A7d"},
              rig.bridge.last("tabs.navigate"))
        rig.do("email open my work label")
        check("E5: a label", rig.bridge.last("tabs.navigate") == {"url": "https://mail.google.com/mail/u/0/#label/Work"})

    cal = {"title": "Design review 15:00", "events": 2, "meet": "", "link": "https://meet.google.com/xyz-abcd-efg"}
    with WebRig(WorkspaceBridge(url="https://calendar.google.com/", calendar=cal)) as rig:
        r = rig.do("calendar what's my next meeting")
        check("E6: the next event by name", "Design review" in r.message, r.message)
        r = rig.do("calendar join my next meeting")
        check("E6: join my next meeting opens the event's own Meet link",
              any(c == "tabs.open" and a.get("url") == "https://meet.google.com/xyz-abcd-efg" for c, a in rig.bridge.calls),
              r.message)
    with WebRig(WorkspaceBridge(url="https://calendar.google.com/", calendar={"title": "Lunch", "events": 1, "meet": ""})) as rig:
        r = rig.do("calendar join my next meeting")
        check("E6: an event without a Meet link is said so", not r.ok and "no Google Meet link" in r.message, r.message)
    with WebRig(WorkspaceBridge(url="https://meet.google.com/abc", clickable=("Admit all",))) as rig:
        r = rig.do("meet admit everyone")
        check("E7: admit everyone clicks Meet's Admit all", rig.bridge.last("page.click") ==
              {"text": "Admit all", "nth": 1} and r.ok, r.message)

    with WebRig(WorkspaceBridge(files=[{"id": "f" * 30, "title": "Feedback survey",
                                        "url": "https://docs.google.com/forms/d/" + "f" * 30 + "/edit"}])) as rig:
        r = rig.do("browser open the responses of my feedback form")
        check("E8: a form's Responses tab", rig.bridge.last("tabs.navigate") ==
              {"url": "https://docs.google.com/forms/d/" + "f" * 30 + "/edit#responses"}
              and "responses" in r.message, (rig.bridge.last("tabs.navigate"), r.message))

    with WebRig(WorkspaceBridge(version="1.6.0")) as rig:
        r = rig.do("open my budget sheet")
        check("an extension older than 1.7.0 is never sent a 1.7.0 command",
              rig.bridge.last("page.docList") is None and not r.ok, r.message)
    source = open(os.path.join(ROOT, "extension", "kayra-bridge", "background.js")).read()
    check("page.fetchText refuses anything but docs.google.com", 'only docs.google.com' in source)
    manifest = json.load(open(os.path.join(ROOT, "extension", "kayra-bridge", "manifest.json")))
    check("extension 1.8.0+ (waits inside the browser), no new permission since 1.6.0",
          tuple(int(x) for x in manifest["version"].split(".")) >= (1, 8, 0)
          and set(manifest["permissions"]) == {"tabs", "scripting", "history", "bookmarks", "sessions",
                                               "alarms", "storage", "tabGroups", "downloads"})

# ┌────────────────────────────────────────────────────────────────────────┐
# │          TUNING PASS V9 — speed and accuracy (no model, no host)        │
# └────────────────────────────────────────────────────────────────────────┘

def section_tuning_v9():
    check.section("[18] Tuning pass V9 — rules before the classifier, one waiting call, relevant text, "
                  "spoken names")
    d = common.direct_tokens
    web_macros.LAST_TAB.update(id=None, title="", at=0.0)      # no tab remembered from earlier sections
    sheet = lambda: {"tab_url": "https://docs.google.com/spreadsheets/d/x/edit"}
    slides = lambda: {"tab_url": "https://docs.google.com/presentation/d/x/edit"}
    for said, expected in (
            ("open the third result", ["browser open the third result"]),
            ("create a new doc", ["browser create a new doc"]),          # live: went to the essay writer
            ("Create a new sheet called Expenses 2026", ["browser Create a new sheet called Expenses 2026"]),
            ("summarize this video", ["browser summarize this video"]),
            ("what does this page say about refunds", ["browser what does this page say about refunds"]),
            ("share the LM Studio window", ["meet share the LM Studio window"]),
            ("let them in", ["meet admit everyone"]),
            ("search my mail for the fee receipt", ["email search my mail for the fee receipt"]),
            ("join my next meeting", ["calendar join my next meeting"]),
            ("what's my next meeting", ["calendar next meeting"]),
            ("find pricing on this page", ["browser find pricing on this page"]),
            ("search my drive for invoices", ["browser search my drive for invoices"])):
        check(f"rules first: {said!r} -> {expected}", d(said) == expected, d(said))
    for said in ("who invented the telephone", "search for best laptops", "highlight the sky",
                 "what's the total of 25 and 30", "open the report file", "find a file called report",
                 "how do I share my screen on zoom", "what are the results of the election",
                 "tell me about the history of rome", "open youtube", "close it"):
        check(f"left to the model: {said!r}", d(said) is None, d(said))
    check("Sheets maths goes rules-first only with a Sheet in front",
          d("what's the total of column C", sheet) == ["browser what's the total of column C"]
          and d("what's the total of column C") is None)
    check("slide steps go rules-first only with Slides in front",
          d("go back a slide", slides) == ["browser go back a slide"] and d("go back a slide") is None)
    saved = (web_macros.LAST_TAB.copy(), web_macros.CLOCK)
    try:
        web_macros.CLOCK = lambda: 100.0
        web_macros.LAST_TAB.update(id=15, title="GitHub", at=99.0)
        check("'close it' goes rules-first only while Kayra remembers a tab", d("close it") == ["browser close it"])
        web_macros.LAST_TAB.update(at=-10_000.0)
        check("...and not once that memory expired", d("close it") is None)
    finally:
        web_macros.LAST_TAB.clear(); web_macros.LAST_TAB.update(saved[0]); web_macros.CLOCK = saved[1]
    check("the router never answers an existing token", d("browser open the third result") is None)

    from kayra.intelligence import llm_engine as E

    class Engine(E.CentralizedLLMEngine):
        def __init__(self):                       # no providers, no network: only the routing
            self.env_vars = {"ASSISTANT_NAME": "Kayra"}

    saved_router = E.RULE_ROUTER
    try:
        E.set_rule_router(lambda text: d(text))
        got = Engine().classify_intent("open the third result")
        check("classify_intent returns the rule route WITHOUT a model call", got == ["browser open the third result"], got)
        E.set_rule_router(lambda text: (_ for _ in ()).throw(RuntimeError("bug")))
        try:
            Engine().classify_intent("open the third result")
            survived = True
        except RuntimeError:
            survived = False
        except Exception:
            survived = True                       # it went on to the (absent) model: fine
        check("a broken rule router never breaks the turn", survived)
    finally:
        E.set_rule_router(saved_router)

    # One "which tab is active?" per command.
    class Counting(TabBridge):
        def __init__(self):
            super().__init__(version="1.8.0")
            self.active_calls = 0

        def call(self, cmd, timeout=10.0, **args):
            if cmd == "tabs.active":
                self.active_calls += 1
            return super().call(cmd, timeout=timeout, **args)

    bridge = Counting()
    with WebRig(bridge):
        web_macros.new_command()
        web_macros._call("tabs.active"); web_macros._call("tabs.active")
        check("a repeated tabs.active is served from the per-command cache", bridge.active_calls == 1,
              bridge.active_calls)
        web_macros._call("tabs.navigate", url="https://x.example/")
        web_macros._call("tabs.active")
        check("...and asked again after anything that can change it (navigate)", bridge.active_calls == 2,
              bridge.active_calls)
        web_macros.new_command()
        web_macros._call("tabs.active")
        check("...and at the start of every command", bridge.active_calls == 3, bridge.active_calls)

    # The relevant part of a long page.
    page = ("Acme Cloud pricing. " + "Our platform offers storage and compute. " * 300
            + "Refunds: you get a full refund within 30 days of purchase. " + "Partners and features. " * 300)
    picked = ai.relevant(page, "what does this page say about refunds", 4000)
    check("relevant(): the paragraph that answers is kept (a blind 3 K cut lost it, measured live)",
          "full refund within 30 days" in picked and len(picked) <= 4200 and picked.startswith("Acme Cloud"),
          len(picked))
    check("relevant(): short text is unchanged", ai.relevant("short page", "q", 4000) == "short page")
    check("relevant(): no match keeps the opening, as before", ai.relevant("x" * 9000, "zebra", 4000) == "x" * 4000)
    with WebRig(PageBridge(version="1.8.0")) as rig, Models("It costs 10 dollars.") as models:
        rig.do("browser what does this page say about pricing")
        check("page questions send at most ~4 K characters of page", len(str(models.asked[-1])) < 6000,
              len(str(models.asked[-1])))

    # Names said aloud.
    n2 = web_macros.spoken_numbers
    check("spoken numbers: 'twenty twenty six' -> 2026, 'two thousand twenty five' -> 2025, "
          "'nineteen oh five' -> 1905, 'twenty five' -> 25",
          n2("budget twenty twenty six") == "budget 2026" and n2("two thousand twenty five plan") == "2025 plan"
          and n2("nineteen oh five") == "1905" and n2("twenty five") == "25")
    items = [{"title": "Budget 2026"}, {"title": "Budget 2025 (old)"}, {"title": "Team roster"}]
    r = web_macros.rank_titles("budjet twenty twenty six", items)
    check("a misheard name ('budjet twenty twenty six') still finds Budget 2026 first",
          r and r[0][1]["title"] == "Budget 2026", r)
    check("'team rooster' finds Team roster", web_macros.rank_titles("team rooster", items)[0][1]["title"] == "Team roster")
    check("unrelated names still match nothing", web_macros.rank_titles("holiday photos", items) == [])
    se = common.spoken_email
    check("spoken emails: 'ana at x dot com', 'ravi underscore k at iitm dot ac dot in'",
          se("ana at x dot com") == "ana@x.com" and se("ravi underscore k at iitm dot ac dot in") == "ravi_k@iitm.ac.in"
          and se("ana") == "")
    a = n("browser share this doc with ana at x dot com")
    check("share takes a spoken address", a.action == "ws_share" and a.target == "ana@x.com", a.target)
    check("share without an address is not a share", n("browser share this doc with ana").action != "ws_share")

    # Sheets: a title row above the table, and a filter.
    titled = "Expenses 2026,,\n,,\nName,Amount,Status\nRavi,500,paid\nAna,250,due\nMeera,300,paid\n"
    with WebRig(WorkspaceBridge(url=SHEET, csv=titled)) as rig, Models("SHOULD NOT BE ASKED"):
        r = rig.do("browser what's the total of amount where status is paid")
        check("Sheets: header found below a title row, and 'where status is paid' filters",
              "800" in r.message and "where status is paid" in r.message, r.message)
        r = rig.do("browser what's the total of amount where status is refunded")
        check("...a filter that matches nothing is said so", not r.ok, r.message)

    # A bot check is not a result list.
    class Blocked(GroupBridge):
        def call(self, cmd, timeout=10.0, **args):
            if cmd == "page.results":
                self.calls.append((cmd, args))
                return {"site": "blocked", "blocked": True, "count": 0, "result": None, "results": []}
            return super().call(cmd, timeout=timeout, **args)

    with WebRig(Blocked()) as rig:
        r = rig.do("browser open the first result")
        check("a captcha page is said so, and nothing is opened",
              "bot check" in r.message and rig.bridge.last("tabs.navigate") is None, r.message)
    js = open(os.path.join(ROOT, "extension", "kayra-bridge", "background.js")).read()
    check("waits run inside the extension (untilReady) for lists, comments, results, Calendar",
          js.count("untilReady(args,") >= 5)

# ┌────────────────────────────────────────────────────────────────────────┐
# │   PART 5 — app context (H0.2), semantic matcher (H0.3), WhatsApp (H0.4) │
# └────────────────────────────────────────────────────────────────────────┘

TODAY = __import__("datetime").date.today()
TODAY_MARK = f"{TODAY.day}/{TODAY.month}/{TODAY.year}"


class WhatsAppBridge(Bridge):
    """WhatsApp Web as a table: a chat list in recency order, search, open, messages, composer."""

    def __init__(self, version="1.9.0", chats=None, ready=True):
        super().__init__(version=version, tabs=[{"id": 5, "title": "WhatsApp", "url": "https://web.whatsapp.com/"}])
        self.chats = chats if chats is not None else [
            {"name": "PR and Outreach Core Team 2026-27", "unread": 5, "preview": "meeting at 6", "when": "10:21"},
            {"name": "Rahul Sharma", "unread": 0, "preview": "ok", "when": "09:02"},
            {"name": "Rahul Verma", "unread": 2, "preview": "call me", "when": "Yesterday"},
            {"name": "Design team 2026", "unread": 0, "preview": "", "when": "Monday"}]
        self.archive = [{"name": "Old Hostel Friends", "unread": 0, "preview": "", "when": "1/1/2026"}]
        self.ready, self.open_chat, self.searching, self.composer = ready, "", "", ""
        self.sent, self.history = [], {
            "PR and Outreach Core Team 2026-27": [
                {"from": "Ana", "text": "Meeting at 6 in room 204", "date": TODAY_MARK, "time": "10:00", "out": False},
                {"from": "Ravi", "text": "I'll bring the posters", "date": TODAY_MARK, "time": "10:05", "out": False},
                {"from": "Ana", "text": "Old message", "date": "1/1/2026", "time": "09:00", "out": False}]}

    def call(self, cmd, timeout=10.0, **args):
        if cmd.startswith("chat.") or cmd == "page.type" or (cmd == "page.click" and args.get("text") == "Send"):
            self.calls.append((cmd, args))
        if cmd == "chat.list":
            if not self.ready:
                return {"ready": False, "chats": []}
            rows = [c for c in self.chats + self.archive if self.searching.lower() in c["name"].lower()] \
                if self.searching else self.chats
            return {"ready": True, "chats": [dict(c) for c in rows]}
        if cmd == "chat.open":
            known = {c["name"] for c in self.chats + self.archive}
            if args["name"] in known:
                self.open_chat, self.searching, self.composer = args["name"], "", ""
                return {"opened": True, "name": args["name"]}
            return {"opened": False}
        if cmd == "chat.messages":
            msgs = self.history.get(self.open_chat, []) + [
                {"from": "", "text": t, "date": TODAY_MARK, "time": "11:00", "out": True} for t in self.sent]
            return {"chat": self.open_chat, "messages": msgs[-args.get("limit", 30):]}
        if cmd == "chat.composer":
            return {"found": bool(self.open_chat), "text": self.composer, "chat": self.open_chat}
        if cmd == "page.type":
            if args.get("field") == "search":
                self.searching = args.get("text", "")
                return {"typed": True}
            if args.get("field") == "type a message":
                if args.get("submit"):
                    if self.composer:
                        self.sent.append(self.composer); self.composer = ""
                    return {"typed": True}
                self.composer = args.get("text", "") if args.get("clear") else self.composer + args.get("text", "")
                return {"typed": True}
        if cmd == "page.click" and args.get("text") == "Send":
            if self.composer:
                self.sent.append(self.composer); self.composer = ""
                return {"clicked": True}
            return {"clicked": False}
        return super().call(cmd, timeout=timeout, **args)


def section_part_5():
    check.section("[19] Part 5 — WhatsApp: app context, the semantic matcher, open / read / send")
    from kayra.automation import semantic_match as S
    names = ["PR and Outreach Core Team 2026-27", "PR Outreach 2025", "Core Team", "Design team 2026",
             "Maa", "Rahul Sharma", "Rahul Verma", "Pune Tech Club", "Dev Club 2026 (coding)", "Ana"]
    for said, decision, best in (
            ("PR and outreach core team", "pick", "PR and Outreach Core Team 2026-27"),   # the user's example
            ("core team", "pick", "Core Team"),                 # the full name, as written
            ("design team", "pick", "Design team 2026"),
            ("rahool sharma", "pick", "Rahul Sharma"),          # a misheard spelling
            ("rahul", "pick", "Rahul Sharma"),                  # a true tie: the most recent chat wins
            ("ptc", "pick", "Pune Tech Club"),                  # initials
            ("the coding group", "pick", "Dev Club 2026 (coding)"),
            ("PR group", "ask", None), ("outreach", "ask", None),   # two close: ask, never guess
            ("xyz", "none", None)):
        m = S.match(said, names)
        ok = m.decision == decision and (best is None or (m.best and m.best.name == best))
        check(f"match {said!r} -> {decision} {best or ''}", ok, m)
    m = S.match("PR group", names)
    check("an 'ask' offers the two closest by name", [c.name for c in m.top(2)] ==
          ["PR Outreach 2025", "PR and Outreach Core Team 2026-27"], m.top(2))
    check("a taught alias picks exactly that chat", S.match("mom", names, aliases={"mom": "Maa"}).best.name == "Maa")

    def far_embed(texts):                 # meaning that is strongly "similar" to everything
        return [[1.0, 0.0]] * len(texts)
    check("meaning alone can only ASK, never open a chat by itself",
          S.match("zzz qqq", ["Alpha", "Beta"], embed=far_embed).decision in ("ask", "none"))

    # H0.2 — the app in front.
    wa = lambda: {"site": "web.whatsapp.com", "app": "chrome-web.whatsapp.com__-default"}
    chrome_wa = lambda: {"app": "google-chrome", "tab_url": "https://web.whatsapp.com/"}
    code = lambda: {"app": "code", "tab_url": "https://web.whatsapp.com/"}
    d = common.direct_tokens
    check("H0.2: 'open XYZ chat' with WhatsApp in front is a CHAT (the bug report)",
          d("open XYZ chat", wa) == ["message open XYZ"], d("open XYZ chat", wa))
    check("...also in a normal Chrome window on web.whatsapp.com", d("open XYZ chat", chrome_wa) == ["message open XYZ"])
    check("...never when another app is in front (Chrome's tab is not 'in front')", d("open XYZ chat", code) is None)
    check("'click on Design Team 2026' keeps the name exactly", d("click on Design Team 2026", wa) ==
          ["message open Design Team 2026"])
    check("'open youtube' in front of WhatsApp is still YouTube; 'open youtube chat' is a chat",
          d("open youtube", wa) is None and d("open youtube chat", wa) == ["message open youtube"])
    check("'read it' / 'who messaged me' with WhatsApp in front", d("read it", wa) == ["message read this"]
          and d("who messaged me", wa) == ["message who messaged me"])
    for token, key, target in (("message open the PR group", "message.open", "pr"),
                               ("message who messaged me", "message.unread", "whatsapp"),
                               ("message read the core team today", "message.read", "core team"),
                               ("message tell rahul saying hi", "message.send", "rahul"),
                               ("message rahul: running late", "message.send", "rahul")):
        a = n(token)
        check(f"'{token}' -> {key} {target!r}", a.key == key and a.target == target, (a.key, a.target))
    check("a 'tell' is not sent to someone called 'tell'", n("message tell rahul saying hi").parameters["text"] == "hi")
    saved = (dict(linux._LAST_FRONT), linux.HYPR)
    try:
        hypr = FakeHyprland([client("0xw", "chrome-web.whatsapp.com__-Default", "WhatsApp", focus=0),
                             client("0xk", "kayra", "Kayra", focus=1)], active="0xw")
        linux.HYPR = hypr
        first = linux.situation()
        hypr.active = "0xk"                              # the user looks at Kayra's own window
        second = linux.situation()
        check("H0.2: talking to Kayra's window keeps the app the user was just in (2 minutes)",
              first.get("site") == "web.whatsapp.com" and second.get("site") == "web.whatsapp.com", (first, second))
        linux._LAST_FRONT["at"] -= 1000
        check("...and forgets it after that", linux.situation().get("site") is None)
    finally:
        linux._LAST_FRONT.clear(); linux._LAST_FRONT.update(saved[0]); linux.HYPR = saved[1]

    saved_embed = web_macros.EMBEDDER
    web_macros.EMBEDDER = False                          # no local embeddings in a unit test
    try:
        with WebRig(WhatsAppBridge()) as rig, Models("SHOULD NOT SEND CHATS ANYWHERE") as models:
            r = rig.do("message open PR and outreach core team")
            check("open: the user's example opens 'PR and Outreach Core Team 2026-27' and says the unread count",
                  rig.bridge.open_chat == "PR and Outreach Core Team 2026-27" and "5 unread" in r.message, r.message)
            rig.bridge.chats.insert(1, {"name": "PR Outreach 2025", "unread": 0, "preview": "", "when": "10:00"})
            r = rig.do("message open PR group")
            check("open: an ambiguous name asks with BOTH names and opens nothing",
                  r.status == Status.AMBIGUOUS and r.message == "Do you mean PR Outreach 2025 or "
                  "PR and Outreach Core Team 2026-27?" and rig.bridge.open_chat == "PR and Outreach Core Team 2026-27",
                  (r.message, rig.bridge.open_chat))
            rig.bridge.chats.pop(1)
            r = rig.do("message open old hostel friends")
            check("open: a chat outside the visible list is found through WhatsApp's own search",
                  rig.bridge.open_chat == "Old Hostel Friends"
                  and ("page.type", {"field": "search", "text": "old hostel friends", "clear": True}) in rig.bridge.calls,
                  r.message)
            r = rig.do("message open nobody at all")
            check("open: nobody is said so", not r.ok and "nobody at all" in r.message, r.message)
            check("...and WhatsApp's search box is emptied again (it filtered every later list)",
                  rig.bridge.searching == "", rig.bridge.searching)
            r = rig.do("message who messaged me")
            check("who messaged me: names with counts, most recent first",
                  r.message.startswith("Unread on WhatsApp: PR and Outreach Core Team 2026-27 (5), Rahul Verma (2)"), r.message)
        with WebRig(WhatsAppBridge()) as rig, Models("Ana set the meeting for 6 in room 204; Ravi brings posters.") as models:
            r = rig.do("message read the core team today")
            check("read today: only today's messages, read out directly when there are few",
                  "Meeting at 6" in r.message and "Old message" not in r.message and models.asked == [], r.message)
            rig.bridge.history["PR and Outreach Core Team 2026-27"] += [
                {"from": "Ana", "text": f"note {i}", "date": TODAY_MARK, "time": "12:00", "out": False} for i in range(6)]
            r = rig.do("message read the core team today")
            check("read: a longer conversation is summarized by the model", r.message.startswith("Ana set the meeting"), r.message)
        saved_local = ai._local_chat
        ai._local_chat = lambda *a, **k: ""              # no local model running
        try:
            with WebRig(WhatsAppBridge()) as rig, Models("A CLOUD SUMMARY") as models:
                rig.bridge.history["PR and Outreach Core Team 2026-27"] += [
                    {"from": "Ana", "text": f"note {i}", "date": TODAY_MARK, "time": "12:00", "out": False} for i in range(6)]
                ai.TEXT_MODEL, scripted = None, ai.TEXT_MODEL      # take the path a real install takes
                saved_complete = ai.complete
                ai.complete = lambda *a, **k: "A CLOUD SUMMARY"
                try:
                    r = rig.do("message read the core team")
                finally:
                    ai.TEXT_MODEL, ai.complete = scripted, saved_complete
                check("PRIVACY: with no local model, chats are NOT sent to a cloud model — the latest few are read",
                      "A CLOUD SUMMARY" not in r.message and "note 5" in r.message, r.message)
        finally:
            ai._local_chat = saved_local

        with WebRig(WhatsAppBridge()) as rig:
            r = rig.do("message rahool sharma: running late")
            check("send: the chat is resolved and OPENED, then it ASKS naming the real chat and the text",
                  r.status == Status.NEEDS_CONFIRMATION and r.message == 'Send "running late" to Rahul Sharma?'
                  and rig.bridge.open_chat == "Rahul Sharma", r.message)
            check("...nothing was typed into the message box and nothing sent before the yes",
                  rig.bridge.composer == "" and rig.bridge.sent == []
                  and not any(c == "page.type" and a.get("field") == "type a message" for c, a in rig.bridge.calls))
            ok, said = linux.resolve_confirmation("yes")
            check("...'yes' types it, reads it back, presses Send and sees it arrive",
                  rig.bridge.sent == ["running late"] and said == "Sent to Rahul Sharma.", said)
            rig.do("message rahul sharma: hello again")
            ok, said = linux.resolve_confirmation("no")
            check("send: 'no' sends nothing", rig.bridge.sent == ["running late"] and "won't" in said, said)
            rig.do("message rahul sharma: are you there")
            rig.bridge.open_chat = "Design team 2026"      # the user clicked another chat meanwhile
            ok, said = linux.resolve_confirmation("yes")
            check("send: if another chat is open by the time of the yes, NOTHING is sent",
                  rig.bridge.sent == ["running late"] and "didn't send" in said, said)
            rig.bridge.chats.insert(1, {"name": "PR Outreach 2025", "unread": 0, "preview": "", "when": "10:00"})
            r = rig.do("message pr group: hi")
            check("send: an ambiguous chat asks WHICH, and never asks to send", r.status == Status.AMBIGUOUS
                  and rig.bridge.sent == ["running late"], r.message)
        with WebRig(WhatsAppBridge(ready=False)) as rig:
            r = rig.do("message open rahul")
            check("not logged in / not loaded is said so", not r.ok and "logged in" in r.message, r.message)
        with WebRig(WhatsAppBridge(version="1.8.0")) as rig:
            r = rig.do("message open rahul")
            check("an extension older than 1.9.0 is never sent a chat command",
                  not any(c.startswith("chat.") for c, _a in rig.bridge.calls) and not r.ok, r.message)
    finally:
        web_macros.EMBEDDER = saved_embed
    check("message.send is not a static policy CONFIRM any more (the handler asks, naming the chat)",
          policy_classify("message rahul: hi") == "ALLOW")
    check("the WhatsApp shapes are in the understanding catalog",
          "message open <chat or group>" in interpreter.CATALOG and "message who messaged me" in interpreter.CATALOG)


# ┌────────────────────────────────────────────────────────────────────────┐
# │   V11 — the fix round: scrolling, search, close search, names          │
# └────────────────────────────────────────────────────────────────────────┘
class AppBridge(PageBridge):
    """PageBridge at 1.10.0: a page that says whether a scroll MOVED, one search box, and a
    search that can be closed."""

    def __init__(self, url="https://mail.google.com/mail/u/0/#inbox", scroll=None, search=True, open_search="", **kw):
        super().__init__(version=kw.pop("version", "1.10.0"), url=url, **kw)
        self.scroll = scroll if scroll is not None else {"scrolled": True, "moved": True, "inner": True}
        self.search, self.open_search = search, open_search

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "page.scroll":
            self.calls.append((cmd, args))
            return dict(self.scroll)
        if cmd == "page.search":
            self.calls.append((cmd, args))
            if self.search:
                self.open_search = args.get("text", "")
            return {"searched": bool(self.search)}
        if cmd == "page.searchClose":
            self.calls.append((cmd, args))
            had, self.open_search = self.open_search, ""
            return {"closed": bool(had), "had": had}
        return super().call(cmd, timeout=timeout, **args)


class WhatsApp11(WhatsAppBridge):
    """WhatsAppBridge at 1.11.0: a search that is waited for and can be closed, a chat that can
    be closed, and no tab groups. `stuck` = the page refuses to empty its search by script."""

    def __init__(self, stuck=0, **kw):
        super().__init__(version="1.11.0", **kw)
        self.stuck = stuck

    def last(self, cmd):
        return next((a for c, a in reversed(self.calls) if c == cmd), None)

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "tabs.active":
            return {"id": 5, "title": "WhatsApp", "url": "https://web.whatsapp.com/"}
        if cmd == "groups.list":
            return []
        if cmd == "page.searchClose":
            self.calls.append((cmd, args))
            if self.stuck and self.searching:
                self.stuck -= 1
                return {"closed": False, "had": self.searching, "left": self.searching}
            had, self.searching = self.searching, ""
            return {"closed": bool(had), "had": had, "left": ""}
        if cmd == "chat.search":
            self.calls.append((cmd, args))
            self.searching = args.get("text", "")
            rows = [c for c in self.chats + self.archive if self.searching.lower() in c["name"].lower()] \
                if self.searching else self.chats
            return {"ready": True, "searched": True, "typed": True, "empty": not rows, "chats": [dict(c) for c in rows]}
        if cmd == "chat.close":
            self.calls.append((cmd, args))
            was, self.open_chat = self.open_chat, ""
            return {"closed": bool(was), "open": False}
        if cmd == "page.highlight":
            return {"count": 0}
        if cmd == "chat.open" and args["name"] in {c["name"] for c in self.chats + self.archive}:
            self.calls.append((cmd, args))                 # the real app keeps the search open
            self.open_chat, self.composer = args["name"], ""
            return {"opened": True, "name": args["name"]}
        return super().call(cmd, timeout=timeout, **args)


class YearFiles(WorkspaceBridge):
    """Google's search finds nothing for a year said the long way; the plain words do."""

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "page.docList":
            self.calls.append((cmd, args))
            return [] if "202027" in self.url or "zzz" in self.url else list(self.files)
        return super().call(cmd, timeout=timeout, **args)


def section_fix_round_v11():
    check.section("[20] V11 — the reported fixes: scroll, WhatsApp names, search, close search, file names")
    d = common.direct_tokens
    nowhere = lambda: {}
    wa = lambda: {"site": "web.whatsapp.com", "app": "chrome-web.whatsapp.com__-default"}

    # ── the sentences as they were reported ──
    for said, key, target in (
            ("scroll the mail to bottom", "browser.page_scroll", "bottom"),
            ("scroll the mail", "browser.page_scroll", "down"),
            ("scroll up a bit", "browser.page_scroll", "up"),
            ("scroll all the way down", "browser.page_scroll", "bottom"),
            ("go to the top of the page", "browser.page_scroll", "top"),
            ("open graphics team group in whatsapp", "message.open", "graphics team"),
            ("open graphics team group", "browser.group_collapse", "graphics team"),
            ("open rahul chat", "message.open", "rahul"),
            ("open the latest chat in whatsapp", "message.open", "recent"),
            ("read the recent chat", "message.read", "recent"),
            ("read the latest messages on whatsapp", "message.read", "recent"),
            ("click on the search button and search for council details", "browser.search_here", "council details"),
            ("click the search bar and type Rahul Sharma and press enter", "browser.search_here", "Rahul Sharma"),
            ("search for rahul in the search bar", "browser.search_here", "rahul"),
            ("close the search bar", "browser.search_close", "search"),
            ("close the search and go back to my chats", "browser.search_close", "search"),
            ("open council details 2026-27 sheet", "browser.ws_open", "council details 2026-27")):
        bare, prefixed = n(said), n("browser " + said)
        check(f"'{said}' -> {key} {target!r} (bare)", bare is not None and bare.key == key and bare.target == target,
              bare and (bare.key, bare.target))
        check("...and the same with the browser prefix", prefixed is not None and prefixed.key == key
              and prefixed.target == target, prefixed and (prefixed.key, prefixed.target))
    for said, token in (
            ("scroll the mail to bottom", "browser scroll the mail to bottom"),
            ("Scroll the chat up.", "browser Scroll the chat up"),
            ("open graphics team group in whatsapp", "message open graphics team group in whatsapp"),
            ("read the recent chat", "message read the recent chat"),
            ("click on the search button and search for council details",
             "browser click on the search button and search for council details"),
            ("close the search bar", "browser close the search bar"),
            ("open graphics team group", "browser open graphics team group")):
        check(f"rules first, no model: '{said}'", d(said, nowhere) == [token], d(said, nowhere))
    a = n("scroll the chat list down a little")
    check("a place and an amount are kept: the chat LIST, a little",
          a.parameters == {"direction": "down", "where": "side", "amount": 0.4}, a.parameters)
    check("'scroll to pricing' is still a scroll TO those words", n("browser scroll to pricing").parameters.get("until") == "pricing")
    check("'scroll down' / 'scroll up' stay the exact tokens they were", n("scroll down").key == "mouse.scroll_down"
          and d("scroll down", nowhere) is None)
    check("a question with 'the end of' in it is not a scroll", n("what's the end of the story") is None
          and common.scroll_request("scroll through my options and pick one") is None)
    check("'close chrome' is still the app, 'open google chat' still not a WhatsApp chat",
          n("close chrome").key == "app.close" and n("open google chat").key != "message.open", n("open google chat"))
    check("with WhatsApp in front, 'open the budget sheet' is still a Sheet (was sent to a chat)",
          d("open council details 2026-27 sheet", wa) == ["browser open council details 2026-27 sheet"],
          d("open council details 2026-27 sheet", wa))
    check("'open the research tab group' never looks in WhatsApp",
          n("browser open the research tab group").parameters.get("or_chat") is False)
    for said, token in (("search for general panchayat", "message search for general panchayat"),
                        ("search general panchayat group and open it", "message search general panchayat group and open it"),
                        ("open the first search result not the second one", "message open result 1"),
                        ("open the second one", "message open result 2"),
                        ("close this group", "message close this chat"),
                        ("close the whatsapp search", "browser close the whatsapp search"),
                        ("delete the text in the search bar", "browser delete the text in the search bar")):
        check(f"WhatsApp in front: '{said}' -> {token!r}", d(said, wa) == [token], d(said, wa))
    for token, key, target in (("message search for general panchayat", "message.search", "general panchayat"),
                               ("search whatsapp for safe house and open it", "message.search", "safe house"),
                               ("find cultural core team on whatsapp", "message.search", "cultural core team"),
                               ("message open result 2", "message.open", "#2"),
                               ("close the chat", "message.close", "current"),
                               ("close search bar in whatsapp", "browser.search_close", "search"),
                               ("clear the search text", "browser.search_close", "search")):
        a = n(token)
        check(f"'{token}' -> {key} {target!r}", a is not None and a.key == key and a.target == target, a)
    check("'search … and open it' opens; a plain search only searches",
          n("message search general panchayat group and open it").parameters == {"open": True}
          and n("message search for general panchayat").parameters == {"open": False})
    check("with WhatsApp in front, 'search for cats on youtube' is NOT a chat search; 'close the research group' is still a tab group",
          not (d("search for cats on youtube", wa) or [""])[0].startswith("message")
          and d("close the research group", wa) == ["browser close the research group"],
          d("search for cats on youtube", wa))
    check("search_close is a rules-first action; the new shapes are in the understanding catalog",
          "search_close" in common._DIRECT_BROWSER and "browser close the search" in interpreter.CATALOG
          and "browser scroll the <mail | chat | page> to the <top | bottom>" in interpreter.CATALOG)

    # ── scrolling: what the page reports is what is said ──
    with WebRig(AppBridge()) as rig:
        r = rig.do("browser scroll the mail to bottom")
        check("scroll: the page is asked for the bottom, and 'At the bottom' is said because it MOVED",
              rig.bridge.last("page.scroll") == {"direction": "bottom", "until": "", "where": "", "amount": 1.0}
              and r.message == "At the bottom.", (rig.bridge.last("page.scroll"), r.message))
        rig.bridge.scroll = {"scrolled": True, "moved": False, "canScroll": True}
        r = rig.do("browser scroll the mail")
        check("scroll: nothing moved because it is at the end -> said so, never 'Done'",
              r.message == "It's already at the bottom.", r.message)
        rig.bridge.scroll = {"scrolled": False, "moved": False, "canScroll": False}
        before = len(rig.runner.ran)
        r = rig.do("browser scroll down a bit")
        keys = [argv for argv, _given in rig.runner.ran[before:] if argv[:1] == ["wtype"]]
        check("scroll: a canvas app (Sheets) with nothing to scroll gets the real Page Down key",
              keys == [linux.wtype_argv("pagedown")] and r.ok, (keys, r.message))
    with WebRig(AppBridge(version="1.9.0")) as rig:
        r = rig.do("browser scroll the mail to bottom")
        check("scroll: an older extension is sent only what it knows",
              rig.bridge.last("page.scroll") == {"direction": "bottom", "until": ""} and r.ok, rig.bridge.last("page.scroll"))

    # ── one search action ──
    with WebRig(AppBridge(url="https://example.com/shop")) as rig:
        r = rig.do("browser click on the search button and search for council details")
        check("search: 'click on the search button and search for X' is ONE search for X, submitted",
              rig.bridge.last("page.search") == {"text": "council details", "submit": True}
              and not any(c == "page.click" for c, _a in rig.bridge.calls) and "council details" in r.message,
              (rig.bridge.calls[-2:], r.message))
        r = rig.do("browser close the search bar")
        check("close search: the open search is closed and said so", rig.bridge.open_search == ""
              and r.message == "Closed the search.", r.message)
        r = rig.do("browser close the search bar")
        check("close search: with none open, that is said (no false 'closed')", "no search open" in r.message, r.message)
    with WebRig(AppBridge(url="https://docs.google.com/spreadsheets/u/0/")) as rig:
        r = rig.do("browser click on the search button and search for council details")
        check("search: on the Sheets home list it is Sheets' own search (was Ctrl+F on the page)",
              rig.bridge.last("tabs.navigate") == {"url": "https://docs.google.com/spreadsheets/u/0/?q=council%20details"}
              and not any(argv[:1] == ["wtype"] for argv, _g in rig.runner.ran), (rig.bridge.last("tabs.navigate"), r.message))
    with WebRig(AppBridge(url="https://mail.google.com/mail/u/0/#search/invoices")) as rig:
        r = rig.do("browser close the search")
        check("close search: Gmail goes back to the inbox",
              rig.bridge.last("tabs.navigate") == {"url": "https://mail.google.com/mail/u/0/#inbox"}, r.message)

    # ── WhatsApp ──
    saved_embed = web_macros.EMBEDDER
    web_macros.EMBEDDER = False
    try:
        chats = [{"name": "Family", "unread": 0, "preview": "", "when": "11:00", "pinned": True},
                 {"name": "Graphics Team (2026-27)", "unread": 2, "preview": "final poster", "when": "10:40"},
                 {"name": "Rahul Sharma", "unread": 0, "preview": "ok", "when": "09:02"},
                 {"name": "Graphics Team (2025-26)", "unread": 0, "preview": "", "when": "1/3/2026"}]
        with WebRig(WhatsApp11(chats=[dict(c) for c in chats])) as rig, Models("SHOULD NOT BE ASKED") as models:
            rig.bridge.history["Graphics Team (2026-27)"] = [
                {"from": "Riya", "text": f"old {i}", "date": TODAY_MARK, "time": "09:00", "out": False} for i in range(5)] + [
                {"from": "Riya", "text": "Final poster is uploaded", "date": TODAY_MARK, "time": "10:39", "out": False},
                {"from": "Aman", "text": "Please review by 5", "date": TODAY_MARK, "time": "10:40", "out": False}]
            r = rig.do("open graphics team group in whatsapp")
            check("WhatsApp: 'graphics team' opens 'Graphics Team (2026-27)' — the edition with the latest message, no question",
                  rig.bridge.open_chat == "Graphics Team (2026-27)" and r.ok and "2 unread" in r.message, r.message)
            rig.bridge.open_chat = ""
            r = rig.do("browser open graphics team group")
            check("WhatsApp: 'open graphics team group' with no such TAB group is the WhatsApp group",
                  rig.bridge.open_chat == "Graphics Team (2026-27)", (rig.bridge.open_chat, r.message))
            rig.bridge.open_chat = ""
            r = rig.do("message read the recent chat")
            check("WhatsApp: 'read the recent chat' with none open opens the newest (not the pinned one) and "
                  "reads only what is NEW", rig.bridge.open_chat == "Graphics Team (2026-27)"
                  and r.message.startswith("2 new in Graphics Team (2026-27): Riya: Final poster is uploaded")
                  and "old 4" not in r.message and models.asked == [], r.message)
            rig.bridge.open_chat = "Rahul Sharma"
            rig.bridge.history["Rahul Sharma"] = [{"from": "Rahul Sharma", "text": "ok", "date": TODAY_MARK, "time": "09:02", "out": False}]
            r = rig.do("message read the recent chat")
            check("WhatsApp: ...and with a chat open, it is THAT chat's recent messages", r.message == "In Rahul Sharma: Rahul Sharma: ok", r.message)
            r = rig.do("message open old hostel friends")
            check("WhatsApp: a chat opened through the search leaves the search CLOSED afterwards",
                  rig.bridge.open_chat == "Old Hostel Friends" and rig.bridge.searching == ""
                  and any(c == "page.searchClose" for c, _a in rig.bridge.calls), rig.bridge.searching)
            r = rig.do("browser search for rahul in the search bar")
            check("WhatsApp: a search is WhatsApp's own, left open, and the chats it found are said",
                  rig.bridge.last("chat.search") == {"text": "rahul"} and rig.bridge.searching == "rahul"
                  and r.message.startswith("I found Rahul Sharma"), r.message)
            r = rig.do("message search for hostel")
            check("WhatsApp: searching for something else REPLACES the old text", rig.bridge.searching == "hostel"
                  and "Old Hostel Friends" in r.message, (rig.bridge.searching, r.message))
            rig.bridge.open_chat = "Rahul Sharma"
            r = rig.do("message open result 1")
            check("WhatsApp: 'open the first one' opens the first chat of the list as shown, and closes the search",
                  rig.bridge.open_chat == "Old Hostel Friends" and rig.bridge.searching == "", (rig.bridge.open_chat, r.message))
            rig.do("message search for rahul")
            r = rig.do("browser close the search bar")
            check("WhatsApp: 'close the search bar' empties it and the chat that was open is still open",
                  rig.bridge.searching == "" and rig.bridge.open_chat == "Old Hostel Friends"
                  and r.message == "Closed the search. You're back in your chats.", r.message)
            r = rig.do("message search for zzzz")
            check("WhatsApp: a search with no hit says so and offers the way out", "nothing for zzzz" in r.message
                  and "close the search" in r.message, r.message)
            rig.bridge.stuck = 1
            before = len(rig.runner.ran)
            r = rig.do("browser close the whatsapp search")
            keys = [argv for argv, _g in rig.runner.ran[before:] if argv[:1] == ["wtype"]]
            check("close search: a page that refuses the script gets the REAL keys (select all, delete, Escape), then is checked again",
                  keys == [linux.wtype_argv("ctrl+a"), linux.wtype_argv("backspace"), linux.wtype_argv("escape")]
                  and r.ok and rig.bridge.searching == "", (keys, r.message))
            rig.bridge.searching, rig.bridge.stuck = "rahul", 5
            r = rig.do("browser close the search")
            check("close search: if it STILL holds text, that is said — never a false 'closed'", not r.ok, r.message)
            rig.bridge.searching, rig.bridge.stuck = "", 0

            # ── the audit log of 2026-11-11: a chat that is not on screen ──
            rig.bridge.archive.append({"name": "General Panchayat", "unread": 0, "preview": "", "when": "2/2/2026"})
            rig.bridge.calls.clear()
            r = rig.do("message open general panchayat")
            check("LOG: a chat that is NOT in the visible list is searched for (waited) and opened",
                  rig.bridge.open_chat == "General Panchayat" and rig.bridge.last("chat.search") == {"text": "general panchayat"}
                  and r.ok, r.message)
            check("LOG: ...and the search is closed afterwards", rig.bridge.searching == ""
                  and [c for c, _a in rig.bridge.calls].index("page.searchClose") > [c for c, _a in rig.bridge.calls].index("chat.open"))
            rig.bridge.calls.clear()
            r = rig.do("message open general punchayat")
            searches = [a["text"] for c, a in rig.bridge.calls if c == "chat.search"]
            check("LOG: a misheard name is searched again by its words, and the right chat still opens",
                  searches == ["general punchayat", "punchayat", "general"] and rig.bridge.open_chat == "General Panchayat", searches)
            rig.bridge.calls.clear()
            r = rig.do("message open cultural core team")
            check("LOG: a chat that does not exist: searched, said so, and the search is NOT left open",
                  not r.ok and "cultural core team" in r.message and rig.bridge.searching == ""
                  and any(c == "chat.search" for c, _a in rig.bridge.calls), (r.message, rig.bridge.searching))
            rig.bridge.calls.clear()
            with Model('{"kind": "automation", "commands": ["message open cultural core team"]}'):
                run_tokens(["message open cultural core team"])
            check("LOG: the model proposing the SAME command does not run it a second time (it ran twice)",
                  [a["text"] for c, a in rig.bridge.calls if c == "chat.search"].count("cultural core team") == 1,
                  [a for c, a in rig.bridge.calls if c == "chat.search"])
            rig.bridge.open_chat = "Rahul Sharma"
            r = rig.do("message close this chat")
            check("'close the chat' closes the open conversation", rig.bridge.open_chat == "" and r.message == "Closed the chat.", r.message)
            r = rig.do("message close this chat")
            check("...and with none open that is said", "No chat is open" in r.message, r.message)
        wa_front = [client("0xw1", "chrome-web.whatsapp.com__-Default", "WhatsApp")]
        with WebRig(WhatsApp11(chats=[dict(c) for c in chats]), hypr=FakeHyprland(wa_front, active="0xw1")) as rig:
            rig.bridge.open_chat = "Rahul Sharma"
            r = rig.do("browser close the group")
            check("LOG: 'close the group' with WhatsApp in front and no tab groups closes the CHAT (was 'no tab groups')",
                  rig.bridge.open_chat == "" and r.message == "Closed the chat.", r.message)
        with WebRig(GroupBridge(version="1.10.0")) as rig:
            r = rig.do("browser open research group")
            check("a TAB group with that name is still expanded (never WhatsApp)", "Expanded" in r.message
                  and not any(c.startswith("chat.") for c, _a in rig.bridge.calls), r.message)
    finally:
        web_macros.EMBEDDER = saved_embed
    # ── third pass, the audit log of 2026-11-11 01:19–01:23 ──
    for said, target, count in (
            ("open and read the last three latest recent chats of mota panda", "mota panda", 3),   # as it was said
            ("read the last 3 messages of mota panda", "mota panda", 3),
            ("read mota panda's last five messages", "mota panda", 5),
            ("what were the last 2 messages from mom", "mom", 2),
            ("read the latest messages from rahul", "rahul", 0)):
        token = d(said, nowhere)
        a = n(token[0]) if token else None
        check(f"LOG: '{said}' -> read {target!r}, count {count} (rules first; the classifier answered with its own example)",
              a is not None and a.key == "message.read" and a.target == target and a.parameters.get("count") == count, (token, a))
    a = n(d("read the last three messages", wa)[0])
    check("'read the last three messages' with WhatsApp in front is the OPEN chat, three of them",
          a.key == "message.read" and a.target == "current" and a.parameters["count"] == 3, a)
    check("'read my last three emails from ravi' and 'the last two chapters of the book' are not WhatsApp",
          d("read my last three emails from ravi", nowhere) is None and d("read the last two chapters of the book", nowhere) is None)
    check("LOG: 'message open' with no name is not a message to someone called 'open'", n("message open") is None
          and n("message open it") is None, n("message open"))
    check("'close whatsapp' is the APP, and a failed close is never handed to the model to reinterpret",
          n("close whatsapp").key == "app.close" and not common._worth_rethinking(
              n("close whatsapp"), web_macros.ActionResult.not_found("WhatsApp isn't open.")))
    saved_embed = web_macros.EMBEDDER
    web_macros.EMBEDDER = False
    try:
        with WebRig(WhatsApp11()) as rig, Models("SHOULD NOT BE ASKED") as models:
            rig.bridge.history["Rahul Sharma"] = [
                {"from": "Rahul Sharma", "text": f"msg {i}", "date": "1/1/2026", "time": "09:00", "out": False} for i in range(1, 9)]
            r = rig.do("message read the last three messages of rahul sharma")
            check("LOG: 'the last three messages of X' opens X and reads exactly the last three, as they are",
                  rig.bridge.open_chat == "Rahul Sharma" and r.message ==
                  "The last 3 in Rahul Sharma: Rahul Sharma: msg 6 ... Rahul Sharma: msg 7 ... Rahul Sharma: msg 8"
                  and models.asked == [], r.message)
            rig.bridge.archive += [{"name": "PR and Outreach Core Team 2026-27", "unread": 0, "preview": "", "when": "1/1/2026"},
                                   {"name": "Outreach volunteers", "unread": 0, "preview": "", "when": "1/1/2026"}]
            rig.bridge.chats = [c for c in rig.bridge.chats if "Outreach" not in c["name"]]
            r = rig.do("message open public relation and outreach coding")
            check("LOG: a name heard wrong is not a flat 'not found' when WhatsApp's search showed near chats: they are offered",
                  r.status == Status.AMBIGUOUS and "PR and Outreach Core Team 2026-27" in r.message
                  and "Outreach volunteers" in r.message and rig.bridge.searching == "", r.message)
        with WebRig(WhatsApp11()) as rig:
            rig.bridge.tabs = [{"id": 1, "title": "Inbox", "url": "https://mail.google.com/"}]
            r = rig.do("message close this chat")
            check("'close the chat' never OPENS WhatsApp when it is not open", not r.ok and "isn't open" in r.message
                  and not any(c in ("tabs.open", "tabs.activate", "chat.close") for c, _a in rig.bridge.calls), (r.message, rig.bridge.calls))
    finally:
        web_macros.EMBEDDER = saved_embed

    def close_whatsapp():
        # The web app runs in "Profile 1": its window class is not the entry's StartupWMClass.
        window = [client("0xw9", "chrome-web.whatsapp.com__-Profile_1", "WhatsApp")]
        hypr = FakeHyprland(window, active="0xw9")
        with WebRig(WhatsApp11(), hypr=hypr) as rig:
            rig.bridge.open_chat = "Rahul Sharma"
            before = len(rig.runner.spawned)
            r = rig.do("open whatsapp")
            check("LOG: 'open whatsapp' sees the web-app window of ANOTHER profile (it waited 8 s and started a second one)",
                  r.ok and len(rig.runner.spawned) == before, (r.message, rig.runner.spawned[before:]))
            r = rig.do("close whatsapp")
            check("LOG: 'close whatsapp' closes the WhatsApp WINDOW — the chat inside it is not touched",
                  rig.bridge.open_chat == "Rahul Sharma" and not any(c == "chat.close" for c, _a in rig.bridge.calls)
                  and "isn't open" not in r.message, (r.message, r.status))
        with WebRig(WhatsApp11(), hypr=FakeHyprland([client("0xc1", "google-chrome", "WhatsApp - Google Chrome")], active="0xc1")) as rig:
            rig.bridge.open_chat = "Rahul Sharma"
            r = rig.do("close whatsapp")
            check("LOG: with WhatsApp only in a Chrome TAB, 'close whatsapp' closes that tab",
                  r.ok and ("tabs.close", {"match": "web.whatsapp.com", "all": True}) in rig.bridge.calls
                  and rig.bridge.open_chat == "Rahul Sharma", (r.message, rig.bridge.calls[-3:]))
    with_desktop_tree(close_whatsapp)
    from kayra.automation import semantic_match as S
    names = ["Family", "Graphics Team (2026-27)", "Rahul", "Graphics Team (2025-26)", "Graphics Design"]
    check("editions: a year said picks that year", S.match("graphics team 2025", names).best.name == "Graphics Team (2025-26)")
    check("editions: one word shared by different groups still ASKS", S.match("graphics", names).decision == "ask")
    check("a year range is one thing however it is said",
          S.tokens("2026 2027") == S.tokens("2026-27") == S.tokens("twenty twenty six to twenty seven") == ["2026", "27"],
          S.tokens("twenty twenty six to twenty seven"))

    # ── Workspace file names ──
    files = [{"id": "p" * 30, "title": "Council Details 2025-26", "url": "https://docs.google.com/spreadsheets/d/" + "p" * 30 + "/edit"},
             {"id": "q" * 30, "title": "Council Details 2026-27 | Sundarbans House",
              "url": "https://docs.google.com/spreadsheets/d/" + "q" * 30 + "/edit"},
             {"id": "r" * 30, "title": "House Council", "url": "https://docs.google.com/spreadsheets/d/" + "r" * 30 + "/edit"}]
    rank = web_macros.rank_titles
    for said in ("council details 2026-27", "council details 2026 2027", "council details 2026 to 27",
                 "council details twenty twenty six twenty seven"):
        top = rank(said, files)[0]
        check(f"file names: {said!r} -> the long real title, strongly", top[1]["id"] == "q" * 30 and top[0] >= 80, top)
    with WebRig(YearFiles(files=files)) as rig, Models("SHOULD NOT BE ASKED") as models:
        r = rig.do("open council details 2026-27 sheet")
        check("the reported one: 'open council details 2026-27 sheet' opens '… | Sundarbans House'",
              (rig.bridge.last("tabs.navigate") or {}).get("url") == files[1]["url"] and "Sundarbans" in r.message, r.message)
        r = rig.do("browser open council details 2026 2027 sheet")
        searched = [a["url"] for c, a in rig.bridge.calls if c in ("tabs.open", "tabs.navigate") and "?q=" in a["url"]]
        check("...the year said the long way is searched for as '2026 27'",
              searched[-1] == "https://docs.google.com/spreadsheets/u/0/?q=council%20details%202026%2027"
              and (rig.bridge.last("tabs.navigate") or {}).get("url") == files[1]["url"], searched)
        r = rig.do("browser open zzz council details sheet")
        check("a name Google's search finds nothing for falls back to the recent list, never a weak guess",
              not r.ok or "Council" in r.message, r.message)
        check("...and no name went to a model (D3)", models.asked == [], models.asked)
    twins = [dict(files[0], title="Budget 2026"), dict(files[1], title="Budget 2025")]
    with WebRig(WorkspaceBridge(files=twins)) as rig:
        r = rig.do("open my budget sheet")
        check("the same name with different years: the one listed first (used last) opens, no question",
              r.ok and (rig.bridge.last("tabs.navigate") or {}).get("url") == twins[0]["url"], r.message)


# ┌────────────────────────────────────────────────────────────────────────┐
# │       PART 6 — FORMS (D1–D5), VIDEO (F1), SITE SEARCH AND SHOPPING      │
# └────────────────────────────────────────────────────────────────────────┘

FORM_FIELDS = [
    {"i": 0, "type": "text", "label": "Full name", "hints": "Full name | name", "autocomplete": "", "sensitive": False,
     "required": True, "filled": False},
    {"i": 1, "type": "email", "label": "Email address", "hints": "Email address", "autocomplete": "email",
     "sensitive": False, "required": True, "filled": False},
    {"i": 2, "type": "tel", "label": "Mobile", "hints": "Mobile", "autocomplete": "", "sensitive": False,
     "required": False, "filled": False},
    {"i": 3, "type": "text", "label": "Father's name", "hints": "Father's name", "autocomplete": "",
     "sensitive": False, "required": True, "filled": False},
    {"i": 4, "type": "date", "label": "Date of birth", "hints": "Date of birth", "autocomplete": "",
     "sensitive": False, "required": False, "filled": False},
    {"i": 5, "type": "select", "label": "Gender", "hints": "Gender", "autocomplete": "", "sensitive": False,
     "required": False, "filled": False, "options": ["Male", "Female", "Other"]},
    {"i": 6, "type": "password", "label": "Password", "hints": "Password", "autocomplete": "new-password",
     "sensitive": True, "required": True, "filled": False},
    {"i": 7, "type": "text", "label": "City", "hints": "City", "autocomplete": "", "sensitive": False,
     "required": False, "filled": True},
    {"i": 8, "type": "checkbox", "label": "I agree to the terms", "hints": "I agree to the terms",
     "autocomplete": "", "sensitive": False, "required": True, "filled": False},
    {"i": 9, "type": "text", "label": "Roll number", "hints": "Roll number | roll", "autocomplete": "",
     "sensitive": False, "required": False, "filled": False},
]


class FormBridge(Bridge):
    """The extension at 1.12.0 with a form, a video and a product on the active page."""

    def __init__(self, url="https://forms.example/apply", fields=None, video=None, product=None,
                 submit=None, file_input=True, version="1.12.0"):
        super().__init__(version=version)
        self.url, self.fields = url, [dict(f) for f in (FORM_FIELDS if fields is None else fields)]
        self.video = dict(video or {"video": True, "paused": False, "time": 200, "duration": 765, "rate": 1.0,
                                    "muted": False, "ad": False, "title": "Attention explained", "captions": False})
        self.product = product
        self.submit = submit or {"submitted": True, "label": "Submit"}
        self.file_input, self.files = file_input, []

    def last(self, cmd):
        return next((a for c, a in reversed(self.calls) if c == cmd), None)

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "tabs.active":
            return {"id": 13, "title": "Page", "url": self.url}
        if cmd == "page.form":
            self.calls.append((cmd, args))
            op = args.get("op")
            if op == "list":
                return {"url": self.url, "fields": [dict(f) for f in self.fields]}
            if op == "fill":
                filled, failed = [], []
                for item in args["items"]:
                    f = self.fields[item["i"]]
                    if f["sensitive"]:
                        failed.append({"i": item["i"], "reason": "sensitive"})
                        continue
                    f["filled"], f["value"] = True, item["value"]
                    filled.append({"i": item["i"], "label": f["label"]})
                return {"filled": filled, "failed": failed}
            if op == "choose":
                if args.get("kind") == "check":
                    return {"done": "agree" in args["field"], "label": "I agree to the terms", "was": False,
                            "now": args.get("state") is not False, "options": ["I agree to the terms"]}
                if args.get("kind") == "date":
                    return {"done": True, "field": "Date of birth", "option": args["option"]}
                field = next((f for f in self.fields if f.get("options") and args["field"] in f["label"].lower()), None)
                if not field:
                    return {"done": False, "reason": "field not found", "fields": ["Gender"]}
                if args["option"].lower() not in [o.lower() for o in field["options"]]:
                    return {"done": False, "field": field["label"], "reason": "no option", "options": field["options"]}
                return {"done": True, "field": field["label"], "option": args["option"]}
            if op == "submit":
                return dict(self.submit)
            if op == "file":
                return {"found": self.file_input, "label": "Upload resume", "armed": bool(args.get("arm"))}
            if op == "fileState":
                return {"files": list(self.files)}
        if cmd == "page.video":
            self.calls.append((cmd, args))
            op, value = args.get("op"), args.get("value")
            v = self.video
            if not v.get("video"):
                return {"video": False}
            if op == "rate":
                v["rate"] = value
            elif op == "seek":
                v["time"] = max(0, v["time"] + value)
            elif op == "seekTo":
                v["time"] = value
            elif op == "pause":
                v["paused"] = True
            elif op == "skipAd":
                return {"skipped": v["ad"] and v.get("skippable", True), "ad": v["ad"], "wait": "Ad · 0:05"}
            elif op == "captions":
                v["captions"] = value
                return {"available": True, "captions": value}
            elif op == "subscribe":
                return {"available": True, "subscribed": True}
            elif op == "like":
                return {"available": True, "was": False, "liked": True}
            return dict(v)
        if cmd == "page.product":
            self.calls.append((cmd, args))
            return self.product or {"found": False}
        if cmd == "page.type":
            self.calls.append((cmd, args))
            return {"typed": args.get("field") != "country", "reason": "field not found"}
        return super().call(cmd, timeout=timeout, **args)


class TempProfile:
    """Points the form profile at a temporary file (never the developer's data/profile.json)."""

    def __enter__(self):
        import tempfile
        from kayra.automation import form_profile
        self.dir = tempfile.mkdtemp(prefix="kayra-profile-")
        self.path = os.path.join(self.dir, "profile.json")
        self._saved, form_profile._path = form_profile._path, (lambda: self.path)
        return self

    def __exit__(self, *exc):
        import shutil
        from kayra.automation import form_profile
        form_profile._path = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class ChooserRunner(RecordingRunner):
    """wtype recorded; the real Space key makes a file chooser window appear (when `opens`)."""

    def __init__(self, hypr, opens=True):
        super().__init__(ALL_TOOLS)
        self.hypr, self.opens = hypr, opens

    def run(self, argv, timeout=4.0, input_text=None, stdin_path=None):
        if self.opens and argv[:1] == ["wtype"] and argv[-1:] == ["space"]:
            self.hypr.clients_list.append(client("0xf11e", "xdg-desktop-portal-gtk", "Open Files", pid=900))
        return super().run(argv, timeout, input_text, stdin_path)


def section_part_6():
    check.section("[21] Part 6 — forms (D1–D5), the video (F1), site search and shopping (F2/F3, D1)")
    from kayra.automation import form_profile, policy
    d = common.direct_tokens
    nowhere = lambda: {}
    youtube = lambda: {"app": "google-chrome", "tab_url": "https://www.youtube.com/watch?v=x"}
    amazon = lambda: {"app": "google-chrome", "tab_url": "https://www.amazon.in/s?k=earbuds"}

    # ── parsing: every shape, bare and with the prefix ──
    table = (
        ("fill this form with my details", "form_fill", "current"),
        ("autofill this form", "form_fill", "current"),
        ("fill in my details", "form_fill", "current"),
        ("submit this form", "form_submit", "current"),
        ("select India in the country dropdown", "form_choose", "India"),
        ("choose male for gender", "form_choose", "male"),
        ("tick i agree", "form_choose", "i agree"),
        ("untick subscribe to newsletter", "form_choose", "subscribe to newsletter"),
        ("check the remember me box", "form_choose", "remember me"),
        ("set the date of birth to 4 may 2003", "form_choose", "2003-05-04"),
        ("upload my resume here", "file_upload", "resume"),
        ("attach my resume", "file_upload", "resume"),
        ("remember my phone number is 98765 43210", "profile_set", "phone"),
        ("save CS21B001 as my roll number", "profile_set", "roll_number"),
        ("what details do you have for forms", "profile_show", "profile"),
        ("forget my phone number", "profile_forget", "phone"),
        ("forget my form profile", "profile_clear", "all"),
        ("skip the ad", "video", "skipAd"),
        ("speed 1.5", "video", "rate"),
        ("play at 2x", "video", "rate"),
        ("normal speed", "video", "rate"),
        ("turn on captions", "video", "captions"),
        ("next video", "video", "next"),
        ("like this video", "video", "like"),
        ("save this to watch later", "video", "watchLater"),
        ("make the video full screen", "video", "fullscreen"),
        ("pause the video", "video", "pause"),
        ("how much is left in this video", "video", "state"),
        ("subscribe to this channel", "video_subscribe", "subscribe"),
        ("search amazon for earbuds under 2000", "site_search", "earbuds"),
        ("search for wireless mouse on flipkart sorted by price low to high", "site_search", "wireless mouse"),
        ("look up rust on wikipedia", "site_search", "rust"),
        ("search github for kokoro onnx", "site_search", "kokoro onnx"),
        ("images of red pandas", "site_search", "red pandas"),
        ("add this to cart", "page_click", "add to cart"),
        ("buy it now", "page_click", "buy now"),
        ("proceed to checkout", "page_click", "checkout"),
    )
    for said, action, target in table:
        bare, prefixed = n(said), n("browser " + said)
        check(f"'{said}' -> browser.{action} {target!r}", bare is not None and bare.action == action
              and bare.target == target and prefixed is not None and prefixed.action == action
              and prefixed.target == target, (bare, prefixed))
        check(f"...rules first, no model: '{said}'", d(said, nowhere) == [f"browser {said}"], d(said, nowhere))
    for token, action, target, params in (
            ("browser skip 10 seconds", "video", "seek", {"op": "seek", "value": 10}),
            ("browser skip ten seconds", "video", "seek", {"op": "seek", "value": 10}),
            ("browser go back 30 seconds", "video", "seek", {"op": "seek", "value": -30}),
            ("browser rewind", "video", "seek", {"op": "seek", "value": -10}),
            ("browser go to 2:30", "video", "seekTo", {"op": "seekTo", "value": 150}),
            ("browser go to 2 minutes", "video", "seekTo", {"op": "seekTo", "value": 120}),
            ("browser play it at one point five times speed", "video", "rate", {"op": "rate", "value": 1.5}),
            ("browser speed up the video", "video", "rate", {"op": "rate", "value": "+0.25"}),
            ("browser unlike this video", "video", "like", {"op": "like", "value": False}),
            ("browser press tab twice", "page_key", "tab", {"count": 2}),
            ("browser press enter", "page_key", "enter", {"count": 1}),
            ("browser sort by price low to high", "shop_refine", "price_asc", {"sort": "price_asc", "lo": 0, "hi": 0}),
            ("browser show only under 2000", "shop_refine", "price", {"sort": "", "lo": 0, "hi": 2000}),
            ("browser how much is this", "product_info", "current", {}),
            ("browser what's the rating", "product_info", "current", {}),
            ("browser set my date of birth to the fourth of may two thousand three", "form_choose", "2003-05-04",
             {"field": "date of birth", "kind": "date", "option": "2003-05-04"})):
        a = n(token)
        check(f"'{token}' -> browser.{action} {target!r} {params}", a is not None and a.action == action
              and a.target == target and a.parameters == params, a)
    a = n("search amazon for the cheapest earbuds between 500 and 1500 in a new tab")
    check("price range, 'cheapest', and where it opens are all read out of the sentence",
          a.parameters == {"site": "amazon", "sort": "price_asc", "lo": 500, "hi": 1500, "where": "tab"}
          and a.target == "earbuds", a)
    check("'two thousand' and '2k' are prices too", common.price_filter("earbuds under two thousand")[2] == 2000
          and common.price_filter("earbuds under 2k rupees") == ("earbuds", 0, 2000))
    # ── context: the same words mean the page only when that page is in front ──
    for said in ("skip 10 seconds", "go back 30 seconds", "rewind", "go to 2:30"):
        check(f"'{said}': rules first with a video in front, the model decides elsewhere",
              d(said, youtube) == [f"browser {said}"] and d(said, nowhere) is None, (d(said, youtube), d(said, nowhere)))
    for said in ("how much is this", "sort by price low to high", "show me the cheapest ones first"):
        check(f"'{said}': rules first on a shop, the model decides elsewhere",
              d(said, amazon) == [f"browser {said}"] and d(said, nowhere) is None, (d(said, amazon), d(said, nowhere)))
    # ── neighbours that must not move ──
    for said, key in (("google search cats in a new tab", "browser.search_web"), ("youtube search lofi", "browser.search_youtube"),
                      ("select all", "keyboard.hotkey"), ("search my drive for budget", "browser.ws_search"),
                      ("search my bookmarks for rust", "browser.bookmarks_search"),
                      ("find rahul on whatsapp", "message.search"), ("add a note saying buy milk", "browser.keep_note"),
                      ("browser fill the email field with ana@x.com", "browser.page_type"),
                      ("browser type hello into search", "browser.page_type")):
        a = n(d(said, nowhere)[0] if d(said, nowhere) else said)
        check(f"unchanged: '{said}' -> {key}", a is not None and a.key == key, a)
    check("'pick a movie for tonight' and 'check my email' are not form commands",
          d("pick a movie for tonight", nowhere) is None and (n("pick a movie for tonight") is None
                                                               or n("pick a movie for tonight").action != "form_choose")
          and common.part_6_action("check my email", "check my email") is None)

    # ── D1: shopping stops before the cart — refused, not asked ──
    for token in ("browser add this to cart", "browser buy it now", "browser click on add to cart",
                  "browser click on proceed to checkout", "browser click on place order", "browser go to cart"):
        check(f"D1: '{token}' is REFUSED", policy_classify(token) == "DENY", policy_classify(token))
    check("D1: a click on the same words on the screen (OCR) is refused too",
          policy.classify_action(policy.Action("mouse", "click_text", target="Add to Cart"))[0] == "DENY")
    check("D1: 'add to cart' is never accepted from the understanding model",
          interpreter.validate(["browser click on add to cart"], n, policy.classify_action) == [])
    for token in ("browser submit this form", "browser subscribe to this channel", "browser forget my form profile"):
        check(f"'{token}' asks first", policy_classify(token) == "CONFIRM", policy_classify(token))
    check("the model may never set, forget or submit the user's details, or choose a file",
          interpreter.validate(["browser remember my email is a@b.com", "browser submit this form",
                                "browser upload my resume here", "browser forget my phone number",
                                "browser skip the ad"], n, policy.classify_action) == ["browser skip the ad"])
    check("the new shapes are in the understanding catalog and every one parses",
          all(p in interpreter.CATALOG for p in ("browser fill this form with my details", "browser skip the ad",
                                                 "browser search amazon for <product> under <price>",
                                                 "browser how much is this"))
          and all(n(t) is not None for t in ("browser fill this form with my details", "browser tick i agree",
                                             "browser skip the ad", "browser images of red pandas")))

    # ── the profile: local, 0600, validated, nothing that looks like a card ──
    with TempProfile() as tp:
        P = form_profile
        check("no profile: an empty dict", P.load() == {})
        check("a spoken email and a spoken phone are stored normalised",
              P.set_value("email", "ana dot r at gmail dot com")[0] and P.set_value("phone", "98765 43210")[0]
              and P.load() == {"email": "ana.r@gmail.com", "phone": "9876543210"}, P.load())
        check("the file is private (0600)", oct(os.stat(tp.path).st_mode & 0o777) == "0o600",
              oct(os.stat(tp.path).st_mode & 0o777))
        check("a bad email, a short phone and a card-shaped value are refused",
              not P.set_value("email", "ana at gmail")[0] and not P.set_value("phone", "12")[0]
              and not P.set_value("college", "4111 1111 1111 1111")[0])
        check("dates are read the ways people say them", [P.parse_date(x) for x in (
            "4 May 2003", "May 4th, 2003", "the fourth of may two thousand three", "04/05/2003", "2003-05-04",
            "31 feb 2001")] == ["2003-05-04"] * 5 + [""])
        P.set_value("name", "Ana Rao")
        P.set_value("dob", "4 may 2003")
        P.set_value("gender", "female")
        check("a full name also gives first and last; gender is capitalised",
              P.load().get("first_name") == "Ana" and P.load().get("last_name") == "Rao"
              and P.load()["gender"] == "Female", P.load())
        keys = {f["label"]: P.key_of_field(f) for f in FORM_FIELDS}
        check("fields map to keys by autocomplete, type and label; someone else's and secret ones never",
              keys == {"Full name": "name", "Email address": "email", "Mobile": "phone", "Father's name": None,
                       "Date of birth": "dob", "Gender": "gender", "Password": None, "City": "city",
                       "I agree to the terms": None, "Roll number": "roll_number"}, keys)
        check("a text date field gets day/month/year; one that says mm/dd gets that",
              P.value_for({"type": "text", "hints": "DOB"}, "dob", P.load()) == "04/05/2003"
              and P.value_for({"type": "text", "hints": "Birthday (mm/dd/yyyy)"}, "dob", P.load()) == "05/04/2003"
              and P.value_for({"type": "date"}, "dob", P.load()) == "2003-05-04")
        check("'my email' / 'roll no' name profile keys", P.key_for("my email") == "email" and P.key_for("roll no") == "roll_number"
              and P.key_for("favourite colour") is None)
        P.forget("phone")
        check("one value forgotten", "phone" not in P.load())
        P.forget()
        check("everything forgotten removes the file", P.load() == {} and not os.path.exists(tp.path))

    # ── D3: fill this form ──
    with TempProfile(), WebRig(FormBridge()) as rig:
        r = rig.do("browser fill this form with my details")
        check("D3: no saved details: says how to give them, fills nothing", r.status == Status.NOT_FOUND
              and "remember my email" in r.message and rig.bridge.last("page.form") is None, r.message)
        for token in ("browser remember my name is Ana Rao", "browser remember my email is ana at x dot com",
                      "browser remember my phone number is 98765 43210", "browser remember my date of birth is 4 May 2003",
                      "browser remember my gender is female"):
            r = rig.do(token)
        check("profile by voice: the last one is saved and said so", r.ok and "gender" in r.message, r.message)
        from kayra.automation.policy import recent_audit
        entry = next(e for e in reversed(recent_audit(40)) if e.get("action") == "browser.profile_set")
        check("the audit log never holds a profile value", "female" not in str(entry).lower()
              and "chars>" in str(entry.get("parameters")), entry)
        r = rig.do("browser what details do you have for forms")
        check("'what details do you have' names them, without the values",
              "name" in r.message and "email" in r.message and "ana" not in r.message.lower(), r.message)
        r = rig.do("browser fill this form with my details")
        check("D3: asks first, naming the fields it can fill and nothing else",
              r.status == Status.NEEDS_CONFIRMATION and "5 fields" in r.message
              and all(w in r.message for w in ("name", "email", "phone number", "date of birth", "gender"))
              and "roll number" in r.message and "don't have" in r.message
              and not any(a.get("op") == "fill" for c, a in rig.bridge.calls if c == "page.form"), r.message)
        pending = linux.CONFIRMATIONS.peek()
        pending_text = str(pending.parameters if pending else "")
        check("...the pending action holds field numbers and keys, never a value",
              "ana@x.com" not in pending_text and "9876543210" not in pending_text and "confirmed" in pending_text,
              pending_text)
        ok, said = linux.resolve_confirmation("yes")
        fill = next(a for c, a in reversed(rig.bridge.calls) if c == "page.form" and a.get("op") == "fill")
        filled = {item["i"]: item["value"] for item in fill["items"]}
        check("...on yes: exactly the empty fields Kayra has values for, in their own formats",
              filled == {0: "Ana Rao", 1: "ana@x.com", 2: "9876543210", 4: "2003-05-04", 5: "Female"}, filled)
        check("...never the password, the father's name, the consent box or the field already filled",
              not set(filled) & {3, 6, 7, 8}, filled)
        check("...and it says it did NOT submit, naming what is still empty",
              "haven't submitted" in said and "Father's name" in said and "Password" in said, said)
        check("...no submit was pressed", not any(a.get("op") == "submit" for c, a in rig.bridge.calls if c == "page.form"))
        rig.bridge.fields = [dict(f, filled=False) for f in FORM_FIELDS]
        rig.do("browser fill this form with my details")
        rig.bridge.url = "https://other.example/"
        ok, said = linux.resolve_confirmation("yes")
        check("...a page that changed before the yes is not filled", "page changed" in said, said)

    # ── D1/D4: choose, tick, date, submit, keys ──
    with TempProfile(), WebRig(FormBridge()) as rig:
        r = rig.do("browser choose male for gender")
        check("D1: a drop-down option", r.ok and r.message == "Chose male for Gender.", r.message)
        r = rig.do("browser choose purple for gender")
        check("D1: a missing option names the real ones", not r.ok and "Male, Female and Other" in r.message, r.message)
        r = rig.do("browser tick i agree")
        check("D1: tick", r.ok and r.message.startswith("Ticked I agree"), r.message)
        r = rig.do("browser set the date of birth to 4 may 2003")
        check("D1: a date, said back in words", r.ok and "4 May 2003" in r.message
              and rig.bridge.last("page.form")["option"] == "2003-05-04", r.message)
        r = rig.do("browser set the country to India")
        check("'set the country to India' on a drop-down: the text box is not there, so the drop-down is chosen",
              rig.bridge.last("page.form").get("kind") == "select" and rig.bridge.last("page.form").get("option") == "India", r.message)
        form_profile.set_value("email", "ana@x.com")
        rig.do("browser fill the email field with my email")
        check("'… with my email' types the saved address, not the words",
              rig.bridge.last("page.type")["text"] == "ana@x.com", rig.bridge.last("page.type"))
        r = rig.do("browser submit this form")
        check("D4: submit asks first and presses nothing", r.status == Status.NEEDS_CONFIRMATION
              and "Submit this form" in r.message and not any(a.get("op") == "submit" for c, a in rig.bridge.calls
                                                               if c == "page.form"), r.message)
        ok, said = linux.resolve_confirmation("yes")
        check("...on yes it presses the form's own button and says which", said == "Pressed Submit.", said)
        rig.bridge.submit = {"submitted": False, "reason": "invalid", "label": "Submit", "missing": ["Father's name"]}
        rig.do("browser submit this form")
        ok, said = linux.resolve_confirmation("yes")
        check("D4: a form the browser would refuse is not reported as sent; the missing field is named",
              said == "I didn't submit it — the form still needs Father's name.", said)
        rig.bridge.submit = {"submitted": False, "reason": "payment", "label": "Place order"}
        rig.do("browser submit this form")
        ok, said = linux.resolve_confirmation("yes")
        check("D1: a 'submit' that is really Place order is refused", "Place order" in said and "yours to do" in said, said)
        r = rig.do("browser press tab twice")
        keys = [a for a in rig.runner.calls if a[:1] == ["wtype"]]
        check("D2: real keys into the page", r.ok and keys[-2:] == [["wtype", "-k", "Tab"]] * 2 and "2 times" in r.message,
              (keys[-2:], r.message))
    with WebRig(FormBridge(version="1.11.0")) as rig:
        r = rig.do("browser fill this form with my details")
        check("an extension older than 1.12.0 is updated, never sent page.form",
              "page.form" not in rig.bridge.cmds(), r.message)

    # ── D5: upload a file ──
    import tempfile
    import shutil
    folder = tempfile.mkdtemp(prefix="kayra-upload-")
    try:
        old_path, new_path = os.path.join(folder, "resume_2025.pdf"), os.path.join(folder, "Ana-Resume-2026.pdf")
        for path, age in ((old_path, 1000), (new_path, 10), (os.path.join(folder, "notes.txt"), 5),
                          (os.path.join(folder, "resume.sh"), 1)):
            open(path, "w").close()
            os.utime(path, (time_now() - age, time_now() - age))
        saved_roots, web_macros.FILE_ROOTS = web_macros.FILE_ROOTS, [folder]
        hypr = FakeHyprland()
        with WebRig(FormBridge(), hypr=hypr, runner=ChooserRunner(hypr)) as rig:
            r = rig.do("browser upload my resume here")
            check("D5: the newest matching file is named in the question, nothing uploaded yet",
                  r.status == Status.NEEDS_CONFIRMATION and "Ana-Resume-2026.pdf" in r.message and "newest of 2" in r.message
                  and not any(a.get("arm") for c, a in rig.bridge.calls if c == "page.form"), r.message)
            rig.bridge.files = ["Ana-Resume-2026.pdf"]
            ok, said = linux.resolve_confirmation("yes")
            typed = [t for a, t in rig.runner.ran if a == ["wtype", "-"]]
            keys = [a for a, _ in rig.runner.ran if a[:1] == ["wtype"] and a != ["wtype", "-"]]
            check("...on yes: the page's input is armed, a REAL Space opens the chooser, the path is typed into it",
                  rig.bridge.last("page.form") is not None and any(a.get("arm") for c, a in rig.bridge.calls if c == "page.form")
                  and ["wtype", "-k", "space"] in keys and ["wtype", "-M", "ctrl", "-k", "l", "-m", "ctrl"] in keys
                  and typed == [new_path] and keys[-1] == ["wtype", "-k", "Return"], (keys, typed))
            check("...and the file name is read back from the page", said == "Attached Ana-Resume-2026.pdf.", said)
        hypr = FakeHyprland()
        with WebRig(FormBridge(), hypr=hypr, runner=ChooserRunner(hypr, opens=False)) as rig:
            rig.do("browser upload my resume here")
            ok, said = linux.resolve_confirmation("yes")
            check("D5: no chooser appeared: nothing is typed anywhere, and it says so",
                  "nothing was uploaded" in said and not any(a == ["wtype", "-"] for a, _ in rig.runner.ran), said)
        with WebRig(FormBridge(file_input=False)) as rig:
            r = rig.do("browser upload my resume here")
            check("D5: a page with no upload field says so", r.status == Status.NOT_FOUND and "upload" in r.message, r.message)
            r = rig.do("browser upload my tax return here")
            check("D5: no such file says so", r.status == Status.NOT_FOUND and "tax return" in r.message, r.message)
    finally:
        web_macros.FILE_ROOTS = saved_roots
        shutil.rmtree(folder, ignore_errors=True)

    # ── F1: the video ──
    with WebRig(FormBridge(url="https://www.youtube.com/watch?v=x")) as rig:
        r = rig.do("browser speed 1.5")
        check("F1: speed", r.message == "Playing at 1.5 times speed.", r.message)
        r = rig.do("browser speed up the video")
        check("F1: 'faster' is a quarter step from where it is", rig.bridge.video["rate"] == 1.75, rig.bridge.video)
        r = rig.do("browser normal speed")
        check("F1: normal speed", r.message == "Playing at normal speed.", r.message)
        r = rig.do("browser skip 30 seconds")
        check("F1: seek ahead, saying where it is now", r.message == "Skipped ahead 30 seconds — now at 3:50.", r.message)
        r = rig.do("browser go back 2 minutes")
        check("F1: seek back by minutes", r.message == "Went back 2 minutes — now at 1:50.", r.message)
        r = rig.do("browser go to 10:05")
        check("F1: jump to a time", r.message == "Jumped to 10:05.", r.message)
        r = rig.do("browser skip the ad")
        check("F1: no ad, said so", r.message == "There's no ad playing.", r.message)
        rig.bridge.video.update(ad=True, skippable=False)
        r = rig.do("browser skip the ad")
        check("F1: an ad that can't be skipped yet says so", not r.ok and "can't be skipped yet" in r.message, r.message)
        r = rig.do("browser turn on captions")
        check("F1: captions", r.message == "Captions on.", r.message)
        r = rig.do("browser how much is left in this video")
        check("F1: where am I", "Attention explained" in r.message and "of 12:45" in r.message, r.message)
        rig.do("browser make the video full screen")
        check("F1: full screen is YouTube's own key, after the page lets go of the keyboard",
              ["wtype", "-k", "f"] in rig.runner.calls and rig.bridge.last("page.video")["op"] == "blur")
        r = rig.do("browser subscribe to this channel")
        check("F1: subscribe asks first", r.status == Status.NEEDS_CONFIRMATION and "Subscribe" in r.message, r.message)
        ok, said = linux.resolve_confirmation("yes")
        check("...then subscribes", said == "Subscribed.", said)
        rig.bridge.video = {"video": False}
        r = rig.do("browser speed 2")
        check("F1: no video on the page, said so", r.status == Status.NOT_FOUND and "no video" in r.message, r.message)

    # ── F2/F3: search a site, refine, read a product ──
    with WebRig(FormBridge(url="https://www.amazon.in/s?k=earbuds&ref=nb")) as rig:
        r = rig.do("browser search amazon for the cheapest earbuds under 2000")
        url = (rig.bridge.last("tabs.open") or {}).get("url", "")
        check("F2: Amazon with a price ceiling (paise) and cheapest first",
              url == "https://www.amazon.in/s?k=earbuds&s=price-asc-rank&rh=p_36%3A-200000"
              and r.message == "Searching Amazon for earbuds under 2,000 rupees, cheapest first.", (url, r.message))
        rig.do("browser search for wireless mouse on flipkart between 500 and 1000")
        url = (rig.bridge.last("tabs.open") or {}).get("url", "")
        check("F2: Flipkart with a price range",
              url.startswith("https://www.flipkart.com/search?q=wireless+mouse&p%5B%5D=facets.price_range.from%3D500"), url)
        rig.do("browser search github for kokoro onnx")
        check("F2: any site with a search page", (rig.bridge.last("tabs.open") or {}).get("url") ==
              "https://github.com/search?q=kokoro+onnx")
        rig.do("browser images of red pandas")
        check("F2: images", (rig.bridge.last("tabs.open") or {}).get("url") ==
              "https://www.google.com/search?tbm=isch&q=red+pandas")
        r = rig.do("browser sort by price high to low")
        nav = (rig.bridge.last("tabs.navigate") or {}).get("url", "")
        check("F3: sort the results in front, keeping the search",
              nav == "https://www.amazon.in/s?k=earbuds&ref=nb&s=price-desc-rank" and "most expensive first" in r.message,
              (nav, r.message))
        r = rig.do("browser how much is this")
        check("F3: no product on the page says how to get one", r.status == Status.NOT_FOUND and "open the first result" in r.message, r.message)
        rig.bridge.product = {"found": True, "name": "boAt Airdopes 141", "price": "1299.00", "currency": "INR",
                              "rating": "4.1 out of 5 stars", "ratings": "2,345", "availability": "InStock"}
        r = rig.do("browser how much is this")
        check("F3: name, price in rupees, rating and stock in one sentence",
              r.message == "boAt Airdopes 141 costs 1,299 rupees. Rated 4.1 out of 5 from 2,345 ratings. In stock.", r.message)
        r = rig.do("browser add this to cart")
        check("D1: refused out loud, nothing clicked", r.status == Status.BLOCKED and "for you to do" in r.message
              and rig.bridge.last("page.click") is None, r.message)
    with WebRig(FormBridge(url="https://news.example/")) as rig:
        r = rig.do("browser sort by price low to high")
        check("F3: sorting is offered only where Kayra can do it honestly", r.status == Status.UNSUPPORTED
              and "Amazon and Flipkart" in r.message, r.message)


# ┌────────────────────────────────────────────────────────────────────────┐
# │   PART 6b — AI SITES (F4), WHATSAPP FILES (F5), SOCIAL (F6), STEPS (G5)  │
# └────────────────────────────────────────────────────────────────────────┘

class AIBridge(Bridge):
    """The extension at 1.13.0 with ChatGPT / Claude / Gemini pages that answer on cue."""

    def __init__(self, version="1.13.0", tabs=None, answer="A monad is a way to chain steps that carry context.",
                 busy_chunks=0, signed_out=False, typed=True):
        super().__init__(version=version, tabs=tabs if tabs is not None else [
            {"id": 1, "title": "Inbox (3) - Gmail", "url": "https://mail.google.com/mail/u/0/", "active": True}])
        self.answer, self.busy_chunks, self.signed_out, self.typed = answer, busy_chunks, signed_out, typed
        self.answers, self.asked = [], []

    def _active(self):
        return next((t for t in self.tabs if t.get("active")), self.tabs[0])

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "tabs.list":
            return [dict(t) for t in self.tabs]
        if cmd == "tabs.active":
            return dict(self._active())
        if cmd == "tabs.open":
            self.calls.append((cmd, args))
            for t in self.tabs:
                t["active"] = False
            tab = {"id": 40 + len(self.tabs), "title": "New chat", "url": args["url"], "active": True}
            self.tabs.append(tab)
            return dict(tab)
        if cmd in ("tabs.activate", "tabs.navigate"):
            self.calls.append((cmd, args))
            tab = next(t for t in self.tabs if t["id"] == args.get("tabId", self._active()["id"]))
            for t in self.tabs:
                t["active"] = t is tab
            if cmd == "tabs.navigate":
                tab["url"], self.answers = args["url"], []
            return dict(tab)
        if cmd == "page.aiChat":
            self.calls.append((cmd, args))
            op = args.get("op")
            state = {"ready": not self.signed_out, "signedOut": self.signed_out, "count": len(self.answers),
                     "busy": False, "last": self.answers[-1] if self.answers else ""}
            if op in ("state", "read"):
                return dict(state, found=bool(self.answers))
            if op == "ask":
                if not self.typed:
                    return dict(state, sent=False, reason="could not type")
                self.asked.append(args["text"])
                return dict(state, sent=True, before=len(self.answers))
            if op == "wait":
                if self.busy_chunks:
                    self.busy_chunks -= 1
                    return dict(state, done=False, busy=True, last="A monad is")
                if len(self.answers) <= args.get("after", 0):
                    self.answers.append(self.answer)
                return dict(state, done=True, count=len(self.answers), last=self.answers[-1])
        return super().call(cmd, timeout=timeout, **args)


class SocialBridge(Bridge):
    """A Reddit / LinkedIn / GitHub page whose list the extension reads."""

    def __init__(self, items=None, url="https://example.com/"):
        super().__init__(version="1.13.0")
        self.url = url
        self.items = items if items is not None else [
            {"title": "Python 3.15 is out", "meta": "r/Python, 4100 points, 380 comments", "url": "https://www.reddit.com/r/Python/1"},
            {"title": "My first package", "meta": "r/Python, 900 points", "url": "https://www.reddit.com/r/Python/2"}]

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "tabs.active":
            return {"id": 7, "title": "Page", "url": self.url}
        if cmd in ("tabs.open", "tabs.navigate"):
            self.calls.append((cmd, args))
            self.url = args["url"]
            return {"id": 7, "url": args["url"], "title": "x"}
        if cmd == "page.social":
            self.calls.append((cmd, args))
            return {"items": [dict(i) for i in self.items], "url": self.url}
        return super().call(cmd, timeout=timeout, **args)


class WhatsAppFiles(WhatsApp11):
    """WhatsApp at 1.13.0 with the Attach menu: the preview shows what the chooser was given."""

    def __init__(self, preview=None, **kw):
        super().__init__(**kw)
        self.version, self.runner, self.preview, self.forced = "1.13.0", None, None, preview
        self.sent_files = []

    def call(self, cmd, timeout=10.0, **args):
        if cmd == "chat.attach":
            self.calls.append((cmd, args))
            op = args.get("op")
            if op == "arm":
                return {"found": bool(self.open_chat), "armed": True, "chat": self.open_chat, "kind": args.get("kind")}
            if op == "state":
                typed = [t for a, t in (self.runner.ran if self.runner else []) if a == ["wtype", "-"]]
                if typed and self.preview is None:
                    self.preview = self.forced if self.forced is not None else [os.path.basename(typed[-1])]
                return {"chat": self.open_chat, "preview": bool(self.preview), "files": list(self.preview or [])}
            if op == "send":
                if not self.preview:
                    return {"sent": False}
                self.sent_files += self.preview
                self.preview = []
                return {"sent": True, "chat": self.open_chat}
        return super().call(cmd, timeout=timeout, **args)


def section_part_6b():
    check.section("[22] Part 6b — ask ChatGPT / Claude / Gemini (F4), WhatsApp reply and files (F5), "
                  "Reddit / LinkedIn / GitHub (F6), several steps in one sentence (G5)")
    import tempfile
    import shutil
    from kayra.automation import workflows
    from kayra.automation.policy import classify_action
    from test_assistant_automation import TempData
    d = common.direct_tokens
    nowhere = lambda: {}
    wa = lambda: {"site": "web.whatsapp.com", "app": "chrome-web.whatsapp.com__-default"}
    reddit = lambda: {"app": "google-chrome", "tab_url": "https://www.reddit.com/r/python/"}
    chatgpt = lambda: {"app": "google-chrome", "tab_url": "https://chatgpt.com/c/1"}

    # ── parsing ──
    for said, key, target, params in (
            ("ask chatgpt what is a monad", "browser.ai_ask", "what is a monad", {"bot": "chatgpt", "new": False}),
            ("ask chat gpt what is a monad", "browser.ai_ask", "what is a monad", {"bot": "chatgpt"}),
            ("ask Claude to write a haiku about Rain", "browser.ai_ask", "write a haiku about Rain", {"bot": "claude"}),
            ("ask gemini about black holes", "browser.ai_ask", "Tell me about black holes", {"bot": "gemini"}),
            ("ask chatgpt in a new chat how tides work", "browser.ai_ask", "how tides work", {"bot": "chatgpt", "new": True}),
            ("start a new chat with claude and ask what's 2+2", "browser.ai_ask", "what's 2+2", {"bot": "claude", "new": True}),
            ("open a new chatgpt chat", "browser.ai_ask", "new chat", {"bot": "chatgpt", "new": True, "question": ""}),
            ("what does gemini think of the new pixel", "browser.ai_ask", "What do you think of the new pixel?", {"bot": "gemini"}),
            ("read chatgpt's answer", "browser.ai_read", "chatgpt", {"full": False}),
            ("read the whole answer", "browser.ai_read", "current", {"full": True}),
            ("what did claude say", "browser.ai_read", "claude", {}),
            ("read my linkedin notifications", "browser.social", "notifications", {"site": "linkedin", "read": True}),
            ("any new github notifications", "browser.social", "notifications", {"site": "github"}),
            ("check my notifications on reddit", "browser.social", "notifications", {"site": "reddit"}),
            ("read my linkedin messages", "browser.social", "messages", {"site": "linkedin"}),
            ("open my linkedin notifications", "browser.social", "notifications", {"site": "linkedin", "read": False}),
            ("what's the top post on r/python this week", "browser.social", "top",
             {"site": "reddit", "name": "python", "period": "this week"}),
            ("what are the top posts in the rust subreddit", "browser.social", "posts", {"site": "reddit", "name": "rust"}),
            ("what's trending on reddit", "browser.social", "top", {"site": "reddit", "name": ""}),
            ("open r/python", "browser.social", "subreddit", {"name": "python", "read": False}),
            ("open the python subreddit", "browser.social", "subreddit", {"name": "python"}),
            ("open r slash india", "browser.social", "subreddit", {"name": "india"}),
            ("open the github repo pallets/flask", "browser.social", "repo", {"name": "pallets/flask"}),
            ("open Kokoro-FastAPI / remsky on github", "browser.social", "repo", {"name": "Kokoro-FastAPI/remsky"}),
            ("search linkedin jobs for data analyst", "browser.site_search", "data analyst", {"site": "linkedin jobs"}),
            ("find python developer jobs on linkedin", "browser.site_search", "python developer", {"site": "linkedin jobs"}),
            ("send my resume to Ravi on whatsapp", "message.send_file", "Ravi", {"what": "resume"}),
            ("send the last screenshot to mom on whatsapp", "message.send_file", "mom", {"what": "last screenshot"}),
            ("open the sheet Ravi sent last week", "browser.ws_search", "ravi", {"kind": "sheet", "open": True}),
            ("total column c", "browser.sheet_calc", "c", {"op": "sum"}),
            ("add up the amount column", "browser.sheet_calc", "amount", {"op": "sum"}),
            ("average column d", "browser.sheet_calc", "d", {"op": "avg"})):
        a = n(said) if key.startswith("message") or said.startswith("open the sheet") else n("browser " + said)
        ok = a is not None and a.key == key and a.target == target and all(a.parameters.get(k) == v for k, v in params.items())
        check(f"'{said}' -> {key} {target!r}", ok, a and (a.key, a.target, a.parameters))
    for token, key, target, params in (
            ("message reply saying I'll be there at 6", "message.send", "current", {"text": "I'll be there at 6"}),
            ("message reply with ok thanks", "message.send", "current", {"text": "ok thanks"}),
            ("message respond to this chat saying see you", "message.send", "current", {"text": "see you"}),
            ("message reply to Rahul saying call me", "message.send", "Rahul", {"text": "call me"}),
            ("message send my resume here", "message.send_file", "current", {"what": "resume"}),
            ("message send this pdf here", "message.send_file", "current", {"what": "pdf"}),
            ("message send the budget.xlsx to Ana", "message.send_file", "Ana", {"what": "budget.xlsx"}),
            ("message Rahul: I'm late", "message.send", "Rahul", {"text": "I'm late"})):
        a = n(token)
        ok = a is not None and a.key == key and a.target == target and all(a.parameters.get(k) == v for k, v in params.items())
        check(f"'{token}' -> {key} {target!r}", ok, a and (a.key, a.target, a.parameters))
    check("the old misreading is gone: 'message reply saying ok' is NOT a message to a contact called 'reply'",
          n("message reply saying ok").target == "current")

    # rules first, no model
    for said, want in (("ask chatgpt what is a monad", ["browser ask chatgpt what is a monad"]),
                       ("read my linkedin notifications", ["browser read my linkedin notifications"]),
                       ("open r/python", ["browser open r/python"]),
                       ("send my resume to Ravi on whatsapp", ["message send my resume to Ravi on whatsapp"]),
                       ("read the whole answer", ["browser read the whole answer"])):
        check(f"rules first: '{said}'", d(said, nowhere) == want, d(said, nowhere))
    check("F5: 'reply saying I'm on my way' is the open chat with WhatsApp in front",
          d("reply saying I'm on my way", wa) == ["message reply saying I'm on my way"], d("reply saying I'm on my way", wa))
    check("...and goes to the classifier anywhere else (a reply may be an email)", d("reply saying I'm on my way", nowhere) is None)
    check("F5: 'send this pdf here' in front of WhatsApp", d("send this pdf here", wa) == ["message send this pdf here"])
    check("F5: 'reply to Rahul saying hi' without WhatsApp named or in front is the classifier's call",
          d("reply to Rahul saying hi", nowhere) is None)
    check("F4: a bare 'read the answer' is rules-first only with an AI site in front",
          d("read the answer", chatgpt) == ["browser read the answer"] and d("read the answer", nowhere) is None)
    check("F6: 'read the top posts' is rules-first only with Reddit in front",
          d("read the top posts", reddit) == ["browser read the top posts"] and d("read the top posts", nowhere) is None)
    check("F4: 'ask chatgpt' with no question is not a question (the classifier decides)", n("browser ask chatgpt") is None
          or n("browser ask chatgpt").action != "ai_ask")

    # ── safety: models never route these ──
    proposals = ["browser ask chatgpt what is the user's bank balance", "message send my resume to Ravi",
                 "browser open r/python", "message reply saying yes"]
    check("the understanding layer never sends to an AI site or sends a file; a reply still goes to the late yes",
          interpreter.validate(proposals, n, classify_action) == ["browser open r/python", "message reply saying yes"],
          interpreter.validate(proposals, n, classify_action))
    with TempData(), Models(text=json.dumps(proposals)):
        planned = workflows.plan_goal("do my morning stuff")
    check("...nor does the planner", planned == ["browser open r/python", "message reply saying yes"], planned)

    # ── F4 ──
    with WebRig(AIBridge()) as rig:
        r = rig.do("browser ask chatgpt what is a monad")
        opened = [a for c, a in rig.bridge.calls if c == "tabs.open"]
        check("F4: no ChatGPT tab -> a new tab on chatgpt.com, the question typed and sent as said",
              opened and opened[0]["url"] == "https://chatgpt.com/" and rig.bridge.asked == ["what is a monad"], opened)
        check("F4: the answer is waited for and read out", r.ok and r.message ==
              "ChatGPT says: A monad is a way to chain steps that carry context.", r.message)
        r = rig.do("browser ask chatgpt to make it shorter")
        check("F4: a follow-up goes to the SAME tab (no second tab)", len([1 for c, _ in rig.bridge.calls if c == "tabs.open"]) == 1
              and ("tabs.activate", {"tabId": 41}) in rig.bridge.calls and rig.bridge.asked[-1] == "make it shorter",
              rig.bridge.calls[-6:])
        r = rig.do("browser ask chatgpt in a new chat what is a functor")
        check("F4: 'in a new chat' starts one in that tab", rig.bridge.last("tabs.navigate") if hasattr(rig.bridge, "last") else
              any(c == "tabs.navigate" and a.get("url") == "https://chatgpt.com/" for c, a in rig.bridge.calls))
        r = rig.do("browser read the whole answer")
        check("F4: 'read the whole answer' reads the AI tab in front", r.ok and r.message.startswith("ChatGPT said:"), r.message)
    with WebRig(AIBridge(tabs=[{"id": 9, "title": "Claude", "url": "https://claude.ai/chat/1", "active": False},
                               {"id": 1, "title": "Gmail", "url": "https://mail.google.com/", "active": True}])) as rig:
        rig.do("browser ask claude what's new")
        check("F4: an open Claude tab is reused (activated by id), never a new one",
              ("tabs.activate", {"tabId": 9}) in rig.bridge.calls and not any(c == "tabs.open" for c, _ in rig.bridge.calls))
    long = ("First point. " * 60) + "Last point."
    with WebRig(AIBridge(answer=long)) as rig:
        r = rig.do("browser ask gemini everything about tides")
        check("F4: a long answer is cut at a sentence and says the rest is on screen",
              len(r.message) < 520 and r.message.endswith("say \"read the whole answer\" to hear it.")
              and r.message.startswith("Gemini says: First point."), r.message)
        check("F4: Gemini opens on its app page", rig.bridge.calls[0][0] == "tabs.open" or
              any(a.get("url") == "https://gemini.google.com/app" for c, a in rig.bridge.calls if c == "tabs.open"))
        r = rig.do("browser read the whole answer")
        check("F4: ...and the whole answer is read on request", "Last point." in r.message, r.message[-60:])
    before = len(common.ANNOUNCED)
    with WebRig(AIBridge(busy_chunks=2)) as rig:
        r = rig.do("browser ask chatgpt to write a long essay")
        check("F4: a slow answer is announced ONCE while Kayra keeps waiting, then read",
              r.ok and "ChatGPT says:" in r.message and common.ANNOUNCED[before:] ==
              ["ChatGPT is still writing. I'll read it when it's done."], (r.message, common.ANNOUNCED[before:]))
    with WebRig(AIBridge(busy_chunks=99)) as rig:
        r = rig.do("browser ask chatgpt to write a long essay")
        check("F4: an answer that never finishes is said as 'still writing, so far', after a bounded wait",
              r.message.startswith("ChatGPT is still writing. So far:")
              and sum(1 for c, a in rig.bridge.calls if c == "page.aiChat" and a.get("op") == "wait") <= 5, r.message)
    with WebRig(AIBridge(signed_out=True)) as rig:
        r = rig.do("browser ask chatgpt what is a monad")
        check("F4: signed out -> says so, nothing typed", not r.ok and "not signed in" in r.message and not rig.bridge.asked, r.message)
    with WebRig(AIBridge(typed=False)) as rig:
        r = rig.do("browser ask claude what is a monad")
        check("F4: text that could not be typed is never sent", not r.ok and "couldn't type" in r.message, r.message)
    with WebRig(AIBridge(version="1.12.0")) as rig:
        rig.do("browser ask chatgpt what is a monad")
        check("F4: an extension older than 1.13.0 is updated, never sent page.aiChat",
              "page.aiChat" not in rig.bridge.cmds())
    with WebRig(AIBridge()) as rig:
        r = rig.do("browser read chatgpt's answer")
        check("F4: reading with no ChatGPT tab says so", r.status == Status.NOT_FOUND, r.message)

    # ── F5: reply in the open chat ──
    with WebRig(WhatsAppFiles()) as rig:
        r = rig.do("message reply saying I'll be there")
        check("F5: a reply with no chat open says so, nothing typed", r.status == Status.NOT_FOUND
              and not [a for c, a in rig.bridge.calls if c == "page.type"], r.message)
        rig.do("message open rahul sharma")
        r = rig.do("message reply saying I'll be there")
        check("F5: a reply asks first, naming the OPEN chat", r.status == Status.NEEDS_CONFIRMATION
              and r.message == "Send \"I'll be there\" to Rahul Sharma?" and not rig.bridge.sent, r.message)
        ok, said = linux.resolve_confirmation("yes")
        check("F5: ...yes sends it to that chat", rig.bridge.sent == ["I'll be there"] and said == "Sent to Rahul Sharma.", said)

    # ── F5: send a file ──
    folder = tempfile.mkdtemp(prefix="kayra-wa-files-")
    shots = tempfile.mkdtemp(prefix="kayra-shots-")
    saved = (web_macros.FILE_ROOTS, web_macros.SCREENSHOT_DIR)
    try:
        resume = os.path.join(folder, "Ana-Resume-2026.pdf")
        for path, age in ((os.path.join(folder, "resume_2024.pdf"), 900), (resume, 10)):
            open(path, "w").close()
            os.utime(path, (time_now() - age, time_now() - age))
        for name, age in (("old.png", 500), ("screenshot-2026-11-11.png", 5)):
            open(os.path.join(shots, name), "w").close()
            os.utime(os.path.join(shots, name), (time_now() - age, time_now() - age))
        web_macros.FILE_ROOTS, web_macros.SCREENSHOT_DIR = [folder], shots
        hypr = FakeHyprland()
        bridge = WhatsAppFiles()
        with WebRig(bridge, hypr=hypr, runner=ChooserRunner(hypr)) as rig:
            bridge.runner = rig.runner
            r = rig.do("message send my resume to rahul sharma")
            check("F5: the file and the RESOLVED chat are both named before anything is sent",
                  r.status == Status.NEEDS_CONFIRMATION and r.message == "Send Ana-Resume-2026.pdf (the newest of 2) to Rahul Sharma?"
                  and bridge.open_chat == "Rahul Sharma" and not [a for c, a in bridge.calls if c == "chat.attach"], r.message)
            ok, said = linux.resolve_confirmation("yes")
            keys = [a for a, _ in rig.runner.ran if a[:1] == ["wtype"] and a != ["wtype", "-"]]
            typed = [t for a, t in rig.runner.ran if a == ["wtype", "-"]]
            arm = next(a for c, a in bridge.calls if c == "chat.attach" and a.get("op") == "arm")
            check("F5: on yes the Attach input is armed as a DOCUMENT, a real Space opens the chooser, the path is typed",
                  arm.get("kind") == "document" and ["wtype", "-k", "space"] in keys and typed == [resume], (arm, keys, typed))
            check("F5: ...and the preview's Send is pressed only once it shows that file",
                  bridge.sent_files == ["Ana-Resume-2026.pdf"] and said == "Sent Ana-Resume-2026.pdf to Rahul Sharma.", said)
        hypr = FakeHyprland()
        bridge = WhatsAppFiles()
        with WebRig(bridge, hypr=hypr, runner=ChooserRunner(hypr)) as rig:
            bridge.runner = rig.runner
            rig.do("message open rahul verma")
            r = rig.do("message send the last screenshot here")
            check("F5: 'the last screenshot here' = the newest screenshot, to the open chat",
                  r.status == Status.NEEDS_CONFIRMATION and r.message == "Send screenshot-2026-11-11.png to Rahul Verma?", r.message)
            linux.resolve_confirmation("yes")
            arm = next(a for c, a in bridge.calls if c == "chat.attach" and a.get("op") == "arm")
            check("F5: ...an image goes as a photo (media), and is sent",
                  arm.get("kind") == "media" and bridge.sent_files == ["screenshot-2026-11-11.png"], (arm, bridge.sent_files))
        hypr = FakeHyprland()
        bridge = WhatsAppFiles()
        with WebRig(bridge, hypr=hypr, runner=ChooserRunner(hypr)) as rig:
            bridge.runner = rig.runner
            rig.do("message send my resume to rahul sharma")
            bridge.open_chat = "Design team 2026"                  # the user clicked another chat meanwhile
            ok, said = linux.resolve_confirmation("yes")
            check("F5: the chat changed before yes -> nothing attached or sent",
                  "no longer the open chat" in said and not bridge.sent_files
                  and not [a for c, a in bridge.calls if c == "chat.attach"], said)
        hypr = FakeHyprland()
        bridge = WhatsAppFiles(preview=["holiday.jpg"])
        with WebRig(bridge, hypr=hypr, runner=ChooserRunner(hypr)) as rig:
            bridge.runner = rig.runner
            rig.do("message send my resume to rahul sharma")
            ok, said = linux.resolve_confirmation("yes")
            check("F5: a preview that shows a DIFFERENT file is never sent", not bridge.sent_files and "not Ana-Resume" in said, said)
        hypr = FakeHyprland()
        bridge = WhatsAppFiles()
        with WebRig(bridge, hypr=hypr, runner=ChooserRunner(hypr, opens=False)) as rig:
            bridge.runner = rig.runner
            rig.do("message send my resume to rahul sharma")
            ok, said = linux.resolve_confirmation("no")
            check("F5: 'no' sends nothing and opens no chooser", said == "Alright, I won't." and not bridge.sent_files
                  and not any(a == ["wtype", "-k", "space"] for a, _ in rig.runner.ran), said)
        with WebRig(WhatsAppFiles()) as rig:
            r = rig.do("message send my tax return to rahul sharma")
            check("F5: no such file says so, no chat opened", r.status == Status.NOT_FOUND and not rig.bridge.open_chat, r.message)
    finally:
        web_macros.FILE_ROOTS, web_macros.SCREENSHOT_DIR = saved
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(shots, ignore_errors=True)

    # ── F6 ──
    with WebRig(SocialBridge()) as rig:
        r = rig.do("browser what's the top post on r/python this week")
        check("F6: the subreddit's top of the week is opened and its first post read",
              rig.bridge.calls[0][1].get("url") == "https://www.reddit.com/r/python/top/?t=week"
              and r.message.startswith("The top post on r/python this week: Python 3.15 is out (r/Python, 4100 points"),
              (rig.bridge.calls[:1], r.message))
        r = rig.do("browser what's trending on reddit")
        check("F6: trending = r/popular's top today", rig.bridge.url == "https://www.reddit.com/r/popular/top/?t=day", rig.bridge.url)
        r = rig.do("browser open r/python")
        check("F6: open r/python", rig.bridge.url == "https://www.reddit.com/r/python/" and r.ok, r.message)
        r = rig.do("browser open the github repo pallets/flask")
        check("F6: a repo by owner/name opens directly", rig.bridge.url == "https://github.com/pallets/flask", rig.bridge.url)
        r = rig.do("browser open the github repo kokoro")
        check("F6: a repo without an owner is GitHub's repository search, said so",
              rig.bridge.url == "https://github.com/search?q=kokoro&type=repositories" and "first result" in r.message, r.message)
        r = rig.do("browser search linkedin jobs for data analyst")
        check("F6: LinkedIn job search", rig.bridge.url == "https://www.linkedin.com/jobs/search/?keywords=data+analyst", rig.bridge.url)
    with WebRig(SocialBridge(items=[{"title": "Ana Rao viewed your profile", "meta": "", "url": ""},
                                    {"title": "Ravi posted: we're hiring", "meta": "", "url": ""}])) as rig:
        r = rig.do("browser read my linkedin notifications")
        check("F6: LinkedIn notifications are opened and read, newest first",
              rig.bridge.url == "https://www.linkedin.com/notifications/" and r.message ==
              "Latest LinkedIn notifications: 1. Ana Rao viewed your profile ... 2. Ravi posted: we're hiring.", r.message)
        n_open = len([1 for c, _ in rig.bridge.calls if c in ("tabs.open", "tabs.navigate")])
        rig.do("browser read my linkedin notifications")
        check("F6: asked again on that page, the page is not reopened",
              len([1 for c, _ in rig.bridge.calls if c in ("tabs.open", "tabs.navigate")]) == n_open)
        r = rig.do("browser open my linkedin messages")
        check("F6: 'open' only opens, it reads nothing", rig.bridge.url == "https://www.linkedin.com/messaging/"
              and rig.bridge.cmds().count("page.social") == 2, rig.bridge.cmds())
    with WebRig(SocialBridge(items=[])) as rig:
        r = rig.do("browser read my github notifications")
        check("F6: an empty list is said honestly (and how to fix a signed-out page)",
              r.ok and "don't see any notifications" in r.message, r.message)

    # ── G5: several steps ──
    for said, want, sit in (
            ("open the budget sheet and total column c", ["browser open the budget sheet", "browser total column c"], nowhere),
            ("open the sheet Ravi sent last week and total column C",
             ["browser open the sheet Ravi sent last week", "browser total column C"], nowhere),
            ("tick i agree and submit this form", ["browser tick i agree", "browser submit this form"], nowhere),
            ("open my budget sheet then read row 5 and then go to cell b2",
             ["browser open my budget sheet", "browser read row 5", "browser go to cell b2"], nowhere),
            ("skip the ad and speed 1.5", ["browser skip the ad", "browser speed 1.5"], nowhere),
            ("ask chatgpt what a monad is and then read the whole answer",
             ["browser ask chatgpt what a monad is", "browser read the whole answer"], nowhere),
            ("ask chatgpt to compare rust and go", ["browser ask chatgpt to compare rust and go"], nowhere),
            ("search amazon for salt and pepper", ["browser search amazon for salt and pepper"], nowhere),
            ("what's the total of 25 and 30", None, nowhere),
            ("total column c and read row 2", None, nowhere)):
        check(f"G5: '{said}'", d(said, sit) == want, d(said, sit))
    with WebRig(WorkspaceBridge(version="1.13.0", url="https://mail.google.com/")) as rig:
        spoken = run_tokens(d("open the budget 2026 sheet and total column b", nowhere))
        order = [c for c in rig.bridge.cmds() if c in ("tabs.navigate", "page.fetchText")]
        check("G5: the sheet is opened (and waited for) BEFORE its column is added up",
              order[:1] == ["tabs.navigate"] and "page.fetchText" in order and "Opened Budget 2026" in spoken
              and "2,050.5" in spoken, (order, spoken))
    with WebRig(WorkspaceBridge(version="1.13.0", files=[], url="https://mail.google.com/")) as rig:
        spoken = run_tokens(["browser open my zzz sheet", "browser total column c"])
        check("G5: when the open fails, the step that needed that page does not run on another page",
              "stopped there" in spoken and "page.fetchText" not in rig.bridge.cmds(), spoken)
    hypr = FakeHyprland()
    with WebRig(FormBridge(version="1.13.0"), hypr=hypr) as rig:
        spoken = run_tokens(["browser tick i agree", "browser submit this form", "browser press enter"])
        check("G5: a step that needs a yes PARKS the rest of the sentence (it used to be dropped)",
              linux.RUNTIME.pending_plan == ["browser press enter"] and "Submit this form?" in spoken, spoken)
        ok, said = linux.resolve_confirmation("yes")
        keys = [a for a, _ in rig.runner.ran if a[:1] == ["wtype"]]
        check("G5: ...yes runs the confirmed step, then the rest", ["wtype", "-k", "Return"] in keys
              and not linux.RUNTIME.pending_plan, (said, keys))
        run_tokens(["browser tick i agree", "browser submit this form", "browser press enter"])
        ok, said = linux.resolve_confirmation("no")
        check("G5: ...no drops the rest and says so", "stopped the rest" in said and not linux.RUNTIME.pending_plan, said)


def policy_classify(token):
    from kayra.automation.policy import classify_action
    return str(getattr(classify_action(n(token))[0], "name", classify_action(n(token))[0])).upper()


def main():
    print_banner("BROWSER CONTROL (round 5)")
    print_info(f"Host: {describe_host()}")
    with EnvironmentGuard() as guard:
        section_registry()
        section_parsing()
        section_opening()
        section_without_bridge()
        section_extension_versions()
        section_app_windows()
        section_understanding()
        section_tuning()
        section_tabs_by_position()
        section_part_2b()
        section_part_2c()
        section_part_3()
        section_part_4()
        section_tuning_v9()
        section_part_5()
        section_fix_round_v11()
        section_part_6()
        section_part_6b()
        real = os.path.join(ROOT, "logs", "automation.log")
        size_before = os.path.getsize(real) if os.path.exists(real) else None
        section_meetings()
        section_share_picker_helper()
        section_audit_log()
        size_after = os.path.getsize(real) if os.path.exists(real) else None
    check.section("[9] The suite has no side effects")
    check("logs/automation.log did not grow during the share tests", size_before == size_after,
          f"{size_before} -> {size_after}")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
