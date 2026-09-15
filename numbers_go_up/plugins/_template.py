"""Copyable template for a numbers-go-up plugin.

Filenames starting with an underscore are never discovered as schedulable
plugins (that's how this file itself stays inert) — copy this to a new
name, without the underscore, to start a real plugin.

The plugin name is this module's filename stem: ``makerworld.py`` becomes
plugin name ``makerworld``. Every key in ``METRICS`` must start with
``"<plugin name>."`` — metric keys are Home Assistant entity IDs, lowercase
and dotted, and stable forever once published.
"""

# Default poll interval for this plugin, in seconds. config can override it
# per-install, but never below the 5-minute floor.
POLL_INTERVAL_SECONDS = 1800  # 30 min

METRICS = {
    "_template.subject.metric": {
        "kind": "cumulative",  # "cumulative" (only rises) or "gauge" (can fall)
        "label": "Human Label",  # required
        "unit": "downloads",  # required, may be ""
        "icon": "mdi:download",  # optional
    },
    # A pattern entry: subjects (here, "{id}") are only known at poll time,
    # e.g. a list of repos or models read from config or from the source
    # itself. Exactly one {placeholder}, occupying a whole dot-separated
    # segment. kind/unit/icon are fixed here and can never be overridden by
    # collect()'s return -- only label/attrs can be, per subject, per poll.
    "_template.thing.{id}.count": {
        "kind": "gauge",
        "label": "Thing Count",
        "unit": "things",
    },
}


def collect(config: dict, http) -> dict[str, int | float | dict]:
    """Fetch and return {metric_key: value} for every key in METRICS.

    config: this plugin's own section of config["plugins"] (its
    ``enabled``/``poll_interval`` keys plus whatever else the plugin needs,
    e.g. a user ID).
    http: the shared HTTP client from numbers_go_up.http. Plugins never
    construct their own client.

    A value may be a plain number, or a dict {"value": ..., "label": ...,
    "attrs": {...}} to set a per-subject label/attrs for a pattern key
    (e.g. {"_template.thing.42.count": {"value": 7, "label": "Widget 42",
    "attrs": {"id": 42}}}). A pattern key not returned on a fully
    successful poll is deactivated (its history is kept); returning it
    again reactivates it.
    """
    raise NotImplementedError


def health(config: dict) -> bool:  # optional; not called by the scheduler yet
    """Optional: cheap reachability/config check, no plugin-specific value."""
    raise NotImplementedError
