from app.core.evaluation import RetrievalExample, evaluation_metadata, recall_at_k, reciprocal_rank


def test_retrieval_metrics() -> None:
    example = RetrievalExample("when effective?", "doc-2", ["doc-1", "doc-2"])
    assert recall_at_k(example, 2) == 1.0
    assert reciprocal_rank(example) == 0.5


def test_experiment_metadata_has_no_secrets() -> None:
    metadata = evaluation_metadata(dataset_name="policy-recall-basic", git_commit="abc", retrieval_mode="hybrid", top_k=5)
    assert "api_key" not in str(metadata).lower()

