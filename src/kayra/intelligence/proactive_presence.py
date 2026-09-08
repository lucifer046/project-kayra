# ┌────────────────────────────────────────────────────────────────────────┐
# │                        proactive_presence.py                           │
# │        Contextual Presence — "Is there a reason to speak right now?"    │
# └────────────────────────────────────────────────────────────────────────┘
"""
The layer that makes Kayra feel PRESENT rather than merely responsive.

It answers exactly one question:

    *Does the assistant have a genuinely good reason to say something right now?*

It does NOT answer "what should Kayra do with a user command" — that is the DMM and the task
pipeline, and nothing in this module touches them. It is a pure decision-and-language layer:
signals in, a scored candidate (or `None`) out, plus the sentence to say.

WHERE IT SITS
-------------
This is an EXTENSION of `services.proactive_agent`, not a replacement for it. The agent still
owns the thread, the habit model, the safety gate, the cooldown ledgers and the single route
to the TTS pipeline. Presence plugs into it at three seams:

    agent.evaluate()   -> presence.candidates(signals)   extra reasons to speak
    agent._cooldown_ok -> presence.should_speak(...)     extra suppression rules
    agent._phrase()    -> presence.realize(candidate)    the actual wording

That layering is the point. Adding a second agent, a second thread, a second TTS queue or a
second cooldown architecture would have been the expensive way to buy nothing — every one of
those already exists and works.

WHAT IT IS NOT
--------------
* It is not an LLM loop. `candidates()` and `should_speak()` are integer and string
  arithmetic over values already resident in memory: no model, no network, no disk. The LLM
  is consulted at most once per *approved and about to be spoken* line, purely to reword it,
  and every kind has a template that is used when the model is absent, slow or wrong.
* It is not a joke engine. Humour here is one observational remark drawn from context, gated
  behind its own switch and a cooldown measured in hours.
* It is not a quote database. There is no copied dialogue anywhere in this file; the
  behavioural pattern being reproduced is *observe, be brief, offer, then be quiet* — the
  language is Kayra's own.
* It is not surveillance. The signals are the clock, the foreground window title Kayra
  already samples, the system metrics the System screen already reads, and counters derived
  from the DMM tokens the habit model already records. Nothing new is watched and nothing
  new is stored.

THE DEFAULT IS SILENCE
----------------------
Every gate in here is written so that "no" is what happens when nothing decides otherwise.
A candidate must clear its tier's score floor, its own cooldown, its cooldown GROUP, the
global spacing, the per-day budget and a similarity check against what was recently said
before it is even offered to the agent — which then still has to find a safe window.
"""

import os
import re
import time
import random
import datetime
import threading
from collections import deque


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘
# Read from the process environment, exactly like `ProactiveConfig` — `.env` is loaded by
# `app.py` long before this module is imported. Every knob is range-clamped and every value
# has a defensive default, so a malformed `.env` degrades to sane behaviour instead of
# crashing a background thread.

def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_num(name, default, cast=float, minimum=None, maximum=None):
    try:
        value = cast(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _env_minutes(name, default_minutes):
    return _env_num(name, default_minutes, float, 0.0, 100000.0) * 60.0


class PresenceConfig:
    """Snapshot of the presence knobs, resolved once at construction."""

    def __init__(self):
        self.enabled = _env_bool("PROACTIVE_PRESENCE_ENABLED", True)

        # Per-category switches. Each one is independently useful: a user may want the
        # late-night observation and none of the humour, or system warnings and no greetings.
        self.greetings = _env_bool("PROACTIVE_GREETINGS_ENABLED", True)
        self.context = _env_bool("PROACTIVE_CONTEXT_ENABLED", True)
        self.late_night = _env_bool("PROACTIVE_LATE_NIGHT_ENABLED", True)
        self.work_session = _env_bool("PROACTIVE_WORK_SESSION_ENABLED", True)
        self.system = _env_bool("PROACTIVE_SYSTEM_ENABLED", True)
        self.humor = _env_bool("PROACTIVE_HUMOR_ENABLED", True)

        # Late-night window. Shared with the legacy agent knobs so there is one answer to
        # "when is late" rather than two that can drift apart.
        self.late_night_start = int(_env_num("PROACTIVE_LATE_NIGHT_START_HOUR", 1, int, 0, 23))
        self.late_night_end = int(_env_num("PROACTIVE_LATE_NIGHT_END_HOUR", 5, int, 0, 23))

        # Work-session milestones, in minutes. The FIRST is deliberately gentle and the later
        # ones are rarer: a reminder that arrives every forty minutes is nagging, which is the
        # failure mode this whole subsystem is built to avoid.
        self.work_milestones = _parse_minutes_list(
            os.environ.get("PROACTIVE_WORK_SESSION_MINUTES"), (45.0, 90.0, 150.0))
        # A gap longer than this ends the current work session, so the milestones restart.
        self.session_gap_s = _env_minutes("PROACTIVE_SESSION_GAP_MINUTES", 20.0)

        # Absence thresholds for the greeting's return detection.
        self.short_absence_s = _env_minutes("PROACTIVE_SHORT_ABSENCE_MINUTES", 12.0)
        self.long_absence_s = _env_minutes("PROACTIVE_LONG_ABSENCE_MINUTES", 180.0)
        # How long the foreground window has to be unreadable (locked screen, nothing
        # focused) before coming back counts as the user RETURNING to the machine.
        self.away_s = _env_minutes("PROACTIVE_AWAY_MINUTES", 25.0)

        # System pressure. A spike is not an anomaly — the reading has to hold across this
        # many consecutive samples before it is worth a sentence.
        self.system_sample_s = _env_num("PROACTIVE_SYSTEM_SAMPLE_SECONDS", 60.0, float, 15.0, 600.0)
        self.ram_warn = _env_num("PROACTIVE_RAM_WARN_PERCENT", 90.0, float, 50.0, 100.0)
        self.ram_critical = _env_num("PROACTIVE_RAM_CRITICAL_PERCENT", 96.0, float, 50.0, 100.0)
        self.cpu_warn = _env_num("PROACTIVE_CPU_WARN_PERCENT", 92.0, float, 50.0, 100.0)
        self.battery_warn = _env_num("PROACTIVE_BATTERY_WARN_PERCENT", 15.0, float, 1.0, 60.0)
        self.pressure_samples = int(_env_num("PROACTIVE_PRESSURE_SAMPLES", 3, int, 1, 20))

        # Anticipation: how many failures of the same action before offering another approach.
        self.repeat_failures = int(_env_num("PROACTIVE_REPEAT_FAILURE_COUNT", 2, int, 2, 10))
        self.repeat_actions = int(_env_num("PROACTIVE_REPEAT_ACTION_COUNT", 4, int, 2, 20))
        self.repeat_window_s = _env_minutes("PROACTIVE_REPEAT_WINDOW_MINUTES", 12.0)

        # Spacing. `min_gap_s` applies across every presence category and sits UNDER the
        # agent's own global cooldown; `daily_budget` is the backstop that keeps a whole day
        # bounded no matter how many distinct reasons occur.
        self.min_gap_s = _env_minutes("PROACTIVE_PRESENCE_MIN_GAP_MINUTES", 25.0)
        self.daily_budget = int(_env_num("PROACTIVE_PRESENCE_DAILY_BUDGET", 8, int, 0, 100))
        # Two utterances this similar are the same remark wearing different words.
        self.similarity_threshold = _env_num("PROACTIVE_SIMILARITY_THRESHOLD", 0.6, float, 0.1, 1.0)
        # After using the form of address, prefer wordings that leave it out for a while.
        self.address_gap_s = _env_minutes("PROACTIVE_ADDRESS_GAP_MINUTES", 10.0)

        # Optional LLM rewording of an ALREADY-APPROVED line. Never used to decide anything.
        self.llm_phrasing = _env_bool("PROACTIVE_PRESENCE_LLM_PHRASING", False)

        self.address = (os.environ.get("PROACTIVE_ADDRESS") or "").strip() or _default_address()


def _default_address():
    """The form of address, derived from the same setting the identity prompt uses."""
    gender = (os.environ.get("USER_GENDER") or "Male").strip().lower()
    return "ma'am" if gender == "female" else "sir"


def _parse_minutes_list(raw, default):
    if not raw:
        return tuple(float(v) * 60.0 for v in default)
    values = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            minutes = float(part)
        except ValueError:
            continue
        if 1.0 <= minutes <= 1440.0:
            values.append(minutes * 60.0)
    return tuple(sorted(set(values))) or tuple(float(v) * 60.0 for v in default)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             PRIORITY TIERS                             │
# └────────────────────────────────────────────────────────────────────────┘
# A tier is three numbers: how strong a reason it needs, how much the reason itself can add,
# and how long it must wait afterwards. Encoding priority as arithmetic rather than as an
# `if` chain is what makes "ambient remarks are rare" a property of the system instead of a
# promise in a comment.

class Tier:
    CRITICAL = "critical"      # something is actually wrong and the user needs to know
    IMPORTANT = "important"    # a useful intervention
    SOCIAL = "social"          # conversational presence
    AMBIENT = "ambient"        # optional flavour; almost always suppressed


# base score, maximum evidence contribution, minimum spacing between two of this tier
TIER_PROFILE = {
    Tier.CRITICAL:  {"base": 0.75, "evidence": 0.25, "cooldown": 20 * 60},
    Tier.IMPORTANT: {"base": 0.45, "evidence": 0.30, "cooldown": 60 * 60},
    Tier.SOCIAL:    {"base": 0.36, "evidence": 0.29, "cooldown": 45 * 60},
    Tier.AMBIENT:   {"base": 0.28, "evidence": 0.32, "cooldown": 6 * 3600},
}

# Only CRITICAL may speak inside the agent's global cooldown. Everything else waits its turn,
# which is what stops a busy hour turning into a monologue.
TIER_BYPASSES_GLOBAL = frozenset({Tier.CRITICAL})

# Per-kind minimum spacing. Longer than the tier's own spacing wherever repeating the same
# observation would be worse than repeating the tier.
KIND_COOLDOWNS = {
    "late_night": 4 * 3600,
    "work_session": 75 * 60,
    "user_return": 90 * 60,
    "system_pressure": 30 * 60,
    "battery_low": 20 * 60,
    "repeated_failure": 30 * 60,
    "repeated_action": 3 * 3600,
    "dry_remark": 6 * 3600,
}

# Kinds that describe the same underlying situation ("you have been at this a long time")
# share a group, so only one of them can fire per cooldown window. Without this, a long
# night at the keyboard produces the break nudge, the work-session observation and the
# late-night remark within minutes of each other, all of them true and all of them the same
# point made three times.
KIND_GROUPS = {
    "work_session": "fatigue",
    "break": "fatigue",
    "late_night": "fatigue",
    "dry_remark": "fatigue",
    "system_pressure": "system",
    "battery_low": "system",
}
GROUP_COOLDOWNS = {"fatigue": 70 * 60, "system": 25 * 60}

# Kinds where an LLM rewording is worth the round-trip: the observation is contextual and
# canned wording is noticeable. Warnings and acknowledgements are deliberately NOT here —
# "memory usage is unusually high" does not benefit from being rephrased, and a warning that
# depends on a cloud call is a worse warning.
LLM_WORTHY_KINDS = frozenset({"work_session", "late_night", "repeated_failure"})


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              THE SIGNALS                               │
# └────────────────────────────────────────────────────────────────────────┘

class PresenceSignals:
    """
    One immutable read of everything a presence decision is allowed to consider.

    Assembled once per tick by the caller and passed down, so every candidate builder in a
    single evaluation reasons about the same instant. Half of these come straight from the
    proactive agent's existing observation (foreground app, focus stopwatch), half from the
    runtime state it already reads, and the system block is the metrics module the System
    screen already uses. Nothing here is sampled specially for this feature.
    """

    __slots__ = ("now", "stamp", "hour", "day_part", "weekday",
                 "focus_app", "focus_seconds", "away_seconds",
                 "session_seconds", "work_seconds", "interactions", "gap_seconds",
                 "seconds_since_user", "sleeping", "mood", "topic", "conversation_mode",
                 "cpu_percent", "ram_percent", "battery_percent", "battery_plugged",
                 "repeated_failure", "repeated_action")

    def __init__(self, now, **kwargs):
        self.now = now
        self.stamp = kwargs.get("stamp") or datetime.datetime.fromtimestamp(now)
        self.hour = self.stamp.hour
        self.day_part = day_part(self.hour)
        self.weekday = self.stamp.weekday()

        self.focus_app = kwargs.get("focus_app")
        self.focus_seconds = float(kwargs.get("focus_seconds") or 0.0)
        self.away_seconds = float(kwargs.get("away_seconds") or 0.0)

        self.session_seconds = float(kwargs.get("session_seconds") or 0.0)
        self.work_seconds = float(kwargs.get("work_seconds") or 0.0)
        self.interactions = int(kwargs.get("interactions") or 0)
        self.gap_seconds = float(kwargs.get("gap_seconds") or 0.0)
        self.seconds_since_user = float(kwargs.get("seconds_since_user") or 0.0)
        self.sleeping = bool(kwargs.get("sleeping"))
        self.mood = kwargs.get("mood")
        # What the exchange is currently about, from `core.conversation_context`. The
        # presence layer wanted this from the start and had no source for it, so its only
        # notion of context was the foreground window title.
        self.topic = kwargs.get("topic") or ""
        self.conversation_mode = kwargs.get("conversation_mode") or ""


        self.cpu_percent = kwargs.get("cpu_percent")
        self.ram_percent = kwargs.get("ram_percent")
        self.battery_percent = kwargs.get("battery_percent")
        self.battery_plugged = kwargs.get("battery_plugged")

        self.repeated_failure = kwargs.get("repeated_failure")   # (action, count) or None
        self.repeated_action = kwargs.get("repeated_action")     # (action, count) or None

    def as_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__ if slot != "stamp"}


def day_part(hour):
    """Coarse label for the hour. Deliberately coarse — greetings, not astronomy."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def salutation(hour):
    """
    The part of day as it is SAID in a greeting, which is not the same thing.

    English has three salutations, not four: "good night" is a farewell, so greeting someone
    who says hello at two in the morning with it is simply wrong. Caught in live testing —
    the engine correctly identified the hour as night and then said the one thing a person
    never says on being greeted. Night maps to evening, which is what people actually say.
    """
    part = day_part(hour)
    return "evening" if part == "night" else part


# ┌────────────────────────────────────────────────────────────────────────┐
# │                              CANDIDATES                                │
# └────────────────────────────────────────────────────────────────────────┘

class PresenceCandidate:
    """
    One thing Kayra could say, with the evidence and the arithmetic that produced it.

    `reason` is a short machine-readable trace ("user_active_143m + late_hour") kept purely
    so the console can explain a decision without anyone adding print statements — the same
    contract the existing agent's `components` dict has.
    """

    __slots__ = ("kind", "tier", "text", "components", "context", "reason",
                 "score", "created_s", "group", "bypass_global", "llm_worthy")

    def __init__(self, kind, tier, text, evidence, reason="", context=None, llm_worthy=None):
        self.kind = kind
        self.tier = tier if tier in TIER_PROFILE else Tier.SOCIAL
        self.text = text
        self.context = dict(context or {})
        self.reason = reason
        self.group = KIND_GROUPS.get(kind)
        self.bypass_global = self.tier in TIER_BYPASSES_GLOBAL
        self.llm_worthy = (kind in LLM_WORTHY_KINDS) if llm_worthy is None else bool(llm_worthy)

        profile = TIER_PROFILE[self.tier]
        strength = _clamp(evidence)
        self.score = round(_clamp(profile["base"] + profile["evidence"] * strength), 3)
        self.components = {"tier": self.tier, "base": profile["base"],
                           "evidence": round(strength, 3)}
        # Stamped by the agent from ITS clock when the candidate is accepted. Mixing this
        # module's clock with the agent's made every deferred candidate look instantly stale.
        self.created_s = None

    def __repr__(self):
        return f"<PresenceCandidate {self.kind}/{self.tier} score={self.score:.2f}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             THE PHRASEBOOK                             │
# └────────────────────────────────────────────────────────────────────────┘
# Original Kayra language, written to one register: composed, brief, observant, formal but
# warm, and never enthusiastic. Every line is one or two short sentences because it is spoken
# aloud and a spoken paragraph is an interruption rather than a remark.
#
# `{address}` is optional by design. The realizer prefers a wording WITHOUT it when the form
# of address was used recently, which is what keeps "sir" from appearing in every sentence.

PHRASES = {
    "greeting.morning": (
        "Good morning, {address}.",
        "Good morning.",
        "Morning, {address}. What can I do for you?",
    ),
    "greeting.afternoon": (
        "Good afternoon, {address}.",
        "Afternoon. How can I help?",
        "Good afternoon, {address}. How may I help?",
    ),
    "greeting.evening": (
        "Good evening, {address}.",
        "Good evening. What do you need?",
        "Evening, {address}.",
    ),
    "greeting.night": (
        "Good evening, {address}.",
        "Still up, I see. What can I do?",
        "Good evening, {address}. You're still working?",
    ),
    "greeting.first_of_session": (
        "Good {part}, {address}. What can I do for you?",
        "Good {part}. I'm here when you need me.",
    ),
    "greeting.short_return": (
        "Back already, {address}?",
        "That didn't take long.",
        "Back so soon?",
    ),
    "greeting.return": (
        "Welcome back, {address}.",
        "Welcome back.",
        "There you are, {address}.",
    ),
    "greeting.long_return": (
        "Welcome back, {address}. I trust that went well.",
        "Welcome back. I've been holding the fort.",
        "Welcome back, {address}.",
    ),
    "greeting.late_working": (
        "Good evening, {address}. Still at it?",
        "You're still working. What do you need?",
    ),

    "boot.default": (
        "{name} online.",
        "{name} here.",
    ),
    "boot.morning": (
        "Good morning, {address}. {name} is online.",
        "{name} online. Good morning.",
    ),
    "boot.night": (
        "{name} online. It's rather late, {address}.",
        "Good evening, {address}. {name} online.",
        "{name} online. It's rather late.",
    ),

    "late_night": (
        "It's rather late, {address}. Still working?",
        "You're still at it. Anything keeping you up?",
        "Good evening, {address}. Planning to call it a night soon?",
        "Well past the usual hours. Shall I help you finish up?",
    ),
    "work_session.first": (
        "You've been at this for about {minutes} minutes, {address}.",
        "That's a fair stretch of work. Worth a moment away from the screen.",
    ),
    "work_session.later": (
        "Still going, I see. Shall I help you finish this?",
        "We're past {hours} hours now, {address}. Would a short break help?",
        "You've been working for {hours} hours. I'd suggest a pause.",
    ),
    "user_return": (
        "Welcome back, {address}.",
        "Welcome back.",
        "You're back. I'm here when you need me.",
    ),
    "system_pressure.ram": (
        "Memory usage is unusually high, {address}. Shall I look into it?",
        "We're at {percent} percent memory. Would you like me to investigate?",
    ),
    "system_pressure.ram_critical": (
        "{address}, memory is at {percent} percent. Something should probably be closed.",
        "Memory is nearly exhausted, {address}. Shall I find out what's holding it?",
        "Memory is at {percent} percent. Something should probably be closed.",
    ),
    "system_pressure.cpu": (
        "The processor has been pinned for a while, {address}. Shall I check what's running?",
        "Something is working the processor hard. Would you like me to look?",
    ),
    "battery_low": (
        "Battery is down to {percent} percent, {address}.",
        "You're at {percent} percent and unplugged.",
    ),
    "repeated_failure": (
        "That's failed twice now, {address}. Shall I try a different approach?",
        "That approach isn't working. Would you like me to try another?",
        "We've had no luck with that, {address}. I can try it differently.",
    ),
    "repeated_action": (
        "You've asked for that a few times, {address}. Shall I keep it open?",
        "We appear to be reconsidering that decision.",
    ),
    "dry_remark.late": (
        "You're still here. I had assumed sleep was part of the design.",
        "The hour has stopped being late and started being early, {address}.",
    ),
    "dry_remark.long_session": (
        "This has stopped being a session and started being a lifestyle, {address}.",
        "The screen and I are both still here. One of us doesn't need rest.",
    ),
}


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE PRESENCE ENGINE                           │
# └────────────────────────────────────────────────────────────────────────┘

class ProactivePresence:
    """
    The contextual layer of the proactive subsystem.

    It owns three things and nothing else: the derived context (session, work stretch,
    absence, sustained system pressure, repetition counters), the candidate builders, and
    the language. It owns no thread, no audio, no model client and no assistant state — the
    proactive agent supplies the tick and the safety gate, and the runtime supplies the
    truth about what the assistant is doing.

    Args:
        config: optional `PresenceConfig`.
        clock: optional `callable() -> float` epoch seconds, for deterministic tests.
        annoyance_fn: optional `callable(kind) -> float` in [0, 1] — the existing habit
            model's learned "this kind of remark is not welcome" signal. Wired by the agent
            so presence does not need its own feedback store.
        metrics_fn: optional `callable() -> dict` returning cpu/ram/battery. Defaults to the
            existing `core.system_profile.pressure_sample`, sampled at most once per
            `system_sample_s` — this module never opens a subprocess or walks a process tree.
    """

    # Bounded, and small: this is a repetition check, not a transcript.
    RECENT_UTTERANCES = 12
    MAX_ACTION_COUNTERS = 24
    # How far back the similarity check looks. Six hours: long enough that "you're still
    # working" cannot come round twice in one evening, short enough that a remark which was
    # right this morning may be right again tonight.
    REPETITION_WINDOW_S = 6 * 3600

    def __init__(self, config=None, clock=None, annoyance_fn=None, metrics_fn=None):
        self.config = config or PresenceConfig()
        self._clock = clock or time.time
        self._annoyance_fn = annoyance_fn
        self._metrics_fn = metrics_fn
        self._lock = threading.RLock()

        now = self._clock()
        self.session_started = now

        # Interaction bookkeeping. `work_started` tracks the CURRENT continuous stretch and
        # resets whenever the user is away longer than `session_gap_s`; that is what makes
        # "you have been working for two hours" true rather than "Kayra has been running for
        # two hours", which is a completely different and much less interesting claim.
        self.interactions = 0
        self.last_interaction = 0.0
        self.last_gap = 0.0
        self.work_started = None

        # Away detection from the foreground window. `getActiveWindow()` returns nothing when
        # the machine is locked or nothing is focused, so a long unbroken run of that is a
        # genuine "the user is not at the desk" signal — and it costs nothing extra, because
        # the agent already samples that window on every tick.
        self._away_since = None
        self._away_seconds = 0.0
        self._return_pending = False

        # Sustained-pressure counters. A single reading never speaks.
        self._metrics = {}
        self._metrics_at = 0.0
        self._ram_streak = 0
        self._cpu_streak = 0

        # Repetition counters for anticipation, bounded and time-windowed.
        self._failures = {}      # action -> [timestamps]
        self._actions = {}       # action -> [timestamps]

        # Cooldown ledgers.
        self._last_any = 0.0
        self._last_kind = {}
        self._last_tier = {}
        self._last_group = {}
        self._last_address_at = 0.0
        self._recent = deque(maxlen=self.RECENT_UTTERANCES)   # (normalized tokens, text, at)
        self._day_key = None
        self._day_count = 0
        self._work_milestones_fired = set()

        # Diagnostics.
        self.last_spoken = None          # (kind, text, at)
        self.last_suppression = None     # (reason, at)
        self._suppression_log_at = {}
        self.stats = {"candidates": 0, "suppressed": 0, "spoken": 0, "llm_calls": 0}

    # ──────────────────────────────────────────────────────────────────────
    #                              SWITCHES
    # ──────────────────────────────────────────────────────────────────────

    @property
    def enabled(self):
        return bool(self.config.enabled)

    def set_enabled(self, enabled):
        self.config.enabled = bool(enabled)

    def set_category(self, name, enabled):
        """Flips one category switch. Unknown names are ignored rather than raising."""
        if name in ("greetings", "context", "late_night", "work_session", "system", "humor"):
            setattr(self.config, name, bool(enabled))
            return True
        return False

    def categories(self):
        return {
            "greetings": self.config.greetings,
            "context": self.config.context,
            "late_night": self.config.late_night,
            "work_session": self.config.work_session,
            "system": self.config.system,
            "humor": self.config.humor,
        }

    def owns_kind(self, kind):
        """
        Kinds this layer has taken over from the legacy agent builders.

        `late_night` used to be a single flat sentence in `proactive_agent`; presence owns the
        wording and the context now, so the agent skips its own builder rather than producing
        a second, duller candidate for the same situation. The cooldown key is unchanged, so
        the two can never both fire.
        """
        return bool(self.config.enabled) and kind == "late_night" and self.config.late_night

    # ──────────────────────────────────────────────────────────────────────
    #                          OBSERVATION / EVENTS
    # ──────────────────────────────────────────────────────────────────────
    # Everything below is called from the runtime event bus (synchronously, on whichever
    # thread emitted) or from the agent's tick. All of it is counter arithmetic: no disk,
    # no network, no lock held across anything that can block.

    def note_interaction(self, now=None):
        """A user turn started. Maintains the session, the gap and the work stretch."""
        now = self._clock() if now is None else now
        with self._lock:
            gap = (now - self.last_interaction) if self.last_interaction else None
            self.last_gap = gap if gap is not None else 0.0
            self.interactions += 1
            self.last_interaction = now
            if self.work_started is None or (gap is not None and gap > self.config.session_gap_s):
                # A long enough silence ends the stretch, so the milestones start over. A
                # user who steps out for lunch has not been "working for five hours".
                self.work_started = now
                self._work_milestones_fired.clear()
            self._away_since = None
            self._away_seconds = 0.0
            self._return_pending = False
        return self.last_gap

    def note_focus(self, app, now=None):
        """
        The agent's foreground-window sample, forwarded.

        `app is None` means the window could not be read — locked screen, nothing focused.
        A long run of that followed by a real window is the "user came back to the desk"
        signal used by `_candidate_user_return`.
        """
        now = self._clock() if now is None else now
        with self._lock:
            if app is None:
                if self._away_since is None:
                    self._away_since = now
                self._away_seconds = now - self._away_since
                return
            if self._away_since is not None:
                away = now - self._away_since
                self._away_seconds = away
                if away >= self.config.away_s:
                    self._return_pending = True
                self._away_since = None

    def note_intents(self, tokens, now=None):
        """
        Counts repeated ACTIONS from the DMM's output — never their payload text.

        The key is the token's first two words, which is the same bounded reduction the habit
        model uses; "open chrome" asked four times in ten minutes is a pattern worth
        noticing, and what the user typed into a search box is not recorded at all.
        """
        now = self._clock() if now is None else now
        with self._lock:
            for token in tokens or []:
                key = _action_key(token)
                if not key:
                    continue
                self._push_counter(self._actions, key, now)

    def note_automation_result(self, ok, failed, tokens=None, now=None):
        """
        Outcome of an automation batch, forwarded from the automation layer.

        Only the COUNT and the action key are kept. This is how anticipation gets its
        evidence — "that approach has failed twice" has to be a fact, not a guess.
        """
        now = self._clock() if now is None else now
        if not failed:
            return
        with self._lock:
            for token in (tokens or [None]):
                key = _action_key(token) or "that"
                self._push_counter(self._failures, key, now)

    def _push_counter(self, bucket, key, now):
        stamps = bucket.setdefault(key, [])
        stamps.append(now)
        cutoff = now - self.config.repeat_window_s
        bucket[key] = [t for t in stamps if t >= cutoff]
        if len(bucket) > self.MAX_ACTION_COUNTERS:
            # Evict whatever has been quiet longest; these are counters, not history.
            oldest = min(bucket.items(), key=lambda kv: max(kv[1]) if kv[1] else 0.0)[0]
            bucket.pop(oldest, None)

    def note_barge_in(self):
        """The user took the floor. Any repetition suspicion this tick is moot."""
        with self._lock:
            self._return_pending = False

    # ──────────────────────────────────────────────────────────────────────
    #                          SYSTEM SIGNAL SAMPLING
    # ──────────────────────────────────────────────────────────────────────

    def _sample_metrics(self, now):
        """
        CPU / RAM / battery, at most once per `system_sample_s`.

        Deliberately NOT `live_metrics()`: that one also walks Kayra's process tree for the
        System screen's footprint line, and a tree walk every minute forever to answer a
        question about RAM percentage is exactly the "high-frequency process scan" this
        subsystem is not allowed to do. `pressure_sample` is the three psutil reads and
        nothing else, cached in the module that already owns system metrics.
        """
        if now - self._metrics_at < self.config.system_sample_s and self._metrics:
            return self._metrics
        sample = {}
        try:
            if self._metrics_fn is not None:
                sample = self._metrics_fn() or {}
            else:
                from kayra.core.system_profile import pressure_sample
                sample = pressure_sample() or {}
        except Exception:
            sample = {}
        self._metrics = sample
        self._metrics_at = now

        ram = sample.get("ram_percent")
        cpu = sample.get("cpu_percent")
        self._ram_streak = (self._ram_streak + 1) if (ram is not None and ram >= self.config.ram_warn) else 0
        self._cpu_streak = (self._cpu_streak + 1) if (cpu is not None and cpu >= self.config.cpu_warn) else 0
        return sample

    # ──────────────────────────────────────────────────────────────────────
    #                          SIGNAL ASSEMBLY
    # ──────────────────────────────────────────────────────────────────────

    def signals(self, now=None, focus_app=None, focus_seconds=0.0,
                seconds_since_user=None, sleeping=False, mood=None):
        """Builds the one snapshot every candidate builder in this tick reasons about."""
        now = self._clock() if now is None else now
        metrics = self._sample_metrics(now) if self.config.system else {}
        topic, conversation_mode = self._conversation()
        with self._lock:
            work_seconds = (now - self.work_started) if self.work_started else 0.0
            since_user = (seconds_since_user if seconds_since_user is not None
                          else ((now - self.last_interaction) if self.last_interaction
                                else float("inf")))
            return PresenceSignals(
                now,
                focus_app=focus_app,
                focus_seconds=focus_seconds,
                away_seconds=self._away_seconds,
                session_seconds=now - self.session_started,
                work_seconds=work_seconds,
                interactions=self.interactions,
                gap_seconds=self.last_gap,
                seconds_since_user=since_user,
                sleeping=sleeping,
                mood=mood,
                topic=topic,
                conversation_mode=conversation_mode,
                cpu_percent=metrics.get("cpu_percent"),
                ram_percent=metrics.get("ram_percent"),
                battery_percent=metrics.get("battery_percent"),
                battery_plugged=metrics.get("battery_plugged"),
                repeated_failure=self._hot_counter(self._failures, self.config.repeat_failures, now),
                repeated_action=self._hot_counter(self._actions, self.config.repeat_actions, now),
            )

    def _conversation(self):
        """
        The current topic and conversation mode, or ("", "").

        Read through the process-wide accessor rather than held as a field, so this always
        reflects the context the turn loop is actually writing to. Guarded because presence
        must keep working with the context layer absent — it is a nicety here, and the one
        place it genuinely matters (the repair stage) is not this module.
        """
        try:
            from kayra.core.conversation_context import get_conversation_context
            context = get_conversation_context()
            return context.topic_summary(4), context.mode
        except Exception:
            return "", ""

    def _hot_counter(self, bucket, minimum, now):
        cutoff = now - self.config.repeat_window_s
        best = None
        for key, stamps in bucket.items():
            live = [t for t in stamps if t >= cutoff]
            if len(live) >= minimum and (best is None or len(live) > best[1]):
                best = (key, len(live))
        return best

    # ──────────────────────────────────────────────────────────────────────
    #                          CANDIDATE GENERATION
    # ──────────────────────────────────────────────────────────────────────

    def candidates(self, signals, critical_only=False):
        """
        Every reason to speak that currently holds, scored, highest first.

        Pure: it reads the signals it was handed and the cooldown ledgers, and mutates
        nothing. That is what lets the test suite drive it at exact clock offsets, and what
        makes "how many LLM calls does an evaluation cost" answerable — zero, by inspection.
        """
        if not self.config.enabled or signals.sleeping:
            return []

        builders = (
            (Tier.CRITICAL, self._candidate_battery),
            (Tier.CRITICAL, self._candidate_system_critical),
            (Tier.IMPORTANT, self._candidate_system_pressure),
            (Tier.IMPORTANT, self._candidate_repeated_failure),
            (Tier.IMPORTANT, self._candidate_work_session),
            (Tier.SOCIAL, self._candidate_late_night),
            (Tier.SOCIAL, self._candidate_user_return),
            (Tier.AMBIENT, self._candidate_repeated_action),
            (Tier.AMBIENT, self._candidate_dry_remark),
        )

        found = []
        for tier, builder in builders:
            if critical_only and tier != Tier.CRITICAL:
                continue
            try:
                candidate = builder(signals)
            except Exception:
                candidate = None
            if candidate is None:
                continue
            candidate.score = round(_clamp(candidate.score - 0.5 * self._annoyance(candidate.kind)), 3)
            found.append(candidate)

        found.sort(key=lambda c: (_tier_rank(c.tier), c.score), reverse=True)
        self.stats["candidates"] += len(found)
        return found

    def _annoyance(self, kind):
        if self._annoyance_fn is None:
            return 0.0
        try:
            return _clamp(self._annoyance_fn(kind))
        except Exception:
            return 0.0

    # ── individual builders ───────────────────────────────────────────────

    def _candidate_battery(self, s):
        """CRITICAL: running out of power is a fact the user cannot recover from later."""
        if not self.config.system:
            return None
        if s.battery_percent is None or s.battery_plugged:
            return None
        if s.battery_percent > self.config.battery_warn:
            return None
        percent = int(round(s.battery_percent))
        evidence = _clamp((self.config.battery_warn - percent) / max(1.0, self.config.battery_warn))
        return PresenceCandidate(
            "battery_low", Tier.CRITICAL,
            self._render("battery_low", percent=percent),
            evidence, reason=f"battery_{percent}pct_unplugged",
            context={"percent": percent})

    def _candidate_system_critical(self, s):
        """CRITICAL: memory genuinely about to run out, held across several samples."""
        if not self.config.system or s.ram_percent is None:
            return None
        if s.ram_percent < self.config.ram_critical:
            return None
        if self._ram_streak < self.config.pressure_samples:
            return None
        percent = int(round(s.ram_percent))
        return PresenceCandidate(
            "system_pressure", Tier.CRITICAL,
            self._render("system_pressure.ram_critical", percent=percent),
            1.0, reason=f"ram_{percent}pct_x{self._ram_streak}",
            context={"percent": percent, "metric": "ram"})

    def _candidate_system_pressure(self, s):
        """IMPORTANT: sustained (not spiky) memory or processor pressure."""
        if not self.config.system:
            return None
        if s.ram_percent is not None and s.ram_percent >= self.config.ram_warn \
                and self._ram_streak >= self.config.pressure_samples:
            percent = int(round(s.ram_percent))
            evidence = _clamp((percent - self.config.ram_warn) /
                              max(1.0, 100.0 - self.config.ram_warn))
            return PresenceCandidate(
                "system_pressure", Tier.IMPORTANT,
                self._render("system_pressure.ram", percent=percent),
                evidence, reason=f"ram_{percent}pct_x{self._ram_streak}",
                context={"percent": percent, "metric": "ram"})
        if s.cpu_percent is not None and s.cpu_percent >= self.config.cpu_warn \
                and self._cpu_streak >= self.config.pressure_samples:
            percent = int(round(s.cpu_percent))
            return PresenceCandidate(
                "system_pressure", Tier.IMPORTANT,
                self._render("system_pressure.cpu", percent=percent),
                _clamp(self._cpu_streak / 6.0), reason=f"cpu_{percent}pct_x{self._cpu_streak}",
                context={"percent": percent, "metric": "cpu"})
        return None

    def _candidate_repeated_failure(self, s):
        """
        IMPORTANT / anticipation: the same action has failed more than once recently.

        Phrased as an offer, never as a diagnosis — presence knows that something failed and
        how often, and nothing about why. Claiming more than that is how an assistant starts
        inventing observations.
        """
        if not self.config.context or not s.repeated_failure:
            return None
        action, count = s.repeated_failure
        return PresenceCandidate(
            "repeated_failure", Tier.IMPORTANT,
            self._render("repeated_failure"),
            _clamp((count - 1) / 3.0), reason=f"failed_{count}x:{action}",
            context={"action": action, "count": count})

    def _candidate_work_session(self, s):
        """
        IMPORTANT: an unbroken stretch of interaction has passed a configured milestone.

        This is about the USER's continuous session, which is a different signal from the
        agent's `break` candidate (unbroken focus in one application). They share a cooldown
        group so a long night cannot produce both.
        """
        if not (self.config.work_session and self.config.context):
            return None
        if not s.work_seconds or s.interactions < 3:
            return None
        milestone = None
        for threshold in self.config.work_milestones:
            if s.work_seconds >= threshold and threshold not in self._work_milestones_fired:
                milestone = threshold
        if milestone is None:
            return None

        minutes = int(s.work_seconds // 60)
        hours = max(1, int(round(s.work_seconds / 3600.0)))
        first = milestone == min(self.config.work_milestones)
        key = "work_session.first" if first else "work_session.later"
        evidence = _clamp(s.work_seconds / (max(self.config.work_milestones) or 1.0))
        return PresenceCandidate(
            "work_session", Tier.IMPORTANT,
            self._render(key, minutes=minutes, hours=hours),
            evidence, reason=f"user_active_{minutes}m",
            context={"minutes": minutes, "hours": hours, "milestone": milestone,
                     "app": s.focus_app})

    def _candidate_late_night(self, s):
        """SOCIAL: the configured late-night window, and the user demonstrably still here."""
        if not (self.config.late_night and self.config.context):
            return None
        if not in_window(s.hour, self.config.late_night_start, self.config.late_night_end):
            return None
        present = s.focus_app is not None or s.seconds_since_user < 1800
        if not present:
            return None
        evidence = _clamp(0.5 + min(0.5, s.work_seconds / (4 * 3600.0)))
        return PresenceCandidate(
            "late_night", Tier.SOCIAL, self._render("late_night"),
            evidence, reason=f"late_hour_{s.hour:02d} + present",
            context={"hour": s.hour})

    def _candidate_user_return(self, s):
        """SOCIAL: the user came back to a machine that was locked or unattended."""
        if not (self.config.greetings and self.config.context):
            return None
        with self._lock:
            if not self._return_pending:
                return None
            away = self._away_seconds
            self._return_pending = False
        evidence = _clamp(away / (4 * 3600.0))
        return PresenceCandidate(
            "user_return", Tier.SOCIAL, self._render("user_return"),
            evidence, reason=f"away_{int(away // 60)}m",
            context={"away_minutes": int(away // 60)})

    def _candidate_repeated_action(self, s):
        """AMBIENT: the same request several times in a few minutes."""
        if not (self.config.context and self.config.humor) or not s.repeated_action:
            return None
        action, count = s.repeated_action
        return PresenceCandidate(
            "repeated_action", Tier.AMBIENT, self._render("repeated_action"),
            _clamp((count - 2) / 4.0), reason=f"repeated_{count}x:{action}",
            context={"action": action, "count": count})

    def _candidate_dry_remark(self, s):
        """
        AMBIENT: one observational remark, and only where the context genuinely carries it.

        There is no joke list and no random humour. The remark exists only when the situation
        is already unusual — the small hours, or a session long past the last milestone — and
        it still has to survive the ambient tier's six-hour cooldown and the daily budget.
        """
        if not (self.config.humor and self.config.context):
            return None
        very_late = in_window(s.hour, self.config.late_night_start, self.config.late_night_end)
        long_session = s.work_seconds >= (max(self.config.work_milestones) + 3600.0)
        if not (very_late and s.interactions >= 5) and not long_session:
            return None
        key = "dry_remark.late" if very_late else "dry_remark.long_session"
        return PresenceCandidate(
            "dry_remark", Tier.AMBIENT, self._render(key),
            _clamp(s.work_seconds / (6 * 3600.0)),
            reason="late_hour + long_session" if very_late else "very_long_session",
            context={"hours": round(s.work_seconds / 3600.0, 1)})

    # ──────────────────────────────────────────────────────────────────────
    #                     THE SPEAK / SILENCE DECISION
    # ──────────────────────────────────────────────────────────────────────

    def should_speak(self, candidate, now=None):
        """
        Returns `(ok, reason)`. The default answer is no.

        This is the presence-side gate: budget, spacing, cooldowns and repetition. It is NOT
        the safety gate — whether the user is mid-sentence, whether audio is draining, whether
        a turn is open and whether the runtime is shutting down are all decided by
        `ProactiveAgent.is_safe_window()`, which is the one place proactive speech has ever
        been authorised and remains so.
        """
        now = self._clock() if now is None else now
        if candidate is None:
            return False, "no candidate"
        if not self.config.enabled:
            return False, "presence disabled"

        floor = TIER_PROFILE[candidate.tier]["base"]
        if candidate.score < floor:
            return False, "low confidence"

        with self._lock:
            self._roll_day(now)
            if self.config.daily_budget and self._day_count >= self.config.daily_budget:
                return False, "daily budget spent"
            if candidate.tier != Tier.CRITICAL and (now - self._last_any) < self.config.min_gap_s:
                return False, "cooldown"
            if (now - self._last_tier.get(candidate.tier, 0.0)) < TIER_PROFILE[candidate.tier]["cooldown"]:
                return False, "tier cooldown"
            kind_cooldown = KIND_COOLDOWNS.get(candidate.kind, 0)
            if kind_cooldown and (now - self._last_kind.get(candidate.kind, 0.0)) < kind_cooldown:
                return False, "kind cooldown"
            if candidate.group:
                group_cooldown = GROUP_COOLDOWNS.get(candidate.group, 0)
                if group_cooldown and (now - self._last_group.get(candidate.group, 0.0)) < group_cooldown:
                    return False, "group cooldown"
            if self._is_repetitive(candidate.text, now):
                return False, "repetitive wording"
        return True, "ok"

    def _roll_day(self, now):
        key = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self._day_count = 0

    def _is_repetitive(self, text, now):
        """
        Token-overlap similarity against what was said recently.

        Cheap on purpose. The failure being prevented is "you're still working, sir" once an
        hour forever, and that is caught by word overlap — an embedding model would cost a
        model load and a vector store to answer a question that Jaccard answers in
        microseconds.
        """
        tokens = _tokens(text)
        if not tokens:
            return False
        for prior_tokens, _prior_text, at in self._recent:
            # The window is deliberately NOT derived from `min_gap_s`. It was, and the
            # consequence was that lowering the spacing to zero — which is a legitimate
            # configuration, and the one a test reaches for — turned repetition detection
            # off completely rather than making it stricter. The backstop against saying
            # the same thing twice must not be a function of how often speech is allowed.
            if now - at > self.REPETITION_WINDOW_S:
                continue
            if _jaccard(tokens, prior_tokens) >= self.config.similarity_threshold:
                return True
        return False

    def note_spoken(self, candidate, text, now=None):
        """Records that a candidate was actually spoken. Called by the agent, never here."""
        now = self._clock() if now is None else now
        with self._lock:
            self._roll_day(now)
            self._day_count += 1
            self._last_any = now
            self._last_kind[candidate.kind] = now
            self._last_tier[candidate.tier] = now
            if candidate.group:
                self._last_group[candidate.group] = now
            if candidate.kind == "work_session":
                milestone = candidate.context.get("milestone")
                if milestone is not None:
                    self._work_milestones_fired.add(milestone)
            self._recent.append((_tokens(text), text, now))
            if self.config.address in (text or "").lower():
                self._last_address_at = now
            self.last_spoken = (candidate.kind, text, now)
            self.stats["spoken"] += 1

    def note_suppressed(self, reason, now=None):
        """
        Records a suppression, and says whether it is worth a log line.

        Returns True at most once per reason per minute. The whole point of logging
        suppressions is to explain a quiet assistant; a line every three seconds while a
        candidate waits for a safe window would bury the one line that mattered.
        """
        now = self._clock() if now is None else now
        self.stats["suppressed"] += 1
        self.last_suppression = (reason, now)
        last = self._suppression_log_at.get(reason, 0.0)
        if now - last < 60.0:
            return False
        self._suppression_log_at[reason] = now
        if len(self._suppression_log_at) > 32:
            self._suppression_log_at.clear()
        return True

    # ──────────────────────────────────────────────────────────────────────
    #                              LANGUAGE
    # ──────────────────────────────────────────────────────────────────────

    def _render(self, key, **fields):
        """
        Picks a wording and fills it in.

        The address rule lives here: when the form of address was used within
        `address_gap_s`, wordings that carry `{address}` are dropped from the pool, so
        "sir" appears where a person would use it and not in every sentence.
        """
        options = PHRASES.get(key) or ()
        if not options:
            return ""
        now = self._clock()
        recent_address = (now - self._last_address_at) < self.config.address_gap_s
        pool = [o for o in options if "{address}" not in o] if recent_address else list(options)
        if not pool:
            pool = list(options)
        template = random.choice(pool)
        fields.setdefault("address", self.config.address)
        fields.setdefault("name", os.environ.get("ASSISTANT_NAME", "Kayra").strip() or "Kayra")
        try:
            text = template.format(**fields)
        except (KeyError, IndexError):
            text = template
        return _sentence_case(text)

    def greeting(self, text=None, now=None, sleeping=False):
        """
        The contextual reply to a bare greeting — the reactive half of presence.

        Chosen from the clock, the session, and how long the user has actually been away,
        with NO model call: a greeting that costs a cloud round-trip is a greeting that
        arrives after the moment for it has passed, and the variation people notice comes
        from the context being right, not from the sentence being generated.

        Returns None when greetings are switched off or the utterance is not a greeting, in
        which case the caller routes it to the chatbot exactly as before.
        """
        if not (self.config.enabled and self.config.greetings):
            return None
        if text is not None and not is_greeting(text):
            return None
        now = self._clock() if now is None else now
        stamp = datetime.datetime.fromtimestamp(now)
        part = day_part(stamp.hour)

        with self._lock:
            first_of_session = self.interactions <= 1
            gap = self.last_gap
            work = (now - self.work_started) if self.work_started else 0.0

        late = in_window(stamp.hour, self.config.late_night_start, self.config.late_night_end) \
            or part == "night"

        if gap and gap >= self.config.long_absence_s:
            key = "greeting.long_return"
        elif gap and gap >= self.config.short_absence_s * 6:
            key = "greeting.return"
        elif gap and gap >= self.config.short_absence_s:
            key = "greeting.short_return"
        elif late and work >= min(self.config.work_milestones):
            key = "greeting.late_working"
        elif first_of_session:
            key = "greeting.first_of_session"
        else:
            key = f"greeting.{part}"

        text_out = self._render(key, part=salutation(stamp.hour))
        if text_out:
            with self._lock:
                if self.config.address in text_out.lower():
                    self._last_address_at = now
                self._recent.append((_tokens(text_out), text_out, now))
        return text_out or None

    def boot_line(self, now=None):
        """
        The single spoken line at startup, made contextual.

        Same budget as before — one short sentence — so this changes what is said and not how
        long the cold start takes.
        """
        now = self._clock() if now is None else now
        if not self.config.enabled:
            return None
        stamp = datetime.datetime.fromtimestamp(now)
        part = day_part(stamp.hour)
        if not self.config.greetings:
            return self._render("boot.default")
        if part == "morning":
            key = "boot.morning"
        elif in_window(stamp.hour, self.config.late_night_start, self.config.late_night_end) \
                or part == "night":
            key = "boot.night"
        else:
            key = "boot.default"
        return self._render(key)

    # ──────────────────────────────────────────────────────────────────────
    #                         LLM REALIZATION CONTRACT
    # ──────────────────────────────────────────────────────────────────────

    def llm_prompt(self, candidate, signals=None):
        """
        Builds the natural-language-realization prompt for an ALREADY-APPROVED candidate.

        Returns None when the LLM must not be used — which is the common case. The model
        never decides whether to speak; by the time this is called that decision is made,
        the cooldowns are cleared and the sentence to fall back to already exists.
        """
        if not (self.config.llm_phrasing and candidate is not None):
            return None
        if not candidate.llm_worthy:
            return None
        stamp = datetime.datetime.fromtimestamp(self._clock())
        recent = "; ".join(text for _t, text, _a in list(self._recent)[-3:]) or "none"
        lines = [
            "Rewrite one short spoken remark for a composed, observant personal assistant.",
            "",
            f"current_time: {stamp.strftime('%H:%M')} ({day_part(stamp.hour)})",
            f"event_type: {candidate.kind}",
            f"priority: {candidate.tier}",
            f"form_of_address: {self.config.address}",
            f"observed: {candidate.reason}",
            f"draft: {candidate.text}",
            f"recently_said: {recent}",
        ]
        if signals is not None:
            lines.append(f"user_activity: {int(signals.work_seconds // 60)} minutes of "
                         f"continuous session, {signals.interactions} interactions")
            if signals.topic:
                # Content words the user has actually used, never a summary Kayra invented.
                lines.append(f"recent_topic: {signals.topic}")
        lines += [
            "",
            "Rules: one or two short sentences. Natural spoken English. No markdown, no "
            "emoji, no bullet points, no headings, no filler, no explanation of how you "
            "know. Do not invent any observation that is not listed above. Do not mention "
            "being an AI. Use the form of address at most once, or not at all. Reply with "
            "the sentence only.",
        ]
        self.stats["llm_calls"] += 1
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────
    #                             DIAGNOSTICS
    # ──────────────────────────────────────────────────────────────────────

    def describe(self):
        """Cheap, allocation-light status for the UI and the console. No I/O."""
        now = self._clock()
        with self._lock:
            next_eligible = max(0.0, self.config.min_gap_s - (now - self._last_any)) \
                if self._last_any else 0.0
            return {
                "enabled": self.config.enabled,
                "categories": self.categories(),
                "interactions": self.interactions,
                "work_minutes": int(((now - self.work_started) // 60) if self.work_started else 0),
                "last_kind": self.last_spoken[0] if self.last_spoken else None,
                "last_text": self.last_spoken[1] if self.last_spoken else None,
                "last_suppression": self.last_suppression[0] if self.last_suppression else None,
                "next_eligible_seconds": int(next_eligible),
                "spoken_today": self._day_count,
                "daily_budget": self.config.daily_budget,
                "stats": dict(self.stats),
            }


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          SMALL PURE HELPERS                            │
# └────────────────────────────────────────────────────────────────────────┘

def _clamp(value, low=0.0, high=1.0):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


_TIER_ORDER = {Tier.AMBIENT: 0, Tier.SOCIAL: 1, Tier.IMPORTANT: 2, Tier.CRITICAL: 3}


def _tier_rank(tier):
    return _TIER_ORDER.get(tier, 0)


def in_window(hour, start, end):
    """Hour-of-day window that may wrap past midnight (1 -> 5, or 22 -> 6)."""
    if start == end:
        return False
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


_WORD = re.compile(r"[a-z0-9']+")
# Words that carry no identity — two sentences that share only these are not the same remark.
_STOPWORDS = frozenset("""a an and are as at be been but by do does for from had has have
he her him his i if in is it its me my no not of on or our out she should so than that the
their them then there these they this to too up us was we were what when which who will
with would you your""".split())


def _tokens(text):
    if not text:
        return frozenset()
    words = _WORD.findall(str(text).lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _sentence_case(text):
    text = (text or "").strip()
    return (text[0].upper() + text[1:]) if text else text


_ACTION_STOP_HEADERS = ("general", "realtime", "deep research", "content", "write", "exit")


def _action_key(token):
    """
    A DMM token reduced to a bounded, non-identifying action key.

    Mirrors the habit model's rule so the two agree on what "the same action" means, and
    discards conversational tokens entirely — repetition counting is about what the user is
    trying to DO, and counting questions would make it a transcript.
    """
    if not token or not isinstance(token, str):
        return None
    lowered = token.strip().lower()
    if not lowered or any(lowered.startswith(h) for h in _ACTION_STOP_HEADERS):
        return None
    return " ".join(lowered.split()[:2])


_GREETING_FILLERS = frozenset({"hey", "hi", "hello", "yo", "hiya", "howdy", "greetings",
                               "there", "again", "please", "so", "well", "okay", "ok"})
_GREETING_HEADS = ("hi", "hello", "hey", "yo", "hiya", "howdy", "greetings",
                   "good morning", "good afternoon", "good evening", "morning",
                   "afternoon", "evening", "good day")


def is_greeting(text, assistant_name=None):
    """
    True when the utterance is a bare greeting and nothing else.

    Deliberately strict, and for the same reason the local control vocabulary is: "hello"
    is a greeting, and "hello, open Chrome" is an instruction. Matching a PREFIX would
    swallow the second one, so the whole utterance has to reduce to a greeting once the
    assistant's name and conversational filler are removed.
    """
    if not text or not isinstance(text, str):
        return False
    name = (assistant_name or os.environ.get("ASSISTANT_NAME") or "Kayra").strip().lower()
    cleaned = " ".join(_WORD.findall(text.lower()))
    if not cleaned:
        return False
    if name:
        cleaned = " ".join(w for w in cleaned.split() if w != name)
    if not cleaned:
        return False
    for head in sorted(_GREETING_HEADS, key=len, reverse=True):
        if cleaned == head or cleaned.startswith(head + " "):
            remainder = cleaned[len(head):].strip()
            if all(word in _GREETING_FILLERS for word in remainder.split()):
                return True
    return False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     DIAGNOSTIC TEST RUNTIME BLOCK                      │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    from kayra.core.config import load_environment
    load_environment()

    presence = ProactivePresence()
    print("boot:", presence.boot_line())
    for probe in ("hi kayra", "hello there", "good evening", "hey kayra open chrome"):
        print(f"  {probe!r} -> greeting={is_greeting(probe)} :: {presence.greeting(probe)}")
    sig = presence.signals(focus_app="Visual Studio Code", focus_seconds=3600)
    print("signals:", sig.as_dict())
    for candidate in presence.candidates(sig):
        print("candidate:", candidate, candidate.text, "|", presence.should_speak(candidate))
    print("describe:", presence.describe())
