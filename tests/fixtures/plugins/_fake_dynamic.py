"""Fixture plugin exercising pattern METRICS: per-key labels/attrs,
lifecycle deactivation, and the cardinality guard. Not a real data source --
``collect`` reads its whole response straight out of ``config["items"]``.
"""

POLL_INTERVAL_SECONDS = 300

METRICS = {
    "fake_dynamic.profile.count": {
        "kind": "gauge",
        "label": "Profile Count",
        "unit": "things",
    },
    "fake_dynamic.item.{id}.value": {
        "kind": "cumulative",
        "label": "Item Value",
        "unit": "things",
    },
}


def collect(config: dict, http) -> dict:
    """config["items"]: {item_id: value-or-metadata-dict}. config["profile"]:
    the plain profile.count value, if present.
    """
    result: dict = {}
    if "profile" in config:
        result["fake_dynamic.profile.count"] = config["profile"]
    for item_id, value in (config.get("items") or {}).items():
        result[f"fake_dynamic.item.{item_id}.value"] = value
    return result
