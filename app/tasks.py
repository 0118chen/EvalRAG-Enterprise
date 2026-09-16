"""Celery entrypoint for production document processing."""

from app.config import get_settings

settings = get_settings()

try:
    from celery import Celery
    celery_app = Celery("evalrag", broker=settings.redis_url, backend=settings.redis_url)
    celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", task_track_started=True)
except ImportError:  # pragma: no cover - dependencies are installed in production image
    celery_app = None


def process_document(document_id: str) -> dict[str, str]:
    """Stable task contract; the ingestion worker will call the pipeline in the next stage."""
    return {"document_id": document_id, "status": "accepted"}


if celery_app is not None:
    process_document = celery_app.task(name="evalrag.process_document")(process_document)

