"""Concurrency that a single-threaded test cannot check.

The application makes three claims about itself that only several threads hitting the same
database at the same instant can test:

* ``claim_document`` is "a single conditional UPDATE, so two workers receiving the same
  task cannot both proceed" - so exactly one of N simultaneous claimants may win;
* a live worker's claim must not be stolen, but a stale one may be taken over;
* ``replace_chunks`` is "atomic", so a reader polling during a replacement must never see
  a half-written index (no documents missing, no duplicate set).

Every test runs against a real database file with one connection per thread, and the same
bodies run against PostgreSQL when ``EVALRAG_TEST_DATABASE_URL`` points at a database that
has been migrated (``alembic upgrade head``); that is what CI does. The PostgreSQL run is
skipped locally rather than faked, because the guarantee under test is the database's.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from app.core.ingestion import Chunk
from app.core.store import SQLAlchemyStore
from app.db.models import utc_now
from app.schemas import Document, KnowledgeBase

WORKERS = 8
POSTGRES_URL_ENV = "EVALRAG_TEST_DATABASE_URL"
TENANT = "tenant-concurrency"
# Deleted children first so the foreign keys stay satisfied on PostgreSQL too.
OWNED_TABLES = (
    ("chunks", "knowledge_base_id"),
    ("documents", "knowledge_base_id"),
    ("knowledge_bases", "id"),
)


@dataclass
class Database:
    """A store plus the knowledge bases a test created, so teardown can delete them."""

    store: SQLAlchemyStore
    knowledge_bases: list[str] = field(default_factory=list)

    def seed_knowledge_base(self) -> str:
        knowledge_base_id = f"kb-{uuid4().hex[:12]}"
        self.knowledge_bases.append(knowledge_base_id)
        self.store.save_knowledge_base(
            KnowledgeBase(id=knowledge_base_id, tenant_id=TENANT, name="concurrency")
        )
        return knowledge_base_id

    def seed_document(
        self,
        *,
        status: str = "pending",
        knowledge_base_id: str | None = None,
        chunks: list[Chunk] | None = None,
    ) -> tuple[str, str]:
        knowledge_base_id = knowledge_base_id or self.seed_knowledge_base()
        document_id = f"doc-{uuid4().hex[:12]}"
        chunks = chunks if chunks is not None else self.chunks(document_id, 0)
        self.store.save_document(
            Document(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                filename="a.txt",
                chunks=len(chunks),
                status=status,
            ),
            chunks,
        )
        return knowledge_base_id, document_id

    @staticmethod
    def chunks(document_id: str, count: int, prefix: str = "c") -> list[Chunk]:
        return [
            Chunk(f"{prefix}-{index}", document_id, 1, f"text {index}", "latest")
            for index in range(count)
        ]


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path: Path) -> Database:
    """The store under test, against each database engine this project supports."""
    if request.param == "postgres":
        url = os.environ.get(POSTGRES_URL_ENV)
        if not url:
            pytest.skip(f"set {POSTGRES_URL_ENV} to a migrated PostgreSQL database")
    else:
        url = f"sqlite:///{tmp_path / 'concurrency.db'}"
    database = Database(store=SQLAlchemyStore(url))
    yield database
    with database.store.engine.begin() as connection:
        for knowledge_base_id in database.knowledge_bases:
            for table, column in OWNED_TABLES:
                connection.execute(
                    text(f"DELETE FROM {table} WHERE {column} = :id"),
                    {"id": knowledge_base_id},
                )


def run_together(worker, count: int = WORKERS) -> list:
    """Run ``worker(index)`` in ``count`` threads released by one barrier.

    The barrier is the point: threads that merely started "at the same time" would in
    practice arrive one after another and quietly turn a race test into a sequential one.
    """
    barrier = threading.Barrier(count)

    def wrapped(index: int):
        barrier.wait(timeout=30)
        return worker(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(wrapped, range(count)))


def test_every_thread_gets_its_own_connection(database: Database) -> None:
    """The other tests are only meaningful if the pool is not handing out one connection."""
    assert not isinstance(database.store.engine.pool, StaticPool)


def test_exactly_one_worker_claims_a_queued_document(database: Database) -> None:
    _, document_id = database.seed_document()

    claims = run_together(lambda _index: database.store.claim_document(document_id))

    assert claims.count(True) == 1, f"{claims.count(True)} of {WORKERS} workers claimed it"


def test_a_live_claim_is_not_stolen_and_a_stale_one_is(database: Database) -> None:
    _, document_id = database.seed_document()
    assert database.store.claim_document(document_id) is True

    fresh = run_together(lambda _index: database.store.claim_document(document_id))

    assert fresh.count(True) == 0, "a worker whose claim is still fresh was preempted"

    backdate_claim(database, document_id, hours=1)
    stale = run_together(lambda _index: database.store.claim_document(document_id))

    assert stale.count(True) == 1, "the redelivered task was not claimed exactly once"


def backdate_claim(database: Database, document_id: str, *, hours: int) -> None:
    """Leave a document claimed and processing, as a worker that died mid-task leaves it."""
    with database.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE documents SET status = 'processing', updated_at = :when WHERE id = :id"
            ),
            {"when": utc_now() - timedelta(hours=hours), "id": document_id},
        )


def test_only_one_forced_rebuild_of_a_ready_document(database: Database) -> None:
    _, document_id = database.seed_document(status="ready")

    without_force = run_together(lambda _index: database.store.claim_document(document_id))

    assert without_force.count(True) == 0, "a ready document was rebuilt without force"

    forced = run_together(
        lambda _index: database.store.claim_document(document_id, force=True)
    )

    assert forced.count(True) == 1


def test_a_reader_never_sees_a_half_replaced_index(database: Database) -> None:
    knowledge_base_id, document_id = database.seed_document()
    database.store.replace_chunks(
        document_id, knowledge_base_id, Database.chunks(document_id, 5, "seed")
    )

    stop = threading.Event()
    seen: list[int] = []
    reader_errors: list[Exception] = []

    def read_until_stopped() -> None:
        while not stop.is_set():
            try:
                seen.append(len(database.store.get_chunks(knowledge_base_id)))
            except Exception as error:  # noqa: BLE001 - reported by the assertion below, not swallowed
                reader_errors.append(error)
                return

    def write(index: int):
        # Every replacement uses its own chunk ids, so a reader that catches a
        # replacement in progress sees a count, not a uniqueness error.
        def worker(_index: int) -> None:
            for iteration in range(5):
                size = 7 if iteration % 2 else 5
                chunks = Database.chunks(document_id, size, f"w{index}-{iteration}")
                database.store.replace_chunks(document_id, knowledge_base_id, chunks)

        return worker

    reader = threading.Thread(target=read_until_stopped, name="index-reader")
    reader.start()
    try:
        run_together(write(0), WORKERS)
    finally:
        stop.set()
        reader.join(timeout=30)

    assert reader_errors == []
    assert not reader.is_alive()
    assert len(seen) > 10, "the reader never sampled the index, so this proved nothing"
    assert set(seen) <= {5, 7}, f"partial index observed: {sorted(set(seen))}"


def test_parallel_writers_keep_every_row(database: Database) -> None:
    def writer(index: int) -> tuple[str, str, int]:
        knowledge_base_id = f"kb-{uuid4().hex[:12]}"
        document_id = f"doc-{uuid4().hex[:12]}"
        chunks = Database.chunks(document_id, 3, f"w{index}")
        database.store.save_knowledge_base(
            KnowledgeBase(id=knowledge_base_id, tenant_id=TENANT, name=f"writer {index}")
        )
        database.store.save_document(
            Document(
                id=document_id,
                knowledge_base_id=knowledge_base_id,
                filename=f"{index}.txt",
                chunks=len(chunks),
                status="ready",
            ),
            chunks,
        )
        return knowledge_base_id, document_id, len(chunks)

    written = run_together(writer)
    database.knowledge_bases.extend(knowledge_base_id for knowledge_base_id, _, _ in written)

    for knowledge_base_id, document_id, expected in written:
        assert database.store.get_document_any(document_id) is not None
        assert len(database.store.get_chunks(knowledge_base_id)) == expected
