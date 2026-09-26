"""Add optional golden answers to evaluation examples."""

import sqlalchemy as sa

from alembic import op

revision = "0006_expected_answers"
down_revision = "0005_feedback_tenant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "expected_answer" not in columns:
        op.add_column(
            "evaluation_examples",
            sa.Column("expected_answer", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "expected_answer" in columns:
        op.drop_column("evaluation_examples", "expected_answer")
