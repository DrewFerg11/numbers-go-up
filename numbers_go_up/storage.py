"""SQLite storage: the connection factory and every query in the app.

This is the only module that touches SQL. Callers get a fresh connection
per operation via :func:`connect` rather than sharing one connection
across threads — a ``sqlite3.Connection`` isn't safe to share across
threads by default, and we don't disable that check.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
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
) -> int:
    """Return the series id for ``metric_key``, creating it if needed.

    ``plugin_name``/``label``/``unit``/``icon`` are refreshed on every call —
    a plugin's ``METRICS`` entry is allowed to drift between releases.
    ``kind`` is not: flipping cumulative <-> gauge changes Home Assistant's
    downstream statistics, so a mismatch is logged and the originally stored
    kind wins.
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
            "SELECT id, kind FROM metric_series WHERE metric_key = ?",
            (metric_key,),
        ).fetchone()
        series_id, stored_kind = row

        if stored_kind != kind:
            logger.warning(
                "Series %s: plugin reports kind=%r but stored kind is %r; "
                "keeping the stored kind",
                metric_key,
                kind,
                stored_kind,
            )

        conn.execute(
            "UPDATE metric_series SET plugin_name = ?, label = ?, unit = ?, "
            "icon = ? WHERE id = ?",
            (plugin_name, label, unit, icon, series_id),
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

    if error is not None and len(error) > _ERROR_TAIL_CHARS:
        error = error[-_ERROR_TAIL_CHARS:]

    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT started_at FROM plugin_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No run with id {run_id}")
        started_at = row[0]
        duration_ms = (finished_at - started_at) * 1000

        conn.execute(
            "UPDATE plugin_runs SET finished_at = ?, status = ?, error = ?, "
            "duration_ms = ?, samples_written = ? WHERE id = ?",
            (finished_at, status, error, duration_ms, samples_written, run_id),
        )
        conn.commit()


def prune_plugin_runs(db_path: str | Path, days: int, now: int) -> int:
    """Delete plugin_runs rows older than ``days`` days before ``now``.

    Never touches samples, metric_series, or state. Returns the number of
    rows deleted.
    """
    cutoff = now - days * 86400
    with contextlib.closing(connect(db_path)) as conn:
        cursor = conn.execute("DELETE FROM plugin_runs WHERE started_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
