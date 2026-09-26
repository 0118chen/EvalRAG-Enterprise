from app import tasks
from app.core.store import SQLiteStore
from app.schemas import Document, KnowledgeBase
from app.tasks import process_document


def test_ready_document_is_not_processed_twice(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(str(tmp_path / "idempotent.db"))
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="t", name="x", description="")
    )
    store.save_document(
        Document(
            id="ready-doc",
            filename="x.txt",
            knowledge_base_id="kb",
            chunks=1,
            status="ready",
        ),
        [],
    )
    monkeypatch.setattr(tasks, "create_store", lambda database_url: store)

    function = process_document.run if hasattr(process_document, "run") else process_document
    result = function("ready-doc")

    assert result["stage"] == "already_indexed"
    assert result["status"] == "ready"
    # An already indexed document is not claimable without an explicit rebuild.
    assert store.claim_document("ready-doc") is False
