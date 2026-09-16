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


@dataclass
class MilvusChunkIndexer:
    collection_name: str
    uri: str
    dimension: int
    token: str | None = None

    def ensure_collection(self) -> None:
        from pymilvus import DataType, MilvusClient
        client = MilvusClient(uri=self.uri, token=self.token)
        if client.has_collection(self.collection_name):
            return
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=128, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field("document_id", DataType.VARCHAR, max_length=128)
        schema.add_field("page", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        index_params = client.prepare_index_params()
        index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        client.create_collection(self.collection_name, schema=schema, index_params=index_params)

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        from pymilvus import MilvusClient
        self.ensure_collection()
        rows = [{"id": c.id, "vector": v, "document_id": c.document_id, "page": c.page, "text": c.text} for c, v in zip(chunks, vectors)]
        MilvusClient(uri=self.uri, token=self.token).upsert(self.collection_name, rows)
        return len(rows)


@dataclass
class ElasticsearchChunkIndexer:
    index_name: str
    url: str
    api_key: str | None = None

    async def upsert(self, chunks: list[Chunk]) -> int:
        from elasticsearch import AsyncElasticsearch
        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            operations = []
            for chunk in chunks:
                operations.extend(({"index": {"_index": self.index_name, "_id": chunk.id}},
                                   {"document_id": chunk.document_id, "page": chunk.page, "text": chunk.text}))
            if operations:
                await client.bulk(operations=operations, refresh="wait_for")
            return len(chunks)
        finally:
            await client.close()
