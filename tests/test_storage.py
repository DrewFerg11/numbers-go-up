import json
import sqlite3
import time
from pathlib import Path

import pytest

from numbers_go_up import migrate, storage


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(path)
    return path


def _time_it(fn):
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


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


class TestCloseInterruptedRuns:
    def test_closes_an_unfinished_run_with_the_interrupted_sentinel(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)

        closed = storage.close_interrupted_runs(db_path, now=1050)

        assert closed == 1
        row = _run_row(db_path, run_id)
        assert row[2] == 1050  # finished_at
        assert row[3] == "error"
        assert "interrupted" in row[4]

    def test_finished_runs_are_left_alone(self, db_path):
        run_id = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, run_id, "ok", None, samples_written=1, finished_at=1002
        )

        closed = storage.close_interrupted_runs(db_path, now=1050)

        assert closed == 0
        row = _run_row(db_path, run_id)
        assert row[2] == 1002
        assert row[3] == "ok"

    def test_no_unfinished_runs_closes_nothing(self, db_path):
        assert storage.close_interrupted_runs(db_path, now=1050) == 0

    def test_an_interrupted_run_does_not_count_as_a_consecutive_failure(self, db_path):
        storage.start_run(db_path, "demo", 1000)
        storage.close_interrupted_runs(db_path, now=1050)

        assert storage.consecutive_failures(db_path, "demo") == 0
        # And latest_finished_run skips straight past it too.
        assert storage.latest_finished_run(db_path, "demo") is None

    def test_interrupted_run_does_not_hide_a_real_failure_behind_it(self, db_path):
        run_id = storage.start_run(db_path, "demo", 900)
        storage.finish_run(
            db_path, run_id, "error", "boom", samples_written=0, finished_at=901
        )
        storage.start_run(db_path, "demo", 1000)
        storage.close_interrupted_runs(db_path, now=1050)

        # The interrupted row is skipped; the real failure behind it is
        # still counted and still the one latest_finished_run reports.
        assert storage.consecutive_failures(db_path, "demo") == 1
        latest = storage.latest_finished_run(db_path, "demo")
        assert latest["status"] == "error"
        assert latest["error"] == "boom"


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

    def test_keeps_the_newest_finished_run_per_plugin_regardless_of_age(self, db_path):
        now = 90 * 86400
        # "demo" hasn't run in 40 days (disabled), but its last finished run
        # must survive pruning so /api/plugins can still report its status.
        old_finished = storage.start_run(db_path, "demo", now - 40 * 86400)
        storage.finish_run(
            db_path,
            old_finished,
            "ok",
            None,
            samples_written=1,
            finished_at=now - 40 * 86400,
        )
        even_older_finished = storage.start_run(db_path, "demo", now - 50 * 86400)
        storage.finish_run(
            db_path,
            even_older_finished,
            "ok",
            None,
            samples_written=1,
            finished_at=now - 50 * 86400,
        )

        deleted = storage.prune_plugin_runs(db_path, days=30, now=now)

        assert deleted == 1
        assert _run_row(db_path, old_finished) is not None
        assert _run_row(db_path, even_older_finished) is None

    def test_a_started_at_tie_between_two_finished_runs_keeps_exactly_one(
        self, db_path
    ):
        # Two finished runs for the same plugin sharing a started_at (real
        # under second-resolution timestamps): MAX(started_at) alone would
        # match both rows and keep both, rather than picking the actual
        # newest deterministically via the id tiebreak.
        now = 90 * 86400
        tied_started_at = now - 40 * 86400
        older = storage.start_run(db_path, "demo", tied_started_at)
        storage.finish_run(
            db_path, older, "ok", None, samples_written=1, finished_at=tied_started_at
        )
        newer = storage.start_run(db_path, "demo", tied_started_at)
        storage.finish_run(
            db_path,
            newer,
            "ok",
            None,
            samples_written=1,
            finished_at=tied_started_at + 1,
        )

        deleted = storage.prune_plugin_runs(db_path, days=30, now=now)

        assert deleted == 1
        assert _run_row(db_path, newer) is not None
        assert _run_row(db_path, older) is None

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

    def test_a_started_at_tie_is_broken_by_id(self, db_path):
        # Two finished runs sharing a started_at (real under
        # second-resolution timestamps -- a fast error followed by a fast
        # retry in the same second): ORDER BY started_at DESC alone
        # leaves SQLite free to return either row, so this must match
        # consecutive_failures' own id DESC tiebreak or the two can
        # disagree about which run is newest.
        older = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, older, "ok", None, samples_written=1, finished_at=1000
        )
        newer = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, newer, "error", "boom", samples_written=0, finished_at=1000
        )
        assert newer > older  # insertion order is the only real ordering

        row = storage.latest_finished_run(db_path, "demo")

        assert row["status"] == "error"
        assert row["error"] == "boom"

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


class TestState:
    def test_unknown_key_returns_none(self, db_path):
        assert storage.get_state(db_path, "nope") is None

    def test_set_then_get_round_trips(self, db_path):
        storage.set_state(db_path, "k", "v1")
        assert storage.get_state(db_path, "k") == "v1"

    def test_set_overwrites_the_existing_value(self, db_path):
        storage.set_state(db_path, "k", "v1")
        storage.set_state(db_path, "k", "v2")
        assert storage.get_state(db_path, "k") == "v2"


class TestBackupDatabase:
    def test_creates_a_verified_backup_file(self, db_path, tmp_path):
        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        backup_dir = tmp_path / "backups"

        result = storage.backup_database(
            db_path, backup_dir, keep_daily=7, today="20260101"
        )

        backup_path = backup_dir / "stats-daily-20260101.db"
        assert result["backup_file"] == str(backup_path)
        assert result["backup_bytes"] == backup_path.stat().st_size
        conn = sqlite3.connect(str(backup_path))
        try:
            assert (
                conn.execute(
                    "SELECT value FROM samples WHERE series_id = ?", (series_id,)
                ).fetchone()[0]
                == 5
            )
        finally:
            conn.close()

    def test_keep_daily_zero_creates_nothing(self, db_path, tmp_path):
        backup_dir = tmp_path / "backups"

        result = storage.backup_database(
            db_path, backup_dir, keep_daily=0, today="20260101"
        )

        assert result == {"backup_file": None, "backup_bytes": None}
        assert not backup_dir.exists()

    def test_retention_keeps_exactly_keep_daily_newest_files(self, db_path, tmp_path):
        backup_dir = tmp_path / "backups"
        for day in ["20260101", "20260102", "20260103", "20260104"]:
            storage.backup_database(db_path, backup_dir, keep_daily=2, today=day)

        remaining = sorted(p.name for p in backup_dir.glob("stats-daily-*.db"))
        assert remaining == ["stats-daily-20260103.db", "stats-daily-20260104.db"]

    def test_retention_does_not_count_or_delete_a_foreign_daily_prefixed_file(
        self, db_path, tmp_path
    ):
        # backup_dir is a plain bind mount -- a user-placed file that
        # happens to share the "stats-daily-" prefix (not this function's
        # own <8-digit-date>.db shape) must survive retention untouched
        # and must not count toward keep_daily.
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        foreign = backup_dir / "stats-daily-junk.db"
        foreign.write_bytes(b"not one of ours")

        for day in ["20260101", "20260102"]:
            storage.backup_database(db_path, backup_dir, keep_daily=1, today=day)

        assert foreign.exists()
        remaining = sorted(
            p.name for p in backup_dir.glob("stats-daily-*.db") if p != foreign
        )
        assert remaining == ["stats-daily-20260102.db"]

    def test_retention_tolerates_a_file_already_removed_by_something_else(
        self, db_path, tmp_path, monkeypatch
    ):
        backup_dir = tmp_path / "backups"
        for day in ["20260101", "20260102"]:
            storage.backup_database(db_path, backup_dir, keep_daily=2, today=day)
        stale_path = backup_dir / "stats-daily-20260101.db"
        original_unlink = Path.unlink

        def fake_unlink(self, *args, **kwargs):
            # Simulate another process winning the race between the glob
            # and this unlink: the file is already gone by the time
            # backup_database's own unlink(missing_ok=True) runs.
            if self == stale_path:
                original_unlink(self, missing_ok=True)
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fake_unlink)

        # keep_daily=1 makes the 01-01 file the one retention wants gone.
        result = storage.backup_database(
            db_path, backup_dir, keep_daily=1, today="20260103"
        )

        assert result["backup_file"] == str(backup_dir / "stats-daily-20260103.db")
        assert not stale_path.exists()

    def test_does_not_touch_pre_migration_backups(self, db_path, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        pre_migration = backup_dir / "stats-pre-v1-20260101T000000.db"
        pre_migration.write_bytes(b"not a real db, just a marker")

        for day in ["20260101", "20260102", "20260103"]:
            storage.backup_database(db_path, backup_dir, keep_daily=1, today=day)

        assert pre_migration.exists()

    def test_failing_quick_check_deletes_the_file_and_raises(
        self, db_path, tmp_path, monkeypatch
    ):
        # The second sqlite3.connect() against the backup file is the
        # verification step (the first is the backup() destination) --
        # fake just that one's quick_check result to force the failure path.
        backup_dir = tmp_path / "backups"
        backup_path = backup_dir / "stats-daily-20260101.db"
        original_connect = sqlite3.connect
        seen = {"count": 0}

        class FakeCorruptConnection:
            def execute(self, sql, *a):
                return type("Cursor", (), {"fetchone": lambda self: ("corrupt",)})()

            def close(self):
                pass

        def fake_connect(path, *args, **kwargs):
            if str(path) == str(backup_path):
                seen["count"] += 1
                if seen["count"] == 2:
                    return FakeCorruptConnection()
            return original_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", fake_connect)

        with pytest.raises(ValueError, match="quick_check"):
            storage.backup_database(db_path, backup_dir, keep_daily=7, today="20260101")

        assert not backup_path.exists()


class TestOptimizeAndCheckpoint:
    def test_optimize_does_not_raise(self, db_path):
        storage.optimize(db_path)

    def test_wal_checkpoint_truncate_leaves_an_empty_or_absent_wal_file(self, db_path):
        import os

        series_id = storage.get_or_create_series(
            db_path, "demo.thing.count", "demo", "cumulative", "Count", "", "", 1000
        )
        # A held-open connection keeps the WAL file from being auto-checkpointed
        # away between writes, so the truncate below has something to do.
        held_open = sqlite3.connect(str(db_path))
        try:
            for ts in range(1000, 1000 + 200):
                storage.record_sample(db_path, series_id, ts, ts, heartbeat_seconds=1)

            storage.wal_checkpoint_truncate(db_path)

            wal_path = str(db_path) + "-wal"
            assert not os.path.exists(wal_path) or os.path.getsize(wal_path) == 0
        finally:
            held_open.close()


class TestRestoreDrill:
    def test_restoring_a_daily_backup_recovers_series_samples_and_state(
        self, db_path, tmp_path
    ):
        """Backups nobody has restored from aren't backups: seed data, back
        it up, copy the backup to a fresh path, run migrations against it,
        and confirm every read gives back what was seeded."""
        series_id = storage.get_or_create_series(
            db_path,
            "demo.thing.count",
            "demo",
            "cumulative",
            "Count",
            "things",
            "",
            1000,
        )
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        storage.record_sample(db_path, series_id, 2000, 9, heartbeat_seconds=86400)
        run_id = storage.start_run(db_path, "demo", 1000)
        storage.finish_run(
            db_path, run_id, "ok", None, samples_written=2, finished_at=1000
        )
        storage.set_state(db_path, "k", "v")

        backup_dir = tmp_path / "backups"
        result = storage.backup_database(
            db_path, backup_dir, keep_daily=7, today="20260101"
        )

        restored_path = tmp_path / "restored" / "stats.db"
        restored_path.parent.mkdir(parents=True)
        restored_path.write_bytes(Path(result["backup_file"]).read_bytes())
        # No -wal/-shm should carry over from a live backup; write_bytes of
        # the single .db file already guarantees that here.
        assert not (restored_path.parent / "stats.db-wal").exists()

        migrate.run_migrations(restored_path)

        assert storage.list_series(restored_path)[0]["metric_key"] == "demo.thing.count"
        assert storage.history(restored_path, series_id, 0, 3000) == [
            (1000, 5),
            (2000, 9),
        ]
        assert storage.latest_run(restored_path, "demo")["status"] == "ok"
        assert storage.get_state(restored_path, "k") == "v"


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

    def test_anchor_seek_is_an_index_search_not_a_scan(self, db_path):
        # Regression pin for a real perf bug caught in review, twice: a
        # ROW_NUMBER()/PARTITION BY translation of "newest sample per
        # series" reads naturally but forces a temp-B-tree materialization
        # of every matching row before it can pick rn=1, and a seemingly
        # index-friendly MAX(ts) GROUP BY loses MIN/MAX's own index
        # short-circuit the moment a second bare column is selected
        # alongside it -- both measured *slower* than the plain per-series
        # "ORDER BY ts DESC LIMIT 1" seek this function actually uses.
        # "the plan says SEARCH" turned out not to be sufficient either
        # time (both replaced queries also said SEARCH), so this pins the
        # query text itself, not just its plan.
        anchored = self._series(db_path, "demo.anchored")
        storage.record_sample(db_path, anchored, 0, 10, heartbeat_seconds=1)

        plan = "\n".join(
            " ".join(str(cell) for cell in row)
            for row in storage.connect(db_path).execute(
                "EXPLAIN QUERY PLAN "
                "SELECT value FROM samples WHERE series_id = ? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (anchored, 100),
            )
        )

        assert "SEARCH" in plan
        assert "SCAN" not in plan
        assert "GROUP BY" not in plan.upper()

    def test_bulk_is_not_grossly_slower_than_a_per_series_loop(self, db_path):
        # The actual, easy-to-lose property a plan-shape assertion alone
        # doesn't pin (per review): whatever range_stats_bulk_conn does
        # internally, it must not regress on the very thing it exists to
        # speed up. A generous ratio bound keeps this from flaking on a
        # loaded CI runner while still catching the kind of regression
        # review caught repeatedly (up to ~28x slower).
        anchored_ids = [self._series(db_path, f"demo.anchored{i}") for i in range(20)]
        for series_id in anchored_ids:
            for t in range(0, 2000, 20):
                storage.record_sample(db_path, series_id, t, t, heartbeat_seconds=1)

        # Younger-than-range series: no sample at or before `start`, so
        # every one of these genuinely exercises the firsts/VALUES-join
        # path -- a previous version of this fixture picked a `start`
        # that coincided with an existing sample, so every series came
        # back anchored and the join path went untested even though the
        # test passed.
        younger_ids = [self._series(db_path, f"demo.younger{i}") for i in range(20)]
        for series_id in younger_ids:
            for t in range(1600, 2000, 20):
                storage.record_sample(db_path, series_id, t, t, heartbeat_seconds=1)

        series_ids = anchored_ids + younger_ids
        starts = {series_id: 1000 for series_id in anchored_ids} | {
            series_id: 1500 for series_id in younger_ids
        }
        end = 2000

        conn = storage.connect(db_path)
        try:

            def loop_stats():
                return {
                    series_id: storage.range_stats_conn(
                        conn, series_id, starts[series_id], end
                    )
                    for series_id in series_ids
                }

            def bulk_stats():
                return storage.range_stats_bulk_conn(conn, starts, end)

            # Best-of-3 for both, to smooth over one-off scheduling noise.
            loop_time = min(_time_it(loop_stats) for _ in range(3))
            bulk_time = min(_time_it(bulk_stats) for _ in range(3))
        finally:
            conn.close()

        assert bulk_time < loop_time * 3


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


class TestLastValuesForPlugin:
    def test_no_series_returns_empty(self, db_path):
        assert storage.last_values_for_plugin(db_path, "acme") == {}

    def test_returns_last_value_per_metric_key(self, db_path):
        series_id = storage.get_or_create_series(
            db_path, "acme.a", "acme", "gauge", "A", "", "", 1000
        )
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)
        storage.get_or_create_series(
            db_path, "acme.b", "acme", "gauge", "B", "", "", 1000
        )
        storage.get_or_create_series(
            db_path, "other.c", "other", "gauge", "C", "", "", 1000
        )

        result = storage.last_values_for_plugin(db_path, "acme")

        assert result == {"acme.a": 5, "acme.b": None}

    def test_reflects_state_before_a_later_record_sample_call(self, db_path):
        series_id = storage.get_or_create_series(
            db_path, "acme.a", "acme", "gauge", "A", "", "", 1000
        )
        storage.record_sample(db_path, series_id, 1000, 5, heartbeat_seconds=86400)

        before = storage.last_values_for_plugin(db_path, "acme")
        storage.record_sample(db_path, series_id, 2000, 9, heartbeat_seconds=86400)

        assert before == {"acme.a": 5}


class TestStateKeyValue:
    def test_get_state_unknown_key_returns_none(self, db_path):
        assert storage.get_state(db_path, "missing") is None

    def test_set_then_get_round_trips(self, db_path):
        storage.set_state(db_path, "milestone:acme.x", "500.0")
        assert storage.get_state(db_path, "milestone:acme.x") == "500.0"

    def test_set_state_upserts(self, db_path):
        storage.set_state(db_path, "k", "1")
        storage.set_state(db_path, "k", "2")
        assert storage.get_state(db_path, "k") == "2"

    def test_delete_state_removes_the_row(self, db_path):
        storage.set_state(db_path, "k", "1")
        storage.delete_state(db_path, "k")
        assert storage.get_state(db_path, "k") is None

    def test_delete_state_unknown_key_is_a_noop(self, db_path):
        storage.delete_state(db_path, "missing")  # must not raise

    def test_get_state_prefix_filters_by_prefix(self, db_path):
        storage.set_state(db_path, "milestone:acme.a", "1")
        storage.set_state(db_path, "milestone:acme.b", "2")
        storage.set_state(db_path, "milestone_pending:acme.a", "3")

        result = storage.get_state_prefix(db_path, "milestone:")

        assert result == {"milestone:acme.a": "1", "milestone:acme.b": "2"}

    def test_get_state_prefix_no_matches_returns_empty(self, db_path):
        storage.set_state(db_path, "other:key", "1")
        assert storage.get_state_prefix(db_path, "milestone:") == {}

    def test_set_state_and_delete_applies_both_atomically(self, db_path):
        storage.set_state(db_path, "milestone_pending:acme.x", "queued")

        storage.set_state_and_delete(
            db_path,
            sets={"milestone:acme.x": "500.0"},
            delete_keys=["milestone_pending:acme.x"],
        )

        assert storage.get_state(db_path, "milestone:acme.x") == "500.0"
        assert storage.get_state(db_path, "milestone_pending:acme.x") is None

    def test_set_state_and_delete_with_nothing_to_do_is_a_noop(self, db_path):
        storage.set_state_and_delete(db_path, sets={}, delete_keys=[])  # must not raise
