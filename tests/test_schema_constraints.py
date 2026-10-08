"""The three schema guarantees of revision 0011, checked where they can actually fail.

* a bad status is rejected (CHECK), both by the API literals and by SQLite itself;
* deleting a knowledge base takes its documents, chunks and datasets with it (CASCADE) but
  leaves evaluation history behind with the links set to NULL (SET NULL);
* JSON payloads survive a round trip as data, not as escaped text.

PostgreSQL is exercised in CI (`docker` service container); SQLite enforces the same
constraints here because `SQLAlchemyStore` turns `PRAGMA foreign_keys` on.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ingestion import Chunk
from app.core.store import SQLAlchemyStore
from app.db.models import (
    DOCUMENT_STATUSES,
    EVALUATION_STATUSES,
    ChunkRecord,
    DocumentRecord,
    EvaluationDatasetRecord,
    EvaluationExampleRecord,
    EvaluationRecord,
)
from app.schemas import (
    Document,
    DocumentStatus,
    EvaluationDataset,
    EvaluationExample,
    EvaluationStatus,
    EvidenceSpan,
    KnowledgeBase,
)

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture
def store(tmp_path: Path) -> SQLAlchemyStore:
    return SQLAlchemyStore(f"sqlite:///{tmp_path / 'schema.db'}")


def knowledge_base(store: SQLAlchemyStore, knowledge_base_id: str = "kb-1", tenant: str = TENANT) -> str:
    store.save_knowledge_base(
        KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant,
            name=f"corpus {knowledge_base_id}",
            description="",
        )
    )
    return knowledge_base_id


def document(store: SQLAlchemyStore, knowledge_base_id: str) -> str:
    store.save_document(
        Document(
            id="doc-1",
            filename="rules.txt",
            knowledge_base_id=knowledge_base_id,
            chunks=2,
            status="ready",
        ),
        [Chunk(f"chunk-{index}", "doc-1", 1, f"text {index}") for index in range(2)],
    )
    return "doc-1"


def dataset(store: SQLAlchemyStore, knowledge_base_id: str) -> str:
    store.save_evaluation_dataset(
        EvaluationDataset(
            id="ds-1",
            tenant_id=TENANT,
            knowledge_base_id=knowledge_base_id,
            name="golden",
            description="",
            examples=[
                EvaluationExample(
                    id="ex-1",
                    dataset_id="ds-1",
                    question="五百元还是三千元？",
                    expected_answer="五百元",
                    expected_document_id="doc-1",
                    expected_page=1,
                    expected_evidence=[
                        EvidenceSpan(document_id="doc-1", page=1, quote="成员首次出资额不得低于人民币五百元")
                    ],
                )
            ],
        )
    )
    return "ds-1"


def evaluation(store: SQLAlchemyStore, knowledge_base_id: str, dataset_id: str) -> str:
    store.create_evaluation(
        "eval-1",
        dataset_name="golden",
        retrieval_mode="hybrid",
        top_k=5,
        tenant_id=TENANT,
        knowledge_base_id=knowledge_base_id,
        dataset_id=dataset_id,
        parameters={"top_k": 5, "notes": "人工核对", "nested": {"modes": ["dense", "sparse"]}},
    )
    return "eval-1"


def _count(store: SQLAlchemyStore, model: type) -> int:
    with Session(store.engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


# --- statuses ---------------------------------------------------------------------------------


def test_api_literals_match_the_status_sets_the_check_constraints_allow() -> None:
    assert get_args(DocumentStatus) == DOCUMENT_STATUSES
    assert get_args(EvaluationStatus) == EVALUATION_STATUSES


def test_a_status_typo_is_rejected_before_it_reaches_the_database() -> None:
    with pytest.raises(ValidationError):
        Document(id="doc-1", filename="a.txt", knowledge_base_id="kb-1", chunks=0, status="redy")


def test_the_database_rejects_a_document_status_it_does_not_declare(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store)
    with Session(store.engine) as session:
        session.add(
            DocumentRecord(
                id="doc-typo",
                filename="a.txt",
                knowledge_base_id=knowledge_base_id,
                status="redy",
            )
        )
        with pytest.raises(IntegrityError, match="ck_documents_status"):
            session.commit()


def test_the_database_rejects_an_evaluation_status_it_does_not_declare(store: SQLAlchemyStore) -> None:
    with Session(store.engine) as session:
        session.add(
            EvaluationRecord(
                id="eval-typo",
                tenant_id=TENANT,
                dataset_name="golden",
                retrieval_mode="hybrid",
                top_k=5,
                status="finishd",
            )
        )
        with pytest.raises(IntegrityError, match="ck_evaluations_status"):
            session.commit()


@pytest.mark.parametrize("status", DOCUMENT_STATUSES)
def test_every_declared_document_status_is_accepted(store: SQLAlchemyStore, status: str) -> None:
    knowledge_base_id = knowledge_base(store)
    with Session(store.engine) as session:
        session.add(
            DocumentRecord(
                id=f"doc-{status}",
                filename="a.txt",
                knowledge_base_id=knowledge_base_id,
                status=status,
            )
        )
        session.commit()


# --- cascades ---------------------------------------------------------------------------------


def test_deleting_a_knowledge_base_takes_its_documents_chunks_and_datasets(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store)
    document(store, knowledge_base_id)
    dataset_id = dataset(store, knowledge_base_id)
    evaluation(store, knowledge_base_id, dataset_id)
    assert (_count(store, DocumentRecord), _count(store, ChunkRecord)) == (1, 2)

    assert store.delete_knowledge_base(knowledge_base_id, TENANT) is True

    assert _count(store, DocumentRecord) == 0
    assert _count(store, ChunkRecord) == 0
    assert _count(store, EvaluationDatasetRecord) == 0
    assert _count(store, EvaluationExampleRecord) == 0


def test_evaluation_history_survives_the_corpus_it_measured(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store)
    document(store, knowledge_base_id)
    dataset_id = dataset(store, knowledge_base_id)
    evaluation(store, knowledge_base_id, dataset_id)

    store.delete_knowledge_base(knowledge_base_id, TENANT)

    record = store.get_evaluation("eval-1")
    assert record is not None
    assert record["knowledge_base_id"] is None
    assert record["dataset_id"] is None
    assert record["dataset_name"] == "golden"


def test_deleting_another_tenants_knowledge_base_is_refused(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store, tenant=OTHER_TENANT)

    assert store.delete_knowledge_base(knowledge_base_id, TENANT) is False
    assert store.get_knowledge_base(knowledge_base_id, OTHER_TENANT) is not None


def test_deleting_a_missing_knowledge_base_reports_no_deletion(store: SQLAlchemyStore) -> None:
    assert store.delete_knowledge_base("kb-missing", TENANT) is False


def test_deleting_a_document_still_leaves_the_dataset_alone(store: SQLAlchemyStore) -> None:
    """The other direction: removing one document must not cascade into evaluation data."""
    knowledge_base_id = knowledge_base(store)
    document(store, knowledge_base_id)
    dataset(store, knowledge_base_id)

    assert store.delete_document("doc-1", TENANT) is True

    assert _count(store, DocumentRecord) == 0
    assert _count(store, EvaluationDatasetRecord) == 1
    assert _count(store, EvaluationExampleRecord) == 1


# --- JSON payloads ----------------------------------------------------------------------------


def test_parameters_and_results_come_back_as_the_objects_that_went_in(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store)
    dataset_id = dataset(store, knowledge_base_id)
    evaluation(store, knowledge_base_id, dataset_id)
    results = {
        "overall": {"recall_at_5": 0.75, "mrr": 0.5},
        "notes": "全对",
        "per_example": [{"question": "五百元还是三千元？", "passed": True}],
    }

    store.update_evaluation("eval-1", "completed", results=results)

    record = store.get_evaluation("eval-1")
    assert record is not None
    assert record["parameters"] == {
        "top_k": 5,
        "notes": "人工核对",
        "nested": {"modes": ["dense", "sparse"]},
    }
    assert record["results"] == results
    assert record["status"] == "completed"


def test_json_is_stored_as_readable_text_not_escaped_ascii(store: SQLAlchemyStore) -> None:
    """`json_serializer` keeps the database usable for whoever debugs a bad run."""
    knowledge_base_id = knowledge_base(store)
    dataset_id = dataset(store, knowledge_base_id)
    evaluation(store, knowledge_base_id, dataset_id)
    store.update_evaluation("eval-1", "completed", results={"notes": "全对"})

    with Session(store.engine) as session:
        raw = session.scalar(text("SELECT results_json FROM evaluations WHERE id = 'eval-1'"))

    assert "全对" in str(raw)


def test_expected_evidence_hops_survive_the_round_trip(store: SQLAlchemyStore) -> None:
    knowledge_base_id = knowledge_base(store)
    dataset_id = dataset(store, knowledge_base_id)

    stored = store.get_evaluation_dataset(dataset_id)

    assert stored is not None
    assert stored.examples[0].expected_evidence == [
        EvidenceSpan(document_id="doc-1", page=1, quote="成员首次出资额不得低于人民币五百元")
    ]
