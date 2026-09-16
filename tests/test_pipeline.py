import asyncio

from app.core.ingestion import Chunk
from app.core.pipeline import default_pipeline


def test_ingestion_pipeline_indexes_all_chunks() -> None:
    pipeline = default_pipeline()
    chunks = [Chunk("c1", "d1", 1, "policy"), Chunk("c2", "d1", 1, "effective date")]
    assert asyncio.run(pipeline.index(chunks)) == 2
    assert set(pipeline.local_index.vectors) == {"c1", "c2"}

