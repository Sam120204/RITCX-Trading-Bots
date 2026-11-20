"""Run the production ETF valuation/execution functions on a synthetic heat."""

from ..execution import confirm_flat
from ..runtime import TradingError
from .bot import validate
from .execution import external_book, unwind_all
from .glide import Glide
from .mock_market import ETFMarket, LocalETFClient
from .model import EQUITIES


class InterruptedSimulation(Exception):
    pass


def run_heat(seed, scenario="case", *, audit=None, on_snapshot=None, stopped=None):
    emit = audit or (lambda *args, **kwargs: None)
    stopped = stopped or (lambda: False)
    market = ETFMarket(seed, scenario)
    peak = drawdown = 0.
    status, state, error = "completed", "STARTING", None
    action = "waiting for a qualifying signal"
    closing = finished = False
    positions_at_error = {}
    glide = Glide()
    last_tick = -1

    def measure():
        nonlocal peak, drawdown
        row = market.snapshot()
        peak = max(peak, row["pnl"])
        drawdown = max(drawdown, peak - row["pnl"])
        row.update(max_drawdown=drawdown, state=state, last_action=action,
                   scenario=scenario, policy="tender_first_glide")
        return row

    def record(event, **data):
        measure()
        emit(event, **data)

    def snapshot():
        if stopped():
            raise InterruptedSimulation
        row = measure()
        emit("mock_snapshot", **row)
        if on_snapshot:
            on_snapshot(row, market.fills)
        if stopped():
            raise InterruptedSimulation

    market.on_tick = snapshot
    client = LocalETFClient(market, record)
    emit("local_simulation_started", seed=seed, scenario=scenario, version=1,
         source="local_synthetic_etf_market", settings=client.settings_report())
    try:
        snapshot()
        client.bind_heat(client.get("case"))
        validate(client.get("case"), client.get("securities"), client.get("limits"))
        while market.clock < 300:
            if stopped():
                raise InterruptedSimulation
            if finished:
                market.advance_time(300 - market.clock)
                break
            case = client.get("case")
            if case["tick"] == last_tick:
                market.advance_time(.1)
                continue
            last_tick = case["tick"]
            glide.finish_resting(client)
            securities = client.get("securities")
            client.get("trader")
            tenders = client.get("tenders")
            if client.get("orders?status=OPEN"):
                raise TradingError("Unresolved local orders")
            validate(case, securities, client.get("limits"))
            books = {t: external_book(client, t) for t in (*EQUITIES, "USD")}
            fresh = client.get("case")
            if fresh["status"] != "ACTIVE":
                break
            if fresh["tick"] - case["tick"] > 2:
                action = "refreshing stale snapshot"
                continue
            tick = fresh["tick"]
            client.bind_snapshot(fresh)
            closing = closing or getattr(client, "entry_halted", False)
            state = "CLOSING" if closing else "TRADING"
            if closing:
                action = "closing inventory and hedging USD"
                unwind_all(client, securities)
                confirm_flat(client)
                finished, state = True, "FLAT - watching to 300s"
            else:
                if tick < 297:
                    glide.cycle(client, case, securities, books, tenders)
                action = "working tender inventory" if glide.resting else "evaluating tenders and basket"
            emit("decision", tick=tick, action=action, closing=closing)
            measure()
            market.advance_time(.15)
    except InterruptedSimulation:
        status, state = "interrupted", "INTERRUPTED (positions saved)"
    except (TradingError, ValueError, KeyError) as exc:
        status, state, error = "error", "TRADING ERROR - watching settlement", str(exc)
        positions_at_error = market.snapshot()["positions"]
        emit("error", reason=error, tick=market.tick, positions=positions_at_error)
        try:
            market.advance_time(300 - market.clock)
        except InterruptedSimulation:
            status, state = "interrupted", "INTERRUPTED after trading error"
    settled_flat = market.tick == 300 and not market.snapshot()["positions"] and not any(
        o["status"] == "OPEN" for o in market.receipts.values()
    )
    if status == "completed":
        finished = finished or (settled_flat and not any(
            abs(q) >= (1 if ticker == "USD" else 1e-9)
            for ticker, q in market.settled_positions.items()
        ))
        state = "COMPLETE"
    result = measure()
    result.update(seed=seed, status=status, error=error, source="local_synthetic_etf_market",
                  version=1, settings=client.settings_report(), positions_at_error=positions_at_error,
                  strategy_finished_flat=finished, account_flat_after_settlement=settled_flat, **glide.summary(client))
    emit("mock_summary", **result)
    if on_snapshot:
        on_snapshot(result, market.fills)
    return result
