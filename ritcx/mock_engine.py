"""Deterministic local heat runner shared by the dashboard and paired evaluation."""

from dataclasses import asdict

from .execution import confirm_flat
from .mock_market import MockClient, MockMarket
from .runtime import TradingError
from .volatility.bot import LIVE_POLICIES, liquidation_start, trade_once, validate
from .volatility.model import VolatilityCurve, candidates

POLICIES = LIVE_POLICIES


class InterruptedSimulation(Exception):
    pass


def run_heat(
    seed, scenario="standard", policy_name="buffered", *, audit=None, on_snapshot=None, stopped=None
):
    """Run 300 simulation seconds; callbacks may pace wall time without altering the path.

    Failed trading stops mutations but the simulated market continues to expiry.
    Reports retain the failure, its inventory, and any terminal settlement. Ctrl-C
    instead returns the partial run immediately without advancing to settlement.
    """
    emit = audit or (lambda *args, **kwargs: None)
    stopped = stopped or (lambda: False)
    policy = POLICIES[policy_name]
    market = MockMarket(seed, scenario=scenario)
    peak = drawdown = 0.0
    max_gross = 0
    first_trade = None
    status, state, error = "completed", "STARTING", None
    positions_at_error = {}
    closing = finished = False
    cycles = []
    action = "waiting for a qualifying signal"

    def measure():
        nonlocal peak, drawdown, max_gross, first_trade
        row = market.snapshot()
        peak = max(peak, row["pnl"])
        drawdown = max(drawdown, peak - row["pnl"])
        gross = sum(abs(v) for k, v in row["positions"].items() if k != "RTM")
        max_gross = max(max_gross, gross)
        if first_trade is None and market.fills:
            first_trade = market.fills[0]["tick"]
        row.update(
            max_drawdown=drawdown,
            scenario=scenario,
            policy=policy_name,
            state=state,
            last_action=action,
        )
        return row

    def record(event, **data):
        if event in ("order_response", "fill_confirmed"):
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
    client = MockClient(market, record)
    client.option_ticket_limit = policy.entry_size
    emit(
        "local_simulation_started",
        version=2,
        seed=seed,
        scenario=scenario,
        settings=asdict(market.scenario),
        policy=policy_name,
        policy_settings=asdict(policy),
    )
    try:
        snapshot()
        client.bind_heat(client.get("case"))
        validate(client.get("case"), client.get("securities"), client.get("limits"), policy)
        state = "TRADING"
        while market.clock < 300:
            if stopped():
                raise InterruptedSimulation
            if finished:
                market.advance_time(300 - market.clock)
                break
            cycle_start = market.clock
            case = client.get("case")
            tick = case["tick"]
            if case["status"] != "ACTIVE":
                break
            client.bind_snapshot(case)
            securities = client.get("securities")
            if scenario.startswith("case"):
                # Match production's recurring reads; their elapsed time is part
                # of the comparison, including heat/receipt checks below.
                validate(case, securities, client.get("limits"), policy)
                if client.get("orders?status=OPEN"):
                    raise TradingError("Open orders in local market")
            curve = VolatilityCurve.from_news(client.get("news"), tick)
            pnl = client.get("trader")["nlv"]
            client.observed_cycle_seconds = max(cycles[-20:], default=3)
            closing = (
                closing
                or getattr(client, "entry_halted", False)
                or (
                    tick >= liquidation_start(securities, max(cycles[-20:], default=0), policy)
                    or pnl <= -policy.stop_loss
                )
            )
            state = "CLOSING" if closing else "TRADING"
            if scenario.startswith("case"):
                client.check_heat()
            action = trade_once(client, securities, curve, tick, closing, policy=policy)
            emit("decision", tick=tick, action=action, closing=closing)
            if action == "hold":
                signals = [
                    c
                    for c in candidates(securities, curve, tick)
                    if c["edge"] > policy.minimum_edge
                ]
                action = "risk capacity / no admissible entry" if signals else "no edge after costs"
            if closing and action == "flat":
                confirm_flat(client)
                finished = True
                state = "FLAT - watching to 300s"
            measure()
            # Preserve the old one-cycle-per-second demo. Other scenarios use
            # production's 0.1-second idle plus time consumed by each request.
            idle = 1.0 if scenario == "demo" else policy.idle_seconds
            cycles.append(market.clock - cycle_start + idle)
            market.advance_time(idle)
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
    if status == "completed":
        state = "COMPLETE"
    result = measure()
    result.update(
        seed=seed,
        status=status,
        error=error,
        source="local_synthetic_market",
        version=2,
        settings=asdict(market.scenario),
        policy_settings=asdict(policy),
        positions_at_error=positions_at_error,
        first_trade_tick=first_trade,
        max_gross_options=max_gross,
        strategy_finished_flat=finished,
        max_simulated_order_delay=max(client.simulated_order_delays, default=0),
    )
    emit("mock_summary", **result)
    if on_snapshot:
        on_snapshot(result, market.fills)
    return result
