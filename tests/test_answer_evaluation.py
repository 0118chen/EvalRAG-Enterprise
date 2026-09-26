from app.core.answer_evaluation import parse_judge_response


def test_parse_judge_response_clamps_scores() -> None:
    result = parse_judge_response(
        '```json\n{"correctness":1.2,"faithfulness":-0.2,'
        '"completeness":0.8,"reason":"partially correct"}\n```'
    )
    assert result.correctness == 1.0
    assert result.faithfulness == 0.0
    assert result.completeness == 0.8


def test_parse_judge_response_handles_invalid_output() -> None:
    result = parse_judge_response("not json")
    assert result.correctness == 0.0
    assert "not valid JSON" in result.reason
