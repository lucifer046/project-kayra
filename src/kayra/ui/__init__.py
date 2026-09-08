"""
The Kayra desktop UI.

Importing this package must stay free of side effects: no QApplication, no widgets, no backend
boot. `kayra.ui.application.main()` owns all of that, and only when called.
"""

__all__ = ["main"]


def main(argv=None):
    """Entry point. Imported lazily so `import kayra.ui` never pulls in Qt."""
    from kayra.ui.application import main as _main
    return _main(argv)
