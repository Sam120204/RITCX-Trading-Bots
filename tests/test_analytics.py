import json
import tempfile
import unittest
from pathlib import Path

from ritcx import analytics


class AnalyticsTests(unittest.TestCase):
    def test_complete_heat_metrics_use_nlv_change_and_do_not_hide_unresolved_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, rows in {
                "good.jsonl": [
                    dict(event="run_started", execution_enabled=True),
                    dict(event="snapshot", tick=1, trader=dict(nlv=100, total_fines=0)),
                    dict(event="order_response", latency_ms=40),
                    dict(
                        event="final_reconciliation",
                        trader=dict(nlv=300, total_fines=5),
                        positions={},
                        open_orders=[],
                    ),
                ],
                "loss.jsonl": [
                    dict(event="snapshot", tick=1, trader=dict(nlv=0, total_fines=0)),
                    dict(event="request_rejected", http_status=429),
                    dict(
                        event="final_reconciliation",
                        trader=dict(nlv=-100, total_fines=0),
                        positions={},
                        open_orders=[],
                    ),
                ],
                "broken.jsonl": [
                    dict(event="snapshot", tick=1, trader=dict(nlv=0, total_fines=0)),
                    dict(
                        event="reconciliation",
                        trader=dict(nlv=-500, total_fines=20),
                        positions={"RITC": 100},
                        open_orders=[],
                    ),
                ],
                "read-only.jsonl": [
                    dict(event="run_started", execution_enabled=False),
                    dict(event="snapshot", tick=1, trader=dict(nlv=99999, total_fines=0)),
                ],
            }.items():
                (root / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
            report = analytics.summarize(list(root.glob("*.jsonl")))
        self.assertEqual(report["completed_heats"], 2)
        self.assertEqual(report["median_heat_pnl"], 50)
        self.assertEqual(report["worst_observed_pnl"], -500)
        self.assertEqual(report["total_fines"], 25)
        self.assertEqual(report["unresolved_runs"], 1)
        self.assertEqual(report["rejected_requests"], 1)
        self.assertEqual(report["order_latency_ms"]["median"], 40)

    def test_truncated_log_is_reported_as_incomplete_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "truncated.jsonl"
            p.write_text('{"event":"snapshot","trader":{"nlv":0,"total_fines":0}}\n{"event":')
            report = analytics.summarize([p])
        self.assertEqual(report["completed_heats"], 0)
        self.assertEqual(report["unresolved_runs"], 1)
        self.assertEqual(report["malformed_lines"], 1)


if __name__ == "__main__":
    unittest.main()
