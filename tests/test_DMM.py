import os
import sys
import time
import msvcrt

# Ensure project root is in path for absolute importing across directories
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from modules.llm_engine import CentralizedLLMEngine
from modules.speech_to_text import SpeechToTextEngine, format_query, translate_query
from modules.utils import print_system, print_info, print_success, print_error, console

def get_hybrid_input(stt_engine):
    """
    Listens to both the microphone (via Chrome STT queue) and the keyboard simultaneously.
    - If you start typing, the console switches to typing mode.
    - If you speak, it captures the speech instantly.
    """
    typed_buffer = ""
    is_typing = False
    
    console.print("\n[dim cyan]Listening (Speak or Type)...[/dim cyan]")
    
    while True:
        # 1. Check for Keyboard Input
        if msvcrt.kbhit():
            if not is_typing:
                is_typing = True
                # Carriage return to overwrite the "Listening..." text with typed text
                print("\r[bold cyan]Typing >[/bold cyan] ", end="", flush=True)
            
            char = msvcrt.getwch()
            if char in ('\r', '\n'):
                print() # Newline on enter
                return typed_buffer.strip()
            elif char == '\b': # Backspace
                if len(typed_buffer) > 0:
                    typed_buffer = typed_buffer[:-1]
                    # Erase character from terminal using backspace trick
                    print("\b \b", end="", flush=True)
            else:
                typed_buffer += char
                print(char, end="", flush=True)
                
        # 2. Check for Voice Input (only if user hasn't started manually typing)
        if not is_typing and stt_engine and stt_engine.driver:
            try:
                # Pop from Chrome's speech queue non-blockingly. Entries are
                # {text, start, end} objects — the capture timestamps are what the main
                # loop uses for echo rejection; this diagnostic only needs the text.
                item = stt_engine._script("return window.speechQueue.shift() || null;")
                if item and item.get("text"):
                    translated = translate_query(item["text"])
                    formatted = format_query(translated)
                    return formatted
            except Exception:
                pass
                
        # Super-low CPU footprint sleep
        time.sleep(0.05)

def run_voice_test():
    print_system("Booting LLM Engine Voice Test Script...")
    
    # 1. Initialize Engine
    engine = CentralizedLLMEngine()
    engine.run_boot_sequence()
    
    # 2. Initialize STT (Microphone)
    print_info("Initializing Speech-to-Text Engine (Chrome)...")
    stt_engine = SpeechToTextEngine()
    print_success("\n=======================================================")
    print_success("TEST READY! Speak into your mic OR start typing.")
    print_success("Say 'exit' or type 'exit' to quit.")
    print_success("=======================================================\n")
    
    try:
        while True:
            # Get input from either keyboard or voice seamlessly
            user_input = get_hybrid_input(stt_engine)
            
            if not user_input:
                continue
                
            print_info(f"Input Received: '{user_input}'")
            
            # Exit condition
            if "exit" in user_input.lower():
                print_system("Exit command recognized. Halting test loop...")
                break
            
            # Step A: Intent Classification
            console.print("[dim yellow]Analyzing intent via DMM...[/dim yellow]")
            dmm_commands = engine.classify_intent(user_input)
            console.print(f"[bold magenta]DMM Trace ->[/bold magenta] {dmm_commands}")
            
    except KeyboardInterrupt:
        print_system("\nCtrl+C detected.")
    finally:
        print_system("Shutting down headless Chrome...")
        try:
            stt_engine.shutdown()
        except Exception:
            pass
            
        # Hard kill just like the main.py script
        try:
            import psutil
            current = psutil.Process(os.getpid())
            for child in current.children(recursive=True):
                if any(name in child.name().lower() for name in ("chrome", "chromedriver")):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
        except Exception:
            pass
            
        print_system("Test script shutdown complete.")
        os._exit(0)

if __name__ == "__main__":
    run_voice_test()
