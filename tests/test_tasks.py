from datetime import timedelta

import pytest

from app import tasks
from app.core.embeddings import HashEmbedding
from app.core.ingestion import Chunk
from app.core.ocr import OcrUnavailableError
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


POLICY_TEXT = "农户贷款应遵循依法合规、审慎经营的原则。".encode()


def _seed(
    tmp_path,
    monkeypatch,
    *,
    status: str = "pending",
    filename: str = "policy.txt",
    payload: bytes | None = None,
) -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "tasks.db"))
    store.save_knowledge_base(
        KnowledgeBase(id=KB, tenant_id="tenant-1", name="policy", description="")
    )
    store.save_document(
        Document(
            id=DOC,
            filename=filename,
            knowledge_base_id=KB,
            chunks=0,
            status=status,
            version=VERSION,
        ),
        [],
    )
    _store_upload(tmp_path, filename, POLICY_TEXT if payload is None else payload)
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


def _store_upload(tmp_path, filename: str, payload: bytes) -> None:
    """Put the document's bytes where the object store will look for them.

    The key is derived from the row (document id + filename), which is exactly what the
    worker rebuilds from the database, so the test cannot accidentally seed a file the
    worker would never find.
    """
    target = tmp_path / "data" / "uploads" / "documents" / DOC
    target.mkdir(parents=True, exist_ok=True)
    (target / filename).write_bytes(payload)


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


def test_external_index_failure_leaves_a_replayable_row_behind(tmp_path, monkeypatch) -> None:
    """The chunks and the intent to publish them survive an index outage.

    The old shape wrote the external index first and the local chunks second, so a failure
    in between left a document that said "failed" with no trace of the work. Now the local
    write commits before any external call, and the outbox row records what is still owed.
    """
    store = _seed(tmp_path, monkeypatch)
    dense, sparse = RecordingIndexer(), RecordingIndexer(fail=True)
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )

    with pytest.raises(RuntimeError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    # Not "failed": the row still has retry budget, so the document is late, not lost.
    assert document.status == "processing"
    assert document.error_message == "external index unavailable"
    assert [chunk.document_id for chunk in store.get_chunks(KB, VERSION)] == [DOC]

    job = store.get_index_job(DOC)
    assert job is not None
    assert job.status == "pending"
    assert job.attempts == 1
    assert job.last_error == "external index unavailable"


def test_a_parked_index_row_marks_the_document_failed(tmp_path, monkeypatch) -> None:
    """Once the retry budget is spent the document stops pretending it is only late."""
    store = _seed(tmp_path, monkeypatch, status="processing")
    chunk = Chunk(id=f"{DOC}:0", document_id=DOC, page=1, text="农户贷款", version=VERSION)
    store.save_document_index(DOC, KB, [chunk])
    monkeypatch.setattr(
        tasks,
        "create_ingestion_pipeline",
        lambda settings: _pipeline(RecordingIndexer(), RecordingIndexer(fail=True)),
    )

    for expected_attempts in (1, 2, store.INDEX_JOB_MAX_ATTEMPTS):
        with pytest.raises(RuntimeError):
            tasks._sync_document_index(store, DOC, due_after_seconds=0)
        job = store.get_index_job(DOC)
        assert job is not None
        assert job.attempts == expected_attempts

    assert job.status == "failed"
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "failed"
    assert document.error_message == "external index unavailable"


def test_a_scanned_file_is_not_indexed_and_is_reported_as_needing_ocr(
    tmp_path, monkeypatch
) -> None:
    """A scan passes the upload extension check but extracts to nothing.

    Indexing zero chunks silently is the worst outcome: the document reports
    success and can never be retrieved.
    """
    store = _seed(tmp_path, monkeypatch, filename="scan.pdf", payload=_blank_page_pdf())
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )
    monkeypatch.setattr(tasks, "create_ocr_backend", lambda settings: None)

    with pytest.raises(tasks.EmptyExtractionError):
        _run(DOC)

    # The category stays "nothing was extracted"; the status says who can fix it.
    assert issubclass(tasks.NeedsOcrError, tasks.EmptyExtractionError)
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "needs_ocr"
    assert "0 extractable characters" in (document.error_message or "")
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
    store = _seed(tmp_path, monkeypatch, filename="mixed.pdf", payload=_pdf_with_one_text_page())
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )

    result = _run(DOC)

    assert result["status"] == "ready"
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "ready"
    assert len(store.get_chunks(KB, VERSION)) == 1


class FakeOcr:
    """An OCR backend that returns whatever the test tells it to."""

    def __init__(self, pages: list[str] | None = None, error: Exception | None = None) -> None:
        self.pages = pages or []
        self.error = error
        self.calls: list[str] = []

    def extract(self, filename: str, payload: bytes) -> list[tuple[int, str]]:
        self.calls.append(filename)
        if self.error is not None:
            raise self.error
        return list(enumerate(self.pages, start=1))


def test_a_scan_without_an_ocr_backend_asks_for_one_instead_of_failing(
    tmp_path, monkeypatch
) -> None:
    """Failing is for files nothing can read; a scan with no engine configured is
    a deployment gap, and the status should send someone to fix it."""
    store = _seed(tmp_path, monkeypatch, filename="scan.pdf", payload=_blank_page_pdf())
    monkeypatch.setattr(tasks, "create_ocr_backend", lambda settings: None)

    with pytest.raises(tasks.NeedsOcrError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "needs_ocr"
    assert "OCR_BACKEND" in (document.error_message or "")
    assert store.get_chunks(KB, VERSION) == []


def test_ocr_text_is_indexed_when_a_backend_is_configured(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch, filename="scan.pdf", payload=_blank_page_pdf())
    dense, sparse = RecordingIndexer(), RecordingIndexer()
    monkeypatch.setattr(
        tasks, "create_ingestion_pipeline", lambda settings: _pipeline(dense, sparse)
    )
    backend = FakeOcr(["第一条 扫描件上的文字应当进入索引。"])
    monkeypatch.setattr(tasks, "create_ocr_backend", lambda settings: backend)

    result = _run(DOC)

    assert result["status"] == "ready"
    assert result["stage"] == "indexed"
    # The worker hands the backend the filename on the row, which is what the key is built
    # from, so the extractor routes by the same extension the uploader chose.
    assert backend.calls and backend.calls[0].endswith("scan.pdf")
    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "ready"
    chunks = store.get_chunks(KB, VERSION)
    assert chunks and "扫描件上的文字" in chunks[0].text
    assert dense.calls and sparse.calls


def test_an_unavailable_engine_sends_the_document_to_a_human(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch, filename="scan.pdf", payload=_blank_page_pdf())
    backend = FakeOcr(error=OcrUnavailableError("tesseract binary not found: tesseract"))
    monkeypatch.setattr(tasks, "create_ocr_backend", lambda settings: backend)

    with pytest.raises(tasks.NeedsOcrError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "needs_ocr"
    assert "tesseract binary not found" in (document.error_message or "")


def test_ocr_that_finds_nothing_also_needs_a_human(tmp_path, monkeypatch) -> None:
    store = _seed(tmp_path, monkeypatch, filename="scan.pdf", payload=_blank_page_pdf())
    backend = FakeOcr(["   "])
    monkeypatch.setattr(tasks, "create_ocr_backend", lambda settings: backend)

    with pytest.raises(tasks.NeedsOcrError):
        _run(DOC)

    document = store.get_document_any(DOC)
    assert document is not None
    assert document.status == "needs_ocr"
    assert "no text" in (document.error_message or "")


def test_needs_ocr_is_not_retried_by_the_queue() -> None:
    if tasks.celery_app is None:
        pytest.skip("celery is not installed in this environment")
    assert tasks.NeedsOcrError in tuple(tasks.process_document.dont_autoretry_for)
