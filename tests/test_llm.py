import asyncio
import json

import httpx
import pytest

import app.core.llm as llm_module
from app.core.llm import LLMStreamError, MockLLM, OpenAICompatibleLLM


async def _collect_stream(llm, question: str = "问题", context: str = "证据") -> list[str]:
    return [token async for token in llm.stream(question, context)]


def test_mock_llm_refuses_empty_context() -> None:
    assert "未找到" in asyncio.run(MockLLM().answer("问题", ""))


def test_mock_llm_returns_answer_with_context() -> None:
    assert "问题" in asyncio.run(MockLLM().answer("问题", "证据"))


def test_mock_llm_streams_its_answer() -> None:
    llm = MockLLM()

    tokens = asyncio.run(_collect_stream(llm))

    assert "".join(tokens) == asyncio.run(llm.answer("问题", "证据"))
    assert len(tokens) > 1


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_openai_compatible_llm_streams_sse_deltas_and_closes_response(monkeypatch) -> None:
    response_stream = _SSEStream(
        [
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n',
            b'data: {"choices":[{"delta":{}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" token"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        return httpx.Response(200, stream=response_stream)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    llm = OpenAICompatibleLLM("https://llm.example/v1", "secret", "model")

    tokens = asyncio.run(_collect_stream(llm))

    assert tokens == ["first", " token"]
    assert response_stream.closed is True


def test_openai_compatible_llm_stream_raises_http_errors_and_closes_response(
    monkeypatch,
) -> None:
    response_stream = _SSEStream([b'{"error":"unavailable"}'])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=response_stream)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    llm = OpenAICompatibleLLM("https://llm.example/v1", "secret", "model")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_collect_stream(llm))

    assert response_stream.closed is True


def _openai_llm_with_stream(monkeypatch, chunks: list[bytes]):
    response_stream = _SSEStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=response_stream)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        llm_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    return (
        OpenAICompatibleLLM("https://llm.example/v1", "secret", "model"),
        response_stream,
    )


def test_openai_stream_parser_handles_split_frames_multiple_events_crlf_and_keepalive(
    monkeypatch,
) -> None:
    llm, response_stream = _openai_llm_with_stream(
        monkeypatch,
        [
            b": keepalive\r\n\r\ndata: {\"choices\":[{\"delta\":{\"content\":\"fir",
            b"st\"}}]}\r\n\r\ndata: {\"choices\":[{\"delta\":{\"content\":\" second\"}}]}\n\n",
            b"data: [DONE]\r\n\r\n",
        ],
    )

    assert asyncio.run(_collect_stream(llm)) == ["first", " second"]
    assert response_stream.closed is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'data: {"error":{"message":"secret upstream detail"}}\n\n', "provider error"),
        (b"data: {not-json}\n\n", "malformed JSON"),
    ],
)
def test_openai_stream_parser_normalizes_provider_and_json_errors(
    monkeypatch,
    payload: bytes,
    message: str,
) -> None:
    llm, response_stream = _openai_llm_with_stream(monkeypatch, [payload])

    with pytest.raises(LLMStreamError, match=message):
        asyncio.run(_collect_stream(llm))

    assert response_stream.closed is True


def test_openai_stream_generator_aclose_closes_response_and_client(monkeypatch) -> None:
    llm, response_stream = _openai_llm_with_stream(
        monkeypatch,
        [
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"second"}}]}\n\n',
        ],
    )

    async def consume_one_and_close() -> None:
        stream = llm.stream("question", "context")
        assert await anext(stream) == "first"
        await stream.aclose()

    asyncio.run(consume_one_and_close())

    assert response_stream.closed is True
