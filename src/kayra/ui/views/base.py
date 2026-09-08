# ┌────────────────────────────────────────────────────────────────────────┐
# │                              base.py                                   │
# │                    Shared Scaffolding for Views                        │
# └────────────────────────────────────────────────────────────────────────┘
"""
The contract every screen implements.

`View.on_show()` / `on_hide()` are the hooks that make the performance rules enforceable: a
screen that polls anything starts its timer in `on_show` and stops it in `on_hide`, so exactly
one screen is ever doing work. Without that, seven views would each keep a timer running
forever and the idle cost of the application would be seven times what the user can see.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QSizePolicy

from kayra.ui.theme import Space
from kayra.ui.components.primitives import PageTitle, Subtitle


class View(QWidget):
    """Base screen. Subclasses build into `self.content`."""

    title = ""
    subtitle = ""

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QScrollArea.NoFrame)

        holder = QWidget()
        holder.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.content = QVBoxLayout(holder)
        self.content.setContentsMargins(Space.xl, Space.lg, Space.xl, Space.xl)
        self.content.setSpacing(Space.base)

        if self.title:
            self.content.addWidget(PageTitle(self.title))
            if self.subtitle:
                self.content.addWidget(Subtitle(self.subtitle))
            self.content.addSpacing(Space.sm)

        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll)

    # Lifecycle hooks. Default to nothing so a static screen costs nothing when shown.
    def on_show(self):
        pass

    def on_hide(self):
        pass
