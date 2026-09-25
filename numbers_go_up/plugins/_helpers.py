"""Shared validation helpers for the built-in plugins.

The leading underscore means this is never discovered as a plugin itself
(see ``_discover_dir`` in ``plugins/__init__.py``, which skips any
underscore-prefixed file). Private to the built-ins for 1.0; a stable,
documented version of this for user plugins can come later.
"""

from __future__ import annotations

import re

# A keyed-entry's "key" slug: lowercase letters, digits, underscore, hyphen.
# Becomes part of a metric key and a Home-Assistant-facing object_id, so it
# gets the same charset discipline as everything else that ends up there.
KEY_SLUG = re.compile(r"^[a-z0-9_-]+$")


def validate_max(value: object, field: str = "max") -> int:
    """A cardinality-guard value: a plain int (bools are int subclasses but
    not counts), at least 1. ``field`` names the config key in the error
    message, e.g. ``"max"``, ``"models.max"``, ``"videos_max"``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"{field} must be at least 1, got {value!r}")
    return value
