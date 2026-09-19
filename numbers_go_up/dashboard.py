"""The overview dashboard: page shell and the endpoint that feeds it.

Server-rendered with Jinja2 (``templates/``), styled and scripted with plain
CSS/JS (``static/``) -- no build step, no CDN. This module owns the page
route and ``/api/stats/overview``; all SQL stays in storage.py.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from numbers_go_up import storage
from numbers_go_up.api import (
    DEFAULT_UNHEALTHY_FAILURES,
    RANGE_HOURS,
    VALID_RANGES,
    _is_stale,
    _parse_attrs,
    _plugin_statuses,
)
from numbers_go_up.http import BLOCKED_ERROR_PREFIX
from numbers_go_up.plugins import is_pattern_key, resolve_metric

logger = logging.getLogger(__name__)

router = APIRouter()

PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEFAULT_RANGE = "1M"

MAX_PINNED = 6
DEFAULT_PINNED_COUNT = 4
MAX_SPARK_POINTS = 60
RECENT_CHANGES_LIMIT = 10
RECORDED_CHANGES_LIMIT = 20

# Keys we've already warned about missing from ``dashboard.pinned`` -- keeps
# a bad config entry from spamming a log line on every request.
_warned_missing_pinned: set[str] = set()


def _format_number(value: float) -> float | int:
    """Render a stored REAL for the template: round away IEEE-754 noise
    from float subtraction (``value - LAG(value)`` can yield e.g.
    ``0.09999999999999964`` for a true ``0.1``), then drop a whole
    number's trailing ``.0`` (samples are stored as REAL even for
    integer-valued metrics).
    """
    value = round(value, 6)
    return int(value) if value == int(value) else value


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
    row,
    stats: dict[str, Any],
    range_key: str,
    now: int,
    default_interval: int,
    intervals: dict[str, int],
    finished_run_cache: dict[str, Any],
) -> dict[str, Any] | None:
    db_path = request.app.state.config["storage"]["path"]
    if row["last_value"] is None:
        return None

    if range_key == "ALL":
        start = row["first_seen"]
    else:
        start = now - RANGE_HOURS[range_key] * 3600

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
    span_days = max((now - start) / 86400, 1)
    avg_per_day = round(change / span_days, 2)

    interval = intervals.get(row["plugin_name"], default_interval)
    stale = _is_stale(
        db_path,
        row["plugin_name"],
        row["last_seen"],
        interval,
        now,
        finished_run_cache,
    )

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
        "avg_per_day": avg_per_day,
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

    active_rows = [row for row in rows if row["last_value"] is not None]
    starts_by_id = {
        row["id"]: row["first_seen"] if range_key == "ALL" else start
        for row in active_rows
    }

    # One shared connection, and one batch of range_stats queries grouped by
    # distinct `start` (a single group for every named range but ALL, whose
    # per-series first_seen start can't be batched the same way) rather than
    # a connection *and* 2-3 queries per series -- with up to 500
    # pattern-matched series per plugin and a 60s auto-refresh per open tab,
    # that per-series cost was the dominant one here.
    finished_run_cache: dict[str, Any] = {}
    metrics: list[dict[str, Any]] = []
    with contextlib.closing(storage.connect(db_path)) as conn:
        stats_by_id = storage.range_stats_bulk_conn(conn, starts_by_id, now)
        for row in active_rows:
            metric = _build_metric(
                request,
                row,
                stats_by_id[row["id"]],
                range_key,
                now,
                default_interval,
                intervals,
                finished_run_cache,
            )
            if metric is not None:
                metrics.append(metric)

    metrics_by_key = {metric["key"]: metric for metric in metrics}
    pinned = _resolve_pinned(config, metrics_by_key)

    changes = storage.recent_changes(db_path, RECENT_CHANGES_LIMIT)

    return {
        "timestamp": _format_iso(now),
        "range": range_key,
        "start": _format_iso(start),
        "pinned": pinned,
        "plugins": _plugin_statuses(request),
        "metrics": metrics,
        "recent_changes": [
            {
                "key": row["metric_key"],
                "ts": _format_iso(row["ts"]),
                "value": row["value"],
                "change": row["change"],
            }
            for row in changes
        ],
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
            "blocked_error_prefix": BLOCKED_ERROR_PREFIX,
        },
    )


def _breadcrumb_group(pattern: str | None) -> str | None:
    """The breadcrumb's third segment for a pattern series -- "Models" for
    ``...model.{id}...``, "Repos" for ``...repo.{id}...``, and generically
    the pluralized literal segment right before the placeholder for any
    other pattern. None for a static (non-pattern) series.
    """
    if pattern is None:
        return None
    segments = pattern.split(".")
    for i, segment in enumerate(segments):
        if segment.startswith("{") and segment.endswith("}") and i > 0:
            noun = segments[i - 1]
            if noun.endswith("y"):
                return noun[:-1].capitalize() + "ies"
            return noun.capitalize() + "s"
    return None


def _safe_url(attrs: dict[str, Any]) -> str | None:
    """``attrs.url`` if it exists and is an ``https`` URL, else None.

    Any other scheme (``javascript:``, plain ``http:``, ...) is dropped --
    this is the one guard against a plugin-supplied URL becoming an
    "Open on {source}" link that does something other than navigate.
    """
    url = attrs.get("url")
    if not isinstance(url, str):
        return None
    return url if urlparse(url).scheme == "https" else None


@router.get("/m/{metric_key}", response_class=HTMLResponse)
def metric_detail(
    request: Request, metric_key: str, range: str = Query(DEFAULT_RANGE)
) -> HTMLResponse:
    range_key = range if range in VALID_RANGES else DEFAULT_RANGE
    config = request.app.state.config
    db_path = config["storage"]["path"]

    row = storage.get_any_series_by_key(db_path, metric_key)
    if row is None or row["last_value"] is None:
        raise HTTPException(status_code=404, detail=f"Unknown metric {metric_key!r}")

    now = int(time.time())
    if range_key == "ALL":
        start = row["first_seen"]
    else:
        start = now - RANGE_HOURS[range_key] * 3600
    stats = storage.range_stats(db_path, row["id"], start, now)

    open_value = stats["open"] if stats["open"] is not None else row["last_value"]
    value = row["last_value"]
    change = value - open_value
    change_pct = None if open_value == 0 else round((change / open_value) * 100, 2)

    points = list(stats["points"])
    values = [v for _, v in points] or [value]
    high = max(max(values), value)
    span_days = max((now - start) / 86400, 1)
    avg_per_day = round((value - open_value) / span_days, 2)
    best_day_change = None
    if len(points) >= 2:
        by_day: dict[int, float] = {}
        for ts, v in points:
            by_day[ts // 86400] = v
        day_values = [v for _, v in sorted(by_day.items())]
        daily_changes = [
            b - a for a, b in zip(day_values, day_values[1:], strict=False)
        ]
        if daily_changes:
            best_day_change = max(daily_changes)

    metrics_for_plugin = _plugin_metrics_for(request, row["plugin_name"])
    resolved = resolve_metric(row["metric_key"], metrics_for_plugin)
    pattern = resolved[0] if resolved and is_pattern_key(resolved[0]) else None

    intervals = getattr(request.app.state, "plugin_intervals", {})
    default_interval = config["poll"]["default_interval"]
    interval = intervals.get(row["plugin_name"], default_interval)
    stale = _is_stale(db_path, row["plugin_name"], row["last_seen"], interval, now)

    attrs = _parse_attrs(row["attrs"])
    recorded = storage.recorded_changes(
        db_path, row["id"], start, now, RECORDED_CHANGES_LIMIT
    )

    metric = {
        "key": row["metric_key"],
        "plugin": row["plugin_name"],
        "pattern": pattern,
        "breadcrumb_group": _breadcrumb_group(pattern),
        "label": row["label"],
        "kind": row["kind"],
        "unit": row["unit"],
        "attrs": attrs,
        "url": _safe_url(attrs),
        "value": _format_number(value),
        "open": _format_number(open_value),
        "change": _format_number(change),
        "change_pct": change_pct,
        "high": _format_number(high),
        "avg_per_day": avg_per_day,
        "best_day_change": (
            None if best_day_change is None else _format_number(best_day_change)
        ),
        "changes": stats["changes"],
        "first_seen": _format_iso(row["first_seen"]),
        "updated": _format_iso(row["last_seen"]),
        "stale": stale,
        "active": bool(row["active"]),
        "recorded_changes": [
            {
                "ts": _format_iso(r["ts"]),
                "value": _format_number(r["value"]),
                "change": _format_number(r["change"]),
            }
            for r in recorded
        ],
    }

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "range": range_key,
            "ranges": VALID_RANGES,
            "metric": metric,
        },
    )
