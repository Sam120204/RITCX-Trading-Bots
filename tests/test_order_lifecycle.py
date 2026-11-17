"""Real HTTP order lifecycle tests; all mutations stay on a local fake simulator."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from test_strategy import stock

from ritcx.api import DMAClient
from ritcx.runtime import TradingError


class OrderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.sec = stock()
        self.posts = []
        self.deletes = []
        self.fill = 4
        self.cancel_race = 0
        self.case = dict(name="RITCx Volatility Trading Case", period=1, tick=20, status="ACTIVE")
        self.order = None
        self.headers = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                outer.headers.append(dict(self.headers))
                ep = urlsplit(self.path).path.removeprefix("/v1/")
                if ep == "case":
                    return self.reply(outer.case)
                if ep == "securities":
                    return self.reply([outer.sec])
                if ep == "orders/42":
                    return self.reply(outer.order)
                if ep == "orders":
                    return self.reply(
                        [outer.order] if outer.order and outer.order["status"] == "OPEN" else []
                    )
                self.reply({})

            def do_POST(self):
                q = parse_qs(urlsplit(self.path).query)
                outer.posts.append(q)
                quantity = int(q["quantity"][0])
                sign = 1 if q["action"][0] == "BUY" else -1
                outer.sec["position"] += sign * outer.fill
                outer.order = dict(
                    order_id=42,
                    ticker="RTM",
                    action=q["action"][0],
                    quantity=quantity,
                    quantity_filled=outer.fill,
                    status="OPEN",
                    vwap=50,
                )
                self.reply(outer.order)

            def do_DELETE(self):
                outer.deletes.append(self.path)
                sign = 1 if outer.order["action"] == "BUY" else -1
                outer.sec["position"] += sign * outer.cancel_race
                outer.order["quantity_filled"] += outer.cancel_race
                outer.order["status"] = "CANCELLED"
                self.reply({"success": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, **kwargs):
        client = DMAClient(self.url, "local-test", "local-secret", execute=True, **kwargs)
        client.bind_heat(self.case)
        return client

    def test_partial_fill_cancels_only_own_remainder_before_reporting_actual_exposure(self):
        with self.assertRaises(TradingError) as caught:
            self.client().order(dict(self.sec), 10, "BUY")
        self.assertEqual(self.sec["position"], 4)
        self.assertEqual(self.deletes, ["/v1/orders/42"])
        self.assertEqual(getattr(caught.exception, "filled", None), 4)
        self.assertEqual(len(self.posts), 1)

    def test_cancel_race_is_reconciled_from_final_status_not_initial_receipt(self):
        self.cancel_race = 2
        with self.assertRaises(TradingError) as caught:
            self.client().order(dict(self.sec), 10, "BUY")
        self.assertEqual(getattr(caught.exception, "filled", None), 6)
        self.assertEqual(self.sec["position"], 6)
        self.assertEqual(len(self.posts), 1)

    def test_stale_decision_cannot_submit_even_if_heat_is_still_active(self):
        client = self.client()
        client.bind_snapshot(self.case)
        self.case["tick"] = 24
        with self.assertRaises(TradingError):
            client.order(dict(self.sec), 10, "BUY")
        self.assertEqual(self.posts, [])

    def test_converter_endpoint_is_never_available_to_trading_code(self):
        with self.assertRaises(TradingError):
            self.client().request("POST", "leases", {"id": 1})
        self.assertEqual(self.posts, [])

    def test_client_rest_uses_api_key_header_without_basic_auth(self):
        self.client(api_key="local-api-key").get("case")
        self.assertEqual(self.headers[-1].get("X-Api-Key"), "local-api-key")
        self.assertNotIn("Authorization", self.headers[-1])


if __name__ == "__main__":
    unittest.main()
