"""Fail the build when retrieval quality regresses.

The evaluation matrices under `docs/evaluation/` are *evidence*, not a gate: nothing stops a
change from quietly moving Recall@1 until a human re-reads the numbers. That happened three
times in one round of work on this repo, so the loop needs a machine in it.

CI runs one small committed fixture corpus (`tests/fixtures/eval_gate/`) through the real
pipeline and diffs every quality metric against a committed baseline. Latency is deliberately
**not** gated - it moves ~10% between runs on the same machine and CI runners are noisier - but
it is printed for eyeballing. Quality metrics are deterministic (same corpus + same code =
bit-identical), so anything beyond float noise is a real change.

Usage:
    python -m scripts.check_eval_regression \
        --baseline tests/fixtures/eval_gate/baseline.json \
        --current /tmp/eval.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The report uses ✓ and ✗, which a legacy console code page (cp936 on this machine) cannot
# encode. An unencodable character raises UnicodeEncodeError *while printing*, which turned a
# passing quality gate into a non-zero exit - the worst possible failure mode for a check whose
# whole job is to be trusted. Degrade the characters, never the exit code.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def quality_metrics(entry: dict) -> dict[str, float]:
    """Every numeric quality scalar of a config: metrics{} plus evidence stats.

    Latency is excluded on purpose (see the module docstring), and so is anything nested -
    `per_example` and the retrieval lists are evidence for humans, not gate material.
    """
    scalars: dict[str, float] = {}
    for source in (entry.get("metrics", {}), entry):
        for key, value in source.items():
            if key.startswith("latency"):
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                scalars[key] = float(value)
    return scalars


def _identity(report: dict) -> dict:
    """What the two reports must agree on before their metrics mean anything."""
    return {
        "dataset": report.get("dataset", {}).get("name"),
        # The golden *file*: the dataset name comes from the knowledge base and is identical
        # across question sets, so the path is what actually pins the question set. Separators
        # are normalised because the baseline is generated on Windows and the gate runs on Linux.
        "dataset_source": (report.get("dataset", {}).get("source") or "").replace("\\", "/"),
        "examples": report.get("dataset", {}).get("examples"),
        "documents": report.get("corpus", {}).get("documents"),
        "chunks": report.get("corpus", {}).get("chunks"),
        "rerank_backend": report.get("rerank_backend"),
        "embedding": report.get("embedding", {}).get("provider"),
    }


def compare(baseline: dict, current: dict, tolerance: float = 1e-9) -> dict:
    """Diff two harness reports. Returns a report dict; `problems` non-empty means fail."""
    problems: list[str] = []
    for key, expected in _identity(baseline).items():
        actual = _identity(current).get(key)
        if expected != actual:
            problems.append(
                f"{key} 不一致：基线 {expected!r} vs 本次 {actual!r} "
                "（语料/题集/后端不同就不能比指标）"
            )

    base_configs = {entry["name"]: entry for entry in baseline.get("configs", [])}
    curr_configs = {entry["name"]: entry for entry in current.get("configs", [])}
    missing = sorted(set(base_configs) - set(curr_configs))
    if missing:
        problems.append(f"本次缺少基线里的配置：{missing}")
    extra = sorted(set(curr_configs) - set(base_configs))

    rows: list[dict] = []
    regressions: list[str] = []
    improvements: list[str] = []
    for name in sorted(set(base_configs) & set(curr_configs)):
        base_metrics = quality_metrics(base_configs[name])
        curr_metrics = quality_metrics(curr_configs[name])
        for metric in sorted(base_metrics):
            if metric not in curr_metrics:
                regressions.append(f"{name}.{metric} 消失了")
                continue
            delta = curr_metrics[metric] - base_metrics[metric]
            if delta < -tolerance:
                regressions.append(f"{name}.{metric} {base_metrics[metric]:.4f} → {curr_metrics[metric]:.4f}")
            elif delta > tolerance:
                improvements.append(f"{name}.{metric} {base_metrics[metric]:.4f} → {curr_metrics[metric]:.4f}")
            rows.append(
                {
                    "config": name,
                    "metric": metric,
                    "baseline": base_metrics[metric],
                    "current": curr_metrics[metric],
                    "delta": delta,
                }
            )

    return {
        "rows": rows,
        "regressions": regressions,
        "improvements": improvements,
        "extra_configs": extra,
        "problems": problems + regressions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-9,
        help="float noise allowance; not a quality budget - raise it only for that reason",
    )
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    result = compare(baseline, current, args.tolerance)

    print(f"基线：{_identity(baseline)}")
    print(f"本次：{_identity(current)}\n")
    print(f"{'配置':<32}{'指标':<28}{'基线':>10}{'本次':>10}{'Δ':>10}")
    for row in result["rows"]:
        marker = "  " if abs(row["delta"]) <= args.tolerance else ("↓ " if row["delta"] < 0 else "↑ ")
        print(
            f"{marker}{row['config']:<30}{row['metric']:<28}"
            f"{row['baseline']:>10.4f}{row['current']:>10.4f}{row['delta']:>+10.4f}"
        )

    if result["extra_configs"]:
        print(f"\n提示：本次多出 {result['extra_configs']}（不算失败，但基线该一起更新）")
    if result["improvements"]:
        print(f"\n改善 {len(result['improvements'])} 项（不是失败，但请复核是不是真变好了）：")
        for line in result["improvements"]:
            print(f"  ↑ {line}")

    if result["problems"]:
        print(f"\n✗ 失败：{len(result['problems'])} 个问题")
        for line in result["problems"]:
            print(f"  - {line}")
        print(
            "\n如果这个变化是有意的，请重新生成基线并说明原因：\n"
            "  python -m scripts.run_golden_experiment --corpus tests/fixtures/eval_gate/corpus \\\n"
            "      --golden tests/fixtures/eval_gate/golden.json --json <baseline> --configs ..."
        )
        sys.exit(1)

    print("\n✓ 质量指标与基线一致（延迟未设门禁，见脚本说明）")


if __name__ == "__main__":
    main()
