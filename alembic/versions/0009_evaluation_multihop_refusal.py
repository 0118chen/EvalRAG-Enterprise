"""Let an example be multi-hop or unanswerable.

Multi-hop questions need one label per hop, and unanswerable questions need no document
at all, so ``expected_document_id`` becomes nullable and the extra hops move into a JSON
column. A JSON column rather than a child table because the hops are read as a whole
label payload and never joined or filtered on; the cost is that SQL cannot enforce their
shape, which the Pydantic layer does instead.
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_evaluation_multihop_refusal"
down_revision = "0008_evaluation_evidence_quote"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("evaluation_examples")}
    with op.batch_alter_table("evaluation_examples") as batch:
        if not columns["expected_document_id"]["nullable"]:
            batch.alter_column(
                "expected_document_id",
                existing_type=sa.String(length=36),
                nullable=True,
            )
        if "should_refuse" not in columns:
            batch.add_column(
                sa.Column(
                    "should_refuse",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "expected_evidence_json" not in columns:
            batch.add_column(sa.Column("expected_evidence_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    unanswerable = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM evaluation_examples "
            "WHERE expected_document_id IS NULL OR should_refuse = :flag"
        ),
        {"flag": True},
    ).scalar_one()
    if unanswerable:
        raise RuntimeError(
            f"{unanswerable} example(s) are multi-hop or unanswerable; "
            "dropping this revision would lose their labels, so delete them first"
        )
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("evaluation_examples")}
    with op.batch_alter_table("evaluation_examples") as batch:
        if "expected_evidence_json" in columns:
            batch.drop_column("expected_evidence_json")
        if "should_refuse" in columns:
            batch.drop_column("should_refuse")
        batch.alter_column(
            "expected_document_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
