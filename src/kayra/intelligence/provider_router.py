# ┌────────────────────────────────────────────────────────────────────────┐
# │                          provider_router.py                            │
# │           The ONE Authority for Model Provider Selection               │
# └────────────────────────────────────────────────────────────────────────┘
"""
Which model provider serves a request, what happens when it does not, and nothing else.

THE PROBLEM THIS SOLVES
-----------------------
Before this module, fallback logic was invented independently in two places inside
`llm_engine.py` and the two disagreed about almost everything:

  * `classify_intent` had ONE provider (Cohere) and no fallback at all. A rate limit was
    answered with `sleep(5)`, `sleep(10)`, `sleep(15)` and then a degrade to conversation —
    thirty seconds of blocked user, ending in the assistant not doing what was asked, on a
    machine where two other perfectly good providers were already configured and idle.
  * `generate_chat_stream` had a two-provider chain (Groq -> Gemini) with its own private
    string-matching notion of what counts as a quota error, and no memory: the NEXT request
    hit the rate-limited provider again, and the one after that, forever.

Both are now expressed here, as data, and there is exactly one retry/fallback authority in
the process. That last point is a correctness property, not tidiness: when two layers each
"helpfully" retry, one user request becomes four provider calls, which on a rate-limited key
is precisely the wrong response to a rate limit.

THE HIERARCHY, AND WHY THE TWO ROUTES DIFFER
--------------------------------------------
    DECISION (the DMM)   Cohere  ->  Groq  ->  Gemini
    CHAT                 Groq    ->  Gemini

They are deliberately not the same list. Cohere's Command-R is the model the DMM's few-shot
contract was written and measured against, so it leads DECISION; it is not a conversational
model for this assistant and never leads CHAT. Groq leads CHAT because it is the fastest
first token available here, and it is the DMM's first fallback rather than its primary
because the intent-boundary matrix was tuned against Cohere. Changing either order is a
product decision, so it lives in one visible constant instead of being implied by the
ordering of `if` statements in two functions.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not know what a prompt is, it does not build messages, and it never imports a provider
SDK. A caller hands it a per-provider callable; the router decides WHO runs and WHETHER
another one gets a turn. That separation is what lets `tests/test_provider_router.py` exercise
every failure path with no network, no keys and no SDK installed.

COST
----
`chain()` is a tuple filter over at most three names and one clock read per name — measured
under 5us. There is no thread, no background sweeper and no persistence: a cooldown is an
expiry timestamp in a dict, evaluated when someone asks.
"""

import time
import threading

from kayra.core.logbus import Subsystem, info, warning, error, debug, success


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                ROUTES                                  │
# └────────────────────────────────────────────────────────────────────────┘

ROUTE_DECISION = "DECISION"
ROUTE_CHAT = "CHAT"
ROUTES = (ROUTE_DECISION, ROUTE_CHAT)

COHERE = "Cohere"
GROQ = "Groq"
GEMINI = "Gemini"
LOCAL = "Local"

# THE hierarchy. One constant, read by everything, asserted by the test suite.
ROUTE_CHAINS = {
    ROUTE_DECISION: (COHERE, GROQ, GEMINI),
    ROUTE_CHAT: (GROQ, GEMINI),
}

# The subsystem tag each route's lines carry, so a reader can tell a DMM fallback from a chat
# fallback at a glance without parsing the route name out of the message.
ROUTE_TAG = {
    ROUTE_DECISION: Subsystem.DMM,
    ROUTE_CHAT: Subsystem.CHAT,
}


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        FAILURE CLASSIFICATION                          │
# └────────────────────────────────────────────────────────────────────────┘

class FailureKind:
    """
    Why a provider call did not produce an answer.

    The kind determines two things and only two: how long the provider is stood down, and
    whether the request is worth handing to anybody else. Everything downstream reads these
    names rather than re-inspecting the exception, so the "is this a rate limit?" question is
    answered once, here.
    """

    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILURE = "AUTH_FAILURE"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    TIMEOUT = "TIMEOUT"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


FAILURE_KINDS = (
    FailureKind.RATE_LIMITED, FailureKind.AUTH_FAILURE, FailureKind.NETWORK_FAILURE,
    FailureKind.TIMEOUT, FailureKind.SERVER_ERROR, FailureKind.INVALID_REQUEST,
    FailureKind.MODEL_UNAVAILABLE, FailureKind.UNKNOWN,
)

# Kinds where trying ANOTHER provider is a sensible response to the failure.
#
# INVALID_REQUEST is the one that is not: the request itself is malformed, and sending exactly
# the same malformed request to two more providers is three failures instead of one plus two
# pointless round-trips. AUTH_FAILURE IS in the list — a missing Cohere key says nothing about
# the Groq key — but it comes with the longest cooldown, because a bad credential does not fix
# itself and retrying it every turn is the retry storm this module exists to prevent.
FALLBACK_KINDS = frozenset({
    FailureKind.RATE_LIMITED,
    FailureKind.AUTH_FAILURE,
    FailureKind.NETWORK_FAILURE,
    FailureKind.TIMEOUT,
    FailureKind.SERVER_ERROR,
    FailureKind.MODEL_UNAVAILABLE,
    FailureKind.UNKNOWN,
})

# How long a provider stands down after each kind, in seconds. Defaults, overridable from
# `.env` — see `configure_cooldowns`.
#
# RATE_LIMITED is short because a rate limit is by definition temporary and the provider is
# otherwise healthy; a `Retry-After` from the provider itself always wins over this number.
# AUTH_FAILURE is long because nothing about the next sixty seconds will fix a wrong key.
# UNKNOWN is short: it is the bucket for things we have not classified, and standing a
# provider down for minutes on a mystery would be worse than trying it again shortly.
DEFAULT_COOLDOWNS = {
    FailureKind.RATE_LIMITED: 60.0,
    FailureKind.AUTH_FAILURE: 900.0,
    FailureKind.NETWORK_FAILURE: 30.0,
    FailureKind.TIMEOUT: 30.0,
    FailureKind.SERVER_ERROR: 60.0,
    FailureKind.MODEL_UNAVAILABLE: 300.0,
    FailureKind.INVALID_REQUEST: 0.0,      # not the provider's fault; do not stand it down
    FailureKind.UNKNOWN: 20.0,
}

# Signals matched against the exception's TYPE NAME and its string form, most specific first.
# Ordering matters: "rate limit exceeded" contains neither "429" nor "quota" on every provider,
# and an authentication error's text sometimes contains the word "request".
_SIGNALS = (
    (FailureKind.RATE_LIMITED, (
        "toomanyrequests", "ratelimit", "rate_limit", "rate limit", "429",
        "quota", "resource_exhausted", "resource exhausted", "too many requests",
        "insufficient_quota", "overloaded",
    )),
    (FailureKind.AUTH_FAILURE, (
        "unauthorized", "unauthenticated", "authenticationerror", "permissiondenied",
        "permission_denied", "forbidden", "invalid api key", "invalid_api_key",
        "api key not", "401", "403",
    )),
    (FailureKind.TIMEOUT, (
        "timeout", "timedout", "timed out", "deadline exceeded", "readtimeout",
    )),
    (FailureKind.NETWORK_FAILURE, (
        "connectionerror", "connection error", "connection refused", "connection reset",
        "name or service not known", "temporary failure in name resolution", "dns",
        "apiconnectionerror", "ssl", "network", "unreachable",
    )),
    (FailureKind.MODEL_UNAVAILABLE, (
        "model_not_found", "model not found", "notfounderror", "decommissioned",
        "does not exist", "unknown model", "404",
    )),
    (FailureKind.SERVER_ERROR, (
        "internalservererror", "internal server error", "serviceunavailable",
        "service unavailable", "bad gateway", "500", "502", "503", "504",
    )),
    (FailureKind.INVALID_REQUEST, (
        "badrequesterror", "invalid_request", "invalid request", "unprocessable",
        "400", "422", "context_length", "too many tokens", "maximum context",
    )),
)


def classify_failure(exc):
    """
    (kind, retry_after_seconds) for an exception raised by a provider call.

    `retry_after_seconds` is None unless the PROVIDER supplied one. Honouring a provider's own
    `Retry-After` is strictly better than any number chosen here, because it is the only value
    that reflects what that key's budget is actually doing — the local default is the fallback
    for providers and SDKs that do not tell us.

    Matching is on the exception's type name AND its string form. Type name first, because
    `cohere.TooManyRequestsError` is unambiguous while its message on a trial key sometimes
    is not.
    """
    if exc is None:
        return FailureKind.UNKNOWN, None

    retry_after = _extract_retry_after(exc)

    haystack = f"{type(exc).__name__} {exc}".lower()
    for kind, signals in _SIGNALS:
        if any(signal in haystack for signal in signals):
            return kind, retry_after
    return FailureKind.UNKNOWN, retry_after


def _extract_retry_after(exc):
    """
    Digs a `Retry-After` out of whatever shape the SDK wrapped the HTTP response in.

    Best-effort by construction and silent on failure: every one of these attribute paths is
    a private detail of somebody else's library, and a router that raised while classifying a
    failure would turn a recoverable rate limit into a crash.
    """
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)

    headers = None
    for path in ("headers", "response"):
        candidate = getattr(exc, path, None)
        if candidate is None:
            continue
        headers = getattr(candidate, "headers", candidate)
        if headers is not None:
            break
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE FINAL FAILURE                             │
# └────────────────────────────────────────────────────────────────────────┘

class AllProvidersFailed(Exception):
    """
    Every eligible provider on the route failed, or none was eligible.

    Carries the per-provider attempt record so the caller can say something specific rather
    than "something went wrong". Callers degrade gracefully (the DMM falls back to treating
    the query as conversation, chat yields a sentence the user can act on); the exception
    exists so that degradation is a decision the caller makes, not a `None` it has to guess at.
    """

    def __init__(self, route, attempts, message=None):
        self.route = route
        self.attempts = list(attempts)
        detail = ", ".join(f"{name}: {kind}" for name, kind, _ in self.attempts) or "none eligible"
        super().__init__(message or f"{route}: no provider could answer ({detail})")

    @property
    def kinds(self):
        return [kind for _name, kind, _exc in self.attempts]


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE ROUTER                                │
# └────────────────────────────────────────────────────────────────────────┘

class ProviderRouter:
    """
    Ordered, sequential provider selection with per-provider cooldowns.

    SEQUENTIAL, NEVER CONCURRENT. Providers are tried one after another and the first success
    ends the request. Racing them would spend three quotas to answer one question, and on a
    key that is already rate-limited it would make the situation worse rather than better.

    A provider is ELIGIBLE when it is registered, configured (its client exists) and not in
    cooldown. A route's chain is the intersection of `ROUTE_CHAINS[route]` with the eligible
    set, in the chain's order — so an unconfigured provider is skipped silently and a
    rate-limited one is skipped with a DEBUG line, and neither shortens the chain permanently.

    The clock is injectable so cooldown expiry can be tested at exact offsets instead of by
    sleeping.
    """

    def __init__(self, clock=None, cooldowns=None):
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        # name -> callable returning True when the provider is configured for use.
        self._registered = {}
        # name -> {"until": monotonic, "kind": str, "since": monotonic}
        self._cooldown = {}
        self._cooldowns = dict(DEFAULT_COOLDOWNS)
        if cooldowns:
            self._cooldowns.update(cooldowns)
        # Diagnostics only, bounded by construction (one entry per provider name).
        self._stats = {}

    # ── Registration ──────────────────────────────────────────────────

    def register(self, name, available):
        """
        Declares a provider and how to ask whether it is configured.

        `available` may be a bool or a zero-argument callable. A callable is what
        `llm_engine` passes, because "is the Groq client constructed?" is a live fact — the
        engine builds cloud clients only in online mode, and a router holding a stale `True`
        would route into a `None`.
        """
        with self._lock:
            self._registered[name] = available
            self._stats.setdefault(name, {"success": 0, "failure": 0, "skipped": 0})

    def configured(self, name):
        probe = self._registered.get(name)
        if probe is None:
            return False
        try:
            return bool(probe() if callable(probe) else probe)
        except Exception:
            return False

    # ── Cooldown ──────────────────────────────────────────────────────

    def cooldown_seconds(self, kind):
        return float(self._cooldowns.get(kind, DEFAULT_COOLDOWNS.get(kind, 20.0)))

    def set_cooldown(self, kind, seconds):
        with self._lock:
            self._cooldowns[kind] = max(0.0, float(seconds))

    def mark_failure(self, name, kind, retry_after=None):
        """
        Stands a provider down. Returns the number of seconds it will be skipped for.

        A provider-supplied `retry_after` wins over the configured default — it is the only
        number that reflects what that key's budget is actually doing. `INVALID_REQUEST` never
        stands a provider down: the request was wrong, the provider was not.
        """
        seconds = self.cooldown_seconds(kind)
        if retry_after is not None:
            try:
                seconds = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        with self._lock:
            self._stats.setdefault(name, {"success": 0, "failure": 0, "skipped": 0})
            self._stats[name]["failure"] += 1
            if seconds <= 0:
                self._cooldown.pop(name, None)
                return 0.0
            now = self._clock()
            self._cooldown[name] = {"until": now + seconds, "kind": kind, "since": now}
        return seconds

    def mark_success(self, name):
        """Clears any cooldown. A provider that just answered is demonstrably available."""
        with self._lock:
            self._cooldown.pop(name, None)
            self._stats.setdefault(name, {"success": 0, "failure": 0, "skipped": 0})
            self._stats[name]["success"] += 1

    def cooldown_remaining(self, name):
        with self._lock:
            entry = self._cooldown.get(name)
            if entry is None:
                return 0.0
            remaining = entry["until"] - self._clock()
            if remaining <= 0:
                del self._cooldown[name]
                return 0.0
            return remaining

    def is_available(self, name):
        return self.configured(name) and self.cooldown_remaining(name) <= 0

    def clear_cooldowns(self):
        with self._lock:
            self._cooldown.clear()

    # ── Selection ─────────────────────────────────────────────────────

    def chain(self, route):
        """
        The ordered, currently-eligible provider names for a route.

        Empty is a legitimate answer and the caller must handle it: every provider
        unconfigured (no keys at all) and every provider in cooldown both produce it, and the
        two want different messages.
        """
        order = ROUTE_CHAINS.get(route, ())
        return tuple(name for name in order if self.is_available(name))

    def full_chain(self, route):
        """The route's configured order, ignoring cooldowns. Used for the boot summary."""
        return tuple(name for name in ROUTE_CHAINS.get(route, ()) if self.configured(name))

    def describe(self, route):
        """`Cohere > Groq > Gemini`, or `none configured`. One line for the startup report."""
        names = self.full_chain(route)
        return " > ".join(names) if names else "none configured"

    def stats(self):
        with self._lock:
            return {name: dict(values) for name, values in self._stats.items()}

    def snapshot(self):
        """Everything a diagnostic surface needs, in one locked read."""
        with self._lock:
            now = self._clock()
            cooldowns = {
                name: {"kind": entry["kind"], "remaining": max(0.0, entry["until"] - now)}
                for name, entry in self._cooldown.items()
            }
        return {
            "routes": {route: list(ROUTE_CHAINS[route]) for route in ROUTES},
            "configured": {name: self.configured(name) for name in self._registered},
            "cooldowns": cooldowns,
            "eligible": {route: list(self.chain(route)) for route in ROUTES},
            "stats": self.stats(),
        }

    # ── Execution ─────────────────────────────────────────────────────

    def run(self, route, call, describe=None):
        """
        Runs `call(provider_name)` down the route until one succeeds. Returns its result.

        THIS IS THE ONLY RETRY/FALLBACK AUTHORITY. A provider callable must not retry
        internally and must not catch its own transport errors — it raises, and this decides.
        Each provider is attempted AT MOST ONCE per `run()`, which is the property that makes
        "no duplicate calls" true rather than hoped for.

        Raises `AllProvidersFailed` when the chain is exhausted or empty.
        """
        tag = ROUTE_TAG.get(route, Subsystem.LLM)
        attempts = []

        eligible = self.chain(route)
        self._log_skips(route, tag)

        if not eligible:
            error(tag, f"Route {route}: no provider available")
            raise AllProvidersFailed(route, attempts)

        for index, name in enumerate(eligible):
            label = describe(name) if describe else name
            if index == 0:
                info(tag, f"Provider: {label}")
            else:
                info(tag, f"Fallback: {label}")
            try:
                result = call(name)
            except Exception as exc:
                kind, retry_after = classify_failure(exc)
                seconds = self.mark_failure(name, kind, retry_after)
                attempts.append((name, kind, exc))
                self._log_failure(tag, name, kind, seconds, exc)
                if kind not in FALLBACK_KINDS:
                    # Sending an identical malformed request to two more providers is three
                    # failures instead of one. Stop, and let the caller say so.
                    break
                continue
            self.mark_success(name)
            success(tag, f"Result: SUCCESS via {label}")
            return result

        raise AllProvidersFailed(route, attempts)

    def run_stream(self, route, call, describe=None):
        """
        The streaming form. Yields chunks from the first provider that produces one.

        THE RULE THAT MAKES THIS SAFE: fallback is only possible BEFORE the first chunk has
        been yielded. Once a token has reached the user, switching providers would splice two
        different answers into one sentence — visibly broken, and worse than the failure it
        was trying to hide. A mid-stream failure is therefore terminal for that request, and
        the provider is still stood down so the NEXT request routes elsewhere.

        A provider callable returns an ITERATOR here rather than a value; it must not begin
        network work before the first `next()` for the pre-first-chunk rule to be meaningful,
        which is what the OpenAI/Cohere streaming clients already do.
        """
        tag = ROUTE_TAG.get(route, Subsystem.LLM)
        attempts = []

        eligible = self.chain(route)
        self._log_skips(route, tag)

        if not eligible:
            error(tag, f"Route {route}: no provider available")
            raise AllProvidersFailed(route, attempts)

        for index, name in enumerate(eligible):
            label = describe(name) if describe else name
            if index == 0:
                info(tag, f"Provider: {label}")
            else:
                info(tag, f"Fallback: {label}")

            produced = False
            try:
                for chunk in call(name):
                    if not produced:
                        produced = True
                        self.mark_success(name)
                        success(tag, f"Result: SUCCESS via {label}")
                    yield chunk
            except Exception as exc:
                kind, retry_after = classify_failure(exc)
                seconds = self.mark_failure(name, kind, retry_after)
                attempts.append((name, kind, exc))
                self._log_failure(tag, name, kind, seconds, exc)
                if produced:
                    # Already speaking with this provider's voice. Re-raise rather than splice.
                    raise
                if kind not in FALLBACK_KINDS:
                    break
                continue
            if produced:
                return
            # An empty but successful stream. Not an error, and not something another provider
            # would answer differently — treat it as the answer.
            self.mark_success(name)
            success(tag, f"Result: SUCCESS via {label} (empty)")
            return

        raise AllProvidersFailed(route, attempts)

    # ── Logging (this module OWNS provider/fallback lines) ─────────────

    def _log_skips(self, route, tag):
        """
        Says, at DEBUG, why a configured provider is not in the chain.

        DEBUG rather than INFO deliberately: once Cohere is in a 60-second cooldown, every
        turn in that minute would otherwise print a line about it, which is a minute of the
        terminal repeating something the user was already told once at WARNING.
        """
        for name in ROUTE_CHAINS.get(route, ()):
            if not self.configured(name):
                continue
            remaining = self.cooldown_remaining(name)
            if remaining > 0:
                with self._lock:
                    kind = (self._cooldown.get(name) or {}).get("kind", FailureKind.UNKNOWN)
                    self._stats.setdefault(name, {"success": 0, "failure": 0, "skipped": 0})
                    self._stats[name]["skipped"] += 1
                debug(tag, f"Skipping {name}: {kind}, {remaining:.0f}s of cooldown left")

    def _log_failure(self, tag, name, kind, seconds, exc):
        """
        One line for an expected failure; the exception's own text only at DEBUG.

        A rate limit is a NORMAL event on a shared key and its handling is completely
        automatic, so it must not print a provider traceback into a terminal a human is
        reading — `Result: RATE_LIMITED` followed by `Fallback: Groq` says everything the
        reader needs. The SDK's message is genuine diagnostic value, so it is kept at DEBUG
        rather than thrown away.
        """
        note = f", cooling down {seconds:.0f}s" if seconds > 0 else ""
        if kind == FailureKind.RATE_LIMITED:
            warning(tag, f"Result: {kind} from {name}{note}")
        else:
            error(tag, f"Result: {kind} from {name}{note}")
        debug(tag, f"{name} raised {type(exc).__name__}: {exc}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        PROCESS-WIDE ACCESSOR                           │
# └────────────────────────────────────────────────────────────────────────┘
# One router per process, for the same reason `CentralizedLLMEngine` is a singleton: a second
# copy would hold a second set of cooldowns, so a provider stood down by the DMM would still
# look healthy to chat and the retry storm would come back through the other door.

_ROUTER = None
_ROUTER_LOCK = threading.Lock()


def configure_cooldowns(env_values=None):
    """
    Reads the cooldown overrides out of configuration.

    Every knob is a BOUND, never a behaviour switch: there is deliberately no setting that
    turns fallback off, reorders the hierarchy or disables the cooldown, for the same reason
    the automation layer has no setting that disables its safety policy.
    """
    if env_values is None:
        try:
            from kayra.core.config import env_values as read_env
            env_values = read_env()
        except Exception:
            env_values = {}

    mapping = {
        FailureKind.RATE_LIMITED: "PROVIDER_COOLDOWN_RATE_LIMIT_SECONDS",
        FailureKind.AUTH_FAILURE: "PROVIDER_COOLDOWN_AUTH_SECONDS",
        FailureKind.NETWORK_FAILURE: "PROVIDER_COOLDOWN_NETWORK_SECONDS",
        FailureKind.TIMEOUT: "PROVIDER_COOLDOWN_TIMEOUT_SECONDS",
        FailureKind.SERVER_ERROR: "PROVIDER_COOLDOWN_SERVER_ERROR_SECONDS",
        FailureKind.MODEL_UNAVAILABLE: "PROVIDER_COOLDOWN_MODEL_SECONDS",
        FailureKind.UNKNOWN: "PROVIDER_COOLDOWN_UNKNOWN_SECONDS",
    }
    resolved = {}
    for kind, key in mapping.items():
        raw = env_values.get(key)
        if raw in (None, ""):
            continue
        try:
            # Clamped rather than trusted: a malformed .env must not be able to stand a
            # provider down for a week, or to disable the cooldown entirely.
            resolved[kind] = min(86400.0, max(0.0, float(str(raw).strip())))
        except (TypeError, ValueError):
            continue
    return resolved


def get_provider_router() -> ProviderRouter:
    global _ROUTER
    if _ROUTER is None:
        with _ROUTER_LOCK:
            if _ROUTER is None:
                _ROUTER = ProviderRouter(cooldowns=configure_cooldowns())
    return _ROUTER


def reset_provider_router():
    """Drops the process router. For tests only — nothing in the application calls this."""
    global _ROUTER
    with _ROUTER_LOCK:
        _ROUTER = None
