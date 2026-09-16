import asyncio

from app.core.backends import LocalRetriever
from app.core.ingestion import Chunk


def test_local_retriever_implements_backend_contract() -> None:
    result = asyncio.run(LocalRetriever([Chunk("c", "d", 1, "effective date")]).search("effective", 1))
    assert result[0][0].document_id == "d"

