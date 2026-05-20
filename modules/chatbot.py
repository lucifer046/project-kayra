# ===========================================================================================================
#                                 chatbot.py (Conversational Memory & Persistence Engine)
# ===========================================================================================================
# This module implements the main conversational memory structure for the assistant.
#
# Core Frameworks & Architectures:
# 1. Dual-Tier Memory Hierarchy: Features a sliding short-term RAM window of the last 6 dialogues
#    coupled with a persistent long-term storage database context baseline.
# 2. Transactional Persistence: Employs an atomic writing strategy (via secondary backup mirrors) 
#    to prevent history JSON file corruptions during sudden host shutdowns.
# 3. Temporal Environmental Calibration: Injects real-time local date/time metrics dynamically.
# ===========================================================================================================

import os 
import datetime
import json
import shutil 
from dotenv import dotenv_values

# Robust imports supporting relative paths across all execution contexts
# Fallbacks are configured to handle standalone execution vs imported module contexts cleanly.
try:
    from .llm_engine import CentralizedLLMEngine
except ImportError:
    try:
        from modules.llm_engine import CentralizedLLMEngine
    except ImportError:
        from llm_engine import CentralizedLLMEngine

try:
    from .utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
except ImportError:
    try:
        from modules.utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
    except ImportError:
        from utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console


# -------------------------------------------------------------------------------------------------------
#                                         Configuration
# -------------------------------------------------------------------------------------------------------

# Load environment configuration parameters from active profile
env_vars = dotenv_values(".env")
assistant_name = env_vars.get("ASSISTANT_NAME", "KYRA")

# Access the centralized mode-switching infrastructure broker (offline Kokoro / online cloud endpoints)
engine = CentralizedLLMEngine()

# Secure Long-term data persistence pathways
# Database holds permanent long-term memory registers across application runs.
DB_FILE = r"data\conversation.json"
BACKUP_FILE = r"data\conversation_backup.json"

# -------------------------------------------------------------------------------------------------------
#                                         Memory Management Layer
# -------------------------------------------------------------------------------------------------------
def AnswerModifier(Answer):
    """
    Cleans structural whitespace anomalies out of the response string.
    Removes empty vertical spaces and breaks to maximize text density on the terminal layout.
    """
    lines = Answer.split("\n")
    non_empty_lines = [line for line in lines if line.strip()]
    return '\n'.join(non_empty_lines)

def load_memory():
    """
    Loads historical message layers from local persistent storage.
    
    Data Recovery Safeguard:
        Uses a transactional schema. If the primary index is missing or corrupted
        due to sudden system halts, it intercepts the FileNotFoundError/JSONDecodeError
        and attempts to restore data state using the rolling secondary backup copy.

    Returns:
        list: A list of dictionaries representing the long-term conversational history.
              Returns an empty list if no valid database or backup exists.
    """
    # Enforce database folder structure presence
    if not os.path.exists("data"):
        os.makedirs("data", exist_ok=True)
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        try:
            with open(BACKUP_FILE, "r") as f:
                data = json.load(f)
            print_warning("Primary index compromised. Restored data from rolling backup.")
            return data
        except:
            # Fallback to an empty schema if both primary and backups are unreadable
            return []
    
def save_memory(memory_list):
    """
    Persists data context tables to disk using an atomic transaction technique.
    
    Pipeline Isolation Strategy:
        1. Writes the fresh context list to the 'BACKUP_FILE' path first.
        2. Performs a transactional copy (via shutil.copy) from backup to the main 'DB_FILE'.
        This prevents file corruption: if the system terminates midway during step 1, 
        the original primary DB file remains uncorrupted.

    Args:
        memory_list (list): The updated long-term memory context array to be written to disk.

    Returns:
        bool: True if the atomic write and copy succeeded, False otherwise.
    """
    try:
        with open(BACKUP_FILE, "w") as f:
            json.dump(memory_list, f, indent=4)
        shutil.copy(BACKUP_FILE, DB_FILE)
        return True
    except Exception as e:
        print_error(f"Persistent storage transaction failed: {e}")
        return False


# -------------------------------------------------------------------------------------------------------
#                                         Memory Space Initialization
# -------------------------------------------------------------------------------------------------------

# Long-term data tables pulled from disk. Contains selectively saved/remembered dialogue frames.
permanent_memory = load_memory()  

# Volatile processing RAM buffer. Acts as a short-term sliding context window for the current session.
session_memory = []              

def RealTimeInformation():
    """
    Compiles live host processing time parameters to inject into the neural system message.
    This dynamically updates the model's temporal awareness on every message transaction.
    """
    current_date_time = datetime.datetime.now()
    data = f"Current Time: {current_date_time.strftime('%I:%M %p')}\n"
    data += f"Day: {current_date_time.strftime('%A')}, Date: {current_date_time.strftime('%d %B %Y')}"
    return data

# -------------------------------------------------------------------------------------------------------
#                                         Main Interaction Pipeline
# -------------------------------------------------------------------------------------------------------

def Chatbot(query):
    """
    Orchestrates the conversational memory tree window, structures payload contexts,
    and returns token response generation patterns natively via the central engine pipeline.
    
    Memory Context Hierarchy Injection Sequence:
        1. Renders the dynamic System Identity Prompt + Temporal Environment metrics.
        2. Appends historical Long-Term context frames (loaded dynamically from disk).
        3. Appends the Short-Term volatile sliding window session context (bounded to the last 6 messages).
        4. Appends the active target User Query.

    Args:
        query (str): The raw text input spoken or typed by the user.

    Returns:
        str: The full, normalized text response generated by the LLM pipeline.
    """
    global permanent_memory, session_memory

    try:
        # 1. Fetch current identity alignment conditions (forces strict behavior directives)
        identity_prompt = engine.get_identity_prompt()

        # Compile base system payload context
        api_messages = [
            {"role": "system", "content": identity_prompt + "\n\n" + RealTimeInformation()}
        ]

        # 2. Append permanent storage context tracking frames (if any exist)
        if len(permanent_memory) > 0:
            for msg in permanent_memory:
                api_messages.append(msg)
        
        # 3. Append volatile sliding window session layers (bounded to the last 6 iterations to protect token context limit)
        recent_session = session_memory[-6:]
        for msg in recent_session:
            api_messages.append(msg)
        
        # 4. Attach raw target user query block
        api_messages.append({"role": "user", "content": query})

        response_text = ""
        console.print("\n[bold white]Streaming Response Live:[/bold white] ", end="")

        # 5. Interface with the auto-switching engine streaming chunk generator channels
        for chunk in engine.generate_chat_stream(api_messages):
            console.print(chunk, end="", style="italic green")
            response_text += chunk

        console.print("\n")

        # 6. Normalize string layouts by removing redundant newline separations
        response_text = AnswerModifier(response_text)

        # 7. Index original input string and pure string response to the sliding short-term cache
        session_memory.append({"role": "user", "content": query})
        session_memory.append({"role": "assistant", "content": response_text})

        # 8. Evaluation loop processing explicit long-term indexing criteria commands.
        # If the user explicitly asks to memorize this exchange, we permanently write it to our local JSON database.
        triggers = ["store this", "remember this", "save this", "memorize this", "note this"]

        if any(trigger in query.lower() for trigger in triggers):
            permanent_memory.append({"role": "user", "content": query})
            permanent_memory.append({"role": "assistant", "content": response_text})

            # Execute transactional commit to disk
            if save_memory(permanent_memory):
                print_success(f"Secure context verified. Stored in {assistant_name} structural database.")
        
        return response_text


    except Exception as e:
        print_error(f"Pipeline failure processing active transaction: {e}")
        return ""

# -------------------------------------------------------------------------------------------------------
#                                         Diagnostic Test Runtime Block
# -------------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    # Boot the chatbot playground with the unified cyberpunk banner and config notifications
    print_banner("OPTIMUS CHATBOT ENGINE", f"Interactive {assistant_name} Conversational Sandbox")
    print_success(f"Conversational Node Online. Saved Memory Index Items: {len(permanent_memory)}")
    
    while True:
        try:
            # Capture user query utilizing rich-enabled premium terminal input prompt
            user_query = console.input("[bold magenta]User >[/bold magenta] ").strip()
            
            if not user_query.strip():
                continue

            if user_query.lower() in ["exit", "quit", "bye"]:
                print_system("Closing modular context workspace. Systems offline.")
                break
            
            Chatbot(user_query)

        except KeyboardInterrupt:
            # Handle terminal interrupts gracefully
            console.print()
            print_system("Terminal Halt Event Intercepted. Shutting down chatbot.")
            break