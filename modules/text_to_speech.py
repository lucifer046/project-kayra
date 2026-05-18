# ===========================================================================================================
#                                 text_to_speech.py (Offline TTS Speech Synthesis Engine)
# ===========================================================================================================
# This module implements a premium, ultra-lightweight, 100% offline Text-to-Speech (TTS) system.
#
# Core Frameworks & Architectures:
# 1. Kokoro-82M ONNX Model: An extremely fast and premium voice model running fully optimized on the CPU,
#    saving 100% of your GPU VRAM for LLM inference (e.g., local Ollama / transformers pipelines).
# 2. Synchronous Sentence Generation: Avoids Windows audio driver buffer-underrun and clipping glitches by
#    generating full-sentence waveforms synchronously instead of streaming microscopic audio chunks.
# 3. Dynamic Environment Configuration: Loads voice profiles directly from your `.env` configuration file.
# 4. Anti-Truncation Buffering: Disables aggressive post-speech trimming and appends a digital silence buffer
#    to ensure physical Digital-to-Analog Converters (DACs) fully flush trailing words (like "it for you").
# ===========================================================================================================

import os 
import sys
import sounddevice as sd 
from kokoro_onnx import Kokoro
from dotenv import load_dotenv


class TextToSpeechEngine:
    """
    Core Text-to-Speech class encapsulating the Kokoro-ONNX runtime,
    automatic file paths detection, voice configurations, and speaker driver playback.
    """
    
    def __init__(self, model_filename="kokoro.onnx", voices_filename="voices.bin"):
        """
        Initializes the Kokoro v1.0 8-bit quantized model and loads configuration parameters.
        
        Parameters:
            model_filename (str): The filename of the quantized ONNX model.
            voices_filename (str): The filename of the binary voice pack.
        """
        # --- Robust Path Resolution Tree ---
        # We determine the absolute path of the project root directory relative to this script.
        # This guarantees that the engine initializes successfully whether run directly,
        # from the root folder, or imported inside subfolders.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Resolve paths robustly checking both the current directory and the central models directory.
        model_path = os.path.join(project_root, "models", model_filename)
        voices_path = os.path.join(project_root, "models", voices_filename)

        # Fallback to local files if they are not in the models directory
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
            # Check if files exist directly in the Current Working Directory (CWD)
            if os.path.exists(model_filename) and os.path.exists(voices_filename):
                model_path = os.path.abspath(model_filename)
                voices_path = os.path.abspath(voices_filename)
            else:
                # Check the exact directory of this script (modules/ sibling folder)
                sibling_model = os.path.join(os.path.dirname(os.path.abspath(__file__)), model_filename)
                sibling_voices = os.path.join(os.path.dirname(os.path.abspath(__file__)), voices_filename)
                if os.path.exists(sibling_model) and os.path.exists(sibling_voices):
                    model_path = sibling_model
                    voices_path = sibling_voices
                else:
                    # Final fallback: Treat inputs as direct absolute paths if they exist
                    if os.path.exists(model_filename) and os.path.exists(voices_filename):
                        model_path = model_filename
                        voices_path = voices_filename

        # Final existence verification: Halt execution cleanly if asset files are missing
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
            print(f"[Error] Missing Kokoro assets! Ensure '{model_filename}' and '{voices_filename}' "
                  f"are available in the 'models/' subdirectory.")
            print(f"Looked in: \n  - {model_path}\n  - {voices_path}")
            sys.exit(1)

        print(f"[TTS Engine] Loading model from: {model_path}")
        print(f"[TTS Engine] Loading voices from: {voices_path}")

        # Initialize the ONNX model wrapper utilizing the determined absolute paths
        self.onnx = Kokoro(model_path, voices_path)

        # Load environment variables from the project `.env` file
        load_dotenv()

        # --- Voice Configuration & Selection ---
        # The v1.0 binary pack contains 26 high-fidelity, premium studio voices.
        # Premium American English Female options: "af_bella" (default), "af_sarah", "af_heart"
        # Premium American English Male options:   "am_adam" (deep, masculine), "am_echo" (friendly)
        #
        # Reads the ASSISTANT_VOICE key from `.env`, defaulting to "am_adam" (masculine) if unset.
        self.voice = os.getenv("ASSISTANT_VOICE", "am_adam")
        print(f"[TTS Engine] Configured assistant voice: '{self.voice}'")
        
        # Kokoro is trained to output ultra-clear, natural 24kHz audio
        self.sample_rate = 24000 

    def speak(self, text):
        """
        Synthesizes text into full-sentence speech and plays it directly over your physical speakers.
        
        Design Notes:
            1. Synchronous Generation: CPU inference is extremely fast (~100-200ms for a full sentence). 
               By generating the full audio array at once, we bypass the stuttering, clicks, and dropouts
               that happen when queuing microscopic audio streams on Windows audio drivers.
            2. Anti-Truncation (trim=False): The model wrapper's silent trimmer is highly aggressive and 
               often clips the final syllables/words (e.g. "it for you"). Disabling it preserves full sentence data.
            3. Hardware Flush Padding: Appends a 0.25-second digital silence tail to the waveform. This ensures 
               hardware Digital-to-Analog Converter (DAC) output lines have time to fully play and settle 
               before the audio device stops playback.
        
        Parameters:
            text (str): The plain text sentence to synthesize and speak.
        """
        if not text.strip():
            return
        
        print(f"\n[OPTIMUS Speaking]: {text}")

        try:
            import numpy as np
            
            # Step 1: Synthesize the full sentence in a single high-fidelity array (trim=False preserves ends)
            audio_samples, sample_rate = self.onnx.create(
                text, 
                voice=self.voice, 
                speed=1.1, # conversational 1.1x speed
                lang="en-us",
                trim=False
            )
            
            # Step 2: Create a 0.25-second silent pad to protect trailing audio from hardware truncation
            padding_length = int(0.25 * sample_rate)
            silence_padding = np.zeros(padding_length, dtype=np.float32)
            
            # Step 3: Concatenate the main speech waveform with the silent tail
            buffered_samples = np.concatenate([audio_samples, silence_padding])
            
            # Step 4: Stream the combined waveform directly to the physical sound driver
            sd.play(buffered_samples, sample_rate)
            sd.wait() # Block the execution thread until the audio finishes playing
        except Exception as e:
            print(f"[TTS Engine Error] Failed to generate or play speech: {e}")


# ===========================================================================================================
#                                  Backward Compatibility Class Aliases
# ===========================================================================================================
# These mappings ensure that any legacy imports or alternate spellings (e.g., in testing blocks or
# third-party wrappers) resolve cleanly to the modern TextToSpeechEngine class without raising NameErrors.
LiveOfflineTTS = TextToSpeechEngine
LiveOffileTTS = TextToSpeechEngine
OfflineTTS = TextToSpeechEngine


# ===========================================================================================================
#                                         Main Script Test Entrypoint
# ===========================================================================================================
if __name__ == "__main__":
    # Instantiate the modern engine under its professional class name
    tts = TextToSpeechEngine()
    
    # Run a test speech synthesis sequence
    tts.speak(
        "Voice matrix active. Type anything, and I will speak it for you."
    )