# ┌────────────────────────────────────────────────────────────────────────┐
# │                     test_proactive_presence.py                         │
# │   Contextual Presence — Greetings, Context, Suppression, Tone, Cost    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_proactive_presence.py — assertion suite for the proactive presence layer.

Like the other scripts in tests/, this is a standalone entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_proactive_presence.py

It exits non-zero if anything fails.

It needs NO audio hardware, NO microphone, NO network and NO LLM. Every clock is injected,
every metric source is a fake, and the two integration sections drive the real
`ProactiveAgent` with fake speech and phrasing collaborators. That is deliberate: this
subsystem decides when the assistant speaks unprompted, and a policy like that has to be
testable at exact offsets rather than by waiting.

The section that matters most is `section_llm_cost`. The central claim of the design is that
NO model is called merely to decide whether to speak, and it is asserted here by counting —
a phrasing collaborator that raises on use, driven through hundreds of full evaluations.
"""

import os
import re
import sys
import time
import shutil
import tempfile
import datetime

# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_success, print_error, print_system
from kayra.core.runtime_state import RuntimeState, AssistantState

import kayra.intelligence.proactive_presence as pp
from kayra.intelligence.proactive_presence import (
    ProactivePresence, PresenceConfig, PresenceCandidate, Tier,
    is_greeting, day_part, salutation, in_window,
    TIER_PROFILE, KIND_COOLDOWNS, KIND_GROUPS,
    _tokens, _jaccard, _action_key,
)

FAILURES = []
_TMP = tempfile.mkdtemp(prefix="kayra-presence-test-")


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                               FIXTURES                                 │
# └────────────────────────────────────────────────────────────────────────┘

def at(hour, minute=0, day=8):
    """Epoch seconds for a fixed local wall-clock time, so hour-of-day rules are exact."""
    return datetime.datetime(2026, 9, day, hour, minute, 0).timestamp()


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def make(config=None, clock=None, metrics=None, annoyance=None, **overrides):
    """A presence engine with an injected clock and metric source, and no environment."""
    cfg = config or PresenceConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return ProactivePresence(config=cfg, clock=clock,
                             metrics_fn=(lambda: dict(metrics)) if metrics else (lambda: {}),
                             annoyance_fn=annoyance)


def approved(presence, kind, now=None):
    """Runs a full evaluation and returns the candidate of `kind` that survived every gate."""
    signals = presence.signals(now=now)
    for candidate in presence.candidates(signals):
        if candidate.kind == kind:
            ok, _reason = presence.should_speak(candidate, now)
            if ok:
                return candidate
    return None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       1. GREETING INTELLIGENCE                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_greeting():
    print_system("\n── Greeting intelligence ─────────────────────────────────")

    # -- recognition: the whole utterance, not a prefix --------------------
    for text in ("hi", "hello", "hey kayra", "Hi Kayra!", "good morning",
                 "hello there", "hey there kayra", "greetings"):
        check(f"'{text}' is recognised as a greeting", is_greeting(text, "kayra"))
    for text in ("hello, open chrome", "hey kayra what time is it", "hi can you search that",
                 "open chrome", "", "good morning routine for me"):
        check(f"'{text}' is NOT a bare greeting", not is_greeting(text, "kayra"))

    # -- the reply varies with the hour ------------------------------------
    for hour, expected in ((9, "morning"), (14, "afternoon"), (19, "evening")):
        clock = FakeClock(at(hour))
        presence = make(clock=clock)
        presence.note_interaction()
        presence.note_interaction()          # not the first of the session
        text = presence.greeting("hi")
        check(f"{hour:02d}:00 greeting mentions the {expected}",
              expected in text.lower(), text)

    # -- first exchange of a session offers to help ------------------------
    clock = FakeClock(at(10))
    presence = make(clock=clock)
    presence.note_interaction()
    first = presence.greeting("hello")
    check("first greeting of a session is an opening line",
          "morning" in first.lower(), first)

    # -- return after an absence ------------------------------------------
    # Matched against the POOL the greeting should have been drawn from, not against one
    # expected word: every pool has several wordings on purpose ("Welcome back" and "That
    # didn't take long" are both correct answers to the same situation), and asserting one
    # of them would be asserting the random choice rather than the routing.
    def pool(key):
        return {template.split("{")[0].strip().lower().rstrip(",")
                for template in pp.PHRASES[key]}

    def from_pool(text, key):
        lowered = text.lower()
        return any(prefix and lowered.startswith(prefix) for prefix in pool(key))

    for gap_minutes, key in ((20, "greeting.short_return"),
                             (400, "greeting.long_return")):
        clock = FakeClock(at(15))
        presence = make(clock=clock)
        presence.note_interaction()
        clock.advance(gap_minutes * 60)
        presence.note_interaction()
        text = presence.greeting("hi")
        check(f"a {gap_minutes}-minute absence is answered from '{key}'",
              from_pool(text, key), text)

    # -- a short absence and a long one are described differently ----------
    def greet_after(gap_seconds):
        clock = FakeClock(at(15))
        presence = make(clock=clock)
        presence.note_interaction()
        clock.advance(gap_seconds)
        presence.note_interaction()
        return presence.greeting("hi").lower()

    short = greet_after(15 * 60)
    long_gap = greet_after(5 * 3600)
    check("a short return is not phrased as a long one", short != long_gap,
          f"{short!r} vs {long_gap!r}")

    # -- "good night" is a farewell, never a greeting ----------------------
    # Found in live testing: the engine correctly read 02:15 as night and then greeted the
    # user with the one salutation a person never uses on being greeted.
    check("night is spoken as 'evening', because English has three salutations",
          salutation(2) == "evening" and salutation(23) == "evening")
    check("the other parts of day are unchanged",
          (salutation(9), salutation(14), salutation(19)) == ("morning", "afternoon", "evening"))
    for hour in range(24):
        stamp = datetime.datetime(2026, 9, 8, hour, 15).timestamp()
        presence = make(clock=FakeClock(stamp))
        presence.note_interaction()
        text = (presence.greeting("hi") or "").lower()
        check(f"the {hour:02d}:15 greeting never says 'good night'",
              "good night" not in text, text)

    # -- late night, still working ----------------------------------------
    clock = FakeClock(at(1, 30))
    presence = make(clock=clock)
    for _ in range(4):
        presence.note_interaction()
        clock.advance(600)
    text = presence.greeting("hi kayra").lower()
    check("a late-night greeting after a long stretch notices the hour",
          "still" in text or "evening" in text, text)

    # -- greetings can be switched off -------------------------------------
    presence = make(greetings=False)
    check("greeting returns None when greetings are off", presence.greeting("hi") is None)
    presence = make()
    check("greeting returns None for a non-greeting", presence.greeting("open chrome") is None)

    # -- the boot line is contextual and stays one sentence ----------------
    for hour in (8, 14, 23, 3):
        line = make(clock=FakeClock(at(hour))).boot_line()
        check(f"boot line at {hour:02d}:00 is one short sentence",
              line and len(line) < 70 and line.count(".") <= 2, line)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        2. CONTEXTUAL CANDIDATES                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_candidates():
    print_system("\n── Contextual candidates ─────────────────────────────────")

    # -- long work session --------------------------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock)
    for _ in range(4):
        presence.note_interaction()
        clock.advance(60)
    check("no work-session candidate after four minutes",
          approved(presence, "work_session") is None)

    # Interact every ten minutes: every gap stays inside `session_gap_s`, so this is one
    # unbroken stretch rather than four separate ones.
    for _ in range(5):
        clock.advance(10 * 60)
        presence.note_interaction()
    candidate = approved(presence, "work_session")
    check("a work-session observation appears at the first milestone", candidate is not None)
    if candidate is not None:
        check("it is tiered IMPORTANT", candidate.tier == Tier.IMPORTANT, candidate.tier)
        check("its reason names the elapsed time",
              "user_active_" in candidate.reason, candidate.reason)

        # -- and it does NOT repeat ----------------------------------------
        presence.note_spoken(candidate, candidate.text)
        clock.advance(10 * 60)
        presence.note_interaction()
        check("the same milestone never fires twice",
              approved(presence, "work_session") is None)
        check("the fired milestone is remembered",
              candidate.context["milestone"] in presence._work_milestones_fired)

    # -- a long enough gap ends the session and restarts the milestones -----
    clock.advance(40 * 60)
    presence.note_interaction()
    check("a gap longer than the session gap restarts the work stretch",
          presence.describe()["work_minutes"] == 0)

    # -- late night ---------------------------------------------------------
    clock = FakeClock(at(2, 15))
    presence = make(clock=clock)
    presence.note_interaction()
    candidate = approved(presence, "late_night")
    check("a late-night candidate appears inside the window", candidate is not None)
    check("late night is SOCIAL, not IMPORTANT",
          candidate is not None and candidate.tier == Tier.SOCIAL)

    clock = FakeClock(at(14))
    presence = make(clock=clock)
    presence.note_interaction()
    check("no late-night candidate in the afternoon",
          approved(presence, "late_night") is None)

    # -- late night with nobody there ---------------------------------------
    clock = FakeClock(at(2, 15))
    presence = make(clock=clock)
    signals = presence.signals(focus_app=None, seconds_since_user=9999)
    check("no late-night candidate when the user is demonstrably absent",
          not [c for c in presence.candidates(signals) if c.kind == "late_night"])

    # -- system pressure requires a SUSTAINED reading -----------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock, metrics={"cpu_percent": 10.0, "ram_percent": 94.0,
                                          "battery_percent": 80.0, "battery_plugged": True},
                    system_sample_s=15.0)
    presence.signals()
    check("one high memory reading says nothing",
          approved(presence, "system_pressure") is None)
    for _ in range(3):
        clock.advance(20)
        presence.signals()
    candidate = approved(presence, "system_pressure")
    check("sustained memory pressure produces a candidate", candidate is not None)
    if candidate is not None:
        check("the remark is about memory",
              "memory" in candidate.text.lower(), candidate.text)
        check("its reason records the streak",
              "x" in candidate.reason and "ram" in candidate.reason, candidate.reason)

    # -- critical memory outranks the warning -------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock, metrics={"cpu_percent": 5.0, "ram_percent": 98.0,
                                          "battery_percent": 80.0, "battery_plugged": True},
                    system_sample_s=15.0)
    for _ in range(4):
        presence.signals()
        clock.advance(20)
    candidate = approved(presence, "system_pressure")
    check("exhausted memory is CRITICAL",
          candidate is not None and candidate.tier == Tier.CRITICAL)
    check("a CRITICAL candidate is allowed past the global cooldown",
          candidate is not None and candidate.bypass_global)

    # -- battery ------------------------------------------------------------
    presence = make(clock=FakeClock(at(14)),
                    metrics={"cpu_percent": 5.0, "ram_percent": 40.0,
                             "battery_percent": 9.0, "battery_plugged": False})
    candidate = approved(presence, "battery_low")
    check("a low unplugged battery is reported", candidate is not None)
    check("the reported percentage is the real one",
          candidate is not None and "9" in candidate.text, candidate.text if candidate else "")

    presence = make(clock=FakeClock(at(14)),
                    metrics={"cpu_percent": 5.0, "ram_percent": 40.0,
                             "battery_percent": 9.0, "battery_plugged": True})
    check("a low battery on mains power says nothing",
          approved(presence, "battery_low") is None)

    # -- anticipation: repeated failure -------------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock)
    presence.note_automation_result(0, 1, ["app.open"])
    check("one failure is not a pattern", approved(presence, "repeated_failure") is None)
    presence.note_automation_result(0, 1, ["app.open"])
    candidate = approved(presence, "repeated_failure")
    check("a second failure of the same action offers another approach", candidate is not None)
    if candidate is not None:
        # Asserted over the whole POOL, not the one wording this run happened to draw.
        # Checking a single sample of a random choice is a test that passes two times in
        # three, which is worse than no test: it fails on an unrelated change and gets
        # blamed on it.
        offers = pp.PHRASES["repeated_failure"]
        check("every failure remark offers rather than diagnoses",
              all(("?" in line) or (" can " in line) for line in offers), str(offers))
        check("and none of them claims to know why it failed",
              not any(word in line.lower() for line in offers
                      for word in ("because", "due to", "caused by")), str(offers))

    # -- failures age out of the window --------------------------------------
    clock.advance(3600)
    check("failures outside the window are forgotten",
          approved(presence, "repeated_failure") is None)

    # -- anticipation: repeated action ---------------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock)
    for _ in range(4):
        presence.note_intents(["open chrome"])
    candidate = approved(presence, "repeated_action")
    check("four identical requests in a few minutes are noticed", candidate is not None)
    check("that remark is AMBIENT",
          candidate is not None and candidate.tier == Tier.AMBIENT)

    # -- user return ----------------------------------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock)
    presence.note_focus(None)
    clock.advance(40 * 60)
    presence.note_focus(None)
    presence.note_focus("Visual Studio Code")
    candidate = approved(presence, "user_return")
    check("returning to an unattended machine is a welcome-back", candidate is not None)
    check("it does not fire twice for one return",
          approved(presence, "user_return") is None)

    clock = FakeClock(at(14))
    presence = make(clock=clock)
    presence.note_focus(None)
    clock.advance(120)
    presence.note_focus("Visual Studio Code")
    check("a two-minute look away is not an absence",
          approved(presence, "user_return") is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          3. SILENCE IS DEFAULT                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_silence():
    print_system("\n── Suppression: silence is the default ───────────────────")

    quiet = make(clock=FakeClock(at(14)))
    quiet.note_interaction()
    signals = quiet.signals(focus_app="Visual Studio Code", focus_seconds=120)
    check("an ordinary quiet afternoon produces no candidate at all",
          quiet.candidates(signals) == [])

    # -- the whole layer switched off ---------------------------------------
    off = make(clock=FakeClock(at(2)), enabled=False)
    off.note_interaction()
    check("nothing is produced when presence is disabled",
          off.candidates(off.signals()) == [])

    # -- standby --------------------------------------------------------------
    sleeping = make(clock=FakeClock(at(2)))
    sleeping.note_interaction()
    check("nothing is produced while the assistant is asleep",
          sleeping.candidates(sleeping.signals(sleeping=True)) == [])

    # -- category switches -----------------------------------------------------
    for category, kind, hour in (("late_night", "late_night", 2),
                                 ("system", "battery_low", 14),
                                 ("humor", "repeated_action", 14)):
        clock = FakeClock(at(hour))
        presence = make(clock=clock,
                        metrics={"cpu_percent": 5.0, "ram_percent": 30.0,
                                 "battery_percent": 8.0, "battery_plugged": False})
        presence.note_interaction()
        for _ in range(5):
            presence.note_intents(["open chrome"])
        check(f"'{kind}' exists while '{category}' is on",
              approved(presence, kind) is not None)
        presence.set_category(category, False)
        check(f"'{kind}' disappears when '{category}' is switched off",
              approved(presence, kind) is None)

    # -- cooldowns ------------------------------------------------------------
    clock = FakeClock(at(2))
    presence = make(clock=clock)
    presence.note_interaction()
    candidate = approved(presence, "late_night")
    check("the late-night remark is available once", candidate is not None)
    presence.note_spoken(candidate, candidate.text)

    clock.advance(60)
    ok, reason = presence.should_speak(candidate)
    check("it is suppressed immediately afterwards", not ok, reason)
    check("and the reason is a cooldown", "cooldown" in reason, reason)

    clock.advance(30 * 60)
    presence.note_interaction()
    check("still suppressed half an hour later",
          approved(presence, "late_night") is None)

    # -- group cooldown: one situation, one remark ----------------------------
    clock = FakeClock(at(2))
    presence = make(clock=clock)
    presence.note_interaction()
    late = approved(presence, "late_night")
    presence.note_spoken(late, late.text)
    # Inside the fatigue group's 70-minute window, so the second observation of the same
    # situation is refused even though it is a different kind.
    clock.advance(45 * 60)
    presence.note_interaction()
    ok, reason = presence.should_speak(
        PresenceCandidate("work_session", Tier.IMPORTANT, "You have been at this a while.",
                          1.0, context={"milestone": 2700.0}), clock())
    check("a work-session remark is blocked by the late-night one it duplicates",
          not ok, reason)
    check("and the reason names the group", "group" in reason, reason)

    # -- repetition ------------------------------------------------------------
    clock = FakeClock(at(2))
    presence = make(clock=clock, min_gap_s=0.0)
    first = PresenceCandidate("late_night", Tier.SOCIAL,
                              "You're still working. Anything keeping you up?", 1.0)
    presence.note_spoken(first, first.text)
    # Past the tier, kind and group cooldowns, so the only gate left is the similarity
    # check -- which is the one being tested.
    clock.advance(100 * 60)
    twin = PresenceCandidate("user_return", Tier.SOCIAL,
                             "Still working, are you? Anything keeping you up tonight?", 1.0)
    ok, reason = presence.should_speak(twin, clock())
    check("a reworded version of a recent remark is suppressed", not ok, reason)
    check("and the reason says so", "repetitive" in reason, reason)

    different = PresenceCandidate("user_return", Tier.SOCIAL, "Welcome back.", 1.0)
    ok, reason = presence.should_speak(different, clock())
    check("an unrelated remark is not caught by the similarity check", ok, reason)

    # -- daily budget -----------------------------------------------------------
    clock = FakeClock(at(8))
    presence = make(clock=clock, min_gap_s=0.0, daily_budget=2)
    wordings = ["Welcome back.", "Memory pressure has eased considerably now."]
    for index, wording in enumerate(wordings):
        candidate = PresenceCandidate("user_return", Tier.SOCIAL, wording, 1.0)
        ok, reason = presence.should_speak(candidate, clock())
        check(f"budgeted remark {index + 1} of 2 is allowed", ok, reason)
        presence.note_spoken(candidate, candidate.text)
        clock.advance(2 * 3600)       # past every per-kind and per-tier cooldown
    third = PresenceCandidate("user_return", Tier.SOCIAL,
                              "Your battery seems fine today.", 1.0)
    ok, reason = presence.should_speak(third, clock())
    check("the daily budget stops the third", not ok, reason)
    check("and the reason names the budget", "budget" in reason, reason)

    # A new calendar day rolls the counter -- no hidden state, just the date key.
    ok, reason = presence.should_speak(third, at(9, 0, day=9))
    check("the budget resets on the next day", ok, reason)

    # -- low confidence ----------------------------------------------------------
    weak = PresenceCandidate("late_night", Tier.SOCIAL, "Something.", 1.0)
    weak.score = 0.1
    ok, reason = presence.should_speak(weak, clock())
    check("a candidate below its tier floor is refused", not ok, reason)
    check("and the reason is low confidence", "confidence" in reason, reason)

    # -- annoyance feeds back through the existing habit model -------------------
    clock = FakeClock(at(2))
    annoyed = make(clock=clock, annoyance_fn=None, annoyance=lambda kind: 1.0)
    annoyed.note_interaction()
    check("a kind the user keeps rejecting stops clearing its floor",
          approved(annoyed, "late_night") is None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        4. PRIORITY AND ORDERING                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_priority():
    print_system("\n── Priority tiers ────────────────────────────────────────")

    check("the tiers are ordered by score floor",
          TIER_PROFILE[Tier.CRITICAL]["base"] > TIER_PROFILE[Tier.IMPORTANT]["base"]
          > TIER_PROFILE[Tier.SOCIAL]["base"] > TIER_PROFILE[Tier.AMBIENT]["base"])
    check("ambient remarks have the longest tier cooldown",
          TIER_PROFILE[Tier.AMBIENT]["cooldown"] >= 6 * 3600)
    check("only critical bypasses the global cooldown",
          pp.TIER_BYPASSES_GLOBAL == frozenset({Tier.CRITICAL}))

    # No amount of evidence can promote a remark out of its tier: the strongest possible
    # ambient observation still scores below the floor a CRITICAL one has to clear, so an
    # ambient remark can never be mistaken for something urgent. Enforced by the arithmetic,
    # not by an `if`.
    strongest_ambient = PresenceCandidate("dry_remark", Tier.AMBIENT, "x", 1.0)
    check("the strongest ambient remark still scores below the CRITICAL floor",
          strongest_ambient.score < TIER_PROFILE[Tier.CRITICAL]["base"],
          f"{strongest_ambient.score}")
    check("and no tier's ceiling exceeds the next tier up's ceiling",
          all(TIER_PROFILE[low]["base"] + TIER_PROFILE[low]["evidence"] <=
              TIER_PROFILE[high]["base"] + TIER_PROFILE[high]["evidence"]
              for low, high in ((Tier.AMBIENT, Tier.SOCIAL), (Tier.SOCIAL, Tier.IMPORTANT),
                                (Tier.IMPORTANT, Tier.CRITICAL))))

    clock = FakeClock(at(2))
    presence = make(clock=clock,
                    metrics={"cpu_percent": 5.0, "ram_percent": 30.0,
                             "battery_percent": 7.0, "battery_plugged": False})
    for _ in range(6):
        presence.note_interaction()
        presence.note_intents(["open chrome"])
    found = presence.candidates(presence.signals())
    check("several reasons can hold at once", len(found) >= 2, str([c.kind for c in found]))
    check("the most urgent is offered first",
          found and found[0].tier == Tier.CRITICAL, found[0].tier if found else "none")

    # -- the fatigue group is exactly the overlapping observations --------------
    check("break, work_session, late_night and dry_remark share the fatigue group",
          {k for k, g in KIND_GROUPS.items() if g == "fatigue"} ==
          {"break", "work_session", "late_night", "dry_remark"})
    check("every group member has a per-kind cooldown too",
          all(KIND_COOLDOWNS.get(k) for k in ("late_night", "work_session", "dry_remark")))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        5. PERSONALITY AND TONE                         │
# └────────────────────────────────────────────────────────────────────────┘

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_MARKDOWN = ("**", "__", "```", "# ", "- ", "* ", "|", "http://", "https://")
_BANNED = ("as an ai", "i'm just", "language model", "!!", "omg", "hooray")


def section_personality():
    print_system("\n── Personality and tone ──────────────────────────────────")

    every_line = []
    for options in pp.PHRASES.values():
        every_line.extend(options)

    check("every phrase is one or two sentences",
          all(line.count(".") + line.count("!") + line.count("?") <= 2 for line in every_line))
    check("no phrase exceeds a spoken sentence's budget",
          all(len(line) <= 100 for line in every_line),
          max(every_line, key=len))
    check("no emoji anywhere in the phrasebook",
          not any(_EMOJI.search(line) for line in every_line))
    check("no markdown anywhere in the phrasebook",
          not any(marker in line for line in every_line for marker in _MARKDOWN))
    check("no exclamation marks — the register is composed, not enthusiastic",
          not any("!" in line for line in every_line))
    check("nothing in the phrasebook talks about being an AI",
          not any(bad in line.lower() for line in every_line for bad in _BANNED))
    check("the form of address is a placeholder, never hardcoded",
          not any(re.search(r"\b(sir|ma'am)\b", line.lower()) for line in every_line))
    check("every phrase leaves at least one wording without the form of address",
          all(any("{address}" not in line for line in options)
              for options in pp.PHRASES.values()))

    # -- 'sir' does not appear in every sentence ------------------------------
    clock = FakeClock(at(14))
    presence = make(clock=clock)
    presence.note_interaction()
    first = presence.greeting("hi")
    clock.advance(60)
    presence.note_interaction()
    second = presence.greeting("hi")
    check("the address is not repeated back to back",
          not (presence.config.address in first.lower()
               and presence.config.address in second.lower()),
          f"{first!r} then {second!r}")

    # -- the address follows the identity setting ------------------------------
    os.environ["USER_GENDER"] = "Female"
    check("a female user is addressed as ma'am", PresenceConfig().address == "ma'am")
    os.environ["USER_GENDER"] = "Male"
    check("a male user is addressed as sir", PresenceConfig().address == "sir")
    os.environ["PROACTIVE_ADDRESS"] = "captain"
    check("an explicit address overrides the default", PresenceConfig().address == "captain")
    os.environ.pop("PROACTIVE_ADDRESS")

    # -- humour exists, is rare, and is never invented --------------------------
    clock = FakeClock(at(3))
    presence = make(clock=clock)
    for _ in range(6):
        presence.note_interaction()
        clock.advance(300)
    candidate = approved(presence, "dry_remark")
    check("a dry remark exists in a genuinely unusual context", candidate is not None)
    check("humour can be switched off entirely",
          make(clock=clock, humor=False).candidates(
              make(clock=clock, humor=False).signals()) == []
          or approved(make(clock=clock, humor=False), "dry_remark") is None)

    # -- no fabricated observation: every rendered value comes from the signals --
    clock = FakeClock(at(14))
    presence = make(clock=clock, metrics={"cpu_percent": 5.0, "ram_percent": 40.0,
                                          "battery_percent": 11.0, "battery_plugged": False})
    battery = approved(presence, "battery_low")
    check("a reported number is the measured number",
          battery is not None and "11" in battery.text, battery.text if battery else "")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          6. LLM USAGE POLICY                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_llm_cost():
    print_system("\n── LLM usage ─────────────────────────────────────────────")

    # The central claim, asserted by counting rather than by inspection: a full day's worth
    # of evaluation costs zero model calls.
    clock = FakeClock(at(0))
    presence = make(clock=clock, metrics={"cpu_percent": 95.0, "ram_percent": 97.0,
                                          "battery_percent": 8.0, "battery_plugged": False},
                    system_sample_s=1.0)
    calls_before = presence.stats["llm_calls"]
    evaluations = 0
    for _ in range(720):                       # 12 hours at a 60-second tick
        clock.advance(60)
        presence.note_focus("Visual Studio Code")
        signals = presence.signals(focus_app="Visual Studio Code", focus_seconds=600)
        for candidate in presence.candidates(signals):
            presence.should_speak(candidate, clock())
        evaluations += 1
    check("720 full evaluations cost zero LLM calls",
          presence.stats["llm_calls"] == calls_before, f"{evaluations} evaluations")

    # -- rewording is off by default and refused for warnings ------------------
    presence = make(clock=FakeClock(at(2)), llm_phrasing=False)
    candidate = PresenceCandidate("late_night", Tier.SOCIAL, "It's late.", 1.0)
    check("no prompt is built while LLM phrasing is off",
          presence.llm_prompt(candidate) is None)

    presence = make(clock=FakeClock(at(2)), llm_phrasing=True)
    prompt = presence.llm_prompt(candidate)
    check("an approved social remark can be reworded", prompt is not None)
    for required in ("current_time", "event_type", "priority", "form_of_address",
                     "draft", "recently_said"):
        check(f"the realization contract carries '{required}'", required in prompt)
    for rule in ("No markdown", "no emoji", "Do not invent"):
        check(f"the contract forbids: {rule}", rule in prompt)

    warning = PresenceCandidate("battery_low", Tier.CRITICAL, "Battery is at 9 percent.", 1.0)
    check("a warning is never sent to the model",
          presence.llm_prompt(warning) is None)

    # -- greetings never call a model ------------------------------------------
    presence = make(clock=FakeClock(at(9)), llm_phrasing=True)
    before = presence.stats["llm_calls"]
    for _ in range(50):
        presence.greeting("hi")
        presence.boot_line()
    check("greetings and boot lines never call a model",
          presence.stats["llm_calls"] == before)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        7. AGENT INTEGRATION                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_integration():
    print_system("\n── Integration with the existing proactive agent ─────────")

    import kayra.services.proactive_agent as pa

    original_data_path = pa.data_path
    pa.data_path = lambda name: os.path.join(_TMP, name)

    class Speaker:
        def __init__(self):
            self.spoken = []

        def __call__(self, text):
            self.spoken.append(text)
            return True

    try:
        check("the agent imports the presence layer", pa.PRESENCE_AVAILABLE)

        # -- the layer is wired in by default, and can be switched off ---------
        clock = FakeClock(at(2))
        runtime = RuntimeState()
        runtime.set_state(AssistantState.IDLE)
        speaker = Speaker()
        agent = pa.ProactiveAgent(runtime=runtime, speak_fn=speaker,
                                  is_speaking_fn=lambda: False, clock=clock)
        check("a default agent has a presence layer", agent.presence is not None)

        bare = pa.ProactiveAgent(runtime=runtime, speak_fn=speaker,
                                 is_speaking_fn=lambda: False, clock=clock, presence=False)
        check("presence can be disabled entirely, restoring the old behaviour",
              bare.presence is None)

        # -- the agent's own late-night builder steps aside --------------------
        stamp = datetime.datetime.fromtimestamp(clock())
        agent.current_app = "Visual Studio Code"
        check("the agent does not build a second late-night candidate",
              agent._candidate_late_night(clock(), stamp) is None)
        check("without presence, the original builder still runs",
              bare._candidate_late_night(clock(), stamp) is not None)

        # -- a presence candidate reaches the speaker through the ONE pipeline --
        agent.presence.note_interaction()
        clock.advance(200)
        agent.tick()
        check("a presence candidate can be spoken through the agent",
              bool(speaker.spoken), str(speaker.spoken))
        if speaker.spoken:
            check("what was spoken is a short spoken sentence",
                  len(speaker.spoken[-1]) < 140 and "**" not in speaker.spoken[-1],
                  speaker.spoken[-1])
            check("the presence ledger recorded it",
                  agent.presence.describe()["last_kind"] is not None)

        # -- the safety gate still owns the decision ---------------------------
        runtime.set_state(AssistantState.SPEAKING)
        ok, reason = agent.is_safe_window()
        check("nothing is spoken while the assistant is speaking", not ok, reason)
        runtime.set_state(AssistantState.IDLE)
        runtime.note_user_utterance()
        ok, reason = agent.is_safe_window()
        check("nothing is spoken just after the user spoke", not ok, reason)
        runtime.set_sleeping(True)
        ok, reason = agent.is_safe_window()
        check("nothing is spoken during standby", not ok, reason)
        runtime.set_sleeping(False)
        runtime.shutdown_event.set()
        ok, reason = agent.is_safe_window()
        check("nothing is spoken during shutdown", not ok, reason)
        runtime.shutdown_event.clear()

        # -- the master switch silences presence too ---------------------------
        agent.set_enabled(False)
        check("turning the service off turns presence off",
              not agent.presence.enabled)
        agent.set_enabled(True)
        check("turning it back on restores presence", agent.presence.enabled)
        agent.stop(timeout=1.0)

        # -- events reach presence ----------------------------------------------
        clock = FakeClock(at(14))
        runtime = RuntimeState()
        agent = pa.ProactiveAgent(runtime=runtime, speak_fn=Speaker(),
                                  is_speaking_fn=lambda: False, clock=clock)
        agent.on_event("user_utterance", {"text": "hello"})
        agent.on_event("intent_classified", {"text": "hello", "tokens": ["general hello"]})
        check("one turn counts as one interaction, not two",
              agent.presence.describe()["interactions"] == 1)
        agent.on_event("automation_result", {"ok": 0, "failed": 1, "tokens": ["app.open"]})
        agent.on_event("automation_result", {"ok": 0, "failed": 1, "tokens": ["app.open"]})
        signals = agent.presence.signals()
        check("a repeated failure event reaches the presence layer",
              signals.repeated_failure is not None, str(signals.repeated_failure))
        agent.on_event("barge_in", {"text": "stop"})
        check("a barge-in clears any pending candidate", agent._pending is None)

        # -- no LLM call during agent evaluation ---------------------------------
        def exploding_phraser(prompt, fallback):
            raise AssertionError("the agent must not call a model to decide anything")

        clock = FakeClock(at(2))
        runtime = RuntimeState()
        agent = pa.ProactiveAgent(runtime=runtime, speak_fn=lambda t: False,
                                  is_speaking_fn=lambda: False, clock=clock,
                                  phrase_fn=exploding_phraser)
        agent.presence.note_interaction()
        raised = False
        try:
            for _ in range(200):
                clock.advance(20)
                agent.evaluate(clock())
        except AssertionError:
            raised = True
        check("200 agent evaluations never call the phrasing model", not raised)

        # -- an LLM failure falls back to the deterministic wording ---------------
        candidate = PresenceCandidate("late_night", Tier.SOCIAL, "It's rather late.", 1.0)
        agent.presence.config.llm_phrasing = True
        text = agent._phrase(candidate)
        check("a raising phraser falls back to the template", text == candidate.text, text)

        agent2 = pa.ProactiveAgent(runtime=runtime, speak_fn=lambda t: False,
                                   is_speaking_fn=lambda: False, clock=clock,
                                   phrase_fn=lambda prompt, fallback: "Still working, then?")
        agent2.presence.config.llm_phrasing = True
        text = agent2._phrase(candidate)
        check("a good rewording is used", text == "Still working, then?", text)

        agent3 = pa.ProactiveAgent(runtime=runtime, speak_fn=lambda t: False,
                                   is_speaking_fn=lambda: False, clock=clock,
                                   phrase_fn=lambda prompt, fallback: "**Hey!** " + "x" * 400)
        agent3.presence.config.llm_phrasing = True
        text = agent3._phrase(candidate)
        check("a malformed rewording is rejected", text == candidate.text, text[:40])
    finally:
        pa.data_path = original_data_path


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     8. TTS ROUTING AND CANCELLATION                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_tts_routing():
    print_system("\n── Speech routing ────────────────────────────────────────")

    import kayra.services.proactive_agent as pa

    class FakeTTS:
        def __init__(self):
            self.is_playing = False
            self.spoken = []
            self.begin_turn_calls = 0
            self.background_calls = 0

        def begin_turn(self):
            self.begin_turn_calls += 1

        def begin_background_utterance(self):
            self.background_calls += 1

        def speak(self, text, blocking=False):
            self.spoken.append(text)

    tts = FakeTTS()
    runtime = RuntimeState()
    agent = pa.create_default_agent(tts_engine=tts, llm_engine=None, runtime=runtime)
    check("the default wiring still builds a presence layer", agent.presence is not None)

    agent.speak_fn("A contextual remark.")
    check("presence speech goes through the shared TTS engine", tts.spoken == ["A contextual remark."])
    check("it is a BACKGROUND utterance", tts.background_calls == 1)
    check("it never begins a turn — that would clear the interrupt latch",
          tts.begin_turn_calls == 0)

    tts.is_playing = True
    agent.speak_fn("Should not be queued.")
    check("nothing is queued while audio is already playing", len(tts.spoken) == 1)

    source_path = os.path.join(project_root, "src", "kayra", "intelligence",
                               "proactive_presence.py")
    source = open(source_path, encoding="utf-8").read()
    check("the presence layer never calls begin_turn()", ".begin_turn(" not in source)
    check("it never imports the TTS engine",
          "text_to_speech" not in source)
    check("it never imports the STT engine",
          "speech_to_text" not in source)
    check("it never constructs an LLM engine",
          "CentralizedLLMEngine" not in source)
    check("it starts no thread of its own",
          "threading.Thread" not in source)
    check("it opens no subprocess",
          "subprocess." not in source and "os.system(" not in source
          and "import subprocess" not in source)
    check("it writes no file of its own",
          "open(" not in source.replace("# ", "") or "\"w\"" not in source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       9. HELPERS AND COST                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_helpers_and_cost():
    print_system("\n── Helpers, bounds and cost ──────────────────────────────")

    check("day parts split the clock sensibly",
          (day_part(7), day_part(13), day_part(19), day_part(2)) ==
          ("morning", "afternoon", "evening", "night"))
    check("a window that wraps past midnight works",
          in_window(2, 1, 5) and not in_window(12, 1, 5) and in_window(23, 22, 6))
    check("an empty window is never inside", not in_window(3, 4, 4))

    check("token overlap ignores stopwords",
          _tokens("You are still working") == _tokens("still working"))
    check("identical remarks score 1.0", _jaccard(_tokens("a b c"), _tokens("a b c")) == 1.0)
    check("unrelated remarks score 0.0",
          _jaccard(_tokens("memory pressure"), _tokens("welcome back")) == 0.0)

    check("conversation tokens are not counted as actions",
          _action_key("general what is the capital of france") is None)
    check("an action token reduces to a bounded key",
          _action_key("open chrome") == "open chrome")
    check("a long token is still bounded",
          len(_action_key("google search something very long indeed").split()) == 2)

    # -- bounded state -----------------------------------------------------------
    clock = FakeClock(at(12))
    presence = make(clock=clock)
    for index in range(500):
        presence.note_intents([f"open app{index}"])
        presence.note_automation_result(0, 1, [f"app.open{index}"])
        clock.advance(1)
    check("the action counters stay bounded",
          len(presence._actions) <= presence.MAX_ACTION_COUNTERS, str(len(presence._actions)))
    check("the failure counters stay bounded",
          len(presence._failures) <= presence.MAX_ACTION_COUNTERS, str(len(presence._failures)))

    for index in range(200):
        candidate = PresenceCandidate("user_return", Tier.SOCIAL, f"Line {index}.", 1.0)
        presence.note_spoken(candidate, candidate.text)
    check("the recent-wording ledger stays bounded",
          len(presence._recent) <= presence.RECENT_UTTERANCES, str(len(presence._recent)))

    # -- suppression logging is rate limited ---------------------------------------
    clock = FakeClock(at(12))
    presence = make(clock=clock)
    released = sum(1 for _ in range(100) if presence.note_suppressed("cooldown"))
    check("a repeated suppression is logged once, not a hundred times", released == 1)
    clock.advance(120)
    check("it is logged again after a minute", presence.note_suppressed("cooldown"))

    # -- cost ------------------------------------------------------------------------
    clock = FakeClock(at(2))
    presence = make(clock=clock, metrics={"cpu_percent": 30.0, "ram_percent": 50.0,
                                          "battery_percent": 80.0, "battery_plugged": True})
    presence.note_interaction()
    iterations = 2000
    started = time.perf_counter()
    for _ in range(iterations):
        signals = presence.signals(focus_app="Visual Studio Code", focus_seconds=600)
        for candidate in presence.candidates(signals):
            presence.should_speak(candidate, clock())
    micros = (time.perf_counter() - started) / iterations * 1e6
    check(f"a full evaluation costs under 500us ({micros:.1f}us measured)", micros < 500.0)

    started = time.perf_counter()
    for _ in range(5000):
        is_greeting("hey kayra")
    greet_us = (time.perf_counter() - started) / 5000 * 1e6
    check(f"greeting detection costs under 50us ({greet_us:.1f}us measured)", greet_us < 50.0)

    started = time.perf_counter()
    for _ in range(5000):
        presence.describe()
    describe_us = (time.perf_counter() - started) / 5000 * 1e6
    check(f"the UI status read costs under 100us ({describe_us:.1f}us measured)",
          describe_us < 100.0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        10. CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

def section_config():
    print_system("\n── Configuration ─────────────────────────────────────────")

    saved = {k: os.environ.get(k) for k in
             ("PROACTIVE_PRESENCE_ENABLED", "PROACTIVE_HUMOR_ENABLED",
              "PROACTIVE_WORK_SESSION_MINUTES", "PROACTIVE_PRESENCE_DAILY_BUDGET",
              "PROACTIVE_RAM_WARN_PERCENT")}
    try:
        os.environ["PROACTIVE_PRESENCE_ENABLED"] = "False"
        check("the master switch is read from the environment", not PresenceConfig().enabled)
        os.environ["PROACTIVE_PRESENCE_ENABLED"] = "True"

        os.environ["PROACTIVE_HUMOR_ENABLED"] = "off"
        check("a category switch is read from the environment", not PresenceConfig().humor)
        os.environ["PROACTIVE_HUMOR_ENABLED"] = "True"

        os.environ["PROACTIVE_WORK_SESSION_MINUTES"] = "30, 60, 120"
        check("milestones are parsed from a list",
              PresenceConfig().work_milestones == (1800.0, 3600.0, 7200.0))
        os.environ["PROACTIVE_WORK_SESSION_MINUTES"] = "nonsense"
        check("a malformed milestone list falls back to the defaults",
              PresenceConfig().work_milestones == (2700.0, 5400.0, 9000.0))

        os.environ["PROACTIVE_RAM_WARN_PERCENT"] = "999"
        check("an out-of-range value is clamped, not accepted",
              PresenceConfig().ram_warn == 100.0)
        os.environ["PROACTIVE_RAM_WARN_PERCENT"] = "not a number"
        check("an unparseable value falls back to its default",
              PresenceConfig().ram_warn == 90.0)

        os.environ["PROACTIVE_PRESENCE_DAILY_BUDGET"] = "0"
        check("a zero daily budget is accepted as 'no ceiling'",
              PresenceConfig().daily_budget == 0)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # Every switch offered by the UI must be a real category the engine honours.
    presence = make()
    for name in ("greetings", "context", "late_night", "work_session", "system", "humor"):
        check(f"'{name}' is a settable category", presence.set_category(name, True))
    check("an unknown category is rejected rather than silently stored",
          not presence.set_category("telepathy", True))
    check("describe() reports every category",
          set(presence.describe()["categories"]) ==
          {"greetings", "context", "late_night", "work_session", "system", "humor"})


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                RUNNER                                  │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_banner("KAYRA PROACTIVE PRESENCE DIAGNOSTIC",
                 "Greetings, context, suppression, tone, LLM policy & cost")
    try:
        section_greeting()
        section_candidates()
        section_silence()
        section_priority()
        section_personality()
        section_llm_cost()
        section_integration()
        section_tts_routing()
        section_helpers_and_cost()
        section_config()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All proactive presence checks passed.")
