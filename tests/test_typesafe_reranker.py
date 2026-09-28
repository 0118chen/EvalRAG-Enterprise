"""TypeSafe 重排器的契约测试：不需要真 key，用 MockTransport 钉住请求/响应形状。

真跑需要 `TYPESAFE_API_KEY`（https://console.typesafe.ai/keys）并把候选文本发到第三方，
所以这里只钉契约：请求体形状、鉴权头、一候选一请求、按 noul 排序、失败降级。
"""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.core.errors import BackendUnavailableError
from app.core.ingestion import Chunk
from app.core.reranking import (
    LexicalReranker,
    NoOpReranker,
    ResilientReranker,
    TypesafeReranker,
    create_reranker,
)


def chunk(name: str, text: str) -> Chunk:
    return Chunk(name, "doc", 1, text)


def noul_response(score: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1.13.0",
            "answers": {"answers_query": {"type": "noul", "noul": score}},
            "usage": {"input_tokens": 296, "output_tokens": 20},
        },
    )


def test_reranker_uses_the_documented_contract_and_sorts_by_noul() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "content_type": request.headers.get("content-type"),
                "body": payload,
            }
        )
        passage = payload["state"]["candidate_passage"]
        return noul_response(0.91 if "十年" in passage else 0.08)

    reranker = TypesafeReranker(api_key="test-key", transport=httpx.MockTransport(handler))
    candidates = [
        (chunk("a", "借款人应当按期归还贷款"), 0.9),
        (chunk("b", "个人消费贷款期限不得超过十年"), 0.1),
    ]
    ranked = asyncio.run(reranker.rank("个人贷款借多久有上限？", candidates, top_k=2))

    assert [c.id for c, _ in ranked] == ["b", "a"]
    assert ranked[0][1] == pytest.approx(0.91)
    assert len(seen) == 2, "cookbook 的做法是一个候选一次请求"
    for call in seen:
        assert call["url"].endswith("/v1/systemone")
        assert call["auth"] == "Bearer test-key"
        assert call["content_type"] == "application/json"
        assert call["body"]["model"] == "jev-latest"
        # 字段名与 rerank cookbook 一致，便于按官方示例调试
        assert set(call["body"]["state"]) == {"query_excerpt", "candidate_passage"}
        question = call["body"]["questions"]["answers_query"]
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"true", "false"}
        assert question["instructions"]


def test_reranker_ties_break_on_content_not_on_chunk_id() -> None:
    """两名候选拿到同一个 noul 时，顺序必须由内容决定。

    chunk id 是 f"{document_id}:{index}"，document_id 每次导入重新生成——用 id 破平会让同一
    份语料重跑时同分候选的先后改变。这里把 id 前后调换，内容顺序必须不变。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return noul_response(0.5)

    reranker = TypesafeReranker(api_key="k", transport=httpx.MockTransport(handler))

    def bodies(first_id: str, second_id: str) -> list[str]:
        ranked = asyncio.run(
            reranker.rank(
                "q",
                [(Chunk(first_id, "doc", 3, "借款人应当按期归还贷款"), 1.0),
                 (Chunk(second_id, "doc", 3, "贷款期限不得超过十年"), 1.0)],
                top_k=2,
            )
        )
        return [c.text for c, _ in ranked]

    low = "11111111-1111-1111-1111-111111111111:1"
    high = "99999999-9999-9999-9999-999999999999:2"
    forward = bodies(low, high)
    assert forward == bodies(high, low)
    # 同页同分时按正文排序："借款" 在 "贷款" 之前
    assert forward[0].startswith("借款人")


def test_reranker_empty_shortlist_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不该发出请求")

    reranker = TypesafeReranker(api_key="k", transport=httpx.MockTransport(handler))
    assert asyncio.run(reranker.rank("q", [], top_k=5)) == []


def test_connection_failure_becomes_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    # retry_attempts=1：连不上没有 retry-after 可等，重试只会让测试白等退避秒数
    reranker = TypesafeReranker(
        api_key="k", transport=httpx.MockTransport(handler), retry_attempts=1
    )
    with pytest.raises(BackendUnavailableError, match="typesafe reranker unavailable"):
        asyncio.run(reranker.rank("q", [(chunk("a", "x"), 1.0)], top_k=1))


def _attempts_until_giving_up(status: int) -> int:
    """同一状态码重试到次数用完，返回实际发出的请求数。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        # retry-after: 0 让退避不真的睡（真实响应通常是秒级）
        return httpx.Response(status, json={"error": "nope"}, headers={"retry-after": "0"})

    reranker = TypesafeReranker(
        api_key="k", transport=httpx.MockTransport(handler), retry_attempts=3
    )
    with pytest.raises(BackendUnavailableError, match="gave up after retries"):
        asyncio.run(reranker.rank("q", [(chunk("a", "x"), 1.0)], top_k=1))
    return len(attempts)


def test_rate_limit_and_server_error_retry_then_give_up() -> None:
    """429/5xx 是可重试的：先按 retry-after 退避，重试次数用完才降级。"""
    for status in (429, 500):
        assert _attempts_until_giving_up(status) == 3, "应该重试到次数用完，而不是第一次 429 就放弃"


def test_rate_limit_then_success_recovers_without_degrading() -> None:
    calls: list[str] = []
    usage: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("call")
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"}, headers={"retry-after": "0"})
        return noul_response(0.77)

    reranker = TypesafeReranker(
        api_key="k", transport=httpx.MockTransport(handler), usage_sink=usage
    )
    ranked = asyncio.run(reranker.rank("q", [(chunk("a", "x"), 1.0)], top_k=1))
    assert ranked[0][1] == pytest.approx(0.77)
    assert len(calls) == 2
    # usage_sink 要能拿到真实 token 用量，好算钱
    assert usage == [{"usage": {"input_tokens": 296, "output_tokens": 20}, "model": "jev-1.13.0"}]


def test_resilient_wrapper_falls_back_to_the_local_reranker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    resilient = ResilientReranker(
        TypesafeReranker(
            api_key="k", transport=httpx.MockTransport(handler), retry_attempts=1
        ),
        LexicalReranker(),
    )
    query = "个人消费贷款期限不得超过十年"
    candidates = [
        (chunk("a", "借款人应当按期归还贷款"), 0.5),
        (chunk("b", "个人消费贷款期限不得超过十年"), 0.4),
    ]
    expected = asyncio.run(LexicalReranker().rank(query, candidates, top_k=2))
    ranked = asyncio.run(resilient.rank(query, candidates, top_k=2))
    # 降级后的排序必须逐字等于本地重排器自己的排序（不是"看起来合理"）
    assert [(c.id, round(s, 9)) for c, s in ranked] == [
        (c.id, round(s, 9)) for c, s in expected
    ]


def test_create_reranker_requires_a_key_for_the_typesafe_backend() -> None:
    # 显式传空串：开发机的 .env 里可能已经配了真 key，测试不能因此改变结论
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        create_reranker(Settings(rerank_backend="typesafe", typesafe_api_key=""))
    assert isinstance(
        create_reranker(Settings(rerank_backend="typesafe", typesafe_api_key="x")),
        ResilientReranker,
    )
    assert isinstance(create_reranker(Settings()), LexicalReranker)
    assert isinstance(create_reranker(Settings(rerank_enabled=False)), NoOpReranker)
    with pytest.raises(ValueError, match="unsupported rerank backend"):
        create_reranker(Settings(rerank_backend="magic"))
