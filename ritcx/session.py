"""Preflight and automatic heat selection for the RITCx simulated contest."""

import argparse
import json
import signal
import statistics
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .api import AuthenticationError, client_from_config
from .runtime import ROOT, TradingError, account_lock, open_audit_log, read_config, validate_profile

CASES = {
    "RITCx Volatility Trading Case": "volatility",
    "RITCx - Algorithmic ETF Arbitrage Case": "etf",
}


class RoundRouter:
    def __init__(self):
        self.previous = None
        self.epoch = 0
        self.claimed = set()

    def observe(self, case):
        signature = (case["name"], case["period"])
        if self.previous and (
            signature != self.previous[:2]
            or case["tick"] < self.previous[2]
            or (self.previous[3] == "ACTIVE" and case["status"] != "ACTIVE")
        ):
            self.epoch += 1
        self.previous = (*signature, case["tick"], case["status"])

    def claim(self, case):
        self.observe(case)
        key = (self.epoch, case["name"], case["period"])
        if case["status"] != "ACTIVE" or case["tick"] >= 250 or key in self.claimed:
            return None
        case_id = CASES.get(case["name"])
        if case_id:
            self.claimed.add(key)
            return f"ritcx.{case_id}.bot"
        return None


def read_case(client, requested_case):
    case = client.get("case")
    if requested_case and CASES.get(case["name"]) != requested_case:
        raise TradingError(
            f"Requested {requested_case}, but the server reports '{case['name']}'. "
            "Check the assigned case URL."
        )
    return case


def preflight(client, volatility_strategy="buffered", requested_case=None):
    """GET-only checks; inactive/late heats can pass without being entry eligible."""
    case = read_case(client, requested_case)
    securities = client.get("securities")
    limits = client.get("limits")
    trader = client.get("trader")
    orders = client.get("orders?status=OPEN")
    if case["name"] == "RITCx Volatility Trading Case":
        from .volatility.bot import LIVE_POLICIES
        from .volatility.bot import validate as validate_volatility

        def validate(case, securities, limits):
            validate_volatility(case, securities, limits, LIVE_POLICIES[volatility_strategy])
    elif case["name"] == "RITCx - Algorithmic ETF Arbitrage Case":
        from .etf.bot import validate
    else:
        raise TradingError("Unknown case; this runner supports only the two reviewed RITCx cases")
    validate(case, securities, limits)
    positions = {
        s["ticker"]: s["position"]
        for s in securities
        if s["ticker"] != "CAD" and abs(s["position"]) >= (1 if s["ticker"] == "USD" else 1e-9)
    }
    if orders or positions:
        raise TradingError(
            f"Account is not ready: inventory={positions}, open_orders={len(orders)}. Reconcile before starting."
        )
    return dict(
        case=case,
        trader=trader,
        positions=positions,
        open_orders=0,
        entry_eligible=case["status"] == "ACTIVE" and case["tick"] < 250,
    )


def child_command(script, config, trade, mode, lock_fd):
    command = [sys.executable, "-u", "-m", script, "--watch", "--config", str(config)]
    if trade:
        command += ["--trade", "--" + mode, "--lock-fd", str(lock_fd)]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "case", nargs="?", choices=CASES.values(), help="Run only the named case (default: detect)"
    )
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument("--trade", action="store_true", help="Enable simulated orders")
    execution_mode.add_argument(
        "--dry-run", action="store_true", help="Observe without submitting orders"
    )
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument("--practice", action="store_true")
    mode_flags.add_argument(
        "--contest",
        action="store_true",
        help="Use the formal simulator profile supplied by the organizers",
    )
    parser.add_argument(
        "--config", type=Path, help="Default: .env.contest for --contest, else .env"
    )
    parser.add_argument(
        "--volatility-strategy",
        choices=("buffered",),
        default="buffered",
        help="Only supported volatility policy (default: buffered)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check connection, rules and account without sending orders, then exit",
    )
    parser.add_argument("--hold-to-close", action="store_true", help="ETF expensive-exit fallback")
    parser.add_argument(
        "--loss-limit-cad", type=float, help="Optional ETF early liquidation trigger"
    )
    parser.add_argument("--no-baskets", action="store_true", help="ETF case: evaluate tenders only")
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Maximum heats to follow; use the announced heat count",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=900, help="Session deadline including waiting time"
    )
    args = parser.parse_args()
    if args.rounds < 1 or args.max_seconds <= 0:
        parser.error("Positive round count and deadline required")
    if args.preflight and args.trade:
        parser.error("--preflight is read-only; omit --trade")
    root = ROOT
    config = (args.config or root / (".env.contest" if args.contest else ".env")).resolve()
    cfg = read_config(config)
    validate_profile(cfg, args.practice, args.contest, args.trade)
    client = client_from_config(cfg)
    if args.preflight:
        result = preflight(client, args.volatility_strategy, args.case)
        print("PREFLIGHT PASSED (read-only): " + json.dumps(result), flush=True)
        print(
            "Ready for this active heat."
            if result["entry_eligible"]
            else "Waiting required: the case is stopped or past the entry cutoff.",
            flush=True,
        )
        return 0
    router = RoundRouter()
    child = None
    stopped = False
    completed = 0

    def interrupt(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    with (
        account_lock(cfg) if args.trade else nullcontext(None) as lock_fd,
        open_audit_log(Path(cfg.get("RIT_LOG_DIR", root / "logs")), "session") as (path, stream),
    ):
        output_lock = threading.Lock()

        def report(event, **data):
            row = json.dumps(dict(event=event, **data))
            with output_lock:
                print(row, flush=True)
                stream.write(row + "\n")
                stream.flush()

        def relay(process, heat_case, starting_nlv):
            nonlocal heat_pnl
            last_status = 0
            last_tick = heat_case["tick"]
            reset = False
            for line in process.stdout:
                with output_lock:
                    stream.write(line)
                    stream.flush()
                try:
                    row = json.loads(line)
                except ValueError:
                    print(line.rstrip(), flush=True)
                    continue
                event = row.get("event")
                if event == "snapshot":
                    last_tick = row["tick"]
                    router.observe(dict(heat_case, tick=row["tick"]))
                elif event == "round_reset":
                    reset = True
                elif event == "final_reconciliation" and row.get("case"):
                    final_case = row["case"]
                    router.observe(final_case)
                    if (
                        args.trade
                        and not reset
                        and final_case["name"] == heat_case["name"]
                        and final_case["period"] == heat_case["period"]
                        and (final_case["status"] != "ACTIVE" or final_case["tick"] >= last_tick)
                        and not row["positions"]
                        and not row["open_orders"]
                    ):
                        heat_pnl = row["trader"]["nlv"] - starting_nlv
                if event == "snapshot" and time.monotonic() - last_status >= 5:
                    last_status = time.monotonic()
                    state = (
                        "CLOSING"
                        if row.get("closing") or row.get("liquidating")
                        else ("TRADING" if args.trade else "READ-ONLY")
                    )
                    print(
                        f"{state} tick={row['tick']}/300 nlv={row['trader']['nlv']} "
                        f"pnl={row['trader']['nlv'] - starting_nlv:+,.2f} "
                        f"fines={row['trader'].get('total_fines')} positions={row.get('positions', {})}",
                        flush=True,
                    )
                elif event in (
                    "order_response",
                    "heat_summary",
                    "tender_signal",
                    "tender_response",
                    "finished_flat",
                    "final_reconciliation",
                    "stopped",
                    "reconciliation",
                    "reconciliation_unavailable",
                    "round_reset",
                    "case_changed",
                    "log_saved",
                ):
                    print(line.rstrip(), flush=True)

        started = time.monotonic()
        previous_state = None
        case_pnls = {}
        last_wait = 0
        auth_wait_started = None
        report(
            "ARMED" if args.trade else "READ_ONLY",
            rounds=args.rounds,
            environment=cfg.get("RIT_ENVIRONMENT", "practice"),
            endpoint=cfg["RIT_API_URL"],
            log=str(path),
        )
        while not stopped and time.monotonic() - started < args.max_seconds:
            try:
                case = read_case(client, args.case)
            except AuthenticationError:
                # The practice server can briefly reject even a GET during a reset.
                # Retry only this idle poll after an authenticated connection.
                if previous_state is None:
                    raise
                if auth_wait_started is None:
                    auth_wait_started = time.monotonic()
                    report("WAITING", reason="server temporarily rejected the case status check")
                if time.monotonic() - auth_wait_started >= 30:
                    raise
                time.sleep(1)
                continue
            auth_wait_started = None
            state = (case["name"], case["status"])
            if state != previous_state or time.monotonic() - last_wait >= 15:
                reason = (
                    "unknown case"
                    if case["name"] not in CASES
                    else "inactive heat"
                    if case["status"] != "ACTIVE"
                    else "heat past entry cutoff"
                    if case["tick"] >= 250
                    else "checking active heat"
                )
                report("WAITING", case=case, reason=reason)
                previous_state = state
                last_wait = time.monotonic()
            script = router.claim(case)
            if script:
                # Validate account and contract parameters before launching every heat.
                ready = preflight(client, args.volatility_strategy, CASES[case["name"]])
                heat_pnl = None
                report("STARTING", script=script, tick=case["tick"])
                command = child_command(
                    script,
                    config,
                    args.trade,
                    cfg.get("RIT_ENVIRONMENT", "practice"),
                    lock_fd,
                )
                if script == "ritcx.volatility.bot":
                    command.extend(["--strategy", args.volatility_strategy])
                if script == "ritcx.etf.bot":
                    if args.hold_to_close:
                        command.append("--hold-to-close")
                    if args.loss_limit_cad is not None:
                        command.extend(["--loss-limit-cad", str(args.loss_limit_cad)])
                    if args.no_baskets:
                        command.append("--no-baskets")
                child = subprocess.Popen(
                    command,
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    pass_fds=(lock_fd,) if lock_fd is not None else (),
                )
                reader = threading.Thread(
                    target=relay, args=(child, case, ready["trader"]["nlv"]), daemon=True
                )
                reader.start()
                close_requested = False
                while child.poll() is None:
                    if (
                        stopped or time.monotonic() - started >= args.max_seconds
                    ) and not close_requested:
                        report(
                            "CLOSE_REQUESTED",
                            message="Waiting for child reconciliation; do not start another bot",
                        )
                        child.send_signal(signal.SIGINT)
                        close_requested = True
                        stopped = True
                    time.sleep(0.2)
                reader.join()
                child.stdout.close()
                code = child.returncode
                report("HEAT_FINISHED", script=script, exit_code=code)
                if code:
                    report(
                        "STOPPED_AFTER_ERROR",
                        message="Inspect reconciliation and the account before restarting; orders will not be retried",
                    )
                    return code
                if heat_pnl is not None and not close_requested and not stopped:
                    pnls = case_pnls.setdefault(case["name"], [])
                    pnls.append(heat_pnl)
                    report(
                        "HEAT_PNL",
                        case=CASES[case["name"]],
                        heats=len(pnls),
                        pnl=round(heat_pnl, 2),
                        average_pnl=round(statistics.mean(pnls), 2),
                    )
                completed += 1
                if completed >= args.rounds:
                    break
            time.sleep(0.25)
        report("SESSION_FINISHED", completed=completed, requested=args.rounds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TradingError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
