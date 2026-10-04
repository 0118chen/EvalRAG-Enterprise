"""The paired bootstrap in scripts/bootstrap_ci.py.

The point of the tool is that a delta on a 20-question set is usually one question, so
these tests pin the two things that decide whether it can be trusted: that pairs stay
aligned per question with mismatched rows dropped, and that a difference the set cannot
resolve is reported as inconclusive rather than as a win.
"""

import pytest

from scripts.bootstrap_ci import (
    aggregate_of,
    compare,
    paired_bootstrap,
    paired_values,
    verdict,
)


def test_pairs_are_per_question_and_unreported_rows_are_dropped():
    reference = [
        {"recall_at_1": 1.0},
        {"recall_at_1": 0.0},
        {"recall_at_1": None},
        {"recall_at_5": 1.0},  # metric absent on this row
    ]
    candidate = [
        {"recall_at_1": 0.0},
        {"recall_at_1": 1.0},
        {"recall_at_1": 1.0},
        {"recall_at_1": 1.0},
    ]

    assert paired_values(reference, candidate, "recall_at_1") == [(1.0, 0.0), (0.0, 1.0)]


def test_booleans_count_as_zero_and_one():
    reference = [{"quote_hit": True}, {"quote_hit": False}]
    candidate = [{"quote_hit": True}, {"quote_hit": True}]

    assert paired_values(reference, candidate, "quote_hit") == [(1.0, 1.0), (0.0, 1.0)]


def test_a_difference_larger_than_the_set_can_explain_is_significant():
    # Every question improves: n=10, so one question is 10pp and the interval must clear 0.
    pairs = [(0.0, 1.0)] * 10

    stats = paired_bootstrap(pairs, iterations=500)

    assert stats["n"] == 10
    assert stats["delta"] == pytest.approx(1.0)
    assert stats["low"] > 0
    assert verdict(stats) == "better"


def test_a_difference_of_one_question_is_not_significant():
    # The real v3 shape: reference misses the first question, candidate the last.
    pairs = [(0.0, 1.0), (1.0, 1.0), (1.0, 1.0), (1.0, 1.0), (1.0, 0.0)]

    stats = paired_bootstrap(pairs, iterations=2000)

    assert stats["delta"] == pytest.approx(0.0)
    assert stats["low"] <= 0 <= stats["high"]
    assert verdict(stats) == "inconclusive"
    assert stats["step_pp"] == pytest.approx(20.0)


def test_identical_configurations_differ_by_nothing():
    stats = paired_bootstrap([(1.0, 1.0)] * 7, iterations=200)

    assert (stats["delta"], stats["low"], stats["high"]) == (0.0, 0.0, 0.0)
    assert verdict(stats) == "inconclusive"


def test_the_same_seed_reproduces_the_same_interval():
    pairs = [(0.0, 1.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    first = paired_bootstrap(pairs, iterations=300, seed=7)
    second = paired_bootstrap(pairs, iterations=300, seed=7)

    assert first == second


def test_no_pairs_is_reported_as_no_data_rather_than_as_a_zero_delta():
    stats = paired_bootstrap([])

    assert stats["n"] == 0
    assert verdict(stats) == "no data"


def artifact() -> dict:
    """One reference and two candidates over four questions."""
    return {
        "dataset": {"name": "tiny"},
        "configs": [
            {
                "name": "reference",
                "per_example": [
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 0.0, "quote_hit": False},
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 1.0, "quote_hit": True},
                ],
                "metrics": {"recall_at_1": 0.75},
            },
            {
                "name": "better",
                "per_example": [
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 1.0, "quote_hit": True},
                ],
                "metrics": {"recall_at_1": 1.0},
                "evidence_quote_hit_rate": 1.0,
            },
            {
                "name": "liar",
                "per_example": [
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 0.0, "quote_hit": False},
                    {"recall_at_1": 1.0, "quote_hit": True},
                    {"recall_at_1": 1.0, "quote_hit": True},
                ],
                # Published aggregate that the per-example rows do not support.
                "metrics": {"recall_at_1": 0.5},
                "evidence_quote_hit_rate": 0.25,
            },
        ],
    }


def test_compare_measures_every_other_config_against_the_reference():
    report = compare(artifact(), "reference", metrics=("recall_at_1",), iterations=500)

    assert report["examples"] == 4
    rows = {row["config"]: row for row in report["comparisons"]}
    assert set(rows) == {"better", "liar"}
    assert rows["better"]["delta"] == pytest.approx(0.25)
    # One question in four improved, and that is the honest answer at n=4: 25pp of delta
    # that the set cannot separate from noise.
    assert rows["better"]["verdict"] == "inconclusive"
    assert rows["better"]["aggregate_matches"] is True


def test_compare_reports_a_win_when_every_question_improves():
    data = artifact()
    data["configs"][0]["per_example"] = [{"recall_at_1": 0.0}] * 4
    data["configs"][0]["metrics"] = {"recall_at_1": 0.0}
    data["configs"][1]["per_example"] = [{"recall_at_1": 1.0}] * 4
    data["configs"][1]["metrics"] = {"recall_at_1": 1.0}

    report = compare(data, "reference", metrics=("recall_at_1",), iterations=500)

    better = next(row for row in report["comparisons"] if row["config"] == "better")
    assert better["delta"] == pytest.approx(1.0)
    assert better["low"] > 0
    assert better["verdict"] == "better"


def test_compare_flags_an_aggregate_that_contradicts_its_own_rows():
    report = compare(artifact(), "reference", metrics=("recall_at_1",), iterations=100)

    liar = next(row for row in report["comparisons"] if row["config"] == "liar")
    assert liar["aggregate_matches"] is False


def test_compare_reads_the_published_quote_hit_key():
    config = {"metrics": {}, "evidence_quote_hit_rate": 0.85}

    assert aggregate_of(config, "quote_hit") == 0.85
    assert aggregate_of(config, "recall_at_1") is None


def test_compare_refuses_an_unknown_reference_config():
    with pytest.raises(SystemExit, match="unknown reference config"):
        compare(artifact(), "nope")


def test_compare_refuses_an_artifact_that_predates_per_example_rows():
    without_rows = {
        "configs": [{"name": "reference", "metrics": {"recall_at_1": 0.75}}],
    }

    with pytest.raises(SystemExit, match="no per_example rows"):
        compare(without_rows, "reference")
