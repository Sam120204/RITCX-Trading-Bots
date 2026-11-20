"""Case semantics and risk checks for the buffered volatility strategy."""

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ritcx.execution import submit_change
from ritcx.mock_market import SCENARIOS, MockClient, MockMarket
from ritcx.runtime import TradingError
from ritcx.volatility import bot
from ritcx.volatility.model import VolatilityCurve


class AnnouncementTests(unittest.TestCase):
    def test_forecast_is_never_promoted_to_confirmed_current_volatility(self):
        for start in (0, 75, 150):
            news = [
                dict(tick=start + 1, body="Risk free rate is 0%. Volatility this week is 20%."),
                dict(tick=start + 37, body="Volatility next week will be between 40% and 50%."),
            ]
            for tick in (start + 75, start + 76):
                self.assertAlmostEqual(VolatilityCurve.from_news(news, tick).current, 0.2)
            news.append(dict(tick=start + 77, body="Volatility this week is 32%."))
            self.assertAlmostEqual(VolatilityCurve.from_news(news, start + 77).current, 0.32)

    def test_forecast_at_last_tick_of_week_targets_the_following_week(self):
        curve = VolatilityCurve.from_news(
            [
                dict(tick=1, body="Risk free rate is 0%. Volatility is 20%."),
                dict(tick=75, body="Volatility next week will be between 30% and 40%."),
            ],
            75,
        )
        self.assertIn(75, curve.weeks)
        self.assertNotIn(150, curve.weeks)


class TicketTests(unittest.TestCase):
    def test_explicit_option_cap_reconciles_larger_order_without_changing_default(self):
        market = MockMarket(7)
        client = MockClient(market, lambda *a, **kw: None)
        client.bind_heat(client.get("case"))
        client.bind_snapshot(client.get("case"))
        sec = market.securities[1]
        with self.assertRaises(TradingError):
            client.order(sec, 100, "BUY")
        client.option_ticket_limit = 100
        result = submit_change(client, client.get("securities"), sec["ticker"], 100)
        self.assertEqual(result[1]["position"], 100)
        self.assertEqual(market.fills[-1]["quantity"], 100)
        client.option_ticket_limit = 1000
        with self.assertRaises(TradingError):
            client.order(sec, 101, "BUY")


class BufferedTests(unittest.TestCase):
    def setUp(self):
        self.policy = bot.DEFAULT_POLICY

    def fixture(self, tick=80):
        market = MockMarket(7)
        market.advance_time(tick)
        market.spot = 50
        market.quote()
        client = MockClient(market, lambda *a, **kw: None)
        client.option_ticket_limit = self.policy.entry_size
        client.bind_heat(client.get("case"))
        client.bind_snapshot(client.get("case"))
        return market, client, VolatilityCurve(0, {0: (0.4, 0.4)})

    def test_larger_entry_remains_inside_intermediate_delta_limit(self):
        market, client, curve = self.fixture()
        action = bot.trade_once(client, client.get("securities"), curve, 80, policy=self.policy)
        self.assertEqual(action, "entry")
        option_fills = [f for f in market.fills if f["ticker"] != "RTM"]
        self.assertGreater(option_fills[0]["quantity"], 10)
        self.assertLessEqual(abs(market.snapshot()["delta"]), 1500)

    def test_buffered_entries_leave_headroom_without_forcing_an_existing_close(self):
        from ritcx.volatility.buffered import admissible_quantity

        self.assertIn("buffered", bot.LIVE_POLICIES)
        market, _, curve = self.fixture(180)
        for s in market.securities:
            s["position"] = {"RTM50C": 40, "RTM50P": 40, "RTM": -700}.get(s["ticker"], 0)
        buffered = bot.LIVE_POLICIES["buffered"]
        original = replace(buffered, entry_risk_fraction=1.0)
        old_size = admissible_quantity(market.securities, "RTM50C", 100, curve, 180, original)
        new_size = admissible_quantity(market.securities, "RTM50C", 100, curve, 180, buffered)
        self.assertGreater(old_size, 0)
        self.assertLess(new_size, old_size)
        # Reserve applies to new exposure; the book may remain inside the
        # existing exit envelope rather than liquidating at the entry threshold.
        sec = deepcopy(market.securities)
        next(s for s in sec if s["ticker"] == "RTM50C")["position"] += old_size
        self.assertEqual(admissible_quantity(sec, "RTM50C", 0, curve, 180, buffered), 1)

    def test_small_residual_delta_does_not_trigger_fee_churning(self):
        market, client, curve = self.fixture(20)
        curve = VolatilityCurve(0, {0: (0.2, 0.2)})
        market.securities[0]["position"] = 400
        self.assertEqual(
            bot.trade_once(client, client.get("securities"), curve, 20, policy=self.policy), "hold"
        )
        self.assertEqual(market.orders, 0)

    def test_confirmed_partial_large_entry_hedges_only_filled_contracts(self):
        market, client, curve = self.fixture()
        original = market.fill

        def partial(ticker, quantity, action):
            if ticker == "RTM":
                return original(ticker, quantity, action)
            row = original(ticker, min(quantity, 30), action)
            return dict(row, status="CANCELLED")

        with patch.object(market, "fill", side_effect=partial):
            result = bot.trade_once(client, client.get("securities"), curve, 80, policy=self.policy)
        self.assertEqual(result, "partial_fill_reconciled")
        self.assertTrue(client.entry_halted)
        self.assertEqual(sum(abs(s["position"]) for s in market.securities[1:]), 30)
        self.assertLess(abs(market.snapshot()["delta"]), 1)

    def test_large_order_cannot_bypass_stale_snapshot_check(self):
        market, client, _ = self.fixture()
        sec = deepcopy(market.securities[1])
        market.advance_time(3)
        with self.assertRaises(TradingError):
            client.order(sec, 100, "BUY")
        self.assertEqual(market.orders, 0)

    def test_stock_only_closeout_budgets_every_10000_share_ticket(self):
        market, _, _ = self.fixture()
        market.securities[0]["position"] = 45000
        self.assertLessEqual(bot.liquidation_start(market.securities, 3, self.policy), 275)

    def test_stock_cap_after_market_move_still_allows_option_risk_reduction(self):
        market, client, curve = self.fixture(190)
        market.spot = 60
        market.quote()
        market.securities[0]["position"] = -45000
        market.securities[1]["position"] = 470
        result = bot.trade_once(client, client.get("securities"), curve, 190, policy=self.policy)
        self.assertEqual(result, "reduce_risk")
        self.assertEqual(market.securities[1]["position"], 430)
        self.assertLess(abs(market.snapshot()["delta"]), 1)

    def test_risk_reduction_prioritizes_delta_over_largest_otm_inventory(self):
        market, client, curve = self.fixture(295)
        for s in market.securities:
            s["position"] = {"RTM": -45000, "RTM48P": 450, "RTM48C": 400, "RTM52P": -150}.get(
                s["ticker"], 0
            )
        before = abs(market.snapshot()["delta"])
        result = bot.trade_once(client, client.get("securities"), curve, 295, policy=self.policy)
        self.assertEqual(result, "reduce_risk")
        self.assertEqual(
            next(s["position"] for s in market.securities if s["ticker"] == "RTM48P"), 450
        )
        self.assertLess(abs(market.snapshot()["delta"]), before - 3000)

    def test_two_tick_news_jump_reprices_before_larger_entry(self):
        market, client, _ = self.fixture(74)
        curve = VolatilityCurve(0, {0: (0.2, 0.2)})
        market.sigmas[1] = 0.4
        securities = client.get("securities")
        market.advance_time(2)
        result = bot.trade_once(client, securities, curve, 74, policy=self.policy)
        self.assertEqual(result, "entry")
        self.assertGreater(sum(s["position"] for s in market.securities[1:]), 10)

    def test_forecast_trades_information_before_current_sigma_changes(self):
        self.assertIn("buffered", bot.LIVE_POLICIES)
        market, client, _ = self.fixture(40)
        curve = VolatilityCurve.from_news(
            [
                dict(tick=1, body="Risk free rate is 0%. Volatility is 20%."),
                dict(tick=37, body="Volatility next week will be between 35% and 40%."),
            ],
            40,
        )
        action = bot.trade_once(
            client, client.get("securities"), curve, 40, policy=bot.DEFAULT_POLICY
        )
        self.assertEqual(action, "entry")
        self.assertGreater(sum(s["position"] for s in market.securities[1:]), 10)

    def test_gamma_and_required_stock_capacity_constrain_entries(self):
        from ritcx.volatility.buffered import admissible_quantity

        market, _, curve = self.fixture(290)
        sec = deepcopy(market.securities)
        for s in sec:
            if s["ticker"] in ("RTM50C", "RTM50P"):
                s["position"] = -400
        self.assertEqual(admissible_quantity(sec, "RTM50C", -100, curve, 290, self.policy, 3), 0)
        sec = deepcopy(market.securities)
        sec[0]["position"] = -44990
        for s in sec:
            if s["ticker"] == "RTM48C":
                s["position"] = 450
        self.assertEqual(admissible_quantity(sec, "RTM48C", 100, curve, 290, self.policy, 3), 0)

    def test_closeout_budgets_small_safe_legs_for_offsetting_itm_inventory(self):
        market, client, curve = self.fixture(190)
        market.spot = 60
        market.quote()
        market.securities[1]["position"] = 400
        market.securities[3]["position"] = -400
        close_tick = bot.liquidation_start(market.securities, 2, policy=self.policy)
        self.assertLessEqual(close_tick, 232)
        for _ in range(80):
            client.bind_snapshot(client.get("case"))
            action = bot.trade_once(
                client, client.get("securities"), curve, market.tick, True, self.policy
            )
            self.assertLess(abs(market.snapshot()["delta"]), 7000)
            if action == "flat":
                break
        self.assertEqual(action, "flat")
        self.assertFalse(market.snapshot()["positions"])


class PublishedCaseTests(unittest.TestCase):
    def test_first_return_of_new_week_uses_new_announced_sigma(self):
        market = MockMarket(7, "case")
        market.advance_time(75)
        market.sigmas[1] = 0.4
        before = market.spot
        with patch.object(market.rng, "gauss", return_value=1):
            market.advance(76)
        self.assertAlmostEqual(market.spot, before * (1 + 0.4 / 60))

    def test_midweek_news_and_week_boundaries(self):
        self.assertIn("case", SCENARIOS)
        market = MockMarket(7, "case")
        market.advance_time(36)
        self.assertEqual(len(market.news()), 1)
        market.advance_time(1)
        self.assertEqual(len(market.news()), 2)
        market.advance_time(38)
        self.assertEqual(market.sigma, 0.2)
        market.advance_time(1)
        self.assertEqual(market.sigma, market.sigmas[1])
        self.assertEqual(len(market.news()), 3)

    def test_announced_sigma_is_quote_target_and_forecasts_do_not_leak(self):
        self.assertIn("case", SCENARIOS)
        a, b = MockMarket(7, "case"), MockMarket(7, "case")
        b.sigmas[1] = 0.7
        b.forecasts[1] = 0.7
        a.advance_time(75)
        b.advance_time(75)
        self.assertEqual(a.snapshot(), b.snapshot())
        self.assertAlmostEqual(a.quoted_sigma, 0.2)

    def test_evaluation_uses_buffered_and_fresh_seed_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ritcx.mock_evaluate",
                    "--seeds",
                    "1",
                    "--scenarios",
                    "case",
                    "--policies",
                    "buffered",
                    "--exploratory-start",
                    "20000",
                    "--held-out-start",
                    "30000",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data["heat_count"], 2)
            self.assertEqual({r["seed"] for g in data["groups"] for r in g["runs"]}, {20000, 30000})
            self.assertTrue(
                all(
                    r["status"] == "completed" and r["strategy_finished_flat"]
                    for g in data["groups"]
                    for r in g["runs"]
                )
            )


if __name__ == "__main__":
    unittest.main()
