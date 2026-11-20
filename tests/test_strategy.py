import math
import unittest

from ritcx.volatility import model as s


def stock(position=0):
    return dict(
        ticker="RTM",
        type="STOCK",
        size=1,
        position=position,
        bid=49.99,
        ask=50.01,
        last=50,
        trading_fee=0.02,
        max_trade_size=10000,
    )


def option(ticker="RTM50C", position=0, bid=1.9, ask=1.92):
    return dict(
        ticker=ticker,
        type="OPTION",
        size=100,
        position=position,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        trading_fee=2,
        max_trade_size=100,
    )


class PricingTests(unittest.TestCase):
    def test_known_black_scholes_and_parity(self):
        c, delta = s.black_scholes(100, 100, 1, 0.05, 0.2, "C")
        p, pd = s.black_scholes(100, 100, 1, 0.05, 0.2, "P")
        self.assertAlmostEqual(c, 10.450583572, places=8)
        self.assertAlmostEqual(delta, 0.636830651, places=8)
        self.assertAlmostEqual(c - p, 100 - 100 * math.exp(-0.05), places=10)
        self.assertAlmostEqual(delta - pd, 1)

    def test_expiry_and_zero_volatility_are_finite(self):
        self.assertEqual(s.black_scholes(49, 50, 0, 0, 0.2, "P"), (1, -1))
        self.assertEqual(s.black_scholes(50, 50, 0, 0, 0.2, "C"), (0, 0.5))
        self.assertEqual(s.black_scholes(51, 50, 1, 0, 0, "C"), (1, 1))

    def test_implied_volatility_recovers_known_price(self):
        self.assertAlmostEqual(
            s.implied_volatility(10.450583572, 100, 100, 1, 0.05, "C"), 0.2, places=8
        )
        self.assertIsNone(s.implied_volatility(101, 100, 100, 1, 0, "C"))


class NewsTests(unittest.TestCase):
    def test_initial_rate_is_not_mistaken_for_volatility(self):
        news = [
            dict(
                tick=1,
                body="The current risk free rate is 0%. Its current annualized realized volatility is 22%.",
            )
        ]
        curve = s.VolatilityCurve.from_news(news, 20)
        self.assertEqual(curve.rate, 0)
        self.assertAlmostEqual(curve.current, 0.22)

    def test_future_forecast_applies_only_to_future_week(self):
        news = [
            dict(
                tick=1,
                body="The current risk free rate is 0%. Its current annualized realized volatility is 20%.",
            ),
            dict(
                tick=36, body="The realized volatility of RTM next week will be between 30% and 40%"
            ),
            dict(tick=75, body="The realized volatility of RTM this week will be 32%"),
        ]
        curve = s.VolatilityCurve.from_news(news, 50)
        self.assertAlmostEqual(curve.current, 0.2)
        # 25 ticks at .20 and 225 at midpoint .35; integrate variance, not sigma.
        self.assertAlmostEqual(curve.effective(50), math.sqrt((25 * 0.04 + 225 * 0.1225) / 250))
        self.assertAlmostEqual(
            curve.effective(50, "low"), math.sqrt((25 * 0.04 + 225 * 0.09) / 250)
        )
        self.assertAlmostEqual(s.VolatilityCurve.from_news(news, 80).current, 0.32)

    def test_missing_news_rejects_trading(self):
        with self.assertRaises(ValueError):
            s.VolatilityCurve.from_news([], 50)


class RiskTests(unittest.TestCase):
    def test_fee_adjusted_edge_prevents_churning(self):
        curve = s.VolatilityCurve(0, {0: (0.2, 0.2)})
        # ATM value is about 1.15 at time 1/12; a 1-cent apparent edge loses money.
        self.assertEqual(s.candidates([stock(), option(bid=1.14, ask=1.16)], curve, 0), [])

    def test_overpriced_option_selects_sell(self):
        curve = s.VolatilityCurve(0, {0: (0.2, 0.2)})
        result = s.candidates([stock(), option(bid=2, ask=2.02)], curve, 0)
        self.assertEqual(result[0]["action"], "SELL")
        self.assertGreater(result[0]["edge"], 0.7)

    def test_hedge_is_target_minus_actual_not_division(self):
        securities = [stock(), option(position=10)]
        # For an ATM zero-rate call, delta=(1+call_price/spot)/2=.5191.
        self.assertEqual(s.hedge_quantity(securities, 0, 0.2, 0), -519)
        securities[0]["position"] = -500
        self.assertEqual(s.hedge_quantity(securities, 0, 0.2, 0), -19)


if __name__ == "__main__":
    unittest.main()
