import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from test_strategy import option, stock

from ritcx.api import DMAClient
from ritcx.execution import confirm_flat
from ritcx.runtime import TradingError
from ritcx.volatility.bot import trade_once


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.securities = [stock(), option(bid=2, ask=2.02)]
        self.posts = []
        self.reject = False
        self.advance_after_option = 0
        self.advance_after_stock = 0
        self.case = dict(
            name="RITCx Volatility Trading Case",
            tick=20,
            status="ACTIVE",
            period=1,
            ticks_per_period=300,
            total_periods=1,
        )
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, data, status=200):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                if (
                    self.headers.get("Authorization")
                    != "Basic " + base64.b64encode(b"test:secret").decode()
                ):
                    return self.answer({"code": "UNAUTHORIZED"}, 401)
                ep = urlsplit(self.path).path
                if ep.endswith("/case"):
                    return self.answer(outer.case)
                if ep.endswith("/securities"):
                    return self.answer(outer.securities)
                if ep.endswith("/orders"):
                    return self.answer([])
                self.answer({})

            def do_POST(self):
                query = parse_qs(urlsplit(self.path).query)
                outer.posts.append(query)
                if outer.reject:
                    return self.answer({"code": "ERROR"}, 500)
                sec = next(x for x in outer.securities if x["ticker"] == query["ticker"][0])
                qty = int(query["quantity"][0])
                side = query["action"][0]
                sec["position"] += qty if side == "BUY" else -qty
                if sec["type"] == "OPTION":
                    outer.case["tick"] += outer.advance_after_option
                else:
                    outer.case["tick"] += outer.advance_after_stock
                self.answer(
                    dict(order_id=len(outer.posts), quantity_filled=qty, status="TRANSACTED")
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, execute=False, password="secret"):
        client = DMAClient(self.url, "test", password, execute=execute, audit=lambda *a, **k: None)
        client.bind_heat(self.case)
        return client

    def test_dry_run_cannot_send_orders(self):
        with self.assertRaises(TradingError):
            self.client().order(self.securities[1], 10, "SELL")
        self.assertEqual(self.posts, [])

    def test_successful_finish_requires_flat_reconciled_account(self):
        self.securities[1]["position"] = 10
        with self.assertRaises(TradingError):
            confirm_flat(self.client())
        self.securities[1]["position"] = 0
        result = confirm_flat(self.client())
        self.assertEqual(result["positions"], {})
        self.assertEqual(result["open_orders"], [])

    def test_auth_failure_stops(self):
        with self.assertRaises(TradingError):
            self.client(password="wrong").get("case")

    def test_failed_submission_is_never_retried(self):
        self.reject = True
        with self.assertRaises(TradingError):
            self.client(True).order(self.securities[1], 10, "SELL")
        self.assertEqual(len(self.posts), 1)

    def test_fill_is_followed_by_etf_delta_hedge(self):
        from ritcx.volatility.model import VolatilityCurve

        client = self.client(True)
        result = trade_once(client, self.securities, VolatilityCurve(0, {0: (0.2, 0.2)}), 20, False)
        self.assertEqual(self.securities[1]["position"], -10)
        self.assertEqual(self.securities[0]["position"], 520)
        self.assertEqual([x["ticker"][0] for x in self.posts], ["RTM50C", "RTM"])
        self.assertEqual(result, "entry")

    def test_delayed_confirmed_option_fill_refreshes_hedge_snapshot(self):
        from ritcx.volatility.model import VolatilityCurve

        self.advance_after_option = 3
        client = self.client(True)
        client.bind_snapshot(self.case)
        trade_once(client, self.securities, VolatilityCurve(0, {0: (0.2, 0.2)}), 20)
        self.assertEqual(self.securities[1]["position"], -10)
        self.assertGreater(self.securities[0]["position"], 0)
        self.assertEqual([p["ticker"][0] for p in self.posts], ["RTM50C", "RTM"])

    def test_delayed_stock_hedge_defers_option_close_during_liquidation(self):
        from ritcx.volatility.model import VolatilityCurve

        self.securities[1]["position"] = -10
        self.advance_after_stock = 3
        client = self.client(True)
        client.bind_snapshot(self.case)
        result = trade_once(client, self.securities, VolatilityCurve(0, {0: (0.2, 0.2)}), 20, True)
        self.assertEqual(result, "hedged")
        self.assertEqual([p["ticker"][0] for p in self.posts], ["RTM"])
        self.assertEqual(self.securities[1]["position"], -10)
        client.bind_snapshot(self.case)
        trade_once(
            client, self.securities, VolatilityCurve(0, {0: (0.2, 0.2)}), self.case["tick"], True
        )
        self.assertEqual([s["position"] for s in self.securities], [0, 0])

    def test_flatten_closes_options_then_stock(self):
        from ritcx.volatility.model import VolatilityCurve

        self.securities[1]["position"] = -10
        self.securities[0]["position"] = 510
        client = self.client(True)
        for _ in range(3):
            trade_once(client, self.securities, VolatilityCurve(0, {0: (0.2, 0.2)}), 20, True)
        self.assertEqual([x["position"] for x in self.securities], [0, 0])

    def test_per_order_maximum_is_enforced(self):
        with self.assertRaises(TradingError):
            self.client(True).order(self.securities[1], 101, "SELL")
        self.assertEqual(self.posts, [])

    def test_every_submission_checks_the_bound_heat(self):
        client = self.client(True)
        client.bind_heat(self.case)
        client.order(self.securities[1], 10, "SELL")
        self.case["tick"] = 0
        with self.assertRaises(TradingError):
            client.order(self.securities[1], 10, "SELL")
        self.assertEqual(len(self.posts), 1)

    def test_changed_period_or_case_or_inactive_blocks_tenders_too(self):
        for change in ({"period": 2}, {"name": "Other"}, {"status": "STOPPED"}):
            with self.subTest(change=change):
                self.case = dict(
                    name="RITCx Volatility Trading Case", tick=20, status="ACTIVE", period=1
                )
                client = self.client(True)
                client.bind_heat(self.case)
                self.case.update(change)
                with self.assertRaises(TradingError):
                    client.request("POST", "tenders/123")
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
