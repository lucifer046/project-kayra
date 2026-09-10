# ┌────────────────────────────────────────────────────────────────────────┐
# │                      test_browser_selection.py                         │
# │        Browser discovery, preference order and capability policy       │
# └────────────────────────────────────────────────────────────────────────┘
"""
Covers the logic that decides WHICH browser runs speech recognition.

Hardware-free: nothing here launches a browser. What is tested is the decision-making — the
ordering rules, the capability priors, the cache asymmetry and the page-side dead-backend
detector — because that is where the silent-failure risk lives.

The one thing these checks CANNOT establish is whether a given browser on a given machine can
actually reach a speech backend. That requires launching it, and is covered by
`tests/test_stt_lifecycle.py` and by the live measurement recorded in
`docs/KAYRA_SYSTEM_ARCHITECTURE.md`.

Run:  .venv\\Scripts\\python.exe tests/test_browser_selection.py
"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_system, print_info, print_success, print_error
from kayra.input import browsers
from kayra.input.browsers import BrowserSpec, _FAMILIES

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILED += 1
        print_error(f"FAIL  {label}" + (f"  ({detail})" if detail else ""))


def spec(key, default=False):
    """A BrowserSpec with a synthetic path, so ordering can be tested without installs."""
    return BrowserSpec(key, rf"C:\fake\{key}.exe", is_default=default)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    1. CAPABILITY PRIORS                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_priors():
    print_system("\n[1] Recognition capability priors")

    check("Chrome is marked as carrying Google's backend",
          _FAMILIES["chrome"]["recognition"] == "google")
    check("Edge is marked as carrying a vendor backend",
          _FAMILIES["edge"]["recognition"] == "vendor")

    # The measured fact this whole module exists for. Brave exposes webkitSpeechRecognition,
    # start() succeeds, then recognition dies with error 'network'. Verified live on Brave 152.
    check("Brave is marked as having NO speech backend",
          _FAMILIES["brave"]["recognition"] == "none",
          "verified live: API present, start() ok, then error 'network'")

    check("Edge is driven by its own driver", _FAMILIES["edge"]["driver"] == "edge")
    for key in ("brave", "chromium", "opera", "vivaldi"):
        check(f"{key} is driven by ChromeDriver", _FAMILIES[key]["driver"] == "chrome")

    # Firefox has no bundled recognition backend and ships the API disabled, so offering it
    # would only be a slower path to the same failure.
    check("Firefox is deliberately not offered", "firefox" not in _FAMILIES)

    check("Chrome needs no binary_location", not spec("chrome").needs_binary_location())
    check("Brave needs binary_location", spec("brave").needs_binary_location(),
          "without it ChromeDriver silently launches Chrome instead")
    check("Edge needs no binary_location (own driver)", not spec("edge").needs_binary_location())


# ┌────────────────────────────────────────────────────────────────────────┐
# │              2. ORDERING — the rule that protects the user             │
# └────────────────────────────────────────────────────────────────────────┘

def section_ordering(monkey):
    print_system("\n[2] Candidate ordering")

    # The user's real situation on the development host: Brave is the default browser.
    monkey(installed=(spec("brave", default=True), spec("chrome"), spec("edge")), verified=None)
    order = [b.key for b in browsers.candidates()]
    check("a backendless default browser is NOT chosen first", order[0] != "brave",
          f"order={order}")
    check("a working browser is chosen instead", order[0] in ("chrome", "edge"), f"order={order}")
    check("the backendless browser is still offered LAST, not dropped", order[-1] == "brave",
          "so a machine with only that browser gets a specific error, not 'no browser found'")

    # A default browser that DOES work must win — honouring the default is the point.
    monkey(installed=(spec("edge", default=True), spec("chrome")), verified=None)
    order = [b.key for b in browsers.candidates()]
    check("a working default browser IS chosen first", order[0] == "edge", f"order={order}")

    # An explicit setting outranks everything, including the default.
    monkey(installed=(spec("edge", default=True), spec("chrome")), verified=None)
    order = [b.key for b in browsers.candidates("chrome")]
    check("an explicit STT_BROWSER outranks the default", order[0] == "chrome", f"order={order}")

    # An explicit setting for a browser that is not installed must not crash or hijack order.
    order = [b.key for b in browsers.candidates("vivaldi")]
    check("an uninstalled explicit choice is ignored gracefully",
          order and order[0] == "edge", f"order={order}")

    # The verified-working browser is tried before the default, to skip re-probing.
    monkey(installed=(spec("brave", default=True), spec("chrome"), spec("edge")),
           verified="edge")
    order = [b.key for b in browsers.candidates()]
    check("a previously verified browser is tried first", order[0] == "edge", f"order={order}")

    # Nothing installed.
    monkey(installed=(), verified=None)
    check("no browsers installed yields an empty candidate list",
          browsers.candidates() == ())

    # Every candidate list is duplicate-free, or a failed browser would be retried.
    monkey(installed=(spec("chrome", default=True), spec("edge")), verified="chrome")
    order = [b.key for b in browsers.candidates("chrome")]
    check("candidate list has no duplicates", len(order) == len(set(order)), f"order={order}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │           3. CACHE ASYMMETRY — only success is persisted               │
# └────────────────────────────────────────────────────────────────────────┘

def section_cache():
    print_system("\n[3] Capability cache")

    source = open(os.path.join(project_root, "src", "kayra", "input", "browsers.py"),
                  encoding="utf-8").read()

    check("there is a remember_working(), and no remember_broken()",
          "def remember_working(" in source and "def remember_broken(" not in source)

    # Why this matters: a `network` error on an OFFLINE machine says nothing about the browser.
    # Persisting that negative would permanently demote a perfectly good browser after one
    # offline boot, and the user would have no way to know why.
    check("the asymmetry is explained in the source",
          "offline" in source.lower() and "negative" in source.lower())

    check("the cache write is atomic (tmp + replace)",
          "os.replace(" in source)
    check("an unwritable cache does not raise", "except OSError:" in source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │        4. THE PAGE MUST NOT RESTART A DEAD BACKEND FOREVER             │
# └────────────────────────────────────────────────────────────────────────┘

def section_page_guard():
    print_system("\n[4] Page-side dead-backend detection")

    stt = open(os.path.join(project_root, "src", "kayra", "input", "speech_to_text.py"),
               encoding="utf-8").read()

    check("the page exposes a dead-backend flag", "kayraRecognitionDead" in stt)
    check("network errors are counted, not merely retried", "kayraNetworkErrors" in stt)
    check("a successful result clears the counter", "kayraEverRecognized = true" in stt)
    check("there is a bounded retry ceiling", "MAX_NETWORK_ERRORS" in stt)

    # The original bug: onerror treated 'network' as transient and restarted unconditionally,
    # so a backendless browser looped forever and the assistant looked alive while deaf.
    error_block = stt[stt.index("recognition.onerror"):stt.index("recognition.onend")]
    check("the network branch can stop restarting",
          "kayraRecognitionDead = true" in error_block and "return;" in error_block,
          "otherwise a backendless browser loops forever")

    end_block = stt[stt.index("recognition.onend"):stt.index("recognition.start();")]
    check("onend does not resurrect a dead backend",
          "kayraRecognitionDead" in end_block,
          "onend is the other path that made the bounded check unbounded")

    check("service-not-allowed is treated as final, not retried",
          "service-not-allowed" in stt)

    # Verification must happen before the session is declared READY.
    check("the engine verifies recognition before reporting READY",
          "_verify_recognition" in stt and
          stt.index("def _verify_recognition(") < stt.index("SttState.READY"))
    check("verification is time-bounded", "RECOGNITION_PROBE_SECONDS" in stt)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      5. ENGINE INTEGRATION                             │
# └────────────────────────────────────────────────────────────────────────┘

def section_engine():
    print_system("\n[5] Engine integration")

    from kayra.input.speech_to_text import SpeechToTextEngine

    # autostart=False must remain free of side effects: no browser, no driver.
    eng = SpeechToTextEngine(autostart=False, browser="edge")
    try:
        check("browser preference is stored", eng.preferred_browser == "edge")
        check("no driver is created without autostart", eng.driver is None)
        check("no browser is resolved without autostart", eng.browser is None)
        check("no processes are owned without autostart", eng.owned_pids == set())

        # THE SENTINELS AND `None` ARE DIFFERENT INPUTS, and this check used to conflate them.
        #
        # "auto" / "" / "default" are the user SAYING "pick for me". `None` means the caller
        # expressed nothing at all, so the engine reads `STT_BROWSER` from the configuration —
        # which is the whole point of having that setting. Grouping them made this check read
        # the developer's own `.env`: it passed on a machine with no `STT_BROWSER` and failed
        # on one that had set it, on identical code. A tier-1 check must not do that.
        for value in ("auto", "", "default"):
            e2 = SpeechToTextEngine(autostart=False, browser=value)
            check(f"{value!r} means no explicit preference", e2.preferred_browser is None)

        # `None` defers to the CONFIGURATION, and both outcomes are pinned explicitly rather
        # than inherited from whatever this machine happens to have configured.
        #
        # The configuration is substituted at the module's own `env` lookup, not through
        # `os.environ`: `core.config.env()` gives `.env` PRECEDENCE over the process
        # environment (documented, and deliberate), so setting a variable here would change
        # nothing and the check would still be reading the developer's file.
        import kayra.input.speech_to_text as stt_module
        _saved_env = stt_module.env
        try:
            stt_module.env = lambda key, default=None: (
                "edge" if key == "STT_BROWSER" else _saved_env(key, default))
            e3 = SpeechToTextEngine(autostart=False, browser=None)
            check("None defers to the configured STT_BROWSER",
                  e3.preferred_browser == "edge", repr(e3.preferred_browser))

            stt_module.env = lambda key, default=None: (
                "auto" if key == "STT_BROWSER" else _saved_env(key, default))
            e4 = SpeechToTextEngine(autostart=False, browser=None)
            check("a configured 'auto' still means no explicit preference",
                  e4.preferred_browser is None, repr(e4.preferred_browser))

            stt_module.env = lambda key, default=None: (
                None if key == "STT_BROWSER" else _saved_env(key, default))
            e5 = SpeechToTextEngine(autostart=False, browser=None)
            check("an unset STT_BROWSER means no explicit preference",
                  e5.preferred_browser is None, repr(e5.preferred_browser))
        finally:
            stt_module.env = _saved_env
            SpeechToTextEngine._active_instance = None

        e3 = SpeechToTextEngine(autostart=False, browser="  EDGE  ")
        check("preference is normalised (case/whitespace)", e3.preferred_browser == "edge")
    finally:
        SpeechToTextEngine._active_instance = None

    # The options builder must stay usable for every family without launching anything.
    eng2 = SpeechToTextEngine(autostart=False)
    try:
        chrome_opts = eng2._chrome_options(spec("chrome"))
        brave_opts = eng2._chrome_options(spec("brave"))
        edge_opts = eng2._chrome_options(spec("edge"))
        check("Brave options carry binary_location",
              brave_opts.binary_location.endswith("brave.exe"))
        check("Chrome options do not force a binary_location",
              not chrome_opts.binary_location)
        check("Edge options are EdgeOptions",
              type(edge_opts).__module__.endswith("edge.options"))
        check("headless is set for every family",
              all("--headless=new" in o.arguments
                  for o in (chrome_opts, brave_opts, edge_opts)))
        check("the mic permission prompt is bypassed for every family",
              all("--use-fake-ui-for-media-stream" in o.arguments
                  for o in (chrome_opts, brave_opts, edge_opts)))
        check("options build without a spec (historical Chrome path)",
              "--headless=new" in eng2._chrome_options().arguments)
    finally:
        SpeechToTextEngine._active_instance = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │        6. PROBE BUDGET — correctness must not cost cold start          │
# └────────────────────────────────────────────────────────────────────────┘

def section_probe_budget():
    print_system("\n[6] Probe budget and deferred detection")

    from kayra.input.speech_to_text import SpeechToTextEngine

    eng = SpeechToTextEngine(autostart=False)
    try:
        real_prev = browsers.previously_working
        browsers.previously_working = lambda: None
        try:
            # A blanket probe on every start was measured at +2.8s on cold start and pushed
            # crash recovery from 2.5s to 6.5s. Browsers with a first-party backend are
            # therefore trusted at start and checked by capture() instead.
            check("Chrome is not probed at start", eng._probe_budget(spec("chrome")) == 0.0,
                  "has Google's backend")
            check("Edge is not probed at start", eng._probe_budget(spec("edge")) == 0.0,
                  "has Microsoft's backend")

            # An unknown or known-backendless browser IS probed, because for those the failure
            # is the expected outcome and catching it early is what lets the fallback happen
            # before the user tries to speak.
            check("an unknown browser IS probed",
                  eng._probe_budget(spec("chromium")) == eng.RECOGNITION_PROBE_SECONDS)
            check("a known-backendless browser IS probed",
                  eng._probe_budget(spec("brave")) == eng.RECOGNITION_PROBE_SECONDS)

            browsers.previously_working = lambda: "brave"
            check("a previously verified browser is not re-probed",
                  eng._probe_budget(spec("brave")) == 0.0)
        finally:
            browsers.previously_working = real_prev

        # Measured: a backendless build declares itself dead in 1.55-1.78s, so the ceiling
        # must stay comfortably above that.
        check("the probe ceiling exceeds the measured time-to-dead",
              eng.RECOGNITION_PROBE_SECONDS >= 3.0,
              f"{eng.RECOGNITION_PROBE_SECONDS}s vs 1.78s measured worst case")
    finally:
        SpeechToTextEngine._active_instance = None

    stt = open(os.path.join(project_root, "src", "kayra", "input", "speech_to_text.py"),
               encoding="utf-8").read()

    check("there is a browser switch path", "_switch_browser" in stt)
    check("capture() can trigger the switch",
          "no speech backend" in stt[stt.index("def capture("):])

    switch = stt[stt.index("def _switch_browser("):stt.index("def _start_session(")]
    check("the switch rejects the failed browser before rebuilding",
          "_rejected_browsers.add" in switch,
          "otherwise the rebuild reselects it and loops")
    check("the switch tears down before building the replacement",
          switch.index("_teardown_session") < switch.index("_start_session"),
          "two live sessions would mean two browsers holding the microphone")

    start = stt[stt.index("def _start_session("):]
    check("session start skips already-rejected browsers",
          "_rejected_browsers" in start[:start.index("failures = []") + 800])

    # Rejection must not be persisted: an offline boot would otherwise permanently demote a
    # perfectly good browser.
    browsers_src = open(os.path.join(project_root, "src", "kayra", "input", "browsers.py"),
                        encoding="utf-8").read()
    check("rejections are session-scoped, never written to disk",
          "_rejected_browsers" not in browsers_src)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    7. THIS MACHINE (informational)                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_this_machine():
    print_system("\n[7] This machine")

    browsers.discover_browsers.cache_clear()
    installed = browsers.discover_browsers()
    print_info(f"default browser: {browsers.default_browser_key()}")
    for b in installed:
        print_info(f"  {b.label:<16} default={b.is_default!s:<5} backend={b.recognition}")
    if installed:
        print_info("order: " + " -> ".join(b.label for b in browsers.candidates()))

    check("discovery returns BrowserSpec objects",
          all(isinstance(b, BrowserSpec) for b in installed))
    check("every discovered browser has an existing binary",
          all(os.path.isfile(b.binary) for b in installed))
    check("at most one browser is marked default",
          sum(1 for b in installed if b.is_default) <= 1)


def main():
    print_banner("BROWSER SELECTION", "Discovery, preference order & recognition capability")

    real_discover = browsers.discover_browsers
    real_prev = browsers.previously_working

    def monkey(installed, verified):
        browsers.discover_browsers = lambda: installed
        browsers.previously_working = lambda: verified

    try:
        section_priors()
        section_ordering(monkey)
        section_cache()
        section_page_guard()
    finally:
        browsers.discover_browsers = real_discover
        browsers.previously_working = real_prev

    section_engine()
    section_probe_budget()
    section_this_machine()

    print_system("\n" + "=" * 60)
    if FAILED:
        print_error(f"{FAILED} browser-selection check(s) FAILED ({PASSED} passed).")
        return 1
    print_success(f"All browser selection checks passed ({PASSED}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
