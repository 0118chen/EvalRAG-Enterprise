from app.core.evaluation import (
    RetrievalExample,
    evaluation_metadata,
    latency_percentiles,
    metrics_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_retrieval_metrics() -> None:
    example = RetrievalExample("when effective?", "doc-2", ["doc-1", "doc-2"])
    assert recall_at_k(example, 2) == 1.0
    assert reciprocal_rank(example) == 0.5


def test_experiment_metadata_has_no_secrets() -> None:
    metadata = evaluation_metadata(dataset_name="policy-recall-basic", git_commit="abc", retrieval_mode="hybrid", top_k=5)
    assert "api_key" not in str(metadata).lower()


def test_metrics_at_k_reports_each_cutoff_from_one_ranking() -> None:
    example = RetrievalExample(
        "when effective?",
        "doc-3",
        ["doc-1", "doc-2", "doc-3", "doc-3", "doc-9"],
    )

    metrics = metrics_at_k(example, (1, 3, 5))

    assert metrics["recall_at_1"] == 0.0
    assert metrics["recall_at_3"] == 1.0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 1 / 3
    assert metrics["ndcg_at_1"] == 0.0
    assert metrics["ndcg_at_3"] == 0.5  # rank 3 -> 1 / log2(3 + 1)
    assert "precision_at_3" in metrics


def test_metrics_at_k_ignores_a_cutoff_beyond_the_ranking() -> None:
    example = RetrievalExample("q", "doc-2", ["doc-2", "doc-1"])

    assert metrics_at_k(example, ()) == {"mrr": 1.0}
    assert metrics_at_k(example, (5,))["recall_at_5"] == 1.0


def test_latency_percentiles_use_nearest_rank() -> None:
    percentiles = latency_percentiles([10.0, 20.0, 30.0, 40.0, 50.0])

    assert percentiles["p50"] == 30.0
    assert percentiles["p95"] == 50.0
    assert percentiles["p100"] == 50.0
    assert latency_percentiles([]) == {}

