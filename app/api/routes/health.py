from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.deps import get_container, get_llm

router = APIRouter()


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
def readiness(request: Request) -> dict[str, str]:
    container = get_container(request)
    with container.store.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@router.get("/health/llm")
async def llm_health(request: Request) -> dict:
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
def langsmith_health(request: Request):
    result = get_container(request).langsmith.health()
    if result["status"] == "error":
        return JSONResponse(status_code=503, content=result)
    return result


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics(request: Request) -> str:
    return get_container(request).metrics.render()
