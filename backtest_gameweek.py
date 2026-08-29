#!/usr/bin/env python3
"""True forward-test: freeze the model and team state at a cutoff (the
first kickoff of a given round), score every fixture in that round using
only data available before it, then compare to what actually happened.

This is different from rolling_validation.py, which re-tests on more
*historical* data — it's the first genuinely prospective check named as
an open caveat in docs/apifootball-dataset-analysis.md: "A true
out-of-sample test would apply this exact frozen approach to new seasons
as they arrive, going forward." A gameweek that has just been played is
exactly that.

Usage:
    APIFOOTBALL_KEY=xxxx python3 backtest_gameweek.py --league PL --round "Regular Season - 2"
"""

import argparse
import datetime
import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import ALL_CANDIDATES, add_derived_features, load
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from predict_upcoming import build_feature_row, replay_to_current_state

warnings.filterwarnings("ignore")


def fetch_round_fixtures(league_code: str, season: int, round_name: str) -> list[dict]:
    info = LEAGUES[league_code]
    data = apifootball.get("/fixtures", {"league": info["id"], "season": season})
    out = []
    for m in data.get("response", []):
        if m["league"]["round"] != round_name:
            continue
        out.append({
            "fixture_id": m["fixture"]["id"],
            "date": m["fixture"]["date"],
            "status": m["fixture"]["status"]["short"],
            "competition": league_code,
            "season": season,
            "home": m["teams"]["home"]["name"],
            "away": m["teams"]["away"]["name"],
            "home_id": m["teams"]["home"]["id"],
            "away_id": m["teams"]["away"]["id"],
            "home_goals": m["goals"]["home"],
            "away_goals": m["goals"]["away"],
        })
    out.sort(key=lambda m: m["date"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", default="PL", choices=list(LEAGUES))
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--round", required=True, dest="round_name", help='e.g. "Regular Season - 2"')
    args = parser.parse_args()

    fixtures = fetch_round_fixtures(args.league, args.season, args.round_name)
    if not fixtures:
        print("No fixtures found for that round.")
        return 1

    cutoff = min(datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) for m in fixtures)
    cutoff_date = pd.Timestamp(cutoff.date())
    print(f"{args.league} {args.round_name}: {len(fixtures)} fixtures, first kickoff {cutoff.isoformat()}")

    print("\nTraining the model on data available strictly BEFORE this round...")
    historical = load()
    model_df = historical[historical["date"] < cutoff_date]
    model_df = model_df[ALL_CANDIDATES + ["over_2_5", "date"]].dropna()
    print(f"  {len(model_df)} complete-case matches used for training (through {model_df['date'].max().date()})")

    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[ALL_CANDIDATES])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0,
    )
    model.fit(X_train, model_df["over_2_5"])

    print("Rebuilding team state from matches before the cutoff...")
    all_finished = fetch_all_fixtures(None)
    prior = [m for m in all_finished
             if datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) < cutoff]
    state = replay_to_current_state(prior)

    rows = []
    for m in fixtures:
        row = build_feature_row(m, state)
        if row is None:
            print(f"  (skipped {m['home']} vs {m['away']} — insufficient history)")
            continue
        row["actual_status"] = m["status"]
        row["actual_home_goals"] = m["home_goals"]
        row["actual_away_goals"] = m["away_goals"]
        rows.append(row)

    if not rows:
        print("No fixtures in this round had enough history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    scoreable = live_df.dropna(subset=ALL_CANDIDATES).copy()
    if scoreable.empty:
        print("All fixtures were missing at least one required feature.")
        return 0

    X_live = scaler.transform(scoreable[ALL_CANDIDATES])
    scoreable["pred_p_over_2_5"] = model.predict_proba(X_live)[:, 1]
    scoreable = scoreable.sort_values("pred_p_over_2_5", ascending=False)

    print(f"\n{args.league} {args.round_name} — predicted BEFORE kickoff, using only prior data:")
    print("-" * 95)
    correct = total_known = 0
    for _, r in scoreable.iterrows():
        pred_p = r["pred_p_over_2_5"]
        if r["actual_status"] == "FT":
            actual_total = r["actual_home_goals"] + r["actual_away_goals"]
            actual_over = actual_total > 2.5
            pred_over = pred_p >= 0.5
            hit = "correct" if pred_over == actual_over else "wrong"
            total_known += 1
            correct += int(pred_over == actual_over)
            outcome = (f"ACTUAL {int(r['actual_home_goals'])}-{int(r['actual_away_goals'])} "
                       f"({'OVER' if actual_over else 'under'} 2.5) — {hit}")
        else:
            outcome = "not yet played"
        print(f"{r['home_team']:<20s} vs {r['away_team']:<20s}  P(over 2.5)={pred_p*100:5.1f}%   {outcome}")

    if total_known:
        print(f"\n{correct}/{total_known} correct (>=50% threshold rule) on games already played in this round.")
        print("n is far too small to mean anything on its own — see rolling_validation.py for the real validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
