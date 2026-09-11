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

    ``label``/``unit``/``icon`` are refreshed on every call — a plugin's
    ``METRICS`` entry is allowed to drift between releases. ``kind`` is not:
    flipping cumulative <-> gauge changes Home Assistant's downstream
    statistics, so a mismatch is logged and the originally stored kind wins.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, kind FROM metric_series WHERE metric_key = ?",
            (metric_key,),
        ).fetchone()

        if row is None:
            cursor = conn.execute(
                "INSERT INTO metric_series "
                "(metric_key, plugin_name, kind, label, unit, icon, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (metric_key, plugin_name, kind, label, unit, icon, now),
            )
            conn.commit()
            return cursor.lastrowid

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
            "UPDATE metric_series SET label = ?, unit = ?, icon = ? WHERE id = ?",
            (label, unit, icon, series_id),
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

    Returns whether a row was written. The sample insert and the
    ``metric_series`` last-value/last-seen update happen in one
    transaction, so a crash between them can't leave the two disagreeing.
    A second write in the same second as an existing sample overwrites it
    (last write wins).
    """
    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT last_value, last_seen FROM metric_series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No series with id {series_id}")
        last_value, last_seen = row

        should_write = (
            last_value is None
            or last_seen is None
            or value != last_value
            or ts - last_seen >= heartbeat_seconds
        )
        if not should_write:
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
