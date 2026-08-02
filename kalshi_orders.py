"""
Real order placement against Kalshi's REST API (create / cancel / amend).
Uses the V2 order endpoints, which live on a different host
(external-api.kalshi.com) than the market-data/WS host this project has
used everywhere else (api.elections.kalshi.com) — confirmed directly from
Kalshi's own quick-start guide, not assumed.

Every function here can genuinely place, cancel, or amend a REAL order
against a REAL account if given real credentials. Nothing in this module
runs unless explicitly called — see oms.py for the layer that decides
when to call it, including the --live dry-run gate.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import kalshi_auth

REST_BASE_PROD = "https://external-api.kalshi.com/trade-api/v2"
REST_BASE_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"


def base_url(use_demo: bool) -> str:
    return REST_BASE_DEMO if use_demo else REST_BASE_PROD


def _price_to_dollars_str(price_cents: int) -> str:
    return f"{price_cents / 100:.4f}"


def _count_to_str(count: int) -> str:
    return f"{count:.2f}"


class KalshiOrderError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


def _request(method: str, use_demo: bool, path: str, body: dict | None) -> dict:
    """
    Signed REST call to a portfolio/order endpoint. `path` is relative to
    /trade-api/v2 (e.g. "/portfolio/events/orders"). Raises KalshiOrderError
    on any non-2xx response rather than silently returning something wrong.
    """
    full_path = "/trade-api/v2" + path
    headers = kalshi_auth.build_rest_headers(method, full_path)
    if headers is None:
        raise RuntimeError("Kalshi auth headers unavailable — check KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH")
    headers = dict(headers)
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = base_url(use_demo) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise KalshiOrderError(e.code, e.read().decode()) from None


def create_order(
    *,
    ticker: str,
    side: str,
    price_cents: int,
    count: int,
    use_demo: bool = False,
    time_in_force: str = "good_till_canceled",
    self_trade_prevention_type: str = "maker",
    client_order_id: str | None = None,
) -> dict:
    """
    Places a real resting limit order. side: 'bid' (buy YES) or 'ask' (sell YES).
    Returns the CreateOrderV2Response dict (order_id, fill_count, remaining_count, ...).
    """
    if side not in ("bid", "ask"):
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
    body = {
        "ticker": ticker,
        "side": side,
        "count": _count_to_str(count),
        "price": _price_to_dollars_str(price_cents),
        "time_in_force": time_in_force,
        "self_trade_prevention_type": self_trade_prevention_type,
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }
    return _request("POST", use_demo, "/portfolio/events/orders", body)


def cancel_order(order_id: str, *, use_demo: bool = False) -> dict:
    """Cancels a resting order. Returns CancelOrderV2Response (order_id, reduced_by, ...)."""
    return _request("DELETE", use_demo, f"/portfolio/events/orders/{order_id}", None)


def amend_order(
    order_id: str,
    *,
    ticker: str,
    side: str,
    price_cents: int,
    count: int,
    use_demo: bool = False,
    client_order_id: str | None = None,
) -> dict:
    """
    Changes the price and/or max fillable count of an existing resting order
    in one call — cheaper than cancel+create, and per Kalshi's docs preserves
    queue position when the amendment only decreases size (any price change,
    including ours since we requote around a moving midpoint, forfeits queue
    position and re-queues at the back regardless).
    """
    if side not in ("bid", "ask"):
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
    body = {
        "ticker": ticker,
        "side": side,
        "price": _price_to_dollars_str(price_cents),
        "count": _count_to_str(count),
    }
    if client_order_id:
        body["updated_client_order_id"] = client_order_id
    return _request("POST", use_demo, f"/portfolio/events/orders/{order_id}/amend", body)


def get_balance(*, use_demo: bool = False) -> dict:
    """Read-only: current account balance. Safe to call anytime."""
    return _request("GET", use_demo, "/portfolio/balance", None)


def get_positions(*, use_demo: bool = False) -> dict:
    """Read-only: current real positions. Safe to call anytime."""
    return _request("GET", use_demo, "/portfolio/positions", None)


def get_orders(*, ticker: str | None = None, status: str | None = None, use_demo: bool = False) -> dict:
    """Read-only: list resting/canceled/executed orders. Safe to call anytime."""
    path = "/portfolio/orders"
    params = []
    if ticker:
        params.append(f"ticker={ticker}")
    if status:
        params.append(f"status={status}")
    if params:
        path += "?" + "&".join(params)
    # Query params are stripped before signing per Kalshi's docs, but the
    # actual request path (sent to the server) must keep them.
    full_path = "/trade-api/v2" + path.split("?")[0]
    headers = kalshi_auth.build_rest_headers("GET", full_path)
    if headers is None:
        raise RuntimeError("Kalshi auth headers unavailable")
    req = urllib.request.Request(base_url(use_demo) + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise KalshiOrderError(e.code, e.read().decode()) from None
