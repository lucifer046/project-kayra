# ===========================================================================================================
#                                         modules Package Initialization
# ===========================================================================================================
# This file configures the import boundaries and initializes the project's core packaging system.
# It cleanly exposes the primary components for both Speech-to-Text (STT) and Text-to-Speech (TTS).
#
# Professional API Boundaries:
# - TextToSpeechEngine: Premium, local offline Kokoro TTS Engine.
# - SpeechToTextEngine: Asynchronous continuous browser Speech-to-Text.
# - recognize_speech(): Legacy wrapper function for quick STT transcription.
# ===========================================================================================================

from .text_to_speech import TextToSpeechEngine
from .speech_to_text import SpeechToTextEngine, recognize_speech
from .utils import get_project_root, setup_logger

# -------------------------------------------------------------------------------------------------------
#                                 Legacy Backward-Compatibility Mappings
# -------------------------------------------------------------------------------------------------------
# Exposing historical class names and methods to avoid breaking existing code blocks.
OfflineTTS = TextToSpeechEngine
LiveOfflineTTS = TextToSpeechEngine
LiveOffileTTS = TextToSpeechEngine
OnlineSpeechEngine = SpeechToTextEngine
SpeechRecognition = recognize_speech
