"""融合变体的行为测试（等权 RRF 之外的三种修法 + 一条真实缺陷的回归）。

各锁一条失效模式：等权让弱通道压过强通道（权重）、弱通道的长尾也投票（截断）、
RRF 只用名次而丢掉分数（convex）；外加"平局由参数顺序决定"这条已在真实数据上
翻掉一道题的回归。
"""

import pytest

from app.core.ingestion import Chunk
from app.core.retrieval import (
    FUSIONS,
    Fusion,
    convex_fusion,
    fuse_rankings,
    fusion_from_name,
    reciprocal_rank_fusion,
)


def chunk(name: str) -> Chunk:
    return Chunk(name, "doc", 1, f"text of {name}")


def names(ranked: list[tuple[Chunk, float]]) -> list[str]:
    return [c.id for c, _ in ranked]


def test_rrf_tie_is_independent_of_argument_order() -> None:
    """两名候选名次互换时分值完全相同；旧实现里"谁先传"决定胜负。"""
    answer = chunk("answer")
    rival = chunk("rival")
    dense = [(answer, 1.0), (rival, 0.5)]
    sparse = [(rival, 1.0), (answer, 0.5)]
    forward = reciprocal_rank_fusion(dense, sparse, top_k=2)
    backward = reciprocal_rank_fusion(sparse, dense, top_k=2)
    assert names(forward) == names(backward)
    assert forward[0][1] == pytest.approx(backward[0][1])


def test_tie_break_survives_a_re_import_that_renames_chunk_ids() -> None:
    """chunk id 是 f"{document_id}:{index}"，document_id 每次导入重新生成。

    破平一旦用到 id（或用到由 id 决定的返回顺序），同一份语料、同一套题重跑一次就会换
    答案，指标也就不可复现。这里把两名平局候选的 id 前后调换，输出必须一字不变。
    """
    body = "贷款期限不得超过十年"
    rival_body = "借款人应当按照约定用途使用贷款"
    low_id = "11111111-1111-1111-1111-111111111111:1"
    high_id = "99999999-9999-9999-9999-999999999999:2"

    def rankings(answer_id: str, rival_id: str) -> tuple[list, list]:
        answer = Chunk(answer_id, "doc", 3, body)
        rival = Chunk(rival_id, "doc", 3, rival_body)
        # 名次对称 → 分数完全相同，胜负只能由破平规则决定
        return [(answer, 0.9), (rival, 0.4)], [(rival, 5.0), (answer, 4.0)]

    def bodies(ranked: list) -> list[str]:
        return [c.text for c, _ in ranked]

    # 两次运行的 chunk id 必然不同（这正是要模拟的重新导入），所以比内容顺序而不是比 id。
    # 谁赢是任意的，但必须由内容决定，而不是由 id 决定。
    assert bodies(
        reciprocal_rank_fusion(*rankings(low_id, high_id), top_k=2)
    ) == bodies(reciprocal_rank_fusion(*rankings(high_id, low_id), top_k=2))
    assert bodies(
        convex_fusion(*rankings(low_id, high_id), top_k=2)
    ) == bodies(convex_fusion(*rankings(high_id, low_id), top_k=2))


def test_rrf_weights_let_one_channel_dominate() -> None:
    dense_first = chunk("dense-first")
    sparse_first = chunk("sparse-first")
    dense = [(dense_first, 1.0), (sparse_first, 0.5)]
    sparse = [(sparse_first, 1.0), (dense_first, 0.5)]
    assert names(reciprocal_rank_fusion(dense, sparse, top_k=2, weights=(1.0, 0.0)))[0] == "dense-first"
    assert names(reciprocal_rank_fusion(dense, sparse, top_k=2, weights=(0.0, 1.0)))[0] == "sparse-first"
    assert names(reciprocal_rank_fusion(dense, sparse, top_k=2, weights=(0.3, 0.7)))[0] == "sparse-first"


def test_rrf_weights_must_match_the_number_of_rankings() -> None:
    with pytest.raises(ValueError, match="one entry per ranking"):
        reciprocal_rank_fusion([(chunk("a"), 1.0)], [(chunk("b"), 1.0)], weights=(1.0,))


def test_rrf_truncate_keeps_the_tail_out_of_the_vote() -> None:
    tail = chunk("tail")
    head = chunk("head")
    dense = [(head, 1.0)]
    sparse = [(chunk(f"filler{i}"), 1.0) for i in range(5)] + [(tail, 1.0)]
    assert "tail" not in names(reciprocal_rank_fusion(dense, sparse, top_k=10, truncate=5))
    assert "tail" in names(reciprocal_rank_fusion(dense, sparse, top_k=10))


def test_rrf_ignores_scores_while_convex_uses_them() -> None:
    """只改分数、名次一字不动：RRF 结论不变，convex 会把中间那个候选从第 1 打到第 3。"""
    a, b, c = chunk("a"), chunk("b"), chunk("c")
    dense_strong_b = [(a, 1.0), (b, 0.9), (c, 0.0)]
    dense_weak_b = [(a, 1.0), (b, 0.1), (c, 0.0)]
    sparse = [(c, 1.0), (b, 0.2), (a, 0.0)]
    assert names(reciprocal_rank_fusion(dense_strong_b, sparse, top_k=3)) == names(
        reciprocal_rank_fusion(dense_weak_b, sparse, top_k=3)
    )
    assert names(convex_fusion(dense_strong_b, sparse, top_k=3)) != names(
        convex_fusion(dense_weak_b, sparse, top_k=3)
    )


def test_convex_normalises_per_channel_so_scale_does_not_decide() -> None:
    """dense 的量纲是 1000 级、sparse 是 1 级；不归一化的话 dense 会碾压一切。"""
    dense_top, sparse_top = chunk("dense-top"), chunk("sparse-top")
    fused = convex_fusion(
        [(dense_top, 1000.0), (sparse_top, 0.0)],
        [(sparse_top, 1.0), (dense_top, 0.0)],
        weights=(0.4, 0.6),
        top_k=2,
    )
    assert names(fused)[0] == "sparse-top"


def test_convex_gives_a_missing_candidate_no_contribution() -> None:
    only_dense = chunk("only-dense")
    both = chunk("both")
    fused = convex_fusion([(only_dense, 1.0), (both, 0.5)], [(both, 1.0)], top_k=2)
    assert names(fused)[0] == "both"


def test_convex_handles_a_single_candidate_and_empty_channels() -> None:
    only = chunk("only")
    assert names(convex_fusion([(only, 3.0)], top_k=2)) == ["only"]
    assert convex_fusion([], [], top_k=2) == []
    assert names(convex_fusion([(only, 3.0)], [], top_k=2)) == ["only"]


def test_fusion_from_name_round_trip_and_unknown_name() -> None:
    assert fusion_from_name("rrf") == Fusion()
    assert fusion_from_name("convex-bm25-heavy") == FUSIONS["convex-bm25-heavy"]
    assert fusion_from_name("convex-bm25-heavy").method == "convex"
    with pytest.raises(ValueError, match="unsupported fusion"):
        fusion_from_name("nope")


def test_fuse_rankings_dispatches_and_rejects_unknown_method() -> None:
    a, b = chunk("a"), chunk("b")
    dense = [(a, 1.0), (b, 0.5)]
    sparse = [(b, 1.0), (a, 0.5)]
    assert names(fuse_rankings(dense, sparse, Fusion(), 2))[0] == "a"
    assert names(
        fuse_rankings(dense, sparse, Fusion(method="convex", weights=(0.0, 1.0)), 2)
    )[0] == "b"
    with pytest.raises(ValueError, match="unsupported fusion method"):
        fuse_rankings(dense, sparse, Fusion(method="magic"), 2)


def test_the_shipped_default_is_still_equal_weight_rrf() -> None:
    """默认必须逐字等于产品原先的行为：等权、k=60、不截断。"""
    default = FUSIONS["rrf"]
    assert (default.method, default.weights, default.truncate, default.k) == (
        "rrf",
        (1.0, 1.0),
        None,
        60,
    )
