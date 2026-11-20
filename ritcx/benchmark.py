"""Reproducible local CPU benchmark; never opens a network connection."""

import argparse
import hashlib
import json
import math
import platform
import statistics
import time
from pathlib import Path

from .volatility.bot import DEFAULT_POLICY
from .volatility.buffered import admissible_quantity
from .volatility.model import VolatilityCurve, black_scholes, candidates, hedge_quantity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("Positive iteration count required")
    securities = [
        dict(ticker="RTM", type="STOCK", position=0, size=1, bid=49.99, ask=50.01, trading_fee=0.02)
    ]
    for strike in range(48, 53):
        for kind in "CP":
            price, _ = black_scholes(50, strike, 200 / 3600, 0, 0.3, kind)
            securities.append(
                dict(
                    ticker=f"RTM{strike}{kind}",
                    type="OPTION",
                    position=5 if kind == "C" else -5,
                    size=100,
                    bid=max(0, price - 0.01),
                    ask=price + 0.01,
                    trading_fee=2,
                )
            )
    curve = VolatilityCurve(0, {0: (0.2, 0.2), 150: (0.25, 0.3)})
    timings = []
    for _ in range(args.iterations):
        start = time.perf_counter()
        candidates(securities, curve, 100)
        hedge_quantity(securities, 100, 0.2, 0)
        admissible_quantity(securities, "RTM50C", 100, curve, 100, DEFAULT_POLICY)
        timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    source = Path(__file__).with_name("volatility") / "model.py"
    print(
        json.dumps(
            dict(
                iterations=args.iterations,
                python=platform.python_version(),
                platform=platform.platform(),
                workload="ten-option scenario valuation, quoted-IV hedge and one proposed risk check; no network",
                median_ms=statistics.median(timings),
                p95_ms=timings[math.ceil(0.95 * len(timings)) - 1],
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
