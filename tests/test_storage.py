"""The object store contract: derived keys, whole-object writes, and both backends.

These tests never need a service: the filesystem backend uses ``tmp_path`` and the S3 backend
takes an injected client, which is why ``S3ObjectStore`` does not build its own.
"""

from io import BytesIO

import pytest
from botocore.exceptions import ClientError

import app.core.storage as storage_module
from app.config import Settings
from app.core.storage import (
    LocalObjectStore,
    ObjectMissing,
    ObjectStoreError,
    PresignUnsupported,
    S3ObjectStore,
    create_object_store,
    document_key,
    download_filename,
)


class FakeS3Client:
    """Records the exact calls boto3 would have made."""

    def __init__(self, *, error: Exception | None = None, body: bytes = b"stored") -> None:
        self.calls: list[tuple] = []
        self.error = error
        self.body = body

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.error is not None:
            raise self.error
        return {"Body": BytesIO(self.body)}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(("presign", operation, Params, ExpiresIn))
        return f"https://minio.test/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


class Unseekable:
    """A body that cannot be measured, like a raw network stream."""

    def seekable(self) -> bool:
        return False


def test_keys_are_derived_from_the_document_id_and_the_filename() -> None:
    assert document_key("doc-1", "policy.txt") == "documents/doc-1/policy.txt"
    assert document_key("doc-1", "借款规定 2024.md") == "documents/doc-1/借款规定_2024.md"


def test_a_client_filename_cannot_escape_the_document_prefix() -> None:
    assert document_key("doc-1", "../../etc/passwd") == "documents/doc-1/.._.._etc_passwd"


def test_an_empty_filename_still_produces_a_usable_key() -> None:
    assert document_key("doc-1", "") == "documents/doc-1/document.txt"


def test_local_store_round_trips_bytes_and_reports_the_size(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    key = document_key("doc-1", "policy.txt")

    written = store.put(key, BytesIO(b"policy text"))

    assert written == len(b"policy text")
    assert store.get(key) == b"policy text"
    stored = tmp_path / "objects" / "documents" / "doc-1" / "policy.txt"
    assert stored.read_bytes() == b"policy text"
    # The temporary file used during the write is gone: no half-written object survives.
    assert list((tmp_path / "objects").rglob("*.part")) == []


def test_local_store_overwrites_an_object_wholesale(tmp_path) -> None:
    """Re-indexing a changed document must not leave the tail of the old one behind."""
    store = LocalObjectStore(tmp_path / "objects")
    key = "documents/doc-1/policy.txt"
    store.put(key, BytesIO(b"a" * 32))

    store.put(key, BytesIO(b"bb"))

    assert store.get(key) == b"bb"


def test_local_store_reports_a_missing_object(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(ObjectMissing):
        store.get("documents/doc-1/policy.txt")


def test_deleting_an_object_that_is_not_there_is_not_an_error(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    store.delete("documents/doc-1/policy.txt")


def test_local_store_refuses_a_key_that_escapes_its_root(tmp_path) -> None:
    """Keys come from client filenames, so the backend is the last line of defence."""
    store = LocalObjectStore(tmp_path / "objects")
    victim = tmp_path / "escape.txt"

    for key in ("../escape.txt", "documents/../../escape.txt", ""):
        with pytest.raises(ObjectStoreError):
            store.put(key, BytesIO(b"owned"))

    assert not victim.exists()


def test_local_store_cannot_hand_out_urls(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(PresignUnsupported):
        store.presign_get("documents/doc-1/policy.txt", expires_seconds=300)


def test_local_is_the_default_backend_and_a_typo_does_not_change_that(tmp_path) -> None:
    """A misspelled backend name must not silently redirect production uploads."""
    for configured in ("local", "Local", "s4"):
        store = create_object_store(
            Settings(object_store=configured, object_store_local_dir=str(tmp_path / "objects"))
        )
        assert isinstance(store, LocalObjectStore)
        assert store.root == tmp_path / "objects"


def test_s3_backend_is_built_only_when_asked_for(tmp_path, monkeypatch) -> None:
    client = FakeS3Client()
    seen: list[Settings] = []

    def fake_client(settings: Settings, *, endpoint_url: str | None = None):
        seen.append(settings)
        return client

    monkeypatch.setattr(storage_module, "_build_s3_client", fake_client)
    settings = Settings(object_store="s3", s3_bucket="regulations")

    store = create_object_store(settings)

    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "regulations"
    assert seen == [settings]


def test_a_public_endpoint_gets_its_own_signing_client(monkeypatch) -> None:
    """A signature covers the Host header, so the URL must be signed for the browser's host."""
    clients = {"internal": FakeS3Client(), "public": FakeS3Client()}
    endpoints: list[str | None] = []

    def fake_client(settings: Settings, *, endpoint_url: str | None = None):
        # Mirror the real builder: an explicit endpoint wins, otherwise the configured one.
        endpoints.append(endpoint_url or settings.s3_endpoint_url)
        return clients["public" if endpoint_url else "internal"]

    monkeypatch.setattr(storage_module, "_build_s3_client", fake_client)
    settings = Settings(
        object_store="s3",
        s3_endpoint_url="http://minio:9000",
        s3_public_endpoint_url="https://objects.example.com",
    )

    store = create_object_store(settings)
    url = store.presign_get("documents/doc-1/policy.txt", expires_seconds=60)

    assert set(endpoints) == {"https://objects.example.com", "http://minio:9000"}
    assert url.startswith("https://minio.test/")
    # The internal client never signs; the public one never moves bytes.
    assert clients["internal"].calls == []
    assert clients["public"].calls[-1][0] == "presign"


def test_s3_put_sends_the_body_and_reports_its_size() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(client, "regulations")

    written = store.put("documents/doc-1/policy.txt", BytesIO(b"policy text"))

    assert written == len(b"policy text")
    operation, call = client.calls[0]
    assert operation == "put"
    assert call["Bucket"] == "regulations"
    assert call["Key"] == "documents/doc-1/policy.txt"
    # The stream is rewound before it is sent, so nothing is lost measuring it.
    assert call["Body"].read() == b"policy text"


def test_s3_put_needs_a_measurable_body() -> None:
    store = S3ObjectStore(FakeS3Client(), "regulations")

    with pytest.raises(ObjectStoreError):
        store.put("documents/doc-1/policy.txt", Unseekable())  # type: ignore[arg-type]


def test_s3_get_reads_the_body() -> None:
    client = FakeS3Client(body=b"policy text")
    store = S3ObjectStore(client, "regulations")

    assert store.get("documents/doc-1/policy.txt") == b"policy text"


def test_s3_get_maps_a_missing_key_to_object_missing() -> None:
    for code in ("NoSuchKey", "NotFound", "404"):
        client = FakeS3Client(error=_client_error(code, 404))
        store = S3ObjectStore(client, "regulations")

        with pytest.raises(ObjectMissing):
            store.get("documents/doc-1/policy.txt")


def test_s3_get_lets_a_real_backend_failure_through() -> None:
    """Only "there is no such object" is a 404; a broken backend must not look like one."""
    client = FakeS3Client(error=_client_error("AccessDenied", 403))
    store = S3ObjectStore(client, "regulations")

    with pytest.raises(ClientError):
        store.get("documents/doc-1/policy.txt")


def test_s3_delete_and_presign_use_the_configured_bucket_and_window() -> None:
    client = FakeS3Client()
    store = S3ObjectStore(client, "regulations")

    store.delete("documents/doc-1/policy.txt")
    url = store.presign_get("documents/doc-1/policy.txt", expires_seconds=120)

    assert url == (
        "https://minio.test/regulations/documents/doc-1/policy.txt?expires=120"
    )
    assert client.calls[-1] == (
        "presign",
        "get_object",
        {"Bucket": "regulations", "Key": "documents/doc-1/policy.txt"},
        120,
    )


def test_download_filename_is_encoded_so_a_client_cannot_break_the_header() -> None:
    value = download_filename("借款规定 2024.md")

    assert value.startswith("attachment; filename*=UTF-8''")
    assert "%E5%80%9F" in value
    assert value.endswith(".md")
    # The filename itself carries no space, quote or semicolon: whatever a client named its
    # upload, it cannot add a header of its own.
    encoded = value.split("''", 1)[1]
    assert not set(encoded) & set(' ;"\r\n')
