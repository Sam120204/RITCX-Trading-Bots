"""Complete trading workflows against local HTTP, never external accounts."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_etf import level, securities
from test_strategy import option, stock

from ritcx.volatility.model import black_scholes


class CompetitionCLITests(unittest.TestCase):
    def run_case(self, kind, trade=True, requested_case=None):
        posted, orders = [], []
        news_reads = 0
        if kind == "volatility":
            sec = [stock()]
            for strike in range(48, 53):
                for cp in "CP":
                    fair = black_scholes(50, strike, 299 / 3600, 0, 0.2, cp)[0]
                    bid = 2 if (strike, cp) == (50, "C") else max(0, fair - 0.01)
                    sec.append(option(f"RTM{strike}{cp}", bid=bid, ask=bid + 0.02))
            name = "RITCx Volatility Trading Case"
            limits = [
                dict(name="Limit-ETF", gross_limit=50000, net_limit=50000),
                dict(name="Limit-OPT", gross_limit=2500, net_limit=1000),
            ]
        else:
            sec = securities()
            for s in sec:
                p = {"CAD": 1, "USD": 1, "BULL": 10, "BEAR": 15, "RITC": 25}[s["ticker"]]
                s.update(bid=p, ask=p + 0.02)
            name = "RITCx - Algorithmic ETF Arbitrage Case"
            limits = [
                dict(name="LIMIT-STOCK", gross_limit=300000, net_limit=200000),
                dict(name="LIMIT-CASH", gross_limit=10000000, net_limit=10000000),
            ]
        tender = dict(
            tender_id=3,
            ticker="RITC",
            quantity=10000,
            action="BUY",
            price=24.7,
            is_fixed_bid=True,
            expires=100,
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                nonlocal news_reads
                path = urlsplit(self.path)
                ep = path.path.removeprefix("/v1/")
                if ep == "case":
                    tick = (
                        (285 if len(posted) >= 2 else (0 if news_reads == 0 else 1))
                        if kind == "volatility"
                        else (275 if len(posted) >= 3 else 20)
                    )
                    settled = kind == "etf" and len(orders) == 3
                    return self.reply(
                        dict(
                            name=name,
                            period=1,
                            tick=0 if settled else tick,
                            status="STOPPED" if settled else "ACTIVE",
                            ticks_per_period=300,
                            total_periods=1,
                        )
                    )
                if ep == "securities":
                    return self.reply(sec)
                if ep == "limits":
                    return self.reply(limits)
                if ep == "trader":
                    return self.reply(dict(nlv=500 - 10 * len(orders), total_fines=0))
                if ep == "orders":
                    return self.reply([])
                if ep == "news":
                    news_reads += 1
                    return self.reply(
                        []
                        if news_reads == 1
                        else [
                            dict(
                                tick=1,
                                body="Risk free rate is 0%. Annualized realized volatility is 20%.",
                            )
                        ]
                    )
                if ep == "tenders":
                    return self.reply([] if posted else [tender])
                if ep == "securities/book":
                    s = next(s for s in sec if s["ticker"] == parse_qs(path.query)["ticker"][0])
                    return self.reply(
                        dict(bids=[level(s["bid"], 1000000)], asks=[level(s["ask"], 1000000)])
                    )
                self.send_error(404)

            def do_POST(self):
                path = urlsplit(self.path)
                q = parse_qs(path.query)
                posted.append(path.path)
                if path.path.endswith("/tenders/3"):
                    next(s for s in sec if s["ticker"] == "RITC")["position"] += 10000
                    next(s for s in sec if s["ticker"] == "USD")["position"] -= 247000
                    return self.reply({"success": True})
                ticker = q["ticker"][0]
                quantity = int(q["quantity"][0])
                side = 1 if q["action"][0] == "BUY" else -1
                s = next(s for s in sec if s["ticker"] == ticker)
                s["position"] += side * quantity
                if kind == "etf":
                    cash = next(
                        c
                        for c in sec
                        if c["ticker"] == ("CAD" if ticker == "USD" else s["currency"])
                    )
                    cash["position"] -= (
                        side * quantity * s["ask" if side > 0 else "bid"]
                        + quantity * s["trading_fee"]
                    )
                orders.append(dict(ticker=ticker, action=q["action"][0], quantity=quantity))
                self.reply(
                    dict(order_id=len(posted), quantity_filled=quantity, status="TRANSACTED")
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                # A fresh checkout keeps this test independent of real account profiles.
                shutil.copytree(
                    Path(__file__).resolve().parents[1] / "ritcx",
                    Path(temp) / "ritcx",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                config = Path(temp) / ".env.contest"
                config.write_text(
                    f"RIT_ENVIRONMENT=contest\nRIT_API_URL=http://127.0.0.1:{server.server_port}/v1\nRIT_USERNAME=cli-{kind}-test\nRIT_PASSWORD=local-secret\nRIT_LOG_DIR={temp}/logs\n"
                )
                command = [
                    sys.executable,
                    "-m",
                    "ritcx",
                    requested_case or kind,
                    "--contest",
                    "--trade" if trade else "--preflight",
                ]
                result = subprocess.run(
                    command,
                    cwd=temp,
                    env={k: v for k, v in os.environ.items() if not k.startswith("RIT_")},
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            if requested_case and requested_case != kind:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertIn(requested_case, result.stderr)
                self.assertNotIn("STARTING", result.stdout)
                self.assertEqual(posted, [])
                return
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if trade:
                self.assertIn("final_reconciliation", result.stdout)
                self.assertIn('"completed": 1', result.stdout)
                summary = next(
                    json.loads(line) for line in result.stdout.splitlines() if '"HEAT_PNL"' in line
                )
                self.assertEqual(summary["pnl"], -10 * len(orders))
                self.assertEqual(summary["average_pnl"], summary["pnl"])
                self.assertEqual(summary["heats"], 1)
                self.assertFalse(any(s["position"] for s in sec if s["ticker"] != "CAD"), sec)
            else:
                self.assertEqual(posted, [])
            return posted, orders
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_volatility_waits_for_initial_news_trades_hedges_and_closes(self):
        _, orders = self.run_case("volatility")
        self.assertEqual(orders[0]["ticker"], "RTM50C")
        self.assertEqual(orders[0]["action"], "SELL")
        self.assertGreater(orders[0]["quantity"], 10)
        self.assertEqual(orders[1]["ticker"], "RTM")
        self.assertEqual(
            sum(
                o["quantity"] * (1 if o["action"] == "BUY" else -1)
                for o in orders
                if o["ticker"] == "RTM50C"
            ),
            0,
        )

    def test_etf_accepts_qualified_tender_unwinds_and_converts_net_usd_cash(self):
        posted, orders = self.run_case("etf")
        self.assertEqual(posted[0], "/v1/tenders/3")
        self.assertEqual([o["action"] for o in orders], ["BUY", "SELL", "SELL"])
        self.assertEqual(
            [(o["ticker"], o["quantity"]) for o in orders],
            [("USD", 247000), ("RITC", 10000), ("USD", 249800)],
        )

    def test_preflight_sends_no_mutations_even_with_profitable_tender(self):
        self.run_case("etf", trade=False)

    def test_wrong_case_stops_before_launch_in_trade_and_preflight_modes(self):
        self.run_case("volatility", requested_case="etf")
        self.run_case("etf", trade=False, requested_case="volatility")


if __name__ == "__main__":
    unittest.main()
