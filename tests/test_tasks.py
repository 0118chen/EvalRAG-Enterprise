from types import SimpleNamespace

from app import tasks
from app.tasks import process_document


def test_document_task_contract() -> None:
    result = process_document.run("doc-1") if hasattr(process_document, "run") else process_document("doc-1")
    assert result["document_id"] == "doc-1"


def test_document_worker_uses_configured_index_pipeline(monkeypatch, tmp_path) -> None:
    calls = []

    class FakeStore:
        def get_document_any(self, document_id):
            return SimpleNamespace(
                id=document_id,
                status="processing",
                knowledge_base_id="kb-policy",
                version="v3",
                filename="policy.txt",
            )

        def update_document_progress(self, *args):
            pass

        def replace_chunks(self, *args):
            pass

        def update_document_status(self, *args):
            pass

    class FakePipeline:
        async def replace_document(self, chunks, knowledge_base_id, document_id):
            calls.append((knowledge_base_id, document_id, len(chunks)))
            return len(chunks)

    upload_dir = tmp_path / "data" / "uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "doc-1_policy.txt").write_text("policy text", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "create_store", lambda database_url: FakeStore())
    monkeypatch.setattr(
        tasks,
        "create_ingestion_pipeline",
        lambda settings: FakePipeline(),
        raising=False,
    )

    function = process_document.run if hasattr(process_document, "run") else process_document
    result = function("doc-1")

    assert result["status"] == "ready"
    assert calls == [("kb-policy", "doc-1", 1)]


def test_force_reindex_processes_ready_document(monkeypatch, tmp_path) -> None:
    calls = []

    class FakeStore:
        def get_document_any(self, document_id):
            return SimpleNamespace(
                id=document_id,
                status="ready",
                knowledge_base_id="kb-policy",
                version="v3",
                filename="policy.txt",
            )

        def update_document_progress(self, *args):
            pass

        def replace_chunks(self, *args):
            pass

        def update_document_status(self, *args):
            pass

    class FakePipeline:
        async def replace_document(self, chunks, knowledge_base_id, document_id):
            calls.append(document_id)
            return len(chunks)

    upload_dir = tmp_path / "data" / "uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "ready-doc_policy.txt").write_text("policy text", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "create_store", lambda database_url: FakeStore())
    monkeypatch.setattr(tasks, "create_ingestion_pipeline", lambda settings: FakePipeline())

    function = process_document.run if hasattr(process_document, "run") else process_document
    result = function("ready-doc", force=True)

    assert result["stage"] == "indexed"
    assert calls == ["ready-doc"]
