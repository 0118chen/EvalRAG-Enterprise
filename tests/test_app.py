from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_tenant_isolation() -> None:
    response = client.post("/api/v1/knowledge-bases?tenant_id=tenant-a", json={"name": "Policies"})
    forbidden = client.post("/api/v1/retrieval/search", json={"tenant_id": "tenant-b", "knowledge_base_id": response.json()["id"], "question": "test"})
    assert forbidden.status_code == 404

