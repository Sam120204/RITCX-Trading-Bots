"""Tender-first ETF decisions. One glide and at most one working exit per heat."""

from dataclasses import dataclass, field

from ..execution import PartialFill, reconcile_order, submit_order
from ..runtime import TradingError
from .execution import accept_tender, basket_trade, change_position, external_book, hedge_fx
from .model import (
    GLIDE_HORIZON,
    basket_allowed,
    basket_opportunities,
    project_positions,
    quotes,
    tender_inventory,
    tender_opportunity,
    within_risk,
)


def basket_signal(books, securities):
    """Size each direction to remaining external touch volume and reserve tender room."""
    byname = {sec["ticker"]: sec for sec in securities}
    paired = -byname["BULL"]["position"]
    signals = []
    for direction in (1, -1):
        # Published gross weights sum to four per pair. Keep room for 100k RITC.
        cap = min(20000, (250000 - 2 * 100000) // 4)
        quantity = min(5000, cap - paired * direction)
        for ticker, sign in (("RITC", direction), ("BULL", -direction), ("BEAR", -direction)):
            levels = books[ticker]["asks" if sign > 0 else "bids"]
            remaining = [
                (x["price"], x["quantity"] - x.get("quantity_filled", 0))
                for x in levels
                if x["quantity"] > x.get("quantity_filled", 0)
            ]
            if not remaining:
                quantity = 0
                break
            touch = (min if sign > 0 else max)(p for p, _ in remaining)
            quantity = min(
                quantity,
                byname[ticker]["max_trade_size"],
                sum(q for p, q in remaining if p == touch),
            )
        quantity = int(quantity)
        if quantity < 500:
            continue
        signal = next(
            (
                s
                for s in basket_opportunities(books, securities, quantity)
                if s["direction"] == direction
            ),
            None,
        )
        if signal and basket_allowed(securities, direction, quantity):
            projected = project_positions(
                securities,
                {
                    "RITC": direction * quantity,
                    "BULL": -direction * quantity,
                    "BEAR": -direction * quantity,
                },
            )
            if all(within_risk(projected, {"RITC": size}) for size in (-100000, 100000)):
                signals.append(signal)
    return max(signals, key=lambda s: s["edge_cad"], default=None)


@dataclass
class Glide:
    horizon: int = GLIDE_HORIZON
    gamma: float = 1.0
    hold_to_close: bool = False
    late_hold: bool = False
    allowed: float = 0
    base: int = 60000
    path_tick: int = -1
    market_tick: int = -1
    urgent_until: int = -1
    resting: tuple | None = None
    accepted_ids: set = field(default_factory=set)
    seen_ids: set = field(default_factory=set)

    def finish_resting(self, client):
        if self.resting is None:
            return
        sec, quantity, action, receipt = self.resting
        if receipt["status"] == "OPEN":
            order_id = receipt["order_id"]
            receipt = client.get(f"orders/{order_id}")
            if receipt.get("order_id") != order_id:
                raise TradingError("Working limit order identity could not be verified")
        reconcile_order(client, sec, quantity, action, receipt)
        self.resting = None

    def plan(self, tick, excess, mid, nav):
        sign = 1 if excess > 0 else -1
        speed = max(0.25, min(2.5, 1 - self.gamma * sign * (nav - mid) / 0.10))
        if tick <= self.urgent_until:
            speed = 2.5
        elapsed = max(0, min(3, tick - self.path_tick))
        self.allowed = max(0, self.allowed - elapsed * min(10000, self.base * speed / self.horizon))
        self.path_tick = max(self.path_tick, tick)
        return speed, max(0, int(abs(excess) - self.allowed))

    def cycle(self, client, case, securities, books, tenders, *, no_baskets=False):
        tick = case["tick"]
        decisions = []
        for tender in tenders:
            self.seen_ids.add(tender["tender_id"])
            if tender["tender_id"] in self.accepted_ids:
                continue
            offer = tender_opportunity(
                tender,
                books["RITC"],
                securities,
                tick,
                horizon=self.horizon,
                hold_to_close=self.hold_to_close,
            )
            client.audit("tender_evaluation", tick=tick, tender=tender, decision=offer)
            if offer and offer["verdict"] == "take":
                decisions.append((offer, tender))
            elif offer and offer["verdict"] == "blocked":
                self.urgent_until = tick + 3
        if not client.execute:
            return
        if decisions:
            _, tender = max(decisions, key=lambda pair: pair[0]["profit_usd"])
            book = external_book(client, "RITC")
            securities = client.get("securities")
            check = client.check_heat()
            offer = tender_opportunity(
                tender,
                book,
                securities,
                check["tick"],
                horizon=self.horizon,
                hold_to_close=self.hold_to_close,
            )
            if offer and offer["verdict"] == "take":
                client.audit("tender_signal", **offer)
                old = tender_inventory(securities)
                securities = accept_tender(client, tender, securities)
                new = tender_inventory(securities)
                if old * new > 0 and abs(new) > abs(old):
                    self.allowed += abs(new - old)
                else:
                    self.allowed = abs(new)
                self.late_hold = check["tick"] >= 285 and (
                    old * new <= 0 or abs(new) > abs(old) or self.late_hold
                )
                self.base = max(abs(new), 60000)
                self.path_tick = check["tick"]
                self.accepted_ids.add(tender["tender_id"])

        excess = tender_inventory(securities)
        prices = quotes(securities)
        if not excess:
            self.allowed = 0
            if not tenders and not no_baskets and 3 <= tick < 295 and prices:
                signal = basket_signal(books, securities)
                if signal:
                    client.audit("basket_signal", **signal)
                    basket_trade(client, securities, signal["direction"], signal["quantity"])
                    return
        speed = 1
        if excess and prices and not (self.hold_to_close or self.late_hold):
            speed, deficit = self.plan(tick, excess, *prices)
            sec = next(s for s in securities if s["ticker"] == "RITC")
            quantity = min(deficit, 10000, sec["max_trade_size"], abs(excess))
            if quantity >= 500 and tick > self.market_tick and tick < 292:
                self.market_tick = tick
                try:
                    securities = change_position(
                        client,
                        securities,
                        "RITC",
                        -quantity if excess > 0 else quantity,
                        purpose="tender_exit",
                    )
                except PartialFill as exc:
                    securities = exc.securities
                self.market_tick = max(tick, client.get("case")["tick"])
                excess = tender_inventory(securities)
        securities = hedge_fx(client, securities)
        if excess and prices and not (self.hold_to_close or self.late_hold):
            sec = next(s for s in securities if s["ticker"] == "RITC")
            action = "SELL" if excess > 0 else "BUY"
            quantity = int(min(10000, sec["max_trade_size"], abs(excess)))
            bid, ask = sec["bid"], sec["ask"]
            if not 0 < bid < ask:
                return
            if excess > 0:
                price = ask if speed < 1 else max(ask - 0.01, bid + 0.01)
            else:
                price = bid if speed < 1 else min(bid + 0.01, ask - 0.01)
            price = round(price, 2)
            change = -quantity if excess > 0 else quantity
            if within_risk(project_positions(securities, {"RITC": change}, {"RITC": price}), {}):
                sec = dict(sec)
                receipt = submit_order(client, sec, quantity, action, price=price)
                self.resting = (sec, quantity, action, receipt)

    def summary(self, client):
        shares = getattr(client, "ritc_market_shares", 0)
        priced = getattr(client, "priced_shares", 0)
        return dict(
            tenders_seen=len(self.seen_ids),
            tenders_accepted=len(self.accepted_ids),
            ritc_market_shares=shares,
            priced_ritc_market_shares=priced,
            tender_exit_shares=getattr(client, "exit_priced", 0),
            tender_exit_usd_per_share=(
                client.exit_cost / client.exit_priced if getattr(client, "exit_priced", 0) else None
            ),
            all_in_usd_per_share=client.market_cost / priced if priced else None,
        )
