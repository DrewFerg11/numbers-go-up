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
