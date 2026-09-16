"""Offline LangSmith evaluation helpers exposed as a top-level module for the MVP."""

from collections.abc import Callable
from typing import Any

from app.config import Settings
from app.core.evaluation import RetrievalExample, recall_at_k, reciprocal_rank


def retrieval_evaluators() -> list[Callable[[dict[str, Any], dict[str, Any]], dict[str, float]]]:
    def recall(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, float]:
        example = RetrievalExample(inputs["question"], inputs["expected_document_id"], outputs["document_ids"])
        return {"key": "recall_at_5", "score": recall_at_k(example, 5)}

    def mrr(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, float]:
        example = RetrievalExample(inputs["question"], inputs["expected_document_id"], outputs["document_ids"])
        return {"key": "mrr", "score": reciprocal_rank(example)}

    return [recall, mrr]


def experiment_metadata(settings: Settings, dataset_name: str, retrieval_mode: str, top_k: int) -> dict[str, Any]:
    return {"dataset_name": dataset_name, "retrieval_mode": retrieval_mode, "top_k": top_k,
            "rag_version": settings.rag_version, "prompt_version": settings.prompt_version,
            "llm_model": settings.llm_model, "environment": settings.app_env}

