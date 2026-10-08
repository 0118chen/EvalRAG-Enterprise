import asyncio
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.routes.documents as documents_route
from app.api.routes.documents import UPLOAD_CHUNK_SIZE, _spool_upload
from app.config import Settings
from app.core.storage import ObjectStoreError
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


def test_upload_is_spooled_in_bounded_chunks() -> None:
    payload = b"a" * (UPLOAD_CHUNK_SIZE * 2 + 17)
    source = RecordingUploadFile(payload)

    spool = asyncio.run(_spool_upload(source, limit=len(payload)))

    try:
        # The object store needs a seekable body, so the spool is rewound for it.
        assert spool.read() == payload
    finally:
        spool.close()
    # Every read is bounded, so the payload is never held in memory as a whole.
    assert set(source.reads) == {UPLOAD_CHUNK_SIZE}
    assert len(source.reads) == 4


def test_oversize_upload_stops_reading_once_the_limit_is_exceeded() -> None:
    payload = b"a" * (UPLOAD_CHUNK_SIZE * 4)
    source = RecordingUploadFile(payload)

    with pytest.raises(HTTPException) as error:
        asyncio.run(_spool_upload(source, limit=UPLOAD_CHUNK_SIZE + 1))

    assert error.value.status_code == 413
    # Reading stops mid-stream instead of after buffering the whole payload.
    assert source.offset <= UPLOAD_CHUNK_SIZE * 2 < len(payload)


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(_spool_upload(RecordingUploadFile(b""), limit=10))

    assert error.value.status_code == 400


def _stored_objects(tmp_path) -> list[str]:
    """Every object in the local store, as a key, so assertions read like the store does."""
    root = tmp_path / "data" / "uploads"
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


class RedirectingStore:
    """Stands in for S3/MinIO where the only interesting part is the URL that gets signed."""

    def __init__(self, *, fail_put: bool = False) -> None:
        self.signed: list[str] = []
        self.fail_put = fail_put

    def put(self, key: str, source: BinaryIO) -> int:
        if self.fail_put:
            raise ObjectStoreError("minio refused")
        return len(source.read())

    def get(self, key: str) -> bytes:
        raise AssertionError("the API should let the browser fetch the bytes, not read them")

    def delete(self, key: str) -> None:
        self.signed = [entry for entry in self.signed if not entry.startswith(key)]

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        self.signed.append(f"{key}?expires={expires_seconds}")
        return f"https://minio.test/{key}?signature=abc"


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


def _upload(client, filename: str = "policy.txt", payload: bytes = b"policy text"):
    return client.post(
        "/api/v1/documents",
        data={"tenant_id": "tenant", "knowledge_base_id": "kb", "version": "v2"},
        files={"file": (filename, payload, "text/plain")},
    )


def test_api_rejects_oversized_upload_and_stores_nothing(tmp_path, monkeypatch) -> None:
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client, "policy.txt", b"a" * (1024 * 1024 + 1))

    assert response.status_code == 413
    assert _stored_objects(tmp_path) == []
    assert client.get("/api/v1/knowledge-bases/kb/documents?tenant_id=tenant").json() == []
    assert queued == []


def test_api_stores_the_upload_under_a_derived_key_and_queues_processing(
    tmp_path, monkeypatch
) -> None:
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client)

    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "pending"
    assert document["version"] == "v2"
    assert queued == [document["id"]]

    # The key is derived from the row (id + filename), so no schema change was needed to
    # move uploads into an object store.
    assert _stored_objects(tmp_path) == [f"documents/{document['id']}/policy.txt"]
    root = tmp_path / "data" / "uploads"
    assert (root / "documents" / document["id"] / "policy.txt").read_bytes() == b"policy text"


def test_a_filename_cannot_walk_out_of_the_store_root(tmp_path, monkeypatch) -> None:
    """The client picks the filename, and the filename is part of the key."""
    client, _ = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client, "../../escape.txt")

    assert response.status_code == 201
    stored = _stored_objects(tmp_path)
    assert stored == [f"documents/{response.json()['id']}/.._.._escape.txt"]
    assert not (tmp_path / "escape.txt").exists()


def test_api_reports_503_when_the_object_store_refuses_the_write(tmp_path, monkeypatch) -> None:
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)
    monkeypatch.setattr(
        client.app.state.container, "storage", RedirectingStore(fail_put=True)
    )

    response = _upload(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "document could not be stored"
    assert queued == []


def test_content_route_returns_the_stored_bytes(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch, max_upload_mb=1)
    document_id = _upload(client).json()["id"]

    response = client.get(f"/api/v1/documents/{document_id}/content?tenant_id=tenant")

    assert response.status_code == 200
    assert response.content == b"policy text"
    assert "policy.txt" in response.headers["content-disposition"]


def test_content_route_redirects_to_a_presigned_url(tmp_path, monkeypatch) -> None:
    """With a signing backend the API answers with a URL and never touches the bytes."""
    client, _ = _client(tmp_path, monkeypatch, max_upload_mb=1)
    document_id = _upload(client).json()["id"]
    store = RedirectingStore()
    monkeypatch.setattr(client.app.state.container, "storage", store)

    response = client.get(
        f"/api/v1/documents/{document_id}/content?tenant_id=tenant",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"https://minio.test/documents/{document_id}/policy.txt?signature=abc"
    )
    # The URL is minted for this one object and expires with the configured window.
    assert store.signed == [f"documents/{document_id}/policy.txt?expires=300"]


def test_content_route_reports_a_missing_object(tmp_path, monkeypatch) -> None:
    client, _ = _client(tmp_path, monkeypatch, max_upload_mb=1)
    document_id = _upload(client).json()["id"]
    stored = tmp_path / "data" / "uploads" / "documents" / document_id / "policy.txt"
    stored.unlink()

    response = client.get(f"/api/v1/documents/{document_id}/content?tenant_id=tenant")

    assert response.status_code == 404
    assert response.json()["detail"] == "document content not found"


@pytest.mark.parametrize(
    "filename",
    ["scan.jpg", "screenshot.png", "old.doc", "table.xls", "bundle.zip"],
)
def test_api_rejects_file_types_it_has_no_parser_for(tmp_path, monkeypatch, filename) -> None:
    """Regression guard: the whitelist is the contract, and it is extension-only."""
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client, filename, b"payload")

    assert response.status_code == 400
    assert response.json()["detail"] == "supported file types: pdf, docx, html, xlsx, txt, md"
    assert queued == []
    assert _stored_objects(tmp_path) == []


@pytest.mark.parametrize("filename", ["page.html", "page.htm"])
def test_api_accepts_html_documents(tmp_path, monkeypatch, filename) -> None:
    """Web-only regulations used to be rejected with 400; they are content."""
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client, filename, b"<html><body><p>first</p></body></html>")

    assert response.status_code == 201
    assert queued == [response.json()["id"]]


def test_api_accepts_spreadsheets(tmp_path, monkeypatch) -> None:
    """A budget table is a supported upload; the legacy .xls is not."""
    client, queued = _client(tmp_path, monkeypatch, max_upload_mb=1)

    response = _upload(client, "table.xlsx", b"PK\x03\x04payload")

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

    response = _upload(client, "scan.pdf", b"%PDF-1.7 scanned")

    assert response.status_code == 201
    assert queued == [response.json()["id"]]
