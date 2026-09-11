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
}


def collect(config: dict, http) -> dict[str, int | float]:
    """Fetch and return {metric_key: value} for every key in METRICS.

    config: this plugin's own section of config["plugins"] (its
    ``enabled``/``poll_interval`` keys plus whatever else the plugin needs,
    e.g. a user ID).
    http: the shared HTTP client from numbers_go_up.http. Plugins never
    construct their own client.
    """
    raise NotImplementedError


def health(config: dict) -> bool:  # optional; not called by the scheduler yet
    """Optional: cheap reachability/config check, no plugin-specific value."""
    raise NotImplementedError
