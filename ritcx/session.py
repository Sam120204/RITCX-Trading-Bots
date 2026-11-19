"""Preflight and automatic heat selection for the RITCx simulated contest."""

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .api import client_from_config
from .runtime import ROOT, TradingError, account_lock, open_audit_log, read_config, validate_profile

SCRIPTS = {
    "RITCx Volatility Trading Case": "ritcx.volatility.bot",
    "RITCx - Algorithmic ETF Arbitrage Case": "ritcx.etf.bot",
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
        script = SCRIPTS.get(case["name"])
        if script:
            self.claimed.add(key)
        return script


def preflight(client, volatility_strategy="buffered"):
    """GET-only checks; inactive/late heats can pass without being entry eligible."""
    case = client.get("case")
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


def child_command(root, script, config, trade, mode, lock_fd):
    command = [sys.executable, "-u", "-m", script, "--watch", "--config", str(config)]
    if trade:
        command += ["--trade", "--" + mode, "--lock-fd", str(lock_fd)]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true", help="Enable simulated orders")
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument("--practice", action="store_true")
    mode_flags.add_argument(
        "--contest",
        action="store_true",
        help="Use the formal simulator profile supplied by the organizers",
    )
    parser.add_argument("--config", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--volatility-strategy",
        choices=(
            "baseline",
            "adaptive_small",
            "adaptive",
            "scaled_small",
            "scaled",
            "buffered",
            "buffered_small",
        ),
        default="buffered",
        help="Volatility policy (default: buffered); baseline restores the original small position limits",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check connection, rules and account without sending orders, then exit",
    )
    parser.add_argument(
        "--passive-tenders",
        action="store_true",
        help="Enable optional bounded passive tender exits",
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
    config = args.config.resolve()
    cfg = read_config(config)
    validate_profile(cfg, args.practice, args.contest, args.trade)
    client = client_from_config(cfg)
    if args.preflight:
        result = preflight(client, args.volatility_strategy)
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

        def relay(process, heat_case):
            last_status = 0
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
                    router.observe(dict(heat_case, tick=row["tick"]))
                elif event == "final_reconciliation" and row.get("case"):
                    router.observe(row["case"])
                if event == "snapshot" and time.monotonic() - last_status >= 5:
                    last_status = time.monotonic()
                    state = (
                        "CLOSING"
                        if row.get("closing") or row.get("liquidating")
                        else ("TRADING" if args.trade else "READ-ONLY")
                    )
                    print(
                        f"{state} tick={row['tick']}/300 nlv={row['trader'].get('nlv')} fines={row['trader'].get('total_fines')} positions={row.get('positions', {})}",
                        flush=True,
                    )
                elif event in (
                    "order_response",
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
        last_wait = 0
        report(
            "ARMED" if args.trade else "READ_ONLY",
            rounds=args.rounds,
            environment=cfg.get("RIT_ENVIRONMENT", "practice"),
            endpoint=cfg["RIT_API_URL"],
            log=str(path),
        )
        while not stopped and time.monotonic() - started < args.max_seconds:
            case = client.get("case")
            state = (case["name"], case["status"])
            if state != previous_state or time.monotonic() - last_wait >= 15:
                reason = (
                    "unknown case"
                    if case["name"] not in SCRIPTS
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
                preflight(client, args.volatility_strategy)
                report("STARTING", script=script, tick=case["tick"])
                command = child_command(
                    root,
                    script,
                    config,
                    args.trade,
                    cfg.get("RIT_ENVIRONMENT", "practice"),
                    lock_fd,
                )
                if script == "ritcx.volatility.bot":
                    command.extend(["--strategy", args.volatility_strategy])
                if script == "ritcx.etf.bot":
                    if args.passive_tenders:
                        command.append("--passive-tenders")
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
                reader = threading.Thread(target=relay, args=(child, case), daemon=True)
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
