#!/usr/bin/env python3
"""Public Over/Under 2.5 goals prices from Polymarket, as a second book
alongside Kalshi (live_kalshi_edge_test.py / forward_test_log.py's
KALSHI_SERIES_BY_COMPETITION). Market data is PUBLIC -- no wallet or API
key needed to read prices; only placing an order would require a funded
Polygon wallet, which is a completely different credential model from
Kalshi's RSA-signed requests and is NOT implemented here.

Polymarket's Gamma API doesn't expose a clean "all upcoming fixtures for
league X" filter the way Kalshi's /events?series_ticker= does -- most
query params on /events are silently ignored rather than erroring, which
looks like a working filter (still 200 OK) but just returns the
unfiltered default listing. `tag_slug` is the one filter param that
actually works (confirmed against a known fixture's own tags field).

Each fixture is actually TWO sibling events under Gamma: a base event
(moneyline only -- home win / draw / away win) and a second event whose
slug is exactly f"{base_slug}-more-markets", which bundles totals,
spreads, BTTS, player props, etc. The Over/Under 2.5 goals market lives
in the "-more-markets" event, identified by groupItemTitle == "O/U 2.5"
(there are separate O/U markets at other lines -- 0.5/1.5/3.5/4.5/5.5 --
and separate ones scoped to a single team or to a half, so matching on
the exact groupItemTitle string matters).

Team names come back as Polymarket's own full/official names (e.g. "AFC
Bournemouth", "Bayer 04 Leverkusen", "FC Internazionale Milano"), which
differ from both our dataset's names and Kalshi's own truncated ones --
a separate normalizer from live_kalshi_edge_test.py's _normalize() to
avoid disturbing that already-tuned Kalshi matching. Matching here is
deliberately conservative: substring containment after stripping a
short list of noise tokens, plus a small table of confirmed real
aliases (_POLY_ALIASES) for the cases substring matching structurally
can't reach -- e.g. "Rennes" vs "Stade Rennais FC 1901", where
"rennais" is the demonym form of "Rennes", not a substring of it.
Anything else that doesn't confidently match is silently skipped
rather than guessed at -- a missing Polymarket price is a safe failure
mode, a wrong one attached to the wrong fixture is not.

Usage:
    python3 polymarket_prices.py PL
"""

import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import json

GAMMA_BASE = "https://gamma-api.polymarket.com"
# The default Python-urllib User-Agent gets a flat 403 from Polymarket's
# CDN -- any normal-looking one works, this isn't about mimicking a real
# browser, just not looking like a bare script.
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_MAX_RETRIES = 4

# Confirmed by fetching one known current fixture per league and reading
# its own `tags` field back (see module docstring) -- these are NOT
# guessed from league names, several don't follow the obvious pattern
# ("sea" for Serie A, "ere" for Eredivisie, "tur" for Super Lig).
POLYMARKET_TAG_BY_COMPETITION = {
    "PL": "EPL",
    "ELC": "efl-championship",
    "LALIGA": "la-liga",
    "BUNDESLIGA": "bundesliga",
    "SERIEA": "sea",
    "LIGUE1": "ligue-1",
    "MLS": "mls",
    "EREDIVISIE": "ere",
    "SUPERLIG": "tur",
    "BRASILEIRAO": "brazil-serie-a",
    "LIGAPORTUGAL": "primeira-liga",
    "JLEAGUE": "japan-j-league",
}

# Whole-word noise tokens stripped before matching -- club-type
# abbreviations and the stray founding-year/numeric tokens Polymarket
# keeps in official names (e.g. "Bayer 04 Leverkusen", "Stade Rennais FC
# 1901") that would otherwise break a substring match against our
# shorter names. Deliberately short: "as", "de", "club" etc. are left
# alone because substring containment already handles those directions
# fine (see docstring), and stripping more only raises collision risk.
_POLY_NOISE_TOKENS = {"fc", "afc", "cf", "cd", "sc", "ud"}

# Confirmed real equivalences that plain substring containment can't
# bridge on its own -- the adjectival/demonym form of a city or region
# name (Rennais -> Rennes, same relationship as "Parisian" -> "Paris")
# isn't a substring of the name it's derived from, so these need to be
# named explicitly rather than caught by the generic rule. Keyed by the
# post-noise-stripping normalized Polymarket name; add to this table
# only for a confirmed real match, never to force a guess through.
_POLY_ALIASES = {
    "stade rennais": "rennes",
}


def _poly_normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower().strip().replace("-", " ")
    tokens = [t for t in re.split(r"\s+", n) if t not in _POLY_NOISE_TOKENS and not t.isdigit()]
    normalized = " ".join(tokens)
    return _POLY_ALIASES.get(normalized, normalized)


def _poly_names_match(our_name: str, poly_name: str) -> bool:
    a, b = _poly_normalize(our_name), _poly_normalize(poly_name)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def polymarket_fee(price: float) -> float:
    """Polymarket's disclosed sports-market fee schedule (feeType
    "sports_fees_v3", rate=0.05, exponent=1, takerOnly=True -- read
    directly off a live market's own feeSchedule field, e.g. the
    Man City vs Sunderland O/U 2.5 market on 2026-09-16): fee = rate *
    price * (1 - price), charged on the taker side of entry. Same
    quadratic shape as kalshi_fee() (rate=0.07 there), just without the
    cent-rounding step -- Polymarket settles in USDC, not whole cents.
    Not verified against a live authenticated order preview (no wallet
    configured), so treat this the same way kalshi_fee() treats its own
    number: the exchange's stated schedule applied as a modeling
    assumption, not a confirmed fill price.
    """
    return 0.05 * price * (1 - price)


def _get(path: str, params: dict | None = None) -> dict | list:
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{GAMMA_BASE}{path}"
    if query:
        url += f"?{query}"
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 403) or attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2**attempt)
    raise last_exc


def _is_base_fixture_event(event: dict) -> bool:
    """Base fixture events are titled exactly "Team A vs. Team B" --
    every sibling sub-event (the ones ending in "- More Markets", "-
    Total Corners", "- Exact Score", etc.) has a " - " suffix appended
    to that same title, which is what this excludes."""
    title = event.get("title", "")
    return " vs. " in title and " - " not in title


def fetch_polymarket_over25_for_league(competition: str) -> list[dict]:
    """[{home, away, ticker, yes_ask, yes_bid, no_ask, no_bid, fair_p}, ...]
    for every open fixture in this competition that has a matching O/U
    2.5 market -- same dict shape as
    live_kalshi_edge_test.fetch_kxepltotal_over25() /
    forward_test_log.fetch_kalshi_over25_for_series() so callers can
    treat this as a second, drop-in price source rather than special-
    casing it. `ticker` here is the Polymarket "-more-markets" event
    slug, not a real Kalshi-style ticker -- kept under the same key
    purely so existing merge/report code doesn't need a second field
    name.
    """
    tag_slug = POLYMARKET_TAG_BY_COMPETITION.get(competition)
    if tag_slug is None:
        return []

    base_events = []
    offset = 0
    while True:
        page = _get("/events", {
            "tag_slug": tag_slug, "closed": "false", "limit": 50, "offset": offset,
            "order": "startDate", "ascending": "true",
        })
        if not page:
            break
        base_events.extend(e for e in page if _is_base_fixture_event(e))
        if len(page) < 50:
            break
        offset += 50

    out = []
    for e in base_events:
        more_slug = f"{e['slug']}-more-markets"
        sibling = _get("/events", {"slug": more_slug})
        if not sibling:
            continue
        ou25 = next((m for m in sibling[0].get("markets", []) if m.get("groupItemTitle") == "O/U 2.5"), None)
        if ou25 is None or ou25.get("bestAsk") is None:
            continue
        home, _, away = e["title"].partition(" vs. ")
        yes_ask, yes_bid = float(ou25["bestAsk"]), float(ou25["bestBid"])
        out.append({
            "home": home.strip(), "away": away.strip(), "ticker": more_slug,
            "yes_ask": yes_ask, "yes_bid": yes_bid,
            "no_ask": round(1 - yes_bid, 4), "no_bid": round(1 - yes_ask, 4),
            "fair_p": round((yes_ask + yes_bid) / 2, 4),
        })
    return out


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in POLYMARKET_TAG_BY_COMPETITION:
        print(f"Usage: python3 polymarket_prices.py <competition>  -- one of {list(POLYMARKET_TAG_BY_COMPETITION)}")
        return 1
    for m in fetch_polymarket_over25_for_league(sys.argv[1]):
        print(f"{m['home']:28s} vs {m['away']:28s} yes_ask={m['yes_ask']:.3f}  yes_bid={m['yes_bid']:.3f}  fair={m['fair_p']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
