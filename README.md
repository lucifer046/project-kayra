# Kayra Desktop Assistant

Kayra is a high-performance, intelligent desktop assistant designed to provide a robust, hands-free hardware and operating system control experience. Equipped with a hybrid **Centralized LLM Engine** and a continuous **Offline Speech Matrix** (Speech-to-Text & Kokoro TTS), Kayra translates your natural language spoken or typed queries into instant hardware actions, real-time web searches, or publication-grade deep research reports.

---

## 🛠️ Installation & Quick Start

### 1. Prerequisites

Ensure you have **Python 3.10+** installed on your system.

### 2. Setup Virtual Environment & Install Dependencies

Create a isolated environment and install the required modules:

```bash
# Clone or navigate to your project directory
cd project-kayra

# Initialize the virtual environment
python -m venv .venv

# Activate the virtual environment
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies (automatically retrieves latest stable versions)
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy `.env.example` to `.env` in the root folder and update the API keys and configurations:

```ini
# Voice Configuration
INPUT_LANGUAGE=hi-IN
ASSISTANT_VOICE=am_adam

# Local Offline LLM Setup (Optional)
FORCE_ONLINE=False
LOCAL_BASE_URL=http://localhost:1234/v1
LOCAL_CHAT_MODEL=local-model
LOCAL_DECISION_MODEL=local-model

# Cloud APIs (Required for Hybrid/Online Mode)
CohereAPIKey=YOUR_COHERE_API_KEY
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
```

---

## 📖 User Manual & Automation Commands

This manual outlines the exact natural language phrases, keywords, and text inputs to speak or type to trigger Kayra's automation protocols.

### 1. Web & Application Launching

Intelligently launches local apps or opens domains directly in the default browser.

- **How to say it:**
  - `"open notepad"` -> Launches the Notepad application.
  - `"open github.com"` -> Directly routes and opens GitHub in your browser.
  - `"open youtube.com, gmail.com, and linkedin.com"` -> Launches all three websites simultaneously in separate browser tabs.

### 2. Tab & Process Closer

Fuzzy-matches visible windows to close them. For browsers, it simulates target ALT-key bypasses to close specific active tabs without altering your windows.

- **How to say it:**
  - `"close notepad"` -> Instantly terminates Notepad.
  - `"close youtube"` -> Target and close active YouTube tabs in your browser.
  - `"close chrome"` -> Gracefully shuts down the entire Chrome browser.

### 3. System Hardware Operations

Directly communicates with system kernels and hardware controllers.

- **How to say it:**
  - `"mute sound"` / `"unmute"` -> Toggles global audio state.
  - `"volume up"` / `"volume down"` -> Increments/decrements volume by 10%.
  - `"increase volume by 25%"` / `"set volume to 80%"` -> Absolute volume calibration.
  - `"increase brightness to 75%"` / `"set brightness to 30%"` -> Changes monitor backlight.
  - `"lock the computer"` -> Locks your active Windows session.
  - `"shutdown the pc"` / `"restart system"` / `"sleep pc"` -> Power management.

### 4. Desktop & Window Management

Simulates clean structural layout changes to arrange your desktop.

- **How to say it:**
  - `"snap this window to the left"` / `"snap right"` -> Snaps the active window into a perfect split-screen.
  - `"maximize window"` / `"minimize window"` -> Adjusts active window state.
  - `"minimize all windows"` / `"show desktop"` -> Instantly clears the screen to reveal the desktop.
  - `"switch window"` / `"alt tab"` -> Cycles through active applications.
  - `"open task view"` -> Displays the Windows Task View board.
  - `"open action center"` -> Launches the Windows Notification panel.
  - `"open emoji picker"` -> Accesses the native Windows emoji keyboard.

### 5. Keyboard & Application Shortcuts

Quickly injects standard hardware shortcut combinations.

- **How to say it:**
  - `"undo that"` / `"redo that"` -> `Ctrl+Z` / `Ctrl+Y` triggers.
  - `"save the file"` -> Simulates `Ctrl+S`.
  - `"select all"` / `"find something"` -> `Ctrl+A` / `Ctrl+F` triggers.
  - `"open a new tab"` / `"close this tab"` -> Browser tab management triggers.
  - `"refresh this page"` / `"reload page"` -> Re-loads the active tab.
  - `"go fullscreen"` -> Toggles `F11`.
  - `"zoom in"` / `"zoom out"` / `"reset zoom"` -> Universal zoom levels adjustments.
  - `"open task manager"` -> Launches Windows Task Manager (`Ctrl+Shift+Esc`).
  - `"open run dialog"` -> Launches the Windows Run dialog (`Win+R`).

### 6. Media Playback Controls

Injects global OS-level multimedia keystrokes compatible with Spotify, YouTube, VLC, etc.

- **How to say it:**
  - `"pause the music"` / `"resume playback"` -> Toggles play/pause states.
  - `"skip song"` / `"next track"` -> Plays the next media file.
  - `"previous track"` / `"go back"` -> Plays the previous media file.
  - `"stop media"` -> Stops all background player actions.

### 7. Instant System Telemetry

Pulls real-time system diagnostics directly from PowerShell and WMI interfaces.

- **How to say it:**
  - `"check battery status"` -> Outputs exact battery percentage and power state (charging/discharging).
  - `"how much ram is used"` -> Displays used, total, and free system memory.
  - `"check disk space"` -> Generates a storage usage breakdown for drives `C:` and `D:`.
  - `"cpu telemetry"` -> Displays processor model details, number of cores, and current loads.
  - `"system uptime"` -> Outputs how long your PC has been running since the last boot.
  - `"what is my ip address"` -> Retrieves local connection credentials.

### 8. Desktop Screenshots

Grabs a screen capture instantly. Automatically detects OneDrive-synced desktops and falls back to home directories or picture files to prevent failures.

- **How to say it:**
  - `"take a screenshot"` / `"capture screen"` -> Saves a timestamped capture directly to your Desktop.

### 9. Timers & Countdown Reminders

Arms an asynchronous background thread that executes a countdown and prompts a native Windows toast notification upon completion.

- **How to say it:**
  - `"set a timer for 10 seconds"`
  - `"set a timer for 5 minutes"`

### 10. Hardware Wi-Fi Controls

Quickly toggles your local Wi-Fi adapter using clean terminal calls.

- **How to say it:**
  - `"turn off wifi"` / `"disable wi-fi"` -> Shuts down the local Wi-Fi connection.
  - `"turn on wifi"` / `"enable wi-fi"` -> Powers up the local Wi-Fi adapter and reconnects.

### 11. Clipboard operations

Direct, high-speed simulated clipboard manipulation.

- **How to say it:**
  - `"copy that"` / `"paste that"` -> Simulates copy/paste actions.
  - `"copy text: [your text]"` -> Injects specific text straight into your system clipboard.
  - `"type [your text]"` / `"write [your text]"` -> Dynamically types designated strings at your active cursor.

### 12. RAG Web Search & Deep Research

Invokes advanced data collection and technical research whitepaper synthesis.

- **How to say it:**
  - `"search on google: [topic]"` / `"search on youtube: [video]"` -> Standard web query targets.
  - `"run deep research on [topic]"` / `"do deep research on [topic]"` -> Triggers Kayra's **advanced 6-stage autonomous research pipeline**. The model builds a research plan (10 queries), scrapes index pools, deep-extracts full page contents (stripping boilerplate via BeautifulSoup), performs gap analysis, completes follow-up scrapes, and compiles a comprehensive, publication-grade markdown technical whitepaper saved in the `Reports/` directory.

---

## Running the Assistant Modules

You can launch and test individual subsystems inside the sandbox or run the unified main controller:

```bash
# Run the Main Desktop Assistant Controller (STT + TTS + Automation Loop)
python main.py

# Test Windows Automation Subsystem directly (CLI prompt simulator)
python modules/automation_windows.py

# Test Advanced 6-stage Deep Research Engine
python modules/deep_research.py

# Test TTS Audio Synthesizer Sandbox
python tests/test_voice.py
```
