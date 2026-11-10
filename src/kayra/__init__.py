"""
Kayra — a voice-driven desktop assistant for Windows and Linux (Omarchy first).

OS-specific behaviour lives in `kayra.platforms`; see docs/platforms/CROSS_PLATFORM.md.

Import-time side effects are deliberately absent from this file. Importing `kayra` must never
start a model, open a browser, or touch the microphone; `kayra.app.bootstrap()` does that, and
only when called.
"""

__version__ = "2.0.0"
