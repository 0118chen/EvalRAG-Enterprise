"""Celery entrypoint for production document processing."""

from pathlib import Path
from app.config import get_settings
from app.core.ingestion import chunk_pages, extract_text
from app.core.pipeline import default_pipeline
from app.core.store import SQLiteStore

settings = get_settings()

try:
    from celery import Celery
    celery_app = Celery("evalrag", broker=settings.redis_url, backend=settings.redis_url)
    celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", task_track_started=True,
                           task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1)
except ImportError:  # pragma: no cover - dependencies are installed in production image
    celery_app = None


def process_document(document_id: str) -> dict[str, str]:
    """Process one persisted upload; Celery retries can safely re-run this task."""
    store = SQLiteStore()
    document = store.get_document_any(document_id)
    if not document:
        return {"document_id": document_id, "status": "failed", "stage": "missing"}
    if document.status == "ready":
        return {"document_id": document_id, "status": "ready", "stage": "already_indexed", "progress": "100"}
    try:
        store.update_document_progress(document_id, 20)
        upload_dir = Path("data/uploads")
        source = next(upload_dir.glob(f"{document_id}_*"))
        pages = extract_text(source.name, source.read_bytes())
        chunks = chunk_pages(document_id, pages)
        store.update_document_progress(document_id, 60)
        import asyncio
        asyncio.run(default_pipeline().index(chunks))
        with store._lock, store._connect() as connection:
            connection.executemany("INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?)", [(c.id, c.document_id, c.page, c.text, document.knowledge_base_id) for c in chunks])
        store.update_document_status(document_id, "ready")
        store.update_document_progress(document_id, 100)
        return {"document_id": document_id, "status": "ready", "stage": "indexed", "progress": "100"}
    except Exception as exc:
        store.update_document_status(document_id, "failed")
        store.update_document_progress(document_id, 0, str(exc))
        raise


if celery_app is not None:
    process_document = celery_app.task(name="evalrag.process_document", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})(process_document)
