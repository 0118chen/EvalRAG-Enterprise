"""External connectivity health checks must not be anonymously reachable."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(tmp_path, **overrides) -> TestClient:
    settings = {
        "database_url": f"sqlite:///{(tmp_path / 'health.db').as_posix()}",
        "llm_provider": "mock",
        "langsmith_enabled": False,
        "langsmith_api_key": None,
    }
    settings.update(overrides)
    return TestClient(create_app(Settings(**settings)))


def test_external_health_checks_require_the_admin_token_outside_development(
    tmp_path,
) -> None:
    client = _client(tmp_path, app_env="staging", health_admin_token="staging-token")

    missing = client.get("/health/llm")
    wrong = client.get("/health/llm", headers={"X-Health-Token": "guess"})
    allowed = client.get("/health/llm", headers={"X-Health-Token": "staging-token"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "mock"


def test_external_health_checks_fail_closed_when_no_token_is_configured(tmp_path) -> None:
    client = _client(tmp_path, app_env="production", health_admin_token=None)

    assert client.get("/health/llm").status_code == 503
    assert client.get("/health/langsmith").status_code == 503
    # Liveness and readiness must stay reachable for orchestrators.
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_external_health_checks_are_rate_limited(tmp_path) -> None:
    client = _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
        rate_limit_backend="memory",
    )

    first = client.get("/health/llm")
    second = client.get("/health/llm")

    assert first.status_code == 200
    assert second.status_code == 429
    # The window is 60s wide and the second call lands mid-window, so the exact
    # value is time dependent; only its bound is guaranteed.
    assert 1 <= int(second.headers["Retry-After"]) <= 60
    # Cheap paths are exempt, so they still answer after the budget is spent.
    assert client.get("/health/ready").status_code == 200
