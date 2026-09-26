import re
from collections import Counter
from math import log, sqrt
from typing import Protocol

from app.core.embeddings import HashEmbedding
from app.core.ingestion import Chunk


class SyncEmbedding(Protocol):
    def embed(self, text: str) -> list[float]: ...


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


def bm25_scores(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Score a corpus with Okapi BM25, including IDF and length normalization."""
    if not documents:
        return []
    query_terms = set(_tokens(query))
    tokenized = [_tokens(document) for document in documents]
    average_length = sum(map(len, tokenized)) / len(tokenized) or 1.0
    document_frequency = {
        term: sum(term in terms for terms in tokenized) for term in query_terms
    }
    scores: list[float] = []
    for terms in tokenized:
        frequencies = Counter(terms)
        length_factor = 1 - b + b * len(terms) / average_length
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            frequency_weight = frequency * (k1 + 1) / (frequency + k1 * length_factor)
            idf = log(1 + (len(documents) - document_frequency[term] + 0.5) /
                      (document_frequency[term] + 0.5))
            score += idf * frequency_weight
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


def retrieve(
    query: str,
    chunks: list[Chunk],
    top_k: int = 5,
    mode: str = "hybrid",
    *,
    embedding: SyncEmbedding | None = None,
) -> list[tuple[Chunk, float]]:
    sparse_scores = bm25_scores(query, [chunk.text for chunk in chunks])
    sparse = [
        (chunk, score)
        for chunk, score in zip(chunks, sparse_scores, strict=True)
        if score > 0
    ]
    sparse.sort(key=lambda item: item[1], reverse=True)
    embedding = embedding or HashEmbedding()
    query_vector = embedding.embed(query)
    dense = []
    for chunk in chunks:
        score = cosine_similarity(query_vector, embedding.embed(chunk.text))
        if score > 0:
            dense.append((chunk, score))
    dense.sort(key=lambda item: item[1], reverse=True)
    if mode == "sparse":
        return sparse[:top_k]
    if mode == "dense":
        return dense[:top_k]
    return reciprocal_rank_fusion(dense, sparse, top_k=top_k)


def reciprocal_rank_fusion(*ranked_lists: list[tuple[Chunk, float]], k: int = 60, top_k: int = 5) -> list[tuple[Chunk, float]]:
    """Fuse independent retriever rankings while preventing duplicate evidence."""
    scores: dict[str, tuple[Chunk, float]] = {}
    for ranked in ranked_lists:
        for rank, (chunk, _) in enumerate(ranked, start=1):
            current = scores.get(chunk.id)
            fused = (current[1] if current else 0.0) + 1.0 / (k + rank)
            scores[chunk.id] = (chunk, fused)
    return sorted(scores.values(), key=lambda item: item[1], reverse=True)[:top_k]
