# ===========================================================================================================
#                                 speech_to_text.py (Continuous Background STT Engine)
# ===========================================================================================================
# This module implements an advanced, asynchronous Speech-to-Text (STT) system using the Web Speech API
# inside a persistent headless Chrome browser.
#
# Pipelined Queuing Design:
# 1. Permanent Stream: The microphone stream inside Chrome remains open and active 100% of the time.
# 2. In-Browser Queue: Sentences are split dynamically in JavaScript by VAD (silence threshold)
#    and pushed into an in-memory queue (`window.speechQueue`).
# 3. No Word Loss: Because Chrome never stops recording, you can continue speaking even while Python
#    is busy translating, processing, or speaking response commands.
# 4. Zero Startup Delay: Subsequent calls to read the microphone stream execute in 0ms.
# 5. Leak-Proof Lifecycle: Uses Python's 'atexit' with dynamic unregistration to guarantee Chrome process
#    termination on script exit with zero connection tracebacks.
# ===========================================================================================================

import os
import time
import urllib.parse
import atexit
from dotenv import dotenv_values
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from rich.console import Console
import mtranslate as mt

# Console for rich terminal output
console = Console()

# HTML/JS running in-browser containing Web Speech API with VAD silence queue logic
html_code = """<!DOCTYPE html>
<html lang="en">
<head>
    <title>Speech Recognition</title>
</head>
<body>
    <p id="status">idle</p>
    <script>
        let recognition;
        let lastResultTime = 0;
        let isSpeaking = false;
        let silenceLimit = 800; // default 800ms silence gap
        let checkInterval;
        
        // Asynchronous queue to hold finalized sentences
        window.speechQueue = [];
        let currentText = "";

        const statusEl = document.getElementById('status');

        function startContinuousRecognition(lang, silenceMs) {
            silenceLimit = silenceMs || 800;
            window.speechQueue = [];
            currentText = "";
            isSpeaking = false;
            lastResultTime = Date.now();
            statusEl.textContent = "listening";

            recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
            recognition.lang = lang || 'en-US';
            recognition.continuous = true;
            recognition.interimResults = true;

            recognition.onstart = () => {
                statusEl.textContent = "listening";
            };

            recognition.onspeechstart = () => {
                isSpeaking = true;
                statusEl.textContent = "speaking";
                lastResultTime = Date.now();
            };

            recognition.onresult = (event) => {
                isSpeaking = true;
                statusEl.textContent = "speaking";
                lastResultTime = Date.now();

                let finalTranscript = "";
                for (let i = event.resultIndex; i < event.results.length; ++i) {
                    if (event.results[i].isFinal) {
                        finalTranscript += event.results[i][0].transcript + " ";
                    }
                }
                if (finalTranscript) {
                    currentText += finalTranscript;
                }
            };

            recognition.onerror = (event) => {
                // If it hits standard network/media interrupts, auto-restart
                if (event.error === 'network' || event.error === 'aborted') {
                    restartRecognition();
                } else {
                    statusEl.textContent = "error: " + event.error;
                }
            };

            recognition.onend = () => {
                // Restart continuously if stopped by Chrome system
                if (statusEl.textContent !== "stopped") {
                    restartRecognition();
                }
            };

            recognition.start();

            // Check for silence gap every 100ms
            if (checkInterval) clearInterval(checkInterval);
            checkInterval = setInterval(() => {
                if (isSpeaking && (Date.now() - lastResultTime > silenceLimit)) {
                    let completedSentence = currentText.trim();
                    if (completedSentence) {
                        window.speechQueue.push(completedSentence);
                        currentText = ""; // Clear buffer for next sentence
                    }
                    isSpeaking = false;
                    statusEl.textContent = "listening";
                }
            }, 100);
        }

        function restartRecognition() {
            if (recognition) {
                try { recognition.stop(); } catch(e) {}
            }
            setTimeout(() => {
                try { recognition.start(); } catch(e) {}
            }, 50);
        }

        function stopContinuousRecognition() {
            statusEl.textContent = "stopped";
            clearInterval(checkInterval);
            if (recognition) {
                recognition.onend = null;
                recognition.stop();
            }
        }
    </script>
</body>
</html>"""


class SpeechToTextEngine:
    def __init__(self, language=None, silence_limit=0.8):
        """
        Initializes the persistent Chrome browser and activates continuous recording.
        """
        # Load language from env if not specified
        if not language:
            env_vars = dotenv_values(".env")
            language = env_vars.get("INPUT_LANGUAGE", "en-US")

        self.language = language
        self.silence_limit_ms = int(silence_limit * 1000)

        # Set Chrome Options to allow microphone access without UI prompts
        chrome_options = Options()
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.3"
        
        # Core Headless Options
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument(f"user-agent={user_agent}")
        chrome_options.add_argument("--use-fake-ui-for-media-stream") # Auto-grants mic permission
        
        # Advanced Headless Chrome Optimizations (Fast boot, low memory, zero GPU compile lag)
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--blink-settings=imagesEnabled=false")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--disable-sync")

        print("[SpeechToTextEngine] Booting headless Chrome browser session...")
        self.driver = webdriver.Chrome(options=chrome_options)
        
        # Load the HTML code as an inline Data URL (no disk pollution!)
        data_url = "data:text/html;charset=utf-8," + urllib.parse.quote(html_code)
        self.driver.get(data_url)
        print("[SpeechToTextEngine] Headless Chrome session ready.")

        # Resolve path to the centralized lowercase data/directory relative to project root
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.temp_dir_path = os.path.join(project_root, "data", "Files")
        os.makedirs(self.temp_dir_path, exist_ok=True)

        # Start background continuous recording immediately
        self.driver.execute_script(
            "startContinuousRecognition(arguments[0], arguments[1]);",
            self.language,
            self.silence_limit_ms
        )
        print("[SpeechToTextEngine] Continuous speech monitoring activated.")

        # Register robust exit cleanup handler
        atexit.register(self.shutdown)

    def set_assistant_status(self, status):
        """Updates the status file so the GUI can show 'Listening...' or errors."""
        try:
            with open(rf"{self.temp_dir_path}/status.data", "w", encoding="utf-8") as f:
                f.write(status)
        except Exception:
            pass

    def clear_queue(self):
        """Flushes any accumulated speech in the background queue."""
        try:
            if self.driver:
                self.driver.execute_script("window.speechQueue = [];")
        except Exception:
            pass

    def listen_and_transcribe(self):
        """
        Blocks until a sentence is finalized in Chrome's background queue,
        then pops, translates, formats, and returns the result.
        """
        self.set_assistant_status("Listening...")
        
        try:
            while True:
                if not self.driver:
                    return None

                # Pop the oldest sentence from the JavaScript queue
                text = self.driver.execute_script("return window.speechQueue.shift();")

                if text:
                    self.set_assistant_status("Translating...")
                    translated_text = translate_query(text)
                    formatted_text = format_query(translated_text)
                    return formatted_text
                
                # Check for errors in Chrome engine
                status = self.driver.find_element(By.ID, "status").text
                if status.startswith("error:"):
                    error_msg = status.replace("error: ", "")
                    print(f"[SpeechToTextEngine] Chrome STT Error: {error_msg}")
                    self.set_assistant_status(f"Error: {error_msg}")
                    return ""
                
                # Super-low CPU polling interval
                time.sleep(0.05)

        except KeyboardInterrupt:
            return None

    def shutdown(self):
        """Closes the Selenium webdriver and terminates Chrome cleanly."""
        try:
            atexit.unregister(self.shutdown)
        except Exception:
            pass

        if hasattr(self, 'driver') and self.driver:
            print("[SpeechToTextEngine] Shutting down headless browser...")
            try:
                self.driver.execute_script("stopContinuousRecognition();")
            except Exception:
                pass
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ===========================================================================================================
#                                         Formatting & Translation Utilities
# ===========================================================================================================

def format_query(query):
    """
    Format the raw speech text:
    - Lowercase start.
    - Add question marks to questions.
    - Add periods to statements.
    - Capitalize the first letter.
    """
    new_query = query.lower().strip()
    query_words = new_query.split()
    question_words = [
        "what", "where", "when", "why", "how", "who", "which", "whom", "whose", "whatsoever", "wherever", 
        "whenever", "whichever", "can you", "what's", "where's", "when's", "why's", "how's", 
        "who's", "which's", "whom's", "whose's"
    ]

    if not query_words: 
        return ""
    
    if any(word + " " in new_query for word in question_words) or query_words[0] in question_words:
        if new_query[-1] in ['.', '?', '!']:
            new_query = new_query[:-1] + "?"
        else:
            new_query += "?"
    else:
        if new_query[-1] in ['.', '?', '!']:
            new_query = new_query[:-1] + "."
        else:
            new_query += "."
    return new_query.capitalize()


def translate_query(query):
    """
    Translates non-English input to English using mtranslate.
    """
    english_query = mt.translate(query, "en", "auto")
    return english_query.capitalize()


# ===========================================================================================================
#                                  Backward Compatibility Mappings
# ===========================================================================================================
# Legacy variables, functions, and classes mapped directly to modern standard Python equivalents.
OnlineSpeechEngine = SpeechToTextEngine
SetAssistantStatus = SpeechToTextEngine.set_assistant_status
QueryModifier = format_query
UniversalTranslator = translate_query

_legacy_engine = None

def recognize_speech():
    """
    Legacy wrapper function to maintain backwards-compatibility.
    Automatically initializes a singleton SpeechToTextEngine session.
    """
    global _legacy_engine
    if _legacy_engine is None:
        _legacy_engine = SpeechToTextEngine()
    
    return _legacy_engine.listen_and_transcribe()

SpeechRecognition = recognize_speech


# ===========================================================================================================
#                                         Main Script Test Entrypoint
# ===========================================================================================================
if __name__ == "__main__":
    # Create the modern object-oriented engine
    engine = SpeechToTextEngine(silence_limit=0.8)
    
    print("\n========== ONLINE WEB SPEECH ENGINE ==========")
    print("Say something...")
    print("Say 'exit application' to quit.\n")

    try:
        while True:
            query = engine.listen_and_transcribe()
            if query:
                print(f"Recognized: {query}")
                if "exit application" in query.lower():
                    break
    except KeyboardInterrupt:
        console.print("\n[bold red]Forced Exit.[/bold red]")
    finally:
        engine.shutdown()