"""检索管线的性能契约：别为用不到的向量付费，也别重复嵌入同一批 chunk。

每一条契约都对应一个实测过的浪费（386 chunk 语料、缓存关闭）：

| 契约 | 改前 | 改后 |
|---|---|---|
| `sparse` 不碰嵌入 | 387 次/查询（1 query + 386 chunk，BM25 根本不用） | 0 |
| 同一语料只嵌一次 | 每次查询都重嵌 386 个 chunk | 只有新文本才嵌 |
| 付费 API 批量 + 复用连接 | 每文本一个请求、每请求一个 httpx 客户端 | 一次请求发一批、命中缓存不发 |
"""

import asyncio
import json

import httpx
import pytest

import app.core.embeddings as embeddings_module
import app.core.retrieval as retrieval_module
from app.config import Settings
from app.core.backends import LocalRetriever
from app.core.cache import MemoryTTLCache
from app.core.embeddings import (
    CachingEmbedding,
    OpenAICompatibleEmbedding,
    embed_many_texts,
)
from app.core.ingestion import Chunk
from app.core.observability import TraceManager
from app.core.query_rewrite import RuleBasedQueryRewriter
from app.core.reranking import LexicalReranker
from app.core.retrieval import retrieve
from app.core.retrieval_service import RetrievalService


class CountingEmbedding:
    """记录每次 embed 的文本；向量内容对断言不重要。"""

    def __init__(self, dimensions: int = 8) -> None:
        self.dimensions = dimensions
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0] + [0.0] * (self.dimensions - 1)


@pytest.fixture(autouse=True)
def _clean_cache():
    """向量缓存是进程级的，测试之间必须隔离，否则命中会掩盖真实调用数。"""
    embeddings_module.reset_embedding_cache()
    yield
    embeddings_module.reset_embedding_cache()


def _chunks(count: int = 6) -> list[Chunk]:
    return [
        Chunk(f"c{index}", "doc", 1, f"第{index}条 农村土地承包经营期限为{index}年", "latest")
        for index in range(count)
    ]


def test_sparse_retrieval_never_embeds() -> None:
    """BM25 只用词频：sparse 模式下 embed 必须是 0 次调用。"""
    embedding = CountingEmbedding()
    retrieve("农村土地承包经营期限", _chunks(), 3, "sparse", embedding=embedding)
    assert embedding.calls == []


def test_repeated_dense_searches_embed_each_chunk_once() -> None:
    """同一份语料查两次，只有"新文本"才该走嵌入（第二次只剩新 query）。"""
    chunks = _chunks()
    inner = CountingEmbedding()
    retriever = LocalRetriever(chunks, "dense", CachingEmbedding(inner, "test:8"))

    asyncio.run(retriever.search("农村土地承包经营期限", 3))
    assert len(inner.calls) == len(chunks) + 1, "第一次：N 个 chunk + 1 个 query"

    asyncio.run(retriever.search("承包经营期限是多少年", 3))
    assert len(inner.calls) == len(chunks) + 2, "第二次只该多一个 query，不该重嵌语料"


def test_hybrid_search_embeds_the_corpus_once(monkeypatch) -> None:
    """hybrid 的两条通道共用同一批向量，不该把语料嵌两遍。

    改前 hybrid 是 774 次/查询（386 chunk × 2）：dense 通道嵌一遍，随后 sparse 侧
    又调用 `retrieve()`、用**它自己新建的**默认 embedding 把整个语料又嵌了一遍。
    正因为那个默认实例在外部看不见，这里把它换成同一个计数器，重复才可观测。
    """
    chunks = _chunks()
    inner = CountingEmbedding()
    monkeypatch.setattr(retrieval_module, "HashEmbedding", lambda *args, **kwargs: inner)
    retriever = LocalRetriever(chunks, "hybrid", CachingEmbedding(inner, "test:8"))

    asyncio.run(retriever.search("农村土地承包经营期限", 3))
    assert len(inner.calls) == len(chunks) + 1, "两条通道该共用同一批向量"


def test_hybrid_mixing_cache_and_non_cache_paths_still_embeds_once() -> None:
    """缓存按 (模型, 文本) 记账：换一个 provider 实例也必须命中同一份缓存。"""
    chunks = _chunks()
    first_inner = CountingEmbedding()
    asyncio.run(LocalRetriever(chunks, "dense", CachingEmbedding(first_inner, "test:8")).search("问题一", 3))
    before = len(first_inner.calls)

    second_inner = CountingEmbedding()
    asyncio.run(LocalRetriever(chunks, "dense", CachingEmbedding(second_inner, "test:8")).search("问题二", 3))
    assert len(second_inner.calls) == 1, "语料向量该从缓存来，只有新 query 需要嵌入"
    assert before == len(chunks) + 1


def test_embedding_cache_keeps_different_models_apart() -> None:
    """不同维度/模型的同一条文本不能互相命中。"""
    small = CachingEmbedding(CountingEmbedding(dimensions=8), "model-a:8")
    large = CachingEmbedding(CountingEmbedding(dimensions=16), "model-b:16")
    assert len(small.embed("同一条文本")) == 8
    assert len(large.embed("同一条文本")) == 16


def test_embedding_cache_is_bounded() -> None:
    """缓存要有上限，不能把整个语料库永久留在内存里。"""
    embeddings_module.reset_embedding_cache(limit=4)
    embedding = CachingEmbedding(CountingEmbedding(), "test:8")
    for index in range(10):
        embedding.embed(f"文本{index}")
    assert embeddings_module.embedding_cache_stats()["size"] == 4


def test_api_embedding_batches_misses_and_skips_cache_hits() -> None:
    """付费接口：一次请求发一批，命中缓存的文本不再发，且不改动返回顺序。"""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        assert isinstance(inputs, list), "批量接口必须一次发多条"
        requests.append({"count": len(inputs), "texts": inputs, "auth": request.headers.get("authorization")})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index)] * body["dimensions"]}
                    for index in range(len(inputs))
                ]
            },
        )

    # 缓存是 CachingEmbedding 这一层的职责（产品里由 create_embedding 统一包上）
    embedding = CachingEmbedding(
        OpenAICompatibleEmbedding(
            base_url="https://embed.example/v1",
            api_key="test-key",
            model="bge-m3",
            dimensions=4,
            transport=httpx.MockTransport(handler),
        ),
        "openai-compatible:bge-m3:4",
    )

    vectors = asyncio.run(embed_many_texts(embedding, ["甲", "乙", "丙"]))
    assert len(requests) == 1 and requests[0]["count"] == 3
    assert requests[0]["auth"] == "Bearer test-key"
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0], "返回顺序必须与输入一致"

    asyncio.run(embed_many_texts(embedding, ["甲", "乙", "丙"]))
    assert len(requests) == 1, "全命中缓存时不该再发请求"

    asyncio.run(embed_many_texts(embedding, ["甲", "丁"]))
    assert len(requests) == 2, "只该为未命中发一次请求"
    assert requests[1]["texts"] == ["丁"]


def test_api_embedding_splits_large_batches() -> None:
    """一次塞几百条会被服务端拒；要按 batch_size 切。"""
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        requests.append(len(inputs))
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [0.5] * 4} for i in range(len(inputs))]},
        )

    embedding = OpenAICompatibleEmbedding(
        base_url="https://embed.example/v1",
        api_key="k",
        model="m",
        dimensions=4,
        batch_size=2,
        transport=httpx.MockTransport(handler),
    )
    vectors = asyncio.run(embed_many_texts(embedding, ["一", "二", "三", "四", "五"]))
    assert requests == [2, 2, 1]
    assert len(vectors) == 5


def _service() -> RetrievalService:
    settings = Settings(query_rewrite_enabled=False, rerank_enabled=False)
    return RetrievalService(
        settings,
        TraceManager(settings),
        MemoryTTLCache(60),
        RuleBasedQueryRewriter(),
        LexicalReranker(),
    )


def test_build_retriever_is_reused_for_an_unchanged_corpus() -> None:
    """同一份语料反复查询不该反复建索引；语料一变就必须换新的。"""
    chunks = _chunks()
    service = _service()
    first = service._build_retriever(chunks, "hybrid", "kb", "latest")
    assert service._build_retriever(chunks, "hybrid", "kb", "latest") is first

    changed = [Chunk("c0", "doc", 1, "改过的正文", "latest"), *chunks[1:]]
    assert service._build_retriever(changed, "hybrid", "kb", "latest") is not first


def test_retriever_cache_does_not_confuse_modes_or_knowledge_bases() -> None:
    """缓存键要带上 mode / 版本 / 知识库，否则会串结果。"""
    chunks = _chunks()
    service = _service()
    hybrid = service._build_retriever(chunks, "hybrid", "kb", "latest")
    assert service._build_retriever(chunks, "sparse", "kb", "latest") is not hybrid
    assert service._build_retriever(chunks, "hybrid", "other-kb", "latest") is not hybrid
