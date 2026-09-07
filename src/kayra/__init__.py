"""
Kayra — a voice-driven Windows desktop assistant.

Import-time side effects are deliberately absent from this file. Importing `kayra` must never
start a model, open a browser, or touch the microphone; `kayra.app.bootstrap()` does that, and
only when called.
"""

__version__ = "2.0.0"
