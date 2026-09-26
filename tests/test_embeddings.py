import asyncio

import pytest

import app.core.embeddings as embeddings_module
from app.core.backends import BackendUnavailableError
from app.core.embeddings import HashEmbedding, OpenAICompatibleEmbedding


def test_hash_embedding_is_normalized_and_deterministic() -> None:
    provider = HashEmbedding(8)
    assert provider.embed("policy") == provider.embed("policy")
    assert len(provider.embed("policy")) == 8


def test_openai_embedding_sends_dimensions_and_validates_response(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(embeddings_module.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleEmbedding("https://embedding.test/v1", "secret", "model", 3)

    assert asyncio.run(provider.embed("policy")) == [0.1, 0.2, 0.3]
    assert captured["json"] == {"model": "model", "input": "policy", "dimensions": 3}


def test_openai_embedding_rejects_wrong_response_dimension(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(embeddings_module.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleEmbedding("https://embedding.test/v1", "secret", "model", 3)

    with pytest.raises(ValueError, match="expected 3, got 2"):
        asyncio.run(provider.embed("policy"))


def test_openai_embedding_wraps_connection_failures_for_fallback(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            raise embeddings_module.httpx.ConnectError("offline")

    monkeypatch.setattr(embeddings_module.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleEmbedding("https://embedding.test/v1", "secret", "model", 3)

    with pytest.raises(BackendUnavailableError, match="embedding service unavailable"):
        asyncio.run(provider.embed("policy"))

