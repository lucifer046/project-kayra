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
                  not web.asked and r.status == Status.NOT_FOUND, (web.asked, r.message))
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
            spoken = run_tokens(["open PR and outreach core team chat"])
            check("'open XYZ chat' with WhatsApp in front: the rules miss, the model clicks the chat",
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
                spoken = run_tokens(["open qwerty zxcv chat"])
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
        web_macros.BRIDGE = current = Bridge(version="1.6.0")
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

    def __init__(self, version="1.6.0", url="https://docs.google.com/", comments=None,
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
    check("extension 1.6.0 adds downloads and still no debugger",
          manifest["version"] == "1.6.0" and web_macros.REQUIRED_VERSION == (1, 6, 0)
          and "downloads" in manifest["permissions"] and "debugger" not in manifest["permissions"])


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
