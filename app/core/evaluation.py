from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil, log2
from statistics import fmean
from typing import Any, NamedTuple


class EvidenceTarget(NamedTuple):
    """One hop of the ground truth: which document, which page, which sentence answers it."""

    document_id: str
    page: int | None
    quote: str | None


def evidence_targets(example: Any) -> list[EvidenceTarget]:
    """Resolve an example's labels into the list of hops it expects.

    A multi-hop example carries ``expected_evidence``; the single-label columns stay the
    fallback, so every dataset written before multi-hop existed keeps its meaning. An
    unanswerable example (``should_refuse``) has no targets at all, which is what the
    runner needs in order to score it on refusal instead of on recall.
    """
    if getattr(example, "should_refuse", False):
        return []
    spans = getattr(example, "expected_evidence", None) or []
    if spans:
        return [EvidenceTarget(span.document_id, span.page, span.quote) for span in spans]
    document_id = getattr(example, "expected_document_id", None)
    if not document_id:
        return []
    return [
        EvidenceTarget(
            document_id,
            getattr(example, "expected_page", None),
            getattr(example, "evidence_quote", None),
        )
    ]


@dataclass(frozen=True)
class RetrievalExample:
    question: str
    # One document id, or several for a multi-hop question.
    expected_document_id: str | Sequence[str]
    retrieved_document_ids: list[str]

    def __post_init__(self) -> None:
        expected = self.expected_document_id
        if isinstance(expected, str):
            object.__setattr__(self, "expected_document_id", (expected,))
        else:
            object.__setattr__(self, "expected_document_id", tuple(dict.fromkeys(expected)))

    @property
    def targets(self) -> tuple[str, ...]:
        return self.expected_document_id  # type: ignore[return-value]


def _ranks(example: RetrievalExample) -> dict[str, int]:
    """First rank (1-based) at which each expected document appears."""
    ranks: dict[str, int] = {}
    for target in example.targets:
        try:
            ranks[target] = example.retrieved_document_ids.index(target) + 1
        except ValueError:
            continue
    return ranks


def recall_at_k(example: RetrievalExample, k: int) -> float:
    """Fraction of the expected documents found within the first ``k`` results.

    With one expected document this is the usual 0/1 Recall@k; with several it is a set
    recall, which is the only honest way to score a question whose answer needs two
    documents — finding the first hop is partial credit, not success.
    """
    if not example.targets:
        return 0.0
    ranks = _ranks(example)
    found = sum(1 for target in example.targets if ranks.get(target, k + 1) <= k)
    return found / len(example.targets)


def all_targets_at_k(example: RetrievalExample, k: int) -> float:
    """1.0 only when every hop is inside the first ``k`` results."""
    if not example.targets:
        return 0.0
    ranks = _ranks(example)
    return float(all(ranks.get(target, k + 1) <= k for target in example.targets))


def reciprocal_rank(example: RetrievalExample) -> float:
    """Mean reciprocal rank over hops; a hop that was never retrieved contributes 0."""
    if not example.targets:
        return 0.0
    ranks = _ranks(example)
    return fmean(1 / ranks[target] if target in ranks else 0.0 for target in example.targets)


def precision_at_k(example: RetrievalExample, k: int) -> float:
    if k <= 0:
        return 0.0
    targets = set(example.targets)
    return sum(
        1 for document_id in example.retrieved_document_ids[:k] if document_id in targets
    ) / k


def ndcg_at_k(example: RetrievalExample, k: int) -> float:
    """Mean gain per hop; discounting is per target rather than over a single result list."""
    if not example.targets:
        return 0.0
    ranks = _ranks(example)
    gains = [
        1.0 / log2(ranks[target] + 1) if 0 < ranks.get(target, k + 1) <= k else 0.0
        for target in example.targets
    ]
    return fmean(gains)


def _without_whitespace(text: str) -> str:
    """Chunking collapses whitespace, so quotes taken from the raw file may not match."""
    return "".join(text.split())


def passage_ranks(
    retrieved_texts: Sequence[str],
    quotes: str | Sequence[str] | None,
) -> list[int | None]:
    """Rank of the first retrieved passage containing each quote, one entry per quote.

    None means that sentence is absent from every passage. Document-level metrics cannot
    see this: with few documents a mediocre ranking still puts the right document on top,
    while the sentence that answers the question may be last. Empty quotes are dropped —
    nothing to judge means nothing to score.
    """
    needles = [quotes] if isinstance(quotes, str) else list(quotes or [])
    normalized = [
        collapsed
        for collapsed in (_without_whitespace(needle or "") for needle in needles)
        if collapsed
    ]
    ranks: list[int | None] = []
    for needle in normalized:
        rank: int | None = None
        for position, text in enumerate(retrieved_texts, start=1):
            if needle in _without_whitespace(text):
                rank = position
                break
        ranks.append(rank)
    return ranks


def passage_metrics(
    retrieved_texts: Sequence[str],
    quotes: str | Sequence[str] | None,
) -> dict[str, float]:
    """Whether the answering passages were retrieved, and how well they were ranked.

    ``passage_hit`` requires every one of them (a two-hop question is not answered by a
    single passage), while ``passage_mrr`` averages per-hop ranks so partial success
    stays visible instead of collapsing to zero.
    """
    ranks = passage_ranks(retrieved_texts, quotes)
    if not ranks:
        return {}
    return {
        "passage_hit": 1.0 if all(ranks) else 0.0,
        "passage_at_1": 1.0 if all(rank == 1 for rank in ranks) else 0.0,
        "passage_mrr": fmean(1 / rank if rank else 0.0 for rank in ranks),
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
        metrics[f"all_targets_at_{k}"] = all_targets_at_k(example, k)
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
