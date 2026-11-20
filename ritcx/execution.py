"""Known-order reconciliation shared by both strategies, including cancel races."""

import math
import time

from .runtime import TradingError


class PartialFill(TradingError):
    """A KNOWN partial fill, with its remainder cancelled and actual inventory read."""

    def __init__(self, receipt, securities):
        self.filled = receipt["quantity_filled"]
        self.securities = securities
        super().__init__(
            f"Confirmed partial fill: {self.filled}/{receipt.get('quantity')}; remainder cancelled"
        )


def submit_order(client, sec, quantity, action, *, price=None):
    payload = dict(
        ticker=sec["ticker"],
        type="LIMIT" if price is not None else "MARKET",
        quantity=quantity,
        action=action,
    )
    if price is not None:
        if not math.isfinite(price) or price <= 0:
            raise TradingError("Invalid limit price")
        payload["price"] = price
    client.audit("order_attempt", **payload)
    start = time.monotonic()
    receipt = client.request("POST", "orders", payload)
    elapsed = time.monotonic() - start
    client.order_latencies.append(elapsed)
    client.audit("order_response", latency_ms=round(elapsed * 1000, 2), **receipt)
    order_id = receipt.get("order_id")
    filled = receipt.get("quantity_filled")
    if (
        not isinstance(order_id, int)
        or not isinstance(filled, (float, int))
        or not math.isfinite(filled)
        or not 0 <= filled <= quantity
    ):
        raise TradingError(
            "Uncertain order receipt; reconcile account before any further submission"
        )
    return receipt


def reconcile_order(client, sec, quantity, action, receipt):
    before = sec["position"]
    order_id = receipt["order_id"]
    # A fully filled receipt also must carry a terminal status.
    if receipt.get("status") not in ("TRANSACTED", "CANCELLED", "REJECTED"):
        # A limit may have fully filled while its cancellation was in flight.
        try:
            client.request("DELETE", f"orders/{order_id}")
        except TradingError:
            receipt = client.get(f"orders/{order_id}")
            if receipt.get("status") not in ("TRANSACTED", "CANCELLED", "REJECTED"):
                raise
        else:
            receipt = client.get(f"orders/{order_id}")
        if receipt.get("status") not in ("TRANSACTED", "CANCELLED", "REJECTED"):
            raise TradingError("Order cancellation not confirmed; no replacement submitted")
    filled = receipt.get("quantity_filled")
    if (
        not isinstance(filled, (int, float))
        or not math.isfinite(filled)
        or filled != int(filled)
        or not 0 <= filled <= quantity
    ):
        raise TradingError("Invalid final fill quantity")
    expected = before + (filled if action == "BUY" else -filled)
    tolerance = 1e-6 if sec.get("type") == "CURRENCY" else 0.0
    for _ in range(6):
        latest = client.get("securities")
        actual = next(s["position"] for s in latest if s["ticker"] == sec["ticker"])
        if math.isclose(actual, expected, abs_tol=tolerance, rel_tol=0):
            break
        time.sleep(0.1)
    else:
        raise TradingError(
            f"Fill could not be reconciled for {sec['ticker']}: expected {expected}, observed {actual}"
        )
    if client.get("orders?status=OPEN"):
        raise TradingError("Open orders remain after reconciliation; entries blocked")
    client.last_securities = latest
    client.audit(
        "fill_confirmed",
        ticker=sec["ticker"],
        position=actual,
        quantity_filled=filled,
        requested=quantity,
        order_id=order_id,
    )
    return dict(receipt, quantity=quantity), latest


def execute_order(client, sec, quantity, action):
    sec = dict(sec)  # Capture the pre-submission position, including for in-process transports.
    receipt = submit_order(client, sec, quantity, action)
    return reconcile_order(client, sec, quantity, action, receipt)


def submit_change(client, securities, ticker, change):
    sec = next(x for x in securities if x["ticker"] == ticker)
    cap = min(100, getattr(client, "option_ticket_limit", 10)) if sec["type"] == "OPTION" else 10000
    quantity = min(abs(int(change)), cap, int(sec["max_trade_size"]))
    if quantity:
        return client.order(sec, quantity, "BUY" if change > 0 else "SELL")
    return securities


def confirm_flat(client):
    securities = client.get("securities")
    orders = client.get("orders?status=OPEN")
    positions = {
        s["ticker"]: s["position"]
        for s in securities
        if s["ticker"] != "CAD" and abs(s["position"]) >= (1 if s["ticker"] == "USD" else 1e-9)
    }
    if positions or orders:
        raise TradingError(
            f"Unresolved inventory={positions}, open_orders={len(orders)}; inspect account before restarting"
        )
    return dict(
        case=client.get("case"),
        trader=client.get("trader"),
        positions=positions,
        open_orders=orders,
    )
