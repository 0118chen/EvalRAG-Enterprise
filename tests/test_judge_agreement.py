"""Tests for scripts/judge_agreement.py: the judge is only trustworthy if checked."""

import json

import pytest

from scripts import judge_agreement as agreement


def experiment_row(question: str, config: str = "sparse-bm25", **judge: float | None) -> dict:
    return {
        "config": config,
        "question": question,
        "judge": {"reason": "judge note", **judge},
        "evidence": [{"document_id": "doc-1", "page": 1, "score": 1.0, "text": "evidence text"}],
        "generated_answer": "generated answer",
        "expected_answer": "expected answer",
    }


def label(question: str, config: str = "sparse-bm25", **scores: float | None) -> dict:
    return {"config": config, "question": question, **scores}


def paired(rows: list[dict], labels: list[dict]) -> list[dict]:
    pairs, _ = agreement.pair_up(rows, labels)
    return pairs


def test_kappa_is_one_when_both_raters_agree_on_every_row():
    rows = [
        experiment_row(f"q{index}", correctness=score)
        for index, score in enumerate([1.0, 1.0, 0.0, 0.0])
    ]
    labels = [
        label(f"q{index}", correctness=score)
        for index, score in enumerate([1, 1, 0, 0])
    ]

    stats = agreement.cohens_kappa(paired(rows, labels), "correctness")

    assert stats["agreement"] == 1.0
    assert stats["kappa"] == pytest.approx(1.0)
    assert stats["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 2}


def test_kappa_matches_a_hand_computed_table():
    # tp=5 fp=2 fn=1 tn=2  ->  po=0.7, pe=0.7*0.6+0.3*0.4=0.54, kappa=0.16/0.46
    scores = [1.0] * 5 + [1.0] * 2 + [0.0] + [0.0] * 2
    labels = [1, 1, 1, 1, 1] + [0, 0] + [1] + [0, 0]
    rows = [experiment_row(f"q{index}", correctness=value) for index, value in enumerate(scores)]
    pairs = paired(rows, [label(f"q{index}", correctness=v) for index, v in enumerate(labels)])

    stats = agreement.cohens_kappa(pairs, "correctness")

    assert stats["confusion"] == {"tp": 5, "fp": 2, "fn": 1, "tn": 2}
    assert stats["agreement"] == pytest.approx(0.7)
    assert stats["kappa"] == pytest.approx(0.16 / 0.46)
    assert stats["interpretation"] == "fair"


def test_kappa_is_undefined_when_both_raters_are_constant():
    rows = [experiment_row(f"q{index}", correctness=1.0) for index in range(4)]
    labels = [label(f"q{index}", correctness=1) for index in range(4)]

    stats = agreement.cohens_kappa(paired(rows, labels), "correctness")

    assert stats["agreement"] == 1.0
    assert stats["kappa"] is None
    assert "undefined" in stats["interpretation"]
    assert agreement.kappa_interval(stats) is None


def test_rows_the_judge_or_the_reviewer_left_blank_are_excluded_not_zeroed():
    rows = [
        experiment_row("judged", correctness=1.0),
        experiment_row("judge failed", correctness=None),
        experiment_row("reviewer skipped", correctness=1.0),
    ]
    labels = [
        label("judged", correctness=1),
        label("judge failed", correctness=0),
        label("reviewer skipped", correctness=None),
    ]

    stats = agreement.cohens_kappa(paired(rows, labels), "correctness")

    assert stats["n"] == 1
    assert stats["unscored"] == 1
    assert stats["unlabelled"] == 1
    assert stats["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0}


def test_a_duplicate_question_inside_one_config_is_a_hard_error():
    rows = [
        experiment_row("这份规定从哪一天开始正式生效？", correctness=1.0),
        experiment_row("这份规定从哪一天开始正式生效？", correctness=0.0),
    ]
    labels = [label("这份规定从哪一天开始正式生效？", correctness=1)]

    with pytest.raises(SystemExit, match="ambiguous label target"):
        agreement.pair_up(rows, labels)


def test_labels_that_match_no_row_are_reported_not_silently_dropped():
    rows = [experiment_row("asked", correctness=1.0)]
    labels = [label("asked", correctness=1), label("typo'd question", correctness=1)]

    pairs, unmatched = agreement.pair_up(rows, labels)

    assert len(pairs) == 1
    assert unmatched == ["sparse-bm25 | typo'd question"]


def test_threshold_decides_whether_a_middle_score_is_a_disagreement():
    rows = [experiment_row("q", correctness=0.4)]
    labels = [label("q", correctness=0.0)]
    pairs = paired(rows, labels)

    lenient = agreement.cohens_kappa(pairs, "correctness", threshold=0.5)
    strict = agreement.cohens_kappa(pairs, "correctness", threshold=0.3)

    assert lenient["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 1}
    assert strict["confusion"] == {"tp": 0, "fp": 1, "fn": 0, "tn": 0}


def test_bootstrap_interval_brackets_the_point_estimate_and_is_reproducible():
    rows = []
    labels = []
    for index in range(12):
        judge = [1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0][index]
        human = [1, 1, 1, 0, 0, 0, 1, 1, 0, 0, 1, 0][index]
        rows.append(experiment_row(f"q{index}", correctness=judge))
        labels.append(label(f"q{index}", correctness=human))
    stats = agreement.cohens_kappa(paired(rows, labels), "correctness")

    first = agreement.kappa_interval(stats, iterations=200, seed=7)
    second = agreement.kappa_interval(stats, iterations=200, seed=7)

    assert first is not None
    assert first == second
    assert first[0] <= stats["kappa"] <= first[1]


def test_disagreements_are_described_from_the_judge_side():
    rows = [
        experiment_row("generous", correctness=1.0),
        experiment_row("harsh", correctness=0.0),
    ]
    labels = [label("generous", correctness=0), label("harsh", correctness=1)]
    pairs = paired(rows, labels)

    result = agreement.report(
        pairs, metrics=["correctness"], threshold=0.5, iterations=50, seed=1
    )

    directions = {item["direction"] for item in result["metrics"]["correctness"]["disagreements"]}
    assert directions == {"judge too generous", "judge too harsh"}
    assert result["metrics"]["correctness"]["disagreements"][0]["judge_reason"] == "judge note"


def test_dump_sample_hands_the_reviewer_a_null_scored_template(tmp_path):
    rows = [experiment_row("q1", correctness=0.5, faithfulness=1.0)]
    destination = tmp_path / "sample.json"

    written = agreement.dump_sample(rows, destination, metrics=("correctness", "faithfulness"))
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert written == 1
    assert payload["labels"][0]["correctness"] is None
    assert payload["labels"][0]["faithfulness"] is None
    assert payload["labels"][0]["judge"]["correctness"] == 0.5
    assert payload["labels"][0]["judge"]["reason"] == "judge note"
    assert payload["labels"][0]["evidence"][0]["text"] == "evidence text"
    assert "judge_agreement.py --labels" in payload["instructions"]


def test_load_rows_tags_every_row_with_its_config_and_rejects_other_json(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps({"configs": [{"name": "c1", "per_example": [{"question": "q"}]}]}),
        encoding="utf-8",
    )

    rows = agreement.load_rows(artifact)

    assert rows == [{"config": "c1", "question": "q"}]

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="not an experiment artifact"):
        agreement.load_rows(empty)
