# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation/screen_ai.py                        │
# │        Seeing the screen: describe, explain, read, copy, record        │
# └────────────────────────────────────────────────────────────────────────┘
"""
"What's on my screen?", "explain this error", "read this to me", "copy the text from the
screen", "record my screen".

Only ever on request — nothing here runs in the background, and every screenshot is a temp
file deleted as soon as it has been read. Vision goes to the LOCAL model first
(`automation.ai.see`: LM Studio), so by default the picture never leaves the machine; a cloud
model is used only when one is configured and the local one cannot answer. With no vision
model at all, the OCR text of the screen is handed to the text model instead, and with no
model at all the OCR text itself is the answer.
"""

import os
import re
import tempfile

from kayra.automation import ai
from kayra.automation.policy import ActionResult


def _L():
    from kayra.automation import linux
    return linux


def capture(scale=0.6, monitor_only=True):
    """JPEG bytes of the focused monitor, downscaled for the model. None when unavailable."""
    L = _L()
    if not L._have("grim"):
        return None
    handle, path = tempfile.mkstemp(prefix="kayra-see-", suffix=".jpg")
    os.close(handle)
    try:
        argv = ["grim", "-t", "jpeg", "-q", "80", "-s", str(scale)]
        monitor = L._focused_monitor() if monitor_only else None
        if monitor:
            argv += ["-o", str(monitor.get("name"))]
        if not L._ok(*argv, path, timeout=8.0):
            return None
        with open(path, "rb") as image:
            return image.read() or None
    except OSError:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def screen_text():
    """Every word OCR can read on the focused monitor, in reading order."""
    L = _L()
    if not (L._have("grim") and L._have("tesseract")):
        return ""
    monitor = L._focused_monitor()
    handle, path = tempfile.mkstemp(prefix="kayra-ocr-", suffix=".png")
    os.close(handle)
    try:
        argv = ["grim"] + (["-o", str(monitor.get("name"))] if monitor else []) + [path]
        if not L._ok(*argv, timeout=8.0):
            return ""
        code, text = L._run("tesseract", path, "stdout", "--psm", "3", timeout=25.0)
        return text.strip() if code == 0 else ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _without_kayra(text):
    """Drop lines that are obviously Kayra's own chat echoing the question back."""
    return "\n".join(line for line in text.splitlines()
                     if not re.search(r"\b(kayra|screen describe|screen explain)\b", line, re.I))


def describe(question="What is on my screen? Describe it briefly and point out anything that "
                      "needs my attention."):
    image = capture()
    if image is None:
        return ActionResult.unsupported("I can't capture the screen here. Install grim.")
    answer, provider = ai.see(question, image)
    if answer:
        return ActionResult.success(answer, provider=provider)
    text = _without_kayra(screen_text())
    if not text:
        return ActionResult.failure("I couldn't make out anything on the screen.")
    spoken = ai.summarize(
        "You are Kayra. The user asked about their screen. You only have the OCR text of it. "
        "Answer in at most 4 spoken sentences.", f"Question: {question}\n\nScreen text:\n{text[:6000]}",
        fallback="I can't run a vision model right now. The screen says: " + " ".join(text.split())[:300])
    return ActionResult.success(spoken, provider="ocr")


def explain(question=""):
    return describe("Look at the error, warning or message on this screen. Say what it means "
                    "in plain words and the most likely fix, in at most 4 sentences. "
                    + (question or ""))


def read_aloud():
    text = " ".join(_without_kayra(screen_text()).split())
    if not text:
        return ActionResult.not_found("I can't read any text on the screen.")
    cut = text[:900]
    return ActionResult.success(cut[: cut.rfind(".") + 1] or cut)


def copy_text():
    text = _without_kayra(screen_text())
    if not text:
        return ActionResult.not_found("I can't read any text on the screen.")
    if _L().clipboard_set(text):
        words = len(text.split())
        return ActionResult.success(f"Copied {words} words from the screen to the clipboard.")
    return ActionResult.failure("I read the text but couldn't put it on the clipboard.")


def record(start=True):
    L = _L()
    tool = "omarchy-capture-screenrecording"
    if L._have(tool):
        if start:
            ok = L.RUNNER.spawn([tool, "--fullscreen", "--with-desktop-audio"])
            return ActionResult.success("Recording your screen. Say \"screen stop recording\" "
                                        "when you're done.") if ok else \
                ActionResult.failure("The recorder didn't start.")
        ok = L._ok(tool, "--stop-recording", timeout=10.0)
        return ActionResult.success("Recording saved to your Videos folder.") if ok else \
            ActionResult.failure("I couldn't stop the recording.")
    if L._have("wf-recorder"):
        if start:
            folder = os.path.expanduser("~/Videos")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "kayra-recording.mp4")
            return ActionResult.success("Recording.") if L.RUNNER.spawn(
                ["wf-recorder", "-f", path]) else ActionResult.failure("The recorder didn't start.")
        return ActionResult.unsupported("Stop the recorder from its own window.")
    return ActionResult.unsupported("I don't have a screen recorder here.")


def vision_locate(target):
    """Ask the vision model where something is: (x, y) as fractions of the screen, or None."""
    image = capture(scale=0.5)
    if image is None:
        return None
    answer, _ = ai.see(f"Where on this screenshot is: {target}? Reply ONLY with two numbers "
                       "between 0 and 1: the horizontal then vertical position of its centre, "
                       "like '0.42 0.77'. Reply 'none' if it is not visible.", image)
    match = re.search(r"(0?\.\d+|1(?:\.0+)?|0)\s*[, ]\s*(0?\.\d+|1(?:\.0+)?|0)", answer or "")
    if not match:
        return None
    x, y = float(match.group(1)), float(match.group(2))
    return (x, y) if 0 <= x <= 1 and 0 <= y <= 1 else None
