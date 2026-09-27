import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.routes.documents as documents_route
from app.api.routes.documents import UPLOAD_CHUNK_SIZE, _stream_upload_to_disk
from app.config import Settings
from app.main import create_app
from app.schemas import KnowledgeBase


class RecordingUploadFile:
    """UploadFile stand-in that records the size requested by every read."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.reads: list[int | None] = []

    async def read(self, size: int | None = -1) -> bytes:
        self.reads.append(size)
        end = len(self.payload) if size in (None, -1) else self.offset + size
        chunk = self.payload[self.offset : end]
        self.offset += len(chunk)
        return chunk


def test_upload_is_written_in_bounded_chunks(tmp_path) -> None:
    payload = b"a" * (UPLOAD_CHUNK_SIZE * 2 + 17)
    source = RecordingUploadFile(payload)
    target = tmp_path / "doc_policy.txt"

    written = asyncio.run(_stream_upload_to_disk(source, target, limit=len(payload)))

    assert written == len(payload)
    assert target.read_bytes() == payload
    # Every read is bounded, so the payload is never held in memory as a whole.
    assert set(source.reads) == {UPLOAD_CHUNK_SIZE}
    assert len(source.reads) == 4
    assert list(tmp_path.glob("*.part")) == []


def test_oversize_upload_stops_reading_once_the_limit_is_exceeded(tmp_path) -> None:
    payload = b"a" * (UPLOAD_CHUNK_SIZE * 4)
    source = RecordingUploadFile(payload)
    target = tmp_path / "doc_policy.txt"

    with pytest.raises(HTTPException) as error:
        asyncio.run(_stream_upload_to_disk(source, target, limit=UPLOAD_CHUNK_SIZE + 1))

    assert error.value.status_code == 413
    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []
    # Reading stops mid-stream instead of after buffering the whole payload.
    assert source.offset <= UPLOAD_CHUNK_SIZE * 2 < len(payload)


def test_empty_upload_is_rejected(tmp_path) -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            _stream_upload_to_disk(RecordingUploadFile(b""), tmp_path / "x.txt", limit=10)
        )

    assert error.value.status_code == 400
    assert list(tmp_path.iterdir()) == []


def _client(tmp_path, monkeypatch, *, max_upload_mb: int):
    monkeypatch.chdir(tmp_path)
    app = create_app(
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'uploads.db').as_posix()}",
            max_upload_mb=max_upload_mb,
        )
    )
    app.state.container.store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="policy", description="")
    )
    queued: list[str] = []
    monkeypatch.setattr(
        documents_route,
        "process_document",
        SimpleNamespace(delay=lambda document_id: queued.append(document_id)),
    )
    return TestClient(app), queued


def test_api_rejects_oversized_upload_and_leaves_nothing_on_disk(tmp_path, monkeypatch) -> None:
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
        files={"file": ("policy.txt", b"a" * (1024 * 1024 + 1), "text/plain")},
    )

    assert response.status_code == 413
    assert list((tmp_path / "data" / "uploads").glob("*")) == []
    assert client.get("/api/v1/knowledge-bases/kb/documents?tenant_id=tenant").json() == []
    assert queued == []


def test_api_streams_upload_to_disk_and_queues_processing(tmp_path, monkeypatch) -> None:
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb", "version": "v2"},
        files={"file": ("policy.txt", b"policy text", "text/plain")},
    )

    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "pending"
    assert document["version"] == "v2"
    assert queued == [document["id"]]

    upload_dir = tmp_path / "data" / "uploads"
    assert [path.name for path in upload_dir.iterdir()] == [
        f"{document['id']}_policy.txt"
    ]
    assert (upload_dir / f"{document['id']}_policy.txt").read_bytes() == b"policy text"


@pytest.mark.parametrize(
    "filename",
    ["scan.jpg", "screenshot.png", "old.doc", "table.xls", "table.xlsx", "bundle.zip"],
)
def test_api_rejects_file_types_it_has_no_parser_for(tmp_path, monkeypatch, filename) -> None:
    """Regression guard: the whitelist is the contract, and it is extension-only."""
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
        files={"file": (filename, b"payload", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "supported file types: pdf, docx, html, txt, md"
    assert queued == []
    upload_dir = tmp_path / "data" / "uploads"
    assert not upload_dir.exists() or list(upload_dir.glob("*")) == []


@pytest.mark.parametrize("filename", ["page.html", "page.htm"])
def test_api_accepts_html_documents(tmp_path, monkeypatch, filename) -> None:
    """Web-only regulations used to be rejected with 400; they are content."""
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
        files={"file": (filename, "<html><body><p>第一条</p></body></html>", "text/html")},
    )

    assert response.status_code == 201
    assert queued == [response.json()["id"]]


def test_api_accepts_a_scanned_pdf_and_leaves_the_verdict_to_the_worker(
    tmp_path, monkeypatch
) -> None:
    """Extension says PDF, content says picture.

    The upload check cannot look inside the file, so an image-only PDF is queued
    and the worker is the layer that fails it - with a reason the client can read.
    """
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
        files={"file": ("scan.pdf", b"%PDF-1.7 scanned", "application/pdf")},
    )

    assert response.status_code == 201
    assert queued == [response.json()["id"]]
