import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from numbers_go_up import api, migrate, storage

DAY = 86400
HOUR = 3600


def make_app(db_path, plugin_intervals=None, default_interval=1800):
    app = FastAPI()
    app.include_router(api.router)
    app.state.config = {
        "storage": {"path": str(db_path)},
        "poll": {"default_interval": default_interval},
    }
    app.state.plugin_intervals = plugin_intervals or {}
    return app


def seed_series(db_path, metric_key, plugin_name="acme", kind="cumulative", now=None):
    now = now if now is not None else int(time.time())
    series_id = storage.get_or_create_series(
        db_path, metric_key, plugin_name, kind, "Label", "unit", "icon", now
    )
    return series_id


def db(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(str(path))
    return str(path)


def client_for(tmp_path, **kwargs):
    db_path = db(tmp_path)
    app = make_app(db_path, **kwargs)
    return TestClient(app), db_path


def deactivate(db_path, series_id):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,))
        conn.commit()
    finally:
        conn.close()


# --- /api/stats/latest -------------------------------------------------


def test_latest_empty_db_returns_empty_metrics(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["metrics"] == {}
    assert "timestamp" in body


def test_latest_includes_deltas_and_not_stale(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now - 2 * DAY)

    storage.record_sample(db_path, series_id, now - 2 * DAY, 100, HOUR)
    storage.record_sample(db_path, series_id, now - 20 * HOUR, 105, HOUR)
    storage.record_sample(db_path, series_id, now - 30 * 60, 110, HOUR)

    response = client.get("/api/stats/latest")
    assert response.status_code == 200
    metric = response.json()["metrics"]["acme.widgets"]

    assert metric["value"] == 110
    assert metric["label"] == "Label"
    assert metric["kind"] == "cumulative"
    assert metric["unit"] == "unit"
    assert metric["icon"] == "icon"
    assert metric["stale"] is False
    assert metric["delta_1h"] == 5
    assert metric["delta_24h"] == 10


def test_latest_stale_requires_both_old_sample_and_last_run_failed(tmp_path):
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    old_ts = now - 4 * 1800 - 10
    series_id = seed_series(db_path, "acme.widgets", now=old_ts)
    storage.record_sample(db_path, series_id, old_ts, 42, HOUR)

    # Old sample, but last run succeeded -> not stale.
    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(db_path, run_id, "ok", None, samples_written=0, finished_at=now)

    response = client.get("/api/stats/latest")
    assert response.json()["metrics"]["acme.widgets"]["stale"] is False

    # Old sample and last run failed -> stale.
    run_id = storage.start_run(db_path, "acme", now + 1)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now + 1
    )

    response = client.get("/api/stats/latest")
    assert response.json()["metrics"]["acme.widgets"]["stale"] is True


def test_latest_poll_in_flight_does_not_make_a_healthy_series_stale(tmp_path):
    # Repro of the in-flight bug: start_run() inserts status='error' (the
    # _RUN_IN_PROGRESS sentinel) and only finish_run() overwrites it, so a
    # healthy plugin mid-poll must not read as failing.
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    old_ts = now - 4 * 1800 - 10
    series_id = seed_series(db_path, "acme.widgets", now=old_ts)
    storage.record_sample(db_path, series_id, old_ts, 42, HOUR)

    # Last finished run succeeded, and a new poll is in flight right now.
    run_id = storage.start_run(db_path, "acme", now - 1)
    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=0, finished_at=now - 1
    )
    storage.start_run(db_path, "acme", now)

    response = client.get("/api/stats/latest")

    assert response.json()["metrics"]["acme.widgets"]["stale"] is False


def test_latest_poll_in_flight_after_a_failure_is_stale(tmp_path):
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    old_ts = now - 4 * 1800 - 10
    series_id = seed_series(db_path, "acme.widgets", now=old_ts)
    storage.record_sample(db_path, series_id, old_ts, 42, HOUR)

    # Last finished run failed; the retry now in flight must not mask it.
    run_id = storage.start_run(db_path, "acme", now - 1)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now - 1
    )
    storage.start_run(db_path, "acme", now)

    response = client.get("/api/stats/latest")

    assert response.json()["metrics"]["acme.widgets"]["stale"] is True


def test_latest_recent_failed_sample_not_stale_due_to_age(tmp_path):
    client, db_path = client_for(tmp_path, plugin_intervals={"acme": 1800})
    now = int(time.time())
    series_id = seed_series(db_path, "acme.widgets", now=now)
    storage.record_sample(db_path, series_id, now, 42, HOUR)

    run_id = storage.start_run(db_path, "acme", now)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now
    )

    response = client.get("/api/stats/latest")
    assert response.json()["metrics"]["acme.widgets"]["stale"] is False


def test_latest_omits_series_with_no_samples(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    storage.get_or_create_series(
        db_path, "acme.never_written", "acme", "gauge", None, None, None, now
    )

    response = client.get("/api/stats/latest")
    assert response.json()["metrics"] == {}


def test_latest_skips_deactivated_series(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.retired", now=now)
    storage.record_sample(db_path, series_id, now, 42, HOUR)
    deactivate(db_path, series_id)

    response = client.get("/api/stats/latest")

    assert response.json()["metrics"] == {}


def test_latest_delta_null_with_no_history_before_window(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.new", now=now - 3 * HOUR)
    storage.record_sample(db_path, series_id, now - 3 * HOUR, 5, HOUR)

    response = client.get("/api/stats/latest")
    metric = response.json()["metrics"]["acme.new"]
    assert metric["delta_24h"] is None


# --- /api/stats/history --------------------------------------------------


def test_history_unknown_metric_404(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/history", params={"metric": "nope", "hours": 24})

    assert response.status_code == 404


def test_history_deactivated_metric_404(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.retired", now=now)
    storage.record_sample(db_path, series_id, now, 7, HOUR)
    deactivate(db_path, series_id)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.retired", "hours": 24}
    )

    assert response.status_code == 404


def test_history_flat_series_returns_carried_forward_anchor(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.flat", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 7, DAY)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.flat", "hours": 24}
    )

    assert response.status_code == 200
    points = response.json()["points"]
    assert len(points) == 1
    assert points[0]["value"] == 7


def test_history_returns_unix_timestamps(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.series", now=now - 2 * HOUR)
    storage.record_sample(db_path, series_id, now - 2 * HOUR, 1, HOUR)
    storage.record_sample(db_path, series_id, now - HOUR, 2, HOUR)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.series", "hours": 3}
    )

    points = response.json()["points"]
    assert all(isinstance(p["ts"], int) for p in points)


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "acme.series"},
        {"metric": "acme.series", "hours": 0},
        {"metric": "acme.series", "hours": -1},
        {"metric": "acme.series", "hours": "abc"},
        {"metric": "acme.series", "hours": 999999},
    ],
)
def test_history_invalid_hours_422(tmp_path, params):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/history", params=params)

    assert response.status_code == 422


def test_history_range_all_returns_from_first_sample(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.series", now=now - 400 * DAY)
    storage.record_sample(db_path, series_id, now - 400 * DAY, 1, DAY)
    storage.record_sample(db_path, series_id, now, 2, DAY)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.series", "range": "ALL"}
    )

    assert response.status_code == 200
    points = response.json()["points"]
    assert points[0]["value"] == 1
    assert points[-1]["value"] == 2


def test_history_range_and_hours_together_422(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get(
        "/api/stats/history",
        params={"metric": "acme.series", "hours": 24, "range": "1D"},
    )

    assert response.status_code == 422


def test_history_neither_range_nor_hours_422(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/history", params={"metric": "acme.series"})

    assert response.status_code == 422


def test_history_invalid_range_422(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.series", "range": "5Y"}
    )

    assert response.status_code == 422


def test_history_named_range_still_works_like_hours(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.series", now=now - HOUR)
    storage.record_sample(db_path, series_id, now - HOUR, 1, HOUR)
    storage.record_sample(db_path, series_id, now, 2, HOUR)

    by_hours = client.get(
        "/api/stats/history", params={"metric": "acme.series", "hours": 24}
    )
    by_range = client.get(
        "/api/stats/history", params={"metric": "acme.series", "range": "1D"}
    )

    assert by_hours.json()["points"] == by_range.json()["points"]


def test_history_bars_excludes_empty_buckets(tmp_path):
    # 1M buckets to 1 day (BAR_BUCKET_SECONDS), so three samples 3+ days
    # apart land in three separate, non-adjacent buckets, and the days
    # between them are simply absent from `bars` rather than zero-filled.
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.series", now=now - 10 * DAY)
    storage.record_sample(db_path, series_id, now - 10 * DAY, 10, DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 15, DAY)
    storage.record_sample(db_path, series_id, now, 20, DAY)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.series", "range": "1M"}
    )

    bars = response.json()["bars"]
    assert len(bars) == 3
    # The first bucket holds the opening sample itself, so its net change
    # is 0 -- there is nothing before it to have changed from.
    assert bars[0]["change"] == 0
    assert bars[1]["change"] == 5
    assert bars[2]["change"] == 5


def test_history_bars_empty_for_single_point(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.flat", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 7, DAY)

    response = client.get(
        "/api/stats/history", params={"metric": "acme.flat", "range": "1W"}
    )

    assert response.json()["bars"] == []


# --- /api/stats/delta -----------------------------------------------------


def test_delta_unknown_metric_404(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/stats/delta", params={"metric": "nope", "hours": 24})

    assert response.status_code == 404


def test_delta_deactivated_metric_404(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.retired", now=now)
    storage.record_sample(db_path, series_id, now, 7, HOUR)
    deactivate(db_path, series_id)

    response = client.get(
        "/api/stats/delta", params={"metric": "acme.retired", "hours": 24}
    )

    assert response.status_code == 404


def test_delta_across_gap_uses_last_value_carried_forward(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.series", now=now - 5 * DAY)
    storage.record_sample(db_path, series_id, now - 5 * DAY, 331, DAY)
    storage.record_sample(db_path, series_id, now - HOUR, 338, DAY)

    response = client.get(
        "/api/stats/delta", params={"metric": "acme.series", "hours": 24}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current"] == 338
    assert body["previous"] == 331
    assert body["delta"] == 7
    assert body["rate_per_hour"] == round(7 / 24, 2)


def test_delta_no_starting_point_is_null_not_zero(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = seed_series(db_path, "acme.new", now=now - 3 * HOUR)
    storage.record_sample(db_path, series_id, now - 3 * HOUR, 5, HOUR)

    response = client.get(
        "/api/stats/delta", params={"metric": "acme.new", "hours": 24}
    )

    body = response.json()
    assert body["previous"] is None
    assert body["delta"] is None
    assert body["rate_per_hour"] is None
    assert body["current"] == 5


# --- /api/integrations ---------------------------------------------------


def test_integrations_with_no_mqtt_publisher_reports_disabled(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/integrations")

    assert response.status_code == 200
    assert response.json() == {
        "mqtt": {
            "enabled": False,
            "connected": False,
            "broker": None,
            "last_publish": None,
            "last_error": None,
        }
    }


def test_integrations_reports_mqtt_publisher_status(tmp_path):
    client, db_path = client_for(tmp_path)

    class FakePublisher:
        @property
        def status(self):
            return {
                "enabled": True,
                "connected": True,
                "broker": "broker.local:1883",
                "last_publish": 1_700_000_000,
                "last_error": None,
            }

    client.app.state.mqtt_publisher = FakePublisher()

    response = client.get("/api/integrations")

    body = response.json()
    assert body["mqtt"]["enabled"] is True
    assert body["mqtt"]["connected"] is True
    assert body["mqtt"]["broker"] == "broker.local:1883"
    assert body["mqtt"]["last_publish"] is not None
    assert body["mqtt"]["last_error"] is None


def test_integrations_response_never_includes_a_password_field(tmp_path):
    client, _ = client_for(tmp_path)

    class FakePublisher:
        @property
        def status(self):
            return {
                "enabled": True,
                "connected": True,
                "broker": "broker.local:1883",
                "last_publish": None,
                "last_error": None,
            }

    client.app.state.mqtt_publisher = FakePublisher()

    response = client.get("/api/integrations")

    assert "password" not in response.text.lower()
