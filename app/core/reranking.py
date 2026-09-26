"""Provider-neutral rerankers with a deterministic local fallback."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.core.ingestion import Chunk
from app.core.retrieval import tokenize


class Reranker(Protocol):
    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]: ...


class NoOpReranker:
    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        return candidates[:top_k]


class LexicalReranker:
    """Blend retrieval score and query-term coverage for a cheap second stage."""

    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        if not candidates:
            return []
        query_terms = set(tokenize(query))
        raw_scores = [score for _, score in candidates]
        low, high = min(raw_scores), max(raw_scores)
        span = high - low
        ranked: list[tuple[Chunk, float]] = []
        for chunk, score in candidates:
            normalized = 1.0 if span == 0 else (score - low) / span
            chunk_terms = set(tokenize(chunk.text))
            coverage = (
                len(query_terms & chunk_terms) / len(query_terms)
                if query_terms
                else 0.0
            )
            ranked.append((chunk, normalized * 0.7 + coverage * 0.3))
        return sorted(ranked, key=lambda item: item[1], reverse=True)[:top_k]


@dataclass
class CrossEncoderReranker:
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __post_init__(self) -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(self.model_name)

    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        def score() -> list[tuple[Chunk, float]]:
            values = self.model.predict([(query, chunk.text) for chunk, _ in candidates])
            ranked = [
                (chunk, float(value))
                for (chunk, _), value in zip(candidates, values, strict=True)
            ]
            return sorted(ranked, key=lambda item: item[1], reverse=True)[:top_k]

        return await asyncio.to_thread(score)


@dataclass
class ResilientReranker:
    primary: Reranker
    fallback: Reranker

    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        try:
            return await self.primary.rank(query, candidates, top_k)
        except (ImportError, RuntimeError, ConnectionError, TimeoutError):
            return await self.fallback.rank(query, candidates, top_k)


def create_reranker(settings: Settings) -> Reranker:
    if not settings.rerank_enabled:
        return NoOpReranker()
    return LexicalReranker()
