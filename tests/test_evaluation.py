from app.core.evaluation import (
    RetrievalExample,
    evaluation_metadata,
    latency_percentiles,
    metrics_at_k,
    passage_metrics,
    passage_ranks,
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


def test_passage_rank_locates_the_answering_chunk() -> None:
    retrieved = ["header of the regulation", "loan   policy\neffective date", "appendix"]

    assert passage_ranks(retrieved, "loan policy effective date") == [2]
    assert passage_ranks(retrieved, "not in any chunk") == [None]


def test_passage_rank_treats_an_empty_quote_as_no_evidence() -> None:
    # An empty quote must never match: it would report a perfect score for nothing.
    assert passage_ranks(["anything"], "") == []
    assert passage_ranks(["anything"], "   ") == []
    # Nothing to judge means no keys at all, so the aggregate cannot average it in.
    assert passage_metrics(["anything"], "") == {}


def test_passage_metrics_reward_finding_the_passage_and_ranking_it_first() -> None:
    quote = "loan policy effective date"

    first = passage_metrics(["loan policy effective date", "other"], quote)
    assert first == {"passage_hit": 1.0, "passage_at_1": 1.0, "passage_mrr": 1.0}

    second = passage_metrics(["other", "loan policy effective date"], quote)
    assert second == {"passage_hit": 1.0, "passage_at_1": 0.0, "passage_mrr": 0.5}

    missing = passage_metrics(["other", "more"], quote)
    assert missing == {"passage_hit": 0.0, "passage_at_1": 0.0, "passage_mrr": 0.0}


def test_ndcg_reaches_one_when_every_hop_is_ranked_as_well_as_possible() -> None:
    # Two hops cannot both sit at rank 1, so the ideal ranking is rank 1 and rank 2.
    # Dividing by the hop count instead of that ideal capped this at (1 + 1/log2(3))/2
    # = 0.8155 - a perfect ranking that could never be scored as perfect.
    perfect = RetrievalExample("q", ("doc-a", "doc-b"), ["doc-a", "doc-b"])
    assert metrics_at_k(perfect, (5,))["ndcg_at_5"] == 1.0

    reversed_but_ideal = RetrievalExample("q", ("doc-a", "doc-b"), ["doc-b", "doc-a"])
    assert metrics_at_k(reversed_but_ideal, (5,))["ndcg_at_5"] == 1.0

    single = RetrievalExample("q", "doc-a", ["doc-a"])
    assert metrics_at_k(single, (5,))["ndcg_at_5"] == 1.0


def test_ndcg_falls_when_a_hop_slips_past_its_ideal_rank() -> None:
    example = RetrievalExample("q", ("doc-a", "doc-b"), ["doc-a", "noise", "doc-b"])

    value = metrics_at_k(example, (5,))["ndcg_at_5"]

    # doc-b landed at rank 3 instead of rank 2: better than nothing, worse than ideal.
    assert 0.9 < value < 0.95
    assert value < 1.0
    # Past the cutoff the hop is simply not retrieved.
    assert metrics_at_k(example, (2,))["ndcg_at_2"] < value

