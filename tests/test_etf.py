import unittest

from ritcx.etf.model import basket_opportunities, execution_value, tender_opportunity, within_risk


def level(price, quantity, filled=0):
    return dict(price=price, quantity=quantity, quantity_filled=filled, trader_id="ANON")


def securities():
    mid = {"CAD": 1, "USD": 1, "BULL": 10, "BEAR": 15, "RITC": 25}
    return [
        dict(
            bid=mid[t] - (0 if t in ("CAD", "USD") else 0.02),
            ask=mid[t] + (0 if t in ("CAD", "USD") else 0.02),
            limit_order_rebate=0.01,
            ticker=t,
            position=0,
            trading_fee=(0 if t in ("CAD", "USD") else 0.02),
            type="CURRENCY" if t in ("CAD", "USD") else "STOCK",
            size=1,
            currency=("" if t == "CAD" else "USD" if t == "RITC" else "CAD"),
            max_trade_size=2500000 if t in ("CAD", "USD") else 10000,
            execution_delay_ms=0,
            limits=[
                dict(
                    name="LIMIT-CASH" if t in ("CAD", "USD") else "LIMIT-STOCK",
                    units=0.5 if t == "RITC" else 1,
                )
            ],
        )
        for t in ("CAD", "USD", "BULL", "BEAR", "RITC")
    ]


class ETFTests(unittest.TestCase):
    def test_depth_uses_remaining_volume_not_original_quantity(self):
        book = {"bids": [level(25, 10000, 9000), level(24, 10000)], "asks": []}
        value, worst = execution_value(book, "SELL", 2000)
        self.assertEqual(value, 49000)
        self.assertEqual(worst, 24)

    def test_insufficient_liquidity_returns_no_value(self):
        self.assertIsNone(execution_value({"bids": [level(25, 100)], "asks": []}, "SELL", 101))

    def test_gate_blends_nav_and_walks_one_exit_clip(self):
        sec = securities()
        sec[2].update(bid=10.18, ask=10.22)
        t = dict(
            tender_id=1,
            ticker="RITC",
            quantity=100000,
            action="BUY",
            price=25,
            is_fixed_bid=True,
            expires=80,
        )
        book = {"bids": [level(24.98, 2500), level(24.96, 2500)], "asks": []}
        result = tender_opportunity(t, book, sec, 60, horizon=20)
        self.assertAlmostEqual(result["fair"], 25.10)
        self.assertAlmostEqual(result["exit_cost_usd"], 0.045)
        self.assertAlmostEqual(result["edge_usd"], 0.055)
        self.assertEqual(result["verdict"], "take")
        t["price"] = 25.03
        self.assertEqual(tender_opportunity(t, book, sec, 60)["verdict"], "pass")

    def test_sell_tender_walks_asks_and_credits_offset_inventory(self):
        t = dict(
            tender_id=1,
            ticker="RITC",
            quantity=10000,
            action="SELL",
            price=25.09,
            is_fixed_bid=True,
            expires=80,
        )
        book = {"bids": [], "asks": [level(25.04, 10000)]}
        sec = securities()
        result = tender_opportunity(t, book, sec, 60)
        self.assertAlmostEqual(result["exit_cost_usd"], 0.05)
        self.assertEqual(result["verdict"], "take")
        sec[-1]["position"] = 10000
        offset = tender_opportunity(t, book, sec, 60)
        self.assertAlmostEqual(offset["exit_cost_usd"], -0.05)
        self.assertAlmostEqual(offset["edge_usd"], 0.14)

    def test_expired_tenders_and_auction_bids_are_not_accepted(self):
        tender = dict(
            ticker="RITC", quantity=10000, action="BUY", price=20, is_fixed_bid=False, expires=80
        )
        book = {"bids": [level(25, 10000)], "asks": []}
        self.assertIsNone(tender_opportunity(tender, book, securities(), 60))
        tender["is_fixed_bid"] = True
        tender["expires"] = 61
        self.assertIsNone(tender_opportunity(tender, book, securities(), 60))

    def test_risk_uses_stricter_published_etf_weight(self):
        sec = securities()
        self.assertTrue(within_risk(sec, {"RITC": 100000}))
        self.assertFalse(within_risk(sec, {"RITC": 100001}))
        sec[2]["position"] = 100000
        self.assertFalse(within_risk(sec, {"RITC": 100000}))

    def test_basket_uses_fx_side_and_depth(self):
        books = {
            "BULL": {"bids": [level(10, 1000)], "asks": [level(10.02, 1000)]},
            "BEAR": {"bids": [level(15, 1000)], "asks": [level(15.02, 1000)]},
            "RITC": {"bids": [level(20, 1000)], "asks": [level(20.02, 1000)]},
            "USD": {"bids": [level(1.3, 100000)], "asks": [level(1.301, 100000)]},
        }
        signals = basket_opportunities(books, securities(), 500)
        self.assertEqual(signals[0]["direction"], -1)
        self.assertGreater(signals[0]["edge_cad"], 0.7)

    def test_basket_profitable_before_fees_but_not_after_is_rejected(self):
        books = {
            "BULL": {"bids": [level(10, 1000)], "asks": [level(10.01, 1000)]},
            "BEAR": {"bids": [level(15, 1000)], "asks": [level(15.01, 1000)]},
            "RITC": {"bids": [level(25.04, 1000)], "asks": [level(25.05, 1000)]},
            "USD": {"bids": [level(1, 100000)], "asks": [level(1, 100000)]},
        }
        self.assertEqual(basket_opportunities(books, securities(), 500), [])


if __name__ == "__main__":
    unittest.main()
