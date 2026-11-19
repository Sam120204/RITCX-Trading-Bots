"""Cost-aware trading toward the maker's announced-volatility convergence target.

This target is a short-horizon quote hypothesis, not a guaranteed expiry value.
The legacy term-variance model remains available for comparison.
"""

import math

from ..execution import submit_change
from .model import VolatilityCurve, candidates, implied_volatility, option_values


def signals(securities, curve, tick, policy=None):
    if policy is not None and policy.algorithm == "forecast_adaptive":
        return candidates(securities, curve, tick)
    stock = next(s for s in securities if s["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    result = []
    for sec in securities:
        if sec["type"] != "OPTION":
            continue
        fair, delta = option_values(sec, spot, tick, curve.current, curve.rate)
        # Both commissions, exit half-spread, stock round-trip costs, and a
        # small reserve for subsequent delta adjustments. Entry crosses ask/bid.
        cost = (
            2 * sec["trading_fee"] / sec["size"]
            + (sec["ask"] - sec["bid"]) / 2
            + 0.06 * abs(delta)
            + 0.01
        )
        for action, edge in (("BUY", fair - sec["ask"] - cost), ("SELL", sec["bid"] - fair - cost)):
            if min(fair, sec["bid"]) >= 0.05 and edge > 0:
                result.append(
                    dict(ticker=sec["ticker"], action=action, edge=edge, fair=fair, delta=delta)
                )
    return sorted(result, key=lambda s: s["edge"], reverse=True)


def _risk_vectors(securities, curve, tick, cycle_seconds):
    stock = next(s for s in securities if s["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    opts = [s for s in securities if s["type"] == "OPTION"]
    horizon = max(3, cycle_seconds)
    shock = 3 * curve.current * math.sqrt(horizon / 3600)
    risk = []
    for sec in opts:
        quoted = implied_volatility(
            (sec["bid"] + sec["ask"]) / 2,
            spot,
            int(sec["ticker"][3:5]),
            max(0, 300 - tick) / 3600,
            curve.rate,
            sec["ticker"][-1],
        )
        quoted = curve.current if quoted is None else quoted
        quote_delta = option_values(sec, spot, tick, quoted, curve.rate)[1] * sec["size"]
        current_delta = option_values(sec, spot, tick, curve.current, curve.rate)[1] * sec["size"]
        # Use a fixed scenario count for all options, even when vol values coincide.
        stressed = [
            option_values(
                sec, max(0.01, spot * (1 + move)), min(300, tick + horizon), vol, curve.rate
            )[1]
            * sec["size"]
            for move in (-shock, 0, shock)
            for vol in (quoted, curve.current * 0.75, curve.current, curve.current * 1.25)
        ]
        risk.append((sec, quote_delta, current_delta, stressed))
    return stock, risk


def admissible_quantity(securities, ticker, change, curve, tick, policy, cycle_seconds=3):
    """Clip entry for limits, pre-hedge delta, hedge capacity, and spot stress.

    +/-3 standard deviations over at least three seconds, and +/-25% sigma,
    form an admission envelope. They do not bound every possible market move.
    A zero change returns 1 for an admissible current book, otherwise 0.
    """
    stock, risk = _risk_vectors(securities, curve, tick, cycle_seconds)
    opts = [r[0] for r in risk]
    # Leave room for small market moves on entry. Existing positions retain
    # the original exit envelope: using the same threshold for both causes
    # repeated buys and risk closes at the boundary.
    fraction = policy.entry_risk_fraction if change else 1.0
    direction = 1 if change >= 0 else -1
    for quantity in range(min(abs(int(change)), policy.entry_size), -1, -1):
        if change and quantity == 0:
            return 0
        positions = [
            s["position"] + (direction * quantity if s["ticker"] == ticker else 0) for s in opts
        ]
        if sum(map(abs, positions)) > policy.option_cap or abs(sum(positions)) > policy.net_cap:
            continue
        option_delta = sum(p * r[1] for p, r in zip(positions, risk))
        hedge_target = -round(option_delta)
        if abs(stock["position"]) > policy.stock_cap or abs(hedge_target) > policy.stock_cap:
            continue
        if abs(stock["position"] + option_delta) > 4500:
            continue
        if abs(stock["position"] + sum(p * r[2] for p, r in zip(positions, risk))) > 6000:
            continue
        if any(
            abs(hedge_target + sum(p * r[3][i] for p, r in zip(positions, risk))) > 5500 * fraction
            for i in range(12)
        ):
            continue
        # Also stress the submitted option leg before its hedge arrives.
        if any(
            abs(stock["position"] + sum(p * r[3][i] for p, r in zip(positions, risk)))
            > 6500 * fraction
            for i in range(12)
        ):
            continue
        return direction * quantity if change else 1
    return 0


def _close_risk(stock, risk, ticker, change, policy):
    positions = [r[0]["position"] + (change if r[0]["ticker"] == ticker else 0) for r in risk]
    delta = sum(p * r[1] for p, r in zip(positions, risk))
    target = max(-policy.stock_cap, min(policy.stock_cap, -round(delta)))
    immediate = max(
        abs(stock["position"] + delta),
        abs(stock["position"] + sum(p * r[2] for p, r in zip(positions, risk))),
    )
    stressed = max(
        abs(target + sum(p * r[3][i] for p, r in zip(positions, risk))) for i in range(12)
    )
    # Compare intermediate delta, required hedge capacity, and residual gamma
    # risk on the same normalized scale. Contract count alone is insufficient.
    # An existing delta breach needs immediate relief before optimizing a
    # hypothetical later shock. Otherwise an OTM leg can win the stress score
    # while leaving a currently fine-incurring ITM exposure untouched.
    return (
        max(0, abs(stock["position"] + delta) - 6000),
        max(immediate / 6500, abs(delta) / policy.stock_cap, stressed / 5500),
    )


def trade(client, securities, curve, tick, flatten, policy):
    from .bot import hedge

    case = client.check_heat()
    if case["tick"] != tick:
        securities = client.get("securities")
        curve = VolatilityCurve.from_news(client.get("news"), case["tick"])
        tick = case["tick"]
        client.bind_snapshot(case)
    before = next(s["position"] for s in securities if s["ticker"] == "RTM")
    securities = hedge(client, securities, curve, tick, policy=policy)
    if before != next(s["position"] for s in securities if s["ticker"] == "RTM"):
        return "hedged"
    if client.snapshot_tick != tick:
        # A no-fill hedge refresh must not authorize a decision using old news/time.
        return "refresh"
    stock = next(s for s in securities if s["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    positions = [s for s in securities if s["type"] == "OPTION" and s["position"]]
    horizon = getattr(client, "observed_cycle_seconds", 3)
    unsafe = positions and not admissible_quantity(
        securities, positions[0]["ticker"], 0, curve, tick, policy, horizon
    )
    close_candidates = sorted(positions, key=lambda s: abs(s["position"]), reverse=True)
    if unsafe:
        risk_stock, risk_vectors = _risk_vectors(securities, curve, tick, horizon)
        close_candidates.sort(
            key=lambda s: _close_risk(
                risk_stock,
                risk_vectors,
                s["ticker"],
                -int(math.copysign(min(abs(s["position"]), 40, policy.entry_size), s["position"])),
                policy,
            )
        )
    for sec in close_candidates:
        sigma = (
            curve.effective(tick, "low" if sec["position"] > 0 else "high")
            if policy.algorithm == "forecast_adaptive"
            else curve.current
        )
        fair, _ = option_values(sec, spot, tick, sigma, curve.rate)
        residual = fair - sec["bid"] if sec["position"] > 0 else sec["ask"] - fair
        if flatten or unsafe or residual <= 0.035:
            change = -int(
                math.copysign(min(abs(sec["position"]), 40, policy.entry_size), sec["position"])
            )
            securities = submit_change(client, securities, sec["ticker"], change)
            hedge(client, securities, curve, tick, force=True, policy=policy)
            return "flatten_option" if flatten else "reduce_risk" if unsafe else "exit_edge"
    if flatten:
        if stock["position"]:
            submit_change(client, securities, "RTM", -stock["position"])
            return "flatten_stock"
        return "flat"
    if tick >= 270:
        return "no_new_entries"
    choices = []
    for choice in signals(securities, curve, tick, policy):
        if choice["edge"] <= policy.minimum_edge:
            continue
        # Stronger dislocations earn a larger ticket; the portfolio risk envelope
        # can clip this to any smaller number, including zero.
        desired = max(1, min(policy.entry_size, int(policy.entry_size * choice["edge"] / 0.20)))
        change = admissible_quantity(
            securities,
            choice["ticker"],
            desired if choice["action"] == "BUY" else -desired,
            curve,
            tick,
            policy,
            horizon,
        )
        if change:
            choices.append((choice["edge"] * abs(change), choice, change))
    if choices:
        _, choice, change = max(choices, key=lambda c: c[0])
        if client.check_heat()["tick"] != tick:
            return "refresh"
        client.audit("signal", tick=tick, target=policy.algorithm, quantity=abs(change), **choice)
        securities = submit_change(client, securities, choice["ticker"], change)
        hedge(client, securities, curve, tick, policy=policy)
        return "entry"
    return "hold"
