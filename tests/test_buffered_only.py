"""Only the selected buffered volatility strategy is runnable."""

import subprocess
import sys
import unittest
from dataclasses import asdict

from ritcx.mock_engine import POLICIES
from ritcx.volatility.bot import DEFAULT_POLICY, LIVE_POLICIES


class BufferedOnlyTests(unittest.TestCase):
    def test_only_buffered_is_registered_and_is_the_default(self):
        self.assertEqual(set(LIVE_POLICIES), {"buffered"})
        self.assertEqual(set(POLICIES), {"buffered"})
        self.assertEqual(DEFAULT_POLICY, LIVE_POLICIES["buffered"])
        self.assertEqual(
            asdict(DEFAULT_POLICY),
            dict(
                minimum_edge=0.04,
                hedge_band=1200,
                entry_size=100,
                option_cap=1000,
                algorithm="forecast_adaptive",
                net_cap=800,
                stock_cap=45000,
                stop_loss=6000,
                idle_seconds=0.1,
                entry_risk_fraction=0.75,
            ),
        )

    def test_cli_help_exposes_only_buffered(self):
        for module in ("ritcx", "ritcx.volatility.bot", "ritcx.mock", "ritcx.mock_evaluate"):
            result = subprocess.run(
                [sys.executable, "-m", module, "--help"], capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("{buffered}", result.stdout)
            for removed in ("straddle", "baseline", "scaled", "adaptive_small", "half_size"):
                self.assertNotIn(removed, result.stdout)


if __name__ == "__main__":
    unittest.main()
