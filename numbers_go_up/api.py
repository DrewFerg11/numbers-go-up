"""REST API: stats, catalogue, and plugin status endpoints.

All routes are read-only queries; new SQL always goes in storage.py, never
here. Mounted on the app in main.py. Anything a route needs beyond a plain
query -- plugin poll intervals, plugin names, the scheduler -- is prepared
once at startup and read off ``app.state`` (see main.lifespan), rather than
recomputed per request or imported as a module-level global.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from numbers_go_up import storage

router = APIRouter(prefix="/api")

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
        }
        for row in storage.list_all_series(db_path)
    ]
    return {"metrics": metrics}


@router.get("/plugins")
def list_plugins(request: Request) -> dict[str, Any]:
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
                    status = "error"
                    last_error = run["error"]

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

    return {"plugins": plugins}
