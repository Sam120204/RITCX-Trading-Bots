"""ETF basket transactions and adaptive direct tender liquidation."""

import math

from ..api import DMAClient
from ..execution import PartialFill, execute_order
from ..runtime import TradingError
from .model import EQUITIES, basket_allowed, project_positions, within_risk


class ETFClient(DMAClient):
    def market(self, ticker, action, quantity, *, price=None, passive_seconds=0):
        cap = 2500000 if ticker == "USD" else 10000
        if (
            not self.execute
            or ticker not in (*EQUITIES, "USD")
            or action not in ("BUY", "SELL")
            or not isinstance(quantity, int)
            or not 0 < quantity <= cap
        ):
            raise TradingError("Invalid ETF/FX order or trading disabled")
        sec = next(s for s in self.get("securities") if s["ticker"] == ticker)
        if quantity > sec["max_trade_size"]:
            raise TradingError("Order exceeds current ticket limit")
        receipt, _ = execute_order(
            self, sec, quantity, action, price=price, passive_seconds=passive_seconds
        )
        if receipt["quantity_filled"] < quantity:
            raise PartialFill(receipt, self.last_securities)
        return receipt


def hedge_fx(client, securities, force=False):
    for _ in range(8):
        byname = {s["ticker"]: s for s in securities}
        etf = byname["RITC"]
        exposure = byname["USD"]["position"] + etf["position"] * (etf["bid"] + etf["ask"]) / 2
        if abs(exposure) < (1 if force else 1000):
            return securities
        quantity = min(2500000, abs(round(exposure)))
        change = -quantity if exposure > 0 else quantity
        fx = byname["USD"]["bid" if change < 0 else "ask"]
        if not within_risk(securities, {"USD": change, "CAD": -change * fx}):
            raise TradingError("FX hedge exceeds projected cash capacity")
        client.bind_snapshot(client.get("case"))
        try:
            client.market("USD", "BUY" if change > 0 else "SELL", quantity)
            securities = client.last_securities
        except PartialFill as exc:
            securities = exc.securities
            if not exc.filled:
                raise TradingError("FX hedge made no progress") from exc
    raise TradingError("FX hedge did not converge within the execution budget")


def change_position(client, securities, ticker, change, *, price=None, passive_seconds=0):
    if not change or int(change) != change:
        raise TradingError("Position change must be a nonzero integer")
    projected = project_positions(securities, {ticker: change}, {ticker: price} if price else None)
    if not within_risk(projected, {}):
        raise TradingError("Proposed ETF leg exceeds projected equity or cash limits")
    client.market(
        ticker,
        "BUY" if change > 0 else "SELL",
        abs(int(change)),
        price=price,
        passive_seconds=passive_seconds,
    )
    return client.last_securities


def external_book(client, ticker):
    book = client.get(f"securities/book?ticker={ticker}&limit=1000")
    trader_id = getattr(client, "trader_id", None)
    return {
        side: [x for x in book[side] if trader_id is None or x.get("trader_id") != trader_id]
        for side in ("bids", "asks")
    }


def unwind_all(client, securities, *, passive=False):
    """Refresh depth each slice. Passive attempts switch to market after one wait."""
    may_rest = passive
    for _ in range(80):
        held = next((s for s in securities if s["ticker"] in EQUITIES and s["position"]), None)
        if held is None:
            return hedge_fx(client, securities, force=True)
        case = client.get("case")
        client.bind_snapshot(case)
        book = external_book(client, held["ticker"])
        action = "SELL" if held["position"] > 0 else "BUY"
        levels = book["bids" if action == "SELL" else "asks"]
        available = sum(max(0, x["quantity"] - x.get("quantity_filled", 0)) for x in levels)
        quantity = min(10000, abs(int(held["position"])), int(available))
        if not quantity:
            raise TradingError("No visible exit liquidity; inventory remains for reconciliation")
        # Near deadline, or after a partial/no passive fill, prioritize completion.
        remaining_slices = math.ceil(
            sum(abs(s["position"]) for s in securities if s["ticker"] in EQUITIES) / 10000
        )
        use_passive = may_rest and case["tick"] + remaining_slices * 3 + 8 < 275
        price = held["ask" if action == "SELL" else "bid"] if use_passive else None
        change = -quantity if action == "SELL" else quantity
        try:
            securities = change_position(
                client,
                securities,
                held["ticker"],
                change,
                price=price,
                passive_seconds=0.75 if use_passive else 0,
            )
        except PartialFill as exc:
            securities = exc.securities
            client.audit("partial_fill_reconciled", filled=exc.filled, ticker=held["ticker"])
            if not exc.filled and not use_passive:
                raise TradingError("Market unwind made no progress") from exc
            may_rest = False
        securities = hedge_fx(client, securities)
    raise TradingError("Unwind slice budget exhausted; inspect remaining inventory")


def accept_and_unwind(client, tender, securities, *, passive=False):
    if not client.execute:
        raise TradingError("Tender execution disabled")
    before = next(x["position"] for x in securities if x["ticker"] == "RITC")
    client.audit("tender_attempt", tender=tender)
    response = client.request("POST", f"tenders/{tender['tender_id']}")
    client.audit("tender_response", response=response)
    latest = client.get("securities")
    actual = next(x["position"] for x in latest if x["ticker"] == "RITC")
    expected = before + tender["quantity"] * (1 if tender["action"] == "BUY" else -1)
    if actual != expected or client.get("orders?status=OPEN"):
        raise TradingError("Accepted tender failed position/open-order reconciliation")
    return unwind_all(client, latest, passive=passive)


def basket_trade(client, securities, direction, quantity):
    if not basket_allowed(securities, direction, quantity):
        raise TradingError("Complete basket plan exceeds intermediate position or cash capacity")
    try:
        for ticker, change in [
            ("RITC", direction * quantity),
            ("BULL", -direction * quantity),
            ("BEAR", -direction * quantity),
        ]:
            client.bind_snapshot(client.get("case"))
            securities = change_position(client, securities, ticker, change)
    except PartialFill as exc:
        client.entry_halted = True
        client.audit("basket_aborted_partial", filled=exc.filled)
        return unwind_all(client, exc.securities)
    return hedge_fx(client, securities)
