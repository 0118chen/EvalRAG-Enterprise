import asyncio
import logging
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse

from app.api.deps import authenticate, get_container, resolve_tenant_id
from app.core.concurrency import run_blocking
from app.core.ingestion import SUPPORTED_LABEL, SUPPORTED_SUFFIXES
from app.core.pipeline import create_ingestion_pipeline
from app.core.storage import (
    ObjectMissing,
    ObjectStore,
    ObjectStoreError,
    PresignUnsupported,
    document_key,
    download_filename,
)
from app.schemas import VERSION_PATTERN, Document
from app.tasks import process_document

router = APIRouter(prefix="/api/v1", tags=["documents"])

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_SIZE = 1024 * 1024

# Uploads above this size are buffered on disk rather than in memory before being stored.
SPOOL_MAX_BYTES = 8 * 1024 * 1024


class UploadSource(Protocol):
    """Minimal surface this module needs from an upload."""

    async def read(self, size: int = -1) -> bytes: ...


def _remove_upload(storage: ObjectStore, document_id: str, filename: str) -> None:
    storage.delete(document_key(document_id, filename))


async def _discard_external_index(container, document_id: str) -> None:
    """Drop one document from the search index, best effort.

    ``store.delete_document`` already recorded the same intent in the outbox within the
    transaction that removed the rows, so this call is only the fast path: search results stop
    mentioning the document immediately in the common case. If it fails, the job is still
    pending and the sweeper retries it, which is better than failing a request whose actual
    promise - "this document is gone from PostgreSQL" - has already been kept.
    """
    try:
        await create_ingestion_pipeline(container.settings).delete_document(document_id)
    except Exception:  # noqa: BLE001 - the outbox row is the durable record of this work
        logger.warning("inline index delete failed for %s; the outbox will retry", document_id)


async def _spool_upload(file: UploadSource, limit: int) -> SpooledTemporaryFile[bytes]:
    """Buffer an upload for the object store, refusing it as soon as it is too large.

    The object store wants a seekable body, and a 50 MB upload must not become 50 MB of
    resident memory: ``SpooledTemporaryFile`` keeps small payloads in memory and rolls larger
    ones onto disk. The limit is enforced while reading, so an oversized upload is rejected
    before a single byte reaches storage. The caller owns the returned file and closes it.
    """
    # The caller closes this: the body has to outlive this function so it can be stored.
    spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(  # noqa: SIM115
        max_size=SPOOL_MAX_BYTES
    )
    size = 0
    try:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            size += len(chunk)
            if size > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {limit // (1024 * 1024)} MB limit",
                )
            await asyncio.to_thread(spool.write, chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="uploaded file is empty")
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool


@router.post("/documents", response_model=Document, status_code=201)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    tenant_id: str = Form(""),
    knowledge_base_id: str = Form(...),
    version: str = Form("latest", pattern=VERSION_PATTERN),
    _auth: str | None = Depends(authenticate),
) -> Document:
    container = get_container(request)
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    knowledge_base = await run_blocking(
        container.store.get_knowledge_base,
        knowledge_base_id,
        resolved_tenant,
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    filename = file.filename or "document.txt"
    if PurePosixPath(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"supported file types: {SUPPORTED_LABEL}",
        )
    limit = container.settings.max_upload_mb * 1024 * 1024
    declared_size = file.size
    if declared_size is not None and declared_size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {container.settings.max_upload_mb} MB limit",
        )
    document_id = str(uuid4())
    key = document_key(document_id, filename)
    spool = await _spool_upload(file, limit)
    try:
        await run_blocking(container.storage.put, key, spool)
    except ObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="document could not be stored") from exc
    finally:
        await run_blocking(spool.close)
    document = Document(
        id=document_id,
        filename=filename,
        knowledge_base_id=knowledge_base.id,
        chunks=0,
        status="pending",
        progress=0,
        version=version.strip() or "latest",
    )
    try:
        await run_blocking(container.store.save_document, document, [])
        if not hasattr(process_document, "delay"):
            raise RuntimeError("Celery worker is not available")
        await run_blocking(process_document.delay, document_id)
    except Exception as exc:
        await run_blocking(container.store.update_document_status, document_id, "failed")
        await run_blocking(
            container.store.update_document_progress, document_id, 0, str(exc)
        )
        await run_blocking(_remove_upload, container.storage, document_id, filename)
        raise HTTPException(
            status_code=503,
            detail="document task could not be queued",
        ) from exc
    return document


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> None:
    container = get_container(request)
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    document = await run_blocking(
        container.store.get_document, document_id, resolved_tenant
    )
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    if not await run_blocking(
        container.store.delete_document, document_id, resolved_tenant
    ):
        raise HTTPException(status_code=409, detail="document changed during deletion")
    await _discard_external_index(container, document_id)
    try:
        await run_blocking(_remove_upload, container.storage, document_id, document.filename)
    except ObjectStoreError:
        # The row is already gone, which is what the request promised. A leftover object costs
        # storage, not correctness, and failing the request now would only hide the deletion.
        logger.warning("stored upload for %s could not be removed", document_id)


@router.get("/documents/{document_id}/content")
async def download_document(
    document_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> Response:
    """Send the client to the bytes instead of through them.

    When the backend can sign, the answer is a 307 to a short-lived URL: the API process never
    touches the file, so a large download costs it one request instead of a long-lived stream.
    The local backend has no URL to hand out, so it reads the object and returns it.
    """
    container = get_container(request)
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    document = await run_blocking(
        container.store.get_document, document_id, resolved_tenant
    )
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    key = document_key(document.id, document.filename)
    try:
        url = await run_blocking(
            container.storage.presign_get,
            key,
            expires_seconds=container.settings.s3_presign_seconds,
        )
    except PresignUnsupported:
        try:
            payload = await run_blocking(container.storage.get, key)
        except ObjectMissing as exc:
            raise HTTPException(status_code=404, detail="document content not found") from exc
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={"Content-Disposition": download_filename(document.filename)},
        )
    return RedirectResponse(url, status_code=307)


@router.get("/documents/{document_id}", response_model=Document)
def get_document(
    document_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> Document:
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    document = get_container(request).store.get_document(
        document_id,
        resolved_tenant,
    )
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.get(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=list[Document],
)
def list_documents(
    knowledge_base_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> list[Document]:
    container = get_container(request)
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    if not container.store.get_knowledge_base(knowledge_base_id, resolved_tenant):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return container.store.list_documents(knowledge_base_id, resolved_tenant)
