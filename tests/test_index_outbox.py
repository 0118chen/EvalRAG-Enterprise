"""The index outbox: PostgreSQL is the source of truth, the search index is replayable.

`process_document` used to write the external index first, then the chunks, then the status.
A crash in between left a document reporting `processing` forever, or a search index holding a
copy of a document PostgreSQL no longer had - and nothing could tell which. Now the chunk write
and the intent to index are one transaction, and a replayable worker performs the external
write, so the only inconsistency the system can be in is "the index is behind" - which the next
sweep fixes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import tasks
from app.core.ingestion import Chunk
from app.core.store import SQLAlchemyStore
from app.db.models import IndexJobRecord, utc_now
from app.schemas import Document, IndexJob, KnowledgeBase

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture
def store(tmp_path: Path) -> SQLAlchemyStore:
    return SQLAlchemyStore(f"sqlite:///{tmp_path / 'outbox.db'}")


def knowledge_base(
    store: SQLAlchemyStore,
    knowledge_base_id: str = "kb-1",
    tenant: str = TENANT,
) -> str:
    store.save_knowledge_base(
        KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant,
            name=f"corpus {knowledge_base_id}",
            description="",
        )
    )
    return knowledge_base_id


def document(
    store: SQLAlchemyStore,
    knowledge_base_id: str,
    document_id: str = "doc-1",
    status: str = "processing",
) -> str:
    store.save_document(
        Document(
            id=document_id,
            filename="rules.txt",
            knowledge_base_id=knowledge_base_id,
            chunks=0,
            status=status,
        ),
        [],
    )
    return document_id


def chunks(document_id: str = "doc-1", count: int = 2) -> list[Chunk]:
    return [
        Chunk(f"{document_id}-c{index}", document_id, 1, f"text {index}")
        for index in range(count)
    ]


def job(store: SQLAlchemyStore, document_id: str) -> IndexJob:
    """The outbox row of one document; the tests below fail loudly if it is missing."""
    row = store.get_index_job(document_id)
    assert row is not None, f"no outbox row for {document_id}"
    return row


class FakePipeline:
    """Stands in for the ingestion pipeline; records what a real one would have been asked."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.replacements: list[tuple[list[Chunk], str, str]] = []
        self.deletions: list[str] = []

    async def replace_document(
        self, chunks: list[Chunk], knowledge_base_id: str, document_id: str
    ) -> None:
        if self.error is not None:
            raise self.error
        self.replacements.append((chunks, knowledge_base_id, document_id))

    async def delete_document(self, document_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.deletions.append(document_id)


class SelectivePipeline(FakePipeline):
    """Publishes everything except the documents named as broken."""

    def __init__(self, broken: set[str]) -> None:
        super().__init__()
        self.broken = broken

    async def replace_document(
        self, chunks: list[Chunk], knowledge_base_id: str, document_id: str
    ) -> None:
        if document_id in self.broken:
            raise RuntimeError("milvus refused")
        await super().replace_document(chunks, knowledge_base_id, document_id)


def use_pipeline(monkeypatch: pytest.MonkeyPatch, pipeline: FakePipeline) -> None:
    monkeypatch.setattr(tasks, "create_ingestion_pipeline", lambda _settings: pipeline)


def use_store(monkeypatch: pytest.MonkeyPatch, store: SQLAlchemyStore) -> None:
    monkeypatch.setattr(tasks, "create_store", lambda _url: store)


def age_the_rows(store: SQLAlchemyStore, seconds: int = 120) -> None:
    """Make the outbox look like the sweeper would find it: older than the retry delay."""
    with Session(store.engine) as session:
        session.execute(
            update(IndexJobRecord).values(updated_at=utc_now() - timedelta(seconds=seconds))
        )
        session.commit()


def run_together(worker: Callable[[int], Any], count: int = 4) -> list[Any]:
    """Run ``worker(index)`` in ``count`` threads released by one barrier.

    Same reasoning as ``tests/test_concurrency.py``: threads that merely start at the same
    time arrive one after another and quietly turn a race into a sequence.
    """
    barrier = threading.Barrier(count)

    def wrapped(index: int) -> Any:
        barrier.wait(timeout=30)
        return worker(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(wrapped, range(count)))


# --- the commit point -------------------------------------------------------------------------


def test_chunks_and_the_index_intent_are_committed_together(store: SQLAlchemyStore) -> None:
    knowledge_base(store)
    document(store, "kb-1")

    store.save_document_index("doc-1", "kb-1", chunks())

    assert [chunk.id for chunk in store.get_document_chunks("doc-1")] == [
        "doc-1-c0",
        "doc-1-c1",
    ]
    row = job(store, "doc-1")
    assert (row.operation, row.status, row.attempts, row.last_error) == (
        "upsert",
        "pending",
        0,
        None,
    )
    assert row.knowledge_base_id == "kb-1"


def test_uploading_a_document_does_not_claim_it_is_indexed(store: SQLAlchemyStore) -> None:
    """Only ingestion's commit point enqueues; a queued upload has nothing to index yet."""
    knowledge_base(store)

    document(store, "kb-1", status="pending")

    assert store.list_index_jobs() == []


def test_reindexing_supersedes_the_previous_intent_instead_of_queueing(
    store: SQLAlchemyStore,
) -> None:
    """One row per document: five edits produce one replay, not five."""
    knowledge_base(store)
    document(store, "kb-1")
    store.save_document_index("doc-1", "kb-1", chunks(count=1))
    claimed = store.claim_index_job(document_id="doc-1")
    assert claimed is not None
    store.fail_index_job(claimed.id, "milvus refused")
    assert job(store, "doc-1").attempts == 1

    store.save_document_index("doc-1", "kb-1", chunks(count=3))

    assert len(store.list_index_jobs()) == 1
    row = job(store, "doc-1")
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, None)
    assert len(store.get_document_chunks("doc-1")) == 3


# --- claiming ---------------------------------------------------------------------------------


def test_only_one_worker_claims_a_row(store: SQLAlchemyStore) -> None:
    store.enqueue_index_job("doc-1", "kb-1")

    claims = run_together(lambda _index: store.claim_index_job(document_id="doc-1"))

    assert sum(claim is not None for claim in claims) == 1
    assert job(store, "doc-1").status == "processing"
    assert store.claim_index_job(document_id="doc-1") is None, "a fresh claim was stolen"


def test_a_dead_worker_does_not_hold_the_row_forever(store: SQLAlchemyStore) -> None:
    store.enqueue_index_job("doc-1", "kb-1")
    assert store.claim_index_job(document_id="doc-1") is not None

    assert store.claim_index_job(document_id="doc-1", stale_after_seconds=3600) is None
    assert store.claim_index_job(document_id="doc-1", stale_after_seconds=0) is not None


def test_a_fresh_row_is_not_due_for_another_sweep_yet(store: SQLAlchemyStore) -> None:
    """`due_after_seconds` is what stops one sweep from burning a row's whole budget."""
    store.enqueue_index_job("doc-1", "kb-1")

    assert store.claim_index_job(document_id="doc-1", due_after_seconds=60) is None
    assert store.claim_index_job(document_id="doc-1") is not None


def test_the_retry_budget_turns_a_hopeless_row_into_a_visible_failure(
    store: SQLAlchemyStore,
) -> None:
    store.enqueue_index_job("doc-1", "kb-1")
    claimed = store.claim_index_job(document_id="doc-1")
    assert claimed is not None

    retried = store.fail_index_job(claimed.id, "milvus is down")
    assert retried is not None
    assert (retried.status, retried.attempts, retried.last_error) == (
        "pending",
        1,
        "milvus is down",
    )

    again = store.fail_index_job(claimed.id, "milvus is down")
    assert again is not None
    assert (again.status, again.attempts) == ("pending", 2)

    parked = store.fail_index_job(claimed.id, "milvus is still down")
    assert parked is not None
    assert (parked.status, parked.attempts) == ("failed", 3)
    assert [row.status for row in store.list_index_jobs()] == ["failed"]
    assert store.list_index_jobs(status="pending") == []


def test_a_long_error_is_truncated_to_what_the_column_holds(store: SQLAlchemyStore) -> None:
    store.enqueue_index_job("doc-1", "kb-1")
    claimed = store.claim_index_job(document_id="doc-1")
    assert claimed is not None

    store.fail_index_job(claimed.id, "x" * 5000)
    assert len(job(store, "doc-1").last_error or "") == 2000

    store.fail_index_job(claimed.id, None)
    assert job(store, "doc-1").last_error is None


# --- the intents the write paths leave behind -------------------------------------------------


def test_deleting_a_document_leaves_a_delete_intent(store: SQLAlchemyStore) -> None:
    knowledge_base(store)
    document(store, "kb-1", status="ready")
    store.replace_chunks("doc-1", "kb-1", chunks())

    assert store.delete_document("doc-1", TENANT) is True

    assert job(store, "doc-1").operation == "delete"
    assert store.get_document_chunks("doc-1") == []


def test_deleting_a_knowledge_base_leaves_one_delete_intent_per_document(
    store: SQLAlchemyStore,
) -> None:
    knowledge_base(store)
    document(store, "kb-1", document_id="doc-1", status="ready")
    document(store, "kb-1", document_id="doc-2", status="ready")
    knowledge_base(store, "kb-2", tenant=OTHER_TENANT)
    document(store, "kb-2", document_id="doc-3", status="ready")

    assert store.delete_knowledge_base("kb-1", TENANT) is True

    assert {row.document_id: row.operation for row in store.list_index_jobs()} == {
        "doc-1": "delete",
        "doc-2": "delete",
    }
    assert store.get_document_any("doc-3") is not None


def test_a_refused_delete_leaves_no_intent_behind(store: SQLAlchemyStore) -> None:
    """The intents are written with the delete, so a tenant mismatch rolls them back too."""
    knowledge_base(store)
    document(store, "kb-1", status="ready")

    assert store.delete_knowledge_base("kb-1", OTHER_TENANT) is False

    assert store.list_index_jobs() == []
    assert store.get_document_any("doc-1") is not None


# --- the replayable worker --------------------------------------------------------------------


def test_a_replay_publishes_the_chunks_postgres_has(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge_base(store)
    document(store, "kb-1")
    store.save_document_index("doc-1", "kb-1", chunks())
    pipeline = FakePipeline()
    use_pipeline(monkeypatch, pipeline)

    result = tasks._sync_document_index(store, "doc-1")

    assert result["stage"] == "indexed"
    assert pipeline.replacements == [(store.get_document_chunks("doc-1"), "kb-1", "doc-1")]
    assert job(store, "doc-1").status == "done"
    record = store.get_document_any("doc-1")
    assert record is not None
    assert (record.status, record.progress) == ("ready", 100)


def test_a_replay_of_a_delete_intent_clears_the_index(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row outlives the document precisely so this can still be done."""
    knowledge_base(store)
    document(store, "kb-1", status="ready")
    store.replace_chunks("doc-1", "kb-1", chunks())
    assert store.delete_document("doc-1", TENANT) is True
    pipeline = FakePipeline()
    use_pipeline(monkeypatch, pipeline)

    result = tasks._sync_document_index(store, "doc-1")

    assert result["stage"] == "deleted"
    assert pipeline.deletions == ["doc-1"]
    assert job(store, "doc-1").status == "done"


def test_a_replay_with_nothing_to_do_says_so(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_pipeline(monkeypatch, FakePipeline())

    assert tasks._sync_document_index(store, "doc-1")["stage"] == "no_index_job"


def test_a_failed_replay_is_booked_and_the_document_stays_unfinished(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure has to be both visible on the row and raised to the caller."""
    knowledge_base(store)
    document(store, "kb-1")
    store.save_document_index("doc-1", "kb-1", chunks())
    use_pipeline(monkeypatch, FakePipeline(RuntimeError("milvus refused")))

    with pytest.raises(RuntimeError, match="milvus refused"):
        tasks._sync_document_index(store, "doc-1")

    row = job(store, "doc-1")
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "milvus refused")
    record = store.get_document_any("doc-1")
    assert record is not None
    assert record.status == "processing"


def test_the_sweeper_replays_what_the_first_attempt_could_not(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge_base(store)
    document(store, "kb-1")
    store.save_document_index("doc-1", "kb-1", chunks())
    use_pipeline(monkeypatch, FakePipeline(RuntimeError("milvus refused")))
    with pytest.raises(RuntimeError):
        tasks._sync_document_index(store, "doc-1")

    pipeline = FakePipeline()
    use_pipeline(monkeypatch, pipeline)
    use_store(monkeypatch, store)

    result = tasks._replay_index_outbox(due_after_seconds=0)

    assert result == {"replayed": "1", "failed": "0"}
    assert job(store, "doc-1").status == "done"
    assert pipeline.replacements != []
    record = store.get_document_any("doc-1")
    assert record is not None
    assert (record.status, record.progress) == ("ready", 100)


def test_the_sweeper_stops_when_nothing_is_due(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_store(monkeypatch, store)
    use_pipeline(monkeypatch, FakePipeline())

    assert tasks._replay_index_outbox() == {"replayed": "0", "failed": "0"}


def test_the_default_sweep_leaves_a_just_committed_row_for_later(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delay is deliberate: ingestion gets a moment to publish before a sweep races it."""
    knowledge_base(store)
    document(store, "kb-1")
    store.save_document_index("doc-1", "kb-1", chunks())
    use_store(monkeypatch, store)
    use_pipeline(monkeypatch, FakePipeline())

    assert tasks._replay_index_outbox() == {"replayed": "0", "failed": "0"}
    assert job(store, "doc-1").status == "pending"


def test_the_sweeper_moves_on_instead_of_hammering_one_broken_document(
    store: SQLAlchemyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry delay is what makes a sweep publish the healthy documents after a failure."""
    knowledge_base(store)
    for document_id in ("doc-1", "doc-2"):
        document(store, "kb-1", document_id=document_id, status="ready")
        store.save_document_index(document_id, "kb-1", chunks(document_id))
    age_the_rows(store)
    use_pipeline(monkeypatch, SelectivePipeline({"doc-1"}))
    use_store(monkeypatch, store)

    result = tasks._replay_index_outbox(limit=2)

    assert result == {"replayed": "1", "failed": "1"}
    assert job(store, "doc-1").status == "pending"
    assert job(store, "doc-2").status == "done"
