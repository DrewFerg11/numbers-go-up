"""A minimal, contract-valid fixture plugin used by tests/test_plugins.py."""

POLL_INTERVAL_SECONDS = 1800

METRICS = {
    "valid.thing.count": {
        "kind": "cumulative",
        "label": "Thing Count",
        "unit": "things",
        "icon": "mdi:counter",
    },
}


def collect(config: dict, http) -> dict:
    raise NotImplementedError


def health(config: dict) -> bool:
    return True
