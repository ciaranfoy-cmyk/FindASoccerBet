#!/usr/bin/env python3
"""Full-season walk-forward backtest of a betting strategy: each calendar
week, freeze the model and team state at that week's first kickoff (using
only data genuinely available beforehand), score every PL + Championship
fixture that week, and bet the top --top-n ranked fixtures, each only if
its predicted P(over 2.5) clears --threshold. Default is top-1 @ 60%
(the selective single-pick strategy). Weeks early in the season are
naturally skipped wherever a fixture is missing a required feature
(season-to-date goals, table position) because no games have been played
yet in that competition's new season — this is what excludes "gameweek 1"
without hardcoding it.

Model is retrained fresh each week on all history strictly before that
week (same no-lookahead discipline as backtest_gameweek.py and
rolling_validation.py). Team state is advanced incrementally week to
week (not replayed from scratch every time) for speed.

Usage:
    APIFOOTBALL_KEY=xxxx python3 backtest_season.py --season 2025
    APIFOOTBALL_KEY=xxxx python3 backtest_season.py --season 2025 --threshold 0.55
    APIFOOTBALL_KEY=xxxx python3 backtest_season.py --season 2025 --top-n 5 --threshold 0
"""

import argparse
import datetime
import warnings
from collections import defaultdict

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import add_derived_features
from analyze_xg_features import add_xg_derived_features
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from predict_upcoming import (
    CORE_CANDIDATES,
    XG_CANDIDATES,
    apply_match,
    build_feature_row,
    new_state,
)
from analyze_player_form import add_player_form_derived_features, load_with_player_form, load_with_xg_and_player_form

MIN_XG_TRAIN_ROWS = 200  # don't bother fitting an xG model on too small a weekly training set

warnings.filterwarnings("ignore")

STAKE = 100.0
PROFIT_ON_WIN = 90.0  # "$90 per $100 bet on a winner" => decimal odds 1.90


def parse_dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=2025, help="Season start-year, e.g. 2025 = 2025-26")
    parser.add_argument("--threshold", type=float, default=0.60, help="Only bet a pick if its predicted P >= this")
    parser.add_argument("--top-n", type=int, default=1, help="Bet the top N ranked fixtures each week (each still gated by --threshold)")
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

    print("Loading historical dataset for weekly model retraining (player-form swapped in for team-goals-form)...")
    historical = load_with_player_form()
    xg_historical = load_with_xg_and_player_form()

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
        model_df = model_df[CORE_CANDIDATES + ["over_2_5"]].dropna()

        scaler = StandardScaler()
        X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
        model = LogisticRegressionCV(
            Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
        )
        model.fit(X_train, model_df["over_2_5"])

        # xG model, same week, same no-lookahead cutoff -- trained only if
        # enough real-xG-covered history exists strictly before this week.
        xg_model_df = xg_historical[xg_historical["date"] < cutoff_ts]
        xg_model_df = xg_model_df[XG_CANDIDATES + ["over_2_5"]].dropna()
        xg_scaler = xg_model = None
        if len(xg_model_df) >= MIN_XG_TRAIN_ROWS:
            xg_scaler = StandardScaler()
            X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
            xg_model = LogisticRegressionCV(
                Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
            )
            xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

        rows = []
        for m in week_matches:
            row = build_feature_row(m, state)
            if row is None:
                continue
            row["actual_home_goals"] = m["home_goals"]
            row["actual_away_goals"] = m["away_goals"]
            rows.append(row)

        week_picks = []
        if rows:
            live_df = pd.DataFrame(rows)
            live_df = add_derived_features(live_df)
            live_df = add_xg_derived_features(live_df)
            live_df = add_player_form_derived_features(live_df)
            scoreable = live_df.dropna(subset=CORE_CANDIDATES).copy()
            if not scoreable.empty:
                has_xg = pd.Series(False, index=scoreable.index)
                if xg_model is not None:
                    has_xg = scoreable[XG_CANDIDATES].notna().all(axis=1)

                scoreable["pred_p"] = pd.NA
                scoreable["model_used"] = "core"
                core_rows = scoreable.loc[~has_xg]
                if not core_rows.empty:
                    X_core = scaler.transform(core_rows[CORE_CANDIDATES])
                    scoreable.loc[~has_xg, "pred_p"] = model.predict_proba(X_core)[:, 1]
                xg_rows = scoreable.loc[has_xg]
                if not xg_rows.empty:
                    X_xg = xg_scaler.transform(xg_rows[XG_CANDIDATES])
                    scoreable.loc[has_xg, "pred_p"] = xg_model.predict_proba(X_xg)[:, 1]
                    scoreable.loc[has_xg, "model_used"] = "xG"
                scoreable["pred_p"] = scoreable["pred_p"].astype(float)
                scoreable = scoreable.sort_values("pred_p", ascending=False)

                for rank, (_, cand) in enumerate(scoreable.head(args.top_n).iterrows(), start=1):
                    actual_total = cand["actual_home_goals"] + cand["actual_away_goals"]
                    actual_over = actual_total > 2.5
                    line = {
                        "week_start": week.start_time.date(),
                        "rank": rank,
                        "competition": cand["competition"],
                        "home": cand["home_team"],
                        "away": cand["away_team"],
                        "pred_p": cand["pred_p"],
                        "model_used": cand["model_used"],
                        "actual_score": f"{int(cand['actual_home_goals'])}-{int(cand['actual_away_goals'])}",
                        "actual_over": actual_over,
                    }
                    if cand["pred_p"] >= args.threshold:
                        bets += 1
                        if actual_over:
                            wins += 1
                            bankroll += PROFIT_ON_WIN
                            line["outcome"] = "WIN"
                            line["pnl"] = f"+${PROFIT_ON_WIN:.0f}"
                        else:
                            bankroll -= STAKE
                            line["outcome"] = "LOSS"
                            line["pnl"] = f"-${STAKE:.0f}"
                    else:
                        line["outcome"] = "no bet (below threshold)"
                        line["pnl"] = "$0"
                    line["bankroll"] = bankroll
                    week_picks.append(line)

        # Advance state with this week's actual results, for next week's cutoff.
        for m in week_matches:
            apply_match(state, m)

        if week_picks:
            report_rows.extend(week_picks)
        else:
            report_rows.append({
                "week_start": week.start_time.date(), "rank": None, "competition": "-", "home": "-", "away": "-",
                "pred_p": None, "model_used": "-", "actual_score": "-", "actual_over": None,
                "outcome": "no scoreable fixtures (season just started)", "pnl": "$0",
                "bankroll": bankroll,
            })

    print("\n" + "=" * 100)
    print(f"Full season {args.season}-{args.season+1-2000} walk-forward backtest (hybrid xG/core model) — "
          f"bet the top {args.top_n} ranked pick(s)/week when P(over 2.5) >= {args.threshold*100:.0f}%")
    print("=" * 100)
    for r in report_rows:
        if r["pred_p"] is None:
            print(f"{r['week_start']}  {r['outcome']}")
            continue
        conf = f"{r['pred_p']*100:.1f}%"
        actual = f"{r['actual_score']} ({'OVER' if r['actual_over'] else 'under'} 2.5)"
        rank_tag = f"#{r['rank']}" if args.top_n > 1 else ""
        print(f"{r['week_start']} {rank_tag:<3s} [{r['competition']}] [{r['model_used']:<4s}] {r['home']:<20s} vs {r['away']:<20s}  "
              f"P={conf:<7s} actual={actual:<20s} {r['outcome']:<25s} {r['pnl']:>6s}  bankroll={r['bankroll']:+.0f}")

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
