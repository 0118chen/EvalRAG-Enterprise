"""Indexing contracts shared by ingestion workers and retrieval backends."""

from dataclasses import dataclass
from typing import Protocol

from app.core.embeddings import HashEmbedding
from app.core.ingestion import Chunk


class ChunkIndexer(Protocol):
    async def upsert(self, chunks: list[Chunk]) -> int: ...


@dataclass
class LocalIndex:
    """Deterministic local index used until external services are configured."""

    embedding: HashEmbedding
    vectors: dict[str, list[float]]

    async def upsert(self, chunks: list[Chunk]) -> int:
        for chunk in chunks:
            self.vectors[chunk.id] = self.embedding.embed(chunk.text)
        return len(chunks)

