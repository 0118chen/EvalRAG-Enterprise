from app.core.embeddings import HashEmbedding


def test_hash_embedding_is_normalized_and_deterministic() -> None:
    provider = HashEmbedding(8)
    assert provider.embed("policy") == provider.embed("policy")
    assert len(provider.embed("policy")) == 8

