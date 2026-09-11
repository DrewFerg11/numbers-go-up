"""Migration runner: version check, downgrade guard, backup, and apply.

Each migration ships as a ``NNNN_name.sql`` file. The runner reads
``schema_migrations`` to find the current version, then applies every
pending file in its own transaction. See ``numbers_go_up/migrations/`` for
the shipped migrations.
"""

from __future__ import annotations

import datetime
import importlib.resources
import logging
import re
import sqlite3
from pathlib import Path

from numbers_go_up import storage

logger = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d+)_(\w+)\.sql$")
_BACKUP_KEEP = 5


class DowngradeError(Exception):
    """The database's schema version is newer than this build understands."""


def _builtin_migrations_dir() -> Path:
    return Path(str(importlib.resources.files("numbers_go_up.migrations")))


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    found = []
    for path in Path(migrations_dir).glob("*.sql"):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            continue
        found.append((int(match.group(1)), match.group(2), path))
    found.sort(key=lambda item: item[0])
    return found


def _current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def _backup(db_path: Path, version: int) -> Path:
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
    backup_path = backups_dir / f"stats-pre-v{version}-{timestamp}.db"

    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    _prune_backups(backups_dir)
    return backup_path


def _prune_backups(backups_dir: Path, keep: int = _BACKUP_KEEP) -> None:
    backups = sorted(backups_dir.glob("stats-pre-v*.db"))
    for stale in backups[: max(0, len(backups) - keep)]:
        stale.unlink()


def run_migrations(db_path: str | Path, migrations_dir: Path | None = None) -> None:
    """Bring the database at ``db_path`` up to the latest schema version.

    ``migrations_dir`` defaults to the built-in package migrations; tests
    point it at a temp directory holding a copy of ``0001`` plus a
    throwaway migration so the real one never ships to production.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if migrations_dir is not None:
        resolved_dir = Path(migrations_dir)
    else:
        resolved_dir = _builtin_migrations_dir()

    migrations = _discover_migrations(resolved_dir)
    latest_known = migrations[-1][0] if migrations else 0

    conn = storage.connect(db_path)
    try:
        current = _current_version(conn)

        if current > latest_known:
            raise DowngradeError(
                f"Database schema is at version {current}, but this build only "
                f"knows migrations up to version {latest_known}. Refusing to "
                "start against a database newer than the code."
            )

        pending = [m for m in migrations if m[0] > current]
        if pending:
            if current > 0:
                _backup(db_path, current)

            conn.isolation_level = None
            for version, name, path in pending:
                sql = path.read_text(encoding="utf-8")
                name_escaped = name.replace("'", "''")
                script = (
                    "BEGIN;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations (version, name, applied_at) "
                    f"VALUES ({version}, '{name_escaped}', strftime('%s', 'now'));\n"
                    "COMMIT;\n"
                )
                try:
                    conn.executescript(script)
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                logger.info("Applied migration %04d_%s", version, name)

        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
