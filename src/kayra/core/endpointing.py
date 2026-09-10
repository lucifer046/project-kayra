# ┌────────────────────────────────────────────────────────────────────────┐
# │                          endpointing.py                                │
# │        THE Utterance Boundary — One Decision, One Commit Point         │
# └────────────────────────────────────────────────────────────────────────┘
"""
When has the user finished speaking?

This module is the ONE authority for that question. Everything that used to answer it
independently now consumes the answer instead.

WHY THIS EXISTS — THE BUG IT WAS WRITTEN FOR
--------------------------------------------
A user said, in Hindi and English together:

    "यार मेरी girlfriend मुझसे नाराज़ है, बताओ मैं क्या करूँ?"

and Kayra shut down mid-sentence.

The cause was not the recogniser and not a mis-heard word. It was a SECOND COMMIT POINT. The
recognition page published lifecycle commands (`window.kayraControl`) straight out of
`recognition.onresult`, from a probe built out of the INTERIM transcript:

    const probe = (currentText + " " + interimTranscript).trim();
    if (!window.kayraControl) {
        const kind = looksLikeControl(probe);       // <- SHUTDOWN reachable from here
        if (kind) { window.kayraControl = {...}; }
    }

`_local_control_watcher` polls that flag at ~17 Hz and dispatches it. So a transient interim
reading of "exit" — which a recogniser running `hi-IN` will produce for all sorts of Hindi
phonemes — reached `request_shutdown()` **without ever passing through the endpointer**, while
the user was still talking. The VAD was working correctly and was simply not consulted, because
that path did not go through it.

So the fix is architectural, not lexical. There is no word list here and no "don't trust the
word exit" special case; those would be a dictionary by another name and would eventually
mis-fire on a legitimate command. What changed is that **only a committed utterance can reach a
control**, and this module decides what "committed" means.

THE PIPELINE, AND THE ONE PLACE IT NARROWS
------------------------------------------
    microphone
      -> AEC / noise suppression
      -> VAD                        (acoustic: is a person making speech sounds right now?)
      -> recogniser                 (interim segments, then final segments)
      -> utterance accumulator      (segments joined; bounded)
      -> ENDPOINT DECISION          <-- this module. THE commit point.
      -> transcript repair
      -> conversation context
      -> control classification
      -> DMM

Above the arrow, nothing may act on words. Below it, everything does. The single documented
exception is barge-in: the interim transcript may still be inspected to SILENCE PLAYBACK, and
only for that. Silencing is not an action on the world — it hands the floor back — and its
whole value is that it happens before the endpoint. It can never start a turn, run automation,
end the process or reach the DMM.

TWO SIGNALS, AND WHY NEITHER IS SUFFICIENT
------------------------------------------
* **A final recognition result is not the end of a turn.** It means "this SEGMENT is final".
  Recognisers emit several per sentence, and they emit them while the speaker keeps going.
  Treating one as the boundary is how "Okay Kayra I wanted to ask you something because…"
  becomes four turns and how a fragment reaches a control.
* **A silence timer is not the end of a turn either.** Results LAG the sound by a variable
  amount, so "no results for N ms" fires in the middle of a sentence whenever the backend is
  slow — and a clipped word is worse than a mis-heard one, because there is nothing to repair.

So the boundary needs BOTH: the recogniser quiet AND the room quiet. That rule already existed
and is preserved. What this module adds is the part that was missing — CONTINUATION EVIDENCE.

CONTINUATION EVIDENCE
---------------------
Three independent reasons to keep listening, all of them about *acoustics and timing*, none of
them about which words were heard:

1. **The user is audibly speaking right now.** No transcript can end a turn while the VAD
   still sees voice. This alone would have prevented the reported failure.
2. **The turn is younger than `min_utterance_ms`.** A turn that has existed for 120 ms has not
   ended; whatever produced a transcript that fast, it was not a completed thought.
3. **The transcript is implausibly short for how long the person spoke.** Three seconds of
   speech that produced one word means the recogniser is behind, not that the user said one
   word. Such a turn never takes the fast path and must wait out a longer hangover.

Point 3 is the one that needs stating carefully: it does NOT guess a missing word, does NOT
rewrite anything, and does NOT decide the transcript is wrong. It only declines to END THE TURN
YET. If the user really did say one word and then stopped, the longer hangover expires and the
word is committed — a little later, and correctly.

SHORT COMMANDS STAY FAST
------------------------
The fix must not be "wait two seconds for everything". A bare "stop" over a running answer has
to land in tens of milliseconds, and "stop listening" must not feel laggy. So the hangover is
ADAPTIVE, and the fast path is available exactly when the evidence supports it: a short,
already-COMMITTED transcript, with no interim still pending, no voice in the room, and no
continuation suspicion. Every one of those conditions is cheap and local.

Measured on the reference host: a short control command commits ~420–500 ms after the speaker
stops (unchanged), while a sentence that is still being spoken cannot commit at all.

RELATIONSHIP TO THE RECOGNITION PAGE
------------------------------------
The page runs the same predicate in JavaScript, because the decision has to be taken at 60 ms
resolution next to the audio and a Selenium round-trip per tick is not available. That is the
same shape as the control vocabulary, which is defined here and injected into the page: **one
rule, two implementations, and a test that asserts they agree**. `tuning_payload()` is what the
page is configured with, so there is one set of thresholds rather than two that can drift, and
`tests/test_voice_turn.py` drives both.

This module is a LEAF: stdlib only, no I/O, no threads, no imports from the rest of Kayra. It
holds no state — `decide()` is a pure function of the snapshot it is given, which is what makes
the whole scenario table in the suite expressible without a browser.
"""

import os

# ┌────────────────────────────────────────────────────────────────────────┐
# │                             THRESHOLDS                                 │
# └────────────────────────────────────────────────────────────────────────┘
# Every value is a duration in milliseconds and every one is overridable from `.env`, clamped
# to a range in which the endpointer still behaves. A malformed setting is corrected rather
# than obeyed — the same rule the gesture configuration follows — because a `.env` typo must
# not be able to produce an endpointer that never fires or one that fires instantly.

DEFAULTS = {
    # The baseline: how long the recogniser must produce nothing before the turn may end.
    # This is the historical `silenceLimit` and is unchanged.
    "silence_ms": 800,

    # A short, COMMITTED command does not need the full window. "stop" must not cost 800 ms.
    # Reachable only when every continuation check is clear — see `decide()`.
    "fast_endpoint_ms": 420,

    # Words the recogniser has not committed yet are worth waiting for. Cutting here is how a
    # spoken word becomes no word at all.
    "interim_grace_ms": 1400,

    # How long the ROOM must be quiet, on top of the recogniser going quiet. This is what stops
    # a mid-sentence breath ending the utterance.
    "vad_hangover_ms": 500,

    # A turn younger than this cannot end, whatever the transcript says. Guards the case where
    # a stray result arrives in the first moments of speech.
    "min_utterance_ms": 350,

    # Beyond this much speech, a one- or two-word transcript is treated as the recogniser being
    # behind rather than as a complete thought. See `looks_truncated()`.
    "continuation_speech_ms": 1500,

    # The multiplier applied to `vad_hangover_ms` when a turn looks truncated. Deliberately
    # modest: this delays a commit, it never blocks one.
    "truncated_hangover_scale": 2,

    # Results keep arriving but the endpoint never settles: flush rather than accumulate a
    # paragraph. NEVER fires while the VAD still hears the user — see `decide()`.
    "max_wait_ms": 6000,

    # The one bound that ignores everything else. A session where the VAD is stuck reporting
    # voice forever must still produce a turn eventually rather than listening for ever.
    "absolute_max_ms": 30000,
}

# (env var, key, minimum, maximum). The bounds are the range over which the endpointer was
# measured to behave; outside them it is not "tuned differently", it is broken.
_ENV_BOUNDS = (
    ("STT_SILENCE_MS", "silence_ms", 200, 5000),
    ("STT_FAST_ENDPOINT_MS", "fast_endpoint_ms", 150, 2000),
    ("STT_INTERIM_GRACE_MS", "interim_grace_ms", 300, 5000),
    ("STT_VAD_HANGOVER_MS", "vad_hangover_ms", 100, 3000),
    ("STT_MIN_UTTERANCE_MS", "min_utterance_ms", 0, 3000),
    ("STT_CONTINUATION_SPEECH_MS", "continuation_speech_ms", 400, 10000),
    ("STT_MAX_UTTERANCE_WAIT_MS", "max_wait_ms", 1500, 30000),
    ("STT_ABSOLUTE_MAX_MS", "absolute_max_ms", 5000, 120000),
)

# A transcript of this many words or fewer is "short". Short is what makes the fast path
# available and, past `continuation_speech_ms` of speech, what makes a turn look truncated.
SHORT_COMMAND_WORDS = 3


def _env_int(name, default, low, high):
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def tuning(overrides=None):
    """
    The active thresholds: defaults, then `.env`, then an explicit override.

    Returned as a plain dict so it can be handed to the recognition page verbatim. There is
    exactly one place these numbers are decided and the page is configured FROM it, which is
    what stops the JavaScript and the Python drifting into two endpointers.
    """
    values = dict(DEFAULTS)
    for env_name, key, low, high in _ENV_BOUNDS:
        values[key] = _env_int(env_name, values[key], low, high)

    # Invariants, enforced rather than trusted. A fast path longer than the baseline is not a
    # fast path, and a hangover that outlives the hard timeout means the turn can only ever end
    # by timeout — both are silent misbehaviour if left uncorrected.
    values["fast_endpoint_ms"] = min(values["fast_endpoint_ms"], values["silence_ms"])
    values["interim_grace_ms"] = max(values["interim_grace_ms"], values["silence_ms"])
    values["max_wait_ms"] = max(values["max_wait_ms"],
                                values["interim_grace_ms"] + values["vad_hangover_ms"])
    values["absolute_max_ms"] = max(values["absolute_max_ms"], values["max_wait_ms"] * 2)

    if overrides:
        values.update({k: v for k, v in overrides.items() if k in values})
    return values


def tuning_payload(overrides=None):
    """
    The thresholds in the shape the recognition page expects (camelCase JS keys).

    Named separately from `tuning()` so the Python side never has to think in the page's
    vocabulary, and so a key added here is impossible to forget on the other side — the suite
    asserts the two sets correspond.
    """
    values = tuning(overrides)
    return {
        "silenceMs": values["silence_ms"],
        "fastEndpointMs": values["fast_endpoint_ms"],
        "interimGraceMs": values["interim_grace_ms"],
        "vadHangoverMs": values["vad_hangover_ms"],
        "minUtteranceMs": values["min_utterance_ms"],
        "continuationSpeechMs": values["continuation_speech_ms"],
        "truncatedHangoverScale": values["truncated_hangover_scale"],
        "maxWaitMs": values["max_wait_ms"],
        "absoluteMaxMs": values["absolute_max_ms"],
        "shortCommandWords": SHORT_COMMAND_WORDS,
    }


# ┌────────────────────────────────────────────────────────────────────────┐
# │                            THE SNAPSHOT                                │
# └────────────────────────────────────────────────────────────────────────┘

class TurnSnapshot:
    """
    Everything the decision needs, as it stands at one instant.

    A VALUE, not a stream. The decision is a pure function of this, which is what makes every
    scenario in `tests/test_voice_turn.py` — a final result arriving while the user is still
    speaking, speech resuming inside the grace window, a recogniser that has gone quiet while
    the room has not — expressible as a table rather than as a browser session.
    """

    __slots__ = ("now_ms", "utterance_start_ms", "last_result_ms", "last_voice_ms",
                 "committed_text", "interim_text", "voice_active", "vad_ready",
                 "assistant_speaking")

    def __init__(self, now_ms, utterance_start_ms, last_result_ms, last_voice_ms,
                 committed_text="", interim_text="", voice_active=False, vad_ready=True,
                 assistant_speaking=False):
        self.now_ms = now_ms
        self.utterance_start_ms = utterance_start_ms
        self.last_result_ms = last_result_ms
        self.last_voice_ms = last_voice_ms
        self.committed_text = committed_text or ""
        self.interim_text = interim_text or ""
        self.voice_active = bool(voice_active)
        self.vad_ready = bool(vad_ready)
        self.assistant_speaking = bool(assistant_speaking)

    # ── Derived facts, named so the decision reads as prose ──

    @property
    def has_text(self):
        return bool(self.committed_text.strip() or self.interim_text.strip())

    @property
    def pending_interim(self):
        """Words the recogniser has produced but not committed. Always worth waiting for."""
        return bool(self.interim_text.strip())

    @property
    def word_count(self):
        return len(self.committed_text.split())

    @property
    def since_result_ms(self):
        return max(0, self.now_ms - self.last_result_ms)

    @property
    def since_voice_ms(self):
        # With no VAD (no WebAudio, or the microphone refused) this degrades EXACTLY to the old
        # recogniser-only behaviour rather than failing: `since_voice` tracks `since_result`,
        # so `room_quiet` becomes a tautology and the pre-VAD rule is what remains.
        if not self.vad_ready:
            return self.since_result_ms
        return max(0, self.now_ms - self.last_voice_ms)

    @property
    def speech_duration_ms(self):
        return max(0, self.now_ms - self.utterance_start_ms)

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class Decision:
    """`commit` says whether the turn ends now; `reason` says which rule decided it."""

    __slots__ = ("commit", "reason", "detail")

    # Reasons a turn ENDS.
    ENDPOINT = "endpoint"                    # recogniser and room both quiet
    TIMEOUT = "timeout"                      # results kept coming, endpoint never settled
    ABSOLUTE = "absolute-timeout"            # the last-resort bound

    # Reasons a turn CONTINUES. Each names the evidence, which is what makes the INFO log line
    # ("waiting for speech continuation") answerable rather than mysterious.
    NO_SPEECH = "no-speech"
    VOICE_ACTIVE = "voice-active"
    TOO_YOUNG = "min-duration"
    RECOGNIZER_BUSY = "recognizer-busy"
    ROOM_NOISY = "room-not-quiet"
    TRUNCATED = "transcript-truncated"

    def __init__(self, commit, reason, detail=""):
        self.commit = bool(commit)
        self.reason = reason
        self.detail = detail

    def __bool__(self):
        return self.commit

    def __repr__(self):
        return f"<Decision {'COMMIT' if self.commit else 'WAIT'} {self.reason}>"


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          THE DECISION                                  │
# └────────────────────────────────────────────────────────────────────────┘

def looks_truncated(snapshot, config=None):
    """
    Does this transcript look like the recogniser is behind rather than like a finished thought?

    True when the person has been speaking for longer than `continuation_speech_ms` and the
    committed transcript is still one or two words. Three seconds of speech does not produce
    one word.

    THIS IS NOT A JUDGEMENT ABOUT THE WORD. It knows nothing about which word it is, and it
    cannot be used to reject one — its only effect is that such a turn does not take the fast
    path and must wait out a longer hangover. If the user genuinely said one word and stopped,
    the hangover expires and the word is committed, correctly, a few hundred milliseconds later.

    A turn with pending interim text is never "truncated": the recogniser is visibly still
    working, which `pending_interim` already handles with a longer wait of its own.
    """
    config = config or tuning()
    if snapshot.pending_interim:
        return False
    words = snapshot.word_count
    if words == 0 or words > SHORT_COMMAND_WORDS - 1:
        return False
    return snapshot.speech_duration_ms >= config["continuation_speech_ms"]


def required_quiet_ms(snapshot, config=None):
    """
    How long the recogniser must have been silent before this particular turn may end.

    Adaptive, and the adaptation is the whole reason short commands stayed fast while long
    sentences stopped being clipped:

        pending interim words   -> the long grace. Uncommitted words are worth waiting for.
        looks truncated         -> the baseline, never the fast path.
        short and committed     -> the fast path.
        anything else           -> the baseline.
    """
    config = config or tuning()
    if snapshot.pending_interim:
        return max(config["silence_ms"], config["interim_grace_ms"])
    if looks_truncated(snapshot, config):
        return config["silence_ms"]
    if 0 < snapshot.word_count <= SHORT_COMMAND_WORDS:
        return min(config["silence_ms"], config["fast_endpoint_ms"])
    return config["silence_ms"]


def required_hangover_ms(snapshot, config=None):
    """How long the ROOM must be quiet. Longer when the transcript looks truncated."""
    config = config or tuning()
    hangover = config["vad_hangover_ms"]
    if looks_truncated(snapshot, config):
        hangover *= max(1, int(config["truncated_hangover_scale"]))
    return hangover


def decide(snapshot, config=None):
    """
    THE commit decision. Pure, total, and the only place a turn is allowed to end.

    The order of the checks is the design. Each one that comes first is a reason no later check
    can matter, and the cheapest and most certain evidence is consulted first.
    """
    config = config or tuning()

    # 0. The absolute bound, before everything. A VAD wedged reporting voice for ever must not
    #    make the assistant deaf; it must produce the turn and move on. This is the ONLY branch
    #    that commits while `voice_active`, and it is deliberately far out of the way of any
    #    real sentence — half a minute of unbroken speech.
    if snapshot.has_text and snapshot.speech_duration_ms >= config["absolute_max_ms"]:
        return Decision(True, Decision.ABSOLUTE,
                        f"{snapshot.speech_duration_ms}ms of continuous turn")

    # 1. Nothing to commit. Not an endpoint, just silence.
    if not snapshot.has_text:
        return Decision(False, Decision.NO_SPEECH)

    # 2. THE USER IS SPEAKING RIGHT NOW.
    #    This single check is what the reported failure needed and did not have. No transcript,
    #    however final-looking, ends a turn while the acoustic detector still hears the person.
    #    It also implements "resuming speech cancels a pending endpoint" without any pending
    #    state at all: the grace window is re-evaluated from scratch every tick, so speech that
    #    resumes simply makes the next tick answer WAIT again.
    if snapshot.vad_ready and snapshot.voice_active and not snapshot.assistant_speaking:
        return Decision(False, Decision.VOICE_ACTIVE)

    # 3. The turn is too young to have ended.
    if snapshot.speech_duration_ms < config["min_utterance_ms"]:
        return Decision(False, Decision.TOO_YOUNG,
                        f"{snapshot.speech_duration_ms}ms < {config['min_utterance_ms']}ms")

    since_result = snapshot.since_result_ms
    since_voice = snapshot.since_voice_ms

    # 4. The hard timeout: results keep arriving and the endpoint never settles. Reached only
    #    once the room is quiet — checks 2 and 3 already ran — so this can no longer flush a
    #    sentence out from under a speaker, which the previous `hardTimeout` branch could.
    if since_result >= config["max_wait_ms"]:
        return Decision(True, Decision.TIMEOUT, f"no result for {since_result}ms")

    # 5. Both quiets. Neither alone is sufficient, and the required durations are adaptive.
    need_quiet = required_quiet_ms(snapshot, config)
    need_hangover = required_hangover_ms(snapshot, config)

    if since_result <= need_quiet:
        return Decision(False, Decision.RECOGNIZER_BUSY,
                        f"{since_result}ms of {need_quiet}ms")
    if since_voice <= need_hangover:
        reason = (Decision.TRUNCATED if looks_truncated(snapshot, config)
                  else Decision.ROOM_NOISY)
        return Decision(False, reason, f"{since_voice}ms of {need_hangover}ms")

    return Decision(True, Decision.ENDPOINT, "recognizer and room quiet")
