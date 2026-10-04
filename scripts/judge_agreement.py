"""Cohen's kappa between the LLM judge and a human review of the same answers.

The judge in `app/core/answer_evaluation.py` is the same model that wrote the answer, and
nothing in the pipeline would notice if it were simply agreeable: `answer_correctness`
would look great while the answers were wrong. This script takes an answer-evaluated
experiment artifact plus a hand-labelled sample of its rows and reports raw agreement,
Cohen's kappa with a bootstrap interval, the confusion matrix, and every row where the two
raters disagree.

Workflow:

    python -m scripts.run_golden_experiment --answer-evaluation --json run.json ...
    python -m scripts.judge_agreement --artifact run.json --dump-sample sample.json
    # fill in correctness/faithfulness/completeness (0..1) for every row of sample.json
    python -m scripts.judge_agreement --artifact run.json --labels labels.json

Kappa is reported with a bootstrap interval because the sample is small: with 32 rows the
point estimate moves by more than 0.1 between resamples, and a bare "kappa = 0.6" hides
that. Rows are matched on (config, question); a duplicate key is a hard error rather than a
silent zip, because the golden sets have contained duplicate questions before.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
METRICS = ("correctness", "faithfulness", "completeness")
DEFAULT_THRESHOLD = 0.5
DEFAULT_ITERATIONS = 2000
DEFAULT_SEED = 20261005
DEFAULT_CONFIDENCE = 0.95
MIN_RESAMPLES = 50


def _read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_rows(artifact: Path | str) -> list[dict[str, Any]]:
    """Every `per_example` row of every config, tagged with the config it came from."""
    payload = _read_json(artifact)
    if not isinstance(payload, dict) or "configs" not in payload:
        raise SystemExit(f"{artifact} is not an experiment artifact (no 'configs' key)")
    rows: list[dict[str, Any]] = []
    for config in payload["configs"]:
        for item in config.get("per_example") or []:
            rows.append({"config": config["name"], **item})
    if not rows:
        raise SystemExit(f"{artifact} has no per_example rows to score")
    return rows


def load_labels(path: Path | str) -> tuple[str, str, list[dict[str, Any]]]:
    payload = _read_json(path)
    if isinstance(payload, list):
        return "unknown", "", payload
    if not isinstance(payload, dict) or "labels" not in payload:
        raise SystemExit(f"{path} is not a label file (expected {{'labels': [...]}})")
    return (
        str(payload.get("labeller", "unknown")),
        str(payload.get("note", "")),
        list(payload["labels"]),
    )


def dump_sample(
    rows: Sequence[dict[str, Any]],
    path: Path | str,
    *,
    metrics: Iterable[str] = METRICS,
) -> int:
    """Write a label template: everything a reviewer needs, nothing they have to fetch.

    Includes the judge's score and reason so the reviewer can disagree with it, the
    generated answer, the expected answer, and the evidence excerpts the judge was shown.
    That is also a limitation worth knowing when reading the kappa: the reviewer sees the
    same evidence, so "faithfulness" agreement cannot catch a judge that misreads it.
    """
    sample = []
    for row in rows:
        judge = row.get("judge") or {}
        entry = {
            "config": row["config"],
            "question": row["question"],
            "category": row.get("category"),
            "should_refuse": row.get("should_refuse"),
            "expected_answer": row.get("expected_answer"),
            "generated_answer": row.get("generated_answer"),
            "evidence": row.get("evidence") or [],
            "judge": {metric: judge.get(metric) for metric in (*metrics, "reason")},
        }
        for metric in metrics:
            entry[metric] = None
        sample.append(entry)
    payload = {
        "instructions": (
            "Score each row 0..1 independently of the 'judge' block, then run "
            "judge_agreement.py --labels this-file. correctness = does the generated "
            "answer state the same fact as expected_answer; faithfulness = is every claim "
            "supported by 'evidence'; completeness = does it cover all parts of the "
            "question. Leave a metric as null to exclude the row from that metric."
        ),
        "labeller": "",
        "note": "",
        "labels": sample,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(sample)


def _one_score(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    raise SystemExit(f"{where} must be a number between 0 and 1 or null, got {value!r}")


def pair_up(
    rows: Sequence[dict[str, Any]], labels: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Match labels to rows by (config, question); report labels with no row."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault((row["config"], row["question"]), []).append(row)
    pairs: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for label in labels:
        key = (str(label.get("config", "")), str(label.get("question", "")))
        candidates = by_key.get(key)
        if not candidates:
            unmatched.append(f"{key[0]} | {key[1]}")
            continue
        if len(candidates) > 1:
            raise SystemExit(
                f"ambiguous label target: {key[0]} asks {key[1]!r} "
                f"{len(candidates)} times; key the label file by row instead"
            )
        pairs.append({"row": candidates[0], "label": label})
    return pairs, unmatched


def cohens_kappa(
    pairs: Sequence[dict[str, Any]],
    metric: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Kappa for one metric over the rows both raters scored.

    Counts rows the judge left unscored (a timed-out or failed example) and rows the
    reviewer left null, rather than treating either as a zero: a missing measurement is
    not a wrong answer.
    """
    scored: list[tuple[float, float]] = []
    unscored = unlabelled = 0
    for pair in pairs:
        judge_value = (pair["row"].get("judge") or {}).get(metric)
        label_value = pair["label"].get(metric)
        if judge_value is None:
            unscored += 1
            continue
        if label_value is None:
            unlabelled += 1
            continue
        where = f"{metric} for {pair['row']['config']} | {pair['row']['question']}"
        scored.append(
            (_one_score(judge_value, where=f"judge {where}"),
             _one_score(label_value, where=f"label {where}"))
        )

    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for judge_value, label_value in scored:
        judge_positive = judge_value >= threshold
        label_positive = label_value >= threshold
        if judge_positive and label_positive:
            counts["tp"] += 1
        elif judge_positive and not label_positive:
            counts["fp"] += 1
        elif not judge_positive and label_positive:
            counts["fn"] += 1
        else:
            counts["tn"] += 1

    total = len(scored)
    positive_judge = (counts["tp"] + counts["fp"]) / total if total else 0.0
    positive_label = (counts["tp"] + counts["fn"]) / total if total else 0.0
    observed = (counts["tp"] + counts["tn"]) / total if total else 0.0
    expected = (
        positive_judge * positive_label + (1 - positive_judge) * (1 - positive_label)
        if total
        else 0.0
    )
    kappa: float | None
    if total == 0 or expected >= 1.0:
        # Both raters put every row in the same class: agreement is 1.0 but kappa is 0/0.
        kappa = None
    else:
        kappa = (observed - expected) / (1 - expected)
    mean_difference = (
        sum(abs(judge_value - label_value) for judge_value, label_value in scored) / total
        if total
        else 0.0
    )
    return {
        "metric": metric,
        "n": total,
        "unscored": unscored,
        "unlabelled": unlabelled,
        "agreement": observed,
        "kappa": kappa,
        "interpretation": interpret(kappa),
        "confusion": counts,
        "judge_positive_rate": positive_judge,
        "label_positive_rate": positive_label,
        "mean_absolute_difference": mean_difference,
        "scores": scored,
    }


def kappa_interval(
    stats: dict[str, Any],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    confidence: float = DEFAULT_CONFIDENCE,
) -> tuple[float, float] | None:
    """Percentile bootstrap over the scored rows, resampling rows not raters."""
    scores = stats["scores"]
    if len(scores) < 3 or stats["kappa"] is None:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(iterations):
        sample = [scores[rng.randrange(len(scores))] for _ in scores]
        resampled = _kappa_of(sample, threshold)
        if resampled is not None:
            draws.append(resampled)
    if len(draws) < MIN_RESAMPLES:
        return None
    draws.sort()
    alpha = (1 - confidence) / 2
    low = draws[max(0, int(alpha * len(draws)) - 1)]
    high = draws[min(len(draws) - 1, int((1 - alpha) * len(draws)))]
    return low, high


def _kappa_of(scores: Sequence[tuple[float, float]], threshold: float) -> float | None:
    total = len(scores)
    if total == 0:
        return None
    positive_judge = sum(1 for judge, _ in scores if judge >= threshold) / total
    positive_label = sum(1 for _, label in scores if label >= threshold) / total
    observed = sum(
        1 for judge, label in scores if (judge >= threshold) == (label >= threshold)
    ) / total
    expected = positive_judge * positive_label + (1 - positive_judge) * (1 - positive_label)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1 - expected)


def interpret(kappa: float | None) -> str:
    if kappa is None:
        return "undefined (both raters put every row in the same class)"
    magnitude = abs(kappa)
    if magnitude < 0.20:
        return "slight"
    if magnitude < 0.40:
        return "fair"
    if magnitude < 0.60:
        return "moderate"
    if magnitude < 0.80:
        return "substantial"
    return "almost perfect"


def _disagreements(
    pairs: Sequence[dict[str, Any]], metric: str, *, threshold: float
) -> list[dict[str, Any]]:
    found = []
    for pair in pairs:
        judge_value = (pair["row"].get("judge") or {}).get(metric)
        label_value = pair["label"].get(metric)
        if judge_value is None or label_value is None:
            continue
        judge_positive = float(judge_value) >= threshold
        label_positive = float(label_value) >= threshold
        if judge_positive == label_positive:
            continue
        found.append(
            {
                "config": pair["row"]["config"],
                "question": pair["row"]["question"],
                "judge": judge_value,
                "label": label_value,
                "direction": "judge too generous" if judge_positive else "judge too harsh",
                "judge_reason": (pair["row"].get("judge") or {}).get("reason"),
                "note": pair["label"].get("note", ""),
            }
        )
    return found


def report(
    pairs: Sequence[dict[str, Any]],
    *,
    metrics: Iterable[str],
    threshold: float,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "threshold": threshold,
        "iterations": iterations,
        "seed": seed,
        "metrics": {},
    }
    for metric in metrics:
        stats = cohens_kappa(pairs, metric, threshold=threshold)
        interval = kappa_interval(stats, threshold=threshold, iterations=iterations, seed=seed)
        stats["kappa_interval"] = list(interval) if interval else None
        stats["disagreements"] = _disagreements(pairs, metric, threshold=threshold)
        stats.pop("scores")
        result["metrics"][metric] = stats
    return result


def print_report(result: dict[str, Any], *, labeller: str, limit: int = 10) -> None:
    print(f"labeller: {labeller}  threshold: {result['threshold']}")
    for metric, stats in result["metrics"].items():
        kappa = stats["kappa"]
        rendered = "undefined" if kappa is None else f"{kappa:.3f}"
        interval = stats["kappa_interval"]
        bracket = f" [{interval[0]:.3f}, {interval[1]:.3f}]" if interval else " [no interval]"
        print(
            f"\n{metric}: n={stats['n']} (unscored={stats['unscored']}, "
            f"unlabelled={stats['unlabelled']}) agreement={stats['agreement']:.3f} "
            f"kappa={rendered}{bracket} {stats['interpretation']}"
        )
        counts = stats["confusion"]
        print(
            f"  judge+label+ {counts['tp']:3d} | judge+label- {counts['fp']:3d} | "
            f"judge-label+ {counts['fn']:3d} | judge-label- {counts['tn']:3d} | "
            f"mean|diff|={stats['mean_absolute_difference']:.3f}"
        )
        for item in stats["disagreements"][:limit]:
            print(
                f"  - [{item['direction']}] {item['config']} | {item['question']} "
                f"judge={item['judge']} label={item['label']} note={item['note']}"
            )
        if len(stats["disagreements"]) > limit:
            print(f"  ... {len(stats['disagreements']) - limit} more disagreement(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--dump-sample", type=Path, default=None)
    parser.add_argument("--metrics", default=",".join(METRICS))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    metrics = tuple(name.strip() for name in args.metrics.split(",") if name.strip())
    rows = load_rows(args.artifact)
    answer_rows = [row for row in rows if row.get("judge")]
    if not answer_rows:
        raise SystemExit(
            f"{args.artifact} carries no judge scores: rerun the experiment with "
            "--answer-evaluation (retrieval-only runs have no answers to review)"
        )
    if args.dump_sample:
        written = dump_sample(answer_rows, args.dump_sample, metrics=metrics)
        print(f"wrote {written} row(s) to {args.dump_sample} for review")
    if not args.labels:
        if not args.dump_sample:
            raise SystemExit("nothing to do: pass --labels or --dump-sample")
        return

    labeller, note, labels = load_labels(args.labels)
    pairs, unmatched = pair_up(rows, labels)
    print(f"matched {len(pairs)} labelled row(s) against {len(rows)} experiment row(s)")
    if unmatched:
        print(f"{len(unmatched)} label(s) matched no experiment row:", file=sys.stderr)
        for key in unmatched[:5]:
            print(f"  {key}", file=sys.stderr)
    if note:
        print(f"labeller note: {note}")

    result = report(
        pairs,
        metrics=metrics,
        threshold=args.threshold,
        iterations=args.iterations,
        seed=args.seed,
    )
    print_report(result, labeller=labeller)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"labeller": labeller, "note": note, **result}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
