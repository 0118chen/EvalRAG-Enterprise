"""Store browser sessions server-side, so a leaked token expires and can be revoked.

The frontend used to keep the long-lived API key in `localStorage` and send it on every
request: one credential, no expiry, no revocation, no record of use. This revision adds the
row behind the replacement - an opaque session token whose SHA-256 is stored with an expiry
and a revocation timestamp, plus a fingerprint of the key that minted it for the audit
trail. Only the hash is kept, so a database dump does not hand out working sessions.

No foreign key to a tenant exists because tenants live in configuration (`API_KEYS`) in this
schema, so there is nothing to cascade from.
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_api_sessions"
down_revision = "0012_index_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "api_sessions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "api_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_api_sessions_tenant_id", "api_sessions", ["tenant_id"])
    op.create_index("ix_api_sessions_token_hash", "api_sessions", ["token_hash"], unique=True)
    op.create_index("ix_api_sessions_expires_at", "api_sessions", ["expires_at"])


def downgrade() -> None:
    if "api_sessions" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_api_sessions_expires_at", table_name="api_sessions")
    op.drop_index("ix_api_sessions_token_hash", table_name="api_sessions")
    op.drop_index("ix_api_sessions_tenant_id", table_name="api_sessions")
    op.drop_table("api_sessions")
