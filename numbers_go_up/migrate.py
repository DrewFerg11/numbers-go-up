"""Migration runner: version check, downgrade guard, backup, and apply.

Each migration ships as a ``NNNN_name.sql`` file. The runner reads
``schema_migrations`` to find the current version, then applies every
pending file inside a single ``BEGIN IMMEDIATE`` transaction so that
concurrent worker processes serialise rather than racing to create the
same tables. See ``numbers_go_up/migrations/`` for the shipped migrations.

**Table rebuilds and foreign keys.** ``storage.connect()`` enables
``PRAGMA foreign_keys``, and ``samples.series_id`` is declared
``REFERENCES metric_series(id) ON DELETE CASCADE``. With foreign keys on,
``DROP TABLE`` does an implicit ``DELETE FROM`` first, which fires that
cascade -- so the ordinary SQLite table-rebuild pattern (create a new
table, copy the data across, drop the old table, rename the new one into
place) would silently delete every row in ``samples`` the moment
``metric_series`` is rebuilt. ``PRAGMA foreign_keys`` cannot be changed
inside a transaction, so this runner disables it before ``BEGIN
IMMEDIATE`` and runs ``PRAGMA foreign_key_check`` before ``COMMIT`` to
catch anything a migration got wrong anyway (including the *other* unsafe
ordering: renaming the live table out of the way first, which SQLite
>= 3.26 handles by rewriting the child's ``REFERENCES`` to follow it, so
it ends up pointing at the dropped table). Only this runner's connection
disables foreign keys -- runtime connections from ``storage.connect()``
keep them on throughout.

A migration that rebuilds a table must therefore always go: create the
new table -> copy the data -> drop the old table -> rename the new table
into place. Never rename the live table out of the way first.
"""

from __future__ import annotations

import datetime
import importlib.resources
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from numbers_go_up import storage

logger = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d+)_(\w+)\.sql$")
_BACKUP_KEEP = 5


class DowngradeError(Exception):
    """The database's schema version is newer than this build understands."""


class MigrationError(Exception):
    """A migration left the database in an inconsistent state."""


def _builtin_migrations_dir() -> Path:
    return Path(str(importlib.resources.files("numbers_go_up.migrations")))


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    found = []
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            raise ValueError(
                f"Migration file {path.name!r} does not match the expected "
                f"NNNN_name.sql pattern. Refusing to silently skip it."
            )
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


_BACKUP_RE = re.compile(r"^stats-pre-v(\d+)-(\d{8}T\d{6})\.db$")


def _backup(db_path: Path, version: int) -> Path:
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
    backup_path = backups_dir / f"stats-pre-v{version}-{timestamp}.db"

    storage.copy_and_verify(db_path, backup_path)

    _prune_backups(backups_dir)
    return backup_path


def _prune_backups(backups_dir: Path, keep: int = _BACKUP_KEEP) -> None:
    """Delete the oldest pre-migration backups over ``keep``.

    Sorts by ``(version, timestamp)`` as integers, not filename text --
    once schema v10 exists, ``stats-pre-v10-...`` sorts lexicographically
    before ``stats-pre-v9-...``, which would prune the newest backups
    first. Only files matching the exact ``stats-pre-v<N>-<timestamp>.db``
    shape are ever considered, so an unrelated file a user drops in the
    same directory is never touched.
    """
    backups = []
    for path in backups_dir.glob("stats-pre-v*.db"):
        match = _BACKUP_RE.match(path.name)
        if match:
            backups.append((int(match.group(1)), match.group(2), path))
    backups.sort(key=lambda item: (item[0], item[1]))
    for _version, _timestamp, stale in backups[: max(0, len(backups) - keep)]:
        stale.unlink()


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Yield each complete SQL statement from a migration file.

    ``executescript()`` auto-commits any surrounding transaction, which
    would release our ``BEGIN IMMEDIATE`` lock mid-migration and re-open
    the very race we are guarding against. Instead, split the file into
    individual statements with ``sqlite3.complete_statement`` and execute
    each one inside the same explicit transaction.
    """
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    tail = pending.strip()
    if tail:
        yield tail


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
        # A best-effort pre-lock read: just to decide whether a backup is
        # worth taking before anything is touched. The version that
        # actually governs which migrations apply is re-read below, inside
        # the lock, since a concurrent process may advance it in between.
        current_before_lock = _current_version(conn)
        if current_before_lock > 0 and any(
            m[0] > current_before_lock for m in migrations
        ):
            _backup(db_path, current_before_lock)

        # Foreign keys must be off for the whole migration: a table rebuild
        # (create-new -> copy -> drop-old -> rename-new) relies on the
        # DROP's implicit delete not cascading into child tables, and
        # PRAGMA foreign_keys cannot change inside a transaction -- so this
        # has to happen before BEGIN IMMEDIATE. See the module docstring.
        conn.execute("PRAGMA foreign_keys=OFF")

        # Take the write lock up front. With ``uvicorn --workers N`` (or two
        # containers / a restart race), every process runs migrations on
        # startup; ``BEGIN IMMEDIATE`` makes the read-then-apply atomic so a
        # second process blocks here (up to ``busy_timeout``) until the first
        # commits, then reads ``current`` already advanced and applies nothing.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _current_version(conn)

            if current > latest_known:
                raise DowngradeError(
                    f"Database schema is at version {current}, but this build only "
                    f"knows migrations up to version {latest_known}. Refusing to "
                    "start against a database newer than the code."
                )

            pending = [m for m in migrations if m[0] > current]
            for version, name, path in pending:
                sql = path.read_text(encoding="utf-8")
                for statement in _iter_sql_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, strftime('%s', 'now'))",
                    (version, name),
                )
                logger.info("Applied migration %04d_%s", version, name)

            if pending:
                # Foreign keys were off for the whole transaction, so
                # nothing enforced them while the migrations ran -- this is
                # what actually catches a rebuild that got the table order
                # wrong (including the "rename the old table out of the
                # way first" variant), instead of committing dangling
                # references.
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    tables = sorted({row[0] for row in violations})
                    raise MigrationError(
                        "Migration left dangling foreign key references in: "
                        + ", ".join(tables)
                    )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
