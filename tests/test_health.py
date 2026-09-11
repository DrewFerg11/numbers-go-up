from fastapi.testclient import TestClient

from numbers_go_up import __version__
from numbers_go_up.main import app

client = TestClient(app)


def test_health_returns_ok_and_version():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
