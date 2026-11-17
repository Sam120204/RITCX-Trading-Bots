"""News-driven volatility trading for one 300-second RITCx heat."""

import argparse
import json
import math
import signal
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from ..api import client_from_config
from ..execution import PartialFill, confirm_flat, submit_change
from ..runtime import (
    ROOT,
    TradingError,
    account_lock,
    open_audit_log,
    read_config,
    validate_profile,
)
from .model import (
    VolatilityCurve,
    allowed,
    candidates,
    hedge_quantity,
    parity_opportunities,
    should_exit,
)


def hedge(client, securities, curve, tick, force=False):
    quantity = hedge_quantity(securities, tick, curve.current, curve.rate)
    if abs(quantity) >= (1 if force else 500):
        # A confirmed fill may take several ticks. Recompute the risk-reducing
        # hedge from fresh inventory/quotes; never merely bypass quote freshness.
        case = client.check_heat(require_fresh=False)
        securities = client.get("securities")
        client.bind_snapshot(case)
        quantity = hedge_quantity(securities, case["tick"], curve.current, curve.rate)
        stock = next(x for x in securities if x["ticker"] == "RTM")
        target = stock["position"] + quantity
        if abs(target) > 10000:
            raise TradingError("Required ETF hedge exceeds conservative share cap")
        securities = submit_change(client, securities, "RTM", quantity)
    return securities


def _trade_once(client, securities, curve, tick, flatten=False):
    prior_stock = next(s["position"] for s in securities if s["ticker"] == "RTM")
    securities = hedge(client, securities, curve, tick)
    if prior_stock != next(s["position"] for s in securities if s["ticker"] == "RTM"):
        return "hedged"
    if flatten:
        positions = [x for x in securities if x["type"] == "OPTION" and x["position"]]
        if positions:
            sec = positions[0]
            securities = submit_change(client, securities, sec["ticker"], -sec["position"])
            hedge(client, securities, curve, tick, force=True)
            return "flatten_option"
        stock = next(x for x in securities if x["ticker"] == "RTM")
        if stock["position"]:
            submit_change(client, securities, "RTM", -stock["position"])
            return "flatten_stock"
        return "flat"
    if tick >= 270:
        return "no_new_entries"
    opportunities = candidates(securities, curve, tick)
    stock = next(s for s in securities if s["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    for sec in securities:
        if sec["type"] == "OPTION" and sec["position"]:
            if should_exit(sec, spot, curve, tick):
                securities = submit_change(client, securities, sec["ticker"], -sec["position"])
                hedge(client, securities, curve, tick, force=True)
                return "exit_edge"
    for choice in opportunities:
        change = 10 if choice["action"] == "BUY" else -10
        if allowed(securities, choice["ticker"], change, tick, curve.current, curve.rate):
            client.audit("signal", tick=tick, **choice)
            securities = submit_change(client, securities, choice["ticker"], change)
            hedge(client, securities, curve, tick, force=True)
            return "entry"
    return "hold"


def trade_once(client, securities, curve, tick, flatten=False):
    try:
        return _trade_once(client, securities, curve, tick, flatten)
    except PartialFill as exc:
        client.entry_halted = True
        hedge(client, exc.securities, curve, tick, force=True)
        client.audit("partial_fill_reconciled", filled=exc.filled)
        return "partial_fill_reconciled"


def liquidation_start(securities, observed_cycle_seconds=0):
    """Budget all remaining option tickets, a stock/reconciliation cycle, and slack."""
    tickets = sum(math.ceil(abs(s["position"]) / 10) for s in securities if s["type"] == "OPTION")
    reserve = max(15, (tickets + 1) * max(3.0, 1.5 * observed_cycle_seconds) + 5)
    return max(0, 300 - reserve)


def validate(case, securities, limits):
    if (
        case["name"] != "RITCx Volatility Trading Case"
        or case["ticks_per_period"] != 300
        or case["total_periods"] != 1
    ):
        raise TradingError("Unexpected case; refusing orders")
    expected = {"RTM"} | {f"RTM{k}{cp}" for k in range(48, 53) for cp in "CP"}
    if {x["ticker"] for x in securities} != expected:
        raise TradingError("Unexpected security universe")
    for x in securities:
        option = x["ticker"] != "RTM"
        if (
            x["size"] != (100 if option else 1)
            or x["trading_fee"] != (2 if option else 0.02)
            or x.get("execution_delay_ms", 0) != 0
        ):
            raise TradingError(
                "Changed contract size, fees, or execution delay; review configuration"
            )
        if x["bid"] < 0 or x["ask"] < x["bid"]:
            raise TradingError("Invalid quotes")
    byname = {x["name"]: x for x in limits}
    for name, gross, net in [("Limit-ETF", 10000, 10000), ("Limit-OPT", 100, 100)]:
        if (
            name not in byname
            or byname[name]["gross_limit"] < gross
            or byname[name]["net_limit"] < net
        ):
            raise TradingError("Live limits below bot caps")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trade", action="store_true", help="Enable simulated orders (otherwise read-only)"
    )
    parser.add_argument(
        "--practice",
        action="store_true",
        help="Confirm this is the user-authorized practice session",
    )
    parser.add_argument(
        "--contest",
        action="store_true",
        help="Use an explicitly configured formal simulator profile",
    )
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch through one round (dry run only without --trade)",
    )
    parser.add_argument("--config", type=Path, default=ROOT / ".env")
    parser.add_argument("--max-seconds", type=float, default=420)
    args = parser.parse_args()
    cfg = read_config(args.config)
    try:
        validate_profile(cfg, args.practice, args.contest, args.trade)
    except TradingError as exc:
        parser.error(str(exc))
    logdir = Path(cfg.get("RIT_LOG_DIR", ROOT / "logs"))
    logdir.mkdir(exist_ok=True, parents=True)
    stop = False

    def interrupted(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    with (
        account_lock(cfg, args.lock_fd) if args.trade else nullcontext(),
        open_audit_log(logdir, "volatility") as (logfile, out),
    ):

        def audit(event, **data):
            row = dict(time=datetime.now(timezone.utc).isoformat(), event=event, **data)
            text = json.dumps(row, allow_nan=False)
            out.write(text + "\n")
            out.flush()
            print(text, flush=True)

        audit(
            "run_started",
            execution_enabled=args.trade,
            environment=cfg.get("RIT_ENVIRONMENT", "practice"),
            strategy_revision="research-v1",
        )
        client = client_from_config(cfg, execute=args.trade, audit=audit)
        last_tick = None
        first_period = None
        start = time.monotonic()
        closing = False
        start_pnl = None
        cycle_durations = []
        try:
            while time.monotonic() - start < args.max_seconds + 20:
                cycle_start = time.monotonic()
                case = client.get("case")
                tick = case["tick"]
                if last_tick is not None and (tick < last_tick or case["period"] != first_period):
                    audit("round_reset", case=case)
                    break
                if case["status"] != "ACTIVE":
                    audit("case_inactive", case=case)
                    if last_tick is not None or not args.watch:
                        break
                    time.sleep(1)
                    continue
                if last_tick is None:
                    first_period = case["period"]
                    client.bind_heat(case)
                client.bind_snapshot(case)
                securities = client.get("securities")
                validate(case, securities, client.get("limits"))
                if client.get("orders?status=OPEN"):
                    raise TradingError("Open orders exist; reconcile before running this bot")
                if last_tick is None and args.trade and any(x["position"] for x in securities):
                    raise TradingError(
                        "Existing positions found; initial run requires a flat account"
                    )
                try:
                    curve = VolatilityCurve.from_news(client.get("news"), tick)
                except ValueError:
                    if any(s["position"] for s in securities):
                        raise
                    audit("waiting_for_news", tick=tick)
                    last_tick = tick
                    if (
                        stop
                        or tick >= 285
                        or time.monotonic() - start >= args.max_seconds
                        or not args.watch
                    ):
                        break
                    time.sleep(0.25)
                    continue
                trader = client.get("trader")
                if start_pnl is None:
                    start_pnl = trader["nlv"]
                pnl = trader["nlv"] - start_pnl
                close_tick = liquidation_start(securities, max(cycle_durations[-20:], default=0))
                closing = (
                    closing
                    or getattr(client, "entry_halted", False)
                    or stop
                    or tick >= close_tick
                    or pnl <= -1500
                    or time.monotonic() - start >= args.max_seconds
                )
                signals = candidates(securities, curve, tick)
                audit(
                    "snapshot",
                    tick=tick,
                    trader=trader,
                    delta=-hedge_quantity(securities, tick, curve.current, curve.rate),
                    volatility=curve.current,
                    effective_volatility=curve.effective(tick),
                    positions={x["ticker"]: x["position"] for x in securities if x["position"]},
                    best=signals[:2],
                    parity_scan=parity_opportunities(securities, tick, curve.rate),
                    closing=closing,
                    close_tick=close_tick,
                )
                if args.trade:
                    fresh = client.get("case")
                    if (
                        fresh["name"] != case["name"]
                        or fresh["period"] != case["period"]
                        or fresh["status"] != "ACTIVE"
                        or fresh["tick"] < tick
                        or fresh["tick"] - tick > 2
                    ):
                        raise TradingError("Stale snapshot or round changed before order")
                    action = trade_once(client, securities, curve, tick, closing)
                    audit("decision", tick=tick, action=action)
                    if closing and action == "flat":
                        audit("finished_flat", trader=client.get("trader"))
                        break
                elif not args.watch or stop:
                    break
                last_tick = tick
                time.sleep(0.6)
                cycle_durations.append(time.monotonic() - cycle_start)
            else:
                raise TradingError("Time limit exhausted; inspect any remaining positions")
            if args.trade:
                audit("final_reconciliation", **confirm_flat(client))
        except (TradingError, ValueError, KeyError) as exc:
            audit("stopped", reason=str(exc))
            try:
                audit(
                    "reconciliation",
                    case=client.get("case"),
                    trader=client.get("trader"),
                    positions={
                        x["ticker"]: x["position"]
                        for x in client.get("securities")
                        if x["position"]
                    },
                    open_orders=client.get("orders?status=OPEN"),
                )
            except TradingError:
                audit("reconciliation_unavailable")
            return 1
        audit("log_saved", path=str(logfile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
