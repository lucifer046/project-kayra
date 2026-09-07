"""
`python -m kayra` entry point.

Thin by design: it exists so the package is runnable without a launcher script, and it holds
no application logic of its own — `kayra.app.main()` owns the lifecycle.
"""

import sys

from kayra.app import main

if __name__ == "__main__":
    sys.exit(main())
