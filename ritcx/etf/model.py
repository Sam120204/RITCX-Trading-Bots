"""Executable basket/tender valuation, with conservative projected inventory."""

import itertools
import math

EQUITIES = ("BULL", "BEAR", "RITC")


def execution_value(book, action, quantity, depth_fraction=1.0):
    """Return (total quote-currency value, worst price), or None if depth is short."""
    if action not in ("BUY", "SELL") or not math.isfinite(quantity):
        raise ValueError("Invalid action or quantity")
    if not 0 < depth_fraction <= 1:
        raise ValueError("Depth fraction must lie in (0, 1]")
    if quantity <= 0:
        return None
    levels = book["asks" if action == "BUY" else "bids"]
    for level in levels:
        values = (level["price"], level["quantity"], level.get("quantity_filled", 0))
        if not all(math.isfinite(x) and x >= 0 for x in values) or values[0] <= 0:
            raise ValueError("Nonfinite or negative order-book data")
    remaining, value = quantity, 0.0
    for level in sorted(levels, key=lambda x: x["price"], reverse=action == "SELL"):
        available = max(0, level["quantity"] - level.get("quantity_filled", 0)) * depth_fraction
        take = min(remaining, available)
        value += take * level["price"]
        remaining -= take
        if remaining <= 1e-8:
            return value, level["price"]
    return None


def within_risk(securities, changes, gross_cap=250000, net_cap=200000, cash_cap=8000000):
    """Check BOTH published and live weights: their net limits are not equivalent."""
    positions = {s["ticker"]: s["position"] + changes.get(s["ticker"], 0) for s in securities}
    if set(changes) - positions.keys() or not all(math.isfinite(p) for p in positions.values()):
        return False
    if sum(abs(positions.get(t, 0)) for t in ("CAD", "USD")) > cash_cap:
        return False
    for published in (True, False):
        gross = net = 0
        for sec in securities:
            if sec["ticker"] not in EQUITIES:
                continue
            weight = (
                (2 if sec["ticker"] == "RITC" else 1)
                if published
                else max(
                    (x["units"] for x in sec["limits"] if x["name"] == "LIMIT-STOCK"), default=1
                )
            )
            if not math.isfinite(weight) or weight <= 0:
                return False
            gross += abs(positions[sec["ticker"]]) * weight
            net += positions[sec["ticker"]] * weight
        if gross > gross_cap or abs(net) > net_cap:
            return False
    return True


def project_positions(securities, changes, prices=None, charge_fees=True):
    """Project inventory AND settlement cash for equity legs; never infer actual fills."""
    result = {s["ticker"]: dict(s) for s in securities}
    for ticker, change in changes.items():
        sec = result[ticker]
        sec["position"] += change
        if ticker not in EQUITIES or not change:
            continue
        price = (
            prices[ticker] if prices and ticker in prices else sec["ask" if change > 0 else "bid"]
        )
        fee = sec["trading_fee"] if charge_fees else 0
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid projection price")
        result[sec["currency"]]["position"] -= change * price + abs(change) * fee
    return list(result.values())


def basket_allowed(securities, direction, quantity, **risk_caps):
    if direction not in (-1, 1) or quantity <= 0:
        return False
    legs = [
        ("RITC", direction * quantity),
        ("BULL", -direction * quantity),
        ("BEAR", -direction * quantity),
    ]
    # Any subset may have filled before a disruption, so check every intermediate state.
    return all(
        within_risk(project_positions(securities, dict(subset)), {}, **risk_caps)
        for n in range(1, 4)
        for subset in itertools.combinations(legs, n)
    )


def tender_opportunity(
    tender,
    book,
    securities,
    tick,
    *,
    fx_book=None,
    seconds_per_slice=1.5,
    passive_seconds=0,
    depth_fraction=0.8,
):
    if (
        not tender.get("is_fixed_bid")
        or tender["ticker"] != "RITC"
        or tender["expires"] - tick < 2
        or tick >= 250
    ):
        return None
    quantity, price = tender["quantity"], tender["price"]
    if (
        not all(math.isfinite(x) for x in (quantity, price))
        or quantity != int(quantity)
        or not 0 < quantity <= 100000
        or price <= 0
    ):
        return None
    action = tender["action"]
    if action not in ("BUY", "SELL"):
        return None
    required_seconds = math.ceil(quantity / 10000) * (seconds_per_slice + passive_seconds) + 8
    if tick + required_seconds >= 275:
        return None
    change = quantity if action == "BUY" else -quantity
    projected = project_positions(securities, {"RITC": change}, {"RITC": price}, charge_fees=False)
    if not within_risk(projected, {}):
        return None
    unwind = "SELL" if action == "BUY" else "BUY"
    execution = execution_value(book, unwind, quantity, depth_fraction)
    if execution is None:
        return None
    total, worst = execution
    fee = next(x["trading_fee"] for x in securities if x["ticker"] == "RITC")
    profit = (total - price * quantity) * (1 if action == "BUY" else -1) - fee * quantity
    if profit < max(500, 0.08 * quantity):
        return None
    # Both cash extremes matter, including the state after the unwind.
    after_exit = project_positions(projected, {"RITC": -change}, {"RITC": total / quantity})
    if not within_risk(after_exit, {}):
        return None
    fx = execution_value(fx_book, "SELL", profit) if fx_book is not None else None
    if fx_book is not None and fx is None:
        return None
    return dict(
        tender_id=tender["tender_id"],
        quantity=int(quantity),
        action=action,
        unwind_action=unwind,
        profit_usd=profit,
        profit_cad=fx[0] if fx else None,
        worst_price=worst,
        required_seconds=required_seconds,
        depth_fraction=depth_fraction,
    )


def basket_opportunities(books, securities, quantity):
    if not isinstance(quantity, int) or not 0 < quantity <= 10000:
        return []
    byname = {x["ticker"]: x for x in securities}
    result = []
    for direction in (1, -1):
        etf_action = "BUY" if direction == 1 else "SELL"
        stock_action = "SELL" if direction == 1 else "BUY"
        etf = execution_value(books["RITC"], etf_action, quantity)
        bull = execution_value(books["BULL"], stock_action, quantity)
        bear = execution_value(books["BEAR"], stock_action, quantity)
        if None in (etf, bull, bear):
            continue
        fx = execution_value(books["USD"], etf_action, etf[0])
        if fx is None:
            continue
        rate = fx[0] / etf[0]
        raw = (bull[0] + bear[0] - fx[0]) * direction
        fee = quantity * (
            byname["BULL"]["trading_fee"]
            + byname["BEAR"]["trading_fee"]
            + byname["RITC"]["trading_fee"] * rate
        )
        edge = (raw - 2 * fee) / quantity
        if edge > 0.15:
            result.append(
                dict(
                    direction=direction,
                    quantity=quantity,
                    edge_cad=edge,
                    fx_notional=etf[0],
                    fx_average=rate,
                )
            )
    return sorted(result, key=lambda x: x["edge_cad"], reverse=True)
