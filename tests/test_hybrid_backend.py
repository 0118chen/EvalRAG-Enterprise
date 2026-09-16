import asyncio

from app.core.backends import HybridRetriever, LocalRetriever
from app.core.ingestion import Chunk


def test_hybrid_backend_runs_two_retrievers() -> None:
    chunks = [Chunk("c", "d", 1, "effective policy date")]
    result = asyncio.run(HybridRetriever(LocalRetriever(chunks, "dense"), LocalRetriever(chunks, "sparse")).search("policy date", 1))
    assert result[0][0].id == "c"

