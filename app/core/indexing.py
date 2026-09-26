"""Indexing contracts shared by ingestion workers and retrieval backends."""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from elasticsearch.exceptions import ConnectionError as ElasticsearchConnectionError
from elasticsearch.exceptions import ConnectionTimeout
from pymilvus.exceptions import ConnectError as MilvusConnectError
from pymilvus.exceptions import (
    ConnectionNotExistException,
    MilvusException,
    MilvusUnavailableException,
)

from app.core.embeddings import EmbeddingProvider, embed_text
from app.core.errors import BackendUnavailableError
from app.core.ingestion import Chunk


def _milvus_unavailable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (MilvusConnectError, ConnectionNotExistException, MilvusUnavailableException),
    ) or (
        isinstance(exc, MilvusException)
        and getattr(exc, "code", None) == 2
        and "connect" in str(exc).lower()
    )


class ChunkIndexer(Protocol):
    async def upsert(self, chunks: list[Chunk]) -> int: ...


@dataclass
class LocalIndex:
    """Deterministic local index used until external services are configured."""

    embedding: EmbeddingProvider
    vectors: dict[str, list[float]]

    async def upsert(self, chunks: list[Chunk]) -> int:
        for chunk in chunks:
            self.vectors[chunk.id] = await embed_text(self.embedding, chunk.text)
        return len(chunks)

    async def delete_document(self, document_id: str) -> int:
        keys = [key for key in self.vectors if key.startswith(f"{document_id}:")]
        for key in keys:
            self.vectors.pop(key, None)
        return len(keys)


@dataclass
class MilvusChunkIndexer:
    collection_name: str
    uri: str
    dimension: int
    token: str | None = None

    def ensure_collection(self) -> None:
        from pymilvus import DataType, MilvusClient

        try:
            client = MilvusClient(uri=self.uri, token=self.token)
            if client.has_collection(self.collection_name):
                self._validate_collection(client, DataType)
                return
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.VARCHAR, max_length=128, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
            schema.add_field("document_id", DataType.VARCHAR, max_length=128)
            schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=128)
            schema.add_field("page", DataType.INT64)
            schema.add_field("text", DataType.VARCHAR, max_length=65535)
            schema.add_field("version", DataType.VARCHAR, max_length=64)
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="AUTOINDEX",
                metric_type="COSINE",
            )
            client.create_collection(
                self.collection_name,
                schema=schema,
                index_params=index_params,
            )
        except Exception as exc:
            if _milvus_unavailable(exc):
                raise BackendUnavailableError(f"Milvus unavailable: {exc}") from exc
            raise

    def _validate_collection(self, client, data_type) -> None:
        description = client.describe_collection(self.collection_name)
        fields = {
            field.get("name"): field
            for field in description.get("fields", [])
            if field.get("name")
        }
        vector_field = fields.get("vector")
        if not vector_field:
            raise RuntimeError(
                f"Milvus collection {self.collection_name!r} is missing field 'vector'"
            )
        actual_dimension = int(vector_field.get("params", {}).get("dim", 0))
        if actual_dimension != self.dimension:
            raise RuntimeError(
                "Milvus vector dimension mismatch: "
                f"expected {self.dimension}, got {actual_dimension}"
            )

        expected_types = {
            "id": data_type.VARCHAR,
            "vector": data_type.FLOAT_VECTOR,
            "document_id": data_type.VARCHAR,
            "knowledge_base_id": data_type.VARCHAR,
            "page": data_type.INT64,
            "text": data_type.VARCHAR,
            "version": data_type.VARCHAR,
        }
        problems = []
        for name, expected_type in expected_types.items():
            field = fields.get(name)
            if not field:
                problems.append(f"missing field {name!r}")
                continue
            actual_type = field.get("type")
            if int(actual_type) != int(expected_type):
                problems.append(
                    f"field {name!r} has type {actual_type!r}, expected {expected_type!r}"
                )
        if fields.get("id") and not fields["id"].get("is_primary", False):
            problems.append("field 'id' must be the primary key")
        if description.get("auto_id", False):
            problems.append("auto_id must be disabled")
        if description.get("enable_dynamic_field", False):
            problems.append("dynamic fields must be disabled")
        if problems:
            raise RuntimeError(
                f"Milvus collection {self.collection_name!r} schema mismatch: "
                + "; ".join(problems)
            )

        indexes = client.list_indexes(self.collection_name)
        index_names = [
            item if isinstance(item, str) else item.get("index_name", "")
            for item in indexes
        ]
        index_descriptions = [
            client.describe_index(self.collection_name, index_name)
            for index_name in index_names
            if index_name
        ]
        vector_indexes = [
            item for item in index_descriptions if item.get("field_name") == "vector"
        ]
        if not vector_indexes:
            raise RuntimeError("Milvus collection is missing an index for field 'vector'")
        metric = str(vector_indexes[0].get("metric_type", "")).upper()
        if metric != "COSINE":
            raise RuntimeError(
                f"Milvus vector index metric mismatch: expected COSINE, got {metric or 'unknown'}"
            )

    async def upsert(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        knowledge_base_id: str = "",
    ) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ValueError(
                    "Milvus vector dimension mismatch: "
                    f"expected {self.dimension}, got {len(vector)}"
                )
        from pymilvus import MilvusClient

        # Milvus exposes a synchronous SDK; keep it off the event loop.
        await asyncio.to_thread(self.ensure_collection)
        rows = [
            {
                "id": chunk.id,
                "vector": vector,
                "document_id": chunk.document_id,
                "knowledge_base_id": knowledge_base_id,
                "page": chunk.page,
                "text": chunk.text,
                "version": chunk.version,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        try:
            await asyncio.to_thread(
                MilvusClient(uri=self.uri, token=self.token).upsert,
                self.collection_name,
                rows,
            )
        except Exception as exc:
            if _milvus_unavailable(exc):
                raise BackendUnavailableError(f"Milvus unavailable: {exc}") from exc
            raise
        return len(rows)

    async def delete_document(self, document_id: str) -> int:
        from pymilvus import MilvusClient

        try:
            result = await asyncio.to_thread(
                MilvusClient(uri=self.uri, token=self.token).delete,
                collection_name=self.collection_name,
                filter=f'document_id == "{document_id}"',
            )
        except Exception as exc:
            if _milvus_unavailable(exc):
                raise BackendUnavailableError(f"Milvus unavailable: {exc}") from exc
            raise
        return int(result.get("delete_count", 0))


@dataclass
class ElasticsearchChunkIndexer:
    index_name: str
    url: str
    api_key: str | None = None

    async def ensure_index(self, client) -> None:
        if await client.indices.exists(index=self.index_name):
            return
        await client.indices.create(
            index=self.index_name,
            mappings={
                "dynamic": "strict",
                "properties": {
                    "document_id": {"type": "keyword"},
                    "knowledge_base_id": {"type": "keyword"},
                    "version": {"type": "keyword"},
                    "page": {"type": "integer"},
                    "text": {"type": "text", "analyzer": "standard"},
                },
            },
        )

    async def upsert(
        self,
        chunks: list[Chunk],
        knowledge_base_id: str = "",
    ) -> int:
        from elasticsearch import AsyncElasticsearch
        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            try:
                await self.ensure_index(client)
                operations = []
                for chunk in chunks:
                    operations.extend(
                        (
                            {"index": {"_index": self.index_name, "_id": chunk.id}},
                            {
                                "document_id": chunk.document_id,
                                "knowledge_base_id": knowledge_base_id,
                                "page": chunk.page,
                                "text": chunk.text,
                                "version": chunk.version,
                            },
                        )
                    )
                if operations:
                    response = await client.bulk(operations=operations, refresh="wait_for")
                    if response.get("errors"):
                        failures = []
                        for item in response.get("items", []):
                            result = next(iter(item.values()), {})
                            error = result.get("error")
                            if error:
                                reason = (
                                    error.get("reason", str(error))
                                    if isinstance(error, dict)
                                    else str(error)
                                )
                                failures.append(f"{result.get('_id', 'unknown')}: {reason}")
                        raise RuntimeError(
                            "Elasticsearch bulk indexing partially failed: "
                            + ("; ".join(failures) or "unknown item failure")
                        )
                return len(chunks)
            except (
                ElasticsearchConnectionError,
                ConnectionTimeout,
                TimeoutError,
            ) as exc:
                raise BackendUnavailableError(
                    f"Elasticsearch unavailable: {exc}"
                ) from exc
        finally:
            await client.close()

    async def delete_document(self, document_id: str) -> int:
        from elasticsearch import AsyncElasticsearch

        client = AsyncElasticsearch(self.url, api_key=self.api_key)
        try:
            try:
                if not await client.indices.exists(index=self.index_name):
                    return 0
                response = await client.delete_by_query(
                    index=self.index_name,
                    query={"term": {"document_id": document_id}},
                    refresh=True,
                    conflicts="proceed",
                )
            except (
                ElasticsearchConnectionError,
                ConnectionTimeout,
                TimeoutError,
            ) as exc:
                raise BackendUnavailableError(
                    f"Elasticsearch unavailable: {exc}"
                ) from exc
            failures = response.get("failures", [])
            if failures:
                raise RuntimeError(
                    f"Elasticsearch document deletion partially failed: {failures}"
                )
            return int(response.get("deleted", 0))
        finally:
            await client.close()
