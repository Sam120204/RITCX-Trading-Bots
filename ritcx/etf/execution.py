"""ETF basket transactions and adaptive direct tender liquidation."""

import math
import time

from ..api import DMAClient
from ..execution import PartialFill, execute_order
from ..runtime import TradingError
from .model import EQUITIES, basket_allowed, project_positions, within_risk


class ETFClient(DMAClient):
    def market(self, ticker, action, quantity, *, purpose="market"):
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
        receipt, _ = execute_order(self, sec, quantity, action)
        filled, vwap = receipt["quantity_filled"], receipt.get("vwap")
        if ticker == "RITC" and filled:
            self.ritc_market_shares = getattr(self, "ritc_market_shares", 0) + filled
            if isinstance(vwap, (float, int)) and math.isfinite(vwap) and vwap > 0:
                mid = (sec["bid"] + sec["ask"]) / 2
                cost = (vwap - mid) * (1 if action == "BUY" else -1) + sec["trading_fee"]
                self.priced_shares = getattr(self, "priced_shares", 0) + filled
                self.market_cost = getattr(self, "market_cost", 0.0) + filled * cost
                if purpose == "tender_exit":
                    self.exit_priced = getattr(self, "exit_priced", 0) + filled
                    self.exit_cost = getattr(self, "exit_cost", 0) + filled * cost
                self.audit(
                    "ritc_market_fill",
                    purpose=purpose,
                    quantity=filled,
                    vwap=vwap,
                    mid=mid,
                    all_in_usd_per_share=cost,
                )
        if receipt["quantity_filled"] < quantity:
            raise PartialFill(receipt, self.last_securities)
        return receipt


def hedge_fx(client, securities, force=False):
    for _ in range(8):
        byname = {s["ticker"]: s for s in securities}
        exposure = byname["USD"]["position"]
        if abs(exposure) < (1 if force else 20000):
            return securities
        case = client.get("case")
        if not force and getattr(client, "fx_tick", None) == case["tick"]:
            return securities
        quantity = min(2500000, int(byname["USD"]["max_trade_size"]), abs(round(exposure)))
        change = -quantity if exposure > 0 else quantity
        fx = byname["USD"]["bid" if change < 0 else "ask"]
        if not within_risk(securities, {"USD": change, "CAD": -change * fx}):
            raise TradingError("FX hedge exceeds projected cash capacity")
        client.bind_snapshot(case)
        client.fx_tick = case["tick"]
        try:
            client.market("USD", "BUY" if change > 0 else "SELL", quantity)
            securities = client.last_securities
        except PartialFill as exc:
            securities = exc.securities
            if not exc.filled:
                raise TradingError("FX hedge made no progress") from exc
        if not force:
            return securities
    raise TradingError("FX hedge did not converge within the execution budget")


def change_position(client, securities, ticker, change, *, purpose="market"):
    if not change or int(change) != change:
        raise TradingError("Position change must be a nonzero integer")
    projected = project_positions(securities, {ticker: change})
    if not within_risk(projected, {}):
        raise TradingError("Proposed ETF leg exceeds projected equity or cash limits")
    client.market(
        ticker,
        "BUY" if change > 0 else "SELL",
        abs(int(change)),
        purpose=purpose,
    )
    return client.last_securities


def external_book(client, ticker):
    book = client.get(f"securities/book?ticker={ticker}&limit=1000")
    trader_id = getattr(client, "trader_id", None)
    return {
        side: [x for x in book[side] if trader_id is None or x.get("trader_id") != trader_id]
        for side in ("bids", "asks")
    }


def unwind_all(client, securities):
    """Close equity positions against fresh depth and hedge the remaining USD."""
    for _ in range(80):
        holdings = [s for s in securities if s["ticker"] in EQUITIES and s["position"]]
        if not holdings:
            return hedge_fx(client, securities, force=True)
        case = client.get("case")
        client.bind_snapshot(case)
        # Closing the wrong leg of a hedged tender can increase weighted net exposure.
        for held in sorted(holdings, key=lambda s: abs(s["position"]), reverse=True):
            book = external_book(client, held["ticker"])
            action = "SELL" if held["position"] > 0 else "BUY"
            levels = book["bids" if action == "SELL" else "asks"]
            available = sum(max(0, x["quantity"] - x.get("quantity_filled", 0)) for x in levels)
            quantity = min(10000, abs(int(held["position"])), int(available))
            change = -quantity if action == "SELL" else quantity
            if quantity and within_risk(
                project_positions(securities, {held["ticker"]: change}), {}
            ):
                break
        else:
            raise TradingError("No visible exit within current equity and cash capacity")
        try:
            securities = change_position(client, securities, held["ticker"], change)
        except PartialFill as exc:
            securities = exc.securities
            client.audit("partial_fill_reconciled", filled=exc.filled, ticker=held["ticker"])
            if not exc.filled:
                raise TradingError("Market unwind made no progress") from exc
    raise TradingError("Unwind slice budget exhausted; inspect remaining inventory")


def accept_tender(client, tender, securities):
    if not client.execute:
        raise TradingError("Tender execution disabled")
    before = next(x["position"] for x in securities if x["ticker"] == "RITC")
    client.audit("tender_attempt", tender=tender)
    response = client.request("POST", f"tenders/{tender['tender_id']}", {"price": tender["price"]})
    client.audit("tender_response", response=response)
    expected = before + tender["quantity"] * (1 if tender["action"] == "BUY" else -1)
    # The practice API can acknowledge a tender before publishing its new position.
    for _ in range(12):
        latest = client.get("securities")
        actual = next(x["position"] for x in latest if x["ticker"] == "RITC")
        if actual == expected:
            break
        if actual != before:
            raise TradingError(
                f"Unexpected tender position: expected {expected}, observed {actual}"
            )
        time.sleep(0.10)
    else:
        raise TradingError(f"Tender acceptance unconfirmed: expected {expected}, observed {actual}")
    if client.get("orders?status=OPEN"):
        raise TradingError("Open orders remain after tender acceptance")
    return latest


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
