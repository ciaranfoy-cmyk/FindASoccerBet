#!/usr/bin/env python3
"""Does venue-split (and winsorized venue-split) season goals-for/against/
goal-diff beat the live blended version -- and does either one reduce the
early-season overconfidence found in check_early_season_reliability.py?

Three things tested, same discipline every other feature in this project
was gated on:
  1. Full-dataset LogisticRegressionCV coefficient survival (the
     decisive test -- a rolling-validation win alone already fooled this
     project twice, on Elo and league-average-finish, before this check
     zeroed them out via L1 on the complete dataset).
  2. Out-of-sample walk-forward Brier/AUC (build_stream, same CV the live
     confidence bar uses) -- baseline vs raw venue-split swap vs
     winsorized (95th-percentile-capped) venue-split swap.
  3. The metric that actually matters here: does either swap reduce the
     +6.1pp early-season calibration gap in the model-confident bucket
     (>=60%) that check_early_season_reliability.py found?

Usage:
    python3 check_home_away_season_full_dataset.py
"""

import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, build_stream
from calibration import apply_calibration, load_calibrators
from predict_upcoming import CORE_CANDIDATES

warnings.filterwarnings("ignore")

BLENDED = ["home_gf_season", "home_ga_season", "away_gf_season", "away_ga_season",
           "home_goal_diff", "away_goal_diff", "goal_diff_gap"]
VENUE_RAW = ["home_gf_season_home", "home_ga_season_home", "away_gf_season_away", "away_ga_season_away",
             "home_goal_diff_home", "away_goal_diff_away", "goal_diff_gap_venue"]
VENUE_W95 = [c + "_w95" if c != "goal_diff_gap_venue" else "goal_diff_gap_venue_w95" for c in VENUE_RAW]


def load_data() -> pd.DataFrame:
    df = load_with_player_form_and_shots_venue()
    venue_df = pd.read_csv("data/home_away_season_features.csv")
    df = df.merge(venue_df, on="fixture_id", how="left")
    return df


def fit_full(df: pd.DataFrame, features: list[str]) -> tuple:
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X, model_df["over_2_5"])
    print(f"  Trained on {len(model_df)} complete-case rows (C={model.C_[0]:.4f})")
    return model, scaler


def report_coefs(name: str, features: list[str], model, watch: list[str]) -> None:
    print(f"\n{name}:")
    for f, c in sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1])):
        flag = "  <-- NEW" if f in watch else ""
        zeroed = " (ZEROED by L1)" if c == 0 else ""
        print(f"    {f:32s} {c:+.4f}{zeroed}{flag}")


def bucket_report(label: str, y: np.ndarray, p: np.ndarray) -> None:
    if len(y) < 20:
        print(f"    {label}: n={len(y)} -- too few")
        return
    brier = brier_score_loss(y, p)
    auc = roc_auc_score(y, p) if len(set(y)) > 1 else float("nan")
    base_rate, mean_pred = y.mean(), p.mean()
    print(f"    {label}: n={len(y):<5} base_rate={base_rate*100:5.1f}%  mean_pred={mean_pred*100:5.1f}%  "
          f"calib_gap={abs(base_rate-mean_pred)*100:+5.1f}pp  Brier={brier:.4f}  AUC={auc:.3f}")


def main() -> int:
    print("Loading full dataset with venue-split season features merged in...")
    df = load_data()

    baseline_features = CORE_CANDIDATES
    raw_swap_features = [f for f in CORE_CANDIDATES if f not in BLENDED] + VENUE_RAW
    w95_swap_features = [f for f in CORE_CANDIDATES if f not in BLENDED] + VENUE_W95
    combine_features = CORE_CANDIDATES + VENUE_RAW

    print("\n=== Full-dataset L1 coefficient survival ===")
    print("Baseline (blended season stats, live features):")
    base_model, base_scaler = fit_full(df, baseline_features)
    report_coefs("Baseline", baseline_features, base_model, BLENDED)

    print("\nVariant A: venue-split RAW swap:")
    a_model, a_scaler = fit_full(df, raw_swap_features)
    report_coefs("Variant A (raw venue-split swap)", raw_swap_features, a_model, VENUE_RAW)

    print("\nVariant B: venue-split WINSORIZED (95th pct cap) swap:")
    b_model, b_scaler = fit_full(df, w95_swap_features)
    report_coefs("Variant B (winsorized venue-split swap)", w95_swap_features, b_model, VENUE_W95)

    print("\nVariant C: venue-split RAW combined alongside blended (not swapped):")
    c_model, c_scaler = fit_full(df, combine_features)
    report_coefs("Variant C (combined)", combine_features, c_model, VENUE_RAW)

    # Out-of-sample walk-forward comparison, same CV as the live bar.
    print("\n=== Out-of-sample walk-forward (core-model CV, same as the live confidence bar) ===")
    calibrators = load_calibrators()
    raw = pd.read_csv("data/matches_apifootball.csv")

    def score_variant(label: str, features: list[str]) -> pd.DataFrame:
        stream = build_stream(df, features, N_FOLDS_CORE, label)
        stream["pred_p_cal"] = apply_calibration(stream["pred_p"], pd.Series(["core"] * len(stream)), calibrators)
        return stream

    base_stream = score_variant("baseline", baseline_features)
    a_stream = score_variant("raw-swap", raw_swap_features)
    b_stream = score_variant("w95-swap", w95_swap_features)

    # Merge in games-into-season to isolate the early-season bucket.
    def games_into_season(raw_df: pd.DataFrame) -> pd.DataFrame:
        raw_df = raw_df.sort_values("date").reset_index(drop=True)
        long = pd.concat([
            raw_df[["fixture_id", "date", "competition", "season", "home_team"]].rename(columns={"home_team": "team"}),
            raw_df[["fixture_id", "date", "competition", "season", "away_team"]].rename(columns={"away_team": "team"}),
        ]).sort_values("date")
        long["games_played"] = long.groupby(["team", "competition", "season"]).cumcount()
        home_map = raw_df[["fixture_id", "home_team"]].merge(
            long.rename(columns={"team": "home_team", "games_played": "home_gis"}),
            on=["fixture_id", "home_team"], how="left")[["fixture_id", "home_gis"]]
        away_map = raw_df[["fixture_id", "away_team"]].merge(
            long.rename(columns={"team": "away_team", "games_played": "away_gis"}),
            on=["fixture_id", "away_team"], how="left")[["fixture_id", "away_gis"]]
        out = raw_df[["fixture_id"]].merge(home_map, on="fixture_id").merge(away_map, on="fixture_id")
        out["min_gis"] = out[["home_gis", "away_gis"]].min(axis=1)
        return out.drop_duplicates("fixture_id")

    season_info = games_into_season(raw)

    print("\n=== Early-season confident-bucket calibration gap (the metric that matters) ===")
    for label, stream in [("BASELINE (blended)", base_stream), ("Variant A (raw venue-split)", a_stream), ("Variant B (winsorized venue-split)", b_stream)]:
        m = stream.merge(season_info, on="fixture_id", how="left")
        y = m["over_2_5"].astype(int).values
        p = m["pred_p_cal"].astype(float).values
        confident = m["pred_p_cal"] >= 0.60
        early = m["min_gis"] <= 4
        print(f"\n  {label}:")
        bucket_report("early, confident", y[(early & confident).values], p[(early & confident).values])
        bucket_report("rest, confident  ", y[(~early & confident).values], p[(~early & confident).values])
        bucket_report("all", y, p)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
