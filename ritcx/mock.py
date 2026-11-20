"""Run a 300-second synthetic Volatility heat and watch P&L in your terminal."""

import argparse
import json
import math
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path

from .mock_engine import POLICIES, run_heat
from .mock_market import SCENARIOS
from .runtime import ROOT, open_audit_log


def chart(values, width=60, height=5):
    count = min(width, len(values))
    samples = [values[round(i * (len(values) - 1) / max(1, count - 1))] for i in range(count)]
    low, high = min(values), max(values)
    span = high - low or 1
    rows = [[" " for _ in samples] for _ in range(height)]
    for x, value in enumerate(samples):
        rows[height - 1 - round((value - low) / span * (height - 1))][x] = "*"
    return [f"  P&L range: ${low:+,.2f} to ${high:+,.2f}"] + ["  |" + "".join(row) for row in rows]


def display(row, history, fills, seed, speed, state, tty):
    compact = shutil.get_terminal_size((80, 30)).lines < 34
    tick = row["tick"]
    progress = int(tick / 300 * 40)
    lines = [
        "  RITCx VOLATILITY | LOCAL SIMULATION",
        f"  Seed {seed}   Speed {speed:g}x   {state}",
        f"  Scenario {row['scenario']}   Policy {row['policy']}   {row['last_action']}",
        f"  [{'#' * progress}{'-' * (40 - progress)}] {tick:3d}/300   {300 - tick:3d}s remaining",
        "",
        f"  NET P&L     ${row['pnl']:+12,.2f}    Max drawdown ${row['max_drawdown']:,.2f}",
        f"  Realized    ${row['realized_net']:+12,.2f}    Unrealized   ${row['unrealized']:+,.2f}",
        f"  Fees        ${row['fees']:12,.2f}    Fines        ${row['fines']:,.2f}",
        f"  RTM         ${row['spot']:12.4f}    Delta         {row['delta']:+,.0f}",
        f"  Volatility   {row['volatility']:12.1%}    Quote vol     {row['quoted_volatility']:.1%}",
        f"  Filled orders {row['orders']:11d}    P&L includes fees and fines",
        "",
    ]
    lines += chart(history, height=3 if compact else 5)
    lines += ["", "  POSITIONS (shares / option contracts)"]
    positions = list(row["positions"].items())
    lines += [
        "  " + "   ".join(f"{k}: {v:+,}" for k, v in positions[i : i + 3])
        for i in range(0, len(positions), 3)
    ] or ["  Flat"]
    lines += ["", "  RECENT FILLS"]
    lines += [
        f"  {f['tick']:3d}s  {f['action']:4s} {f['quantity']:5,d} {f['ticker']:7s} @ {f['price']:.4f}"
        for f in fills[-(2 if compact else 4) :]
    ] or ["  Waiting for a qualifying signal"]
    lines += [
        "",
        "  Synthetic stress test; full fills, simulated latency; uncalibrated.",
        "  Ctrl-C stops and saves the local run. RIT account is not connected.",
    ]
    if compact:
        lines = [line for line in lines if line]
        lines[-2:] = ["  Synthetic, uncalibrated | Ctrl-C saves | No RIT connection"]
    text = "\n".join(lines)
    if tty:
        # Clear the previous frame without leaving the terminal's normal scrollback.
        print("\033[H\033[2J" + text, flush=True)
    else:
        print(
            f"LOCAL SIMULATION | {state} | {tick}/300 | P&L ${row['pnl']:+,.2f} | "
            f"fees ${row['fees']:,.2f} | fines ${row['fines']:,.2f} | "
            f"orders {row['orders']} | positions {row['positions']}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, help="Repeatable market path; default: new random seed")
    parser.add_argument(
        "--speed",
        type=float,
        default=1,
        help="Simulation speed, default 1 (300 wall-clock seconds)",
    )
    parser.add_argument(
        "--plain", action="store_true", help="Scrolling updates instead of a refreshing dashboard"
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="case",
        help="Market assumptions (default: case); standard/stress are adverse sensitivity tests",
    )
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default="buffered",
        help="Only supported volatility policy (default: buffered)",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "mock")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or args.speed <= 0:
        parser.error("--speed must be a finite positive number")
    seed = args.seed if args.seed is not None else secrets.randbelow(2**32)
    tty = sys.stdout.isatty() and not args.plain
    stop = False

    def interrupt(*_):
        nonlocal stop
        stop = True

    history = []
    last_tick = -1
    started = time.monotonic()
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        with open_audit_log(args.output_dir, "mock-volatility") as (logpath, stream):

            def audit(event, **data):
                stream.write(json.dumps(dict(event=event, **data), allow_nan=False) + "\n")
                stream.flush()

            def on_snapshot(row, fills):
                nonlocal last_tick
                deadline = started + row["tick"] / args.speed
                while not stop and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                if row["tick"] == last_tick and history:
                    history[-1] = row["pnl"]
                else:
                    history.append(row["pnl"])
                last_tick = row["tick"]
                if tty or row["tick"] % 5 == 0 or "status" in row:
                    display(row, history, fills, seed, args.speed, row["state"], tty)

            row = run_heat(
                seed,
                args.scenario,
                args.policy,
                audit=audit,
                on_snapshot=on_snapshot,
                stopped=lambda: stop,
            )
            row.update(speed=args.speed, wall_seconds=time.monotonic() - started)
            summary = logpath.with_suffix(".json")
            summary.write_text(json.dumps(row, indent=2, allow_nan=False) + "\n")
            print(
                f"\n{row['status'].upper()}: net P&L ${row['pnl']:+,.2f}; "
                f"fees ${row['fees']:,.2f}; fines ${row['fines']:,.2f}"
            )
            if row["settled_positions"]:
                print(
                    f"Positions settled at expiry (not closed by strategy): {row['settled_positions']}"
                )
            if row["error"]:
                print(f"Trading stopped: {row['error']}")
            print(f"Report: {summary}\nEvent log: {logpath}")
    finally:
        signal.signal(signal.SIGINT, previous)
    return 1 if row["status"] == "error" else 130 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
