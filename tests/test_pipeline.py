import asyncio

from app.config import Settings
from app.core.indexing import ElasticsearchChunkIndexer, MilvusChunkIndexer
from app.core.ingestion import Chunk
from app.core.pipeline import create_ingestion_pipeline, default_pipeline


def test_ingestion_pipeline_indexes_all_chunks() -> None:
    pipeline = default_pipeline()
    chunks = [Chunk("c1", "d1", 1, "policy"), Chunk("c2", "d1", 1, "effective date")]
    assert asyncio.run(pipeline.index(chunks)) == 2
    assert set(pipeline.local_index.vectors) == {"c1", "c2"}


def test_external_index_factory_is_used_by_pipeline(monkeypatch) -> None:
    calls = []

    async def fake_milvus_upsert(self, chunks, vectors, knowledge_base_id):
        calls.append(("milvus", knowledge_base_id, len(vectors[0])))
        return len(chunks)

    async def fake_elasticsearch_upsert(self, chunks, knowledge_base_id):
        calls.append(("elasticsearch", knowledge_base_id, len(chunks)))
        return len(chunks)

    monkeypatch.setattr(MilvusChunkIndexer, "upsert", fake_milvus_upsert)
    monkeypatch.setattr(ElasticsearchChunkIndexer, "upsert", fake_elasticsearch_upsert)
    settings = Settings(
        dense_retrieval_backend="milvus",
        sparse_retrieval_backend="elasticsearch",
        embedding_dimensions=4,
    )
    pipeline = create_ingestion_pipeline(settings)

    count = asyncio.run(
        pipeline.index([Chunk("c1", "d1", 1, "policy")], "kb-policy")
    )

    assert count == 1
    assert calls == [
        ("milvus", "kb-policy", 4),
        ("elasticsearch", "kb-policy", 1),
    ]


def test_external_document_replace_deletes_stale_chunks_before_upsert() -> None:
    calls = []

    class DenseIndexer:
        dimension = 2

        async def delete_document(self, document_id):
            calls.append(("dense-delete", document_id))

        async def upsert(self, chunks, vectors, knowledge_base_id):
            calls.append(("dense-upsert", [chunk.id for chunk in chunks]))

    class SparseIndexer:
        async def delete_document(self, document_id):
            calls.append(("sparse-delete", document_id))

        async def upsert(self, chunks, knowledge_base_id):
            calls.append(("sparse-upsert", [chunk.id for chunk in chunks]))

    pipeline = create_ingestion_pipeline(Settings(embedding_dimensions=2))
    pipeline.local_index = None
    pipeline.dense_indexer = DenseIndexer()
    pipeline.sparse_indexer = SparseIndexer()

    count = asyncio.run(
        pipeline.replace_document(
            [Chunk("new", "doc-1", 1, "replacement")],
            "kb-policy",
            "doc-1",
        )
    )

    assert count == 1
    assert calls == [
        ("dense-delete", "doc-1"),
        ("sparse-delete", "doc-1"),
        ("dense-upsert", ["new"]),
        ("sparse-upsert", ["new"]),
    ]

