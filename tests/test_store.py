from pathlib import Path

from app.core.store import SQLiteStore
from app.schemas import Document, KnowledgeBase


def test_sqlite_persists_knowledge_base(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "test.db"))
    kb = KnowledgeBase(id="kb-1", tenant_id="tenant-a", name="Policies", description="")
    store.save_knowledge_base(kb)
    assert store.get_knowledge_base("kb-1", "tenant-a") == kb
    assert store.get_knowledge_base("kb-1", "tenant-b") is None

