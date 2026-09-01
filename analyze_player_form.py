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

# The team-goals-form features player-form is meant to replace (not add
# alongside) -- once real xG is present these are largely redundant with
# it, and adding player-form on top of them just lets L1 arbitrarily pick
# one of two correlated signals (see rolling_validation_player_form_v2.py
# and rolling_validation_xg_player_form_v2.py for the validated swap).
TEAM_GOALS_FORM = [
    "home_gf_last5", "away_gf_last5", "home_gf_last10", "away_gf_last10",
    "home_gf_season", "away_gf_season", "combined_gf_last5", "naive_expected_total_last5",
]


def add_player_form_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Shared by the historical loaders and the live scorer (predict_upcoming.py),
    so a live prediction derives these identically to training.
    """
    df["attacking_form_total"] = df["home_attacking_form"] + df["away_attacking_form"]
    df["attacking_form_gap"] = (df["home_attacking_form"] - df["away_attacking_form"]).abs()
    return df


def load_with_player_form() -> pd.DataFrame:
    df = load_with_lineups()
    form_df = pd.read_csv(PLAYER_FORM_PATH)[["fixture_id"] + PLAYER_FORM_RAW_FEATURES]
    df = df.merge(form_df, on="fixture_id", how="left")
    return add_player_form_derived_features(df)


def load_with_xg_and_player_form() -> pd.DataFrame:
    """xG-covered rows + player-form, WITHOUT also requiring lineup-feature
    completeness (that unrelated requirement starved rolling_validation_xg_player_form.py's
    first attempt down to 1,720 rows and produced a degenerate 1-feature
    L1 fit in 2 of 3 folds -- see rolling_validation_xg_player_form_v2.py,
    which fixed it and is the validated basis for this loader).
    """
    from analyze_xg_features import load_with_xg

    df = load_with_xg()
    form_df = pd.read_csv(PLAYER_FORM_PATH)[["fixture_id"] + PLAYER_FORM_RAW_FEATURES]
    df = df.merge(form_df, on="fixture_id", how="left")
    return add_player_form_derived_features(df)


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
