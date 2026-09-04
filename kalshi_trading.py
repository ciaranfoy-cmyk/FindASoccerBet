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
        --side yes --count 1 --price 0.59

Order creation uses Kalshi's V2 order endpoint
(POST https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders),
which replaced the legacy /portfolio/orders path this client used to call.
The V2 body only knows a single YES-leg book: side "bid" buys YES, side
"ask" sells YES (== economically buying NO at 1 - price). buy_yes()/buy_no()
below translate the "yes"/"no" mental model into that bid/ask + price shape.
Balance/positions/orders/fills are unaffected -- those still live on the
original host and are unchanged.
"""

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SECRETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets")
ENV_PATH = os.path.join(SECRETS_DIR, "kalshi.env")
KEY_PATH = os.path.join(SECRETS_DIR, "kalshi_private_key.pem")
API_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"

ORDER_V2_BASE = "https://external-api.kalshi.com"
ORDER_V2_PATH = "/trade-api/v2/portfolio/events/orders"


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


def _request(method: str, path: str, body: dict | None = None, base: str | None = None) -> dict:
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

    url = (base or API_BASE) + path
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
    ticker: str, side: str, count: float, price: float,
    time_in_force: str = "good_till_canceled",
    self_trade_prevention_type: str = "taker_at_cross",
    client_order_id: str | None = None,
    post_only: bool = False,
    cancel_order_on_pause: bool = False,
    reduce_only: bool = False,
    subaccount: int = 0,
    exchange_index: int = 0,
) -> dict:
    """Places a REAL order on the real account via Kalshi's V2 order
    endpoint. Nothing calls this automatically -- it only runs when
    explicitly invoked with a specific ticker/side/count/price, confirmed
    by the user. Prefer buy_yes()/buy_no() below unless you specifically
    need to place a raw bid/ask (e.g. to sell an existing position).

    ticker: the Kalshi market ticker, e.g. "KXEPLTOTAL-26SEP05MCICOV-3"
    side: "bid" (buy YES) or "ask" (sell YES, == buying NO at 1 - price)
    count: number of contracts (formatted to "N.00")
    price: price in dollars, e.g. 0.56 for 56c (formatted to "N.NNNN")
    """
    if side not in ("bid", "ask"):
        raise ValueError("side must be 'bid' (buy YES) or 'ask' (sell YES)")
    if time_in_force not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
        raise ValueError("time_in_force must be fill_or_kill, good_till_canceled, or immediate_or_cancel")
    if self_trade_prevention_type not in ("taker_at_cross", "maker"):
        raise ValueError("self_trade_prevention_type must be taker_at_cross or maker")
    if not (0 < price < 1):
        raise ValueError("price must be a dollar amount strictly between 0 and 1, e.g. 0.56")

    body = {
        "ticker": ticker,
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "side": side,
        "count": f"{count:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": self_trade_prevention_type,
        "post_only": post_only,
        "cancel_order_on_pause": cancel_order_on_pause,
        "reduce_only": reduce_only,
        "subaccount": subaccount,
        "exchange_index": exchange_index,
    }
    return _request("POST", ORDER_V2_PATH, body=body, base=ORDER_V2_BASE)


def buy_yes(ticker: str, count: float, price: float, **kwargs) -> dict:
    """Buy YES (e.g. 'Over 2.5') at `price` dollars -- side=bid."""
    return place_order(ticker, "bid", count, price, **kwargs)


def buy_no(ticker: str, count: float, price: float, **kwargs) -> dict:
    """Buy NO (e.g. 'Under 2.5') at `price` dollars. Per Kalshi's V2
    schema there is no direct NO leg -- this is placed as an "ask"
    (sell YES) at 1 - price, which the docs state is economically
    equivalent.
    """
    return place_order(ticker, "ask", count, round(1 - price, 4), **kwargs)


def cancel_order(order_id: str) -> dict:
    return _request("DELETE", f"{API_PREFIX}/portfolio/orders/{order_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance")
    sub.add_parser("positions")
    sub.add_parser("orders")
    sub.add_parser("fills")

    place = sub.add_parser("place", help="Places a REAL order -- double check every argument")
    place.add_argument("--ticker", required=True)
    place.add_argument("--side", required=True, choices=["yes", "no"], help="yes=buy Over-side outcome, no=buy Under-side outcome")
    place.add_argument("--count", required=True, type=float, help="number of contracts")
    place.add_argument("--price", required=True, type=float, help="price in dollars, e.g. 0.56")
    place.add_argument("--tif", dest="time_in_force", default="good_till_canceled",
                        choices=["fill_or_kill", "good_till_canceled", "immediate_or_cancel"])

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)

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
            print(f"Placing REAL order: buy {args.count}x {args.side.upper()} "
                  f"on {args.ticker} @ ${args.price:.4f} ({args.time_in_force})")
            fn = buy_yes if args.side == "yes" else buy_no
            result = fn(args.ticker, args.count, args.price, time_in_force=args.time_in_force)
            print(json.dumps(result, indent=2))
        elif args.cmd == "cancel":
            print(json.dumps(cancel_order(args.order_id), indent=2))
    except KalshiError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
