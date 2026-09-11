"""Fixture plugin: always returns the same value.

Used to prove store-on-change end to end: N simulated ticks should
produce exactly one sample (or two, once ticks span the heartbeat).
"""

POLL_INTERVAL_SECONDS = 300

METRICS = {
    "fake_constant.demo.value": {
        "kind": "gauge",
        "label": "Constant",
        "unit": "",
    },
}


def collect(config: dict, http) -> dict:
    return {"fake_constant.demo.value": 42}
