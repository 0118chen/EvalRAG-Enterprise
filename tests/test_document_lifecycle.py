from fastapi.testclient import TestClient

import app.api.routes.documents as documents_route
from app.config import Settings
from app.core.ingestion import Chunk
from app.core.store import SQLiteStore
from app.main import create_app
from app.schemas import Document, KnowledgeBase


def test_document_delete_cleans_chunks_and_enforces_tenant(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "lifecycle.db"))
    store.save_knowledge_base(KnowledgeBase(id="kb", tenant_id="a", name="x", description=""))
    document = Document(id="doc", filename="x.txt", knowledge_base_id="kb", chunks=1)
    store.save_document(document, [Chunk("chunk", "doc", 1, "text")])
    assert not store.delete_document("doc", "b")
    assert store.delete_document("doc", "a")
    assert store.get_chunks("kb") == []


def test_document_delete_cleans_external_indexes_before_database(monkeypatch, tmp_path) -> None:
    calls = []

    class FakePipeline:
        async def delete_document(self, document_id):
            calls.append(document_id)

    app = create_app(
        Settings(database_url=f"sqlite:///{(tmp_path / 'api-lifecycle.db').as_posix()}")
    )
    store = app.state.container.store
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="x", description="")
    )
    store.save_document(
        Document(id="doc", filename="x.txt", knowledge_base_id="kb", chunks=0),
        [],
    )
    monkeypatch.setattr(
        documents_route,
        "create_ingestion_pipeline",
        lambda settings: FakePipeline(),
    )

    response = TestClient(app).delete("/api/v1/documents/doc?tenant_id=tenant")

    assert response.status_code == 204
    assert calls == ["doc"]
    assert store.get_document("doc", "tenant") is None
