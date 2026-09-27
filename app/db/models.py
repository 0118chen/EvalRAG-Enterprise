"""SQLAlchemy schema shared by PostgreSQL production and migration tooling."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class KnowledgeBaseRecord(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())


class DocumentRecord(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(64), default="latest", server_default="latest")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class ChunkRecord(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    version: Mapped[str] = mapped_column(String(64), default="latest", server_default="latest", index=True)


class EvaluationDatasetRecord(Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "name",
            name="uq_evaluation_dataset_tenant_name",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())


class EvaluationExampleRecord(Base):
    __tablename__ = "evaluation_examples"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expected_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(100), default="general")
    should_refuse: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON list of {"document_id", "page", "quote"} hops; read whole, never filtered in SQL.
    expected_evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "all": every hop is required. "any": the hops are equivalent alternatives.
    evidence_mode: Mapped[str] = mapped_column(String(16), default="all")


class EvaluationRecord(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="demo-enterprise")
    knowledge_base_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id"), index=True, nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True, nullable=True)
    dataset_name: Mapped[str] = mapped_column(String(200))
    retrieval_mode: Mapped[str] = mapped_column(String(20))
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    experiment_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baseline_evaluation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parameters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FeedbackRecord(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        index=True,
        default="demo-enterprise",
    )
    trace_id: Mapped[str] = mapped_column(String(100), index=True)
    feedback: Mapped[str] = mapped_column(String(30))
    comment: Mapped[str] = mapped_column(Text, default="")
    rag_version: Mapped[str] = mapped_column(String(50))
    prompt_version: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())
