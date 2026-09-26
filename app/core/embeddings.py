"""Embedding providers with a deterministic local implementation for tests."""

import hashlib
import inspect
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from app.core.errors import BackendUnavailableError

if TYPE_CHECKING:
    from app.config import Settings


class EmbeddingProvider(Protocol):
    def embed(self, text: str): ...


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

    async def embed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
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


async def embed_text(provider: EmbeddingProvider, text: str) -> list[float]:
    result = provider.embed(text)
    if inspect.isawaitable(result):
        result = await result
    return list(result)


def create_embedding(settings: "Settings") -> EmbeddingProvider:
    if settings.embedding_provider == "hash":
        return HashEmbedding(settings.embedding_dimensions)
    if settings.embedding_provider == "openai-compatible":
        if not settings.embedding_api_key:
            raise ValueError("EMBEDDING_API_KEY is required for openai-compatible embeddings")
        return OpenAICompatibleEmbedding(
            settings.embedding_base_url,
            settings.embedding_api_key,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
    raise ValueError(f"unsupported embedding provider: {settings.embedding_provider}")

