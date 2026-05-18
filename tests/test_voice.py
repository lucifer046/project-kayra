import os
import sys

# Append the project root folder to the Python path to allow importing the modules package
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules import TextToSpeechEngine

def main():
    print("=" * 60)
    print("           OPTIMUS TTS INTERACTIVE VOICE PLAYGROUND           ")
    print("=" * 60)
    print("Initializing the offline premium Kokoro-ONNX voice engine...")
    
    try:
        tts = TextToSpeechEngine()
    except Exception as e:
        print(f"\n[Error] Failed to initialize voice engine: {e}")
        sys.exit(1)
        
    print("\nInitialization successful!")
    print("Type any text and press ENTER to speak it. Type 'exit' or 'quit' to quit.\n")
    
    # Friendly startup greeting
    tts.speak("Voice matrix active. Type anything, and I will speak it for you.")

    while True:
        try:
            user_text = input("TTS Input > ").strip()
            if not user_text:
                continue
            if user_text.lower() in ["exit", "quit"]:
                tts.speak("Deactivating speech systems. Goodbye.")
                break
            
            tts.speak(user_text)
        except KeyboardInterrupt:
            print("\nExiting voice playground...")
            break
        except Exception as e:
            print(f"[Error] Failed to generate speech: {e}")

if __name__ == "__main__":
    main()
