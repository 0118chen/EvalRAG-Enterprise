"""Shared FastAPI dependencies including authentication and tenant binding."""

from fastapi import HTTPException, Request, status

from app.container import AppContainer
from app.core.llm import create_llm


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def authenticate(request: Request) -> str | None:
    container = get_container(request)
    settings = container.settings
    if not settings.auth_enabled:
        return None
    api_key = request.headers.get("X-API-Key", "")
    tenant_id = settings.api_keys.get(api_key)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="valid X-API-Key is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.api_tenant_id = tenant_id
    return tenant_id


def resolve_tenant_id(request: Request, requested_tenant_id: str) -> str:
    container = get_container(request)
    if container.settings.auth_enabled:
        authenticated = getattr(request.state, "api_tenant_id", None)
        if not authenticated:
            raise HTTPException(status_code=401, detail="authentication required")
        if requested_tenant_id and requested_tenant_id != authenticated:
            raise HTTPException(
                status_code=403,
                detail="tenant_id does not match the authenticated tenant",
            )
        return authenticated
    return requested_tenant_id


def get_llm(request: Request):
    settings = get_container(request).settings
    try:
        return create_llm(settings)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="LLM provider is not configured",
        ) from exc
