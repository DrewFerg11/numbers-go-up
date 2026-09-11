"""Fixture plugin: returns a counter that increments on every collect().

Each call to load_plugin_from_path creates a fresh module object, so this
counter is scoped to whichever test loaded it -- no shared state leaks
between tests.
"""

POLL_INTERVAL_SECONDS = 300

METRICS = {
    "fake_incrementing.demo.count": {
        "kind": "cumulative",
        "label": "Incrementing",
        "unit": "",
    },
}

_count = 0


def collect(config: dict, http) -> dict:
    global _count
    _count += 1
    return {"fake_incrementing.demo.count": _count}
