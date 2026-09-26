#!/usr/bin/env python3
"""Export the app's OpenAPI schema to a JSON file for the Pages site.

Run from a bare ``pip install -e .`` checkout -- no config file, no
database, no server. ``numbers_go_up.main`` only calls ``load_config()``
inside the lifespan, not at import time, so importing it here to reach
``app.openapi()`` is side-effect-free by construction. Keep it that way:
this script must never need a running instance.

The output is written into ``docs/assets/`` (gitignored -- see
``.gitignore``) rather than generated outside ``docs/`` and copied into
``site/`` after the build, so it is also present for a local
``mkdocs serve`` and the Redoc page's relative fetch works the same way in
dev and in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "assets" / "openapi.json"


def export(output: Path) -> None:
    from numbers_go_up.main import app

    schema = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2) + "\n")


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    export(output)
    print(f"Wrote OpenAPI schema to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
