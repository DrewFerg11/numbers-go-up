"""Shared, tested helpers for the read paths: api.py, dashboard.py, and
milestones.py all import from here rather than from each other's private
names (#128).

Nothing here touches SQL directly -- that stays in storage.py. This module
is the one place for logic that's about *shaping* what storage.py returns
into what a route (JSON) or a template (HTML) needs, when more than one of
those callers needs the same shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Request

from numbers_go_up import storage
from numbers_go_up.http import BLOCKED_ERROR_PREFIX

# Matches the dashboard footer's "red" (Failure Handling #2). Used both by
# /health/plugins' own (overridable via its ?failures= query param)
# unhealthy check and, at this default, by plugin_health()'s health field.
DEFAULT_UNHEALTHY_FAILURES = 3

# Range bounds in hours, shared by /api/stats/overview, /api/stats/history,
# and /m/{key} -- the same named ranges everywhere in the app. ALL has no
# fixed bound: each series (or set of series) starts at its own first
# sample, resolved by whoever calls range_start().
#
# 1H/6H/12H are here temporarily for testing -- more frequent checking while
# the app is being shaken out. Revisit per issue #116 (button row vs.
# dropdown) once that settles down.
RANGE_HOURS: dict[str, int] = {
    "1H": 1,
    "6H": 6,
    "12H": 12,
    "1D": 24,
    "1W": 24 * 7,
    "1M": 24 * 30,
    "3M": 24 * 90,
    "1Y": 24 * 365,
}
VALID_RANGES = (*RANGE_HOURS, "ALL")
# Kept in sync with VALID_RANGES by construction (both are built from
# RANGE_HOURS plus "ALL"), so FastAPI's enum in /docs and the runtime
# VALID_RANGES tuple can never drift apart.
RangeKey = Literal["1H", "6H", "12H", "1D", "1W", "1M", "3M", "1Y", "ALL"]


def iso(ts: int | float | None) -> str | None:
    """UTC ISO-8601 with a trailing Z, or None for a None timestamp.

    The one timestamp formatter in the app -- api.py, dashboard.py and
    milestones.py each had their own copy of this exact line before #128.
    """
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_attrs(attrs_json: str | None) -> dict[str, Any]:
    """Best-effort parse of ``metric_series.attrs``.

    The column is ``NOT NULL DEFAULT '{}'`` and every writer
    (``storage.get_or_create_series``) validates before storing, so this
    should never actually be invalid JSON -- but /api/metrics reports on
    every series that ever existed, including ones no plugin will ever
    rewrite again, so a corrupt row (a hand-edited DB, a bug in some future
    writer) degrades to an empty dict rather than 500ing the whole
    catalogue. That includes valid JSON that isn't an object ("5", "[]") --
    ``json.loads`` accepts those without complaint, and callers that expect
    a dict (MetricCatalogueEntry, #92) would 500 every row otherwise.
    """
    if not attrs_json:
        return {}
    try:
        parsed = json.loads(attrs_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_stale(
    plugin_name: str,
    interval_seconds: int,
    now: int,
    last_ok: dict[str, int],
) -> bool:
    """A series is stale when its plugin has had no successful poll within
    3x its interval -- exactly Home Assistant's own ``expire_after`` rule
    (mqtt.py's ``_expire_after``), per the README and #133's decision.

    Not "the plugin's newest run failed": for a flat, store-on-change
    series that only writes on the heartbeat, that older rule collapsed to
    just the newest-run check, so a single failed poll could mark it stale
    for up to a full heartbeat interval even though HA still saw it as
    fresh.

    ``last_ok`` is :func:`storage.last_ok_runs`'s ``{plugin_name:
    finished_at}`` result -- one grouped query shared across every series
    in a request (the dashboard overview, ``/api/stats/latest``) instead of
    one query per series.
    """
    finished_at = last_ok.get(plugin_name)
    return finished_at is None or now - finished_at > 3 * interval_seconds


def is_blocked(error: str | None) -> bool:
    """Whether a ``plugin_runs.error`` string is a blocked (403) failure.

    The one place that knows what "blocked" means (#127b): a 403 is
    recorded as an ordinary ``status='error'`` row (the schema's CHECK
    constraint allows only ``ok``/``error``) with this text prefix, so
    every reader that needs to tell a blocked source apart from a broken
    plugin -- ``plugin_statuses``, ``api._unhealthy_reason`` -- goes
    through here instead of re-checking the prefix itself. JS no longer
    needs its own copy: ``plugin.health`` already reflects this.
    """
    return error is not None and error.startswith(BLOCKED_ERROR_PREFIX)


PluginHealth = Literal["disabled", "pending", "ok", "warn", "error"]


def plugin_health(
    status: str,
    enabled: bool,
    consecutive_failures: int,
    last_error: str | None,
    failure_threshold: int = DEFAULT_UNHEALTHY_FAILURES,
) -> PluginHealth:
    """One rolled-up health verdict per plugin, computed once server-side
    (#127b) instead of dashboard.js's own copy of this exact rule
    (``statusClass``, which the run-state work's removal of
    ``consecutive_failures``' in-flight-run double-count left subtly wrong
    until this replaced it -- it still subtracted 1 for "polling").

    ``disabled``/``pending`` pass ``status`` straight through: neither has
    a failure history worth rolling up yet. Otherwise: blocked is ``error``
    unconditionally (a source refusing this client isn't a blip, and the
    scheduler is already backing off); ``failure_threshold`` or more
    consecutive finished failures is ``error``; at least one is ``warn``;
    zero is ``ok`` -- whether ``status`` is currently ``ok`` or ``polling``,
    since an in-flight retry with no finished failures behind it is a
    healthy plugin mid-poll, not a reason to downgrade.
    """
    if not enabled:
        return "disabled"
    if status == "pending":
        return "pending"
    if is_blocked(last_error):
        return "error"
    if consecutive_failures >= failure_threshold:
        return "error"
    if consecutive_failures >= 1:
        return "warn"
    return "ok"


def plugin_statuses(request: Request) -> list[dict[str, Any]]:
    """One status report per discovered plugin, shared by /api/plugins,
    /health/plugins, and the dashboard overview so none of them can
    disagree."""
    config = request.app.state.config
    db_path = config["storage"]["path"]
    # All read off app.state, not module-level globals, so a test can
    # build an app with no scheduler running and no plugin discovery at
    # all (main.lifespan prepares both once at startup).
    job_scheduler = getattr(request.app.state, "scheduler", None)
    plugin_names = getattr(request.app.state, "plugin_names", [])
    plugins_config = config.get("plugins") or {}

    plugins = []
    for name in plugin_names:
        plugin_config = plugins_config.get(name)
        enabled = isinstance(plugin_config, dict) and bool(plugin_config.get("enabled"))

        last_poll = None
        next_poll = None
        last_error = None

        if not enabled:
            status = "disabled"
        else:
            if job_scheduler is not None:
                job = job_scheduler.get_job(f"plugin:{name}")
                if job is not None and job.next_run_time is not None:
                    next_poll = iso(int(job.next_run_time.timestamp()))

            # The newest *finished* run, not latest_run(): start_run()
            # inserts every run as status='error' (the _RUN_IN_PROGRESS
            # sentinel) and only finish_run() overwrites it, so reading the
            # newest row outright reports a healthy in-flight poll as an
            # error -- the trap is_stale() already avoids via
            # storage.last_ok_runs().
            run = storage.latest_finished_run(db_path, name)
            if run is None:
                status = "pending"
            else:
                last_poll = iso(run["started_at"])
                if run["status"] == "ok":
                    status = "ok"
                else:
                    last_error = run["error"]
                    # A 403 is recorded as status='error' (the CHECK
                    # constraint allows only ok/error). Surface it as its
                    # own state: a source refusing this client needs a
                    # different fix than a broken plugin.
                    status = "blocked" if is_blocked(last_error) else "error"

            # A poll currently in flight is liveness, not an error: report
            # it as its own state, keeping last_poll/last_error from the
            # newest finished run so the endpoint doesn't flip
            # ok -> error -> ok as runs start and finish.
            newest = storage.latest_run(db_path, name)
            if newest is not None and newest["finished_at"] is None:
                status = "polling"

        consecutive_failures = storage.consecutive_failures(db_path, name)
        plugins.append(
            {
                "name": name,
                "status": status,
                "enabled": enabled,
                "health": plugin_health(
                    status, enabled, consecutive_failures, last_error
                ),
                "last_poll": last_poll,
                "next_poll": next_poll,
                "consecutive_failures": consecutive_failures,
                "last_error": last_error,
                "metrics": storage.metric_keys_for_plugin(db_path, name),
            }
        )

    return plugins


def range_start(range_key: str, now: int, earliest: int | None = None) -> int:
    """The ``start`` timestamp for ``range_key``.

    For every named range, ``now`` minus its span. For ``ALL``, ``earliest``
    -- the caller's own idea of where "everything" begins, since that
    differs by context: the earliest first-sample across a whole overview's
    worth of series, a single series' own ``first_seen`` on the detail page,
    or a fixed 0 ("from the beginning of time", cheaper than a query) for
    ``/api/stats/history``, which only ever returns samples that exist
    regardless of how far back ``start`` reaches. ``None`` (the default)
    falls back to ``now`` -- "all" of a series/set that has no data yet.
    """
    if range_key == "ALL":
        return earliest if earliest is not None else now
    return now - RANGE_HOURS[range_key] * 3600


def summarize(
    open_value: float | None,
    value: float,
    high: float | None,
    low: float | None,
    start: int,
    now: int,
) -> dict[str, Any]:
    """change/change_pct/high/low/avg_per_day from one series' stats.

    ``open_value``/``high``/``low`` are exactly what :func:`storage.range_stats`
    (or its bulk sibling) returns: None when the series has no points in the
    window, in which case this falls back to ``value`` for all three --
    an unchanged, flat window. ``value`` is the series' current last_value.

    Shared between the dashboard overview (per-row) and the detail page
    (its own single-series stats), which computed this identically but
    separately before #128 -- and a third time, differently-shaped, in
    detail.js.
    """
    if open_value is None:
        open_value = value
    high = value if high is None else max(high, value)
    low = value if low is None else min(low, value)

    change = value - open_value
    change_pct = None if open_value == 0 else round((change / open_value) * 100, 2)
    span_days = max((now - start) / 86400, 1)
    avg_per_day = round(change / span_days, 2)

    return {
        "change": change,
        "change_pct": change_pct,
        "high": high,
        "low": low,
        "avg_per_day": avg_per_day,
    }
