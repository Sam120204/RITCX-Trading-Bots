"""Offline JSONL heat summaries. No network access or trading capabilities."""

import argparse
import json
import math
import statistics
from pathlib import Path


def summarize(paths):
    runs, latency = [], []
    malformed = rejected = 0
    for path in sorted(set(Path(p) for p in paths)):
        rows = []
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Expected object")
                rows.append(row)
            except ValueError:
                malformed += 1
        # Session logs repeat child output; exclude them to avoid double counting.
        if any(r.get("event") in ("ARMED", "READ_ONLY", "HEAT_FINISHED") for r in rows):
            continue
        if any(
            r.get("event") == "run_started" and r.get("execution_enabled") is False for r in rows
        ):
            continue
        snapshots = [r for r in rows if r.get("event") == "snapshot" and "trader" in r]
        finals = [r for r in rows if r.get("event") == "final_reconciliation"]
        reconciliations = [
            r for r in rows if r.get("event") in ("reconciliation", "final_reconciliation")
        ]
        first = snapshots[0]["trader"] if snapshots else None
        last_row = reconciliations[-1] if reconciliations else snapshots[-1] if snapshots else {}
        last = last_row.get("trader")
        completed = bool(
            finals and not finals[-1].get("positions") and not finals[-1].get("open_orders")
        )
        pnl = last["nlv"] - first["nlv"] if first and last else None
        fines = (
            max(0, last.get("total_fines", 0) - first.get("total_fines", 0))
            if first and last
            else None
        )
        rejected += sum(r.get("event") == "request_rejected" for r in rows)
        latency.extend(
            r["latency_ms"]
            for r in rows
            if r.get("event") == "order_response"
            and isinstance(r.get("latency_ms"), (int, float))
            and math.isfinite(r["latency_ms"])
        )
        runs.append(
            dict(
                log=str(path),
                completed=completed,
                pnl=pnl,
                fines=fines,
                positions=last_row.get("positions"),
                open_orders=last_row.get("open_orders"),
            )
        )
    completed_pnl = [r["pnl"] for r in runs if r["completed"] and r["pnl"] is not None]
    all_pnl = [r["pnl"] for r in runs if r["pnl"] is not None]
    latency.sort()
    return dict(
        runs=runs,
        completed_heats=len(completed_pnl),
        median_heat_pnl=statistics.median(completed_pnl) if completed_pnl else None,
        worst_completed_heat_pnl=min(completed_pnl) if completed_pnl else None,
        worst_observed_pnl=min(all_pnl) if all_pnl else None,
        total_fines=sum(r["fines"] for r in runs if r["fines"] is not None),
        unresolved_runs=sum(not r["completed"] for r in runs),
        rejected_requests=rejected,
        malformed_lines=malformed,
        order_latency_ms=dict(
            count=len(latency),
            median=statistics.median(latency) if latency else None,
            p95=latency[math.ceil(0.95 * len(latency)) - 1] if latency else None,
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "logs", nargs="+", type=Path, help="Child bot JSONL files (shell globs supported)"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(summarize(args.logs), indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
