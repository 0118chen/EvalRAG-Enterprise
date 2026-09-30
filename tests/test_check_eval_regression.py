"""回归门禁的判定逻辑：什么算回归、什么不算、什么情况下根本不该比。

延迟不设门禁（同一台机都会 ±10%），所以这些测试的重点是"该报的报、不该报的不报"。
"""

from scripts.check_eval_regression import compare, quality_metrics


def _report(*, chunks: int = 12, sparse_r1: float = 0.75, latency: float = 100.0) -> dict:
    return {
        "dataset": {
            "name": "eval-gate-fixture",
            "examples": 9,
            "source": "tests/fixtures/eval_gate/golden.json",
        },
        "corpus": {"documents": 2, "chunks": chunks},
        "rerank_backend": "lexical",
        "embedding": {"provider": "hash"},
        "configs": [
            {
                "name": "sparse-bm25",
                "metrics": {"recall_at_1": sparse_r1, "page_hit": 0.875, "latency_ms_p50": latency},
                "evidence_quote_hit_rate": 0.625,
            },
            {
                "name": "hybrid-rrf",
                "metrics": {"recall_at_1": 0.625, "page_hit": 0.750, "latency_ms_p50": latency},
                "evidence_quote_hit_rate": 0.500,
            },
        ],
    }


def test_identical_reports_pass() -> None:
    result = compare(_report(), _report())
    assert result["problems"] == []
    assert result["regressions"] == []


def test_latency_movement_is_not_a_regression() -> None:
    result = compare(_report(latency=100.0), _report(latency=180.0))
    assert result["problems"] == [], "延迟抖动不该让门禁失败"


def test_recall_drop_is_a_regression() -> None:
    result = compare(_report(sparse_r1=0.75), _report(sparse_r1=0.50))
    assert any("sparse-bm25.recall_at_1" in line for line in result["regressions"])
    assert result["problems"]


def test_improvement_is_reported_but_not_a_failure() -> None:
    result = compare(_report(sparse_r1=0.50), _report(sparse_r1=0.875))
    assert result["problems"] == []
    assert any("sparse-bm25.recall_at_1" in line for line in result["improvements"])


def test_tolerance_absorbs_float_noise_only() -> None:
    assert compare(_report(sparse_r1=0.75), _report(sparse_r1=0.75 - 1e-12))["problems"] == []
    assert compare(_report(sparse_r1=0.75), _report(sparse_r1=0.74))["problems"], "0.01 不是噪声"


def test_a_missing_config_fails_the_gate() -> None:
    current = _report()
    current["configs"] = current["configs"][:1]
    result = compare(_report(), current)
    assert any("hybrid-rrf" in line for line in result["problems"])


def test_a_missing_metric_fails_the_gate() -> None:
    current = _report()
    del current["configs"][0]["metrics"]["page_hit"]
    result = compare(_report(), current)
    assert any("page_hit 消失" in line for line in result["regressions"])


def test_different_corpus_size_refuses_to_compare() -> None:
    """字数不同的语料/题集下比较指标毫无意义，必须直接失败而不是给出一个 Δ。"""
    result = compare(_report(chunks=386), _report(chunks=12))
    assert any("chunks 不一致" in line for line in result["problems"])


def test_different_golden_set_refuses_to_compare() -> None:
    current = _report()
    current["dataset"] = {"name": "another-set", "examples": 9}
    result = compare(_report(), current)
    assert any("dataset 不一致" in line for line in result["problems"])


def test_path_separators_do_not_break_the_identity_check() -> None:
    """基线在 Windows 上生成、门禁在 Linux 上跑：分隔符不同不能算"题集变了"。"""
    windows = _report()
    windows["dataset"]["source"] = "tests\\fixtures\\eval_gate\\golden.json"
    linux = _report()
    linux["dataset"]["source"] = "tests/fixtures/eval_gate/golden.json"
    assert compare(windows, linux)["problems"] == []


def test_quality_metrics_excludes_latency_and_nested_data() -> None:
    entry = {
        "name": "sparse-bm25",
        "metrics": {"recall_at_1": 0.5, "latency_ms_p95": 42.0},
        "evidence_quote_hit_rate": 0.25,
        "per_example": [{"question": "x"}],
        "rerank": False,
    }
    scalars = quality_metrics(entry)
    assert "recall_at_1" in scalars and "evidence_quote_hit_rate" in scalars
    assert "latency_ms_p95" not in scalars
    assert "rerank" not in scalars, "布尔不是质量指标"
    assert "per_example" not in scalars
