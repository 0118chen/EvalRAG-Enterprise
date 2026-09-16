from app.core.ingestion import Chunk
from app.core.retrieval import retrieve


def test_hybrid_retrieval_ranks_matching_chunk() -> None:
    chunks = [Chunk("1", "doc-a", 1, "贷款政策生效日期为一月一日"), Chunk("2", "doc-b", 2, "其他说明")]
    results = retrieve("贷款政策生效日期", chunks, 2, "hybrid")
    assert results[0][0].id == "1"
