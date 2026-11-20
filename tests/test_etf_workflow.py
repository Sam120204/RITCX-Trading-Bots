import unittest
from unittest.mock import patch

from test_etf import level, securities

from ritcx.etf.execution import ETFClient, accept_tender, basket_trade, unwind_all
from ritcx.runtime import TradingError


class ETFWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.sec = securities()
        for s in self.sec:
            p = {"CAD": 1, "USD": 1, "RITC": 25, "BULL": 10, "BEAR": 15}[s["ticker"]]
            s.update(bid=p, ask=p + 0.02)
        self.orders = {}
        self.posted = []
        self.partial_first = False
        self.case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case", period=1, tick=30, status="ACTIVE"
        )
        self.client = ETFClient("http://127.0.0.1/v1", "test", "secret", execute=True)
        self.client.bind_heat(self.case)

    def transport(self, method, endpoint, params=None):
        if method == "GET":
            if endpoint == "case":
                return dict(self.case)
            if endpoint == "securities":
                return [dict(s) for s in self.sec]
            if endpoint == "orders?status=OPEN":
                return [dict(o) for o in self.orders.values() if o["status"] == "OPEN"]
            if endpoint.startswith("orders/"):
                return dict(self.orders[int(endpoint.split("/")[1])])
            if endpoint.startswith("securities/book"):
                ticker = endpoint.split("ticker=")[1].split("&")[0]
                s = next(s for s in self.sec if s["ticker"] == ticker)
                return {"bids": [level(s["bid"], 1000000)], "asks": [level(s["ask"], 1000000)]}
        if method == "DELETE":
            self.orders[int(endpoint.split("/")[1])]["status"] = "CANCELLED"
            return {"success": True}
        if method == "POST" and endpoint == "orders":
            self.posted.append(dict(params))
            q = params["quantity"]
            fill = 100 if self.partial_first and len(self.posted) == 1 else q
            side = 1 if params["action"] == "BUY" else -1
            sec = next(s for s in self.sec if s["ticker"] == params["ticker"])
            sec["position"] += fill * side
            price = sec["ask" if side == 1 else "bid"]
            cash = next(
                s
                for s in self.sec
                if s["ticker"] == ("CAD" if params["ticker"] == "USD" else sec["currency"])
            )
            cash["position"] -= fill * side * price + fill * sec["trading_fee"]
            order = dict(
                order_id=len(self.posted),
                quantity=q,
                quantity_filled=fill,
                status="OPEN" if fill < q else "TRANSACTED",
                vwap=price,
            )
            self.orders[order["order_id"]] = order
            return dict(order)
        raise AssertionError((method, endpoint, params))

    def test_tender_ack_can_precede_position_update_without_a_second_post(self):
        before = self.transport("GET", "securities")
        after = self.transport("GET", "securities")
        next(s for s in after if s["ticker"] == "RITC")["position"] = 75000
        tender = dict(tender_id=721, action="BUY", quantity=75000, price=24.92)
        with (
            patch.object(self.client, "request", return_value={"success": True}) as post,
            patch.object(self.client, "get", side_effect=[before, before, after, []]),
            patch("ritcx.etf.execution.time.sleep"),
        ):
            self.assertEqual(accept_tender(self.client, tender, before), after)
        post.assert_called_once_with("POST", "tenders/721", {"price": 24.92})

    def test_fx_receipt_accepts_exact_decimal_balance_after_float_arithmetic(self):
        before = self.transport("GET", "securities")
        after = self.transport("GET", "securities")
        next(s for s in before if s["ticker"] == "USD")["position"] = -7471.52
        next(s for s in after if s["ticker"] == "USD")["position"] = 0.48
        receipt = dict(order_id=9, quantity_filled=7472, status="TRANSACTED", vwap=1.0124)
        with (
            patch.object(self.client, "request", return_value=receipt),
            patch.object(self.client, "get", side_effect=[before, after, []]),
        ):
            self.assertEqual(self.client.market("USD", "BUY", 7472)["quantity_filled"], 7472)

    def test_partial_first_basket_leg_is_cancelled_and_unwound_before_other_legs(self):
        self.partial_first = True
        with patch.object(self.client, "request", side_effect=self.transport):
            basket_trade(self.client, self.transport("GET", "securities"), 1, 500)
        self.assertEqual([p["ticker"] for p in self.posted[:2]], ["RITC", "RITC"])
        self.assertEqual(self.posted[1]["quantity"], 100)
        self.assertFalse(
            any(s["position"] for s in self.sec if s["ticker"] in ("BULL", "BEAR", "RITC"))
        )
        self.assertFalse(any(o["status"] == "OPEN" for o in self.orders.values()))

    def test_emergency_exit_uses_market_orders_and_hedges_cash(self):
        next(s for s in self.sec if s["ticker"] == "RITC")["position"] = 500
        with patch.object(self.client, "request", side_effect=self.transport):
            latest = unwind_all(self.client, self.transport("GET", "securities"))
        self.assertEqual([p["type"] for p in self.posted], ["MARKET", "MARKET"])
        self.assertFalse(
            any(s["position"] for s in latest if s["ticker"] in ("BULL", "BEAR", "RITC", "USD"))
        )

    def test_partial_market_exit_closes_only_reconciled_remainder(self):
        self.partial_first = True
        next(s for s in self.sec if s["ticker"] == "RITC")["position"] = 500
        with patch.object(self.client, "request", side_effect=self.transport):
            latest = unwind_all(self.client, self.transport("GET", "securities"))
        self.assertEqual(self.orders[1]["status"], "CANCELLED")
        replacements = [p for p in self.posted if p["ticker"] == "RITC"]
        self.assertEqual([p["quantity"] for p in replacements], [500, 400])
        self.assertEqual(next(s["position"] for s in latest if s["ticker"] == "RITC"), 0)

    def test_basket_plan_preflight_sends_nothing_when_cash_capacity_is_too_small(self):
        next(s for s in self.sec if s["ticker"] == "USD")["position"] = -7990000
        with patch.object(self.client, "request", side_effect=self.transport):
            with self.assertRaises(TradingError):
                basket_trade(self.client, self.transport("GET", "securities"), 1, 500)
        self.assertEqual(self.posted, [])


if __name__ == "__main__":
    unittest.main()
