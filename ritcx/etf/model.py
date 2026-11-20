"""Executable basket/tender valuation, with conservative projected inventory."""

import itertools
import math

EQUITIES = ("BULL", "BEAR", "RITC")
GLIDE_HORIZON = 30


def execution_value(book, action, quantity):
    """Return (total quote-currency value, worst price), or None if depth is short."""
    if action not in ("BUY", "SELL") or not math.isfinite(quantity):
        raise ValueError("Invalid action or quantity")
    if quantity <= 0:
        return None
    levels = book["asks" if action == "BUY" else "bids"]
    for level in levels:
        values = (level["price"], level["quantity"], level.get("quantity_filled", 0))
        if not all(math.isfinite(x) and x >= 0 for x in values) or values[0] <= 0:
            raise ValueError("Nonfinite or negative order-book data")
    remaining, value = quantity, 0.0
    for level in sorted(levels, key=lambda x: x["price"], reverse=action == "SELL"):
        available = max(0, level["quantity"] - level.get("quantity_filled", 0))
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


def quotes(securities):
    byname = {s["ticker"]: s for s in securities}
    mids = {}
    for ticker in (*EQUITIES, "USD"):
        bid, ask = byname[ticker].get("bid"), byname[ticker].get("ask")
        if not all(isinstance(p, (float, int)) and math.isfinite(p) for p in (bid, ask)):
            return None
        if not 0 < bid <= ask:
            return None
        mids[ticker] = (bid + ask) / 2
    return mids["RITC"], (mids["BULL"] + mids["BEAR"]) / mids["USD"]


def tender_inventory(securities):
    pos = {s["ticker"]: s["position"] for s in securities}
    if pos["BULL"] != pos["BEAR"]:
        raise ValueError("Unbalanced basket legs; reconcile before trading")
    return int(pos["RITC"] + pos["BULL"])


def tender_opportunity(
    tender, book, securities, tick, *, horizon=GLIDE_HORIZON, hold_to_close=False
):
    """Compute a fixed-price tender decision, including the signed inventory offset."""
    quantity, price = tender.get("quantity"), tender.get("price")
    if (
        not tender.get("is_fixed_bid")
        or tender.get("ticker") != "RITC"
        or tender.get("action") not in ("BUY", "SELL")
        or not isinstance(quantity, (float, int))
        or not 0 < quantity <= 100000
        or int(quantity) != quantity
        or not isinstance(price, (float, int))
        or not math.isfinite(price)
        or price <= 0
        or tender["expires"] - tick < 2
        or tick >= 297
    ):
        return None
    prices = quotes(securities)
    if prices is None:
        return None
    mid, nav = prices
    etf = next(s for s in securities if s["ticker"] == "RITC")
    sign = 1 if tender["action"] == "BUY" else -1
    offset = min(quantity, max(0, -sign * tender_inventory(securities)))
    added = quantity - offset
    clip = min(max(added or quantity, 60000) / horizon, etf["max_trade_size"])
    execution = execution_value(book, "SELL" if sign > 0 else "BUY", clip)
    half = (etf["ask"] - etf["bid"]) / 2
    taker = (abs(mid - execution[0] / clip) if execution else half + 0.03) + etf["trading_fee"]
    maker = max(0, half - 0.01) - etf.get("limit_order_rebate", 0)
    cost = ((maker + taker) / 2 + 0.02) * (added - offset) / quantity
    bar = 0.03
    if hold_to_close or (tick >= 285 and added > 0):
        cost, bar = 0, 0.08 if hold_to_close else 0.10
    fair = (mid + nav) / 2
    edge = sign * (fair - price) - cost
    projected = project_positions(
        securities, {"RITC": sign * quantity}, {"RITC": price}, charge_fees=False
    )
    verdict = "pass" if edge < bar - 1e-9 else "take" if within_risk(projected, {}) else "blocked"
    return dict(
        tender_id=tender["tender_id"],
        quantity=int(quantity),
        action=tender["action"],
        price=price,
        mid=mid,
        nav=nav,
        fair=fair,
        exit_cost_usd=cost,
        bar_usd=bar,
        edge_usd=edge,
        profit_usd=edge * quantity,
        verdict=verdict,
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
        edge = (raw - fee) / quantity
        if edge >= 0.06 - 1e-9:
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
