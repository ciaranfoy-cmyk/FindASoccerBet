#!/usr/bin/env python3
"""Test whether the new player-form features (rolling goals-per-start of
today's starting attackers, not career average) add real signal — same
discipline as analyze_lineup_features.py: Bonferroni-corrected univariate
scan, then L1 selection on a fixed, fair complete-case subset.

Usage:
    python3 analyze_player_form.py
"""

import os
import warnings

import pandas as pd

from analyze_dataset_apifootball import calibration_check, lasso_feature_selection, univariate_correlations
from analyze_lineup_features import LINEUP_DERIVED_FEATURES, LINEUP_RAW_FEATURES, load_with_lineups

warnings.filterwarnings("ignore")

PLAYER_FORM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "player_form_features.csv")

PLAYER_FORM_RAW_FEATURES = ["home_attacking_form", "away_attacking_form"]
PLAYER_FORM_DERIVED_FEATURES = ["attacking_form_total", "attacking_form_gap"]


def load_with_player_form() -> pd.DataFrame:
    df = load_with_lineups()
    form_df = pd.read_csv(PLAYER_FORM_PATH)[["fixture_id"] + PLAYER_FORM_RAW_FEATURES]
    df = df.merge(form_df, on="fixture_id", how="left")

    df["attacking_form_total"] = df["home_attacking_form"] + df["away_attacking_form"]
    df["attacking_form_gap"] = (df["home_attacking_form"] - df["away_attacking_form"]).abs()
    return df


def main() -> None:
    from analyze_dataset_apifootball import ALL_CANDIDATES

    df = load_with_player_form()
    lineup_candidates = LINEUP_RAW_FEATURES + LINEUP_DERIVED_FEATURES
    new_candidates = PLAYER_FORM_RAW_FEATURES + PLAYER_FORM_DERIVED_FEATURES
    extended_candidates = ALL_CANDIDATES + lineup_candidates + new_candidates

    print(f"Loaded {len(df)} matches, {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"Testing {len(extended_candidates)} total candidates "
          f"({len(ALL_CANDIDATES) + len(lineup_candidates)} existing + {len(new_candidates)} new player-form features)")

    univariate_correlations(df, extended_candidates, "Full extended candidate set (existing + lineup + player-form)")

    fixed_df = df.dropna(subset=extended_candidates + ["over_2_5", "date"]).reset_index(drop=True)
    print(f"\nFixed complete-case subset for a fair comparison: {len(fixed_df)} matches "
          f"({fixed_df['date'].min().date()} to {fixed_df['date'].max().date()})")

    baseline_candidates = ALL_CANDIDATES + lineup_candidates
    print(f"\n### L1 model: existing + lineup features only, on the fixed subset (baseline) ###")
    test_old, pred_old = lasso_feature_selection(fixed_df, baseline_candidates)

    print(f"\n### L1 model: + new player-form features, same fixed subset ###")
    test_new, pred_new = lasso_feature_selection(fixed_df, extended_candidates)
    calibration_check(test_new, pred_new)


if __name__ == "__main__":
    main()
