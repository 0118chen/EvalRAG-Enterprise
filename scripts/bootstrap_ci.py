"""Paired bootstrap confidence intervals for "is this config actually better?".

The evaluation reports quote deltas between configurations - "+3.4pp Recall@1", "-34pp on
the hard set" - on question sets of 20 to 75 items, with no statement about how much of
that is signal. One question is worth 1/n of the metric, so at n=20 a single question moves
Recall@1 by 5 points and a 3-point "win" is noise. This turns each delta into an interval.

The comparison is **paired**: both configurations answer the same questions, so the
bootstrap resamples *examples* and recomputes the mean of the per-question differences.
Pairing is what makes the interval narrow enough to be useful - it removes the variance
that comes from some questions simply being harder than others.

What the interval does and does not cover:

* it covers **sampling uncertainty over the question set**, which is the dominant source at
  these sizes and the one the reports ignore;
* it does **not** cover run-to-run variance (retrieval is deterministic on a fixed corpus,
  so there is none to cover), label error, or the fact that the question set was written
  by a model and audited by the same parser that wrote it.

Usage:
    python -m scripts.bootstrap_ci --artifact docs/evaluation/golden-set-v3-corpus18-2026-09-27.json \
        --against sparse-bm25
    python -m scripts.bootstrap_ci --artifact ... --against sparse-bm25 --json out.json
"""

import argparse
import json
import random
from pathlib import Path
from statistics import fmean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# The per-example numbers the harness records, in report order.
DEFAULT_METRICS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "all_targets_at_5",
    "any_target_at_5",
    "page_hit",
    "quote_hit",
)

# Metrics whose aggregate is published under a different key. `quote_hit` is the harness's
# own independent recomputation, reported as an evidence statistic rather than under
# `metrics`, and the point of the cross-check is that the two agree.
AGGREGATE_KEYS = {"quote_hit": "evidence_quote_hit_rate"}

# Fixed so a published interval can be reproduced exactly.
DEFAULT_SEED = 20261005
DEFAULT_ITERATIONS = 2000


def numeric(value: Any) -> bool:
    """True for a number or a bool.

    The per-example rows mix the two: ``page_hit`` is a float from the metric layer while
    ``quote_hit`` is a bool, and both are 0/1 scores that a bootstrap can average.
    """
    return isinstance(value, (int, float, bool)) and value is not None


def aggregate_of(config: dict, metric: str) -> Any:
    """The artifact's own aggregate for a metric, wherever it publishes it."""
    key = AGGREGATE_KEYS.get(metric, metric)
    published = config.get("metrics", {}).get(key)
    return published if numeric(published) else config.get(key)


def paired_values(
    reference_rows: list[dict],
    candidate_rows: list[dict],
    metric: str,
) -> list[tuple[float, float]]:
    """Per-question (reference, candidate) pairs where both sides report the metric.

    A row reports only the keys that apply to it - an equivalence question has
    ``any_target_at_5`` and no ``all_targets_at_5``, a refusal question has neither - so a
    metric is averaged over its own population. That is also why the denominators differ
    between metrics and are reported next to every interval.
    """
    pairs: list[tuple[float, float]] = []
    for reference, candidate in zip(reference_rows, candidate_rows, strict=True):
        left, right = reference.get(metric), candidate.get(metric)
        if numeric(left) and numeric(right):
            pairs.append((float(left), float(right)))
    return pairs


def paired_bootstrap(
    pairs: list[tuple[float, float]],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Mean difference with a percentile bootstrap interval over the paired examples."""
    if not pairs:
        return {"n": 0, "delta": 0.0, "low": 0.0, "high": 0.0, "step_pp": 0.0}
    differences = [candidate - reference for reference, candidate in pairs]
    count = len(differences)
    rng = random.Random(seed)
    draw = rng.randrange
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(count):
            total += differences[draw(count)]
        means.append(total / count)
    means.sort()
    tail = (1 - confidence) / 2
    low = means[max(0, int(tail * iterations))]
    high = means[min(iterations - 1, int((1 - tail) * iterations))]
    return {
        "n": count,
        "delta": fmean(differences),
        "low": low,
        "high": high,
        # One question's worth of the metric: the smallest difference this set can express.
        "step_pp": 100.0 / count,
    }


def verdict(stats: dict[str, float]) -> str:
    if not stats["n"]:
        return "no data"
    if stats["low"] > 0:
        return "better"
    if stats["high"] < 0:
        return "worse"
    return "inconclusive"


def compare(
    artifact: dict,
    reference_name: str,
    *,
    metrics: tuple[str, ...] = DEFAULT_METRICS,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    configs = {config["name"]: config for config in artifact["configs"]}
    if reference_name not in configs:
        raise SystemExit(
            f"unknown reference config {reference_name!r}; "
            f"available: {sorted(configs)}"
        )
    reference = configs[reference_name]
    if "per_example" not in reference:
        raise SystemExit(
            "this artifact has no per_example rows, so no interval can be computed "
            "(regenerate it with scripts.run_golden_experiment)"
        )

    results: list[dict[str, Any]] = []
    for name, config in configs.items():
        if name == reference_name:
            continue
        for metric in metrics:
            pairs = paired_values(reference["per_example"], config["per_example"], metric)
            stats = paired_bootstrap(pairs, iterations=iterations, seed=seed)
            per_example_mean = fmean([candidate for _, candidate in pairs]) if pairs else 0.0
            aggregate = aggregate_of(config, metric)
            results.append(
                {
                    "config": name,
                    "metric": metric,
                    **stats,
                    "reference_mean": fmean([reference_ for reference_, _ in pairs])
                    if pairs
                    else 0.0,
                    "candidate_mean": per_example_mean,
                    "aggregate_mean": aggregate,
                    # The product aggregates the same numbers; disagreeing means one of the
                    # two is wrong, which is worth knowing before quoting either. None when
                    # there is nothing to compare against.
                    "aggregate_matches": (
                        None
                        if not stats["n"] or not numeric(aggregate)
                        else abs(float(aggregate) - per_example_mean) < 1e-9
                    ),
                    "verdict": verdict(stats),
                }
            )
    return {
        "artifact": artifact.get("dataset"),
        "examples": len(reference["per_example"]),
        "reference": reference_name,
        "iterations": iterations,
        "seed": seed,
        "comparisons": results,
    }


def print_report(report: dict[str, Any]) -> None:
    print(
        f"artifact dataset: {report['artifact']}  examples: {report['examples']}\n"
        f"reference config: {report['reference']}  "
        f"bootstrap: {report['iterations']} resamples, seed {report['seed']}\n"
    )
    header = (
        f"{'config':<34}{'metric':<18}{'n':>4}{'reference':>11}{'candidate':>11}"
        f"{'delta':>9}{'95% CI':>20}{'1 q =':>8}  verdict"
    )
    print(header)
    mismatches = 0
    skipped: dict[str, list[str]] = {}
    for row in report["comparisons"]:
        if not row["n"]:
            skipped.setdefault(row["metric"], []).append(row["config"])
            continue
        if row["aggregate_matches"] is False:
            mismatches += 1
        interval = f"[{row['low']:+.3f}, {row['high']:+.3f}]"
        print(
            f"{row['config']:<34}{row['metric']:<18}{row['n']:>4}"
            f"{row['reference_mean']:>11.3f}{row['candidate_mean']:>11.3f}"
            f"{row['delta']:>+9.3f}{interval:>20}"
            f"{row['step_pp']:>6.1f}pp  {row['verdict']}"
        )
    print(
        "\n'1 q =' is what one question is worth in this metric: a delta smaller than that "
        "cannot be resolved by this set at all."
    )
    for metric, configs in sorted(skipped.items()):
        print(
            f"\nskipped {metric}: no paired data in any of {len(configs)} config(s) - the "
            "question set has no example this metric applies to."
        )
    if mismatches:
        print(
            f"\nWARNING: {mismatches} row(s) where the per-example mean and the artifact's "
            "aggregate disagree - one of them is wrong (older artifacts average over a "
            "different population)."
        )
    print(
        "\nThe interval covers sampling over the question set only. It is not run-to-run "
        "variance (retrieval is deterministic here), label error, or the fact that the "
        "questions and their evidence were written by the same model call."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--against", required=True, help="reference config name")
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    path = args.artifact if args.artifact.is_absolute() else ROOT / args.artifact
    artifact = json.loads(path.read_text(encoding="utf-8"))
    metrics = tuple(metric.strip() for metric in args.metrics.split(",") if metric.strip())

    report = compare(
        artifact,
        args.against,
        metrics=metrics,
        iterations=args.iterations,
        seed=args.seed,
    )
    print_report(report)
    if args.json:
        target = args.json if args.json.is_absolute() else ROOT / args.json
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")


if __name__ == "__main__":
    main()
