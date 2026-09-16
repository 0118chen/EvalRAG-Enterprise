import asyncio

from app.core.embeddings import HashEmbedding
from app.core.indexing import LocalIndex
from app.core.ingestion import Chunk


def test_local_index_upserts_vectors() -> None:
    index = LocalIndex(HashEmbedding(4), {})
    count = asyncio.run(index.upsert([Chunk("c", "d", 1, "evidence")]))
    assert count == 1 and len(index.vectors["c"]) == 4

