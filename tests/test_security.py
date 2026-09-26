import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.schemas import EvaluationCreate, SearchRequest


def test_api_key_binds_tenant(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'auth.db').as_posix()}",
        auth_enabled=True,
        api_keys={"secret": "tenant-a"},
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/knowledge-bases?tenant_id=tenant-a").status_code == 401
    assert (
        client.get(
            "/api/v1/knowledge-bases?tenant_id=tenant-a",
            headers={"X-API-Key": "secret"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/knowledge-bases?tenant_id=tenant-b",
            headers={"X-API-Key": "secret"},
        ).status_code
        == 403
    )


def test_rate_limit_returns_429(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'rate.db').as_posix()}",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/knowledge-bases?tenant_id=tenant").status_code == 200
    blocked = client.get("/api/v1/knowledge-bases?tenant_id=tenant")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]


@pytest.mark.parametrize("model", [SearchRequest, EvaluationCreate])
def test_document_version_rejects_filter_injection(model) -> None:
    payload = {
        "tenant_id": "tenant",
        "knowledge_base_id": "kb",
        "document_version": 'v1" or version != "',
    }
    if model is SearchRequest:
        payload["question"] = "policy"
    else:
        payload.update(dataset_name="set", retrieval_mode="hybrid", top_k=3)

    with pytest.raises(ValidationError, match="document_version"):
        model(**payload)
