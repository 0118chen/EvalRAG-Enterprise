from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("knowledge_bases", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(128), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("description", sa.String(500), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
    op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
    op.create_table("documents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("filename", sa.String(255), nullable=False), sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("knowledge_bases.id"), nullable=False), sa.Column("chunks", sa.Integer(), nullable=False, server_default="0"), sa.Column("status", sa.String(20), nullable=False, server_default="pending"), sa.Column("progress", sa.Integer(), nullable=False, server_default="0"), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()))
    op.create_index("ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"])

def downgrade():
    op.drop_table("documents")
    op.drop_index("ix_knowledge_bases_tenant_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
