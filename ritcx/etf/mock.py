"""Watch a 300-second local Case 2 ETF heat; no RIT account or network required."""

import argparse
import json
import math
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path

from ..mock import chart
from ..runtime import ROOT, open_audit_log
from .mock_engine import run_heat
from .mock_market import SCENARIOS


def display(row, history, fills, seed, speed, tty):
    if not tty:
        print(f"CASE 2 LOCAL SIMULATION | {row['state']} | {row['tick']}/300 | "
              f"P&L CAD {row['pnl']:+,.2f} | fees {row['fees']:,.2f} | "
              f"orders {row['orders']} | tenders {row['tenders_accepted']} | {row['last_action']}", flush=True)
        return
    compact = shutil.get_terminal_size((80, 30)).lines < 34
    progress = int(row["tick"] / 300 * 40)
    lines = [
        "  RITCx CASE 2: ETF ARBITRAGE | LOCAL SIMULATION",
        f"  Seed {seed}   Speed {speed:g}x   Scenario {row['scenario']}",
        f"  [{'#' * progress}{'-' * (40 - progress)}] {row['tick']:3d}/300 seconds",
        f"  {row['state']} | {row['last_action']}", "",
        f"  NET P&L CAD  ${row['pnl']:+12,.2f}   Drawdown ${row['max_drawdown']:,.2f}",
        f"  Fees CAD     ${row['fees']:12,.2f}   Fines    ${row['fines']:,.2f}",
        f"  Cash in CAD  ${row['cash_cad']:+12,.2f}   Inventory ${row['inventory_value_cad']:+,.2f}",
        f"  USD/CAD       {row['fx']:12.5f}   ETF gap CAD {row['gap_cad']:+.4f}",
        f"  Filled orders {row['orders']:12d}   Tenders accepted {row['tenders_accepted']}", "",
    ]
    lines += chart(history, height=3 if compact else 5)
    positions = row["positions"]
    lines += ["", "  POSITIONS (equity shares / USD cash)"]
    lines += ["  " + "   ".join(f"{k}: {v:+,.0f}" for k, v in positions.items()) if positions else "  Flat"]
    lines += ["", "  RECENT FILLS"]
    lines += [f"  {f['tick']:3d}s {f['action']:4s} {f['quantity']:7,d} {f['ticker']:5s} @ {f['price']:.4f}"
              for f in fills[-(2 if compact else 4):]] or ["  Waiting for a qualifying signal"]
    lines += ["", "  Net P&L = CAD-valued cash + inventory; fees already deducted.",
              "  Synthetic, uncalibrated | No RIT connection | Ctrl-C saves"]
    if compact:
        lines = [line for line in lines if line]
    print("\033[H\033[2J" + "\n".join(lines), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, help="Repeatable path; default: a new random seed")
    parser.add_argument("--speed", type=float, default=1, help="Default 1: 300 wall-clock seconds")
    parser.add_argument("--scenario", choices=SCENARIOS, default="case")
    parser.add_argument("--plain", action="store_true", help="Scrolling updates every 5 seconds")
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

    previous = signal.signal(signal.SIGINT, interrupt)
    history, last_tick = [], -1
    started = time.monotonic()
    try:
        with open_audit_log(args.output_dir, "mock-etf") as (logpath, stream):

            def audit(event, **data):
                stream.write(json.dumps(dict(event=event, **data), allow_nan=False) + "\n")
                stream.flush()

            def on_snapshot(row, fills):
                nonlocal last_tick
                deadline = started + row["tick"] / args.speed
                while not stop and time.monotonic() < deadline:
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
                if row["tick"] == last_tick and history:
                    history[-1] = row["pnl"]
                else:
                    history.append(row["pnl"])
                last_tick = row["tick"]
                if tty or row["tick"] % 5 == 0 or "status" in row:
                    display(row, history, fills, seed, args.speed, tty)

            result = run_heat(seed, args.scenario, audit=audit, on_snapshot=on_snapshot,
                              stopped=lambda: stop)
            result.update(speed=args.speed, wall_seconds=time.monotonic() - started)
            summary = logpath.with_suffix(".json")
            summary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
            print(f"\n{result['status'].upper()}: net P&L CAD ${result['pnl']:+,.2f}; "
                  f"fees CAD ${result['fees']:,.2f}; max drawdown CAD ${result['max_drawdown']:,.2f}")
            if result["settled_positions"]:
                print(f"Inventory settled at expiry: {result['settled_positions']}")
            if result["error"]:
                print(f"Trading stopped: {result['error']}")
            print(f"Report: {summary}\nEvent log: {logpath}")
    finally:
        signal.signal(signal.SIGINT, previous)
    return 1 if result["status"] == "error" else 130 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
