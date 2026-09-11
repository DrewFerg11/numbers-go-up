"""Fixture plugin: always raises, to exercise per-poll isolation."""

POLL_INTERVAL_SECONDS = 300

METRICS = {
    "fake_raises.demo.value": {
        "kind": "gauge",
        "label": "Raises",
        "unit": "",
    },
}


def collect(config: dict, http) -> dict:
    raise RuntimeError("simulated plugin failure")
