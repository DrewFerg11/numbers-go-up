"""REST API: stats, catalogue, and plugin status endpoints.

All routes are read-only queries; new SQL always goes in storage.py, never
here. Mounted on the app in main.py. Anything a route needs beyond a plain
query -- plugin poll intervals, plugin names, the scheduler -- is prepared
once at startup and read off ``app.state`` (see main.lifespan), rather than
recomputed per request or imported as a module-level global.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ValidationError

from numbers_go_up import queries, scheduler, storage
from numbers_go_up.queries import RangeKey

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
    health: queries.PluginHealth
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


class MaintenanceLastRun(BaseModel):
    ts: int
    pruned_rows: int | None
    backup_file: str | None
    backup_bytes: int | None
    errors: list[str]


class MaintenanceStatus(BaseModel):
    last_run: MaintenanceLastRun | None
    next_run: str | None


class MqttStatus(BaseModel):
    enabled: bool
    connected: bool
    broker: str | None
    last_publish: str | None
    last_error: str | None


class MilestoneRule(BaseModel):
    metric: str
    every: float | None
    at: list[float]


class MilestonePending(BaseModel):
    metric: str
    threshold: float
    since: str | None


class MilestoneStatus(BaseModel):
    enabled: bool
    rules: list[MilestoneRule]
    pending: list[MilestonePending]
    last_sent: str | None
    last_error: str | None


class IntegrationsResponse(BaseModel):
    maintenance: MaintenanceStatus
    mqtt: MqttStatus
    milestones: MilestoneStatus


# A full year of one series was measured at 4.9ms, so this cap is about
# keeping responses reasonable, not performance.
MAX_HOURS = 8760

# Change-bar bucket width in seconds, keyed by named range: 1H/6H->5m,
# 12H->15m, 1D->1h, 1W->6h, 1M/3M->1d, 1Y/ALL->1w, per the big chart's
# change-bar spec.
BAR_BUCKET_SECONDS = {
    "1H": 5 * 60,
    "6H": 5 * 60,
    "12H": 15 * 60,
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
    same table applied to the closest range by span for an ``hours`` call.
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


@router.get(
    "/stats/latest",
    tags=["stats"],
    summary="Latest value and staleness for every active metric",
    response_model=StatsLatestResponse,
)
def stats_latest(request: Request) -> dict[str, Any]:
    """Batched (#128): one connection for the whole request rather than
    2-3 fresh connections (5 PRAGMAs each) per series -- with 500 active
    series this endpoint was measurably slower than /api/stats/overview,
    which does far more work but was already batched (#91).
    """
    config = request.app.state.config
    db_path = config["storage"]["path"]
    default_interval = config["poll"]["default_interval"]
    intervals = getattr(request.app.state, "plugin_intervals", {})
    now = int(time.time())
    last_ok = storage.last_ok_runs(db_path)

    rows = [
        row for row in storage.list_series(db_path) if row["last_value"] is not None
    ]
    series_ids = [row["id"] for row in rows]

    with contextlib.closing(storage.connect(db_path)) as conn:
        values_1h = storage.values_as_of_bulk(conn, series_ids, now - 3600)
        values_24h = storage.values_as_of_bulk(conn, series_ids, now - 86400)

    metrics: dict[str, Any] = {}
    for row in rows:
        interval = intervals.get(row["plugin_name"], default_interval)
        stale = queries.is_stale(row["plugin_name"], interval, now, last_ok)

        value_1h = values_1h.get(row["id"])
        value_24h = values_24h.get(row["id"])

        metrics[row["metric_key"]] = {
            "value": row["last_value"],
            "label": row["label"],
            "kind": row["kind"],
            "unit": row["unit"],
            "icon": row["icon"],
            "updated": queries.iso(row["last_seen"]),
            "stale": stale,
            "delta_1h": None if value_1h is None else row["last_value"] - value_1h,
            "delta_24h": None if value_24h is None else row["last_value"] - value_24h,
        }

    return {"timestamp": queries.iso(now), "metrics": metrics}


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
    # ruff's Query-in-default exemption only recognizes an inline
    # Literal[...], not an imported type alias -- RangeKey is exactly that
    # alias (queries.py), kept as the one place its members are listed.
    range: RangeKey | None = Query(None),  # noqa: B008
) -> dict[str, Any]:
    # range's Literal type (#128) already gets FastAPI to 422 an invalid
    # value -- and document it as an enum on /docs -- before this handler
    # ever runs; only the hours/range interaction needs a manual check.
    if (hours is None) == (range is None):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of `hours` or `range` is required",
        )

    db_path = request.app.state.config["storage"]["path"]
    # Any known series, active or not (#133): history is kept precisely so
    # a retired series can still be looked at, and /api/metrics already
    # lists inactive keys -- 404ing their history here was the one place
    # that contradicted that. /api/stats/latest stays active-only.
    series = storage.get_any_series_by_key(db_path, metric)
    if series is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric!r}")

    now = int(time.time())
    if hours is not None:
        start = now - hours * 3600
    else:
        # ALL: 0 (long before any real timestamp), not a query for the
        # series' first_seen -- history() already returns nothing before
        # whatever samples exist, so 0 is exactly "from the beginning"
        # without the extra lookup.
        start = queries.range_start(range, now, earliest=0)

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
    # Same active-or-not resolution as /api/stats/history (#133).
    series = storage.get_any_series_by_key(db_path, metric)
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
            "last_seen": queries.iso(row["last_seen"]),
            "active": bool(row["active"]),
            "attrs": queries.parse_attrs(row["attrs"]),
        }
        for row in storage.list_all_series(db_path)
    ]
    return {"metrics": metrics}


@router.get(
    "/plugins",
    tags=["plugins"],
    summary="Poll status for every discovered plugin",
    response_model=ListPluginsResponse,
)
def list_plugins(request: Request) -> dict[str, Any]:
    return {"plugins": queries.plugin_statuses(request)}


def _unhealthy_reason(plugin: dict[str, Any], failure_threshold: int) -> str | None:
    """Why an enabled plugin counts as unhealthy, or None if it doesn't.

    Blocked is unhealthy on the first 403: that isn't a blip, it's a source
    refusing this client, and the scheduler is already backing off. Other
    failures only count once ``failure_threshold`` *finished* runs in a row
    have failed -- storage.consecutive_failures() already excludes an
    in-flight or interrupted run (see its docstring), so there's no
    "currently polling" adjustment to make here.
    """
    if queries.is_blocked(plugin["last_error"]):
        return "blocked"

    finished_failures = plugin["consecutive_failures"]
    if finished_failures >= failure_threshold:
        return f"{finished_failures} consecutive failures"
    return None


@router.get(
    "/integrations",
    tags=["integrations"],
    summary="Status of the maintenance job, MQTT publisher, and milestone webhooks",
    response_model=IntegrationsResponse,
)
def integrations(request: Request) -> dict[str, Any]:
    """Status of background integrations that aren't a plugin poll: the
    scheduled maintenance job (pruning, backups, PRAGMA tuning), the MQTT
    publisher, and milestone webhooks. Never includes a password, broker
    credential, or webhook URL -- only connectivity/delivery status.
    """
    config = request.app.state.config
    db_path = config["storage"]["path"]

    raw = storage.get_state(db_path, scheduler.MAINTENANCE_STATE_KEY)
    last_run = None
    if raw is not None:
        try:
            # Validated here, not left to response_model serialization, so
            # a record shape from some future schema change degrades to
            # None instead of 500ing the endpoint -- there's exactly one
            # writer today (_run_maintenance), but that won't always hold.
            last_run = MaintenanceLastRun(**json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValidationError):
            last_run = None

    next_run = None
    job_scheduler = getattr(request.app.state, "scheduler", None)
    if job_scheduler is not None:
        job = job_scheduler.get_job(scheduler.MAINTENANCE_JOB_ID)
        if job is not None and job.next_run_time is not None:
            next_run = queries.iso(int(job.next_run_time.timestamp()))

    publisher = getattr(request.app.state, "mqtt_publisher", None)
    mqtt_status = (
        publisher.status
        if publisher is not None
        else {
            "enabled": False,
            "connected": False,
            "broker": None,
            "last_publish": None,
            "last_error": None,
        }
    )
    last_publish = mqtt_status.get("last_publish")

    milestone_evaluator = getattr(request.app.state, "milestone_evaluator", None)
    milestone_status = (
        milestone_evaluator.status
        if milestone_evaluator is not None
        else {
            "enabled": False,
            "rules": [],
            "pending": [],
            "last_sent": None,
            "last_error": None,
        }
    )
    last_sent = milestone_status.get("last_sent")
    pending = [
        {
            "metric": item["metric"],
            "threshold": item["threshold"],
            "since": queries.iso(int(item["since"])) if item.get("since") else None,
        }
        for item in milestone_status.get("pending", [])
    ]

    return {
        "maintenance": {
            "last_run": last_run,
            "next_run": next_run,
        },
        "mqtt": {
            "enabled": mqtt_status["enabled"],
            "connected": mqtt_status["connected"],
            "broker": mqtt_status["broker"],
            "last_publish": queries.iso(int(last_publish)) if last_publish else None,
            "last_error": mqtt_status["last_error"],
        },
        "milestones": {
            "enabled": milestone_status["enabled"],
            "rules": milestone_status["rules"],
            "pending": pending,
            "last_sent": queries.iso(int(last_sent)) if last_sent else None,
            "last_error": milestone_status["last_error"],
        },
    }


@health_router.get(
    "/health/plugins",
    tags=["health"],
    summary="Plugin health for uptime monitors",
    response_model=PluginsHealthResponse,
)
def plugins_health(
    request: Request,
    response: Response,
    failures: int = Query(queries.DEFAULT_UNHEALTHY_FAILURES, ge=1),
) -> dict[str, Any]:
    """Plugin health for uptime monitors: 200 when every enabled plugin is
    polling successfully, 503 when any is blocked or has failed ``failures``
    finished polls in a row. Disabled plugins are ignored, and so is a plugin
    that hasn't finished its first poll yet.

    Separate from /health on purpose: that one is liveness, and a source
    being down is no reason for Docker to restart the container.
    """
    unhealthy = []
    for plugin in queries.plugin_statuses(request):
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
