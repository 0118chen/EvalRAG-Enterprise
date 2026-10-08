"""Shared FastAPI dependencies including authentication and tenant binding."""

import secrets
from datetime import timedelta

from fastapi import HTTPException, Request, status

from app.config import Settings
from app.container import AppContainer
from app.core.llm import create_llm
from app.core.sessions import bearer_token, hash_token, is_session_token
from app.db.models import utc_now
from app.schemas import ApiSession

# Environments where an operator is working on their own machine.
LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test"})

SESSION_AUTH_SCHEME = "Bearer"


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


def api_key_tenant(settings: Settings, api_key: str) -> str | None:
    """The tenant a long-lived key belongs to, or None when the key is unknown."""
    return settings.api_keys.get(api_key) if api_key else None


def unauthorized(detail: str, scheme: str = SESSION_AUTH_SCHEME) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": scheme},
    )


def resolve_session(container: AppContainer, token: str) -> ApiSession:
    """Return the live session behind a bearer token, or raise 401.

    Expiry and revocation are re-checked against the database on every request rather than
    trusted from the token, which is the entire reason the session lives in a row.
    """
    session = container.store.get_api_session_by_token_hash(hash_token(token))
    if session is None:
        raise unauthorized("unknown session token")
    if session.revoked_at is not None:
        raise unauthorized("session token has been revoked")
    if session.expires_at <= utc_now():
        raise unauthorized("session token has expired")
    return session


def _note_session_use(container: AppContainer, session: ApiSession) -> ApiSession:
    """Refresh `last_used_at`, at most once per `session_touch_seconds`.

    A read-only screen issues a handful of requests; writing an audit timestamp for each
    of them would cost more than the reads themselves.
    """
    now = utc_now()
    interval = max(0, container.settings.session_touch_seconds)
    if session.last_used_at is None or session.last_used_at <= now - timedelta(seconds=interval):
        container.store.touch_api_session(session.id, now=now)
        session.last_used_at = now
    return session


def session_token_of(request: Request) -> str | None:
    token = bearer_token(request.headers.get("Authorization"))
    if token and is_session_token(token):
        return token
    return None


def current_session(request: Request) -> ApiSession:
    """Depend on this when the *session row* is the subject, not just the tenant.

    Unlike `authenticate` it also works while `AUTH_ENABLED` is off: the bearer token, not
    the environment flag, is the credential here, so revoking a session ends it either way.
    """
    token = session_token_of(request)
    if token is None:
        raise unauthorized("a session token is required (Authorization: Bearer ers_...)")
    container = get_container(request)
    session = _note_session_use(container, resolve_session(container, token))
    request.state.api_tenant_id = session.tenant_id
    request.state.api_session = session
    return session


def authenticate(request: Request) -> str | None:
    container = get_container(request)
    settings = container.settings
    if not settings.auth_enabled:
        return None
    token = session_token_of(request)
    if token is not None:
        session = _note_session_use(container, resolve_session(container, token))
        request.state.api_tenant_id = session.tenant_id
        request.state.api_session = session
        return session.tenant_id
    api_key = request.headers.get("X-API-Key", "")
    tenant_id = api_key_tenant(settings, api_key)
    if not tenant_id:
        raise unauthorized(
            "valid X-API-Key or session token is required",
            scheme="ApiKey",
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
