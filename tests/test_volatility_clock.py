"""Advancing-clock regression: latency consumes real heat time during close-out."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_strategy import option, stock

from ritcx.volatility.bot import main
from ritcx.volatility.model import black_scholes


class VolatilityClockTests(unittest.TestCase):
    def test_full_inventory_is_liquidated_with_100ms_per_request(self):
        now = 200.0
        sec = [stock()] + [option(f"RTM{k}{cp}") for k in range(48, 53) for cp in "CP"]
        max_contracts = 0
        posts = 0

        def sleep(delay):
            nonlocal now
            now += delay

        def transport(request, timeout=3):
            nonlocal now, posts, max_contracts
            now += 0.1
            path = urlsplit(request.full_url)
            ep = path.path.removeprefix("/v1/")
            if request.method == "POST":
                q = parse_qs(path.query)
                quantity = int(q["quantity"][0])
                side = 1 if q["action"][0] == "BUY" else -1
                target = next(s for s in sec if s["ticker"] == q["ticker"][0])
                target["position"] += side * quantity
                max_contracts = max(
                    max_contracts, sum(abs(s["position"]) for s in sec if s["type"] == "OPTION")
                )
                posts += 1
                result = dict(order_id=posts, quantity_filled=quantity, status="TRANSACTED")
            elif ep == "case":
                result = dict(
                    name="RITCx Volatility Trading Case",
                    period=1,
                    tick=min(300, int(now)),
                    status="ACTIVE" if now < 300 else "STOPPED",
                    ticks_per_period=300,
                    total_periods=1,
                )
            elif ep == "securities":
                for s in sec[1:]:
                    fair = black_scholes(
                        50,
                        int(s["ticker"][3:5]),
                        max(0, 300 - int(now)) / 3600,
                        0,
                        0.2,
                        s["ticker"][-1],
                    )[0]
                    s["bid"] = 2 if s["ticker"] == "RTM50C" else max(0, fair - 0.01)
                    s["ask"] = s["bid"] + 0.02
                result = sec
            elif ep == "news":
                result = [dict(tick=1, body="Risk free rate is 0%. Realized volatility is 20%.")]
            elif ep == "limits":
                result = [
                    dict(name="Limit-ETF", gross_limit=50000, net_limit=50000),
                    dict(name="Limit-OPT", gross_limit=2500, net_limit=1000),
                ]
            elif ep == "trader":
                result = dict(nlv=0, total_fines=0)
            elif ep == "orders":
                result = []
            else:
                raise AssertionError(ep)
            return io.BytesIO(json.dumps(result).encode())

        with tempfile.TemporaryDirectory() as temp:
            cfg = Path(temp) / "test.env"
            cfg.write_text(
                f"RIT_ENVIRONMENT=contest\nRIT_API_URL=http://localhost:9999/v1\nRIT_USERNAME=virtual-clock-test\nRIT_PASSWORD=local\nRIT_LOG_DIR={temp}/logs\n"
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    ["volatility", "--config", str(cfg), "--contest", "--trade", "--watch"],
                ),
                patch("ritcx.api.urlopen", side_effect=transport),
                patch("time.monotonic", side_effect=lambda: now),
                patch("time.sleep", side_effect=sleep),
                patch("signal.signal"),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                result = main()
        self.assertEqual(max_contracts, 100)
        self.assertEqual(result, 0, "\n".join(output.getvalue().splitlines()[-6:]))
        self.assertLess(now, 300)
        self.assertTrue(all(s["position"] == 0 for s in sec))


if __name__ == "__main__":
    unittest.main()
