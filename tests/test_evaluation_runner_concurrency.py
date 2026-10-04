"""Bounded concurrency, per-example timeouts and resumable checkpoints.

Every case here builds its own retrieval stub: the production ``RetrievalService`` is
deterministic and instant, so it cannot show that six questions run three at a time,
that one hung backend call is dropped instead of holding the whole run, or that a
retried evaluation keeps the rows the first attempt already paid for.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.core.evaluation_runner import EvaluationRunner
from app.core.ingestion import Chunk
from app.core.observability import TraceManager
from app.core.store import SQLiteStore
from app.schemas import Document, EvaluationDataset, EvaluationExample, KnowledgeBase

CHUNK = Chunk("chunk", "doc", 2, "loan policy effective date", "v1")


def _example(index: int) -> EvaluationExample:
    return EvaluationExample(
        id=f"example-{index}",
        dataset_id="dataset",
        question=f"question {index}",
        expected_document_id="doc",
        expected_page=2,
        evidence_quote="loan policy effective date",
    )


class StubRetrieval:
    """Retrieval that records overlap and can be made slow or failing per question."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        slow: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self.delay = delay
        self.slow = set(slow)
        self.failing = set(failing)
        self.questions: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def search(self, *, question: str, **_kwargs) -> SimpleNamespace:
        self.questions.append(question)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(5 if question in self.slow else self.delay)
            if question in self.failing:
                raise RuntimeError(f"retrieval exploded on {question}")
            return SimpleNamespace(
                results=[(CHUNK, 1.0)],
                cache_hit=False,
                rewritten_queries=[],
            )
        finally:
            self.in_flight -= 1


class CheckpointFailingStore(SQLiteStore):
    """A store whose progress writes fail while everything else keeps working."""

    def update_evaluation(  # type: ignore[override]
        self,
        evaluation_id,
        status,
        results=None,
        error_message=None,
    ):
        if status == "running" and results and "progress" in results:
            raise RuntimeError("checkpoint store is down")
        return super().update_evaluation(evaluation_id, status, results, error_message)


def _seed(tmp_path, count: int, store_class=SQLiteStore) -> SQLiteStore:
    store = store_class(str(tmp_path / "eval.db"))
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="Policy", description="")
    )
    store.save_document(
        Document(
            id="doc",
            filename="policy.txt",
            knowledge_base_id="kb",
            chunks=0,
            status="ready",
            version="v1",
        ),
        [],
    )
    store.replace_chunks("doc", "kb", [CHUNK])
    store.save_evaluation_dataset(
        EvaluationDataset(
            id="dataset",
            tenant_id="tenant",
            knowledge_base_id="kb",
            name="golden",
            description="",
            examples=[_example(index) for index in range(count)],
        )
    )
    store.create_evaluation(
        "eval",
        "golden",
        "hybrid",
        3,
        tenant_id="tenant",
        knowledge_base_id="kb",
        dataset_id="dataset",
        parameters={
            "document_version": "v1",
            "rerank": False,
            "query_rewrite": False,
            "answer_evaluation": False,
        },
    )
    return store


def _runner(
    store: SQLiteStore,
    retrieval: StubRetrieval,
    *,
    concurrency: int = 4,
    timeout: float = 30.0,
    rag_version: str = "v1",
) -> EvaluationRunner:
    settings = Settings(
        langsmith_enabled=False,
        evaluation_concurrency=concurrency,
        evaluation_example_timeout_seconds=timeout,
        rag_version=rag_version,
    )
    return EvaluationRunner(settings, store, retrieval, TraceManager(settings))


def test_examples_run_within_the_concurrency_limit_in_dataset_order(tmp_path) -> None:
    store = _seed(tmp_path, 6)
    retrieval = StubRetrieval(delay=0.05)

    result = asyncio.run(_runner(store, retrieval, concurrency=3).run("eval"))

    assert result["status"] == "completed"
    # Six questions, three at a time, each asked exactly once.
    assert retrieval.max_in_flight == 3
    assert sorted(retrieval.questions) == [f"question {index}" for index in range(6)]
    # Completion order is not dataset order; the reported rows have to be pairable
    # across runs, which is what scripts/bootstrap_ci.py relies on.
    assert [item["example_id"] for item in result["results"]["examples"]] == [
        f"example-{index}" for index in range(6)
    ]
    assert result["results"]["progress"]["concurrency"] == 3
    assert result["results"]["progress"]["completed"] == 6
    assert result["results"]["progress"]["resumed"] == 0


def test_a_hung_example_is_reported_and_left_out_of_every_average(tmp_path) -> None:
    store = _seed(tmp_path, 2)
    retrieval = StubRetrieval(slow=frozenset({"question 1"}))

    result = asyncio.run(_runner(store, retrieval, concurrency=2, timeout=0.05).run("eval"))

    assert result["status"] == "completed"
    metrics = result["results"]["metrics"]
    assert metrics["example_count"] == 2
    assert metrics["completed_example_count"] == 1
    assert metrics["timed_out_example_count"] == 1
    # The hung row contributes no metric at all, so recall is averaged over one row,
    # not diluted by a row that never produced an answer.
    assert metrics["recall_at_1"] == 1.0
    timed_out = [item for item in result["results"]["examples"] if item.get("timed_out")]
    assert [item["example_id"] for item in timed_out] == ["example-1"]
    assert "timed out" in timed_out[0]["error"]
    assert timed_out[0]["metrics"] == {}
    assert timed_out[0]["retrieved"] == []
    assert timed_out[0]["generated_answer"] is None


def test_a_retried_evaluation_resumes_from_the_checkpoint(tmp_path) -> None:
    store = _seed(tmp_path, 4)
    failing = StubRetrieval(failing=frozenset({"question 3"}))

    # concurrency=1 keeps "which rows finished" deterministic: 0, 1, 2 then the failure.
    with pytest.raises(RuntimeError, match="retrieval exploded"):
        asyncio.run(_runner(store, failing, concurrency=1).run("eval"))

    failed = store.get_evaluation("eval")
    assert failed["status"] == "failed"
    assert [item["example_id"] for item in failed["results"]["examples"]] == [
        "example-0",
        "example-1",
        "example-2",
    ]
    assert failed["error_message"]

    resumed = StubRetrieval()
    result = asyncio.run(_runner(store, resumed, concurrency=1).run("eval"))

    # Only the missing question is asked again - the paid-for rows are reused.
    assert resumed.questions == ["question 3"]
    assert result["status"] == "completed"
    assert result["results"]["progress"]["resumed"] == 3
    assert result["results"]["progress"]["completed"] == 4
    assert result["results"]["metrics"]["example_count"] == 4
    assert result["results"]["metrics"]["recall_at_1"] == 1.0
    assert [item["example_id"] for item in result["results"]["examples"]] == [
        f"example-{index}" for index in range(4)
    ]


def test_a_checkpoint_from_different_settings_is_discarded(tmp_path) -> None:
    store = _seed(tmp_path, 3)
    failing = StubRetrieval(failing=frozenset({"question 2"}))

    with pytest.raises(RuntimeError, match="retrieval exploded"):
        asyncio.run(_runner(store, failing, concurrency=1, rag_version="v1").run("eval"))
    assert len(store.get_evaluation("eval")["results"]["examples"]) == 2

    rerun = StubRetrieval()
    result = asyncio.run(_runner(store, rerun, concurrency=1, rag_version="v2").run("eval"))

    # Rows produced under the old rag_version must never be mixed into the new run.
    assert sorted(rerun.questions) == ["question 0", "question 1", "question 2"]
    assert result["results"]["progress"]["resumed"] == 0
    assert result["results"]["progress"]["completed"] == 3


def test_a_failing_checkpoint_write_does_not_fail_the_run(tmp_path) -> None:
    store = _seed(tmp_path, 2, store_class=CheckpointFailingStore)

    result = asyncio.run(_runner(store, StubRetrieval(), concurrency=2).run("eval"))

    # A checkpoint that cannot be saved costs a resume, not the experiment.
    assert result["status"] == "completed"
    assert result["results"]["metrics"]["example_count"] == 2
    assert result["results"]["metrics"]["recall_at_1"] == 1.0
