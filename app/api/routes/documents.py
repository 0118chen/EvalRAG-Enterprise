import asyncio
import re
from pathlib import Path
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.deps import authenticate, get_container, resolve_tenant_id
from app.core.concurrency import run_blocking
from app.core.pipeline import create_ingestion_pipeline
from app.schemas import VERSION_PATTERN, Document
from app.tasks import process_document

router = APIRouter(prefix="/api/v1", tags=["documents"])

UPLOAD_CHUNK_SIZE = 1024 * 1024


class UploadSource(Protocol):
    """Minimal surface this module needs from an upload."""

    async def read(self, size: int = -1) -> bytes: ...


def _remove_upload(document_id: str) -> None:
    for path in Path("data/uploads").glob(f"{document_id}_*"):
        path.unlink(missing_ok=True)


async def _stream_upload_to_disk(file: UploadSource, target: Path, limit: int) -> int:
    """Persist an upload in bounded chunks, aborting as soon as it exceeds ``limit``.

    The payload is never buffered as a whole, so an oversized upload fails during
    the read instead of after the process has already held all of it in memory. The
    file is written to a sibling ``.part`` path and renamed only once complete.
    """
    temporary = target.with_name(f"{target.name}.part")
    size = 0
    try:
        with temporary.open("wb") as handle:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file exceeds {limit // (1024 * 1024)} MB limit",
                    )
                await asyncio.to_thread(handle.write, chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return size


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
    if not re.search(r"\.(pdf|docx|txt|md)$", filename.lower()):
        raise HTTPException(
            status_code=400,
            detail="supported file types: pdf, docx, txt, md",
        )
    limit = container.settings.max_upload_mb * 1024 * 1024
    declared_size = file.size
    if declared_size is not None and declared_size > limit:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {container.settings.max_upload_mb} MB limit",
        )
    document_id = str(uuid4())
    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    upload_path = upload_dir / f"{document_id}_{safe_name}"
    await _stream_upload_to_disk(file, upload_path, limit)
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
        await run_blocking(_remove_upload, document_id)
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
    await create_ingestion_pipeline(container.settings).delete_document(document_id)
    if not await run_blocking(
        container.store.delete_document, document_id, resolved_tenant
    ):
        raise HTTPException(status_code=409, detail="document changed during deletion")
    await run_blocking(_remove_upload, document_id)


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
