from datetime import timedelta

import pytest

from app import tasks
from app.core.embeddings import HashEmbedding
from app.core.pipeline import IngestionPipeline
from app.core.store import SQLiteStore
from app.db.models import utc_now
from app.schemas import Document, KnowledgeBase
from app.tasks import process_document

KB = "kb-policy"
DOC = "doc-1"
VERSION = "v3"


class RecordingIndexer:
    """Stand-in for an external chunk indexer that records what it received."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list, tuple]] = []
        self.deleted: list[str] = []
        self.fail = fail

    async def upsert(self, chunks, *args):
        if self.fail:
            raise RuntimeError("external index unavailable")
        self.calls.append((list(chunks), args))
        return len(chunks)

    async def delete_document(self, document_id: str) -> int:
        self.deleted.append(document_id)
        return 0


def _run(document_id: str, force: bool = False) -> dict:
    function = process_document.run if hasattr(process_document, "run") else process_document
    return function(document_id, force=force)


def _seed(tmp_path, monkeypatch, *, status: str = "pending") -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "tasks.db"))
    store.save_knowledge_base(
        KnowledgeBase(id=KB, tenant_id="tenant-1", name="policy", description="")
    )
    store.save_document(
        Document(
            id=DOC,
            filename="policy.txt",
            knowledge_base_id=KB,
            chunks=0,
            status=status,
            version=VERSION,
        ),
        [],
    )
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / f"{DOC}_policy.txt").write_text(
        "农户贷款应遵循依法合规、审慎经营的原则。",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "create_store", lambda database_url: store)
    return store


def _pipeline(dense, sparse) -> IngestionPipeline:
    return IngestionPipeline(
        local_index=None,
        dense_indexer=dense,
        sparse_indexer=sparse,
        embedding=HashEmbedding(4),
    )


def _blank_page_pdf() -> bytes:
    """A PDF whose page has no text layer - what a scan looks like to the extractor."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(40, 40, 400, 700), color=(0, 0, 0), width=2)
    payload = document.tobytes()
    document.close()
    return payload


def _pdf_with_one_text_page() -> bytes:
    """Same shape, but at least one page carries selectable text."""
    import pymupdf

    document = pymupdf.open()
    document.new_page()  # page 1: no text
    second = document.new_page()
    second.insert_text((72, 100), "nong hu dai kuan guan li", fontsize=12)
    payload = document.tobytes()
    document.close()
    return payload


def _replace_upload(tmp_path, name: str, payload: bytes) -> None:
    uploads = tmp_path / "data" / "uploads"
    for existing in uploads.glob(f"{DOC}_*"):
        existing.unlink()
    (uploads / f"{DOC}_{name}").write_bytes(payload)


def test_worker_writes_every_configured_backend_and_the_database(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )

    result = _run(DOC)

    assert result == {
        "document_id": DOC,
        "status": "ready",
        "stage": "indexed",
        "progress": "100",
    }
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "ready"
    assert document.chunks == 1

    chunks = store.get_chunks(KB, VERSION)
    assert [chunk.version for chunk in chunks] == [VERSION]

    # Replacing a document clears previous rows in both external indexes first.
    assert dense.deleted == [DOC]
    assert sparse.deleted == [DOC]

    dense_chunks, dense_args = dense.calls[0]
    assert [chunk.id for chunk in dense_chunks] == [chunk.id for chunk in chunks]
    vectors, dense_knowledge_base = dense_args
    assert dense_knowledge_base == KB
    assert [len(vector) for vector in vectors] == [4]

    sparse_chunks, sparse_args = sparse.calls[0]
    assert [chunk.id for chunk in sparse_chunks] == [chunk.id for chunk in chunks]
    assert sparse_args == (KB,)


def test_concurrent_delivery_cannot_reindex_a_claimed_document(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )

    # A first worker already owns the document.
    assert store.claim_document(DOC) is True

    result = _run(DOC)

    assert result["stage"] == "not_claimed"
    assert result["status"] == "processing"
    assert dense.calls == []
    assert sparse.calls == []


def test_claim_is_atomic_and_ready_documents_need_force(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch, status="ready")

    assert store.claim_document(DOC) is False
    assert store.claim_document(DOC, force=True) is True
    assert store.claim_document(DOC, force=True) is False


def test_stale_processing_document_can_be_reclaimed(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch, status="processing")

    assert store.claim_document(DOC, stale_after_seconds=900) is False
    assert store.claim_document(DOC, now=utc_now() + timedelta(seconds=901)) is True


def test_external_index_failure_keeps_document_failed_and_db_unchanged(
    tmp_path, monkeypatch
) -> None:
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer(fail=True)
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )

    with pytest.raises(RuntimeError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "failed"
    assert document.error_message == "external index unavailable"
    # Local chunks are only written after every external write succeeded.
    assert store.get_chunks(KB, VERSION) == []


def test_scanned_file_without_a_text_layer_fails_instead_of_indexing_nothing(
    tmp_path, monkeypatch
) -> None:
    """A scan passes the upload extension check but extracts to nothing.

    Indexing zero chunks silently is the worst outcome: the document reports
    success and can never be retrieved.
    """
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )
    _replace_upload(tmp_path, "scan.pdf", _blank_page_pdf())

    with pytest.raises(tasks.EmptyExtractionError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "failed"
    assert "characters" in (document.error_message or "")
    assert store.get_chunks(KB, VERSION) == []
    # Nothing was indexed anywhere, and no progress was reported for a success path.
    assert dense.calls == []
    assert sparse.calls == []
    assert document.chunks == 0


def test_empty_extraction_is_not_retried_by_the_queue() -> None:
    if tasks.celery_app is None:
        pytest.skip("celery is not installed in this environment")
    assert tasks.EmptyExtractionError in tuple(tasks.process_document.dont_autoretry_for)


def test_a_partly_machine_readable_document_is_still_indexed(
    tmp_path, monkeypatch
) -> None:
    """Regression guard: one blank page must not fail a document that has text."""
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )
    _replace_upload(tmp_path, "mixed.pdf", _pdf_with_one_text_page())

    result = _run(DOC)

    assert result["status"] == "ready"
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "ready"
    assert len(store.get_chunks(KB, VERSION)) == 1
