"""Record whether an example's labels are conjunctive hops or equivalent alternatives.

The same definition is written verbatim into several regulations, so a question about it
has three correct answers, not one. Storing that as a single-document label would invent a
wrong ground truth, and scoring it as "every hop required" would fail a correct answer.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_evaluation_evidence_mode"
down_revision = "0009_evaluation_multihop_refusal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "evidence_mode" not in columns:
        op.add_column(
            "evaluation_examples",
            sa.Column(
                "evidence_mode",
                sa.String(length=16),
                nullable=False,
                server_default="all",
            ),
        )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("evaluation_examples")
    }
    if "evidence_mode" in columns:
        op.drop_column("evaluation_examples", "evidence_mode")
