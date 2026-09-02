#!/usr/bin/env python3
"""Simple, direct test: score the next PL gameweek with the live model,
pull REAL current market odds for "Over 2.5" from API-Football (only
available for upcoming/near-term fixtures, confirmed working -- see
2026-09-01 session notes), and check whether the model's edge (predicted
probability vs. market-implied probability) is actually there once real
prices are used, instead of any flat profit assumption.

Edge = model_p - market_implied_p, where market_implied_p = 1/best_odds
(the market's break-even probability at the best price available).
Positive edge = the model thinks this is more likely than the market's
best price implies -- a positive-EV bet if the model is right.

Usage:
    APIFOOTBALL_KEY=xxxx python3 live_odds_edge_test.py
    APIFOOTBALL_KEY=xxxx python3 live_odds_edge_test.py --days 10
"""

import argparse
import sys
import warnings

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
    apply_match,
    build_feature_row,
    fetch_upcoming_fixtures,
    new_state,
    replay_to_current_state,
)

warnings.filterwarnings("ignore")


def over_2_5_odds_for(fixture_id: int) -> list[float]:
    """All bookmakers' 'Over 2.5' decimal odds for this fixture, or [] if
    no odds are posted yet (only a near-term rolling window has odds --
    confirmed nothing older than a few days returns any)."""
    try:
        data = apifootball.get("/odds", {"fixture": fixture_id})
    except apifootball.ApiFootballError:
        return []
    odds = []
    for resp in data.get("response", []):
        for bm in resp.get("bookmakers", []):
            for bet in bm.get("bets", []):
                if bet["name"] != "Goals Over/Under":
                    continue
                for v in bet["values"]:
                    if v["value"] == "Over 2.5":
                        odds.append(float(v["odd"]))
    return odds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=10, help="Look at fixtures in the next N days, default 10")
    args = parser.parse_args()

    print("Training the core model (player-form + venue-split shots)...")
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

    print("\nFetching upcoming fixtures...")
    try:
        upcoming = fetch_upcoming_fixtures(args.days)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    upcoming = [m for m in upcoming if m["competition"] == "PL"]
    if not upcoming:
        print("No upcoming PL fixtures found in the window.")
        return 0

    print(f"Found {len(upcoming)} upcoming PL fixtures. Rebuilding current team state...")
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)

    print("\nScoring fixtures and pulling live odds...")
    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            row["fixture_id"] = m["fixture_id"]
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
        X_core = scaler.transform(core_rows[CORE_CANDIDATES])
        live_df.loc[~has_xg, "pred_p"] = model.predict_proba(X_core)[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        X_xg = xg_scaler.transform(xg_rows[XG_CANDIDATES])
        live_df.loc[has_xg, "pred_p"] = xg_model.predict_proba(X_xg)[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"
    live_df["pred_p"] = live_df["pred_p"].astype(float)

    results = []
    for _, r in live_df.iterrows():
        odds = over_2_5_odds_for(int(r["fixture_id"]))
        if not odds:
            results.append({**r.to_dict(), "n_books": 0, "best_odds": None, "avg_odds": None,
                             "implied_p": None, "edge": None})
            continue
        best = max(odds)
        avg = sum(odds) / len(odds)
        implied_p = 1.0 / best
        results.append({**r.to_dict(), "n_books": len(odds), "best_odds": best, "avg_odds": avg,
                         "implied_p": implied_p, "edge": r["pred_p"] - implied_p})

    out = pd.DataFrame(results)
    priced = out.dropna(subset=["edge"]).sort_values("edge", ascending=False)
    unpriced = out[out["best_odds"].isna()]

    print(f"\n{len(priced)}/{len(out)} fixtures had real market odds posted "
          f"(the rest are likely too far out -- odds only appear close to kickoff).\n")
    print("-" * 100)
    print(f"{'Date':<12}{'Fixture':<44}{'Model P':<10}{'Best odds':<11}{'Implied P':<11}{'Edge':<8}{'Books'}")
    print("-" * 100)
    for _, r in priced.iterrows():
        fixture = f"{r['home_team']} vs {r['away_team']}"
        print(f"{r['date']:<12}{fixture:<44}{r['pred_p']*100:5.1f}%   "
              f"{r['best_odds']:<11.2f}{r['implied_p']*100:5.1f}%      {r['edge']*100:+.1f}pp   {r['n_books']}")

    if not unpriced.empty:
        print(f"\n{len(unpriced)} fixture(s) with no odds posted yet (too far ahead of kickoff):")
        for _, r in unpriced.iterrows():
            print(f"  {r['date']}  {r['home_team']} vs {r['away_team']}  (model P={r['pred_p']*100:.1f}%)")

    if not priced.empty:
        positive_edge = priced[priced["edge"] > 0]
        print(f"\n{len(positive_edge)}/{len(priced)} priced fixtures show a positive edge "
              f"(model P > market-implied P at the best available price).")
        print("Reminder: this compares the model's OWN historical hit rate against a REAL market "
              "price for the first time this session -- a positive edge here means the model and "
              "the market disagree, not that the model is provably right; the market has its own "
              "track record worth respecting.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
