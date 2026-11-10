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
from test_assistant_automation import FakeBridge, WebRig  # noqa: E402

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
    check("no new permission was added (no downloads/debugger yet)",
          not set(manifest["permissions"]) & {"debugger", "downloads", "nativeMessaging"})


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
    check.section("[9] The suite has no side effects")
    check("nothing the developer owns was modified", not guard.modified(), guard.report())
    return check.finish()


if __name__ == "__main__":
    sys.exit(run(main))
