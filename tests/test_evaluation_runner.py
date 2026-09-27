import asyncio

from app.config import Settings
from app.core.cache import MemoryTTLCache
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
    KnowledgeBase,
)


class FakeJudgeLLM:
    async def answer(self, question: str, context: str) -> str:
        if "裁判" in question:
            return '{"correctness":1,"faithfulness":1,"completeness":1,"reason":"ok"}'
        return "贷款政策生效日期为一月一日。"


def _prepared_runner(tmp_path, extra_examples=None):
    """One document, one chunk and one golden example, wired exactly as production is."""
    settings = Settings(langsmith_enabled=False)
    store = SQLiteStore(str(tmp_path / "eval.db"))
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
    store.replace_chunks(
        "doc",
        "kb",
        [Chunk("chunk", "doc", 2, "loan policy effective date", "v1")],
    )
    store.save_evaluation_dataset(
        EvaluationDataset(
            id="dataset",
            tenant_id="tenant",
            knowledge_base_id="kb",
            name="golden",
            description="",
            examples=[
                EvaluationExample(
                    id="example",
                    dataset_id="dataset",
                    question="loan policy effective date",
                    expected_answer="The effective date is January first.",
                    expected_document_id="doc",
                    expected_page=2,
                    evidence_quote="loan policy effective date",
                ),
                *(extra_examples or []),
            ],
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
            "rerank": True,
            "query_rewrite": False,
            "answer_evaluation": True,
        },
    )
    traces = TraceManager(settings)
    retrieval = RetrievalService(
        settings,
        traces,
        MemoryTTLCache(),
        IdentityQueryRewriter(),
        LexicalReranker(),
    )
    runner = EvaluationRunner(
        settings,
        store,
        retrieval,
        traces,
        llm=FakeJudgeLLM(),
    )
    return runner, store


def test_evaluation_runner_persists_real_metrics(tmp_path) -> None:
    runner, _store = _prepared_runner(tmp_path)
    result = asyncio.run(runner.run("eval"))
    assert result["status"] == "completed"
    assert result["results"]["metrics"]["recall_at_3"] == 1.0
    assert result["results"]["metrics"]["page_hit"] == 1.0
    assert result["results"]["metrics"]["answer_correctness"] == 1.0
    assert result["results"]["metrics"]["answer_faithfulness"] == 1.0
    assert result["results"]["metrics"]["answer_completeness"] == 1.0
    assert result["results"]["examples"][0]["retrieved"][0]["version"] == "v1"


def test_evaluation_runner_reports_every_cutoff_latency_and_retrieved_text(tmp_path) -> None:
    runner, _store = _prepared_runner(tmp_path)

    result = asyncio.run(runner.run("eval"))

    metrics = result["results"]["metrics"]
    assert {"recall_at_1", "recall_at_3", "ndcg_at_3", "precision_at_3"} <= set(metrics)
    # top_k=3 caps the reported cutoffs: Recall@5 could not be measured honestly.
    assert "recall_at_5" not in metrics
    assert metrics["latency_ms_p50"] > 0
    assert metrics["latency_ms_p95"] >= metrics["latency_ms_p50"]
    retrieved = result["results"]["examples"][0]["retrieved"]
    assert retrieved[0]["chunk_id"] == "chunk"
    assert retrieved[0]["text"] == "loan policy effective date"


def test_evaluation_runner_reports_passage_metrics_for_annotated_examples(tmp_path) -> None:
    runner, _store = _prepared_runner(tmp_path)

    result = asyncio.run(runner.run("eval"))

    metrics = result["results"]["metrics"]
    assert metrics["passage_hit"] == 1.0
    assert metrics["passage_at_1"] == 1.0
    assert metrics["passage_mrr"] == 1.0
    assert result["results"]["examples"][0]["passage_rank"] == 1


def test_passage_metrics_ignore_examples_without_a_quote(tmp_path) -> None:
    unannotated = EvaluationExample(
        id="example-2",
        dataset_id="dataset",
        question="something the corpus cannot answer",
        expected_document_id="doc",
        expected_page=1,
    )
    runner, _store = _prepared_runner(tmp_path, extra_examples=[unannotated])

    result = asyncio.run(runner.run("eval"))

    metrics = result["results"]["metrics"]
    assert metrics["example_count"] == 2
    # The unannotated example must not dilute the passage average: it is not evidence.
    assert metrics["passage_hit"] == 1.0
    assert metrics["passage_at_1"] == 1.0
    assert any(item["passage_rank"] is None for item in result["results"]["examples"])
