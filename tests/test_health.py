import logging

import pytest
from fastapi.testclient import TestClient

from numbers_go_up import __version__, main
from numbers_go_up.config import ConfigError
from numbers_go_up.main import app

client = TestClient(app)


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", path, "1.1", 200),
        None,
    )


def test_health_check_access_lines_are_filtered_out():
    access_filter = main.HealthCheckAccessFilter()

    assert access_filter.filter(_access_record("/health")) is False
    assert access_filter.filter(_access_record("/health/plugins?failures=2")) is False
    assert access_filter.filter(_access_record("/api/plugins")) is True
    assert access_filter.filter(_access_record("/healthz")) is True


def test_access_filter_passes_records_it_does_not_recognise():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, "plain message", None, None
    )

    assert main.HealthCheckAccessFilter().filter(record) is True


def test_configure_logging_is_idempotent():
    main.configure_logging(env={})
    main.configure_logging(env={})

    package_logger = logging.getLogger("numbers_go_up")
    handlers = [
        h for h in package_logger.handlers if isinstance(h, main.PackageLogHandler)
    ]
    filters = [
        f
        for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, main.HealthCheckAccessFilter)
    ]
    assert len(handlers) == 1
    assert len(filters) == 1
    assert package_logger.level == logging.INFO


def test_log_level_comes_from_env_and_falls_back_to_info():
    package_logger = logging.getLogger("numbers_go_up")
    try:
        main.configure_logging(env={"NGU_LOG_LEVEL": "debug"})
        assert package_logger.level == logging.DEBUG

        main.configure_logging(env={"NGU_LOG_LEVEL": "loud"})
        assert package_logger.level == logging.INFO
    finally:
        main.configure_logging(env={})


def test_health_plugins_route_is_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("NGU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NGU_CONFIG_FILE", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.setenv("NGU_PLUGIN_DIR", str(tmp_path / "plugins"))

    # A fresh install enables no plugins, so the endpoint answers healthy.
    with TestClient(app) as scoped_client:
        response = scoped_client.get("/health/plugins")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_ok_and_version():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_lifespan_runs_migrations_before_serving(tmp_path, monkeypatch):
    monkeypatch.setenv("NGU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NGU_CONFIG_FILE", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.setenv("NGU_PLUGIN_DIR", str(tmp_path / "plugins"))

    with TestClient(app) as scoped_client:
        response = scoped_client.get("/health")

    assert response.status_code == 200

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "data" / "stats.db"))
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

    assert "metric_series" in tables


def test_service_starts_with_an_mqtt_broker_that_is_unreachable(tmp_path, monkeypatch):
    # No network access in tests: 127.0.0.1 on a port nothing listens on is
    # about as close to "unreachable broker" as this suite can get without
    # touching a real socket in a blocking way -- the point here is only
    # that startup and polling never depend on the connect succeeding.
    monkeypatch.setenv("NGU_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("mqtt:\n  host: 127.0.0.1\n  port: 1\n")
    monkeypatch.setenv("NGU_CONFIG_FILE", str(config_dir / "config.yaml"))
    monkeypatch.setenv("NGU_PLUGIN_DIR", str(tmp_path / "plugins"))

    with TestClient(app) as scoped_client:
        health_response = scoped_client.get("/health")
        integrations_response = scoped_client.get("/api/integrations")

    assert health_response.status_code == 200
    body = integrations_response.json()
    assert body["mqtt"]["enabled"] is True
    assert "password" not in integrations_response.text.lower()


def test_missing_mqtt_host_fails_startup_with_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("NGU_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("mqtt:\n  port: 1883\n")
    monkeypatch.setenv("NGU_CONFIG_FILE", str(config_dir / "config.yaml"))
    monkeypatch.setenv("NGU_PLUGIN_DIR", str(tmp_path / "plugins"))

    with pytest.raises(ConfigError), TestClient(app):
        pass


def test_service_starts_with_every_plugin_broken(tmp_path, monkeypatch):
    monkeypatch.setenv("NGU_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plugin_dir = tmp_path / "user-plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text(
        "METRICS = {'broken.x': {'kind': 'gauge', 'label': 'X', 'unit': ''}}\n"
        "def collect(config, http):\n"
        "    raise RuntimeError('always broken')\n"
    )
    (config_dir / "config.yaml").write_text(
        "poll:\n  default_interval: 300\nplugins:\n  broken:\n    enabled: true\n"
    )
    monkeypatch.setenv("NGU_CONFIG_FILE", str(config_dir / "config.yaml"))
    monkeypatch.setenv("NGU_PLUGIN_DIR", str(plugin_dir))

    with TestClient(app) as scoped_client:
        response = scoped_client.get("/health")

    assert response.status_code == 200
