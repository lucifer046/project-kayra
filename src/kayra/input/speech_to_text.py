# ┌────────────────────────────────────────────────────────────────────────┐
# │                           speech_to_text.py                            │
# │                  Continuous Background STT Engine                      │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements a continuous, background Speech-to-Text (STT) transcription system
using the HTML5 Web Speech API running within a headless Selenium-controlled Chrome instance.
It employs voice activity detection (VAD) to segment spoken audio into discrete sentences
without losing words during processing delays.

Two properties of this engine exist specifically to solve the self-listening / barge-in
problem, and both live in the browser page rather than in Python:

1. **Every utterance carries the wall-clock window it was SPOKEN in** (`start`/`end`, from
   `Date.now()`), not just the moment Python happened to pop it. A sentence is only
   finalized ~800ms after the speaker stops, so "was the assistant talking when this audio
   was captured?" can only be answered with the capture timestamps — comparing against
   "is the assistant talking right now" is off by a full VAD window and is exactly why the
   old echo filter mis-classified echoes as user commands.

2. **Interrupt words are detected on INTERIM results** and published immediately on
   `window.kayraInterrupt`, bypassing the VAD silence timer and the translation round-trip.
   That is what makes "stop" register in ~200ms instead of ~1.2s.

The browser session itself is a managed, single-instance resource. See `SpeechToTextEngine`
for the lifecycle contract: one WebDriver and one browser session per Kayra process,
recovered in place when it dies, and torn down by PID (never by process name) on exit.
"""

import os
import re
import time
import urllib.parse
import atexit
import threading
import mtranslate as mt
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.common.exceptions import JavascriptException, WebDriverException

# psutil drives PID-based process ownership tracking. Optional: without it the engine still
# quits the driver cleanly, it just cannot verify/force-terminate stragglers.
try:
    import psutil
except ImportError:
    psutil = None

# Robust imports supporting relative paths across all execution contexts
from kayra.core.config import env
from kayra.core.paths import data_path
from kayra.input import browsers
from kayra.utils import print_info, print_warning, print_error, print_system, print_success, print_banner, console, now_ms


# Interrupt vocabulary shared with the TTS engine's INTERRUPT_WORDS. Hindi entries are
# included because INPUT_LANGUAGE is frequently 'hi-IN', in which case interim results
# come back in Devanagari and would never match the English list alone.
INTERRUPT_PHRASES = [
    "stop", "wait", "shut up", "pause", "hold on", "quiet", "silence",
    "enough", "cancel", "nevermind", "never mind",
    "रुको", "रुक", "ठहरो", "बस", "चुप",
    # English interrupt words as the hi-IN recognizer transliterates them. Users speak
    # English commands to Kayra while INPUT_LANGUAGE is hi-IN, and Google returns
    # Devanagari for them, which would never match the Latin entries above.
    "स्टॉप", "वेट", "रुको जरा",
]

# Words stripped before matching, so "Kayra, stop please" still reduces to "stop".
INTERRUPT_FILLERS = {"kayra", "please", "just", "ok", "okay", "hey", "yo", "now"}

_INTERRUPT_PHRASE_SET = set(INTERRUPT_PHRASES)
_SINGLE_WORD_INTERRUPTS = {p for p in INTERRUPT_PHRASES if " " not in p}

# ┌────────────────────────────────────────────────────────────────────────┐
# │        IN-BROWSER WEB SPEECH API & VAD SILENCE QUEUING HTML/JS         │
# └────────────────────────────────────────────────────────────────────────┘
# We run this minimal web page inside our headless browser session.
# It configures the Web Speech API (webkitSpeechRecognition) and implements real-time silence detection:
# - Continually listens for speech input.
# - If silence is detected for more than `silenceLimit` ms, the accumulated interim text buffer is finalized.
# - The finalized sentence is appended to `window.speechQueue` WITH its capture timestamps.
# - Interim results are scanned for interrupt words and published instantly on `window.kayraInterrupt`.
# - In case of browser engine interruptions or pauses, it automatically restarts without losing context.
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
        let utteranceStart = 0;  // Date.now() when the current utterance began

        // Asynchronous queue of finalized utterances: {text, start, end}
        window.speechQueue = [];
        // Set the instant an interim result looks like an interruption: {text, at}
        window.kayraInterrupt = null;
        // Interrupt vocabulary, injected from Python so both sides share one list.
        window.kayraInterruptWords = [];
        window.kayraInterruptFillers = [];
        window.kayraSingleWordInterrupts = [];
        // Recognition-backend health. `webkitSpeechRecognition` exists in every Chromium
        // derivative, but only builds carrying a speech backend can actually transcribe:
        // Chrome has Google's key, Edge has Microsoft's, Brave deliberately ships neither.
        // A backendless browser fails with error 'network' the moment it starts, and the
        // handler below used to treat that as transient and restart — an infinite loop in
        // which the assistant looks alive and never hears a word. These counters let Python
        // tell "no backend in this browser" apart from "this machine is briefly offline".
        window.kayraNetworkErrors = 0;      // consecutive, reset by any successful result
        window.kayraEverRecognized = false; // has this session EVER produced a result?
        window.kayraRecognitionDead = false;
        const MAX_NETWORK_ERRORS = 3;
        let currentText = "";

        const statusEl = document.getElementById('status');

        // Cap on pending utterances. The queue only drains when the main loop is back in
        // Listen(), so during a long spoken response it grows unattended. 32 entries is far
        // more backlog than any real interaction produces; beyond that the oldest are stale
        // anyway and holding them just leaks renderer memory.
        const MAX_QUEUE = 32;

        // Keep a processed microphone stream open for the whole session. This attaches
        // Chrome's audio processing module (echo cancellation / noise suppression / AGC) to
        // the capture device, reducing how much of the assistant's own speaker output ever
        // reaches the recognizer.
        //
        // This ONLY works because the page is served from http://127.0.0.1 (a trustworthy
        // origin, hence a secure context). On the `data:` URL this page used to load from,
        // `navigator.mediaDevices` is undefined and this function was silently a no-op.
        // Echo rejection still does not *depend* on it — the timestamp gate in main.py is
        // the guarantee — but this removes much of the echo at the source.
        function primeProcessedMicrophone() {
            try {
                navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true
                    }
                }).then(function (stream) {
                    window.kayraMicStream = stream;
                }).catch(function () { /* non-fatal */ });
            } catch (e) { /* non-fatal */ }
        }

        function looksLikeInterrupt(text) {
            const raw = (text || "").toLowerCase().replace(/[.,!?;:]/g, " ").trim();
            if (!raw) return false;
            // EXACT match on the whole utterance, filler words removed. A prefix test
            // would fire on "stop the music", which is a real command, not a barge-in.
            const words = raw.split(/\\s+/).filter(function (w) {
                return w && window.kayraInterruptFillers.indexOf(w) === -1;
            });
            if (!words.length || words.length > 3) return false;
            if (window.kayraInterruptWords.indexOf(words.join(" ")) !== -1) return true;
            // "stop stop stop" is still a stop.
            return words.every(function (w) {
                return window.kayraSingleWordInterrupts.indexOf(w) !== -1;
            });
        }

        function startContinuousRecognition(lang, silenceMs, interruptWords, fillers) {
            silenceLimit = silenceMs || 800;
            window.speechQueue = [];
            window.kayraInterrupt = null;
            window.kayraInterruptWords = interruptWords || [];
            window.kayraInterruptFillers = fillers || [];
            window.kayraSingleWordInterrupts = window.kayraInterruptWords.filter(function (w) {
                return w.indexOf(" ") === -1;
            });
            currentText = "";
            isSpeaking = false;
            window.kayraNetworkErrors = 0;
            window.kayraEverRecognized = false;
            window.kayraRecognitionDead = false;
            lastResultTime = Date.now();
            utteranceStart = Date.now();
            statusEl.textContent = "listening";

            primeProcessedMicrophone();

            recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
            recognition.lang = lang || 'en-US';
            recognition.continuous = true;
            recognition.interimResults = true;

            recognition.onstart = () => {
                statusEl.textContent = "listening";
            };

            recognition.onspeechstart = () => {
                if (!isSpeaking) { utteranceStart = Date.now(); }
                isSpeaking = true;
                statusEl.textContent = "speaking";
                lastResultTime = Date.now();
            };

            recognition.onresult = (event) => {
                if (!isSpeaking) { utteranceStart = Date.now(); }
                isSpeaking = true;
                statusEl.textContent = "speaking";
                lastResultTime = Date.now();
                // Proof the backend is reachable: clears any accumulated network errors so a
                // genuine transient blip mid-session never trips the dead-backend detector.
                window.kayraEverRecognized = true;
                window.kayraNetworkErrors = 0;

                let finalTranscript = "";
                let interimTranscript = "";
                for (let i = event.resultIndex; i < event.results.length; ++i) {
                    if (event.results[i].isFinal) {
                        finalTranscript += event.results[i][0].transcript + " ";
                    } else {
                        interimTranscript += event.results[i][0].transcript + " ";
                    }
                }
                if (finalTranscript) {
                    currentText += finalTranscript;
                }

                // Fast path: publish an interruption the moment we see one, without
                // waiting for the silence timer or for the sentence to be finalized.
                const probe = (currentText + " " + interimTranscript).trim();
                if (!window.kayraInterrupt && looksLikeInterrupt(probe)) {
                    window.kayraInterrupt = { text: probe, at: Date.now(), start: utteranceStart };
                }
            };

            recognition.onerror = (event) => {
                if (event.error === 'network') {
                    // Bounded, NOT infinite. A browser without a speech backend fails this way
                    // forever; restarting it forever is how that turned into a silent hang.
                    // After MAX_NETWORK_ERRORS with no result ever produced, declare the
                    // backend dead and stop, so Python can try a different browser.
                    window.kayraNetworkErrors++;
                    if (!window.kayraEverRecognized &&
                        window.kayraNetworkErrors >= MAX_NETWORK_ERRORS) {
                        window.kayraRecognitionDead = true;
                        statusEl.textContent = "error: no speech backend";
                        return;             // deliberately no restart
                    }
                    restartRecognition();
                } else if (event.error === 'service-not-allowed' ||
                           event.error === 'language-not-supported') {
                    // The backend answered and refused. Retrying cannot change that.
                    window.kayraRecognitionDead = true;
                    statusEl.textContent = "error: " + event.error;
                    return;
                } else if (event.error === 'aborted') {
                    restartRecognition();
                } else if (event.error === 'no-speech') {
                    // Routine on a quiet mic — keep going rather than surfacing an error.
                    restartRecognition();
                } else {
                    statusEl.textContent = "error: " + event.error;
                }
            };

            recognition.onend = () => {
                // Restart continuously if stopped by the browser — but never resurrect a
                // backend already judged dead, or the bounded check above becomes unbounded
                // again through this path.
                if (window.kayraRecognitionDead) { return; }
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
                        window.speechQueue.push({
                            text: completedSentence,
                            start: utteranceStart,
                            // The utterance physically ended when results stopped arriving,
                            // i.e. `silenceLimit` ms ago — not now.
                            end: lastResultTime
                        });
                        // Drop the oldest if the consumer has fallen far behind.
                        while (window.speechQueue.length > MAX_QUEUE) {
                            window.speechQueue.shift();
                        }
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


class SttState:
    """
    Explicit lifecycle states for the STT subsystem.

    Transitions are single-writer: only `_start_session`, `recover` and `shutdown` move the
    engine between lifecycle states, and each holds `_lifecycle_lock` for the whole
    transition. Readers (`capture`, `poll_interrupt`) never mutate it beyond the
    READY <-> LISTENING pair, which is purely informational.
    """
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    READY = "READY"
    LISTENING = "LISTENING"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class _PageServer:
    """
    Serves the recognition page from http://127.0.0.1:<ephemeral port>.

    Why this exists rather than the `data:` URL the engine used before: a `data:` URL has an
    opaque origin and is NOT a secure context, so `navigator.mediaDevices` is `undefined`
    there. The page's `primeProcessedMicrophone()` — which requests echo cancellation, noise
    suppression and auto gain control on the capture device — was therefore silently dead
    code (measured: `hasMediaDevices: "undefined"`, `micStream: false`). `127.0.0.1` is a
    trustworthy origin per spec, so the same page served from here IS a secure context and
    the constraints really are applied (verified: `echoCancellation: True` in the resulting
    track settings).

    It costs one daemon thread and one loopback socket inside the existing Python process —
    no extra process. It binds 127.0.0.1 only, so nothing outside the machine can reach it.
    """

    def __init__(self, html: str):
        self._html = html.encode("utf-8")
        self._httpd = None
        self._thread = None
        self.url = None

    def start(self):
        import http.server
        import socketserver

        html_bytes = self._html

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html_bytes)

            def log_message(self, *args):
                pass  # never pollute the Rich console with request logs

        class _Server(socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # Chrome resets this connection whenever the browser goes away (including
                # every recovery). The default handler dumps a full traceback to stderr,
                # which looks like a crash in the middle of the Rich console output.
                pass

        # Port 0 = let the OS pick a free ephemeral port; bound to loopback only.
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="kayra-stt-page")
        self._thread.start()
        self.url = f"http://127.0.0.1:{port}/"
        return self.url

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        self._thread = None


class SpeechToTextEngine:
    """
    Continuous Asynchronous Speech-to-Text Engine.

    Lifecycle contract
    ------------------
    ONE engine owns ONE WebDriver, which owns ONE browser session, for the entire Kayra
    process. Utterances are consumed from that session; a new browser is never launched per
    utterance or per listening cycle. When the session dies, `recover()` tears the old one
    down, verifies its processes are gone, and only then builds a replacement.

    Process ownership
    -----------------
    The engine records the ChromeDriver PID and every Chrome PID beneath it
    (`owned_pids`). Shutdown terminates exactly those. It never matches on process *name*:
    Kayra can legitimately open Chrome for the user (`automation_windows.OpenApp` ->
    AppOpener -> `subprocess.Popen`), which makes the user's own browser a child of the
    Kayra process — a name-based sweep would kill the user's windows.

    Thread-safety
    -------------
    Selenium is not thread-safe and is touched from the main capture loop and the barge-in
    watcher. Every command goes through `_driver_lock`, acquired WITH A TIMEOUT so a hung
    driver cannot wedge the other threads (see `_script`).
    """

    # Guard against a second live session being created by accident.
    _active_instance = None
    _instance_lock = threading.Lock()

    # A dead ChromeDriver leaves Selenium's HTTP call hanging on connect. Without a bound,
    # `capture()` blocks forever WHILE HOLDING the driver lock, which also freezes the
    # barge-in watcher. Measured before this bound: capture() never returned.
    COMMAND_TIMEOUT_SECONDS = 12
    LOCK_TIMEOUT_SECONDS = 15

    MAX_RECOVERY_ATTEMPTS = 3

    def __init__(self, language=None, silence_limit=0.8, autostart=True, browser=None):
        """
        Prepares the engine and (by default) brings up the single browser session.

        Parameters:
            language (str): Target language code (e.g. 'en-US', 'hi-IN'). Defaults to INPUT_LANGUAGE in .env.
            silence_limit (float): Silence detection threshold in seconds (VAD gap size).
            autostart (bool): Start the browser session immediately. False is for tests that
                              want to inspect configuration without launching a browser.
            browser (str): Which browser to drive - "chrome", "edge", "brave", ... or "auto"
                           (the default) to pick by preference and verified capability.
                           Defaults to STT_BROWSER in .env.
        """
        # Load language configurations from environmental setups.
        # Resolved through core.config, which reads the project's .env exactly once and
        # locates it from the package root rather than the working directory.
        if not language:
            language = env("INPUT_LANGUAGE", "en-US")

        self.language = language
        self.silence_limit_ms = int(silence_limit * 1000)
        self._driver_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()

        # Which browser to drive. "auto" (the default) means: honour the user's preference and
        # their default browser where those can actually transcribe, and fall back where they
        # cannot - see kayra.input.browsers for why that distinction has to exist.
        preferred = (browser if browser is not None else env("STT_BROWSER", "auto")) or "auto"
        preferred = str(preferred).strip().lower()
        self.preferred_browser = None if preferred in ("auto", "", "default") else preferred

        self.driver = None
        self.browser = None          # BrowserSpec actually in use, set by _start_session
        # Browsers that proved unable to transcribe DURING THIS RUN. Session-scoped on purpose:
        # a backend failure can mean "this browser has none" or "this machine is offline right
        # now", and only the first is permanent. Nothing is written to disk from here.
        self._rejected_browsers = set()
        self.state = SttState.NOT_STARTED
        self.owned_pids = set()
        self._service_pid = None
        self._recovery_count = 0
        self._page_server = _PageServer(html_code)
        self._shutdown_done = False

        # Translation is a NETWORK round-trip on the critical input path. When the
        # recognizer is already producing English there is nothing to translate, so we
        # skip it entirely and save ~200-600ms on every single utterance.
        self._needs_translation = not str(self.language).lower().startswith("en")

        # Resolve path to the centralized LOWERCASE 'data/Files' directory relative to project root
        self.temp_dir_path = data_path("Files")
        os.makedirs(self.temp_dir_path, exist_ok=True)

        with SpeechToTextEngine._instance_lock:
            live = SpeechToTextEngine._active_instance
            if live is not None and live is not self and live.driver is not None:
                print_warning(
                    "A live SpeechToTextEngine already exists. Creating a second one means a "
                    "second browser session — use get_shared_engine() unless this is deliberate."
                )
            SpeechToTextEngine._active_instance = self

        if autostart:
            self._start_session()

        # Register robust process termination cleanup handler
        atexit.register(self.shutdown)

    # ──────────────────────────────────────────────────────────────────────
    #                        SESSION LIFECYCLE
    # ──────────────────────────────────────────────────────────────────────

    def _chrome_options(self, spec=None):
        """
        Builds the browser option set.

        The footprint flags below were chosen by measurement, not by copying a list: on this
        host they took the session from 10 processes / 548MB to 8 processes / 473MB with the
        recognition page still reporting `listening`. Nothing here disables audio capture,
        the network service, or recognition quality.

        `spec` is a `browsers.BrowserSpec`. Every flag used here is a Chromium flag, so the
        same set applies to Chrome, Edge, Brave and the other derivatives; only the driver
        class and `binary_location` differ. Passing None keeps the historical Chrome-only
        behaviour, which is what the existing tests construct.
        """
        if spec is not None and spec.driver == "edge":
            chrome_options = EdgeOptions()
        else:
            chrome_options = Options()
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.3"

        # Headless Configuration
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument(f"user-agent={user_agent}")
        chrome_options.add_argument("--use-fake-ui-for-media-stream")  # Bypasses browser mic permission popup

        # Advanced Headless Performance Optimizations (Fast boot, low RAM, zero GPU compile delays)
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--blink-settings=imagesEnabled=false")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--disable-sync")

        # ── Measured footprint reductions ──
        # -1 renderer (Chrome keeps a warm spare we never use), -1 GPU process (folded into
        # the browser process), no crash reporter, tiny caches, capped V8 heap for a page
        # whose entire job is a few hundred bytes of transcript.
        chrome_options.add_argument("--renderer-process-limit=1")
        chrome_options.add_argument("--in-process-gpu")
        chrome_options.add_argument("--disable-software-rasterizer")
        chrome_options.add_argument("--disable-breakpad")
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--no-service-autorun")
        chrome_options.add_argument("--disable-default-apps")
        chrome_options.add_argument("--disable-client-side-phishing-detection")
        chrome_options.add_argument("--disable-component-extensions-with-background-pages")
        chrome_options.add_argument("--disable-hang-monitor")
        chrome_options.add_argument("--mute-audio")  # Chrome never needs to PLAY anything
        chrome_options.add_argument("--disk-cache-size=1")
        chrome_options.add_argument("--media-cache-size=1")
        chrome_options.add_argument("--js-flags=--max-old-space-size=64")
        chrome_options.add_argument(
            "--disable-features=SpareRendererForSitePerProcess,Translate,BackForwardCache,"
            "OptimizationHints,MediaRouter,InterestFeedContentSuggestions,"
            "CalculateNativeWinOcclusion"
        )

        # Chrome's WebRTC audio processing (AEC / noise suppression / AGC) must stay ON for
        # captured audio — the page asks for it via getUserMedia constraints, and it only
        # works because the page is served from a secure origin (see _PageServer).

        # A Chromium derivative is launched by pointing ChromeDriver at its executable. Without
        # this, ChromeDriver silently starts Chrome instead — which would "work" while quietly
        # ignoring the browser the user actually asked for.
        if spec is not None and spec.needs_binary_location():
            chrome_options.binary_location = spec.binary

        return chrome_options

    # Probe budget for an UNPROVEN browser. Measured on this host: a backendless build
    # (Brave 152) declares itself dead in 1.55-1.78s across three trials, so 4s is a
    # comfortable ceiling rather than a guess.
    RECOGNITION_PROBE_SECONDS = 4.0

    def _probe_budget(self, spec):
        """
        How long to wait for THIS browser to prove itself, in seconds. 0 means "do not wait".

        Waiting is not free: a blanket probe on every start added ~2.8s to the cold start and
        pushed crash recovery from 2.5s to 6.5s, which is a real regression against the whole
        point of the STT design. It is also unnecessary for a browser that either carries a
        first-party backend (Chrome, Edge) or was already seen working here.

        Those browsers are therefore trusted at start and checked LATER, for free: `capture()`
        already reads the page status on every poll in the same round-trip it uses to pop the
        speech queue, so a backend that dies is detected within one 50ms poll once listening
        begins. The only thing skipping the probe costs is that the failure is noticed at the
        first listen instead of at boot - and a browser whose backend is unreachable produces
        no transcript either way.

        An UNKNOWN or known-backendless browser still gets the full probe, because for those
        the failure is the expected outcome and catching it at start is what allows the
        fallback to happen before the user ever tries to speak.
        """
        if spec.recognition in ("google", "vendor"):
            return 0.0
        if spec.key == browsers.previously_working():
            return 0.0
        return self.RECOGNITION_PROBE_SECONDS

    def _verify_recognition(self, budget=None):
        """
        Waits briefly for evidence that this browser can actually transcribe.

        Returns (ok, reason). `ok=False` means the page declared the backend dead: the API was
        present and start() succeeded, but recognition could not reach a service. That is the
        Brave case, and it is indistinguishable from a working browser by any check made before
        this point - which is precisely why it is made here rather than assumed away.

        A browser that has not failed by the deadline is accepted. Absence of an error is the
        strongest signal available without real speech, and waiting longer would put seconds on
        every cold start to gain nothing.
        """
        if budget is None:
            budget = self.RECOGNITION_PROBE_SECONDS
        if budget <= 0:
            # Trusted browser: one cheap read catches an already-dead backend (the page fails
            # fast), and anything later is caught by capture().
            try:
                if self.driver.execute_script("return window.kayraRecognitionDead === true;"):
                    return False, "recognition backend unavailable"
            except WebDriverException:
                return False, "browser session died during verification"
            return True, "trusted"

        deadline = time.time() + budget
        while time.time() < deadline:
            try:
                dead = self.driver.execute_script("return window.kayraRecognitionDead === true;")
                if dead:
                    status = self.driver.execute_script(
                        "return document.getElementById('status').textContent;")
                    return False, str(status or "recognition backend unavailable")
                if self.driver.execute_script("return window.kayraEverRecognized === true;"):
                    return True, "produced a result"
            except WebDriverException:
                return False, "browser session died during verification"
            time.sleep(0.1)
        return True, "no error"

    def _launch(self, spec, page_url):
        """Builds one browser session and starts recognition in it. Raises on failure."""
        if spec is not None and spec.driver == "edge":
            self.driver = webdriver.Edge(options=self._chrome_options(spec))
        else:
            self.driver = webdriver.Chrome(options=self._chrome_options(spec))

        # Bound every HTTP command so a dead driver fails fast instead of hanging.
        try:
            self.driver.command_executor.client_config.timeout = self.COMMAND_TIMEOUT_SECONDS
        except Exception:
            pass  # Older/newer Selenium internals - non-fatal, recovery still works.

        self._record_owned_processes()

        self.driver.get(page_url)
        self.driver.execute_script(
            "startContinuousRecognition(arguments[0], arguments[1], arguments[2], arguments[3]);",
            self.language,
            self.silence_limit_ms,
            INTERRUPT_PHRASES,
            sorted(INTERRUPT_FILLERS),
        )

        # Late-spawning renderers/utilities are not children yet at driver creation.
        self._record_owned_processes()

    def _switch_browser(self, reason):
        """
        Abandons the current browser and rebuilds the session on the next candidate.

        Called when a browser that was trusted at start turns out to be unable to reach a
        speech backend. Returns True if a working session was established, False if no
        candidate remains.

        The failed browser is added to the session-scoped reject set FIRST, so the rebuild
        cannot select it again and loop. Teardown happens before the new session is built, for
        the same reason `recover()` does it: two live sessions would mean two browsers holding
        the microphone.
        """
        failed = self.browser
        label = failed.label if failed else "the current browser"
        if failed is not None:
            self._rejected_browsers.add(failed.key)

        print_warning(f"{label} stopped being able to transcribe ({reason}). "
                      f"Switching speech input to another browser.")

        with self._lifecycle_lock:
            self._teardown_session(quiet=True)
            self.driver = None
            self.browser = None
            self.state = SttState.NOT_STARTED
            try:
                self._start_session()
            except Exception as e:
                self.state = SttState.FAILED
                print_error(f"No usable browser for speech input: {e}")
                return False

        self.state = SttState.LISTENING
        return True

    def _start_session(self):
        """
        Brings up the single owned browser session. Raises on failure after marking FAILED,
        so a caller cannot mistake a dead subsystem for a working one.

        Tries each candidate browser in preference order and VERIFIES that recognition actually
        works before declaring the session ready. Kayra does not require Chrome specifically -
        Edge ships on every Windows 11 machine and has its own backend - but it does require a
        browser that can genuinely transcribe, and that cannot be determined from the browser's
        name or from the presence of `webkitSpeechRecognition`.
        """
        with self._lifecycle_lock:
            if self.driver is not None:
                return self.driver  # Already up: never build a second session.

            self.state = SttState.STARTING
            page_url = self._page_server.start()

            options = browsers.candidates(self.preferred_browser)
            if not options:
                self.state = SttState.FAILED
                raise RuntimeError(
                    "STT session failed to start: no supported browser found. Kayra's speech "
                    "input needs a Chromium-based browser (Microsoft Edge is preinstalled on "
                    "Windows 11 and works).")

            failures = []
            for spec in options:
                if spec.key in self._rejected_browsers:
                    continue        # already proved it cannot transcribe in this session
                try:
                    self._launch(spec, page_url)
                except Exception as e:
                    failures.append(f"{spec.label}: {str(e).splitlines()[0][:120]}")
                    self._teardown_session(quiet=True)
                    continue

                ok, reason = self._verify_recognition(self._probe_budget(spec))
                if ok:
                    self.browser = spec
                    browsers.remember_working(spec.key)
                    browsers.warn_about_default(spec)
                    print_info(f"Speech input using {spec.label} ({reason}).")
                    self.state = SttState.READY
                    return self.driver

                failures.append(f"{spec.label}: {reason}")
                print_warning(f"{spec.label} cannot transcribe ({reason}); trying another browser.")
                self._rejected_browsers.add(spec.key)
                self._teardown_session(quiet=True)

            # Every browser failed. If they ALL failed on the backend, the machine is far more
            # likely to be offline than for every browser on it to be backendless - say that,
            # rather than blaming a browser and sending the user to reinstall something.
            self.state = SttState.FAILED
            detail = "; ".join(failures)
            raise RuntimeError(
                f"STT session failed to start: no browser could reach a speech recognition "
                f"service. This is usually a network problem rather than a browser problem. "
                f"Tried - {detail}")

    def _record_owned_processes(self):
        """
        Records the ChromeDriver PID and every process beneath it as Kayra-owned.

        PID-based, deliberately: the alternative (matching process names) would sweep up the
        user's own Chrome windows whenever Kayra had opened one for them.
        """
        try:
            service_proc = getattr(getattr(self.driver, "service", None), "process", None)
            if service_proc is None:
                return
            self._service_pid = service_proc.pid
            self.owned_pids.add(service_proc.pid)

            if psutil is None:
                return
            try:
                driver_proc = psutil.Process(service_proc.pid)
                for child in driver_proc.children(recursive=True):
                    self.owned_pids.add(child.pid)
            except psutil.Error:
                pass
        except Exception:
            pass

    def refresh_owned_processes(self):
        """Re-scans for Chrome processes spawned after startup (lazily created renderers)."""
        self._record_owned_processes()
        return set(self.owned_pids)

    def _service_alive(self):
        """
        Process-level liveness check for ChromeDriver — no HTTP, microseconds.

        This is the fast path for detecting a dead session. Discovering it through Selenium
        instead costs ~16s: a command against a dead driver spends that long in urllib3
        connect retries before raising (measured), and it holds the driver lock the whole
        time, which would freeze the barge-in watcher along with it.
        """
        if self._service_pid is None:
            return True  # Unknown ownership — fall back to the HTTP path.
        if psutil is None:
            return True
        try:
            proc = psutil.Process(self._service_pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def is_session_alive(self):
        """
        True when the browser session is present AND responding.

        Checks the ChromeDriver process first (instant) and only then pays for a round-trip
        (`return 1`), so this is cheap enough to use as a health gate on the polling path.
        """
        if self.driver is None:
            return False
        if not self._service_alive():
            return False
        try:
            return self._raw_script("return 1;") == 1
        except Exception:
            return False

    def get_or_create_driver(self):
        """
        Returns the existing healthy driver, or recovers exactly one replacement.

        This is the only sanctioned way to obtain a driver. It never creates a session while
        another is alive: `recover()` tears the old one down and verifies its processes are
        gone before a new one is built.
        """
        if self.driver is not None and self.is_session_alive():
            return self.driver
        if self.state in (SttState.STOPPING, SttState.STOPPED):
            return None
        return self.recover(reason="session unresponsive")

    def recover(self, reason="unknown"):
        """
        Replaces a dead session, in strict order: stop -> verify gone -> start.

        Returns the new driver, or None when recovery is exhausted or the engine is stopping.
        """
        with self._lifecycle_lock:
            if self.state in (SttState.STOPPING, SttState.STOPPED):
                return None

            # Another thread may have recovered while we waited for the lock.
            if self.driver is not None and self.is_session_alive():
                return self.driver

            if self._recovery_count >= self.MAX_RECOVERY_ATTEMPTS:
                self.state = SttState.FAILED
                print_error(
                    f"STT recovery abandoned after {self._recovery_count} attempts. "
                    "Voice input is offline for the rest of this session; restart Kayra."
                )
                return None

            self._recovery_count += 1
            self.state = SttState.RECOVERING
            print_warning(
                f"STT session lost ({reason}). Recovering "
                f"[attempt {self._recovery_count}/{self.MAX_RECOVERY_ATTEMPTS}]..."
            )

            # 1. Old session down first — never run two.
            #    session_dead=True: we are here precisely because it stopped responding, so
            #    there is nothing to say to it politely.
            self._teardown_session(quiet=True, session_dead=True)
            # 2. Verify the OS actually released them before claiming new ones. Short grace:
            #    these are orphans of a dead driver, not a cooperative shutdown.
            survivors = self._await_owned_termination(timeout=1.0)
            if survivors:
                print_warning(f"Old STT processes would not die: {sorted(survivors)}")

            # 3. Only now build the replacement.
            try:
                self._start_session()
                print_success("STT session restored.")
                return self.driver
            except Exception as e:
                self.state = SttState.FAILED
                print_error(f"STT recovery failed: {e}")
                return None

    # ──────────────────────────────────────────────────────────────────────
    #                          DRIVER PLUMBING
    # ──────────────────────────────────────────────────────────────────────

    def _raw_script(self, script, *args):
        """Executes JS under the driver lock without any recovery logic. May raise."""
        acquired = self._driver_lock.acquire(timeout=self.LOCK_TIMEOUT_SECONDS)
        if not acquired:
            # Another thread is stuck inside a driver command. Report rather than pile on.
            raise TimeoutError("driver lock busy")
        try:
            if self.driver is None:
                raise WebDriverException("no active session")
            # Fail immediately on a dead ChromeDriver instead of waiting out the retry storm.
            if not self._service_alive():
                raise WebDriverException("chromedriver process is gone")
            return self.driver.execute_script(script, *args)
        finally:
            self._driver_lock.release()

    def _script(self, script, *args, recover=True):
        """
        Runs JS in the STT page under the driver lock.

        Returns None when the call could not be completed. When the failure looks like a dead
        session (rather than a JS error in the snippet), a single recovery is attempted and
        the call is retried once — this is what turns "Chrome crashed" from a permanent
        voice-input outage into a two-second blip.
        """
        try:
            return self._raw_script(script, *args)
        except JavascriptException:
            return None  # Bug in the snippet, not a dead browser — never recover for this.
        except TimeoutError:
            return None
        except Exception as e:
            if not recover or self.state in (SttState.STOPPING, SttState.STOPPED):
                return None
            if self.recover(reason=type(e).__name__) is None:
                return None
            try:
                return self._raw_script(script, *args)
            except Exception:
                return None

    def set_assistant_status(self, status):
        """
        Updates the status file so external applications/GUIs can show 'Listening...' or errors.

        Parameters:
            status (str): Current state description text.
        """
        try:
            with open(rf"{self.temp_dir_path}/status.data", "w", encoding="utf-8") as f:
                f.write(status)
        except Exception:
            pass

    def clear_queue(self):
        """
        Purges the pending utterance queue and any latched interrupt.

        Called after a barge-in so the interrupt utterance itself (and any echo captured
        just before it) can't be replayed as the user's next command.
        """
        self._script("window.speechQueue = []; window.kayraInterrupt = null;")

    def poll_interrupt(self):
        """
        Non-blocking check for an interruption word detected on an INTERIM result.

        Returns:
            dict | None: {'text', 'at', 'start'} if the user just said an interrupt word,
                         otherwise None. Consuming clears the flag.
        """
        payload = self._script(
            "var i = window.kayraInterrupt; window.kayraInterrupt = null; return i;"
        )
        if isinstance(payload, dict) and payload.get("text"):
            return payload
        return None

    # ──────────────────────────────────────────────────────────────────────
    #                             CAPTURE
    # ──────────────────────────────────────────────────────────────────────

    def capture(self, poll_interval: float = 0.05):
        """
        Blocks until a completed utterance is finalized in Chrome's queue, then pops,
        translates (only when needed), formats, and returns it WITH its capture window.

        Returns:
            dict | None: {'text': str, 'raw': str, 'start_ms': float, 'end_ms': float}
                         or None if the session is gone for good / the user hit Ctrl+C.
        """
        self.set_assistant_status("Listening...")
        if self.state == SttState.READY:
            self.state = SttState.LISTENING

        try:
            while True:
                if self.state in (SttState.STOPPING, SttState.STOPPED):
                    return None
                if self.state == SttState.FAILED:
                    return None

                # Pop the oldest utterance AND read the engine status in a single
                # round-trip — at 20 polls/second, halving the Selenium calls matters.
                payload = self._script(
                    "return {item: window.speechQueue.shift() || null,"
                    " status: document.getElementById('status').textContent};"
                )

                if payload is None:
                    # _script already attempted recovery. If it could not restore the
                    # session, stop blocking the caller instead of spinning silently.
                    if self.state in (SttState.FAILED, SttState.STOPPING, SttState.STOPPED):
                        return None
                    time.sleep(poll_interval)
                    continue

                item = payload.get("item")
                if item and item.get("text"):
                    raw_text = item["text"]
                    self.set_assistant_status("Translating...")
                    translated_text = translate_query(raw_text, needs_translation=self._needs_translation)
                    return {
                        "text": format_query(translated_text),
                        "raw": raw_text,
                        "start_ms": float(item.get("start") or now_ms()),
                        "end_ms": float(item.get("end") or now_ms()),
                    }

                # Check for critical runtime errors reported inside the browser engine
                status = payload.get("status") or ""
                if status.startswith("error:"):
                    error_msg = status.replace("error: ", "")

                    # A dead recognition backend is recoverable by moving to a DIFFERENT
                    # browser, which is not true of any other error here. This is the deferred
                    # half of the capability check: browsers with a first-party backend are
                    # trusted at start (so cold start pays nothing) and verified here instead,
                    # at the first listen, using a status this loop already reads.
                    if "no speech backend" in error_msg or "service-not-allowed" in error_msg:
                        if self._switch_browser(error_msg):
                            continue
                        return None

                    print_error(f"STT Internal Error: {error_msg}")
                    return {"text": "", "raw": "", "start_ms": now_ms(), "end_ms": now_ms()}

                # Super-low CPU polling sleep interval (50ms) to ensure minimal host thread impact
                time.sleep(poll_interval)

        except KeyboardInterrupt:
            return None

    def listen_and_transcribe(self):
        """
        Backward-compatible wrapper around `capture()` returning only the transcript text.

        Returns:
            str: The capitalized, formatted English query transcript.
        """
        result = self.capture()
        if result is None:
            return None
        return result["text"]

    # ──────────────────────────────────────────────────────────────────────
    #                             SHUTDOWN
    # ──────────────────────────────────────────────────────────────────────

    def _teardown_session(self, quiet=False, session_dead=False):
        """
        Stops recognition and disposes of the driver. Idempotent; safe to call from any
        thread and from a recovery path.

        `session_dead=True` skips every HTTP-based step. Against a dead ChromeDriver,
        `execute_script` and `quit()` cost ~16s EACH in connect retries, while
        `service.stop()` costs 0.00s and killing the orphaned Chrome processes by PID costs
        0.04s (all measured). Recovery therefore never speaks WebDriver to a corpse.
        """
        with self._lifecycle_lock:
            driver, self.driver = self.driver, None
            if driver is None:
                return

            if not quiet:
                label = self.browser.label if getattr(self, "browser", None) else "browser"
                print_system(f"Shutting down headless {label} background session...")

            if not session_dead:
                # 1. Stop Web Speech recognition inside the page.
                try:
                    driver.execute_script("stopContinuousRecognition();")
                except BaseException:
                    pass

                # 2. Release the Selenium session (quit() exactly once per driver object).
                try:
                    driver.quit()
                except BaseException:
                    pass
            else:
                # Reap the ChromeDriver process without any WebDriver traffic. The orphaned
                # Chrome children it leaves behind are terminated by PID right after.
                try:
                    driver.service.stop()
                except BaseException:
                    pass

    def _await_owned_termination(self, timeout=6.0):
        """
        Waits for the processes this engine owns to actually disappear, then force-kills any
        stragglers BY PID. Returns the set of PIDs that survived (normally empty).
        """
        if psutil is None or not self.owned_pids:
            self.owned_pids = set()
            return set()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._live_owned_processes():
                self.owned_pids = set()
                return set()
            time.sleep(0.15)

        # Straggler: terminate, then kill — but only PIDs this engine created.
        for proc in self._live_owned_processes():
            try:
                proc.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(self._live_owned_processes(), timeout=2.0)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass

        survivors = {p.pid for p in self._live_owned_processes()}
        self.owned_pids = set()
        return survivors

    def _live_owned_processes(self):
        """psutil handles for the owned PIDs that are still running."""
        if psutil is None:
            return []
        live = []
        for pid in list(self.owned_pids):
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    live.append(proc)
            except psutil.Error:
                continue
        return live

    def terminate_owned_processes(self, timeout=6.0):
        """
        Public hook for the application's shutdown handler.

        Terminates ONLY the ChromeDriver/Chrome processes this engine created. Never matches
        on process name, so Chrome windows Kayra opened for the user (which become children
        of the Kayra process via AppOpener's subprocess.Popen) are left alone.
        """
        return self._await_owned_termination(timeout=timeout)

    def shutdown(self):
        """
        Deterministic teardown: stop recognition, quit the driver, verify the owned processes
        are gone, drop the page server. Idempotent — safe from atexit, a signal handler and
        an explicit call in the same run.
        """
        with self._lifecycle_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self.state = SttState.STOPPING

        try:
            atexit.unregister(self.shutdown)
        except Exception:
            pass

        self.refresh_owned_processes()
        self._teardown_session()
        survivors = self._await_owned_termination()
        if survivors:
            print_warning(f"STT processes still alive after shutdown: {sorted(survivors)}")

        self._page_server.stop()

        with SpeechToTextEngine._instance_lock:
            if SpeechToTextEngine._active_instance is self:
                SpeechToTextEngine._active_instance = None

        self.state = SttState.STOPPED


def get_shared_engine(**kwargs):
    """
    Returns the process-wide STT engine, creating it on first use.

    Every entry point (main.py, the legacy `recognize_speech()` helper, diagnostics) should
    come through here so a Kayra process can never end up with two browser sessions.
    """
    with SpeechToTextEngine._instance_lock:
        engine = SpeechToTextEngine._active_instance
        if engine is not None and engine.driver is not None:
            return engine
    return SpeechToTextEngine(**kwargs)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                  FORMATTING & TRANSLATION UTILITIES                    │
# └────────────────────────────────────────────────────────────────────────┘

def format_query(query):
    """
    Cleans and structures raw synthesized query speech:
    - Normalizes word boundaries.
    - Resolves typical query interrogators (e.g. what, where, can you) to append a question mark '?'.
    - Appends periods '.' to generic command declarations.
    - Capitalizes the final text string for premium presentation.

    Parameters:
        query (str): The raw text sequence to format.

    Returns:
        str: The structured, formatted transcript.
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

    # Interrogate first word or interior structures for questioning contexts
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


def translate_query(query, needs_translation=True):
    """
    Translates non-English input speech into English text using mtranslate.

    Parameters:
        query (str): The input text in any foreign tongue.
        needs_translation (bool): False when INPUT_LANGUAGE is already English. The
            mtranslate call is a blocking network request on the critical input path,
            so skipping it when it cannot possibly change the text removes hundreds of
            milliseconds from every utterance (including "stop").

    Returns:
        str: The translated English equivalent in capitalized format.
    """
    # Pre-translation phonetic corrections:
    # Google Speech-to-Text in Hindi ('hi-IN') transcribes the phonetic name "Kayra"
    # either as the real Hindi name "कायरा" or the homophonic "कायर" (meaning "coward").
    # We swap both to "Kayra" before translating so they remain stable.
    corrected_query = query
    if "कायर" in corrected_query:
        corrected_query = corrected_query.replace("कायर", "Kayra")
    if "कायरा" in corrected_query:
        corrected_query = corrected_query.replace("कायरा", "Kayra")

    # Nothing to translate: an English-configured recognizer that returned pure ASCII.
    if not needs_translation and corrected_query.isascii():
        english_query = corrected_query
    else:
        try:
            english_query = mt.translate(corrected_query, "en", "auto")
        except Exception as e:
            print_warning(f"Translation unavailable, using raw transcript: {e}")
            english_query = corrected_query

    # Post-translation robustness:
    # Handle any cases where English/Hinglish transcribes "kaira" or "coward".
    # We perform case-insensitive whole-word replacements to enforce the "Kayra" spelling.
    english_query = re.sub(r"\bcowards\b", "Kayras", english_query, flags=re.IGNORECASE)
    english_query = re.sub(r"\bcoward's\b", "Kayra's", english_query, flags=re.IGNORECASE)
    english_query = re.sub(r"\bcoward\b", "Kayra", english_query, flags=re.IGNORECASE)
    english_query = re.sub(r"\bkairas\b", "Kayras", english_query, flags=re.IGNORECASE)
    english_query = re.sub(r"\bkaira's\b", "Kayra's", english_query, flags=re.IGNORECASE)
    english_query = re.sub(r"\bkaira\b", "Kayra", english_query, flags=re.IGNORECASE)

    return english_query.capitalize()


def is_interrupt_phrase(text: str) -> bool:
    """
    True when an utterance is *only* an interruption command ("stop", "wait", "hold on").

    Matching is EXACT on the utterance with filler words removed, never a prefix test.
    A prefix test would classify "stop the music" — a legitimate automation command that
    must reach the DMM — as a barge-in and silently swallow it.
    """
    if not text:
        return False

    # Strip punctuation, then drop the assistant's name and politeness filler so
    # "Kayra, stop please" reduces to "stop".
    #
    # Only explicit punctuation is removed — a `[^\w\s]` class would also eat Devanagari
    # combining vowel signs (category Mn, which Python's \w excludes), turning "रुको"
    # into "र क" and silently breaking every Hindi interrupt word.
    cleaned = re.sub(r"[.,!?;:\"'()\[\]।]+", " ", text.lower())
    words = [w for w in cleaned.split() if w not in INTERRUPT_FILLERS]
    if not words or len(words) > 3:
        return False

    if " ".join(words) in _INTERRUPT_PHRASE_SET:
        return True

    # Repetition of a single-word interrupt ("stop stop stop") still means stop.
    return all(w in _SINGLE_WORD_INTERRUPTS for w in words)


# ┌────────────────────────────────────────────────────────────────────────┐
# │                 BACKWARD COMPATIBILITY CLASS ALIASES                   │
# └────────────────────────────────────────────────────────────────────────┘
OnlineSpeechEngine = SpeechToTextEngine
SetAssistantStatus = SpeechToTextEngine.set_assistant_status
QueryModifier = format_query
UniversalTranslator = translate_query

def recognize_speech():
    """
    Legacy wrapper function to maintain backwards-compatibility.

    Routes through `get_shared_engine()` rather than holding its own module-level instance,
    so calling this from a script that already booted an engine reuses that one Chrome
    session instead of quietly starting a second.
    """
    return get_shared_engine().listen_and_transcribe()

SpeechRecognition = recognize_speech


# ┌────────────────────────────────────────────────────────────────────────┐
# │                     MAIN SCRIPT TEST ENTRYPOINT                        │
# └────────────────────────────────────────────────────────────────────────┘
if __name__ == "__main__":
    # Instantiate the continuous STT engine session
    engine = SpeechToTextEngine(silence_limit=0.8)

    print_banner("ONLINE WEB SPEECH ENGINE", "Say something to start speaking... (Type 'exit application' to quit)")

    try:
        while True:
            # Capture speech transcribed inputs in a loop
            result = engine.capture()
            if result and result["text"]:
                latency = now_ms() - result["end_ms"]
                print_success(
                    f"Speech Recognized: [bold highlight]{result['text']}[/bold highlight] "
                    f"[dim](finalize+transcribe latency {latency:.0f}ms)[/dim]"
                )
                if "exit application" in result["text"].lower():
                    break
    except KeyboardInterrupt:
        console.print("\n[bold red]Forced Exit.[/bold red]")
    finally:
        engine.shutdown()
