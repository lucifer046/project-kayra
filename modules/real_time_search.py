# ┌────────────────────────────────────────────────────────────────────────┐
# │                          real_time_search.py                           │
# │     Real-Time Search & Retrieval-Augmented Generation (RAG) Engine     │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements the real-time search subsystem for the KAYRA assistant.
It performs web searches via DuckDuckGo (using auto-switching HTML/text backends),
augments the LLM system prompt with live retrieved document contexts, and 
orchestrates conversational streaming with support for dual-tier memory management.
"""

import os
from dotenv import dotenv_values

# Fallback block to safely import DDGS from ddgs or duckduckgo_search packages
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# Robust relative path imports supporting standalone and package-level execution
try:
    from .llm_engine import CentralizedLLMEngine
except ImportError:
    try:
        from modules.llm_engine import CentralizedLLMEngine
    except ImportError:
        from llm_engine import CentralizedLLMEngine

try:
    from .utils import (
        print_banner, print_info, print_success, print_warning, print_error, print_system, console,
        load_conversation_memory, save_conversation_memory, answer_modifier, real_time_info,
        SentenceStreamer,
    )
except ImportError:
    try:
        from modules.utils import (
            print_banner, print_info, print_success, print_warning, print_error, print_system, console,
            load_conversation_memory, save_conversation_memory, answer_modifier, real_time_info,
            SentenceStreamer,
        )
    except ImportError:
        from utils import (
            print_banner, print_info, print_success, print_warning, print_error, print_system, console,
            load_conversation_memory, save_conversation_memory, answer_modifier, real_time_info,
            SentenceStreamer,
        )

# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

# Dynamically calculate project root directory to ensure .env is discovered reliably
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if __file__ else "."
env_vars = dotenv_values(os.path.join(root, ".env")) or {}

assistant_name = env_vars.get("ASSISTANT_NAME", "").strip()
if not assistant_name:
    assistant_name = "Kayra"

# Centralized LLM orchestrator handling model routing and streaming
engine = CentralizedLLMEngine()

# Memory persistence, response cleanup, and temporal-context helpers now live in modules/utils.py
# (previously duplicated near-verbatim across chatbot.py / real_time_search.py).
AnswerModifier = answer_modifier
RealTimeInformation = real_time_info

# ┌────────────────────────────────────────────────────────────────────────┐
# │                         WEB SEARCH SUBSYSTEM                           │
# └────────────────────────────────────────────────────────────────────────┘

def WebSearch(query):
    """
    Executes a zero-cost, unauthenticated search query on DuckDuckGo.
    Extracts and structures the top 5 relevant documents as live context.

    Parameters:
        query (str): The search query phrase.

    Returns:
        str: A formatted block containing titles, content snippets, and URLs.
             Returns None if search results are completely empty or fail.
    """
    try:
        # DDGS context manager guarantees TCP/HTTP socket cleanup on termination
        with DDGS() as ddgs:
            # DuckDuckGo's standard API ('auto' backend) can hit rate limits or block empty search strings.
            # We execute the default backend search first.
            results = list(ddgs.text(query, max_results=5))
            
            # If rate-limited or blocked (returning empty), fallback to the scraper HTML backend
            if not results:
                results = list(ddgs.text(query, backend="html", max_results=5))

        if not results:
            return None

        # Build a highly structured context template to ground the LLM's response
        formatted_results = f"The live web search results for '{query}' are:\n[START OF SEARCH DATA]\n"
        for idx, result in enumerate(results, 1):
            title = result.get("title", "Untitled Document")
            body = result.get("body", 'No snippet text available.')
            link = result.get('href', 'No link available.')
            formatted_results += f"Document [{idx}]:\nTitle: {title}\nContent: {body}\nLink: {link}\n\n"
        formatted_results += "[END OF SEARCH DATA]"

        return formatted_results
    except Exception as e:
        print_warning(f"Web scraping pipeline execution failed: {e}")
        return None
    
# ┌────────────────────────────────────────────────────────────────────────┐
# │                     MEMORY SPACE INITIALIZATION                        │
# └────────────────────────────────────────────────────────────────────────┘

# Long-term persistent memory loaded from database
permanent_memory = load_conversation_memory()
# Short-term volatile RAM cache serving as a sliding session context
session_memory = []

# ┌────────────────────────────────────────────────────────────────────────┐
# │                       MAIN INTERACTION PIPELINE                        │
# └────────────────────────────────────────────────────────────────────────┘

def RealTimeSearchEngine(query, mood=None, tts_engine=None):
    """
    Orchestrates the live Web Search RAG loop:
    1. Dispatches search terms to the unauthenticated DuckDuckGo scraper.
    2. Constructs a temporary system context template injected with retrieved documentation.
    3. Merges long-term persistent registers and short-term sliding context buffers.
    4. Handles real-time live streaming of answers on stdout (and to the voice, if given).
    5. Saves conversational frames to sliding memory and long-term stores (if triggered).

    Args:
        query (str): The user's spoken/typed question.
        mood (str): Optional detected mood used to steer tone.
        tts_engine: Optional TTS engine. When supplied, sentences are spoken as they
            stream out of the model rather than being buffered and read aloud only after
            the whole answer is complete - which is what the caller used to do, costing
            the user the entire generation time before hearing a single word.
    """
    global permanent_memory, session_memory

    try:
        # Step 1: Query the Web Scraper
        print_info(f"Polling active web networks: '{query}...")
        search_context = WebSearch(query)

        # Step 2: Inject search results into the prompt context payload
        if search_context:
            # The old payload said only "answer based ONLY on the context, and supplement from
            # your own knowledge if it is incomplete". That produced two failure modes worth
            # naming: answers that read like a recital of the snippets ("Document 1 says..."),
            # and silent blending of stale training knowledge into what the user hears as a
            # live answer. The instructions below separate synthesis from sourcing and require
            # the model to say when the evidence is thin or disagrees.
            prompt_payload = f"""
            [Context from Live Web Search]
            {search_context}

            [User Query]
            {query}

            [How to answer]
            You have just looked this up. Explain what you found, the way a knowledgeable person
            would explain it out loud — do not read the search results back.

            1. Answer the actual question first, in your own words. Do not quote or paraphrase
               documents one by one, do not number them, and never say things like "according to
               the search results", "Document 2 states" or "based on the context provided".
            2. Use the retrieved evidence as your source of truth for anything current: dates,
               numbers, prices, names, versions, outcomes, who currently holds a position.
            3. Weigh the evidence. Prefer the more recent and more authoritative documents, and
               ignore snippets that are off-topic, promotional, or clearly outdated — retrieval
               is noisy and some of these results will not be about the question at all.
            4. If the documents disagree on a fact that matters, say so briefly and give the most
               likely answer rather than silently picking one.
            5. If the evidence does not actually cover the question, say plainly what is known and
               what is not. You may add relevant background from your own knowledge, but make the
               distinction audible — "as of my own knowledge" — and never present it as freshly
               retrieved. Never invent a figure, date or source to fill a gap.
            6. Mention a source by name only when it genuinely matters for trust (an official
               announcement, a primary source). Do not read out URLs.
            """
        else:
            print_warning("Network returned empty search tokens. Reverting to frozen parameters.")
            prompt_payload = query

        # Step 3: Compile System Prompt and Temporal Calibration data
        identity_prompt = engine.get_identity_prompt(mood=mood)

        api_messages = [
            {"role": "system", "content": identity_prompt + "\n\n" + RealTimeInformation()}
        ]

        # Step 4: Merge permanent conversation records
        if len(permanent_memory) > 0:
            for msg in permanent_memory:
                api_messages.append(msg)
        
        # Step 5: Merge short-term volatile RAM sliding context window (limited to last 6 entries)
        recent_session = session_memory[-6:]
        for msg in recent_session:
            api_messages.append(msg)
        
        # Step 6: Append the compiled injected payload
        api_messages.append({"role": "user", "content": prompt_payload})

        response_text = ""
        console.print("\n[bold white]Streaming Real-Time Response Live:[/bold white] ", end="")

        streamer = None
        if tts_engine:
            # Capture this turn's token once. Checking the token (rather than the global
            # `interrupted` flag) means nothing else clearing that flag — a proactive
            # suggestion calling begin_turn(), say — can resurrect a response the user
            # already interrupted.
            turn_token = tts_engine.turn_token()
            streamer = SentenceStreamer(
                tts_engine.speak,
                stop_check=lambda: tts_engine.is_cancelled(turn_token),
            )

        # Step 7: Stream generated response token-by-token
        for chunk in engine.generate_chat_stream(api_messages):
            console.print(chunk, end="", style="italic green")
            response_text += chunk
            if streamer:
                streamer.feed(chunk)
                if tts_engine.is_cancelled(turn_token):
                    print_system("Response cancelled by user interruption.")
                    break

        if streamer:
            streamer.flush()

        console.print("\n")

        # Step 8: Clean and format final text
        response_text = AnswerModifier(response_text)

        # Step 9: Store original query (without scraped noise) and output to session RAM
        session_memory.append({"role": "user", "content": query})
        session_memory.append({"role": "assistant", "content": response_text})

        # Step 10: Scan for explicit persistent indexing commands
        triggers = ["store this", "remember this", "save this", "memorize this", "note this"]
        if any(trigger in query.lower() for trigger in triggers):
            permanent_memory.append({"role": "user", "content": query})
            permanent_memory.append({"role": "assistant", "content": response_text})

            if save_conversation_memory(permanent_memory):
                print_success(f"Secure context verified. Stored in {assistant_name} structural database.")

        return response_text

    except Exception as e:
        print_error(f"Pipeline failure inside Real-Time search node execution: {e}")
        return ""

# ┌────────────────────────────────────────────────────────────────────────┐
# │                     DIAGNOSTIC TEST RUNTIME BLOCK                      │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    # Test playground enabling real-time search evaluation and RAG synthesis locally
    print_banner("KAYRA REALTIME SEARCH ENGINE", f"Live RAG Web Synthesis Sandbox")
    print_success(f"Realtime Subsystem Node Online. Free Web Scraping Interface Active.")

    while True:
        try:
            user_query = console.input("[bold cyan]Search >[/bold cyan] ").strip()

            if not user_query:
                continue

            if user_query.lower() in ["exit", "quit", "bye"]:
                print_system("Terminating real-time tracking workspace instance.")
                break

            RealTimeSearchEngine(user_query)

        except KeyboardInterrupt:
            console.print()
            print_system("Terminal Halt Event Intercepted. Shutting down system search.")
            break