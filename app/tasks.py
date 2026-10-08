"""Celery entrypoint for production document processing."""

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any

from app.config import get_settings
from app.core.cache import create_cache
from app.core.evaluation_runner import EvaluationRunner
from app.core.ingestion import Chunk, chunk_pages, extract_text
from app.core.langsmith_eval import LangSmithEvaluationAdapter
from app.core.llm import create_llm
from app.core.metrics import Metrics
from app.core.observability import TraceManager
from app.core.ocr import OcrUnavailableError, create_ocr_backend
from app.core.pipeline import create_ingestion_pipeline
from app.core.query_rewrite import create_query_rewriter
from app.core.reranking import create_reranker
from app.core.retrieval_service import RetrievalService
from app.core.storage import create_object_store, document_key
from app.core.store import SQLAlchemyStore, create_store

settings = get_settings()
traces = TraceManager(settings)
# Worker-side metric state. A Celery worker has no request to hang an app container off, and
# each worker is its own process, so this is the same per-process exposition the API serves.
metrics = Metrics()

try:
    from celery import Celery
    celery_app = Celery("evalrag", broker=settings.redis_url, backend=settings.redis_url)
    celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", task_track_started=True,
                           task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1)
except ImportError:  # pragma: no cover - dependencies are installed in production image
    celery_app = None


class EmptyExtractionError(RuntimeError):
    """The file passed the upload check but yielded no text to index."""


class NeedsOcrError(EmptyExtractionError):
    """The document is fine; this deployment cannot read it without OCR.

    Distinct from a plain failure because the fix is environmental (configure an
    OCR backend, or hand the file to a human), not a retry and not a bug.
    """


def _read_scan_with_ocr(
    filename: str, payload: bytes, settings, pages: list[tuple[int, str]]
) -> tuple[list[tuple[int, str]], int]:
    """Try OCR for a file with no text layer, or say why it could not."""
    summary = f"{filename}: {len(pages)} page(s), 0 extractable characters"
    try:
        backend = create_ocr_backend(settings)
    except ValueError as exc:
        raise NeedsOcrError(f"{summary} - {exc}") from exc
    if backend is None:
        raise NeedsOcrError(
            f"{summary} - no OCR backend configured "
            "(set OCR_BACKEND=tesseract to read scans)"
        )
    try:
        ocr_pages = backend.extract(filename, payload)
    except OcrUnavailableError as exc:
        raise NeedsOcrError(
            f"{summary} - OCR backend '{settings.ocr_backend}' unavailable: {exc}"
        ) from exc
    characters = sum(len(text.strip()) for _, text in ocr_pages)
    if characters == 0:
        raise NeedsOcrError(
            f"{summary} - OCR ran with '{settings.ocr_backend}' but produced no text either"
        )
    return ocr_pages, characters


def _observe_task(name: str, outcome: str) -> None:
    """Record one task lifecycle event; ``outcome`` is ``started``, ``succeeded`` or ``failed``."""
    if outcome == "started":
        metrics.observe_task_started(name)
    elif outcome == "succeeded":
        metrics.observe_task_succeeded(name)
    else:
        metrics.observe_task_failed(name)


def _tracked(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
    """Count starts, successes and failures around a task body.

    Counting here rather than in ``celery.signals`` matters for two reasons: a retry would be
    booked as a failure even though the task has not given up yet, and the task body is also
    reachable through ``process_document.run(...)``, which never emits a signal. The wrapper is
    the single place both paths pass through, so exactly one outcome is recorded per call.
    """

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _observe_task(name, "started")
        try:
            result = function(*args, **kwargs)
        except BaseException:
            _observe_task(name, "failed")
            raise
        _observe_task(name, "succeeded")
        return result

    return wrapper


def _process_document(document_id: str, force: bool = False) -> dict[str, str]:
    """Process one persisted upload; concurrent or retried deliveries stay safe."""
    store = create_store(settings.database_url)
    storage = create_object_store(settings)
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
            # The bytes live in the object store, not on this machine's disk: that is what lets
            # a worker on another host pick up a document the API uploaded.
            payload = storage.get(document_key(document_id, document.filename))
            with traces.span(
                "ingestion.extract",
                run_type="tool",
                metadata={"document_id": document_id},
            ) as extract_span:
                pages = extract_text(document.filename, payload)
                characters = sum(len(text.strip()) for _, text in pages)
                extract_span.set_outputs({"page_count": len(pages), "characters": characters})
            if characters == 0:
                # A scan or an image-only PDF: the extension passed the upload check,
                # but there is nothing to index. A configured OCR backend gets a
                # chance; otherwise the document is parked as needs_ocr instead of
                # being published as "ready" with zero chunks - a success that can
                # never be retrieved and never reports why.
                with traces.span(
                    "ingestion.ocr",
                    run_type="tool",
                    metadata={"document_id": document_id, "backend": settings.ocr_backend},
                ) as ocr_span:
                    pages, characters = _read_scan_with_ocr(
                        document.filename, payload, settings, pages
                    )
                    ocr_span.set_outputs({"page_count": len(pages), "characters": characters})
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
            # Commit the chunks and the intent to publish them in one transaction, then let the
            # outbox driver do the external write. Dying anywhere after this line leaves a
            # replayable row instead of a document that is indexed but still says "processing".
            store.save_document_index(document_id, document.knowledge_base_id, chunks)
            with traces.span(
                "ingestion.index",
                run_type="tool",
                metadata={"document_id": document_id},
            ) as index_span:
                job = _sync_document_index(store, document_id)
                index_span.set_outputs(
                    {"chunk_count": len(chunks), "index_job": job["stage"]}
                )
            document_span.set_outputs({"chunk_count": len(chunks), "status": "ready"})
            return {
                "document_id": document_id,
                "status": "ready",
                "stage": "indexed",
                "progress": "100",
            }
    except Exception as exc:
        # needs_ocr is not a failure: the file is intact and a human or an
        # OCR-capable worker has to pick it up, so the status must say so.
        status = "needs_ocr" if isinstance(exc, NeedsOcrError) else "failed"
        progress = 0
        outstanding = store.get_index_job(document_id)
        if (
            status == "failed"
            and outstanding is not None
            and outstanding.status in ("pending", "processing")
        ):
            # The chunks and the intent to publish them are committed, and the outbox row
            # still carries retry budget: the document is late, not lost. It becomes
            # "failed" only once a sweep parks the row (see _sync_document_index).
            status = "processing"
            progress = 60
        store.update_document_status(document_id, status)
        store.update_document_progress(document_id, progress, str(exc))
        raise


def _sync_document_index(
    store: SQLAlchemyStore,
    document_id: str | None = None,
    *,
    stale_after_seconds: int = 900,
    due_after_seconds: int = 0,
) -> dict[str, str]:
    """Replay one index-outbox row: make the search index match PostgreSQL.

    Returns an ``indexed``/``deleted``/``no_index_job`` stage instead of raising for "nothing
    to do". A real failure is booked on the row - so a later sweep can retry it - and then
    re-raised, because the caller's error path (the document is failed, Celery retries the
    task) must still see it.
    """
    job = store.claim_index_job(
        document_id=document_id,
        stale_after_seconds=stale_after_seconds,
        due_after_seconds=due_after_seconds,
    )
    if job is None:
        return {
            "document_id": document_id or "",
            "stage": "no_index_job",
            "operation": "",
            "chunks": "0",
        }
    pipeline = create_ingestion_pipeline(settings)
    try:
        if job.operation == "delete":
            asyncio.run(pipeline.delete_document(job.document_id))
            chunk_count = 0
        else:
            chunks = store.get_document_chunks(job.document_id)
            asyncio.run(
                pipeline.replace_document(chunks, job.knowledge_base_id, job.document_id)
            )
            chunk_count = len(chunks)
            # Only now is the document really retrievable; until this line nothing outside
            # PostgreSQL had a copy of it.
            store.update_document_status(job.document_id, "ready")
            store.update_document_progress(job.document_id, 100)
    except Exception as exc:
        job = store.fail_index_job(job.id, str(exc))
        if job is not None and job.status == "failed":
            # The retry budget is spent, so the document is not "late" any more: it is a
            # dead end until a human looks at last_error. Say that on the document too.
            store.update_document_status(job.document_id, "failed")
            store.update_document_progress(job.document_id, 0, str(exc))
        raise
    store.complete_index_job(job.id)
    return {
        "document_id": job.document_id,
        "stage": "indexed" if job.operation == "upsert" else "deleted",
        "operation": job.operation,
        "chunks": str(chunk_count),
    }


#: A pending row is left alone by the sweeper for this long: a retry budget should be spent
#: over minutes (a redeploy, a backend coming back), not inside one loop.
INDEX_REPLAY_DELAY_SECONDS = 30


def _replay_index_outbox(
    limit: int = 10,
    *,
    stale_after_seconds: int = 900,
    due_after_seconds: int = INDEX_REPLAY_DELAY_SECONDS,
) -> dict[str, str]:
    """Drive every index-outbox row that is due; one bad document must not stop the sweep.

    Each row carries its own retry budget, so a document that keeps failing is parked as
    ``failed`` after a few sweeps and a human can read ``last_error``. The exception is
    swallowed here only because it is already recorded on the row.
    """
    store = create_store(settings.database_url)
    replayed = 0
    failed = 0
    for _ in range(limit):
        try:
            result = _sync_document_index(
                store,
                stale_after_seconds=stale_after_seconds,
                due_after_seconds=due_after_seconds,
            )
        except Exception:  # noqa: BLE001 - the failure is booked on the outbox row
            failed += 1
            continue
        if result["stage"] == "no_index_job":
            break
        replayed += 1
    return {"replayed": str(replayed), "failed": str(failed)}


def _process_evaluation(evaluation_id: str) -> dict[str, str]:
    store = create_store(settings.database_url)
    cache = create_cache(settings, metrics)
    retrieval_service = RetrievalService(
        settings,
        traces,
        cache,
        create_query_rewriter(settings),
        create_reranker(settings),
        metrics,
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


process_document = _tracked("evalrag.process_document", _process_document)
process_evaluation = _tracked("evalrag.process_evaluation", _process_evaluation)
sync_document_index = _tracked("evalrag.sync_document_index", _sync_document_index)
replay_index_outbox = _tracked("evalrag.replay_index_outbox", _replay_index_outbox)

if celery_app is not None:
    process_document = celery_app.task(name="evalrag.process_document", autoretry_for=(Exception,), dont_autoretry_for=(EmptyExtractionError, NeedsOcrError), retry_backoff=True, retry_kwargs={"max_retries": 3})(process_document)
    process_evaluation = celery_app.task(
        name="evalrag.process_evaluation",
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 2},
    )(process_evaluation)
    sync_document_index = celery_app.task(
        name="evalrag.sync_document_index",
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 2},
    )(sync_document_index)
    # No autoretry: the row itself carries the retry budget, so a failed sweep should return
    # to whoever scheduled it instead of multiplying attempts.
    replay_index_outbox = celery_app.task(name="evalrag.replay_index_outbox")(
        replay_index_outbox
    )
