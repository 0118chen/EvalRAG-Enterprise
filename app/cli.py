"""Operational CLI for evaluation experiments and LangSmith dataset sync."""

import argparse
import asyncio

from app.container import build_container
from app.core.evaluation_runner import EvaluationRunner
from app.core.langsmith_eval import dataset_example
from app.core.llm import create_llm
from app.tasks import process_document, replay_index_outbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evalrag")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-evaluation")
    run_parser.add_argument("evaluation_id")

    sync_parser = subparsers.add_parser("sync-dataset")
    sync_parser.add_argument("dataset_id")
    sync_parser.add_argument("tenant_id")

    subparsers.add_parser("check-llm")
    subparsers.add_parser("check-langsmith")
    reindex_parser = subparsers.add_parser(
        "reindex-document",
        help="reprocess one uploaded document; --force rebuilds an already indexed one",
    )
    reindex_parser.add_argument("document_id")
    reindex_parser.add_argument("--force", action="store_true")
    replay_parser = subparsers.add_parser(
        "replay-index-outbox",
        help="retry the external index writes that never completed; safe to run on a timer",
    )
    replay_parser.add_argument("--limit", type=int, default=10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    container = build_container()
    if args.command == "reindex-document":
        function = process_document.run if hasattr(process_document, "run") else process_document
        result = function(args.document_id, force=args.force)
        print(f"{result['document_id']} {result['status']} {result['stage']}")
        if result.get("stage") == "already_indexed" and not args.force:
            print("document is already indexed; pass --force to rebuild it")
        return
    if args.command == "replay-index-outbox":
        function = (
            replay_index_outbox.run
            if hasattr(replay_index_outbox, "run")
            else replay_index_outbox
        )
        result = function(args.limit)
        print(f"replayed={result['replayed']} failed={result['failed']}")
        return
    if args.command == "run-evaluation":
        runner = EvaluationRunner(
            settings=container.settings,
            store=container.store,
            retrieval_service=container.retrieval,
            traces=container.traces,
            langsmith=container.langsmith,
        )
        result = asyncio.run(runner.run(args.evaluation_id))
        print(result["status"])
        return

    if args.command == "check-llm":
        if container.settings.llm_provider == "mock":
            print("mock: LLM is not configured")
            return
        answer = asyncio.run(
            create_llm(container.settings).answer(
                "请仅回复：连接成功",
                "这是一次 LLM 连接测试。",
            )
        )
        print(answer[:200])
        return

    if args.command == "check-langsmith":
        result = container.langsmith.health()
        print(
            f"status={result['status']} "
            f"connected={result['connected']} "
            f"project={result['project']}"
        )
        return

    dataset = container.store.get_evaluation_dataset(args.dataset_id, args.tenant_id)
    if not dataset:
        raise SystemExit("dataset not found")
    remote_id = container.langsmith.ensure_dataset(
        dataset.name,
        dataset.description,
        tenant_id=dataset.tenant_id,
    )
    if not remote_id:
        raise SystemExit("LangSmith is disabled or unavailable")
    created = container.langsmith.create_examples(
        remote_id,
        [
            dataset_example(
                example.question,
                dataset.knowledge_base_id,
                tenant_id=dataset.tenant_id,
                category=example.category,
                expected_document_id=example.expected_document_id,
                reference={
                    "expected_page": example.expected_page,
                    "expected_answer": example.expected_answer,
                },
            )
            for example in dataset.examples
        ],
    )
    print(f"synced {len(created)} examples to {remote_id}")


if __name__ == "__main__":
    main()
