"""The golden-set reader has to accept both shapes without changing v1's meaning."""

import json

import pytest

from scripts.golden_format import GoldenExample, load_golden, parse_example

V1_FLAT = [
    {
        "question": "《某办法》适用于哪些机构？",
        "expected_answer": "适用于依法设立的银行业金融机构。",
        "page": 2,
        "category": "适用范围",
        "evidence_quote": "本办法适用于……",
        "source_filename": "05_固定资产贷款管理办法_2024.pdf",
    },
    {
        "question": "另一题",
        "expected_answer": "另一答案",
        "expected_page": 3,  # the API-shaped files use this key
        "category": "期限",
        "evidence_quote": "贷款期限……",
        "source_filename": "06_某办法.pdf",
    },
]


def test_flat_v1_examples_become_single_all_mode_hops() -> None:
    examples = [parse_example(entry) for entry in V1_FLAT]

    assert all(isinstance(example, GoldenExample) for example in examples)
    first = examples[0]
    assert first.source_filenames == ("05_固定资产贷款管理办法_2024.pdf",)
    assert first.quotes == ("本办法适用于……",)
    assert first.mode == "all"
    assert first.should_refuse is False
    assert examples[1].hops[0].page == 3


def test_multi_hop_example_keeps_every_span() -> None:
    example = parse_example(
        {
            "question": "承包期与流转期限的关系？",
            "category": "多跳",
            "evidence_mode": "all",
            "expected_evidence": [
                {"source_filename": "11_农村土地承包法.docx", "page": 1, "evidence_quote": "耕地的承包期为三十年"},
                {"source_filename": "13_农村集体经济组织法.docx", "page": 1, "evidence_quote": "流转期限不得超过承包期"},
            ],
        }
    )

    assert [hop.source_filename for hop in example.hops] == [
        "11_农村土地承包法.docx",
        "13_农村集体经济组织法.docx",
    ]
    assert example.mode == "all"


def test_equivalence_and_refusal_examples() -> None:
    equivalent = parse_example(
        {
            "question": "贷款人指的是谁？",
            "category": "等价多标签",
            "evidence_mode": "any",
            "expected_evidence": [
                {"source_filename": "a.pdf", "page": 1, "evidence_quote": "同一句定义"},
                {"source_filename": "b.pdf", "page": 1, "evidence_quote": "同一句定义"},
            ],
        }
    )
    assert equivalent.mode == "any"
    assert len(equivalent.hops) == 2

    refusal = parse_example(
        {"question": "私募基金的杠杆比例是多少？", "category": "应拒答", "should_refuse": True}
    )
    assert refusal.should_refuse is True
    assert refusal.hops == ()


def test_an_unknown_mode_falls_back_to_all_instead_of_inventing_one() -> None:
    example = parse_example(
        {"question": "q", "source_filename": "a.pdf", "evidence_quote": "x", "evidence_mode": "sometimes"}
    )

    assert example.mode == "all"


def test_load_golden_reads_both_file_shapes(tmp_path) -> None:
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps(V1_FLAT, ensure_ascii=False), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"dataset": "v2", "examples": V1_FLAT}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert len(load_golden(flat)) == len(load_golden(wrapped)) == 2

    with pytest.raises(TypeError):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"dataset": "v2"}), encoding="utf-8")
        load_golden(bad)  # {"examples": null} is a shape we do not guess at
