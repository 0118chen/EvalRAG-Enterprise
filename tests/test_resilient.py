import asyncio

from app.core.backends import LocalRetriever, ResilientRetriever
from app.core.ingestion import Chunk


class BrokenRetriever:
    async def search(self, query: str, top_k: int):
        raise RuntimeError("backend unavailable")


def test_resilient_retriever_falls_back() -> None:
    fallback = LocalRetriever([Chunk("c", "d", 1, "policy")])
    result = asyncio.run(ResilientRetriever(BrokenRetriever(), fallback).search("policy", 1))
    assert result[0][0].id == "c"
