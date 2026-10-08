"""Where uploaded documents live, behind one interface.

The API used to write uploads to ``data/uploads`` and the Celery worker read them back from
that same directory. The arrangement works on a laptop and quietly breaks in production: the
two processes only agree on the file if they share a disk, so a second worker on another host
fails with "no such file" for a document that is in the database, and a container restart
between the upload and the task loses the bytes while the row still says "queued".

Everything downstream now asks for an ``ObjectStore``. ``local`` keeps the laptop story
(one directory, no services); ``s3`` points the same contract at MinIO/S3, which is what makes
the API and the worker independently scalable and lets the API answer a download with a
short-lived presigned URL instead of proxying every byte through itself.
"""

import os
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from urllib.parse import quote

from app.config import Settings

# How much of an object is moved per read, for the filesystem backend.
COPY_CHUNK_SIZE = 1024 * 1024

# Objects are addressed by document id, so a key can be rebuilt from the database row rather
# than stored in a new column.
UPLOAD_PREFIX = "documents"


class ObjectStoreError(RuntimeError):
    """The object store could not do what it was asked."""


class ObjectMissing(ObjectStoreError):
    """The key does not exist. Distinct from a backend failure: the caller answers 404."""


class PresignUnsupported(ObjectStoreError):
    """This backend has no HTTP surface that can be handed to a browser."""


def _safe_name(filename: str) -> str:
    """Reduce a client-supplied filename to something safe to put in a key.

    The name is part of the object key, so a filename like ``../../etc/passwd`` must not be
    able to walk out of the prefix. Anything that is not a word character, a dot or a dash
    becomes an underscore (same rule the upload route always used).
    """
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_" for character in filename
    )
    return cleaned or "document.txt"


def document_key(document_id: str, filename: str) -> str:
    """The object key for one uploaded document.

    Deriving the key instead of storing it keeps the schema untouched: every existing row
    already carries the id and the filename the key is built from.
    """
    return f"{UPLOAD_PREFIX}/{document_id}/{_safe_name(filename)}"


def _body_size(source: BinaryIO) -> int:
    """Length of ``source``, leaving its read position untouched.

    ``put_object`` streams the body and reports no size, but callers want the byte count for
    logs and for the upload limit; the position is restored so the backend can then send the
    whole object.
    """
    if not source.seekable():
        raise ObjectStoreError("the object store needs a seekable body")
    position = source.tell()
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(position)
    return size


class ObjectStore(Protocol):
    """What the rest of the application is allowed to assume about storage.

    ``put`` takes a binary file rather than bytes on purpose: an upload must not have to be
    resident in memory before it can be stored.
    """

    def put(self, key: str, source: BinaryIO) -> int:
        """Store ``source`` under ``key``; return how many bytes were stored."""
        ...

    def get(self, key: str) -> bytes:
        """Return the object, or raise ``ObjectMissing``."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object. Deleting something that is not there is not an error."""
        ...

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        """Return a URL the client can fetch directly, or raise ``PresignUnsupported``."""
        ...


class LocalObjectStore:
    """The default backend: one directory, no service, same contract.

    The directory is the old ``data/uploads`` path, but the layout inside it is the key
    layout, so a deployment that switches to S3 later moves files one-for-one.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        target = (root / key).resolve()
        if target == root or not target.is_relative_to(root):
            raise ObjectStoreError(f"object key escapes the store root: {key!r}")
        return target

    def put(self, key: str, source: BinaryIO) -> int:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a crash mid-copy cannot leave a truncated
        # object that a later read would happily hand out as the whole document.
        temporary = target.with_name(f"{target.name}.part")
        size = 0
        try:
            with temporary.open("wb") as handle:
                while chunk := source.read(COPY_CHUNK_SIZE):
                    handle.write(chunk)
                    size += len(chunk)
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return size

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise ObjectMissing(key) from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        raise PresignUnsupported("the local object store cannot hand out URLs")


# Error codes S3-compatible backends report for a missing key.
MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") in MISSING_CODES or status == 404


class S3ObjectStore:
    """S3 or anything that speaks its API. The client is injected so tests need no service.

    ``presign_client`` is the second, optional client: a signature covers the ``Host`` header,
    so a URL minted against the internal endpoint is only valid for that endpoint. When the
    browser reaches the object store by another name, the URL has to be signed for the name the
    browser will use.
    """

    def __init__(self, client: Any, bucket: str, presign_client: Any | None = None) -> None:
        self._client = client
        self._presign_client = presign_client or client
        self.bucket = bucket

    def put(self, key: str, source: BinaryIO) -> int:
        size = _body_size(source)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=source)
        return size

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                raise ObjectMissing(key) from exc
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            # The body is a live HTTP stream: leaving it open leaks a connection back to the
            # pool for as long as the garbage collector takes to notice.
            body.close()

    def delete(self, key: str) -> None:
        # S3 deletes are already idempotent, so no existence check is needed.
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


def _build_s3_client(settings: Settings, *, endpoint_url: str | None = None) -> Any:
    # Imported here so a local-only deployment never pays for (or needs) the SDK at startup.
    import boto3
    from botocore.config import Config

    addressing_style = "path" if settings.s3_path_style else "auto"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(s3={"addressing_style": addressing_style}),
    )


def create_object_store(settings: Settings) -> ObjectStore:
    """Pick the backend from configuration.

    Anything other than an explicit ``s3`` stays on disk: a typo in an environment variable
    should not silently send production uploads somewhere else.
    """
    if settings.object_store.strip().lower() == "s3":
        presign_client = (
            _build_s3_client(settings, endpoint_url=settings.s3_public_endpoint_url)
            if settings.s3_public_endpoint_url
            else None
        )
        return S3ObjectStore(
            _build_s3_client(settings),
            settings.s3_bucket,
            presign_client,
        )
    return LocalObjectStore(settings.object_store_local_dir)


def download_filename(filename: str) -> str:
    """RFC 5987 ``Content-Disposition`` value for a filename that may not be ASCII."""
    return f"attachment; filename*=UTF-8''{quote(filename)}"
