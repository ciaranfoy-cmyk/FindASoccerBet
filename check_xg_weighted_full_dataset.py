#!/usr/bin/env python3
"""Full-dataset coefficient check for recency+competition-weighted xG
features (build_xg_weighted_features.py) -- the same verification that
overturned the apparent rolling-validation wins for both Elo
(build_elo_features.py) and league-average-finish
(build_league_finish_features.py): a LogisticRegressionCV fit on the
COMPLETE dataset (not walk-forward folds) can zero out a feature via L1
even when it looked like a real improvement fold-by-fold. Before wiring
weighted xG into predict_upcoming.py's live XG_CANDIDATES, confirm it
actually survives this.

Usage:
    python3 check_xg_weighted_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from predict_upcoming import XG_CANDIDATES
from rolling_validation_xg_weighted import (
    FLAT_XG,
    FLAT_XG_ALL,
    WEIGHTED_PATH,
    WEIGHTED_XG,
    WEIGHTED_XG_ALL,
    load_data,
)

warnings.filterwarnings("ignore")


def fit_full(df: pd.DataFrame, features: list[str]) -> LogisticRegressionCV:
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, model_df["over_2_5"])
    print(f"  Trained on {len(model_df)} complete-case rows")
    return model


def report(name: str, features: list[str], model: LogisticRegressionCV) -> None:
    print(f"\n{name} (C={model.C_[0]:.4f}):")
    for f, c in sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1])):
        flag = "" if abs(c) > 1e-6 else "  <-- ZEROED"
        print(f"  {f:<45s} {c:+.5f}{flag}")


def main() -> None:
    df = load_data()

    combined = list(dict.fromkeys(
        [f for f in XG_CANDIDATES if f not in FLAT_XG_ALL] + FLAT_XG_ALL + WEIGHTED_XG_ALL
    ))

    print("=== Full-dataset fit: combined (flat + weighted xG together) ===")
    model_combined = fit_full(df, combined)
    report("combined", combined, model_combined)

    weighted_zeroed = [
        f for f in WEIGHTED_XG_ALL
        if abs(dict(zip(combined, model_combined.coef_[0]))[f]) <= 1e-6
    ]
    flat_zeroed = [
        f for f in FLAT_XG_ALL
        if abs(dict(zip(combined, model_combined.coef_[0]))[f]) <= 1e-6
    ]
    print(f"\nWeighted features zeroed: {len(weighted_zeroed)}/{len(WEIGHTED_XG_ALL)} {weighted_zeroed}")
    print(f"Flat features zeroed:     {len(flat_zeroed)}/{len(FLAT_XG_ALL)} {flat_zeroed}")

    print("\n=== Full-dataset fit: weighted-only (swap, not combine) ===")
    weighted_only = list(dict.fromkeys(
        [f for f in XG_CANDIDATES if f not in FLAT_XG_ALL] + WEIGHTED_XG_ALL
    ))
    model_weighted_only = fit_full(df, weighted_only)
    report("weighted-only", weighted_only, model_weighted_only)
    wo_zeroed = [
        f for f in WEIGHTED_XG_ALL
        if abs(dict(zip(weighted_only, model_weighted_only.coef_[0]))[f]) <= 1e-6
    ]
    print(f"\nWeighted features zeroed in swap-only fit: {len(wo_zeroed)}/{len(WEIGHTED_XG_ALL)} {wo_zeroed}")

    print("\n=== Full-dataset fit: flat-only (today's live model, for reference) ===")
    flat_only = list(dict.fromkeys(
        [f for f in XG_CANDIDATES if f not in FLAT_XG_ALL] + FLAT_XG_ALL
    ))
    model_flat_only = fit_full(df, flat_only)
    report("flat-only", flat_only, model_flat_only)


if __name__ == "__main__":
    main()
