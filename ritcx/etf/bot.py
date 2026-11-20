"""Tender-first NAV glide bot for the simulated RITCx ETF case."""

import argparse
import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from ..api import client_from_config
from ..execution import confirm_flat
from ..runtime import (
    ROOT,
    TradingError,
    account_lock,
    open_audit_log,
    read_config,
    validate_profile,
)
from .execution import ETFClient, unwind_all
from .glide import Glide

EQUITIES = ("BULL", "BEAR", "RITC")


def read_many(client, pool, endpoints):
    futures = {ep: pool.submit(client.get, ep) for ep in endpoints}
    return {ep: f.result() for ep, f in futures.items()}


def get_books(client, pool):
    endpoints = [f"securities/book?ticker={ticker}&limit=1000" for ticker in (*EQUITIES, "USD")]
    values = read_many(client, pool, endpoints)
    # Remove own orders so valuation cannot rely on self-execution.
    result = {ticker: values[ep] for ticker, ep in zip((*EQUITIES, "USD"), endpoints)}
    for book in result.values():
        for side in ("bids", "asks"):
            book[side] = [x for x in book[side] if x.get("trader_id") != client.trader_id]
    return result


def validate(case, securities, limits):
    if (
        case["name"] != "RITCx - Algorithmic ETF Arbitrage Case"
        or case["ticks_per_period"] != 300
        or case["total_periods"] != 1
    ):
        raise TradingError("ETF bot refuses a different case")
    if {x["ticker"] for x in securities} != {"CAD", "USD", *EQUITIES}:
        raise TradingError("Unexpected ETF security universe")
    for sec in securities:
        equity = sec["ticker"] in EQUITIES
        currency = "" if sec["ticker"] == "CAD" else "USD" if sec["ticker"] == "RITC" else "CAD"
        if (
            sec["type"] != ("STOCK" if equity else "CURRENCY")
            or sec["size"] != 1
            or sec["currency"] != currency
            or sec["trading_fee"] != (0.02 if equity else 0)
            or sec["max_trade_size"] < (10000 if equity else 2500000)
            or sec.get("execution_delay_ms", 0) != 0
        ):
            raise TradingError("Unexpected contract, currency, fee or execution settings")
    limit = next((x for x in limits if x["name"] == "LIMIT-STOCK"), None)
    if limit is None or limit["gross_limit"] < 250000 or limit["net_limit"] < 200000:
        raise TradingError("Live equity limits below configured caps")
    cash_limit = next((x for x in limits if x["name"] == "LIMIT-CASH"), None)
    if (
        cash_limit is None
        or cash_limit["gross_limit"] < 8000000
        or cash_limit["net_limit"] < 8000000
    ):
        raise TradingError("Live cash limits below configured cap")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true")
    parser.add_argument("--practice", action="store_true")
    parser.add_argument("--contest", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / ".env")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="Observe a heat without submitting orders"
    )
    parser.add_argument(
        "--hold-to-close",
        action="store_true",
        help="Expensive-exit fallback: hold selective tenders for settlement",
    )
    parser.add_argument(
        "--loss-limit-cad",
        type=float,
        help="Optional heat loss trigger for early liquidation (previous policy: 2500)",
    )
    parser.add_argument("--no-baskets", action="store_true", help="Trade tenders only")
    parser.add_argument("--max-seconds", type=float, default=420)
    args = parser.parse_args()
    if args.loss_limit_cad is not None and not 0 < args.loss_limit_cad < float("inf"):
        parser.error("--loss-limit-cad requires a positive finite amount")
    if args.dry_run and args.trade:
        parser.error("--dry-run and --trade are mutually exclusive")
    if args.dry_run:
        args.watch = True
    cfg = read_config(args.config)
    try:
        validate_profile(cfg, args.practice, args.contest, args.trade)
    except TradingError as exc:
        parser.error(str(exc))
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logdir = Path(cfg.get("RIT_LOG_DIR", ROOT / "logs"))
    logdir.mkdir(exist_ok=True, parents=True)
    with (
        account_lock(cfg, args.lock_fd) if args.trade else nullcontext(),
        open_audit_log(logdir, "etf") as (log, out),
        ThreadPoolExecutor(max_workers=6) as pool,
    ):
        rate_limits = 0

        def audit(event, **data):
            nonlocal rate_limits
            if event == "rate_limited" or (
                event == "request_rejected" and data.get("http_status") == 429
            ):
                rate_limits += 1
            row = dict(time=datetime.now(timezone.utc).isoformat(), event=event, **data)
            text = json.dumps(row, allow_nan=False)
            out.write(text + "\n")
            out.flush()
            print(text, flush=True)

        audit(
            "run_started",
            execution_enabled=args.trade,
            environment=cfg.get("RIT_ENVIRONMENT", "practice"),
            strategy_revision="tender-first-glide",
            hold_to_close=args.hold_to_close,
            loss_limit_cad=args.loss_limit_cad,
        )
        client = client_from_config(cfg, execute=args.trade, audit=audit, client_type=ETFClient)
        client.trader_id = cfg["RIT_USERNAME"]
        started = time.monotonic()
        last_tick = None
        first_period = None
        initial_pnl = None
        final_trader = None
        glide = Glide(hold_to_close=args.hold_to_close)
        liquidating = False
        try:
            while time.monotonic() - started < args.max_seconds + 20:
                read_start = time.perf_counter()
                case = client.get("case")
                tick = case["tick"]
                if case["name"] != "RITCx - Algorithmic ETF Arbitrage Case":
                    audit("case_changed", case=case)
                    if args.trade and last_tick is not None:
                        raise TradingError("Case changed before settlement was confirmed")
                    break
                if (
                    case["status"] == "ACTIVE"
                    and last_tick is not None
                    and (tick < last_tick or case["period"] != first_period)
                ):
                    audit("round_reset", case=case)
                    if args.trade:
                        raise TradingError("Heat reset before settlement was confirmed")
                    break
                if case["status"] != "ACTIVE":
                    audit("case_inactive", case=case)
                    if last_tick is not None or not args.watch or stopping:
                        break
                    time.sleep(0.5)
                    continue
                if (
                    tick == last_tick
                    and not stopping
                    and time.monotonic() - started < args.max_seconds
                ):
                    time.sleep(0.10)
                    continue
                client.bind_snapshot(case)
                glide.finish_resting(client)
                state = read_many(
                    client, pool, ["securities", "trader", "tenders", "orders?status=OPEN"]
                )
                securities = state["securities"]
                if state["orders?status=OPEN"]:
                    raise TradingError("Open orders found; reconcile before continuing")
                if last_tick is None:
                    first_period = case["period"]
                    client.bind_heat(case)
                    if args.trade and any(
                        abs(x["position"]) >= (1 if x["ticker"] == "USD" else 1e-9)
                        for x in securities
                        if x["ticker"] in (*EQUITIES, "USD")
                    ):
                        raise TradingError("Initial ETF run requires flat equity positions")
                    initial_pnl = state["trader"]["nlv"]
                limits = client.get("limits")
                validate(case, securities, limits)
                if last_tick is None:
                    audit(
                        "etf_contracts",
                        horizon=glide.horizon,
                        gamma=glide.gamma,
                        limits=limits,
                        securities=[
                            {
                                k: sec.get(k)
                                for k in (
                                    "ticker",
                                    "trading_fee",
                                    "limit_order_rebate",
                                    "max_trade_size",
                                    "limits",
                                )
                            }
                            for sec in securities
                        ],
                    )
                client.bind_snapshot(case)
                liquidating = (
                    liquidating
                    or getattr(client, "entry_halted", False)
                    or stopping
                    or (
                        args.loss_limit_cad is not None
                        and state["trader"]["nlv"] - initial_pnl <= -args.loss_limit_cad
                    )
                    or time.monotonic() - started >= args.max_seconds
                )
                positions = {x["ticker"]: x["position"] for x in securities if x["position"]}
                books = get_books(client, pool)
                audit(
                    "snapshot",
                    tick=tick,
                    trader=state["trader"],
                    positions=positions,
                    snapshot_latency_ms=round(1000 * (time.perf_counter() - read_start), 2),
                    liquidating=liquidating,
                )
                if args.trade and liquidating:
                    unwind_all(client, securities)
                    audit("finished_flat", **confirm_flat(client))
                    break
                fresh = client.get("case")
                if (
                    fresh["name"] != case["name"]
                    or fresh["status"] != "ACTIVE"
                    or fresh["period"] != case["period"]
                    or fresh["tick"] < tick
                ):
                    audit("case_changed_before_trade", case=fresh)
                    if args.trade and (
                        fresh["name"] != case["name"]
                        or fresh["period"] != case["period"]
                        or (fresh["status"] == "ACTIVE" and fresh["tick"] < tick)
                    ):
                        raise TradingError("Heat changed before settlement was confirmed")
                    break
                if tick >= 297:
                    if not args.trade and (not args.watch or stopping):
                        break
                    last_tick = tick
                    time.sleep(0.15)
                    continue
                if fresh["tick"] - tick > 2:
                    audit("stale_snapshot", tick=tick)
                    continue
                glide.cycle(
                    client, case, securities, books, state["tenders"], no_baskets=args.no_baskets
                )
                if not args.trade and (not args.watch or stopping):
                    break
                last_tick = tick
                time.sleep(0.15)
            else:
                raise TradingError("Operating deadline exceeded; inspect positions")
            if args.trade:
                # The exchange cancels remaining orders and settles inventory at natural expiry.
                # Reconciliation reads the account, including when no settlement was observed.
                final = confirm_flat(client)
                final_trader = final["trader"]
                audit("final_reconciliation", **final)
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
        finally:
            audit(
                "heat_summary",
                **glide.summary(client),
                rate_limited_responses=rate_limits,
                pnl_cad=(
                    final_trader["nlv"] - initial_pnl
                    if final_trader is not None and initial_pnl is not None
                    else None
                ),
                total_fines=final_trader.get("total_fines") if final_trader else None,
            )
        audit("log_saved", path=str(log))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TradingError as exc:
        raise SystemExit(str(exc))
