import sqlite3
from pathlib import Path

import pytest

from numbers_go_up import migrate, storage

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "migrations"


def _schema(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(row[0] for row in rows)


def test_migration_applied_to_empty_db_creates_every_table(tmp_path):
    db_path = tmp_path / "stats.db"

    migrate.run_migrations(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert tables == {
        "metric_series",
        "samples",
        "plugin_runs",
        "state",
        "schema_migrations",
    }


def test_running_migrations_twice_is_a_noop(tmp_path):
    db_path = tmp_path / "stats.db"

    migrate.run_migrations(db_path)
    schema_after_first = _schema(db_path)
    migrate.run_migrations(db_path)
    schema_after_second = _schema(db_path)

    assert schema_after_first == schema_after_second


def test_throwaway_migration_applies_and_preserves_seeded_v1_data(tmp_path):
    db_path = tmp_path / "stats.db"

    migrate.run_migrations(db_path, migrations_dir=FIXTURES_DIR)

    conn = storage.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO metric_series (metric_key, plugin_name, first_seen) "
            "VALUES ('demo.count', 'demo', 1000)"
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run against the same fixtures dir; 0002_test.sql is already applied.
    migrate.run_migrations(db_path, migrations_dir=FIXTURES_DIR)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        row = conn.execute(
            "SELECT metric_key FROM metric_series WHERE metric_key='demo.count'"
        ).fetchone()
    finally:
        conn.close()

    assert "test_marker" in tables
    assert row == ("demo.count",)


def test_downgrade_guard_refuses_to_start_on_newer_db(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (99, 'from_the_future', strftime('%s', 'now'))"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(migrate.DowngradeError):
        migrate.run_migrations(db_path)


def test_pre_migration_backup_exists_and_is_a_valid_sqlite_db(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)

    migrate.run_migrations(db_path, migrations_dir=FIXTURES_DIR)

    backups = list((tmp_path / "backups").glob("stats-pre-v1-*.db"))
    assert len(backups) == 1

    conn = sqlite3.connect(str(backups[0]))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "metric_series" in tables


def test_backup_is_skipped_on_a_brand_new_database(tmp_path):
    db_path = tmp_path / "stats.db"

    migrate.run_migrations(db_path)

    assert not (tmp_path / "backups").exists()


def test_only_five_newest_backups_are_kept(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    for i in range(7):
        (backups_dir / f"stats-pre-v1-2020010{i}T000000.db").write_bytes(b"")

    migrate._prune_backups(backups_dir, keep=5)

    remaining = sorted(backups_dir.glob("stats-pre-v1-*.db"))
    assert len(remaining) == 5
    assert remaining[-1].name == "stats-pre-v1-20200106T000000.db"


def test_failed_migration_leaves_no_partial_schema_or_ledger_row(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)

    broken_dir = tmp_path / "broken_migrations"
    broken_dir.mkdir()
    (broken_dir / "0001_initial.sql").write_text(
        (FIXTURES_DIR / "0001_initial.sql").read_text()
    )
    (broken_dir / "0002_broken.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER PRIMARY KEY);\nTHIS IS NOT VALID SQL;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        migrate.run_migrations(db_path, migrations_dir=broken_dir)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
            0
        ]
    finally:
        conn.close()

    assert "half_applied" not in tables
    assert version == 1


def test_every_connection_reports_all_five_pragmas_set(tmp_path):
    db_path = tmp_path / "stats.db"
    conn = storage.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
    finally:
        conn.close()


def test_schema_matches_database_md_exactly(tmp_path):
    db_path = tmp_path / "stats.db"
    migrate.run_migrations(db_path)

    committed_sql = (
        Path(__file__).resolve().parent.parent
        / "numbers_go_up"
        / "migrations"
        / "0001_initial.sql"
    ).read_text()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND type IN "
            "('table', 'index') AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        statement = row[0]
        assert statement.strip() in committed_sql
