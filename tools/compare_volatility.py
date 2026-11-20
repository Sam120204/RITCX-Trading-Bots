"""Pair the supplied vol_algo.py live loop and the selected policy on local RITCx markets."""

import argparse
import hashlib
import io
import json
import math
import statistics
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ritcx.mock_engine import run_heat  # noqa: E402
from ritcx.mock_market import MockClient, MockMarket  # noqa: E402

SCENARIOS = ("case", "case_fast", "case_latency", "standard", "stress")
SEEDS = [*range(101000, 101050), *range(111000, 111050)]


def straddle_heat(source, seed, scenario):
    market = MockMarket(seed, scenario)
    local = MockClient(market, lambda *args, **kwargs: None)
    rejected = []
    received_stop = False

    class Rest(source.RitClient):
        # Retain every public client method, including how order receipts are interpreted.
        def __init__(self):
            pass

        def _request(self, method, path, params=None, tries=4):
            nonlocal received_stop
            if method == "GET":
                response = local.get(path.removeprefix("/"))
                if path == "/case":
                    received_stop = response["status"] == "STOPPED"
                return response
            if (method, path, params["type"]) != ("POST", "/orders", "MARKET"):
                raise ValueError("Unexpected offline request")
            # A direct REST POST consumes one request and the configured fill delay.
            # MockClient's POST also makes a case read specific to the repository bot.
            s = market.scenario
            market.advance_time(local.transport_rng.uniform(s.request_min, s.request_max))
            market.advance_time(local.transport_rng.uniform(s.order_delay_min, s.order_delay_max))
            try:
                return market.fill(params["ticker"], params["quantity"], params["action"])
            except ValueError as exc:
                rejected.append(str(exc))
                return None  # The supplied client returns None for a rejected HTTP request.

    class HeatEnded(Exception):
        pass

    def sleep(seconds):
        if received_stop:
            raise HeatEnded
        market.advance_time(seconds)

    timer = SimpleNamespace(
        monotonic=lambda: market.clock,
        perf_counter=lambda: market.clock,
        sleep=sleep,
        strftime=time.strftime,
    )
    log = io.StringIO()
    with (
        tempfile.TemporaryDirectory() as folder,
        patch.object(source, "time", timer),
        patch.object(source, "_console", lambda *args: None),
        redirect_stdout(log),
    ):
        try:
            source.run_live(Rest(), source.Config(log_dir=folder))
        except HeatEnded:
            pass
    lines = log.getvalue().splitlines()
    assert received_stop and market.tick == 300
    assert math.isclose(market.cash, market.realized - market.fees - market.fines, abs_tol=0.0001)
    return dict(
        pnl=market.cash,
        fees=market.fees,
        fines=market.fines,
        orders=market.orders,
        rejected=len(rejected),
        step_errors=[s for s in lines if "step failed:" in s],
        news_messages=sum(" NEWS week " in s for s in lines),
        settled_positions=market.settled_positions,
    )


def init_worker(bundle):
    sys.path.insert(0, str(bundle))


def run(job):
    import vol_algo

    scenario, seed = job
    alternate = straddle_heat(vol_algo, seed, scenario)
    current = run_heat(seed, scenario, "buffered")
    assert current["tick"] == 300
    return dict(
        seed=seed,
        scenario=scenario,
        straddle=alternate,
        buffered={k: current[k] for k in ("pnl", "fees", "fines", "orders", "status", "error")},
    )


def summarize(pairs):
    out = {}
    for policy in ("straddle", "buffered"):
        p = [r[policy]["pnl"] for r in pairs]
        out[policy] = dict(
            mean=statistics.mean(p),
            median=statistics.median(p),
            worst=min(p),
            losses=sum(v < 0 for v in p),
        )
    diffs = [r["straddle"]["pnl"] - r["buffered"]["pnl"] for r in pairs]
    out.update(
        n=len(pairs),
        straddle_wins=sum(d > 0 for d in diffs),
        ties=sum(d == 0 for d in diffs),
        mean_advantage=statistics.mean(diffs),
    )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/volatility-buffered-replay/exact-comparison.json",
    )
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    files = {"bundle/vol_algo.py": bundle / "vol_algo.py"}
    for name in (
        "mock_market.py",
        "mock_engine.py",
        "api.py",
        "execution.py",
        "volatility/bot.py",
        "volatility/buffered.py",
        "volatility/model.py",
    ):
        files[f"ritcx/{name}"] = ROOT / "ritcx" / name
    files["tools/compare_volatility.py"] = Path(__file__)
    hashes = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in files.items()}
    rows, summaries = [], {}
    with ProcessPoolExecutor(args.workers, initializer=init_worker, initargs=(bundle,)) as pool:
        for scenario in SCENARIOS:
            pairs = list(pool.map(run, ((scenario, seed) for seed in SEEDS), chunksize=2))
            rows.extend(pairs)
            summaries[scenario] = summarize(pairs)
            print(scenario, summaries[scenario], flush=True)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == hashes[k] for k, p in files.items())
    args.output.write_text(
        json.dumps(
            dict(source_sha256=hashes, summaries=summaries, runs=rows), indent=2, allow_nan=False
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
