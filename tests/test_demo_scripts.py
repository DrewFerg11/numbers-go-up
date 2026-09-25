"""Offline smoke tests for scripts/demo/{seed,export}.py (#160).

Uses a tiny ``days`` window (not the full :data:`seed.DAYS_OF_HISTORY`) so
a full seed-and-export round trip stays fast in CI.
"""

from __future__ import annotations

import asyncio
import sqlite3

from scripts.demo import export, seed

DAYS = 5


def test_seed_produces_an_up_to_date_migrated_database(tmp_path):
    db_path = tmp_path / "seed.db"

    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    from numbers_go_up import migrate

    # run_migrations is idempotent -- calling it again on an
    # already-current database must not raise or change anything.
    migrate.run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    series = conn.execute("SELECT plugin_name, active FROM metric_series").fetchall()
    conn.close()

    by_plugin = {}
    for plugin_name, active in series:
        by_plugin.setdefault(plugin_name, set()).add(bool(active))

    # The retired plugin's series are all inactive; every other plugin's
    # are all active -- retirement only ever touched the one plugin.
    assert by_plugin[seed.RETIRED_PLUGIN] == {False}
    for plugin_name, active_flags in by_plugin.items():
        if plugin_name != seed.RETIRED_PLUGIN:
            assert active_flags == {True}, plugin_name


def test_seed_gives_the_flaky_plugin_only_failed_runs(tmp_path):
    db_path = tmp_path / "seed.db"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    conn = sqlite3.connect(db_path)
    statuses = {
        row[0]
        for row in conn.execute(
            "SELECT status FROM plugin_runs WHERE plugin_name = ?",
            (seed.FLAKY_PLUGIN,),
        )
    }
    conn.close()

    # No successful run at all -- storage.last_ok_runs() has no entry for
    # this plugin, so queries.is_stale() reads every one of its metrics as
    # stale regardless of interval.
    assert statuses == {"error"}


def test_seed_covers_both_kinds(tmp_path):
    db_path = tmp_path / "seed.db"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    conn = sqlite3.connect(db_path)
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM metric_series")}
    conn.close()

    assert kinds == {"gauge", "cumulative"}


def test_export_produces_every_range_and_no_third_party_urls(tmp_path):
    db_path = tmp_path / "seed.db"
    output = tmp_path / "dist"
    seed.seed(db_path, now=1_800_000_000, days=DAYS)

    asyncio.run(export.export(db_path, output))

    from numbers_go_up.queries import VALID_RANGES

    assert (output / "index.html").exists()
    for range_key in VALID_RANGES:
        assert (output / "api" / f"overview-{range_key}.json").exists()

    metric_dirs = sorted(p.name for p in (output / "m").iterdir())
    assert "counter.orders" in metric_dirs
    # The retired plugin's series is inactive but its history is kept
    # (#133) -- /m/<key> still resolves for it.
    assert "legacy.widget_count" in metric_dirs

    for metric_dir in metric_dirs:
        for range_key in VALID_RANGES:
            assert (output / "api" / f"history-{metric_dir}-{range_key}.json").exists()

    index_html = (output / "index.html").read_text()
    assert "cdn." not in index_html
    detail_html = (output / "m" / "counter.orders" / "index.html").read_text()
    assert "cdn." not in detail_html
    assert 'href="static/' in index_html or "href=\"/static/" not in index_html

    assert not (output / "static" / "img" / "icon-512.png").exists()
    assert (output / "static" / "css" / "dashboard.css").exists()
