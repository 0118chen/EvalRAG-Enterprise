"""Optional production retrieval backends with a deterministic local fallback."""

from dataclasses import dataclass
from typing import Protocol

from app.core.ingestion import Chunk
from app.core.retrieval import retrieve
from app.core.retrieval import reciprocal_rank_fusion


class Retriever(Protocol):
    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]: ...


@dataclass
class HybridRetriever:
    """Compose two independent retrievers and fuse their ranked evidence."""

    dense: Retriever
    sparse: Retriever
    fusion_k: int = 60

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        import asyncio
        dense_results, sparse_results = await asyncio.gather(self.dense.search(query, top_k), self.sparse.search(query, top_k))
        return reciprocal_rank_fusion(dense_results, sparse_results, k=self.fusion_k, top_k=top_k)


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
    uri: str = "http://localhost:19530"
    token: str | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        if not query.strip():
            return []
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError("install pymilvus to use the Milvus backend") from exc
        # Embedding is intentionally injected in the next layer; this adapter accepts a
        # precomputed query vector through `search_vector` for deterministic integration.
        raise RuntimeError("call search_vector(query_vector, top_k) after embedding the query")

    async def search_vector(self, query_vector: list[float], top_k: int) -> list[tuple[Chunk, float]]:
        from pymilvus import MilvusClient
        client = MilvusClient(uri=self.uri, token=self.token)
        rows = client.search(collection_name=self.collection_name, data=[query_vector], limit=top_k, output_fields=["document_id", "page", "text"])[0]
        return [(Chunk(str(row["id"]), row["entity"]["document_id"], int(row["entity"]["page"]), row["entity"]["text"]), float(row["distance"])) for row in rows]

    def health(self) -> bool:
        from pymilvus import MilvusClient
        MilvusClient(uri=self.uri, token=self.token).list_collections()
        return True


@dataclass
class ElasticsearchBM25Retriever:
    """Adapter boundary for Elasticsearch; index client wiring is injected later."""

    index_name: str
    url: str = "http://localhost:9200"
    api_key: str | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        from elasticsearch import AsyncElasticsearch
        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            response = await client.search(index=self.index_name, query={"match": {"text": query}}, size=top_k)
            return [(Chunk(str(hit["_id"]), hit["_source"]["document_id"], int(hit["_source"]["page"]), hit["_source"]["text"]), float(hit["_score"] or 0)) for hit in response["hits"]["hits"]]
        finally:
            await client.close()

    async def health(self) -> bool:
        from elasticsearch import AsyncElasticsearch
        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            return bool(await client.ping())
        finally:
            await client.close()
