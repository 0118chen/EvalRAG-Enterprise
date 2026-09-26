"""Add the persisted retrieval chunk index."""

import sqlalchemy as sa

from alembic import op

revision = "0002_add_chunks"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("chunks"):
        op.create_table(
            "chunks",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("chunks")}
    if "ix_chunks_document_id" not in indexes:
        op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    if "ix_chunks_knowledge_base_id" not in indexes:
        op.create_index("ix_chunks_knowledge_base_id", "chunks", ["knowledge_base_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("chunks"):
        op.drop_table("chunks")
