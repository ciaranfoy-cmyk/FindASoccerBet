#!/usr/bin/env python3
"""Does the two-way (attack/defense) regression team-quality rating
(build_team_ratings_features.py) actually beat what's already live, on
the same decisive test every other feature in this project was gated
on: full-dataset LogisticRegressionCV coefficient survival. A rolling-
validation win alone already fooled this project twice (Elo,
league-average-finish) before full-dataset L1 zeroed them out -- so
that's checked here, not treated as decisive.

Two variants against the current live XG_CANDIDATES baseline (which
already only carries the weighted xG features, not the original flat
ones -- see build_xg_weighted_features.py, flat xG was already swapped
out and rejected as a live feature before this check ever runs):
  A. COMBINE  -- ratings features added alongside everything live keeps
     (nothing else captures opponent-adjusted, jointly-solved quality,
     so this is the natural test, same logic as LEAGUE_FINISH_FEATURES
     being combined rather than swapped in).
  B. SWAP for weighted xG -- ratings features replace
     WEIGHTED_XG_RAW_FEATURES + WEIGHTED_XG_DERIVED_FEATURES entirely,
     testing whether the regression-based rating is just a strictly
     better version of the same "opponent quality" signal.

Also runs the same out-of-sample walk-forward Brier/AUC comparison
(build_stream, same CV the live confidence bar uses) as every other
check in this project, for a second opinion -- but full-dataset L1
survival is what decides it. That's a good thing: check_team_ratings_
significance.py bootstrapped this OOS comparison and found it's NOT
statistically distinguishable from noise at n~5337 (directionally
consistent, not independent corroborating evidence) -- had this been
the decisive test instead of L1, the result would have been a coin
flip, not a validated finding.

(This file previously mislabeled the calibration call ("xg" instead of
calibration.py's exact-match "xG"), so the "calibrated" OOS numbers
below were silently uncalibrated raw model output. Fixed -- see
check_team_ratings_significance.py's docstring.)

Usage:
    python3 check_team_ratings_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_XG, build_stream
from build_team_ratings_features import (
    RATINGS_DERIVED_FEATURES,
    RATINGS_RAW_FEATURES,
    load_team_ratings,
)
from build_xg_weighted_features import WEIGHTED_XG_DERIVED_FEATURES, WEIGHTED_XG_RAW_FEATURES, load_weighted_xg
from calibration import apply_calibration, load_calibrators
from predict_upcoming import XG_CANDIDATES

warnings.filterwarnings("ignore")

RATINGS_ALL = RATINGS_RAW_FEATURES + RATINGS_DERIVED_FEATURES
WEIGHTED_XG_ALL = WEIGHTED_XG_RAW_FEATURES + WEIGHTED_XG_DERIVED_FEATURES


def load_data() -> pd.DataFrame:
    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)
    return load_team_ratings(df)


def fit_full(df: pd.DataFrame, features: list[str]) -> LogisticRegressionCV:
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, model_df["over_2_5"])
    print(f"  Trained on {len(model_df)} complete-case rows (C={model.C_[0]:.4f})")
    return model


def report(name: str, features: list[str], model: LogisticRegressionCV, watch: list[str]) -> None:
    print(f"\n{name}:")
    for f, c in sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1])):
        flag = "  <-- NEW" if f in watch else ""
        zeroed = " (ZEROED by L1)" if c == 0 else ""
        print(f"    {f:32s} {c:+.5f}{zeroed}{flag}")


def survival(features: list[str], model: LogisticRegressionCV, watch: list[str]) -> None:
    coefs = dict(zip(features, model.coef_[0]))
    zeroed = [f for f in watch if abs(coefs.get(f, 0.0)) <= 1e-6]
    print(f"  Watched features zeroed: {len(zeroed)}/{len(watch)} {zeroed}")


def main() -> int:
    print("Loading full dataset with team-ratings features merged in...")
    df = load_data()

    # XG_CANDIDATES already includes the ratings features as of this
    # script's run -- they were wired into the live model on the strength
    # of the result this script originally produced. Strip them back out
    # to reconstruct the pre-ratings baseline, so re-running this later
    # doesn't silently double the ratings columns (duplicate-column crash)
    # or compare ratings against itself.
    pre_ratings_features = [f for f in XG_CANDIDATES if f not in RATINGS_ALL]
    baseline_features = pre_ratings_features
    combine_features = pre_ratings_features + RATINGS_ALL
    swap_weighted_features = [f for f in pre_ratings_features if f not in WEIGHTED_XG_ALL] + RATINGS_ALL

    print("\n=== Full-dataset L1 coefficient survival ===")
    print("Baseline (live XG_CANDIDATES):")
    base_model = fit_full(df, baseline_features)
    report("Baseline", baseline_features, base_model, [])

    print("\nVariant A: COMBINE (ratings added alongside everything live):")
    a_model = fit_full(df, combine_features)
    report("Variant A (combine)", combine_features, a_model, RATINGS_ALL)
    survival(combine_features, a_model, RATINGS_ALL)

    print("\nVariant B: SWAP for weighted xG (ratings replace weighted xG entirely):")
    b_model = fit_full(df, swap_weighted_features)
    report("Variant B (swap weighted xG)", swap_weighted_features, b_model, RATINGS_ALL)
    survival(swap_weighted_features, b_model, RATINGS_ALL)
    print("  Weighted xG features that would be given up:")
    survival(baseline_features, base_model, WEIGHTED_XG_ALL)

    # Out-of-sample walk-forward comparison, same CV as the live bar.
    print("\n=== Out-of-sample walk-forward (XG-model CV, same as the live confidence bar) ===")
    calibrators = load_calibrators()

    def score_variant(label: str, features: list[str]) -> pd.DataFrame:
        stream = build_stream(df, features, N_FOLDS_XG, label)
        stream["pred_p_cal"] = apply_calibration(stream["pred_p"], pd.Series(["xG"] * len(stream)), calibrators)
        return stream

    from sklearn.metrics import brier_score_loss, roc_auc_score

    def summarize(label: str, stream: pd.DataFrame) -> None:
        y = stream["over_2_5"].astype(int).values
        p = stream["pred_p_cal"].astype(float).values
        brier = brier_score_loss(y, p)
        auc = roc_auc_score(y, p) if len(set(y)) > 1 else float("nan")
        print(f"  {label:35s} n={len(y):<5} Brier={brier:.4f}  AUC={auc:.3f}")

    base_stream = score_variant("baseline", baseline_features)
    a_stream = score_variant("combine", combine_features)
    b_stream = score_variant("swap-weighted", swap_weighted_features)

    summarize("Baseline (live XG_CANDIDATES)", base_stream)
    summarize("Variant A (combine)", a_stream)
    summarize("Variant B (swap weighted xG)", b_stream)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
