import asyncio

import app.core.retrieval_service as retrieval_service_module
from app.config import Settings
from app.core.backends import (
    ElasticsearchBM25Retriever,
    HybridRetriever,
    MilvusDenseRetriever,
)
from app.core.cache import MemoryTTLCache
from app.core.ingestion import Chunk
from app.core.observability import TraceManager
from app.core.query_rewrite import RuleBasedQueryRewriter
from app.core.reranking import LexicalReranker
from app.core.retrieval_service import RetrievalService


def _service() -> RetrievalService:
    settings = Settings(
        query_rewrite_enabled=True,
        rerank_enabled=True,
        retrieval_candidate_multiplier=2,
    )
    traces = TraceManager(settings)
    return RetrievalService(
        settings,
        traces,
        MemoryTTLCache(60),
        RuleBasedQueryRewriter(),
        LexicalReranker(),
    )


def test_retrieval_service_rewrites_reranks_and_caches() -> None:
    chunks = [
        Chunk("c1", "policy", 1, "贷款政策生效日期为2026年1月1日", "v2026"),
        Chunk("c2", "other", 1, "其他说明", "v2026"),
    ]
    service = _service()

    async def run():
        first = await service.search(
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="贷款政策生效日期",
            chunks=chunks,
            top_k=1,
            mode="hybrid",
            document_version="v2026",
        )
        second = await service.search(
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="贷款政策生效日期",
            chunks=chunks,
            top_k=1,
            mode="hybrid",
            document_version="v2026",
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.results[0][0].id == "c1"
    assert first.reranked
    assert len(first.rewritten_queries) == 2
    assert not first.cache_hit
    assert second.cache_hit


def test_retrieval_service_selects_configured_external_backends() -> None:
    settings = Settings(
        dense_retrieval_backend="milvus",
        sparse_retrieval_backend="elasticsearch",
        external_retrieval_fallback=False,
    )
    service = RetrievalService(
        settings,
        TraceManager(settings),
        MemoryTTLCache(60),
        RuleBasedQueryRewriter(),
        LexicalReranker(),
    )

    retriever = service._build_retriever([], "hybrid", "kb-policy", "v3")

    assert isinstance(retriever, HybridRetriever)
    assert isinstance(retriever.dense, MilvusDenseRetriever)
    assert isinstance(retriever.sparse, ElasticsearchBM25Retriever)
    assert retriever.dense.knowledge_base_id == "kb-policy"
    assert retriever.sparse.knowledge_base_id == "kb-policy"


def test_external_retrieval_gets_candidate_limit_without_local_chunks(monkeypatch) -> None:
    requested_limits = []

    class FakeRetriever:
        async def search(self, query, top_k):
            requested_limits.append(top_k)
            return []

    monkeypatch.setattr(
        retrieval_service_module,
        "create_retriever",
        lambda *args: FakeRetriever(),
    )
    settings = Settings(query_rewrite_enabled=False, rerank_enabled=False)
    service = RetrievalService(
        settings,
        TraceManager(settings),
        MemoryTTLCache(60),
        RuleBasedQueryRewriter(),
        LexicalReranker(),
    )

    asyncio.run(
        service.search(
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="policy",
            chunks=[],
            top_k=3,
            mode="dense",
        )
    )

    assert requested_limits == [12]


def test_retrieval_cache_key_fingerprints_embedding_and_external_indexes(monkeypatch) -> None:
    captured_keys = []

    class CapturingCache:
        async def get(self, key):
            captured_keys.append(key)

        async def set(self, key, value, ttl_seconds=None):
            pass

    class FakeRetriever:
        async def search(self, query, top_k):
            return []

    monkeypatch.setattr(
        retrieval_service_module,
        "create_retriever",
        lambda *args: FakeRetriever(),
    )
    settings = Settings(
        embedding_provider="openai-compatible",
        embedding_model="embedding-v2",
        embedding_api_key="secret",
        embedding_dimensions=1536,
        dense_retrieval_backend="milvus",
        sparse_retrieval_backend="elasticsearch",
        milvus_collection="chunks-v2",
        elasticsearch_index="chunks-bm25-v2",
        query_rewrite_enabled=False,
        rerank_enabled=False,
    )
    service = RetrievalService(
        settings,
        TraceManager(settings),
        CapturingCache(),
        RuleBasedQueryRewriter(),
        LexicalReranker(),
    )

    asyncio.run(
        service.search(
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="policy",
            chunks=[],
            top_k=3,
            mode="hybrid",
        )
    )

    key = captured_keys[0]
    for expected in (
        '"embedding_provider": "openai-compatible"',
        '"embedding_model": "embedding-v2"',
        '"embedding_dimensions": 1536',
        '"dense_backend": "milvus"',
        '"sparse_backend": "elasticsearch"',
        '"milvus_collection": "chunks-v2"',
        '"elasticsearch_index": "chunks-bm25-v2"',
    ):
        assert expected in key
