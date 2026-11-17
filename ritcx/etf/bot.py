"""Depth-aware tender and basket bot for the simulated RITCx ETF case."""

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
from .execution import (
    ETFClient,
    accept_and_unwind,
    basket_trade,
    external_book,
    hedge_fx,
    unwind_all,
)
from .model import basket_opportunities, execution_value, tender_opportunity

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
        "--passive-tenders",
        action="store_true",
        help="Try bounded passive tender exits before crossing the spread",
    )
    parser.add_argument("--no-baskets", action="store_true", help="Trade tenders only")
    parser.add_argument("--max-seconds", type=float, default=420)
    args = parser.parse_args()
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
        client = client_from_config(cfg, execute=args.trade, audit=audit, client_type=ETFClient)
        client.trader_id = cfg["RIT_USERNAME"]
        started = time.monotonic()
        last_tick = None
        first_period = None
        initial_pnl = None
        checked = set()
        liquidating = False
        try:
            while time.monotonic() - started < args.max_seconds + 20:
                read_start = time.perf_counter()
                state = read_many(
                    client, pool, ["case", "securities", "trader", "tenders", "orders?status=OPEN"]
                )
                case = state["case"]
                securities = state["securities"]
                tick = case["tick"]
                if case["name"] != "RITCx - Algorithmic ETF Arbitrage Case":
                    audit("case_changed", case=case)
                    break
                if last_tick is not None and (tick < last_tick or case["period"] != first_period):
                    audit("round_reset", case=case)
                    break
                if case["status"] != "ACTIVE":
                    audit("case_inactive", case=case)
                    if last_tick is not None or not args.watch or stopping:
                        break
                    time.sleep(0.5)
                    continue
                if state["orders?status=OPEN"]:
                    raise TradingError("Open orders found; reconcile before continuing")
                if last_tick is None:
                    first_period = case["period"]
                    client.bind_heat(case)
                    validate(case, securities, client.get("limits"))
                    if args.trade and any(
                        abs(x["position"]) >= (1 if x["ticker"] == "USD" else 1e-9)
                        for x in securities
                        if x["ticker"] in (*EQUITIES, "USD")
                    ):
                        raise TradingError("Initial ETF run requires flat equity positions")
                    initial_pnl = state["trader"]["nlv"]
                validate(case, securities, client.get("limits"))
                client.bind_snapshot(case)
                byname = {x["ticker"]: x for x in securities}
                liquidating = (
                    liquidating
                    or getattr(client, "entry_halted", False)
                    or stopping
                    or tick >= 275
                    or state["trader"]["nlv"] - initial_pnl <= -2500
                    or time.monotonic() - started >= args.max_seconds
                )
                positions = {x["ticker"]: x["position"] for x in securities if x["position"]}
                books = get_books(client, pool)
                signals = [] if args.no_baskets else basket_opportunities(books, securities, 500)
                tenders = []
                for tender in state["tenders"]:
                    if tender["tender_id"] not in checked:
                        offer = tender_opportunity(
                            tender,
                            books["RITC"],
                            securities,
                            tick,
                            fx_book=books["USD"],
                            seconds_per_slice=max(
                                1.5, 4 * max(client.order_latencies[-20:], default=0)
                            ),
                            passive_seconds=0.75 if args.passive_tenders else 0,
                        )
                        side = "SELL" if tender["action"] == "BUY" else "BUY"
                        depth = execution_value(books["RITC"], side, tender["quantity"])
                        fee = byname["RITC"]["trading_fee"] * tender["quantity"]
                        estimated_profit = (
                            None
                            if depth is None
                            else (
                                (depth[0] - tender["price"] * tender["quantity"])
                                * (1 if tender["action"] == "BUY" else -1)
                                - fee
                            )
                        )
                        audit(
                            "tender_evaluation",
                            tender=tender,
                            decision=offer,
                            complete_book_liquidity=depth is not None,
                            estimated_profit_usd=estimated_profit,
                            minimum_profit_usd=max(500, 0.08 * tender["quantity"]),
                        )
                        if offer:
                            tenders.append((offer, tender))
                audit(
                    "snapshot",
                    tick=tick,
                    trader=state["trader"],
                    positions=positions,
                    best=signals[:1],
                    snapshot_latency_ms=round(1000 * (time.perf_counter() - read_start), 2),
                    liquidating=liquidating,
                )
                if not args.trade:
                    if not args.watch or stopping:
                        break
                else:
                    fresh = client.get("case")
                    if (
                        fresh["name"] != case["name"]
                        or fresh["status"] != "ACTIVE"
                        or fresh["period"] != case["period"]
                        or fresh["tick"] < tick
                    ):
                        audit("case_changed_before_trade", case=fresh)
                        break
                    if fresh["tick"] - tick > 2:
                        audit("stale_snapshot", tick=tick)
                        continue
                    if liquidating:
                        securities = unwind_all(client, securities)
                        if (
                            not any(x["position"] for x in securities if x["ticker"] in EQUITIES)
                            and abs(next(x["position"] for x in securities if x["ticker"] == "USD"))
                            < 1
                        ):
                            audit(
                                "finished_flat",
                                trader=client.get("trader"),
                                positions={
                                    x["ticker"]: x["position"] for x in securities if x["position"]
                                },
                            )
                            break
                    else:
                        q = int(byname["RITC"]["position"])
                        basket_consistent = (
                            byname["BULL"]["position"] == -q and byname["BEAR"]["position"] == -q
                        )
                        if not basket_consistent:
                            raise TradingError("Unexpected inventory outside an ETF basket")
                        gap = (byname["RITC"]["bid"] + byname["RITC"]["ask"]) / 2 * (
                            byname["USD"]["bid"] + byname["USD"]["ask"]
                        ) / 2 - (
                            byname["BULL"]["bid"]
                            + byname["BULL"]["ask"]
                            + byname["BEAR"]["bid"]
                            + byname["BEAR"]["ask"]
                        ) / 2
                        if q and (1 if q > 0 else -1) * gap >= -0.03:
                            audit("basket_exit", gap=gap, quantity=q)
                            basket_trade(client, securities, -1 if q > 0 else 1, min(abs(q), 500))
                        elif q == 0 and tenders and tick < 250:
                            offer, tender = max(tenders, key=lambda x: x[0]["profit_usd"])
                            # Re-evaluate the offer on a fresh book and tick before acceptance.
                            check = client.get("case")
                            book = external_book(client, "RITC")
                            fx_book = external_book(client, "USD")
                            securities = client.get("securities")
                            latest = tender_opportunity(
                                tender,
                                book,
                                securities,
                                check["tick"],
                                fx_book=fx_book,
                                seconds_per_slice=max(
                                    1.5, 4 * max(client.order_latencies[-20:], default=0)
                                ),
                                passive_seconds=0.75 if args.passive_tenders else 0,
                            )
                            client.bind_snapshot(check)
                            if (
                                latest
                                and check["name"] == case["name"]
                                and check["status"] == "ACTIVE"
                                and check["period"] == case["period"]
                            ):
                                checked.add(tender["tender_id"])
                                audit("tender_signal", **latest)
                                accept_and_unwind(
                                    client, tender, securities, passive=args.passive_tenders
                                )
                        elif (
                            signals
                            and tick < 260
                            and abs(q) < 5000
                            and (q == 0 or q * signals[0]["direction"] > 0)
                        ):
                            signal_ = signals[0]
                            audit("basket_signal", **signal_)
                            basket_trade(client, securities, signal_["direction"], 500)
                        else:
                            hedge_fx(client, securities)
                last_tick = tick
                time.sleep(0.15)
            else:
                raise TradingError("Operating deadline exceeded; inspect positions")
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
        audit("log_saved", path=str(log))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TradingError as exc:
        raise SystemExit(str(exc))
