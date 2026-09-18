"""SQLite storage: the connection factory and every query in the app.

This is the only module that touches SQL. Callers get a fresh connection
per operation via :func:`connect` rather than sharing one connection
across threads — a ``sqlite3.Connection`` isn't safe to share across
threads by default, and we don't disable that check.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_KINDS = {"gauge", "cumulative"}
VALID_RUN_STATUSES = {"ok", "error"}

# plugin_runs.status has NOT NULL + CHECK(status IN ('ok', 'error')), so
# there's no schema-level "running" state. start_run() inserts this sentinel
# into `error` and status='error'; finish_run() overwrites both. The nice
# side effect: a run that crashes mid-flight, and is never finished, already
# reads as a failure. #19's consecutive_failures and the /api/plugins
# endpoint both rely on this convention.
_RUN_IN_PROGRESS = "run in progress"

_ERROR_TAIL_CHARS = 500


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a new connection with the project's five pragmas applied.

    ``journal_mode`` must be set before anything else touches the
    connection — it can't be changed inside a transaction.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def get_or_create_series(
    db_path: str | Path,
    metric_key: str,
    plugin_name: str,
    kind: str,
    label: str | None,
    unit: str | None,
    icon: str | None,
    now: int,
    attrs: dict | None = None,
) -> int:
    """Return the series id for ``metric_key``, creating it if needed.

    ``plugin_name``/``label``/``unit``/``icon`` are refreshed on every call —
    a plugin's ``METRICS`` entry is allowed to drift between releases.
    ``kind`` is not: flipping cumulative <-> gauge changes Home Assistant's
    downstream statistics, so a mismatch is logged and the originally stored
    kind wins.

    ``attrs``, when given, is merged into the stored ``metric_series.attrs``
    JSON object (new keys added, existing keys overwritten) rather than
    replacing it wholesale -- a poll that reports a subset of attrs must not
    erase attrs a previous poll wrote. ``None`` (the default) leaves the
    stored attrs untouched.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO metric_series "
            "(metric_key, plugin_name, kind, label, unit, icon, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (metric_key) DO NOTHING",
            (metric_key, plugin_name, kind, label, unit, icon, now),
        )
        row = conn.execute(
            "SELECT id, kind, attrs FROM metric_series WHERE metric_key = ?",
            (metric_key,),
        ).fetchone()
        series_id, stored_kind, stored_attrs_json = row

        if stored_kind != kind:
            logger.warning(
                "Series %s: plugin reports kind=%r but stored kind is %r; "
                "keeping the stored kind",
                metric_key,
                kind,
                stored_kind,
            )

        if attrs:
            try:
                merged_attrs = (
                    json.loads(stored_attrs_json) if stored_attrs_json else {}
                )
            except json.JSONDecodeError:
                merged_attrs = {}
            # A hand-edited row (or a future buggy writer) could hold
            # valid-but-non-object JSON ("[1,2]", "3"); .update() on
            # anything but a dict raises AttributeError, which would
            # escape into run_plugin_once's blanket except and abort the
            # rest of the poll -- exactly what the scheduler-boundary
            # validation of the *new* attrs is there to prevent. Treat a
            # corrupt stored value as "start fresh" instead.
            if not isinstance(merged_attrs, dict):
                merged_attrs = {}
            merged_attrs.update(attrs)
            attrs_json = json.dumps(merged_attrs)
        else:
            attrs_json = stored_attrs_json

        conn.execute(
            "UPDATE metric_series SET plugin_name = ?, label = ?, unit = ?, "
            "icon = ?, attrs = ? WHERE id = ?",
            (plugin_name, label, unit, icon, attrs_json, series_id),
        )
        conn.commit()
        return series_id


def record_sample(
    db_path: str | Path,
    series_id: int,
    ts: int,
    value: float,
    heartbeat_seconds: int,
) -> bool:
    """Store-on-change: write a sample only if the value changed or the
    heartbeat interval has elapsed since the last written sample.

    Returns whether a row was written. The store-on-change check, the sample
    insert, and the ``metric_series`` last-value/last-seen update all happen
    in one ``BEGIN IMMEDIATE`` transaction, so concurrent callers can't both
    read a stale last-value and write a redundant sample, and a crash can't
    leave the sample and the last-value disagreeing. A second write in the
    same second as an existing sample overwrites it (last write wins).
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_value, last_seen FROM metric_series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise ValueError(f"No series with id {series_id}")
        last_value, last_seen = row

        should_write = (
            last_value is None
            or last_seen is None
            or value != last_value
            or ts - last_seen >= heartbeat_seconds
        )
        if not should_write:
            conn.rollback()
            return False

        conn.execute(
            "INSERT INTO samples (series_id, ts, value) VALUES (?, ?, ?) "
            "ON CONFLICT (series_id, ts) DO UPDATE SET value = excluded.value",
            (series_id, ts, value),
        )
        conn.execute(
            "UPDATE metric_series SET last_value = ?, last_seen = ? WHERE id = ?",
            (value, ts, series_id),
        )
        conn.commit()
        return True


def start_run(db_path: str | Path, plugin_name: str, started_at: int) -> int:
    """Record that ``plugin_name`` started a poll. Returns the run id.

    The row is inserted as an in-progress failure (see ``_RUN_IN_PROGRESS``)
    and ``finish_run`` overwrites it with the real outcome.
    """
    with contextlib.closing(connect(db_path)) as conn:
        cursor = conn.execute(
            "INSERT INTO plugin_runs "
            "(plugin_name, started_at, status, error, samples_written) "
            "VALUES (?, ?, 'error', ?, 0)",
            (plugin_name, started_at, _RUN_IN_PROGRESS),
        )
        conn.commit()
        return cursor.lastrowid


def error_tail(error: str | None) -> str | None:
    """Cap error text at its last ``_ERROR_TAIL_CHARS`` characters.

    Failure Handling #5: store the *tail* of a traceback, not the head —
    the exception is at the end. This is the single place the cap is
    applied, so the text a caller keeps and the text written to
    ``plugin_runs`` are always identical. A real traceback's length
    depends on the filesystem paths it was raised from, so "short enough"
    is a property of the environment, never something to rely on.
    """
    if error is not None and len(error) > _ERROR_TAIL_CHARS:
        return error[-_ERROR_TAIL_CHARS:]
    return error


def finish_run(
    db_path: str | Path,
    run_id: int,
    status: str,
    error: str | None,
    samples_written: int,
    finished_at: int,
) -> None:
    """Record the outcome of a run started by :func:`start_run`.

    ``error`` is truncated to its last ~500 characters — the tail of a
    traceback, where the actual exception is, not the head.
    """
    if status not in VALID_RUN_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(VALID_RUN_STATUSES)}, got {status!r}"
        )

    error = error_tail(error)

    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT started_at FROM plugin_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No run with id {run_id}")
        started_at = row[0]
        # started_at/finished_at are unix seconds, so duration_ms is only ever
        # a multiple of 1000 and sub-second polls round to 0 ms. That's fine
        # here: plugin_runs is a liveness record, not a profiler.
        duration_ms = (finished_at - started_at) * 1000

        conn.execute(
            "UPDATE plugin_runs SET finished_at = ?, status = ?, error = ?, "
            "duration_ms = ?, samples_written = ? WHERE id = ?",
            (finished_at, status, error, duration_ms, samples_written, run_id),
        )
        conn.commit()


def prune_plugin_runs(db_path: str | Path, days: int, now: int) -> int:
    """Delete plugin_runs rows older than ``days`` days before ``now``.

    The boundary is exclusive: a row exactly ``days`` days old is kept, so
    the last ``days`` days of runs are always retained. Never touches
    samples, metric_series, or state. Returns the number of rows deleted.
    """
    cutoff = now - days * 86400
    with contextlib.closing(connect(db_path)) as conn:
        cursor = conn.execute("DELETE FROM plugin_runs WHERE started_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount


def consecutive_failures(db_path: str | Path, plugin_name: str) -> int:
    """Count of the most recent runs for ``plugin_name`` that errored,
    counting back until (and not including) the last success.

    Derived from ``plugin_runs`` via ``idx_runs_plugin_time``; no new
    column. A plugin with no runs at all has 0 consecutive failures.
    """
    with contextlib.closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT status FROM plugin_runs WHERE plugin_name = ? "
            "ORDER BY started_at DESC",
            (plugin_name,),
        ).fetchall()

    count = 0
    for (status,) in rows:
        if status != "ok":
            count += 1
        else:
            break
    return count


def latest_finished_run(db_path: str | Path, plugin_name: str) -> sqlite3.Row | None:
    """The newest plugin_runs row for ``plugin_name`` with a known outcome,
    or None if the plugin has never finished a run.

    Rows still in flight (``finished_at IS NULL`` -- start_run's
    ``_RUN_IN_PROGRESS`` sentinel, overwritten by ``finish_run``) are
    skipped rather than read as failures: a poll in flight means the
    plugin is alive. Deliberately the opposite of
    :func:`consecutive_failures`, whose liveness convention (#19) counts
    an unfinished run as a failure.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT started_at, finished_at, status, error FROM plugin_runs "
            "WHERE plugin_name = ? AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (plugin_name,),
        ).fetchone()


def latest(db_path: str | Path, series_ids: Iterable[int]) -> dict[int, float]:
    """Return the newest value for each of ``series_ids`` in one call.

    Reads ``metric_series.last_value`` directly rather than querying
    ``samples`` — correct as long as :func:`record_sample` keeps it
    updated in the same transaction as the insert. Series with no samples
    yet are omitted rather than reported as an error or a zero.
    """
    ids = list(series_ids)
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    with contextlib.closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT id, last_value FROM metric_series "
            f"WHERE id IN ({placeholders}) AND last_value IS NOT NULL",
            ids,
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def value_as_of(db_path: str | Path, series_id: int, ts: int) -> float | None:
    """Last-value-carried-forward: the newest sample with ``ts`` <= the given time.

    Returns ``None`` if the series has no sample at or before ``ts``
    (including an unknown series).
    """
    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM samples WHERE series_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (series_id, ts),
        ).fetchone()
    return row[0] if row is not None else None


def list_series(db_path: str | Path) -> list[sqlite3.Row]:
    """Every active metric_series row, ordered by metric_key.

    /api/stats/latest needs every series' current state in one shot rather
    than one query per series. Deactivated series (``active = 0``) are
    excluded so the first future writer of that flag can't end up silently
    serving deactivated metrics. /api/metrics wants every series regardless
    of ``active`` -- see :func:`list_all_series`.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, plugin_name, kind, label, unit, icon, "
            "last_value, last_seen FROM metric_series "
            "WHERE active = 1 ORDER BY metric_key"
        ).fetchall()


def list_all_series(db_path: str | Path) -> list[sqlite3.Row]:
    """Every metric_series row, active or not, ordered by metric_key,
    ``active`` and ``attrs`` columns included.

    The catalogue for /api/metrics, which reports on every series that
    ever existed -- including a retired one whose plugin has since been
    disabled or removed -- unlike :func:`list_series`, which /api/stats/
    latest uses and which excludes deactivated series.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, plugin_name, kind, label, unit, icon, "
            "last_value, last_seen, active, attrs FROM metric_series "
            "ORDER BY metric_key"
        ).fetchall()


def series_for_plugin(db_path: str | Path, plugin_name: str) -> list[sqlite3.Row]:
    """Every metric_series row (active or not) for ``plugin_name``, with
    just ``id``, ``metric_key`` and ``active`` -- what the pattern-series
    lifecycle reconciliation in the scheduler needs after a successful run.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, active FROM metric_series WHERE plugin_name = ?",
            (plugin_name,),
        ).fetchall()


def set_series_active_bulk(
    db_path: str | Path, activate_ids: Iterable[int], deactivate_ids: Iterable[int]
) -> None:
    """Flip ``metric_series.active`` for many series in one transaction.

    The first writer of this column (#54): deactivating keeps a pattern
    series' history intact while dropping it from
    :func:`list_series`/``/api/stats/latest``; reactivating brings it back
    when the plugin returns the key again on a later successful poll.

    Used by the scheduler's pattern-series lifecycle reconciliation, which
    can touch many series after one poll (a MakerWorld-style plugin with
    hundreds of subjects). A single transaction rather than one
    connection+commit per series means the whole sweep is atomic -- a
    crash mid-sweep can no longer leave some subjects deactivated and
    others not until the next successful poll happens to repair it.
    """
    activate_ids = list(activate_ids)
    deactivate_ids = list(deactivate_ids)
    if not activate_ids and not deactivate_ids:
        return

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if activate_ids:
            placeholders = ",".join("?" for _ in activate_ids)
            conn.execute(
                f"UPDATE metric_series SET active = 1 WHERE id IN ({placeholders})",
                activate_ids,
            )
        if deactivate_ids:
            placeholders = ",".join("?" for _ in deactivate_ids)
            conn.execute(
                f"UPDATE metric_series SET active = 0 WHERE id IN ({placeholders})",
                deactivate_ids,
            )
        conn.commit()


def get_series_by_key(db_path: str | Path, metric_key: str) -> sqlite3.Row | None:
    """The active metric_series row for ``metric_key`` -- None if it's
    unknown or deactivated (``active = 0``), so history/delta on a
    deactivated key 404s instead of serving its history.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, plugin_name, kind, label, unit, icon, "
            "last_value, last_seen FROM metric_series "
            "WHERE active = 1 AND metric_key = ?",
            (metric_key,),
        ).fetchone()


def get_any_series_by_key(db_path: str | Path, metric_key: str) -> sqlite3.Row | None:
    """The metric_series row for ``metric_key``, active or not -- None if
    the key was never seen. Unlike :func:`get_series_by_key`, which
    /api/stats/history and /api/stats/delta use and which 404s a
    deactivated key, the dashboard's detail page (``/m/{key}``) renders an
    inactive series too (its history is kept), with an "inactive" chip.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, plugin_name, kind, label, unit, icon, "
            "last_value, last_seen, first_seen, active, attrs FROM metric_series "
            "WHERE metric_key = ?",
            (metric_key,),
        ).fetchone()


def latest_run(db_path: str | Path, plugin_name: str) -> sqlite3.Row | None:
    """The single newest plugin_runs row for ``plugin_name``, whatever its
    outcome. None if the plugin has never run.

    Unlike :func:`consecutive_failures`, which stops counting at the first
    success, this always returns the most recent run -- /api/plugins'
    ``last_poll`` and ``last_error`` need the newest run regardless of
    whether it succeeded.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT started_at, finished_at, status, error FROM plugin_runs "
            "WHERE plugin_name = ? ORDER BY started_at DESC LIMIT 1",
            (plugin_name,),
        ).fetchone()


def metric_keys_for_plugin(db_path: str | Path, plugin_name: str) -> list[str]:
    """Every metric_key currently in metric_series for ``plugin_name``."""
    with contextlib.closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT metric_key FROM metric_series WHERE plugin_name = ? "
            "ORDER BY metric_key",
            (plugin_name,),
        ).fetchall()
    return [row[0] for row in rows]


def list_active_series_full(db_path: str | Path) -> list[sqlite3.Row]:
    """Every active metric_series row with the extra columns the dashboard
    overview needs (``first_seen``, ``attrs``) that :func:`list_series`
    leaves out, ordered by metric_key.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, metric_key, plugin_name, kind, label, unit, icon, "
            "last_value, last_seen, first_seen, attrs FROM metric_series "
            "WHERE active = 1 ORDER BY metric_key"
        ).fetchall()


def range_stats_conn(
    conn: sqlite3.Connection, series_id: int, start: int, end: int
) -> dict[str, Any]:
    """Same as :func:`range_stats`, against an already-open connection.

    The dashboard overview calls this once per active series on every
    request (up to the pattern-key cap of 500 per plugin); opening a fresh
    connection per series -- each paying its own five PRAGMA statements --
    is the dominant cost at that scale, so callers doing many of these in
    one request should open a single connection with :func:`connect` and
    reuse it here rather than call :func:`range_stats` in a loop.
    """
    anchor = conn.execute(
        "SELECT value FROM samples WHERE series_id = ? AND ts <= ? "
        "ORDER BY ts DESC LIMIT 1",
        (series_id, start),
    ).fetchone()

    if anchor is not None:
        open_value = anchor[0]
        after_ts = start
        points: list[tuple[int, float]] = [(start, open_value)]
    else:
        # No sample at or before `start`: the series is younger than
        # the range. Its own first sample becomes `open`, and only
        # samples strictly after that one count toward `changes` --
        # otherwise the opening sample would be double-counted, once
        # as `open` and once as a "change".
        first = conn.execute(
            "SELECT ts, value FROM samples WHERE series_id = ? ORDER BY ts ASC LIMIT 1",
            (series_id,),
        ).fetchone()
        if first is None:
            open_value = None
            after_ts = start
            points = []
        else:
            open_value = first[1]
            after_ts = first[0]
            points = [(first[0], open_value)]

    rows = conn.execute(
        "SELECT ts, value FROM samples WHERE series_id = ? AND ts > ? AND ts <= ? "
        "ORDER BY ts ASC",
        (series_id, after_ts, end),
    ).fetchall()

    points.extend((row[0], row[1]) for row in rows)

    values = [value for _, value in points]
    return {
        "open": open_value,
        "high": max(values) if values else None,
        "low": min(values) if values else None,
        "changes": len(rows),
        "points": points,
    }


def range_stats_bulk_conn(
    conn: sqlite3.Connection, starts: dict[int, int], end: int
) -> dict[int, dict[str, Any]]:
    """Same per-series contract as :func:`range_stats_conn` (keyed by
    ``series_id``, one entry per key in ``starts``), grouped by distinct
    ``start`` value so every series sharing one shares its *points*
    queries too -- the part of :func:`range_stats_conn` that genuinely
    batches well. See :func:`_range_stats_group` for what does and
    doesn't batch, and why.

    Every named range but ``ALL`` shares one ``start`` across every series
    in a dashboard overview request, so this is the common case; for
    ``ALL`` (each series' own ``first_seen``), starts rarely coincide and
    this falls back to one group per series -- not the O(series)
    *connections* the shared connection in :func:`build_overview
    <numbers_go_up.dashboard.build_overview>` already eliminates, just one
    ungrouped call to :func:`_range_stats_group` per series.
    """
    by_start: dict[int, list[int]] = {}
    for series_id, start in starts.items():
        by_start.setdefault(start, []).append(series_id)

    result: dict[int, dict[str, Any]] = {}
    for start, series_ids in by_start.items():
        result.update(_range_stats_group(conn, series_ids, start, end))
    return result


def _range_stats_group(
    conn: sqlite3.Connection, series_ids: list[int], start: int, end: int
) -> dict[int, dict[str, Any]]:
    """One ``start``/``end`` window's worth of :func:`range_stats_conn`
    for every series in ``series_ids``. The anchor/opening-value lookups
    stay one indexed seek per series (see the comment below for why a
    batched version of those was measured slower); the points lookups --
    the actual per-series row data, not just one value -- are batched
    into two queries total for the whole group instead of one per
    series. See :func:`range_stats_bulk_conn`.
    """
    # Anchor / firsts: the newest sample at or before `start` (or, for a
    # series younger than the range, its own first sample), per series.
    # These stay per-series "ORDER BY ts DESC/ASC LIMIT 1" seeks rather
    # than a single batched query across series -- both a ROW_NUMBER/
    # PARTITION BY window and a MAX(ts)/MIN(ts) GROUP BY were tried and
    # measured *slower* than the seeks they'd replace (confirmed in
    # review): the window forces SQLite to materialize every matching row
    # per group into a temp B-tree before it can pick rn=1, and GROUP BY
    # loses the MIN/MAX aggregate's own index short-circuit the moment a
    # second bare column (``value``) is selected alongside it, so it still
    # visits every row in each group. A LIMIT-1 seek has neither problem,
    # and it's cheap: no PRAGMA/connect() cost since every series here
    # shares the one already-open connection -- that per-*connection* cost
    # is what batching this function exists to remove, not the seeks
    # themselves. The points queries below are where batching genuinely
    # wins (one query across every series in the group, not one per
    # series), so those stay batched.
    anchors: dict[int, float] = {}
    unanchored_ids: list[int] = []
    for series_id in series_ids:
        row = conn.execute(
            "SELECT value FROM samples WHERE series_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (series_id, start),
        ).fetchone()
        if row is not None:
            anchors[series_id] = row[0]
        else:
            unanchored_ids.append(series_id)

    firsts: dict[int, tuple[int, float]] = {}
    for series_id in unanchored_ids:
        row = conn.execute(
            "SELECT ts, value FROM samples WHERE series_id = ? ORDER BY ts ASC LIMIT 1",
            (series_id,),
        ).fetchone()
        if row is not None:
            firsts[series_id] = row

    points_by_series: dict[int, list[tuple[int, float]]] = {
        sid: [] for sid in series_ids
    }

    anchored_ids = list(anchors)
    if anchored_ids:
        placeholders_a = ",".join("?" for _ in anchored_ids)
        for series_id, ts, value in conn.execute(
            f"SELECT series_id, ts, value FROM samples "
            f"WHERE series_id IN ({placeholders_a}) AND ts > ? AND ts <= ? "
            f"ORDER BY series_id, ts",
            (*anchored_ids, start, end),
        ):
            points_by_series[series_id].append((ts, value))

    if firsts:
        # Every sample after the series' own first, same "strictly after
        # the opening sample" rule range_stats_conn applies via
        # `after_ts = first[0]`. A per-series ``first_ts`` threshold
        # can't share one `ts > ?` bound the way the anchored group
        # above shares `start`, so this joins each series to its own
        # threshold via a VALUES row instead of a ROW_NUMBER window --
        # same "index seek per series, not a materialized sort" reasoning
        # as the anchor/firsts lookups above.
        values_clause = ",".join("(?,?)" for _ in firsts)
        params: list[int] = []
        for series_id, (first_ts, _) in firsts.items():
            params.extend((series_id, first_ts))
        params.append(end)
        for series_id, ts, value in conn.execute(
            f"""
            SELECT s.series_id, s.ts, s.value
            FROM samples s
            JOIN (
                SELECT column1 AS series_id, column2 AS first_ts
                FROM (VALUES {values_clause})
            ) AS f ON f.series_id = s.series_id
            WHERE s.ts > f.first_ts AND s.ts <= ?
            ORDER BY s.series_id, s.ts
            """,
            params,
        ):
            points_by_series[series_id].append((ts, value))

    result: dict[int, dict[str, Any]] = {}
    for series_id in series_ids:
        if series_id in anchors:
            open_value: float | None = anchors[series_id]
            points: list[tuple[int, float]] = [(start, open_value)]
        elif series_id in firsts:
            first_ts, open_value = firsts[series_id]
            points = [(first_ts, open_value)]
        else:
            open_value = None
            points = []

        extra = points_by_series[series_id]
        points.extend(extra)

        values = [value for _, value in points]
        result[series_id] = {
            "open": open_value,
            "high": max(values) if values else None,
            "low": min(values) if values else None,
            "changes": len(extra),
            "points": points,
        }
    return result


def range_stats(
    db_path: str | Path, series_id: int, start: int, end: int
) -> dict[str, Any]:
    """Everything the dashboard overview needs for one series over one range,
    in a single connection.

    ``open`` is the carried-forward value as of ``start`` when one exists,
    else the series' very first sample (so a series younger than the range
    still gets a non-null open, per the overview endpoint's contract).
    ``points`` is ``[(ts, value), ...]``: the open point (if any) followed by
    every literal sample in ``(start, end]`` -- the same shape
    :func:`history` returns, plus ``high``/``low``/``changes`` computed from
    the same rows so the overview endpoint doesn't need extra round trips.

    A thin single-series wrapper around :func:`range_stats_conn` for
    callers (tests, one-off queries) that don't already hold a connection
    -- the dashboard overview loop uses :func:`range_stats_conn` directly
    against one shared connection instead of calling this per series.
    """
    with contextlib.closing(connect(db_path)) as conn:
        return range_stats_conn(conn, series_id, start, end)


def recorded_changes(
    db_path: str | Path, series_id: int, start: int, end: int, limit: int
) -> list[dict[str, Any]]:
    """The ``limit`` newest stored samples for ``series_id`` in ``(start,
    end]``, newest first, each with its signed change from the previous
    stored sample (across the whole series history, not just the range, so
    the oldest row returned still has a correct change).

    This is exactly what's stored, heartbeats included: a heartbeat row
    with no real change comes back with ``change == 0`` rather than being
    filtered out, unlike :func:`recent_changes`.
    """
    with contextlib.closing(connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT ts, value, value - LAG(value) OVER (ORDER BY ts) AS change
            FROM samples
            WHERE series_id = ?
            ORDER BY ts
            """,
            (series_id,),
        ).fetchall()

    in_range = [
        {"ts": ts, "value": value, "change": 0 if change is None else change}
        for ts, value, change in rows
        if start < ts <= end
    ]
    in_range.sort(key=lambda row: row["ts"], reverse=True)
    return in_range[:limit]


def recent_changes(db_path: str | Path, limit: int) -> list[sqlite3.Row]:
    """The ``limit`` newest value-to-previous-value changes across every
    active series, newest first.

    A ``LAG()`` window over ``samples`` (joined to ``metric_series`` for the
    key and active flag) gives each sample's change from the one before it
    in the same series; a null change (a series' very first sample, nothing
    to compare against) or a zero change (a heartbeat with no real change)
    is dropped, so only genuine value changes ever show up here.
    """
    with contextlib.closing(connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT metric_key, ts, value, change FROM (
                SELECT
                    ms.metric_key AS metric_key,
                    ms.active AS active,
                    s.ts AS ts,
                    s.value AS value,
                    s.value - LAG(s.value) OVER (
                        PARTITION BY s.series_id ORDER BY s.ts
                    ) AS change
                FROM samples s
                JOIN metric_series ms ON ms.id = s.series_id
            )
            WHERE active = 1 AND change IS NOT NULL AND change != 0
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def history(
    db_path: str | Path, series_id: int, start: int, end: int
) -> list[tuple[int, float]]:
    """Samples for ``series_id`` in ``(start, end]``, anchored at ``start``.

    Under store-on-change a series flat across the whole window has no
    samples inside it, so the first point is always the carried-forward
    value as of ``start`` (i.e. ``value_as_of(series_id, start)``) when one
    exists, followed by any literal samples strictly after ``start`` up to
    and including ``end``. An unknown series, or one with nothing at or
    before ``start`` and nothing in range, returns an empty list.
    """
    with contextlib.closing(connect(db_path)) as conn:
        anchor = conn.execute(
            "SELECT value FROM samples WHERE series_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (series_id, start),
        ).fetchone()
        rows = conn.execute(
            "SELECT ts, value FROM samples WHERE series_id = ? AND ts > ? AND ts <= ? "
            "ORDER BY ts ASC",
            (series_id, start, end),
        ).fetchall()

    points: list[tuple[int, float]] = []
    if anchor is not None:
        points.append((start, anchor[0]))
    points.extend((row[0], row[1]) for row in rows)
    return points
