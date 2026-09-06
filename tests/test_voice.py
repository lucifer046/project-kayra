# ┌────────────────────────────────────────────────────────────────────────┐
# │                            test_voice.py                               │
# │                     Offline Voice Synthesis Sandbox                    │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_voice.py - Interactive testing sandbox for offline Kokoro TTS.
Enables real-time command-line voice synthesis testing.
"""

import os
import sys

# Append the project root folder to the Python path to allow importing the modules package
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules import TextToSpeechEngine
from modules.utils import print_banner, print_info, print_error, print_system, print_success, console

def main():
    print_banner("KAYRA TTS PLAYGROUND", "Interactive Offline Voice Synthesis Sandbox")
    
    try:
        with console.status("[bold cyan]Initializing offline premium Kokoro-ONNX voice engine...[/bold cyan]"):
            tts = TextToSpeechEngine()
    except Exception as e:
        print_error(f"Failed to initialize voice engine: {e}")
        sys.exit(1)
        
    print_success("Voice Engine initialization successful!")
    print_system("Type any text below and press ENTER to synthesize speech. Type 'exit' or 'quit' to quit.\n")
    
    # Friendly startup greeting
    tts.speak("Voice matrix active. Type anything, and I will speak it for you.")

    while True:
        try:
            # Let's style the prompt beautifully
            user_text = console.input("[bold magenta]TTS Input >[/bold magenta] ").strip()
            if not user_text:
                continue
            if user_text.lower() in ["exit", "quit"]:
                # blocking=True: speak() queues asynchronously now, and the process
                # exits on the next line — without this the farewell is cut off.
                tts.speak("Deactivating speech systems. Goodbye.", blocking=True)
                break
            
            tts.speak(user_text)
        except KeyboardInterrupt:
            console.print()
            print_system("Exiting voice playground. Systems offline.")
            break
        except Exception as e:
            print_error(f"Failed to generate speech: {e}")

if __name__ == "__main__":
    main()
