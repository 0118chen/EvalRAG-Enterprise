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
from pathlib import Path
from typing import Any
from uuid import uuid4

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


def stage_upload(document_id: str, filename: str) -> Path:
    """Place the raw file where the worker looks for it, using its own naming."""
    upload_dir = ROOT / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    target = upload_dir / f"{document_id}_{safe_name}"
    shutil.copyfile(LAW_DIR / filename, target)
    return target


def ingest_corpus(store, tenant_id: str, kb_id: str, kb_name: str) -> dict[str, str]:
    """Import every corpus file through the production worker function."""
    from app.schemas import Document, KnowledgeBase

    store.save_knowledge_base(
        KnowledgeBase(
            id=kb_id, tenant_id=tenant_id, name=kb_name, description="golden set experiment"
        )
    )
    by_filename: dict[str, str] = {}
    for path in sorted(LAW_DIR.iterdir()):
        if path.suffix.lower() not in {".pdf", ".docx"}:
            continue
        document_id = str(uuid4())
        stage_upload(document_id, path.name)
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


def build_dataset(store, tenant_id: str, kb_id: str, by_filename: dict[str, str], name: str):
    from app.schemas import EvaluationDataset, EvaluationExample

    golden = json.loads(GOLDEN_EVAL.read_text(encoding="utf-8"))
    missing = sorted({e["source_filename"] for e in golden} - set(by_filename))
    if missing:
        raise SystemExit(f"golden set references files that are not in the corpus: {missing}")
    dataset_id = str(uuid4())
    dataset = EvaluationDataset(
        id=dataset_id,
        tenant_id=tenant_id,
        knowledge_base_id=kb_id,
        name=name,
        description="52 page-level questions over 13 national rural finance regulations",
        examples=[
            EvaluationExample(
                id=str(uuid4()),
                dataset_id=dataset_id,
                question=example["question"],
                expected_answer=example["expected_answer"],
                expected_document_id=by_filename[example["source_filename"]],
                expected_page=example["page"],
                category=example["category"],
            )
            for example in golden
        ],
    )
    store.save_evaluation_dataset(dataset)
    return dataset, {e["question"]: e for e in golden}


def random_reference(
    store, kb_id: str, golden_by_question: dict[str, dict], by_filename: dict[str, str]
) -> dict[str, float]:
    """Document- and page-level hit rates a random 5-chunk draw would reach.

    Without this reference a 0.98 Recall@1 on 13 documents is unreadable: the
    interesting question is how much of it is the retriever and how much is the
    corpus being small.
    """
    from math import comb

    chunks = store.get_chunks(kb_id, "latest")
    total = len(chunks)
    per_document: dict[str, int] = {}
    per_page: dict[tuple[str, int], int] = {}
    for chunk in chunks:
        per_document[chunk.document_id] = per_document.get(chunk.document_id, 0) + 1
        per_page[(chunk.document_id, chunk.page)] = (
            per_page.get((chunk.document_id, chunk.page), 0) + 1
        )

    def hit(relevant: int) -> float:
        if relevant <= 0 or total < TOP_K:
            return 0.0
        return 1 - comb(total - relevant, TOP_K) / comb(total, TOP_K)

    document_hits = []
    page_hits = []
    for example in golden_by_question.values():
        document_id = by_filename[example["source_filename"]]
        document_hits.append(hit(per_document.get(document_id, 0)))
        page_hits.append(hit(per_page.get((document_id, example["page"]), 0)))
    return {
        "random_document_hit_at_5": sum(document_hits) / len(document_hits),
        "random_page_hit_at_5": sum(page_hits) / len(page_hits),
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


def per_example(results: dict, golden_by_question: dict[str, dict]) -> list[dict]:
    """Per-question rows, so a headline number can be traced back to its questions.

    Chunk text stays out of here on purpose: the point is to make claims checkable
    without turning the result file into a copy of the corpus.
    """
    rows = []
    for item in results["examples"]:
        golden = golden_by_question[item["question"]]
        quote = normalize(golden["evidence_quote"])
        retrieved = item["retrieved"]
        rows.append(
            {
                "question": item["question"],
                "category": item["category"],
                "source_filename": golden["source_filename"],
                "expected_page": item["expected_page"],
                "recall_at_1": item["metrics"].get("recall_at_1"),
                "recall_at_5": item["metrics"].get("recall_at_5"),
                "page_hit": item["metrics"].get("page_hit"),
                "quote_hit": any(quote in normalize(chunk.get("text", "")) for chunk in retrieved),
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


def evidence_stats(results: dict, golden_by_question: dict[str, dict]) -> dict[str, float]:
    """A retrieved chunk containing the ground-truth quote is the passage that answers it."""
    exact = 0
    on_page = 0
    for item in results["examples"]:
        quote = normalize(golden_by_question[item["question"]]["evidence_quote"])
        retrieved = item["retrieved"]
        if any(quote in normalize(chunk.get("text", "")) for chunk in retrieved):
            exact += 1
        if any(
            chunk["document_id"] == item["expected_document_id"]
            and chunk["page"] == item["expected_page"]
            for chunk in retrieved
        ):
            on_page += 1
    total = len(results["examples"])
    return {
        "evidence_quote_hit_rate": exact / total,
        "expected_page_retrieved_rate": on_page / total,
    }


def category_table(
    results: dict, golden_by_question: dict[str, dict]
) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[dict]] = {}
    for item in results["examples"]:
        buckets.setdefault(item["category"], []).append(item)
    table = {}
    for category, items in sorted(buckets.items()):
        table[category] = {
            "examples": len(items),
            "recall_at_3": sum(i["metrics"].get("recall_at_3", 0.0) for i in items) / len(items),
            "page_hit": sum(i["metrics"].get("page_hit", 0.0) for i in items) / len(items),
        }
    return table


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/experiments/golden.db")
    parser.add_argument("--tenant", default="golden-experiment")
    parser.add_argument("--knowledge-base", default="rural-finance-regulations")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--keep-database", action="store_true")
    args = parser.parse_args()

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
    by_filename = ingest_corpus(store, args.tenant, kb_id, args.knowledge_base)
    print(f"staged {len(by_filename)} files, indexing ...")
    index_corpus(list(by_filename.values()))
    chunk_count = len(store.get_chunks(kb_id, "latest"))
    print(f"indexed {chunk_count} chunks across {len(by_filename)} documents")

    dataset, golden_by_question = build_dataset(
        store, args.tenant, kb_id, by_filename, f"{args.knowledge_base}-golden"
    )
    print(f"dataset {dataset.name}: {len(dataset.examples)} examples\n")

    traces = TraceManager(settings)
    retrieval_service = RetrievalService(
        settings,
        traces,
        create_cache(settings),
        create_query_rewriter(settings),
        create_reranker(settings),
    )

    configs: list[dict[str, Any]] = []
    last_results: dict | None = None
    last_config: dict | None = None
    for config in CONFIGS:
        results = run_config(
            store, settings, retrieval_service, traces, args.tenant, kb_id, dataset, config
        )
        evidence = evidence_stats(results, golden_by_question)
        entry = {
            "name": config["name"],
            "isolates": config["isolates"],
            "retrieval_mode": config["retrieval_mode"],
            "rerank": config["rerank"],
            "query_rewrite": config["query_rewrite"],
            "top_k": TOP_K,
            "metrics": results["metrics"],
            **evidence,
            "per_example": per_example(results, golden_by_question),
        }
        configs.append(entry)
        last_results, last_config = results, config
        metrics = results["metrics"]
        print(
            f"{config['name']:32} R@1={metrics.get('recall_at_1', 0):.3f} "
            f"R@3={metrics.get('recall_at_3', 0):.3f} R@5={metrics.get('recall_at_5', 0):.3f} "
            f"MRR={metrics.get('mrr', 0):.3f} nDCG@3={metrics.get('ndcg_at_3', 0):.3f} "
            f"page={metrics.get('page_hit', 0):.3f} quote={entry['evidence_quote_hit_rate']:.3f} "
            f"p50={metrics.get('latency_ms_p50', 0):.1f}ms"
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
            "source": str(GOLDEN_EVAL.relative_to(ROOT)),
            "top_k": TOP_K,
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "dimensions": settings.embedding_dimensions,
            "dense_backend": settings.dense_retrieval_backend,
            "sparse_backend": settings.sparse_retrieval_backend,
        },
        "configs": configs,
        "reference": random_reference(store, kb_id, golden_by_question, by_filename),
        "by_category": (
            {last_config["name"]: category_table(last_results, golden_by_question)}
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
        "| category | examples | Recall@3 | page_hit |",
        "|---|---|---|---|",
    ]
    for table in report["by_category"].values():
        for category, row in table.items():
            lines.append(
                f"| {category} | {row['examples']} | {row['recall_at_3']:.3f} | {row['page_hit']:.3f} |"
            )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
