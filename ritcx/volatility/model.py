"""Pricing and risk rules for the RITCx simulated volatility case."""

import math
import re
from dataclasses import dataclass


def black_scholes(spot, strike, years, rate, volatility, kind):
    if (
        kind not in ("C", "P")
        or spot <= 0
        or strike <= 0
        or not all(math.isfinite(x) for x in (spot, strike, years, rate, volatility))
        or volatility < 0
    ):
        raise ValueError("Invalid option inputs")
    if years <= 0 or volatility <= 0:
        discounted = strike * math.exp(-rate * max(0, years))
        dc = 1.0 if spot > discounted else 0.0 if spot < discounted else 0.5
        return (
            (max(0, spot - discounted), dc) if kind == "C" else (max(0, discounted - spot), dc - 1)
        )
    root = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + volatility**2 / 2) * years) / root
    d2 = d1 - root

    def cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    call = spot * cdf(d1) - strike * math.exp(-rate * years) * cdf(d2)
    if kind == "C":
        return call, cdf(d1)
    return call - spot + strike * math.exp(-rate * years), cdf(d1) - 1


def implied_volatility(price, spot, strike, years, rate, kind):
    if years <= 0:
        return None
    low, high = 1e-6, 5.0
    if (
        price < black_scholes(spot, strike, years, rate, low, kind)[0] - 1e-9
        or price > black_scholes(spot, strike, years, rate, high, kind)[0]
    ):
        return None
    for _ in range(60):
        mid = (low + high) / 2
        if black_scholes(spot, strike, years, rate, mid, kind)[0] < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


@dataclass
class VolatilityCurve:
    rate: float
    weeks: dict

    @classmethod
    def from_news(cls, news, tick):
        rate = None
        weeks = {}
        confirmed = None
        confirmed_tick = None
        for item in sorted(news, key=lambda n: (n["tick"], n.get("news_id", 0))):
            t = int(item["tick"])
            if t > tick:
                continue
            body = re.sub("<[^>]+>", " ", item.get("body", "")).lower()
            r = re.search(r"risk[ -]free rate\s+is\s+(-?[\d.]+)\s*%", body)
            if r:
                rate = float(r[1]) / 100
            values = re.search(
                r"volatility.*?(?:is|be)\s+(?:between\s+)?([\d.]+)\s*(?:%\s*)?(?:\s*(?:and|to|[-–])\s*([\d.]+)\s*)?%",
                body,
            )
            if not values:
                continue
            low = float(values[1]) / 100
            high = float(values[2]) / 100 if values[2] else low
            if not 0 < low <= high <= 3:
                raise ValueError("Unrecognized volatility announcement")
            week = (t // 75) * 75
            if "next week" in body:
                # Page 3 uses ticks 1..75 for week one. A forecast is never
                # promoted to a confirmed realized-vol announcement by time alone.
                week = (max(0, t - 1) // 75 + 1) * 75
            else:
                confirmed = (low + high) / 2
                confirmed_tick = t
            weeks[week] = (low, high)
        if rate is None or confirmed is None or not any(t <= tick for t in weeks):
            raise ValueError("No usable risk-free rate/current volatility news")
        curve = cls(rate, weeks)
        curve._tick = tick
        curve._confirmed = confirmed
        curve.confirmed_tick = confirmed_tick
        return curve

    @property
    def current(self):
        if hasattr(self, "_confirmed"):
            return self._confirmed
        tick = getattr(self, "_tick", 0)
        low, high = self.weeks[max(k for k in self.weeks if k <= tick)]
        return (low + high) / 2

    def effective(self, tick, bound="mid"):
        if tick >= 300:
            return self.current
        variance = 0
        cursor = tick
        while cursor < 300:
            end = min(300, (cursor // 75 + 1) * 75)
            low, high = self.weeks[max(k for k in self.weeks if k <= cursor)]
            sigma = low if bound == "low" else high if bound == "high" else (low + high) / 2
            variance += (end - cursor) * sigma * sigma
            cursor = end
        return math.sqrt(variance / (300 - tick))


def option_values(sec, spot, tick, sigma, rate):
    match = re.fullmatch(r"RTM(48|49|50|51|52)([CP])", sec["ticker"])
    if not match:
        raise ValueError("Unexpected option ticker")
    return black_scholes(spot, float(match[1]), max(0, 300 - tick) / 3600, rate, sigma, match[2])


def portfolio_delta(securities, tick, sigma, rate):
    stock = next(x for x in securities if x["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    return stock["position"] + sum(
        x["position"] * x["size"] * option_values(x, spot, tick, sigma, rate)[1]
        for x in securities
        if x["type"] == "OPTION"
    )


def hedge_quantity(securities, tick, sigma, rate):
    stock = next(x for x in securities if x["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    total = stock["position"]
    for sec in securities:
        if sec["type"] != "OPTION" or not sec["position"]:
            continue
        match = re.fullmatch(r"RTM(48|49|50|51|52)([CP])", sec["ticker"])
        if not match:
            raise ValueError("Unexpected option ticker")
        quoted_vol = implied_volatility(
            (sec["bid"] + sec["ask"]) / 2,
            spot,
            float(match[1]),
            max(0, 300 - tick) / 3600,
            rate,
            match[2],
        )
        total += (
            sec["position"]
            * sec["size"]
            * option_values(sec, spot, tick, quoted_vol if quoted_vol is not None else sigma, rate)[
                1
            ]
        )
    return -round(total)


def candidates(securities, curve, tick):
    stock = next(x for x in securities if x["ticker"] == "RTM")
    spot = (stock["bid"] + stock["ask"]) / 2
    result = []
    for sec in securities:
        if sec["type"] != "OPTION":
            continue
        low, delta = option_values(sec, spot, tick, curve.effective(tick, "low"), curve.rate)
        high, _ = option_values(sec, spot, tick, curve.effective(tick, "high"), curve.rate)
        # Both option commissions, exit half-spread, and a reserve for ETF hedges.
        cost = 2 * sec["trading_fee"] / sec["size"] + (sec["ask"] - sec["bid"]) / 2 + 0.04
        for action, edge in [("BUY", low - sec["ask"] - cost), ("SELL", sec["bid"] - high - cost)]:
            if edge > 0.025:
                result.append(
                    dict(
                        ticker=sec["ticker"],
                        action=action,
                        edge=edge,
                        fair_low=low,
                        fair_high=high,
                        delta=delta,
                    )
                )
    return sorted(result, key=lambda x: x["edge"], reverse=True)


def parity_opportunities(securities, tick, rate):
    """Diagnostic only: executable parity packages, with round-trip commissions."""
    byname = {s["ticker"]: s for s in securities}
    stock = byname["RTM"]
    discounted = math.exp(-rate * max(0, 300 - tick) / 3600)
    result = []
    for strike in range(48, 53):
        call, put = byname.get(f"RTM{strike}C"), byname.get(f"RTM{strike}P")
        if call is None or put is None:
            continue
        fees = 2 * (call["trading_fee"] + put["trading_fee"] + 100 * stock["trading_fee"])
        rich = 100 * (call["bid"] - put["ask"] - stock["ask"] + strike * discounted) - fees
        cheap = 100 * (stock["bid"] - call["ask"] + put["bid"] - strike * discounted) - fees
        for direction, edge in [("sell_synthetic", rich), ("buy_synthetic", cheap)]:
            if edge > 3:
                result.append(dict(strike=strike, direction=direction, net_edge=edge))
    return sorted(result, key=lambda x: x["net_edge"], reverse=True)
