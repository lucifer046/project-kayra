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

  1. The hierarchy — DECISION and CHAT are both Claude -> Groq -> Gemini, and any one key
     alone serves both.
  2. Failure classification across all eight kinds, including `Retry-After`, with the HTTP
     status deciding first (a retired model or a plan with no allowance is not a rate limit).
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

from kayra.utils import print_banner, print_success, print_error, print_system
from kayra.core import logbus
from kayra.intelligence import provider_router as pr
from kayra.intelligence.provider_router import (
    ProviderRouter, AllProvidersFailed, FailureKind, classify_failure,
    ROUTE_CHAT, ROUTE_DECISION, ROUTE_CHAINS, CLAUDE, GROQ, GEMINI,
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


def make_router(clock=None, configured=(CLAUDE, GROQ, GEMINI)):
    router = ProviderRouter(clock=clock or FakeClock())
    for name in (CLAUDE, GROQ, GEMINI):
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
    print_system("\n[1] Routing hierarchy — one order, explicit, any key alone is enough")

    check("DECISION is exactly Claude -> Groq -> Gemini",
          ROUTE_CHAINS[ROUTE_DECISION] == (CLAUDE, GROQ, GEMINI),
          str(ROUTE_CHAINS[ROUTE_DECISION]))
    check("CHAT is exactly Claude -> Groq -> Gemini",
          ROUTE_CHAINS[ROUTE_CHAT] == (CLAUDE, GROQ, GEMINI),
          str(ROUTE_CHAINS[ROUTE_CHAT]))
    check("Cohere is gone from every route",
          all("Cohere" not in chain for chain in ROUTE_CHAINS.values())
          and not hasattr(pr, "COHERE"))
    check("Claude is the primary on both routes",
          ROUTE_CHAINS[ROUTE_CHAT][0] == CLAUDE and ROUTE_CHAINS[ROUTE_DECISION][0] == CLAUDE)
    check("Gemini is the last fallback on both routes",
          ROUTE_CHAINS[ROUTE_CHAT][-1] == GEMINI and ROUTE_CHAINS[ROUTE_DECISION][-1] == GEMINI)

    router = make_router()
    check("chain(DECISION) with everything configured is the full order",
          router.chain(ROUTE_DECISION) == (CLAUDE, GROQ, GEMINI))
    check("chain(CHAT) with everything configured is the full order",
          router.chain(ROUTE_CHAT) == (CLAUDE, GROQ, GEMINI))
    check("describe(DECISION) reads as a hierarchy",
          router.describe(ROUTE_DECISION) == "Claude > Groq > Gemini",
          router.describe(ROUTE_DECISION))

    # An unconfigured provider is skipped, never an error, and never shortens the chain
    # permanently.
    partial = make_router(configured=(GROQ, GEMINI))
    check("no Claude key: both routes fall through to Groq -> Gemini",
          partial.chain(ROUTE_DECISION) == (GROQ, GEMINI)
          and partial.chain(ROUTE_CHAT) == (GROQ, GEMINI))
    only_claude = make_router(configured=(CLAUDE,))
    check("only a Claude key: Claude alone serves BOTH routes",
          only_claude.chain(ROUTE_DECISION) == (CLAUDE,)
          and only_claude.chain(ROUTE_CHAT) == (CLAUDE,))
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

    # THE HTTP STATUS DECIDES FIRST. These are the cases that made fresh keys look broken.
    def http_error(status, text, name="APIStatusError", headers=None):
        exc = type(name, (Exception,), {})(text)
        exc.status_code = status
        exc.response = types.SimpleNamespace(status_code=status, headers=headers or {})
        return exc

    status_cases = [
        (http_error(404, "models/gemini-2.5-flash is not found; check quota page"),
         FailureKind.MODEL_UNAVAILABLE, "a retired model is MODEL_UNAVAILABLE, even if it says quota"),
        (http_error(429, "RESOURCE_EXHAUSTED quota exceeded, limit: 0, model gemini-x"),
         FailureKind.MODEL_UNAVAILABLE, "a 429 with 'limit: 0' is no allowance, not a rate limit"),
        (http_error(429, "Your credit balance is too low to access the API"),
         FailureKind.AUTH_FAILURE, "a 429 for no credit is an account problem, not a rate limit"),
        (http_error(429, "rate limit exceeded", headers={"retry-after": "4"}),
         FailureKind.RATE_LIMITED, "a plain 429 is a rate limit"),
        (http_error(401, "invalid x-api-key"), FailureKind.AUTH_FAILURE, "401 is a key problem"),
        (http_error(400, "max_tokens: 429 is too small for request 500"),
         FailureKind.INVALID_REQUEST, "400 wins over numbers in the text"),
        (http_error(529, "Overloaded"), FailureKind.SERVER_ERROR, "529 overloaded is a server error"),
        (http_error(502, "Bad gateway"), FailureKind.SERVER_ERROR, "any 5xx is a server error"),
    ]
    for exc, expected, label in status_cases:
        kind, _ = classify_failure(exc)
        check(label, kind == expected, f"got {kind}")
    _, retry = classify_failure(status_cases[3][0])
    check("a plain 429 keeps the provider's Retry-After", retry == 4.0, str(retry))
    _, retry = classify_failure(http_error(429, "limit: 0", headers={"retry-after": "2"}))
    check("a no-allowance 429 ignores Retry-After (it would only bring the refusal back)",
          retry is None, str(retry))
    check("a bare number in an unclassified message is not evidence of anything",
          classify_failure(Exception("request req_5004290 failed"))[0] == FailureKind.UNKNOWN)

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

    check("INVALID_REQUEST falls through (each provider gets a differently built request)",
          FailureKind.INVALID_REQUEST in pr.FALLBACK_KINDS)
    no_credit = Exception("Error code: 400 - {'type': 'invalid_request_error', 'message': "
                          "'Your credit balance is too low to access the Anthropic API. Please "
                          "go to Plans & Billing to upgrade or purchase credits.'}")
    no_credit.status_code = 400
    check("Anthropic's 400 'credit balance is too low' is an ACCOUNT problem, not a bad request",
          classify_failure(no_credit)[0] == FailureKind.AUTH_FAILURE,
          str(classify_failure(no_credit)))
    check("is_no_credit recognises it", pr.is_no_credit(no_credit))
    check("RATE_LIMITED IS a fallback kind",
          FailureKind.RATE_LIMITED in pr.FALLBACK_KINDS)
    check("AUTH_FAILURE IS a fallback kind (another key may be fine)",
          FailureKind.AUTH_FAILURE in pr.FALLBACK_KINDS)


# ──────────────────────────────────────────────────────────────────────────
#                     3. SEQUENTIAL FALLBACK, EXACT ORDER
# ──────────────────────────────────────────────────────────────────────────

def section_fallback():
    print_system("\n[3] Sequential fallback — exact order, at most one call per provider")

    # ── DMM: Claude succeeds ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION, lambda n: call(n, {CLAUDE: "open chrome"}))
    check("DMM: Claude success returns Claude's answer", result == "open chrome")
    check("DMM: Claude success calls nobody else", seen == [CLAUDE], str(seen))

    # ── DMM: Claude 429 -> Groq ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION,
                        lambda n: call(n, {CLAUDE: RateLimit(), GROQ: "general hi"}))
    check("DMM: Claude 429 falls back to Groq", result == "general hi")
    check("DMM: the order was exactly Claude then Groq", seen == [CLAUDE, GROQ], str(seen))
    check("DMM: Gemini was never touched", GEMINI not in seen)

    # ── DMM: Claude 429, Groq server error -> Gemini ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION, lambda n: call(n, {
        CLAUDE: RateLimit(), GROQ: ServerDown("503 Service Unavailable"), GEMINI: "exit"}))
    check("DMM: Claude -> Groq -> Gemini, in that order",
          seen == [CLAUDE, GROQ, GEMINI] and result == "exit", str(seen))

    # ── DMM: everything fails -> a single clear final error ──
    router = make_router()
    seen, call = calls_recorded()
    try:
        router.run(ROUTE_DECISION, lambda n: call(n, {
            CLAUDE: RateLimit(), GROQ: ServerDown("500 Internal Server Error"),
            GEMINI: RateLimit()}))
        raised = None
    except AllProvidersFailed as exc:
        raised = exc
    check("DMM: total failure raises AllProvidersFailed", raised is not None)
    check("DMM: total failure tried each provider exactly once",
          seen == [CLAUDE, GROQ, GEMINI], str(seen))
    check("DMM: the failure carries every provider's kind",
          raised is not None and raised.kinds ==
          [FailureKind.RATE_LIMITED, FailureKind.SERVER_ERROR, FailureKind.RATE_LIMITED],
          str(raised.kinds if raised else None))

    # ── DMM: Claude unconfigured -> straight to Groq ──
    router = make_router(configured=(GROQ, GEMINI))
    seen, call = calls_recorded()
    router.run(ROUTE_DECISION, lambda n: call(n, {GROQ: "ok"}))
    check("DMM: an unconfigured Claude is skipped without being called",
          seen == [GROQ], str(seen))

    # ── CHAT: Claude succeeds ──
    router = make_router()
    seen, call = calls_recorded()
    router.run(ROUTE_CHAT, lambda n: call(n, {CLAUDE: "hello"}))
    check("CHAT: Claude success calls nobody else", seen == [CLAUDE], str(seen))

    # ── CHAT: Claude 429 -> Groq 503 -> Gemini ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_CHAT, lambda n: call(n, {
        CLAUDE: RateLimit(), GROQ: ServerDown("503 Service Unavailable"), GEMINI: "hi"}))
    check("CHAT: falls back Claude -> Groq -> Gemini", result == "hi")
    check("CHAT: the order was exactly Claude, Groq, Gemini",
          seen == [CLAUDE, GROQ, GEMINI], str(seen))

    # ── CHAT without a Claude key: Groq -> Gemini, exactly as before ──
    router = make_router(configured=(GROQ, GEMINI))
    seen, call = calls_recorded()
    result = router.run(ROUTE_CHAT, lambda n: call(n, {GROQ: RateLimit(), GEMINI: "hi"}))
    check("CHAT without Claude: Groq 429 falls back to Gemini immediately", result == "hi")
    check("CHAT without Claude: the order was exactly Groq then Gemini",
          seen == [GROQ, GEMINI], str(seen))

    # ── A request one provider rejects is handed to the next (different request shapes) ──
    router = make_router()
    seen, call = calls_recorded()
    result = router.run(ROUTE_DECISION, lambda n: call(n, {
        CLAUDE: Exception("invalid_request_error: bad field"), GROQ: "open chrome"}))
    check("a 400 from Claude is answered by Groq, not by 'no model could answer'",
          seen == [CLAUDE, GROQ] and result == "open chrome", str(seen))

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
    router.run(ROUTE_DECISION, lambda n: call(n, {CLAUDE: RateLimit(), GROQ: "ok"}))
    check("after a 429, Claude is in cooldown",
          router.cooldown_remaining(CLAUDE) > 0,
          f"{router.cooldown_remaining(CLAUDE):.0f}s")
    check("after a 429, Claude is not available", not router.is_available(CLAUDE))
    check("the NEXT DMM chain starts at Groq, not Claude",
          router.chain(ROUTE_DECISION) == (GROQ, GEMINI),
          str(router.chain(ROUTE_DECISION)))

    seen2, call2 = calls_recorded()
    router.run(ROUTE_DECISION, lambda n: call2(n, {GROQ: "ok"}))
    check("the next request does not touch the cooled-down provider at all",
          seen2 == [GROQ], str(seen2))

    clock.advance(router.cooldown_seconds(FailureKind.RATE_LIMITED) + 1)
    check("Claude becomes eligible again once the cooldown expires",
          router.is_available(CLAUDE))
    check("and it leads the chain again",
          router.chain(ROUTE_DECISION) == (CLAUDE, GROQ, GEMINI))

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
    check("INVALID_REQUEST stands a provider down only briefly",
          0 < router3.mark_failure(GEMINI, FailureKind.INVALID_REQUEST) <= 60.0)

    # A success clears the cooldown: a provider that just answered is demonstrably available.
    router4 = make_router(clock=FakeClock())
    router4.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    router4.mark_success(GROQ)
    check("a success clears an outstanding cooldown", router4.is_available(GROQ))

    # 11.1: a DMM cooldown must not block chat.
    router5 = make_router(clock=FakeClock())
    router5.mark_failure(CLAUDE, FailureKind.RATE_LIMITED)
    check("a Claude cooldown moves CHAT to Groq -> Gemini",
          router5.chain(ROUTE_CHAT) == (GROQ, GEMINI))
    # 11.2: a Groq cooldown affects both, correctly, and differently.
    router6 = make_router(clock=FakeClock())
    router6.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    check("a Groq cooldown leaves CHAT on Claude -> Gemini",
          router6.chain(ROUTE_CHAT) == (CLAUDE, GEMINI))
    check("a Groq cooldown leaves DECISION on Claude -> Gemini",
          router6.chain(ROUTE_DECISION) == (CLAUDE, GEMINI))

    # A replaced key clears ONLY that provider's verdict.
    router7 = make_router(clock=FakeClock())
    router7.mark_failure(CLAUDE, FailureKind.AUTH_FAILURE)
    router7.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    router7.clear_cooldowns([CLAUDE])
    check("clear_cooldowns([name]) forgets that provider's cooldown",
          router7.is_available(CLAUDE))
    check("and leaves every other provider's cooldown in place",
          not router7.is_available(GROQ))

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
    # module, each of those was 5 + 10 + 15 seconds of blocking sleep and four Claude calls.
    total = []
    for _ in range(20):
        seen, call = calls_recorded()
        try:
            router.run(ROUTE_DECISION, lambda n: call(n, {
                CLAUDE: RateLimit(), GROQ: RateLimit(), GEMINI: "ok"}))
        except AllProvidersFailed:
            pass
        total.extend(seen)

    claude_calls = total.count(CLAUDE)
    check("20 requests against a rate-limited Claude produce ONE Claude call, not 20",
          claude_calls == 1, f"{claude_calls} calls")
    check("and the survivor answers every one of them",
          total.count(GEMINI) == 20, f"{total.count(GEMINI)}")

    # And no sleeping anywhere on the path. This is the specific defect: the old handler
    # blocked the user for 5, then 10, then 15 seconds before trying anything else.
    started = time.perf_counter()
    router2 = make_router(clock=FakeClock())
    for _ in range(50):
        seen, call = calls_recorded()
        router2.run(ROUTE_DECISION, lambda n: call(n, {CLAUDE: RateLimit(), GROQ: "ok"}))
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
          not ({"cohere", "openai", "anthropic", "groq", "google", "requests"} & imported),
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
    router = make_router(configured=(GROQ, GEMINI))
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
    router = make_router(configured=(GROQ, GEMINI))
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
        router.run(ROUTE_DECISION, lambda n: call(n, {CLAUDE: RateLimit(), GROQ: "ok"}))
    logbus.set_level(logbus.INFO)

    text = rec.text()
    check("the DMM route logs under [DMM]", "[DMM]" in text)
    check("the primary attempt is named", "Provider: Claude" in text, text)
    check("the rate limit is reported as RATE_LIMITED",
          "Result: RATE_LIMITED from Claude" in text, text)
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
        router.run(ROUTE_CHAT, lambda n: call(n, {CLAUDE: RateLimit(), GROQ: "ok"}))
    text = rec.text()
    check("the CHAT route logs under [CHAT]", "[CHAT]" in text and "[DMM]" not in text)
    check("chat fallback reads Claude -> Groq",
          "Provider: Claude" in text and "Fallback: Groq" in text, text)

    # A cooled-down provider is mentioned at DEBUG, not once per turn at INFO.
    logbus.set_level(logbus.DEBUG)
    with Recorder() as rec:
        router = make_router()
        router.mark_failure(CLAUDE, FailureKind.RATE_LIMITED)
        _, call = calls_recorded()
        router.run(ROUTE_DECISION, lambda n: call(n, {GROQ: "ok"}))
    logbus.set_level(logbus.INFO)
    skip_lines = rec.containing("Skipping Claude")
    check("a skipped, cooling-down provider is explained", bool(skip_lines), rec.text())
    check("and only at DEBUG, so it does not repeat at INFO for a whole minute",
          all("DEBUG" in line for line in skip_lines), str(skip_lines))

    # No secrets, ever — even when the provider echoes the key into its own error.
    with Recorder() as rec:
        router = make_router()

        def leaky(name):
            raise Exception("401 Unauthorized: CLAUDE_API_KEY=zzTOPSECRETzz rejected")

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
          snapshot["eligible"][ROUTE_CHAT] == [CLAUDE, GROQ, GEMINI])


# ──────────────────────────────────────────────────────────────────────────
#                    9. INTEGRATION WITH THE REAL ENGINE
# ──────────────────────────────────────────────────────────────────────────

def section_engine():
    print_system("\n[9] CentralizedLLMEngine — one authority, one prompt contract")

    from kayra.core import config
    from kayra.intelligence import llm_engine
    from kayra.intelligence.llm_engine import CentralizedLLMEngine

    source = inspect.getsource(llm_engine)

    # The engine must not have grown a second fallback authority back.
    check("the engine no longer imports Cohere", "import cohere" not in source)
    check("the engine no longer sleeps on a rate limit",
          "time.sleep(cooldown)" not in source)
    check("the engine defines no private quota-error predicate",
          "_is_quota_error" not in source)
    check("the DMM goes through the router", "ROUTE_DECISION" in source)
    check("chat goes through the router", "ROUTE_CHAT" in source)
    check("the Anthropic SDK is imported lazily, not at module top",
          not re.search(r"^import anthropic|^from anthropic", source, re.M))

    # A tier-1 suite must not read the host's .env: the engine sees a pinned configuration.
    fake_env = {"CLAUDE_API_KEY": "sk-ant-test-one", "GROQ_API_KEY": "gsk-test",
                "GEMINI_API_KEY": "gm-test", "FORCE_ONLINE": "True"}
    saved_env_values, saved_reset = config.env_values, config.reset_cache
    config_env = lambda: dict(fake_env)
    config.env_values = config_env
    config.reset_cache = lambda: None
    saved_engine_env = llm_engine.env_values
    llm_engine.env_values = config_env

    CentralizedLLMEngine._instance = None
    CentralizedLLMEngine._has_booted = True
    original_probe = CentralizedLLMEngine._check_local_server
    CentralizedLLMEngine._check_local_server = lambda self: False
    try:
        engine = CentralizedLLMEngine()
    finally:
        CentralizedLLMEngine._check_local_server = original_probe

    check("the default Claude model is Haiku 5.5", engine.claude_model == "claude-haiku-5-5")
    check("Groq decides with the small model and chats with the large one",
          engine.groq_decision_model == "openai/gpt-oss-20b"
          and engine.groq_model == "openai/gpt-oss-120b")
    check("Gemini follows Google's current Flash alias",
          engine.gemini_model == "gemini-flash-latest")

    pr.reset_provider_router()
    engine.router = ProviderRouter(clock=FakeClock())
    engine.router.register(CLAUDE, lambda: engine.claude_client is not None)
    engine.router.register(GROQ, lambda: engine.groq_client is not None)
    engine.router.register(GEMINI, lambda: engine.gemini_client is not None)

    calls = []

    class StubClaudeMessages:
        def __init__(self, outcome):
            self.outcome = outcome

        def create(self, **kwargs):
            calls.append(("claude", kwargs))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return types.SimpleNamespace(content=[
                types.SimpleNamespace(type="thinking", thinking=""),
                types.SimpleNamespace(type="text", text=self.outcome)])

        def stream(self, **kwargs):
            calls.append(("claude", kwargs))
            outcome = self.outcome

            class Ctx:
                def __enter__(self_inner):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return types.SimpleNamespace(text_stream=iter(outcome))

                def __exit__(self_inner, *exc):
                    return False
            return Ctx()

    def stub_claude(outcome):
        return types.SimpleNamespace(messages=StubClaudeMessages(outcome))

    class StubCompletions:
        def __init__(self, name, outcome):
            self.name, self.outcome = name, outcome

        def create(self, **kwargs):
            calls.append((self.name, kwargs))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            if kwargs.get("stream"):
                def gen():
                    for piece in self.outcome:
                        yield types.SimpleNamespace(
                            choices=[types.SimpleNamespace(
                                delta=types.SimpleNamespace(content=piece))])
                return gen()
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=self.outcome))])

    def stub_client(name, outcome):
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=StubCompletions(name, outcome)))

    engine.is_online = True

    # ── DMM on Claude: the same contract, cached, fast ──
    engine.claude_client = stub_claude("open chrome")
    engine.groq_client = stub_client("groq", "general hello")
    engine.gemini_client = stub_client("gemini", "general hello")
    tokens = engine.classify_intent("open chrome please do it")
    check("Claude answers the DMM", tokens == ["open chrome"], str(tokens))
    check("and nobody else is called", [n for n, _ in calls] == ["claude"],
          str([n for n, _ in calls]))
    claude_kwargs = calls[0][1]
    check("Claude's DMM system prompt is the rule plus the preamble",
          engine.dmm_preamble.strip() in claude_kwargs["system"][0]["text"])
    check("the system prompt is cached",
          claude_kwargs["system"][0].get("cache_control") == {"type": "ephemeral"})
    check("Claude receives EVERY few-shot example, unsliced, then the question",
          len(claude_kwargs["messages"]) == len(engine.dmm_chat_history) + 1
          and claude_kwargs["messages"][-1]["content"] == "open chrome please do it")
    check("the cache breakpoint sits on the LAST example, before the user's words",
          claude_kwargs["messages"][-2]["content"][-1].get("cache_control")
          == {"type": "ephemeral"})
    check("Haiku 5.5 is asked with thinking off and low effort",
          claude_kwargs.get("thinking") == {"type": "disabled"}
          and claude_kwargs.get("output_config") == {"effort": "low"}, str(claude_kwargs.get("thinking")))
    check("the DMM output budget stays small", claude_kwargs["max_tokens"] == 128)
    check("no sampling parameter is sent to Claude (the API rejects them)",
          "temperature" not in claude_kwargs)

    # ── DMM: Claude rate-limited -> Groq, with the same prompt ──
    calls.clear()
    engine.claude_client = stub_claude(RateLimit())
    engine.groq_client = stub_client("groq", "open chrome")
    tokens = engine.classify_intent("open chrome")
    check("a rate-limited Claude DMM request is answered by Groq",
          tokens == ["open chrome"], str(tokens))
    check("Claude was called once and Groq once — no duplicates",
          [name for name, _ in calls] == ["claude", "groq"],
          str([name for name, _ in calls]))
    groq_kwargs = calls[1][1]
    check("the Groq fallback carries the SAME preamble",
          engine.dmm_preamble.strip() in groq_kwargs["messages"][0]["content"])
    check("the Groq fallback carries EVERY few-shot example, unsliced",
          len(groq_kwargs["messages"]) == len(engine.dmm_chat_history) + 2,
          f"{len(groq_kwargs['messages'])} vs {len(engine.dmm_chat_history) + 2}")
    check("the Groq fallback asks the same question",
          groq_kwargs["messages"][-1]["content"] == "open chrome")
    check("Groq decides with its small model and low reasoning effort",
          groq_kwargs["model"] == engine.groq_decision_model
          and groq_kwargs.get("reasoning_effort") == "low", str(groq_kwargs.get("model")))
    check("the DMM never mixes chat history into the request",
          all(m["content"] in
              {msg["message"] for msg in engine.dmm_chat_history} |
              {groq_kwargs["messages"][0]["content"], "open chrome"}
              for m in groq_kwargs["messages"]))

    # A provider that rejects the reasoning field is asked once more without it, remembered.
    engine.router.clear_cooldowns()
    calls.clear()

    class Rejects(StubCompletions):
        def create(self, **kwargs):
            if "reasoning_effort" in kwargs:
                calls.append((self.name, kwargs))
                exc = type("BadRequestError", (Exception,), {})("reasoning_effort not supported")
                exc.status_code = 400
                raise exc
            return super().create(**kwargs)

    engine.claude_client = None
    engine.groq_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=Rejects("groq", "pause")))
    tokens = engine.classify_intent("pause the music for me now")
    check("a rejected reasoning field is retried once without it",
          tokens == ["pause"] and len(calls) == 2, f"{tokens} {len(calls)}")
    calls.clear()
    engine.classify_intent("pause the music for me now")
    check("and the next request does not send the field again",
          len(calls) == 1 and "reasoning_effort" not in calls[0][1], str(len(calls)))

    # With every decision provider down, the DMM degrades to conversation rather than failing.
    engine.router.clear_cooldowns()
    engine.claude_client = stub_claude(RateLimit())
    engine.groq_client = stub_client("groq", RateLimit())
    engine.gemini_client = stub_client("gemini", RateLimit())
    tokens = engine.classify_intent("what is the capital of France")
    check("a fully exhausted DECISION chain degrades to conversation",
          tokens == ["general what is the capital of France"], str(tokens))

    # ── CHAT on Claude: OpenAI-shaped messages become Claude's shape ──
    engine.router.clear_cooldowns()
    calls.clear()
    engine.claude_client = stub_claude(["Good ", "evening."])
    out = "".join(engine.generate_chat_stream([
        {"role": "system", "content": "You are Kayra."},
        {"role": "system", "content": "Context: it is evening."},
        {"role": "user", "content": "hi"}]))
    check("Claude streams the chat reply", out == "Good evening.", repr(out))
    chat_kwargs = calls[0][1]
    check("system layers are joined into Claude's system field, in order",
          chat_kwargs.get("system") == "You are Kayra.\n\nContext: it is evening.",
          repr(chat_kwargs.get("system")))
    check("only user/assistant turns reach Claude's messages",
          chat_kwargs["messages"] == [{"role": "user", "content": "hi"}])
    check("chat on Haiku 5.5 runs with thinking off for a fast first word",
          chat_kwargs.get("thinking") == {"type": "disabled"})

    # ── CHAT: Claude down -> Groq -> Gemini ──
    engine.router.clear_cooldowns()
    calls.clear()
    engine.claude_client = stub_claude(RateLimit())
    engine.groq_client = stub_client("groq", RateLimit())
    engine.gemini_client = stub_client("gemini", ["Good ", "evening."])
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("a rate-limited Claude and Groq leave Gemini to answer",
          out == "Good evening.", repr(out))
    check("chat called Claude, Groq, Gemini in that order and nothing else",
          [name for name, _ in calls] == ["claude", "groq", "gemini"],
          str([name for name, _ in calls]))

    # Every provider down: ONE sentence that names each provider and the real reason.
    engine.router.clear_cooldowns()
    engine.claude_client = stub_claude(RateLimit())
    engine.groq_client = stub_client("groq", RateLimit())
    gone = Exception("model gemini-2.5-flash is not found")
    gone.status_code = 404
    engine.gemini_client = stub_client("gemini", gone)
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("an exhausted chat chain yields one clear sentence",
          len(out) < 260 and out.count("[") == 1, repr(out))
    check("it names the rate-limited providers as rate-limited",
          "Claude is rate-limited" in out and "Groq is rate-limited" in out, repr(out))
    check("and the retired model as a model problem, NOT a rate limit",
          "Gemini cannot serve the configured model" in out, repr(out))

    # No credit on the Claude account: said in those words, with what to do about it.
    engine.router.clear_cooldowns()
    broke = Exception("Error code: 400 - {'message': 'Your credit balance is too low to "
                      "access the Anthropic API.'}")
    broke.status_code = 400
    engine.claude_client = stub_claude(broke)
    engine.groq_client = None
    engine.gemini_client = None
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("a Claude account without credit is reported as exactly that",
          "Claude has no API credit" in out and "refused" not in out, repr(out))
    check("...with the way out: a fallback key", "Groq or Gemini key" in out, repr(out))
    engine.groq_client = stub_client("groq", ["Yes."])
    engine.gemini_client = stub_client("gemini", ["Yes."])

    # A DMM cooldown must not prevent chat from working.
    engine.router.clear_cooldowns()
    engine.router.mark_failure(CLAUDE, FailureKind.RATE_LIMITED)
    engine.groq_client = stub_client("groq", ["Yes."])
    out = "".join(engine.generate_chat_stream([{"role": "user", "content": "hi"}]))
    check("a Claude cooldown hands chat to Groq", out == "Yes.", repr(out))

    # ── A key saved in Settings applies now, and clears THAT provider's verdict ──
    engine.router.clear_cooldowns()
    engine.router.mark_failure(CLAUDE, FailureKind.AUTH_FAILURE)
    engine.router.mark_failure(GROQ, FailureKind.RATE_LIMITED)
    fake_env["CLAUDE_API_KEY"] = "sk-ant-test-two"
    changed = engine.reload_providers()
    check("reload_providers reports the provider whose key changed",
          changed == [CLAUDE], str(changed))
    check("its 15-minute wrong-key cooldown is forgotten",
          engine.router.is_available(CLAUDE))
    check("an unrelated provider keeps its cooldown",
          not engine.router.is_available(GROQ))
    check("the Claude client was rebuilt for the new key",
          engine.claude_client is not None
          and getattr(engine.claude_client, "api_key", "") == "sk-ant-test-two")
    fake_env["CLAUDE_MODEL"] = "claude-haiku-4-5"
    engine.reload_providers()
    check("a changed model id applies without a restart",
          engine.claude_model == "claude-haiku-4-5")
    check("Haiku 4.5 is sent no thinking/effort fields (it rejects effort)",
          llm_engine.claude_speed_args("claude-haiku-4-5") == {})
    check("Opus/Sonnet 5.5 get low effort but never 'thinking disabled' (a 400 there)",
          llm_engine.claude_speed_args("claude-sonnet-5-5") == {"output_config": {"effort": "low"}})

    # ── Local first, and followed live ──
    fake_env.pop("FORCE_ONLINE", None)
    fake_env["LOCAL_BASE_URL"] = ""          # what Settings writes for an empty field
    probes = []
    original_check = CentralizedLLMEngine._check_local_server

    def fake_probe(self_engine):
        probes.append(self_engine.local_base_url)
        return local_up["value"]

    local_up = {"value": True}
    CentralizedLLMEngine._check_local_server = fake_probe
    try:
        engine.reload_providers()
        check("a BLANK LM Studio address means the default, not 'nowhere'",
              engine.local_base_url == "http://127.0.0.1:1234/v1", engine.local_base_url)
        check("with LM Studio running and no cloud switch on, the local model answers",
              not engine.is_online and probes[-1] == "http://127.0.0.1:1234/v1",
              str(probes[-1:]))
        local_up["value"] = False
        engine._sync_mode(force=True)
        check("LM Studio quitting hands the session to the cloud", engine.is_online)
        local_up["value"] = True
        engine._mode_checked_at = 0.0
        engine._sync_mode()
        check("LM Studio starting AFTER Kayra is picked up without a restart",
              not engine.is_online)
        before = len(probes)
        engine._sync_mode()
        check("the re-check is throttled, not paid on every request", len(probes) == before)
        fake_env["CLOUD_FIRST"] = "True"
        engine.reload_providers()
        check("the cloud switch ON uses the cloud even while LM Studio runs", engine.is_online)
        fake_env["CLOUD_FIRST"] = "False"
        engine.reload_providers()
        check("switching it OFF goes straight back to LM Studio", not engine.is_online)
    finally:
        CentralizedLLMEngine._check_local_server = original_check

    config.env_values, config.reset_cache = saved_env_values, saved_reset
    llm_engine.env_values = saved_engine_env
    CentralizedLLMEngine._instance = None
    pr.reset_provider_router()


def section_vision():
    print_system("\n[10] Vision order — local first (private), then Claude, Gemini, Groq")

    from kayra.automation import ai
    saved_env, saved_probe = ai._env, ai._local_vision_model
    values = {"CLAUDE_API_KEY": "sk-ant-test", "GEMINI_API_KEY": "gm", "GROQ_API_KEY": "gq"}
    ai._env = lambda name, default="": values.get(name, default) or default
    ai._local_vision_model = lambda base: "gemma-4-e4b"
    try:
        order = [label for label, *_ in ai._vision_providers()]
        check("local model first, then Claude, Gemini, Groq",
              order == ["local", "claude", "gemini", "groq"], str(order))
        claude = next(p for p in ai._vision_providers() if p[0] == "claude")
        check("Claude vision follows CLAUDE_MODEL's default (Haiku 5.5)",
              claude[3] == "claude-haiku-5-5", claude[3])
        gemini = next(p for p in ai._vision_providers() if p[0] == "gemini")
        check("Gemini vision is no longer pinned to the retired 2.5 model",
              gemini[3] == "gemini-flash-latest", gemini[3])
        values["CLOUD_FIRST"] = "True"
        order = [label for label, *_ in ai._vision_providers()]
        check("CLOUD_FIRST puts the cloud before the local model",
              order == ["claude", "gemini", "groq", "local"], str(order))
        values["CLAUDE_API_KEY"] = "sk-ant-your_key_here"
        order = [label for label, *_ in ai._vision_providers()]
        check("a placeholder key is not a key", "claude" not in order, str(order))
    finally:
        ai._env, ai._local_vision_model = saved_env, saved_probe


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
    section_vision()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print_success("All provider router checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
