"""Add persistent evaluation datasets and experiment parameters."""

import sqlalchemy as sa

from alembic import op

revision = "0003_evaluation_datasets"
down_revision = "0002_add_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("evaluation_datasets"):
        op.create_table(
            "evaluation_datasets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column(
                "knowledge_base_id",
                sa.String(36),
                sa.ForeignKey("knowledge_bases.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.String(500), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "tenant_id",
                "name",
                name="uq_evaluation_dataset_tenant_name",
            ),
        )
        op.create_index(
            "ix_evaluation_datasets_tenant_id",
            "evaluation_datasets",
            ["tenant_id"],
        )
        op.create_index(
            "ix_evaluation_datasets_knowledge_base_id",
            "evaluation_datasets",
            ["knowledge_base_id"],
        )

    if not inspector.has_table("evaluation_examples"):
        op.create_table(
            "evaluation_examples",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "dataset_id",
                sa.String(36),
                sa.ForeignKey("evaluation_datasets.id"),
                nullable=False,
            ),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("expected_document_id", sa.String(36), nullable=False),
            sa.Column("expected_page", sa.Integer(), nullable=True),
            sa.Column("category", sa.String(100), nullable=False, server_default="general"),
        )
        op.create_index(
            "ix_evaluation_examples_dataset_id",
            "evaluation_examples",
            ["dataset_id"],
        )
        op.create_index(
            "ix_evaluation_examples_expected_document_id",
            "evaluation_examples",
            ["expected_document_id"],
        )

    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("evaluations")
    }
    additions = {
        "tenant_id": sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default="demo-enterprise",
        ),
        "knowledge_base_id": sa.Column("knowledge_base_id", sa.String(36), nullable=True),
        "dataset_id": sa.Column("dataset_id", sa.String(36), nullable=True),
        "experiment_name": sa.Column("experiment_name", sa.String(200), nullable=True),
        "baseline_evaluation_id": sa.Column(
            "baseline_evaluation_id",
            sa.String(36),
            nullable=True,
        ),
        "parameters_json": sa.Column("parameters_json", sa.Text(), nullable=True),
        "completed_at": sa.Column("completed_at", sa.DateTime(), nullable=True),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("evaluations", column)

    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("evaluations")
    }
    for name, column in (
        ("ix_evaluations_tenant_id", "tenant_id"),
        ("ix_evaluations_knowledge_base_id", "knowledge_base_id"),
        ("ix_evaluations_dataset_id", "dataset_id"),
    ):
        if name not in indexes:
            op.create_index(name, "evaluations", [column])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("evaluation_examples"):
        op.drop_table("evaluation_examples")
    if inspector.has_table("evaluation_datasets"):
        op.drop_table("evaluation_datasets")

    # The columns and indexes this revision added to `evaluations` are part of the same
    # change, so they have to go as well: otherwise a downgrade leaves `evaluations` in a
    # state 0002 never had, and the next upgrade finds a half-upgraded table.
    if not inspector.has_table("evaluations"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("evaluations")}
    for name in (
        "ix_evaluations_tenant_id",
        "ix_evaluations_knowledge_base_id",
        "ix_evaluations_dataset_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="evaluations")
    columns = {column["name"] for column in inspector.get_columns("evaluations")}
    for name in (
        "tenant_id",
        "knowledge_base_id",
        "dataset_id",
        "experiment_name",
        "baseline_evaluation_id",
        "parameters_json",
        "completed_at",
    ):
        if name in columns:
            op.drop_column("evaluations", name)
