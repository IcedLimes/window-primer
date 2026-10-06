"""window-primer entry point for every OS: `python bin/primer.py <command>`.

Scheduled jobs, the statusline and the `primer` launchers run this with an exact interpreter
path, so nothing depends on which `python3` happens to be first on PATH.
"""
import os
import sys

if sys.version_info < (3, 11):
    sys.exit(f"window-primer needs Python 3.11 or newer; this is {sys.version.split()[0]}")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from primer.cli import main  # noqa: E402

sys.exit(main())
