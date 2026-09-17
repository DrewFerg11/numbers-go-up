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
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from numbers_go_up import storage
from numbers_go_up.http import BLOCKED_ERROR_PREFIX

router = APIRouter(prefix="/api")
# Mounted at the root next to /health, not under /api: it's for uptime
# monitors, not dashboard clients.
health_router = APIRouter()

# Matches the dashboard footer's "red" (Failure Handling #2).
DEFAULT_UNHEALTHY_FAILURES = 3

# A full year of one series was measured at 4.9ms, so this cap is about
# keeping responses reasonable, not performance.
MAX_HOURS = 8760


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


@router.get("/stats/latest")
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


@router.get("/stats/history")
def stats_history(
    request: Request, metric: str, hours: int = Query(..., gt=0, le=MAX_HOURS)
) -> dict[str, Any]:
    db_path = request.app.state.config["storage"]["path"]
    series = storage.get_series_by_key(db_path, metric)
    if series is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric!r}")

    now = int(time.time())
    start = now - hours * 3600
    points = storage.history(db_path, series["id"], start, now)

    return {
        "metric": metric,
        "points": [{"ts": ts, "value": value} for ts, value in points],
    }


@router.get("/stats/delta")
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


@router.get("/metrics")
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


@router.get("/integrations")
def integrations(request: Request) -> dict[str, Any]:
    """Status of external integrations -- today, just MQTT. Never includes
    a password or anything broker-credential-shaped, only connectivity."""
    publisher = getattr(request.app.state, "mqtt_publisher", None)
    status = (
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
    last_publish = status.get("last_publish")

    return {
        "mqtt": {
            "enabled": status["enabled"],
            "connected": status["connected"],
            "broker": status["broker"],
            "last_publish": _iso(int(last_publish)) if last_publish else None,
            "last_error": status["last_error"],
        }
    }


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


@router.get("/plugins")
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


@health_router.get("/health/plugins")
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
