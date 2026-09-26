"""Transactional ingestion orchestration for chunking, embedding and indexing."""

from dataclasses import dataclass

from app.config import Settings
from app.core.embeddings import EmbeddingProvider, HashEmbedding, create_embedding, embed_text
from app.core.indexing import ElasticsearchChunkIndexer, LocalIndex, MilvusChunkIndexer
from app.core.ingestion import Chunk


@dataclass
class IngestionPipeline:
    """Keep indexing policy in one place so API handlers stay thin."""

    local_index: LocalIndex | None = None
    dense_indexer: MilvusChunkIndexer | None = None
    sparse_indexer: ElasticsearchChunkIndexer | None = None
    embedding: EmbeddingProvider | None = None

    async def index(
        self,
        chunks: list[Chunk],
        knowledge_base_id: str = "",
    ) -> int:
        if self.local_index is not None:
            await self.local_index.upsert(chunks)
        if self.dense_indexer is not None:
            if self.embedding is None:
                raise RuntimeError("dense indexing requires an embedding provider")
            vectors = [await embed_text(self.embedding, chunk.text) for chunk in chunks]
            await self.dense_indexer.upsert(chunks, vectors, knowledge_base_id)
        if self.sparse_indexer is not None:
            await self.sparse_indexer.upsert(chunks, knowledge_base_id)
        return len(chunks)

    async def delete_document(self, document_id: str) -> None:
        if self.local_index is not None:
            await self.local_index.delete_document(document_id)
        if self.dense_indexer is not None:
            await self.dense_indexer.delete_document(document_id)
        if self.sparse_indexer is not None:
            await self.sparse_indexer.delete_document(document_id)

    async def replace_document(
        self,
        chunks: list[Chunk],
        knowledge_base_id: str,
        document_id: str,
    ) -> int:
        await self.delete_document(document_id)
        return await self.index(chunks, knowledge_base_id)


def default_pipeline() -> IngestionPipeline:
    return IngestionPipeline(LocalIndex(HashEmbedding(), {}))


def create_ingestion_pipeline(settings: Settings) -> IngestionPipeline:
    embedding = create_embedding(settings)
    dense_indexer = None
    sparse_indexer = None
    local_index = None

    if settings.dense_retrieval_backend == "milvus":
        dense_indexer = MilvusChunkIndexer(
            settings.milvus_collection,
            settings.milvus_uri,
            settings.embedding_dimensions,
            settings.milvus_token,
        )
    elif settings.dense_retrieval_backend != "local":
        raise ValueError(
            f"unsupported dense retrieval backend: {settings.dense_retrieval_backend}"
        )

    if settings.sparse_retrieval_backend == "elasticsearch":
        sparse_indexer = ElasticsearchChunkIndexer(
            settings.elasticsearch_index,
            settings.elasticsearch_url,
            settings.elasticsearch_api_key,
        )
    elif settings.sparse_retrieval_backend != "local":
        raise ValueError(
            f"unsupported sparse retrieval backend: {settings.sparse_retrieval_backend}"
        )

    if dense_indexer is None and sparse_indexer is None:
        local_index = LocalIndex(embedding, {})
    return IngestionPipeline(local_index, dense_indexer, sparse_indexer, embedding)
