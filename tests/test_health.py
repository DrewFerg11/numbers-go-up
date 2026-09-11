from fastapi.testclient import TestClient

from numbers_go_up import __version__
from numbers_go_up.main import app

client = TestClient(app)


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
