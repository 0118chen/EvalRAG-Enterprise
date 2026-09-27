"""Execute persisted retrieval experiments against a versioned knowledge base."""

import logging
from dataclasses import dataclass
from statistics import fmean
from time import perf_counter
from typing import Any

from app.config import Settings
from app.core.answer_evaluation import judge_answer
from app.core.evaluation import (
    RetrievalExample,
    evidence_targets,
    latency_percentiles,
    metrics_at_k,
    passage_metrics,
    passage_ranks,
)
from app.core.langsmith_eval import LangSmithEvaluationAdapter
from app.core.observability import TraceManager
from app.core.rag import answer_with_evidence
from app.core.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)


@dataclass
class EvaluationRunner:
    settings: Settings
    store: Any
    retrieval_service: RetrievalService
    traces: TraceManager
    langsmith: LangSmithEvaluationAdapter | None = None
    llm: Any | None = None

    async def run(self, evaluation_id: str) -> dict[str, Any]:
        evaluation = self.store.get_evaluation(evaluation_id)
        if not evaluation:
            raise ValueError("evaluation not found")
        if evaluation["status"] == "completed":
            return evaluation
        if not evaluation.get("knowledge_base_id"):
            raise ValueError("knowledge_base_id is required to run an evaluation")

        dataset = (
            self.store.get_evaluation_dataset(
                evaluation["dataset_id"],
                evaluation["tenant_id"],
            )
            if evaluation.get("dataset_id")
            else self.store.get_evaluation_dataset_by_name(
                evaluation["dataset_name"],
                evaluation["tenant_id"],
            )
        )
        if not dataset:
            raise ValueError("evaluation dataset not found")

        parameters = evaluation.get("parameters") or {}
        document_version = parameters.get("document_version", "latest")
        rerank = parameters.get("rerank")
        query_rewrite = parameters.get("query_rewrite")
        answer_evaluation = bool(parameters.get("answer_evaluation", False))
        if answer_evaluation and self.llm is None:
            raise ValueError("LLM is required for answer evaluation")
        chunks = self.store.get_chunks(
            evaluation["knowledge_base_id"],
            document_version,
        )
        if not chunks:
            raise ValueError("knowledge base has no indexed chunks for the selected version")

        self.store.update_evaluation(evaluation_id, "running")
        metadata = {
            "evaluation_id": evaluation_id,
            "dataset_name": dataset.name,
            "knowledge_base_id": evaluation["knowledge_base_id"],
            "document_version": document_version,
            "retrieval_mode": evaluation["retrieval_mode"],
            "top_k": evaluation["top_k"],
            "rag_version": self.settings.rag_version,
            "prompt_version": self.settings.prompt_version,
        }
        try:
            with self.traces.span(
                "evaluation.experiment",
                run_type="chain",
                metadata=metadata,
                inputs={"example_count": len(dataset.examples)},
            ) as experiment_span:
                examples, metric_values = await self._run_examples(
                    evaluation,
                    dataset,
                    chunks,
                    document_version,
                    rerank,
                    query_rewrite,
                    answer_evaluation,
                )
                aggregate = {
                    key: fmean(values)
                    for key, values in metric_values.items()
                    if values and key != "retrieved_something"
                }
                aggregate["example_count"] = len(examples)
                refusal_examples = [item for item in examples if item["should_refuse"]]
                answerable = [item for item in examples if not item["should_refuse"]]
                aggregate["answerable_example_count"] = len(answerable)
                aggregate["refusal_example_count"] = len(refusal_examples)
                aggregate["all_mode_example_count"] = sum(
                    1 for item in answerable if item["evidence_mode"] == "all"
                )
                aggregate["any_mode_example_count"] = sum(
                    1 for item in answerable if item["evidence_mode"] == "any"
                )
                if refusal_examples:
                    # Kept out of every retrieval metric above: a question the corpus
                    # cannot answer has no correct document to find.
                    aggregate["negative_retrieved_rate"] = fmean(
                        item["metrics"]["retrieved_something"] for item in refusal_examples
                    )
                latencies = [item["latency_ms"] for item in examples]
                aggregate["latency_ms"] = fmean(latencies)
                percentiles = latency_percentiles(latencies)
                if percentiles:
                    aggregate["latency_ms_p50"] = percentiles["p50"]
                    aggregate["latency_ms_p95"] = percentiles["p95"]
                langsmith_result = await self._run_langsmith(
                    evaluation,
                    dataset,
                    chunks,
                    document_version,
                    rerank,
                    query_rewrite,
                    answer_evaluation,
                )
                results = {
                    "metrics": aggregate,
                    "examples": examples,
                    "langsmith": langsmith_result,
                }
                baseline_diff = self._baseline_diff(evaluation, aggregate)
                if baseline_diff is not None:
                    results["baseline_diff"] = baseline_diff
                self.store.update_evaluation(evaluation_id, "completed", results)
                experiment_span.set_outputs(
                    {
                        "metrics": aggregate,
                        "langsmith": langsmith_result,
                    }
                )
                return self.store.get_evaluation(evaluation_id) or results
        except Exception as exc:
            self.store.update_evaluation(
                evaluation_id,
                "failed",
                {"metrics": {}, "examples": []},
                str(exc),
            )
            raise

    async def _run_examples(
        self,
        evaluation: dict[str, Any],
        dataset,
        chunks,
        document_version: str,
        rerank: bool | None,
        query_rewrite: bool | None,
        answer_evaluation: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, list[float]]]:
        examples: list[dict[str, Any]] = []
        metric_values: dict[str, list[float]] = {}
        for example in dataset.examples:
            started = perf_counter()
            with self.traces.span(
                "evaluation.example",
                run_type="chain",
                metadata={"question_id": example.id, "category": example.category},
                inputs={"question": example.question},
            ) as example_span:
                context_top_k = (
                    max(evaluation["top_k"], 8)
                    if answer_evaluation
                    else evaluation["top_k"]
                )
                retrieved = await self.retrieval_service.search(
                    tenant_id=evaluation["tenant_id"],
                    knowledge_base_id=evaluation["knowledge_base_id"],
                    question=example.question,
                    chunks=chunks,
                    top_k=context_top_k,
                    mode=evaluation["retrieval_mode"],
                    document_version=document_version,
                    rerank=rerank,
                    query_rewrite=query_rewrite,
                )
                metric_results = retrieved.results[:evaluation["top_k"]]
                retrieved_ids = [
                    chunk.document_id
                    for chunk, _ in metric_results
                ]
                targets = evidence_targets(example)
                mode = example.evidence_mode
                metric_example = RetrievalExample(
                    question=example.question,
                    expected_document_id=[target.document_id for target in targets],
                    retrieved_document_ids=retrieved_ids,
                    mode=mode,
                )
                cutoffs = tuple(
                    sorted(
                        k
                        for k in (1, 3, 5, evaluation["top_k"])
                        if 0 < k <= evaluation["top_k"]
                    )
                )
                retrieved_texts = [chunk.text for chunk, _ in metric_results]
                passage_rank_values: list[int | None] = []
                if targets:
                    metrics = metrics_at_k(metric_example, cutoffs)
                    # Under "any" the labels are interchangeable, so one matched page is
                    # enough; under "all" every labelled page has to be retrieved.
                    matched = [
                        any(
                            chunk.document_id == target.document_id
                            and (target.page is None or chunk.page == target.page)
                            for chunk, _ in metric_results
                        )
                        for target in targets
                    ]
                    metrics["page_hit"] = float(any(matched) if mode == "any" else all(matched))
                    # Only annotated examples carry these keys, so the aggregate averages
                    # over the examples that actually hold ground truth.
                    quotes = [target.quote for target in targets]
                    passage_rank_values = passage_ranks(retrieved_texts, quotes)
                    metrics.update(passage_metrics(retrieved_texts, quotes, mode=mode))
                else:
                    # An unanswerable example: retrieval is only allowed to stay empty.
                    # This is a false-positive proxy - grading an actual refusal needs the
                    # answer path, which is why the number is reported separately instead
                    # of folded into recall.
                    metrics = {"retrieved_something": float(bool(metric_results))}
                generated_answer: str | None = None
                judge_reason: str | None = None
                if answer_evaluation:
                    generated_answer, _ = await answer_with_evidence(
                        self.llm,
                        example.question,
                        retrieved.results,
                    )
                    quality = await judge_answer(
                        self.llm,
                        question=example.question,
                        expected_answer=example.expected_answer or "",
                        generated_answer=generated_answer,
                        evidence=retrieved.results,
                    )
                    metrics.update(
                        {
                            "answer_correctness": quality.correctness,
                            "answer_faithfulness": quality.faithfulness,
                            "answer_completeness": quality.completeness,
                        }
                    )
                    judge_reason = quality.reason
                for key, value in metrics.items():
                    metric_values.setdefault(key, []).append(value)
                item = {
                    "example_id": example.id,
                    "question": example.question,
                    "category": example.category,
                    "expected_document_id": example.expected_document_id,
                    "expected_page": example.expected_page,
                    "expected_answer": example.expected_answer,
                    "should_refuse": bool(example.should_refuse),
                    "evidence_mode": mode,
                    "expected_documents": [target.document_id for target in targets],
                    "generated_answer": generated_answer,
                    "judge_reason": judge_reason,
                    "retrieved": [
                        {
                            "chunk_id": chunk.id,
                            "document_id": chunk.document_id,
                            "page": chunk.page,
                            "version": chunk.version,
                            "score": score,
                            "text": chunk.text,
                        }
                        for chunk, score in metric_results
                    ],
                    "metrics": metrics,
                    "passage_rank": passage_rank_values[0] if passage_rank_values else None,
                    "passage_ranks": passage_rank_values,
                    "cache_hit": retrieved.cache_hit,
                    "rewritten_queries": retrieved.rewritten_queries,
                    "latency_ms": (perf_counter() - started) * 1000,
                }
                examples.append(item)
                example_span.set_outputs(
                    {
                        "retrieved_document_ids": retrieved_ids,
                        "metrics": metrics,
                    }
                )
        return examples, metric_values

    async def _run_langsmith(
        self,
        evaluation: dict[str, Any],
        dataset,
        chunks,
        document_version: str,
        rerank: bool | None,
        query_rewrite: bool | None,
        answer_evaluation: bool,
    ) -> dict[str, Any] | None:
        if not self.langsmith or not self.langsmith.enabled:
            return None

        async def target(inputs: dict[str, Any]) -> dict[str, Any]:
            context_top_k = (
                max(evaluation["top_k"], 8)
                if answer_evaluation
                else evaluation["top_k"]
            )
            result = await self.retrieval_service.search(
                tenant_id=evaluation["tenant_id"],
                knowledge_base_id=evaluation["knowledge_base_id"],
                question=inputs["question"],
                chunks=chunks,
                top_k=context_top_k,
                mode=evaluation["retrieval_mode"],
                document_version=document_version,
                rerank=rerank,
                query_rewrite=query_rewrite,
            )
            outputs = {
                "document_ids": [chunk.document_id for chunk, _ in result.results],
                "version": document_version,
            }
            if answer_evaluation and self.llm is not None:
                generated_answer, _ = await answer_with_evidence(
                    self.llm,
                    inputs["question"],
                    result.results,
                )
                outputs["answer"] = generated_answer
            return outputs

        return await self.langsmith.run_experiment(
            dataset_name=dataset.name,
            tenant_id=evaluation["tenant_id"],
            target=target,
            evaluators=[
                self.langsmith.recall_evaluator,
                self.langsmith.mrr_evaluator,
            ],
            experiment_prefix=evaluation.get("experiment_name")
            or f"{dataset.name}-{evaluation['retrieval_mode']}",
            metadata={
                "retrieval_mode": evaluation["retrieval_mode"],
                "top_k": evaluation["top_k"],
                "document_version": document_version,
                "rag_version": self.settings.rag_version,
                "prompt_version": self.settings.prompt_version,
            },
        )

    def _baseline_diff(
        self,
        evaluation: dict[str, Any],
        current: dict[str, float],
    ) -> dict[str, float] | None:
        baseline_id = evaluation.get("baseline_evaluation_id")
        if not baseline_id:
            return None
        baseline = self.store.get_evaluation(baseline_id)
        if not baseline or baseline.get("status") != "completed":
            return None
        if baseline.get("tenant_id") != evaluation.get("tenant_id"):
            # The baseline lookup is not tenant filtered, so a stored reference
            # from another tenant must never leak into this tenant's metrics.
            logger.warning(
                "ignoring baseline %s: it belongs to a different tenant",
                baseline_id,
            )
            return None
        baseline_metrics = (baseline.get("results") or {}).get("metrics", {})
        return {
            key: current[key] - float(baseline_metrics[key])
            for key in current
            if key in baseline_metrics
            and isinstance(current[key], (int, float))
            and isinstance(baseline_metrics[key], (int, float))
        }
