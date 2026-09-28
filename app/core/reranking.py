"""Provider-neutral rerankers with a deterministic local fallback."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings
from app.core.errors import BackendUnavailableError
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


@dataclass(frozen=True)
class TypesafeReranker:
    """Score a shortlist with one typed Noul judgment per (query, candidate) pair.

    This is the semantic second stage the two local channels cannot provide: the model reads
    the query and one candidate together and returns the probability that the candidate is
    the passage that answers the query. Contract taken from the live docs
    (https://docs.typesafe.ai/api: ``POST /v1/systemone``, ``model: jev-latest``, a
    ``questions`` map whose entry is ``{"type": "noul", "instructions": ..., "criteria": ...}``
    and an ``answers`` map carrying the ``noul`` probability) and the pattern from
    https://docs.typesafe.ai/cookbooks/rerank_typesafe.md — one request per candidate, fired
    concurrently, then sorted by the returned probability.

    `transport` is the test seam: the contract test drives it with `httpx.MockTransport`, the
    same way the Milvus and Elasticsearch clients are contract-tested without a cluster.
    """

    api_key: str
    base_url: str = "https://api.typesafe.ai"
    model: str = "jev-latest"
    instructions: str = (
        "Is `candidate` the passage that answers `query`? Judge only whether this passage "
        "states the rule, figure or fact the query asks for."
    )
    criteria_true: str = (
        "The passage states the specific rule, figure or fact the query asks for."
    )
    criteria_false: str = (
        "The passage is merely on a similar topic, restates the question, or answers a "
        "different question."
    )
    concurrency: int = 8
    timeout: float = 30.0
    transport: httpx.AsyncBaseTransport | None = None
    retry_attempts: int = 3
    # Optional sink for the raw `usage` blocks, so a caller can total tokens and cost without
    # the reranker having to know about pricing.
    usage_sink: list[dict] | None = None

    async def rank(
        self,
        query: str,
        candidates: list[tuple[Chunk, float]],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        if not candidates:
            return []
        gate = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            scores = await asyncio.gather(
                *[
                    self._score(client, gate, query, chunk.text)
                    for chunk, _ in candidates
                ]
            )
        ranked = [
            (chunk, score)
            for (chunk, _), score in zip(candidates, scores, strict=True)
        ]
        # Ties break on content, never on the chunk id: ids are `f"{document_id}:{index}"` and
        # `document_id` is regenerated on every ingest, so an id tie-break makes the order of
        # equal-probability candidates differ between runs of the same corpus.
        return sorted(ranked, key=lambda item: (-item[1], item[0].page, item[0].text))[:top_k]

    async def _score(
        self,
        client: httpx.AsyncClient,
        gate: asyncio.Semaphore,
        query: str,
        candidate: str,
    ) -> float:
        # Field names follow the rerank cookbook: `state` carries the query excerpt and the
        # candidate passage, `questions` carries the judgment.
        payload = {
            "model": self.model,
            "state": {"query_excerpt": query, "candidate_passage": candidate},
            "questions": {
                "answers_query": {
                    "type": "noul",
                    "instructions": self.instructions,
                    "criteria": {"true": self.criteria_true, "false": self.criteria_false},
                }
            },
        }
        last_error: str = ""
        for attempt in range(1, self.retry_attempts + 1):
            try:
                async with gate:
                    response = await client.post(
                        f"{self.base_url.rstrip('/')}/v1/systemone",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    # 429 doubles as the throttle signal: wait what the server asks for when it
                    # says so, otherwise back off exponentially. Rate limits here are dynamic
                    # (250k tokens/s, 1200 req/min), so a hard run will meet them.
                    if attempt == self.retry_attempts:
                        last_error = f"HTTP {response.status_code}"
                        break
                    await asyncio.sleep(self._retry_delay(response, attempt))
                    continue
                response.raise_for_status()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt == self.retry_attempts:
                    raise BackendUnavailableError(f"typesafe reranker unavailable: {exc}") from exc
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue
            except httpx.HTTPStatusError as exc:
                raise BackendUnavailableError(
                    f"typesafe reranker rejected the request: {exc.response.status_code}"
                ) from exc
            body = response.json()
            if self.usage_sink is not None:
                self.usage_sink.append(
                    {"usage": body.get("usage", {}), "model": body.get("model", self.model)}
                )
            return float(body["answers"]["answers_query"]["noul"])
        raise BackendUnavailableError(f"typesafe reranker gave up after retries: {last_error}")

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), 30.0)
                except ValueError:
                    pass
        return min(2.0**attempt, 30.0)


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
        except (
            ImportError,
            RuntimeError,
            ConnectionError,
            TimeoutError,
            BackendUnavailableError,
        ):
            return await self.fallback.rank(query, candidates, top_k)


def create_reranker(
    settings: Settings, usage_sink: list[dict] | None = None
) -> Reranker:
    """`usage_sink` only has an effect on the paid backend: it collects the raw `usage` blocks
    so a caller (the experiment harness) can report tokens and cost next to the scores."""
    if not settings.rerank_enabled:
        return NoOpReranker()
    if settings.rerank_backend == "typesafe":
        if not settings.typesafe_api_key:
            raise ValueError(
                "TYPESAFE_API_KEY is required for the typesafe reranker "
                "(get one from https://console.typesafe.ai/keys)"
            )
        return ResilientReranker(
            TypesafeReranker(
                api_key=settings.typesafe_api_key,
                base_url=settings.typesafe_base_url,
                model=settings.typesafe_model,
                concurrency=settings.typesafe_concurrency,
                usage_sink=usage_sink,
            ),
            LexicalReranker(),
        )
    if settings.rerank_backend != "lexical":
        raise ValueError(f"unsupported rerank backend: {settings.rerank_backend}")
    return LexicalReranker()
