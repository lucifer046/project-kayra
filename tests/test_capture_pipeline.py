# ┌────────────────────────────────────────────────────────────────────────┐
# │                      test_capture_pipeline.py                          │
# │   Conversation Context, Transcript Repair & the Recognition Page       │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_capture_pipeline.py — assertion suite for the input pipeline's last two stages.

    audio capture -> AEC / noise suppression -> VAD / endpointing -> STT -> repair
                     [the page, checked structurally here]                 [checked fully]

Like the other scripts in tests/, this is a standalone entry point (no pytest runner):

    .venv\\Scripts\\python tests\\test_capture_pipeline.py

It exits non-zero if anything fails. It needs NO microphone, NO browser and NO network: the
page is checked as source (the live behaviour is covered by the live capture script), and
the context and repair stages are pure.

THE SECTION THAT MATTERS MOST is `section_repair_refuses`. This stage is allowed to change
what the user said, which makes every check on what it REFUSES to do worth more than any
check on what it does. The single most important one is that it can never invent a shutdown:
"exist" and "exit" are two edits apart and share a phonetic key, so without that guard a
correction layer would eventually quit the assistant over a word the user said perfectly.
"""

import os
import re
import sys
import time

# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_success, print_error, print_system
from kayra.core.conversation_context import (ConversationContext, ConversationMode,
                                             get_conversation_context)
from kayra.input.transcript_repair import (TranscriptRepair, RepairResult, phonetic_key,
                                           edit_distance, CONFIDENT_ENOUGH,
                                           MAX_PHONETIC_WORDS, get_transcript_repair)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


class FakeClock:
    def __init__(self, start=None):
        self.t = (start if start is not None else time.time()) * 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds * 1000.0
        return self.t


VOCABULARY = ("open chrome", "close window", "close tab", "play music", "pause music",
              "take screenshot", "volume up", "minimize all", "google search", "battery")


def make_repair(context=None, vocabulary=VOCABULARY):
    return TranscriptRepair(context=context or ConversationContext(), vocabulary=vocabulary)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     1. THE CONVERSATION CONTEXT                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_context():
    print_system("\n── Conversation context ──────────────────────────────────")

    context = ConversationContext()
    check("a fresh context is idle", context.mode == ConversationMode.IDLE)
    check("and has no turns", context.turn_count == 0)
    check("and expects no answer", not context.expects_confirmation())

    context.note_user_turn("open chrome and check the deployment logs")
    check("a user turn is counted", context.turn_count == 1)
    check("content words become the topic",
          "deployment" in context.topic_terms() and "chrome" in context.topic_terms(),
          str(context.topic_terms()))
    check("stopwords do not", "and" not in context.topic_terms())
    check("short words do not", "the" not in context.topic_terms())

    context.note_intent(["open chrome"])
    check("an action token means automation", context.mode == ConversationMode.AUTOMATION)
    context.note_intent(["general what is rust"])
    check("a conversation token means conversation",
          context.mode == ConversationMode.CONVERSATION)
    context.note_intent([])
    check("no tokens means idle", context.mode == ConversationMode.IDLE)

    context.set_pending_confirmation("Should I restart your computer?")
    check("a pending question changes the mode", context.mode == ConversationMode.CONFIRMING)
    check("and is reported", context.expects_confirmation())
    context.set_pending_confirmation("")
    check("clearing it leaves confirming", context.mode != ConversationMode.CONFIRMING)

    # Answering a question returns to the exchange it interrupted. Found live: clearing a
    # confirmation dropped the mode to IDLE, which told the repair stage the conversation
    # had no context at the exact moment it most obviously did.
    context.note_intent(["open chrome"])
    check("an automation exchange is in progress", context.mode == ConversationMode.AUTOMATION)
    context.set_pending_confirmation("Close all four windows?")
    check("a question interrupts it", context.mode == ConversationMode.CONFIRMING)
    context.set_pending_confirmation("")
    check("answering returns to the exchange, not to idle",
          context.mode == ConversationMode.AUTOMATION, context.mode)

    context.note_automation(action="open", target="Chrome")
    check("automation targets are recorded lowercase", "chrome" in context.recent_targets())
    context.note_automation(action="open", target="Chrome")
    check("a repeated target is not duplicated",
          list(context.recent_targets()).count("chrome") == 1)

    # The assistant's own words are context, but must not steer the repair vocabulary.
    context.note_assistant_turn("I have opened the quarterly spreadsheet for you")
    check("the assistant's words are remembered",
          context.snapshot()["last_assistant_text"].startswith("I have opened"))
    check("but never enter the topic vocabulary",
          "quarterly" not in context.topic_terms() and
          "spreadsheet" not in context.topic_terms(),
          str(context.topic_terms()))

    # ── bounds ──
    context = ConversationContext()
    for index in range(200):
        context.note_user_turn(f"turn number {index} about subject{index} and matters")
    check("the turn ring stays bounded",
          len(context.recent_turns()) <= ConversationContext.MAX_TURNS,
          str(len(context.recent_turns())))
    check("the topic vocabulary stays bounded",
          len(context.topic_terms(9999)) <= ConversationContext.MAX_TOPIC_TERMS,
          str(len(context.topic_terms(9999))))
    for index in range(50):
        context.note_automation(action="open", target=f"app{index}")
    check("the target list stays bounded",
          len(context.recent_targets()) <= ConversationContext.MAX_TARGETS,
          str(len(context.recent_targets())))
    check("a long utterance is truncated, never stored whole",
          len(context.recent_turns()[-1]["text"]) <= ConversationContext.MAX_TEXT)

    # A frequently-used term outranks a one-off, which is what makes the topic a signal.
    context = ConversationContext()
    for _ in range(4):
        context.note_user_turn("more about kubernetes please")
    context.note_user_turn("something about badminton")
    check("the topic is frequency-ordered",
          context.topic_terms()[0] == "kubernetes", str(context.topic_terms()))

    # ── robustness: bookkeeping must never cost the user their command ──
    context = ConversationContext()
    for bad in (None, 123, b"bytes", "", "   ", object()):
        context.note_user_turn(bad)
        context.note_assistant_turn(bad)
        context.note_automation(action=bad, target=bad)
    check("malformed input is ignored rather than raising", context.turn_count == 0)
    context.note_intent(None)
    context.note_intent([None, "", "open chrome"])
    check("malformed token lists are filtered", context.last_intent() == ("open chrome",))

    # ── the clock is injectable, like RuntimeState's ──
    clock = FakeClock()
    context = ConversationContext(clock_ms=clock)
    context.note_user_turn("hello")
    clock.advance(30)
    check("elapsed time is measured on the injected clock",
          abs(context.seconds_since_last_user_turn() - 30.0) < 0.01,
          str(context.seconds_since_last_user_turn()))

    # ── one per process ──
    check("the accessor returns one shared context",
          get_conversation_context() is get_conversation_context())

    # ── it is STATE, not memory: nothing is persisted ──
    source = open(os.path.join(project_root, "src", "kayra", "core",
                               "conversation_context.py"), encoding="utf-8").read()
    check("the context never writes to disk",
          "open(" not in source and "json" not in source.replace("# ", ""))
    check("the context imports nothing from the rest of kayra",
          "from kayra" not in source and "import kayra" not in source)

    context = ConversationContext()
    context.note_user_turn("something")
    context.note_automation(action="open", target="chrome")
    context.reset()
    check("reset clears the exchange",
          not context.recent_targets() and not context.topic_terms()
          and context.mode == ConversationMode.IDLE)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       2. PHONETICS AND DISTANCE                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_phonetics():
    print_system("\n── Phonetic key and distance ─────────────────────────────")

    check("sound-alikes share a key", phonetic_key("stop") == phonetic_key("stopp"))
    check("and so do close spellings", phonetic_key("chrome") == phonetic_key("crome"))
    check("unrelated words do not",
          phonetic_key("quit") != phonetic_key("great"),
          f"{phonetic_key('quit')} vs {phonetic_key('great')}")
    check("an empty word has no key", phonetic_key("") == "" and phonetic_key(None) == "")
    check("accents fold", phonetic_key("cafe") == phonetic_key("café"))
    check("the key is a fixed width",
          all(len(phonetic_key(w)) == 4 for w in ("a", "screenshot", "extraordinarily")))

    check("distance counts edits", edit_distance("exit", "exist") == 1)
    check("it abandons past the ceiling", edit_distance("cat", "elephant", 2) == 3)
    check("identical strings are zero", edit_distance("chrome", "chrome") == 0)
    check("it is symmetric",
          edit_distance("quit", "quiet") == edit_distance("quiet", "quit"))

    started = time.perf_counter()
    for _ in range(20000):
        phonetic_key("screenshot")
    micros = (time.perf_counter() - started) / 20000 * 1e6
    check(f"the key costs under 10us ({micros:.2f}us measured)", micros < 10.0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │              3. RULE 1 — RE-RANK WHAT THE RECOGNIZER SAID              │
# └────────────────────────────────────────────────────────────────────────┘

def section_rerank():
    print_system("\n── Repair rule 1: re-ranking the recognizer's own N-best ─")

    context = ConversationContext()
    repair = make_repair(context)

    # A pending question is the strongest context there is.
    context.set_pending_confirmation("Should I close all your windows?")
    outcome = repair.repair("yet", alternatives=[{"text": "yet", "confidence": 0.4},
                                                 {"text": "yes", "confidence": 0.3}])
    check("an outstanding question promotes the answer",
          outcome.changed and outcome.text == "yes", repr(outcome))
    check("and the reason names the evidence",
          outcome.reason == "alternative:confirmation", outcome.reason)

    # The recognizer's own first choice wins whenever it already makes sense.
    outcome = repair.repair("no", alternatives=[{"text": "no", "confidence": 0.4},
                                                {"text": "yes", "confidence": 0.9}])
    check("a plausible top reading is never overridden",
          not outcome.changed and outcome.text == "no", repr(outcome))
    context.set_pending_confirmation("")

    # A control command the recognizer offered.
    outcome = repair.repair("stoop", alternatives=[{"text": "stoop", "confidence": 0.3},
                                                   {"text": "stop", "confidence": 0.25}])
    check("a control command is promoted when nothing else fits",
          outcome.changed and outcome.text == "stop", repr(outcome))

    # A command header from the injected vocabulary.
    outcome = repair.repair("oakland chrome",
                            alternatives=[{"text": "oakland chrome", "confidence": 0.2},
                                          {"text": "open chrome", "confidence": 0.19}])
    check("a command shape is promoted", outcome.changed and outcome.text == "open chrome",
          repr(outcome))

    # A target the user just referred to.
    context.note_automation(action="open", target="spotify")
    outcome = repair.repair("spot a fee",
                            alternatives=[{"text": "spot a fee", "confidence": 0.2},
                                          {"text": "close spotify", "confidence": 0.1}])
    check("a recently used target is promoted", outcome.changed, repr(outcome))

    # Refusals.
    outcome = repair.repair("banana", alternatives=[{"text": "banana", "confidence": 0.2}])
    check("a single alternative is never re-ranked", not outcome.changed, repr(outcome))
    outcome = repair.repair("banana", alternatives=[{"text": "banana", "confidence": 0.2},
                                                    {"text": "bandana", "confidence": 0.1}])
    check("an implausible alternative is not promoted just for existing",
          not outcome.changed, repr(outcome))

    # Rank matters: the recognizer's ordering is evidence too.
    context.set_pending_confirmation("Shall I go ahead?")
    outcome = repair.repair("zzz", alternatives=[{"text": "zzz", "confidence": 0.2},
                                                 {"text": "yes", "confidence": 0.1},
                                                 {"text": "no", "confidence": 0.05}])
    check("among equally plausible alternatives the recognizer's order wins",
          outcome.text == "yes", repr(outcome))
    context.set_pending_confirmation("")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                4. RULE 2 — AND EVERYTHING IT REFUSES                   │
# └────────────────────────────────────────────────────────────────────────┘

def section_repair_refuses():
    print_system("\n── Repair rule 2: what it refuses to do ──────────────────")

    context = ConversationContext()
    repair = make_repair(context)

    # THE guard. "exist" and "exit" are one edit apart and share a phonetic key.
    check("'exist' and 'exit' really are confusable",
          phonetic_key("exist") == phonetic_key("exit") or
          edit_distance("exist", "exit") <= 1)
    for word in ("exist", "exits", "excite"):
        outcome = repair.repair(word, confidence=0.05)
        check(f"'{word}' is never repaired into a shutdown",
              not outcome.changed or "exit" not in outcome.text.split(), repr(outcome))

    # No dictionary anywhere in the module. Asserted STRUCTURALLY, over the parsed module
    # rather than over its text: the prose in the docstring says the words "great" and
    # "quit" precisely in order to state that no such mapping exists, and a grep for them
    # would fail on the very sentence promising they are absent.
    source = open(os.path.join(project_root, "src", "kayra", "input",
                               "transcript_repair.py"), encoding="utf-8").read()
    import ast
    word_maps = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)
                 and isinstance(v, ast.Constant) and isinstance(v.value, str)]
        # A word->word mapping is one whose keys are WORDS. `_CODES` maps single letters to
        # digit strings, which is a phonetic alphabet, not a correction table.
        if any(len(k.value) > 1 and v.value.isalpha() for k, v in pairs):
            word_maps.append([(k.value, v.value) for k, v in pairs][:5])
    check("the module contains no word-to-word replacement table",
          not word_maps, str(word_maps))
    check("its only string mapping is the phonetic alphabet",
          "_CODES" in source and all(len(k) == 1 for k in
                                     __import__("kayra.input.transcript_repair",
                                                fromlist=["_CODES"])._CODES))
    check("it never calls a model",
          "llm" not in source.lower() and "classify_intent" not in source)
    check("it does not import the intelligence layer",
          "kayra.intelligence" not in source)
    check("it does not import the automation layer",
          "kayra.automation" not in source)

    # Every refusal condition, one at a time.
    long_text = "what is the weather in exist today please"
    check("a long utterance is never phonetically repaired",
          not repair.repair(long_text, confidence=0.01).changed)
    check(f"the word limit is {MAX_PHONETIC_WORDS}", MAX_PHONETIC_WORDS <= 3)

    check("a confident reading is left alone",
          not repair.repair("crome", confidence=CONFIDENT_ENOUGH + 0.05).changed)

    check("a word that already means something here is left alone",
          not repair.repair("battery", confidence=0.1).changed)

    check("a word too short to be distinctive is left alone",
          not repair.repair("uh", confidence=0.1).changed)

    check("an empty transcript produces an empty result",
          repair.repair("").text == "" and not repair.repair("").changed)

    # Ambiguity is a refusal, not a coin toss.
    ambiguous = TranscriptRepair(context=ConversationContext(),
                                 vocabulary=("mute", "mate"))
    outcome = ambiguous.repair("moot", confidence=0.1)
    check("two equally close candidates means no repair", not outcome.changed, repr(outcome))

    # A repair DOES happen when every guard is satisfied — otherwise the stage is dead code.
    context = ConversationContext()
    context.note_automation(action="open", target="spotify")
    willing = make_repair(context)
    outcome = willing.repair("spotifi", confidence=0.1)
    check("a genuine sound-alike of a live target IS repaired",
          outcome.changed and outcome.text == "spotify", repr(outcome))
    check("and it says why", outcome.reason.startswith("phonetic:"), outcome.reason)

    # Every result is explainable.
    check("an unchanged result reports itself as unchanged",
          isinstance(outcome, RepairResult) and repair.repair("hello there").changed is False)
    check("the stage keeps counters", set(repair.describe()["stats"]) ==
          {"seen", "reranked", "phonetic", "unchanged"})

    # One stage per process, and injecting vocabulary updates it rather than forking it.
    first = get_transcript_repair(vocabulary=["open chrome"])
    second = get_transcript_repair(vocabulary=["close window"])
    check("the accessor returns one shared stage", first is second)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       5. CONTEXT DRIVES REPAIR                         │
# └────────────────────────────────────────────────────────────────────────┘

def section_context_drives_repair():
    print_system("\n── The context is what makes repair conservative ─────────")

    empty = ConversationContext()
    stage = make_repair(empty, vocabulary=())
    alternatives = [{"text": "yet", "confidence": 0.3}, {"text": "yes", "confidence": 0.2}]
    check("with no context at all, nothing is promoted",
          not stage.repair("yet", alternatives=alternatives).changed)

    empty.set_pending_confirmation("Shall I?")
    check("the SAME utterance is repaired once a question is outstanding",
          stage.repair("yet", alternatives=alternatives).changed)
    empty.set_pending_confirmation("")
    check("and stops being repaired once it is answered",
          not stage.repair("yet", alternatives=alternatives).changed)

    # The plausible vocabulary genuinely changes with the conversation.
    context = ConversationContext()
    stage = make_repair(context)
    before = stage.plausible_terms()
    context.note_user_turn("tell me about the kubernetes migration")
    context.note_automation(action="open", target="grafana")
    after = stage.plausible_terms()
    check("plausible terms grow with the conversation", len(after) > len(before))
    check("a topic word becomes plausible", "kubernetes" in after)
    check("a referenced target becomes plausible", "grafana" in after)
    check("an unrelated word never does", "badminton" not in after)

    # Cost: this runs between the user finishing a word and Kayra acting on it.
    context = ConversationContext()
    context.note_user_turn("open chrome and check the deployment dashboard")
    stage = make_repair(context)
    started = time.perf_counter()
    for _ in range(2000):
        stage.repair("open chrome", alternatives=[{"text": "open chrome", "confidence": 0.9}])
    micros = (time.perf_counter() - started) / 2000 * 1e6
    check(f"the common (unchanged) path costs under 500us ({micros:.1f}us measured)",
          micros < 500.0)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    6. THE RECOGNITION PAGE CONTRACT                    │
# └────────────────────────────────────────────────────────────────────────┘

def section_page():
    print_system("\n── The recognition page ──────────────────────────────────")

    import kayra.input.speech_to_text as stt

    page = stt.html_code
    script = re.search(r"<script>(.*?)</script>", page, re.S)
    check("the page has a script block", script is not None)
    js = script.group(1) if script else ""

    # ── stage 1: capture ──
    for constraint in ("echoCancellation", "noiseSuppression", "autoGainControl",
                       "channelCount"):
        check(f"the capture requests {constraint}", f"{constraint}: " in js)
    check("the granted settings are read back from the live track",
          "getSettings()" in js and "kayraAudioSettings" in js)
    check("a refused microphone is recorded rather than swallowed",
          "kayraAudioError" in js)

    # ── stage 3: VAD ──
    check("a voice-activity detector exists", "startVoiceActivityDetection" in js)
    check("it uses an analyser rather than a fixed threshold",
          "createAnalyser" in js and "getFloatTimeDomainData" in js)
    check("the noise floor is learned, not constant", "noiseFloor" in js)
    check("the microphone is NEVER routed to the speakers",
          "connect(audioCtx.destination)" not in js and ".destination" not in js)
    check("the threshold rises while Kayra is audible",
          "vadEchoMargin" in js and "kayraSpeaking ? tuning.vadEchoMargin" in js)
    check("the floor is not learned from Kayra's own voice",
          "else if (!window.kayraSpeaking)" in js)

    # ── endpointing ──
    check("endpointing needs BOTH recognizer silence and room silence",
          "recognizerQuiet && roomQuiet" in js)
    check("it degrades to recognizer-only when there is no VAD",
          "window.kayraVad.ready ? (now - lastVoiceMs) : sinceResult" in js)
    check("uncommitted words extend the wait rather than being cut",
          "interimGraceMs" in js)
    check("a short committed command is endpointed faster",
          "fastEndpointMs" in js)
    check("nothing waits forever", "maxWaitMs" in js and "hardTimeout" in js)

    # ── the truncation bug this replaced ──
    check("an utterance that never finalized is still delivered",
          "if (!text && uncommitted)" in js)
    check("and is flagged so the repair stage can distrust it",
          "uncommitted:" in js)

    # ── stage 4: N-best ──
    check("the recognizer is asked for alternatives", "maxAlternatives" in js)
    check("alternatives are carried with the utterance", "alternatives: alternatives" in js)
    check("confidence is carried too", "confidence:" in js)
    check("the segment list is bounded", "MAX_SEGMENTS" in js)
    check("the queue is still bounded", "MAX_QUEUE" in js)

    # ── the engine's side of the contract ──
    engine = stt.SpeechToTextEngine.__dict__
    for name in ("_alternatives", "_note_capture", "audio_pipeline_report"):
        check(f"the engine exposes {name}", name in engine)

    check("alternatives are only offered for a single committed segment",
          stt.SpeechToTextEngine._alternatives(
              {"segments": [{"alternatives": [{"text": "a", "confidence": 0.5}]}]}) ==
          [{"text": "a", "confidence": 0.5}])
    check("a multi-segment utterance yields none",
          stt.SpeechToTextEngine._alternatives(
              {"segments": [{"alternatives": [{"text": "a"}]},
                            {"alternatives": [{"text": "b"}]}]}) == [])
    check("an empty item yields none",
          stt.SpeechToTextEngine._alternatives({}) == [])
    check("blank alternatives are dropped",
          stt.SpeechToTextEngine._alternatives(
              {"segments": [{"alternatives": [{"text": "  "}, {"text": "ok"}]}]}) ==
          [{"text": "ok", "confidence": None}])

    tuning = stt._capture_tuning()
    check("the tuning is complete",
          set(tuning) == {"fastEndpointMs", "interimGraceMs", "maxWaitMs", "vadHangoverMs",
                          "vadMargin", "vadEchoMargin", "vadFloorMin", "vadIntervalMs",
                          "maxAlternatives"})
    check("the echo margin is stricter than the ordinary one",
          tuning["vadEchoMargin"] > tuning["vadMargin"])
    check("more than one alternative is requested", tuning["maxAlternatives"] > 1)

    saved = os.environ.get("STT_VAD_HANGOVER_MS")
    try:
        os.environ["STT_VAD_HANGOVER_MS"] = "999999"
        from kayra.core.config import reset_cache
        reset_cache()
        check("an out-of-range tuning value is clamped",
              stt._capture_tuning()["vadHangoverMs"] == 3000)
        os.environ["STT_VAD_HANGOVER_MS"] = "not a number"
        reset_cache()
        check("an unparseable tuning value falls back to its default",
              stt._capture_tuning()["vadHangoverMs"] == 500)
    finally:
        if saved is None:
            os.environ.pop("STT_VAD_HANGOVER_MS", None)
        else:
            os.environ["STT_VAD_HANGOVER_MS"] = saved
        from kayra.core.config import reset_cache
        reset_cache()

    # The page is served over a trustworthy origin, which is what makes any of stage 1 real.
    check("the page is still served from a loopback origin, not a data: URL",
          "_PageServer" in dir(stt) and "127.0.0.1" in
          open(os.path.join(project_root, "src", "kayra", "input", "speech_to_text.py"),
               encoding="utf-8").read())


# ┌────────────────────────────────────────────────────────────────────────┐
# │                      7. WIRING INTO THE TURN LOOP                      │
# └────────────────────────────────────────────────────────────────────────┘

def section_wiring():
    print_system("\n── Wiring ────────────────────────────────────────────────")

    app_source = open(os.path.join(project_root, "src", "kayra", "app.py"),
                      encoding="utf-8").read()
    check("Listen runs the repair stage", "_repair_transcript(result)" in app_source)
    check("repair runs BEFORE the control interpreter",
          app_source.index("_repair_transcript(result)") <
          app_source.index("control = classify_control(user_input)"))
    check("a repair is announced on the console", "[TRANSCRIPT]" in app_source)
    check("the turn loop keeps the context current",
          "CONTEXT.note_user_turn(user_input)" in app_source and
          "CONTEXT.note_intent(" in app_source)
    check("a pending confirmation reaches the context",
          "CONTEXT.set_pending_confirmation(" in app_source)
    check("automation targets reach the context", "CONTEXT.note_automation(" in app_source)
    check("the capture pipeline is reported at boot",
          "_report_audio_pipeline" in app_source)
    # `getUserMedia` is asynchronous, so a single read at boot reports "unavailable" every
    # time. The report waits, and waits OFF the boot path so cold start is untouched.
    check("the report waits for the device instead of racing it",
          "AUDIO_REPORT_TIMEOUT_S" in app_source)
    check("and it waits on its own thread, not on the boot path",
          'name="kayra-audio-report"' in app_source)

    session_source = open(os.path.join(project_root, "src", "kayra", "ui", "session.py"),
                          encoding="utf-8").read()
    check("the UI keeps the same context current",
          "_context.note_user_turn(" in session_source and
          "_context.note_intent(" in session_source)
    check("the UI uses the process-wide context, not its own",
          "get_conversation_context()" in session_source)

    repair_source = open(os.path.join(project_root, "src", "kayra", "input",
                                      "transcript_repair.py"), encoding="utf-8").read()
    check("the repair stage takes its vocabulary by injection",
          "def set_vocabulary" in repair_source)
    check("and app.py injects the classifier's own tokens",
          'getattr(engine, "funcs"' in app_source)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                RUNNER                                  │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_banner("KAYRA CAPTURE PIPELINE DIAGNOSTIC",
                 "Conversation context, transcript repair & the recognition page")
    section_context()
    section_phonetics()
    section_rerank()
    section_repair_refuses()
    section_context_drives_repair()
    section_page()
    section_wiring()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All capture pipeline checks passed.")
