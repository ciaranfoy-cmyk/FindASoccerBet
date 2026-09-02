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
import urllib.request
import warnings
import json

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
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


def _normalize(name: str) -> str:
    n = name.lower().strip()
    for suf in _SUFFIXES:
        if n.endswith(suf) and n != suf.strip():
            n = n[: -len(suf)]
    return n.strip()


def kalshi_get(path: str, params: dict | None = None) -> dict:
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{KALSHI_BASE}{path}"
    if query:
        url += f"?{query}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read())


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
    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        live_df.loc[~has_xg, "pred_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        live_df.loc[has_xg, "pred_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
    live_df["pred_p"] = live_df["pred_p"].astype(float)

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
    print(f"{'Date':<12}{'Fixture':<38}{'Model P':<10}{'Kalshi ask':<12}{'Kalshi bid':<12}{'Edge vs ask'}")
    print("-" * 100)
    for _, r in out.iterrows():
        fixture = f"{r['home']} vs {r['away']}"
        print(f"{r['date']:<12}{fixture:<38}{r['model_p']*100:5.1f}%   "
              f"${r['yes_ask']:.2f}       ${r['yes_bid']:.2f}       {r['edge_vs_ask']*100:+.1f}pp")

    positive = out[out["edge_vs_ask"] > 0]
    print(f"\n{len(positive)}/{len(out)} matched fixtures show a positive edge buying YES at Kalshi's ask price.")
    print("Remember the calibration finding: the model is well-behaved around 45-60% predicted "
          "probability but overconfident above ~65% -- discount any high-confidence edge here accordingly. "
          "This reads real, current, actually-tradeable Kalshi prices; it does not place any order.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
