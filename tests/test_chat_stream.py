import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

import app.api.routes.chat as chat_module
from app.config import Settings
from app.core.ingestion import Chunk
from app.core.llm import LLMStreamError
from app.core.observability import TraceManager
from app.core.retrieval_service import RetrievalResult
from app.schemas import SearchRequest


class _ControlledStreamingLLM:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.answer_called = False
        self.finished = False

    async def answer(self, question: str, context: str) -> str:
        self.answer_called = True
        raise AssertionError("streaming route must not call answer()")

    async def stream(self, question: str, context: str):
        yield "first"
        await self.release.wait()
        yield "second"
        self.finished = True


async def _exercise_stream(monkeypatch):
    records = []
    settings = Settings(langsmith_enabled=False)
    traces = TraceManager(settings, records.append)
    container = SimpleNamespace(traces=traces, settings=settings)
    retrieval = RetrievalResult(
        results=[(Chunk("chunk-1", "doc-1", 1, "evidence"), 0.9)],
        trace_id="retrieval-trace",
        cache_hit=False,
        rewritten_queries=["question"],
        candidate_count=1,
        reranked=False,
        document_version="latest",
    )

    async def fake_retrieve(payload, request):
        return container, SimpleNamespace(id="kb-1"), retrieval

    llm = _ControlledStreamingLLM()
    monkeypatch.setattr(chat_module, "get_container", lambda request: container)
    monkeypatch.setattr(chat_module, "get_llm", lambda request: llm)
    monkeypatch.setattr(chat_module, "_retrieve", fake_retrieve)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "state": {"request_id": "request-1"},
        }
    )
    payload = SearchRequest(
        tenant_id="tenant-1",
        knowledge_base_id="kb-1",
        question="question",
    )

    response = await chat_module.chat_stream(payload, request)
    iterator = response.body_iterator.__aiter__()
    observed = []
    while not any('data: "first"' in chunk for chunk in observed):
        observed.append(await anext(iterator))

    assert llm.answer_called is False
    assert llm.finished is False
    assert not any(record.name == "generation.answer" for record in records)
    assert "event: retrieval\n" in observed
    assert "event: citations\n" in observed

    llm.release.set()
    remainder = [chunk async for chunk in iterator]
    return observed, remainder, records


def test_chat_stream_forwards_first_native_token_before_generation_finishes(monkeypatch) -> None:
    observed, remainder, records = asyncio.run(_exercise_stream(monkeypatch))

    assert 'data: "first"\n\n' in observed
    assert 'data: "second"\n\n' in remainder
    assert "event: trace\n" in remainder
    assert "data: [DONE]\n\n" in remainder
    generation = next(record for record in records if record.name == "generation.answer")
    assert generation.outputs == {"answer_length": 11, "evidence_count": 1}


class _FailingStreamingLLM:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def answer(self, question: str, context: str) -> str:
        raise AssertionError("streaming route must not call answer()")

    async def stream(self, question: str, context: str):
        yield "partial"
        raise self.error


class _MisCitingStreamingLLM:
    """Streams an answer that cites a document the retrieval never returned."""

    async def answer(self, question: str, context: str) -> str:
        raise AssertionError("streaming route must not call answer()")

    async def stream(self, question: str, context: str):
        yield "依据 [doc-9 p.3] 的规定，"
        yield "应当如此办理。"


class _CloseAwareStreamingLLM:
    def __init__(self) -> None:
        self.closed = False

    async def answer(self, question: str, context: str) -> str:
        raise AssertionError("streaming route must not call answer()")

    async def stream(self, question: str, context: str):
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            self.closed = True


async def _make_response(monkeypatch, llm):
    records = []
    settings = Settings(langsmith_enabled=False)
    traces = TraceManager(settings, records.append)
    container = SimpleNamespace(traces=traces, settings=settings)
    retrieval_contexts: list[str | None] = []
    retrieval = RetrievalResult(
        results=[(Chunk("chunk-1", "doc-1", 1, "evidence"), 0.9)],
        trace_id="retrieval-trace",
        cache_hit=False,
        rewritten_queries=["question"],
        candidate_count=1,
        reranked=False,
        document_version="latest",
    )

    async def fake_retrieve(payload, request):
        retrieval_contexts.append(traces.current_trace_id)
        return container, SimpleNamespace(id="kb-1"), retrieval

    monkeypatch.setattr(chat_module, "get_container", lambda request: container)
    monkeypatch.setattr(chat_module, "get_llm", lambda request: llm)
    monkeypatch.setattr(chat_module, "_retrieve", fake_retrieve)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "state": {"request_id": "request-1"},
        }
    )
    payload = SearchRequest(
        tenant_id="tenant-1",
        knowledge_base_id="kb-1",
        question="question",
    )
    response = await chat_module.chat_stream(payload, request)
    return response, records, retrieval_contexts


def test_chat_stream_trace_covers_retrieval_and_generation_as_child(monkeypatch) -> None:
    async def exercise():
        llm = _ControlledStreamingLLM()
        response, records, retrieval_contexts = await _make_response(monkeypatch, llm)
        iterator = response.body_iterator.__aiter__()
        observed = []
        while not any('data: "first"' in chunk for chunk in observed):
            observed.append(await anext(iterator))
        assert records == []
        llm.release.set()
        remainder = [chunk async for chunk in iterator]
        return observed + remainder, records, retrieval_contexts

    chunks, records, retrieval_contexts = asyncio.run(exercise())
    stream = "".join(chunks)
    markers = [
        "event: retrieval\n",
        "event: citations\n",
        'data: "first"\n\n',
        'data: "second"\n\n',
        "event: trace\n",
        "data: [DONE]\n\n",
    ]
    assert [stream.index(marker) for marker in markers] == sorted(
        stream.index(marker) for marker in markers
    )
    generation = next(record for record in records if record.name == "generation.answer")
    rag = next(record for record in records if record.name == "rag.request")
    assert records.index(generation) < records.index(rag)
    assert generation.parent_id == rag.span_id
    assert generation.trace_id == rag.trace_id == retrieval_contexts[0]
    assert rag.outputs == {"citation_count": 1, "answer_length": 11}


@pytest.mark.parametrize(
    "provider_error",
    [
        httpx.HTTPStatusError(
            "secret HTTP detail",
            request=httpx.Request("POST", "https://llm.example"),
            response=httpx.Response(503),
        ),
        LLMStreamError("malformed JSON with secret-key"),
        httpx.ReadError("secret socket detail"),
    ],
)
def test_chat_stream_sends_safe_terminal_error_and_marks_spans_failed(
    monkeypatch,
    provider_error: Exception,
) -> None:
    async def exercise():
        response, records, _contexts = await _make_response(
            monkeypatch,
            _FailingStreamingLLM(provider_error),
        )
        return [chunk async for chunk in response.body_iterator], records

    chunks, records = asyncio.run(exercise())
    stream = "".join(chunks)
    error_payload = json.dumps(
        {"code": "generation_failed", "message": "模型生成失败，请稍后重试。"},
        ensure_ascii=False,
    )
    assert 'data: "partial"\n\n' in stream
    assert f"event: error\ndata: {error_payload}\n\n" in stream
    assert stream.endswith("data: [DONE]\n\n")
    assert "event: trace\n" not in stream
    assert "secret-key" not in stream
    generation = next(record for record in records if record.name == "generation.answer")
    rag = next(record for record in records if record.name == "rag.request")
    assert generation.error == "generation failed"
    assert rag.error == "generation failed"
    assert generation.outputs == {"answer_length": 7, "evidence_count": 1}
    assert generation.ended_at <= rag.ended_at


def test_chat_stream_aclose_closes_upstream_without_trace_or_done(monkeypatch) -> None:
    async def exercise():
        llm = _CloseAwareStreamingLLM()
        response, records, _contexts = await _make_response(monkeypatch, llm)
        iterator = response.body_iterator.__aiter__()
        observed = []
        while not any('data: "first"' in chunk for chunk in observed):
            observed.append(await anext(iterator))
        await iterator.aclose()
        return llm, observed, records

    llm, observed, records = asyncio.run(exercise())
    stream = "".join(observed)
    assert llm.closed is True
    assert "event: trace\n" not in stream
    assert "data: [DONE]\n\n" not in stream
    generation = next(record for record in records if record.name == "generation.answer")
    rag = next(record for record in records if record.name == "rag.request")
    assert generation.error == "stream cancelled"
    assert rag.error == "stream cancelled"


def test_chat_stream_logs_the_provider_failure_for_operators(monkeypatch, caplog) -> None:
    async def exercise():
        response, _records, _contexts = await _make_response(
            monkeypatch,
            _FailingStreamingLLM(httpx.ReadError("secret socket detail")),
        )
        return [chunk async for chunk in response.body_iterator]

    with caplog.at_level(logging.WARNING):
        chunks = asyncio.run(exercise())

    stream = "".join(chunks)
    # The client keeps getting a generic message ...
    assert "event: error" in stream
    assert "secret socket detail" not in stream
    # ... but the server must leave a diagnostic trail for the operator.
    failures = [record for record in caplog.records if record.name == "app.api.routes.chat"]
    assert [record.getMessage() for record in failures], "no log record for the failure"
    assert any(record.exc_info for record in failures), "exception not attached to the log"


def test_chat_stream_reports_an_answer_that_cites_unretrieved_evidence(monkeypatch) -> None:
    """Tokens are already on the wire, so an unverifiable citation cannot be retracted.

    The stream therefore ends with a terminal error instead of a `trace` event, exactly as
    a failed generation does, and the trace records what could not be verified.
    """

    async def exercise():
        response, records, _contexts = await _make_response(
            monkeypatch, _MisCitingStreamingLLM()
        )
        return [chunk async for chunk in response.body_iterator], records

    chunks, records = asyncio.run(exercise())
    stream = "".join(chunks)

    assert "依据 [doc-9 p.3] 的规定，" in stream
    assert '"code": "ungrounded_citation"' in stream
    assert "event: error\n" in stream
    assert "event: trace\n" not in stream
    assert stream.endswith("data: [DONE]\n\n")

    rag = next(record for record in records if record.name == "rag.request")
    assert rag.error == "unverifiable citation"
    assert rag.metadata["citations"] == 1
    assert rag.metadata["unknown_citations"] == 1
    assert rag.metadata["ungrounded_citations"] == 0
    # The generation itself succeeded, so it is not marked failed.
    generation = next(record for record in records if record.name == "generation.answer")
    assert generation.error is None
