import asyncio
import logging

import pytest

from app.core.backends import BackendUnavailableError, LocalRetriever, ResilientRetriever
from app.core.ingestion import Chunk


class BrokenRetriever:
    async def search(self, query: str, top_k: int):
        raise BackendUnavailableError("backend unavailable")


def test_resilient_retriever_falls_back_and_warns(caplog) -> None:
    fallback = LocalRetriever([Chunk("c", "d", 1, "policy")])
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            ResilientRetriever(BrokenRetriever(), fallback).search("policy", 1)
        )
    assert result[0][0].id == "c"
    assert "backend unavailable" in caplog.text


def test_resilient_retriever_does_not_hide_configuration_errors() -> None:
    class InvalidRetriever:
        async def search(self, query: str, top_k: int):
            raise ValueError("schema mismatch")

    fallback = LocalRetriever([Chunk("c", "d", 1, "policy")])
    with pytest.raises(ValueError, match="schema mismatch"):
        asyncio.run(ResilientRetriever(InvalidRetriever(), fallback).search("policy", 1))
