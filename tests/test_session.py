import tempfile
import unittest
from pathlib import Path

from test_etf import securities

from ritcx.runtime import TradingError, open_audit_log
from ritcx.session import RoundRouter, child_command, preflight


class SessionTests(unittest.TestCase):
    def test_routes_each_case_and_only_once_per_heat(self):
        router = RoundRouter()
        vol = dict(name="RITCx Volatility Trading Case", period=1, tick=1, status="ACTIVE")
        self.assertEqual(router.claim(vol), "ritcx.volatility.bot")
        vol["tick"] = 20
        self.assertIsNone(router.claim(vol))
        etf = dict(name="RITCx - Algorithmic ETF Arbitrage Case", period=1, tick=1, status="ACTIVE")
        self.assertEqual(router.claim(etf), "ritcx.etf.bot")

    def test_same_case_reset_starts_a_new_heat(self):
        router = RoundRouter()
        case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case", period=1, tick=10, status="ACTIVE"
        )
        self.assertEqual(router.claim(case), "ritcx.etf.bot")
        case["tick"] = 285
        self.assertIsNone(router.claim(case))
        case["tick"] = 0
        case["status"] = "STOPPED"
        self.assertIsNone(router.claim(case))
        case["tick"] = 1
        case["status"] = "ACTIVE"
        self.assertEqual(router.claim(case), "ritcx.etf.bot")

    def test_child_final_tick_prevents_missing_reset_between_parent_polls(self):
        router = RoundRouter()
        case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case", period=1, tick=10, status="ACTIVE"
        )
        self.assertIsNotNone(router.claim(case))
        router.observe(dict(case, tick=275))
        self.assertIsNotNone(router.claim(dict(case, tick=20)))

    def test_stopped_to_active_at_tick_zero_can_start_new_heat(self):
        router = RoundRouter()
        case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case", period=1, tick=0, status="ACTIVE"
        )
        self.assertIsNotNone(router.claim(case))
        self.assertIsNone(router.claim(dict(case, status="STOPPED")))
        self.assertIsNotNone(router.claim(case))

    def test_unknown_stopped_and_late_cases_do_not_trade(self):
        for name, tick, status in [
            ("Other", 1, "ACTIVE"),
            ("RITCx Volatility Trading Case", 1, "STOPPED"),
            ("RITCx Volatility Trading Case", 280, "ACTIVE"),
        ]:
            self.assertIsNone(
                RoundRouter().claim(dict(name=name, period=1, tick=tick, status=status))
            )

    def test_audit_files_do_not_collide_when_started_together(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                open_audit_log(Path(directory), "test") as (p1, f1),
                open_audit_log(Path(directory), "test") as (p2, f2),
            ):
                f1.write("first")
                f2.write("second")
            self.assertNotEqual(p1, p2)
            self.assertEqual(p1.read_text(), "first")
            self.assertEqual(p2.read_text(), "second")

    def test_preflight_is_read_only_and_rejects_inventory_or_open_orders(self):
        case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case",
            period=1,
            tick=109,
            status="ACTIVE",
            ticks_per_period=300,
            total_periods=1,
        )
        sec = securities()
        for s in sec:
            s.update(
                max_trade_size=2500000 if s["ticker"] in ("USD", "CAD") else 10000,
                execution_delay_ms=0,
            )
        data = {
            "case": case,
            "securities": sec,
            "trader": {"nlv": 0, "total_fines": 0},
            "limits": [
                dict(name="LIMIT-STOCK", gross_limit=300000, net_limit=200000),
                dict(name="LIMIT-CASH", gross_limit=10000000, net_limit=10000000),
            ],
            "orders?status=OPEN": [],
        }

        class Client:
            def get(self, endpoint):
                return data[endpoint]

        self.assertTrue(preflight(Client())["entry_eligible"])
        sec[2]["position"] = 500
        with self.assertRaises(TradingError):
            preflight(Client())
        sec[2]["position"] = 0
        data["orders?status=OPEN"] = [{"order_id": 5}]
        with self.assertRaises(TradingError):
            preflight(Client())
        data["orders?status=OPEN"] = []
        case["tick"] = 267
        self.assertFalse(preflight(Client())["entry_eligible"])

    def test_child_receives_same_connection_profile_and_lock(self):
        cmd = child_command(
            Path("/project"), "ritcx.etf.bot", Path("/private/contest.env"), True, "contest", 17
        )
        self.assertIn("--contest", cmd)
        self.assertNotIn("--practice", cmd)
        self.assertEqual(cmd[cmd.index("--config") + 1], "/private/contest.env")
        self.assertEqual(cmd[cmd.index("--lock-fd") + 1], "17")


if __name__ == "__main__":
    unittest.main()
