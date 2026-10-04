import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import log, sqrt
from typing import Protocol

from app.core.embeddings import HashEmbedding
from app.core.ingestion import Chunk


class SyncEmbedding(Protocol):
    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class Fusion:
    """How the dense and sparse rankings combine into one.

    The default reproduces what the product ships: equal-weight reciprocal rank fusion.
    The variants exist because equal-weight fusion of a weak channel loses more questions
    than it rescues on this corpus (docs/evaluation-report.md §11) — a candidate that is
    merely decent in both rankings outranks a candidate that is best in either — and because
    RRF discards the score, which is the only signal about *how much* better a candidate is.
    """

    method: str = "rrf"  # "rrf" (rank only) | "convex" (min-max normalised scores)
    weights: tuple[float, float] = (1.0, 1.0)  # (dense, sparse)
    truncate: int | None = None  # per-channel candidate cap applied before fusing
    k: int = 60


FUSIONS: dict[str, Fusion] = {
    "rrf": Fusion(),
    "rrf-bm25-heavy": Fusion(weights=(0.3, 0.7)),
    "rrf-truncate-5": Fusion(truncate=5),
    "convex": Fusion(method="convex"),
    "convex-bm25-heavy": Fusion(method="convex", weights=(0.3, 0.7)),
}


def fusion_from_name(name: str) -> Fusion:
    try:
        return FUSIONS[name]
    except KeyError as exc:
        raise ValueError(
            f"unsupported fusion: {name}; expected one of {sorted(FUSIONS)}"
        ) from exc


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", lowered)
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        if len(run) <= 8:
            tokens.append(run)
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
        tokens.extend(run[index:index + 3] for index in range(len(run) - 2))
    return tokens


def tokenize(text: str) -> list[str]:
    return _tokens(text)


def query_terms(text: str) -> list[str]:
    """Distinct query terms in the order they first appear.

    Order matters twice over. The float score is a sum over terms, and adding floats is
    not associative, so the order decides the last bits of the result; and the previous
    implementation iterated a ``set``, whose order depends on string hashing and
    therefore on ``PYTHONHASHSEED``. Two runs of the same corpus could then disagree in
    the last bits - enough to flip the tie that breaks a ranking. First-appearance order
    is deterministic and cheap.
    """
    return list(dict.fromkeys(_tokens(text)))


@dataclass
class BM25Index:
    """Okapi BM25 over a fixed corpus, with the term statistics built once.

    ``bm25_scores`` re-tokenised the whole corpus on every call: on the 386-chunk corpus
    that is ~245k characters of tokenisation plus one ``Counter`` per document, for a
    table that cannot change while the corpus does not - and a query does it again, and
    again. Measured on a 400-document corpus a single query went from ~210 ms to ~2 ms.

    Two things are different from the previous implementation and one is not:

    * queries read an inverted index (term -> [(document, frequency)]) plus a
      document-length table, and only touch documents containing a query term. Documents
      with no query term score zero and were filtered out before, so the candidate set is
      identical;
    * the per-document sum runs in ``query_terms`` order (see above);
    * the formula itself - IDF, ``k1``, ``b``, length normalisation - is untouched, so
      rankings are unchanged. The CI quality gate confirms it at 1e-6 on the fixture
      corpus.
    """

    documents: list[str]
    k1: float = 1.5
    b: float = 0.75
    lengths: list[int] = field(init=False, repr=False)
    average_length: float = field(init=False, repr=False)
    postings: dict[str, list[tuple[int, int]]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        lengths: list[int] = []
        postings: dict[str, list[tuple[int, int]]] = {}
        for index, document in enumerate(self.documents):
            tokens = _tokens(document)
            lengths.append(len(tokens))
            for term, frequency in Counter(tokens).items():
                postings.setdefault(term, []).append((index, frequency))
        self.lengths = lengths
        self.average_length = (sum(lengths) / len(lengths)) if lengths else 1.0
        self.postings = postings

    def __len__(self) -> int:
        return len(self.documents)

    def document_frequency(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def idf(self, term: str) -> float:
        frequency = self.document_frequency(term)
        return log(
            1 + (len(self.documents) - frequency + 0.5) / (frequency + 0.5)
        )

    def _length_factor(self, index: int) -> float:
        return 1 - self.b + self.b * self.lengths[index] / self.average_length

    def scores(self, query: str) -> list[float]:
        """One score per document, in corpus order (zero where no query term occurs)."""
        scores = [0.0] * len(self.documents)
        for index, contribution in self._contributions(query):
            scores[index] += contribution
        return scores

    def ranked(self, query: str) -> list[tuple[int, float]]:
        """``(document index, score)`` for every positive score, best first.

        Ties keep corpus order, which is what a stable sort over an ascending index list
        produced before; downstream fusion depends on that order.
        """
        contributions: dict[int, float] = {}
        for index, contribution in self._contributions(query):
            contributions[index] = contributions.get(index, 0.0) + contribution
        ranked = [
            (index, score) for index, score in sorted(contributions.items()) if score > 0
        ]
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    def _contributions(self, query: str):
        """Yield ``(document index, term contribution)`` in deterministic query-term order.

        The IDF is hoisted out of the per-document loop: it depends only on the term, and
        the old inner-loop placement recomputed a logarithm for every (term, document)
        pair.
        """
        for term in query_terms(query):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            k1 = self.k1
            for index, frequency in postings:
                distance = self._length_factor(index)
                yield index, idf * (frequency * (k1 + 1) / (frequency + k1 * distance))


def bm25_scores(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Score a corpus with Okapi BM25 in a single pass over the documents.

    Kept alongside the index on purpose. Assembling the inverted index costs about half
    again as much as scoring the corpus once (measured with
    ``scripts/benchmark_local_retrieval.py``: ~171 ms vs ~112 ms for 20 queries over 400
    documents, i.e. ~8.5 ms vs ~5.6 ms per query), so a caller that will not repeat the
    query should not pay for it. Any caller that does repeat it holds a ``BM25Index``.

    ``tests/test_bm25_index.py`` pins the two implementations to identical output, so the
    duplication is checked rather than trusted.
    """
    if not documents:
        return []
    terms = query_terms(query)
    tokenized = [_tokens(document) for document in documents]
    average_length = sum(map(len, tokenized)) / len(tokenized) or 1.0
    document_frequency = {
        term: sum(term in tokens for tokens in tokenized) for term in terms
    }
    scores: list[float] = []
    for tokens in tokenized:
        frequencies = Counter(tokens)
        length_factor = 1 - b + b * len(tokens) / average_length
        score = 0.0
        for term in terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            idf = log(
                1
                + (len(documents) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            score += idf * (frequency * (k1 + 1) / (frequency + k1 * length_factor))
        scores.append(score)
    return scores


def bm25_score(query: str, text: str, *, k1: float = 1.5, b: float = 0.75) -> float:
    return bm25_scores(query, [text], k1=k1, b=b)[0]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same dimensions")
    denominator = sqrt(sum(value * value for value in left)) * sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


def _sparse_channel(
    query: str,
    chunks: list[Chunk],
    index: BM25Index | None,
) -> list[tuple[Chunk, float]]:
    """The BM25 ranking of the corpus, best first, with zero-scoring chunks dropped.

    Two paths that must agree exactly - ``tests/test_bm25_index.py`` and the CI quality
    gate both check it: score the corpus once, or read an index built for it. Both drop
    chunks with no query term and both leave ties in corpus order (a stable sort over a
    list built in corpus order), which is what fusion downstream reads as rank.
    """
    if index is not None:
        return [(chunks[position], score) for position, score in index.ranked(query)]
    scores = bm25_scores(query, [chunk.text for chunk in chunks])
    ranked = [
        (chunk, score)
        for chunk, score in zip(chunks, scores, strict=True)
        if score > 0
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def retrieve(
    query: str,
    chunks: list[Chunk],
    top_k: int = 5,
    mode: str = "hybrid",
    *,
    embedding: SyncEmbedding | None = None,
    fusion: Fusion | None = None,
    index: BM25Index | None = None,
) -> list[tuple[Chunk, float]]:
    """Score the corpus in the requested mode - and only that mode.

    The channels are computed lazily on purpose. Computing both up front meant a BM25-only
    query paid for 386 chunk embeddings on this corpus (BM25 needs term statistics, nothing
    else), and the hybrid path embedded the corpus twice: once here and once in the dense
    retriever that called it.

    ``index`` is the same laziness applied to the sparse channel: pass a prebuilt
    ``BM25Index`` and the query stops re-tokenising the corpus. When it is omitted the
    index is built here, which is the right thing for a one-off call and the wrong thing
    for a caller that repeats queries against one corpus.
    """
    if mode not in {"sparse", "dense", "hybrid"}:
        raise ValueError(f"unsupported retrieval mode: {mode}")

    sparse: list[tuple[Chunk, float]] = []
    if mode in {"sparse", "hybrid"}:
        sparse = _sparse_channel(query, chunks, index)
        if mode == "sparse":
            return sparse[:top_k]

    embedding = embedding or HashEmbedding()
    query_vector = embedding.embed(query)
    dense = []
    for chunk in chunks:
        score = cosine_similarity(query_vector, embedding.embed(chunk.text))
        if score > 0:
            dense.append((chunk, score))
    dense.sort(key=lambda item: item[1], reverse=True)
    if mode == "dense":
        return dense[:top_k]
    return fuse_rankings(dense, sparse, fusion or Fusion(), top_k)


def reciprocal_rank_fusion(
    *ranked_lists: list[tuple[Chunk, float]],
    k: int = 60,
    top_k: int = 5,
    weights: Sequence[float] | None = None,
    truncate: int | None = None,
) -> list[tuple[Chunk, float]]:
    """Fuse independent retriever rankings while preventing duplicate evidence.

    Ties break on the best rank across channels and then on the chunk's own content (page,
    then text) - never on the chunk id. Two properties are at stake, and the id fails the
    second one:

    1. The output must not depend on the order the rankings were passed in. With the earlier
       first-inserted-wins rule, a real question whose answer and a title block scored
       identically (1/(k+1) + 1/(k+2) each) lost to the block purely because the dense list
       was passed first.
    2. The output must not depend on the *import*. Chunk ids are ``f"{document_id}:{index}"``
       and ``document_id`` is a fresh UUID on every ingest, while the store returns chunks
       ordered by id - so ranking on ids makes tie-decided questions flip between runs of the
       same corpus and metric values stop being reproducible (docs/evaluation-report.md §11).
    """
    if weights is None:
        weights = (1.0,) * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights needs one entry per ranking")
    scores: dict[str, tuple[Chunk, float, int]] = {}
    for ranked, weight in zip(ranked_lists, weights, strict=True):
        window = ranked[:truncate] if truncate else ranked
        for rank, (chunk, _) in enumerate(window, start=1):
            current = scores.get(chunk.id)
            fused = (current[1] if current else 0.0) + weight / (k + rank)
            best_rank = min(current[2], rank) if current else rank
            scores[chunk.id] = (chunk, fused, best_rank)
    ordered = sorted(
        scores.values(), key=lambda item: (-item[1], item[2], item[0].page, item[0].text)
    )
    return [(chunk, score) for chunk, score, _ in ordered][:top_k]


def convex_fusion(
    *ranked_lists: list[tuple[Chunk, float]],
    weights: Sequence[float] | None = None,
    top_k: int = 5,
    truncate: int | None = None,
) -> list[tuple[Chunk, float]]:
    """Weighted sum of min-max normalised channel scores.

    RRF keeps only the rank and throws the score away; where a channel's scores are
    comparable within a query, a convex combination keeps that confidence signal. Normalising
    per channel is what makes two differently-scaled scorers addable at all.
    """
    if weights is None:
        weights = (1.0,) * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights needs one entry per ranking")
    totals: dict[str, tuple[Chunk, float, int]] = {}
    for ranked, weight in zip(ranked_lists, weights, strict=True):
        window = ranked[:truncate] if truncate else ranked
        if not window:
            continue
        values = [score for _, score in window]
        low, high = min(values), max(values)
        span = high - low
        for rank, (chunk, score) in enumerate(window, start=1):
            normalized = 1.0 if span == 0 else (score - low) / span
            current = totals.get(chunk.id)
            fused = (current[1] if current else 0.0) + weight * normalized
            best_rank = min(current[2], rank) if current else rank
            totals[chunk.id] = (chunk, fused, best_rank)
    ordered = sorted(
        totals.values(), key=lambda item: (-item[1], item[2], item[0].page, item[0].text)
    )
    return [(chunk, score) for chunk, score, _ in ordered][:top_k]


def fuse_rankings(
    dense: list[tuple[Chunk, float]],
    sparse: list[tuple[Chunk, float]],
    fusion: "Fusion",
    top_k: int,
) -> list[tuple[Chunk, float]]:
    """Apply one Fusion spec to the (dense, sparse) pair."""
    if fusion.method == "rrf":
        return reciprocal_rank_fusion(
            dense,
            sparse,
            k=fusion.k,
            top_k=top_k,
            weights=fusion.weights,
            truncate=fusion.truncate,
        )
    if fusion.method == "convex":
        return convex_fusion(
            dense,
            sparse,
            weights=fusion.weights,
            top_k=top_k,
            truncate=fusion.truncate,
        )
    raise ValueError(f"unsupported fusion method: {fusion.method}")
