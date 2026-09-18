"""REST API: stats, catalogue, and plugin status endpoints.

All routes are read-only queries; new SQL always goes in storage.py, never
here. Mounted on the app in main.py. Anything a route needs beyond a plain
query -- plugin poll intervals, plugin names, the scheduler -- is prepared
once at startup and read off ``app.state`` (see main.lifespan), rather than
recomputed per request or imported as a module-level global.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from numbers_go_up import storage
from numbers_go_up.http import BLOCKED_ERROR_PREFIX

router = APIRouter(prefix="/api")
# Mounted at the root next to /health, not under /api: it's for uptime
# monitors, not dashboard clients.
health_router = APIRouter()


# --- Response models (#92) ----------------------------------------------
#
# These describe exactly what the handlers below already return -- adding
# them gives /docs a populated schema and example instead of a bare
# `dict[str, Any]`, without changing any payload. A handler still returns a
# plain dict; FastAPI validates/serializes it against the model named in
# response_model.


class MetricLatest(BaseModel):
    value: float
    label: str | None
    kind: Literal["gauge", "cumulative"]
    unit: str | None
    icon: str | None
    updated: str | None
    stale: bool
    delta_1h: float | None
    delta_24h: float | None


class StatsLatestResponse(BaseModel):
    timestamp: str | None
    metrics: dict[str, MetricLatest]


class HistoryPoint(BaseModel):
    ts: int
    value: float


class ChangeBar(BaseModel):
    ts: int
    change: float


class StatsHistoryResponse(BaseModel):
    metric: str
    points: list[HistoryPoint]
    bars: list[ChangeBar]


class StatsDeltaResponse(BaseModel):
    metric: str
    window_hours: int
    delta: float | None
    rate_per_hour: float | None
    current: float | None
    previous: float | None


class MetricCatalogueEntry(BaseModel):
    key: str
    plugin: str
    kind: Literal["gauge", "cumulative"]
    label: str | None
    unit: str | None
    icon: str | None
    last_value: float | None
    last_seen: str | None
    active: bool
    attrs: dict[str, Any]


class ListMetricsResponse(BaseModel):
    metrics: list[MetricCatalogueEntry]


PluginStatusName = Literal["disabled", "pending", "ok", "error", "blocked", "polling"]


class PluginStatus(BaseModel):
    name: str
    status: PluginStatusName
    enabled: bool
    last_poll: str | None
    next_poll: str | None
    consecutive_failures: int
    last_error: str | None
    metrics: list[str]


class ListPluginsResponse(BaseModel):
    plugins: list[PluginStatus]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class UnhealthyPlugin(BaseModel):
    name: str
    reason: str
    consecutive_failures: int
    last_poll: str | None
    last_error: str | None


class PluginsHealthResponse(BaseModel):
    status: Literal["ok", "unhealthy"]
    failure_threshold: int
    unhealthy: list[UnhealthyPlugin]


# Matches the dashboard footer's "red" (Failure Handling #2).
DEFAULT_UNHEALTHY_FAILURES = 3

# A full year of one series was measured at 4.9ms, so this cap is about
# keeping responses reasonable, not performance.
MAX_HOURS = 8760

# Range bounds in hours, shared by /api/stats/overview, /api/stats/history,
# and /m/{key} -- the same six named ranges everywhere in the app. ALL has
# no fixed bound: each series starts at its own first sample.
RANGE_HOURS = {
    "1D": 24,
    "1W": 24 * 7,
    "1M": 24 * 30,
    "3M": 24 * 90,
    "1Y": 24 * 365,
}
VALID_RANGES = (*RANGE_HOURS, "ALL")

# Change-bar bucket width in seconds, keyed by named range: 1D->1h, 1W->6h,
# 1M/3M->1d, 1Y/ALL->1w, per the big chart's change-bar spec.
BAR_BUCKET_SECONDS = {
    "1D": 3600,
    "1W": 6 * 3600,
    "1M": 86400,
    "3M": 86400,
    "1Y": 7 * 86400,
    "ALL": 7 * 86400,
}


def _bar_bucket_seconds(range_key: str | None, hours: int | None) -> int:
    """The change-bar bucket width for this request: exact per
    :data:`BAR_BUCKET_SECONDS` when a named ``range`` was given, else the
    same table applied to the closest range by span for a legacy ``hours``
    call.
    """
    if range_key is not None:
        return BAR_BUCKET_SECONDS[range_key]
    if hours <= 24:
        return BAR_BUCKET_SECONDS["1D"]
    if hours <= 168:
        return BAR_BUCKET_SECONDS["1W"]
    if hours <= 2160:
        return BAR_BUCKET_SECONDS["1M"]
    return BAR_BUCKET_SECONDS["1Y"]


def _bucket_changes(
    points: list[tuple[int, float]], bucket_seconds: int
) -> list[dict[str, Any]]:
    """Net change per bucket: the last value in each bucket minus the last
    value in the previous non-empty bucket (or, for the first bucket, minus
    ``points[0]`` -- the carried-forward open). A bucket with no points is
    simply absent, per the change-bar spec ("empty buckets drawn as
    nothing"), not filled with a zero.
    """
    if len(points) < 2:
        return []

    open_value = points[0][1]
    origin = points[0][0]

    last_per_bucket: dict[int, float] = {}
    for ts, value in points:
        index = (ts - origin) // bucket_seconds
        last_per_bucket[index] = value

    bars = []
    previous_value = open_value
    for index in sorted(last_per_bucket):
        value = last_per_bucket[index]
        bars.append(
            {
                "ts": origin + index * bucket_seconds,
                "change": value - previous_value,
            }
        )
        previous_value = value
    return bars


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_stale(
    db_path: str,
    plugin_name: str,
    last_seen: int | None,
    interval_seconds: int,
    now: int,
) -> bool:
    """Both halves of the stale rule: an old newest sample *and* a plugin
    whose newest *finished* run failed. Age alone would mark every flat,
    store-on-change series stale. consecutive_failures() is deliberately
    not used here: start_run() inserts every run as status='error' (the
    _RUN_IN_PROGRESS sentinel) and only finish_run() overwrites it, so
    while a poll is in flight it counts a healthy plugin as failing.
    """
    if last_seen is None or now - last_seen <= 3 * interval_seconds:
        return False
    finished = storage.latest_finished_run(db_path, plugin_name)
    return finished is not None and finished["status"] != "ok"


@router.get(
    "/stats/latest",
    tags=["stats"],
    summary="Latest value and staleness for every active metric",
    response_model=StatsLatestResponse,
)
def stats_latest(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    db_path = config["storage"]["path"]
    default_interval = config["poll"]["default_interval"]
    intervals = getattr(request.app.state, "plugin_intervals", {})
    now = int(time.time())

    metrics: dict[str, Any] = {}
    for row in storage.list_series(db_path):
        if row["last_value"] is None:
            continue

        interval = intervals.get(row["plugin_name"], default_interval)
        stale = _is_stale(db_path, row["plugin_name"], row["last_seen"], interval, now)

        value_1h = storage.value_as_of(db_path, row["id"], now - 3600)
        value_24h = storage.value_as_of(db_path, row["id"], now - 86400)

        metrics[row["metric_key"]] = {
            "value": row["last_value"],
            "label": row["label"],
            "kind": row["kind"],
            "unit": row["unit"],
            "icon": row["icon"],
            "updated": _iso(row["last_seen"]),
            "stale": stale,
            "delta_1h": None if value_1h is None else row["last_value"] - value_1h,
            "delta_24h": None if value_24h is None else row["last_value"] - value_24h,
        }

    return {"timestamp": _iso(now), "metrics": metrics}


@router.get(
    "/stats/history",
    tags=["stats"],
    summary="Time series and bucketed change bars for one metric",
    response_model=StatsHistoryResponse,
)
def stats_history(
    request: Request,
    metric: str,
    hours: int | None = Query(None, gt=0, le=MAX_HOURS),
    range: str | None = Query(None),
) -> dict[str, Any]:
    if (hours is None) == (range is None):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of `hours` or `range` is required",
        )
    if range is not None and range not in VALID_RANGES:
        raise HTTPException(
            status_code=422,
            detail=f"range must be one of {VALID_RANGES}, got {range!r}",
        )

    db_path = request.app.state.config["storage"]["path"]
    series = storage.get_series_by_key(db_path, metric)
    if series is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric!r}")

    now = int(time.time())
    if hours is not None:
        start = now - hours * 3600
    elif range == "ALL":
        # Everything from the series' first sample: history() already
        # returns nothing before whatever samples exist, so a start of 0
        # (long before any real timestamp) is exactly "from the beginning".
        start = 0
    else:
        start = now - RANGE_HOURS[range] * 3600

    points = storage.history(db_path, series["id"], start, now)
    bucket_seconds = _bar_bucket_seconds(range, hours)

    return {
        "metric": metric,
        "points": [{"ts": ts, "value": value} for ts, value in points],
        "bars": _bucket_changes(points, bucket_seconds),
    }


@router.get(
    "/stats/delta",
    tags=["stats"],
    summary="Change and rate over a trailing window for one metric",
    response_model=StatsDeltaResponse,
)
def stats_delta(
    request: Request, metric: str, hours: int = Query(..., gt=0, le=MAX_HOURS)
) -> dict[str, Any]:
    db_path = request.app.state.config["storage"]["path"]
    series = storage.get_series_by_key(db_path, metric)
    if series is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric!r}")

    now = int(time.time())
    start = now - hours * 3600
    current = storage.value_as_of(db_path, series["id"], now)
    previous = storage.value_as_of(db_path, series["id"], start)

    delta = None if current is None or previous is None else current - previous
    rate_per_hour = None if delta is None else round(delta / hours, 2)

    return {
        "metric": metric,
        "window_hours": hours,
        "delta": delta,
        "rate_per_hour": rate_per_hour,
        "current": current,
        "previous": previous,
    }


def _parse_attrs(attrs_json: str | None) -> dict[str, Any]:
    """Best-effort parse of ``metric_series.attrs``.

    The column is ``NOT NULL DEFAULT '{}'`` and every writer
    (``get_or_create_series``) validates before storing, so this should
    never actually be invalid JSON -- but this is the one endpoint that
    reports on every series that ever existed, including ones no plugin
    will ever rewrite, so a corrupt row (a hand-edited DB, a bug in some
    future writer) degrades to an empty dict rather than 500ing the whole
    catalogue.
    """
    if not attrs_json:
        return {}
    try:
        return json.loads(attrs_json)
    except json.JSONDecodeError:
        return {}


@router.get(
    "/metrics",
    tags=["metrics"],
    summary="Catalogue of every metric series that has ever existed",
    response_model=ListMetricsResponse,
)
def list_metrics(request: Request) -> dict[str, Any]:
    db_path = request.app.state.config["storage"]["path"]

    metrics = [
        {
            "key": row["metric_key"],
            "plugin": row["plugin_name"],
            "kind": row["kind"],
            "label": row["label"],
            "unit": row["unit"],
            "icon": row["icon"],
            "last_value": row["last_value"],
            "last_seen": _iso(row["last_seen"]),
            "active": bool(row["active"]),
            "attrs": _parse_attrs(row["attrs"]),
        }
        for row in storage.list_all_series(db_path)
    ]
    return {"metrics": metrics}


def _plugin_statuses(request: Request) -> list[dict[str, Any]]:
    """One status report per discovered plugin, shared by /api/plugins and
    /health/plugins so the two can never disagree."""
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
                    next_poll = _iso(int(job.next_run_time.timestamp()))

            # The newest *finished* run, not latest_run(): start_run()
            # inserts every run as status='error' (the _RUN_IN_PROGRESS
            # sentinel) and only finish_run() overwrites it, so reading the
            # newest row outright reports a healthy in-flight poll as an
            # error -- the trap _is_stale() already avoids via
            # latest_finished_run().
            run = storage.latest_finished_run(db_path, name)
            if run is None:
                status = "pending"
            else:
                last_poll = _iso(run["started_at"])
                if run["status"] == "ok":
                    status = "ok"
                else:
                    last_error = run["error"]
                    # A 403 is recorded as status='error' (the CHECK
                    # constraint allows only ok/error) with the Blocked
                    # prefix. Surface it as its own state: a source refusing
                    # this client needs a different fix than a broken plugin.
                    blocked = (last_error or "").startswith(BLOCKED_ERROR_PREFIX)
                    status = "blocked" if blocked else "error"

            # A poll currently in flight is liveness, not an error: report
            # it as its own state, keeping last_poll/last_error from the
            # newest finished run so the endpoint doesn't flip
            # ok -> error -> ok as runs start and finish.
            newest = storage.latest_run(db_path, name)
            if newest is not None and newest["finished_at"] is None:
                status = "polling"

        plugins.append(
            {
                "name": name,
                "status": status,
                "enabled": enabled,
                "last_poll": last_poll,
                "next_poll": next_poll,
                "consecutive_failures": storage.consecutive_failures(db_path, name),
                "last_error": last_error,
                "metrics": storage.metric_keys_for_plugin(db_path, name),
            }
        )

    return plugins


@router.get(
    "/plugins",
    tags=["plugins"],
    summary="Poll status for every discovered plugin",
    response_model=ListPluginsResponse,
)
def list_plugins(request: Request) -> dict[str, Any]:
    return {"plugins": _plugin_statuses(request)}


def _unhealthy_reason(plugin: dict[str, Any], failure_threshold: int) -> str | None:
    """Why an enabled plugin counts as unhealthy, or None if it doesn't.

    Blocked is unhealthy on the first 403: that isn't a blip, it's a source
    refusing this client, and the scheduler is already backing off. Other
    failures only count once ``failure_threshold`` *finished* runs in a row
    have failed -- consecutive_failures() also counts an in-flight run (its
    liveness convention, #19), which would flag a healthy plugin mid-poll.
    """
    last_error = plugin["last_error"]
    if last_error is not None and last_error.startswith(BLOCKED_ERROR_PREFIX):
        return "blocked"

    finished_failures = plugin["consecutive_failures"]
    if plugin["status"] == "polling":
        finished_failures = max(finished_failures - 1, 0)
    if finished_failures >= failure_threshold:
        return f"{finished_failures} consecutive failures"
    return None


@health_router.get(
    "/health/plugins",
    tags=["health"],
    summary="Plugin health for uptime monitors",
    response_model=PluginsHealthResponse,
)
def plugins_health(
    request: Request,
    response: Response,
    failures: int = Query(DEFAULT_UNHEALTHY_FAILURES, ge=1),
) -> dict[str, Any]:
    """Plugin health for uptime monitors: 200 when every enabled plugin is
    polling successfully, 503 when any is blocked or has failed ``failures``
    finished polls in a row. Disabled plugins are ignored, and so is a plugin
    that hasn't finished its first poll yet.

    Separate from /health on purpose: that one is liveness, and a source
    being down is no reason for Docker to restart the container.
    """
    unhealthy = []
    for plugin in _plugin_statuses(request):
        if not plugin["enabled"]:
            continue
        reason = _unhealthy_reason(plugin, failures)
        if reason is not None:
            unhealthy.append(
                {
                    "name": plugin["name"],
                    "reason": reason,
                    "consecutive_failures": plugin["consecutive_failures"],
                    "last_poll": plugin["last_poll"],
                    "last_error": plugin["last_error"],
                }
            )

    if unhealthy:
        response.status_code = 503
    return {
        "status": "unhealthy" if unhealthy else "ok",
        "failure_threshold": failures,
        "unhealthy": unhealthy,
    }
