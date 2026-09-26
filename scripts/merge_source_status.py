#!/usr/bin/env python3
"""Merge per-plugin canary results into one ``status/sources.json``.

``sources-canary.yml``'s ``probe`` job is a matrix -- one job per plugin,
each with ``continue-on-error: true`` so one 403 can't fail the others.
Each matrix job uploads its own ``status-<plugin>.json`` as a build
artifact (see the workflow's "Write status artifact" step); a separate
``publish-status`` job (``needs: probe``, ``if: always()``) downloads all
of them into one directory and runs this script to combine them into the
single file the status page fetches.

Doing the merge here, in a separate job, rather than one matrix job
writing directly to ``gh-pages``, avoids five jobs racing to commit the
same file.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Sources known to run from GitHub Actions' shared Azure IP ranges, which
# MakerWorld and TikTok sometimes block while working fine from a
# residential connection (#30). The status page must show this caveat
# next to the result, not bury it -- a bare "broken" here would be a false
# positive from the visitor's point of view.
AZURE_IP_CAVEAT_PLUGINS = frozenset({"makerworld", "tiktok"})

VALID_STATUSES = frozenset({"ok", "broken", "skipped"})


def merge(input_dir: Path) -> dict:
    sources = []
    for path in sorted(input_dir.glob("status-*.json")):
        data = json.loads(path.read_text())
        if data.get("status") not in VALID_STATUSES:
            raise ValueError(f"{path}: invalid status {data.get('status')!r}")
        sources.append(
            {
                "plugin": data["plugin"],
                "status": data["status"],
                "checked_at": data["checked_at"],
                "exit_code": data.get("exit_code"),
                "azure_ip_caveat": data["plugin"] in AZURE_IP_CAVEAT_PLUGINS,
            }
        )
    sources.sort(key=lambda s: s["plugin"])
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sources,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input-dir> <output-file>", file=sys.stderr)
        return 2
    input_dir = Path(argv[1])
    output_file = Path(argv[2])

    result = merge(input_dir)
    if not result["sources"]:
        print(
            "No status-*.json files found; refusing to write an empty result",
            file=sys.stderr,
        )
        return 1

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote {output_file} with {len(result['sources'])} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
