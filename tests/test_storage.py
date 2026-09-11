import sqlite3

import pytest

from numbers_go_up import migrate, storage


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return path


def _series_row(db_path, series_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT metric_key, plugin_name, kind, label, unit, icon, "
            "first_seen, last_seen, last_value FROM metric_series WHERE id = ?",
            (series_id,),
        ).fetchone()
    finally:
        conn.close()


def _sample_count(db_path, series_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM samples WHERE series_id = ?", (series_id,)
        ).fetchone()[0]
    finally:
        conn.close()


class TestGetOrCreateSeries:
    def test_creates_a_new_series_and_records_first_seen(self, db_path):
        series_id = storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "cumulative",
            "Count",
            "things",
            "mdi:counter",
            1000,
        )

        row = _series_row(db_path, series_id)
        assert row[0] == "demo.thing.count"
        assert row[1] == "demo"
        assert row[2] == "cumulative"
        assert row[6] == 1000  # first_seen

    def test_returns_the_same_series_id_across_calls(self, db_path):
        first = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        second = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 2000
        )

        assert first == second

    def test_returns_the_same_series_id_across_a_simulated_restart(self, db_path):
        first = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        # A fresh call with no shared state simulates a process restart,
        # since get_or_create_series never caches anything in memory.
        second = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 3000
        )

        assert first == second

    def test_updates_label_unit_and_icon_on_sight(self, db_path):
        series_id = storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "cumulative",
            "Old",
            "old",
            "mdi:old",
            1000,
        )
        storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "cumulative",
            "New",
            "new",
            "mdi:new",
            2000,
        )

        row = _series_row(db_path, series_id)
        assert row[3] == "New"
        assert row[4] == "new"
        assert row[5] == "mdi:new"

    def test_updates_plugin_name_on_sight(self, db_path):
        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        storage.get_or_create_series(
            db_path, "demo.thing.count", "renamed", "cumulative", "Count", "", "", 2000
        )

        row = _series_row(db_path, series_id)
        assert row[1] == "renamed"

    def test_kind_mismatch_keeps_the_stored_kind(self, db_path, caplog):
        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        with caplog.at_level("WARNING"):
            storage.get_or_create_series(
                db_path, "demo.thing.count", "demo", "gauge", "Count", "", "", 2000
            )

        row = _series_row(db_path, series_id)
        assert row[2] == "cumulative"
        assert "kind" in caplog.text.lower()

    def test_rejects_kind_outside_gauge_or_cumulative(self, db_path):
        with pytest.raises(ValueError):
            storage.get_or_create_series(
                db_path, "demo.thing.count", "demo", "counter", "Count", "", "", 1000
            )


class TestRecordSample:
    def _series(self, db_path):
        return storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )

    def test_writes_on_first_value(self, db_path):
        series_id = self._series(db_path)

        wrote = storage.record_sample(
            db_path, series_id, 1000, 5, heartbeat_seconds=86400
        )

        assert wrote is True
        assert _sample_count(db_path, series_id) == 1

    def test_skips_an_identical_repeat(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        wrote = storage.record_sample(
            db_path, series_id, 1100, 5, heartbeat_seconds=86400
        )

        assert wrote is False
        assert _sample_count(db_path, series_id) == 1

    def test_writes_again_on_change(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        wrote = storage.record_sample(
            db_path, series_id, 1100, 6, heartbeat_seconds=86400
        )

        assert wrote is True
        assert _sample_count(db_path, series_id) == 2

    def test_writes_on_heartbeat_expiry(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        wrote = storage.record_sample(
            db_path, series_id, 1000 + 86400, 5, heartbeat_seconds=86400
        )

        assert wrote is True
        assert _sample_count(db_path, series_id) == 2

    def test_skipped_poll_does_not_reset_the_heartbeat_clock(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        # An unchanged value below the heartbeat threshold must not bump
        # last_seen, or the heartbeat would never fire.
        storage.record_sample(
            db_path, series_id, 1000 + 100, 5, heartbeat_seconds=86400
        )
        row = _series_row(db_path, series_id)
        assert row[7] == 1000  # last_seen unchanged

        wrote = storage.record_sample(
            db_path, series_id, 1000 + 86400, 5, heartbeat_seconds=86400
        )
        assert wrote is True

    def test_updates_last_value_and_last_seen_on_write(self, db_path):
        series_id = self._series(db_path)

        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        row = _series_row(db_path, series_id)
        assert row[7] == 1000  # last_seen
        assert row[8] == 5  # last_value

    def test_same_second_write_overwrites_via_last_write_wins(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE metric_series SET last_value = NULL WHERE id = ?", (series_id,)
            )
            conn.commit()
        finally:
            conn.close()

        # Forcing last_value back to NULL simulates two polls racing to
        # write the same (series_id, ts): the second write should win.
        wrote = storage.record_sample(
            db_path, series_id, 1000, 7, heartbeat_seconds=86400
        )

        assert wrote is True
        assert _sample_count(db_path, series_id) == 1
        conn = sqlite3.connect(str(db_path))
        try:
            value = conn.execute(
                "SELECT value FROM samples WHERE series_id = ? AND ts = ?",
                (series_id, 1000),
            ).fetchone()[0]
        finally:
            conn.close()
        assert value == 7

    def test_raises_for_unknown_series(self, db_path):
        with pytest.raises(ValueError):
            storage.record_sample(db_path, 999, 1000, 5, heartbeat_seconds=86400)


def _run_row(db_path, run_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT plugin_name, started_at, finished_at, status, error, "
            "duration_ms, samples_written FROM plugin_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()


class TestRunLedger:
    def test_start_run_returns_a_run_id_and_records_started_at(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)

        row = _run_row(db_path, run_id)
        assert row[0] == "demo"
        assert row[1] == 1000

    def test_in_progress_run_reads_as_a_failure(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)

        row = _run_row(db_path, run_id)
        assert row[3] == "error"

    def test_finish_run_records_outcome(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)

        storage.finish_run(
            db_path, run_id, "ok", None, samples_written=3, finished_at=1002
        )

        row = _run_row(db_path, run_id)
        assert row[2] == 1002  # finished_at
        assert row[3] == "ok"
        assert row[4] is None
        assert row[5] == 2000  # duration_ms
        assert row[6] == 3

    def test_error_is_capped_at_last_500_characters(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)
        traceback_text = ("x" * 600) + "ValueError: boom"

        storage.finish_run(
            db_path,
            run_id,
            "error",
            traceback_text,
            samples_written=0,
            finished_at=1001,
        )

        row = _run_row(db_path, run_id)
        assert len(row[4]) == 500
        assert row[4].endswith("ValueError: boom")

    def test_status_outside_ok_or_error_is_rejected(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)

        with pytest.raises(ValueError):
            storage.finish_run(
                db_path, run_id, "running", None, samples_written=0, finished_at=1001
            )

    def test_finish_run_raises_for_unknown_run(self, db_path):
        with pytest.raises(ValueError):
            storage.finish_run(
                db_path, 999, "ok", None, samples_written=0, finished_at=1001
            )


class TestPrunePluginRuns:
    def test_deletes_only_rows_past_the_cutoff(self, db_path):
        now = 30 * 86400
        old_run = storage.start_run(db_path, "demo", now - 31 * 86400)
        recent_run = storage.start_run(db_path, "demo", now - 1 * 86400)

        deleted = storage.prune_plugin_runs(db_path, days=30, now=now)

        assert deleted == 1
        assert _run_row(db_path, old_run) is None
        assert _run_row(db_path, recent_run) is not None

    def test_returns_the_number_of_rows_deleted(self, db_path):
        now = 30 * 86400
        for _ in range(3):
            storage.start_run(db_path, "demo", now - 40 * 86400)

        deleted = storage.prune_plugin_runs(db_path, days=30, now=now)

        assert deleted == 3

    def test_never_touches_samples_series_or_state(self, db_path):
        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        now = 30 * 86400
        storage.start_run(db_path, "demo", now - 40 * 86400)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("INSERT INTO state (key, value) VALUES ('k', 'v')")
            conn.commit()
        finally:
            conn.close()

        storage.prune_plugin_runs(db_path, days=30, now=now)

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM metric_series").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM state").fetchone()[0] == 1
        finally:
            conn.close()
