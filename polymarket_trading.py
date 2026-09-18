#!/usr/bin/env python3
"""Authenticated Polymarket US trading client -- signed requests against
the real account (not the public market-data reads polymarket_prices.py
already does, and NOT the international Polymarket CLOB, which is
geoblocked for order placement from this environment -- see
docs.polymarket.us/developers/CLOB/geoblock discussion in chat).
Polymarket US is a separate, US-regulated product requiring its own KYC
and its own credentials; nothing here is compatible with a wallet
private key or the international CLOB's L1/L2 auth.

Auth is a custom header scheme, not HMAC and not a bearer token:
X-PM-Access-Key (key ID), X-PM-Timestamp (ms), X-PM-Signature (Ed25519
signature over timestamp + method + path). The secret key is a 64-byte
base64 blob; only the first 32 bytes are the actual Ed25519 seed (the
rest appears to be the derived public key, standard for how some
libraries serialize an Ed25519 keypair together) -- confirmed against
docs.polymarket.us/api-reference/authentication's own example, not
guessed.

Credentials are read from secrets/polymarket.env (POLYMARKET_API_KEY,
POLYMARKET_API_SECRET) -- gitignored, never committed, same pattern as
secrets/kalshi.env.

This module deliberately keeps order placement as an explicit, separate
call (place_order) that is never invoked automatically -- nothing in
this file fires a live trade on import or on module load.

UNTESTED: unlike kalshi_trading.py (validated against live balance/
order calls in this same session), no call in this module has been run
against the real API -- the harness's own safety classifier declined a
plain unauthenticated connectivity check against api.polymarket.us as a
real-world-transactions risk, so every endpoint path, field name and
enum value here is transcribed from docs.polymarket.us and has NOT been
confirmed to actually work. Run `balance` first and read the raw
response before trusting anything else in here, and expect to have to
fix field names against whatever error comes back.

Usage (read-only, safe -- but still unverified, see above):
    python3 polymarket_trading.py balance
    python3 polymarket_trading.py positions
    python3 polymarket_trading.py activities

Usage (places a REAL order -- confirm every field before running):
    python3 polymarket_trading.py place --market-slug epl-mac-sun-2026-09-20-total-2pt5 \
        --side yes --price 0.67 --quantity 1.49
"""

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric import ed25519

SECRETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets")
ENV_PATH = os.path.join(SECRETS_DIR, "polymarket.env")
API_BASE = "https://api.polymarket.us"


class PolymarketError(RuntimeError):
    pass


def _load_creds() -> tuple[str, str]:
    if not os.path.exists(ENV_PATH):
        raise PolymarketError(f"Missing {ENV_PATH} -- expected POLYMARKET_API_KEY=... and POLYMARKET_API_SECRET=... in it.")
    key_id = secret = None
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("POLYMARKET_API_KEY="):
                key_id = line.strip().split("=", 1)[1]
            elif line.startswith("POLYMARKET_API_SECRET="):
                secret = line.strip().split("=", 1)[1]
    if not key_id or not secret:
        raise PolymarketError("POLYMARKET_API_KEY / POLYMARKET_API_SECRET not found in secrets/polymarket.env")
    return key_id, secret


def _sign(secret_b64: str, message: str) -> str:
    raw = base64.b64decode(secret_b64)
    seed = raw[:32]  # see module docstring -- the rest of the 64 bytes is not the signing key
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    signature = private_key.sign(message.encode("utf-8"))
    return base64.b64encode(signature).decode("utf-8")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    key_id, secret = _load_creds()
    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + method.upper() + path
    signature = _sign(secret, message)

    headers = {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp_ms,
        "X-PM-Signature": signature,
        "Content-Type": "application/json",
    }

    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        raise PolymarketError(f"{method} {path} failed: {exc.code} {exc.reason} -- {error_body}") from exc


def get_balance() -> dict:
    return _request("GET", "/v1/account/balances")


def get_positions() -> dict:
    return _request("GET", "/v1/portfolio/positions")


def get_activities() -> dict:
    return _request("GET", "/v1/portfolio/activities")


def place_order(
    market_slug: str, outcome_side: str, action: str, price: float, quantity: float,
    order_type: str = "ORDER_TYPE_LIMIT",
    tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    manual: bool = True,
    participate_dont_initiate: bool = False,
) -> dict:
    """Places a REAL order on the real Polymarket US account. Nothing
    calls this automatically -- it only runs when explicitly invoked
    with a specific market/side/price/quantity, confirmed by the user.
    Prefer buy_yes()/buy_no() below.

    market_slug: the Polymarket event/market slug, e.g.
        "epl-mac-sun-2026-09-20-total-2pt5" -- NOT the same as a Kalshi
        ticker; see polymarket_prices.py's fetch results for the right
        slug per fixture (its "ticker" field, despite the misleading
        name kept for drop-in compatibility with the Kalshi shape).
    outcome_side: "OUTCOME_SIDE_YES" or "OUTCOME_SIDE_NO"
    action: "ORDER_ACTION_BUY" or "ORDER_ACTION_SELL"
    price: dollars, e.g. 0.67 for 67c
    quantity: number of contracts
    """
    if outcome_side not in ("OUTCOME_SIDE_YES", "OUTCOME_SIDE_NO"):
        raise ValueError("outcome_side must be OUTCOME_SIDE_YES or OUTCOME_SIDE_NO")
    if action not in ("ORDER_ACTION_BUY", "ORDER_ACTION_SELL"):
        raise ValueError("action must be ORDER_ACTION_BUY or ORDER_ACTION_SELL")
    if not (0.01 <= price <= 0.99):
        raise ValueError("price must be between 0.01 and 0.99")

    body = {
        "marketSlug": market_slug,
        "type": order_type,
        "price": {"value": f"{price:.4f}", "currency": "USD"},
        "quantity": quantity,
        "tif": tif,
        "outcomeSide": outcome_side,
        "action": action,
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL" if manual else "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "participateDontInitiate": participate_dont_initiate,
    }
    return _request("POST", "/v1/orders", body=body)


def buy_yes(market_slug: str, price: float, quantity: float, **kwargs) -> dict:
    """Buy YES (e.g. 'Over 2.5') at `price` dollars."""
    return place_order(market_slug, "OUTCOME_SIDE_YES", "ORDER_ACTION_BUY", price, quantity, **kwargs)


def buy_no(market_slug: str, price: float, quantity: float, **kwargs) -> dict:
    """Buy NO (e.g. 'Under 2.5') at `price` dollars."""
    return place_order(market_slug, "OUTCOME_SIDE_NO", "ORDER_ACTION_BUY", price, quantity, **kwargs)


def cancel_order(order_id: str) -> dict:
    return _request("POST", f"/v1/order/{order_id}/cancel")


def cancel_all_open_orders() -> dict:
    return _request("POST", "/v1/orders/open/cancel")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance")
    sub.add_parser("positions")
    sub.add_parser("activities")

    place = sub.add_parser("place", help="Places a REAL order -- double check every argument")
    place.add_argument("--market-slug", required=True)
    place.add_argument("--side", required=True, choices=["yes", "no"])
    place.add_argument("--price", required=True, type=float)
    place.add_argument("--quantity", required=True, type=float)
    place.add_argument("--tif", default="TIME_IN_FORCE_GOOD_TILL_CANCEL")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)

    sub.add_parser("cancel-all")

    args = parser.parse_args()

    try:
        if args.cmd == "balance":
            print(json.dumps(get_balance(), indent=2))
        elif args.cmd == "positions":
            print(json.dumps(get_positions(), indent=2))
        elif args.cmd == "activities":
            print(json.dumps(get_activities(), indent=2))
        elif args.cmd == "place":
            print(f"Placing REAL order: buy {args.quantity}x {args.side.upper()} "
                  f"on {args.market_slug} @ ${args.price:.4f} ({args.tif})")
            fn = buy_yes if args.side == "yes" else buy_no
            result = fn(args.market_slug, args.price, args.quantity, tif=args.tif)
            print(json.dumps(result, indent=2))
        elif args.cmd == "cancel":
            print(json.dumps(cancel_order(args.order_id), indent=2))
        elif args.cmd == "cancel-all":
            print(json.dumps(cancel_all_open_orders(), indent=2))
    except PolymarketError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
