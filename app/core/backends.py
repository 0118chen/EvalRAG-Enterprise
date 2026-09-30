"""Optional production retrieval backends with a deterministic local fallback."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from elasticsearch.exceptions import ConnectionError as ElasticsearchConnectionError
from elasticsearch.exceptions import ConnectionTimeout
from pymilvus.exceptions import (
    ConnectError,
    ConnectionNotExistException,
    MilvusException,
    MilvusUnavailableException,
)

from app.config import Settings
from app.core.embeddings import (
    EmbeddingProvider,
    create_embedding,
    embed_many_texts,
    embed_text,
)
from app.core.errors import BackendUnavailableError
from app.core.ingestion import Chunk
from app.core.retrieval import Fusion, cosine_similarity, fuse_rankings, retrieve

logger = logging.getLogger(__name__)
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_document_version(version: str | None) -> None:
    if version is not None and not SAFE_VERSION.fullmatch(version):
        raise ValueError(
            "invalid document version: use 1-64 letters, numbers, dots, underscores, or hyphens"
        )


class Retriever(Protocol):
    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]: ...


@dataclass
class HybridRetriever:
    """Compose two independent retrievers and fuse their ranked evidence."""

    dense: Retriever
    sparse: Retriever
    fusion: Fusion | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        dense_results, sparse_results = await asyncio.gather(self.dense.search(query, top_k), self.sparse.search(query, top_k))
        # `fusion=None` keeps the shipped equal-weight RRF; the spec carries k, weights
        # and the per-channel cap that the fusion experiments vary.
        return fuse_rankings(dense_results, sparse_results, self.fusion or Fusion(), top_k)


@dataclass
class ResilientRetriever:
    """Use a production retriever when healthy and fall back without failing requests."""

    primary: Retriever
    fallback: Retriever

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        try:
            return await self.primary.search(query, top_k)
        except BackendUnavailableError as exc:
            logger.warning("retrieval backend unavailable; using local fallback: %s", exc)
            return await self.fallback.search(query, top_k)


@dataclass
class LocalRetriever:
    chunks: list[Chunk]
    mode: str = "hybrid"
    embedding: EmbeddingProvider | None = None
    fusion: Fusion | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        if self.mode == "sparse":
            # BM25 needs term statistics, not vectors: this path must not touch the embedding
            # provider at all (it used to, for every chunk in the corpus).
            return retrieve(query, self.chunks, top_k, "sparse")
        embedding = self.embedding
        if embedding is None:
            return retrieve(query, self.chunks, top_k, self.mode, fusion=self.fusion)
        # Query and corpus in one batch: the provider then decides how many requests that is
        # (one for a local model, ceil(n / batch_size) for an HTTP API), and cached vectors
        # never leave the process.
        vectors = await embed_many_texts(embedding, [query, *[chunk.text for chunk in self.chunks]])
        query_vector = vectors[0]
        dense = []
        for chunk, chunk_vector in zip(self.chunks, vectors[1:], strict=True):
            score = cosine_similarity(query_vector, chunk_vector)
            if score > 0:
                dense.append((chunk, score))
        dense.sort(key=lambda item: item[1], reverse=True)
        if self.mode == "dense":
            return dense[:top_k]
        sparse = retrieve(query, self.chunks, top_k, "sparse")
        return fuse_rankings(dense, sparse, self.fusion or Fusion(), top_k)


@dataclass
class MilvusDenseRetriever:
    """Embed the query and search Milvus with tenant corpus filters."""

    collection_name: str
    embedding: EmbeddingProvider
    knowledge_base_id: str
    uri: str = "http://localhost:19530"
    token: str | None = None
    version: str | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        if not query.strip():
            return []
        return await self.search_vector(await embed_text(self.embedding, query), top_k)

    async def search_vector(self, query_vector: list[float], top_k: int) -> list[tuple[Chunk, float]]:
        validate_document_version(self.version)
        expected_dimension = getattr(self.embedding, "dimensions", None)
        if expected_dimension is not None and len(query_vector) != expected_dimension:
            raise ValueError(
                "Milvus query vector dimension mismatch: "
                f"expected {expected_dimension}, got {len(query_vector)}"
            )
        filters = [f'knowledge_base_id == "{self.knowledge_base_id}"']
        if self.version:
            filters.append(f'version == "{self.version}"')
        expression = " and ".join(filters)

        def _search() -> list[dict]:
            from pymilvus import MilvusClient

            # Milvus exposes a synchronous SDK; keep it off the event loop.
            return MilvusClient(uri=self.uri, token=self.token).search(
                collection_name=self.collection_name,
                data=[query_vector],
                limit=top_k,
                filter=expression,
                output_fields=[
                    "document_id",
                    "page",
                    "text",
                    "version",
                    "knowledge_base_id",
                ],
            )[0]

        try:
            rows = await asyncio.to_thread(_search)
        except (
            ConnectError,
            ConnectionNotExistException,
            MilvusUnavailableException,
            ConnectionError,
            TimeoutError,
        ) as exc:
            raise BackendUnavailableError(f"Milvus unavailable: {exc}") from exc
        except MilvusException as exc:
            if getattr(exc, "code", None) == 2 and "connect" in str(exc).lower():
                raise BackendUnavailableError(f"Milvus unavailable: {exc}") from exc
            raise
        return [
            (
                Chunk(
                    str(row["id"]),
                    row["entity"]["document_id"],
                    int(row["entity"]["page"]),
                    row["entity"]["text"],
                    row["entity"].get("version", "latest"),
                ),
                float(row["distance"]),
            )
            for row in rows
        ]

    def health(self) -> bool:
        from pymilvus import MilvusClient
        MilvusClient(uri=self.uri, token=self.token).list_collections()
        return True


@dataclass
class ElasticsearchBM25Retriever:
    """Adapter boundary for Elasticsearch; index client wiring is injected later."""

    index_name: str
    knowledge_base_id: str
    url: str = "http://localhost:9200"
    api_key: str | None = None
    version: str | None = None

    async def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]:
        from elasticsearch import AsyncElasticsearch

        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            filters: list[dict] = [
                {"term": {"knowledge_base_id": self.knowledge_base_id}}
            ]
            if self.version:
                filters.append({"term": {"version": self.version}})
            try:
                response = await client.search(
                    index=self.index_name,
                    query={
                        "bool": {
                            "must": [{"match": {"text": query}}],
                            "filter": filters,
                        }
                    },
                    size=top_k,
                )
            except (ElasticsearchConnectionError, ConnectionTimeout, TimeoutError) as exc:
                raise BackendUnavailableError(f"Elasticsearch unavailable: {exc}") from exc
            return [
                (
                    Chunk(
                        str(hit["_id"]),
                        hit["_source"]["document_id"],
                        int(hit["_source"]["page"]),
                        hit["_source"]["text"],
                        hit["_source"].get("version", "latest"),
                    ),
                    float(hit["_score"] or 0),
                )
                for hit in response["hits"]["hits"]
            ]
        finally:
            await client.close()

    async def health(self) -> bool:
        from elasticsearch import AsyncElasticsearch
        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            return bool(await client.ping())
        finally:
            await client.close()


def create_retriever(
    settings: Settings,
    chunks: list[Chunk],
    mode: str,
    knowledge_base_id: str,
    document_version: str | None,
    fusion: Fusion | None = None,
) -> Retriever:
    embedding = create_embedding(settings)
    local_dense = LocalRetriever(chunks, "dense", embedding)
    local_sparse = LocalRetriever(chunks, "sparse", embedding)

    dense: Retriever = local_dense
    if settings.dense_retrieval_backend == "milvus":
        dense = MilvusDenseRetriever(
            settings.milvus_collection,
            embedding,
            knowledge_base_id,
            settings.milvus_uri,
            settings.milvus_token,
            document_version,
        )
        if settings.external_retrieval_fallback:
            dense = ResilientRetriever(dense, local_dense)
    elif settings.dense_retrieval_backend != "local":
        raise ValueError(
            f"unsupported dense retrieval backend: {settings.dense_retrieval_backend}"
        )

    sparse: Retriever = local_sparse
    if settings.sparse_retrieval_backend == "elasticsearch":
        sparse = ElasticsearchBM25Retriever(
            settings.elasticsearch_index,
            knowledge_base_id,
            settings.elasticsearch_url,
            settings.elasticsearch_api_key,
            document_version,
        )
        if settings.external_retrieval_fallback:
            sparse = ResilientRetriever(sparse, local_sparse)
    elif settings.sparse_retrieval_backend != "local":
        raise ValueError(
            f"unsupported sparse retrieval backend: {settings.sparse_retrieval_backend}"
        )

    if mode == "dense":
        return dense
    if mode == "sparse":
        return sparse
    return HybridRetriever(dense, sparse, fusion)
