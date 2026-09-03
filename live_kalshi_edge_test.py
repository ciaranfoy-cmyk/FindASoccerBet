#!/usr/bin/env python3
"""Real edge test against Kalshi (the exchange the user can actually bet
on), not a sportsbook. Kalshi's KXEPLTOTAL series lists "Over 2.5 goals"
contracts per EPL fixture; market data is PUBLIC (no API key needed to
read prices -- only placing an order requires an authenticated,
signed request from the user's own account, which this script does NOT
do). A Kalshi "yes" contract price IS the market's implied probability
directly (e.g. yes_ask $0.54 means the market wants $0.54 to pay out
$1.00 if true -- roughly a 54% implied probability, modulo the spread),
so no odds-to-probability conversion is needed the way it is for
decimal sportsbook odds.

Usage:
    APIFOOTBALL_KEY=xxxx python3 live_kalshi_edge_test.py
"""

import re
import sys
import time
import urllib.error
import urllib.request
import warnings
import json

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from calibration import apply_calibration, load_calibrators
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from analyze_xg_features import XG_RAW_FEATURES, XG_DERIVED_FEATURES, add_xg_derived_features
from build_dataset_apifootball import fetch_all_fixtures
from predict_upcoming import (
    CORE_CANDIDATES,
    XG_CANDIDATES,
    build_feature_row,
    fetch_upcoming_fixtures,
    replay_to_current_state,
)

warnings.filterwarnings("ignore")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi club names sometimes differ slightly from API-Football's (e.g.
# "Ipswich Town" vs "Ipswich", "Leeds United" vs "Leeds") -- normalize
# both sides to their shortest common form before matching.
_SUFFIXES = (" town", " united", " city", " fc")

# MLS and Eredivisie diverge from API-Football far more than the suffix
# rule below can fix -- Kalshi truncates to bare city/market names or odd
# abbreviations ("Los Angeles G"/"Los Angeles F" for Galaxy/LAFC, "GA
# Eagles" for Go Ahead Eagles) while API-Football uses full club names
# with PREFIXES ("FC Cincinnati", "Real Salt Lake", "PSV Eindhoven") the
# suffix rule can't touch, or compound suffixes ("Orlando City SC",
# "Sporting Kansas City") it only partially strips. Verified against every
# fixture in both leagues' currently-open Kalshi markets after a real
# fixture (Inter Miami vs Atlanta United) silently fell through as
# "unpriced" when a live $0.77 market actually existed for it.
#
# Checked first, as exact aliases, before the generic suffix rule -- which
# also wrongly mangles "New York City" and "Kansas City" (proper-noun
# "City" isn't a club-type suffix there) if left to fall through ungated;
# the self-mapping entries below exist purely to short-circuit that.
_TEAM_ALIASES = {
    # Pre-existing bug, not introduced by the additions below: the generic
    # suffix rule strips " city" from "Manchester City" and " united" from
    # "Manchester United" down to the same "manchester" -- a genuine
    # different-team collision, found while auditing the rest of this
    # table. Self-map both before the generic rule can touch them.
    "manchester city": "manchester city",
    "manchester united": "manchester united",
    "man city": "manchester city",
    "man united": "manchester united",
    "man utd": "manchester united",
    # MLS
    "atlanta united fc": "atlanta",
    "inter miami": "miami",
    "fc cincinnati": "cincinnati",
    "columbus crew": "columbus",
    "colorado rapids": "colorado",
    "houston dynamo": "houston",
    "orlando city sc": "orlando",
    "cf montreal": "montreal",
    "philadelphia union": "philadelphia",
    "fc dallas": "dallas",
    "sporting kansas city": "kansas city",
    "kansas city": "kansas city",
    "seattle sounders": "seattle",
    "new york rb": "new york red bulls",
    "new york red bulls": "new york red bulls",
    "new york city": "new york city",
    "new york city fc": "new york city",
    "san jose earthquakes": "san jose",
    "los angeles g": "la galaxy",
    "los angeles galaxy": "la galaxy",
    "los angeles f": "lafc",
    "los angeles fc": "lafc",
    "new england revolution": "new england",
    "real salt lake": "salt lake",
    "portland timbers": "portland",
    "minnesota united fc": "minnesota",
    "saint louis": "st louis",
    "st. louis city": "st louis",
    "st louis city": "st louis",
    "vancouver whitecaps": "vancouver",
    "nashville sc": "nashville",
    # Eredivisie
    "sparta rotterdam": "sparta",
    "pec zwolle": "zwolle",
    "nec nijmegen": "nijmegen",
    "ga eagles": "go ahead eagles",
    "go ahead eagles": "go ahead eagles",
    "psv eindhoven": "eindhoven",
    # Championship -- found these ALSO silently mismatching in the
    # original 6 leagues once actually checked, not just the 3 new ones.
    "west bromwich": "west brom",
    "sheffield united": "sheffield utd",
    # La Liga
    "bilbao": "athletic club",
    "athletic club": "athletic club",
    "atletico": "atletico madrid",
    "vallecano": "rayo vallecano",
    # Bundesliga -- every single open market mismatched before this fix
    # (9/9), since API-Football keeps the German prefix ("1. FC Köln",
    # "SC Freiburg", "Bayer Leverkusen") that Kalshi drops entirely.
    "1. fc köln": "fc köln",
    "vfb stuttgart": "stuttgart",
    "m´gladbach": "monchengladbach",
    "borussia monchengladbach": "monchengladbach",
    "borussia mönchengladbach": "monchengladbach",
    "leverkusen": "bayer leverkusen",
    "paderborn": "sc paderborn 07",
    "freiburg": "sc freiburg",
    "bremen": "werder bremen",
    "leipzig": "rb leipzig",
    "hoffenheim": "1899 hoffenheim",
    "dortmund": "borussia dortmund",
    "schalke": "fc schalke 04",
    "bayern münchen": "bayern munich",
    "hamburg": "hamburger sv",
    "mainz": "fsv mainz 05",
    "frankfurt": "eintracht frankfurt",
    "augsburg": "fc augsburg",
    "fc st. pauli": "st. pauli",
    "hertha berlin": "hertha",
    "fortuna dusseldorf": "dusseldorf",
    "spvgg greuther furth": "greuther furth",
    # Serie A
    "as roma": "roma",
    "parma calcio": "parma",
    "ac milan": "milan",
    # Ligue 1
    "paris saint germain": "psg",
    "stade brestois 29": "stade brest 29",
    "estac troyes": "troyes",
    "strasbourg alsace": "strasbourg",
    "stade rennais": "rennes",
    # Below this line: not verified against a live Kalshi market at the
    # time added (these teams hadn't had an open fixture yet) -- added on
    # the same demonstrated pattern (drop a generic club-type prefix/
    # suffix) as everything above, but unconfirmed until they trade.
    # Real Madrid, Real Betis, Real Sociedad need no alias at all -- Kalshi
    # uses the full name verbatim, same as API-Football, confirmed by the
    # user directly (they trade all three "Real ___" clubs unabbreviated).
    "1. fc heidenheim": "heidenheim",
    "fc heidenheim": "heidenheim",
    "arminia bielefeld": "bielefeld",
    "holstein kiel": "kiel",
    "sv darmstadt 98": "darmstadt",
    "vfl bochum": "bochum",
    "vfl wolfsburg": "wolfsburg",
    "hellas verona": "verona",
    "clermont foot": "clermont",
    "ado den haag": "den haag",
    "fc volendam": "volendam",
    "fortuna sittard": "sittard",
    "nac breda": "breda",
    "vvv venlo": "venlo",
    "az alkmaar": "alkmaar",
}


def _normalize(name: str) -> str:
    n = name.lower().strip()
    if n in _TEAM_ALIASES:
        return _TEAM_ALIASES[n]
    for suf in _SUFFIXES:
        if n.endswith(suf) and n != suf.strip():
            n = n[: -len(suf)]
    return n.strip()


_KALSHI_MAX_RETRIES = 5


def kalshi_get(path: str, params: dict | None = None) -> dict:
    """9 leagues now means up to ~9x sequential series/events/markets
    calls per run, which trips Kalshi's rate limit far more often than
    the original 6-league version ever did -- unlike apifootball.get(),
    this had no retry at all, so a single 429 killed an entire league's
    prices for the run. Retry with exponential backoff on 429s.
    """
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{KALSHI_BASE}{path}"
    if query:
        url += f"?{query}"
    last_exc: Exception | None = None
    for attempt in range(_KALSHI_MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 or attempt == _KALSHI_MAX_RETRIES - 1:
                raise
            time.sleep(2**attempt)
    raise last_exc


def fetch_kxepltotal_over25() -> list[dict]:
    """[{home, away, date_hint, yes_ask, yes_bid, mid, implied_p, ticker}, ...]
    for every open KXEPLTOTAL event's "Over 2.5" market."""
    events = kalshi_get("/events", {"series_ticker": "KXEPLTOTAL", "status": "open", "limit": 100}).get("events", [])
    out = []
    for e in events:
        title = e.get("title", "")
        m = re.match(r"(.+?) vs (.+?): Total Goals", title)
        if not m:
            continue
        home, away = m.group(1).strip(), m.group(2).strip()
        markets = kalshi_get("/markets", {"event_ticker": e["event_ticker"]}).get("markets", [])
        for mk in markets:
            if mk.get("floor_strike") != 2.5 or mk.get("strike_type") != "greater":
                continue
            yes_ask = mk.get("yes_ask_dollars")
            yes_bid = mk.get("yes_bid_dollars")
            if yes_ask is None or yes_bid is None:
                continue
            yes_ask, yes_bid = float(yes_ask), float(yes_bid)
            out.append({
                "home": home, "away": away,
                "ticker": mk["ticker"],
                "yes_ask": yes_ask, "yes_bid": yes_bid,
                "mid": (yes_ask + yes_bid) / 2,
            })
    return out


def main() -> int:
    print("Fetching Kalshi's live 'Over 2.5' EPL markets (public data, no auth)...")
    try:
        kalshi_markets = fetch_kxepltotal_over25()
    except Exception as exc:
        print(f"Could not reach Kalshi: {exc}", file=sys.stderr)
        return 1
    print(f"  {len(kalshi_markets)} open 'Over 2.5' markets found")
    for k in kalshi_markets:
        print(f"    {k['home']} vs {k['away']}  yes_bid=${k['yes_bid']:.2f}  yes_ask=${k['yes_ask']:.2f}  ({k['ticker']})")

    if not kalshi_markets:
        print("No open Kalshi markets to compare against.")
        return 0

    print("\nTraining the core model (player-form + venue-split shots)...")
    historical = load_with_player_form_and_shots_venue()
    model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X_train, model_df["over_2_5"])

    print("Training the xG-augmented model...")
    xg_historical = load_with_xg_player_form_and_shots_venue()
    xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
    xg_scaler = StandardScaler()
    X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

    print("\nFetching upcoming fixtures and scoring...")
    upcoming = [m for m in fetch_upcoming_fixtures(21) if m["competition"] == "PL"]
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)

    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            rows.append(row)
    if not rows:
        print("No fixtures had enough team history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_xg_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if live_df.empty:
        print("All fixtures were missing a required feature.")
        return 0

    has_xg = live_df[XG_RAW_FEATURES + XG_DERIVED_FEATURES].notna().all(axis=1)
    live_df["pred_p"] = pd.NA
    live_df["model_used"] = ""
    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        live_df.loc[~has_xg, "pred_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        live_df.loc[has_xg, "pred_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"
    live_df["pred_p"] = live_df["pred_p"].astype(float)

    calibrators = load_calibrators()
    live_df["pred_p"] = apply_calibration(live_df["pred_p"], live_df["model_used"], calibrators)

    print("\nMatching Kalshi markets to model predictions by team name...")
    matched = []
    for k in kalshi_markets:
        k_home, k_away = _normalize(k["home"]), _normalize(k["away"])
        hit = live_df[
            (live_df["home_team"].apply(_normalize) == k_home)
            & (live_df["away_team"].apply(_normalize) == k_away)
        ]
        if hit.empty:
            print(f"  No model prediction found for {k['home']} vs {k['away']} -- skipping")
            continue
        r = hit.iloc[0]
        matched.append({
            "date": r["date"], "home": k["home"], "away": k["away"],
            "model_p": r["pred_p"], "yes_ask": k["yes_ask"], "yes_bid": k["yes_bid"], "mid": k["mid"],
            "edge_vs_ask": r["pred_p"] - k["yes_ask"], "edge_vs_mid": r["pred_p"] - k["mid"],
        })

    if not matched:
        print("Nothing matched -- can't compare.")
        return 0

    out = pd.DataFrame(matched).sort_values("edge_vs_ask", ascending=False)
    print("\n" + "-" * 100)
    print(f"{'Date':<12}{'Fixture':<38}{'Model P (calibrated)':<22}{'Kalshi ask':<12}{'Kalshi bid':<12}{'Edge vs ask'}")
    print("-" * 100)
    for _, r in out.iterrows():
        fixture = f"{r['home']} vs {r['away']}"
        print(f"{r['date']:<12}{fixture:<38}{r['model_p']*100:5.1f}%                "
              f"${r['yes_ask']:.2f}       ${r['yes_bid']:.2f}       {r['edge_vs_ask']*100:+.1f}pp")

    positive = out[out["edge_vs_ask"] > 0]
    print(f"\n{len(positive)}/{len(out)} matched fixtures show a positive edge buying YES at Kalshi's ask price.")
    print("Model P here is Platt-calibrated (see calibration.py), not the raw model output -- the raw "
          "number runs overconfident, especially above ~65%, so this edge is the honest one, not inflated. "
          "This reads real, current, actually-tradeable Kalshi prices; it does not place any order.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
