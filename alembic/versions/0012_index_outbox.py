"""Persist the intent to index a document, so PostgreSQL and the search index can be repaired.

PostgreSQL holds the chunk text and Milvus/Elasticsearch hold a rebuildable copy of it. The
old ingestion wrote the external index first and only then marked the document ``ready``, so a
crash in between left a document that is searchable but reported as still processing (and, the
other way round, chunks that no retriever can find). This revision adds the missing durable
step: one row per document saying which external state it should end up in.

The table has no foreign keys on purpose - a row must outlive the document or the whole
knowledge base it refers to, which is exactly when a cascade would erase the only record that
the external index still needs cleaning up.
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_index_outbox"
down_revision = "0011_postgres_constraints"
branch_labels = None
depends_on = None

DOCUMENT_STATUS_CHECK = "status IN ('pending', 'processing', 'done', 'failed')"
OPERATION_CHECK = "operation IN ('upsert', 'delete')"


def upgrade() -> None:
    if "index_outbox" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "index_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False, server_default="upsert"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(DOCUMENT_STATUS_CHECK, name="ck_index_outbox_status"),
        sa.CheckConstraint(OPERATION_CHECK, name="ck_index_outbox_operation"),
        sa.UniqueConstraint("document_id", name="uq_index_outbox_document_id"),
    )
    op.create_index("ix_index_outbox_document_id", "index_outbox", ["document_id"])
    op.create_index("ix_index_outbox_knowledge_base_id", "index_outbox", ["knowledge_base_id"])
    op.create_index("ix_index_outbox_status", "index_outbox", ["status"])


def downgrade() -> None:
    if "index_outbox" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_index_outbox_status", table_name="index_outbox")
    op.drop_index("ix_index_outbox_knowledge_base_id", table_name="index_outbox")
    op.drop_index("ix_index_outbox_document_id", table_name="index_outbox")
    op.drop_table("index_outbox")
