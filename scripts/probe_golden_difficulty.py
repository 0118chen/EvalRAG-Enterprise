"""Why does the golden set saturate? Two probes with measurable answers.

A. Rank granularity. Document-level recall hides where the answering chunk actually
   lands. Counting the position of the ground-truth evidence quote shows how much
   discrimination the aggregate throws away.

B. Lexical leakage. Most questions name their source law, and that full title appears
   verbatim in the document body, which is a near-unique lexical cue. Re-running the
   same retrieval without the 《...》 span measures how much of the score comes from it.

Usage:
    python -m scripts.probe_golden_difficulty --database data/experiments/golden.db \
        --tenant golden-experiment
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_EVAL = ROOT / "law" / "golden_eval_v1.json"
TITLE = re.compile(r"《[^》]{2,40}》")


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/experiments/golden.db")
    parser.add_argument("--tenant", default="golden-experiment")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--config",
        default="sparse-bm25-rerank",
        help="stored experiment whose questions and labels drive the probes",
    )
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
    from app.core.evaluation import RetrievalExample, metrics_at_k
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
    quotes = {
        example["question"]: example["evidence_quote"]
        for example in json.loads(GOLDEN_EVAL.read_text(encoding="utf-8"))
    }
    stored = {
        evaluation["experiment_name"]: store.get_evaluation(evaluation["id"])["results"]
        for evaluation in store.list_evaluations(args.tenant)
    }
    if args.config not in stored:
        sys.exit(f"no stored experiment named {args.config}; have {sorted(stored)}")
    reference = stored[args.config]["examples"]

    print("=== A. position of the evidence quote in the top-k ===")
    print(f"{'config':<34}{'rank1':>7}{'rank2-5':>9}{'absent':>8}{'passage@1':>11}")
    for name, results in stored.items():
        positions = []
        for item in results["examples"]:
            quote = normalize(quotes[item["question"]])
            position = next(
                (
                    index
                    for index, chunk in enumerate(item["retrieved"], start=1)
                    if quote in normalize(chunk.get("text", ""))
                ),
                None,
            )
            positions.append(position or 0)
        rank1 = positions.count(1)
        middle = sum(1 for position in positions if 2 <= position <= args.top_k)
        absent = sum(1 for position in positions if position == 0)
        print(f"{name:<34}{rank1:>7}{middle:>9}{absent:>8}{rank1 / len(positions):>11.3f}")

    print("\n=== B. same retrieval with the law title removed from the question ===")
    service = RetrievalService(
        settings,
        TraceManager(settings),
        create_cache(settings),
        create_query_rewriter(settings),
        create_reranker(settings),
    )

    async def evaluate(questions: list[str], mode: str, rerank: bool) -> dict[str, float]:
        totals: dict[str, list[float]] = {}
        page_hits = 0
        quote_hits = 0
        for item, question in zip(reference, questions, strict=True):
            result = await service.search(
                tenant_id=args.tenant,
                knowledge_base_id=knowledge_base.id,
                question=question,
                chunks=chunks,
                top_k=args.top_k,
                mode=mode,
                document_version="latest",
                rerank=rerank,
                query_rewrite=False,
            )
            metric_input = RetrievalExample(
                question=item["question"],
                expected_document_id=item["expected_document_id"],
                retrieved_document_ids=[chunk.document_id for chunk, _ in result.results],
            )
            for key, value in metrics_at_k(metric_input, (1, 3, 5)).items():
                totals.setdefault(key, []).append(value)
            if any(
                chunk.document_id == item["expected_document_id"]
                and chunk.page == item["expected_page"]
                for chunk, _ in result.results
            ):
                page_hits += 1
            quote = normalize(quotes[item["question"]])
            if any(quote in normalize(chunk.text) for chunk, _ in result.results):
                quote_hits += 1
        count = len(reference)
        summary = {key: sum(values) / len(values) for key, values in totals.items()}
        summary["page_hit"] = page_hits / count
        summary["quote_hit"] = quote_hits / count
        return summary

    original = [item["question"] for item in reference]
    stripped = [TITLE.sub("", question).strip() for question in original]
    print(
        f"questions containing a law title: {sum(a != b for a, b in zip(original, stripped, strict=True))}/{len(original)}"
    )
    keys = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr", "page_hit", "quote_hit")
    for label, questions in (("original", original), ("title-removed", stripped)):
        for mode, rerank in (("sparse", True), ("sparse", False)):
            summary = asyncio.run(evaluate(questions, mode, rerank))
            variant = f"{mode}{'+rerank' if rerank else ''}"
            print(
                f"{label:<14}{variant:<12}" + "  ".join(f"{key}={summary[key]:.3f}" for key in keys)
            )


if __name__ == "__main__":
    main()
