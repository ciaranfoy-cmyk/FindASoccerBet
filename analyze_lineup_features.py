#!/usr/bin/env python3
"""Test whether the new lineup-based features (attacking output missing,
defensive regulars missing, lineup disruption, new signings) add real
signal on top of the existing 54-feature model — same discipline as
analyze_dataset_apifootball.py: Bonferroni-corrected univariate scan,
then L1 selection and out-of-sample evaluation on a chronological holdout.

Kept as a separate script (rather than editing analyze_dataset_apifootball.py
directly) so the existing, already-validated pipeline isn't touched until
this is actually shown to help.

Usage:
    python3 analyze_lineup_features.py
"""

import os
import warnings

import pandas as pd

from analyze_dataset_apifootball import (
    ALL_CANDIDATES,
    calibration_check,
    lasso_feature_selection,
    load,
    univariate_correlations,
)

warnings.filterwarnings("ignore")

LINEUP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lineup_features.csv")

LINEUP_RAW_FEATURES = [
    "home_lineup_change_count", "away_lineup_change_count",
    "home_attacking_output_missing", "away_attacking_output_missing",
    "home_defensive_regulars_missing", "away_defensive_regulars_missing",
    "home_new_signings_count", "away_new_signings_count",
]

LINEUP_DERIVED_FEATURES = [
    "lineup_change_total", "attacking_output_missing_total",
    "defensive_regulars_missing_total", "new_signings_total",
]


def load_with_lineups() -> pd.DataFrame:
    df = load()
    lineup_df = pd.read_csv(LINEUP_PATH)[["fixture_id"] + LINEUP_RAW_FEATURES]
    df = df.merge(lineup_df, on="fixture_id", how="left")

    df["lineup_change_total"] = df["home_lineup_change_count"] + df["away_lineup_change_count"]
    df["attacking_output_missing_total"] = df["home_attacking_output_missing"] + df["away_attacking_output_missing"]
    df["defensive_regulars_missing_total"] = df["home_defensive_regulars_missing"] + df["away_defensive_regulars_missing"]
    df["new_signings_total"] = df["home_new_signings_count"] + df["away_new_signings_count"]
    return df


def main() -> None:
    df = load_with_lineups()
    extended_candidates = ALL_CANDIDATES + LINEUP_RAW_FEATURES + LINEUP_DERIVED_FEATURES

    print(f"Loaded {len(df)} matches, {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"Testing {len(extended_candidates)} total candidates "
          f"({len(ALL_CANDIDATES)} existing + {len(LINEUP_RAW_FEATURES) + len(LINEUP_DERIVED_FEATURES)} new lineup features)")

    print("\n=== Just the new lineup features (for readability) ===")
    univariate_correlations(df, LINEUP_RAW_FEATURES + LINEUP_DERIVED_FEATURES,
                             "New lineup features only (uncorrected preview, see full scan below for the real bar)")

    univariate_correlations(df, extended_candidates, "Full extended candidate set (existing + new)")

    # Fix both runs to the SAME complete-case row set (based on the extended
    # candidate list) so the AUC comparison isn't confounded by a different,
    # possibly easier/harder subset of games — the only thing that should
    # differ between the two runs is which features the model can use.
    fixed_df = df.dropna(subset=extended_candidates + ["over_2_5", "date"]).reset_index(drop=True)
    print(f"\nFixed complete-case subset for a fair comparison: {len(fixed_df)} matches "
          f"({fixed_df['date'].min().date()} to {fixed_df['date'].max().date()})")

    print("\n### L1 model: existing 54 features only, on the fixed subset (baseline) ###")
    test_old, pred_old = lasso_feature_selection(fixed_df, ALL_CANDIDATES)

    print("\n### L1 model: existing 54 + new lineup features, same fixed subset ###")
    test_new, pred_new = lasso_feature_selection(fixed_df, extended_candidates)
    calibration_check(test_new, pred_new)


if __name__ == "__main__":
    main()
