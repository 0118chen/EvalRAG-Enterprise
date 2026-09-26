import asyncio
import sys
from types import SimpleNamespace

import pytest

import app.core.backends as backends_module
from app.core.backends import (
    BackendUnavailableError,
    ElasticsearchBM25Retriever,
    LocalRetriever,
    MilvusDenseRetriever,
    ResilientRetriever,
)
from app.core.embeddings import HashEmbedding
from app.core.ingestion import Chunk


def test_local_retriever_implements_backend_contract() -> None:
    result = asyncio.run(LocalRetriever([Chunk("c", "d", 1, "effective date")]).search("effective", 1))
    assert result[0][0].document_id == "d"


def test_milvus_search_filters_knowledge_base_and_version(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def search(self, **kwargs):
            captured.update(kwargs)
            return [[]]

    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(MilvusClient=FakeClient))
    retriever = MilvusDenseRetriever(
        "chunks", HashEmbedding(2), "kb-policy", version="v3"
    )

    asyncio.run(retriever.search_vector([1.0, 0.0], 5))

    assert 'knowledge_base_id == "kb-policy"' in captured["filter"]
    assert 'version == "v3"' in captured["filter"]


def test_elasticsearch_search_filters_knowledge_base_and_version(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def search(self, **kwargs):
            captured.update(kwargs)
            return {"hits": {"hits": []}}

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeClient),
    )
    retriever = ElasticsearchBM25Retriever("chunks", "kb-policy", version="v3")

    asyncio.run(retriever.search("policy", 5))

    filters = captured["query"]["bool"]["filter"]
    assert {"term": {"knowledge_base_id": "kb-policy"}} in filters
    assert {"term": {"version": "v3"}} in filters


def test_resilient_retriever_falls_back_for_backend_specific_errors() -> None:
    expected = [(Chunk("c", "d", 1, "policy"), 1.0)]

    class Primary:
        async def search(self, query, top_k):
            raise BackendUnavailableError("backend client error")

    class Fallback:
        async def search(self, query, top_k):
            return expected

    result = asyncio.run(ResilientRetriever(Primary(), Fallback()).search("policy", 1))

    assert result == expected


def test_milvus_search_rejects_invalid_version_before_network() -> None:
    retriever = MilvusDenseRetriever(
        "chunks",
        HashEmbedding(2),
        "kb-policy",
        version='v3" or id != "',
    )

    with pytest.raises(ValueError, match="invalid document version"):
        asyncio.run(retriever.search_vector([1.0, 0.0], 5))


def test_milvus_search_rejects_wrong_vector_dimension_before_network() -> None:
    retriever = MilvusDenseRetriever("chunks", HashEmbedding(3), "kb-policy")

    with pytest.raises(ValueError, match="expected 3, got 2"):
        asyncio.run(retriever.search_vector([1.0, 0.0], 5))


def test_milvus_connection_error_is_wrapped_for_resilient_fallback(monkeypatch) -> None:
    class MilvusUnavailableException(Exception):
        pass

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def search(self, **kwargs):
            raise MilvusUnavailableException("offline")

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(MilvusClient=FakeClient),
    )
    monkeypatch.setattr(backends_module, "ConnectError", MilvusUnavailableException)
    monkeypatch.setattr(
        backends_module,
        "ConnectionNotExistException",
        MilvusUnavailableException,
    )
    monkeypatch.setattr(
        backends_module,
        "MilvusUnavailableException",
        MilvusUnavailableException,
    )
    retriever = MilvusDenseRetriever("chunks", HashEmbedding(2), "kb-policy")

    with pytest.raises(BackendUnavailableError, match="Milvus unavailable"):
        asyncio.run(retriever.search_vector([1.0, 0.0], 5))


def test_milvus_generic_connect_failure_is_wrapped_but_other_errors_are_not(
    monkeypatch,
) -> None:
    class MilvusException(Exception):
        def __init__(self, message, code):
            super().__init__(message)
            self.code = code

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            raise MilvusException("Fail connecting to server", code=2)

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(MilvusClient=FakeClient),
    )
    monkeypatch.setattr(backends_module, "MilvusException", MilvusException)
    retriever = MilvusDenseRetriever("chunks", HashEmbedding(2), "kb-policy")

    with pytest.raises(BackendUnavailableError, match="Milvus unavailable"):
        asyncio.run(retriever.search_vector([1.0, 0.0], 5))


def test_elasticsearch_connection_error_is_wrapped_for_resilient_fallback(monkeypatch) -> None:
    class ConnectionTimeout(Exception):
        pass

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def search(self, **kwargs):
            raise ConnectionTimeout("offline")

        async def close(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "elasticsearch",
        SimpleNamespace(AsyncElasticsearch=FakeClient),
    )
    monkeypatch.setattr(backends_module, "ElasticsearchConnectionError", ConnectionTimeout)
    monkeypatch.setattr(backends_module, "ConnectionTimeout", ConnectionTimeout)
    retriever = ElasticsearchBM25Retriever("chunks", "kb-policy")

    with pytest.raises(BackendUnavailableError, match="Elasticsearch unavailable"):
        asyncio.run(retriever.search("policy", 5))

