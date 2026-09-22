"""/docs, /redoc, and /openapi.json (#92): offline Swagger UI/ReDoc via
fastapi-offline (no CDN), and every hand-written response model actually
lands in the schema."""

import re

from fastapi.testclient import TestClient

from numbers_go_up.main import app

client = TestClient(app)

# The seven endpoints issue #92 asks for real response schemas on.
DOCUMENTED_ENDPOINTS = [
    ("/api/stats/latest", "get", "StatsLatestResponse"),
    ("/api/stats/history", "get", "StatsHistoryResponse"),
    ("/api/stats/delta", "get", "StatsDeltaResponse"),
    ("/api/metrics", "get", "ListMetricsResponse"),
    ("/api/plugins", "get", "ListPluginsResponse"),
    ("/health", "get", "HealthResponse"),
    ("/health/plugins", "get", "PluginsHealthResponse"),
]

# href="..."/src="..." pointing anywhere but fastapi-offline's own bundled
# static mount or /openapi.json -- a CDN reference is exactly what would
# blank-page /docs on a LAN-only box with no egress.
_ASSET_URL = re.compile(r'(?:href|src)="([^"]+)"')


def _assert_only_local_assets(html: str) -> None:
    urls = _ASSET_URL.findall(html)
    assert urls, "expected at least one asset URL in the page"
    for url in urls:
        assert url.startswith("/static-offline-docs/"), f"non-local asset URL: {url}"


def test_docs_returns_200_and_references_only_local_assets():
    response = client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    _assert_only_local_assets(response.text)


def test_docs_does_not_reference_a_cdn():
    response = client.get("/docs")

    assert "cdn." not in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text


def test_docs_is_excluded_from_the_schema_it_documents():
    schema = client.get("/openapi.json").json()

    assert "/docs" not in schema["paths"]


def test_dashboard_internal_routes_are_excluded_from_the_schema():
    # / and /m/{metric_key} serve the dashboard's own HTML pages, and
    # /api/stats/overview is a denormalized batch fetch for the overview's
    # auto-refresh -- none of the three is a documented public endpoint, so
    # none should appear alongside DOCUMENTED_ENDPOINTS on /docs.
    schema = client.get("/openapi.json").json()

    assert "/" not in schema["paths"]
    assert "/m/{metric_key}" not in schema["paths"]
    assert "/api/stats/overview" not in schema["paths"]


def test_redoc_returns_200_and_references_only_local_assets():
    # fastapi-offline bundles ReDoc too, so unlike a hand-vendored Swagger
    # UI (which would need a second bundle for no real gain), this comes
    # free -- no reason to leave it disabled.
    response = client.get("/redoc")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    _assert_only_local_assets(response.text)
    assert "cdn." not in response.text


def test_openapi_schema_has_a_populated_component_for_every_documented_endpoint():
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]

    for path, method, model_name in DOCUMENTED_ENDPOINTS:
        response_schema = schema["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {"$ref": f"#/components/schemas/{model_name}"}

        model = schemas[model_name]
        assert model.get("properties"), f"{model_name} has no declared properties"


def test_documented_endpoints_are_grouped_with_a_tag_and_summary():
    schema = client.get("/openapi.json").json()

    expected_tags = {
        "/api/stats/latest": "stats",
        "/api/stats/history": "stats",
        "/api/stats/delta": "stats",
        "/api/metrics": "metrics",
        "/api/plugins": "plugins",
        "/health": "health",
        "/health/plugins": "health",
    }
    for path, method, _ in DOCUMENTED_ENDPOINTS:
        operation = schema["paths"][path][method]
        assert operation["tags"] == [expected_tags[path]]
        assert operation.get("summary")


def test_app_metadata_has_a_description_and_license():
    schema = client.get("/openapi.json").json()

    assert schema["info"]["description"]
    assert schema["info"]["license"]["name"]
