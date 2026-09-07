"""
Backward-compatibility shim.

`python main.py` has been the way to start Kayra for the whole life of this project, and it
still works. The application itself now lives in the `kayra` package under `src/`, which is
what makes it importable, testable and installable.

Prefer `python run.py`: it locates the project virtual environment and re-executes there, so
the user never has to activate it by hand.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from kayra.app import main

if __name__ == "__main__":
    sys.exit(main())
