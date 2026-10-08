"""The API-key-to-session-token exchange, and everything that makes the token expire.

The point of these tests is that a session is a *server-side* object: it can be revoked,
it stops working when its TTL passes even though the token string has not changed, and the
raw API key never has to be stored anywhere on the client.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import SESSION_AUTH_SCHEME
from app.core.sessions import hash_token, key_fingerprint
from app.db.models import utc_now
from app.main import create_app
from tests.conftest import isolated_settings

ACME_KEY = "key-acme-secret"
OTHER_KEY = "key-other-secret"
API_KEYS = {ACME_KEY: "acme", OTHER_KEY: "other"}


@pytest.fixture
def authed_client() -> TestClient:
    settings = isolated_settings(
        auth_enabled=True,
        api_keys=dict(API_KEYS),
        session_touch_seconds=0,
    )
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def open_client() -> TestClient:
    """The default developer configuration: `AUTH_ENABLED` off."""
    with TestClient(create_app(isolated_settings())) as client:
        yield client


def open_session(client: TestClient, api_key: str = ACME_KEY) -> dict:
    response = client.post("/api/v1/auth/session", headers={"X-API-Key": api_key})
    assert response.status_code == 201, response.text
    return response.json()


def store_of(client: TestClient):
    return client.app.state.container.store


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_exchanging_a_key_returns_a_short_lived_bearer_token(authed_client: TestClient) -> None:
    body = open_session(authed_client)

    assert body["token"].startswith("ers_")
    assert body["token_type"] == "Bearer"
    assert body["tenant_id"] == "acme"
    assert body["authenticated"] is True
    assert body["expires_in"] == 3600
    assert body["expires_at"] > utc_now().isoformat()


def test_only_the_hash_of_the_token_is_stored(authed_client: TestClient) -> None:
    token = open_session(authed_client)["token"]

    stored = store_of(authed_client).get_api_session_by_token_hash(hash_token(token))
    assert stored is not None
    assert stored.token_hash == hash_token(token)
    assert token not in stored.token_hash
    # The key itself never reaches the database either: only a 16-hex-char fingerprint.
    assert stored.key_fingerprint == key_fingerprint(ACME_KEY)
    assert ACME_KEY not in stored.key_fingerprint


def test_a_bad_key_cannot_open_a_session(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/v1/auth/session",
        headers={"X-API-Key": "not-a-key"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "ApiKey"
    assert store_of(authed_client).list_api_sessions("acme") == []


def test_a_session_token_authenticates_where_the_key_did(authed_client: TestClient) -> None:
    token = open_session(authed_client)["token"]

    response = authed_client.get("/api/v1/knowledge-bases", params={"tenant_id": "acme"}, headers=bearer(token))
    assert response.status_code == 200

    # ...and the raw key still works, so server-to-server clients are unaffected.
    raw = authed_client.get(
        "/api/v1/knowledge-bases",
        params={"tenant_id": "acme"},
        headers={"X-API-Key": ACME_KEY},
    )
    assert raw.status_code == 200


def test_a_request_without_any_credential_is_rejected(authed_client: TestClient) -> None:
    response = authed_client.get("/api/v1/knowledge-bases", params={"tenant_id": "acme"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "ApiKey"


def test_a_token_cannot_claim_another_tenant(authed_client: TestClient) -> None:
    token = open_session(authed_client)["token"]

    response = authed_client.get(
        "/api/v1/knowledge-bases",
        params={"tenant_id": "other"},
        headers=bearer(token),
    )

    assert response.status_code == 403


def test_an_unknown_token_is_rejected(authed_client: TestClient) -> None:
    response = authed_client.get(
        "/api/v1/knowledge-bases",
        params={"tenant_id": "acme"},
        headers=bearer("ers_not-a-real-token"),
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == SESSION_AUTH_SCHEME
    assert "unknown session token" in response.json()["detail"]


def test_an_expired_token_stops_working_even_though_the_string_is_unchanged(
    authed_client: TestClient,
) -> None:
    token = "ers_expired-on-arrival"
    expired_at = utc_now() - timedelta(seconds=1)
    store_of(authed_client).create_api_session("acme", hash_token(token), expired_at)

    response = authed_client.get(
        "/api/v1/knowledge-bases",
        params={"tenant_id": "acme"},
        headers=bearer(token),
    )

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_logging_out_revokes_the_session_server_side(authed_client: TestClient) -> None:
    token = open_session(authed_client)["token"]

    assert authed_client.delete("/api/v1/auth/session", headers=bearer(token)).status_code == 204

    after = authed_client.get(
        "/api/v1/knowledge-bases",
        params={"tenant_id": "acme"},
        headers=bearer(token),
    )
    assert after.status_code == 401
    assert "revoked" in after.json()["detail"]
    stored = store_of(authed_client).get_api_session_by_token_hash(hash_token(token))
    assert stored is not None and stored.revoked_at is not None


def test_a_session_can_describe_itself(authed_client: TestClient) -> None:
    token = open_session(authed_client)["token"]

    response = authed_client.get("/api/v1/auth/session", headers=bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "acme"
    assert body["current"] is True
    assert body["key_fingerprint"] == key_fingerprint(ACME_KEY)
    assert body["last_used_at"] is not None


def test_describing_a_session_needs_a_session(authed_client: TestClient) -> None:
    """The API key is a credential, not a session: this endpoint is about the session row."""
    response = authed_client.get("/api/v1/auth/session", headers={"X-API-Key": ACME_KEY})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == SESSION_AUTH_SCHEME


def test_last_used_is_refreshed_at_most_once_per_interval(authed_client: TestClient) -> None:
    """A read-only screen must not write one audit row per request."""
    quiet_client = TestClient(
        create_app(isolated_settings(auth_enabled=True, api_keys=dict(API_KEYS)))
    )
    with quiet_client:
        token = open_session(quiet_client)["token"]
        quiet_client.get(
            "/api/v1/knowledge-bases",
            params={"tenant_id": "acme"},
            headers=bearer(token),
        )
        store = store_of(quiet_client)
        first = store.get_api_session_by_token_hash(hash_token(token))
        assert first is not None and first.last_used_at is not None

        quiet_client.get(
            "/api/v1/knowledge-bases",
            params={"tenant_id": "acme"},
            headers=bearer(token),
        )
        second = store.get_api_session_by_token_hash(hash_token(token))

    assert second is not None
    assert second.last_used_at == first.last_used_at


def test_the_audit_view_lists_and_marks_the_current_session(authed_client: TestClient) -> None:
    first = open_session(authed_client)["token"]
    second = open_session(authed_client)["token"]

    listed = authed_client.get("/api/v1/auth/sessions", headers=bearer(first))
    assert listed.status_code == 200
    sessions = listed.json()
    assert len(sessions) == 2
    assert [session["current"] for session in sessions] == [False, True]
    assert {session["key_fingerprint"] for session in sessions} == {key_fingerprint(ACME_KEY)}

    assert authed_client.delete("/api/v1/auth/session", headers=bearer(second)).status_code == 204
    assert len(authed_client.get("/api/v1/auth/sessions", headers=bearer(first)).json()) == 1
    with_revoked = authed_client.get(
        "/api/v1/auth/sessions",
        params={"include_revoked": "true"},
        headers=bearer(first),
    ).json()
    assert len(with_revoked) == 2
    assert sum(session["revoked_at"] is not None for session in with_revoked) == 1


def test_revoking_another_tenants_session_is_a_404(authed_client: TestClient) -> None:
    mine = open_session(authed_client)["token"]
    theirs = open_session(authed_client, OTHER_KEY)["token"]
    their_id = store_of(authed_client).get_api_session_by_token_hash(hash_token(theirs)).id

    response = authed_client.delete(f"/api/v1/auth/sessions/{their_id}", headers=bearer(mine))

    assert response.status_code == 404
    # ...and their session is untouched.
    assert authed_client.get("/api/v1/auth/session", headers=bearer(theirs)).status_code == 200


def test_an_operator_can_kill_another_session_of_the_same_tenant(
    authed_client: TestClient,
) -> None:
    mine = open_session(authed_client)["token"]
    theirs = open_session(authed_client)["token"]
    their_id = store_of(authed_client).get_api_session_by_token_hash(hash_token(theirs)).id

    assert authed_client.delete(f"/api/v1/auth/sessions/{their_id}", headers=bearer(mine)).status_code == 204
    assert authed_client.get("/api/v1/auth/session", headers=bearer(theirs)).status_code == 401
    assert authed_client.get("/api/v1/auth/session", headers=bearer(mine)).status_code == 200


def test_purging_drops_expired_rows_and_keeps_recent_revocations(
    authed_client: TestClient,
) -> None:
    store = store_of(authed_client)
    now = utc_now()
    store.create_api_session("acme", hash_token("ers_gone"), now - timedelta(seconds=1))
    store.create_api_session("acme", hash_token("ers_revoked"), now + timedelta(hours=1))
    store.revoke_api_session_by_token_hash(hash_token("ers_revoked"))
    store.create_api_session("acme", hash_token("ers_live"), now + timedelta(hours=1))

    assert store.purge_api_sessions(now=now) == 1
    remaining = {session.token_hash for session in store.list_api_sessions("acme", include_revoked=True)}
    assert remaining == {hash_token("ers_revoked"), hash_token("ers_live")}

    # A day later the revoked row is history and can go too.
    assert store.purge_api_sessions(now=now + timedelta(days=2)) == 2


def test_auth_disabled_still_hands_out_a_revocable_session(open_client: TestClient) -> None:
    """With no keys configured the API is already open - but the browser still gets a TTL."""
    response = open_client.post("/api/v1/auth/session", json={"tenant_id": "open-tenant"})

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "open-tenant"
    assert body["authenticated"] is False
    token = body["token"]

    assert open_client.get("/api/v1/auth/session", headers=bearer(token)).status_code == 200
    assert open_client.delete("/api/v1/auth/session", headers=bearer(token)).status_code == 204
    assert open_client.get("/api/v1/auth/session", headers=bearer(token)).status_code == 401


def test_auth_disabled_falls_back_to_the_demo_tenant(open_client: TestClient) -> None:
    body = open_client.post("/api/v1/auth/session").json()

    assert body["tenant_id"] == "demo-enterprise"


def test_a_session_cannot_be_opened_for_a_tenant_the_key_does_not_own(
    authed_client: TestClient,
) -> None:
    response = authed_client.post(
        "/api/v1/auth/session",
        headers={"X-API-Key": ACME_KEY},
        json={"tenant_id": "other"},
    )

    assert response.status_code == 403
