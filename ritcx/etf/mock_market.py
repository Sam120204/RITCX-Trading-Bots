"""Synthetic ETF exchange: finite depth, CAD/USD ledger, no network transport."""

import math
import random
from copy import deepcopy
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit

from ..runtime import TradingError
from .execution import ETFClient
from .model import EQUITIES, within_risk


@dataclass(frozen=True)
class Scenario:
    stock_sigma: float = .0015
    fx_sigma: float = .0003
    gap_noise: float = .025
    gap_decay: float = .94
    shock_size: float = .65
    spread: float = .02
    depth: int = 3000
    impact_per_share: float = .000002
    request_min: float = .04
    request_max: float = .08
    order_min: float = .08
    order_max: float = .20


SCENARIOS = {
    "case": Scenario(),
    "stress": Scenario(stock_sigma=.003, fx_sigma=.0008, gap_noise=.06,
                       gap_decay=.98, spread=.06, depth=600,
                       impact_per_share=.000008, request_min=.08, request_max=.15,
                       order_min=.2, order_max=.6),
}


class ETFMarket:
    """Model assumptions are scenarios, not a reconstruction of the RIT server.

    Exogenous prices and tender terms use independent seeded random streams.
    Depth replenishes each second; consumed depth and decaying own-price impact
    affect execution. A resting exit fills if the next book crosses its limit,
    up to the displayed crossing volume. Market remainders are cancelled.
    """

    def __init__(self, seed, scenario="case"):
        self.seed, self.scenario_name = seed, scenario
        self.settings = SCENARIOS[scenario]
        self.rng = random.Random(f"etf:{seed}:prices")
        self.tender_rng = random.Random(f"etf:{seed}:tenders")
        self.clock = 0.0
        self.tick = 0
        self.on_tick = lambda: None
        self.prices = dict(CAD=1., USD=1., BULL=10., BEAR=15., RITC=25.)
        self.gap = 0.
        self.impact = dict.fromkeys((*EQUITIES, "USD"), 0.)
        self.fees = 0.
        self.fills, self.offers = [], []
        self.accepted = set()
        self.settled_positions = {}
        self.receipts = {}
        self.byname = {}
        for ticker in ("CAD", "USD", *EQUITIES):
            equity = ticker in EQUITIES
            self.byname[ticker] = dict(
                ticker=ticker, type="STOCK" if equity else "CURRENCY", size=1,
                currency="" if ticker == "CAD" else "USD" if ticker == "RITC" else "CAD",
                position=0., trading_fee=.02 if equity else 0., limit_order_rebate=.01 if equity else 0.,
                max_trade_size=10000 if equity else 2500000, execution_delay_ms=0,
                limits=[dict(name="LIMIT-STOCK" if equity else "LIMIT-CASH",
                             units=2 if ticker == "RITC" else 1)],
            )
        self.books = {}
        self.quote()

    @property
    def fair_etf(self):
        return (self.prices["BULL"] + self.prices["BEAR"]) / self.prices["USD"]

    def mid(self, ticker):
        sec = self.byname[ticker]
        return (sec["bid"] + sec["ask"]) / 2

    def quote(self):
        self.prices["RITC"] = max(1., self.fair_etf + self.gap)
        for ticker, sec in self.byname.items():
            p = max(.01, self.prices[ticker] + self.impact.get(ticker, 0.))
            spread = 0 if ticker == "CAD" else .0002 if ticker == "USD" else self.settings.spread
            sec.update(bid=p - spread / 2, ask=p + spread / 2, last=p)
            if ticker == "CAD":
                continue
            step = .0001 if ticker == "USD" else .01
            depth = 500000 if ticker == "USD" else self.settings.depth
            self.books[ticker] = {
                side: [dict(price=sec[key] + sign * step * i, quantity=depth,
                            quantity_filled=0, trader_id="synthetic-maker") for i in range(12)]
                for side, key, sign in [("bids", "bid", -1), ("asks", "ask", 1)]
            }

    def case(self):
        return dict(name="RITCx - Algorithmic ETF Arbitrage Case", period=1,
                    tick=self.tick, total_periods=1, ticks_per_period=300,
                    status="ACTIVE" if self.tick < 300 else "STOPPED")

    def limits(self):
        return [dict(name="LIMIT-STOCK", gross_limit=250000, net_limit=200000),
                dict(name="LIMIT-CASH", gross_limit=8000000, net_limit=8000000)]

    def tenders(self):
        return deepcopy([t for t in self.offers if t["tender_id"] not in self.accepted
                         and self.tick < t["expires"]])

    def advance_time(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Clock advance must be finite and nonnegative")
        target = min(300., self.clock + seconds)
        while self.tick < int(target):
            self.tick += 1
            self.clock = float(self.tick)
            for ticker in ("BULL", "BEAR", "USD"):
                sigma = self.settings.fx_sigma if ticker == "USD" else self.settings.stock_sigma
                self.prices[ticker] *= math.exp(self.rng.gauss(-sigma * sigma / 2, sigma))
            self.gap = self.gap * self.settings.gap_decay + self.rng.gauss(0, self.settings.gap_noise)
            if self.tick % 45 == 15:
                self.gap += self.rng.choice((-1, 1)) * self.settings.shock_size
            for ticker in self.impact:
                self.impact[ticker] *= .8
            self.quote()
            for order in self.receipts.values():
                if order["status"] == "OPEN" and self.tick < 300:
                    self.match(order, resting=True)
            if self.tick in (35, 85, 135, 185, 235):
                action = self.tender_rng.choice(("BUY", "SELL"))
                discount = self.tender_rng.uniform(-.08, .3)
                self.offers.append(dict(
                    tender_id=len(self.offers) + 1, ticker="RITC", is_fixed_bid=True,
                    action=action, quantity=self.tender_rng.choice((10000, 20000, 30000)),
                    price=self.mid("RITC") - discount * (1 if action == "BUY" else -1),
                    expires=self.tick + 12,
                ))
            if self.tick == 300:
                self.settle()
            self.on_tick()
        self.clock = target

    def _apply(self, ticker, quantity, action, price, fee):
        sign = 1 if action == "BUY" else -1
        currency = self.byname[ticker]["currency"]
        projected = deepcopy(list(self.byname.values()))
        for sec in projected:
            if sec["ticker"] == ticker:
                sec["position"] += sign * quantity
            if sec["ticker"] == currency:
                sec["position"] -= sign * quantity * price + fee
        if not within_risk(projected, {}):
            raise TradingError("Synthetic exchange position limit exceeded")
        for sec in projected:
            self.byname[sec["ticker"]]["position"] = sec["position"]
        self.fees += fee * (self.mid("USD") if currency == "USD" else 1.)

    def fill(self, ticker, quantity, action, price=None):
        if (self.tick >= 300 or ticker not in self.books or action not in ("BUY", "SELL")
                or not isinstance(quantity, int) or not 0 < quantity <= self.byname[ticker]["max_trade_size"]):
            raise TradingError("Invalid synthetic order or heat ended")
        order_id = len(self.receipts) + 1
        order = dict(order_id=order_id, ticker=ticker, action=action, quantity=quantity,
                     quantity_filled=0, vwap=0., price=price, status="OPEN", tick=self.tick)
        self.receipts[order_id] = order
        self.match(order)
        return deepcopy(order)

    def match(self, order, resting=False):
        ticker, action, limit = order["ticker"], order["action"], order["price"]
        remaining = order["quantity"] - order["quantity_filled"]
        sign = 1 if action == "BUY" else -1
        levels = self.books[ticker]["asks" if sign == 1 else "bids"]
        takes, value = [], 0.
        for level in levels:
            if not remaining or (limit is not None and sign * (level["price"] - limit) > 0):
                break
            take = min(remaining, level["quantity"] - level["quantity_filled"])
            takes.append((level, take))
            value += take * (limit if resting else level["price"])
            remaining -= take
        filled = sum(take for _, take in takes)
        if filled:
            price = value / filled
            sec = self.byname[ticker]
            fee = -sec["limit_order_rebate"] if resting else sec["trading_fee"]
            self._apply(ticker, filled, action, price, filled * fee)
            for level, take in takes:
                level["quantity_filled"] += take
            total = value + order["vwap"] * order["quantity_filled"]
            order["quantity_filled"] += filled
            order["vwap"] = total / order["quantity_filled"]
            self.fills.append(dict(order, quantity=filled, price=price, tick=self.tick))
            if ticker in EQUITIES:
                self.impact[ticker] += sign * filled * self.settings.impact_per_share
        if not remaining:
            order["status"] = "TRANSACTED"
        elif limit is None:
            order["status"] = "CANCELLED"

    def accept(self, tender_id):
        if self.tick >= 300:
            raise TradingError("Heat ended")
        tender = next((t for t in self.tenders() if t["tender_id"] == tender_id), None)
        if tender is None:
            raise TradingError("Tender unavailable, expired, or already accepted")
        self._apply("RITC", tender["quantity"], tender["action"], tender["price"], 0.)
        self.accepted.add(tender_id)
        return dict(success=True, tender_id=tender_id)

    def settle(self):
        for order in self.receipts.values():
            if order["status"] == "OPEN":
                order["status"] = "CANCELLED"
        self.settled_positions = self.snapshot()["positions"]
        for ticker in EQUITIES:
            sec = self.byname[ticker]
            price = self.fair_etf if ticker == "RITC" else self.prices[ticker]
            self.byname[sec["currency"]]["position"] += sec["position"] * price
            sec["position"] = 0.
        self.byname["CAD"]["position"] += self.byname["USD"]["position"] * self.mid("USD")
        self.byname["USD"]["position"] = 0.

    def snapshot(self):
        fx = self.mid("USD")
        cash = self.byname["CAD"]["position"] + self.byname["USD"]["position"] * fx
        inventory = sum(self.byname[t]["position"] * self.mid(t) * (fx if t == "RITC" else 1)
                        for t in EQUITIES)
        return dict(tick=self.tick, pnl=cash + inventory, cash_cad=cash,
                    inventory_value_cad=inventory, fees=self.fees, fines=0.,
                    positions={k: s["position"] for k, s in self.byname.items()
                               if k != "CAD" and s["position"]},
                    orders=len(self.fills), tenders_accepted=len(self.accepted),
                    fx=fx, gap_cad=self.mid("RITC") * fx - self.mid("BULL") - self.mid("BEAR"),
                    settled_positions=dict(self.settled_positions))


class LocalETFClient(ETFClient):
    """Reuse production reconciliation/execution with an entirely local transport."""

    def __init__(self, market, audit=None):
        super().__init__("local://etf", "local", "", execute=True, audit=audit)
        self.trader_id = "local"
        self.market_state = market
        self.transport_rng = random.Random(f"etf:{market.seed}:transport")
        self.simulated_order_delays = []

    def request(self, method, endpoint, params=None):
        market, settings = self.market_state, self.market_state.settings
        if method != "GET":
            if not self.execute:
                raise TradingError("Execution disabled")
            if self.heat is not None:
                self.check_heat(require_fresh=method != "DELETE")
        delay = self.transport_rng.uniform(settings.request_min, settings.request_max)
        if method == "POST":
            delay += self.transport_rng.uniform(settings.order_min, settings.order_max)
            self.simulated_order_delays.append(delay)
        market.advance_time(delay)
        if method != "GET" and market.tick >= 300:
            raise TradingError("Heat ended before synthetic submission arrived")
        if method == "GET":
            if endpoint == "case":
                return market.case()
            if endpoint == "securities":
                return deepcopy(list(market.byname.values()))
            if endpoint == "limits":
                return market.limits()
            if endpoint == "trader":
                return dict(nlv=market.snapshot()["pnl"], total_fines=0.)
            if endpoint == "tenders":
                return market.tenders()
            if endpoint == "orders?status=OPEN":
                return [deepcopy(o) for o in market.receipts.values() if o["status"] == "OPEN"]
            if endpoint.startswith("securities/book?"):
                ticker = parse_qs(urlsplit(endpoint).query)["ticker"][0]
                return deepcopy(market.books[ticker])
            if endpoint.startswith("orders/"):
                return deepcopy(market.receipts[int(endpoint.split("/")[1])])
        if method == "POST" and endpoint == "orders":
            return market.fill(params["ticker"], params["quantity"], params["action"], params.get("price"))
        if method == "DELETE" and endpoint.startswith("orders/"):
            order = market.receipts[int(endpoint.split("/")[1])]
            if order["status"] == "OPEN":
                order["status"] = "CANCELLED"
            return dict(success=True)
        if method == "POST" and endpoint.startswith("tenders/"):
            return market.accept(int(endpoint.split("/")[1]))
        raise TradingError(f"Unsupported local request: {method} {endpoint}")

    def settings_report(self):
        return asdict(self.market_state.settings)
