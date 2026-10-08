import asyncio
from collections.abc import Callable
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.deps import get_container, get_llm, require_health_admin
from app.config import Settings
from app.container import AppContainer

router = APIRouter()

# Statuses reported per dependency. `skipped` is not a shade of "degraded": a backend that is
# not configured cannot be down, and folding it into the degraded branch would make every
# single-backend deployment permanently degraded.
_OK = "ok"
_ERROR = "error"
_SKIPPED = "skipped"


def _timeout(settings: Settings) -> float:
    return max(float(settings.readiness_timeout_seconds), 0.001)


async def _check_redis(settings: Settings) -> dict[str, str]:
    """Ping the configured Redis; a memory-only deployment reports `skipped`."""
    if not settings.cache_enabled or settings.cache_backend != "redis":
        return {"status": _SKIPPED, "reason": "cache_backend is not redis"}
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return {"status": _ERROR, "error": "redis package is not installed"}
    client = None
    try:
        # A short per-connection bound: the ping must not outlive the readiness budget.
        client = redis_asyncio.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_timeout(settings),
            socket_timeout=_timeout(settings),
        )
        await asyncio.wait_for(client.ping(), timeout=_timeout(settings))
    except Exception as exc:  # noqa: BLE001 - a broken Redis is a degraded check, not a 500
        return {"status": _ERROR, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if client is not None:
            await client.aclose()
    return {"status": _OK}


def _check_database(container: AppContainer) -> dict[str, str]:
    try:
        with container.store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - database outages become a degraded check
        return {"status": _ERROR, "error": f"{type(exc).__name__}: {exc}"}
    return {"status": _OK}


def _inspect_queue(settings: Settings) -> dict[str, object]:
    """Synchronous broker probe; run it off the event loop via `asyncio.to_thread`."""
    if settings.cache_backend != "redis":
        # The Celery app uses `redis_url` as its broker, so a deployment that has not moved
        # its cache to Redis has no broker to probe here either.
        return {"status": _SKIPPED, "reason": "no redis broker is configured"}
    try:
        from app.tasks import celery_app
    except Exception as exc:  # noqa: BLE001 - readiness must degrade, never raise  # pragma: no cover - import-time failure, e.g. missing celery
        return {"status": _ERROR, "error": f"{type(exc).__name__}: {exc}"}
    if celery_app is None:
        return {"status": _SKIPPED, "reason": "celery is not installed"}
    inspector = celery_app.control.inspect(timeout=_timeout(settings))
    try:
        replies = inspector.ping() or {}
        if not replies:
            return {"status": _ERROR, "error": "no Celery worker answered the ping"}
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
    except Exception as exc:  # noqa: BLE001 - an unreachable broker degrades the queue check
        return {"status": _ERROR, "error": f"{type(exc).__name__}: {exc}"}
    pending = sum(len(entries) for entries in active.values()) + sum(
        len(entries) for entries in reserved.values()
    )
    backlog = pending > settings.queue_backlog_threshold
    payload: dict[str, object] = {
        "status": _ERROR if backlog else _OK,
        "workers": len(replies),
        "running": pending,
    }
    if backlog:
        payload["error"] = (
            f"queue backlog {pending} exceeds threshold {settings.queue_backlog_threshold}"
        )
    return payload


async def _check_backends(container: AppContainer) -> dict[str, dict[str, str]]:
    """Probe each *enabled* external retrieval backend and publish its health gauge.

    A disabled backend is reported as `skipped` and gets no gauge line: publishing a 0 for a
    backend that is simply not part of this deployment would page someone about it.
    """
    settings = container.settings
    checks: dict[str, dict[str, str]] = {}
    metrics = container.metrics

    async def probe(name: str, enabled: bool, make_retriever: Callable[[], Any]) -> None:
        if not enabled:
            checks[name] = {"status": _SKIPPED, "reason": f"{name} is not the configured backend"}
            return
        retriever = make_retriever()
        try:
            healthy = bool(await asyncio.wait_for(retriever.health(), timeout=_timeout(settings)))
        except Exception as exc:  # noqa: BLE001 - a probe failure is the health signal itself
            metrics.set_backend_health(name, False)
            checks[name] = {"status": _ERROR, "error": f"{type(exc).__name__}: {exc}"}
            return
        metrics.set_backend_health(name, healthy)
        if healthy:
            checks[name] = {"status": _OK}
        else:
            checks[name] = {"status": _ERROR, "error": "probe returned false"}

    def milvus() -> Any:
        from app.core.backends import MilvusDenseRetriever
        from app.core.embeddings import create_embedding

        return MilvusDenseRetriever(
            settings.milvus_collection,
            create_embedding(settings),
            "readiness",
            settings.milvus_uri,
            settings.milvus_token,
        )

    def elasticsearch() -> Any:
        from app.core.backends import ElasticsearchBM25Retriever

        return ElasticsearchBM25Retriever(
            settings.elasticsearch_index,
            "readiness",
            settings.elasticsearch_url,
            settings.elasticsearch_api_key,
        )

    await probe(
        "milvus",
        settings.dense_retrieval_backend == "milvus",
        milvus,
    )
    await probe(
        "elasticsearch",
        settings.sparse_retrieval_backend == "elasticsearch",
        elasticsearch,
    )
    return checks


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    container = get_container(request)
    return {
        "status": "ok",
        "environment": container.settings.app_env,
        "version": container.settings.rag_version,
    }


@router.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Report every configured dependency, degrading one at a time.

    The response keeps the original `status` / `database` fields so existing callers and
    tests keep working, and adds a `checks` block. HTTP 503 is returned as soon as one
    *configured* dependency is in `error` (the body names it); `skipped` entries - a memory
    cache, a backend that is not part of this deployment - never make the process unready.
    """
    container = get_container(request)
    settings = container.settings

    database = await asyncio.to_thread(_check_database, container)
    redis = await _check_redis(settings)
    queue = await asyncio.to_thread(_inspect_queue, settings)
    checks: dict[str, object] = {"database": database, "redis": redis, "queue": queue}
    checks.update(await _check_backends(container))

    healthy = all(
        isinstance(check, dict) and check.get("status") != _ERROR for check in checks.values()
    )
    payload = {
        "status": "ready" if healthy else "degraded",
        "database": database["status"],
        "checks": checks,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=payload)


@router.get("/health/llm")
async def llm_health(
    request: Request,
    _admin: None = Depends(require_health_admin),
) -> dict:
    container = get_container(request)
    settings = container.settings
    if settings.llm_provider == "mock":
        return {
            "status": "mock",
            "connected": False,
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "message": "当前使用 MockLLM，尚未接入真实模型",
        }
    started = perf_counter()
    try:
        answer = await get_llm(request).answer(
            "请仅回复：连接成功",
            "这是一次 LLM 连接测试，不需要检索其他资料。",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LLM connection failed: {type(exc).__name__}",
        ) from exc
    return {
        "status": "ok",
        "connected": True,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "response_preview": answer[:200],
    }


@router.get("/health/langsmith")
def langsmith_health(
    request: Request,
    _admin: None = Depends(require_health_admin),
):
    result = get_container(request).langsmith.health()
    if result["status"] == "error":
        return JSONResponse(status_code=503, content=result)
    return result


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics(request: Request) -> str:
    container = get_container(request)
    # Sampling the pool at scrape time keeps the gauges current without a background task;
    # the pool object is read-only here and the endpoint never opens a connection.
    container.metrics.read_engine_pool(container.store.engine)
    return container.metrics.render()
