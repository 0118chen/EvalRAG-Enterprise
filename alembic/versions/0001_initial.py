from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("knowledge_bases"):
        op.create_table("knowledge_bases", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(128), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("description", sa.String(500), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
    if not inspector.has_table("documents"):
        op.create_table("documents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("filename", sa.String(255), nullable=False), sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id"), nullable=False), sa.Column("chunks", sa.Integer(), nullable=False, server_default="0"), sa.Column("status", sa.String(20), nullable=False, server_default="pending"), sa.Column("progress", sa.Integer(), nullable=False, server_default="0"), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("knowledge_bases")}
    if "ix_knowledge_bases_tenant_id" not in indexes:
        op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("documents")}
    if "ix_documents_knowledge_base_id" not in indexes:
        op.create_index("ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"])
    if not sa.inspect(bind).has_table("evaluations"):
        op.create_table("evaluations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("dataset_name", sa.String(200), nullable=False), sa.Column("retrieval_mode", sa.String(20), nullable=False), sa.Column("top_k", sa.Integer(), nullable=False, server_default="5"), sa.Column("status", sa.String(20), nullable=False, server_default="queued"), sa.Column("results_json", sa.Text()), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
    else:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("evaluations")}
        if "results_json" not in columns:
            op.add_column("evaluations", sa.Column("results_json", sa.Text()))
        if "error_message" not in columns:
            op.add_column("evaluations", sa.Column("error_message", sa.Text()))
    if not sa.inspect(bind).has_table("feedback"):
        op.create_table("feedback", sa.Column("id", sa.String(36), primary_key=True), sa.Column("trace_id", sa.String(100), nullable=False), sa.Column("feedback", sa.String(30), nullable=False), sa.Column("comment", sa.Text(), nullable=False, server_default=""), sa.Column("rag_version", sa.String(50), nullable=False), sa.Column("prompt_version", sa.String(100), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
        op.create_index("ix_feedback_trace_id", "feedback", ["trace_id"])

def downgrade():
    op.drop_table("documents")
    op.drop_index("ix_knowledge_bases_tenant_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
