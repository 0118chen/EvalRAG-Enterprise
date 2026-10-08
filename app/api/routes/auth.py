"""Exchanging a long-lived API key for a short-lived, revocable session token.

The browser never held anything it could lose before this: the API key itself lived in
`localStorage`, had no expiry and could not be revoked without rotating the shared secret
for every client. Here the key is presented once and immediately traded for an opaque
token with a TTL; from then on the browser sends `Authorization: Bearer ers_...`, which
the server resolves against a row it can expire, revoke and audit.
"""

import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import (
    api_key_tenant,
    current_session,
    get_container,
    unauthorized,
)
from app.container import AppContainer
from app.core.sessions import hash_token, key_fingerprint, new_session_token
from app.db.models import utc_now
from app.schemas import ApiSession, SessionInfo, SessionRequest, SessionToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["auth"])

# Tenants live in configuration (`API_KEYS`) rather than in a table, so with authentication
# switched off there is no key to read one from; this is the same fallback the schema uses
# for rows that predate multi-tenancy.
DEFAULT_TENANT_ID = "demo-enterprise"
MIN_SESSION_TTL_SECONDS = 60


def _session_info(session: ApiSession, *, current: bool) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        tenant_id=session.tenant_id,
        key_fingerprint=session.key_fingerprint,
        created_at=session.created_at,
        expires_at=session.expires_at,
        last_used_at=session.last_used_at,
        revoked_at=session.revoked_at,
        current=current,
    )


@router.post("/auth/session", response_model=SessionToken, status_code=status.HTTP_201_CREATED)
def create_session(
    request: Request,
    container: Annotated[AppContainer, Depends(get_container)],
    payload: SessionRequest | None = None,
) -> SessionToken:
    """Trade the long-lived key (or, with auth off, just a tenant) for a short-lived token."""
    settings = container.settings
    api_key = request.headers.get("X-API-Key", "")
    requested = payload.tenant_id if payload else None

    if settings.auth_enabled:
        tenant_id = api_key_tenant(settings, api_key)
        if not tenant_id:
            raise unauthorized(
                "valid X-API-Key is required to open a session",
                scheme="ApiKey",
            )
        if requested and requested != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="tenant_id does not match the authenticated tenant",
            )
        fingerprint = key_fingerprint(api_key)
        authenticated = True
    else:
        # Authentication is off, so the API is already open; the exchange cannot prove
        # anything and does not pretend to. It still mints a row so the browser holds a
        # credential that expires (and that the same client code works in both modes).
        tenant_id = requested or DEFAULT_TENANT_ID
        fingerprint = ""
        authenticated = False

    ttl = max(MIN_SESSION_TTL_SECONDS, settings.session_ttl_seconds)
    now = utc_now()
    expires_at = now + timedelta(seconds=ttl)
    token = new_session_token()
    session = container.store.create_api_session(
        tenant_id,
        hash_token(token),
        expires_at,
        fingerprint,
        now=now,
    )
    logger.info(
        "session opened id=%s tenant=%s key=%s expires_at=%s",
        session.id,
        tenant_id,
        fingerprint or "-",
        expires_at.isoformat(),
    )
    return SessionToken(
        token=token,
        tenant_id=tenant_id,
        expires_at=expires_at,
        expires_in=ttl,
        authenticated=authenticated,
    )


@router.get("/auth/session", response_model=SessionInfo)
def read_session(session: Annotated[ApiSession, Depends(current_session)]) -> SessionInfo:
    """Describe the session behind the presented token (401 once it is dead)."""
    return _session_info(session, current=True)


@router.delete("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session: Annotated[ApiSession, Depends(current_session)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> Response:
    """Revoke the presented session. Logging out must end the credential, not just forget it."""
    revoked = container.store.revoke_api_session(session.id, session.tenant_id)
    if revoked:
        logger.info("session revoked id=%s tenant=%s", session.id, session.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/sessions", response_model=list[SessionInfo])
def list_sessions(
    session: Annotated[ApiSession, Depends(current_session)],
    container: Annotated[AppContainer, Depends(get_container)],
    include_revoked: bool = False,
) -> list[SessionInfo]:
    """Who is holding a session for this tenant - the audit view an operator asked for."""
    stored = container.store.list_api_sessions(session.tenant_id, include_revoked=include_revoked)
    return [_session_info(item, current=item.id == session.id) for item in stored]


@router.delete("/auth/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_other_session(
    session_id: str,
    session: Annotated[ApiSession, Depends(current_session)],
    container: Annotated[AppContainer, Depends(get_container)],
) -> Response:
    """Revoke one other session of the same tenant (rotation without rotating the key)."""
    if not container.store.revoke_api_session(session_id, session.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no such live session for this tenant",
        )
    logger.info("session revoked id=%s tenant=%s by=%s", session_id, session.tenant_id, session.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
