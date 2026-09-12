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

    def test_boundary_row_exactly_days_old_is_kept(self, db_path):
        now = 30 * 86400
        boundary_run = storage.start_run(db_path, "demo", now - 30 * 86400)

        deleted = storage.prune_plugin_runs(db_path, days=30, now=now)

        assert deleted == 0
        assert _run_row(db_path, boundary_run) is not None

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


class TestValueAsOf:
    def _series(self, db_path):
        return storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )

    def test_returns_last_value_carried_forward_across_a_gap(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_id, 2000, 9, heartbeat_seconds=86400)

        # No sample at 1500; the value as of 1000 must carry forward.
        assert storage.value_as_of(db_path, series_id, 1500) == 5

    def test_before_first_sample_returns_none(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        assert storage.value_as_of(db_path, series_id, 500) is None

    def test_at_exactly_a_samples_ts_returns_that_sample(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_id, 2000, 9, heartbeat_seconds=86400)

        assert storage.value_as_of(db_path, series_id, 2000) == 9

    def test_unknown_series_returns_none(self, db_path):
        assert storage.value_as_of(db_path, 999, 1000) is None

    def test_10000_samples_value_as_of_is_sub_millisecond(self, db_path):
        import time

        series_id = self._series(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executemany(
                "INSERT INTO samples (series_id, ts, value) VALUES (?, ?, ?)",
                [(series_id, ts, float(ts)) for ts in range(1000, 1000 + 10_000)],
            )
            conn.commit()
        finally:
            conn.close()

        iterations = 200
        started = time.perf_counter()
        for _ in range(iterations):
            storage.value_as_of(db_path, series_id, 5500)
        elapsed = time.perf_counter() - started

        # Measured figure in the issue is 0.018 ms/call; 1 ms leaves ~50x
        # headroom so this doesn't flake on a slow CI runner.
        assert (elapsed / iterations) < 0.001


class TestLatest:
    def test_returns_newest_value_for_every_requested_series_in_one_call(self, db_path):
        series_a = storage.get_or_create_series(
            db_path, "demo.a.count", "demo", "cumulative", "A", "", "", 1000
        )
        series_b = storage.get_or_create_series(
            db_path, "demo.b.count", "demo", "cumulative", "B", "", "", 1000
        )
        storage.record_sample(db_path, series_a, 1000, 1, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_a, 2000, 2, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_b, 1000, 100, heartbeat_seconds=86400)

        result = storage.latest(db_path, [series_a, series_b])

        assert result == {series_a: 2, series_b: 100}

    def test_omits_series_with_no_samples(self, db_path):
        series_a = storage.get_or_create_series(
            db_path, "demo.a.count", "demo", "cumulative", "A", "", "", 1000
        )
        series_b = storage.get_or_create_series(
            db_path, "demo.b.count", "demo", "cumulative", "B", "", "", 1000
        )
        storage.record_sample(db_path, series_a, 1000, 1, heartbeat_seconds=86400)

        result = storage.latest(db_path, [series_a, series_b])

        assert result == {series_a: 1}

    def test_empty_input_returns_empty_dict(self, db_path):
        assert storage.latest(db_path, []) == {}


class TestHistory:
    def _series(self, db_path):
        return storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )

    def test_unknown_series_returns_empty(self, db_path):
        assert storage.history(db_path, 999, 0, 10_000) == []

    def test_includes_carried_forward_anchor_when_series_is_flat_across_the_window(
        self, db_path
    ):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        # No samples inside [2000, 3000] under store-on-change; the anchor
        # carries the flat value forward instead of returning nothing.
        points = storage.history(db_path, series_id, 2000, 3000)

        assert points == [(2000, 5)]

    def test_includes_literal_samples_after_the_anchor(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_id, 2500, 9, heartbeat_seconds=86400)

        points = storage.history(db_path, series_id, 2000, 3000)

        assert points == [(2000, 5), (2500, 9)]

    def test_no_anchor_when_nothing_exists_before_start(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 5000, 5, heartbeat_seconds=86400)

        points = storage.history(db_path, series_id, 0, 1000)

        assert points == []
