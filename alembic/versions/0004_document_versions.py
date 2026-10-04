"""Add document and chunk version metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0004_document_versions"
down_revision = "0003_evaluation_datasets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    document_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("documents")
    }
    if "version" not in document_columns:
        op.add_column(
            "documents",
            sa.Column("version", sa.String(64), nullable=False, server_default="latest"),
        )

    chunk_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("chunks")
    }
    if "version" not in chunk_columns:
        op.add_column(
            "chunks",
            sa.Column("version", sa.String(64), nullable=False, server_default="latest"),
        )

    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("chunks")
    }
    if "ix_chunks_version" not in indexes:
        op.create_index("ix_chunks_version", "chunks", ["version"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("chunks")}
    if "ix_chunks_version" in indexes:
        op.drop_index("ix_chunks_version", table_name="chunks")
    # Dropping the index is not enough: the `version` columns are introduced here, so a
    # downgrade that keeps them does not restore the schema 0003 had.
    for table in ("chunks", "documents"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "version" in columns:
            op.drop_column(table, "version")
