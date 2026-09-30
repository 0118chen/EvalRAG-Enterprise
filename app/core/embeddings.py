"""Embedding providers with a deterministic local implementation for tests."""

import hashlib
import inspect
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from app.core.errors import BackendUnavailableError

if TYPE_CHECKING:
    from app.config import Settings


class EmbeddingProvider(Protocol):
    def embed(self, text: str): ...


# Process-wide vector cache: keyed by (model identity, sha256(text)). Sharing it across
# instances is the point - a fresh retriever per query must still hit the same vectors, and a
# paid embedding API must never be asked twice for the same text. 4096 entries of a 1024-d
# float vector is ~33 MB, and this corpus is 386 chunks, so the default is generous; bound it
# rather than letting a long-lived process grow without limit.
_VECTOR_CACHE: "OrderedDict[tuple[str, str], tuple[float, ...]]" = OrderedDict()
_CACHE_LIMIT = 4096
_CACHE_STATS = {"hits": 0, "misses": 0}


def _digest(model_key: str, text: str) -> tuple[str, str]:
    return (model_key, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _cache_lookup(cache_key: tuple[str, str]) -> tuple[float, ...] | None:
    vector = _VECTOR_CACHE.get(cache_key)
    if vector is None:
        _CACHE_STATS["misses"] += 1
        return None
    _VECTOR_CACHE.move_to_end(cache_key)
    _CACHE_STATS["hits"] += 1
    return vector


def _cache_store(cache_key: tuple[str, str], vector: list[float]) -> list[float]:
    stored = tuple(float(value) for value in vector)
    _VECTOR_CACHE[cache_key] = stored
    _VECTOR_CACHE.move_to_end(cache_key)
    while len(_VECTOR_CACHE) > _CACHE_LIMIT:
        _VECTOR_CACHE.popitem(last=False)
    return list(stored)


def reset_embedding_cache(limit: int | None = None) -> None:
    """Clear the shared vector cache (tests need this; a corpus swap may want it too)."""
    global _CACHE_LIMIT
    _VECTOR_CACHE.clear()
    _CACHE_STATS["hits"] = 0
    _CACHE_STATS["misses"] = 0
    if limit is not None:
        _CACHE_LIMIT = limit


def embedding_cache_stats() -> dict[str, int]:
    return {
        "hits": _CACHE_STATS["hits"],
        "misses": _CACHE_STATS["misses"],
        "size": len(_VECTOR_CACHE),
        "limit": _CACHE_LIMIT,
    }


@dataclass(frozen=True)
class HashEmbedding:
    dimensions: int = 32

    def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        features = re.findall(r"[a-z0-9_]+", lowered)
        for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
            features.extend(run)
            features.extend(run[index:index + 2] for index in range(len(run) - 1))
        values = [0.0] * self.dimensions
        for feature in features or [lowered]:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


@dataclass(frozen=True)
class OpenAICompatibleEmbedding:
    base_url: str
    api_key: str
    model: str
    dimensions: int
    batch_size: int = 64
    transport: httpx.AsyncBaseTransport | None = None

    async def embed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    json={
                        "model": self.model,
                        "input": text,
                        "dimensions": self.dimensions,
                    },
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                vector = list(response.json()["data"][0]["embedding"])
                if len(vector) != self.dimensions:
                    raise ValueError(
                        "embedding response dimension mismatch: "
                        f"expected {self.dimensions}, got {len(vector)}"
                    )
                return vector
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise BackendUnavailableError(
                f"embedding service unavailable: {exc}"
            ) from exc

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch with as few requests as the provider allows.

        One client per *batch* rather than per text: the corpus path embeds hundreds of chunks
        at once, and creating an `httpx.AsyncClient` (plus a TLS handshake) per chunk was the
        single largest waste on this path. The OpenAI-compatible API answers with an explicit
        `index` per item, and it is not guaranteed to be sorted, so map by index instead of
        trusting the response order.
        """
        if not texts:
            return []
        vectors: list[list[float] | None] = [None] * len(texts)
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                for start in range(0, len(texts), self.batch_size):
                    batch = texts[start : start + self.batch_size]
                    response = await client.post(
                        f"{self.base_url.rstrip('/')}/embeddings",
                        json={
                            "model": self.model,
                            "input": batch,
                            "dimensions": self.dimensions,
                        },
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    response.raise_for_status()
                    for item in response.json()["data"]:
                        index = int(item.get("index", 0))
                        vector = [float(value) for value in item["embedding"]]
                        if len(vector) != self.dimensions:
                            raise ValueError(
                                "embedding response dimension mismatch: "
                                f"expected {self.dimensions}, got {len(vector)}"
                            )
                        vectors[start + index] = vector
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise BackendUnavailableError(
                f"embedding service unavailable: {exc}"
            ) from exc
        missing = [index for index, vector in enumerate(vectors) if vector is None]
        if missing:
            raise ValueError(f"embedding response missing items at {missing}")
        return [vector for vector in vectors if vector is not None]


async def embed_text(provider: EmbeddingProvider, text: str) -> list[float]:
    result = provider.embed(text)
    if inspect.isawaitable(result):
        result = await result
    return list(result)


async def embed_many_texts(
    provider: EmbeddingProvider, texts: list[str]
) -> list[list[float]]:
    """Embed many texts, preferring the provider's batch endpoint when it has one."""
    if not texts:
        return []
    embed_many = getattr(provider, "embed_many", None)
    if embed_many is not None:
        return [list(vector) for vector in await embed_many(texts)]
    return [await embed_text(provider, text) for text in texts]


@dataclass
class CachingEmbedding:
    """Wrap a provider with a process-wide vector cache keyed by (model identity, text hash).

    Why a wrapper instead of caching inside each provider: the cache has to be shared between
    instances (every query builds a new retriever), and keeping it here means an arbitrary
    provider - including a test double - gets the same behaviour for free. `key` is the model
    identity (`provider:model:dimensions`), so switching models cannot serve stale vectors of
    the wrong dimension or the wrong embedding space.
    """

    inner: EmbeddingProvider
    key: str

    @property
    def dimensions(self) -> int | None:
        return getattr(self.inner, "dimensions", None)

    def embed(self, text: str):
        cache_key = _digest(self.key, text)
        cached = _cache_lookup(cache_key)
        if cached is not None:
            return list(cached)
        result = self.inner.embed(text)
        if inspect.isawaitable(result):
            return self._store_when_ready(cache_key, result)
        return _cache_store(cache_key, result)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Batch path: only cache misses travel to the provider, and duplicates collapse."""
        if not texts:
            return []
        vectors: list[list[float] | None] = [None] * len(texts)
        misses: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            cached = _cache_lookup(_digest(self.key, text))
            if cached is not None:
                vectors[index] = list(cached)
            else:
                misses.append((index, text))
        if misses:
            unique = list(dict.fromkeys(text for _, text in misses))
            fresh = await _embed_uncached(self.inner, unique)
            by_text = dict(zip(unique, fresh, strict=True))
            for index, text in misses:
                vectors[index] = _cache_store(_digest(self.key, text), by_text[text])
        return [vector for vector in vectors if vector is not None]

    async def _store_when_ready(
        self, cache_key: tuple[str, str], awaitable
    ) -> list[float]:
        return _cache_store(cache_key, list(await awaitable))


async def _embed_uncached(
    provider: EmbeddingProvider, texts: list[str]
) -> list[list[float]]:
    embed_many = getattr(provider, "embed_many", None)
    if embed_many is not None:
        return [list(vector) for vector in await embed_many(texts)]
    return [await embed_text(provider, text) for text in texts]


def create_embedding(settings: "Settings") -> EmbeddingProvider:
    """Build the configured provider, always wrapped in the shared vector cache.

    The wrapper is not optional: without it every query re-embeds the whole corpus, which cost
    387 provider calls per query on this corpus (and one HTTP client per chunk on the
    openai-compatible path).
    """
    if settings.embedding_provider == "hash":
        return CachingEmbedding(
            HashEmbedding(settings.embedding_dimensions),
            f"hash:{settings.embedding_dimensions}",
        )
    if settings.embedding_provider == "openai-compatible":
        if not settings.embedding_api_key:
            raise ValueError("EMBEDDING_API_KEY is required for openai-compatible embeddings")
        return CachingEmbedding(
            OpenAICompatibleEmbedding(
                settings.embedding_base_url,
                settings.embedding_api_key,
                settings.embedding_model,
                settings.embedding_dimensions,
            ),
            f"openai-compatible:{settings.embedding_model}:{settings.embedding_dimensions}",
        )
    raise ValueError(f"unsupported embedding provider: {settings.embedding_provider}")

