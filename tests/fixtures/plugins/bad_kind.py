"""Fixture plugin with an invalid metric kind -- must be logged and skipped."""

METRICS = {
    "bad_kind.thing.count": {
        "kind": "counter",  # not "gauge" or "cumulative"
        "label": "Thing",
        "unit": "",
    },
}


def collect(config: dict, http) -> dict:
    raise NotImplementedError
