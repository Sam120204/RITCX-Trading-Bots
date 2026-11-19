"""Local simulation accounting, isolation, and complete-heat checks."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ritcx.mock_market import MockClient, MockMarket


class MockMarketTests(unittest.TestCase):
    def test_stock_round_trip_charges_spread_and_both_fees(self):
        market = MockMarket(7)
        market.fill("RTM", 100, "BUY")
        self.assertAlmostEqual(market.snapshot()["pnl"], -3)
        market.fill("RTM", 100, "SELL")
        result = market.snapshot()
        self.assertAlmostEqual(result["pnl"], -6)
        self.assertAlmostEqual(result["fees"], 4)
        self.assertAlmostEqual(result["realized_net"], -6)
        self.assertEqual(result["positions"], {})

    def test_option_contract_multiplier_and_short_close(self):
        market = MockMarket(7)
        market.fill("RTM50C", 10, "SELL")
        self.assertAlmostEqual(market.snapshot()["pnl"], -30)
        market.fill("RTM50C", 10, "BUY")
        self.assertAlmostEqual(market.snapshot()["pnl"], -60)
        self.assertAlmostEqual(market.snapshot()["fees"], 40)

    def test_seed_and_future_news_are_reproducible(self):
        a, b, c = MockMarket(7), MockMarket(7), MockMarket(8)
        for tick in range(1, 81):
            a.advance(tick)
            b.advance(tick)
            c.advance(tick)
        self.assertEqual(a.snapshot(), b.snapshot())
        self.assertNotEqual(a.spot, c.spot)
        self.assertTrue(all(n["tick"] <= 80 for n in a.news()))
        self.assertEqual(len(a.news()), 3)

    def test_delta_fine_and_expiry_settlement(self):
        market = MockMarket(7)
        market.fill("RTM", 8000, "BUY")
        market.advance(1)
        self.assertAlmostEqual(market.fines, 100)
        for tick in range(2, 301):
            market.advance(tick)
        self.assertEqual(market.snapshot()["positions"], {})
        self.assertEqual(market.snapshot()["unrealized"], 0)
        self.assertAlmostEqual(market.snapshot()["pnl"], market.cash)
        with self.assertRaises(ValueError):
            market.fill("RTM", 1, "BUY")

    def test_client_reuses_execution_without_network(self):
        market = MockMarket(7)
        client = MockClient(market, lambda *args, **kwargs: None)
        case = client.get("case")
        client.bind_heat(case)
        client.bind_snapshot(case)
        with patch("ritcx.api.urlopen", side_effect=AssertionError("Network forbidden")):
            sec = client.get("securities")[0]
            result = client.order(sec, 100, "BUY")
        self.assertEqual(result[0]["position"], 100)

    def test_invalid_order_does_not_change_account(self):
        market = MockMarket(7)
        for args in [("RTM", -1, "BUY"), ("RTM", 1, "BAD"), ("RTM50C", 101, "BUY")]:
            with self.assertRaises(ValueError):
                market.fill(*args)
        self.assertEqual(market.snapshot()["pnl"], 0)

    def test_pnl_identity_after_partial_close_and_reversal(self):
        market = MockMarket(7)
        market.fill("RTM", 100, "BUY")
        market.advance(1)
        market.fill("RTM", 50, "SELL")
        market.advance(2)
        market.fill("RTM", 100, "SELL")
        market.fill("RTM50P", 10, "SELL")
        market.advance(3)
        row = market.snapshot()
        self.assertAlmostEqual(row["realized_net"] + row["unrealized"], row["pnl"])
        market.fill("RTM", 50, "BUY")
        market.fill("RTM50P", 10, "BUY")
        row = market.snapshot()
        self.assertAlmostEqual(row["pnl"], row["realized_net"])
        self.assertEqual(row["unrealized"], 0)

    def test_published_risk_limits_reject_order_without_charging(self):
        market = MockMarket(7)
        for _ in range(5):
            market.fill("RTM", 10000, "BUY")
        before = market.snapshot()
        with self.assertRaises(ValueError):
            market.fill("RTM", 1, "BUY")
        self.assertEqual(before, market.snapshot())


class MockCLITests(unittest.TestCase):
    def test_interrupt_saves_partial_run_and_returns_130(self):
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "ritcx.mock",
                    "--seed",
                    "7",
                    "--output-dir",
                    temp,
                    "--plain",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=Path(__file__).resolve().parents[1],
            )
            try:
                self.assertIn("0/300", process.stdout.readline())
                process.send_signal(signal.SIGINT)
                out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, out + err)
                report = json.loads(next(Path(temp).glob("*.json")).read_text())
                self.assertEqual(report["status"], "interrupted")
                self.assertLess(report["tick"], 300)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_full_heat_ignores_live_environment_and_saves_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, RIT_API_URL="http://invalid.invalid:9999/v1")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ritcx.mock",
                    "--speed",
                    "1000",
                    "--seed",
                    "7",
                    "--output-dir",
                    temp,
                    "--plain",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("LOCAL SIMULATION", result.stdout)
            self.assertIn("300/300", result.stdout)
            report = json.loads(next(Path(temp).glob("*.json")).read_text())
            self.assertEqual(report["tick"], 300)
            self.assertEqual(report["scenario"], "case")
            self.assertEqual(report["policy"], "buffered")
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["positions"], {})
            self.assertGreater(report["orders"], 0)
            self.assertGreater(report["fees"], 0)
            self.assertAlmostEqual(report["pnl"], report["realized_net"])
            self.assertEqual(report["settled_positions"], {})


class MockStressTests(unittest.TestCase):
    def test_forecasts_can_miss_actual_volatility_and_stay_private_until_release(self):
        from ritcx.volatility.model import VolatilityCurve

        misses = 0
        for seed in range(20):
            market = MockMarket(seed, scenario="standard")
            self.assertEqual(len(market.news()), 1)
            market.advance_time(60)
            curve = VolatilityCurve.from_news(market.news(), 60)
            low, high = curve.weeks[75]
            misses += not low <= market.sigmas[1] <= high
        self.assertGreater(misses, 0)
        self.assertLess(misses, 20)

    def test_orders_fill_after_delay_at_arrival_quote(self):
        market = MockMarket(7, scenario="stress")
        client = MockClient(market, lambda *args, **kwargs: None)
        client.bind_heat(client.get("case"))
        client.bind_snapshot(client.get("case"))
        market.advance_time(0.99 - market.clock)
        quoted_before = market.securities[0]["ask"]
        before = market.clock
        receipt = client.request(
            "POST", "orders", dict(type="MARKET", ticker="RTM", quantity=10, action="BUY")
        )
        self.assertGreater(market.clock, before)
        self.assertAlmostEqual(receipt["price"], market.securities[0]["ask"])
        self.assertNotEqual(receipt["price"], quoted_before)
        self.assertGreater(receipt["simulated_delay_seconds"], 0)

    def test_market_path_does_not_depend_on_request_count(self):
        a = MockMarket(7, scenario="standard")
        b = MockMarket(7, scenario="standard")
        client = MockClient(a, lambda *args, **kwargs: None)
        for _ in range(20):
            client.get("trader")
        a.advance_time(80 - a.clock)
        b.advance_time(80)
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_unannounced_future_truth_does_not_leak_into_current_quotes(self):
        a = MockMarket(7, scenario="standard")
        b = MockMarket(7, scenario="standard")
        b.sigmas[1] = 0.7
        b.forecasts[1] = 0.7
        a.advance_time(59)
        b.advance_time(59)
        self.assertEqual(a.snapshot(), b.snapshot())

    def test_transport_cannot_fill_after_heat_ends(self):
        from ritcx.runtime import TradingError

        market = MockMarket(7, scenario="stress")
        client = MockClient(market, lambda *args, **kwargs: None)
        client.bind_heat(client.get("case"))
        market.advance_time(299.99 - market.clock)
        with self.assertRaises(TradingError):
            client.request(
                "POST", "orders", dict(type="MARKET", ticker="RTM", quantity=10, action="BUY")
            )
        self.assertEqual(market.orders, 0)

    def test_engine_retains_failed_run_and_settles_remaining_inventory(self):
        from ritcx.mock_engine import run_heat
        from ritcx.runtime import TradingError

        original = MockClient.request

        def fail_after_first_fill(client, method, endpoint, params=None):
            if method == "POST" and client.market.orders:
                raise TradingError("Injected known request rejection")
            return original(client, method, endpoint, params)

        with patch.object(MockClient, "request", fail_after_first_fill):
            report = run_heat(7, "demo")
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["tick"], 300)
        self.assertTrue(report["positions_at_error"])
        self.assertTrue(report["settled_positions"])

    def test_demo_replays_previous_result_and_half_size_limits_inventory(self):
        from ritcx.mock_engine import run_heat

        baseline = run_heat(7, "demo")
        smaller = run_heat(7, "demo", policy_name="half_size")
        self.assertAlmostEqual(baseline["pnl"], 4218.161749434879)
        self.assertLessEqual(smaller["max_gross_options"], 50)
        self.assertGreater(smaller["orders"], 0)


if __name__ == "__main__":
    unittest.main()
