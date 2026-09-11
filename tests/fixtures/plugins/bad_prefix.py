"""Fixture plugin whose metric key isn't prefixed with the plugin's own
name -- must be logged and skipped.
"""

METRICS = {
    "someone_else.thing.count": {
        "kind": "gauge",
        "label": "Thing",
        "unit": "",
    },
}


def collect(config: dict, http) -> dict:
    raise NotImplementedError
