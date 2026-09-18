import json
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

    def test_attrs_are_merged_not_replaced_wholesale(self, db_path):
        series_id = storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "gauge",
            "Count",
            "",
            "",
            1000,
            attrs={"a": 1},
        )
        storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "gauge",
            "Count",
            "",
            "",
            2000,
            attrs={"b": 2},
        )

        conn = sqlite3.connect(str(db_path))
        try:
            stored = conn.execute(
                "SELECT attrs FROM metric_series WHERE id = ?", (series_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert json.loads(stored) == {"a": 1, "b": 2}

    def test_attrs_none_leaves_stored_attrs_untouched(self, db_path):
        series_id = storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "gauge",
            "Count",
            "",
            "",
            1000,
            attrs={"a": 1},
        )
        storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "gauge", "Count", "", "", 2000
        )

        conn = sqlite3.connect(str(db_path))
        try:
            stored = conn.execute(
                "SELECT attrs FROM metric_series WHERE id = ?", (series_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert json.loads(stored) == {"a": 1}

    def test_non_object_stored_attrs_are_replaced_instead_of_crashing(self, db_path):
        # A hand-edited row (or a future buggy writer) could leave
        # valid-but-non-object JSON in attrs ("[1,2]", "3"). merging a new
        # dict into that must not raise AttributeError and abort the
        # caller (run_plugin_once's poll loop) -- it should just treat the
        # corrupt value as empty and start fresh.
        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "gauge", "Count", "", "", 1000
        )
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE metric_series SET attrs = ? WHERE id = ?", ("[1,2]", series_id)
            )
            conn.commit()
        finally:
            conn.close()

        storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "gauge",
            "Count",
            "",
            "",
            2000,
            attrs={"a": 1},
        )

        conn = sqlite3.connect(str(db_path))
        try:
            stored = conn.execute(
                "SELECT attrs FROM metric_series WHERE id = ?", (series_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert json.loads(stored) == {"a": 1}


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


def _set_active(db_path, series_id, active):
    # Stand-in for the first future writer of the flag: nothing in the app
    # sets `active` yet, so deactivation is simulated with raw SQL exactly
    # the way that writer would leave the row.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE metric_series SET active = ? WHERE id = ?", (active, series_id)
        )
        conn.commit()
    finally:
        conn.close()


class TestLatestFinishedRun:
    def test_returns_the_newest_finished_run_whatever_its_outcome(self, db_path):
        first = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, first, "ok", None, samples_written=1, finished_at=1001
        )
        second = storage.start_run(db_path, "demo", 2000)
        storage.finish_run(
            db_path, second, "error", "boom", samples_written=0, finished_at=2001
        )

        row = storage.latest_finished_run(db_path, "demo")

        assert row["status"] == "error"
        assert row["started_at"] == 2000
        assert row["finished_at"] == 2001
        assert row["error"] == "boom"

    def test_skips_a_run_still_in_flight(self, db_path):
        failed = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, failed, "error", "boom", samples_written=0, finished_at=1001
        )
        storage.start_run(db_path, "demo", 2000)  # never finished: in flight

        row = storage.latest_finished_run(db_path, "demo")

        assert row["status"] == "error"
        assert row["finished_at"] == 1001

    def test_only_run_in_flight_returns_none(self, db_path):
        storage.start_run(db_path, "demo", 1000)

        assert storage.latest_finished_run(db_path, "demo") is None

    def test_never_ran_returns_none(self, db_path):
        assert storage.latest_finished_run(db_path, "demo") is None

    def test_ignores_other_plugins(self, db_path):
        other = storage.start_run(db_path, "other", 1000)
        storage.finish_run(
            db_path, other, "error", "boom", samples_written=0, finished_at=1001
        )

        assert storage.latest_finished_run(db_path, "demo") is None


class TestActiveSeriesFiltering:
    def _series(self, db_path, metric_key):
        return storage.get_or_create_series(
            db_path, metric_key, "demo", "cumulative", "Count", "", "", 1000
        )

    def test_list_series_skips_deactivated_series(self, db_path):
        self._series(db_path, "demo.live.count")
        retired = self._series(db_path, "demo.retired.count")
        _set_active(db_path, retired, 0)

        keys = [row["metric_key"] for row in storage.list_series(db_path)]

        assert keys == ["demo.live.count"]

    def test_get_series_by_key_returns_none_for_deactivated_series(self, db_path):
        retired = self._series(db_path, "demo.retired.count")
        _set_active(db_path, retired, 0)

        assert storage.get_series_by_key(db_path, "demo.retired.count") is None


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


class TestRangeStatsConn:
    def _series(self, db_path):
        return storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )

    def test_matches_range_stats_against_a_shared_connection(self, db_path):
        # range_stats_conn is the one build_overview actually calls (once
        # per series, all against one shared connection, to avoid paying
        # connect()'s five PRAGMA statements per series on every request);
        # range_stats is a thin single-series wrapper around it. The two
        # must agree.
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=100)
        storage.record_sample(db_path, series_id, 1500, 9, heartbeat_seconds=100)

        via_wrapper = storage.range_stats(db_path, series_id, 0, 2000)

        conn = storage.connect(db_path)
        try:
            via_conn = storage.range_stats_conn(conn, series_id, 0, 2000)
        finally:
            conn.close()

        assert via_conn == via_wrapper

    def test_one_connection_serves_multiple_series(self, db_path):
        # The actual point of range_stats_conn: the same open connection
        # can be reused across series without reopening it.
        first_id = storage.get_or_create_series(
            db_path, "demo.a", "demo", "cumulative", "A", "", "", 1000
        )
        second_id = storage.get_or_create_series(
            db_path, "demo.b", "demo", "cumulative", "B", "", "", 1000
        )
        storage.record_sample(db_path, first_id, 1000, 1, heartbeat_seconds=100)
        storage.record_sample(db_path, second_id, 1000, 2, heartbeat_seconds=100)

        conn = storage.connect(db_path)
        try:
            first = storage.range_stats_conn(conn, first_id, 0, 2000)
            second = storage.range_stats_conn(conn, second_id, 0, 2000)
        finally:
            conn.close()

        assert first["open"] == 1
        assert second["open"] == 2


class TestRangeStatsBulkConn:
    def _series(self, db_path, key):
        return storage.get_or_create_series(
            db_path, key, "demo", "cumulative", "Label", "", "", 1000
        )

    def test_matches_range_stats_conn_for_every_series_shape(self, db_path):
        # One series of each shape range_stats_conn has to special-case:
        # anchored with points after start, anchored and flat (no points
        # after start), no anchor (younger than the range, falls back to
        # its own first sample), a single-sample series, and one with no
        # samples at all.
        anchored = self._series(db_path, "demo.anchored")
        flat = self._series(db_path, "demo.flat")
        younger = self._series(db_path, "demo.younger")
        single = self._series(db_path, "demo.single")
        empty = self._series(db_path, "demo.empty")

        storage.record_sample(db_path, anchored, 0, 10, heartbeat_seconds=1)
        storage.record_sample(db_path, anchored, 150, 30, heartbeat_seconds=1)
        storage.record_sample(db_path, anchored, 250, 40, heartbeat_seconds=1)

        storage.record_sample(db_path, flat, 0, 100, heartbeat_seconds=100000)

        storage.record_sample(db_path, younger, 120, 5, heartbeat_seconds=1)
        storage.record_sample(db_path, younger, 300, 25, heartbeat_seconds=1)

        storage.record_sample(db_path, single, 130, 7, heartbeat_seconds=100000)

        start, end = 100, 400
        series_ids = [anchored, flat, younger, single, empty]

        conn = storage.connect(db_path)
        try:
            expected = {
                sid: storage.range_stats_conn(conn, sid, start, end)
                for sid in series_ids
            }
            actual = storage.range_stats_bulk_conn(
                conn, dict.fromkeys(series_ids, start), end
            )
        finally:
            conn.close()

        assert actual == expected

    def test_groups_by_distinct_start(self, db_path):
        # The ALL range gives each series its own start (its first_seen) --
        # distinct starts must still match range_stats_conn per series,
        # not just when every series shares one start.
        first = self._series(db_path, "demo.first")
        second = self._series(db_path, "demo.second")
        storage.record_sample(db_path, first, 0, 10, heartbeat_seconds=1)
        storage.record_sample(db_path, first, 150, 30, heartbeat_seconds=1)
        storage.record_sample(db_path, second, 400, 100, heartbeat_seconds=1)
        storage.record_sample(db_path, second, 450, 110, heartbeat_seconds=1)

        starts = {first: 0, second: 400}
        end = 500

        conn = storage.connect(db_path)
        try:
            expected = {
                sid: storage.range_stats_conn(conn, sid, starts[sid], end)
                for sid in starts
            }
            actual = storage.range_stats_bulk_conn(conn, starts, end)
        finally:
            conn.close()

        assert actual == expected

    def test_empty_input_returns_empty(self, db_path):
        conn = storage.connect(db_path)
        try:
            assert storage.range_stats_bulk_conn(conn, {}, 1000) == {}
        finally:
            conn.close()

    def test_anchor_and_firsts_queries_are_index_seeks_not_temp_btrees(self, db_path):
        # Regression pin for a real perf bug caught in review: a
        # ROW_NUMBER()/PARTITION BY translation of "newest/oldest sample
        # per series" reads naturally, but SQLite has to materialize every
        # matching row into a temp B-tree to number them before filtering
        # to rn=1/rn=n -- turning an index seek into a sort, exactly where
        # this function's whole point is to be fast. The MAX(ts)/MIN(ts)
        # GROUP BY form it uses instead must compile to a plain index
        # search on every query this function issues.
        anchored = self._series(db_path, "demo.anchored")
        younger = self._series(db_path, "demo.younger")
        storage.record_sample(db_path, anchored, 0, 10, heartbeat_seconds=1)
        storage.record_sample(db_path, younger, 500, 20, heartbeat_seconds=1)

        conn = storage.connect(db_path)
        try:
            storage.range_stats_bulk_conn(conn, {anchored: 100, younger: 100}, 1000)

            plans = "\n".join(
                " ".join(str(cell) for cell in row)
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT series_id, MAX(ts), value FROM samples "
                    "WHERE series_id IN (?, ?) AND ts <= ? GROUP BY series_id",
                    (anchored, younger, 100),
                )
            )
        finally:
            conn.close()

        assert "SEARCH" in plans
        assert "TEMP B-TREE" not in plans.upper()


class TestRecentChanges:
    def _series(self, db_path, key, plugin="demo"):
        return storage.get_or_create_series(
            db_path, key, plugin, "cumulative", "Label", "", "", 1000
        )

    def test_excludes_zero_change_heartbeats(self, db_path):
        series_id = self._series(db_path, "demo.a")
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=100)
        storage.record_sample(db_path, series_id, 1100, 5, heartbeat_seconds=100)
        storage.record_sample(db_path, series_id, 1200, 8, heartbeat_seconds=100)

        changes = storage.recent_changes(db_path, 10)

        assert [(row["ts"], row["change"]) for row in changes] == [(1200, 3)]

    def test_excludes_a_series_very_first_sample(self, db_path):
        series_id = self._series(db_path, "demo.a")
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=100)

        assert storage.recent_changes(db_path, 10) == []

    def test_orders_newest_first_and_respects_limit(self, db_path):
        series_id = self._series(db_path, "demo.a")
        storage.record_sample(db_path, series_id, 1000, 1, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 1001, 2, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 1002, 3, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 1003, 4, heartbeat_seconds=1)

        changes = storage.recent_changes(db_path, 2)

        assert [row["ts"] for row in changes] == [1003, 1002]

    def test_excludes_inactive_series(self, db_path):
        series_id = self._series(db_path, "demo.a")
        storage.record_sample(db_path, series_id, 1000, 1, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 1001, 5, heartbeat_seconds=1)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,)
            )
            conn.commit()
        finally:
            conn.close()

        assert storage.recent_changes(db_path, 10) == []


class TestRecordedChanges:
    def _series(self, db_path, key="demo.a"):
        return storage.get_or_create_series(
            db_path, key, "demo", "cumulative", "Label", "", "", 1000
        )

    def test_newest_first_with_signed_change(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 1001, 8, heartbeat_seconds=1)

        rows = storage.recorded_changes(db_path, series_id, 0, 2000, 10)

        assert rows == [
            {"ts": 1001, "value": 8, "change": 3},
            {"ts": 1000, "value": 5, "change": 0},
        ]

    def test_heartbeat_with_zero_change_is_kept(self, db_path):
        series_id = self._series(db_path)
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=1)
        storage.record_sample(db_path, series_id, 2000, 5, heartbeat_seconds=1)

        rows = storage.recorded_changes(db_path, series_id, 0, 3000, 10)

        assert rows[0] == {"ts": 2000, "value": 5, "change": 0}

    def test_respects_range_and_limit(self, db_path):
        series_id = self._series(db_path)
        for ts, value in [(1000, 1), (1001, 2), (1002, 3), (1003, 4)]:
            storage.record_sample(db_path, series_id, ts, value, heartbeat_seconds=1)

        rows = storage.recorded_changes(db_path, series_id, 1000, 1003, 2)

        assert [row["ts"] for row in rows] == [1003, 1002]


class TestGetAnySeriesByKey:
    def test_returns_inactive_series(self, db_path):
        series_id = storage.get_or_create_series(
            db_path, "demo.a", "demo", "cumulative", "Label", "", "", 1000
        )
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,)
            )
            conn.commit()
        finally:
            conn.close()

        row = storage.get_any_series_by_key(db_path, "demo.a")

        assert row["active"] == 0

    def test_unknown_key_returns_none(self, db_path):
        assert storage.get_any_series_by_key(db_path, "nope") is None
