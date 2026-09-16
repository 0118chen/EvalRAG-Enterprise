from dataclasses import dataclass
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


def evaluation_metadata(*, dataset_name: str, git_commit: str, retrieval_mode: str, top_k: int) -> dict[str, Any]:
    return {"dataset_name": dataset_name, "git_commit": git_commit,
            "retrieval_mode": retrieval_mode, "top_k": top_k}

