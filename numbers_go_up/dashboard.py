"""The overview dashboard: page shell and the endpoint that feeds it.

Server-rendered with Jinja2 (``templates/``), styled and scripted with plain
CSS/JS (``static/``) -- no build step, no CDN. This module owns the page
route and ``/api/stats/overview``; all SQL stays in storage.py.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from numbers_go_up import storage
from numbers_go_up.api import (
    DEFAULT_UNHEALTHY_FAILURES,
    _is_stale,
    _parse_attrs,
    _plugin_statuses,
)
from numbers_go_up.plugins import is_pattern_key, resolve_metric

logger = logging.getLogger(__name__)

router = APIRouter()

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Range bounds in hours, per the overview endpoint contract. ALL has no
# fixed bound -- each series starts at its own first sample.
RANGE_HOURS = {
    "1D": 24,
    "1W": 24 * 7,
    "1M": 24 * 30,
    "3M": 24 * 90,
    "1Y": 24 * 365,
}
VALID_RANGES = (*RANGE_HOURS, "ALL")
DEFAULT_RANGE = "1M"

MAX_PINNED = 6
DEFAULT_PINNED_COUNT = 4
MAX_SPARK_POINTS = 60

# Keys we've already warned about missing from ``dashboard.pinned`` -- keeps
# a bad config entry from spamming a log line on every request.
_warned_missing_pinned: set[str] = set()


def _format_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bucket_spark(
    points: list[tuple[int, float]],
    start: int,
    end: int,
    open_value: float,
    value: float,
) -> list[list[float]]:
    """Reduce ``points`` to at most :data:`MAX_SPARK_POINTS` buckets, keeping
    the last value seen in each bucket -- preserves step shape instead of
    smoothing it away. Always starts at ``open_value`` and ends at ``value``,
    even when the open (or latest) sample shares a bucket with another point
    and would otherwise be overwritten.
    """
    if not points:
        return [[start, open_value], [end, value]]

    span = max(end - start, 1)
    bucket_width = max(span // MAX_SPARK_POINTS, 1)

    buckets: dict[int, list[float]] = {}
    for ts, point_value in points:
        index = min((ts - start) // bucket_width, MAX_SPARK_POINTS - 1)
        buckets[index] = [ts, point_value]

    result = [buckets[i] for i in sorted(buckets)]
    result[0][1] = open_value
    result[-1][1] = value
    return result


def _range_bounds(range_key: str, now: int, earliest: int | None) -> int:
    """The ``start`` timestamp for ``range_key``. For ``ALL``, that's the
    earliest first-sample across the included series, or ``now`` if there
    are none yet.
    """
    if range_key == "ALL":
        return earliest if earliest is not None else now
    return now - RANGE_HOURS[range_key] * 3600


def _plugin_metrics_for(
    request: Request, plugin_name: str
) -> dict[str, dict[str, Any]]:
    return getattr(request.app.state, "plugin_metrics", {}).get(plugin_name, {})


def _build_metric(
    request: Request,
    conn: sqlite3.Connection,
    row,
    range_key: str,
    now: int,
    default_interval: int,
    intervals: dict[str, int],
) -> dict[str, Any] | None:
    db_path = request.app.state.config["storage"]["path"]
    if row["last_value"] is None:
        return None

    if range_key == "ALL":
        start = row["first_seen"]
    else:
        start = now - RANGE_HOURS[range_key] * 3600
    stats = storage.range_stats_conn(conn, row["id"], start, now)

    open_value = stats["open"]
    value = row["last_value"]
    if open_value is None:
        open_value = value

    points = list(stats["points"])

    high = stats["high"] if stats["high"] is not None else value
    low = stats["low"] if stats["low"] is not None else value
    high = max(high, value)
    low = min(low, value)

    change = value - open_value
    change_pct = None if open_value == 0 else round((change / open_value) * 100, 2)

    interval = intervals.get(row["plugin_name"], default_interval)
    stale = _is_stale(db_path, row["plugin_name"], row["last_seen"], interval, now)

    metrics_for_plugin = _plugin_metrics_for(request, row["plugin_name"])
    resolved = resolve_metric(row["metric_key"], metrics_for_plugin)
    pattern = resolved[0] if resolved and is_pattern_key(resolved[0]) else None

    return {
        "key": row["metric_key"],
        "plugin": row["plugin_name"],
        "pattern": pattern,
        "label": row["label"],
        "kind": row["kind"],
        "unit": row["unit"],
        "icon": row["icon"],
        "attrs": _parse_attrs(row["attrs"]),
        "value": value,
        "open": open_value,
        "change": change,
        "change_pct": change_pct,
        "high": high,
        "low": low,
        "changes": stats["changes"],
        "updated": _format_iso(row["last_seen"]),
        "stale": stale,
        "spark": _bucket_spark(points, start, now, open_value, value),
    }


def _resolve_pinned(
    config: dict[str, Any], metrics_by_key: dict[str, dict[str, Any]]
) -> list[str]:
    dashboard_config = config.get("dashboard")
    configured = (
        dashboard_config.get("pinned") if isinstance(dashboard_config, dict) else None
    )
    if not isinstance(configured, list):
        configured = []

    pinned: list[str] = []
    for key in configured[:MAX_PINNED]:
        if key not in metrics_by_key:
            if key not in _warned_missing_pinned:
                logger.warning(
                    "dashboard.pinned: metric %r is not a known active metric; "
                    "skipping",
                    key,
                )
                _warned_missing_pinned.add(key)
            continue
        pinned.append(key)

    if pinned:
        return pinned

    # Default: the first N cumulative metrics in catalogue (metric_key) order.
    return [
        metric["key"]
        for metric in metrics_by_key.values()
        if metric["kind"] == "cumulative"
    ][:DEFAULT_PINNED_COUNT]


def build_overview(request: Request, range_key: str) -> dict[str, Any]:
    config = request.app.state.config
    db_path = config["storage"]["path"]
    default_interval = config["poll"]["default_interval"]
    intervals = getattr(request.app.state, "plugin_intervals", {})
    now = int(time.time())

    rows = storage.list_active_series_full(db_path)
    earliest = min(
        (row["first_seen"] for row in rows if row["last_value"] is not None),
        default=None,
    )
    start = _range_bounds(range_key, now, earliest)

    # One shared connection for every series' range_stats, rather than one
    # connection (and its five PRAGMA statements) per series -- with up to
    # 500 pattern-matched series per plugin and a 60s auto-refresh per open
    # tab, that per-series connection cost was the dominant one here.
    metrics: list[dict[str, Any]] = []
    with contextlib.closing(storage.connect(db_path)) as conn:
        for row in rows:
            metric = _build_metric(
                request, conn, row, range_key, now, default_interval, intervals
            )
            if metric is not None:
                metrics.append(metric)

    metrics_by_key = {metric["key"]: metric for metric in metrics}
    pinned = _resolve_pinned(config, metrics_by_key)

    return {
        "timestamp": _format_iso(now),
        "range": range_key,
        "start": _format_iso(start),
        "pinned": pinned,
        "plugins": _plugin_statuses(request),
        "metrics": metrics,
    }


@router.get("/api/stats/overview")
def stats_overview(
    request: Request, range: str = Query(DEFAULT_RANGE)
) -> dict[str, Any]:
    range_key = range
    if range_key not in VALID_RANGES:
        raise HTTPException(
            status_code=422,
            detail=f"range must be one of {VALID_RANGES}, got {range_key!r}",
        )
    return build_overview(request, range_key)


@router.get("/", response_class=HTMLResponse)
def dashboard_index(
    request: Request, range: str = Query(DEFAULT_RANGE)
) -> HTMLResponse:
    range_key = range if range in VALID_RANGES else DEFAULT_RANGE
    plugins_config = request.app.state.config.get("plugins") or {}
    any_enabled = any(
        isinstance(cfg, dict) and cfg.get("enabled") for cfg in plugins_config.values()
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "range": range_key,
            "ranges": VALID_RANGES,
            "any_enabled": any_enabled,
            "unhealthy_failure_threshold": DEFAULT_UNHEALTHY_FAILURES,
        },
    )
