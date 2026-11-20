"""Synthetic ETF accounting, execution isolation, and heat lifecycle."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ritcx.etf.mock_engine import run_heat
from ritcx.etf.mock_market import ETFMarket, LocalETFClient
from ritcx.runtime import TradingError


class ETFMarketTests(unittest.TestCase):
    def test_round_trip_charges_spread_and_fees_in_quote_currency(self):
        market = ETFMarket(7)
        for ticker, rate in [("BULL", 1), ("RITC", market.mid("USD"))]:
            before = market.snapshot()["pnl"]
            spread = market.byname[ticker]["ask"] - market.byname[ticker]["bid"]
            market.fill(ticker, 100, "BUY")
            market.fill(ticker, 100, "SELL")
            self.assertAlmostEqual(market.snapshot()["pnl"] - before, -(spread + .04) * 100 * rate)

    def test_cad_nlv_includes_usd_cash_and_inventory_without_double_fees(self):
        market = ETFMarket(7)
        market.fill("RITC", 100, "BUY")
        market.fill("BULL", 100, "SELL")
        market.advance_time(10)
        p = {k: s["position"] for k, s in market.byname.items()}
        expected = p["CAD"] + p["USD"] * market.mid("USD")
        expected += p["RITC"] * market.mid("RITC") * market.mid("USD")
        expected += p["BULL"] * market.mid("BULL") + p["BEAR"] * market.mid("BEAR")
        self.assertAlmostEqual(market.snapshot()["pnl"], expected)
        self.assertGreater(market.snapshot()["fees"], 0)

    def test_depth_is_consumed_and_market_order_can_partially_fill(self):
        market = ETFMarket(7)
        market.books["RITC"]["asks"] = [dict(price=25.01, quantity=120, quantity_filled=0)]
        receipt = market.fill("RITC", 500, "BUY")
        self.assertEqual(receipt["quantity_filled"], 120)
        self.assertEqual(receipt["status"], "CANCELLED")
        self.assertEqual(market.fill("RITC", 500, "BUY")["quantity_filled"], 0)

    def test_terminal_settlement_corrects_etf_divergence_and_clears_fx(self):
        market = ETFMarket(7)
        market.fill("RITC", 100, "BUY")
        market.advance_time(299)
        usd_before = market.byname["USD"]["position"]
        fees = market.fees
        market.advance_time(1)
        expected = (usd_before + 100 * market.fair_etf) * market.mid("USD")
        self.assertAlmostEqual(market.snapshot()["pnl"], expected)
        self.assertEqual(market.snapshot()["positions"], {})
        self.assertEqual(market.fees, fees)
        self.assertEqual(market.settled_positions["RITC"], 100)
        with self.assertRaises(TradingError):
            market.fill("BULL", 1, "BUY")

    def test_tender_is_hidden_until_release_and_cannot_be_accepted_twice(self):
        market = ETFMarket(7)
        self.assertEqual(market.tenders(), [])
        market.advance_time(35)
        tender = market.tenders()[0]
        market.accept(tender["tender_id"])
        self.assertEqual(abs(market.byname["RITC"]["position"]), tender["quantity"])
        self.assertEqual(market.fees, 0)
        with self.assertRaises(TradingError):
            market.accept(tender["tender_id"])

    def test_seed_is_independent_of_transport_requests(self):
        a, b = ETFMarket(7), ETFMarket(7)
        client = LocalETFClient(a)
        for _ in range(20):
            client.get("securities")
        a.advance_time(80 - a.clock)
        b.advance_time(80)
        self.assertEqual(a.snapshot(), b.snapshot())


class ETFHeatTests(unittest.TestCase):
    def test_full_heat_reuses_execution_without_network_and_is_repeatable(self):
        with patch("ritcx.api.urlopen", side_effect=AssertionError("Network forbidden")):
            a, b = run_heat(7), run_heat(7)
        self.assertEqual(a, b)
        self.assertEqual(a["status"], "completed", a["error"])
        self.assertEqual(a["tick"], 300)
        self.assertTrue(a["account_flat_after_settlement"])
        self.assertEqual(a["positions"], {})
        self.assertGreater(a["orders"], 0)
        self.assertGreater(a["fees"], 0)

    def test_failed_run_is_retained_and_inventory_settles(self):
        original = LocalETFClient.request

        def fail(client, method, endpoint, params=None):
            if method == "POST" and client.market_state.fills:
                raise TradingError("injected failure")
            return original(client, method, endpoint, params)

        with patch.object(LocalETFClient, "request", fail):
            result = run_heat(7)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["tick"], 300)
        self.assertTrue(result["positions_at_error"])
        self.assertTrue(result["settled_positions"])
        self.assertFalse(result["strategy_finished_flat"])

    def test_interruption_does_not_advance_to_expiry(self):
        result = run_heat(7, stopped=lambda: True)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["tick"], 0)

    def test_cli_saves_full_heat_and_rejects_invalid_speed(self):
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, "-m", "ritcx.etf.mock", "--seed", "7", "--plain",
                       "--output-dir", folder, "--speed"]
            result = subprocess.run(command + ["10000"], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("300/300", result.stdout)
            report = json.loads(next(Path(folder).glob("*.json")).read_text())
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["source"], "local_synthetic_etf_market")
            result = subprocess.run(command + ["nan"], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
