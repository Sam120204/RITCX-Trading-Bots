"""Regressions for executable prices, intermediate risk and research signals."""

import math
import unittest

from test_etf import level, securities
from test_strategy import option, stock

from ritcx.etf import model as etf
from ritcx.volatility import model as vol


class ETFResearchTests(unittest.TestCase):
    def books(self):
        return {
            "BULL": {"bids": [level(10, 100000)], "asks": [level(10.02, 100000)]},
            "BEAR": {"bids": [level(15, 100000)], "asks": [level(15.02, 100000)]},
            "RITC": {"bids": [level(20, 100000)], "asks": [level(20.02, 100000)]},
            "USD": {"bids": [level(1.3, 1000000)], "asks": [level(1.301, 1000000)]},
        }

    def test_fx_depth_cannot_price_whole_basket_from_one_dollar(self):
        books = self.books()
        books["USD"]["bids"] = [level(1.3, 1), level(1.0, 100000)]
        self.assertEqual(etf.basket_opportunities(books, securities(), 500), [])

    def test_insufficient_full_notional_fx_depth_rejects_signal(self):
        books = self.books()
        books["USD"]["bids"] = [level(1.3, 9999)]
        self.assertEqual(etf.basket_opportunities(books, securities(), 500), [])

    def test_late_tender_requires_larger_edge_for_settlement(self):
        t = dict(
            tender_id=1,
            ticker="RITC",
            quantity=100000,
            action="BUY",
            price=24.91,
            is_fixed_bid=True,
            expires=300,
        )
        book = {"bids": [level(24.98, 10000)], "asks": []}
        offer = etf.tender_opportunity(t, book, securities(), 286)
        self.assertEqual(offer["verdict"], "pass")
        self.assertEqual(offer["exit_cost_usd"], 0)
        self.assertEqual(offer["bar_usd"], 0.10)
        self.assertIsNone(etf.tender_opportunity(t, book, securities(), 297))

    def test_tender_reserves_purchase_cash_before_acceptance(self):
        sec = securities()
        sec[1]["position"] = -7000000
        t = dict(
            tender_id=1,
            ticker="RITC",
            quantity=100000,
            action="BUY",
            price=24.80,
            is_fixed_bid=True,
            expires=100,
        )
        offer = etf.tender_opportunity(t, {"bids": [level(24.98, 200000)], "asks": []}, sec, 20)
        self.assertEqual(offer["verdict"], "blocked")

    def test_thin_book_uses_cost_allowance_and_invalid_fx_skips_offer(self):
        sec = securities()
        t = dict(
            tender_id=1,
            ticker="RITC",
            quantity=10000,
            action="BUY",
            price=24.92,
            is_fixed_bid=True,
            expires=100,
        )
        book = {"bids": [level(24.98, 50)], "asks": []}
        offer = etf.tender_opportunity(t, book, sec, 20)
        self.assertAlmostEqual(offer["exit_cost_usd"], 0.055)
        self.assertEqual(offer["verdict"], "pass")
        sec[1]["bid"] = 0
        self.assertIsNone(etf.tender_opportunity(t, book, sec, 20))

    def test_malformed_book_and_action_are_rejected(self):
        for book, action in [
            ({"bids": [level(float("nan"), 10)], "asks": []}, "SELL"),
            ({"bids": [level(25, 10)], "asks": []}, "WRONG"),
        ]:
            with self.assertRaises(ValueError):
                etf.execution_value(book, action, 10)

    def test_live_net_limit_is_checked_as_well_as_published_weights(self):
        sec = securities()
        changes = {"RITC": 100000, "BULL": -100000, "BEAR": -100000}
        self.assertFalse(etf.within_risk(sec, changes, gross_cap=500000, net_cap=100000))

    def test_projected_cash_includes_equity_fees(self):
        sec = securities()
        for item in sec:
            item.update(bid=20, ask=20.02)
        projected = etf.project_positions(sec, {"RITC": 100})
        self.assertAlmostEqual(
            next(s["position"] for s in projected if s["ticker"] == "USD"), -2004
        )
        self.assertEqual(next(s["position"] for s in projected if s["ticker"] == "RITC"), 100)

    def test_full_basket_plan_is_rejected_before_first_risky_leg(self):
        sec = securities()
        for item in sec:
            item.update(bid=20, ask=20.02)
        # Final weighted net is zero, but the first ETF leg would breach net capacity.
        self.assertFalse(etf.basket_allowed(sec, 1, 500, net_cap=900))


class VolatilityResearchTests(unittest.TestCase):
    def test_range_with_single_percent_sign_and_html_is_parsed(self):
        news = [
            dict(tick=1, body="Risk-free rate is 0%. Realized volatility is 20%."),
            dict(
                tick=37,
                body="Realized volatility of RTM for next week will be between <b>27-30%</b>.",
            ),
        ]
        curve = vol.VolatilityCurve.from_news(news, 40)
        self.assertEqual(curve.weeks[75], (0.27, 0.30))
        self.assertAlmostEqual(
            curve.effective(40, "low"), math.sqrt((35 * 0.2**2 + 225 * 0.27**2) / 260)
        )

    def test_invalid_numeric_pricing_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            vol.black_scholes(float("nan"), 50, 0.1, 0, 0.2, "C")

    def test_put_call_parity_scanner_subtracts_three_leg_costs(self):
        sec = [stock(), option("RTM50C", bid=2, ask=2.02), option("RTM50P", bid=1, ask=1.02)]
        signals = vol.parity_opportunities(sec, 0, 0)
        self.assertEqual(signals[0]["direction"], "sell_synthetic")
        # 100 * (2 - 1.02 - 50.01 + 50) = 97; round-trip fees = 12.
        self.assertAlmostEqual(signals[0]["net_edge"], 85)
        self.assertEqual(signals[0]["strike"], 50)


if __name__ == "__main__":
    unittest.main()
