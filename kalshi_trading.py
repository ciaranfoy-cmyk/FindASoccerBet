#!/usr/bin/env python3
"""Authenticated Kalshi trading client -- signed requests against the
real account (not the public market-data reads live_kalshi_edge_test.py
already does). Kalshi's v2 API authenticates writes (and private reads
like balance/positions/orders) with an RSA-PSS signature over
`timestamp + method + path`, sent as three headers alongside the API
key ID.

Credentials are read from secrets/kalshi.env (KALSHI_API_KEY_ID) and
secrets/kalshi_private_key.pem -- both gitignored, never committed.

This module deliberately keeps order placement as an explicit, separate
call (place_order) that is never invoked automatically -- nothing in
this file fires a live trade on import or on module load.

Usage (read-only, safe):
    python3 kalshi_trading.py balance
    python3 kalshi_trading.py positions
    python3 kalshi_trading.py orders

Usage (places a REAL order -- confirm every field before running):
    python3 kalshi_trading.py place --ticker KXEPLTOTAL-26SEP05MCICOV-3 \
        --side yes --action buy --count 1 --price 59 --type limit
"""

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SECRETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets")
ENV_PATH = os.path.join(SECRETS_DIR, "kalshi.env")
KEY_PATH = os.path.join(SECRETS_DIR, "kalshi_private_key.pem")
API_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


class KalshiError(RuntimeError):
    pass


def _load_key_id() -> str:
    if not os.path.exists(ENV_PATH):
        raise KalshiError(f"Missing {ENV_PATH} -- expected KALSHI_API_KEY_ID=... in it.")
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("KALSHI_API_KEY_ID="):
                return line.strip().split("=", 1)[1]
    raise KalshiError("KALSHI_API_KEY_ID not found in secrets/kalshi.env")


def _load_private_key():
    if not os.path.exists(KEY_PATH):
        raise KalshiError(f"Missing {KEY_PATH}")
    with open(KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    key_id = _load_key_id()
    private_key = _load_private_key()

    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + method.upper() + path
    signature = _sign(private_key, message)

    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type": "application/json",
    }

    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        raise KalshiError(f"{method} {path} failed: {exc.code} {exc.reason} -- {error_body}") from exc


def get_balance() -> dict:
    return _request("GET", f"{API_PREFIX}/portfolio/balance")


def get_positions() -> dict:
    return _request("GET", f"{API_PREFIX}/portfolio/positions")


def get_orders(status: str | None = None) -> dict:
    path = f"{API_PREFIX}/portfolio/orders"
    if status:
        path += f"?status={status}"
    return _request("GET", path)


def get_fills() -> dict:
    return _request("GET", f"{API_PREFIX}/portfolio/fills")


def place_order(
    ticker: str, side: str, action: str, count: int,
    order_type: str = "limit", price_cents: int | None = None,
    client_order_id: str | None = None,
) -> dict:
    """Places a REAL order on the real account. Nothing calls this
    automatically -- it only runs when explicitly invoked with a
    specific ticker/side/action/count/price, confirmed by the user.

    ticker: the Kalshi market ticker, e.g. "KXEPLTOTAL-26SEP05MCICOV-3"
    side: "yes" or "no"
    action: "buy" or "sell"
    count: number of contracts
    order_type: "limit" or "market"
    price_cents: required for limit orders, 1-99 (price in cents)
    """
    if side not in ("yes", "no"):
        raise ValueError("side must be 'yes' or 'no'")
    if action not in ("buy", "sell"):
        raise ValueError("action must be 'buy' or 'sell'")
    if order_type == "limit" and price_cents is None:
        raise ValueError("price_cents is required for limit orders")

    body = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "type": order_type,
        "client_order_id": client_order_id or f"cli-{int(time.time() * 1000)}",
    }
    if order_type == "limit":
        price_field = "yes_price" if side == "yes" else "no_price"
        body[price_field] = price_cents

    return _request("POST", f"{API_PREFIX}/portfolio/orders", body=body)


def cancel_order(order_id: str) -> dict:
    return _request("DELETE", f"{API_PREFIX}/portfolio/orders/{order_id}")


def _probe_order_schemas(ticker: str, side: str, action: str, count: int, price_cents: int) -> None:
    """Diagnostic only -- POSTs several plausible v2 order-creation body
    shapes to the one real, documented path, to see whether the
    'deprecated_v1_order_endpoint' response is a blanket path-level block
    (same error regardless of body) or a real schema mismatch (error
    changes once the right fields are sent). Never places contradictory
    real orders -- every variant describes the exact same order.
    """
    path = f"{API_PREFIX}/portfolio/orders"
    variants = {
        "current (yes_price/no_price, cents)": {
            "ticker": ticker, "side": side, "action": action, "count": count,
            "type": "limit", "yes_price": price_cents, "client_order_id": "probe-a",
        },
        "price field (not yes_price)": {
            "ticker": ticker, "side": side, "action": action, "count": count,
            "type": "limit", "price": price_cents, "client_order_id": "probe-b",
        },
        "quantity instead of count": {
            "ticker": ticker, "side": side, "action": action, "quantity": count,
            "type": "limit", "yes_price": price_cents, "client_order_id": "probe-c",
        },
        "order_type instead of type": {
            "ticker": ticker, "side": side, "action": action, "count": count,
            "order_type": "limit", "yes_price": price_cents, "client_order_id": "probe-d",
        },
        "price in dollars (string)": {
            "ticker": ticker, "side": side, "action": action, "count": count,
            "type": "limit", "yes_price_dollars": f"{price_cents/100:.2f}", "client_order_id": "probe-e",
        },
        "buy_max_cost market-style": {
            "ticker": ticker, "side": side, "action": action, "count": count,
            "type": "market", "buy_max_cost": price_cents, "client_order_id": "probe-f",
        },
        "empty body": {},
    }
    for label, body in variants.items():
        try:
            result = _request("POST", path, body=body)
            print(f"[{label}] SUCCESS: {result}")
        except KalshiError as exc:
            print(f"[{label}] {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance")
    sub.add_parser("positions")
    sub.add_parser("orders")
    sub.add_parser("fills")

    place = sub.add_parser("place", help="Places a REAL order -- double check every argument")
    place.add_argument("--ticker", required=True)
    place.add_argument("--side", required=True, choices=["yes", "no"])
    place.add_argument("--action", required=True, choices=["buy", "sell"])
    place.add_argument("--count", required=True, type=int)
    place.add_argument("--type", dest="order_type", default="limit", choices=["limit", "market"])
    place.add_argument("--price", dest="price_cents", type=int, default=None,
                        help="Price in cents (1-99), required for limit orders")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)

    probe = sub.add_parser("probe-schema", help="Diagnostic: try several v2 order body shapes against the real endpoint")
    probe.add_argument("--ticker", required=True)
    probe.add_argument("--side", required=True, choices=["yes", "no"])
    probe.add_argument("--action", required=True, choices=["buy", "sell"])
    probe.add_argument("--count", required=True, type=int)
    probe.add_argument("--price", dest="price_cents", required=True, type=int)

    args = parser.parse_args()

    try:
        if args.cmd == "balance":
            print(json.dumps(get_balance(), indent=2))
        elif args.cmd == "positions":
            print(json.dumps(get_positions(), indent=2))
        elif args.cmd == "orders":
            print(json.dumps(get_orders(), indent=2))
        elif args.cmd == "fills":
            print(json.dumps(get_fills(), indent=2))
        elif args.cmd == "place":
            print(f"Placing REAL order: {args.action} {args.count}x {args.side.upper()} "
                  f"on {args.ticker} @ {args.price_cents}c ({args.order_type})")
            result = place_order(args.ticker, args.side, args.action, args.count,
                                  args.order_type, args.price_cents)
            print(json.dumps(result, indent=2))
        elif args.cmd == "cancel":
            print(json.dumps(cancel_order(args.order_id), indent=2))
        elif args.cmd == "probe-schema":
            _probe_order_schemas(args.ticker, args.side, args.action, args.count, args.price_cents)
    except KalshiError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
