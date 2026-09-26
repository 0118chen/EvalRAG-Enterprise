"""Async handlers must not run synchronous store or broker calls on the event loop."""

import asyncio
import threading

import httpx

import app.api.routes.documents as documents_route
from app.config import Settings
from app.main import create_app
from app.schemas import KnowledgeBase


def _spy(store, name: str, calls: list[int]) -> None:
    original = getattr(store, name)

    def wrapper(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)

    setattr(store, name, wrapper)


class _QueueStub:
    """Celery task stand-in that records which thread published the message."""

    def __init__(self, calls: list[int]) -> None:
        self.calls = calls

    def delay(self, document_id: str) -> None:
        self.calls.append(threading.get_ident())


def _app(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'async-routes.db').as_posix()}",
            llm_provider="mock",
            langsmith_enabled=False,
            langsmith_api_key=None,
        )
    )


def test_search_route_keeps_blocking_store_calls_off_the_event_loop(tmp_path) -> None:
    async def exercise():
        app = _app(tmp_path)
        store = app.state.container.store
        store.save_knowledge_base(
            KnowledgeBase(id="kb", tenant_id="tenant", name="policy", description="")
        )
        calls: list[int] = []
        _spy(store, "get_knowledge_base", calls)
        _spy(store, "get_chunks", calls)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/retrieval/search",
                json={
                    "tenant_id": "tenant",
                    "knowledge_base_id": "kb",
                    "question": "农户贷款应遵循哪些原则？",
                },
            )
        return response, calls

    response, calls = asyncio.run(exercise())

    assert response.status_code == 200
    assert len(calls) == 2
    # The handler coroutine runs on this thread's loop; the store calls must not.
    assert threading.get_ident() not in calls


def test_upload_route_keeps_store_and_broker_calls_off_the_event_loop(
    tmp_path, monkeypatch
) -> None:
    async def exercise():
        app = _app(tmp_path)
        store = app.state.container.store
        store.save_knowledge_base(
            KnowledgeBase(id="kb", tenant_id="tenant", name="policy", description="")
        )
        calls: list[int] = []
        _spy(store, "get_knowledge_base", calls)
        _spy(store, "save_document", calls)
        monkeypatch.setattr(documents_route, "process_document", _QueueStub(calls))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/documents",
                data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
                files={"file": ("policy.txt", b"policy text", "text/plain")},
            )
        return response, calls

    response, calls = asyncio.run(exercise())

    assert response.status_code == 201
    # lookup + save + queue publish
    assert len(calls) == 3
    assert threading.get_ident() not in calls


def test_delete_route_keeps_store_calls_off_the_event_loop(tmp_path, monkeypatch) -> None:
    async def exercise():
        app = _app(tmp_path)
        store = app.state.container.store
        store.save_knowledge_base(
            KnowledgeBase(id="kb", tenant_id="tenant", name="policy", description="")
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/documents",
                data={"tenant_id": "tenant", "knowledge_base_id": "kb"},
                files={"file": ("policy.txt", b"policy text", "text/plain")},
            )
            calls: list[int] = []
            document_id = created.json()["id"]
            _spy(store, "get_document", calls)
            _spy(store, "delete_document", calls)
            response = await client.delete(
                f"/api/v1/documents/{document_id}?tenant_id=tenant"
            )
        return response, calls

    monkeypatch.setattr(documents_route, "process_document", _QueueStub([]))

    response, calls = asyncio.run(exercise())

    assert response.status_code == 204
    assert len(calls) == 2
    assert threading.get_ident() not in calls
