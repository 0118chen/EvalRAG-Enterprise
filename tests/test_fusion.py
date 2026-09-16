from app.core.ingestion import Chunk
from app.core.retrieval import reciprocal_rank_fusion


def test_rrf_deduplicates_and_promotes_cross_retriever_hits() -> None:
    shared = Chunk("shared", "d", 1, "policy")
    dense_only = Chunk("dense", "d1", 1, "semantic")
    sparse_only = Chunk("sparse", "d2", 1, "keyword")
    result = reciprocal_rank_fusion([(shared, 1.0), (dense_only, 0.5)], [(sparse_only, 1.0), (shared, 0.5)], top_k=3)
    assert result[0][0].id == "shared"
    assert len({chunk.id for chunk, _ in result}) == 3

