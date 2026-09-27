"""Celery entrypoint for production document processing."""

import asyncio
from pathlib import Path

from app.config import get_settings
from app.core.cache import create_cache
from app.core.evaluation_runner import EvaluationRunner
from app.core.ingestion import Chunk, chunk_pages, extract_text
from app.core.langsmith_eval import LangSmithEvaluationAdapter
from app.core.llm import create_llm
from app.core.observability import TraceManager
from app.core.pipeline import create_ingestion_pipeline
from app.core.query_rewrite import create_query_rewriter
from app.core.reranking import create_reranker
from app.core.retrieval_service import RetrievalService
from app.core.store import create_store

settings = get_settings()
traces = TraceManager(settings)

try:
    from celery import Celery
    celery_app = Celery("evalrag", broker=settings.redis_url, backend=settings.redis_url)
    celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", task_track_started=True,
                           task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1)
except ImportError:  # pragma: no cover - dependencies are installed in production image
    celery_app = None


class EmptyExtractionError(RuntimeError):
    """The file passed the upload check but yielded no text to index."""


def process_document(document_id: str, force: bool = False) -> dict[str, str]:
    """Process one persisted upload; concurrent or retried deliveries stay safe."""
    store = create_store(settings.database_url)
    document = store.get_document_any(document_id)
    if not document:
        return {"document_id": document_id, "status": "failed", "stage": "missing"}
    if document.status == "ready" and not force:
        return {"document_id": document_id, "status": "ready", "stage": "already_indexed", "progress": "100"}
    if not store.claim_document(document_id, force=force):
        # Another worker already owns this document, or it finished between the
        # read above and the claim. Returning normally avoids pointless retries.
        current = store.get_document_any(document_id)
        return {
            "document_id": document_id,
            "status": current.status if current else "unknown",
            "stage": "not_claimed",
            "progress": str(current.progress) if current else "0",
        }
    try:
        with traces.span(
            "ingestion.document",
            run_type="chain",
            metadata={
                "document_id": document_id,
                "knowledge_base_id": document.knowledge_base_id,
                "version": document.version,
            },
            inputs={"filename": document.filename},
        ) as document_span:
            store.update_document_progress(document_id, 20)
            upload_dir = Path("data/uploads")
            source = next(upload_dir.glob(f"{document_id}_*"))
            with traces.span(
                "ingestion.extract",
                run_type="tool",
                metadata={"document_id": document_id},
            ) as extract_span:
                pages = extract_text(source.name, source.read_bytes())
                characters = sum(len(text.strip()) for _, text in pages)
                extract_span.set_outputs({"page_count": len(pages), "characters": characters})
            if characters == 0:
                # A scan or an image-only PDF: the extension passed the upload check,
                # but there is nothing to index. Without this guard the document is
                # published as "ready" with zero chunks - a success that can never be
                # retrieved and never reports why.
                raise EmptyExtractionError(
                    f"{source.name}: {len(pages)} page(s), 0 extractable characters - "
                    "likely a scanned document without a text layer (OCR required)"
                )
            with traces.span(
                "ingestion.chunk",
                run_type="chain",
                metadata={"document_id": document_id},
            ) as chunk_span:
                chunks = [
                    Chunk(
                        id=chunk.id,
                        document_id=chunk.document_id,
                        page=chunk.page,
                        text=chunk.text,
                        version=document.version,
                    )
                    for chunk in chunk_pages(document_id, pages)
                ]
                chunk_span.set_outputs({"chunk_count": len(chunks)})
            store.update_document_progress(document_id, 60)
            with traces.span(
                "ingestion.index",
                run_type="tool",
                metadata={"document_id": document_id},
            ) as index_span:
                asyncio.run(
                    create_ingestion_pipeline(settings).replace_document(
                        chunks,
                        document.knowledge_base_id,
                        document_id,
                    )
                )
                store.replace_chunks(document_id, document.knowledge_base_id, chunks)
                index_span.set_outputs({"chunk_count": len(chunks)})
            store.update_document_status(document_id, "ready")
            store.update_document_progress(document_id, 100)
            document_span.set_outputs({"chunk_count": len(chunks), "status": "ready"})
            return {
                "document_id": document_id,
                "status": "ready",
                "stage": "indexed",
                "progress": "100",
            }
    except Exception as exc:
        store.update_document_status(document_id, "failed")
        store.update_document_progress(document_id, 0, str(exc))
        raise


def process_evaluation(evaluation_id: str) -> dict[str, str]:
    store = create_store(settings.database_url)
    cache = create_cache(settings)
    retrieval_service = RetrievalService(
        settings,
        traces,
        cache,
        create_query_rewriter(settings),
        create_reranker(settings),
    )
    runner = EvaluationRunner(
        settings=settings,
        store=store,
        retrieval_service=retrieval_service,
        traces=traces,
        langsmith=LangSmithEvaluationAdapter(settings),
        llm=create_llm(settings),
    )
    evaluation = asyncio.run(runner.run(evaluation_id))
    return {
        "evaluation_id": evaluation_id,
        "status": evaluation["status"],
    }


if celery_app is not None:
    process_document = celery_app.task(name="evalrag.process_document", autoretry_for=(Exception,), dont_autoretry_for=(EmptyExtractionError,), retry_backoff=True, retry_kwargs={"max_retries": 3})(process_document)
    process_evaluation = celery_app.task(
        name="evalrag.process_evaluation",
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 2},
    )(process_evaluation)
