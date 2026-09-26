#!/usr/bin/env python3
"""Generate a Markdown table of every ``Config`` key from its Pydantic
schema, so the reference can never drift from ``config.py``.

Walks ``Config.model_json_schema()`` -- ``$ref``s resolved against
``$defs`` -- rather than parsing a defaults dict, so a key's type,
default, required-ness and (once added to the model) description come
from the same schema FastAPI-style validation actually enforces at
startup. Run from a bare checkout: importing ``numbers_go_up.config``
does no I/O at import time.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from numbers_go_up.config import Config  # noqa: E402

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "getting-started"
    / "config-reference.md"
)

# Top-level keys documented by hand instead, in the same generated page --
# they're intentionally loose dicts validated by their own module, not
# part of this strict schema. See Config's own docstring in config.py.
_FREEFORM_TOP_LEVEL = {
    "plugins": (
        "See each [plugin's own page](../plugins/index.md) for its config "
        "keys, or the [metric catalogue](../plugins/metrics.md) for what "
        "each one can emit."
    ),
    "mqtt": "See the [Home Assistant](../home-assistant/index.md) page.",
    "milestones": "Validated by `milestones.py`; not covered by this reference.",
}


def _type_str(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in schema:
        return _type_str(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        parts = [_type_str(s, defs) for s in schema["anyOf"]]
        parts = [p for p in dict.fromkeys(parts) if p != "null"]
        suffix = " (optional)" if len(parts) < len(schema["anyOf"]) else ""
        return " or ".join(parts) + suffix
    if schema.get("type") == "array":
        return f"list of {_type_str(schema.get('items', {}), defs)}"
    return {
        "integer": "integer",
        "number": "number",
        "string": "string",
        "boolean": "boolean",
        "object": "object",
        "null": "null",
    }.get(schema.get("type"), schema.get("type", "any"))


def _default_at(instance: Any, path: str) -> Any:
    """Walk a dotted path off a real, fully-defaulted model instance.

    ``model_json_schema()`` only serializes a literal ``default=``; a
    ``default_factory`` field (``dashboard.pinned``, ``plugins``) has no
    ``"default"`` key in the schema at all, which is not the same thing as
    being required. Reading the actual instance instead gets the real
    value either way and can't drift from what the field actually
    defaults to at runtime.
    """
    value = instance
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _rows(
    schema: dict[str, Any], defs: dict[str, Any], prefix: str, instance: Any
) -> list[dict[str, str]]:
    rows = []
    required = set(schema.get("required", ()))
    for name, field_schema in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        if "$ref" in field_schema:
            nested = defs[field_schema["$ref"].rsplit("/", 1)[-1]]
            rows.extend(_rows(nested, defs, path, instance))
            continue
        is_required = name in required
        rows.append(
            {
                "path": path,
                "type": _type_str(field_schema, defs),
                "default": None if is_required else repr(_default_at(instance, path)),
                "required": is_required,
                "description": field_schema.get("description", ""),
            }
        )
    return rows


def generate() -> str:
    schema = Config.model_json_schema()
    defs = schema.get("$defs", {})
    # A representative instance, defaulted everywhere the schema allows --
    # only storage.path has no default, so that's the one value supplied.
    from numbers_go_up.config import StorageConfig

    instance = Config(storage=StorageConfig(path="/data/stats.db"))
    rows = _rows(schema, defs, prefix="", instance=instance)

    lines = [
        "# Config reference",
        "",
        "Generated from the app's own config schema on every deploy --",
        "every key below is exactly what a running instance will accept.",
        "For a narrative walkthrough, start at",
        "[Configuration](configuration.md); this page is the complete table",
        "it links into, not a replacement for it.",
        "",
        "An unknown key anywhere in this schema fails startup (a typo'd",
        "section or field name), and a value of the wrong type -- a quoted",
        '`"86400"` where an integer is required -- fails startup too rather',
        "than being coerced. Set `NGU_CONFIG_STRICT=0` to downgrade that to",
        "a warning; see [Configuration](configuration.md) for when that's",
        "actually the right call.",
        "",
        "| Key | Type | Default | Description |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        default = "**required**" if row["required"] else row["default"]
        lines.append(
            f"| `{row['path']}` | {row['type']} | {default} | {row['description']} |"
        )

    lines += ["", "## Free-form sections", ""]
    lines.append(
        "Not part of the strict schema above -- each validates itself, "
        "against its own rules, not this one:"
    )
    lines.append("")
    for key, note in _FREEFORM_TOP_LEVEL.items():
        lines.append(f"- **`{key}`** -- {note}")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate())
    print(f"Wrote config reference to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
