"""Optional production retrieval backends with a deterministic local fallback."""

from dataclasses import dataclass
from typing import Protocol

from app.core.ingestion import Chunk
from app.core.retrieval import retrieve


class Retriever(Protocol):
    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]: ...


@dataclass
class LocalRetriever:
    chunks: list[Chunk]
    mode: str = "hybrid"

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        return retrieve(query, self.chunks, top_k, self.mode)


@dataclass
class MilvusDenseRetriever:
    """Adapter boundary for Milvus; embedding and collection wiring are injected later."""

    collection_name: str

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        raise RuntimeError(f"Milvus backend is not configured for collection {self.collection_name}")


@dataclass
class ElasticsearchBM25Retriever:
    """Adapter boundary for Elasticsearch; index client wiring is injected later."""

    index_name: str

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        raise RuntimeError(f"Elasticsearch backend is not configured for index {self.index_name}")

