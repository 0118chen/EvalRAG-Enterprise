from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, create_app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health", headers={"X-Request-ID": "req-test-1"})
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "req-test-1"
    metrics = client.get("/metrics").text
    assert "evalrag_http_requests_total" in metrics


def test_tenant_isolation() -> None:
    response = client.post("/api/v1/knowledge-bases?tenant_id=tenant-a", json={"name": "Policies"})
    forbidden = client.post("/api/v1/retrieval/search", json={"tenant_id": "tenant-b", "knowledge_base_id": response.json()["id"], "question": "test"})
    assert forbidden.status_code == 404


def test_management_lists_are_tenant_scoped() -> None:
    response = client.post("/api/v1/knowledge-bases?tenant_id=tenant-list-a", json={"name": "A"})
    kb_id = response.json()["id"]
    assert any(item["id"] == kb_id for item in client.get("/api/v1/knowledge-bases?tenant_id=tenant-list-a").json())
    assert client.get("/api/v1/knowledge-bases?tenant_id=tenant-list-b").json() == []
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/documents?tenant_id=tenant-list-b").status_code == 404


def test_metrics_endpoint_is_prometheus_compatible() -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_llm_health_reports_mock_mode(tmp_path) -> None:
    isolated_client = TestClient(
        create_app(
            Settings(
                database_url=f"sqlite:///{(tmp_path / 'llm-health.db').as_posix()}",
                llm_provider="mock",
                langsmith_enabled=False,
                langsmith_api_key=None,
            )
        )
    )
    response = isolated_client.get("/health/llm")
    assert response.status_code == 200
    assert response.json()["status"] == "mock"
    assert response.json()["connected"] is False

    langsmith_response = isolated_client.get("/health/langsmith")
    assert langsmith_response.status_code == 200
    assert langsmith_response.json()["status"] == "disabled"
    assert langsmith_response.json()["connected"] is False
