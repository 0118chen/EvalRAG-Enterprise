"""Database persistence shared by local development and production deployments."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import and_, create_engine, delete, event, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.ingestion import Chunk
from app.db.models import (
    Base,
    ChunkRecord,
    DocumentRecord,
    EvaluationDatasetRecord,
    EvaluationExampleRecord,
    EvaluationRecord,
    FeedbackRecord,
    KnowledgeBaseRecord,
    utc_now,
)
from app.schemas import (
    Document,
    EvaluationDataset,
    EvaluationExample,
    EvidenceMode,
    EvidenceSpan,
    KnowledgeBase,
)


def normalize_database_url(database_url: str) -> str:
    """Use the installed psycopg driver for PostgreSQL URLs."""
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql+asyncpg://")
    return database_url


def _is_memory_sqlite(database_url: str) -> bool:
    """In-memory SQLite exists only inside its connection, so it needs one shared one."""
    return database_url.startswith("sqlite") and (
        ":memory:" in database_url or "mode=memory" in database_url
    )


class SQLAlchemyStore:
    """Synchronous SQLAlchemy store for SQLite and PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        self.database_url = normalize_database_url(database_url)
        connect_args = {"check_same_thread": False} if self.database_url.startswith("sqlite") else {}
        self._memory_sqlite = _is_memory_sqlite(self.database_url)
        # An in-memory SQLite database exists only inside its connection, so it must be
        # served by one shared connection (StaticPool) rather than a per-thread pool that
        # would hand each thread its own empty database. SQLAlchemy infers this from the
        # URL today but warns that it will stop doing so, so it is stated explicitly.
        poolclass = StaticPool if self._memory_sqlite else None
        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
            poolclass=poolclass,
        )
        if self.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
            Base.metadata.create_all(self.engine)
            self._upgrade_sqlite_schema()

    def _configure_sqlite_connection(self, dbapi_connection, _connection_record) -> None:
        """Settings every SQLite connection needs to survive concurrent request threads.

        ``foreign_keys`` is off by default in SQLite, so the schema's cascades and checks
        would otherwise silently not apply. ``busy_timeout`` makes a writer wait for a
        concurrent writer instead of failing straight away with "database is locked",
        which is what several threads sharing one database file hit first. WAL lets
        readers keep reading while a writer commits; it is a property of the file, so it
        is skipped for the in-memory database shared through a single connection.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        if not self._memory_sqlite:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def _upgrade_sqlite_schema(self) -> None:
        """Add columns introduced after the original local MVP database."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_tenant_id "
                "ON knowledge_bases (tenant_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_knowledge_base_id "
                "ON documents (knowledge_base_id)"
            )
            for table in ("knowledge_bases", "documents", "evaluations", "feedback"):
                columns = {
                    row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                if columns and "created_at" not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN created_at DATETIME")
                if columns:
                    connection.exec_driver_sql(
                        f"UPDATE {table} SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"
                    )
            document_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(documents)")
            }
            if document_columns and "version" not in document_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE documents ADD COLUMN version VARCHAR(64) DEFAULT 'latest'"
                )
            chunk_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chunks)")
            }
            if chunk_columns and "version" not in chunk_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE chunks ADD COLUMN version VARCHAR(64) DEFAULT 'latest'"
                )
            if chunk_columns:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_version ON chunks (version)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_document_id "
                    "ON chunks (document_id)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_knowledge_base_id "
                    "ON chunks (knowledge_base_id)"
                )
            evaluation_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(evaluations)")
            }
            evaluation_additions = {
                "tenant_id": "VARCHAR(128) DEFAULT 'demo-enterprise'",
                "knowledge_base_id": "VARCHAR(36)",
                "dataset_id": "VARCHAR(36)",
                "experiment_name": "VARCHAR(200)",
                "baseline_evaluation_id": "VARCHAR(36)",
                "parameters_json": "TEXT",
                "completed_at": "DATETIME",
            }
            for column, declaration in evaluation_additions.items():
                if evaluation_columns and column not in evaluation_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE evaluations ADD COLUMN {column} {declaration}"
                    )
            if evaluation_columns:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_evaluations_tenant_id "
                    "ON evaluations (tenant_id)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_evaluations_knowledge_base_id "
                    "ON evaluations (knowledge_base_id)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_evaluations_dataset_id "
                    "ON evaluations (dataset_id)"
                )
            feedback_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(feedback)")
            }
            if feedback_columns and "tenant_id" not in feedback_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE feedback ADD COLUMN tenant_id VARCHAR(128) "
                    "DEFAULT 'demo-enterprise'"
                )
            if feedback_columns:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_feedback_tenant_id "
                    "ON feedback (tenant_id)"
                )
            example_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(evaluation_examples)"
                )
            }
            if example_columns and "expected_answer" not in example_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE evaluation_examples "
                    "ADD COLUMN expected_answer TEXT"
                )

    def save_knowledge_base(self, knowledge_base: KnowledgeBase) -> None:
        with Session(self.engine) as session:
            session.add(KnowledgeBaseRecord(**knowledge_base.model_dump()))
            session.commit()

    def get_knowledge_base(self, knowledge_base_id: str, tenant_id: str) -> KnowledgeBase | None:
        with Session(self.engine) as session:
            record = session.scalar(
                select(KnowledgeBaseRecord).where(
                    KnowledgeBaseRecord.id == knowledge_base_id,
                    KnowledgeBaseRecord.tenant_id == tenant_id,
                )
            )
            return self._knowledge_base(record) if record else None

    def list_knowledge_bases(self, tenant_id: str) -> list[KnowledgeBase]:
        with Session(self.engine) as session:
            records = session.scalars(
                select(KnowledgeBaseRecord)
                .where(KnowledgeBaseRecord.tenant_id == tenant_id)
                .order_by(KnowledgeBaseRecord.created_at.desc(), KnowledgeBaseRecord.id.desc())
            ).all()
            return [self._knowledge_base(record) for record in records]

    def save_document(self, document: Document, chunks: list[Chunk]) -> None:
        with Session(self.engine) as session:
            session.add(
                DocumentRecord(
                    id=document.id,
                    filename=document.filename,
                    knowledge_base_id=document.knowledge_base_id,
                    chunks=document.chunks,
                    status=document.status,
                    progress=document.progress,
                    error_message=document.error_message,
                    version=document.version,
                )
            )
            session.flush()
            session.add_all(self._chunk_records(document.knowledge_base_id, chunks))
            session.commit()

    def update_document_status(self, document_id: str, status: str) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(DocumentRecord)
                .where(DocumentRecord.id == document_id)
                .values(status=status, updated_at=utc_now())
            )
            session.commit()

    def update_document_progress(self, document_id: str, progress: int, error_message: str | None = None) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(DocumentRecord)
                .where(DocumentRecord.id == document_id)
                .values(progress=progress, error_message=error_message, updated_at=utc_now())
            )
            session.commit()

    def claim_document(
        self,
        document_id: str,
        *,
        force: bool = False,
        stale_after_seconds: int = 900,
        now: datetime | None = None,
    ) -> bool:
        """Atomically take ownership of one document so a single worker processes it.

        The claim is a single conditional UPDATE, so two workers receiving the same
        task cannot both proceed: only one of them sees ``rowcount == 1``. Queued and
        previously failed documents are claimable, a ``ready`` document only when
        ``force`` is set to rebuild it, and a ``processing`` document only once its
        timestamp looks stale (the previous worker died and Celery redelivered).
        """
        current = now or utc_now()
        stale_before = current - timedelta(seconds=stale_after_seconds)
        claimable = [
            DocumentRecord.status.in_(("pending", "failed")),
            and_(
                DocumentRecord.status == "processing",
                or_(
                    DocumentRecord.updated_at.is_(None),
                    DocumentRecord.updated_at <= stale_before,
                ),
            ),
        ]
        if force:
            claimable.append(DocumentRecord.status == "ready")
        with Session(self.engine) as session:
            result = cast(
                CursorResult,
                session.execute(
                    update(DocumentRecord)
                    .where(DocumentRecord.id == document_id, or_(*claimable))
                    .values(
                        status="processing",
                        progress=0,
                        error_message=None,
                        updated_at=current,
                    )
                ),
            )
            session.commit()
            return result.rowcount == 1

    def replace_chunks(self, document_id: str, knowledge_base_id: str, chunks: list[Chunk]) -> None:
        """Atomically replace a document index and keep its chunk count accurate."""
        with Session(self.engine) as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.document_id == document_id))
            session.add_all(self._chunk_records(knowledge_base_id, chunks))
            session.execute(
                update(DocumentRecord)
                .where(DocumentRecord.id == document_id)
                .values(chunks=len(chunks))
            )
            session.commit()

    def delete_document(self, document_id: str, tenant_id: str) -> bool:
        with Session(self.engine) as session:
            record = session.scalar(
                select(DocumentRecord)
                .join(KnowledgeBaseRecord, KnowledgeBaseRecord.id == DocumentRecord.knowledge_base_id)
                .where(DocumentRecord.id == document_id, KnowledgeBaseRecord.tenant_id == tenant_id)
            )
            if not record:
                return False
            session.execute(delete(ChunkRecord).where(ChunkRecord.document_id == document_id))
            session.delete(record)
            session.commit()
            return True

    def get_chunks(
        self,
        knowledge_base_id: str,
        document_version: str | None = None,
    ) -> list[Chunk]:
        with Session(self.engine) as session:
            statement = select(ChunkRecord).where(
                ChunkRecord.knowledge_base_id == knowledge_base_id
            )
            if document_version:
                statement = statement.where(ChunkRecord.version == document_version)
            records = session.scalars(statement.order_by(ChunkRecord.id)).all()
            return [
                Chunk(
                    id=record.id,
                    document_id=record.document_id,
                    page=record.page,
                    text=record.text,
                    version=record.version,
                )
                for record in records
            ]

    def get_document(self, document_id: str, tenant_id: str) -> Document | None:
        with Session(self.engine) as session:
            record = session.scalar(
                select(DocumentRecord)
                .join(KnowledgeBaseRecord, KnowledgeBaseRecord.id == DocumentRecord.knowledge_base_id)
                .where(DocumentRecord.id == document_id, KnowledgeBaseRecord.tenant_id == tenant_id)
            )
            return self._document(record) if record else None

    def get_document_any(self, document_id: str) -> Document | None:
        with Session(self.engine) as session:
            record = session.get(DocumentRecord, document_id)
            return self._document(record) if record else None

    def list_documents(self, knowledge_base_id: str, tenant_id: str) -> list[Document]:
        with Session(self.engine) as session:
            records = session.scalars(
                select(DocumentRecord)
                .join(KnowledgeBaseRecord, KnowledgeBaseRecord.id == DocumentRecord.knowledge_base_id)
                .where(
                    DocumentRecord.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseRecord.tenant_id == tenant_id,
                )
                .order_by(DocumentRecord.created_at.desc(), DocumentRecord.id.desc())
            ).all()
            return [self._document(record) for record in records]

    def save_evaluation_dataset(self, dataset: EvaluationDataset) -> None:
        with Session(self.engine) as session:
            session.add(
                EvaluationDatasetRecord(
                    id=dataset.id,
                    tenant_id=dataset.tenant_id,
                    knowledge_base_id=dataset.knowledge_base_id,
                    name=dataset.name,
                    description=dataset.description,
                    created_at=dataset.created_at or datetime.now(UTC).replace(tzinfo=None),
                )
            )
            session.add_all(
                [
                    EvaluationExampleRecord(
                        id=example.id,
                        dataset_id=dataset.id,
                        question=example.question,
                        expected_answer=example.expected_answer,
                        expected_document_id=example.expected_document_id,
                        expected_page=example.expected_page,
                        evidence_quote=example.evidence_quote,
                        category=example.category,
                        should_refuse=example.should_refuse,
                        evidence_mode=example.evidence_mode,
                        expected_evidence_json=(
                            json.dumps(
                                [span.model_dump() for span in example.expected_evidence],
                                ensure_ascii=False,
                            )
                            if example.expected_evidence
                            else None
                        ),
                    )
                    for example in dataset.examples
                ]
            )
            session.commit()

    def get_evaluation_dataset(
        self,
        dataset_id: str,
        tenant_id: str | None = None,
    ) -> EvaluationDataset | None:
        with Session(self.engine) as session:
            statement = select(EvaluationDatasetRecord).where(
                EvaluationDatasetRecord.id == dataset_id
            )
            if tenant_id:
                statement = statement.where(EvaluationDatasetRecord.tenant_id == tenant_id)
            record = session.scalar(statement)
            if not record:
                return None
            examples = session.scalars(
                select(EvaluationExampleRecord)
                .where(EvaluationExampleRecord.dataset_id == dataset_id)
                .order_by(EvaluationExampleRecord.id)
            ).all()
            return self._evaluation_dataset(record, examples)

    def get_evaluation_dataset_by_name(
        self,
        name: str,
        tenant_id: str,
    ) -> EvaluationDataset | None:
        with Session(self.engine) as session:
            record = session.scalar(
                select(EvaluationDatasetRecord).where(
                    EvaluationDatasetRecord.name == name,
                    EvaluationDatasetRecord.tenant_id == tenant_id,
                )
            )
            if not record:
                return None
            examples = session.scalars(
                select(EvaluationExampleRecord)
                .where(EvaluationExampleRecord.dataset_id == record.id)
                .order_by(EvaluationExampleRecord.id)
            ).all()
            return self._evaluation_dataset(record, examples)

    def list_evaluation_datasets(self, tenant_id: str) -> list[EvaluationDataset]:
        with Session(self.engine) as session:
            records = session.scalars(
                select(EvaluationDatasetRecord)
                .where(EvaluationDatasetRecord.tenant_id == tenant_id)
                .order_by(
                    EvaluationDatasetRecord.created_at.desc(),
                    EvaluationDatasetRecord.id.desc(),
                )
            ).all()
            datasets: list[EvaluationDataset] = []
            for record in records:
                examples = session.scalars(
                    select(EvaluationExampleRecord)
                    .where(EvaluationExampleRecord.dataset_id == record.id)
                    .order_by(EvaluationExampleRecord.id)
                ).all()
                datasets.append(self._evaluation_dataset(record, examples))
            return datasets

    def create_evaluation(
        self,
        evaluation_id: str,
        dataset_name: str,
        retrieval_mode: str,
        top_k: int,
        *,
        tenant_id: str = "demo-enterprise",
        knowledge_base_id: str | None = None,
        dataset_id: str | None = None,
        experiment_name: str | None = None,
        baseline_evaluation_id: str | None = None,
        parameters: dict | None = None,
    ) -> None:
        with Session(self.engine) as session:
            session.add(
                EvaluationRecord(
                    id=evaluation_id,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    dataset_id=dataset_id,
                    dataset_name=dataset_name,
                    retrieval_mode=retrieval_mode,
                    top_k=top_k,
                    experiment_name=experiment_name,
                    baseline_evaluation_id=baseline_evaluation_id,
                    parameters_json=json.dumps(parameters or {}, ensure_ascii=False),
                    status="queued",
                )
            )
            session.commit()

    def list_evaluations(
        self,
        tenant_id: str,
        *,
        dataset_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        with Session(self.engine) as session:
            statement = select(EvaluationRecord).where(
                EvaluationRecord.tenant_id == tenant_id
            )
            if dataset_name:
                statement = statement.where(EvaluationRecord.dataset_name == dataset_name)
            if status:
                statement = statement.where(EvaluationRecord.status == status)
            records = session.scalars(
                statement.order_by(
                    EvaluationRecord.created_at.desc(),
                    EvaluationRecord.id.desc(),
                ).limit(limit)
            ).all()
            return [self._evaluation(record) for record in records]

    def get_evaluation(self, evaluation_id: str) -> dict | None:
        with Session(self.engine) as session:
            record = session.get(EvaluationRecord, evaluation_id)
            return self._evaluation(record) if record else None

    def update_evaluation(
        self,
        evaluation_id: str,
        status: str,
        results: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(EvaluationRecord)
                .where(EvaluationRecord.id == evaluation_id)
                .values(
                    status=status,
                    results_json=json.dumps(results, ensure_ascii=False) if results is not None else None,
                    error_message=error_message,
                    completed_at=(
                        datetime.now(UTC).replace(tzinfo=None)
                        if status in {"completed", "failed"}
                        else None
                    ),
                )
            )
            session.commit()

    def save_feedback(
        self,
        feedback_id: str,
        trace_id: str,
        feedback: str,
        comment: str,
        rag_version: str,
        prompt_version: str,
        tenant_id: str = "demo-enterprise",
    ) -> None:
        with Session(self.engine) as session:
            session.add(
                FeedbackRecord(
                    id=feedback_id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    feedback=feedback,
                    comment=comment,
                    rag_version=rag_version,
                    prompt_version=prompt_version,
                )
            )
            session.commit()

    @staticmethod
    def _knowledge_base(record: KnowledgeBaseRecord) -> KnowledgeBase:
        return KnowledgeBase(
            id=record.id,
            tenant_id=record.tenant_id,
            name=record.name,
            description=record.description,
        )

    @staticmethod
    def _document(record: DocumentRecord) -> Document:
        return Document(
            id=record.id,
            filename=record.filename,
            knowledge_base_id=record.knowledge_base_id,
            chunks=record.chunks,
            status=record.status,
            progress=record.progress,
            error_message=record.error_message,
            version=record.version,
        )

    @staticmethod
    def _evaluation(record: EvaluationRecord) -> dict:
        return {
            "id": record.id,
            "tenant_id": record.tenant_id,
            "knowledge_base_id": record.knowledge_base_id,
            "dataset_id": record.dataset_id,
            "dataset_name": record.dataset_name,
            "retrieval_mode": record.retrieval_mode,
            "top_k": record.top_k,
            "experiment_name": record.experiment_name,
            "baseline_evaluation_id": record.baseline_evaluation_id,
            "parameters": json.loads(record.parameters_json) if record.parameters_json else {},
            "status": record.status,
            "results": json.loads(record.results_json) if record.results_json else None,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        }

    @staticmethod
    def _evaluation_dataset(
        record: EvaluationDatasetRecord,
        examples: Sequence[EvaluationExampleRecord],
    ) -> EvaluationDataset:
        return EvaluationDataset(
            id=record.id,
            tenant_id=record.tenant_id,
            knowledge_base_id=record.knowledge_base_id,
            name=record.name,
            description=record.description,
            created_at=record.created_at,
            examples=[
                EvaluationExample(
                    id=example.id,
                    dataset_id=example.dataset_id,
                    question=example.question,
                    expected_answer=example.expected_answer,
                    expected_document_id=example.expected_document_id,
                    expected_page=example.expected_page,
                    evidence_quote=example.evidence_quote,
                    category=example.category,
                    should_refuse=bool(example.should_refuse),
                    evidence_mode=cast(EvidenceMode, example.evidence_mode),
                    expected_evidence=[
                        EvidenceSpan(**span)
                        for span in (
                            json.loads(example.expected_evidence_json)
                            if example.expected_evidence_json
                            else []
                        )
                    ],
                )
                for example in examples
            ],
        )

    @staticmethod
    def _chunk_records(knowledge_base_id: str, chunks: list[Chunk]) -> list[ChunkRecord]:
        return [
            ChunkRecord(
                id=chunk.id,
                document_id=chunk.document_id,
                page=chunk.page,
                text=chunk.text,
                knowledge_base_id=knowledge_base_id,
                version=chunk.version,
            )
            for chunk in chunks
        ]


class SQLiteStore(SQLAlchemyStore):
    """Backward-compatible path-based constructor used by local tooling."""

    def __init__(self, path: str = "data/evalrag.db") -> None:
        database_path = Path(path).resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(f"sqlite:///{database_path.as_posix()}")


def create_store(database_url: str) -> SQLAlchemyStore:
    return SQLAlchemyStore(database_url)
