from app.core.store import SQLiteStore
from app.schemas import Document, KnowledgeBase


def test_document_progress_and_error_are_persisted(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "progress.db"))
    store.save_knowledge_base(KnowledgeBase(id="kb", tenant_id="t", name="x", description=""))
    store.save_document(Document(id="d", filename="x.txt", knowledge_base_id="kb", chunks=0, status="pending", progress=10), [])
    store.update_document_progress("d", 0, "parse failed")
    document = store.get_document("d", "t")
    assert document and document.progress == 0 and document.error_message == "parse failed"
