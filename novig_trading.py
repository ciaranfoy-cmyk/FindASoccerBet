#!/usr/bin/env python3
"""Authenticated Novig client (v3 API, docs.novig.com), the third book
alongside Kalshi and Polymarket US. Prices for the pick table come from
novig_prices.py, which uses Novig's public (keyless) catalog; this file
is only needed for balances, funding and orders.

Auth: every request is signed with an Ed25519 (or P-256) private key.
The signature covers "NOVIG-V3\\n<ts ms>\\n<METHOD>\\n<path>\\n<canonical
query>\\n<sha256 hex of body>" and goes in Novig-Signature, alongside
Novig-Key-Id and Novig-Timestamp. Confirmed live 2026-10-26: POST
/v3/echo answered 200 from the cloud, so Novig's location check passes
from here.

Two keys, two jobs:
  management -- created on novig.com Profile > Settings > Novig API.
      Opens and funds subaccounts and reads balances. Can't place orders.
  trading -- one per subaccount, created by POST /v3/account/subaccounts
      from a public key we send. Places and cancels orders.
Our subaccount is labelled "findasoccerbet" (opened 2026-10-26).

Credentials: secrets/novig_mgmt.pem and secrets/novig_trading.pem
(gitignored), written at session start from the NOVIG_PRIVATE_KEY_PEM
and NOVIG_TRADING_PRIVATE_KEY_PEM env vars, plus NOVIG_KEY_ID. If the
.pem files are missing they're written from the env vars on first use.
The trading key ID isn't secret and is looked up from the subaccount
list, so nothing else needs storing.

Units (see docs api/concepts/money.md): one contract pays 1 CENT, not $1
like Kalshi. 500 contracts at 0.60 cost $3.00 and pay $5.00. Prices are
strings on a grid (0.005 steps between 0.055 and 0.945); every order is
a BUY of one outcome, so buying Over at P matches someone buying Under
at 1 - P. Taker fee is 0.06 * P * (1 - P) per contract, but game markets
charge it only once the match is live (fee.charged == WHEN_LIVE), so a
pregame fill pays no fee.

Nothing here places an order on import. `place` is an explicit CLI call
and must only be run after Ciaran approves the exact order list.

Usage (read-only):
    python3 novig_trading.py echo
    python3 novig_trading.py balance          # main account + subaccount
    python3 novig_trading.py subaccounts
    python3 novig_trading.py book --market-id <uuid>
    python3 novig_trading.py orders
    python3 novig_trading.py positions

Usage (moves money or places a REAL order -- approval first):
    python3 novig_trading.py fund --amount 25.00
    python3 novig_trading.py place --outcome-id <uuid> --price 0.590 --qty 500 --tif IOC
    python3 novig_trading.py cancel --order-id <uuid>
"""

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.parse
import uuid
from decimal import Decimal

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key

HOST = "https://api.novig.com"
SECRETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets")
MGMT_PEM = os.path.join(SECRETS_DIR, "novig_mgmt.pem")
TRADING_PEM = os.path.join(SECRETS_DIR, "novig_trading.pem")
SUBACCOUNT_LABEL = "findasoccerbet"

_keys: dict = {}


class NovigError(RuntimeError):
    pass


def _load_pem(path: str, env_var: str):
    if not os.path.exists(path):
        pem = os.environ.get(env_var)
        if not pem:
            raise NovigError(f"Missing {path} and {env_var} is not set")
        os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
        with open(path, "w") as f:
            f.write(pem.strip() + "\n")
        os.chmod(path, 0o600)
    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), None)


def _sign(private_key, text: bytes) -> bytes:
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return private_key.sign(text)
    return private_key.sign(text, ec.ECDSA(hashes.SHA256()))


def management_key() -> tuple[str, object]:
    if "mgmt" not in _keys:
        key_id = os.environ.get("NOVIG_KEY_ID")
        if not key_id:
            raise NovigError("NOVIG_KEY_ID is not set")
        _keys["mgmt"] = (key_id, _load_pem(MGMT_PEM, "NOVIG_PRIVATE_KEY_PEM"))
    return _keys["mgmt"]


def trading_key() -> tuple[str, object]:
    if "trading" not in _keys:
        sub = find_subaccount()
        if sub is None:
            raise NovigError(f"No subaccount labelled {SUBACCOUNT_LABEL!r} -- open one first")
        _keys["trading"] = (sub["keyId"], _load_pem(TRADING_PEM, "NOVIG_TRADING_PRIVATE_KEY_PEM"))
    return _keys["trading"]


def call(key: tuple[str, object], method: str, path: str, params: dict | None = None, body: dict | None = None):
    """Signs and sends one request. The query must be canonical (sorted
    by name, percent-encoded) because it's part of the signed text."""
    key_id, private_key = key
    query = urllib.parse.urlencode(sorted((params or {}).items()), quote_via=urllib.parse.quote)
    data = b"" if body is None else json.dumps(body).encode()
    ts = str(int(time.time() * 1000))
    text = "\n".join(["NOVIG-V3", ts, method, path, query, hashlib.sha256(data).hexdigest()])
    signature = base64.b64encode(_sign(private_key, text.encode())).decode()
    url = HOST + path + (f"?{query}" if query else "")
    response = requests.request(method, url, data=data or None, timeout=20, headers={
        "Novig-Key-Id": key_id,
        "Novig-Timestamp": ts,
        "Novig-Signature": signature,
        "Content-Type": "application/json",
    })
    if response.status_code >= 400:
        raise NovigError(f"{method} {path} failed: {response.status_code} -- {response.text[:500]}")
    return response.json() if response.content else {}


def snap_price(price: float | str) -> str:
    """Rounds a price DOWN onto Novig's grid (every order buys, so down
    never pays more than intended)."""
    milli = int(Decimal(str(price)) * 1000)
    if not 1 <= milli <= 999:
        raise ValueError(f"price {price} is outside 0.001-0.999")
    if 51 <= milli <= 949:
        milli -= milli % 5
    return f"{milli / 1000:.3f}"


def echo() -> dict:
    return call(management_key(), "POST", "/v3/echo", body={"hello": "novig"})


def list_subaccounts() -> list:
    return call(management_key(), "GET", "/v3/account/subaccounts")


def find_subaccount() -> dict | None:
    return next((s for s in list_subaccounts() if s.get("label") == SUBACCOUNT_LABEL), None)


def account_balance() -> dict:
    return call(management_key(), "GET", "/v3/account/balance")


def subaccount_balance() -> dict:
    key_id, _ = trading_key()
    return call(management_key(), "GET", f"/v3/account/subaccounts/{key_id}/balance")


def fund_subaccount(amount: str, direction: str = "fund") -> dict:
    """Moves money between the main account and our subaccount. MOVES
    REAL MONEY -- only after Ciaran approves the amount. A 202 means
    accepted, not applied; check the transfer status before trading."""
    key_id, _ = trading_key()
    return call(management_key(), "POST", f"/v3/account/subaccounts/{key_id}/transfer", body={
        "direction": direction, "amount": f"{Decimal(amount):.5f}",
        "clientTransferId": f"fasb-{uuid.uuid4()}",
    })


def get_book(market_id: str) -> dict:
    return call(trading_key(), "GET", f"/v3/catalog/markets/{market_id}/book")


def open_orders() -> dict:
    return call(trading_key(), "GET", "/v3/orders")


def positions() -> dict:
    return call(trading_key(), "GET", "/v3/portfolio/positions")


def place_order(outcome_id: str, price: float | str, qty: int, tif: str = "IOC", client_id: str | None = None) -> dict:
    """Places a REAL order. Nothing calls this automatically -- only run
    it after Ciaran approves the exact order list.

    outcome_id: the outcome you're BUYING (e.g. the "Over 2.5" outcome),
        never the market ID.
    qty: contracts, each paying 1 cent -- $5 at 0.60 is ~833 contracts.
    tif: IOC by default so an unfilled remainder doesn't rest on the
        book; every resting order is voided at kickoff anyway.
    """
    if qty <= 0 or int(qty) != qty:
        raise ValueError("qty must be a positive whole number of contracts")
    body = {"outcomeId": outcome_id, "price": snap_price(price), "qty": int(qty), "tif": tif}
    if client_id:
        body["clientId"] = client_id
    return call(trading_key(), "POST", "/v3/orders", body=body)


def cancel_order(order_id: str) -> dict:
    return call(trading_key(), "DELETE", f"/v3/orders/{order_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("echo")
    sub.add_parser("balance")
    sub.add_parser("subaccounts")
    sub.add_parser("orders")
    sub.add_parser("positions")
    book = sub.add_parser("book")
    book.add_argument("--market-id", required=True)
    fund = sub.add_parser("fund", help="Moves REAL money into the subaccount")
    fund.add_argument("--amount", required=True)
    defund = sub.add_parser("defund", help="Moves money back to the main account")
    defund.add_argument("--amount", required=True)
    place = sub.add_parser("place", help="Places a REAL order -- double check every argument")
    place.add_argument("--outcome-id", required=True)
    place.add_argument("--price", required=True)
    place.add_argument("--qty", required=True, type=int)
    place.add_argument("--tif", default="IOC", choices=["GTC", "IOC", "FOK"])
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)
    args = parser.parse_args()

    try:
        if args.cmd == "echo":
            result = echo()
        elif args.cmd == "balance":
            result = {"account": account_balance(), "subaccount": subaccount_balance()}
        elif args.cmd == "subaccounts":
            result = list_subaccounts()
        elif args.cmd == "orders":
            result = open_orders()
        elif args.cmd == "positions":
            result = positions()
        elif args.cmd == "book":
            result = get_book(args.market_id)
        elif args.cmd in ("fund", "defund"):
            result = fund_subaccount(args.amount, args.cmd)
        elif args.cmd == "place":
            price = snap_price(args.price)
            cost = Decimal(price) * args.qty / 100
            print(f"Placing REAL order: {args.qty} contracts @ {price} on outcome {args.outcome_id} "
                  f"({args.tif}), cost ${cost:.2f}, pays ${Decimal(args.qty) / 100:.2f} if it wins")
            result = place_order(args.outcome_id, price, args.qty, tif=args.tif)
        elif args.cmd == "cancel":
            result = cancel_order(args.order_id)
        print(json.dumps(result, indent=2))
    except NovigError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
