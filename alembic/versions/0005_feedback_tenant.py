"""Bind feedback records to tenants."""

import sqlalchemy as sa

from alembic import op

revision = "0005_feedback_tenant"
down_revision = "0004_document_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("feedback")
    }
    if "tenant_id" not in columns:
        op.add_column(
            "feedback",
            sa.Column(
                "tenant_id",
                sa.String(128),
                nullable=False,
                server_default="demo-enterprise",
            ),
        )
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("feedback")
    }
    if "ix_feedback_tenant_id" not in indexes:
        op.create_index("ix_feedback_tenant_id", "feedback", ["tenant_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {
        index["name"] for index in inspector.get_indexes("feedback")
    }
    if "ix_feedback_tenant_id" in indexes:
        op.drop_index("ix_feedback_tenant_id", table_name="feedback")
    # The column is introduced here too, so dropping only the index would leave 0004's
    # schema plus an unasked-for column.
    columns = {column["name"] for column in inspector.get_columns("feedback")}
    if "tenant_id" in columns:
        op.drop_column("feedback", "tenant_id")
