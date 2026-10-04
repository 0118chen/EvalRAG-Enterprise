"""Does the LLM judge actually notice a planted fabrication?

`judge_agreement.py` measures whether the judge and a human agree on answers the
system produced. On an easy fixture they agree on everything, which proves little.
This script perturbs the answers instead: it takes the answers that scored 1.0,
appends one plausible-looking rule that the evidence does not contain, and asks the
same judge to score them again. A judge that separates "wrong" from "unfaithful"
drops faithfulness while holding correctness; a judge that only checks whether an
answer was produced drops all three together.

Usage:

    python -m scripts.judge_calibration --artifact docs/evaluation/answer-quality-eval-gate-2026-10-05.json \
        --json docs/evaluation/judge-calibration-2026-10-05.json
    python -m scripts.judge_agreement --artifact docs/evaluation/judge-calibration-2026-10-05.json \
        --dump-sample docs/evaluation/judge-calibration-labels-2026-10-05.json

The output uses the artifact shape `judge_agreement.py` reads (a `configs` list with
`per_example` rows), so the second command works unchanged.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.answer_evaluation import judge_answer
from app.core.ingestion import Chunk
from app.core.llm import create_llm
from scripts.judge_agreement import load_rows

CONFIG_NAME = "calibration-fabrication"
DEFAULT_SAMPLE = 16
DEFAULT_SEED = 20261005

# Plausible rules that appear nowhere in the fixture corpus: right register, wrong facts.
FABRICATIONS = (
    "此外，根据第二十五条，借款人逾期后应当按日加收千分之五的违约金。",
    "另外，第二十六条要求每笔借款均须提供两名公职人员的书面担保。",
    "补充一点，第三十三条规定的成员首次出资额不得低于人民币一千元。",
    "另外，第三十七条明确互助资金的年度管理费率不得超过百分之三。",
)


def answerable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows this probe can perturb: an expected answer, a produced answer, and evidence."""
    return [
        row
        for row in rows
        if row.get("expected_answer")
        and (row.get("generated_answer") or "").strip()
        and row.get("evidence")
        and not row.get("should_refuse")
    ]


def plant(answer: str, fabrication: str) -> str:
    return f"{answer.rstrip()}\n{fabrication}"


def evidence_pairs(row: dict[str, Any]) -> list[tuple[Chunk, float]]:
    return [
        (
            Chunk(
                id=f"{CONFIG_NAME}-{index}",
                document_id=str(chunk["document_id"]),
                page=int(chunk["page"]),
                text=str(chunk["text"]),
            ),
            float(chunk["score"]),
        )
        for index, chunk in enumerate(row["evidence"])
    ]


def build_plan(
    rows: list[dict[str, Any]], *, sample: int, seed: int, fabrications: tuple[str, ...]
) -> list[tuple[dict[str, Any], str]]:
    """Pick rows deterministically and pair each with a fabrication (round-robin)."""
    pool = answerable_rows(rows)
    chosen = random.Random(seed).sample(pool, min(sample, len(pool)))
    return [(row, fabrications[index % len(fabrications)]) for index, row in enumerate(chosen)]


async def calibrate(
    plan: list[tuple[dict[str, Any], str]], *, llm: Any
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row, fabrication in plan:
        answer = plant(str(row.get("generated_answer") or ""), fabrication)
        score = await judge_answer(
            llm,
            question=str(row["question"]),
            expected_answer=str(row["expected_answer"]),
            generated_answer=answer,
            evidence=evidence_pairs(row),
        )
        entries.append(
            {
                # No "config" key: judge_agreement.load_rows() tags rows with the config name
                # from the `configs` list, and a per-row key would overwrite it.
                "source_config": row["config"],
                "question": row["question"],
                "category": row.get("category"),
                "should_refuse": False,
                "expected_answer": row.get("expected_answer"),
                "generated_answer": answer,
                "evidence": row.get("evidence") or [],
                "judge": {
                    "correctness": score.correctness,
                    "faithfulness": score.faithfulness,
                    "completeness": score.completeness,
                    "reason": score.reason,
                },
                "fabrication": fabrication,
                "original_judge": row.get("judge"),
            }
        )
    return entries


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """How the judge reacted to one fabricated rule per row.

    `fabrications_flagged` is the metric that matters: the answer's own fact was accepted
    (correctness >= 0.5) and the planted claim was still rejected (faithfulness < 0.5).
    Counting raw low faithfulness would credit the judge for rows it scored 0 for unrelated
    reasons, e.g. an answer that refused the question.
    """
    if not entries:
        return {"rows": 0}

    def mean(key: str) -> float:
        return round(sum(float(entry["judge"][key]) for entry in entries) / len(entries), 4)

    def correct(entry: dict[str, Any]) -> bool:
        return float(entry["judge"]["correctness"]) >= 0.5

    def faithfulness(entry: dict[str, Any]) -> float:
        return float(entry["judge"]["faithfulness"])

    collapsed = [
        entry
        for entry in entries
        if len({entry["judge"][key] for key in ("correctness", "faithfulness", "completeness")})
        == 1
    ]
    return {
        "rows": len(entries),
        "mean_correctness": mean("correctness"),
        "mean_faithfulness": mean("faithfulness"),
        "mean_completeness": mean("completeness"),
        "correctness_kept": sum(1 for entry in entries if correct(entry)),
        "faithfulness_full": sum(1 for entry in entries if faithfulness(entry) >= 1.0),
        "faithfulness_partial": sum(
            1 for entry in entries if 0.5 <= faithfulness(entry) < 1.0
        ),
        "faithfulness_clearly_low": sum(1 for entry in entries if faithfulness(entry) < 0.5),
        "fabrications_flagged": sum(
            1 for entry in entries if correct(entry) and faithfulness(entry) < 0.5
        ),
        "all_three_equal": len(collapsed),
    }


def write_artifact(
    entries: list[dict[str, Any]],
    path: Path,
    *,
    source: Path,
    seed: int,
    summary: dict[str, Any],
) -> None:
    # One config per source config: the same question is perturbed under several retrieval
    # configurations, and judge_agreement.py keys a label by (config, question).
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(str(entry.get("source_config") or "unknown"), []).append(entry)
    payload = {
        "generated_by": "scripts/judge_calibration.py",
        "source_artifact": str(source),
        "note": (
            "Answers that scored well in source_artifact, with one fabricated rule appended. "
            "The fabricated sentence is per row in 'fabrication'; a faithful judge lowers "
            "faithfulness."
        ),
        "seed": seed,
        "summary": summary,
        "configs": [
            {"name": f"{CONFIG_NAME}/{name}", "per_example": items}
            for name, items in sorted(groups.items())
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_report(entries: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    for entry in entries:
        judge = entry["judge"]
        question = str(entry["question"])[:34]
        print(
            f"c={judge['correctness']:<4} f={judge['faithfulness']:<4} "
            f"m={judge['completeness']:<4} | {question} | {judge['reason'][:70]}"
        )
    print(json.dumps(summary, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, help="answer-evaluated artifact to perturb")
    parser.add_argument("--json", help="write the calibration artifact here")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="rows to perturb")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed")
    parser.add_argument("--fabrication", help="use this fabrication for every row")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the planted answers without calling the LLM"
    )
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.artifact))
    fabrications = (args.fabrication,) if args.fabrication else FABRICATIONS
    plan = build_plan(rows, sample=args.sample, seed=args.seed, fabrications=fabrications)
    if not plan:
        print("no answerable rows with evidence to perturb", file=sys.stderr)
        return 1

    if args.dry_run:
        for row, fabrication in plan:
            print(f"--- {row['question']}\n{plant(str(row.get('generated_answer') or ''), fabrication)}")
        print(f"would judge {len(plan)} row(s)")
        return 0

    llm = create_llm(get_settings())
    entries = asyncio.run(calibrate(plan, llm=llm))
    summary = summarize(entries)
    print_report(entries, summary)
    if args.json:
        write_artifact(
            entries, Path(args.json), source=Path(args.artifact), seed=args.seed, summary=summary
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
