# ===========================================================================================================
#                                 llm_engine.py (Centralized LLM Engine & Intent Classifier)
# ===========================================================================================================
# This module implements the central orchestrator and fallback intelligence system.
#
# Core Frameworks & Architectures:
# 1. Decision-Making Model (DMM): Classifies raw user inputs into structured system-level execution arrays.
# 2. Local-Cloud Switcher Matrix: Automatically routes queries to cloud engines (Gemini/Cohere) or offline
#    local endpoints (Ollama/LM Studio) based on host availability and environment configurations.
# 3. Stream-Based Generation: Leverages token generators to stream dialogue responses with zero lag.
# ===========================================================================================================

import os
import time
import requests
import cohere
from openai import OpenAI
from dotenv import dotenv_values
try:
    from .utils import print_info, print_warning, print_error, print_system, print_success
except ImportError:
    try:
        from modules.utils import print_info, print_warning, print_error, print_system, print_success
    except ImportError:
        from utils import print_info, print_warning, print_error, print_system, print_success


class CentralizedLLMEngine:
    def __init__(self):
            # Load environment variables
            self.env_vars = dotenv_values(".env")
            
            # Pull model configurations from .env (with safe defaults)
            self.cohere_model = self.env_vars.get("COHERE_DECISION_MODEL", "command-r-plus-08-2024")
            self.gemini_model = self.env_vars.get("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
            self.local_chat_model = self.env_vars.get("LOCAL_CHAT_MODEL", "local-model")
            self.local_decision_model = self.env_vars.get("LOCAL_DECISION_MODEL", "local-model")
            
            force_online = self.env_vars.get("FORCE_ONLINE", "False").lower() == "true"
            
            
            # MODE SELECTION MATRIX
            if force_online:
                print_system("FORCE_ONLINE is active. Bypassing local checks to run Cloud Mode.")
                self.is_online = True
                self.is_local_active = False
            else:
                self.local_base_url = self.env_vars.get("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
                self.is_local_active = self._check_local_server()
                self.is_online = not self.is_local_active

            
            # CLIENT ALLOCATION
            if self.is_online:
                print_info(f"Booting Cloud APIs: Cohere ({self.cohere_model}) & Gemini ({self.gemini_model})")
                self.cohere_client = cohere.Client(api_key=self.env_vars.get("CohereAPIKey"))
                self.online_chat_client = OpenAI(
                    api_key=self.env_vars.get("GEMINI_API_KEY"), 
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
                )
            else:
                print_info(f"Local Server detected at {self.local_base_url}. Running 100% Offline.")
                print_info(f"Using Local Chat: '{self.local_chat_model}' | Local DMM: '{self.local_decision_model}'")
                local_key = self.env_vars.get("LOCAL_API_KEY", "lm-studio")
                self.local_client = OpenAI(base_url=self.local_base_url, api_key=local_key)

            # DMM Configuration (Intent Processing Core)
            self.funcs = [
                "exit", "general", "realtime", "open", "close", "play",
                "generate image", "system", "content", "google search", 
                "youtube search", "reminder", "deep research"
            ]
            
            self.dmm_preamble = """
                You are a very accurate Decision-Making Model, which decides what kind of a query is given to you.
                You will decide whether a query is a 'general' query, a 'realtime' query, or is asking to perform any task or automation like 'open facebook, instagram', 'can you write a application and open it in notepad'
                *** Do not answer any query, just decide what kind of query is given to you. ***
                -> Respond with 'deep research (topic)' if the query explicitly requests to deeply analyze, research, do a deep dive, or write an exhaustive technical report on a specific topic. Example: if the query is 'do deep research on carbon batteries' respond with 'deep research carbon batteries'.
                -> Respond with 'general ( query )' if a query can be answered by a llm model (conversational ai chatbot) and doesn't require any up to date information like if the query is 'who was akbar?' respond with 'general who was akbar?'.
                -> Respond with 'realtime ( query )' if a query can not be answered by a llm model (because they don't have realtime data) and requires up to date information like if the query is 'who is indian prime minister' respond with 'realtime who is indian prime minister', if the query is 'what is today's news?' respond with 'realtime what is today's news?'.
                -> Respond with 'open (application name or website name)' if a query is asking to open any application like 'open facebook'.
                -> Respond with 'close (application name)' if a query is asking to close any application like 'close notepad'.
                -> Respond with 'play (song name)' if a query is asking to play any song like 'play afsanay by ys'.
                -> Respond with 'generate image (image prompt)' if a query is requesting to generate a image with given prompt like 'generate image of a lion'.
                -> Respond with 'reminder (datetime with message)' if a query is requesting to set a reminder.
                -> Respond with 'system (task name)' if a query is asking to mute, unmute, volume up, volume down, etc.
                -> Respond with 'content (topic)' if a query is asking to write any type of content like application, codes, emails or anything else.
                -> Respond with 'google search (topic)' if a query is asking to search a specific topic on google.
                -> Respond with 'youtube search (topic)' if a query is asking to search a specific topic on youtube.
                *** If the query is asking to perform multiple tasks like 'open facebook and close whatsapp' respond with 'open facebook, close whatsapp' ***
                *** If the user is saying goodbye or wants to end the conversation like 'bye jarvis.' respond with 'exit'.***
                *** Respond with 'general (query)' if you can't decide the kind of query or if a query is asking to perform a task which is not mentioned above. ***
                """
            
            self.dmm_chat_history = [
                {"role": "User", "message": "how are you?"},
                {"role": "Chatbot", "message": "general how are you?"},
                {"role": "User", "message": "open chrome and tell me about mahatma gandhi."},
                {"role": "Chatbot", "message": "open chrome, general tell me about mahatma gandhi."},
                {"role": "User", "message": "what is todays date by the way remind me that i have a dancing performance on 5th aug 11:00pm"},
                {"role": "Chatbot", "message": "general what is today's date, reminder 11:00pm 5 aug dancing performance"},
                {"role": "User", "message": "run a deep research query on solid state hydrogen storage vectors"},
                {"role": "Chatbot", "message": "deep research solid state hydrogen storage vectors"},
                {"role": "User", "message": "chat with me."},
                {"role": "Chatbot", "message": "general chat with me."}
            ]

    def get_identity_prompt(self):
        """
        Compiles the system persona instructions for the assistant based on env configuration.
        """
        name = self.env_vars.get("ASSISTANT_NAME", "KYRA").strip()
        gender = self.env_vars.get("ASSISTANT_GENDER", "Female").strip()
        lang = self.env_vars.get("LANGUAGE", "English").strip()
        username = self.env_vars.get("USERNAME", "User").strip()
        
        prompt = (
            f"Hello, I am {username}. You are a highly accurate, advanced, and premium AI chatbot named {name}. "
            f"Your gender profile is {gender}. You must always respond and converse fluently in {lang}. "
            f"You have access to real-time, up-to-date information from the internet.\n\n"
            f"CRITICAL BEHAVIORAL DIRECTIVES:\n"
            f"1. Be extremely concise.\n"
            f"2. Maintain a respectful, professional, and polite tone at all times.\n"
            f"3. Do not tell the time unless explicitly requested.\n"
            f"4. Never provide conversational 'notes' or disclaimers in the output. Simply deliver the precise answer.\n"
            f"5. Under no circumstances should you ever mention your training data, AI architecture, or model limitations."
        )
        return prompt

    def _check_local_server(self):
            """Pings the local model endpoint. Returns True if alive."""
            try:
                response = requests.get(f"{self.local_base_url}/models", timeout=1.5)
                # Both 200 (Success) and 401 (Unauthorized/Auth required) show the server is alive
                return response.status_code in [200, 401]
            except (requests.ConnectionError, requests.Timeout):
                return False
    
    # 1. DECISION MAKING MODEL (DMM)
    def classify_intent(self, prompt: str, retries: int = 0):
        response_text = ""
        try:
            if self.is_online:
                # Cloud Mode: Stream from Cohere Command-R model
                stream = self.cohere_client.chat_stream(
                    model=self.cohere_model,  # <-- Using ENV Variable
                    preamble=self.dmm_preamble,
                    message=prompt,
                    chat_history=self.dmm_chat_history,
                    prompt_truncation='OFF',
                    temperature=0.7
                )
                for event in stream:
                    if event.event_type == "text-generation":
                        response_text += event.text
            else:
                local_messages = [{"role": "system", "content": self.dmm_preamble}]
                for msg in self.dmm_chat_history:
                    role = "user" if msg["role"] == "User" else "assistant"
                    local_messages.append({"role":role, "content":msg['message']})
                local_messages.append({"role": "user", "content": prompt})

                local_response = self.local_client.chat.completions.create(
                    model=self.local_decision_model,  # <-- Using ENV Variable
                    messages=local_messages,
                    temperature=0.7,
                )
                response_text = local_response.choices[0].message.content
            
            response_text = response_text.replace("\n","")
            raw_tasks = [i.strip() for i in response_text.split(",")]

            parsed_task = []
            for task in raw_tasks:
                for func in self.funcs:
                    if task.startswith(func):
                        parsed_task.append(task)
            
            if len(parsed_task) == 0:
                if retries < 3:
                    print_warning(f"Empty token response. Retrying DMM step #{retries + 1}...")
                    return self.classify_intent(prompt=prompt, retries=retries + 1)
                else:
                    return ["general " + prompt]
            return parsed_task
        except cohere.TooManyRequestsError:
            print_error("Rate Limit Reached (10 calls/min). Cooling down for 10 seconds...")
            time.sleep(10)
            return self.classify_intent(prompt=prompt, retries=retries)

        except Exception as e:
            print_error(f"An unexpected DMM exception occurred: {e}")
            return ["general " + prompt]

    # 2. CHAT & SEARCH STREAMING CHUNKS GENERATOR
    def generate_chat_stream(self, api_messages):
        try:
            if self.is_online:
                stream = self.online_chat_client.chat.completions.create(
                    model=self.gemini_model,  # <-- Using ENV Variable
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
            else:
                stream = self.local_client.chat.completions.create(
                    model=self.local_chat_model,  # <-- Using ENV Variable
                    messages=api_messages,
                    temperature=0.7,
                    stream=True,
                )
            for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            yield f"\n[Fatal Engine Connectivity Failure: {e}]"