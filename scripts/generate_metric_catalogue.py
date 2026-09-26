#!/usr/bin/env python3
"""Generate a catalogue of every metric every shipped plugin declares,
read statically from each plugin's ``METRICS`` -- no config, no
database, no network.

Uses :func:`numbers_go_up.plugins.discover` (an empty config: zero
plugins enabled, discovery still runs and validates every built-in
plugin file) rather than re-implementing module loading, so a plugin
that fails the loader's own contract check is silently omitted here too
-- it never appears as if it shipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from numbers_go_up.plugins import discover, is_pattern_key  # noqa: E402

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent / "docs" / "plugins" / "metrics.md"
)

# A plugin with a prose page (#106) links to it; one without links to its
# source on GitHub instead (same fallback docs/plugins/index.md already
# uses for "more plugins ship than are documented here so far"). Checked
# against the docs directory itself, not a hand-maintained list, so a new
# prose page is picked up automatically instead of silently going stale.
_DOCS_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "docs" / "plugins"
_SOURCE_URL_TEMPLATE = (
    "https://github.com/DrewFerg11/numbers-go-up/blob/main/"
    "numbers_go_up/plugins/{name}.py"
)


def _plugin_link(name: str) -> str:
    if (_DOCS_PLUGINS_DIR / f"{name}.md").exists():
        return f"[{name}]({name}.md)"
    return f"[{name}]({_SOURCE_URL_TEMPLATE.format(name=name)})"


def generate() -> str:
    plugins = sorted(discover({}), key=lambda p: p.name)

    lines = [
        "# Metric catalogue",
        "",
        "Every metric every shipped plugin can emit, generated from each",
        "plugin's own `METRICS` declaration -- the single source of truth",
        "for a metric's `kind`, `unit` and label. A running instance's",
        "[`/api/metrics`](../api.md) only lists series that already exist in",
        "*that* database; this page is the full declared catalogue, plugin",
        "or not yet enabled included.",
        "",
        "## Kinds",
        "",
        "Every metric is one of two kinds, decided once in `METRICS` and",
        "never at collect time -- a plugin's `collect()` return has no kind",
        "or unit of its own to set:",
        "",
        "- **`cumulative`**: a running total that only goes up. Home",
        "  Assistant gets `state_class: total_increasing`.",
        "- **`gauge`**: a level that can go down as well as up. Home",
        "  Assistant gets `state_class: measurement`.",
        "",
        "## Pattern keys",
        "",
        "A key containing a `{placeholder}` -- `makerworld.model.{id}.downloads`,",
        "never more than one placeholder per key -- is a template, not a",
        "literal key: each configured subject (a model ID, a repo, a",
        "channel) resolves it to its own concrete series",
        "(`makerworld.model.12345.downloads`). A key with no `{}` is exact",
        "and always means the same one series.",
        "",
    ]

    for plugin in plugins:
        lines.append(f"## {_plugin_link(plugin.name)}")
        lines.append("")
        lines.append("| Key | Pattern? | Kind | Unit | Label |")
        lines.append("| --- | --- | --- | --- | --- |")
        for key in sorted(plugin.metrics):
            meta = plugin.metrics[key]
            pattern = "Yes" if is_pattern_key(key) else "No"
            lines.append(
                f"| `{key}` | {pattern} | {meta['kind']} | {meta.get('unit') or ''} "
                f"| {meta.get('label') or ''} |"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate())
    print(f"Wrote metric catalogue to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
