"""End-to-end launcher tests against a local simulator, never the practice server."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_etf import securities


class RunnerCLITests(unittest.TestCase):
    def test_runner_passes_profile_and_account_lock_to_real_child_and_finishes_flat(self):
        started = time.monotonic()
        posts = []
        sec = securities()
        prices = {"CAD": 1, "USD": 1, "BULL": 10, "BEAR": 15, "RITC": 25}
        for s in sec:
            s.update(bid=prices[s["ticker"]] - 0.01, ask=prices[s["ticker"]] + 0.01)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, value):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                path = urlsplit(self.path)
                ep = path.path.removeprefix("/v1/")
                if ep == "case":
                    return self.answer(
                        dict(
                            name="RITCx - Algorithmic ETF Arbitrage Case",
                            period=1,
                            tick=10 if time.monotonic() - started < 1.5 else 275,
                            status="ACTIVE",
                            ticks_per_period=300,
                            total_periods=1,
                        )
                    )
                if ep == "securities":
                    return self.answer(sec)
                if ep == "trader":
                    return self.answer(dict(nlv=0, total_fines=0))
                if ep == "limits":
                    return self.answer(
                        [
                            dict(name="LIMIT-STOCK", gross_limit=300000, net_limit=200000),
                            dict(name="LIMIT-CASH", gross_limit=10000000, net_limit=10000000),
                        ]
                    )
                if ep == "securities/book":
                    ticker = parse_qs(path.query)["ticker"][0]
                    price = prices[ticker]
                    return self.answer(
                        dict(
                            bids=[
                                dict(
                                    price=price - 0.01,
                                    quantity=1000000,
                                    quantity_filled=0,
                                    trader_id="ANON",
                                )
                            ],
                            asks=[
                                dict(
                                    price=price + 0.01,
                                    quantity=1000000,
                                    quantity_filled=0,
                                    trader_id="ANON",
                                )
                            ],
                        )
                    )
                if ep in ("orders", "tenders"):
                    return self.answer([])
                self.send_error(404)

            def do_POST(self):
                posts.append(self.path)
                self.send_error(500)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "contest.env"
                config.write_text(
                    f"RIT_ENVIRONMENT=contest\nRIT_API_URL=http://127.0.0.1:{server.server_port}/v1\nRIT_USERNAME=local-integration-test\nRIT_PASSWORD=local-test\nRIT_LOG_DIR={directory}/logs\n"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ritcx",
                        "--config",
                        str(config),
                        "--contest",
                        "--trade",
                        "--rounds",
                        "1",
                        "--max-seconds",
                        "5",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=Path(__file__).resolve().parents[1],
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("STARTING", result.stdout)
            self.assertIn("finished_flat", result.stdout)
            self.assertIn("HEAT_FINISHED", result.stdout)
            self.assertIn('"completed": 1', result.stdout)
            self.assertEqual(posts, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
