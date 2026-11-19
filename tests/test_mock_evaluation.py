"""Evaluation must retain losing and failed heats and separate validation seeds."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class MockEvaluationTests(unittest.TestCase):
    def test_summary_includes_failed_losses(self):
        from ritcx.mock_evaluate import summarize

        rows = [
            dict(
                pnl=-100,
                status="error",
                max_drawdown=120,
                fees=20,
                fines=5,
                settled_positions={"RTM": 1},
                strategy_finished_flat=False,
            ),
            dict(
                pnl=50,
                status="completed",
                max_drawdown=30,
                fees=10,
                fines=0,
                settled_positions={},
                strategy_finished_flat=True,
            ),
        ]
        result = summarize(rows)
        self.assertEqual(result["mean_pnl"], -25)
        self.assertEqual(result["loss_rate"], 0.5)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["settlement_runs"], 1)
        self.assertEqual(result["fines"], 5)

    def test_cli_reports_disjoint_paired_seeds_and_all_policies(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "evaluation.json"
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ritcx.mock_evaluate",
                    "--seeds",
                    "1",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["heat_count"], 16)
            groups = result["groups"]
            self.assertEqual(len(groups), 16)
            self.assertEqual(
                {g["runs"][0]["seed"] for g in groups if g["split"] == "exploratory"}, {0}
            )
            self.assertEqual(
                {g["runs"][0]["seed"] for g in groups if g["split"] == "held_out"}, {10000}
            )
            self.assertTrue(all("mean_change_vs_baseline" in g["summary"] for g in groups))


if __name__ == "__main__":
    unittest.main()
