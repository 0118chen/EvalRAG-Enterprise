"""Run the golden set through the real retrieval stack and report the metrics.

This is the offline counterpart of the evaluation API: it imports the corpus the
same way the worker does, binds the page-level golden set to the imported
document ids, and then runs several retrieval configurations through the same
EvaluationRunner the product uses. Retrieval metrics need no LLM, so the whole
run is deterministic and free.

Usage:
    python -m scripts.run_golden_experiment --database data/experiments/golden.db \
        --json docs/evaluation/golden-set-<stamp>.json
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from math import comb
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from app.core.ingestion import SUPPORTED_SUFFIXES
from scripts.golden_format import GoldenExample, load_golden, resolve_document_ids

ROOT = Path(__file__).resolve().parents[1]
LAW_DIR = ROOT / "law"
GOLDEN_EVAL = LAW_DIR / "golden_eval_v1.json"
TOP_K = 5

CONFIGS: list[dict[str, Any]] = [
    {
        "name": "sparse-bm25",
        "retrieval_mode": "sparse",
        "rerank": False,
        "query_rewrite": False,
        "isolates": "BM25 only, the no-frills baseline",
    },
    {
        "name": "sparse-bm25-rerank",
        "retrieval_mode": "sparse",
        "rerank": True,
        "query_rewrite": False,
        "isolates": "the reranking stage on top of BM25 alone",
    },
    {
        "name": "dense-hash",
        "retrieval_mode": "dense",
        "rerank": False,
        "query_rewrite": False,
        "isolates": "the dense channel on its own (32-d hash embedding, no semantics)",
    },
    {
        "name": "hybrid-rrf",
        "retrieval_mode": "hybrid",
        "rerank": False,
        "query_rewrite": False,
        "isolates": "RRF fusion of both channels, no rerank",
    },
    {
        "name": "hybrid-rrf-weighted",
        "retrieval_mode": "hybrid",
        "rerank": False,
        "query_rewrite": False,
        "fusion": "rrf-bm25-heavy",
        "isolates": "equal-weight fusion was the problem: weight the weak dense channel down",
    },
    {
        "name": "hybrid-rrf-truncate",
        "retrieval_mode": "hybrid",
        "rerank": False,
        "query_rewrite": False,
        "fusion": "rrf-truncate-5",
        "isolates": "only the top 5 of each channel may contribute, not its tail",
    },
    {
        "name": "hybrid-convex",
        "retrieval_mode": "hybrid",
        "rerank": False,
        "query_rewrite": False,
        "fusion": "convex",
        "isolates": "keep the score magnitudes RRF throws away (min-max normalised sum)",
    },
    {
        "name": "hybrid-convex-weighted",
        "retrieval_mode": "hybrid",
        "rerank": False,
        "query_rewrite": False,
        "fusion": "convex-bm25-heavy",
        "isolates": "normalised scores plus a down-weighted dense channel",
    },
    {
        "name": "hybrid-convex-weighted-rerank",
        "retrieval_mode": "hybrid",
        "rerank": True,
        "query_rewrite": False,
        "fusion": "convex-bm25-heavy",
        "isolates": (
            "the same shortlist through the reranker; which backend that is depends on "
            "RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing "
            "lexical against TypeSafe"
        ),
    },
    {
        "name": "hybrid-rrf-rerank",
        "retrieval_mode": "hybrid",
        "rerank": True,
        "query_rewrite": False,
        "isolates": "reranking on top of the fusion",
    },
    {
        "name": "hybrid-rrf-rerank-rewrite",
        "retrieval_mode": "hybrid",
        "rerank": True,
        "query_rewrite": True,
        "isolates": "rule-based query expansion on top of the product default",
    },
]


# TypeSafe's published price (https://docs.typesafe.ai/models): $42 per billion input tokens,
# output tokens free. The per-call token count is measured, not guessed: 427,239 input tokens
# over 400 calls on the v3 run (2026-09-28) = 1068 per call. It feeds the pre-flight estimate
# only - the report always carries the totals the API actually returned. (A first guess of 398
# came from a hand-written smoke prompt whose "candidates" were one short sentence each; real
# chunks are ~800 characters, i.e. ~2.7x more tokens.)
TYPESAFE_USD_PER_MTOK = 0.042
TYPESAFE_TOKENS_PER_CALL = 1068


def rerank_usage_totals(usage: list[dict]) -> dict[str, Any]:
    """Token and cost totals for the paid rerank backend (empty when it is not in use)."""
    input_tokens = sum(item["usage"].get("input_tokens", 0) for item in usage)
    output_tokens = sum(item["usage"].get("output_tokens", 0) for item in usage)
    return {
        "calls": len(usage),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": usage[0]["model"] if usage else None,
        "cost_usd": round(input_tokens / 1_000_000 * TYPESAFE_USD_PER_MTOK, 6),
        "price_note": "input tokens only, $42/Btok, https://docs.typesafe.ai/models",
    }


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):  # pragma: no cover - git unavailable
        return "unknown"


def stage_upload(document_id: str, filename: str, source_dir: Path = LAW_DIR) -> Path:
    """Place the raw file where the worker looks for it, using its own naming."""
    upload_dir = ROOT / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    target = upload_dir / f"{document_id}_{safe_name}"
    shutil.copyfile(source_dir / filename, target)
    return target


def ingest_corpus(
    store, tenant_id: str, kb_id: str, kb_name: str, corpus_dir: Path = LAW_DIR
) -> dict[str, str]:
    """Import every corpus file through the production worker function."""
    from app.schemas import Document, KnowledgeBase

    store.save_knowledge_base(
        KnowledgeBase(
            id=kb_id, tenant_id=tenant_id, name=kb_name, description="golden set experiment"
        )
    )
    by_filename: dict[str, str] = {}
    for path in sorted(corpus_dir.iterdir()):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        document_id = str(uuid4())
        stage_upload(document_id, path.name, corpus_dir)
        store.save_document(
            Document(
                id=document_id,
                filename=path.name,
                knowledge_base_id=kb_id,
                chunks=0,
                status="pending",
                version="latest",
            ),
            [],
        )
        by_filename[path.name] = document_id
    return by_filename


def index_corpus(document_ids: list[str]) -> None:
    """Index through the Celery task function itself, so parsing/chunking match production."""
    from app.tasks import process_document

    for document_id in document_ids:
        result = process_document(document_id)
        if result.get("status") != "ready":
            raise SystemExit(f"ingestion failed for {document_id}: {result}")


def build_dataset(
    store,
    tenant_id: str,
    kb_id: str,
    by_filename: dict[str, str],
    name: str,
    golden_path: Path,
    description: str,
):
    from app.schemas import EvaluationDataset, EvaluationExample, EvidenceSpan

    examples = load_golden(golden_path)
    resolve_document_ids(examples, by_filename)
    dataset_id = str(uuid4())
    prepared = []
    for example in examples:
        spans = [
            EvidenceSpan(
                document_id=by_filename[hop.source_filename],
                page=hop.page,
                quote=hop.quote,
            )
            for hop in example.hops
        ]
        prepared.append(
            EvaluationExample(
                id=str(uuid4()),
                dataset_id=dataset_id,
                question=example.question,
                expected_answer=example.expected_answer,
                # The primary label mirrors the first hop so a v2 example still shows a
                # document in the dashboard; the metrics read the full span list.
                expected_document_id=spans[0].document_id if spans else None,
                expected_page=spans[0].page if spans else None,
                evidence_quote=spans[0].quote if spans else None,
                category=example.category,
                should_refuse=example.should_refuse,
                evidence_mode=example.mode,
                expected_evidence=spans,
            )
        )
    dataset = EvaluationDataset(
        id=dataset_id,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        name=name,
        description=description,
        examples=prepared,
    )
    store.save_evaluation_dataset(dataset)
    return dataset, {example.question: example for example in examples}


def _probability_all_hops_hit(counts: list[int], total: int, draws: int) -> float:
    """P(every hop's document is among ``draws`` chunks) by inclusion-exclusion.

    The draws are without replacement from one chunk pool, so the hops are not
    independent; multiplying their individual hit rates would overstate a random
    baseline and make the retriever look better than it is.
    """
    from itertools import combinations

    probability_missing_some = 0.0
    for size in range(1, len(counts) + 1):
        for subset in combinations(counts, size):
            if sum(subset) > total:
                continue
            probability = (
                comb(total - sum(subset), draws) / comb(total, draws)
                if total - sum(subset) >= draws
                else 0.0
            )
            probability_missing_some += (-1) ** (size + 1) * probability
    return max(0.0, 1 - probability_missing_some)


def _probability_any_hop_hit(counts: list[int], total: int, draws: int) -> float:
    """P(at least one hop's document is drawn): the equivalence-question baseline."""
    from itertools import combinations

    probability_none = 1.0
    for size in range(1, len(counts) + 1):
        for subset in combinations(counts, size):
            if sum(subset) > total:
                continue
            probability = (
                comb(total - sum(subset), draws) / comb(total, draws)
                if total - sum(subset) >= draws
                else 0.0
            )
            probability_none += (-1) ** size * probability
    return max(0.0, min(1.0, 1 - probability_none))


def random_reference(
    store,
    kb_id: str,
    golden_by_question: dict[str, GoldenExample],
    by_filename: dict[str, str],
) -> dict[str, float]:
    """Document- and page-level hit rates a random 5-chunk draw would reach.

    Without this reference a 0.98 Recall@1 on 13 documents is unreadable: the
    interesting question is how much of it is the retriever and how much is the
    corpus being small. Multi-hop examples get the exact joint probability for their
    mode, and unanswerable ones are excluded - a random draw cannot succeed or fail them.
    """
    chunks = store.get_chunks(kb_id, "latest")
    total = len(chunks)
    per_document: dict[str, int] = {}
    per_page: dict[tuple[str, int], int] = {}
    for chunk in chunks:
        per_document[chunk.document_id] = per_document.get(chunk.document_id, 0) + 1
        per_page[(chunk.document_id, chunk.page)] = (
            per_page.get((chunk.document_id, chunk.page), 0) + 1
        )

    document_hits: list[float] = []
    page_hits: list[float] = []
    for example in golden_by_question.values():
        if example.should_refuse or not example.hops:
            continue
        document_counts = [
            per_document.get(by_filename[hop.source_filename], 0) for hop in example.hops
        ]
        page_counts = [
            per_page.get((by_filename[hop.source_filename], hop.page), 0)
            for hop in example.hops
        ]
        if total < TOP_K:
            document_hits.append(0.0)
            page_hits.append(0.0)
            continue
        if example.mode == "any":
            document_hits.append(_probability_any_hop_hit(document_counts, total, TOP_K))
            page_hits.append(_probability_any_hop_hit(page_counts, total, TOP_K))
        else:
            document_hits.append(_probability_all_hops_hit(document_counts, total, TOP_K))
            page_hits.append(_probability_all_hops_hit(page_counts, total, TOP_K))
    return {
        "random_document_hit_at_5": sum(document_hits) / max(len(document_hits), 1),
        "random_page_hit_at_5": sum(page_hits) / max(len(page_hits), 1),
        "chunks_per_document": round(total / max(len(per_document), 1), 1),
    }


def run_config(
    store, settings, retrieval_service, traces, tenant_id, kb_id, dataset, config
) -> dict:
    evaluation_id = str(uuid4())
    store.create_evaluation(
        evaluation_id,
        dataset.name,
        config["retrieval_mode"],
        TOP_K,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        dataset_id=dataset.id,
        experiment_name=config["name"],
        parameters={
            "document_version": "latest",
            "rerank": config["rerank"],
            "query_rewrite": config["query_rewrite"],
            "fusion": config.get("fusion"),
            "answer_evaluation": False,
        },
    )
    from app.core.evaluation_runner import EvaluationRunner

    asyncio.run(
        EvaluationRunner(
            settings=settings,
            store=store,
            retrieval_service=retrieval_service,
            traces=traces,
            langsmith=None,
            llm=None,
        ).run(evaluation_id)
    )
    evaluation = store.get_evaluation(evaluation_id)
    if evaluation["status"] != "completed":
        raise SystemExit(f"config {config['name']} ended as {evaluation['status']}")
    return evaluation["results"]


def _quote_found(example: GoldenExample, retrieved: list[dict]) -> bool:
    """Does the retrieved list hold the answering passage(s) under this example's mode?"""
    texts = [normalize(chunk.get("text", "")) for chunk in retrieved]
    hits = [any(normalize(quote) in text for text in texts) for quote in example.quotes]
    if not hits:
        return False
    return any(hits) if example.mode == "any" else all(hits)


def per_example(results: dict, golden_by_question: dict[str, GoldenExample]) -> list[dict]:
    """Per-question rows, so a headline number can be traced back to its questions.

    Chunk text stays out of here on purpose: the point is to make claims checkable
    without turning the result file into a copy of the corpus.
    """
    rows = []
    for item in results["examples"]:
        golden = golden_by_question[item["question"]]
        retrieved = item["retrieved"]
        rows.append(
            {
                "question": item["question"],
                "category": item["category"],
                "source_filename": (
                    golden.hops[0].source_filename if golden.hops else None
                ),
                "source_filenames": list(golden.source_filenames),
                "evidence_mode": golden.mode,
                "should_refuse": golden.should_refuse,
                "expected_page": item["expected_page"],
                "recall_at_1": item["metrics"].get("recall_at_1"),
                "recall_at_3": item["metrics"].get("recall_at_3"),
                "recall_at_5": item["metrics"].get("recall_at_5"),
                "all_targets_at_5": item["metrics"].get("all_targets_at_5"),
                "any_target_at_5": item["metrics"].get("any_target_at_5"),
                "page_hit": item["metrics"].get("page_hit"),
                "retrieved_something": item["metrics"].get("retrieved_something"),
                "quote_hit": _quote_found(golden, retrieved),
                "passage_rank": item.get("passage_rank"),
                "passage_ranks": item.get("passage_ranks"),
                "top_score": round(max((c["score"] for c in retrieved), default=0.0), 4),
                "latency_ms": round(item["latency_ms"], 1),
                "retrieved": [
                    {
                        "document_id": chunk["document_id"],
                        "page": chunk["page"],
                        "score": round(chunk["score"], 4),
                    }
                    for chunk in retrieved
                ],
            }
        )
    return rows


def evidence_stats(results: dict, golden_by_question: dict[str, GoldenExample]) -> dict[str, float]:
    """A retrieved chunk containing the ground-truth quote is the passage that answers it.

    Deliberately re-implemented here rather than read from the evaluation results: the
    product reports its own passage metrics, and two independent computations agreeing
    is the cheapest available check that neither is wrong.
    """
    answerable = refusal_found = refusal_total = 0
    exact = 0
    for item in results["examples"]:
        golden = golden_by_question[item["question"]]
        if golden.should_refuse:
            refusal_total += 1
            refusal_found += int(bool(item["retrieved"]))
            continue
        answerable += 1
        if _quote_found(golden, item["retrieved"]):
            exact += 1
    stats = {
        "evidence_quote_hit_rate": exact / max(answerable, 1),
        "answerable_examples": answerable,
    }
    if refusal_total:
        stats["negative_retrieved_rate"] = refusal_found / refusal_total
        stats["refusal_examples"] = refusal_total
    return stats


def category_table(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Per-category averages, with the refusal examples kept out of them.

    A question that has no answer in the corpus has no recall to average: folding it in
    would drag every category down and report a failure that did not happen. It gets its
    own row carrying only the signals that do mean something for it (whether anything was
    retrieved, and how high the top score went).
    """
    buckets: dict[str, list[dict]] = {}
    refusals: list[dict] = []
    for row in rows:
        if row["should_refuse"]:
            refusals.append(row)
            continue
        buckets.setdefault(row["category"], []).append(row)
    table: dict[str, dict[str, float]] = {}
    for category, items in sorted(buckets.items()):
        table[category] = {
            "examples": len(items),
            "recall_at_3": sum(i["recall_at_3"] or 0.0 for i in items) / len(items),
            "page_hit": sum(i["page_hit"] or 0.0 for i in items) / len(items),
            "quote_hit": sum(1.0 for i in items if i["quote_hit"]) / len(items),
        }
    if refusals:
        scores = sorted(i["top_score"] for i in refusals if i["top_score"] is not None)
        table["应拒答"] = {
            "examples": len(refusals),
            "recall_at_3": None,
            "page_hit": None,
            "quote_hit": None,
            "retrieved_rate": sum(1.0 for i in refusals if i["retrieved"]) / len(refusals),
            # statistics.median, not the middle element: with an even count the middle
            # element is the *upper* median and quietly overstates the score.
            "top_score_median": median(scores) if scores else None,
        }
    return table


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--configs",
        default=None,
        help=(
            "comma-separated subset of CONFIGS names to run (default: all). Needed for the "
            "paid rerank backends, where running the whole matrix would be 10x the cost."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt when the estimated TypeSafe spend is large",
    )
    parser.add_argument("--database", default="data/experiments/golden.db")
    parser.add_argument("--tenant", default="golden-experiment")
    parser.add_argument("--knowledge-base", default="rural-finance-regulations")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--keep-database", action="store_true")
    parser.add_argument(
        "--golden",
        type=Path,
        default=GOLDEN_EVAL,
        help="golden set to score: v1 (flat) or v2 (multi-hop, equivalence, refusals)",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=LAW_DIR,
        help=(
            "directory to import as the corpus (default: law/). The CI regression gate points "
            "this at tests/fixtures/eval_gate/corpus, so the gate runs on committed "
            "deterministic text instead of the untracked law/ documents."
        ),
    )
    args = parser.parse_args()
    golden_path = args.golden if args.golden.is_absolute() else ROOT / args.golden
    corpus_dir = args.corpus if args.corpus.is_absolute() else ROOT / args.corpus
    if not corpus_dir.is_dir():
        raise SystemExit(f"corpus directory not found: {corpus_dir}")

    # 先校验配置名：写错一个字母就该立刻失败，而不是等语料导入（约一分钟）跑完才报错。
    selected = CONFIGS
    if args.configs:
        wanted = [name.strip() for name in args.configs.split(",") if name.strip()]
        known = {config["name"] for config in CONFIGS}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise SystemExit(f"unknown config name(s): {unknown}; known: {sorted(known)}")
        selected = [config for config in CONFIGS if config["name"] in wanted]
        print(f"限定配置：{[config['name'] for config in selected]}\n")

    database = ROOT / args.database
    if database.exists() and not args.keep_database:
        database.unlink()
    database.parent.mkdir(parents=True, exist_ok=True)

    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ["CACHE_ENABLED"] = "false"
    os.environ["LANGSMITH_ENABLED"] = "false"

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
    kb_id = str(uuid4())
    by_filename = ingest_corpus(
        store, args.tenant, kb_id, args.knowledge_base, corpus_dir
    )
    print(f"staged {len(by_filename)} files, indexing ...")
    index_corpus(list(by_filename.values()))
    chunk_count = len(store.get_chunks(kb_id, "latest"))
    print(f"indexed {chunk_count} chunks across {len(by_filename)} documents")

    dataset, golden_by_question = build_dataset(
        store,
        args.tenant,
        kb_id,
        by_filename,
        f"{args.knowledge_base}-golden",
        golden_path,
        f"{len(load_golden(golden_path))} questions from {golden_path.name}",
    )
    print(f"dataset {dataset.name}: {len(dataset.examples)} examples\n")

    traces = TraceManager(settings)
    # 付费后端的 token 用量收集器：只有 RERANK_BACKEND/typesafe 会往里写，
    # 跑完写进报告，免得"花了多少钱"只能靠事后估。
    rerank_usage: list[dict] = []
    retrieval_service = RetrievalService(
        settings,
        traces,
        create_cache(settings),
        create_query_rewriter(settings),
        create_reranker(settings, usage_sink=rerank_usage),
    )

    # 一次 rerank 请求对应一个候选，所以"题数 x 候选数 x rerank 配置数"就是调用次数。
    rerank_configs = [config for config in selected if config["rerank"]]
    per_question = max(TOP_K, TOP_K * settings.retrieval_candidate_multiplier)
    expected_calls = len(dataset.examples) * per_question * len(rerank_configs)

    if settings.rerank_backend == "typesafe":
        # 付费调用先报账再动手：实测每次约 398 输入 token（scratch/ts_smoke.py 的真实调用），
        # 单价见 https://docs.typesafe.ai/models
        calls = expected_calls
        estimate = calls * TYPESAFE_TOKENS_PER_CALL * TYPESAFE_USD_PER_MTOK / 1_000_000
        print(
            f"TypeSafe 预估：{len(rerank_configs)} 个 rerank 配置 x {len(dataset.examples)} 题 x "
            f"{per_question} 候选 = {calls} 次请求，约 ${estimate:.4f}\n"
        )
        if estimate > 0.10 and not args.yes:
            answer = input(f"预计花费 ${estimate:.2f}，继续？[y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                raise SystemExit("已取消（要跳过确认加 --yes）")

    configs: list[dict[str, Any]] = []
    last_results: dict | None = None
    last_config: dict | None = None
    for config in selected:
        results = run_config(
            store, settings, retrieval_service, traces, args.tenant, kb_id, dataset, config
        )
        evidence = evidence_stats(results, golden_by_question)
        product_passage = results["metrics"].get("passage_hit")
        entry = {
            "name": config["name"],
            "isolates": config["isolates"],
            "retrieval_mode": config["retrieval_mode"],
            "rerank": config["rerank"],
            "query_rewrite": config["query_rewrite"],
            "fusion": config.get("fusion"),
            "top_k": TOP_K,
            "metrics": results["metrics"],
            **evidence,
            "passage_cross_check": product_passage is None
            or abs(product_passage - evidence["evidence_quote_hit_rate"]) < 1e-9,
            "per_example": per_example(results, golden_by_question),
        }
        configs.append(entry)
        last_results, last_config = results, entry
        metrics = results["metrics"]
        print(
            f"{config['name']:32} R@1={metrics.get('recall_at_1', 0):.3f} "
            f"R@3={metrics.get('recall_at_3', 0):.3f} R@5={metrics.get('recall_at_5', 0):.3f} "
            f"MRR={metrics.get('mrr', 0):.3f} nDCG@3={metrics.get('ndcg_at_3', 0):.3f} "
            f"page={metrics.get('page_hit', 0):.3f} passage@1={metrics.get('passage_at_1', 0):.3f} "
            f"all@5={metrics.get('all_targets_at_5', 0):.3f} "
            f"any@5={metrics.get('any_target_at_5', 0):.3f} "
            f"quote={entry['evidence_quote_hit_rate']:.3f} "
            f"p50={metrics.get('latency_ms_p50', 0):.1f}ms"
        )

    usage_totals = rerank_usage_totals(rerank_usage)
    # TypeSafe 失败会静默回退到词面重排，而回退不发请求——所以"实际调用数"必须和
    # "题数 x 候选数 x rerank 配置数"对得上；对不上就说明这批数字不是 TypeSafe 给的。
    usage_totals["expected_calls"] = expected_calls
    usage_totals["degraded"] = settings.rerank_backend == "typesafe" and (
        usage_totals["calls"] < expected_calls
    )
    if usage_totals["degraded"]:
        print(
            f"!!! 警告：TypeSafe 只发出 {usage_totals['calls']} 次请求（预期 {expected_calls}），"
            "说明部分问题回退到了词面重排——这批数字不能当成语义重排的结果\n"
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": git_commit(),
        "corpus": {
            "documents": len(by_filename),
            "chunks": chunk_count,
            "versions": ["latest"],
        },
        "dataset": {
            "name": dataset.name,
            "examples": len(dataset.examples),
            "source": (
                str(golden_path.relative_to(ROOT))
                if golden_path.is_relative_to(ROOT)
                else str(golden_path)
            ),
            "top_k": TOP_K,
        },
        "rerank_backend": settings.rerank_backend,
        "rerank_usage": usage_totals,
        "embedding": {
            "provider": settings.embedding_provider,
            "dimensions": settings.embedding_dimensions,
            "dense_backend": settings.dense_retrieval_backend,
            "sparse_backend": settings.sparse_retrieval_backend,
        },
        "configs": configs,
        "reference": random_reference(store, kb_id, golden_by_question, by_filename),
        "by_category": (
            {last_config["name"]: category_table(last_config["per_example"])}
            if last_results and last_config
            else {}
        ),
    }

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {args.markdown}")


def render_markdown(report: dict[str, Any]) -> str:
    columns = [
        ("Recall@1", "recall_at_1"),
        ("Recall@3", "recall_at_3"),
        ("Recall@5", "recall_at_5"),
        ("MRR", "mrr"),
        ("nDCG@3", "ndcg_at_3"),
        ("nDCG@5", "ndcg_at_5"),
        ("page_hit", "page_hit"),
        ("passage@1", "passage_at_1"),
        ("passage_mrr", "passage_mrr"),
        ("all@5", "all_targets_at_5"),
        ("any@5", "any_target_at_5"),
        ("neg_retrieved", "negative_retrieved_rate"),
        ("quote_hit", "evidence_quote_hit_rate"),
        ("p50 ms", "latency_ms_p50"),
        ("p95 ms", "latency_ms_p95"),
    ]
    lines = [
        (
            f"# Golden set experiment ({report['dataset']['examples']} questions, "
            f"{report['corpus']['documents']} documents, {report['corpus']['chunks']} chunks)"
        ),
        "",
        f"- commit: `{report['git_commit']}`  ",
        f"- generated: {report['generated_at']}  ",
        (
            f"- embedding: {report['embedding']['provider']} "
            f"({report['embedding']['dimensions']}d), dense={report['embedding']['dense_backend']}, "
            f"sparse={report['embedding']['sparse_backend']}  "
        ),
        f"- top_k: {report['dataset']['top_k']}  ",
        f"- rerank backend: `{report['rerank_backend']}`"
        + (
            f" (TypeSafe, {report['rerank_usage']['calls']}/{report['rerank_usage']['expected_calls']} "
            f"calls, {report['rerank_usage']['input_tokens']} input tokens, "
            f"${report['rerank_usage']['cost_usd']}"
            + ("，**有回退，数字不可信**" if report["rerank_usage"]["degraded"] else "")
            + ")  "
            if report["rerank_usage"]["calls"]
            else "  "
        ),
        (
            f"- random 5-chunk draw: document hit {report['reference']['random_document_hit_at_5']:.3f}, "
            f"page hit {report['reference']['random_page_hit_at_5']:.3f} "
            f"({report['reference']['chunks_per_document']} chunks/document)"
        ),
        "",
        "| config | what it isolates | " + " | ".join(label for label, _ in columns) + " |",
        "|" + "---|" * (len(columns) + 2),
    ]
    for config in report["configs"]:
        values = []
        for _label, key in columns:
            value = config.get(key, config["metrics"].get(key))
            values.append("—" if value is None else f"{value:.3f}")
        lines.append(f"| {config['name']} | {config['isolates']} | " + " | ".join(values) + " |")

    lines += [
        "",
        "## Per category (product default configuration)",
        "",
        "| category | examples | Recall@3 | page_hit | quote_hit | notes |",
        "|---|---|---|---|---|---|",
    ]
    for table in report["by_category"].values():
        for category, row in table.items():
            values = [
                "—" if row.get(key) is None else f"{row[key]:.3f}"
                for key in ("recall_at_3", "page_hit", "quote_hit")
            ]
            note = ""
            if row.get("top_score_median") is not None:
                note = (
                    f"无检索指标；检索到内容的比例 {row['retrieved_rate']:.3f}，"
                    f"top 分数中位数 {row['top_score_median']:.1f}"
                )
            lines.append(
                f"| {category} | {row['examples']} | " + " | ".join(values) + f" | {note} |"
            )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
