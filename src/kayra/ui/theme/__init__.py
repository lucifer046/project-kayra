"""
Kayra's design system: tokens in, stylesheet out.

`apply(app)` is the only thing the application layer needs to call.
"""

import math
from kayra.ui.theme.tokens import (
    Color, Font, Space, Radius, Size, Motion, Elevation,
    STATE_COLORS, STATE_LABELS, VERDICT_COLORS, VERDICT_LABELS,
    meter_color, with_alpha,
)
from kayra.ui.theme.stylesheet import build

__all__ = [
    "Color", "Font", "Space", "Radius", "Size", "Motion", "Elevation",
    "STATE_COLORS", "STATE_LABELS", "VERDICT_COLORS", "VERDICT_LABELS",
    "meter_color", "with_alpha", "build", "apply", "repolish",
]


def apply(app):
    """
    Installs the stylesheet and the base palette on a QApplication.

    The Qt palette is set as well as the stylesheet because a few native pieces — the text
    cursor, selection in unstyled contexts, tooltips before first polish — read the palette
    rather than QSS, and a light default there flashes white on a near-black window.
    """
    from PySide6.QtGui import QPalette, QColor
    from PySide6.QtCore import Qt

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(Color.base))
    palette.setColor(QPalette.WindowText, QColor(Color.text))
    palette.setColor(QPalette.Base, QColor(Color.inset))
    palette.setColor(QPalette.AlternateBase, QColor(Color.surface))
    palette.setColor(QPalette.Text, QColor(Color.text))
    palette.setColor(QPalette.Button, QColor(Color.overlay))
    palette.setColor(QPalette.ButtonText, QColor(Color.text))
    palette.setColor(QPalette.Highlight, QColor(Color.accent))
    palette.setColor(QPalette.HighlightedText, QColor(Color.text_on_accent))
    palette.setColor(QPalette.ToolTipBase, QColor(Color.overlay))
    palette.setColor(QPalette.ToolTipText, QColor(Color.text))
    palette.setColor(QPalette.PlaceholderText, QColor(Color.text_tertiary))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(Color.text_disabled))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(Color.text_disabled))
    app.setPalette(palette)

    app.setStyleSheet(build())
    return app


def repolish(widget):
    """
    Re-evaluates stylesheet selectors after a dynamic property changed.

    Qt does not re-run property selectors (`QPushButton[variant="accent"]`) when the property
    changes, so a widget that switches variant keeps its old look until it is unpolished and
    polished again. Every place that flips a styling property calls this; forgetting it is the
    single most common way Qt theming appears broken.
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()

_GLYPH_DPR = None


def glyph_dpr():
    """
    The device-pixel-ratio every drawn icon should be rasterised at.

    WHY THIS IS NOT JUST `2`. Each glyph is painted into a QPixmap once and reused; if that
    pixmap's ratio is lower than the screen's, Qt UPSCALES it at paint time and the 1.4px
    strokes go soft — which is exactly the "slightly rough or pixelated" edge quality that
    prompted this. Windows laptops ship at 1.25x, 1.5x and 1.75x as often as at 2x, and a
    3x tablet is not unusual.

    Rounded UP to a whole number, for two reasons: a fractional pixmap ratio puts stroke
    centres on half pixels (which is what makes a 1px border look grey rather than sharp), and
    rasterising slightly larger than needed only ever costs Qt a downscale, which is clean.
    Clamped at 4 so an unusual display cannot make every icon a megabyte.

    Read once and cached: this is called for every icon and `devicePixelRatio()` is a
    round-trip into the platform plugin. A user who drags the window to a differently-scaled
    monitor keeps the ratio the application started with, which is a downscale at worst.
    """
    global _GLYPH_DPR
    if _GLYPH_DPR is not None:
        return _GLYPH_DPR
    ratio = 2
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            screens = app.screens()
            if screens:
                ratio = max(1, min(4, int(math.ceil(max(s.devicePixelRatio()
                                                        for s in screens)))))
    except Exception:
        ratio = 2
    _GLYPH_DPR = ratio
    return ratio


def reset_glyph_dpr():
    """Test hook: forget the cached ratio so a different screen setup can be simulated."""
    global _GLYPH_DPR
    _GLYPH_DPR = None
