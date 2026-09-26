from dataclasses import dataclass
from math import log2
from typing import Any


@dataclass(frozen=True)
class RetrievalExample:
    question: str
    expected_document_id: str
    retrieved_document_ids: list[str]


def recall_at_k(example: RetrievalExample, k: int) -> float:
    return float(example.expected_document_id in example.retrieved_document_ids[:k])


def reciprocal_rank(example: RetrievalExample) -> float:
    try:
        return 1 / (example.retrieved_document_ids.index(example.expected_document_id) + 1)
    except ValueError:
        return 0.0


def precision_at_k(example: RetrievalExample, k: int) -> float:
    if k <= 0:
        return 0.0
    retrieved = example.retrieved_document_ids[:k]
    return float(retrieved.count(example.expected_document_id)) / k


def ndcg_at_k(example: RetrievalExample, k: int) -> float:
    try:
        rank = example.retrieved_document_ids[:k].index(example.expected_document_id) + 1
    except ValueError:
        return 0.0
    return 1.0 / log2(rank + 1)


def retrieval_metrics(example: RetrievalExample, k: int) -> dict[str, float]:
    return {
        f"recall_at_{k}": recall_at_k(example, k),
        "mrr": reciprocal_rank(example),
        f"precision_at_{k}": precision_at_k(example, k),
        f"ndcg_at_{k}": ndcg_at_k(example, k),
    }


def evaluation_metadata(*, dataset_name: str, git_commit: str, retrieval_mode: str, top_k: int) -> dict[str, Any]:
    return {"dataset_name": dataset_name, "git_commit": git_commit,
            "retrieval_mode": retrieval_mode, "top_k": top_k}
