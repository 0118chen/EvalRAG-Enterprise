import asyncio
import sys
from types import SimpleNamespace

import pytest

import app.core.indexing as indexing_module
from app.core.errors import BackendUnavailableError
from app.core.indexing import ElasticsearchChunkIndexer, MilvusChunkIndexer
from app.core.ingestion import Chunk


def test_milvus_indexer_rejects_mismatched_vectors_before_network() -> None:
    indexer = MilvusChunkIndexer("test", "http://invalid", 2)
    try:
        asyncio.run(indexer.upsert([Chunk("c", "d", 1, "text")], []))
    except ValueError as exc:
        assert "same length" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_milvus_indexer_rejects_wrong_vector_dimension_before_network() -> None:
    indexer = MilvusChunkIndexer("test", "http://invalid", 3)

    with pytest.raises(ValueError, match="expected 3, got 2"):
        asyncio.run(indexer.upsert([Chunk("c", "d", 1, "text")], [[0.1, 0.2]]))


def test_milvus_existing_collection_validates_schema_and_metric(monkeypatch) -> None:
    class DataType:
        VARCHAR = 21
        FLOAT_VECTOR = 101
        INT64 = 5

    fields = [
        {"name": "id", "type": 21, "is_primary": True},
        {"name": "vector", "type": 101, "params": {"dim": 3}},
        {"name": "document_id", "type": 21},
        {"name": "knowledge_base_id", "type": 21},
        {"name": "page", "type": 5},
        {"name": "text", "type": 21},
        {"name": "version", "type": 21},
    ]

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def has_collection(self, name):
            return True

        def describe_collection(self, name):
            return {"auto_id": False, "enable_dynamic_field": False, "fields": fields}

        def list_indexes(self, name):
            return ["vector"]

        def describe_index(self, collection_name, index_name):
            return {"field_name": "vector", "metric_type": "COSINE"}

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(DataType=DataType, MilvusClient=FakeClient),
    )

    MilvusChunkIndexer("chunks", "http://milvus", 3).ensure_collection()


def test_milvus_existing_collection_rejects_dimension_mismatch(monkeypatch) -> None:
    class DataType:
        VARCHAR = 21
        FLOAT_VECTOR = 101
        INT64 = 5

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def has_collection(self, name):
            return True

        def describe_collection(self, name):
            return {
                "fields": [
                    {"name": "id", "type": 21, "is_primary": True},
                    {"name": "vector", "type": 101, "params": {"dim": 2}},
                ]
            }

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(DataType=DataType, MilvusClient=FakeClient),
    )

    with pytest.raises(RuntimeError, match="vector dimension.*expected 3, got 2"):
        MilvusChunkIndexer("chunks", "http://milvus", 3).ensure_collection()


def test_elasticsearch_indexer_creates_explicit_mapping_and_bulk_payload(monkeypatch) -> None:
    captured = {}

    class FakeIndices:
        async def exists(self, *, index):
            return False

        async def create(self, *, index, mappings):
            captured["mapping"] = mappings
            return {"acknowledged": True}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.indices = FakeIndices()

        async def bulk(self, **kwargs):
            captured["bulk"] = kwargs
            return {"errors": False, "items": [{"index": {"status": 201}}]}

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeClient),
    )
    chunk = Chunk("chunk-1", "doc-1", 7, "policy text", "v3")

    assert asyncio.run(
        ElasticsearchChunkIndexer("chunks", "http://es").upsert([chunk], "kb-1")
    ) == 1
    properties = captured["mapping"]["properties"]
    assert properties["knowledge_base_id"] == {"type": "keyword"}
    assert properties["version"] == {"type": "keyword"}
    assert properties["document_id"] == {"type": "keyword"}
    assert properties["text"] == {"type": "text", "analyzer": "standard"}
    assert properties["page"] == {"type": "integer"}
    assert captured["bulk"]["operations"][1]["knowledge_base_id"] == "kb-1"


def test_elasticsearch_bulk_partial_failure_is_not_silent(monkeypatch) -> None:
    class FakeIndices:
        async def exists(self, *, index):
            return True

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.indices = FakeIndices()

        async def bulk(self, **kwargs):
            return {
                "errors": True,
                "items": [
                    {"index": {"_id": "chunk-1", "status": 201}},
                    {
                        "index": {
                            "_id": "chunk-2",
                            "status": 400,
                            "error": {"reason": "mapping rejected"},
                        }
                    },
                ],
            }

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeClient),
    )
    chunks = [
        Chunk("chunk-1", "doc", 1, "one"),
        Chunk("chunk-2", "doc", 2, "two"),
    ]

    with pytest.raises(RuntimeError, match="chunk-2.*mapping rejected"):
        asyncio.run(ElasticsearchChunkIndexer("chunks", "http://es").upsert(chunks, "kb"))


def test_external_indexers_delete_document_by_exact_identifier(monkeypatch) -> None:
    captured = {}

    class FakeMilvusClient:
        def __init__(self, **kwargs) -> None:
            pass

        def delete(self, **kwargs):
            captured["milvus"] = kwargs
            return {"delete_count": 2}

    class FakeIndices:
        async def exists(self, *, index):
            return True

    class FakeElasticsearchClient:
        def __init__(self, *args, **kwargs) -> None:
            self.indices = FakeIndices()

        async def delete_by_query(self, **kwargs):
            captured["elasticsearch"] = kwargs
            return {"deleted": 2, "failures": []}

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(MilvusClient=FakeMilvusClient),
    )
    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeElasticsearchClient),
    )

    assert asyncio.run(
        MilvusChunkIndexer("chunks", "http://milvus", 3).delete_document("doc-1")
    ) == 2
    assert asyncio.run(
        ElasticsearchChunkIndexer("chunks", "http://es").delete_document("doc-1")
    ) == 2
    assert captured["milvus"]["filter"] == 'document_id == "doc-1"'
    assert captured["elasticsearch"]["query"] == {
        "term": {"document_id": "doc-1"}
    }


def test_indexers_wrap_only_connection_failures(monkeypatch) -> None:
    class DataType:
        VARCHAR = 21
        FLOAT_VECTOR = 101
        INT64 = 5

    class ConnectError(Exception):
        pass

    class FakeMilvusClient:
        def __init__(self, **kwargs) -> None:
            raise ConnectError("offline")

    class FakeIndices:
        async def exists(self, *, index):
            raise ConnectError("offline")

    class FakeElasticsearchClient:
        def __init__(self, *args, **kwargs) -> None:
            self.indices = FakeIndices()

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(MilvusClient=FakeMilvusClient, DataType=DataType),
    )
    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeElasticsearchClient),
    )
    monkeypatch.setattr(indexing_module, "MilvusConnectError", ConnectError)
    monkeypatch.setattr(indexing_module, "ElasticsearchConnectionError", ConnectError)

    with pytest.raises(BackendUnavailableError, match="Milvus unavailable"):
        asyncio.run(
            MilvusChunkIndexer("chunks", "http://milvus", 2).upsert(
                [Chunk("c", "d", 1, "text")],
                [[0.1, 0.2]],
            )
        )
    with pytest.raises(BackendUnavailableError, match="Elasticsearch unavailable"):
        asyncio.run(
            ElasticsearchChunkIndexer("chunks", "http://es").upsert(
                [Chunk("c", "d", 1, "text")],
                "kb",
            )
        )

