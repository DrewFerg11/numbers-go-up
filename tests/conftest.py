"""``scripts/`` isn't part of the installed package (pyproject.toml's
``packages`` list is deliberately just the runtime app), so a plain
``pytest`` invocation from the repo root wouldn't otherwise put it on
``sys.path`` for ``tests/test_demo_scripts.py`` to import it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
