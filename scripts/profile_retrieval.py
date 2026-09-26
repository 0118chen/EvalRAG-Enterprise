"""Attribute retrieval latency to functions, so performance claims stay checkable.

The query path is the one part of a RAG service users feel directly, and "the
retriever is slow" is not an actionable finding. This script prints a p50 per
configuration plus a cProfile breakdown of the same call.

Usage:
    python -m scripts.profile_retrieval --database data/experiments/golden.db \
        --tenant golden-experiment --question "农村土地承包的承包期是多少年？"
"""

import argparse
import asyncio
import cProfile
import os
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBES = (
    ("sparse", False, False),
    ("sparse", True, False),
    ("dense", False, False),
    ("hybrid", False, False),
    ("hybrid", True, False),
    ("hybrid", True, True),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/experiments/golden.db")
    parser.add_argument("--tenant", default="golden-experiment")
    parser.add_argument("--question", default="农村土地承包的承包期是多少年？")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--profile-rounds", type=int, default=3)
    parser.add_argument("--functions", type=int, default=12)
    args = parser.parse_args()

    database = (ROOT / args.database).resolve()
    if not database.exists():
        sys.exit(f"database not found: {database} (run scripts.run_golden_experiment first)")
    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ["CACHE_ENABLED"] = "false"
    os.environ["LANGSMITH_ENABLED"] = "false"

    sys.path.insert(0, str(ROOT))
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    from app.core.cache import create_cache
    from app.core.observability import TraceManager
    from app.core.query_rewrite import create_query_rewriter
    from app.core.reranking import create_reranker
    from app.core.retrieval_service import RetrievalService
    from app.core.store import create_store

    store = create_store(settings.database_url)
    knowledge_bases = store.list_knowledge_bases(args.tenant)
    if not knowledge_bases:
        sys.exit(f"no knowledge base for tenant {args.tenant}")
    knowledge_base = knowledge_bases[0]
    chunks = store.get_chunks(knowledge_base.id, "latest")
    print(
        f"{len(chunks)} chunks, {sum(len(chunk.text) for chunk in chunks)} chars | "
        f"embedding={settings.embedding_provider}/{settings.embedding_dimensions}d "
        f"dense={settings.dense_retrieval_backend} sparse={settings.sparse_retrieval_backend}"
    )

    service = RetrievalService(
        settings,
        TraceManager(settings),
        create_cache(settings),
        create_query_rewriter(settings),
        create_reranker(settings),
    )

    async def search(mode: str, rerank: bool, rewrite: bool) -> None:
        await service.search(
            tenant_id=args.tenant,
            knowledge_base_id=knowledge_base.id,
            question=args.question,
            chunks=chunks,
            top_k=args.top_k,
            mode=mode,
            document_version="latest",
            rerank=rerank,
            query_rewrite=rewrite,
        )

    async def probe(mode: str, rerank: bool, rewrite: bool, rounds: int) -> float:
        samples = []
        for _ in range(rounds):
            start = time.perf_counter()
            await search(mode, rerank, rewrite)
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        return samples[len(samples) // 2]

    for mode, rerank, rewrite in PROBES:
        label = f"{mode} rerank={rerank} rewrite={rewrite}"
        p50 = asyncio.run(probe(mode, rerank, rewrite, args.rounds))
        print(f"\n=== {label}: p50 {p50:.1f} ms ===")
        profiler = cProfile.Profile()
        profiler.enable()
        asyncio.run(probe(mode, rerank, rewrite, args.profile_rounds))
        profiler.disable()
        stats = pstats.Stats(profiler)
        stats.sort_stats("cumulative").print_stats(args.functions)


if __name__ == "__main__":
    main()
