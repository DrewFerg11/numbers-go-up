#!/usr/bin/env python3
"""Fail if requirements-dev.txt has drifted from requirements.txt.

requirements-dev.in includes requirements.txt, so uv writes the runtime
pins (and hashes) into requirements-dev.txt as well, which makes the dev lock
self-contained. The flip side: Dependabot bumping a pin in requirements.txt
alone leaves the dev lock -- and so the test job -- on the old version. This
check turns that into a red CI run with the fix in the message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# "name==1.2.3" at column 0, optionally followed by a marker, ' \' or both.
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*==[^\s;\\]+)", re.MULTILINE)
_HASH = re.compile(r"--hash=(sha256:[0-9a-f]+)")


def _pins(path: Path) -> dict[str, set[str]]:
    """Map each ``name==version`` pin to the set of hashes listed under it."""
    out: dict[str, set[str]] = {}
    text = path.read_text()
    matches = list(_PIN.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = set(_HASH.findall(text[m.end() : end]))
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    runtime = _pins(root / "requirements.txt")
    dev = _pins(root / "requirements-dev.txt")
    stale = sorted(p for p, hashes in runtime.items() if dev.get(p) != hashes)
    if stale:
        print("requirements-dev.txt is out of sync with requirements.txt for:")
        for pin in stale:
            print(f"  {pin}")
        print(
            "Regenerate it (command in CONTRIBUTING.md, 'Updating the locks'): "
            "uv pip compile requirements-dev.in --universal --generate-hashes "
            "--python-version 3.12 -o requirements-dev.txt"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
