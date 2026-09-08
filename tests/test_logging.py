# ┌────────────────────────────────────────────────────────────────────────┐
# │                          test_logging.py                               │
# │        Structured Terminal Logging — format, ownership, secrets        │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_logging.py — standalone diagnostic for the structured log surface.

    .venv\\Scripts\\python tests\\test_logging.py

HARDWARE-FREE. Nothing here needs a model, a browser or a network.

WHAT IT IS ACTUALLY CHECKING. "Well structured" is easy to claim and easy to lose, and the
ways it is lost are specific: a call site invents a subsystem name, two layers log the same
event, a diagnostic gets promoted to INFO and drowns the nine lines a human reads, or an SDK
error carrying an API key is printed verbatim. Each of those is a check below.

It exercises

  1. One format, and one only.
  2. Canonical subsystem names, and that no call site invents its own.
  3. Levels, thresholds, and DEBUG staying out of INFO.
  4. Secrets: never, in any shape, including ones this module did not compose.
  5. Ownership: one owner per event, asserted against the modules that could duplicate it.
  6. The settings recorder, including the transactional shape.
  7. Third-party noise control that does not hide failures.
  8. Configuration, correlation, and cost.
"""

import os
import re
import sys
import ast
import time
import inspect
import logging

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core import logbus
from kayra.core.logbus import Subsystem, SUBSYSTEMS, LEVELS, format_line, redact
from kayra.core import settings_log

FAILURES = []

# The one shape every Kayra log line has: [HH:MM:SS] [LEVEL] [SUBSYSTEM] message
LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\s*\] \[([A-Z]+)\] (.*)$")


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def code_of(module):
    """
    A module's source with every docstring stripped.

    Several of the ownership rules below are STATED IN PROSE inside the module they govern —
    `llm_engine` documents that fallback is the router's job and not its own. A substring test
    over the raw source then fails on the documentation rather than on the code, which trains
    the reader to delete the explanation.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


class Capture:
    """
    Collects everything written to the console, without printing it.

    Substitutes `safe_print` rather than redirecting stdout: that is the one function every
    line in this codebase goes through, so capturing it proves the routing as well as the
    content.
    """

    def __init__(self):
        self.lines = []
        self._saved = None

    def __enter__(self):
        # Read out of `sys.modules` rather than `import kayra.utils.console as ...`:
        # `kayra.utils.__init__` re-exports the Console OBJECT under the name `console`,
        # which shadows the submodule on the package and makes the plain import return the
        # wrong thing entirely.
        import kayra.utils.console          # noqa: F401  (ensures it is in sys.modules)
        console_module = sys.modules["kayra.utils.console"]
        self._saved = console_module.safe_print
        console_module.safe_print = lambda text, **kwargs: self.lines.append(text)
        self._module = console_module
        return self

    def __exit__(self, *exc):
        self._module.safe_print = self._saved
        return False

    def plain(self):
        """The captured lines with Rich markup and its escapes removed."""
        out = []
        for line in self.lines:
            text = re.sub(r"\[/?[a-z_]+\]", "", line)
            out.append(text.replace("\\[", "["))
        return out

    def text(self):
        return "\n".join(self.plain())


# ──────────────────────────────────────────────────────────────────────────
#                            1. ONE FORMAT
# ──────────────────────────────────────────────────────────────────────────

def section_format():
    print_system("\n[1] One format, for every line")

    line = format_line(logbus.INFO, Subsystem.STT, "Backend: Google Chrome",
                       timestamp="21:48:03")
    check("the format is exactly as specified",
          line == "[21:48:03] [INFO   ] [STT] Backend: Google Chrome", line)

    match = LINE.match(line)
    check("it parses as timestamp, level, subsystem, message", match is not None, line)
    check("the timestamp comes first", match.group(1) == "21:48:03")
    check("then the level", match.group(2) == "INFO")
    check("then the subsystem", match.group(3) == "STT")
    check("then the message", match.group(4) == "Backend: Google Chrome")

    # Every level renders in the same shape, at the same width.
    widths = set()
    for level in LEVELS:
        rendered = format_line(level, Subsystem.LLM, "x", timestamp="00:00:00")
        parsed = LINE.match(rendered)
        check(f"{level} renders in the standard shape", parsed is not None, rendered)
        widths.add(rendered.index("] [", rendered.index("] [") + 1))
    check("the subsystem column is aligned for every level", len(widths) == 1, str(widths))

    # Correlation.
    correlated = format_line(logbus.INFO, Subsystem.AUTO, "Target: YouTube",
                             turn=184, timestamp="00:00:00")
    check("a turn id is carried in the message, not as a fifth column",
          "Turn #184 · Target: YouTube" in correlated, correlated)
    check("and a short one, not a UUID",
          not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", correlated))

    # Every emitted line goes through the format.
    logbus.set_level(logbus.DEBUG)
    with Capture() as cap:
        logbus.info(Subsystem.STT, "one")
        logbus.success(Subsystem.AUTO, "two")
        logbus.warning(Subsystem.LLM, "three")
        logbus.error(Subsystem.TTS, "four")
        logbus.debug(Subsystem.VOICE, "five")
    logbus.set_level(logbus.INFO)
    check("every emitted line matches the format",
          all(LINE.match(line) for line in cap.plain()), cap.text())
    check("all five were emitted at DEBUG level", len(cap.plain()) == 5, str(len(cap.plain())))

    # Brackets in a message must not be swallowed by Rich markup.
    with Capture() as cap:
        logbus.info(Subsystem.TTS, "provider [CUDAExecutionProvider] selected")
    check("brackets inside a message survive",
          "[CUDAExecutionProvider]" in cap.text(), cap.text())


# ──────────────────────────────────────────────────────────────────────────
#                      2. CANONICAL SUBSYSTEM NAMES
# ──────────────────────────────────────────────────────────────────────────

def section_subsystems():
    print_system("\n[2] One canonical name per subsystem")

    expected = {"BOOT", "STT", "VOICE", "DMM", "LLM", "CHAT", "SEARCH", "RESEARCH", "AUTO",
                "TTS", "PROACTIVE", "PRESENCE", "MEMORY", "SETTINGS", "UI", "GPU", "SYSTEM",
                "SHUTDOWN"}
    check("every subsystem in the brief is declared", expected <= SUBSYSTEMS,
          str(sorted(expected - SUBSYSTEMS)))
    check("all names are uppercase", all(name.isupper() for name in SUBSYSTEMS))
    check("all names are short enough to scan",
          all(len(name) <= 10 for name in SUBSYSTEMS),
          str(sorted(n for n in SUBSYSTEMS if len(n) > 10)))
    check("there is exactly one speech-input name",
          len({n for n in SUBSYSTEMS if "SPEECH" in n or "RECOG" in n}) == 0
          and "STT" in SUBSYSTEMS)

    # NO CALL SITE INVENTS A NAME. Every subsystem argument in the retrofitted modules must be
    # a `Subsystem.X` attribute reference, never a bare string — a string is how `[Speech
    # input]`, `[Speech]` and `[Recognizer]` end up in three places.
    from kayra.intelligence import provider_router
    from kayra.input import stt_backend
    from kayra.memory import store
    from kayra.core import settings_log as sl

    for module in (provider_router, stt_backend, store, sl):
        tree = ast.parse(inspect.getsource(module))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name not in ("info", "warning", "error", "success", "debug", "log",
                            "field", "section", "transition"):
                continue
            args = node.args
            first = args[1] if name in ("log", "transition") and len(args) > 1 else (
                args[0] if args else None)
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                offenders.append(first.value)
        check(f"{module.__name__} never passes a literal subsystem name",
              not offenders, str(offenders))

    # And every name that IS used is a declared one.
    for module in (provider_router, stt_backend, store, sl):
        source = inspect.getsource(module)
        used = set(re.findall(r"Subsystem\.([A-Z_]+)", source))
        undeclared = {name for name in used if not hasattr(Subsystem, name)}
        check(f"{module.__name__} uses only declared subsystems",
              not undeclared, str(sorted(undeclared)))


# ──────────────────────────────────────────────────────────────────────────
#                              3. LEVELS
# ──────────────────────────────────────────────────────────────────────────

def section_levels():
    print_system("\n[3] Levels — and DEBUG staying out of INFO")

    check("five printable levels", set(LEVELS) ==
          {"DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"})
    check("four are selectable as a threshold",
          set(logbus.CONFIGURABLE_LEVELS) == {"DEBUG", "INFO", "WARNING", "ERROR"})
    check("SUCCESS is not selectable — it is a rendering level, not a filter",
          "SUCCESS" not in logbus.CONFIGURABLE_LEVELS)

    logbus.set_level(logbus.INFO)
    check("SUCCESS prints at INFO, because 'it worked' is normal-path information",
          logbus.is_enabled(logbus.SUCCESS) is True)
    check("DEBUG does NOT print at INFO", logbus.is_enabled(logbus.DEBUG) is False)
    check("WARNING prints at INFO", logbus.is_enabled(logbus.WARNING) is True)

    logbus.set_level(logbus.WARNING)
    check("at WARNING, INFO is suppressed", logbus.is_enabled(logbus.INFO) is False)
    check("at WARNING, SUCCESS is suppressed too — a quiet log stays quiet",
          logbus.is_enabled(logbus.SUCCESS) is False)
    check("but WARNING and ERROR still print",
          logbus.is_enabled(logbus.WARNING) and logbus.is_enabled(logbus.ERROR))

    logbus.set_level(logbus.ERROR)
    check("at ERROR, only errors print",
          logbus.is_enabled(logbus.ERROR) and not logbus.is_enabled(logbus.WARNING))

    logbus.set_level(logbus.INFO)
    with Capture() as cap:
        logbus.debug(Subsystem.VOICE, "interim=\"Open You...\"")
        logbus.debug(Subsystem.PRESENCE, "candidate=late_night score=0.72")
        logbus.info(Subsystem.VOICE, "State: LISTENING -> USER_SPEAKING")
    check("at INFO, DEBUG lines are not printed at all",
          len(cap.plain()) == 1, str(cap.plain()))
    check("and the INFO line still is", "State:" in cap.text(), cap.text())

    logbus.set_level(logbus.DEBUG)
    with Capture() as cap:
        logbus.debug(Subsystem.VOICE, "interim=\"Open You...\"")
    check("at DEBUG they appear", len(cap.plain()) == 1)
    check("and are marked as DEBUG", "DEBUG" in cap.text(), cap.text())
    logbus.set_level(logbus.INFO)

    check("an unknown level is ignored rather than crashing a boot",
          logbus.set_level("LOUD") == logbus.INFO)
    check("and the threshold is unchanged", logbus.get_level() == logbus.INFO)

    # An expected recoverable failure must not dump a traceback at INFO.
    with Capture() as cap:
        try:
            raise ValueError("session lost")
        except ValueError as exc:
            logbus.exception(Subsystem.STT, "Session lost", exc)
    check("an exception logs one line at INFO", len(cap.plain()) == 1, cap.text())
    check("naming the failure", "ValueError" in cap.text() and "session lost" in cap.text())
    check("and no traceback", "Traceback" not in cap.text() and "File \"" not in cap.text())

    logbus.set_level(logbus.DEBUG)
    with Capture() as cap:
        try:
            raise ValueError("session lost")
        except ValueError as exc:
            logbus.exception(Subsystem.STT, "Session lost", exc)
    check("at DEBUG the traceback IS kept — it is diagnostics, not noise",
          len(cap.plain()) > 1 and "Traceback" in cap.text(), str(len(cap.plain())))
    logbus.set_level(logbus.INFO)


# ──────────────────────────────────────────────────────────────────────────
#                             4. SECRETS
# ──────────────────────────────────────────────────────────────────────────

def section_secrets():
    print_system("\n[4] No secret ever reaches a log line")

    cases = [
        ("CohereAPIKey=zzTOPSECRETzz", "zzTOPSECRETzz"),
        ("GROQ_API_KEY: gsk_liveKeyValue123", "gsk_liveKeyValue123"),
        ("api_key=sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9xxxx", "eyJhbGciOiJIUzI1NiJ9xxxx"),
        ("password=hunter2secretvalue", "hunter2secretvalue"),
        ("access_token = abcdef1234567890abcdef", "abcdef1234567890abcdef"),
        ("my key is gsk_AAAAAAAAAAAAAAAAAAAAAA", "gsk_AAAAAAAAAAAAAAAAAAAAAA"),
        ("google says AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA is bad",
         "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("sk-proj_ABCDEFGHIJKLMNOPQRSTUVWXYZ rejected",
         "sk-proj_ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ]
    for message, secret in cases:
        cleaned = redact(message)
        check(f"redacted: {message[:34]}…", secret not in cleaned, cleaned)

    check("redaction leaves the KEY NAME, so the line is still diagnosable",
          "CohereAPIKey" in redact("CohereAPIKey=zzTOPSECRETzz"))
    check("redaction is applied by format_line itself, not only at call sites",
          "zzTOPSECRETzz" not in format_line(logbus.ERROR, Subsystem.LLM,
                                             "CohereAPIKey=zzTOPSECRETzz",
                                             timestamp="00:00:00"))

    with Capture() as cap:
        logbus.error(Subsystem.LLM, "401 Unauthorized: CohereAPIKey=zzTOPSECRETzz rejected")
    check("and by the emitting path", "zzTOPSECRETzz" not in cap.text(), cap.text())
    check("while the useful part of the error survives",
          "401 Unauthorized" in cap.text(), cap.text())

    # Over-redaction is its own harm: an unreadable error is an undebuggable one.
    for benign in ("Chrome could not reach a speech backend",
                   "provider=CUDAExecutionProvider device=NVIDIA RTX 4060",
                   "Cohere rate limit, cooling down 60s",
                   "opened D:\\Kayra\\data\\conversation.json"):
        check(f"benign text is untouched: {benign[:36]}…", redact(benign) == benign,
              redact(benign))

    # The settings recorder must never print a secret's VALUE.
    recorder = settings_log.SettingsRecorder()
    with Capture() as cap:
        recorder.record("CohereAPIKey", "zzTOPSECRETzz", old_value="")
        recorder.record("GROQ_API_KEY", "gsk_realkey", old_value="old")
        recorder.record("GEMINI_API_KEY", "", old_value="something")
    text = cap.text()
    check("a changed API key logs 'set', not the key",
          "zzTOPSECRETzz" not in text and "set" in text, text)
    check("gsk_realkey never appears", "gsk_realkey" not in text, text)
    check("a cleared key logs 'cleared'", "cleared" in text, text)
    check("and the setting is still named", "Cohere API key" in text, text)

    # Memory content is a secret of a different kind, and is checked the same way.
    from kayra.memory import store
    check("the memory store never logs a record's content",
          not re.search(r"(info|warning|error|debug)\([^)]*\bcontent\b", code_of(store)), "")


# ──────────────────────────────────────────────────────────────────────────
#                            5. OWNERSHIP
# ──────────────────────────────────────────────────────────────────────────

def section_ownership():
    print_system("\n[5] One owner per event — nothing is logged twice")

    from kayra.intelligence import provider_router, llm_engine
    from kayra.input import stt_backend
    from kayra.ui import bridge as ui_bridge, session as ui_session
    from kayra.ui.views import settings as settings_view, memory as memory_view, home
    from kayra.ui import application as ui_app
    from kayra.ui.components import orb, navigation

    # PROVIDER / FALLBACK lines belong to the router.
    router_source = inspect.getsource(provider_router)
    check("the router logs provider attempts", "Provider: " in router_source)
    check("the router logs fallbacks", "Fallback: " in router_source)
    engine_code = code_of(llm_engine)
    check("the engine emits no fallback line of its own",
          "Fallback:" not in engine_code, "")
    check("nor its own provider-attempt line",
          "Generating via" not in engine_code and "Switching to Gemini" not in engine_code)
    check("the chatbot emits no second fallback sequence",
          "Fallback" not in code_of(__import__("kayra.services.chatbot", fromlist=["x"])))

    # BACKEND TRANSITIONS belong to the manager.
    backend_source = inspect.getsource(stt_backend)
    check("the manager logs backend transitions", "Backend switch" in backend_source)
    check("the manager logs session recovery", "Session lost" in backend_source)
    from kayra.input import speech_to_text
    stt_code = code_of(speech_to_text)
    check("the engine no longer prints its own session-lost line",
          "STT session lost" not in stt_code)
    check("nor its own 'session restored' line",
          "STT session restored" not in stt_code)

    # SETTING CHANGES belong to the recorder.
    recorder_source = inspect.getsource(settings_log)
    check("the recorder announces setting changes",
          "label_for(key)" in recorder_source)
    for module, label in ((settings_view, "the settings view"),
                          (ui_bridge, "the bridge"), (ui_session, "the session")):
        check(f"{label} prints no setting-change line of its own",
              "[SETTINGS]" not in code_of(module), label)

    # VOICE STATE TRANSITIONS belong to `app`.
    import kayra.app as app
    app_source = inspect.getsource(app)
    check("app logs voice transitions", "State: {transition.previous}" in app_source)
    for module, label in ((orb, "the orb"), (navigation, "the sidebar"),
                          (home, "Home"), (ui_app, "the window")):
        code = code_of(module)
        check(f"{label} logs no state transition of its own",
              "[VOICE]" not in code and "State:" not in code, label)

    # And no UI module logs through logbus at all — the UI renders, the backend reports.
    for module, label in ((orb, "the orb"), (navigation, "the sidebar"),
                          (home, "Home"), (memory_view, "the memory view"),
                          (settings_view, "the settings view")):
        source = inspect.getsource(module)
        check(f"{label} does not import the logger",
              "from kayra.core.logbus import" not in source
              and "core import logbus" not in source, label)


# ──────────────────────────────────────────────────────────────────────────
#                      6. THE SETTINGS RECORDER
# ──────────────────────────────────────────────────────────────────────────

def section_settings():
    print_system("\n[6] Settings changes — announced once, and transactionally")

    recorder = settings_log.SettingsRecorder()

    from kayra.input.stt_backend import label_for as browser_label

    with Capture() as cap:
        recorder.record("STT_BROWSER", "chrome", old_value="auto",
                        label_value=browser_label)
    check("a change reads 'label: before -> after', in human words",
          "Speech input backend: Automatic -> Google Chrome" in cap.text(), cap.text())
    check("it is tagged [SETTINGS]", "[SETTINGS]" in cap.text(), cap.text())

    with Capture() as cap:
        recorder.record("STT_BROWSER", "chrome", label_value=browser_label)
    check("an unchanged value is silent — a re-sync must not print a page of no-ops",
          cap.plain() == [], cap.text())

    with Capture() as cap:
        recorder.record("PROACTIVE_ENABLED", True, old_value=False)
        recorder.record("PROACTIVE_HUMOR_ENABLED", False, old_value=True)
    text = cap.text()
    check("booleans read as ON and OFF", "OFF -> ON" in text and "ON -> OFF" in text, text)
    check("and are labelled in English",
          "Proactive suggestions" in text and "Humour" in text, text)

    with Capture() as cap:
        recorder.record("TTS_DEVICE_MODE", "GPU", old_value="AUTO")
    check("the TTS device change is announced",
          "TTS device: AUTO -> GPU" in cap.text(), cap.text())

    with Capture() as cap:
        recorder.record("SOME_NEW_SETTING", "x", old_value="y")
    check("an unlabelled setting is still logged, under its key",
          "SOME_NEW_SETTING: y -> x" in cap.text(), cap.text())

    # A long value is bounded so one line cannot run off the screen.
    with Capture() as cap:
        recorder.record("LOCAL_BASE_URL", "http://" + "a" * 200, old_value="")
    check("a very long value is truncated", len(max(cap.plain(), key=len)) < 160,
          str(len(max(cap.plain(), key=len))))

    # Bulk persistence: only the changed values.
    recorder2 = settings_log.SettingsRecorder()
    previous = {"ASSISTANT_NAME": "Kayra", "USERNAME": "Sam", "INPUT_LANGUAGE": "en-US"}
    with Capture() as cap:
        emitted = recorder2.record_many(
            {"ASSISTANT_NAME": "Kayra", "USERNAME": "Alex", "INPUT_LANGUAGE": "en-US"},
            previous)
    check("saving thirty untouched controls logs only what changed",
          emitted == 1, str(emitted))
    check("and names it", "Your name" in cap.text(), cap.text())

    # The transaction.
    with Capture() as cap:
        committed, detail = recorder.apply("STT_BROWSER", "edge",
                                           runtime=lambda: (True, "Microsoft Edge"),
                                           old_value="chrome",
                                           label_value=browser_label)
    text = cap.text()
    check("a transaction announces the request first",
          text.index("Google Chrome -> Microsoft Edge") < text.index("committed"), text)
    check("then commits", committed is True and "committed: Microsoft Edge" in text, text)

    with Capture() as cap:
        committed, detail = recorder.apply("STT_BROWSER", "brave",
                                           runtime=lambda: (False, "no speech backend"),
                                           label_value=browser_label)
    text = cap.text()
    check("a failed transaction says the change failed",
          committed is False and "change failed" in text, text)
    check("and says explicitly that it was NOT committed",
          "not committed" in text, text)
    check("and says what is still in force",
          "still Microsoft Edge" in text, text)

    with Capture() as cap:
        committed, detail = recorder.apply("TTS_DEVICE_MODE", "GPU",
                                           runtime=lambda: (_ for _ in ()).throw(
                                               RuntimeError("CUDA unavailable")))
    check("a runtime operation that raises is a failed change, not a crash",
          committed is False and "CUDA unavailable" in cap.text(), cap.text())

    check("the recorder is a process-wide singleton",
          settings_log.get_settings_recorder() is settings_log.get_settings_recorder())


# ──────────────────────────────────────────────────────────────────────────
#                       7. THIRD-PARTY NOISE
# ──────────────────────────────────────────────────────────────────────────

def section_third_party():
    print_system("\n[7] Third-party noise controlled, failures preserved")

    before = {name: logging.getLogger(name).level
              for name in ("urllib3", "selenium", "httpx", "openai", "cohere")}
    logbus.quiet_third_party()

    for name in ("urllib3", "selenium", "httpx", "openai", "cohere", "requests"):
        level = logging.getLogger(name).level
        check(f"{name} is raised to WARNING", level == logging.WARNING, str(level))
        check(f"{name} is NOT disabled", logging.getLogger(name).disabled is False)
        check(f"{name} still reports errors", level <= logging.ERROR)

    source = inspect.getsource(logbus.quiet_third_party)
    body = source.split('"""')[-1]          # past the docstring, which explains the rules
    check("nothing is raised past WARNING",
          "CRITICAL" not in body and "ERROR" not in body, body)
    check("nothing is disabled", "disabled = True" not in body)
    check("ONNX Runtime is deliberately NOT touched here",
          "onnxruntime" not in body, body)

    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


# ──────────────────────────────────────────────────────────────────────────
#              8. CONFIGURATION, CORRELATION AND COST
# ──────────────────────────────────────────────────────────────────────────

def section_config():
    print_system("\n[8] Configuration, correlation and cost")

    check("the level is read from an environment variable, not from source",
          "KAYRA_LOG_LEVEL" in inspect.getsource(logbus))
    check("file logging is opt-in", "KAYRA_LOG_FILE" in inspect.getsource(logbus))
    check("the file log rotates", "RotatingFileHandler" in inspect.getsource(logbus))
    check("and is bounded", "maxBytes" in inspect.getsource(logbus)
          and "backupCount" in inspect.getsource(logbus))

    saved = os.environ.get("KAYRA_LOG_LEVEL")
    os.environ["KAYRA_LOG_LEVEL"] = "DEBUG"
    try:
        check("DEBUG can be enabled without a code change",
              logbus._resolve_initial_level() == logbus.DEBUG)
        os.environ["KAYRA_LOG_LEVEL"] = "nonsense"
        check("an invalid level falls back to INFO",
              logbus._resolve_initial_level() == logbus.INFO)
    finally:
        if saved is None:
            os.environ.pop("KAYRA_LOG_LEVEL", None)
        else:
            os.environ["KAYRA_LOG_LEVEL"] = saved

    # Correlation.
    logbus.begin_turn(184)
    check("a turn can be opened", logbus.current_turn() == 184)
    with Capture() as cap:
        logbus.info(Subsystem.VOICE, "User: \"Open YouTube\"")
        logbus.info(Subsystem.AUTO, "Target: YouTube")
    check("every line in a turn carries the same id",
          all("Turn #184" in line for line in cap.plain()), cap.text())
    with Capture() as cap:
        logbus.info(Subsystem.SETTINGS, "TTS device: AUTO -> GPU", correlate=False)
    check("a setting change does NOT borrow the open turn's id",
          "Turn #" not in cap.text(), cap.text())
    logbus.end_turn()
    with Capture() as cap:
        logbus.info(Subsystem.BOOT, "ready")
    check("closing a turn stops the correlation", "Turn #" not in cap.text(), cap.text())
    check("auto-incrementing works", logbus.begin_turn() == 1)
    logbus.end_turn()

    # Cost. A log call on a suppressed level must be close to free — the DEBUG lines on the
    # VAD path run at 5-17Hz and a formatted string per call would be pure waste.
    logbus.set_level(logbus.INFO)
    iterations = 50000
    started = time.perf_counter()
    for _ in range(iterations):
        logbus.debug(Subsystem.VOICE, "vad level=0.031 floor=0.019")
    per_call = (time.perf_counter() - started) / iterations * 1e6
    check("a suppressed DEBUG call costs under 25us", per_call < 25.0, f"{per_call:.2f}us")

    started = time.perf_counter()
    for _ in range(20000):
        format_line(logbus.INFO, Subsystem.STT, "Backend: Google Chrome")
    per_call = (time.perf_counter() - started) / 20000 * 1e6
    check("formatting a line costs under 30us", per_call < 30.0, f"{per_call:.2f}us")

    # The module must stay a leaf that renders through the one console.
    source = inspect.getsource(logbus)
    tree = ast.parse(source)
    module_imports = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_imports.add(node.module)
        elif isinstance(node, ast.Import):
            module_imports.update(alias.name for alias in node.names)
    check("logbus imports nothing from kayra but core.paths",
          {i for i in module_imports if i.startswith("kayra")} == {"kayra.core.paths"},
          str(sorted(module_imports)))
    check("it starts no thread", "threading.Thread" not in source)
    check("it renders through the one console", "safe_print" in source)
    check("it does not print directly", not re.search(r"(?<!safe_)\bprint\(", source))


def main():
    print_banner("LOGGING DIAGNOSTIC", "Format · subsystems · levels · secrets · ownership")
    section_format()
    section_subsystems()
    section_levels()
    section_secrets()
    section_ownership()
    section_settings()
    section_third_party()
    section_config()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All logging checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
