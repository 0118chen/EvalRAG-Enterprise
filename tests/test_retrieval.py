from app.core.ingestion import Chunk
from app.core.retrieval import bm25_scores, retrieve


def test_hybrid_retrieval_ranks_matching_chunk() -> None:
    chunks = [Chunk("1", "doc-a", 1, "贷款政策生效日期为一月一日"), Chunk("2", "doc-b", 2, "其他说明")]
    results = retrieve("贷款政策生效日期", chunks, 2, "hybrid")
    assert results[0][0].id == "1"


def test_chinese_retrieval_matches_farmer_loan_terms() -> None:
    chunks = [
        Chunk("farmer", "farmer-doc", 1, "本办法所称农户贷款，是指贷款人向农户发放的贷款。"),
        Chunk("bank", "bank-doc", 1, "商业银行依法开展贷款业务并保护存款人利益。"),
    ]
    results = retrieve("农户贷款的定义是什么", chunks, 2, "hybrid")
    assert results[0][0].id == "farmer"
    assert all(score > 0 for _, score in results)


def test_bm25_uses_corpus_idf() -> None:
    documents = ["common rare", "common ordinary", "common routine"]

    rare_score = bm25_scores("rare", documents)[0]
    common_score = bm25_scores("common", documents)[0]

    assert rare_score > common_score


def test_bm25_normalizes_document_length() -> None:
    chunks = [
        Chunk("short", "doc", 1, "policy effective"),
        Chunk("long", "doc", 2, "policy effective " + "background " * 30),
    ]

    results = retrieve("policy effective", chunks, 2, "sparse")

    assert [chunk.id for chunk, _ in results] == ["short", "long"]


def test_dense_retrieval_uses_embedding_cosine_not_token_overlap() -> None:
    class FixedEmbedding:
        def embed(self, text: str) -> list[float]:
            return {
                "alpha": [1.0, 0.0],
                "different words": [1.0, 0.0],
                "alpha unrelated": [0.0, 1.0],
            }[text]

    chunks = [
        Chunk("semantic", "doc", 1, "different words"),
        Chunk("lexical", "doc", 2, "alpha unrelated"),
    ]

    results = retrieve("alpha", chunks, 2, "dense", embedding=FixedEmbedding())

    assert [chunk.id for chunk, _ in results] == ["semantic"]
