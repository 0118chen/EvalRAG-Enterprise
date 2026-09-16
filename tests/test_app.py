from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health", headers={"X-Request-ID": "req-test-1"})
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "req-test-1"


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
