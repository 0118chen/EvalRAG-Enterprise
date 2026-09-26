"""Track when a document last changed so workers can claim it safely."""

import sqlalchemy as sa

from alembic import op

revision = "0007_document_updated_at"
down_revision = "0006_expected_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")
    }
    if "updated_at" not in columns:
        op.add_column(
            "documents",
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.execute("UPDATE documents SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")
    }
    if "updated_at" in columns:
        op.drop_column("documents", "updated_at")
