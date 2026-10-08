"""SQLAlchemy schema shared by PostgreSQL production and migration tooling."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


#: Every value ``documents.status`` may take.  Enforced by a CHECK constraint, so a typo in
#: a new code path fails at write time instead of leaving a row nobody can query for.
DOCUMENT_STATUSES: tuple[str, ...] = ("pending", "processing", "ready", "failed", "needs_ocr")

#: Every value ``evaluations.status`` may take.
EVALUATION_STATUSES: tuple[str, ...] = ("queued", "running", "completed", "failed")

#: Every value ``index_outbox.status`` may take.  ``done`` rows are kept as the record of
#: what the last replay did; ``failed`` means the retry budget is exhausted.
INDEX_JOB_STATUSES: tuple[str, ...] = ("pending", "processing", "done", "failed")

#: Every value ``index_outbox.operation`` may take: publish this document's chunks, or
#: remove them from the external index.
INDEX_JOB_OPERATIONS: tuple[str, ...] = ("upsert", "delete")

#: JSON columns are JSONB on PostgreSQL (validated on write, stored decomposed, indexable)
#: and the generic JSON type everywhere else.  The application passes Python objects rather
#: than encoded strings, so one code path serves both dialects.
JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def _status_check(column: str, allowed: tuple[str, ...]) -> str:
    values = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({values})"



class KnowledgeBaseRecord(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            _status_check("status", DOCUMENT_STATUSES),
            name="ck_documents_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
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
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
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
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())


class EvaluationExampleRecord(Base):
    __tablename__ = "evaluation_examples"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expected_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(100), default="general")
    should_refuse: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON list of {"document_id", "page", "quote"} hops; read whole, never filtered in SQL.
    expected_evidence_json: Mapped[list | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    # "all": every hop is required. "any": the hops are equivalent alternatives.
    evidence_mode: Mapped[str] = mapped_column(String(16), default="all")


class EvaluationRecord(Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            _status_check("status", EVALUATION_STATUSES),
            name="ck_evaluations_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="demo-enterprise")
    # Deleting a knowledge base or a dataset must not erase the evaluation history: the
    # measurements stay, with the link to the thing that was deleted set to NULL.
    knowledge_base_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"), index=True, nullable=True
    )
    dataset_id: Mapped[str | None] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="SET NULL"), index=True, nullable=True
    )
    dataset_name: Mapped[str] = mapped_column(String(200))
    retrieval_mode: Mapped[str] = mapped_column(String(20))
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    experiment_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baseline_evaluation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parameters_json: Mapped[dict | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    results_json: Mapped[dict | None] = mapped_column(JSON_DOCUMENT, nullable=True)
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


class IndexJobRecord(Base):
    """A replayable intent row for the external search index (an outbox).

    PostgreSQL holds the chunk text; Milvus and Elasticsearch hold a copy that can always be
    rebuilt from it.  Writing those two stores one after the other is what breaks: the
    external write can succeed and the process die before the document is marked ``ready``
    (indexed but reported as still processing), or the external write can fail after the
    chunks were committed (searchable in PostgreSQL, invisible to retrieval).  A row here is
    the missing durable step: "this document's external index is stale, and here is what the
    last known desired state is".

    Deliberately **no foreign keys**.  The row describes work that must still happen after the
    document - or its whole knowledge base - is gone, which is exactly when an ``ondelete``
    cascade would delete the only evidence that the external index still needs cleaning up.
    One row per document, upserted on every change, keeps the queue bounded and makes a replay
    converge on the latest desired state instead of racing the earlier ones.
    """

    __tablename__ = "index_outbox"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_index_outbox_document_id"),
        CheckConstraint(
            _status_check("status", INDEX_JOB_STATUSES),
            name="ck_index_outbox_status",
        ),
        CheckConstraint(
            _status_check("operation", INDEX_JOB_OPERATIONS),
            name="ck_index_outbox_operation",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(36), index=True)
    operation: Mapped[str] = mapped_column(String(16), default="upsert")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class ApiSessionRecord(Base):
    """A short-lived browser session won by presenting the long-lived API key once.

    Storing the session here rather than in the browser is the point: the row can expire,
    can be revoked, and names which key minted it (`key_fingerprint`, a truncated hash -
    never the key itself) so an operator can answer "who is using this tenant" and "kill
    that session" without rotating the shared credential.

    `token_hash` is unique because it is the lookup key: authentication hashes the bearer
    token and asks for exactly one row. There is no foreign key to a tenant table because
    tenants live in configuration in this schema, so a session cannot cascade away with a
    row it does not own.
    """

    __tablename__ = "api_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_fingerprint: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
