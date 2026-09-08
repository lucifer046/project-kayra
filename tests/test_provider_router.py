# ┌────────────────────────────────────────────────────────────────────────┐
# │                       test_provider_router.py                          │
# │        Model Provider Hierarchy, Failover and Cooldown Diagnostics     │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_provider_router.py — standalone diagnostic for the provider routing authority.

Like the other scripts in tests/, this is a manual entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_provider_router.py

HARDWARE-FREE AND NETWORK-FREE. No API keys, no SDK calls, no sockets. Every provider is a
callable that returns or raises exactly what the test wants, which is the whole reason the
router takes a callable instead of importing the clients itself.

It exercises

  1. The hierarchy — DECISION is Cohere -> Groq -> Gemini, CHAT is Groq -> Gemini, and Cohere
     is not a chat provider.
  2. Failure classification across all eight kinds, including `Retry-After`.
  3. Sequential fallback: the exact order, and AT MOST ONE call per provider per request —
     the property that makes "no duplicate calls" true rather than hoped for.
  4. Cooldowns: a rate-limited provider is skipped by the NEXT request and becomes eligible
     again when it expires.
  5. No retry storm: a sustained failure does not multiply calls, and an unconfigured or
     exhausted chain fails once with a clear error rather than recursing.
  6. Streaming fallback, and the rule that makes it safe — fallback only before the first
     chunk, never a splice mid-sentence.
  7. The log lines the router owns, captured and asserted.
  8. Cost: selection is arithmetic, not I/O.
  9. Integration with the real `CentralizedLLMEngine`, with its clients stubbed out.
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
from kayra.intelligence import provider_router as pr
from kayra.intelligence.provider_router import (
    ProviderRouter, AllProvidersFailed, FailureKind, classify_failure,
    ROUTE_CHAT, ROUTE_DECISION, ROUTE_CHAINS, COHERE, GROQ, GEMINI,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f" [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ──────────────────────────────────────────────────────────────────────────
#                              TEST SCAFFOLD
# ──────────────────────────────────────────────────────────────────────────

class FakeClock:
    """A monotonic clock the test advances by hand, so cooldowns are tested without sleeping."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Recorder:
    """
    Captures the router's log output.

    The router OWNS provider and fallback lines, so those lines are part of its contract and
    are asserted here rather than eyeballed. `logbus.log` is substituted for the duration.
    """

    def __init__(self):
        self.lines = []
        self._saved = {}

    def __enter__(self):
        def capture(level, subsystem, message, turn=None, correlate=True):
            line = logbus.format_line(level, subsystem, message, timestamp="00:00:00")
            self.lines.append(line)
            return line

        for name in ("log", "info", "warning", "error", "debug", "success"):
            self._saved[name] = getattr(logbus, name)
        logbus.log = capture
        logbus.info = lambda s, m, **k: capture(logbus.INFO, s, m)
        logbus.warning = lambda s, m, **k: capture(logbus.WARNING, s, m)
        logbus.error = lambda s, m, **k: capture(logbus.ERROR, s, m)
        logbus.debug = lambda s, m, **k: capture(logbus.DEBUG, s, m)
        logbus.success = lambda s, m, **k: capture(logbus.SUCCESS, s, m)
        # provider_router imported the helpers by name, so rebind them there too.
        self._pr_saved = {n: getattr(pr, n) for n in ("info", "warning", "error", "debug", "success")}
        pr.info = logbus.info
        pr.warning = logbus.warning
        pr.error = logbus.error
        pr.debug = logbus.debug
        pr.success = logbus.success
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(logbus, name, fn)
        for name, fn in self._pr_saved.items():
            setattr(pr, name, fn)
        return False

    def text(self):
        return "\n".join(self.lines)

    def containing(self, needle):
        return [line for line in self.lines if needle in line]


def make_router(clock=None, configured=(COHERE, GROQ, GEMINI)):
    router = ProviderRouter(clock=clock or FakeClock())
    for name in (COHERE, GROQ, GEMINI):
        router.register(name, name in configured)
    return router


class RateLimit(Exception):
    def __init__(self, message="429 rate limit exceeded", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerDown(Exception):
    pass


def calls_recorded():
    """A provider callable plus the list of names it was invoked with, in order."""
    seen = []

    def call(name, behaviour):
        seen.append(name)
        outcome = behaviour.get(name, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    return seen, call


# ──────────────────────────────────────────────────────────────────────────
#                            1. THE HIERARCHY
# ──────────────────────────────────────────────────────────────────────────

def section_hierarchy():
    print_system("\n[1] Routing hierarchy — the orders are separate and explicit")

    check("DECISION is exactly Cohere -> Groq -> Gemini",
          ROUTE_CHAINS[ROUTE_DECISION] == (COHERE, GROQ, GEMINI),
          str(ROUTE_CHAINS[ROUTE_DECISION]))
    check("CHAT is exactly Groq -> Gemini",
          ROUTE_CHAINS[ROUTE_CHAT] == (GROQ, GEMINI),
          str(ROUTE_CHAINS[ROUTE_CHAT]))
    check("Cohere is never a CHAT provider",
          COHERE not in ROUTE_CHAINS[ROUTE_CHAT])
    check("Gemini is never the DECISION primary",
          ROUTE_CHAINS[ROUTE_DECISION][0] != GEMINI)
    check("Cohere is never the CHAT primary",
          ROUTE_CHAINS[ROUTE_CHAT][0] != COHERE)
    check("Groq is the CHAT primary", ROUTE_CHAINS[ROUTE_CHAT][0] == GROQ)
    check("Cohere is the DECISION primary", ROUTE_CHAINS[ROUTE_DECISION][0] == COHERE)

    router = make_router()
    check("chain(DECISION) with everything configured is the full order",
          router.chain(ROUTE_DECISION) == (COHERE, GROQ, GEMINI))
    check("chain(CHAT) with everything configured is the full order",
          router.chain(ROUTE_CHAT) == (GROQ, GEMINI))
    check("describe(DECISION) reads as a hierarchy",
          router.describe(ROUTE_DECISION) == "Cohere > Groq > Gemini",
          router.describe(ROUTE_DECISION))

    # An unconfigured provider is skipped, never an error, and never shortens the chain
    # permanently — 1.9's "if a provider is not configured, skip it gracefully".
    partial = make_router(configured=(GROQ, GEMINI))
    check("no Cohere key: DECISION falls through to Groq -> Gemini",
          partial.chain(ROUTE_DECISION) == (GROQ, GEMINI))
    only_gemini = make_router(configured=(GEMINI,))
    check("only Gemini configured: DECISION is Gemini alone",
          only_gemini.chain(ROUTE_DECISION) == (GEMINI,))
    check("only Gemini configured: CHAT is Gemini alone",
          only_gemini.chain(ROUTE_CHAT) == (GEMINI,))
    none = make_router(configured=())
    check("nothing configured: both chains are empty",
          none.chain(ROUTE_DECISION) == () and none.chain(ROUTE_CHAT) == ())
    check("nothing configured: describe says so rather than lying",
          none.describe(ROUTE_CHAT) == "none configured")


# ──────────────────────────────────────────────────────────────────────────
#                       2. FAILURE CLASSIFICATION
# ──────────────────────────────────────────────────────────────────────────

def section_classification():
    print_system("\n[2] Failure classification — eight kinds, and Retry-After")

    cases = [
        (Exception("429 Too Many Requests"), FailureKind.RATE_LIMITED),
        (Exception("rate_limit_exceeded for model"), FailureKind.RATE_LIMITED),
        (Exception("RESOURCE_EXHAUSTED: quota"), FailureKind.RATE_LIMITED),
        (type("TooManyRequestsError", (Exception,), {})(), FailureKind.RATE_LIMITED),
        (Exception("401 Unauthorized"), FailureKind.AUTH_FAILURE),
        (Exception("invalid api key provided"), FailureKind.AUTH_FAILURE),
        (Exception("Read timed out"), FailureKind.TIMEOUT),
        (type("APIConnectionError", (Exception,), {})("connection refused"),
         FailureKind.NETWORK_FAILURE),
        (Exception("model_not_found: llama-2"), FailureKind.MODEL_UNAVAILABLE),
        (Exception("model has been decommissioned"), FailureKind.MODEL_UNAVAILABLE),
        (Exception("503 Service Unavailable"), FailureKind.SERVER_ERROR),
        (Exception("internal server error"), FailureKind.SERVER_ERROR),
        (Exception("invalid_request_error: bad field"), FailureKind.INVALID_REQUEST),
        (Exception("context_length_exceeded"), FailureKind.INVALID_REQUEST),
        (Exception("something nobody has seen before"), FailureKind.UNKNOWN),
    ]
    for exc, expected in cases:
        kind, _ = classify_failure(exc)
        check(f"{type(exc).__name__}({str(exc)[:34]!r}) -> {expected}",
              kind == expected, f"got {kind}")

    check("every classified kind is a declared kind",
          all(classify_failure(exc)[0] in pr.FAILURE_KINDS for exc, _ in cases))
    check("None classifies as UNKNOWN rather than raising",
          classify_failure(None)[0] == FailureKind.UNKNOWN)

    # Retry-After, in each of the shapes an SDK actually presents it.
    _, retry = classify_failure(RateLimit(retry_after=12))
    check("Retry-After read from an attribute", retry == 12.0, str(retry))

    headers_exc = Exception("429 slow down")
    headers_exc.response = types.SimpleNamespace(headers={"retry-after": "7"})
    _, retry = classify_failure(headers_exc)
    check("Retry-After read from response headers", retry == 7.0, str(retry))

    bad = Exception("429")
    bad.response = types.SimpleNamespace(headers={"retry-after": "soon"})
    _, retry = classify_failure(bad)
    check("an unparseable Retry-After is ignored, not crashed on", retry is None)

    check("INVALID_REQUEST is NOT a fallback kind",
          FailureKind.INVALID_REQUEST not in pr.FALLBACK_KINDS)
    check("RATE_LIMITED IS a fallback kind",
          FailureKind.RATE_LIMITED in pr.FALLBACK_KINDS)
    check("AUTH_FAILURE IS a fallback kind (another key may be fine)",
          FailureKind.AUTH_FAILURE in pr.FALLBACK_KINDS)


# ──────────────────────────────────────────────────────────────────────────
#                     3. SEQUENTIAL FALLBACK, EXACT ORDER
# ──────────────────────────────────────────────────────────────────────────

def section_fallback():
    print_system("\n[3] Sequential fallback — exact order, at most one call per provider")

    # ── DMM: Cohere succeeds ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION, lambda n: call(n, {COHERE: "open chrome"}))
    check("DMM: Cohere success returns Cohere's answer", result == "open chrome")
    check("DMM: Cohere success calls nobody else", seen == [COHERE], str(seen))

    # ── DMM: Cohere 429 -> Groq ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION,
                        lambda n: call(n, {COHERE: RateLimit(), GROQ: "general hi"}))
    check("DMM: Cohere 429 falls back to Groq", result == "general hi")
    check("DMM: the order was exactly Cohere then Groq", seen == [COHERE, GROQ], str(seen))
    check("DMM: Gemini was never touched", GEMINI not in seen)

    # ── DMM: Cohere 429, Groq server error -> Gemini ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION, lambda n: call(n, {
        COHERE: RateLimit(), GROQ: ServerDown("503 Service Unavailable"), GEMINI: "exit"}))
    check("DMM: Cohere -> Groq -> Gemini, in that order",
          seen == [COHERE, GROQ, GEMINI] and result == "exit", str(seen))

    # ── DMM: everything fails -> a single clear final error ──
    router = make_router()
    seen, call = calls_recorded()
    try:
        router.run(ROUTE_DECISION, lambda n: call(n, {
            COHERE: RateLimit(), GROQ: ServerDown("500"), GEMINI: RateLimit()}))
        raised = None
    except AllProvidersFailed as exc:
        raised = exc
    check("DMM: total failure raises AllProvidersFailed", raised is not None)
    check("DMM: total failure tried each provider exactly once",
          seen == [COHERE, GROQ, GEMINI], str(seen))
    check("DMM: the failure carries every provider's kind",
          raised is not None and raised.kinds ==
          [FailureKind.RATE_LIMITED, FailureKind.SERVER_ERROR, FailureKind.RATE_LIMITED],
          str(raised.kinds if raised else None))

    # ── DMM: Cohere unconfigured -> straight to Groq ──
    router = make_router(configured=(GROQ, GEMINI))
    seen, call = calls_recorded()
    router.run(ROUTE_DECISION, lambda n: call(n, {GROQ: "ok"}))
    check("DMM: an unconfigured Cohere is skipped without being called",
          seen == [GROQ], str(seen))

    # ── CHAT: Groq succeeds ──
    router = make_router()
    seen, call = calls_recorded()
    router.run(ROUTE_CHAT, lambda n: call(n, {GROQ: "hello"}))
    check("CHAT: Groq success calls nobody else", seen == [GROQ], str(seen))

    # ── CHAT: Groq 429 -> Gemini, and never Cohere ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_CHAT, lambda n: call(n, {GROQ: RateLimit(), GEMINI: "hi"}))
    check("CHAT: Groq 429 falls back to Gemini immediately", result == "hi")
    check("CHAT: the order was exactly Groq then Gemini", seen == [GROQ, GEMINI], str(seen))
    check("CHAT: Cohere is never called, even when everything else fails",
          COHERE not in seen)

    router = make_router()
    seen, call = calls_recorded()
    try:
        router.run(ROUTE_CHAT, lambda n: call(n, {GROQ: RateLimit(), GEMINI: ServerDown("500")}))
    except AllProvidersFailed:
        pass
    check("CHAT: exhausted chain still never reaches Cohere",
          seen == [GROQ, GEMINI], str(seen))

    # ── INVALID_REQUEST stops the chain instead of repeating a bad request ──
    router = make_router()
    seen, call = calls_recorded()
    try:
        router.run(ROUTE_DECISION, lambda n: call(n, {
            COHERE: Exception("invalid_request_error: bad field")}))
    except AllProvidersFailed:
        pass
    check("INVALID_REQUEST is not sent to the next provider", seen == [COHERE], str(seen))

    # ── An empty chain fails once, and calls nothing ──
    router = make_router(configured=())
    seen, call = calls_recorded()
    try:
        router.run(ROUTE_CHAT, lambda n: call(n, {}))
        raised = None
    except AllProvidersFailed as exc:
        raised = exc
    check("an empty chain raises without calling anything",
          raised is not None and seen == [], str(seen))


# ──────────────────────────────────────────────────────────────────────────
#                            4. COOLDOWNS
# ──────────────────────────────────────────────────────────────────────────

def section_cooldown():
    print_system("\n[4] Provider cooldown — the rate-limited provider stops being asked")

    clock = FakeClock()
    router = make_router(clock=clock)

    seen, call = calls_recorded()
    router.run(ROUTE_DECISION, lambda n: call(n, {COHERE: RateLimit(), GROQ: "ok"}))
    check("after a 429, Cohere is in cooldown",
          router.cooldown_remaining(COHERE) > 0,
          f"{router.cooldown_remaining(COHERE):.0f}s")
    check("after a 429, Cohere is not available", not router.is_available(COHERE))
    check("the NEXT DMM chain starts at Groq, not Cohere",
          router.chain(ROUTE_DECISION) == (GROQ, GEMINI),
          str(router.chain(ROUTE_DECISION)))

    seen2, call2 = calls_recorded()
    router.run(ROUTE_DECISION, lambda n: call2(n, {GROQ: "ok"}))
    check("the next request does not touch the cooled-down provider at all",
          seen2 == [GROQ], str(seen2))

    clock.advance(router.cooldown_seconds(FailureKind.RATE_LIMITED) + 1)
    check("Cohere becomes eligible again once the cooldown expires",
          router.is_available(COHERE))
    check("and it leads the chain again",
          router.chain(ROUTE_DECISION) == (COHERE, GROQ, GEMINI))

    # Retry-After wins over the configured default.
    clock2 = FakeClock()
    router2 = make_router(clock=clock2)
    router2.mark_failure(GROQ, FailureKind.RATE_LIMITED, retry_after=3)
    check("a provider-supplied Retry-After overrides the default cooldown",
          2.5 < router2.cooldown_remaining(GROQ) <= 3.0,
          f"{router2.cooldown_remaining(GROQ):.1f}s")
    clock2.advance(4)
    check("and it expires on the provider's own schedule", router2.is_available(GROQ))

    # Kind-specific cooldowns, and the one that does not stand a provider down at all.
    router3 = make_router(clock=FakeClock())
    check("AUTH_FAILURE cools down far longer than a rate limit",
          router3.cooldown_seconds(FailureKind.AUTH_FAILURE) >
          router3.cooldown_seconds(FailureKind.RATE_LIMITED))
    check("INVALID_REQUEST does not stand a provider down",
          router3.mark_failure(GEMINI, FailureKind.INVALID_REQUEST) == 0.0
          and router3.is_available(GEMINI))

    # A success clears the cooldown: a provider that just answered is demonstrably available.
    router4 = make_router(clock=FakeClock())
    router4.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    router4.mark_success(GROQ)
    check("a success clears an outstanding cooldown", router4.is_available(GROQ))

    # 11.1: a DMM cooldown must not block chat.
    router5 = make_router(clock=FakeClock())
    router5.mark_failure(COHERE, FailureKind.RATE_LIMITED)
    check("a Cohere cooldown does not affect the CHAT chain",
          router5.chain(ROUTE_CHAT) == (GROQ, GEMINI))
    # 11.2: a Groq cooldown affects both, correctly, and differently.
    router6 = make_router(clock=FakeClock())
    router6.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    check("a Groq cooldown leaves CHAT on Gemini",
          router6.chain(ROUTE_CHAT) == (GEMINI,))
    check("a Groq cooldown leaves DECISION on Cohere -> Gemini",
          router6.chain(ROUTE_DECISION) == (COHERE, GEMINI))

    # Config clamping — a malformed .env must not be able to disable or extend the policy.
    resolved = pr.configure_cooldowns({"PROVIDER_COOLDOWN_RATE_LIMIT_SECONDS": "not a number"})
    check("a malformed cooldown value is ignored", FailureKind.RATE_LIMITED not in resolved)
    resolved = pr.configure_cooldowns({"PROVIDER_COOLDOWN_RATE_LIMIT_SECONDS": "999999999"})
    check("an absurd cooldown value is clamped",
          resolved[FailureKind.RATE_LIMITED] <= 86400.0,
          str(resolved[FailureKind.RATE_LIMITED]))
    resolved = pr.configure_cooldowns({"PROVIDER_COOLDOWN_RATE_LIMIT_SECONDS": "45"})
    check("a valid cooldown override is honoured",
          resolved[FailureKind.RATE_LIMITED] == 45.0)


# ──────────────────────────────────────────────────────────────────────────
#                        5. NO RETRY STORM
# ──────────────────────────────────────────────────────────────────────────

def section_no_storm():
    print_system("\n[5] No retry storm — the failure mode this replaced")

    clock = FakeClock()
    router = make_router(clock=clock)

    # Twenty consecutive requests against a provider that is rate-limiting. Before this
    # module, each of those was 5 + 10 + 15 seconds of blocking sleep and four Cohere calls.
    total = []
    for _ in range(20):
        seen, call = calls_recorded()
        try:
            router.run(ROUTE_DECISION, lambda n: call(n, {
                COHERE: RateLimit(), GROQ: RateLimit(), GEMINI: "ok"}))
        except AllProvidersFailed:
            pass
        total.extend(seen)

    cohere_calls = total.count(COHERE)
    check("20 requests against a rate-limited Cohere produce ONE Cohere call, not 20",
          cohere_calls == 1, f"{cohere_calls} calls")
    check("and the survivor answers every one of them",
          total.count(GEMINI) == 20, f"{total.count(GEMINI)}")

    # And no sleeping anywhere on the path. This is the specific defect: the old handler
    # blocked the user for 5, then 10, then 15 seconds before trying anything else.
    started = time.perf_counter()
    router2 = make_router(clock=FakeClock())
    for _ in range(50):
        seen, call = calls_recorded()
        router2.run(ROUTE_DECISION, lambda n: call(n, {COHERE: RateLimit(), GROQ: "ok"}))
    elapsed = time.perf_counter() - started
    check("50 rate-limited fallbacks complete with no blocking backoff",
          elapsed < 0.5, f"{elapsed * 1000:.1f}ms total")

    source = inspect.getsource(pr)
    check("the router never sleeps", "time.sleep" not in source)
    check("the router never recurses into itself",
          "self.run(" not in source and "self.run_stream(" not in source)

    tree = ast.parse(source)
    imported = {n.names[0].name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {(n.module or "").split(".")[0]
                 for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    check("the router imports no provider SDK",
          not ({"cohere", "openai", "groq", "google", "requests"} & imported),
          str(sorted(imported)))


# ──────────────────────────────────────────────────────────────────────────
#                        6. STREAMING FALLBACK
# ──────────────────────────────────────────────────────────────────────────

def section_streaming():
    print_system("\n[6] Streaming — fallback before the first token, never a splice")

    def stream_of(*chunks):
        def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    # Groq fails before producing anything -> Gemini answers in full.
    router = make_router()
    seen = []

    def call(name):
        seen.append(name)
        if name == GROQ:
            raise RateLimit()
        return stream_of("Hel", "lo.")

    out = "".join(router.run_stream(ROUTE_CHAT, call))
    check("a pre-token failure falls back and the answer is whole",
          out == "Hello." and seen == [GROQ, GEMINI], f"{out!r} {seen}")

    # Groq fails AFTER a token. The answer must not be spliced with Gemini's.
    router = make_router()
    seen = []

    def call_mid(name):
        seen.append(name)
        if name == GROQ:
            def gen():
                yield "Certainly, "
                raise ServerDown("503")
            return gen()
        return stream_of("COMPLETELY DIFFERENT")

    collected = []
    try:
        for chunk in router.run_stream(ROUTE_CHAT, call_mid):
            collected.append(chunk)
        raised = False
    except Exception:
        raised = True
    check("a mid-stream failure re-raises rather than splicing two answers", raised)
    check("nothing from the second provider reached the output",
          "".join(collected) == "Certainly, ", repr("".join(collected)))
    check("the failed provider is still stood down for the NEXT request",
          not router.is_available(GROQ))
    check("and the second provider was never started",
          seen == [GROQ], str(seen))

    # An exhausted streaming chain raises rather than yielding nothing silently.
    router = make_router()

    def call_all_fail(name):
        raise RateLimit()

    try:
        list(router.run_stream(ROUTE_CHAT, call_all_fail))
        raised = None
    except AllProvidersFailed as exc:
        raised = exc
    check("an exhausted streaming chain raises AllProvidersFailed", raised is not None)


# ──────────────────────────────────────────────────────────────────────────
#                          7. FALLBACK LOGGING
# ──────────────────────────────────────────────────────────────────────────

def section_logging():
    print_system("\n[7] Logging — the router owns provider and fallback lines")

    logbus.set_level(logbus.DEBUG)
    with Recorder() as rec:
        router = make_router()
        _, call = calls_recorded()
        router.run(ROUTE_DECISION, lambda n: call(n, {COHERE: RateLimit(), GROQ: "ok"}))
    logbus.set_level(logbus.INFO)

    text = rec.text()
    check("the DMM route logs under [DMM]", "[DMM]" in text)
    check("the primary attempt is named", "Provider: Cohere" in text, text)
    check("the rate limit is reported as RATE_LIMITED",
          "Result: RATE_LIMITED from Cohere" in text, text)
    check("the fallback provider is named", "Fallback: Groq" in text, text)
    check("success is reported with the provider that produced it",
          "Result: SUCCESS via Groq" in text, text)
    check("the cooldown is stated in the same line as the failure",
          "cooling down" in text, text)
    check("the expected rate limit is a WARNING, not an ERROR",
          any("WARNING" in line and "RATE_LIMITED" in line for line in rec.lines))
    check("the SDK's own exception text is DEBUG, not INFO",
          any("DEBUG" in line and "RateLimit" in line for line in rec.lines), text)

    with Recorder() as rec:
        router = make_router()
        _, call = calls_recorded()
        router.run(ROUTE_CHAT, lambda n: call(n, {GROQ: RateLimit(), GEMINI: "ok"}))
    text = rec.text()
    check("the CHAT route logs under [CHAT]", "[CHAT]" in text and "[DMM]" not in text)
    check("chat fallback reads Groq -> Gemini",
          "Provider: Groq" in text and "Fallback: Gemini" in text, text)

    # A cooled-down provider is mentioned at DEBUG, not once per turn at INFO.
    logbus.set_level(logbus.DEBUG)
    with Recorder() as rec:
        router = make_router()
        router.mark_failure(COHERE, FailureKind.RATE_LIMITED)
        _, call = calls_recorded()
        router.run(ROUTE_DECISION, lambda n: call(n, {GROQ: "ok"}))
    logbus.set_level(logbus.INFO)
    skip_lines = rec.containing("Skipping Cohere")
    check("a skipped, cooling-down provider is explained", bool(skip_lines), rec.text())
    check("and only at DEBUG, so it does not repeat at INFO for a whole minute",
          all("DEBUG" in line for line in skip_lines), str(skip_lines))

    # No secrets, ever — even when the provider echoes the key into its own error.
    with Recorder() as rec:
        router = make_router()

        def leaky(name):
            raise Exception("401 Unauthorized: CohereAPIKey=zzTOPSECRETzz rejected")

        try:
            router.run(ROUTE_DECISION, leaky)
        except AllProvidersFailed:
            pass
    check("a key echoed in a provider error never reaches the log",
          "zzTOPSECRETzz" not in rec.text(), rec.text())
    check("and the line still says what failed",
          "AUTH_FAILURE" in rec.text())


# ──────────────────────────────────────────────────────────────────────────
#                         8. COST AND SHAPE
# ──────────────────────────────────────────────────────────────────────────

def section_cost():
    print_system("\n[8] Cost — selection is arithmetic")

    router = make_router()
    iterations = 20000
    started = time.perf_counter()
    for _ in range(iterations):
        router.chain(ROUTE_DECISION)
    per_call = (time.perf_counter() - started) / iterations * 1e6
    check("chain() costs well under 20us", per_call < 20.0, f"{per_call:.2f}us")

    started = time.perf_counter()
    for _ in range(20000):
        classify_failure(RateLimit())
    per_call = (time.perf_counter() - started) / 20000 * 1e6
    check("classify_failure() costs well under 60us", per_call < 60.0, f"{per_call:.2f}us")

    check("the router starts no thread",
          "threading.Thread" not in inspect.getsource(pr))
    check("the router persists nothing",
          not re.search(r"\bopen\(|json\.dump|\.write\(", inspect.getsource(pr)))

    snapshot = router.snapshot()
    check("snapshot() reports both routes",
          set(snapshot["routes"]) == {ROUTE_DECISION, ROUTE_CHAT})
    check("snapshot() reports eligibility per route",
          snapshot["eligible"][ROUTE_CHAT] == [GROQ, GEMINI])


# ──────────────────────────────────────────────────────────────────────────
#                    9. INTEGRATION WITH THE REAL ENGINE
# ──────────────────────────────────────────────────────────────────────────

def section_engine():
    print_system("\n[9] CentralizedLLMEngine — one authority, one prompt contract")

    from kayra.intelligence.llm_engine import CentralizedLLMEngine

    source = inspect.getsource(CentralizedLLMEngine)

    # The engine must not have grown a second fallback authority back.
    check("the engine no longer catches Cohere's rate-limit type itself",
          "cohere.TooManyRequestsError" not in source)
    check("the engine no longer sleeps on a rate limit",
          "time.sleep(cooldown)" not in source)
    check("the engine defines no private quota-error predicate",
          "_is_quota_error" not in source)
    check("the DMM goes through the router", "ROUTE_DECISION" in source)
    check("chat goes through the router", "ROUTE_CHAT" in source)

    # Construct without touching the network: stub the local probe and the clients.
    CentralizedLLMEngine._instance = None
    CentralizedLLMEngine._has_booted = True
    original_probe = CentralizedLLMEngine._check_local_server
    CentralizedLLMEngine._check_local_server = lambda self: False
    try:
        engine = CentralizedLLMEngine()
    finally:
        CentralizedLLMEngine._check_local_server = original_probe

    pr.reset_provider_router()
    engine.router = ProviderRouter(clock=FakeClock())
    engine.router.register(COHERE, lambda: engine.cohere_client is not None)
    engine.router.register(GROQ, lambda: engine.groq_client is not None)
    engine.router.register(GEMINI, lambda: engine.gemini_client is not None)

    calls = []

    class StubCohere:
        def chat_stream(self, **kwargs):
            calls.append(("cohere", kwargs))
            raise RateLimit()

    class StubCompletions:
        def __init__(self, name, outcome):
            self.name, self.outcome = name, outcome

        def create(self, **kwargs):
            calls.append((self.name, kwargs))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=self.outcome))])

    def stub_client(name, outcome):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=StubCompletions(name, outcome)))

    engine.is_online = True
    engine.cohere_client = StubCohere()
    engine.groq_client = stub_client("groq", "open chrome")
    engine.gemini_client = stub_client("gemini", "general hello")

    tokens = engine.classify_intent("open chrome")
    check("a rate-limited Cohere DMM request is answered by Groq",
          tokens == ["open chrome"], str(tokens))
    check("Cohere was called once and Groq once — no duplicates",
          [name for name, _ in calls] == ["cohere", "groq"],
          str([name for name, _ in calls]))

    # The prompt contract must be identical across providers.
    cohere_kwargs = calls[0][1]
    groq_kwargs = calls[1][1]
    check("Cohere receives the unsliced few-shot history",
          cohere_kwargs["chat_history"] is engine.dmm_chat_history)
    check("Cohere's preamble is the DMM system rule plus the preamble",
          engine.dmm_preamble.strip() in cohere_kwargs["preamble"])
    check("the Groq fallback carries the SAME preamble",
          engine.dmm_preamble.strip() in groq_kwargs["messages"][0]["content"])
    check("the Groq fallback carries EVERY few-shot example, unsliced",
          len(groq_kwargs["messages"]) == len(engine.dmm_chat_history) + 2,
          f"{len(groq_kwargs['messages'])} vs {len(engine.dmm_chat_history) + 2}")
    check("the Groq fallback asks the same question",
          groq_kwargs["messages"][-1]["content"] == "open chrome")
    check("the DMM stays at low temperature on the fallback",
          groq_kwargs["temperature"] == 0.1)
    check("the DMM never mixes chat history into the request",
          all(m["content"] in
              {msg["message"] for msg in engine.dmm_chat_history} |
              {groq_kwargs["messages"][0]["content"], "open chrome"}
              for m in groq_kwargs["messages"]))

    # With every decision provider down, the DMM degrades to conversation rather than failing.
    engine.router.clear_cooldowns()
    engine.cohere_client = StubCohere()
    engine.groq_client = stub_client("groq", RateLimit())
    engine.gemini_client = stub_client("gemini", RateLimit())
    tokens = engine.classify_intent("what is the capital of France")
    check("a fully exhausted DECISION chain degrades to conversation",
          tokens == ["general what is the capital of France"], str(tokens))

    # Chat routes to Groq, then Gemini, and never to Cohere.
    engine.router.clear_cooldowns()
    calls.clear()

    class StubStream:
        def __init__(self, name, outcome):
            self.name, self.outcome = name, outcome

        def create(self, **kwargs):
            calls.append((self.name, kwargs))
            if isinstance(self.outcome, Exception):
                raise self.outcome

            def gen():
                for piece in self.outcome:
                    yield types.SimpleNamespace(
                        choices=[types.SimpleNamespace(
                            delta=types.SimpleNamespace(content=piece))])
            return gen()

    engine.groq_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=StubStream("groq", RateLimit())))
    engine.gemini_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=StubStream("gemini", ["Good ", "evening."])))

    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("a rate-limited Groq chat request is answered by Gemini",
          out == "Good evening.", repr(out))
    check("chat called Groq then Gemini and nothing else",
          [name for name, _ in calls] == ["groq", "gemini"],
          str([name for name, _ in calls]))
    check("the Cohere client was never used for chat",
          not any(name == "cohere" for name, _ in calls))

    # Both chat providers down: a clear final message, not an exception into the speech path.
    engine.router.clear_cooldowns()
    engine.gemini_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=StubStream("gemini", RateLimit())))
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("an exhausted chat chain yields one clear sentence",
          "rate-limited" in out.lower() and len(out) < 200, repr(out))

    # 11.1: a DMM cooldown must not prevent chat from working.
    engine.router.clear_cooldowns()
    engine.router.mark_failure(COHERE, FailureKind.RATE_LIMITED)
    engine.groq_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=StubStream("groq", ["Yes."])))
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("a Cohere DMM cooldown does not block normal chat", out == "Yes.", repr(out))

    CentralizedLLMEngine._instance = None
    pr.reset_provider_router()


def main():
    print_banner("PROVIDER ROUTER DIAGNOSTIC", "Hierarchy · failover · cooldown · logging")
    section_hierarchy()
    section_classification()
    section_fallback()
    section_cooldown()
    section_no_storm()
    section_streaming()
    section_logging()
    section_cost()
    section_engine()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All provider router checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
