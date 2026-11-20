"""Replay the current volatility trader on the eight markets in the supplied bundle.

Run from the checkout, e.g. python3 tools/replay_volatility.py --bundle ~/Downloads/ritcx-vol-algo
"""

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, nullcontext, redirect_stdout
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ritcx.api import DMAClient  # noqa: E402
from ritcx.volatility import bot, buffered, model  # noqa: E402


def load_market(bundle):
    sys.path.insert(0, str(bundle))
    import backtest

    return backtest


class ReplayClient(DMAClient):
    """Expose the supplied market state and adapt its limit names and fill receipts."""

    def __init__(self, market, *, execute, audit):
        super().__init__("offline", "replay", "", execute=execute, audit=audit)
        self.market = market

    def request(self, method, endpoint, params=None):
        market = self.market
        if method == "GET":
            if endpoint == "case":
                return market.case()
            if endpoint == "trader":
                return market.trader()
            if endpoint == "securities":
                return market.securities()
            if endpoint == "news":
                return market.news()
            if endpoint == "limits":
                names = {"LIMIT-STOCK": "Limit-ETF", "LIMIT-OPTIONS": "Limit-OPT"}
                return [dict(item, name=names[item["name"]]) for item in market.limits()]
            if endpoint == "orders?status=OPEN":
                return []  # Sim.order is synchronous and leaves no resting orders.
        if (
            method == "POST"
            and endpoint == "orders"
            and self.execute
            and params["type"] == "MARKET"
        ):
            self.check_heat()
            filled = market.order(params["ticker"], params["quantity"], params["action"])
            return dict(
                order_id=market.n_orders + market.rejected,
                quantity_filled=filled,
                status="TRANSACTED" if filled else "REJECTED",
                **params,
            )
        raise ValueError(f"Unsupported replay request {method} {endpoint}")


def run_heat(job):
    scenario, seed = job
    from backtest import SCENARIOS
    from sim import Sim

    market = Sim(seed, **SCENARIOS[scenario])
    market.advance()  # The supplied backtest makes its first decision at tick 1.
    trace = []
    show_trace = scenario == "spike15" and seed == 7
    stop_reason = None
    flat_tick = None

    def observe():
        if show_trace:
            curve = model.VolatilityCurve.from_news(market.news(), market.tick)
            trace.append(
                dict(
                    tick=market.tick,
                    quoted_vol=market.mm_iv,
                    weekly_vol=market.vols[market.week() - 1],
                    forecast_low=curve.effective(market.tick, "low"),
                    forecast_high=curve.effective(market.tick, "high"),
                    net_options=sum(q for k, q in market.pos.items() if k != "RTM"),
                    pnl=market.nlv(),
                )
            )

    def advance(_seconds):
        # Match final_run.py: one decision per tick, zero transport time within a tick.
        observe()
        market.advance()

    def audit_line(line):
        nonlocal stop_reason, flat_tick
        if '"event": "stopped"' in line:
            stop_reason = json.loads(line)["reason"]
        if '"event": "finished_flat"' in line:
            flat_tick = market.tick

    clock = SimpleNamespace(monotonic=lambda: float(market.tick), sleep=advance)
    log = SimpleNamespace(write=audit_line, flush=lambda: None)
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        config = dict(
            RIT_ENVIRONMENT="practice",
            RIT_API_KIND="rest",
            RIT_API_URL="http://localhost:1/v1",
            RIT_USERNAME="offline",
            RIT_API_KEY="offline",
            RIT_LOG_DIR=tmp,
        )
        stack.enter_context(patch.object(bot, "time", clock))
        stack.enter_context(patch.object(bot, "read_config", lambda _: config))
        stack.enter_context(patch.object(bot, "account_lock", lambda *a: nullcontext()))
        stack.enter_context(
            patch.object(bot, "open_audit_log", lambda *a: nullcontext(("replay", log)))
        )
        stack.enter_context(
            patch.object(bot, "client_from_config", lambda cfg, **kw: ReplayClient(market, **kw))
        )
        stack.enter_context(
            patch.object(sys, "argv", ["volatility", "--trade", "--watch", "--practice"])
        )
        sink = stack.enter_context(open(os.devnull, "w"))
        with redirect_stdout(sink):
            code = bot.main()
    while market.status == "ACTIVE":
        advance(1)
    observe()
    if code and stop_reason is None:
        raise RuntimeError("Trader returned an unexplained failure")
    return dict(
        scenario=scenario,
        seed=seed,
        pnl=market.nlv(),
        fees=market.fees,
        fines=market.fines,
        orders=market.n_orders,
        rejected=market.rejected,
        flat_tick=flat_tick,
        stop_reason=stop_reason,
        trace=trace,
    )


def init_worker(bundle):
    load_market(bundle)
    # Repeated calls within a tick have identical scalar inputs. Memoize only pure
    # pricing functions; use the same implementation and full precision on cache misses.
    model.implied_volatility = lru_cache(maxsize=256)(model.implied_volatility)
    buffered.implied_volatility = model.implied_volatility
    model.black_scholes = lru_cache(maxsize=2048)(model.black_scholes)


def summarize(rows):
    pnl = [r["pnl"] for r in rows]
    return dict(
        count=len(rows),
        mean=statistics.mean(pnl),
        median=statistics.median(pnl),
        total=math.fsum(pnl),
        worst=min(pnl),
        best=max(pnl),
        losses=sum(v < 0 for v in pnl),
        fees_mean=statistics.mean(r["fees"] for r in rows),
        fines_mean=statistics.mean(r["fines"] for r in rows),
        flat_before_expiry=sum(r["flat_tick"] is not None for r in rows),
        stopped=sum(r["stop_reason"] is not None for r in rows),
        rejected_orders=sum(r["rejected"] for r in rows),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=300)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/volatility-buffered-replay/results.json"
    )
    args = parser.parse_args()
    if args.seeds < 1 or args.workers < 1:
        parser.error("seeds and workers must be positive")
    bundle = args.bundle.resolve()
    backtest = load_market(bundle)
    scenarios = backtest.DEFAULT_SCENARIOS
    paths = [
        Path(__file__),
        *(
            ROOT / "ritcx" / f
            for f in (
                "api.py",
                "execution.py",
                "runtime.py",
                "volatility/bot.py",
                "volatility/model.py",
                "volatility/buffered.py",
            )
        ),
    ]
    inputs = {str(p.relative_to(ROOT)): p for p in paths}
    inputs.update({f"bundle/{f}": bundle / f for f in ("backtest.py", "vol_algo.py", "sim.py")})
    hashes = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in inputs.items()}
    result = dict(
        policy=asdict(bot.DEFAULT_POLICY),
        source_sha256=hashes,
        seeds=[1, args.seeds],
        clock="one decision per tick, no simulated request latency",
        scenarios=backtest.SCENARIOS,
        summaries={},
        runs=[],
        trace=[],
    )
    with ProcessPoolExecutor(args.workers, initializer=init_worker, initargs=(bundle,)) as pool:
        for scenario in scenarios:
            jobs = ((scenario, seed) for seed in range(1, args.seeds + 1))
            rows = list(pool.map(run_heat, jobs, chunksize=3))
            for row in rows:
                trace = row.pop("trace")
                if trace:
                    result["trace"] = trace
            result["summaries"][scenario] = summarize(rows)
            result["runs"].extend(rows)
            print(scenario, result["summaries"][scenario], flush=True)
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != hashes[k] for k, p in inputs.items()):
        raise RuntimeError("Replay input changed during evaluation")
    result["overall"] = summarize(result["runs"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, allow_nan=False, indent=2) + "\n")
    print("All scenarios", result["overall"])


if __name__ == "__main__":
    main()
