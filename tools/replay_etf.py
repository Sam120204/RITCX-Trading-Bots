"""Run a checkout's production ETF bot against the supplied recorded-quote exchange.

python3 tools/replay_etf.py --checkout . --bundle /path/to/ritcx_etf_algo --output run.json
Each invocation loads exactly one strategy. The transport is in-process and offline.
"""

import argparse
import importlib.util
import json
import os
import statistics
import sys
import tempfile
from contextlib import ExitStack, nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit


def run(record, bundle, bot, execution, folder, sink, flags, horizon):
    from ritcx.runtime import TradingError

    mock = bundle
    market = mock.Market(
        SimpleNamespace(
            seed=1,
            replay=None,
            ticks=300,
            gross_limit=300000,
            net_limit=200000,
            impact=0.000002,
            cycles_per_tick=0,
            orders_per_second=0,
            require_tender_price=False,
        )
    )
    market.rec = record
    market.set_quotes()
    market.build_books()
    clock = 0.0
    accepted = 0
    errors = []
    costs = []

    def advance(seconds):
        nonlocal clock
        clock += seconds
        while market.tick < min(300, int(clock)):
            # Identical exogenous depth on each heat/tick, independent of previous fills.
            market.rng.seed(f"{record['id']}/{market.tick + 1}")
            market.step()
            if market.status == "STOPPED":
                # The supplied exchange marks terminal NLV but leaves its ledger open.
                # Settle at exactly those marks so confirm_flat can inspect the result.
                market.cad = market.nlv()
                market.pos = dict.fromkeys(market.pos, 0.0)

    def response(status, body, headers=None):
        if status != 200:
            raise TradingError(str(body))
        return body

    handler = object.__new__(mock.Handler)
    handler.send = response

    class Client(bot.ETFClient):
        def request(self, method, endpoint, params=None):
            nonlocal accepted
            if method != "GET":
                if not self.execute:
                    raise AssertionError("unexpected read-only mutation")
                self.check_heat(require_fresh=method != "DELETE")
            advance(0.09 if method == "POST" else 0.04)
            url = urlsplit(endpoint)
            query = dict(parse_qsl(url.query), **(params or {}))
            data = handler.dispatch(method, url.path.split("/"), query, market)
            if endpoint == "case":
                data["name"] = "RITCx - Algorithmic ETF Arbitrage Case"
            elif endpoint == "securities":
                data.append(
                    dict(
                        ticker="CAD",
                        type="CURRENCY",
                        currency="",
                        trading_fee=0,
                        max_trade_size=2500000,
                        position=market.cad,
                        limits=[],
                    )
                )
                for sec in data:
                    sec["size"] = 1
            elif endpoint == "limits":
                data.append(dict(name="LIMIT-CASH", gross_limit=10000000, net_limit=10000000))
            elif method == "POST" and endpoint.startswith("tenders/"):
                accepted += 1
            return data

    def audit_line(line):
        for row in line.splitlines():
            if not row:
                continue
            event = json.loads(row)
            if event["event"] == "stopped":
                errors.append(event["reason"])
            if event["event"] == "heat_summary":
                costs.append({k: v for k, v in event.items() if k not in ("time", "event")})

    outfile = SimpleNamespace(write=audit_line, flush=lambda: None)
    cfg = dict(RIT_USERNAME="REPLAY", RIT_LOG_DIR=folder)
    timer = SimpleNamespace(monotonic=lambda: clock, perf_counter=lambda: clock, sleep=advance)
    with ExitStack() as stack:
        for obj, name, value in [
            (bot, "read_config", lambda _: cfg),
            (bot, "validate_profile", lambda *a: None),
            (
                bot,
                "client_from_config",
                lambda cfg, execute, audit, client_type: Client(
                    "offline://", "REPLAY", "", execute=execute, audit=audit
                ),
            ),
            (bot, "open_audit_log", lambda *a: nullcontext(("offline", outfile))),
            (bot, "account_lock", lambda *a: nullcontext()),
            (
                bot,
                "read_many",
                lambda client, pool, endpoints: {e: client.get(e) for e in endpoints},
            ),
            (bot, "time", timer),
            (execution, "time", timer),
            (sys, "argv", ["etf", "--practice", "--trade", "--watch", *flags]),
        ]:
            stack.enter_context(patch.object(obj, name, value))
        if horizon:
            from functools import partial

            stack.enter_context(
                patch.object(bot, "Glide", partial(bot.Glide, horizon=horizon[0], gamma=horizon[1]))
            )
        with redirect_stdout(sink):
            code = bot.main()
            advance(300)
    return dict(
        heat=record["id"],
        pnl_cad=round(market.nlv(), 2),
        accepted=accepted,
        exit_code=code,
        errors=errors,
        summary=costs[-1] if costs else None,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--heats", type=int)
    parser.add_argument(
        "--horizon", type=int, choices=(20, 30), help="Research override for the glide"
    )
    parser.add_argument("--hold", action="store_true", help="Evaluate selective settlement")
    parser.add_argument("--loss-limit-cad", type=float)
    args = parser.parse_args()
    sys.path.insert(0, str(args.checkout.resolve()))
    from ritcx import execution
    from ritcx.etf import bot

    spec = importlib.util.spec_from_file_location(
        "replay_market", args.bundle / "mock_rit_server.py"
    )
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    heats = json.loads((args.bundle / "replay_heats.json").read_text())["heats"]
    heats = [heat for heat in heats if heat["tenders"]][: args.heats]
    with tempfile.TemporaryDirectory() as temp, open(os.devnull, "w") as sink:
        flags = ["--hold-to-close"] if args.hold else []
        if args.loss_limit_cad:
            flags.extend(["--loss-limit-cad", str(args.loss_limit_cad)])
        rows = [
            run(
                heat,
                mock,
                bot,
                execution,
                temp,
                sink,
                flags,
                (args.horizon, {20: 0.5, 30: 1}[args.horizon]) if args.horizon else None,
            )
            for heat in heats
        ]
    pnl = sorted(row["pnl_cad"] for row in rows)
    summary = dict(
        heats=len(rows),
        errors=sum(bool(r["exit_code"]) for r in rows),
        accepted=sum(r["accepted"] for r in rows),
        mean=statistics.mean(pnl),
        median=statistics.median(pnl),
        worst=pnl[0],
        losses=sum(p < 0 for p in pnl),
    )
    args.output.write_text(json.dumps(dict(summary=summary, runs=rows), indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
