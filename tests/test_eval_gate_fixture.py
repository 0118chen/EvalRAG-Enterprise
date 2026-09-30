"""CI 评测夹具的自洽性：引文能回验、基线跟得上语料。

这两条如果破了，门禁会以两种糟糕的方式失效：引文打分不到会让指标长期偏低（噪声变大），
而语料改了没重生成基线会让门禁**误报回归**——比没有门禁更坏，因为人会开始无视它。
"""

import json
from pathlib import Path

import pytest

from app.core.ingestion import chunk_pages

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eval_gate"
CORPUS = FIXTURE / "corpus"


def _read(name: str) -> str:
    return " ".join((CORPUS / name).read_text(encoding="utf-8-sig").split())


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((FIXTURE / "golden.json").read_text(encoding="utf-8"))


def test_every_evidence_quote_verifies_against_its_source_file(golden) -> None:
    checked = 0
    for example in golden["examples"]:
        if example.get("should_refuse"):
            continue
        text = _read(example["source_filename"])
        assert example["evidence_quote"] in text, (
            f"{example['source_filename']} 里找不到引文：{example['evidence_quote']}"
        )
        checked += 1
    assert checked == golden["counts"]["同义改写"]


def test_questions_do_not_name_their_source_file(golden) -> None:
    """夹具题也要去泄漏：问题里不该出现文件名或条款号。"""
    for example in golden["examples"]:
        question = example["question"]
        assert "第" not in question or "条" not in question
        assert ".md" not in question and ".txt" not in question


def test_every_corpus_file_is_reachable_from_a_question(golden) -> None:
    """每份夹具文件都该至少被一道题考到，否则它只是干扰项。"""
    files = {path.name for path in CORPUS.iterdir() if path.is_file()}
    asked = {
        example["source_filename"]
        for example in golden["examples"]
        if example.get("source_filename")
    }
    assert files == asked


def test_baseline_matches_the_current_corpus_and_question_set(golden) -> None:
    """语料换了就必须重生成基线，否则门禁会用旧数字误报。"""
    baseline = json.loads((FIXTURE / "baseline.json").read_text(encoding="utf-8"))
    documents = sorted(path.name for path in CORPUS.iterdir() if path.is_file())
    chunks = sum(
        len(chunk_pages(f"fixture:{name}", [(1, _read(name))])) for name in documents
    )
    # dataset 名来自知识库（两套题集同名），所以这里比的是会真正变化的三项
    assert baseline["dataset"]["examples"] == len(golden["examples"])
    # artifacts record the path with the platform separator (Windows 反斜杠 / Linux 正斜杠）
    source = baseline["dataset"]["source"].replace("\\", "/")
    assert source.endswith("tests/fixtures/eval_gate/golden.json")
    assert baseline["corpus"]["documents"] == len(documents)
    assert baseline["corpus"]["chunks"] == chunks, (
        "夹具语料变了但基线没重生成："
        "python -m scripts.run_golden_experiment --corpus tests/fixtures/eval_gate/corpus "
        "--golden tests/fixtures/eval_gate/golden.json --configs ... "
        "--json tests/fixtures/eval_gate/baseline.json"
    )


def test_baseline_covers_the_configs_the_gate_runs(golden) -> None:
    """基线的配置集合要和 CI 里跑的配置一致，不然门禁会"缺少配置"直接失败。"""
    baseline = json.loads((FIXTURE / "baseline.json").read_text(encoding="utf-8"))
    names = {entry["name"] for entry in baseline["configs"]}
    assert names == {
        "sparse-bm25",
        "dense-hash",
        "hybrid-rrf",
        "hybrid-convex-weighted",
        "hybrid-convex-weighted-rerank",
    }
