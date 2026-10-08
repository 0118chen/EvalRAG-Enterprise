"""HTTP middleware for request tracing, metrics and rate limiting."""

from time import perf_counter
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.container import AppContainer
from app.core.metrics import normalize_path
from app.core.sessions import bearer_token, hash_token

# Cheap endpoints that must not consume the rate limit budget: orchestrator
# probes, Prometheus scraping and the docs page. Everything else, including the
# paid /health/llm and /health/langsmith connectivity checks, is rate limited.
RATE_LIMIT_EXEMPT_PATHS = frozenset(
    {
        "/",
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }
)


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


def _path_label(request: Request) -> str:
    """The route template, e.g. ``/api/v1/documents/{document_id}``.

    Using the raw path instead would put a fresh document id in a metric label on
    every request, leaking memory in-process and cardinality in Prometheus. Starlette
    exposes the matched template on the scope; when it does not, the identifiers are
    rebuilt from the path parameters, and as a last resort collapsed by hand - which
    is what covers a 404, where nothing matched at all.
    """
    template = getattr(request.scope.get("route"), "path", None)
    if template:
        return template
    path = request.url.path
    for name, value in (request.scope.get("path_params") or {}).items():
        if isinstance(value, str) and value:
            path = path.replace(f"/{value}", f"/{{{name}}}")
    return normalize_path(path)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        container: AppContainer = request.app.state.container
        started = perf_counter()
        response = await call_next(request)
        container.metrics.observe(
            _path_label(request),
            response.status_code,
            perf_counter() - started,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        container: AppContainer = request.app.state.container
        limiter = container.rate_limiter
        if not limiter or request.url.path in RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)
        api_key = request.headers.get("X-API-Key")
        identity = container.settings.api_keys.get(api_key or "")
        if not identity:
            # A session token is per-session, not per-tenant, so its bucket is keyed by the
            # token itself (hashed, never logged). Minting one already required a valid
            # long-lived key, so this cannot be used to escape the limit: an attacker who
            # can mint sessions can also send the key directly.
            token = bearer_token(request.headers.get("Authorization"))
            if token:
                identity = f"session:{hash_token(token)[:16]}"
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
