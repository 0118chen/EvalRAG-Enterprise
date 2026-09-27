"""Store the ground-truth evidence quote so passage-level metrics can be computed."""

import sqlalchemy as sa

from alembic import op

revision = "0008_evaluation_evidence_quote"
down_revision = "0007_document_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "evidence_quote" not in columns:
        op.add_column(
            "evaluation_examples",
            sa.Column("evidence_quote", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "evidence_quote" in columns:
        op.drop_column("evaluation_examples", "evidence_quote")
