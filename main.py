# ┌────────────────────────────────────────────────────────────────────────┐
# │                            main.py                                     │
# │                 Kayra Master Orchestrator Node                │
# └────────────────────────────────────────────────────────────────────────┘
"""
The Central Nervous System of the AI.
This module loops infinitely, capturing voice/text input, routing it through
the Decision-Making Model (DMM), and concurrently dispatching tasks to the
Chatbot, Real-Time Search, Deep Research, Hardware Automation, and TTS matrices.
"""

import os
import sys
import signal
import asyncio
from dotenv import dotenv_values, load_dotenv

# Reconfigure stdout/stderr to support UTF-8 characters on Windows legacy consoles
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in path for absolute importing across directories
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CORE IMPORTS                                │
# └────────────────────────────────────────────────────────────────────────┘

# 1. Brain (LLM Engine)
try:
    from modules.llm_engine import CentralizedLLMEngine
except ImportError as e:
    print(f"Fatal Error: Could not locate llm_engine.py. {e}")
    sys.exit(1)

# 2. UI & Logging Utilities
try:
    from modules.utils import print_banner, print_system, print_info, print_error, print_success, print_warning, console
except ImportError:
    print("Warning: utils.py not found. Running with standard print statements.")
    print_banner = print_system = print_info = print_error = print_success = print_warning = print
    class ConsoleMock:
        def print(self, *args, **kwargs): print(*args)
        def input(self, prompt): return input(prompt)
    console = ConsoleMock()

# 3. Action Modules
try:
    from modules.chatbot import Chatbot
except ImportError:
    def Chatbot(q): print_error("Chatbot module is offline.")

try:
    from modules.real_time_search import RealTimeSearchEngine
except ImportError:
    def RealTimeSearchEngine(q): print_error("Real-Time Search is offline.")

try:
    from modules.deep_research import DeepResearchEngine
except ImportError:
    def DeepResearchEngine(topic): print_error("Deep Research Engine is offline.")

try:
    from modules.automation_windows import Automation
except ImportError:
    try:
        from modules.automation_windows import translate_and_execute as Automation
    except ImportError:
        async def Automation(cmds): print_error("Automation Engine is offline.")

try:
    from modules.emotion_engine import EmotionEngine
    emotion_engine = EmotionEngine()
    EMOTION_ENABLED = True
except ImportError:
    emotion_engine = None
    EMOTION_ENABLED = False

# 4. Ears (Speech-to-Text integration)
try:
    from modules.speech_to_text import SpeechToTextEngine
    AUDIO_ENABLED = True
except ImportError:
    AUDIO_ENABLED = False

def Listen():
    """Captures audio from STT with intelligent barge-in support."""
    if AUDIO_ENABLED and 'stt_engine' in globals() and stt_engine is not None:
        
        while True:
            # Block until we hear a sentence
            user_input = stt_engine.listen_and_transcribe()
            
            if not user_input:
                return ""
                
            # If the TTS is currently speaking, we must determine if the STT heard an echo or a barge-in
            if 'tts_engine' in globals() and tts_engine and tts_engine.is_playing:
                import difflib
                
                clean_input = user_input.lower().strip()
                last_spoken = tts_engine.last_spoken_text
                
                # Check for explicit interruption commands first
                barge_commands = ["stop", "wait", "shut up", "pause", "hold on", "kayra stop"]
                if any(clean_input.startswith(cmd) for cmd in barge_commands):
                    print_system("[BARGE-IN DETECTED] Interrupting TTS playback...")
                    tts_engine.stop()
                    return user_input
                
                # Otherwise, check if what it heard is just an echo of what it's saying
                # Compare similarity ratio
                similarity = difflib.SequenceMatcher(None, clean_input, last_spoken).ratio()
                
                # If similarity is high, it's an echo -> discard and keep listening
                # Or if the input is a direct substring of the spoken text
                if similarity > 0.4 or clean_input in last_spoken:
                    continue 
                
                # If similarity is very low and it's not a stop command, it's the user interrupting with a new question!
                print_system("[BARGE-IN DETECTED] Interrupting TTS playback...")
                tts_engine.stop()
                return user_input
                
            # If TTS is not playing, return the input normally
            return user_input
            
    return console.input("\n[bold cyan]User >[/bold cyan] ").strip()

# 5. Mouth (Text-to-Speech integration)
try:
    from modules.text_to_speech import TextToSpeechEngine
    TTS_ENABLED = True
except ImportError:
    TTS_ENABLED = False


# ┌────────────────────────────────────────────────────────────────────────┐
# │                             INITIALIZATION                             │
# └────────────────────────────────────────────────────────────────────────┘

load_dotenv(os.path.join(project_root, ".env"))
env_vars = dotenv_values(os.path.join(project_root, ".env")) or {}
assistant_name = env_vars.get("ASSISTANT_NAME", "Kayra").strip()

# Boot the Centralized Brain
engine = CentralizedLLMEngine()

# Boot the Vocal Matrix FIRST so she can speak the boot sequence
if TTS_ENABLED:
    print_info("Loading Kokoro-ONNX Vocal Matrix...")
    tts_engine = TextToSpeechEngine()
    tts_engine.speak("Loading vocal matrix.")
else:
    print_warning("Text-to-Speech module not found. Assistant will be muted.")
    tts_engine = None

# Speak and Print the LLM Engine initialization line by line
engine.run_boot_sequence(tts_engine)

# Boot the Audio Listen Matrix
if AUDIO_ENABLED:
    print_info("Mounting Headless Chrome STT Web Matrix...")
    stt_engine = SpeechToTextEngine()
else:
    stt_engine = None


# ┌────────────────────────────────────────────────────────────────────────┐
# │                        SHUTDOWN SIGNAL HANDLER                         │
# └────────────────────────────────────────────────────────────────────────┘

def _force_shutdown(signum=None, frame=None):
    """
    Instant hard-shutdown handler registered for SIGINT (Ctrl+C) and SIGTERM.
    Kills all Chrome / ChromeDriver child processes spawned by the STT engine
    immediately so they never linger as zombies in the background.
    """
    # 1. Clean Selenium driver session first
    if AUDIO_ENABLED and stt_engine:
        try:
            stt_engine.shutdown()
        except BaseException:
            pass

    # 2. Force-kill any remaining chrome/chromedriver children at the OS level
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
        pass  # psutil not installed - graceful degradation

    print_system("System shutdown complete.")
    os._exit(0)


# Register for both Ctrl+C (SIGINT) and kill (SIGTERM)
signal.signal(signal.SIGINT,  _force_shutdown)
signal.signal(signal.SIGTERM, _force_shutdown)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                             TASK ROUTER                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Execute_Task(intent_array, original_query):
    """
    Takes the parsed intent array from the DMM and routes it to the correct modules.
    Groups hardware automation tasks together to execute them concurrently, 
    and passes AI responses to the TTS engine.
    """
    automation_commands = []

    for task in intent_array:
        task_lower = task.strip().lower()

        # 1. Exit Protocol
        if task_lower == "exit":
            shutdown_msg = f"Initiating shutdown sequence for {assistant_name}. Goodbye!"
            print_system(shutdown_msg)
            if TTS_ENABLED:
                await asyncio.to_thread(tts_engine.speak, "Shutting down. Goodbye.")
            
            # Use the robust force-shutdown handler to kill all processes instantly
            _force_shutdown()

        # 2. General Conversation (Knowledge, Math, Logic)
        elif task_lower.startswith("general "):
            response = await asyncio.to_thread(Chatbot, original_query, tts_engine if TTS_ENABLED else None)

        # 3. Real-Time Web Search (Live RAG)
        elif task_lower.startswith("realtime "):
            response = await asyncio.to_thread(RealTimeSearchEngine, original_query)
            if TTS_ENABLED and response:
                await asyncio.to_thread(tts_engine.speak, response)

        # 4. Autonomous Deep Research
        elif task_lower.startswith("deep research "):
            topic = task.replace("deep research", "", 1).strip()
            if TTS_ENABLED:
                await asyncio.to_thread(tts_engine.speak, "Initiating deep research protocol. This may take a few minutes.")
            
            await asyncio.to_thread(DeepResearchEngine, topic)
            
            if TTS_ENABLED:
                await asyncio.to_thread(tts_engine.speak, "Deep research complete. The report has been saved to your system.")

        # 5. Hardware & System Automation (Grouped)
        else:
            automation_commands.append(task.strip())

    # 6. Execute all grouped automation commands concurrently
    if automation_commands:
        print_info(f"Dispatching hardware automation tasks: {automation_commands}")
        # Optionally announce automation execution
        # if TTS_ENABLED:
        #     await asyncio.to_thread(tts_engine.speak, "Executing system commands.")
        await Automation(automation_commands)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                             MASTER LOOP                                │
# └────────────────────────────────────────────────────────────────────────┘

async def Main_Loop():
    """
    The infinite listening and routing loop.
    """
    print_banner(f"{assistant_name.upper()} SYSTEM ONLINE", "Master Orchestrator Node Active")
    
    if AUDIO_ENABLED:
        print_success("Microphone arrays hot. Faster-Whisper/Web-Speech Recognition ONLINE.")
    else:
        print_warning("SpeechToText module not detected. Defaulting to Keyboard Input Mode.")

    if TTS_ENABLED:
        await asyncio.to_thread(tts_engine.speak, f"{assistant_name} systems online and ready.")

    while True:
        try:
            try:
                if AUDIO_ENABLED:
                    console.print("\n[bold cyan]Listening...[/bold cyan]")
            except ValueError:
                os._exit(1) # Terminal died
            
            # 1. Capture Input
            user_input = await asyncio.to_thread(Listen)
            
            if not user_input or not user_input.strip():
                continue
                
            if AUDIO_ENABLED:
                print_info(f"Transcribed Input: '{user_input}'")

            if EMOTION_ENABLED:
                detected_mood = emotion_engine.analyze_text(user_input)

            # 2. Feed text into the Decision Making Model (DMM)
            try:
                console.print("[dim yellow]Analyzing semantic intent...[/dim yellow]")
            except ValueError:
                os._exit(1)
            
            dmm_commands = await asyncio.to_thread(engine.classify_intent, user_input)
            
            try:
                console.print(f"[bold magenta]System Trace ->[/bold magenta] {dmm_commands}")
            except ValueError:
                os._exit(1)

            # 3. Dispatch to Execution Router
            await Execute_Task(dmm_commands, user_input)
            
        except KeyboardInterrupt:
            try:
                console.print()
            except Exception:
                pass
            print_system("Manual interrupt detected. Halting main execution loop.")
            _force_shutdown()
        except Exception as e:
            print_error(f"Critical failure in Master Loop: {e}")

# ┌────────────────────────────────────────────────────────────────────────┐
# │                               BOOT LOGIC                               │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(Main_Loop())
    except KeyboardInterrupt:
        print_system("System shutdown complete.")
        _force_shutdown()
