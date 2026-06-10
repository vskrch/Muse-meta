"""HTTP security baseline tests."""

from fastapi.testclient import TestClient

from muse_meta.config import Settings
from muse_meta.main import create_app


def test_health_is_public_and_sets_security_headers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_docs_are_disabled_by_default(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 404


def test_protected_routes_require_bearer_token(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_bearer_token_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_valid_bearer_token_can_list_models(client: TestClient) -> None:
    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "muse-spark"


def test_request_body_limit_rejects_oversized_payload(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-token"},
        content=b"x" * 131_072,
    )

    assert response.status_code == 413


def test_readiness_reports_missing_upstream_auth(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 503
    assert "upstream authentication" in response.json()["detail"]


def test_rate_limit_returns_retry_after() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        api_key="rate-limit-token",
        allowed_hosts="testserver",
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    limited_client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer rate-limit-token"}

    first_response = limited_client.get("/v1/models", headers=headers)
    second_response = limited_client.get("/v1/models", headers=headers)

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.headers["retry-after"]
