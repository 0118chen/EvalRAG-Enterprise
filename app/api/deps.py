"""Shared FastAPI dependencies including authentication and tenant binding."""

import secrets

from fastapi import HTTPException, Request, status

from app.config import Settings
from app.container import AppContainer
from app.core.llm import create_llm

# Environments where an operator is working on their own machine.
LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def external_health_checks_open(settings: Settings) -> bool:
    """Cheap by default only on a developer machine.

    `/health/llm` and `/health/langsmith` call paid third-party services, so a
    deployed environment must present the admin token instead.
    """
    return settings.health_checks_public or settings.app_env in LOCAL_ENVIRONMENTS


def require_health_admin(request: Request) -> None:
    settings = get_container(request).settings
    if external_health_checks_open(settings):
        return
    token = settings.health_admin_token
    if not token:
        # Fail closed: a deployment without a token must not silently expose a
        # paid endpoint rather than run a check it cannot protect.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HEALTH_ADMIN_TOKEN is not configured for this environment",
        )
    provided = request.headers.get("X-Health-Token", "")
    if not secrets.compare_digest(provided, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="valid X-Health-Token is required",
            headers={"WWW-Authenticate": "HealthToken"},
        )


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
