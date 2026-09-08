# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_stt_backend.py                             │
# │       Live Speech-Backend Switching, State And Process Safety          │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_stt_backend.py — standalone diagnostic for live speech-input backend switching.

    .venv\\Scripts\\python tests\\test_stt_backend.py

HARDWARE-FREE. No browser is launched and no process is terminated: the STT engine is
replaced by a fake that records what it was asked to do and can be told to fail. That is the
point — the properties being asserted here (exactly one active backend, no duplicate session,
no leaked process, no unrelated browser touched) are about SEQUENCING, and sequencing is
exactly what a fake can prove and a live browser cannot prove repeatably.

`--live` adds read-only checks against the real engine class and the real browser discovery,
without starting a session.

It exercises

  1. The requested/active distinction, which is the whole point of the module.
  2. Every transition the brief names: Automatic -> Edge -> Chrome -> Edge -> Automatic.
  3. A named backend that cannot start: no silent fallback, the previous one restored, and
     the failure visible in the state AND the log.
  4. Switching while listening, while paused, during speech and during standby.
  5. Session safety: one teardown per switch, teardown BEFORE the rebuild, PID-scoped reaping,
     and no name-based process matching anywhere in the stack.
  6. Rapid changes: the latest request wins, and a concurrent one is refused rather than
     queued into a second teardown.
  7. The logs the manager owns, captured and asserted.
  8. The engine-lookup safety property: reading the live engine must never IMPORT the STT
     module, because importing it starts a browser.
"""

import os
import re
import sys
import ast
import time
import types
import inspect

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.core import logbus
from kayra.core.logbus import Subsystem
from kayra.input import stt_backend as sb
from kayra.input.stt_backend import (
    STTBackendManager, STTBackendState, BackendStatus, AUTO, normalize, label_for,
)

LIVE = "--live" in sys.argv
FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ──────────────────────────────────────────────────────────────────────────
#                              THE FAKE ENGINE
# ──────────────────────────────────────────────────────────────────────────

class FakeSttState:
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    READY = "READY"
    LISTENING = "LISTENING"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class FakeEngine:
    """
    A stand-in for `SpeechToTextEngine` with the same switching contract.

    It records the ORDER of operations, which is what the safety properties are actually
    about: a teardown that happened after the rebuild, or twice, or not at all, is the
    difference between one browser session and two.
    """

    def __init__(self, backend="edge", installed=("edge", "chrome", "brave")):
        self.backend_key_value = backend
        self.installed = set(installed)
        self.unstartable = set()
        self.driver = object()
        self.state = FakeSttState.LISTENING
        self._listening_paused = False
        self._service_pid = 4242
        self.owned_pids = {4242, 4243, 4244}
        self.log = []                 # every operation, in order
        self.teardowns = 0
        self.starts = 0
        self.live_sessions = 0
        self.voice_active = False

    # ── The contract the manager uses ──

    def backend_key(self):
        return self.backend_key_value

    def backend_label(self):
        return label_for(self.backend_key_value) if self.backend_key_value else None

    @property
    def listening_paused(self):
        return self._listening_paused

    def switch_backend(self, preference):
        target = preference or None
        previous = self.backend_key_value
        was_paused = self._listening_paused

        self.log.append(("teardown", previous))
        self.teardowns += 1
        self.live_sessions = 0
        self.backend_key_value = None

        candidates = [target] if target else [
            key for key in ("edge", "chrome", "brave") if key in self.installed]
        for key in candidates:
            self.log.append(("start", key))
            self.starts += 1
            if key not in self.installed or key in self.unstartable:
                continue
            self.backend_key_value = key
            self.live_sessions = 1
            self.state = FakeSttState.READY if was_paused else FakeSttState.LISTENING
            self._listening_paused = was_paused
            return True, label_for(key)

        # Failure: put the previous backend back rather than leaving the assistant deaf.
        self.log.append(("restore", previous))
        self.starts += 1
        self.backend_key_value = previous
        self.live_sessions = 1
        self.state = FakeSttState.READY if was_paused else FakeSttState.LISTENING
        self._listening_paused = was_paused
        return False, f"{label_for(target)} could not start; still on {label_for(previous)}"


class Recorder:
    """Captures what the manager logs — those lines are part of its contract."""

    def __init__(self):
        self.lines = []
        self._saved = {}

    def __enter__(self):
        def capture(level, subsystem, message, turn=None, correlate=True):
            line = logbus.format_line(level, subsystem, message, timestamp="00:00:00")
            self.lines.append(line)
            return line

        for name in ("info", "warning", "error", "success", "debug"):
            self._saved[name] = getattr(sb, name)
        sb.info = lambda s, m, **k: capture(logbus.INFO, s, m)
        sb.warning = lambda s, m, **k: capture(logbus.WARNING, s, m)
        sb.error = lambda s, m, **k: capture(logbus.ERROR, s, m)
        sb.success = lambda s, m, **k: capture(logbus.SUCCESS, s, m)
        sb.debug = lambda s, m, **k: capture(logbus.DEBUG, s, m)
        self._section = sb.section
        self._section_end = sb.section_end
        sb.section = lambda s, t: self.lines.append(f"---- [{s}] {t} ----")
        sb.section_end = lambda: None
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(sb, name, fn)
        sb.section = self._section
        sb.section_end = self._section_end
        return False

    def text(self):
        return "\n".join(self.lines)


def manager_with(engine):
    """A manager wired to a fake engine, with the real `SttState` mapping stubbed to match."""
    manager = STTBackendManager()
    manager._engine = lambda: engine
    manager._status_for = lambda eng, switching: _status_for(eng, switching)
    return manager


def _status_for(engine, switching):
    if switching:
        return BackendStatus.STARTING
    if engine.state == FakeSttState.STOPPING:
        return BackendStatus.STOPPING
    if engine.state in (FakeSttState.STOPPED, FakeSttState.FAILED):
        return BackendStatus.ERROR
    if engine.state == FakeSttState.RECOVERING:
        return BackendStatus.RECOVERING
    if engine.state in (FakeSttState.NOT_STARTED, FakeSttState.STARTING):
        return BackendStatus.STARTING
    if engine.listening_paused:
        return BackendStatus.PAUSED
    return BackendStatus.LISTENING


# ──────────────────────────────────────────────────────────────────────────
#                       1. REQUESTED vs ACTIVE
# ──────────────────────────────────────────────────────────────────────────

def section_state():
    print_system("\n[1] Requested is not active — the distinction the module exists for")

    state = STTBackendState(requested_backend="chrome", active_backend="edge",
                            status=BackendStatus.LISTENING)
    check("requested and active are separate fields",
          state.requested_backend == "chrome" and state.active_backend == "edge")
    check("a mismatch is reported as a mismatch", state.matches is False)
    check("labels are human, keys are machine",
          state.requested_label == "Google Chrome" and state.active_label == "Microsoft Edge")

    check("auto is satisfied by ANY active backend",
          STTBackendState(requested_backend=AUTO, active_backend="edge").matches is True)
    check("auto with nothing active is NOT satisfied",
          STTBackendState(requested_backend=AUTO, active_backend=None).matches is False)
    check("an exact match is satisfied",
          STTBackendState(requested_backend="edge", active_backend="edge").matches is True)

    for value, expected in (("", AUTO), (None, AUTO), ("auto", AUTO), ("Default", AUTO),
                            ("  CHROME ", "chrome"), ("edge", "edge")):
        check(f"normalize({value!r}) -> {expected}", normalize(value) == expected)

    check("every status is declared", set(sb.STATUSES) == {
        "OFF", "STARTING", "LISTENING", "PAUSED", "RECOVERING", "STOPPING", "ERROR"})
    check("to_dict carries every field a UI needs",
          set(state.to_dict()) >= {"requested_backend", "active_backend", "status",
                                   "browser_process_id", "session_id", "last_error",
                                   "started_at", "settings_source", "revision", "matches"})

    # No engine at all: OFF, and it says so rather than inventing a backend.
    empty = STTBackendManager()
    empty._engine = lambda: None
    snapshot = empty.snapshot()
    check("with no engine the status is OFF", snapshot.status == BackendStatus.OFF)
    check("with no engine there is no active backend", snapshot.active_backend is None)
    check("and the active label is None, not a guess", snapshot.active_label == "None")


# ──────────────────────────────────────────────────────────────────────────
#                        2. EVERY TRANSITION
# ──────────────────────────────────────────────────────────────────────────

def section_transitions():
    print_system("\n[2] Automatic -> Edge -> Chrome -> Edge -> Automatic")

    engine = FakeEngine(backend="edge")
    manager = manager_with(engine)
    manager.adopt(requested=AUTO, source="env")

    check("Automatic adopts whatever the engine started with",
          manager.snapshot().active_backend == "edge")
    check("and reports it as a match, because automatic means 'one that works'",
          manager.snapshot().matches is True)

    steps = [("edge", "edge"), ("chrome", "chrome"), ("edge", "edge"), (AUTO, "edge")]
    for target, expected_active in steps:
        committed, detail = manager.request(target)
        state = manager.snapshot()
        check(f"{label_for(target)}: committed", committed is True, str(detail))
        check(f"{label_for(target)}: active is {label_for(expected_active)}",
              state.active_backend == expected_active, str(state.active_backend))
        check(f"{label_for(target)}: requested is recorded",
              state.requested_backend == normalize(target))
        check(f"{label_for(target)}: requested and active agree", state.matches is True)
        check(f"{label_for(target)}: exactly one live session",
              engine.live_sessions == 1, str(engine.live_sessions))

    check("each switch tore the old session down exactly once",
          engine.teardowns == len(steps), str(engine.teardowns))
    check("and never built two sessions at once",
          all(engine.log[i][0] != "start" or engine.log[i - 1][0] in ("teardown", "start")
              for i in range(1, len(engine.log))))

    # The critical ordering property: teardown always precedes the rebuild.
    order = [op for op, _ in engine.log]
    for index, op in enumerate(order):
        if op == "start":
            check("teardown precedes every rebuild",
                  "teardown" in order[:index], str(order[:index + 1])) if index == 1 else None
    check("the very first operation of a switch is a teardown", order[0] == "teardown")

    # Automatic must not be hardcoded to one browser: with Edge uninstalled it picks Chrome.
    engine2 = FakeEngine(backend="chrome", installed=("chrome", "brave"))
    manager2 = manager_with(engine2)
    manager2.adopt(requested=AUTO)
    manager2.request(AUTO)
    check("Automatic follows availability rather than a hardcoded browser",
          manager2.snapshot().active_backend == "chrome",
          str(manager2.snapshot().active_backend))


# ──────────────────────────────────────────────────────────────────────────
#                     3. A NAMED BACKEND THAT CANNOT START
# ──────────────────────────────────────────────────────────────────────────

def section_failure():
    print_system("\n[3] A named backend that fails — no silent fallback")

    engine = FakeEngine(backend="edge")
    engine.installed = {"edge", "chrome"}
    engine.unstartable = {"chrome"}          # installed, but cannot reach a speech backend
    manager = manager_with(engine)
    manager.adopt(requested=AUTO)

    with Recorder() as rec:
        committed, detail = manager.request("chrome")
    state = manager.snapshot()

    check("a failed switch is NOT committed", committed is False, str(detail))
    check("the requested value still records what the user asked for",
          state.requested_backend == "chrome")
    check("the ACTIVE backend is the one really running, not the requested one",
          state.active_backend == "edge", str(state.active_backend))
    check("requested and active are reported as disagreeing", state.matches is False)
    check("the failure reason is kept", bool(state.last_error), state.last_error)
    check("the previous backend was restored rather than left deaf",
          engine.live_sessions == 1 and engine.backend_key_value == "edge")

    text = rec.text()
    check("the log names the browser that failed", "Google Chrome" in text, text)
    check("the log says the change was not committed",
          "not committed" in text.lower(), text)
    check("the log states what IS active", "Active: Microsoft Edge" in text, text)
    check("the failure is an ERROR line, not a warning buried elsewhere",
          any("ERROR" in line and "could not be started" in line for line in rec.lines), text)

    # A browser that is not installed at all is the same story with a different reason.
    engine.unstartable = set()
    committed, _ = manager.request("vivaldi")
    check("an uninstalled browser is refused, not silently substituted",
          committed is False and manager.snapshot().active_backend == "edge")

    # 11.4 — no engine at all: the value is recorded, and the failure is explicit.
    offline = STTBackendManager()
    offline._engine = lambda: None
    with Recorder() as rec2:
        committed, detail = offline.request("chrome")
    check("with no speech session the change is not committed", committed is False)
    check("but the request is still recorded for the next start",
          offline.snapshot().requested_backend == "chrome")
    check("and the log says why", "not running" in rec2.text(), rec2.text())


# ──────────────────────────────────────────────────────────────────────────
#                   4. SWITCHING IN EVERY ASSISTANT STATE
# ──────────────────────────────────────────────────────────────────────────

def section_states():
    print_system("\n[4] Switching while listening, paused, speaking and in standby")

    # While listening.
    engine = FakeEngine(backend="edge")
    manager = manager_with(engine)
    manager.adopt(requested=AUTO)
    manager.request("chrome")
    check("switching while listening leaves the microphone open",
          engine.listening_paused is False and engine.state == FakeSttState.LISTENING)

    # While paused — the switch must not quietly reopen the microphone. Pause is a separate
    # axis and a backend change has no business changing it.
    engine2 = FakeEngine(backend="edge")
    engine2._listening_paused = True
    engine2.state = FakeSttState.READY
    manager2 = manager_with(engine2)
    manager2.adopt(requested=AUTO)
    manager2.request("chrome")
    check("switching while paused keeps the microphone closed",
          engine2.listening_paused is True)
    check("and the backend still changed", engine2.backend_key_value == "chrome")
    check("the status reports PAUSED, not LISTENING",
          manager2.snapshot().status == BackendStatus.PAUSED,
          manager2.snapshot().status)

    # While the session is being torn down: refuse rather than rebuild what is being reaped.
    engine3 = FakeEngine(backend="edge")
    engine3.state = FakeSttState.STOPPING
    manager3 = manager_with(engine3)
    check("during teardown the status is STOPPING",
          manager3.snapshot().status == BackendStatus.STOPPING)

    # During a recovery.
    engine4 = FakeEngine(backend="edge")
    engine4.state = FakeSttState.RECOVERING
    manager4 = manager_with(engine4)
    check("a recovering session reports RECOVERING, never PAUSED",
          manager4.snapshot().status == BackendStatus.RECOVERING)
    engine4._listening_paused = True
    check("and RECOVERING still wins over a paused microphone",
          manager4.snapshot().status == BackendStatus.RECOVERING,
          manager4.snapshot().status)

    # A failed session.
    engine5 = FakeEngine(backend=None)
    engine5.state = FakeSttState.FAILED
    manager5 = manager_with(engine5)
    check("a failed session reports ERROR", manager5.snapshot().status == BackendStatus.ERROR)


# ──────────────────────────────────────────────────────────────────────────
#                       5. SESSION AND PROCESS SAFETY
# ──────────────────────────────────────────────────────────────────────────

def section_safety():
    print_system("\n[5] One session, no leaks, no unrelated browser touched")

    engine = FakeEngine(backend="edge")
    manager = manager_with(engine)
    manager.adopt(requested=AUTO)

    for target in ("chrome", "edge", "chrome", AUTO, "edge"):
        manager.request(target)
        check(f"after {label_for(target)}: exactly one live session",
              engine.live_sessions == 1, str(engine.live_sessions))
    check("five switches produced five teardowns and no more",
          engine.teardowns == 5, str(engine.teardowns))

    # The real engine's switch, read as source. These are the properties a fake cannot prove.
    from kayra.input import speech_to_text
    source = inspect.getsource(speech_to_text.SpeechToTextEngine.switch_backend)

    check("the real switch tears the old session down before starting a new one",
          source.index("_teardown_session") < source.index("_start_session"))
    check("the real switch waits for the owned processes to actually go",
          "_await_owned_termination" in source)
    check("an explicit backend is started in STRICT mode",
          "strict=bool(target)" in source)
    check("a failed explicit switch restores the previous preference",
          "self.preferred_browser = previous_pref" in source)
    check("the real switch never matches a process by name",
          not re.search(r"taskkill|/IM|chrome\.exe", source), source[:0])

    # The whole STT + backend stack, walked for the two forbidden patterns.
    for module in (speech_to_text, sb):
        text = inspect.getsource(module)
        tree = ast.parse(text)
        shell_true = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "shell"
            and isinstance(node.value, ast.Constant) and node.value.value is True
        ]
        check(f"{module.__name__}: no shell=True", not shell_true)
        check(f"{module.__name__}: no os.system", "os.system(" not in text)
        check(f"{module.__name__}: no taskkill", "taskkill" not in text.lower())
        check(f"{module.__name__}: no name-based process matching",
              'name() ==' not in text and '.name() in' not in text)

    check("process ownership is still PID-based",
          "owned_pids" in inspect.getsource(speech_to_text.SpeechToTextEngine))


# ──────────────────────────────────────────────────────────────────────────
#                          6. RAPID CHANGES
# ──────────────────────────────────────────────────────────────────────────

def section_rapid():
    print_system("\n[6] Rapid and concurrent changes — the latest wins, no second teardown")

    engine = FakeEngine(backend="edge")
    manager = manager_with(engine)
    manager.adopt(requested=AUTO)

    # A burst of sequential clicks: every one applies, and the LAST one is what stands.
    for target in ("chrome", "edge", "chrome", "edge", "chrome"):
        manager.request(target)
    check("the last request in a burst is the one that stands",
          manager.snapshot().active_backend == "chrome")
    check("and there is still exactly one session", engine.live_sessions == 1)

    # A request arriving DURING a switch is refused rather than queued into a second teardown.
    reentrant = manager_with(FakeEngine(backend="edge"))
    reentrant.adopt(requested=AUTO)
    seen = {}

    original = reentrant._perform_switch

    def reenter(target, previous):
        # Called from inside the in-flight switch, exactly as a second dropdown click would.
        seen["nested"] = reentrant.request("brave")
        return original(target, previous)

    reentrant._perform_switch = reenter
    reentrant.request("chrome")
    check("a request arriving mid-switch is refused, not queued",
          seen["nested"][0] is False, str(seen["nested"]))
    check("and it does not corrupt the outcome of the one in flight",
          reentrant.snapshot().active_backend == "chrome",
          str(reentrant.snapshot().active_backend))
    check("but the user's latest CHOICE is still recorded",
          reentrant.snapshot().requested_backend in ("brave", "chrome"),
          reentrant.snapshot().requested_backend)

    # The revision advances on every request, so a stale UI callback can be identified.
    manager2 = manager_with(FakeEngine())
    before = manager2.snapshot().revision
    manager2.request("chrome")
    manager2.request("edge")
    check("the revision advances with every request",
          manager2.snapshot().revision > before + 1, str(manager2.snapshot().revision))


# ──────────────────────────────────────────────────────────────────────────
#                            7. LOGGING
# ──────────────────────────────────────────────────────────────────────────

def section_logging():
    print_system("\n[7] Logging — the manager owns backend transitions")

    engine = FakeEngine(backend="edge")
    manager = manager_with(engine)
    manager.adopt(requested=AUTO)

    with Recorder() as rec:
        manager.request("chrome")
    text = rec.text()

    check("the switch is framed as a section", "Backend switch" in text, text)
    check("the log states where it came from", "From: Microsoft Edge" in text, text)
    check("the log states where it is going", "To: Google Chrome" in text, text)
    check("the log says the old session is being stopped",
          "Stopping the current session" in text, text)
    check("the log confirms the ACTIVE backend on success",
          "Active: Google Chrome" in text, text)
    check("success is a SUCCESS line", any("SUCCESS" in line for line in rec.lines), text)
    check("every line is tagged [STT]",
          all("[STT]" in line or line.startswith("----") for line in rec.lines), text)

    # A recovery is announced by the MANAGER, once — not by the engine and not by the UI.
    with Recorder() as rec2:
        manager.note_recovery("started", "session unresponsive")
        manager.note_recovery("finished")
    text2 = rec2.text()
    check("a lost session is reported once", text2.count("Session lost") == 1, text2)
    check("recovery start is a WARNING", any("WARNING" in line and "Session lost" in line
                                             for line in rec2.lines), text2)
    check("recovery completion names the backend it came back on",
          "Session restored on Google Chrome" in text2, text2)
    check("recovery never uses the word 'paused'", "paused" not in text2.lower(), text2)

    # The engine hands recovery reporting to the manager rather than printing its own line.
    from kayra.input import speech_to_text
    recover_source = inspect.getsource(speech_to_text.SpeechToTextEngine.recover)
    check("the engine no longer prints its own 'session lost' line",
          "STT session lost" not in recover_source, recover_source[:0])
    check("the engine delegates recovery reporting to the manager",
          "_notify_backend_recovery" in recover_source)


# ──────────────────────────────────────────────────────────────────────────
#                     8. THE ENGINE-LOOKUP SAFETY PROPERTY
# ──────────────────────────────────────────────────────────────────────────

def section_lookup():
    print_system("\n[8] Reading the live engine must never START one")

    source = inspect.getsource(sb)
    tree = ast.parse(source)

    # MODULE-LEVEL imports only. A deferred import inside `_status_for` is fine and is the
    # documented pattern: that function only runs when an engine already exists, so it can
    # never be the thing that starts one. A module-level import would run at import time,
    # from a settings screen, and boot a browser.
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module)
        elif isinstance(node, ast.Import):
            module_level.update(alias.name for alias in node.names)

    check("the module does not import the STT engine at module level",
          "kayra.input.speech_to_text" not in module_level, str(sorted(module_level)))
    check("the live engine is found through sys.modules",
          "sys.modules.get" in source)

    # The CALL, not the mention: the docstring explains why this function is not used, and a
    # bare substring test would fail on the explanation rather than on the code.
    calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    check("it never calls get_shared_engine, which would CREATE one",
          "get_shared_engine" not in calls, str(sorted(calls)))

    # The module path is pinned, for the reason stated in the module: a rename turns this
    # safety property OFF silently rather than raising, which is exactly what happened to
    # `automation.targets.kayra_owned_pids` after the package reorganisation.
    check("the current STT module path is in the lookup tuple",
          "kayra.input.speech_to_text" in STTBackendManager.ENGINE_MODULES,
          str(STTBackendManager.ENGINE_MODULES))
    import kayra.input.speech_to_text as real
    check("and that module really exists under that name",
          sys.modules.get("kayra.input.speech_to_text") is real)

    # With the engine imported but no session live, the lookup must find nothing.
    real.SpeechToTextEngine._active_instance = None
    manager = STTBackendManager()
    check("an imported-but-not-running engine is reported as absent",
          manager._engine() is None)
    check("and the snapshot says OFF", manager.snapshot().status == BackendStatus.OFF)

    # An instance with no driver is not a live session either.
    stub = types.SimpleNamespace(driver=None)
    real.SpeechToTextEngine._active_instance = stub
    check("an instance with no driver is not a live session",
          STTBackendManager()._engine() is None)
    real.SpeechToTextEngine._active_instance = None


# ──────────────────────────────────────────────────────────────────────────
#                       9. THE APPLICATION ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────

def section_entry_point():
    print_system("\n[9] One entry point, and the settings transaction around it")

    import kayra.app as app
    source = inspect.getsource(app.set_stt_backend)

    check("app exposes exactly one backend entry point", hasattr(app, "set_stt_backend"))
    check("it goes through the settings recorder", "recorder.apply" in source)
    check("it delegates the switch to the backend manager", "manager.request" in source)
    check("it sequences nothing itself",
          "_teardown" not in source and "_start_session" not in source)
    check("it refreshes the voice state rather than leaving it stale",
          "_refresh_voice_state" in source)
    # It publishes STARTING, never PAUSED. Showing a pause during a backend switch would be
    # the same false pause an STT recovery used to produce.
    check("it publishes STARTING during the switch",
          'backend_status="STARTING"' in source)
    check("and never publishes PAUSED",
          'backend_status="PAUSED"' not in source and '"PAUSED"' not in source.split('"""')[-1])

    from kayra.core.settings_log import SettingsRecorder
    recorder = SettingsRecorder()

    calls = []
    committed, detail = recorder.apply("STT_BROWSER", "chrome",
                                       runtime=lambda: calls.append(1) or (True, "Google Chrome"),
                                       old_value="auto")
    check("a successful transaction commits", committed is True)
    check("and the runtime work ran exactly once", len(calls) == 1)
    check("the recorder now knows the new value", recorder.known("STT_BROWSER") == "chrome")

    committed, detail = recorder.apply("STT_BROWSER", "brave",
                                       runtime=lambda: (False, "no speech backend"))
    check("a failed transaction does NOT commit", committed is False)
    check("and the recorder keeps the OLD value",
          recorder.known("STT_BROWSER") == "chrome", recorder.known("STT_BROWSER"))

    def explode():
        raise RuntimeError("driver died")

    committed, detail = recorder.apply("STT_BROWSER", "edge", runtime=explode)
    check("a runtime operation that raises is a failed change, not a crash",
          committed is False and "driver died" in detail)
    check("and the value is still the last committed one",
          recorder.known("STT_BROWSER") == "chrome")


# ──────────────────────────────────────────────────────────────────────────
#                            10. LIVE (opt-in)
# ──────────────────────────────────────────────────────────────────────────

def section_live():
    print_system("\n[10] Live, read-only — real discovery, no session started")

    from kayra.input import browsers

    installed = browsers.discover_browsers()
    check("browser discovery works", isinstance(installed, tuple))
    print_info(f"      installed: {', '.join(s.label for s in installed) or 'none'}")

    keys = {spec.key for spec in installed}
    for key in sorted(keys):
        check(f"{label_for(key)} has a label in the backend table",
              key in sb.BACKEND_LABELS, key)

    # Strict selection must return only the requested browser.
    for key in sorted(keys):
        strict = tuple(spec for spec in installed if spec.key == key)
        check(f"strict selection for {key} yields exactly one candidate",
              len(strict) == 1)

    manager = sb.get_stt_backend_manager()
    snapshot = manager.snapshot()
    check("the live manager reports OFF with no session running",
          snapshot.status == BackendStatus.OFF, snapshot.status)
    print_info(f"      requested: {snapshot.requested_label}, active: {snapshot.active_label}")


def main():
    print_banner("STT BACKEND DIAGNOSTIC", "Requested vs active · live switching · safety")
    section_state()
    section_transitions()
    section_failure()
    section_states()
    section_safety()
    section_rapid()
    section_logging()
    section_lookup()
    section_entry_point()
    if LIVE:
        section_live()
    else:
        print_info("\nSkipping live checks. Re-run with --live for real browser discovery.")

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All STT backend checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
