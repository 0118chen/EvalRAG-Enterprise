import asyncio

from app.core.indexing import MilvusChunkIndexer
from app.core.ingestion import Chunk


def test_milvus_indexer_rejects_mismatched_vectors_before_network() -> None:
    indexer = MilvusChunkIndexer("test", "http://invalid", 2)
    try:
        asyncio.run(indexer.upsert([Chunk("c", "d", 1, "text")], []))
    except ValueError as exc:
        assert "same length" in str(exc)
    else:
        raise AssertionError("expected validation error")

