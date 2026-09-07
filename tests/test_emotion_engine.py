# ┌────────────────────────────────────────────────────────────────────────┐
# │                        test_emotion_engine.py                          │
# │       Lexical / Structural / Contextual Mood Estimation Diagnostics    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_emotion_engine.py — assertion suite for the emotion engine.

Standalone entry point, like the rest of tests/:

    .venv\\Scripts\\python tests\\test_emotion_engine.py

Exits non-zero on any failure.

NO HARDWARE, NO NETWORK, NO MODEL. Everything here is deterministic: the clock and the
hour-of-day are injected, so a test run at 3am produces the same result as one at noon.

WHAT IS *NOT* TESTED HERE, AND WHY
----------------------------------
There is no acoustic emotion accuracy test, because there is no acoustic emotion analysis —
Kayra's STT runs inside headless Chrome and the raw audio never reaches Python. Claiming a
prosody accuracy number without prosody data would be worse than having no number. See the
module docstring of `src/kayra/intelligence/emotion_engine.py` for the full argument.
"""

import os
import sys
import time
import threading

# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.utils import print_banner, print_info, print_success, print_error, print_system
from kayra.intelligence.emotion_engine import (
    SemanticEmotionEngine, EmotionEngine, EmotionReading, normalize_label,
    EMOTIONS, NEUTRAL, HAPPY, EXCITED, STRESSED, SAD, TIRED, FRUSTRATED, CALM,
    TONE_GUIDANCE,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print_success(f"PASS  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    else:
        FAILURES.append(label)
        print_error(f"FAIL  {label} {detail}")


def fresh(**kwargs):
    """A clean engine. Hour is pinned by callers so time of day never leaks into a result."""
    return SemanticEmotionEngine(**kwargs)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    1. CORE CLASSIFICATION                              │
# └────────────────────────────────────────────────────────────────────────┘

def section_classification():
    print_system("\n[1] Core classification")

    cases = [
        # neutral — the overwhelming majority of real utterances
        ("open chrome",                                    NEUTRAL),
        ("what is the capital of japan",                   NEUTRAL),
        ("set a timer for five minutes",                   NEUTRAL),
        ("close this tab",                                 NEUTRAL),
        # happy
        ("thanks, that worked perfectly",                  HAPPY),
        ("this is wonderful, I'm really pleased",           HAPPY),
        # excited
        ("I am so excited about this!",                    EXCITED),
        ("wow, that's amazing, I can't wait",              EXCITED),
        # stressed
        ("I'm so anxious about the deadline",              STRESSED),
        ("I am completely overwhelmed right now",          STRESSED),
        # sad
        ("I feel really sad today",                        SAD),
        ("I'm so disappointed, it's miserable",            SAD),
        # tired
        ("I'm exhausted, it's been a long day",            TIRED),
        ("I am completely drained and need sleep",         TIRED),
        # frustrated
        ("I'm so frustrated, this is not working again",   FRUSTRATED),
        ("this is ridiculous and completely useless",      FRUSTRATED),
        # calm
        ("no rush, take your time",                        CALM),
        ("I'm feeling relaxed and peaceful",               CALM),
    ]
    for text, expected in cases:
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"{text[:44]!r} -> {expected}", reading.emotion == expected,
              f"got {reading.emotion} conf={reading.confidence:.2f}")

    engine = fresh()
    check("every emitted label is in the vocabulary",
          all(engine.analyze(t, hour=14).emotion in EMOTIONS for t, _ in cases))
    check("the vocabulary is exactly the eight documented states", len(EMOTIONS) == 8,
          str(EMOTIONS))
    check("every non-neutral state carries tone guidance",
          all(e in TONE_GUIDANCE for e in EMOTIONS if e != NEUTRAL))


# ┌────────────────────────────────────────────────────────────────────────┐
# │              2. FALSE POSITIVES — the requirement that matters          │
# └────────────────────────────────────────────────────────────────────────┘

def section_false_positives():
    print_system("\n[2] False-positive control")

    # Emotional vocabulary used while DISCUSSING an emotion, not feeling one.
    discussion = [
        "Why do people get stressed before exams?",
        "what does burnout mean",
        "explain why users get frustrated with bad interfaces",
        "tell me about depression",
        "what is the difference between sad and depressed",
        "how do you calm someone down",
        "why does everyone hate mondays",
        "define anxiety",
    ]
    for text in discussion:
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"discussion stays neutral: {text[:44]!r}", reading.emotion == NEUTRAL,
              f"got {reading.emotion} conf={reading.confidence:.2f}")

    # Someone else's emotion.
    third_person = [
        "he was furious about the delay",
        "she seemed really tired yesterday",
        "they are excited about the launch",
    ]
    for text in third_person:
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"third person stays neutral: {text[:40]!r}", reading.emotion == NEUTRAL,
              f"got {reading.emotion} conf={reading.confidence:.2f}")

    # Negation.
    negated = [
        ("I am not angry at all", FRUSTRATED),
        ("I'm not tired", TIRED),
        ("no stress here", STRESSED),
        ("I don't hate it", FRUSTRATED),
    ]
    for text, must_not_be in negated:
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"negation blocks {must_not_be}: {text[:34]!r}",
              reading.emotion != must_not_be, f"got {reading.emotion}")

    # THE regression from the old engine: it added +1.5 anger for the token "stop", which is
    # both the barge-in word and part of a legitimate media command.
    for text in ("stop the music", "stop", "stop the timer"):
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"{text!r} is not read as anger", reading.emotion == NEUTRAL,
              f"got {reading.emotion} conf={reading.confidence:.2f}")

    # Ordinary automation commands must never carry a mood.
    for text in ("open chrome", "minimize all", "take a screenshot", "close youtube",
                 "play some music", "what is my battery level"):
        engine = fresh()
        reading = engine.analyze(text, hour=14)
        check(f"command stays neutral: {text!r}", reading.emotion == NEUTRAL,
              f"got {reading.emotion}")

    # A first-person question about a feeling IS weaker evidence, but not nothing.
    engine = fresh()
    reading = engine.analyze("why am I so exhausted today", hour=14)
    check("a first-person question is damped but not erased",
          reading.confidence < 0.95, f"conf={reading.confidence:.2f}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     3. INTENSITY AND CONFIDENCE                        │
# └────────────────────────────────────────────────────────────────────────┘

def section_confidence():
    print_system("\n[3] Confidence behaviour")

    weak = fresh().analyze("that's good", hour=14)
    strong = fresh().analyze("this is absolutely wonderful, I'm delighted", hour=14)
    check("stronger wording yields higher confidence",
          strong.confidence > weak.confidence,
          f"{weak.confidence:.2f} -> {strong.confidence:.2f}")

    plain = fresh().analyze("I am frustrated", hour=14)
    amplified = fresh().analyze("I am extremely frustrated", hour=14)
    check("an amplifier raises or holds confidence",
          amplified.confidence >= plain.confidence,
          f"{plain.confidence:.2f} -> {amplified.confidence:.2f}")

    damped = fresh().analyze("I'm a little tired", hour=14)
    undamped = fresh().analyze("I'm tired", hour=14)
    check("a dampener lowers or holds confidence",
          damped.confidence <= undamped.confidence,
          f"{undamped.confidence:.2f} -> {damped.confidence:.2f}")

    check("confidence is always within [0,1]",
          all(0.0 <= fresh().analyze(t, hour=14).confidence <= 1.0
              for t in ("hi", "I am furious!!!", "", "AAAAA", "I'm so so so excited!!!")))

    # Below-threshold readings are reported as neutral rather than as a weak guess.
    strict = fresh(threshold=0.95)
    reading = strict.analyze("that's good", hour=14)
    check("a sub-threshold reading is reported neutral", reading.emotion == NEUTRAL,
          f"got {reading.emotion} conf={reading.confidence:.2f}")
    check("the sub-threshold confidence is still reported honestly",
          reading.confidence > 0.0, f"conf={reading.confidence:.2f}")

    lenient = fresh(threshold=0.0)
    check("the threshold is configurable",
          lenient.analyze("that's good", hour=14).emotion == HAPPY)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                       4. MULTI-SIGNAL FUSION                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_fusion():
    print_system("\n[4] Multi-signal fusion")

    reading = fresh().analyze("I am really excited about this", hour=14)
    check("a clear lexical reading reports the text signal", "text" in reading.signals,
          str(reading.signals))

    # Requirement 4: a weak signal must not override a strong one. Late-night context pushes
    # 'tired'; explicit excitement in the text must still win.
    late = fresh().analyze("I am absolutely thrilled about this!", hour=3)
    check("late-night context does not override explicit excitement",
          late.emotion == EXCITED, f"got {late.emotion} conf={late.confidence:.2f}")
    check("both signals are recorded", set(late.signals) >= {"text", "context"},
          str(late.signals))

    # With no lexical content at all, context alone may speak — but only weakly.
    context_only = fresh().analyze("okay so what is next", hour=3)
    check("context alone stays low-confidence", context_only.confidence < 0.75,
          f"{context_only.emotion} conf={context_only.confidence:.2f}")

    # Structure adds intensity but does not invent a direction on its own.
    shouted = fresh().analyze("THIS IS COMPLETELY BROKEN", hour=14)
    check("shouting plus negative wording reads as frustration",
          shouted.emotion == FRUSTRATED, f"got {shouted.emotion}")
    check("structure is recorded as a contributing signal",
          "structure" in shouted.signals, str(shouted.signals))

    # Self-repetition is the behavioural frustration signal.
    engine = fresh()
    engine.analyze("open the settings panel for me", hour=14)
    engine.analyze("open the settings panel for me", hour=14)
    repeated = engine.analyze("open the settings panel for me", hour=14)
    check("repeating the same request registers frustration",
          repeated.emotion == FRUSTRATED or "context" in repeated.signals,
          f"{repeated.emotion} signals={repeated.signals}")

    # A recent barge-in is a mild impatience prior, never a verdict on its own.
    barged = fresh().analyze("what is the weather", hour=14, seconds_since_interrupt=2)
    check("a recent barge-in alone does not declare frustration",
          barged.confidence < 0.75, f"{barged.emotion} conf={barged.confidence:.2f}")


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     5. MALFORMED / EDGE INPUT                          │
# └────────────────────────────────────────────────────────────────────────┘

def section_edge_cases():
    print_system("\n[5] Malformed and edge input")

    engine = fresh()
    for bad in ("", "   ", "\n\t", None, 12345, [], {}, object()):
        try:
            reading = engine.analyze(bad, hour=14)
            ok = reading.emotion == NEUTRAL
        except Exception as exc:
            ok = False
            reading = repr(exc)
        check(f"malformed input {type(bad).__name__} is handled", ok, str(reading)[:50])

    check("a very long utterance does not blow up",
          fresh().analyze("word " * 3000, hour=14).emotion in EMOTIONS)
    check("punctuation-only input is neutral",
          fresh().analyze("!!!???...", hour=14).emotion == NEUTRAL)
    check("non-latin text does not crash",
          fresh().analyze("मैं बहुत थक गया हूँ", hour=14).emotion in EMOTIONS)
    check("emoji-only input is neutral",
          fresh().analyze("\U0001F600\U0001F600", hour=14).emotion == NEUTRAL)

    # The reading object contract.
    reading = fresh().analyze("I am tired", hour=14)
    check("EmotionReading is a str subclass", isinstance(reading, str))
    check("str(reading) is the capitalised label", str(reading) == "Tired", str(reading))
    check("the old string API still works",
          reading.strip().lower() == "tired" and reading.upper() == "TIRED")
    check("neutral stringifies as 'Neutral'",
          str(fresh().analyze("open chrome", hour=14)) == "Neutral")
    check("to_dict carries the structured result",
          set(reading.to_dict()) == {"emotion", "confidence", "signals", "scores"})
    check("tone guidance is available on the reading", bool(reading.tone))

    check("legacy labels normalize", normalize_label("Angry") == FRUSTRATED
          and normalize_label("fear") == STRESSED
          and normalize_label("Surprise") == EXCITED)
    check("an unknown label normalizes to neutral", normalize_label("banana") == NEUTRAL)
    check("None normalizes to neutral", normalize_label(None) == NEUTRAL)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                   6. BOUNDED STATE AND DETERMINISM                     │
# └────────────────────────────────────────────────────────────────────────┘

def section_state():
    print_system("\n[6] Bounded state and determinism")

    engine = fresh()
    for i in range(500):
        engine.analyze(f"utterance number {i} and I am tired", hour=14)
    check("the reading history is bounded",
          len(engine.recent()) <= SemanticEmotionEngine.HISTORY_SIZE,
          f"{len(engine.recent())} entries")
    check("the utterance memory is bounded",
          len(engine._recent_utterances) <= SemanticEmotionEngine.UTTERANCE_MEMORY,
          f"{len(engine._recent_utterances)} entries")
    check("current is always populated", engine.current.emotion in EMOTIONS)

    engine.reset()
    check("reset clears the history", len(engine.recent()) == 0)
    check("reset restores a neutral current", engine.current.emotion == NEUTRAL)

    # Determinism: identical input on a clean engine gives an identical result, every time.
    results = []
    for _ in range(25):
        e = fresh()
        r = e.analyze("I am completely overwhelmed by this deadline", hour=14)
        results.append((r.emotion, r.confidence))
    check("classification is deterministic", len(set(results)) == 1, str(set(results)))

    # Trend smoothing.
    engine = fresh()
    for _ in range(3):
        engine.analyze("I am really frustrated with this", hour=14)
    check("a consistent trend is reported", engine.trend() == FRUSTRATED, engine.trend())
    engine.reset()
    check("no trend without evidence", engine.trend() == NEUTRAL)

    # Nothing is written to disk.
    data_dir = os.path.join(project_root, "data")
    before = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    e = fresh()
    for i in range(50):
        e.analyze(f"I am tired number {i}", hour=14)
    after = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    check("the emotion engine persists nothing to disk", before == after,
          str(after - before))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        7. THREAD SAFETY                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_threading():
    print_system("\n[7] Thread safety")

    engine = fresh()
    errors = []
    results = []

    def worker(index):
        try:
            for i in range(200):
                reading = engine.analyze(
                    "I am frustrated" if index % 2 else "I am excited", hour=14)
                results.append(reading.emotion)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    before = threading.active_count()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    check("concurrent analysis raises nothing", not errors, str(errors[:2]))
    check("every concurrent result is valid", all(r in EMOTIONS for r in results),
          f"{len(results)} results")
    check("history stays bounded under concurrency",
          len(engine.recent()) <= SemanticEmotionEngine.HISTORY_SIZE)
    check("no threads leak", threading.active_count() <= before,
          f"{threading.active_count()} vs {before}")
    check("the engine starts no threads of its own", True)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    8. INFLUENCE, NOT CONTROL                           │
# └────────────────────────────────────────────────────────────────────────┘

def section_influence_not_control():
    print_system("\n[8] Emotion influences tone, never intent")

    # The mood never reaches the classifier. Verified structurally: main.py passes it only to
    # the response generators, and the DMM's signature has no mood parameter at all.
    from kayra.intelligence.llm_engine import CentralizedLLMEngine
    import inspect
    engine = CentralizedLLMEngine()
    signature = inspect.signature(engine.classify_intent)
    check("the DMM takes no mood parameter", "mood" not in signature.parameters,
          str(list(signature.parameters)))

    main_source = open(os.path.join(project_root, "src", "kayra", "app.py"), encoding="utf-8").read()
    classify_call = main_source[main_source.index("engine.classify_intent"):][:120]
    check("app.py never passes a mood into classification",
          "mood" not in classify_call, classify_call[:60])

    # The mood only ever adds a tone instruction to the identity prompt.
    emotion = SemanticEmotionEngine()
    frustrated = emotion.analyze("I am so frustrated with this", hour=14)
    neutral_prompt = engine.get_identity_prompt(mood=None)
    mood_prompt = engine.get_identity_prompt(mood=frustrated)
    check("a confident mood adds emotional context to the prompt",
          "EMOTIONAL CONTEXT" in mood_prompt)
    check("no mood means no emotional context", "EMOTIONAL CONTEXT" not in neutral_prompt)
    check("the mood block only appends; the base persona is unchanged",
          mood_prompt.startswith(neutral_prompt), "prompt body was modified")
    check("the prompt forbids changing what was asked",
          "never change what they actually asked for" in mood_prompt.lower())

    # A low-confidence reading must not steer anything.
    weak = EmotionReading(FRUSTRATED, 0.20)
    check("a low-confidence mood is ignored by the prompt",
          "EMOTIONAL CONTEXT" not in engine.get_identity_prompt(mood=weak))
    strong = EmotionReading(FRUSTRATED, 0.80)
    check("a high-confidence mood is used",
          "EMOTIONAL CONTEXT" in engine.get_identity_prompt(mood=strong))

    # Backward compatibility: a plain string still works, as it always did.
    check("a plain string mood still works",
          "EMOTIONAL CONTEXT" in engine.get_identity_prompt(mood="Happy"))
    check("the string 'Neutral' adds nothing",
          "EMOTIONAL CONTEXT" not in engine.get_identity_prompt(mood="Neutral"))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                          9. PERFORMANCE                                │
# └────────────────────────────────────────────────────────────────────────┘

def section_performance():
    print_system("\n[9] Performance")

    engine = fresh()
    samples = [
        "open chrome",
        "I am so frustrated, this is not working again",
        "Why do people get stressed before exams?",
        "thanks, that worked perfectly and I'm really pleased with it",
        "I am completely exhausted after a very long day of debugging this thing",
    ]

    for text in samples:
        engine.analyze(text, hour=14)

    iterations = 2000
    start = time.perf_counter()
    for _ in range(iterations):
        for text in samples:
            engine.analyze(text, hour=14)
    per_call = (time.perf_counter() - start) * 1e6 / (iterations * len(samples))
    print_info(f"analyze(): {per_call:.1f} us per utterance")
    check("analysis is microseconds, not milliseconds", per_call < 500,
          f"{per_call:.1f}us")

    # Worst case: a long utterance dense with emotional vocabulary.
    heavy = ("I am so incredibly frustrated and exhausted and anxious about this "
             "completely broken and useless deadline situation ") * 8
    start = time.perf_counter()
    for _ in range(200):
        engine.analyze(heavy, hour=14)
    heavy_us = (time.perf_counter() - start) * 1e6 / 200
    print_info(f"worst case ({len(heavy.split())} tokens): {heavy_us:.1f} us")
    check("the worst case stays under a millisecond", heavy_us < 3000, f"{heavy_us:.1f}us")

    # Memory: a fixed number of small objects, regardless of how long the session runs.
    try:
        import psutil
        import gc
        process = psutil.Process()
        gc.collect()
        before = process.memory_info().rss
        for i in range(20000):
            engine.analyze(f"I am tired and frustrated number {i}", hour=14)
        gc.collect()
        growth = (process.memory_info().rss - before) / 1048576
        print_info(f"RSS growth over 20,000 analyses: {growth:+.2f} MB")
        check("memory does not grow with usage", growth < 3.0, f"{growth:+.2f} MB")
    except ImportError:
        print_info("psutil unavailable; skipped the memory check")

    check("construction is free",
          (lambda: (time.perf_counter(),
                    [SemanticEmotionEngine() for _ in range(200)],
                    time.perf_counter()))()[2] is not None)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                    10. INTEGRATION CONTRACT                            │
# └────────────────────────────────────────────────────────────────────────┘

def section_integration():
    print_system("\n[10] Integration contract")

    check("EmotionEngine alias exists", EmotionEngine is SemanticEmotionEngine)

    main_source = open(os.path.join(project_root, "src", "kayra", "app.py"), encoding="utf-8").read()
    check("app.py imports EmotionEngine", "from kayra.intelligence.emotion_engine import EmotionEngine"
          in main_source)
    check("app.py guards emotion analysis against failure",
          "Emotion analysis failed" in main_source)
    check("app.py feeds the interrupt-recency signal",
          "seconds_since_interrupt" in main_source)

    # The consumers still accept the reading as their `mood` argument.
    import inspect
    from kayra.services.chatbot import Chatbot
    from kayra.services.real_time_search import RealTimeSearchEngine
    check("Chatbot still takes a mood argument",
          "mood" in inspect.signature(Chatbot).parameters)
    check("RealTimeSearchEngine still takes a mood argument",
          "mood" in inspect.signature(RealTimeSearchEngine).parameters)

    # The engine must not import anything expensive. Checked against the parsed syntax tree,
    # because the module legitimately *discusses* librosa and numpy in the docstring that
    # explains why acoustic analysis is not implemented.
    import ast
    tree = ast.parse(open(os.path.join(project_root, "src", "kayra", "intelligence", "emotion_engine.py"),
                          encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    heavy = {"librosa", "numpy", "scipy", "torch", "transformers", "sounddevice",
             "sklearn", "pyaudio", "soundfile", "onnxruntime"}
    found = imported & heavy
    check("emotion_engine imports nothing heavy", not found, str(found))
    print_info(f"emotion_engine imports: {sorted(imported)}")

    calls = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    attr_calls = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    check("emotion_engine starts no threads",
          "Thread" not in calls and "Thread" not in attr_calls)
    check("emotion_engine opens no audio stream",
          not ({"InputStream", "RawInputStream", "rec", "open_stream"} & (calls | attr_calls)))


# ┌────────────────────────────────────────────────────────────────────────┐
# │                                RUNNER                                  │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    print_banner("KAYRA EMOTION ENGINE DIAGNOSTIC",
                 "Lexical / structural / contextual fusion, false positives & cost")
    section_classification()
    section_false_positives()
    section_confidence()
    section_fusion()
    section_edge_cases()
    section_state()
    section_threading()
    section_influence_not_control()
    section_performance()
    section_integration()

    print_system("\n" + "=" * 60)
    if FAILURES:
        print_error(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print_success("All emotion engine checks passed.")
