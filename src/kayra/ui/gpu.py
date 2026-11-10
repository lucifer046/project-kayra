# ┌────────────────────────────────────────────────────────────────────────┐
# │                                gpu.py                                  │
# │          Hardware-Accelerated Rendering, With a Safe Way Out           │
# └────────────────────────────────────────────────────────────────────────┘
"""
Whether the interface draws on the GPU, decided ONCE, at startup.

WHAT "GPU" MEANS FOR A QT WIDGETS APPLICATION
---------------------------------------------
Qt Widgets paint with the CPU raster engine; there is no switch that moves every widget to the
GPU (that would be a Qt Quick rewrite). What this enables is the part that matters for
smoothness:

  * the full-window animated layer — the ambient backdrop — is an OpenGL widget, drawn by the
    GPU at display rate (`backdrop.create_backdrop`);
  * the moment a window contains an OpenGL widget, Qt COMPOSES THE WHOLE WINDOW ON THE GPU:
    the widget layer is uploaded as a texture and blended over the GL layer. That is the real
    win: the backdrop animating no longer forces every translucent card, the transcript and the
    dock to be repainted on the CPU, which it did 8 times a second before;
  * vsync (`swapInterval = 1`) so animation frames land on display refreshes, and 4x MSAA so
    the GL paint engine's curves stay antialiased.

The same code path runs on X11 (GLX/EGL) and Wayland (EGL); nothing here picks a platform.

WHEN IT IS OFF
--------------
* `KAYRA_UI_GPU=off` — the user's switch;
* a headless platform (`offscreen`, `minimal`, `vnc`), which is what the test suite runs on;
* no OpenGL context can be created or made current;
* a SOFTWARE OpenGL renderer (llvmpipe, softpipe, swrast) — emulated GL is slower than Qt's
  raster engine, so "accelerated" would be a lie.

Then every widget falls back to CPU drawing, the application looks identical, and the reason
is logged once.
"""

import os
from dataclasses import dataclass

from PySide6.QtCore import Qt, QCoreApplication
from PySide6.QtGui import QSurfaceFormat, QGuiApplication

_SOFTWARE = ("llvmpipe", "softpipe", "swrast", "software rasterizer", "gdi generic")
_HEADLESS = ("offscreen", "minimal", "vnc", "linuxfb")

MSAA_SAMPLES = 4


@dataclass
class GpuStatus:
    enabled: bool = False
    renderer: str = ""
    reason: str = "not probed"

    def describe(self):
        if self.enabled:
            return f"GPU ({self.renderer})" if self.renderer else "GPU"
        return f"CPU ({self.reason})"


STATUS = GpuStatus()


def _switched_off():
    return str(os.environ.get("KAYRA_UI_GPU", "")).strip().lower() in (
        "0", "off", "false", "no", "cpu")


def configure_before_app():
    """
    Process-wide GL settings. MUST run before the QApplication exists — Qt reads both the
    shared-context attribute and the default surface format at construction.
    """
    if _switched_off():
        return
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(1)                      # vsync: one frame per display refresh
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)


def probe():
    """Decides `STATUS` once. Call after the QApplication exists; never raises."""
    global STATUS
    if _switched_off():
        STATUS = GpuStatus(False, "", "turned off by KAYRA_UI_GPU")
        return STATUS
    platform = (QGuiApplication.platformName() or "").lower()
    if platform in _HEADLESS:
        STATUS = GpuStatus(False, "", f"{platform} platform")
        return STATUS
    try:
        from PySide6.QtGui import QOpenGLContext, QOffscreenSurface
        context = QOpenGLContext()
        if not context.create():
            STATUS = GpuStatus(False, "", "no OpenGL context")
            return STATUS
        surface = QOffscreenSurface()
        surface.setFormat(context.format())
        surface.create()
        if not context.makeCurrent(surface):
            STATUS = GpuStatus(False, "", "OpenGL context could not be made current")
            return STATUS
        functions = context.functions()
        renderer = str(functions.glGetString(0x1F01) or "")      # GL_RENDERER
        context.doneCurrent()
    except Exception as exc:
        STATUS = GpuStatus(False, "", f"OpenGL unavailable ({type(exc).__name__})")
        return STATUS
    if any(name in renderer.lower() for name in _SOFTWARE):
        STATUS = GpuStatus(False, renderer, f"software OpenGL ({renderer})")
        return STATUS
    STATUS = GpuStatus(True, renderer.split("(")[0].strip() or renderer, "")
    return STATUS


def enabled():
    return STATUS.enabled
