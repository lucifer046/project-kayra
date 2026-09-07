# ┌────────────────────────────────────────────────────────────────────────┐
# │                            test_engine.py                              │
# │                     LLM Engine Integration Test Suite                  │
# └────────────────────────────────────────────────────────────────────────┘
"""
test_engine.py - Integration tests for the Centralized LLM Engine.
Verifies DMM intent classification and real-time streaming token generation.
"""

import os
import sys
import time

# Reconfigure stdout/stderr to support UTF-8 characters on Windows
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Append the project root folder to the Python path to allow importing the modules package
# The package lives under src/; put it on the path so the suite runs without installing.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from kayra.intelligence.llm_engine import CentralizedLLMEngine
from kayra.utils import (
    print_info,
    print_warning,
    print_error,
    print_critical,
    print_system,
    print_success,
    print_banner,
    print_section,
    console
)

def test_run():
    print_banner("LLM ENGINE INTEGRATION TEST", "Verifying Decision Classification & Live Chat Streaming")

    # 1. Initialize the Engine
    try:
        with console.status("[bold cyan]Initializing Centralized LLM Engine...[/bold cyan]"):
            engine = CentralizedLLMEngine()
    except Exception as e:
        print_critical(f"Failed to initialize engine instance: {e}")
        return

    print_section("Active Target Setup")
    print_info(f"Network Status -> Is Online Mode? [bold highlight]{engine.is_online}[/bold highlight]")

    # 2. Test Layer 1: Decision Making Model (DMM / Classification)
    print_section("TEST 1/2: Intent Classification (DMM)")
    test_queries = [
        "What is the weather like in New Delhi today?",
        "Can you help me write a quick python sorting algorithm?",
        "Run a deep research pass on sodium ion battery scalability"
    ]

    for q in test_queries:
        console.print(f"\n[bold highlight]Sending Query:[/] [italic text]'{q}'[/]")
        start = time.time()
        try:
            with console.status("[bold cyan]Classifying intent using Decision Making Model...[/bold cyan]"):
                intent_result = engine.classify_intent(q)
            elapsed = time.time() - start
            print_success(f"DMM Response Array: [bold green]{intent_result}[/] [dim](Took {elapsed:.2f}s)[/dim]")
        except Exception as e:
            print_error(f"Error in DMM Phase: {e}")

    # 3. Test Layer 2: Chat Stream (Token Generation)
    print_section("TEST 2/2: Chat Streaming Generation")
    
    test_messages = [
        {"role": "system", "content": "You are a helpful assistant. Keep your response under 15 words."},
        {"role": "user", "content": "Say hello and give me one word of motivation."}
    ]

    console.print("\n[bold highlight]Streaming Output Live:[/] ", end="")
    start_stream = time.time()
    chunks_received = 0
    
    try:
        # Loop through the stream generator
        for chunk in engine.generate_chat_stream(test_messages):
            console.print(f"[italic green]{chunk}[/italic green]", end="")
            chunks_received += 1
        
        elapsed_stream = time.time() - start_stream
        console.print()
        print_success(f"Stream finished successfully! Received [bold highlight]{chunks_received}[/] chunks in [bold cyan]{elapsed_stream:.2f}s[/bold cyan].")
        
    except Exception as e:
        console.print()
        print_error(f"Error in Streaming Phase: {e}")

    print_section("TEST NODE RUN COMPLETE")

if __name__ == "__main__":
    test_run()