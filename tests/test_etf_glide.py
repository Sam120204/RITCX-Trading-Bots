"""Regressions for the live tender-first policy across multiple decision ticks."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from ritcx.etf.glide import Glide, basket_signal
from ritcx.etf.mock_market import ETFMarket, LocalETFClient
from ritcx.runtime import TradingError


class GlideTests(unittest.TestCase):
    def setUp(self):
        self.market = ETFMarket(12)
        self.market.settings = replace(
            self.market.settings, request_min=0, request_max=0, order_min=0, order_max=0
        )
        self.client = LocalETFClient(self.market)
        self.client.bind_heat(self.market.case())
        self.glide = Glide()
        self.tender = dict(
            tender_id=1000,
            ticker="RITC",
            action="BUY",
            quantity=70000,
            price=24.75,
            expires=6,
            is_fixed_bid=True,
        )
        self.market.offers.append(self.tender)

    def cycle(self):
        case = self.market.case()
        self.client.bind_snapshot(case)
        self.glide.finish_resting(self.client)
        self.glide.cycle(
            self.client,
            case,
            self.client.get("securities"),
            self.market.books,
            [self.tender],
            no_baskets=True,
        )

    def test_owned_quote_and_tender_are_reconciled_across_refreshes(self):
        self.cycle()
        self.assertEqual(self.market.accepted, {1000})
        self.assertEqual(len(self.client.get("orders?status=OPEN")), 1)
        self.assertLessEqual(abs(self.market.byname["USD"]["position"]), 1)
        self.market.advance_time(1)
        self.cycle()  # Feed the same accepted ID as if a tender list were cached.
        self.assertEqual(self.glide.accepted_ids, {1000})
        self.assertLessEqual(len(self.client.get("orders?status=OPEN")), 1)
        self.glide.finish_resting(self.client)
        self.assertFalse(self.client.get("orders?status=OPEN"))
        self.assertGreater(self.market.byname["RITC"]["position"], 0)

    def test_observation_and_hold_to_close_do_not_place_unwind_orders(self):
        self.client.execute = False
        self.cycle()
        self.assertFalse(self.market.accepted)
        self.assertFalse(self.market.receipts)
        self.client.execute = True
        self.glide.hold_to_close = True
        self.cycle()
        self.market.advance_time(2)
        self.cycle()
        self.assertEqual(self.market.byname["RITC"]["position"], 70000)
        self.assertEqual({o["ticker"] for o in self.market.receipts.values()}, {"USD"})

    def test_favorable_reversion_slows_both_long_and_short_exits(self):
        for sign in (1, -1):
            favorable, adverse = (Glide(allowed=90000, base=90000, path_tick=10) for _ in range(2))
            slow, small = favorable.plan(11, sign * 90000, 25, 25 + sign * 0.10)
            fast, large = adverse.plan(11, sign * 90000, 25, 25 - sign * 0.10)
            self.assertLess(slow, fast)
            self.assertLess(small, large)
            self.assertEqual(adverse.plan(11, sign * 90000, 25, 25 - sign * 0.10)[1], large)

    def test_basket_uses_remaining_touch_and_leaves_room_for_tenders(self):
        books = self.market.books
        for row in books["RITC"]["asks"]:
            row["price"] -= 1
        books["RITC"]["asks"][0].update(quantity=1200, quantity_filled=550)
        signal = basket_signal(books, self.client.get("securities"))
        self.assertEqual((signal["direction"], signal["quantity"]), (1, 650))
        for t, pos in (("BULL", -12500), ("BEAR", -12500), ("RITC", 12500)):
            self.market.byname[t]["position"] = pos
        self.assertIsNone(basket_signal(books, self.client.get("securities")))

    def test_wrong_order_id_halts_before_cancel(self):
        self.cycle()
        with patch.object(self.client, "get", return_value={"order_id": 999, "status": "OPEN"}):
            with self.assertRaises(TradingError):
                self.glide.finish_resting(self.client)
        self.assertTrue(self.client.get("orders?status=OPEN"))
