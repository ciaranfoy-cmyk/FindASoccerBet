#!/usr/bin/env python3
"""Test whether real xG-based features add signal — same discipline as
analyze_lineup_features.py / analyze_player_form.py: Bonferroni-corrected
univariate scan, then L1 selection on a fixed, fair complete-case subset.

Deliberately tested against just the core dataset (not stacked on top of
shots/lineup/player-form coverage too), since xG's own recent-seasons-only
window is already the binding constraint on sample size — stacking
further partial-coverage requirements would shrink it needlessly.

Usage:
    python3 analyze_xg_features.py
"""

import os
import warnings

import pandas as pd

from analyze_dataset_apifootball import ALL_CANDIDATES, calibration_check, lasso_feature_selection, load, univariate_correlations

warnings.filterwarnings("ignore")

XG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_features.csv")

XG_RAW_FEATURES = [
    "home_xg_last5", "away_xg_last5",
    "home_xg_against_last5", "away_xg_against_last5",
    "home_finishing_last5", "away_finishing_last5",
]
XG_DERIVED_FEATURES = [
    "combined_xg_last5", "xg_gap_last5",
    "naive_expected_total_xg_last5",
]


def load_with_xg() -> pd.DataFrame:
    df = load()
    xg_df = pd.read_csv(XG_PATH)[["fixture_id"] + XG_RAW_FEATURES]
    df = df.merge(xg_df, on="fixture_id", how="left")

    df["combined_xg_last5"] = df["home_xg_last5"] + df["away_xg_last5"]
    df["xg_gap_last5"] = (df["home_xg_last5"] - df["away_xg_last5"]).abs()
    df["naive_expected_total_xg_last5"] = (
        df["home_xg_last5"] + df["away_xg_against_last5"] + df["away_xg_last5"] + df["home_xg_against_last5"]
    )
    return df


def main() -> None:
    df = load_with_xg()
    new_candidates = XG_RAW_FEATURES + XG_DERIVED_FEATURES
    extended_candidates = ALL_CANDIDATES + new_candidates

    print(f"Loaded {len(df)} matches, {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"Testing {len(extended_candidates)} total candidates "
          f"({len(ALL_CANDIDATES)} existing + {len(new_candidates)} new xG features)")

    univariate_correlations(df, extended_candidates, "Full extended candidate set (existing + real xG)")

    fixed_df = df.dropna(subset=extended_candidates + ["over_2_5", "date"]).reset_index(drop=True)
    print(f"\nFixed complete-case subset for a fair comparison: {len(fixed_df)} matches "
          f"({fixed_df['date'].min().date()} to {fixed_df['date'].max().date()})")

    print(f"\n### L1 model: existing 54 features only, on the fixed subset (baseline) ###")
    test_old, pred_old = lasso_feature_selection(fixed_df, ALL_CANDIDATES)

    print(f"\n### L1 model: + real xG features, same fixed subset ###")
    test_new, pred_new = lasso_feature_selection(fixed_df, extended_candidates)
    calibration_check(test_new, pred_new)


if __name__ == "__main__":
    main()
