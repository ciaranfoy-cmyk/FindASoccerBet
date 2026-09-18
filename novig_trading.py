#!/usr/bin/env python3
"""Authenticated Novig (NBX) trading client. Novig covers EPL and MLS
(confirmed via GET /nbx/v2/emm/event-metadata/leagues -- both leagues
appear in the docs' own example response) alongside US sports, with a
TOTAL market type that's a direct Over/Under-2.5-goals equivalent to
what Kalshi and Polymarket US already price.

Auth is OAuth 2.0 Client Credentials (JWT bearer tokens, 30-minute
expiry). Novig's own docs disagree with themselves on the token
endpoint: the plain-prose Authentication page says
POST https://api.novig.com/nbx/v1/auth/emm-token, but the OpenAPI spec
embedded in literally every other endpoint's page (get-leagues,
get-events, place-order, etc. -- checked several, all identical) says
POST https://auth.novig.us/oauth/token with an "audience" field. This
client uses the OpenAPI version as primary since it's the one repeated
everywhere, but this is a real discrepancy in Novig's own docs, not a
guess resolved with confidence -- if auth fails with the current
AUTH_URL, try the other one before assuming credentials are wrong.

Credentials are read from secrets/novig.env (NOVIG_CLIENT_ID,
NOVIG_CLIENT_SECRET) -- gitignored, never committed, same pattern as
secrets/kalshi.env and secrets/polymarket.env.

CURRENCY WARNING: every balance/market/order call takes a required
`currency` of "CASH" or "COIN". Novig's docs describe COIN as
"1 unit = 1 Novig Coin" and CASH as "1 unit = 0.01 Novig Cash" but
never say outright which one is real, withdrawable money versus a
virtual/promotional balance -- this is the same CASH/Coin split many
US social-sports-betting apps use, where one side is real money and
the other is play-money or sweepstakes credit. Nothing in this file
defaults `currency` to anything -- every call requires it explicitly --
specifically so a script can't silently place a real-money order
because a default happened to point at CASH. Confirm which currency is
real money in your own Novig account before running `place` for real.

This module deliberately keeps order placement as an explicit, separate
call (place_order) that is never invoked automatically -- nothing in
this file fires a live trade on import or on module load.

UNTESTED: no call in this module has been run against the live API --
written directly from docs.novig.com's OpenAPI pages, not confirmed
live. Run `leagues` first (cheapest possible read) and see what comes
back before trusting anything else here.

Usage (read-only, safe -- but still unverified, see above):
    python3 novig_trading.py leagues
    python3 novig_trading.py events --league EPL --status OPEN_PREGAME
    python3 novig_trading.py markets --event-id <uuid> --currency CASH
    python3 novig_trading.py balance --currency CASH

Usage (places a REAL order -- confirm every field before running):
    python3 novig_trading.py place --outcome-id <uuid> --price 0.667 --qty 100 --currency CASH
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SECRETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets")
ENV_PATH = os.path.join(SECRETS_DIR, "novig.env")

AUTH_URL = "https://auth.novig.us/oauth/token"  # see module docstring -- docs disagree with themselves
AUDIENCE = "https://api.novig.us"
API_BASE = "https://api.novig.com/nbx/v2"

_token_cache: dict = {"access_token": None, "expires_at": 0.0}


class NovigError(RuntimeError):
    pass


def _load_creds() -> tuple[str, str]:
    if not os.path.exists(ENV_PATH):
        raise NovigError(f"Missing {ENV_PATH} -- expected NOVIG_CLIENT_ID=... and NOVIG_CLIENT_SECRET=... in it.")
    client_id = client_secret = None
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("NOVIG_CLIENT_ID="):
                client_id = line.strip().split("=", 1)[1]
            elif line.startswith("NOVIG_CLIENT_SECRET="):
                client_secret = line.strip().split("=", 1)[1]
    if not client_id or not client_secret:
        raise NovigError("NOVIG_CLIENT_ID / NOVIG_CLIENT_SECRET not found in secrets/novig.env")
    return client_id, client_secret


def _fetch_token() -> str:
    client_id, client_secret = _load_creds()
    body = json.dumps({
        "audience": AUDIENCE,
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(
        AUTH_URL, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        raise NovigError(f"token request failed: {exc.code} {exc.reason} -- {error_body}") from exc
    token = data.get("access_token")
    if not token:
        raise NovigError(f"token response had no access_token: {data}")
    # Expiry documented as 30 minutes; refresh 60s early to avoid a
    # request landing right on the boundary.
    ttl = data.get("expires_in", 1800)
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = time.time() + ttl - 60
    return token


def _get_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]
    return _fetch_token()


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    token = _get_token()
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        raise NovigError(f"{method} {path} failed: {exc.code} {exc.reason} -- {error_body}") from exc


def get_leagues() -> list:
    return _request("GET", "/emm/event-metadata/leagues")


def get_events(league: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0) -> list:
    params = {"limit": limit, "offset": offset}
    if league:
        params["league"] = league
    if status:
        params["status"] = status
    return _request("GET", "/emm/events", params=params)


def get_event(event_id: str) -> dict:
    return _request("GET", f"/emm/events/{event_id}")


def get_markets_by_event(event_id: str, currency: str) -> list:
    if currency not in ("CASH", "COIN"):
        raise ValueError("currency must be CASH or COIN")
    return _request("GET", f"/emm/events/getMarketsByEvent/{event_id}", params={"currency": currency})


def get_wallet_balance(currency: str) -> dict:
    if currency not in ("CASH", "COIN"):
        raise ValueError("currency must be CASH or COIN")
    return _request("GET", "/emm/account/balance", params={"currency": currency})


def get_all_my_positions(limit: int = 100, offset: int = 0) -> dict:
    return _request("GET", "/emm/positions/all", params={"limit": limit, "offset": offset})


def get_all_my_orders(limit: int = 100, offset: int = 0) -> dict:
    return _request("GET", "/emm/orders/all", params={"limit": limit, "offset": offset})


def place_order(
    outcome_id: str, price: float, qty: int, currency: str,
    tif: str = "GTC", ttl: int | None = None, flags: str | None = None,
) -> dict:
    """Places a REAL order on the real Novig account. Nothing calls this
    automatically -- it only runs when explicitly invoked with a
    specific outcome/price/qty/currency, confirmed by the user.

    outcome_id: UUID of the outcome (from get_markets_by_event's
        market.outcomes[].id -- NOT the market ID)
    price: decimal probability, 0.001-0.999, up to 3 decimal places
    qty: positive integer in MINIMAL currency units -- for CASH,
        1 unit = $0.01 (qty=100 means $1.00); for COIN, 1 unit = 1 coin.
        Not dollars directly -- see currency warning in module docstring.
    currency: "CASH" or "COIN" -- required, never defaulted, see
        module docstring's CURRENCY WARNING
    tif: GTC, GTT, IOC, FOK, or PO (default GTC)
    """
    if currency not in ("CASH", "COIN"):
        raise ValueError("currency must be CASH or COIN")
    if not (0.001 <= price <= 0.999):
        raise ValueError("price must be between 0.001 and 0.999")
    if qty <= 0 or int(qty) != qty:
        raise ValueError("qty must be a positive integer of minimal currency units")

    body = {"outcomeId": outcome_id, "price": price, "qty": int(qty), "currency": currency, "tif": tif}
    if ttl is not None:
        body["ttl"] = ttl
    if flags is not None:
        body["flags"] = flags
    return _request("POST", "/emm/orders/place", body=body)


def cancel_order(order_id: str) -> dict:
    return _request("DELETE", f"/emm/orders/{order_id}")


def cancel_all_orders() -> dict:
    return _request("DELETE", "/emm/orders/all")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("leagues")

    events = sub.add_parser("events")
    events.add_argument("--league")
    events.add_argument("--status")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--offset", type=int, default=0)

    event = sub.add_parser("event")
    event.add_argument("--event-id", required=True)

    markets = sub.add_parser("markets")
    markets.add_argument("--event-id", required=True)
    markets.add_argument("--currency", required=True, choices=["CASH", "COIN"])

    balance = sub.add_parser("balance")
    balance.add_argument("--currency", required=True, choices=["CASH", "COIN"])

    sub.add_parser("positions")
    sub.add_parser("orders")

    place = sub.add_parser("place", help="Places a REAL order -- double check every argument")
    place.add_argument("--outcome-id", required=True)
    place.add_argument("--price", required=True, type=float)
    place.add_argument("--qty", required=True, type=int)
    place.add_argument("--currency", required=True, choices=["CASH", "COIN"])
    place.add_argument("--tif", default="GTC", choices=["GTC", "GTT", "IOC", "FOK", "PO"])

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)

    sub.add_parser("cancel-all")

    args = parser.parse_args()

    try:
        if args.cmd == "leagues":
            print(json.dumps(get_leagues(), indent=2))
        elif args.cmd == "events":
            print(json.dumps(get_events(args.league, args.status, args.limit, args.offset), indent=2))
        elif args.cmd == "event":
            print(json.dumps(get_event(args.event_id), indent=2))
        elif args.cmd == "markets":
            print(json.dumps(get_markets_by_event(args.event_id, args.currency), indent=2))
        elif args.cmd == "balance":
            print(json.dumps(get_wallet_balance(args.currency), indent=2))
        elif args.cmd == "positions":
            print(json.dumps(get_all_my_positions(), indent=2))
        elif args.cmd == "orders":
            print(json.dumps(get_all_my_orders(), indent=2))
        elif args.cmd == "place":
            print(f"Placing REAL order: {args.qty} units @ {args.price} on outcome {args.outcome_id} "
                  f"({args.currency}, {args.tif})")
            print(json.dumps(place_order(args.outcome_id, args.price, args.qty, args.currency, tif=args.tif), indent=2))
        elif args.cmd == "cancel":
            print(json.dumps(cancel_order(args.order_id), indent=2))
        elif args.cmd == "cancel-all":
            print(json.dumps(cancel_all_orders(), indent=2))
    except NovigError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
