#!/usr/bin/env python3
"""Over/Under 2.5 goals prices from Novig, the third book alongside
Kalshi and Polymarket US. Reads Novig's PUBLIC catalog
(GET /v3/public/catalog/markets) -- no key needed for prices; see
novig_trading.py for balances and orders.

Novig is an exchange where every order buys one outcome, and a market's
summary only shows each outcome's best BID. Buying Over right now means
matching the best resting Under bid, so:
    over ask  = 1 - best Under bid   (depth = that bid's qty)
    under ask = 1 - best Over bid
Contracts pay 1 cent each (not $1), so the qty fields are in cents of
payout: 300000 contracts at the ask is $3,000 of payout.

Fees: 0.06 * P * (1 - P), charged to the taker only while the match is
live (fee.charged == "WHEN_LIVE" on every game market checked
2026-10-26). A pregame fill pays nothing, so novig_fee() returns 0 for
those markets.

Each market's description is "<Away> @ <Home> t<line>" (e.g. "Fulham @
Aston Villa t2.5"). Novig's team names are short forms ("Man Utd",
"Nottm Forest", "PAD" for Paderborn) that differ from API-Football's, so
matching strips accents and club-type tokens, then uses substring
containment plus a table of confirmed aliases (_NOVIG_ALIASES, built by
listing every team in Novig's 2026-10-26 catalog against our names).
Anything that doesn't confidently match is skipped -- a missing price is
safe, a price on the wrong fixture is not.

Leagues not listed by Novig on 2026-10-26: ELC, Eredivisie, Süper Lig,
Brasileirão, Liga Portugal, J-League.

Usage:
    python3 novig_prices.py PL
"""

import re
import sys
import time
import unicodedata

import requests

HOST = "https://api.novig.com"
_MAX_RETRIES = 4

# Our competition code -> Novig's league name (from
# GET /v3/public/types/markets/live, 2026-10-26).
NOVIG_LEAGUE_BY_COMPETITION = {
    "PL": "EPL",
    "LALIGA": "La Liga",
    "BUNDESLIGA": "Bundesliga",
    "SERIEA": "Serie A",
    "LIGUE1": "Ligue 1",
    "MLS": "MLS",
    "LIGAMX": "Liga MX",
}

_NOISE_TOKENS = {"fc", "afc", "cf", "sc", "sco", "club", "fsv", "vfb", "vfl", "sv", "as", "ac", "1"}

# Normalized Novig name -> normalized form of our name, for the cases
# substring containment can't reach.
_NOVIG_ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "nottm forest": "nottingham forest",
    "bayern munich": "bayern munchen",
    "borussia m gladbach": "borussia monchengladbach",
    "pad": "paderborn",
    "celta de vigo": "celta vigo",
    "olympique lyonnais": "lyon",
    "brest": "stade brestois",
    "psg": "paris saint germain",
    "new york rb": "new york red bulls",
    "sj earthquakes": "san jose earthquakes",
    "sporting kc": "sporting kansas city",
    "pumas unam": "u n a m pumas",
}


def _normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    tokens = [t for t in n.split() if t not in _NOISE_TOKENS and not t.isdigit()]
    return " ".join(tokens)


def novig_names_match(our_name: str, novig_name: str) -> bool:
    a = _normalize(our_name)
    b = _normalize(novig_name)
    if b in _NOVIG_ALIASES:
        # An alias is an exact name, so no substring slack: "paris" (Paris
        # FC) must not match PSG's "paris saint germain".
        return a == _NOVIG_ALIASES[b]
    if not a or not b:
        return False
    return a == b or a in b or b in a


def novig_fee(price: float, charged: str = "WHEN_LIVE") -> float:
    """Per-$1-of-payout taker fee, comparable to kalshi_fee()/
    polymarket_fee(). Zero for a pregame fill on a WHEN_LIVE market."""
    if charged != "ALWAYS":
        return 0.0
    return 0.06 * price * (1 - price)


def _get(path: str, params: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = requests.get(HOST + path, params=params, timeout=20)
            if response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Novig {path} failed after {_MAX_RETRIES} tries: {last_exc}")


def fetch_novig_over25_for_league(competition: str) -> list[dict]:
    """[{home, away, ticker, yes_ask, yes_bid, no_ask, no_bid, fair_p,
    yes_ask_qty, no_ask_qty, over_outcome_id, under_outcome_id,
    starts_ts, fee_charged}, ...] -- same core shape as
    fetch_polymarket_over25_for_league(), so callers can treat it as one
    more drop-in price source. `ticker` is the Novig market ID."""
    league = NOVIG_LEAGUE_BY_COMPETITION.get(competition)
    if league is None:
        return []

    markets = []
    after = None
    while True:
        params = {"league": league, "marketType": "TOTAL", "limit": 100}
        if after:
            params["after"] = after
        page = _get("/v3/public/catalog/markets", params)
        markets.extend(page.get("items", []))
        after = page.get("next")
        if not after or not page.get("items"):
            break

    out = []
    for m in markets:
        if m.get("strike") != "2.5" or m.get("status") != "OPEN":
            continue
        fixture, _, _ = m["description"].rpartition(" t")
        away, sep, home = fixture.partition(" @ ")
        if not sep:
            continue
        outcomes = {o["name"]: o for o in m.get("outcomes", [])}
        over, under = outcomes.get("Over 2.5"), outcomes.get("Under 2.5")
        # Both sides need a bid: an ask on one side is the other side's bid.
        if not over or not under or not over.get("bid") or not under.get("bid"):
            continue
        over_bid, under_bid = float(over["bid"]["price"]), float(under["bid"]["price"])
        yes_ask, no_ask = round(1 - under_bid, 4), round(1 - over_bid, 4)
        out.append({
            "home": home.strip(), "away": away.strip(), "ticker": m["marketId"],
            "yes_ask": yes_ask, "yes_bid": over_bid,
            "no_ask": no_ask, "no_bid": under_bid,
            "fair_p": round((yes_ask + over_bid) / 2, 4),
            "yes_ask_qty": under["bid"].get("qty"), "no_ask_qty": over["bid"].get("qty"),
            "over_outcome_id": over["outcomeId"], "under_outcome_id": under["outcomeId"],
            "starts_ts": m.get("startsTs"), "fee_charged": (m.get("fee") or {}).get("charged", "WHEN_LIVE"),
        })
    return out


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in NOVIG_LEAGUE_BY_COMPETITION:
        print(f"Usage: python3 novig_prices.py <competition>  -- one of {list(NOVIG_LEAGUE_BY_COMPETITION)}")
        return 1
    for m in fetch_novig_over25_for_league(sys.argv[1]):
        print(f"{m['home']:24s} vs {m['away']:24s} over_ask={m['yes_ask']:.3f} (${(m['yes_ask_qty'] or 0) / 100:,.0f} deep)  "
              f"under_ask={m['no_ask']:.3f}  fair={m['fair_p']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
