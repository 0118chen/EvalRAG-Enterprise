"""把融合的失效与修法算成数字：同一份语料、同一条产品路径、四种融合逐一对比。

这不是单元测试，是归因工具。对每道单跳题：

- 走**产品路径**（`create_retriever` + `candidate_k = top_k × multiplier`，即每个通道各取 20 个
  候选再融合），而不是直接调 `retrieve()` 拿全量排序——两者候选集不同，结论也会不同；
- 记录标注页在 sparse / dense / 融合排序里的名次；
- 统计"BM25 页级第一"里被融合挤掉的、以及被融合救回的，按融合方式分别算。

用法：
    python -m scripts.probe_fusion --database data/experiments/golden-v2-fusion.db \
        --golden law/golden_eval_v2.json
"""

import argparse
import asyncio
import json
import pathlib
import re
import sqlite3
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.backends import create_retriever
from app.core.ingestion import Chunk
from app.core.retrieval import fuse_rankings, fusion_from_name

TOP_K = 5
FUSION_ORDER = ["rrf", "rrf-bm25-heavy", "rrf-truncate-5", "convex", "convex-bm25-heavy"]


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def load_chunks(database: pathlib.Path) -> list[Chunk]:
    con = sqlite3.connect(database)
    rows = con.execute("select id, document_id, page, text from chunks").fetchall()
    return [Chunk(id=r[0], document_id=r[1], page=r[2], text=r[3]) for r in rows]


def load_document_ids(database: pathlib.Path) -> dict[str, str]:
    """文件名 -> 文档 id，取本次运行的 DB（重跑会生成新 id，别用 manifest）。"""
    con = sqlite3.connect(database)
    return {
        filename: doc_id
        for doc_id, filename in con.execute("select id, filename from documents")
    }


def rank_of(ranked: list[tuple[Chunk, float]], targets: set[tuple[str, int]]) -> int | None:
    for rank, (chunk, _score) in enumerate(ranked, start=1):
        if (chunk.document_id, chunk.page) in targets:
            return rank
    return None


def single_hop_targets(golden: dict[str, Any], by_name: dict[str, str]):
    for example in golden["examples"]:
        if example.get("should_refuse") or "source_filename" not in example:
            continue
        doc = by_name.get(example["source_filename"])
        if doc is None:
            raise SystemExit(f"文件名不在本次运行的 DB 里：{example['source_filename']}")
        yield example, {(doc, example["page"])}


async def probe(database: pathlib.Path, golden_path: pathlib.Path) -> None:
    settings = Settings(cache_enabled=False, rerank_enabled=False, query_rewrite_enabled=False)
    chunks = load_chunks(database)
    by_name = load_document_ids(database)
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    candidate_k = max(TOP_K, TOP_K * settings.retrieval_candidate_multiplier)

    sparse_retriever = create_retriever(settings, chunks, "sparse", "probe", None)
    dense_retriever = create_retriever(settings, chunks, "dense", "probe", None)

    stats = {
        name: {"total": 0, "bm25_first": 0, "kept": 0, "lost": 0, "out_of_top5": 0, "rescued": 0}
        for name in FUSION_ORDER
    }
    examples = 0
    dense_first = 0
    for example, targets in single_hop_targets(golden, by_name):
        examples += 1
        question = example["question"]
        sparse = await sparse_retriever.search(question, candidate_k)
        dense = await dense_retriever.search(question, candidate_k)
        sparse_rank = rank_of(sparse, targets)
        if rank_of(dense, targets) == 1:
            dense_first += 1
        for name in FUSION_ORDER:
            # 与 HybridRetriever.search(candidate_k) 逐字等价：两个通道各取 candidate_k 再融合
            fused = fuse_rankings(dense, sparse, fusion_from_name(name), candidate_k)
            fused_rank = rank_of(fused[:TOP_K], targets)
            bucket = stats[name]
            bucket["total"] += 1
            if sparse_rank == 1:
                bucket["bm25_first"] += 1
                if fused_rank == 1:
                    bucket["kept"] += 1
                else:
                    bucket["lost"] += 1
                    if fused_rank is None:
                        bucket["out_of_top5"] += 1
            elif fused_rank == 1:
                bucket["rescued"] += 1

    print(f"\n===== {golden_path.name}（{examples} 道单跳题，{len(chunks)} chunk，"
          f"每通道 {candidate_k} 候选）=====")
    header = f"{'融合方式':<20}{'BM25第一':>9}{'融合后仍第一':>13}{'被挤掉':>8}{'掉出top5':>10}{'救回':>7}"
    print(header)
    for name in FUSION_ORDER:
        s = stats[name]
        print(
            f"{name:<20}{s['bm25_first']:>9}{s['kept']:>13}{s['lost']:>8}"
            f"{s['out_of_top5']:>10}{s['rescued']:>7}"
        )
    print(f"(对照) 单个 dense 通道在同一批题上的页级第一：{dense_first}/{examples}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=pathlib.Path, required=True)
    parser.add_argument("--golden", type=pathlib.Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.database, args.golden))


if __name__ == "__main__":
    main()
