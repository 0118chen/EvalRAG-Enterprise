"""Equivalent labels (`evidence_mode="any"`): one of several documents answers the question."""

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.cache import MemoryTTLCache
from app.core.evaluation import (
    RetrievalExample,
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

DEFINITION = "本办法所称贷款人是指在中华人民共和国境内依法设立的银行业金融机构"


class StubLLM:
    async def answer(self, question: str, context: str) -> str:
        return "无关回答。"


def test_evidence_mode_defaults_to_all_and_rejects_unknown_values() -> None:
    example = EvaluationExample(id="e", dataset_id="d", question="q", expected_document_id="doc")
    assert example.evidence_mode == "all"

    equivalent = EvaluationExample(
        id="e",
        dataset_id="d",
        question="q",
        expected_evidence=[EvidenceSpan(document_id="doc-a"), EvidenceSpan(document_id="doc-b")],
        evidence_mode="any",
    )
    assert equivalent.evidence_mode == "any"

    with pytest.raises(ValidationError):
        EvaluationExample(
            id="e",
            dataset_id="d",
            question="q",
            expected_document_id="doc",
            evidence_mode="sometimes",
        )


def test_any_mode_scores_the_best_hop_instead_of_averaging() -> None:
    # The same definition sits verbatim in three regulations: whichever is retrieved
    # answers the question, so the ranking that matters is the best one, not the mean.
    texts = ["unrelated", DEFINITION, "other"]

    any_metrics = passage_metrics(texts, [DEFINITION, "missing", "missing"], mode="any")
    assert any_metrics == {"passage_hit": 1.0, "passage_at_1": 0.0, "passage_mrr": 0.5}

    all_metrics = passage_metrics(texts, [DEFINITION, "missing", "missing"], mode="all")
    assert all_metrics == {"passage_hit": 0.0, "passage_at_1": 0.0, "passage_mrr": 1 / 6}


def test_any_mode_reports_its_own_target_key() -> None:
    equivalent = RetrievalExample("q", ("doc-a", "doc-b", "doc-c"), ["other", "doc-b"], mode="any")

    metrics = metrics_at_k(equivalent, (1, 2))

    assert "all_targets_at_2" not in metrics
    assert metrics["any_target_at_1"] == 0.0  # doc-b is second, not first
    assert metrics["any_target_at_2"] == 1.0
    assert metrics["recall_at_2"] == 1 / 3  # one of three equivalent documents
    assert metrics["mrr"] == 0.5  # the best hop, not the mean of the three


def _equivalent_store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "equivalent.db"))
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="Policy", description="")
    )
    for document_id, text in (
        ("doc-a", f"{DEFINITION} 第一条"),
        ("doc-b", "农户贷款是指银行业金融机构向农户发放的贷款"),
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
            name="equivalent",
            description="",
            examples=[
                EvaluationExample(
                    id="equivalent",
                    dataset_id="dataset",
                    question="贷款人指的是谁？",
                    expected_evidence=[
                        EvidenceSpan(document_id="doc-a", page=1, quote=DEFINITION),
                        EvidenceSpan(document_id="doc-b", page=1, quote="贷款人指的是谁的定义"),
                    ],
                    evidence_mode="any",
                    category="等价多标签",
                )
            ],
        )
    )
    store.create_evaluation(
        "eval",
        "equivalent",
        "sparse",
        2,
        tenant_id="tenant",
        knowledge_base_id="kb",
        dataset_id="dataset",
        parameters={"document_version": "v1", "rerank": False, "query_rewrite": False},
    )
    return store


def test_runner_honours_any_mode_end_to_end(tmp_path) -> None:
    store = _equivalent_store(tmp_path)
    settings = Settings(langsmith_enabled=False)
    traces = TraceManager(settings)
    runner = EvaluationRunner(
        settings,
        store,
        RetrievalService(settings, traces, MemoryTTLCache(), IdentityQueryRewriter(), LexicalReranker()),
        traces,
        llm=StubLLM(),
    )

    result = asyncio.run(runner.run("eval"))

    metrics = result["results"]["metrics"]
    # Only doc-a is retrieved; that is enough under mode="any".
    assert metrics["any_target_at_2"] == 1.0
    # Each target key covers only the examples whose mode gives it meaning, and the
    # counts say which population that is.
    assert "all_targets_at_2" not in metrics
    assert metrics["any_mode_example_count"] == 1
    assert metrics["all_mode_example_count"] == 0
    assert metrics["passage_hit"] == 1.0
    item = result["results"]["examples"][0]
    assert item["evidence_mode"] == "any"
    assert item["expected_documents"] == ["doc-a", "doc-b"]
