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
