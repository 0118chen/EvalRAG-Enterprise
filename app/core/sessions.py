"""Short-lived session tokens exchanged for a long-lived API key.

The browser used to hold the API key itself: one credential with no expiry, no way to
revoke it and no record of who used it. `/auth/session` hands out an opaque random token
instead, kept server-side as a hash with an expiry and a revocation timestamp.

Two properties are deliberate:

* only the hash is stored, so a database dump does not hand out working sessions. A
  SHA-256 is sufficient here, unlike for a password: the token is 256 bits of
  `secrets` randomness, so there is no dictionary to guess and no need for a slow KDF.
* the token carries no claims of its own, so revoking one row ends that session
  immediately instead of waiting for a signature to expire.
"""

import hashlib
import secrets

# A recognisable prefix keeps a session token from being mistaken for an API key in a
# log whose owner is trying to work out which credential leaked.
SESSION_TOKEN_PREFIX = "ers_"
SESSION_TOKEN_BYTES = 32
BEARER_SCHEME = "bearer"


def new_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(SESSION_TOKEN_BYTES)}"


def is_session_token(value: str) -> bool:
    return value.startswith(SESSION_TOKEN_PREFIX)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def key_fingerprint(api_key: str) -> str:
    """A stable, non-reversible label the audit trail can name without keeping the key."""
    return hash_token(api_key)[:16]


def bearer_token(header: str | None) -> str | None:
    """Extract the credential from an `Authorization: Bearer <value>` header."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != BEARER_SCHEME:
        return None
    return value.strip() or None
