"""Golden-set rows must be matched to their labels by example id, never by question text.

v2 ships two examples with the same question - "这份规定从哪一天开始正式生效？" asked of two
different regulations, with different correct answers. The harness used to build its golden
lookup as ``{question: example}``, which silently collapsed them: one row was scored against
the *other* example's expected documents, and the random-reference baseline counted one
example instead of two. Nothing in the pipeline could notice, because the numbers still
looked plausible.
"""

from scripts.golden_format import GoldenExample, GoldenHop
from scripts.run_golden_experiment import evidence_stats, per_example

SHARED_QUESTION = "这份规定从哪一天开始正式生效？"


def _example(filename: str, quote: str) -> GoldenExample:
    return GoldenExample(
        question=SHARED_QUESTION,
        category="同义改写",
        hops=(GoldenHop(source_filename=filename, page=1, quote=quote),),
    )


# Two examples, one identical question text, two different labels.
GOLDEN_BY_ID = {
    "example-12": _example("12_law.docx", "本法自2021年6月1日起施行。"),
    "example-13": _example("13_law.docx", "本法自2025年5月1日起施行。"),
}


def _item(example_id: str, documents: list[tuple[str, str]]) -> dict:
    return {
        "example_id": example_id,
        "question": SHARED_QUESTION,
        "category": "同义改写",
        "expected_page": 1,
        "metrics": {
            "recall_at_1": 1.0,
            "recall_at_3": 1.0,
            "recall_at_5": 1.0,
            "all_targets_at_5": 1.0,
            "page_hit": 1.0,
        },
        "passage_rank": 1,
        "passage_ranks": [1],
        "latency_ms": 1.0,
        "retrieved": [
            {
                "document_id": document,
                "page": 1,
                "score": 1.0,
                "text": quote,
            }
            for document, quote in documents
        ],
    }


def test_two_examples_sharing_a_question_keep_their_own_labels() -> None:
    results = {
        "examples": [
            # Only doc-12's text is retrieved for this row, so only its quote can hit.
            _item("example-12", [("doc-12", "本法自2021年6月1日起施行。")]),
            _item("example-13", [("doc-13", "本法自2025年5月1日起施行。")]),
        ]
    }

    rows = per_example(results, GOLDEN_BY_ID)

    assert [row["source_filename"] for row in rows] == ["12_law.docx", "13_law.docx"]
    # Previously both rows were attributed to whichever example won the dict, so one of
    # these two was scored against the other regulation's quote and reported quote_hit False.
    assert [row["quote_hit"] for row in rows] == [True, True]


def test_the_quote_check_notices_a_row_retrieved_the_other_regulation() -> None:
    results = {
        "examples": [
            _item("example-12", [("doc-13", "本法自2025年5月1日起施行。")]),
        ]
    }

    rows = per_example(results, GOLDEN_BY_ID)

    assert rows[0]["source_filename"] == "12_law.docx"
    assert rows[0]["quote_hit"] is False


def test_the_random_reference_counts_every_example_even_when_a_question_repeats() -> None:
    results = {
        "examples": [
            _item("example-12", [("doc-12", "本法自2021年6月1日起施行。")]),
            _item("example-13", [("doc-13", "本法自2025年5月1日起施行。")]),
        ]
    }

    stats = evidence_stats(results, GOLDEN_BY_ID)

    # Two answerable examples, both with their own quote retrieved.
    assert stats["answerable_examples"] == 2
    assert stats["evidence_quote_hit_rate"] == 1.0
