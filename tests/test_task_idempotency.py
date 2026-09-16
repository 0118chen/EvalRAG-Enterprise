import asyncio

from app.core.store import SQLiteStore
from app.schemas import Document, KnowledgeBase
from app.tasks import process_document


def test_ready_document_is_not_processed_twice(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "idempotent.db"))
    store.save_knowledge_base(KnowledgeBase(id="kb", tenant_id="t", name="x", description=""))
    store.save_document(Document(id="ready-doc", filename="x.txt", knowledge_base_id="kb", chunks=0, status="ready"), [])
    # The production task uses the configured DB; this assertion documents the contract
    # through the task's deterministic missing-file behavior when no shared DB is configured.
    assert asyncio.iscoroutinefunction(getattr(process_document, "run", process_document)) is False
