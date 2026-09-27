from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil, log2
from statistics import fmean
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


def _without_whitespace(text: str) -> str:
    """Chunking collapses whitespace, so quotes taken from the raw file may not match."""
    return "".join(text.split())


def passage_rank(retrieved_texts: Sequence[str], quote: str | None) -> int | None:
    """Rank of the first retrieved passage containing the ground-truth quote.

    Returns None when the quote is empty or absent from every passage. Document-level
    metrics cannot see this: with few documents a mediocre ranking still puts the right
    document on top, while the sentence that actually answers the question may be last.
    """
    needle = _without_whitespace(quote or "")
    if not needle:
        return None
    for rank, text in enumerate(retrieved_texts, start=1):
        if needle in _without_whitespace(text):
            return rank
    return None


def passage_metrics(retrieved_texts: Sequence[str], quote: str | None) -> dict[str, float]:
    """Whether the answering passage was retrieved, and how well it was ranked."""
    rank = passage_rank(retrieved_texts, quote)
    return {
        "passage_hit": 1.0 if rank else 0.0,
        "passage_at_1": 1.0 if rank == 1 else 0.0,
        "passage_mrr": 1.0 / rank if rank else 0.0,
    }


def retrieval_metrics(example: RetrievalExample, k: int) -> dict[str, float]:
    return {
        f"recall_at_{k}": recall_at_k(example, k),
        "mrr": reciprocal_rank(example),
        f"precision_at_{k}": precision_at_k(example, k),
        f"ndcg_at_{k}": ndcg_at_k(example, k),
    }


def metrics_at_k(example: RetrievalExample, cutoffs: Iterable[int]) -> dict[str, float]:
    """Report every cutoff from a single ranking.

    One retrieval run produces one ranking, so measuring Recall@1 and Recall@5
    from the same list keeps the numbers comparable; re-running retrieval with a
    different top_k would change the candidate pool and therefore the ranking.
    """
    metrics: dict[str, float] = {"mrr": reciprocal_rank(example)}
    for k in cutoffs:
        metrics[f"recall_at_{k}"] = recall_at_k(example, k)
        metrics[f"ndcg_at_{k}"] = ndcg_at_k(example, k)
        metrics[f"precision_at_{k}"] = precision_at_k(example, k)
    return metrics


def latency_percentiles(values: Sequence[float]) -> dict[str, float]:
    """Nearest-rank percentiles; empty input returns no keys rather than zeros."""
    if not values:
        return {}
    ordered = sorted(values)

    def at(percent: float) -> float:
        index = max(0, ceil(len(ordered) * percent / 100) - 1)
        return ordered[min(index, len(ordered) - 1)]

    return {
        "p50": at(50),
        "p95": at(95),
        "p100": at(100),
        "mean": fmean(ordered),
    }


def evaluation_metadata(*, dataset_name: str, git_commit: str, retrieval_mode: str, top_k: int) -> dict[str, Any]:
    return {"dataset_name": dataset_name, "git_commit": git_commit,
            "retrieval_mode": retrieval_mode, "top_k": top_k}
