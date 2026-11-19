"""Authenticated, paced HTTP transport. Mutations are never blindly retried."""

import base64
import json
import math
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .runtime import TradingError


class DMAClient:
    """RIT DMA by default; an explicit api_key selects client REST authentication."""

    def __init__(
        self,
        url,
        username,
        password,
        execute=False,
        audit=None,
        *,
        api_key=None,
        request_interval=0.04,
    ):
        self.url = url.rstrip("/")
        self.headers = (
            {"X-API-Key": api_key}
            if api_key is not None
            else {
                "Authorization": "Basic "
                + base64.b64encode(f"{username}:{password}".encode()).decode()
            }
        )
        self.execute = execute
        self.audit = audit or (lambda *args, **kwargs: None)
        self.heat = None
        self.snapshot_tick = None
        self.request_interval = request_interval
        self._pace_lock = threading.Lock()
        self._next_request = 0
        self.order_latencies = []
        self.last_securities = None
        self.option_ticket_limit = 10

    def bind_heat(self, case):
        self.heat = (case["name"], case["period"], case["tick"])

    def bind_snapshot(self, case):
        if self.heat and (case["name"], case["period"]) != self.heat[:2]:
            raise TradingError("Snapshot belongs to a different heat")
        self.snapshot_tick = case["tick"]

    def check_heat(self, require_fresh=True):
        case = self.get("case")
        name, period, tick = self.heat
        if (
            case["name"] != name
            or case["period"] != period
            or case["status"] != "ACTIVE"
            or case["tick"] < tick
        ):
            raise TradingError("Heat changed before submission; inspect account before restarting")
        if (
            require_fresh
            and self.snapshot_tick is not None
            and case["tick"] - self.snapshot_tick > 2
        ):
            raise TradingError("Decision snapshot is over two ticks old; submission blocked")
        self.bind_heat(case)
        return case

    def _pace(self):
        with self._pace_lock:
            now = time.monotonic()
            delay = max(0, self._next_request - now)
            self._next_request = max(now, self._next_request) + self.request_interval
        if delay:
            time.sleep(delay)

    def request(self, method, endpoint, params=None):
        if method != "GET":
            if not self.execute:
                raise TradingError("Submissions disabled in read-only mode")
            if not (
                (
                    method == "POST"
                    and (endpoint == "orders" or re.fullmatch(r"tenders/\d+", endpoint))
                )
                or (method == "DELETE" and re.fullmatch(r"orders/\d+", endpoint))
            ):
                raise TradingError("Unsupported mutation; converters are manual-only")
            if self.heat is not None:
                self.check_heat(require_fresh=method != "DELETE")
        suffix = "?" + urlencode(params) if params else ""
        req = Request(self.url + "/" + endpoint + suffix, method=method, headers=self.headers)
        for attempt in range(4):
            self._pace()
            try:
                with urlopen(req, timeout=3) as response:
                    data = response.read()
                    return json.loads(data) if data else {}
            except HTTPError as exc:
                if method == "GET" and exc.code == 429 and attempt < 3:
                    try:
                        body = json.loads(exc.read() or b"{}")
                        wait = float(exc.headers.get("Retry-After", body.get("wait", 1)))
                    except (ValueError, TypeError):
                        raise TradingError("Invalid rate-limit retry timing") from None
                    if not math.isfinite(wait) or wait < 0 or wait > 5:
                        raise TradingError(
                            "Rate-limit wait exceeds operating budget; refresh required"
                        )
                    self.audit("rate_limited", endpoint=endpoint, wait_seconds=wait)
                    time.sleep(wait)
                    continue
                self.audit(
                    "request_rejected", method=method, endpoint=endpoint, http_status=exc.code
                )
                raise TradingError(
                    f"{method} {endpoint}: HTTP {exc.code}; submission not retried"
                ) from None
            except (URLError, TimeoutError, OSError, ValueError):
                raise TradingError(
                    f"{method} {endpoint}: connection/response failure; submission not retried. Reconcile positions before restarting."
                ) from None
        raise TradingError("Request retry limit")

    def get(self, endpoint):
        return self.request("GET", endpoint)

    def order(self, sec, quantity, action):
        from .execution import PartialFill, execute_order

        cap = min(100, self.option_ticket_limit) if sec["type"] == "OPTION" else 10000
        if (
            not self.execute
            or action not in ("BUY", "SELL")
            or not isinstance(quantity, int)
            or not 0 < quantity <= min(cap, sec["max_trade_size"])
        ):
            raise TradingError("Invalid order, disabled execution, or ticket exceeds risk cap")
        receipt, latest = execute_order(self, sec, quantity, action)
        if receipt["quantity_filled"] != quantity:
            raise PartialFill(receipt, latest)
        return latest


def client_from_config(cfg, *, execute=False, audit=None, client_type=DMAClient):
    return client_type(
        cfg["RIT_API_URL"],
        cfg["RIT_USERNAME"],
        cfg.get("RIT_PASSWORD", ""),
        execute=execute,
        audit=audit,
        api_key=cfg.get("RIT_API_KEY") if cfg.get("RIT_API_KIND") == "rest" else None,
    )
