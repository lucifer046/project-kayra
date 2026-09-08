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
from kayra.core.logbus import Subsystem, info, warning, error
from kayra.utils import print_warning, print_error, print_success, print_banner, console, now_ms


# The control vocabulary lives in `core.voice_control` — one table, shared by the STT page
# (which does the first-pass interim match in JavaScript), this module, and the orchestrator's
# local control interpreter. It is re-exported here under its historical names because that is
# where every caller and every test has always imported it from.
#
# `core` is a leaf package: importing it here cannot create a cycle, which is exactly why the
# vocabulary was moved there rather than being duplicated.
from kayra.core.voice_control import (          # noqa: F401  (re-exported)
    INTERRUPT_PHRASES, INTERRUPT_FILLERS, ControlKind, ControlCommand,
    PAUSE_LISTENING_PHRASES, RESUME_LISTENING_PHRASES, SLEEP_PHRASES,
    WAKE_PHRASES, SHUTDOWN_PHRASES,
    classify_control, is_interrupt_phrase, interrupt_in_tail,
)

# Named explicitly so the re-export is code rather than a side effect of an import statement —
# a linter cannot tell the two apart, and "unused import" is the wrong answer for a name this
# module deliberately publishes.
CONTROL_VOCABULARY = (classify_control, is_interrupt_phrase, interrupt_in_tail,
                      ControlKind, ControlCommand)

# Lifecycle phrases the page publishes on `window.kayraControl`. These are matched EXACTLY on
# the whole utterance (never on a tail), because unlike a barge-in they are not urgent enough
# to justify any risk of a false positive: quitting the process on a misheard suffix would be
# the worst failure this system could have.
_CONTROL_PHRASE_TABLE = [
    [phrase, kind]
    for phrases, kind in (
        (PAUSE_LISTENING_PHRASES, ControlKind.PAUSE_LISTENING),
        (RESUME_LISTENING_PHRASES, ControlKind.RESUME_LISTENING),
        (SLEEP_PHRASES, ControlKind.SLEEP),
        (WAKE_PHRASES, ControlKind.WAKE),
        (SHUTDOWN_PHRASES, ControlKind.SHUTDOWN),
    )
    for phrase in phrases
]

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
        // Set the instant an interim result IS a lifecycle command: {text, kind, at}
        window.kayraControl = null;
        // Interrupt vocabulary, injected from Python so both sides share one list.
        window.kayraInterruptWords = [];
        window.kayraInterruptFillers = [];
        window.kayraSingleWordInterrupts = [];
        // [[phrase, kind], ...] for the lifecycle commands (pause/sleep/wake/shutdown).
        window.kayraControlPhrases = [];
        // Whether Kayra's own voice is currently leaving the speakers. Written by Python on
        // the barge-in watcher's existing poll, so it costs no extra round-trip.
        //
        // THIS FLAG IS WHY "stop" NOW WORKS RELIABLY. The microphone stays open during
        // playback, so whatever echo of Kayra's own voice survives Chrome's canceller is
        // already sitting in the recognizer's buffer when the user barges in. The probe is
        // therefore not "stop" but "...and then the rollout takes ten minutes stop", which no
        // whole-utterance test can ever match. While this flag is set — and ONLY while it is
        // set — the trailing words are matched as well.
        window.kayraSpeaking = false;
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

        // ── THE CAPTURE PIPELINE, IN ORDER ────────────────────────────────
        // audio capture -> AEC / noise suppression -> VAD / endpointing -> STT -> (Python)
        //                                                                          conservative,
        //                                                                          context-aware
        //                                                                          repair
        //
        // Everything below implements the first three stages. The fourth is the browser's own
        // recognizer, and the fifth deliberately lives in Python where the conversation
        // context is — a correction stage with no evidence about what is plausible is a
        // spelling corrector, and a spelling corrector will eventually turn a legitimate word
        // into the wrong command.

        // What the microphone track ACTUALLY granted, read back from the live track rather
        // than assumed from what was requested. Constraints are a request, not a promise, and
        // an assistant that reports echo cancellation it did not get is lying about the one
        // thing that explains its mistakes.
        window.kayraAudioSettings = null;
        window.kayraAudioError = "";

        // Live voice-activity state, published for diagnostics. `voice` is the only field the
        // endpointer reads; the rest exist so a bad room can be diagnosed rather than guessed
        // at.
        window.kayraVad = { ready: false, rms: 0, floor: 0, voice: false, threshold: 0 };

        // The finalized segments of the utterance being assembled, each with the recognizer's
        // OWN alternatives. This is what makes context-aware repair safe: Python re-ranks
        // among readings the recognizer actually offered instead of inventing one.
        let segments = [];
        let interimText = "";
        let lastVoiceMs = 0;
        let noiseFloor = 0.006;
        let audioCtx = null, analyser = null, vadFrame = null, vadTimer = null;

        // Endpointing tuning. Overridable from Python so there is one source of truth, with
        // these as the defaults every value was measured against.
        let tuning = {
            // A short, already-finalized command does not need the full silence window; the
            // recognizer has committed and waiting longer only makes the assistant feel slow.
            fastEndpointMs: 420,
            // Words the recognizer has not committed yet are worth waiting for. Cutting the
            // utterance here is how a spoken word becomes no word at all.
            interimGraceMs: 1400,
            // Nothing waits forever: if results keep arriving but the endpoint never settles,
            // flush anyway rather than accumulating a paragraph.
            maxWaitMs: 6000,
            // How long the room must be quiet, in ENERGY terms, on top of the recognizer
            // going quiet. This is what stops a mid-sentence pause ending the utterance.
            vadHangoverMs: 500,
            // Voice is RMS above this multiple of the learned noise floor.
            vadMargin: 3.2,
            // ...and above this much higher multiple while Kayra is audible, so residual echo
            // of her own voice cannot hold the endpoint open or open a new utterance.
            vadEchoMargin: 7.0,
            vadFloorMin: 0.004,
            vadIntervalMs: 50,
            maxAlternatives: 5
        };
        const MAX_SEGMENTS = 8;

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
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    window.kayraAudioError = "mediaDevices unavailable (insecure context?)";
                    return;
                }
                navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                        // One channel at a speech rate. A stereo 48k capture gives the
                        // recognizer nothing extra and gives the processing module more to do.
                        channelCount: 1,
                        sampleRate: 16000
                    }
                }).then(function (stream) {
                    window.kayraMicStream = stream;
                    try {
                        const track = stream.getAudioTracks()[0];
                        const settings = track ? track.getSettings() : null;
                        window.kayraAudioSettings = settings ? {
                            echoCancellation: settings.echoCancellation,
                            noiseSuppression: settings.noiseSuppression,
                            autoGainControl: settings.autoGainControl,
                            channelCount: settings.channelCount,
                            sampleRate: settings.sampleRate,
                            deviceId: settings.deviceId ? "present" : "",
                            label: track ? String(track.label || "").slice(0, 80) : ""
                        } : null;
                    } catch (e) { /* settings are diagnostics, never load-bearing */ }
                    startVoiceActivityDetection(stream);
                }).catch(function (err) {
                    window.kayraAudioError = String((err && err.name) || err || "denied");
                });
            } catch (e) {
                window.kayraAudioError = String(e);
            }
        }

        // ── VAD: energy-based voice activity on the processed stream ──────
        // The recognizer's own timing is not an endpointer. It reports when it produced a
        // RESULT, which lags the sound by a variable amount, so a fixed "no results for
        // 800ms" window ends the utterance while the user is still talking whenever the
        // backend is slow — and a clipped word is not a misheard word, it is a missing one.
        //
        // The stream is NEVER connected to the destination: playing the microphone back
        // through the speakers would create exactly the feedback loop this pipeline exists to
        // suppress.
        function startVoiceActivityDetection(stream) {
            try {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) { return; }
                if (audioCtx) { try { audioCtx.close(); } catch (e) {} }
                audioCtx = new Ctx();
                const source = audioCtx.createMediaStreamSource(stream);
                analyser = audioCtx.createAnalyser();
                analyser.fftSize = 1024;
                analyser.smoothingTimeConstant = 0.2;
                source.connect(analyser);
                vadFrame = new Float32Array(analyser.fftSize);
                lastVoiceMs = Date.now();
                window.kayraVad.ready = true;
                if (vadTimer) { clearInterval(vadTimer); }
                vadTimer = setInterval(sampleVoiceActivity, tuning.vadIntervalMs);
            } catch (e) {
                window.kayraVad.ready = false;
            }
        }

        function sampleVoiceActivity() {
            if (!analyser || !vadFrame) { return; }
            try {
                analyser.getFloatTimeDomainData(vadFrame);
            } catch (e) { return; }
            let sum = 0;
            for (let i = 0; i < vadFrame.length; i++) { sum += vadFrame[i] * vadFrame[i]; }
            const rms = Math.sqrt(sum / vadFrame.length);

            // The threshold is a multiple of the LEARNED floor, not a constant: a quiet room
            // and a noisy one need different numbers, and a fixed threshold is wrong in both.
            const margin = window.kayraSpeaking ? tuning.vadEchoMargin : tuning.vadMargin;
            const threshold = Math.max(tuning.vadFloorMin, noiseFloor * margin);
            const voice = rms > threshold;

            if (voice) {
                lastVoiceMs = Date.now();
            } else if (!window.kayraSpeaking) {
                // Adapt only on quiet frames, and NEVER while Kayra is audible. Learning the
                // floor from her own voice would raise it until the detector went deaf.
                noiseFloor = (noiseFloor * 0.95) + (rms * 0.05);
            }
            window.kayraVad.rms = rms;
            window.kayraVad.floor = noiseFloor;
            window.kayraVad.voice = voice;
            window.kayraVad.threshold = threshold;
        }

        // Shared normalization. Mirrors `voice_control.normalize_utterance` on the Python
        // side; the two must agree, and `tests/test_voice_control.py` compares them case for
        // case against this exact table.
        // mode "strip": every filler removed, the assistant's name included — the form the
        //               interrupt vocabulary is matched against ("Kayra, please stop" -> "stop").
        // mode "named": every filler removed EXCEPT the assistant's name, which is rewritten to
        //               the canonical "kayra" the lifecycle table is written in
        //               ("hey Vega, go to sleep" -> "kayra go to sleep").
        function normalizeWords(text, mode) {
            const raw = (text || "").toLowerCase().replace(/[.,!?;:'"]/g, " ").trim();
            if (!raw) return [];
            const words = raw.split(/\\s+/);
            const out = [];
            const name = window.kayraAssistantName || "kayra";
            for (let i = 0; i < words.length; i++) {
                const w = words[i];
                if (!w) continue;
                const isName = (w === name || w === "kayra");
                if (mode === "named" && isName) { out.push("kayra"); continue; }
                if (window.kayraInterruptFillers.indexOf(w) !== -1) continue;
                out.push(w);
            }
            return out;
        }

        function isInterruptExactly(words) {
            if (!words.length || words.length > 4) return false;
            if (window.kayraInterruptWords.indexOf(words.join(" ")) !== -1) return true;
            // "stop stop stop" is still a stop.
            return words.every(function (w) {
                return window.kayraSingleWordInterrupts.indexOf(w) !== -1;
            });
        }

        function looksLikeInterrupt(text) {
            // EXACT match on the whole utterance, filler words removed. A prefix test
            // would fire on "stop the music", which is a real command, not a barge-in.
            const words = normalizeWords(text, "strip");
            if (isInterruptExactly(words)) return true;

            // TAIL match, and ONLY while Kayra is audible. See `window.kayraSpeaking` above:
            // during playback the buffer is polluted by echo, so the user's actual word is at
            // the end of the probe rather than being the whole of it. Outside playback this
            // branch is skipped entirely, which is what keeps "close this tab and stop" a
            // normal command.
            if (!window.kayraSpeaking) return false;
            for (let size = 1; size <= 4 && size <= words.length; size++) {
                if (window.kayraInterruptWords.indexOf(
                        words.slice(words.length - size).join(" ")) !== -1) {
                    return true;
                }
            }
            return false;
        }

        // Lifecycle commands: whole-utterance only, no tail matching, ever. Returns the kind
        // string or null.
        function looksLikeControl(text) {
            const words = normalizeWords(text, "named");
            if (!words.length || words.length > 5) return null;
            const joined = words.join(" ");
            for (let i = 0; i < window.kayraControlPhrases.length; i++) {
                if (window.kayraControlPhrases[i][0] === joined) {
                    return window.kayraControlPhrases[i][1];
                }
            }
            return null;
        }

        function startContinuousRecognition(lang, silenceMs, interruptWords, fillers,
                                            controlPhrases, assistantName, tuningOverride) {
            silenceLimit = silenceMs || 800;
            if (tuningOverride) {
                for (const key in tuningOverride) {
                    if (Object.prototype.hasOwnProperty.call(tuning, key) &&
                        typeof tuningOverride[key] === "number") {
                        tuning[key] = tuningOverride[key];
                    }
                }
            }
            segments = [];
            interimText = "";
            lastVoiceMs = Date.now();
            window.speechQueue = [];
            window.kayraInterrupt = null;
            window.kayraControl = null;
            window.kayraInterruptWords = interruptWords || [];
            window.kayraInterruptFillers = fillers || [];
            window.kayraControlPhrases = controlPhrases || [];
            window.kayraAssistantName = (assistantName || "kayra").toLowerCase();
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

            // Re-arm the VAD sampler if the stream is already open (a resume after a pause),
            // otherwise acquire the device and start it.
            if (window.kayraMicStream && analyser) {
                if (vadTimer) { clearInterval(vadTimer); }
                lastVoiceMs = Date.now();
                vadTimer = setInterval(sampleVoiceActivity, tuning.vadIntervalMs);
            } else {
                primeProcessedMicrophone();
            }

            recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
            recognition.lang = lang || 'en-US';
            recognition.continuous = true;
            recognition.interimResults = true;
            // N-best. This is the single most important line for accuracy: it is what lets
            // the repair stage in Python prefer a reading the recognizer ITSELF considered,
            // rather than rewriting a word into something nobody heard.
            try { recognition.maxAlternatives = tuning.maxAlternatives; } catch (e) {}

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
                    const result = event.results[i];
                    if (result.isFinal) {
                        finalTranscript += result[0].transcript + " ";
                        // Keep every reading the recognizer offered for this segment, with
                        // its confidence. Bounded on both axes so a long dictation cannot
                        // grow the renderer's memory.
                        if (segments.length < MAX_SEGMENTS) {
                            const alternatives = [];
                            const count = Math.min(result.length, tuning.maxAlternatives);
                            for (let a = 0; a < count; a++) {
                                alternatives.push({
                                    text: String(result[a].transcript || "").trim(),
                                    confidence: (typeof result[a].confidence === "number")
                                        ? result[a].confidence : null
                                });
                            }
                            segments.push({ text: String(result[0].transcript || "").trim(),
                                            alternatives: alternatives });
                        }
                    } else {
                        interimTranscript += result[0].transcript + " ";
                    }
                }
                if (finalTranscript) {
                    currentText += finalTranscript;
                }
                // Held so the endpointer can tell "the user stopped" from "the recognizer has
                // not committed yet", and so an interim that never finalizes is delivered
                // instead of silently discarded. Dropping it was a real cause of a spoken
                // word producing nothing at all.
                interimText = interimTranscript;

                // Fast path: publish an interruption the moment we see one, without
                // waiting for the silence timer or for the sentence to be finalized.
                const probe = (currentText + " " + interimTranscript).trim();
                if (!window.kayraInterrupt && looksLikeInterrupt(probe)) {
                    window.kayraInterrupt = { text: probe, at: Date.now(), start: utteranceStart };
                }
                // Same fast path for the lifecycle commands, so "exit" spoken over a long
                // answer ends the process immediately instead of after the ~800ms VAD window
                // and the translation round-trip. Whole-utterance match only.
                if (!window.kayraControl) {
                    const kind = looksLikeControl(probe);
                    if (kind) {
                        window.kayraControl = { text: probe, kind: kind, at: Date.now(),
                                                start: utteranceStart };
                    }
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

            // ── ENDPOINTING ──────────────────────────────────────────
            // TWO conditions, not one. The recognizer must have gone quiet AND the room must
            // have gone quiet. Either alone is wrong in a way that costs words: results lag
            // the sound, so recognizer-silence alone ends the utterance while the user is
            // still speaking; and energy alone would wait out every background noise.
            if (checkInterval) clearInterval(checkInterval);
            checkInterval = setInterval(() => {
                if (!isSpeaking) { return; }
                const now = Date.now();
                const sinceResult = now - lastResultTime;
                // With no VAD (no WebAudio, or permission refused) this degrades exactly to
                // the old recognizer-only behaviour rather than failing.
                const sinceVoice = window.kayraVad.ready ? (now - lastVoiceMs) : sinceResult;

                const pendingInterim = interimText.trim().length > 0;
                const settled = currentText.trim();
                const shortCommand = settled && settled.split(/\\s+/).length <= 3;

                let quietNeeded = silenceLimit;
                if (pendingInterim) {
                    // Uncommitted words: wait for them. This is the whole reason short
                    // commands used to vanish.
                    quietNeeded = Math.max(silenceLimit, tuning.interimGraceMs);
                } else if (shortCommand) {
                    // Committed and short: answer promptly. "stop" should not cost 800ms.
                    quietNeeded = Math.min(silenceLimit, tuning.fastEndpointMs);
                }

                const recognizerQuiet = sinceResult > quietNeeded;
                const roomQuiet = sinceVoice > tuning.vadHangoverMs;
                const hardTimeout = sinceResult > tuning.maxWaitMs;

                if (!((recognizerQuiet && roomQuiet) || hardTimeout)) { return; }
                flushUtterance(hardTimeout ? "timeout" : "endpoint");
            }, 60);
        }

        // Publishes the assembled utterance. Everything the repair stage needs to be
        // conservative travels WITH it — the alternatives, the confidence, whether any of it
        // was still uncommitted, and whether Kayra was audible while it was captured.
        function flushUtterance(reason) {
            const uncommitted = interimText.trim();
            let text = currentText.trim();
            if (!text && uncommitted) {
                // The recognizer never committed these words. Delivering them flagged is
                // strictly better than delivering nothing: the repair stage knows not to
                // trust them, and the user's word is at least heard.
                text = uncommitted;
            }
            if (text) {
                window.speechQueue.push({
                    text: text,
                    start: utteranceStart,
                    // The utterance physically ended when results stopped arriving, not now.
                    end: lastResultTime,
                    segments: segments.slice(0, MAX_SEGMENTS),
                    uncommitted: (!currentText.trim() && uncommitted) ? uncommitted : "",
                    duringSpeech: !!window.kayraSpeaking,
                    reason: reason || "endpoint",
                    vadReady: !!window.kayraVad.ready
                });
                while (window.speechQueue.length > MAX_QUEUE) {
                    window.speechQueue.shift();
                }
            }
            currentText = "";
            interimText = "";
            segments = [];
            isSpeaking = false;
            statusEl.textContent = "listening";
        }

        // Discards the partially-accumulated utterance without touching the session. Python
        // calls this through `clear_queue()` after a barge-in: the buffer at that moment holds
        // echo plus the interrupt word, and leaving it in place meant the silence timer
        // delivered that string as the user's next command one VAD window later.
        function resetUtteranceBuffer() {
            currentText = "";
            interimText = "";
            segments = [];
            isSpeaking = false;
            lastResultTime = Date.now();
            lastVoiceMs = Date.now();
            utteranceStart = Date.now();
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
            // The VAD timer stops with recognition; the STREAM and the AudioContext are kept,
            // because pausing must not tear down the capture path — reacquiring the device is
            // the expensive part, and `pause_listening` exists precisely to avoid it.
            if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
            if (recognition) {
                recognition.onend = null;
                recognition.stop();
            }
        }
    </script>
</body>
</html>"""


# `pause_listening()` / `resume_listening()` are a THIRD thing, separate from both the
# lifecycle states above and from the audio barge-in:
#
#   barge-in          cancels what the assistant is SAYING          (tts_engine.stop)
#   listening pause   stops what the assistant is HEARING           (this pair)
#   shutdown          ends the process                              (app._force_shutdown)
#
# They are deliberately not expressed as lifecycle states: a paused engine is still READY or
# LISTENING, still owns its browser, and still recovers from a crash. Pausing is a property of
# the microphone, not a stage in the session's life.


def _capture_tuning():
    """
    Endpointing and VAD tuning, resolved once from the environment.

    These are the numbers the capture pipeline is shaped by, and they are configurable for the
    same reason the automation bounds are: a quiet office and a noisy room genuinely need
    different hangovers, and the alternative to a setting is a user with no way to fix a
    recognizer that keeps cutting them off. Every value is range-clamped, so a malformed
    `.env` degrades to the measured defaults instead of producing an endpointer that never
    fires.
    """
    from kayra.core.config import env_float, env_int
    return {
        "fastEndpointMs": env_int("STT_FAST_ENDPOINT_MS", 420, 150, 2000),
        "interimGraceMs": env_int("STT_INTERIM_GRACE_MS", 1400, 300, 5000),
        "maxWaitMs": env_int("STT_MAX_UTTERANCE_WAIT_MS", 6000, 1500, 30000),
        "vadHangoverMs": env_int("STT_VAD_HANGOVER_MS", 500, 100, 3000),
        "vadMargin": env_float("STT_VAD_MARGIN", 3.2, 1.2, 20.0),
        "vadEchoMargin": env_float("STT_VAD_ECHO_MARGIN", 7.0, 1.5, 40.0),
        "vadFloorMin": env_float("STT_VAD_FLOOR_MIN", 0.004, 0.0001, 0.2),
        "vadIntervalMs": env_int("STT_VAD_INTERVAL_MS", 50, 10, 250),
        "maxAlternatives": env_int("STT_MAX_ALTERNATIVES", 5, 1, 10),
    }


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


def _notify_backend_recovery(phase, reason=""):
    """
    Tells the backend manager that a session recovery started or finished.

    Guarded and lazy on purpose. This sits on the recovery path, which is the least
    appropriate place in the system for an import error or a listener bug to matter: a
    reporting concern must never be able to stop a dead speech session from being rebuilt.
    """
    try:
        from kayra.input.stt_backend import get_stt_backend_manager
        get_stt_backend_manager().note_recovery(phase, reason)
    except Exception:
        pass


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

    _listening_paused = False           # class-level default: the engine starts listening

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
        # Endpointing and VAD tuning, handed to the page at `startContinuousRecognition` so
        # there is ONE source of truth for these numbers rather than a set in Python and a
        # second set baked into the JavaScript.
        self.tuning = _capture_tuning()
        # Diagnostics for the capture pipeline. `last_capture` describes the most recent
        # utterance; the warning latch keeps a degraded pipeline to one line per session
        # instead of one per utterance.
        self.last_capture = {}
        self._warned_no_vad = False
        # The name the user addresses the assistant by. The page needs it because "turn off
        # Vega" has to reach the shutdown table with the same meaning "turn off Kayra" does,
        # and the lifecycle phrases are stored in their canonical "kayra" form.
        try:
            from kayra.core.config import assistant_name as _assistant_name
            self.assistant_alias = (_assistant_name() or "kayra").strip().lower()
        except Exception:
            self.assistant_alias = "kayra"
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
        # Sampled by `poll_controls`; read by the voice state machine for the assistant
        # visual. Initialised here so a read before the first poll is False, not an error.
        self._voice_active = False
        self._page_status = ""
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
        self._start_recognition()

        # Late-spawning renderers/utilities are not children yet at driver creation.
        self._record_owned_processes()

    # ┌────────────────────────────────────────────────────────────────┐
    # │                     LISTENING PAUSE / RESUME                   │
    # └────────────────────────────────────────────────────────────────┘
    # Pausing listening is NOT shutting the engine down, and the difference is the whole
    # point of this pair. `shutdown()` tears the browser session down and reaps its processes;
    # a user who pauses the microphone for a phone call and resumes a minute later would pay
    # the full 1.3-2.5s session rebuild for it, and every recovery path would have to run.
    #
    # So: the browser session, the driver, the loopback page server and the owned-PID ledger
    # all stay exactly as they are. Only the page's SpeechRecognition object is stopped, which
    # is what actually releases the microphone — Chrome drops the capture when recognition
    # ends, and the tab's recording indicator goes out.

    def _start_recognition(self):
        """(Re)starts continuous recognition on the page with this engine's configuration."""
        self.driver.execute_script(
            "startContinuousRecognition(arguments[0], arguments[1], arguments[2], arguments[3],"
            " arguments[4], arguments[5], arguments[6]);",
            self.language,
            self.silence_limit_ms,
            INTERRUPT_PHRASES,
            sorted(INTERRUPT_FILLERS),
            _CONTROL_PHRASE_TABLE,
            self.assistant_alias,
            self.tuning,
        )

    @property
    def listening_paused(self):
        return bool(getattr(self, "_listening_paused", False))

    def pause_listening(self):
        """
        Stops recognition and releases the microphone, keeping the session alive.

        Returns True when the engine is paused afterwards, whether or not this call is what
        paused it — the caller wants the resulting STATE, not a report of who won a race.
        """
        if self.state in (SttState.STOPPING, SttState.STOPPED, SttState.FAILED):
            return False
        self._listening_paused = True
        try:
            self._raw_script("stopContinuousRecognition();")
        except Exception:
            # A failed script call still leaves the flag set, so `capture()` stops handing
            # utterances upward. Better to be deaf than to claim a pause that did not happen
            # in one place and did in another.
            pass
        try:
            self.clear_queue()
            self.set_assistant_status("Listening paused")
        except Exception:
            pass
        return True

    def resume_listening(self):
        """Restarts recognition. Rebuilds the session first if it died while paused."""
        self._listening_paused = False
        if self.state in (SttState.STOPPING, SttState.STOPPED, SttState.FAILED):
            return False
        try:
            if not self.is_session_alive():
                # A browser that died during the pause is exactly the case `recover` exists
                # for; resuming has to be able to survive it.
                self.recover("resume after pause")
            else:
                self._start_recognition()
            self.clear_queue()          # anything captured mid-restart is not a command
            self.set_assistant_status("Listening...")
            return True
        except Exception:
            return False

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

        warning(Subsystem.STT, f"{label} stopped being able to transcribe ({reason}). "
                               f"Switching to another browser.")

        with self._lifecycle_lock:
            self._teardown_session(quiet=True)
            self.driver = None
            self.browser = None
            self.state = SttState.NOT_STARTED
            try:
                self._start_session()
            except Exception as e:
                self.state = SttState.FAILED
                error(Subsystem.STT, f"No usable browser for speech input: {e}")
                return False

        self.state = SttState.LISTENING
        return True

    # ──────────────────────────────────────────────────────────────────────
    #                    EXPLICIT BACKEND SELECTION (live)
    # ──────────────────────────────────────────────────────────────────────

    def backend_key(self):
        """The key of the browser ACTUALLY running recognition, or None."""
        return self.browser.key if self.browser is not None else None

    def backend_label(self):
        """The label of the browser ACTUALLY running recognition, or None."""
        return self.browser.label if self.browser is not None else None

    def switch_backend(self, preference):
        """
        Changes which browser runs recognition, on a LIVE engine. Returns (ok, detail).

        `preference` is a browser key ("chrome", "edge", …) or "auto"/None for the existing
        capability-driven selection.

        WHY THE OLD SESSION IS TORN DOWN FIRST, AND FULLY
        -------------------------------------------------
        Two live sessions would mean two browsers holding the microphone. `_teardown_session`
        followed by `_await_owned_termination` is the same order `recover()` uses, for the same
        reason, and it is what keeps the "no duplicate STT session, no leaked browser process"
        guarantee true across a switch. Only PIDs this engine recorded are reaped — the user's
        own Chrome windows are never in `owned_pids`, so a switch cannot touch them.

        WHY A FAILED SWITCH RESTORES THE PREVIOUS BACKEND
        -------------------------------------------------
        Leaving Kayra deaf because a browser the user named could not start would turn a wrong
        setting into a broken assistant. The previous backend is brought back and the failure
        is reported, so the requested and active values genuinely differ and the UI can say so.
        Returning True here on a failed start would be the exact lie this whole path exists to
        prevent.
        """
        requested = (str(preference).strip().lower() if preference else "auto")
        if requested in ("", "auto", "default"):
            requested = "auto"
        target = None if requested == "auto" else requested

        previous_pref = self.preferred_browser
        previous_rejects = set(self._rejected_browsers)
        was_paused = self.listening_paused

        if self.state in (SttState.STOPPING, SttState.STOPPED, SttState.FAILED) \
                and self.driver is None and self.state != SttState.FAILED:
            return False, "the speech session is shutting down"

        with self._lifecycle_lock:
            self.preferred_browser = target
            # A browser rejected earlier in this session may be exactly the one the user is
            # now naming — a machine that was offline when Kayra started is the ordinary
            # case. An explicit request clears that session-scoped verdict and re-probes.
            if target:
                self._rejected_browsers.discard(target)

            self._teardown_session(quiet=True)
            self._await_owned_termination(timeout=6.0)
            self.driver = None
            self.browser = None
            self.state = SttState.NOT_STARTED

            try:
                self._start_session(strict=bool(target))
            except Exception as exc:
                detail = str(exc).splitlines()[0][:160]
                # Put the previous backend back rather than leaving the assistant deaf.
                self.preferred_browser = previous_pref
                self._rejected_browsers = previous_rejects
                self._teardown_session(quiet=True)
                self.driver = None
                self.browser = None
                self.state = SttState.NOT_STARTED
                try:
                    self._start_session()
                    self.state = SttState.LISTENING if not was_paused else SttState.READY
                    restored = self.backend_label() or "none"
                except Exception:
                    self.state = SttState.FAILED
                    return False, f"{detail}; and the previous backend could not be restored"
                return False, f"{detail}; still on {restored}"

            self.state = SttState.READY

        # The recognition loop only runs when the microphone is meant to be open. A switch
        # performed while listening was paused must NOT quietly reopen it — pause is a
        # separate axis and this operation has no business changing it.
        if was_paused:
            self._listening_paused = True
            try:
                self._raw_script("stopContinuousRecognition();")
            except Exception:
                pass
        else:
            self.state = SttState.LISTENING
        self.clear_queue()
        return True, self.backend_label() or ""

    def _start_session(self, strict=False):
        """
        Brings up the single owned browser session. Raises on failure after marking FAILED,
        so a caller cannot mistake a dead subsystem for a working one.

        Tries each candidate browser in preference order and VERIFIES that recognition actually
        works before declaring the session ready. Kayra does not require Chrome specifically -
        Edge ships on every Windows 11 machine and has its own backend - but it does require a
        browser that can genuinely transcribe, and that cannot be determined from the browser's
        name or from the presence of `webkitSpeechRecognition`.

        STRICT MODE, AND WHY IT HAD TO EXIST
        ------------------------------------
        `strict=True` restricts the attempt to `self.preferred_browser` and nothing else. It is
        used when the USER has named a backend from Settings, and it is the difference between
        a setting and a suggestion: with the ordinary (non-strict) list, choosing "Google
        Chrome" on a machine where Chrome cannot reach a backend would quietly bring up Edge
        and report success, so the screen would read "Chrome" while Edge held the microphone.
        A named backend that cannot start is an honest, visible failure.

        `auto` never uses strict mode: the whole meaning of `auto` is "pick one that works",
        and the capability logic in `kayra.input.browsers` stays exactly as it was.
        """
        with self._lifecycle_lock:
            if self.driver is not None:
                return self.driver  # Already up: never build a second session.

            self.state = SttState.STARTING
            page_url = self._page_server.start()

            if strict and self.preferred_browser:
                options = tuple(spec for spec in browsers.discover_browsers()
                                if spec.key == self.preferred_browser)
                if not options:
                    self.state = SttState.FAILED
                    raise RuntimeError(
                        f"STT session failed to start: {self.preferred_browser} is not "
                        f"installed on this machine.")
            else:
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
                    info(Subsystem.STT, f"Backend: {spec.label} ({reason})")
                    self.state = SttState.READY
                    return self.driver

                failures.append(f"{spec.label}: {reason}")
                warning(Subsystem.STT,
                        f"{spec.label} cannot transcribe ({reason}); trying another browser.")
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
            # The BACKEND MANAGER announces the recovery, not this method. One owner per
            # event: the manager also publishes the transition that drives the assistant
            # visual's RECOVERING state, so a lost session shows as "Reconnecting" rather
            # than as a false "Listening paused" — and it cannot be announced twice.
            _notify_backend_recovery(
                "started",
                f"{reason} [attempt {self._recovery_count}/{self.MAX_RECOVERY_ATTEMPTS}]")

            # 1. Old session down first — never run two.
            #    session_dead=True: we are here precisely because it stopped responding, so
            #    there is nothing to say to it politely.
            self._teardown_session(quiet=True, session_dead=True)
            # 2. Verify the OS actually released them before claiming new ones. Short grace:
            #    these are orphans of a dead driver, not a cooperative shutdown.
            survivors = self._await_owned_termination(timeout=1.0)
            if survivors:
                warning(Subsystem.STT,
                        f"Old speech processes would not die: {sorted(survivors)}")

            # 3. Only now build the replacement.
            try:
                self._start_session()
                self.state = SttState.LISTENING if not self.listening_paused else SttState.READY
                _notify_backend_recovery("finished")
                return self.driver
            except Exception as e:
                self.state = SttState.FAILED
                _notify_backend_recovery("finished")
                error(Subsystem.STT, f"Recovery failed: {e}")
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
        Purges everything the recognizer is holding: the finalized queue, the latched
        interrupt/control flags, AND the partially-accumulated sentence.

        RESETTING `currentText` IS THE PART THAT MATTERS. It used to be left alone, so after a
        barge-in the recognizer still held the echo it had accumulated plus the interrupt word
        itself — and the silence timer pushed that whole polluted string onto the queue ~800ms
        later, where it arrived as the user's next "command". Clearing the queue without
        clearing the buffer only delayed the problem by one VAD window.
        """
        self._script(
            "window.speechQueue = [];"
            " window.kayraInterrupt = null;"
            " window.kayraControl = null;"
            " if (typeof resetUtteranceBuffer === 'function') { resetUtteranceBuffer(); }"
        )

    def set_speaking(self, speaking):
        """
        Tells the page whether Kayra's own voice is currently audible.

        This gates the tail-matching branch of `looksLikeInterrupt` — see the comment on
        `window.kayraSpeaking` in the page source. Callers should prefer `poll_controls`,
        which carries the flag in the same round-trip it uses to read the flags back.
        """
        self._script("window.kayraSpeaking = arguments[0];", bool(speaking))

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

    def poll_controls(self, speaking=None):
        """
        ONE round-trip that publishes the speaking flag and reads back both fast-path flags.

        Returns `(interrupt, control)`, either of which may be None. Consuming clears them.

        Combining the three is not micro-optimisation: the watcher runs this at ~17Hz while
        Kayra is talking, and each Selenium command is an HTTP request over the driver lock
        that the capture loop also needs. Three calls per tick would triple that contention
        for information that is read and written at exactly the same instant.
        """
        payload = self._script(
            "if (arguments[0] !== null) { window.kayraSpeaking = arguments[0]; }"
            " var i = window.kayraInterrupt, c = window.kayraControl;"
            " window.kayraInterrupt = null; window.kayraControl = null;"
            " var v = window.kayraVad || {};"
            " return {interrupt: i || null, control: c || null,"
            "         voice: !!v.voice, vadReady: !!v.ready,"
            "         status: (document.getElementById('status') || {}).textContent || ''};",
            None if speaking is None else bool(speaking),
        )
        if not isinstance(payload, dict):
            self._voice_active = False
            return None, None
        # Voice-activity and page status ride along in the SAME round-trip, so the orb learns
        # that the user is speaking at no extra cost. A separate poll for this would be a
        # second Selenium command per tick over the same driver lock the capture loop needs —
        # exactly the contention `poll_controls` was created to remove.
        self._voice_active = bool(payload.get("voice")) and bool(payload.get("vadReady"))
        self._page_status = str(payload.get("status") or "")
        interrupt = payload.get("interrupt")
        control = payload.get("control")
        return (interrupt if isinstance(interrupt, dict) and interrupt.get("text") else None,
                control if isinstance(control, dict) and control.get("kind") else None)

    @property
    def voice_active(self):
        """
        True when the page's VAD last reported the user's voice above the noise floor.

        Sampled by `poll_controls`, which the local control watcher already runs — this is a
        READ of a value the system was collecting anyway, not a new observation. It is
        deliberately a best-effort indicator for the assistant visual and nothing else: no
        decision in Kayra is made from it, so a stale sample costs a frame of animation and
        never a wrong action.
        """
        return bool(getattr(self, "_voice_active", False))

    @property
    def page_status(self):
        """The recognition page's own status string ("listening", "stopped", …), or ""."""
        return str(getattr(self, "_page_status", ""))

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
                # Paused: return immediately rather than blocking the caller's thread in a
                # poll loop against a page that is not recognising anything. The caller waits
                # on an event instead, so a paused microphone costs no polling at all.
                if getattr(self, "_listening_paused", False):
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
                    result = {
                        "text": format_query(translated_text),
                        "raw": raw_text,
                        "start_ms": float(item.get("start") or now_ms()),
                        "end_ms": float(item.get("end") or now_ms()),
                        # Everything the repair stage needs to be conservative, carried WITH
                        # the utterance rather than re-derived from it afterwards.
                        "segments": item.get("segments") or [],
                        "uncommitted": item.get("uncommitted") or "",
                        "during_speech": bool(item.get("duringSpeech")),
                        "endpoint_reason": item.get("reason") or "",
                        "vad": bool(item.get("vadReady")),
                        # Only meaningful when the recognizer produced a single committed
                        # segment: alternatives for a two-segment utterance would have to be
                        # a cross product, which is neither what the recognizer meant nor
                        # something a conservative stage should invent.
                        "alternatives": self._alternatives(item),
                    }
                    self._note_capture(result)
                    return result

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

                    error(Subsystem.STT, f"Recognition page reported: {error_msg}")
                    return {"text": "", "raw": "", "start_ms": now_ms(), "end_ms": now_ms()}

                # Super-low CPU polling sleep interval (50ms) to ensure minimal host thread impact
                time.sleep(poll_interval)

        except KeyboardInterrupt:
            return None

    @staticmethod
    def _alternatives(item):
        """
        The recognizer's own N-best readings for the utterance, best first.

        Returned ONLY for a single committed segment. For a multi-segment utterance the
        honest answer is "no alternatives for the whole thing" — combining per-segment lists
        would manufacture readings the recognizer never proposed, which is the exact failure
        mode the repair stage exists to avoid.
        """
        segments = item.get("segments") or []
        if len(segments) != 1:
            return []
        out = []
        for alternative in (segments[0].get("alternatives") or [])[:10]:
            text = str(alternative.get("text") or "").strip()
            if not text:
                continue
            out.append({"text": text, "confidence": alternative.get("confidence")})
        return out

    def _note_capture(self, result):
        """
        Records the last capture for diagnostics, and warns ONCE about a degraded pipeline.

        A degraded capture path — no echo cancellation, or no VAD — is the single most useful
        thing to know when transcripts start coming back wrong, and it is invisible without
        this: the assistant keeps working, just less accurately.
        """
        self.last_capture = {
            "endpoint_reason": result.get("endpoint_reason"),
            "vad": result.get("vad"),
            "during_speech": result.get("during_speech"),
            "uncommitted": bool(result.get("uncommitted")),
            "alternatives": len(result.get("alternatives") or []),
        }
        if not result.get("vad") and not self._warned_no_vad:
            self._warned_no_vad = True
            print_warning("Speech endpointing is running without voice-activity detection "
                          "(WebAudio unavailable or the microphone was refused). Utterances "
                          "will be ended by recognizer silence alone.")

    def audio_pipeline_report(self):
        """
        What the capture pipeline is ACTUALLY doing, read from the live page.

        Reported rather than assumed, for the same reason the speech-device card reports the
        provider the ONNX session really got: constraints are a request, and an assistant
        that claims echo cancellation it was never granted is misdescribing the one thing
        that explains its mistakes.
        """
        report = {"settings": None, "error": "", "vad": None, "tuning": dict(self.tuning)}
        try:
            payload = self._script(
                "return {settings: window.kayraAudioSettings || null,"
                " error: window.kayraAudioError || '',"
                " vad: window.kayraVad || null};", recover=False)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            report.update(settings=payload.get("settings"),
                          error=payload.get("error") or "",
                          vad=payload.get("vad"))
        return report

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
                info(Subsystem.SHUTDOWN, f"Closing the headless {label} session",
                     correlate=False)

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
            warning(Subsystem.SHUTDOWN,
                    f"Speech processes still alive after shutdown: {sorted(survivors)}")

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
            warning(Subsystem.STT, f"Translation unavailable, using raw transcript: {e}")
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


# `is_interrupt_phrase`, `classify_control` and `interrupt_in_tail` are imported at the top of
# this module from `core.voice_control` and re-exported here, which is where every caller and
# every test has always found them. They used to be implemented in this file; the copy was
# removed when the vocabulary moved, because two implementations of "is this a barge-in?" is
# precisely the kind of drift the JS/Python pair in this module already has to guard against.


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
