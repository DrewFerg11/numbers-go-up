import time
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from numbers_go_up import api, migrate, storage


class _FakeJob:
    def __init__(self, next_run_time):
        self.next_run_time = next_run_time


class _FakeScheduler:
    def __init__(self, jobs=None):
        self._jobs = jobs or {}

    def get_job(self, job_id):
        return self._jobs.get(job_id)


def make_app(db_path, plugins_config=None, job_scheduler=None, plugin_names=None):
    app = FastAPI()
    app.include_router(api.router)
    app.state.config = {
        "storage": {"path": str(db_path)},
        "poll": {"default_interval": 1800},
        "plugins": plugins_config or {},
        # No NGU_PLUGIN_DIR set for these tests, and names come from the
        # plugin_names snapshot below -- nothing here discovers plugins.
        "plugin_dir": None,
    }
    if job_scheduler is not None:
        app.state.scheduler = job_scheduler
    # What main.lifespan computes once at startup: /api/plugins reads this
    # snapshot off app.state instead of discovering per request.
    if plugin_names is None:
        plugin_names = ["makerworld"]
    app.state.plugin_names = list(plugin_names)
    return app


def db(tmp_path):
    path = tmp_path / "stats.db"
    migrate.run_migrations(str(path))
    return str(path)


def client_for(tmp_path, **kwargs):
    db_path = db(tmp_path)
    app = make_app(db_path, **kwargs)
    return TestClient(app), db_path


# --- /api/metrics ----------------------------------------------------------


def test_metrics_empty_db(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json() == {"metrics": []}


def test_metrics_includes_series_for_disabled_or_removed_plugin(tmp_path):
    client, db_path = client_for(tmp_path, plugins_config={})
    now = int(time.time())
    storage.get_or_create_series(
        db_path, "ghost.old_metric", "ghost", "gauge", "Ghost", "u", "i", now
    )

    response = client.get("/api/metrics")

    assert response.status_code == 200
    keys = [m["key"] for m in response.json()["metrics"]]
    assert "ghost.old_metric" in keys


def test_metrics_shape(tmp_path):
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = storage.get_or_create_series(
        db_path, "acme.widgets", "acme", "cumulative", "Widgets", "u", "i", now
    )
    storage.record_sample(db_path, series_id, now, 5, 86400)

    response = client.get("/api/metrics")
    metric = response.json()["metrics"][0]

    assert metric["key"] == "acme.widgets"
    assert metric["plugin"] == "acme"
    assert metric["kind"] == "cumulative"
    assert metric["last_value"] == 5
    assert metric["last_seen"] is not None
    assert metric["active"] is True


def test_metrics_includes_deactivated_series(tmp_path):
    # Unlike /api/stats/latest (storage.list_series), the catalogue is
    # meant to report every series that ever existed -- storage.py's
    # deactivation guard is for serving stats, not for the catalogue.
    client, db_path = client_for(tmp_path)
    now = int(time.time())
    series_id = storage.get_or_create_series(
        db_path, "acme.retired", "acme", "gauge", "Retired", "u", "i", now
    )
    conn = storage.connect(db_path)
    try:
        conn.execute("UPDATE metric_series SET active = 0 WHERE id = ?", (series_id,))
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/metrics")

    metric = next(m for m in response.json()["metrics"] if m["key"] == "acme.retired")
    assert metric["active"] is False


# --- /api/plugins ------------------------------------------------------------


def test_plugins_zero_plugins_configured(tmp_path):
    client, _ = client_for(tmp_path, plugins_config={})

    response = client.get("/api/plugins")

    assert response.status_code == 200
    plugins = response.json()["plugins"]
    # Every discovered plugin is reported, just disabled -- none crash.
    assert all(p["enabled"] is False for p in plugins)
    assert all(p["status"] == "disabled" for p in plugins)
    assert all(p["last_poll"] is None for p in plugins)
    assert all(p["next_poll"] is None for p in plugins)


def test_plugins_disabled_plugin_has_null_poll_times(tmp_path):
    client, _ = client_for(tmp_path, plugins_config={"makerworld": {"enabled": False}})

    response = client.get("/api/plugins")

    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")
    assert plugin["enabled"] is False
    assert plugin["status"] == "disabled"
    assert plugin["last_poll"] is None
    assert plugin["next_poll"] is None


def test_plugins_pending_when_enabled_but_never_run(tmp_path):
    client, _ = client_for(tmp_path, plugins_config={"makerworld": {"enabled": True}})

    response = client.get("/api/plugins")

    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")
    assert plugin["enabled"] is True
    assert plugin["status"] == "pending"
    assert plugin["last_poll"] is None


def test_plugins_polling_while_run_in_flight(tmp_path):
    # start_run() inserts every run as status='error' ("run in progress")
    # until finish_run() overwrites it -- an in-flight poll must not read
    # as an error here, the same trap _is_stale() avoids via
    # storage.latest_finished_run().
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())
    storage.start_run(db_path, "makerworld", now)

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["status"] == "polling"
    assert plugin["last_poll"] is None
    assert plugin["last_error"] is None
    # consecutive_failures keeps #28's liveness convention (an unfinished
    # run counts as a failure); only ``status`` is spared the sentinel.
    assert plugin["consecutive_failures"] == 1


def test_plugins_error_status_and_last_error_from_latest_finished_run(tmp_path):
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())

    run_id = storage.start_run(db_path, "makerworld", now - 100)
    storage.finish_run(
        db_path, run_id, "ok", None, samples_written=1, finished_at=now - 100
    )
    run_id = storage.start_run(db_path, "makerworld", now)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now
    )

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["status"] == "error"
    assert plugin["last_error"] == "boom"
    assert plugin["consecutive_failures"] == 1
    assert plugin["last_poll"] is not None


def test_plugins_blocked_status_when_latest_finished_run_was_a_403(tmp_path):
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())
    error = "blocked (HTTP 403, cf-mitigated=challenge) for url 'https://x/'"
    run_id = storage.start_run(db_path, "makerworld", now)
    storage.finish_run(
        db_path, run_id, "error", error, samples_written=0, finished_at=now
    )

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["status"] == "blocked"
    assert plugin["last_error"] == error
    assert plugin["consecutive_failures"] == 1


def test_plugins_ok_status_has_null_last_error(tmp_path):
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())
    run_id = storage.start_run(db_path, "makerworld", now)
    storage.finish_run(db_path, run_id, "ok", None, samples_written=1, finished_at=now)

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["status"] == "ok"
    assert plugin["last_error"] is None
    assert plugin["consecutive_failures"] == 0


def test_plugins_polling_after_failed_run_keeps_last_error(tmp_path):
    # The in-flight run takes over ``status``; last_poll/last_error stay
    # pinned to the newest *finished* run rather than the sentinel row.
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())

    run_id = storage.start_run(db_path, "makerworld", now - 200)
    storage.finish_run(
        db_path, run_id, "error", "boom", samples_written=0, finished_at=now - 200
    )
    storage.start_run(db_path, "makerworld", now)

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["status"] == "polling"
    expected = datetime.fromtimestamp(now - 200, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert plugin["last_poll"] == expected
    assert plugin["last_error"] == "boom"


def test_plugins_next_poll_comes_from_scheduler_job(tmp_path):
    next_run = datetime(2030, 1, 1, tzinfo=UTC)
    scheduler = _FakeScheduler({"plugin:makerworld": _FakeJob(next_run)})
    client, _ = client_for(
        tmp_path,
        plugins_config={"makerworld": {"enabled": True}},
        job_scheduler=scheduler,
    )

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["next_poll"] == "2030-01-01T00:00:00Z"


def test_plugins_no_scheduler_on_app_state_is_handled(tmp_path):
    client, _ = client_for(tmp_path, plugins_config={"makerworld": {"enabled": True}})

    response = client.get("/api/plugins")

    assert response.status_code == 200
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")
    assert plugin["next_poll"] is None


def test_plugins_reports_names_from_app_state_snapshot(tmp_path):
    # The route reports exactly the startup snapshot -- it does not
    # rediscover plugins per request, so even a name with no module
    # behind it right now is listed (disabled, like any unconfigured
    # plugin).
    client, _ = client_for(tmp_path, plugin_names=["makerworld", "custom"])

    response = client.get("/api/plugins")
    plugins = response.json()["plugins"]

    assert [p["name"] for p in plugins] == ["makerworld", "custom"]
    custom = next(p for p in plugins if p["name"] == "custom")
    assert custom["enabled"] is False
    assert custom["status"] == "disabled"
    assert custom["last_poll"] is None
    assert custom["next_poll"] is None


def test_plugins_no_discovery_per_request(tmp_path, monkeypatch):
    # Discovery executes every plugin module (module-level code, including
    # NGU_PLUGIN_DIR user code), so it must happen once at startup, never
    # inside a request handler. If the route so much as touches
    # _discover_dir, this test fails.
    def _boom(directory):
        raise AssertionError("discovery ran inside the request handler")

    monkeypatch.setattr("numbers_go_up.plugins._discover_dir", _boom)
    client, _ = client_for(tmp_path, plugins_config={"makerworld": {"enabled": True}})

    response = client.get("/api/plugins")

    assert response.status_code == 200
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")
    assert plugin["status"] == "pending"


def test_plugins_metrics_list_reflects_metric_series(tmp_path):
    client, db_path = client_for(
        tmp_path, plugins_config={"makerworld": {"enabled": True}}
    )
    now = int(time.time())
    storage.get_or_create_series(
        db_path,
        "makerworld.profile.design_downloads",
        "makerworld",
        "cumulative",
        "Downloads",
        "downloads",
        "🖨️",
        now,
    )

    response = client.get("/api/plugins")
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "makerworld")

    assert plugin["metrics"] == ["makerworld.profile.design_downloads"]


def test_plugins_never_includes_config(tmp_path):
    client, _ = client_for(
        tmp_path,
        plugins_config={"makerworld": {"enabled": True, "user_id": "secret-handle"}},
    )

    response = client.get("/api/plugins")

    assert "secret-handle" not in response.text
    assert "user_id" not in response.text
