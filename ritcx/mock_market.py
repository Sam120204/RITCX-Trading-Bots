"""Synthetic volatility market. No sockets, credentials, or external services."""

import math
import random
from copy import deepcopy
from dataclasses import dataclass

from .api import DMAClient
from .runtime import TradingError
from .volatility.model import black_scholes, portfolio_delta


@dataclass(frozen=True)
class Scenario:
    forecast_error: float
    forecast_width: float
    quote_lag_min: float
    quote_lag_max: float
    quote_noise: float
    request_min: float
    request_max: float
    order_delay_min: float
    order_delay_max: float


SCENARIOS = {
    # Published mechanics; lag and forecast distributions remain assumptions.
    "case": Scenario(0.01, 0.03, 5, 15, 0, 0.04, 0.08, 0, 0),
    "case_fast": Scenario(0.01, 0.03, 1, 3, 0, 0.04, 0.08, 0, 0),
    "case_latency": Scenario(0.01, 0.03, 5, 15, 0, 0.10, 0.18, 0, 0),
    "demo": Scenario(0, 0.03, 15, 15, 0, 0, 0, 0, 0),
    "standard": Scenario(0.045, 0.03, 1, 12, 0.003, 0.015, 0.055, 0.08, 0.4),
    "stress": Scenario(0.09, 0.025, 0.5, 8, 0.008, 0.06, 0.18, 0.3, 1.5),
}


class MockMarket:
    """One-second GBM steps, lagged option quotes, immediate bid/ask fills.

    A synthetic week is 75 ticks. Quotes learn new weekly volatility over
    15 ticks. Forecast ranges are available 15 ticks before a week starts.
    Orders have unlimited liquidity at the displayed quote, zero latency,
    and the published commission. These are modeling assumptions, not RIT.
    """

    def __init__(self, seed, scenario="demo"):
        self.rng = random.Random(seed)
        self.seed = seed
        self.scenario_name = scenario
        self.scenario = SCENARIOS[scenario]
        settings = self.scenario
        # Separate streams keep the market path fixed across strategy policies.
        forecast_rng = random.Random(f"{seed}:forecasts")
        self.quote_rng = random.Random(f"{seed}:quotes")
        self.clock = 0.0
        self.on_tick = lambda: None
        self._quoted_sigma = 0.20
        self.sigmas = [0.20] + [self.rng.uniform(0.15, 0.45) for _ in range(3)]
        bias = forecast_rng.gauss(0, settings.forecast_error / 2)
        self.forecasts = [
            max(0.04, min(0.8, s + bias + forecast_rng.gauss(0, settings.forecast_error)))
            for s in self.sigmas
        ]
        self.quote_lags = [
            self.quote_rng.uniform(settings.quote_lag_min, settings.quote_lag_max) for _ in range(4)
        ]
        self.quote_bias = self.quote_rng.gauss(0, settings.quote_noise * 2)
        self.tick = 0
        self.spot = 50.0
        self.cash = self.fees = self.fines = self.realized = 0.0
        self.orders = 0
        self.fills = []
        self.settled_positions = {}
        self.securities = []
        self.basis = {}
        for ticker in ["RTM"] + [f"RTM{k}{cp}" for k in range(48, 53) for cp in "CP"]:
            option = ticker != "RTM"
            self.securities.append(
                dict(
                    ticker=ticker,
                    type="OPTION" if option else "STOCK",
                    size=100 if option else 1,
                    position=0,
                    trading_fee=2 if option else 0.02,
                    max_trade_size=100 if option else 10000,
                )
            )
            self.basis[ticker] = 0.0
        self.quote()

    @property
    def sigma(self):
        tick = max(0, self.tick - 1) if self.scenario_name.startswith("case") else self.tick
        return self.sigmas[min(tick // 75, 3)]

    @property
    def quoted_sigma(self):
        if self.scenario_name != "demo":
            return self._quoted_sigma
        week = min(self.tick // 75, 3)
        fraction = min(1, (self.tick - week * 75) / 15)
        return (
            self.sigmas[week]
            if week == 0
            else (self.sigmas[week - 1] * (1 - fraction) + self.sigmas[week] * fraction)
        )

    def quote(self):
        for sec in self.securities:
            price = (
                self.spot
                if sec["type"] == "STOCK"
                else black_scholes(
                    self.spot,
                    int(sec["ticker"][3:5]),
                    max(0, 300 - self.tick) / 3600,
                    0,
                    self.quoted_sigma,
                    sec["ticker"][-1],
                )[0]
            )
            # Center on fair price except for the nonnegative bid floor.
            bid = max(0, price - 0.01)
            sec.update(bid=bid, ask=bid + 0.02, last=price)

    def news(self):
        result = []
        for week, sigma in enumerate(self.sigmas):
            official = self.scenario_name.startswith("case")
            start = week * 75 + (1 if official and week else 0)
            release = (week - 1) * 75 + 37 if official else start - 15
            if week and release <= self.tick:
                result.append(
                    dict(
                        tick=release,
                        body=(
                            f"Volatility next week will be between {max(0.01, self.forecasts[week] - self.scenario.forecast_width) * 100:.3f}% "
                            f"and {(self.forecasts[week] + self.scenario.forecast_width) * 100:.3f}%."
                        ),
                    )
                )
            if start <= self.tick:
                result.append(
                    dict(
                        tick=start,
                        body=(
                            f"Risk free rate is 0%. Annualized realized volatility is {sigma * 100:.3f}%."
                        ),
                    )
                )
        return result

    def advance(self, tick):
        if tick != self.tick + 1 or tick > 300:
            raise ValueError("Advance exactly one tick, through tick 300")
        delta = portfolio_delta(self.securities, self.tick, self.quoted_sigma, 0)
        fine = max(0, abs(delta) - 7000) * 0.10
        self.fines += fine
        self.cash -= fine
        sigma = self.sigma
        if self.scenario_name.startswith("case"):
            sigma = self.sigmas[min(max(0, tick - 1) // 75, 3)]
        shock = sigma / 60 * self.rng.gauss(0, 1)
        self.spot *= (
            (1 + shock)
            if self.scenario_name.startswith("case")
            else math.exp(-0.5 * sigma**2 / 3600 + shock)
        )
        self.tick = tick
        self.clock = float(tick)
        if self.scenario_name != "demo":
            # A market maker reacts to public announcements, including forecasts.
            # Unknown future weeks revert toward a long-run 25% estimate instead
            # of carrying the current volatility forever. No future truth is read.
            week = min(tick // 75, 3)
            variance = 0.0
            for future in range(week, 4):
                duration = max(0, (future + 1) * 75 - max(tick, future * 75))
                estimate = (
                    self.sigma
                    if future == week
                    else (
                        self.forecasts[future]
                        if future * 75 - 15 <= tick
                        else 0.25 + (self.sigma - 0.25) * 0.5 ** (future - week)
                    )
                )
                variance += duration * estimate**2
            target = math.sqrt(variance / max(1, 300 - tick)) if tick < 300 else self.sigma
            if self.scenario_name.startswith("case"):
                week = min(max(0, tick - 1) // 75, 3)
                target = self.sigma
            alpha = 1 - math.exp(-1 / self.quote_lags[week])
            self._quoted_sigma = max(
                0.04,
                min(
                    0.8,
                    self._quoted_sigma
                    + alpha * (target + self.quote_bias - self._quoted_sigma)
                    + self.quote_rng.gauss(0, self.scenario.quote_noise),
                ),
            )
        self.quote()
        if tick == 300:
            self.settle()

    def advance_time(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Clock advance must be finite and nonnegative")
        target = min(300, self.clock + seconds)
        while self.tick < int(target):
            self.advance(self.tick + 1)
            self.on_tick()
        self.clock = target

    def fill(self, ticker, quantity, action):
        sec = next((s for s in self.securities if s["ticker"] == ticker), None)
        if (
            self.tick >= 300
            or sec is None
            or action not in ("BUY", "SELL")
            or not isinstance(quantity, int)
            or not 0 < quantity <= sec["max_trade_size"]
        ):
            raise ValueError("Invalid local market order")
        change = quantity if action == "BUY" else -quantity
        proposed = {s["ticker"]: s["position"] for s in self.securities}
        proposed[ticker] += change
        options = [v for k, v in proposed.items() if k != "RTM"]
        if (
            abs(proposed["RTM"]) > 50000
            or sum(map(abs, options)) > 2500
            or abs(sum(options)) > 1000
        ):
            raise ValueError("Published position limit exceeded")
        price = sec["ask"] if change > 0 else sec["bid"]
        fee = quantity * sec["trading_fee"]
        self.book(sec, change, price, fee)
        self.orders += 1
        row = dict(
            order_id=self.orders,
            tick=self.tick,
            ticker=ticker,
            action=action,
            quantity=quantity,
            quantity_filled=quantity,
            price=price,
            fee=fee,
            status="TRANSACTED",
        )
        self.fills.append(row)
        return row

    def book(self, sec, change, price, fee):
        ticker, old, size = sec["ticker"], sec["position"], sec["size"]
        basis = self.basis[ticker]
        new = old + change
        if old * change < 0:
            self.realized += (
                min(abs(old), abs(change)) * (price - basis) * (1 if old > 0 else -1) * size
            )
        if new == 0:
            self.basis[ticker] = 0.0
        elif old == 0 or old * new < 0:
            self.basis[ticker] = price
        elif old * change > 0:
            self.basis[ticker] = (abs(old) * basis + abs(change) * price) / abs(new)
        sec["position"] = new
        self.fees += fee
        self.cash -= change * price * size + fee

    def settle(self):
        self.settled_positions = {
            s["ticker"]: s["position"] for s in self.securities if s["position"]
        }
        for sec in self.securities:
            if sec["position"]:
                self.book(sec, -sec["position"], sec["last"], 0)

    def snapshot(self):
        marked = sum(s["position"] * s["size"] * (s["bid"] + s["ask"]) / 2 for s in self.securities)
        unrealized = sum(
            s["position"] * s["size"] * ((s["bid"] + s["ask"]) / 2 - self.basis[s["ticker"]])
            for s in self.securities
        )
        return dict(
            tick=self.tick,
            pnl=self.cash + marked,
            cash=self.cash,
            fees=self.fees,
            fines=self.fines,
            realized_net=self.realized - self.fees - self.fines,
            unrealized=unrealized,
            orders=self.orders,
            spot=self.spot,
            volatility=self.sigma,
            quoted_volatility=self.quoted_sigma,
            delta=portfolio_delta(self.securities, self.tick, self.quoted_sigma, 0),
            positions={s["ticker"]: s["position"] for s in self.securities if s["position"]},
            settled_positions=self.settled_positions.copy(),
        )


class MockClient(DMAClient):
    """Use the existing order/reconciliation code with a strictly local transport."""

    def __init__(self, market, audit):
        super().__init__("local-simulation", "local", "", execute=True, audit=audit)
        self.market = market
        self.transport_rng = random.Random(f"{market.seed}:transport")
        self.simulated_order_delays = []

    def request(self, method, endpoint, params=None):
        market = self.market
        settings = market.scenario
        market.advance_time(self.transport_rng.uniform(settings.request_min, settings.request_max))
        if method == "GET":
            if endpoint == "case":
                return dict(
                    name="RITCx Volatility Trading Case",
                    period=1,
                    tick=market.tick,
                    status="ACTIVE" if market.tick < 300 else "STOPPED",
                    ticks_per_period=300,
                    total_periods=1,
                )
            if endpoint == "securities":
                return deepcopy(market.securities)
            if endpoint == "news":
                return market.news()
            if endpoint == "trader":
                return dict(nlv=market.snapshot()["pnl"], total_fines=market.fines)
            if endpoint == "limits":
                return [
                    dict(name="Limit-ETF", gross_limit=50000, net_limit=50000),
                    dict(name="Limit-OPT", gross_limit=2500, net_limit=1000),
                ]
            if endpoint in ("orders", "orders?status=OPEN"):
                return []
        if method == "POST" and endpoint == "orders" and params and params.get("type") == "MARKET":
            self.check_heat()
            delay = self.transport_rng.uniform(settings.order_delay_min, settings.order_delay_max)
            market.advance_time(delay)
            self.simulated_order_delays.append(delay)
            try:
                receipt = market.fill(params["ticker"], params["quantity"], params["action"])
                return dict(receipt, simulated_delay_seconds=delay)
            except ValueError as exc:
                raise TradingError(str(exc)) from exc
        raise TradingError(f"Unsupported local request: {method} {endpoint}")
