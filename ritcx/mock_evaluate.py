"""Compare policies on paired synthetic markets; preserve failures and held-out seeds."""

import argparse
import hashlib
import json
import math
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .mock_engine import POLICIES, run_heat
from .mock_market import SCENARIOS
from .runtime import ROOT


def summarize(rows):
    pnl = sorted(r["pnl"] for r in rows)
    return dict(
        runs=len(rows),
        mean_pnl=statistics.mean(pnl),
        median_pnl=statistics.median(pnl),
        worst_pnl=pnl[0],
        p10_pnl=pnl[max(0, math.ceil(0.1 * len(pnl)) - 1)],
        loss_rate=sum(p < 0 for p in pnl) / len(pnl),
        worst_drawdown=max(r["max_drawdown"] for r in rows),
        mean_fees=statistics.mean(r["fees"] for r in rows),
        fines=sum(r["fines"] for r in rows),
        errors=sum(r["status"] != "completed" for r in rows),
        settlement_runs=sum(bool(r["settled_positions"]) for r in rows),
        strategy_flat_runs=sum(r["strategy_finished_flat"] for r in rows),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seeds",
        type=int,
        default=20,
        help="Seeds per scenario per split (default 20; total heats = seeds x 16)",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports" / "mock-stress-evaluation.json"
    )
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+", default=["standard", "stress"])
    parser.add_argument(
        "--policies",
        choices=POLICIES,
        nargs="+",
        default=["baseline", "strict_edge", "wide_hedge", "half_size"],
    )
    parser.add_argument("--exploratory-start", type=int, default=0)
    parser.add_argument("--held-out-start", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.seeds <= 10000:
        parser.error("--seeds must be between 1 and 10000 (keeps seed sets disjoint)")
    if abs(args.exploratory_start - args.held_out_start) < args.seeds:
        parser.error("Exploratory and held-out seed ranges must not overlap")
    result = dict(
        source="synthetic stress tests, uncalibrated to official RIT markets",
        created_utc=datetime.now(timezone.utc).isoformat(),
        version=2,
        method="Fixed policies and paired seeds; failures retained at settlement. First selected policy is comparison baseline. No live strategy selection.",
        seed_ranges=dict(
            exploratory=args.exploratory_start, held_out=args.held_out_start, count=args.seeds
        ),
        scenarios={k: asdict(SCENARIOS[k]) for k in args.scenarios},
        policies={k: asdict(POLICIES[k]) for k in args.policies},
        source_sha256={
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "ritcx" / f
                for f in (
                    "mock_market.py",
                    "mock_engine.py",
                    "mock_evaluate.py",
                    "volatility/bot.py",
                    "volatility/model.py",
                    "volatility/adaptive.py",
                    "api.py",
                    "execution.py",
                )
            ]
        },
        heat_count=0,
        groups=[],
    )
    print("SYNTHETIC EVALUATION | all P&L includes fees/fines; failures retained", flush=True)
    print("Split       Market    Policy         Mean P&L   Worst P&L   Loss% Errors", flush=True)
    for split, start in (
        ("exploratory", args.exploratory_start),
        ("held_out", args.held_out_start),
    ):
        for scenario in args.scenarios:
            baseline = None
            for policy in args.policies:
                rows = [
                    run_heat(seed, scenario, policy) for seed in range(start, start + args.seeds)
                ]
                if baseline is None:
                    baseline = rows
                summary = summarize(rows)
                differences = [r["pnl"] - b["pnl"] for r, b in zip(rows, baseline)]
                summary.update(
                    mean_change_vs_baseline=statistics.mean(differences),
                    beats_baseline=sum(d > 1e-8 for d in differences),
                )
                result["groups"].append(
                    dict(split=split, scenario=scenario, policy=policy, summary=summary, runs=rows)
                )
                result["heat_count"] += len(rows)
                print(
                    f"{split:11s} {scenario:9s} {policy:12s} {summary['mean_pnl']:10.2f} "
                    f"{summary['worst_pnl']:11.2f} {summary['loss_rate']:6.0%} {summary['errors']:6d}",
                    flush=True,
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"Saved {result['heat_count']} heats: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
