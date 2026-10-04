"""Execute persisted retrieval experiments against a versioned knowledge base."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
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

# Where a run in flight keeps its state inside `results_json`: the same `examples` list a
# finished run uses, plus a fingerprint of everything that would make those rows
# meaningless if it changed.
PROGRESS_KEY = "progress"


@dataclass(frozen=True)
class Checkpoint:
    """The examples an earlier attempt at this same experiment already finished."""

    fingerprint: str
    examples: list[dict[str, Any]]

    @property
    def example_ids(self) -> set[str]:
        return {
            item["example_id"]
            for item in self.examples
            if isinstance(item.get("example_id"), str)
        }


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
        # No version filter unless the experiment asked for one: defaulting to the
        # literal "latest" would score against whichever documents happened to be
        # uploaded without an explicit version, which is not "the newest version".
        document_version = parameters.get("document_version")
        rerank = parameters.get("rerank")
        query_rewrite = parameters.get("query_rewrite")
        fusion = parameters.get("fusion")
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
        # The Celery task retries this same evaluation_id (autoretry_for=Exception), so a
        # run that died two thirds of the way through must not pay for the first two
        # thirds again - and must not mix rows produced under different settings either.
        fingerprint = self._fingerprint(evaluation, dataset)
        checkpoint = self._checkpoint(evaluation, fingerprint)
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
                    fusion,
                    checkpoint=checkpoint,
                    on_progress=self._progress_writer(
                        evaluation_id,
                        fingerprint,
                        len(dataset.examples),
                    ),
                )
                aggregate = {
                    key: fmean(values)
                    for key, values in metric_values.items()
                    if values and key != "retrieved_something"
                }
                aggregate["example_count"] = len(examples)
                # A timed-out example is excluded from every average above, so the count
                # has to be visible: a metric averaged over 70 of 75 questions is not the
                # same claim as one averaged over all 75.
                aggregate["completed_example_count"] = sum(
                    1 for item in examples if not item.get("timed_out")
                )
                aggregate["timed_out_example_count"] = sum(
                    1 for item in examples if item.get("timed_out")
                )
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
                    fusion,
                )
                results = {
                    "metrics": aggregate,
                    "examples": examples,
                    "langsmith": langsmith_result,
                    "progress": {
                        "completed": len(examples),
                        "total": len(dataset.examples),
                        "resumed": len(checkpoint.examples),
                        "concurrency": self._concurrency(),
                        "example_timeout_seconds": (
                            self.settings.evaluation_example_timeout_seconds
                        ),
                        "fingerprint": fingerprint,
                    },
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
            # Keep the checkpoint: the retry re-enters run() with this same id and picks up
            # where it stopped. Overwriting it with an empty result would throw away every
            # example that already succeeded (and, with answer evaluation, calls the
            # operator already paid for).
            preserved = (self.store.get_evaluation(evaluation_id) or {}).get("results") or {}
            self.store.update_evaluation(
                evaluation_id,
                "failed",
                preserved,
                str(exc),
            )
            raise

    def _concurrency(self) -> int:
        return max(1, int(self.settings.evaluation_concurrency))

    def _fingerprint(self, evaluation: dict[str, Any], dataset) -> str:
        """Identify the experiment a checkpoint belongs to.

        Anything that changes what a per-example row means - the questions, the labels, the
        retrieval settings, the prompt version - changes the fingerprint, so a resumed run
        can never mix rows produced under different settings.
        """
        payload = {
            "dataset_id": dataset.id,
            "dataset_name": dataset.name,
            "example_ids": [example.id for example in dataset.examples],
            "retrieval_mode": evaluation["retrieval_mode"],
            "top_k": evaluation["top_k"],
            "parameters": evaluation.get("parameters") or {},
            "rag_version": self.settings.rag_version,
            "prompt_version": self.settings.prompt_version,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _checkpoint(self, evaluation: dict[str, Any], fingerprint: str) -> Checkpoint:
        stored = evaluation.get("results") or {}
        progress = stored.get(PROGRESS_KEY) or {}
        if progress.get("fingerprint") != fingerprint:
            # Either the row predates checkpoints or a different experiment reused it.
            # Scoring a stale subset as if it were the whole dataset would be worse than
            # paying for the run again, so the checkpoint is dropped.
            return Checkpoint(fingerprint, [])
        examples = [item for item in stored.get("examples") or [] if isinstance(item, dict)]
        return Checkpoint(fingerprint, examples)

    def _progress_writer(
        self,
        evaluation_id: str,
        fingerprint: str,
        total: int,
    ) -> Callable[[list[dict[str, Any]]], None]:
        def write(examples: list[dict[str, Any]]) -> None:
            self.store.update_evaluation(
                evaluation_id,
                "running",
                {
                    "metrics": {},
                    "examples": examples,
                    PROGRESS_KEY: {
                        "completed": len(examples),
                        "total": total,
                        "fingerprint": fingerprint,
                    },
                },
            )

        return write

    async def _run_examples(
        self,
        evaluation: dict[str, Any],
        dataset,
        chunks,
        document_version: str | None,
        rerank: bool | None,
        query_rewrite: bool | None,
        answer_evaluation: bool,
        fusion: str | None = None,
        *,
        checkpoint: Checkpoint | None = None,
        on_progress: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, list[float]]]:
        checkpoint = checkpoint or Checkpoint("", [])
        finished: dict[str, dict[str, Any]] = {
            item["example_id"]: item for item in checkpoint.examples
        }
        pending = [
            example for example in dataset.examples if example.id not in checkpoint.example_ids
        ]
        semaphore = asyncio.Semaphore(self._concurrency())
        timeout = self.settings.evaluation_example_timeout_seconds or None
        write_lock = asyncio.Lock()

        def ordered() -> list[dict[str, Any]]:
            # Dataset order, not completion order: two runs of the same experiment have to
            # produce identical example lists so their rows can be paired question by
            # question (scripts/bootstrap_ci.py does exactly that).
            return [
                finished[example.id]
                for example in dataset.examples
                if example.id in finished
            ]

        async def run_one(example) -> None:
            async with semaphore:
                started = perf_counter()
                try:
                    item = await asyncio.wait_for(
                        self._evaluate_example(
                            evaluation,
                            example,
                            chunks,
                            document_version,
                            rerank,
                            query_rewrite,
                            answer_evaluation,
                            fusion,
                        ),
                        timeout,
                    )
                except TimeoutError:
                    # One hung backend call (or a stalled judge) must not hold the whole
                    # experiment; the question is recorded as an error instead, and the
                    # aggregate reports how many rows that was.
                    item = self._timed_out_example(example, started, timeout)
            async with write_lock:
                finished[example.id] = item
                if on_progress is not None:
                    try:
                        on_progress(ordered())
                    except Exception:
                        # A checkpoint that cannot be written costs a resume, not the
                        # experiment: the results are returned to the caller either way.
                        logger.warning("checkpoint write failed", exc_info=True)

        if pending:
            tasks = [asyncio.create_task(run_one(example)) for example in pending]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                # Stop the still-running examples before the caller records "failed": a
                # late checkpoint write would otherwise overwrite that status.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        examples = ordered()
        metric_values: dict[str, list[float]] = {}
        for item in examples:
            for key, value in item["metrics"].items():
                metric_values.setdefault(key, []).append(value)
        return examples, metric_values

    def _timed_out_example(
        self,
        example,
        started: float,
        timeout: float | None,
    ) -> dict[str, Any]:
        logger.warning(
            "example %s timed out after %ss and is excluded from every average",
            example.id,
            timeout,
        )
        return {
            "example_id": example.id,
            "question": example.question,
            "category": example.category,
            "expected_document_id": example.expected_document_id,
            "expected_page": example.expected_page,
            "expected_answer": example.expected_answer,
            "should_refuse": bool(example.should_refuse),
            "evidence_mode": example.evidence_mode,
            "expected_documents": [
                target.document_id for target in evidence_targets(example)
            ],
            "generated_answer": None,
            "judge_reason": None,
            "retrieved": [],
            "metrics": {},
            "passage_rank": None,
            "passage_ranks": [],
            "cache_hit": False,
            "rewritten_queries": [],
            "latency_ms": (perf_counter() - started) * 1000,
            "timed_out": True,
            "error": f"timed out after {timeout:g}s",
        }

    async def _evaluate_example(
        self,
        evaluation: dict[str, Any],
        example,
        chunks,
        document_version: str | None,
        rerank: bool | None,
        query_rewrite: bool | None,
        answer_evaluation: bool,
        fusion: str | None,
    ) -> dict[str, Any]:
        """Answer one question and score it. Runs under the concurrency gate."""
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
                fusion=fusion,
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
                # Empty quotes are dropped by passage_ranks anyway; leaving them out
                # here keeps the argument type honest instead of passing None through.
                quotes = [target.quote for target in targets if target.quote]
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
                llm = self.llm
                assert llm is not None, "run() rejects answer evaluation without an LLM"
                generated_answer, _ = await answer_with_evidence(
                    llm,
                    example.question,
                    retrieved.results,
                )
                quality = await judge_answer(
                    llm,
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
            example_span.set_outputs(
                {
                    "retrieved_document_ids": retrieved_ids,
                    "metrics": metrics,
                }
            )
            return item

    async def _run_langsmith(
        self,
        evaluation: dict[str, Any],
        dataset,
        chunks,
        document_version: str | None,
        rerank: bool | None,
        query_rewrite: bool | None,
        answer_evaluation: bool,
        fusion: str | None = None,
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
                fusion=fusion,
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
