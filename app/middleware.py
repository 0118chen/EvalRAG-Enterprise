"""HTTP middleware for request tracing, metrics and rate limiting."""

from time import perf_counter
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.container import AppContainer


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        container: AppContainer = request.app.state.container
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        with container.traces.span(
            "http.request",
            run_type="chain",
            metadata={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
            inputs={"query_keys": sorted(request.query_params.keys())},
        ) as span:
            response = await call_next(request)
            span.set_outputs({"status_code": response.status_code})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = span.trace_id
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        container: AppContainer = request.app.state.container
        started = perf_counter()
        response = await call_next(request)
        container.metrics.observe(
            request.url.path,
            response.status_code,
            perf_counter() - started,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        container: AppContainer = request.app.state.container
        limiter = container.rate_limiter
        if not limiter or not request.url.path.startswith("/api/"):
            return await call_next(request)
        api_key = request.headers.get("X-API-Key")
        identity = container.settings.api_keys.get(api_key or "")
        if not identity:
            identity = request.client.host if request.client else "unknown"
        decision = await limiter.check(identity)
        if not decision.allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(decision.retry_after)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response
