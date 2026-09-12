"""Fixture plugin missing collect() -- must be logged and skipped."""

METRICS = {
    "no_collect.thing.count": {
        "kind": "gauge",
        "label": "Thing",
        "unit": "",
    },
}
