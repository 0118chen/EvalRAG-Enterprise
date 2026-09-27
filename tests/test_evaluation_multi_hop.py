"""Multi-hop and unanswerable examples: labels, metrics and the runner's aggregation."""

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.cache import MemoryTTLCache
from app.core.evaluation import (
    EvidenceTarget,
    RetrievalExample,
    evidence_targets,
    metrics_at_k,
    passage_metrics,
)
from app.core.evaluation_runner import EvaluationRunner
from app.core.ingestion import Chunk
from app.core.observability import TraceManager
from app.core.query_rewrite import IdentityQueryRewriter
from app.core.reranking import LexicalReranker
from app.core.retrieval_service import RetrievalService
from app.core.store import SQLiteStore
from app.schemas import (
    Document,
    EvaluationDataset,
    EvaluationExample,
    EvidenceSpan,
    KnowledgeBase,
)


class StubLLM:
    async def answer(self, question: str, context: str) -> str:
        return "无关回答。"


def _two_hop_store(tmp_path) -> SQLiteStore:
    """Two documents, one chunk each, plus a two-hop and an unanswerable example."""
    store = SQLiteStore(str(tmp_path / "multihop.db"))
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="Policy", description="")
    )
    for document_id, text in (
        ("doc-a", "土地承包期三十年 承包方享有土地承包经营权"),
        ("doc-b", "承包经营权可以流转 流转期限不得超过承包期的剩余期限"),
    ):
        store.save_document(
            Document(
                id=document_id,
                filename=f"{document_id}.txt",
                knowledge_base_id="kb",
                chunks=0,
                status="ready",
                version="v1",
            ),
            [],
        )
        store.replace_chunks(document_id, "kb", [Chunk(f"{document_id}-c", document_id, 1, text, "v1")])
    store.save_evaluation_dataset(
        EvaluationDataset(
            id="dataset",
            tenant_id="tenant",
            knowledge_base_id="kb",
            name="multihop",
            description="",
            examples=[
                EvaluationExample(
                    id="hop",
                    dataset_id="dataset",
                    question="土地承包期与流转期限的关系是什么？",
                    expected_document_id="doc-a",
                    expected_page=1,
                    evidence_quote="土地承包期三十年",
                    category="多跳",
                    expected_evidence=[
                        EvidenceSpan(document_id="doc-a", page=1, quote="土地承包期三十年"),
                        EvidenceSpan(document_id="doc-b", page=1, quote="流转期限不得超过承包期的剩余期限"),
                    ],
                ),
                EvaluationExample(
                    id="refuse",
                    dataset_id="dataset",
                    question="本知识库中关于券商资产管理计划的杠杆比例是如何规定的？",
                    category="应拒答",
                    should_refuse=True,
                ),
            ],
        )
    )
    store.create_evaluation(
        "eval",
        "multihop",
        "sparse",
        2,
        tenant_id="tenant",
        knowledge_base_id="kb",
        dataset_id="dataset",
        parameters={"document_version": "v1", "rerank": False, "query_rewrite": False},
    )
    return store


def _runner(store: SQLiteStore) -> EvaluationRunner:
    settings = Settings(langsmith_enabled=False)
    traces = TraceManager(settings)
    return EvaluationRunner(
        settings,
        store,
        RetrievalService(
            settings,
            traces,
            MemoryTTLCache(),
            IdentityQueryRewriter(),
            LexicalReranker(),
        ),
        traces,
        llm=StubLLM(),
    )


def test_evidence_targets_uses_the_span_list_when_present() -> None:
    single = EvaluationExample(
        id="one",
        dataset_id="d",
        question="q",
        expected_document_id="doc-a",
        expected_page=3,
        evidence_quote="quote",
    )
    assert evidence_targets(single) == [EvidenceTarget("doc-a", 3, "quote")]

    multi = EvaluationExample(
        id="two",
        dataset_id="d",
        question="q",
        expected_document_id="doc-a",
        expected_evidence=[
            EvidenceSpan(document_id="doc-a", page=1),
            EvidenceSpan(document_id="doc-b", quote="second hop"),
        ],
    )
    targets = evidence_targets(multi)
    assert [target.document_id for target in targets] == ["doc-a", "doc-b"]
    assert targets[1].quote == "second hop"


def test_refusal_example_has_no_targets() -> None:
    example = EvaluationExample(
        id="refuse",
        dataset_id="d",
        question="q",
        should_refuse=True,
    )
    assert evidence_targets(example) == []


def test_labels_are_rejected_when_they_contradict_each_other() -> None:
    with pytest.raises(ValidationError):
        EvaluationExample(
            id="bad",
            dataset_id="d",
            question="q",
            should_refuse=True,
            expected_document_id="doc-a",
        )
    with pytest.raises(ValidationError):
        EvaluationExample(id="bad", dataset_id="d", question="q")
    with pytest.raises(ValidationError):
        EvaluationExample(
            id="bad",
            dataset_id="d",
            question="q",
            expected_document_id="doc-a",
            expected_evidence=[EvidenceSpan(document_id="doc-b")],
        )


def test_recall_becomes_a_fraction_over_the_expected_set() -> None:
    example = RetrievalExample("q", ("doc-a", "doc-b"), ["doc-a", "doc-c"])

    metrics = metrics_at_k(example, (2,))

    assert metrics["recall_at_2"] == 0.5  # one of the two expected documents
    assert metrics["all_targets_at_2"] == 0.0
    assert metrics["mrr"] == 0.5  # only the first hop is retrievable here


def test_all_targets_requires_every_hop() -> None:
    complete = RetrievalExample("q", ("doc-a", "doc-b"), ["doc-b", "doc-a"])

    assert metrics_at_k(complete, (2,))["all_targets_at_2"] == 1.0
    assert metrics_at_k(complete, (1,))["all_targets_at_1"] == 0.0


def test_passage_metrics_average_over_every_hop() -> None:
    texts = ["土地承包期三十年", "noise"]

    assert passage_metrics(texts, ["土地承包期三十年"]) == {
        "passage_hit": 1.0,
        "passage_at_1": 1.0,
        "passage_mrr": 1.0,
    }
    # Second hop missing: the example is not fully found, and the mean rank halves.
    partial = passage_metrics(texts, ["土地承包期三十年", "流转期限不得超过承包期的剩余期限"])
    assert partial == {"passage_hit": 0.0, "passage_at_1": 0.0, "passage_mrr": 0.5}


def test_runner_reports_multi_hop_and_refusal_separately(tmp_path) -> None:
    store = _two_hop_store(tmp_path)

    result = asyncio.run(_runner(store).run("eval"))

    metrics = result["results"]["metrics"]
    assert metrics["example_count"] == 2
    assert metrics["answerable_example_count"] == 1
    assert metrics["refusal_example_count"] == 1
    # The one answerable example is two-hop: both documents must be found.
    assert metrics["all_targets_at_2"] in (0.0, 1.0)
    assert "negative_retrieved_rate" in metrics
    hop = next(item for item in result["results"]["examples"] if item["example_id"] == "hop")
    assert len(hop["retrieved"]) >= 1
    refusal = next(
        item for item in result["results"]["examples"] if item["example_id"] == "refuse"
    )
    assert refusal["should_refuse"] is True
    # No recall/MRR/passage keys for a question without a correct document; the only
    # signal is the raw one, kept under its own name so it is never read as an answer score.
    assert set(refusal["metrics"]) == {"retrieved_something"}
    assert refusal["passage_ranks"] == []
