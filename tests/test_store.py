from pathlib import Path

from app.core.ingestion import Chunk
from app.core.store import SQLiteStore, create_store, normalize_database_url
from app.schemas import Document, KnowledgeBase


def test_sqlite_persists_knowledge_base(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "test.db"))
    kb = KnowledgeBase(id="kb-1", tenant_id="tenant-a", name="Policies", description="")
    store.save_knowledge_base(kb)
    assert store.get_knowledge_base("kb-1", "tenant-a") == kb
    assert store.get_knowledge_base("kb-1", "tenant-b") is None


def test_sqlalchemy_store_persists_document_chunks_and_evaluation(tmp_path: Path) -> None:
    store = create_store(f"sqlite:///{(tmp_path / 'runtime.db').as_posix()}")
    store.save_knowledge_base(KnowledgeBase(id="kb", tenant_id="tenant", name="Policies", description=""))
    document = Document(id="doc", filename="policy.txt", knowledge_base_id="kb", chunks=0, status="pending", progress=20)
    store.save_document(document, [])

    chunks = [Chunk("chunk-1", "doc", 1, "first"), Chunk("chunk-2", "doc", 2, "second")]
    store.replace_chunks("doc", "kb", chunks)
    indexed = store.get_document("doc", "tenant")
    assert indexed and indexed.chunks == 2
    assert store.get_chunks("kb") == chunks

    store.create_evaluation("eval", "golden-set", "hybrid", 5)
    store.update_evaluation("eval", "completed", {"recall_at_5": 0.9})
    evaluation = store.get_evaluation("eval")
    assert evaluation and evaluation["status"] == "completed"
    assert evaluation["results"] == {"recall_at_5": 0.9}


def test_postgresql_urls_use_psycopg_driver() -> None:
    assert normalize_database_url("postgres://user:pass@db/app") == "postgresql+psycopg://user:pass@db/app"
    assert normalize_database_url("postgresql://user:pass@db/app") == "postgresql+psycopg://user:pass@db/app"
    assert normalize_database_url("postgresql+asyncpg://user:pass@db/app") == "postgresql+psycopg://user:pass@db/app"


def test_create_store_initializes_postgresql_engine() -> None:
    store = create_store("postgresql://user:pass@db/app")
    assert store.engine.dialect.name == "postgresql"
    store.engine.dispose()
