"""Embedding providers with a deterministic local implementation for tests."""

import hashlib
import math
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HashEmbedding:
    dimensions: int = 32

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [((digest[i % len(digest)] / 255.0) * 2) - 1 for i in range(self.dimensions)]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


@dataclass(frozen=True)
class OpenAICompatibleEmbedding:
    base_url: str
    api_key: str
    model: str

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.base_url.rstrip('/')}/embeddings",
                                         json={"model": self.model, "input": text},
                                         headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            return list(response.json()["data"][0]["embedding"])

