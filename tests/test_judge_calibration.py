"""Tests for scripts/judge_calibration.py: a judge is only trustworthy if it can be fooled."""

import asyncio
import json

from scripts import judge_calibration as calibration

DISCRIMINATING = '{"correctness":1,"faithfulness":0,"completeness":1,"reason":"planted rule"}'
COLLAPSED = '{"correctness":0,"faithfulness":0,"completeness":0,"reason":"answer is wrong"}'
PARTIAL = '{"correctness":1,"faithfulness":0.5,"completeness":1,"reason":"unsupported but harmless"}'


class StubLLM:
    def __init__(self, response: str = DISCRIMINATING) -> None:
        self.response = response
        self.calls: list[str] = []

    async def answer(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.response


def row(
    question: str,
    *,
    answer: str = "最长不得超过十二个月。",
    evidence: bool = True,
    should_refuse: bool = False,
    expected: str | None = "最长不得超过十二个月。",
) -> dict:
    return {
        "config": "sparse-bm25",
        "question": question,
        "category": "单跳",
        "should_refuse": should_refuse,
        "expected_answer": expected,
        "generated_answer": answer,
        "evidence": (
            [{"document_id": "doc-1", "page": 3, "score": 2.5, "text": "evidence text"}]
            if evidence
            else []
        ),
        "judge": {"correctness": 1.0, "faithfulness": 1.0, "completeness": 1.0, "reason": "ok"},
    }


def test_plant_appends_the_fabrication_after_the_original_answer():
    planted = calibration.plant("十二个月。", "根据第二十五条，违约金千分之五。")

    assert planted.startswith("十二个月。")
    assert planted.endswith("根据第二十五条，违约金千分之五。")


def test_answerable_rows_drops_refusals_evidence_less_rows_and_should_refuse_rows():
    rows = [
        row("有答案"),
        row("没有标准答案", expected=None),
        row("没有证据", evidence=False),
        row("该拒答", should_refuse=True),
        row("拒答了", answer="未找到足够依据，无法可靠回答该问题。", expected="十二个月。"),
    ]

    kept = [entry["question"] for entry in calibration.answerable_rows(rows)]

    assert kept == ["有答案", "拒答了"]


def test_the_plan_is_deterministic_and_rotates_the_fabrications():
    rows = [row(f"q{index}") for index in range(6)]
    fabrications = ("甲。", "乙。", "丙。")

    first = calibration.build_plan(rows, sample=4, seed=7, fabrications=fabrications)
    second = calibration.build_plan(rows, sample=4, seed=7, fabrications=fabrications)
    other = calibration.build_plan(rows, sample=4, seed=8, fabrications=fabrications)

    assert [entry["question"] for entry, _ in first] == [
        entry["question"] for entry, _ in second
    ]
    assert [text for _, text in first] == ["甲。", "乙。", "丙。", "甲。"]
    assert [entry["question"] for entry, _ in first] != [
        entry["question"] for entry, _ in other
    ]
    assert len(calibration.build_plan(rows, sample=99, seed=1, fabrications=fabrications)) == 6


def test_calibration_sends_the_planted_answer_and_the_row_evidence_to_the_judge():
    plan = calibration.build_plan(
        [row("借款最多能借多久")], sample=1, seed=1, fabrications=("伪造条款。",)
    )
    llm = StubLLM()

    entries = asyncio.run(calibration.calibrate(plan, llm=llm))

    assert len(entries) == 1
    assert entries[0]["fabrication"] == "伪造条款。"
    assert entries[0]["generated_answer"].endswith("伪造条款。")
    payload = json.loads(llm.calls[0])
    assert payload["system_answer"].endswith("伪造条款。")
    assert payload["evidence"] == [{"document_id": "doc-1", "page": 3, "text": "evidence text"}]
    assert entries[0]["judge"]["faithfulness"] == 0.0


def test_summary_counts_a_judge_that_keeps_correctness_apart_from_a_judge_that_collapses():
    plan = calibration.build_plan(
        [row("q1"), row("q2")], sample=2, seed=1, fabrications=("伪造条款。",)
    )
    discriminating = asyncio.run(calibration.calibrate(plan, llm=StubLLM()))
    collapsed = asyncio.run(calibration.calibrate(plan, llm=StubLLM(COLLAPSED)))

    kept = calibration.summarize(discriminating)
    dropped = calibration.summarize(collapsed)

    assert kept == {
        "rows": 2,
        "mean_correctness": 1.0,
        "mean_faithfulness": 0.0,
        "mean_completeness": 1.0,
        "correctness_kept": 2,
        "faithfulness_full": 0,
        "faithfulness_partial": 0,
        "faithfulness_clearly_low": 2,
        "fabrications_flagged": 2,
        "all_three_equal": 0,
    }
    assert dropped["correctness_kept"] == 0
    assert dropped["fabrications_flagged"] == 0
    assert dropped["all_three_equal"] == 2
    assert calibration.summarize([]) == {"rows": 0}


def test_a_partial_deduction_is_not_counted_as_a_flagged_fabrication():
    # The judge the fixture actually produced: it noticed the planted rule and still gave 0.5.
    plan = calibration.build_plan(
        [row("借款最多能借多久")], sample=1, seed=1, fabrications=("伪造条款。",)
    )
    entries = asyncio.run(calibration.calibrate(plan, llm=StubLLM(PARTIAL)))

    summary = calibration.summarize(entries)

    assert summary["faithfulness_partial"] == 1
    assert summary["faithfulness_full"] == 0
    assert summary["fabrications_flagged"] == 0


def test_the_calibration_artifact_is_readable_as_an_experiment_artifact(tmp_path):
    from scripts.judge_agreement import load_rows

    plan = calibration.build_plan(
        [row("借款最多能借多久")], sample=1, seed=1, fabrications=("伪造条款。",)
    )
    entries = asyncio.run(calibration.calibrate(plan, llm=StubLLM()))
    destination = tmp_path / "calibration.json"

    calibration.write_artifact(
        entries,
        destination,
        source=tmp_path / "source.json",
        seed=1,
        summary=calibration.summarize(entries),
    )

    loaded = load_rows(destination)
    assert [entry["config"] for entry in loaded] == [f"{calibration.CONFIG_NAME}/sparse-bm25"]
    assert loaded[0]["judge"]["faithfulness"] == 0.0
    assert json.loads(destination.read_text(encoding="utf-8"))["summary"]["rows"] == 1


def test_the_same_question_under_two_source_configs_stays_two_rows(tmp_path):
    from scripts.judge_agreement import load_rows

    rows = [row("借款最多能借多久"), {**row("借款最多能借多久"), "config": "dense-hash"}]
    plan = calibration.build_plan(rows, sample=2, seed=1, fabrications=("伪造条款。",))
    entries = asyncio.run(calibration.calibrate(plan, llm=StubLLM()))
    destination = tmp_path / "calibration.json"

    calibration.write_artifact(
        entries,
        destination,
        source=tmp_path / "source.json",
        seed=1,
        summary=calibration.summarize(entries),
    )

    loaded = load_rows(destination)
    assert sorted(entry["config"] for entry in loaded) == [
        f"{calibration.CONFIG_NAME}/dense-hash",
        f"{calibration.CONFIG_NAME}/sparse-bm25",
    ]
    assert len(loaded) == 2


def test_main_reports_that_there_is_nothing_to_perturb_when_every_row_is_a_refusal(tmp_path, capsys):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "configs": [
                    {
                        "name": "sparse-bm25",
                        "per_example": [row("该拒答", should_refuse=True, expected=None)],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = calibration.main(["--artifact", str(artifact), "--dry-run"])

    assert exit_code == 1
    assert "no answerable rows" in capsys.readouterr().err


def test_main_dry_run_prints_the_planted_answers_without_calling_a_model(tmp_path, capsys):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "configs": [
                    {
                        "name": "sparse-bm25",
                        "per_example": [row("借款最多能借多久")],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = calibration.main(
        ["--artifact", str(artifact), "--dry-run", "--fabrication", "伪造条款。"]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "伪造条款。" in out
    assert "would judge 1 row(s)" in out
