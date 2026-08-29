#!/usr/bin/env python3
"""Full-season walk-forward backtest of the recommended betting strategy:
each calendar week, freeze the model and team state at that week's first
kickoff (using only data genuinely available beforehand), score every
PL + Championship fixture that week, and bet ONLY the single highest-
ranked fixture if its predicted P(over 2.5) clears the 60% confidence bar
established as the selective threshold — otherwise skip the week
entirely. Weeks early in the season are naturally skipped wherever a
fixture is missing a required feature (season-to-date goals, table
position) because no games have been played yet in that competition's
new season — this is what excludes "gameweek 1" without hardcoding it.

Model is retrained fresh each week on all history strictly before that
week (same no-lookahead discipline as backtest_gameweek.py and
rolling_validation.py). Team state is advanced incrementally week to
week (not replayed from scratch every time) for speed.

Usage:
    APIFOOTBALL_KEY=xxxx python3 backtest_season.py --season 2025
    APIFOOTBALL_KEY=xxxx python3 backtest_season.py --season 2025 --threshold 0.55
"""

import argparse
import datetime
import warnings
from collections import defaultdict

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import ALL_CANDIDATES, add_derived_features, load
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from predict_upcoming import apply_match, build_feature_row, new_state

warnings.filterwarnings("ignore")

STAKE = 100.0
PROFIT_ON_WIN = 90.0  # "$90 per $100 bet on a winner" => decimal odds 1.90


def parse_dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=2025, help="Season start-year, e.g. 2025 = 2025-26")
    parser.add_argument("--threshold", type=float, default=0.60, help="Only bet if top pick's predicted P >= this")
    args = parser.parse_args()

    print(f"Fetching season {args.season} fixtures for {list(LEAGUES)}...")
    season_matches = []
    for code, info in LEAGUES.items():
        data = apifootball.get("/fixtures", {"league": info["id"], "season": args.season})
        for m in data["response"]:
            if m["fixture"]["status"]["short"] != "FT":
                continue
            season_matches.append({
                "fixture_id": m["fixture"]["id"],
                "date": m["fixture"]["date"],
                "competition": code,
                "season": args.season,
                "home": m["teams"]["home"]["name"],
                "away": m["teams"]["away"]["name"],
                "home_id": m["teams"]["home"]["id"],
                "away_id": m["teams"]["away"]["id"],
                "home_goals": m["goals"]["home"],
                "away_goals": m["goals"]["away"],
            })
    season_matches.sort(key=lambda m: m["date"])
    print(f"  {len(season_matches)} finished fixtures across both competitions")

    season_start = parse_dt(season_matches[0]["date"])

    print("Fetching full fixture history (cached)...")
    all_finished = fetch_all_fixtures(None)
    prior_history = [m for m in all_finished if parse_dt(m["date"]) < season_start]
    print(f"Building pre-season state from {len(prior_history)} prior matches (one-time replay)...")
    state = new_state()
    for i, m in enumerate(prior_history, start=1):
        apply_match(state, m)
        if i % 2000 == 0:
            print(f"  ...replayed {i}/{len(prior_history)}")

    print("Loading historical dataset for weekly model retraining...")
    historical = load()

    # Group this season's matches into calendar weeks, chronological.
    df = pd.DataFrame(season_matches)
    df["dt"] = df["date"].apply(parse_dt)
    df["week"] = df["dt"].dt.tz_localize(None).dt.to_period("W")
    weeks = sorted(df["week"].unique())

    report_rows = []
    bankroll = 0.0
    bets = 0
    wins = 0

    for week in weeks:
        week_matches = [season_matches[i] for i in df.index[df["week"] == week]]
        week_matches.sort(key=lambda m: m["date"])
        cutoff = parse_dt(week_matches[0]["date"])
        cutoff_ts = pd.Timestamp(cutoff.date())

        model_df = historical[historical["date"] < cutoff_ts]
        model_df = model_df[ALL_CANDIDATES + ["over_2_5"]].dropna()

        scaler = StandardScaler()
        X_train = scaler.fit_transform(model_df[ALL_CANDIDATES])
        model = LogisticRegressionCV(
            Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
        )
        model.fit(X_train, model_df["over_2_5"])

        rows = []
        for m in week_matches:
            row = build_feature_row(m, state)
            if row is None:
                continue
            row["actual_home_goals"] = m["home_goals"]
            row["actual_away_goals"] = m["away_goals"]
            rows.append(row)

        pick_line = None
        if rows:
            live_df = pd.DataFrame(rows)
            live_df = add_derived_features(live_df)
            scoreable = live_df.dropna(subset=ALL_CANDIDATES).copy()
            if not scoreable.empty:
                X_live = scaler.transform(scoreable[ALL_CANDIDATES])
                scoreable["pred_p"] = model.predict_proba(X_live)[:, 1]
                scoreable = scoreable.sort_values("pred_p", ascending=False)
                top = scoreable.iloc[0]

                actual_total = top["actual_home_goals"] + top["actual_away_goals"]
                actual_over = actual_total > 2.5

                if top["pred_p"] >= args.threshold:
                    bets += 1
                    if actual_over:
                        wins += 1
                        bankroll += PROFIT_ON_WIN
                        outcome = "WIN"
                        pnl = f"+${PROFIT_ON_WIN:.0f}"
                    else:
                        bankroll -= STAKE
                        outcome = "LOSS"
                        pnl = f"-${STAKE:.0f}"
                    pick_line = {
                        "week_start": week.start_time.date(),
                        "competition": top["competition"],
                        "home": top["home_team"],
                        "away": top["away_team"],
                        "pred_p": top["pred_p"],
                        "actual_score": f"{int(top['actual_home_goals'])}-{int(top['actual_away_goals'])}",
                        "actual_over": actual_over,
                        "bet": True,
                        "outcome": outcome,
                        "pnl": pnl,
                        "bankroll": bankroll,
                    }
                else:
                    pick_line = {
                        "week_start": week.start_time.date(),
                        "competition": top["competition"],
                        "home": top["home_team"],
                        "away": top["away_team"],
                        "pred_p": top["pred_p"],
                        "actual_score": f"{int(top['actual_home_goals'])}-{int(top['actual_away_goals'])}",
                        "actual_over": actual_over,
                        "bet": False,
                        "outcome": "no bet (best pick below threshold)",
                        "pnl": "$0",
                        "bankroll": bankroll,
                    }

        # Advance state with this week's actual results, for next week's cutoff.
        for m in week_matches:
            apply_match(state, m)

        if pick_line:
            report_rows.append(pick_line)
        else:
            report_rows.append({
                "week_start": week.start_time.date(), "competition": "-", "home": "-", "away": "-",
                "pred_p": None, "actual_score": "-", "actual_over": None,
                "bet": False, "outcome": "no scoreable fixtures (season just started)", "pnl": "$0",
                "bankroll": bankroll,
            })

    print("\n" + "=" * 100)
    print(f"Full season {args.season}-{args.season+1-2000} walk-forward backtest — "
          f"bet only the week's top pick when P(over 2.5) >= {args.threshold*100:.0f}%")
    print("=" * 100)
    for r in report_rows:
        if r["pred_p"] is None:
            print(f"{r['week_start']}  {r['outcome']}")
            continue
        conf = f"{r['pred_p']*100:.1f}%"
        actual = f"{r['actual_score']} ({'OVER' if r['actual_over'] else 'under'} 2.5)"
        print(f"{r['week_start']}  [{r['competition']}] {r['home']:<20s} vs {r['away']:<20s}  "
              f"P={conf:<7s} actual={actual:<20s} {r['outcome']:<35s} {r['pnl']:>6s}  bankroll={r['bankroll']:+.0f}")

    total_staked = bets * STAKE
    print("\n" + "-" * 100)
    print(f"Bets placed: {bets}   Wins: {wins}   Losses: {bets - wins}")
    if bets:
        print(f"Hit rate: {wins}/{bets} = {wins/bets*100:.1f}%")
        print(f"Total staked: ${total_staked:.0f}")
        print(f"Total P&L: ${bankroll:+.0f}")
        print(f"ROI: {bankroll/total_staked*100:+.1f}%")
        breakeven = STAKE / (STAKE + PROFIT_ON_WIN)
        print(f"Breakeven hit rate at these odds: {breakeven*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
