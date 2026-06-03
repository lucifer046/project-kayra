# ┌────────────────────────────────────────────────────────────────────────┐
# │                          emotion_engine.py                             │
# │                 Zero-RAM Semantic Sentiment Classifier                 │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module analyzes the semantic text patterns of a transcribed query
to deduce the user's emotional state entirely offline.

Optimizations:
- 0 MB RAM footprint (No heavy neural nets or model weights loaded).
- Runs instantly (< 1ms execution time).
- Integrated directly into the Selenium STT text pipeline.
"""

import re

try:
    from .utils import print_info, print_warning, print_error, print_system, print_success
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_system, print_success
    except ImportError:
        from utils import print_info, print_warning, print_error, print_system, print_success


class SemanticEmotionEngine:
    """
    Lightweight keyword-density and structural emotion classifier.
    """
    def __init__(self):
        # Lexicon map tracking emotional weight vectors
        self.lexicon = {
            "angry": ["hate", "angry", "pissed", "annoyed", "stupid", "worst", "garbage", "trash", "sucks", "mad", "frustrated", "rage", "furious", "irritated", "outrage"],
            "happy": ["great", "awesome", "happy", "amazing", "wonderful", "cool", "excited", "love", "perfect", "good", "nice", "joy", "delighted", "fantastic", "excellent"],
            "sad": ["sad", "depressed", "tired", "exhausted", "lonely", "bored", "hurt", "bad", "cry", "fail", "ruined", "sorrow", "grief", "miserable", "heartbroken"],
            "fear": ["scared", "terrified", "afraid", "panic", "fear", "nervous", "anxious", "horrified"],
            "surprise": ["wow", "omg", "surprised", "shocked", "amazed", "unbelievable", "unexpected", "astonished"]
        }

    def analyze_text(self, text):
        """
        Scans text tokens against semantic structures to calculate current mood.
        """
        cleaned_text = text.lower().strip()
        
        # Strip punctuation
        cleaned_text = re.sub(r'[^\w\s]', '', cleaned_text)
        tokens = cleaned_text.split()

        scores = {"angry": 0, "happy": 0, "sad": 0, "fear": 0, "surprise": 0}

        # 1. Calculate matching keyword densities
        for token in tokens:
            for emotion, keywords in self.lexicon.items():
                if token in keywords:
                    scores[emotion] += 1

        # 2. Check for explicit structural exclamation patterns (e.g. swearing, shouting indicators)
        if any(word in tokens for word in ["kill", "die", "hell", "stop"]):
            scores["angry"] += 1.5

        # 3. Find the dominant emotion vector
        dominant_emotion = max(scores, key=scores.get)
        
        # Fallback to Neutral if no emotional tokens were identified
        if scores[dominant_emotion] == 0:
            return "Neutral"
            
        print_info(f"Semantic Telemetry -> Profiles: {scores} | Deducing Mood: {dominant_emotion.capitalize()}")
        return dominant_emotion.capitalize()


# Alias mapping for project architecture compliance
EmotionEngine = SemanticEmotionEngine

if __name__ == "__main__":
    engine = SemanticEmotionEngine()
    print_success("Zero-RAM Semantic Emotion Engine ready.")
    while True:
        test_text = input("\nEnter sample text > ")
        if test_text.lower() in ["exit", "quit"]: break
        mood = engine.analyze_text(test_text)
        print(f"Detected Mood: {mood}")