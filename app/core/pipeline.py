"""Transactional ingestion orchestration for chunking, embedding and indexing."""

from dataclasses import dataclass

from app.core.embeddings import HashEmbedding
from app.core.indexing import LocalIndex
from app.core.ingestion import Chunk


@dataclass
class IngestionPipeline:
    """Keep indexing policy in one place so API handlers stay thin."""

    local_index: LocalIndex

    async def index(self, chunks: list[Chunk]) -> int:
        vectors = [self.local_index.embedding.embed(chunk.text) for chunk in chunks]
        return await self.local_index.upsert(chunks)


def default_pipeline() -> IngestionPipeline:
    return IngestionPipeline(LocalIndex(HashEmbedding(), {}))

