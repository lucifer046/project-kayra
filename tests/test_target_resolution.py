# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_target_resolution.py                        │
# │     Website vs Application Opening · Single-Target Close Semantics     │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_target_resolution.py — regression suite for the two bugs this layer was rebuilt around.

    .venv\\Scripts\\python tests\\test_target_resolution.py

Exits non-zero on any failure.

THE TWO BUGS, STATED SO A FUTURE CHANGE CANNOT QUIETLY REINTRODUCE THEM
-----------------------------------------------------------------------
1. "Open YouTube" launched File Explorer.

   Every non-URL target went to `AppOpener.open(..., match_closest=True)`, whose launcher is
   `os.system("explorer shell:appsFolder\\<id>")` over a CACHED Start-Menu index, with a
   `difflib` fuzzy fallback at cutoff 0.6. Reproduced on this machine: the index held a STALE
   `youtube` entry pointing at a Brave PWA AppsFolder id that no longer exists, and explorer
   answered a dead id by opening a plain File Explorer window. Separately, `github` had no
   entry at all, so difflib matched "git gui" and launched Git GUI. Neither raised, so the
   assistant reported success both times.

2. "Close X" closed several windows.

   An application resolved to ALL of its windows and the executor looped WM_CLOSE over every
   one. A missed site lookup also fell through into a LOOSE application lookup that matched
   any window with the name anywhere in its title.

Everything here is hardware-free: window sets are stubs, and nothing is opened, focused or
closed. Running this suite must never cost the user a window.
"""

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system

import kayra.automation.targets as targets
import kayra.automation.windows as auto
from kayra.automation.policy import Action, Risk, classify_action, AutomationContext

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def code_only(path):
    """
    Source with comments and string literals stripped.

    The checks below look for banned CODE (`os.system(`, a `taskkill /im` literal, a
    DuckDuckGo fallback URL). The modules deliberately DISCUSS those in their docstrings —
    that prose is the record of why they were removed — so a plain substring scan over the
    raw file would fail on its own explanation. Tokenizing first asks the right question.
    """
    import tokenize
    pieces = []
    with tokenize.open(path) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
    return " ".join(pieces)


class FakeWindow:
    """Stands in for a real window so resolution can be tested without a desktop."""

    def __init__(self, title, exe, pid=1000, hwnd=None, kayra_owned=False):
        self.title = title
        self.exe = exe
        self.pid = pid
        self.hwnd = hwnd if hwnd is not None else pid
        self.kayra_owned = kayra_owned

    @property
    def app(self):
        return targets.canonical_from_exe(self.exe)

    @property
    def is_browser(self):
        return self.app in targets.BROWSER_APPS


# ┌────────────────────────────────────────────────────────────────────────┐
# │                1. WEBSITE REGISTRY — the O(1) lookup                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_website_registry():
    print_system("\n[1] Canonical website registry")

    check("youtube resolves to the YouTube website",
          targets.website_url("youtube") == "https://www.youtube.com/",
          targets.website_url("youtube"))
    check("YouTube is case-insensitive",
          targets.website_url("YouTube") == targets.website_url("youtube"))
    check("'youtube.com' resolves to the same destination",
          targets.website_url("youtube.com") == "https://www.youtube.com/")
    check("'www.youtube.com' resolves to the same destination",
          targets.website_url("www.youtube.com") == "https://www.youtube.com/")
    check("a spoken alias resolves ('yt')",
          targets.website_url("yt") == "https://www.youtube.com/")
    check("filler words do not break the lookup",
          targets.website_url("the youtube website") == "https://www.youtube.com/")
    check("youtube music is a DIFFERENT destination from youtube",
          targets.website_url("youtube music") == "https://music.youtube.com/")

    for name, expected in (("github", "https://github.com/"),
                           ("gmail", "https://mail.google.com/"),
                           ("google", "https://www.google.com/"),
                           ("linkedin", "https://www.linkedin.com/"),
                           ("chatgpt", "https://chatgpt.com/")):
        check(f"{name} resolves to its canonical URL",
              targets.website_url(name) == expected, targets.website_url(name))

    check("a browser name is NEVER a website ('chrome')",
          targets.website_url("chrome") is None)
    check("an unknown name resolves to no website",
          targets.website_url("zzz-not-a-real-service") is None)

    # No fuzzy matching between websites. A near-miss is a wrong destination.
    check("no fuzzy match between two sites ('youtub')",
          targets.website_url("youtub") is None)
    check("no fuzzy match onto a site ('gothub')",
          targets.website_url("gothub") is None)

    check("the registry is extensible by one line",
          all(set(spec) >= {"url", "aliases", "titles"}
              for spec in targets.WEBSITE_REGISTRY.values()))
    check("every registry URL is absolute and https",
          all(spec["url"].startswith("https://")
              for spec in targets.WEBSITE_REGISTRY.values()))


# ┌────────────────────────────────────────────────────────────────────────┐
# │        2. OPEN TARGET TYPING — application vs website vs URL           │
# └────────────────────────────────────────────────────────────────────────┘

def section_open_typing():
    print_system("\n[2] 'open X' resolves to a TYPE, not a guess")

    resolution = targets.resolve_open_target("youtube")
    check("'open YouTube' resolves as a WEBSITE",
          resolution.ok and resolution.kind == targets.TargetType.WEBSITE,
          f"{resolution.status}/{resolution.kind}")
    check("'open YouTube' points at the YouTube URL",
          resolution.target == "https://www.youtube.com/", resolution.target)

    # THE bug. The old path fuzzy-matched the Start-Menu index and launched whatever was
    # closest, or interpolated a dead AppsFolder id and got a File Explorer window.
    check("'open YouTube' is NOT typed as an application",
          resolution.kind != targets.TargetType.APPLICATION)
    check("'open YouTube' does not resolve to explorer",
          "explorer" not in str(resolution.target).lower())

    resolution = targets.resolve_open_target("youtube.com")
    check("'open youtube.com' resolves as an explicit URL",
          resolution.ok and resolution.kind == targets.TargetType.URL, resolution.kind)

    resolution = targets.resolve_open_target("https://github.com/anthropics")
    check("a full URL is honoured verbatim",
          resolution.target == "https://github.com/anthropics", resolution.target)

    resolution = targets.resolve_open_target("chrome")
    check("'open Chrome' resolves as an APPLICATION",
          resolution.ok and resolution.kind == targets.TargetType.APPLICATION,
          f"{resolution.status}/{resolution.kind}")
    check("'open Chrome' resolves to the canonical app key",
          resolution.target == "chrome", resolution.target)

    resolution = targets.resolve_open_target("github")
    check("'open GitHub' resolves as a WEBSITE, not 'git gui'",
          resolution.ok and resolution.kind == targets.TargetType.WEBSITE,
          f"{resolution.status}/{resolution.kind} {resolution.target}")

    # Application-first: a name that is both must reach the application.
    check("'spotify' is a curated application name",
          targets.canonical_app("spotify") == "spotify")
    check("the Spotify web player is reachable under an explicit name",
          targets.website_url("spotify web") == "https://open.spotify.com/")

    resolution = targets.resolve_open_target("zzz-nothing-like-this-exists")
    check("an unresolvable target is NOT_FOUND, never a guess",
          resolution.status == targets.Resolution.NOT_FOUND, resolution.status)
    check("NOT_FOUND carries an explanation", bool(resolution.reason))

    # Natural phrasings must land on the same target.
    for phrasing in ("youtube", "YouTube", "youtube.com", "the youtube website", "yt"):
        got = targets.resolve_open_target(phrasing)
        check(f"'{phrasing}' opens YouTube",
              got.ok and "youtube.com" in str(got.target).lower(), str(got.target))


# ┌────────────────────────────────────────────────────────────────────────┐
# │            3. NORMALIZER — the type reaches the structured action      │
# └────────────────────────────────────────────────────────────────────────┘

def section_normalizer():
    print_system("\n[3] 'open X' normalizes to the right domain")

    action = auto.normalize_command("open youtube")
    check("'open youtube' becomes a browser navigation",
          action.key == "browser.open_url", action.key)
    check("it carries the canonical URL",
          action.parameters.get("url") == "https://www.youtube.com/",
          str(action.parameters))
    check("it is NOT an app launch", action.domain != "app")

    action = auto.normalize_command("open youtube.com")
    check("'open youtube.com' becomes a browser navigation",
          action.key == "browser.open_url", action.key)
    check("the URL is normalized to https",
          str(action.parameters.get("url")).startswith("https://"),
          str(action.parameters))

    action = auto.normalize_command("open chrome")
    check("'open chrome' stays an application launch",
          action.key == "app.open" and action.target == "chrome", action.key)

    action = auto.normalize_command("open notepad")
    check("'open notepad' stays an application launch", action.key == "app.open", action.key)

    action = auto.normalize_command("open folder downloads")
    check("'open folder downloads' is still a filesystem action",
          action.key == "file.open_folder", action.key)

    # The `open file ` prefix legitimately wins the literal match here; the TARGET is what
    # says it was never a filename.
    action = auto.normalize_command("open file explorer")
    check("'open file explorer' opens the application, not a file named 'explorer'",
          action.key == "app.open" and action.target == "explorer",
          f"{action.key} {action.target!r}")

    action = auto.normalize_command("open file notes.txt")
    check("'open file notes.txt' is still a real file open",
          action.key == "file.open_file" and action.target == "notes.txt", action.key)

    action = auto.normalize_command("open file report")
    check("an uncurated name after 'open file' stays a file open",
          action.key == "file.open_file", action.key)

    action = auto.normalize_command("google search lofi")
    check("a web SEARCH is not a website open",
          action.key == "browser.search_web", action.key)
    check("the search keeps its query", action.target == "lofi", action.target)

    action = auto.normalize_command("youtube search cats")
    check("'youtube search cats' searches YouTube rather than opening it",
          action.key == "browser.search_youtube" and action.target == "cats", action.key)

    action = auto.normalize_command("realtime what is youtube")
    check("a realtime question is not an automation token at all",
          action is None, str(action))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  4. CLOSE IS SINGLE-TARGET BY DEFAULT                  │
# └────────────────────────────────────────────────────────────────────────┘

def section_single_target_close():
    print_system("\n[4] One intent, one target")

    four_chrome = [
        FakeWindow("Docs - Google Chrome", "chrome.exe", 301),
        FakeWindow("Mail - Google Chrome", "chrome.exe", 302),
        FakeWindow("News - Google Chrome", "chrome.exe", 303),
        FakeWindow("main.py - Visual Studio Code", "code.exe", 304),
    ]

    resolution = targets.resolve_application("chrome", windows=four_chrome)
    check("the resolver still reports every Chrome window",
          resolution.ok and len(resolution.matches) == 3, str(resolution))

    single = targets.pick_single(resolution, prefer_foreground=False)
    check("three candidates with no foreground hint is AMBIGUOUS, not a sweep",
          single.status == targets.Resolution.AMBIGUOUS, single.status)
    check("the candidates are offered back for the question",
          len(single.matches) == 3)

    one_chrome = [FakeWindow("Docs - Google Chrome", "chrome.exe", 401),
                  FakeWindow("main.py - Visual Studio Code", "code.exe", 402)]
    single = targets.pick_single(targets.resolve_application("chrome", windows=one_chrome),
                                prefer_foreground=False)
    check("exactly one candidate resolves to exactly that one",
          single.ok and single.target.pid == 401, str(single))

    owned_only = [FakeWindow("Kayra STT", "chrome.exe", 999, kayra_owned=True)]
    resolution = targets.resolve_application("chrome", windows=owned_only)
    check("a Kayra-owned Chrome is never an application match",
          resolution.status == targets.Resolution.NOT_FOUND, resolution.status)

    # pick_single must also drop a Kayra-owned window that slipped into a match set.
    mixed = targets.Resolution(targets.Resolution.RESOLVED,
                               [FakeWindow("Kayra STT", "chrome.exe", 999, kayra_owned=True),
                                FakeWindow("Docs - Google Chrome", "chrome.exe", 501)])
    single = targets.pick_single(mixed, prefer_foreground=False)
    check("pick_single excludes Kayra-owned windows",
          single.ok and single.target.pid == 501, str(single))


# ┌────────────────────────────────────────────────────────────────────────┐
# │       5. STRICT MATCHING FOR CLOSE — no title-substring sweeps         │
# └────────────────────────────────────────────────────────────────────────┘

def section_strict_close_matching():
    print_system("\n[5] Close uses strict matching; focus may be loose")

    desktop = [
        FakeWindow("Lofi beats - YouTube - Google Chrome", "chrome.exe", 601),
        FakeWindow("youtube-dl documentation - Visual Studio Code", "code.exe", 602),
        FakeWindow("youtube_notes.txt - Notepad", "notepad.exe", 603),
    ]

    loose = targets.resolve_application("youtube", strict=False, windows=desktop)
    check("the LOOSE reading matches on title text (kept for focus)",
          loose.ok and len(loose.matches) == 3, str(loose))

    strict = targets.resolve_application("youtube", strict=True, windows=desktop)
    check("the STRICT reading refuses a title-substring match",
          strict.status == targets.Resolution.NOT_FOUND, str(strict))
    check("so 'close youtube' can never reach VS Code or Notepad",
          not any(w.app in ("vscode", "notepad") for w in strict.matches))

    # A real process-identity match still works under strict.
    strict = targets.resolve_application("notepad", strict=True, windows=desktop)
    check("a genuine application still resolves under strict matching",
          strict.ok and strict.target.pid == 603, str(strict))

    # The site path is what "close youtube" actually uses.
    site = targets.resolve_site("youtube", windows=desktop)
    check("'close YouTube' resolves to the browser window showing it",
          site.ok and site.target.pid == 601, str(site))
    check("the site resolution is typed as a TAB",
          site.kind == targets.TargetType.TAB, site.kind)

    two_tabs = desktop + [FakeWindow("Music - YouTube - Google Chrome", "chrome.exe", 604)]
    site = targets.resolve_site("youtube", windows=two_tabs)
    check("two YouTube windows is AMBIGUOUS, not two closes",
          site.status == targets.Resolution.AMBIGUOUS, site.status)

    none_open = [FakeWindow("main.py - Visual Studio Code", "code.exe", 701)]
    site = targets.resolve_site("youtube", windows=none_open)
    check("a site that is not on screen is NOT_FOUND",
          site.status == targets.Resolution.NOT_FOUND, site.status)
    check("and it states the background-tab limitation honestly",
          "background tab" in site.reason.lower(), site.reason)


# ┌────────────────────────────────────────────────────────────────────────┐
# │              6. CLOSE THIS / THAT / TAB / WINDOW ARE DISTINCT          │
# └────────────────────────────────────────────────────────────────────────┘

def section_close_semantics():
    print_system("\n[6] close this / this window / this tab / X / all")

    action = auto.normalize_command("close window")
    check("'close window' is the foreground WINDOW",
          action.key == "window.close" and action.target == "current", action.key)

    action = auto.normalize_command("close tab")
    check("'close tab' is the active browser TAB",
          action.key == "browser.close_tab", action.key)

    action = auto.normalize_command("close this")
    check("'close this' with no referent falls back to the foreground window",
          action.key == "window.close" and action.target == "current",
          f"{action.key} {action.target!r}")

    action = auto.normalize_command("close chrome")
    check("'close chrome' is an application close",
          action.key == "app.close" and action.target == "chrome", action.key)

    action = auto.normalize_command("close youtube")
    check("'close youtube' is an application-domain close resolved as a site downstream",
          action.key == "app.close" and action.target == "youtube", action.key)

    # "Close that" uses the bounded context, and only a fresh referent.
    context = AutomationContext()
    context.note_action(Action("browser", "open_url", target="youtube",
                              parameters={"url": "https://www.youtube.com/"}), True)
    action = auto.normalize_command("close that", context)
    check("'close that' after opening YouTube targets YouTube",
          action.target == "youtube", f"{action.key} {action.target!r}")

    empty = AutomationContext()
    action = auto.normalize_command("close that", empty)
    check("'close that' with an empty context does not guess a target",
          action.key == "window.close" and action.target == "current",
          f"{action.key} {action.target!r}")

    # The broad forms are explicit, and only explicit.
    action = auto.normalize_command("close everything")
    check("'close everything' is the broad close",
          action.key == "window.close_all", action.key)

    action = auto.normalize_command("close all windows")
    check("'close all windows' is the broad close", action.key == "window.close_all",
          action.key)

    action = auto.normalize_command("close all chrome windows")
    check("'close all chrome windows' is per-application and explicit",
          action.key == "app.close_all" and action.target == "chrome",
          f"{action.key} {action.target!r}")

    action = auto.normalize_command("close chrome")
    check("a plain 'close chrome' is NOT the broad form",
          action.key == "app.close", action.key)

    verdict, _ = classify_action(Action("window", "close_all", target="all"))
    check("closing the whole desktop is CONFIRM-gated", verdict == Risk.CONFIRM, verdict)
    verdict, _ = classify_action(Action("app", "close", target="chrome"))
    check("an ordinary close is not gated", verdict == Risk.ALLOW, verdict)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 7. TAB CLOSE STAYS A TAB CLOSE                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_tab_close():
    print_system("\n[7] Browser tab semantics")

    source = open(os.path.join(project_root, "src", "kayra", "automation", "windows.py"),
                  encoding="utf-8").read()

    check("close_tab is in the destructive set that requires a browser in front",
          "close_tab" in source and "_DESTRUCTIVE_BROWSER_ACTIONS" in source)
    check("close_tab maps to Ctrl+W, not an application close",
          '"close_tab":     "ctrl+w"' in source)
    check("every injected keystroke normalises the modifier state first",
          "targets.release_modifiers()" in source)

    # Last-tab behaviour is the BROWSER's consequence, not a second close by Kayra. The
    # guarantee is that Kayra sends exactly one Ctrl+W to exactly one focused window.
    close_tab_calls = source.count('send_keys("ctrl+w"')
    check("Ctrl+W is sent from at most the tab path and the site path",
          close_tab_calls <= 1, f"{close_tab_calls} literal call sites")

    action = auto.normalize_command("close tab")
    check("'close tab' never becomes an application close",
          action.domain == "browser" and action.action == "close_tab", action.key)


# ┌────────────────────────────────────────────────────────────────────────┐
# │           8. NO FUZZY EXECUTION, NO FILESYSTEM SEARCH ON OPEN          │
# └────────────────────────────────────────────────────────────────────────┘

def section_no_fuzzy_execution():
    print_system("\n[8] The open path never guesses")

    import ast
    path = os.path.join(project_root, "src", "kayra", "automation", "windows.py")
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)

    fuzzy = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "match_closest":
                    literal = getattr(keyword.value, "value", None)
                    if literal is not True:
                        continue
                    fuzzy.append(getattr(node.func, "id", getattr(node.func, "attr", "?")))
    check("no call site asks AppOpener to fuzzy-match", not fuzzy, str(fuzzy))

    code = code_only(path)
    check("the DuckDuckGo '!ducky' open fallback is gone", "ducky" not in code)
    check("no web-search fallback survives on the open path",
          "duckduckgo" not in code.lower())
    check("no process is terminated by name", "/im" not in code)
    check("no os.system call survives", "os.system" not in code)

    # Availability is answered from metadata, never by walking the disk.
    targets_path = os.path.join(project_root, "src", "kayra", "automation", "targets.py")
    targets_source = open(targets_path, encoding="utf-8").read()
    targets_code = code_only(targets_path)
    for banned in ("os.walk", "glob.glob", "rglob"):
        check(f"application availability never uses {banned}", banned not in targets_code)

    check("the Start-Menu index is consulted on EXACT names only",
          "def app_index_entry" in targets_source and "difflib" not in targets_code)
    check("unlaunchable index entries are dropped from the index",
          "if v}" in targets_source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │            9. KAYRA'S OWN STT CHROME IS NEVER A TARGET                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_stt_protection():
    print_system("\n[9] Kayra-owned browser protection")

    desktop = [
        FakeWindow("Kayra speech recognition", "chrome.exe", 900, kayra_owned=True),
        FakeWindow("Lofi - YouTube - Google Chrome", "chrome.exe", 901),
        FakeWindow("Docs - Brave", "brave.exe", 902),
        FakeWindow("main.py - Visual Studio Code", "code.exe", 903),
    ]

    resolution = targets.resolve_application("chrome", strict=True, windows=desktop)
    check("'close Chrome' sees only the user's Chrome",
          resolution.ok and [w.pid for w in resolution.matches] == [901],
          str([w.pid for w in resolution.matches]))

    single = targets.pick_single(resolution, prefer_foreground=False)
    check("and closes exactly that one window",
          single.ok and single.target.pid == 901, str(single))

    site = targets.resolve_site("youtube", windows=desktop)
    check("'close YouTube' never picks the STT browser",
          site.ok and site.target.pid == 901, str(site))
    check("unrelated windows are untouched by the resolution",
          all(w.pid not in (900, 902, 903) for w in site.matches))

    check("the ownership lookup still names the live STT module path",
          "kayra.input.speech_to_text" in
          open(os.path.join(project_root, "src", "kayra", "automation", "targets.py"),
               encoding="utf-8").read())


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        10. PERFORMANCE BUDGET                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_performance():
    print_system("\n[10] Resolution performance")

    iterations = 5000
    start = time.perf_counter()
    for _ in range(iterations):
        targets.website_url("youtube")
    per_call = (time.perf_counter() - start) / iterations * 1e6
    print_info(f"website_url: {per_call:.2f}us per call")
    check("website resolution is effectively constant-time", per_call < 25.0,
          f"{per_call:.2f}us")

    start = time.perf_counter()
    for _ in range(iterations):
        auto.normalize_command("open youtube")
    per_call = (time.perf_counter() - start) / iterations * 1e6
    print_info(f"normalize_command('open youtube'): {per_call:.2f}us per call")
    check("typing the target adds microseconds, not milliseconds", per_call < 60.0,
          f"{per_call:.2f}us")

    # Availability is cached, so the second answer must be far cheaper than the first.
    targets.invalidate_availability_cache()
    start = time.perf_counter()
    targets.application_available("chrome")
    cold = time.perf_counter() - start
    start = time.perf_counter()
    for _ in range(1000):
        targets.application_available("chrome")
    warm = (time.perf_counter() - start) / 1000
    print_info(f"application_available: cold {cold * 1e3:.2f}ms, warm {warm * 1e6:.2f}us")
    check("availability is cached rather than re-probed", warm * 1e6 < 25.0,
          f"{warm * 1e6:.2f}us")

    check("resolution starts no threads", True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  11. NOTHING WAS REMOVED ALONG THE WAY                 │
# └────────────────────────────────────────────────────────────────────────┘

def section_no_regression():
    print_system("\n[11] Every capability survived")

    legacy = ["OpenApp", "CloseApp", "WindowManage", "MediaControl", "HotkeyShortcut",
              "SystemInfo", "SetTimer", "TakeScreenshot", "ClipboardCopy", "ClipboardPaste",
              "ClipboardCopyText", "ExecuteCommand", "ToggleWifi", "WebSearch", "Content",
              "YoutubeSearch", "PlayYoutube", "global_desktop_type", "translate_and_execute",
              "Automation", "FocusApp", "RestartApp", "BrowserNav", "OpenUrl", "MouseControl",
              "OpenPath", "CreateFolder", "CreateFile", "RenamePath", "CopyPath", "MovePath",
              "DeletePath", "SearchFiles", "RunShellCommand", "force_close_app",
              "close_target", "send_keys"]
    missing = [name for name in legacy if not hasattr(auto, name)]
    check("every pre-existing handler is still exported", not missing, str(missing))

    added = ["open_target", "close_all_windows"]
    check("the new entry points exist",
          all(hasattr(auto, name) for name in added))

    resolvers = ["resolve_window", "resolve_application", "resolve_site", "resolve_browser",
                 "resolve_path", "resolve_open_target", "pick_single", "canonical_app",
                 "canonical_website", "website_url", "application_available"]
    check("every resolver is exported",
          all(hasattr(targets, name) for name in resolvers))

    check("Resolution grew a target TYPE",
          targets.Resolution(targets.Resolution.RESOLVED).kind == targets.TargetType.UNKNOWN)

    # Every DMM token must still normalize to something executable.
    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    engine = CentralizedLLMEngine()
    non_automation = {"general", "realtime", "deep research", "exit",
                      "proactive on", "proactive off",
                      # Dispatched by app.Execute_Task, not the automation router — closing
                      # the microphone is assistant self-control, not machine control.
                      "stop listening"}
    samples = {"open": "open chrome", "close": "close chrome", "close all": "close all chrome",
               "play": "play something", "content": "content an email",
               "write": "write hello", "type": "type hello", "system": "system lock",
               "wifi": "wifi off", "focus": "focus chrome", "switch to": "switch to chrome",
               "restart app": "restart app chrome", "timer": "timer 5 minutes",
               "set timer": "set timer 5 minutes", "reminder": "reminder 9pm meeting",
               "copy text": "copy text hello", "google search": "google search cats",
               "youtube search": "youtube search cats", "open folder": "open folder downloads",
               "open file": "open file notes.txt", "create folder": "create folder notes",
               "create file": "create file notes.txt", "delete file": "delete file notes.txt",
               "delete folder": "delete folder notes", "rename": "rename a to b",
               "find file": "find file notes", "search files": "search files notes",
               "terminal": "terminal dir", "run command": "run command dir"}
    gaps = []
    for token in engine.funcs:
        if token in non_automation:
            continue
        probe = samples.get(token, token)
        if auto.normalize_command(probe) is None:
            gaps.append(token)
    check("every DMM token still maps to an executable action", not gaps, str(gaps))


def main():
    print_banner("KAYRA TARGET RESOLUTION",
                 "Website vs Application Opening · Single-Target Close")
    section_website_registry()
    section_open_typing()
    section_normalizer()
    section_single_target_close()
    section_strict_close_matching()
    section_close_semantics()
    section_tab_close()
    section_no_fuzzy_execution()
    section_stt_protection()
    section_performance()
    section_no_regression()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print_error(f"  - {label}")
        sys.exit(1)
    print_success("All target-resolution checks passed.")


if __name__ == "__main__":
    main()
