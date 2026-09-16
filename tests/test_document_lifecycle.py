from app.core.ingestion import Chunk
from app.core.store import SQLiteStore
from app.schemas import Document, KnowledgeBase


def test_document_delete_cleans_chunks_and_enforces_tenant(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "lifecycle.db"))
    store.save_knowledge_base(KnowledgeBase(id="kb", tenant_id="a", name="x", description=""))
    document = Document(id="doc", filename="x.txt", knowledge_base_id="kb", chunks=1)
    store.save_document(document, [Chunk("chunk", "doc", 1, "text")])
    assert not store.delete_document("doc", "b")
    assert store.delete_document("doc", "a")
    assert store.get_chunks("kb") == []
