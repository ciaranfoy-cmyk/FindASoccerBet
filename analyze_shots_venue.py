#!/usr/bin/env python3
"""Venue-split shot volume, validated in rolling_validation_shots_venue.py:
venue-only clean-beats blended shots-last5 (63.0% vs 61.2% combined
top-5% hit rate, higher AUC/univariate correlation too), not just a tie
like the xG venue split. Loaders + derived-feature helper here, shared
by the historical scripts and predict_upcoming.py (the live scorer), so
a live prediction derives these identically to training -- same pattern
as analyze_xg_features.py / analyze_player_form.py.
"""

import os

import pandas as pd

VENUE_SHOTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shots_venue_features.csv")

# The blended shot features venue-split replaces (not adds alongside --
# same collinearity trap every other clean-swap test in this project
# guards against).
BLENDED_SHOTS = [
    "home_shots_last5", "home_shots_on_goal_last5", "home_shots_inside_box_last5",
    "away_shots_last5", "away_shots_on_goal_last5", "away_shots_inside_box_last5",
    "combined_shots_last5", "combined_shots_inside_box_last5", "shots_gap_last5",
    "home_conversion_rate_last5", "away_conversion_rate_last5",
]
VENUE_SHOTS_RAW_FEATURES = [
    "home_venue_shots_last5", "away_venue_shots_last5",
    "home_venue_shots_on_goal_last5", "away_venue_shots_on_goal_last5",
    "home_venue_shots_inside_box_last5", "away_venue_shots_inside_box_last5",
]
VENUE_SHOTS_DERIVED_FEATURES = [
    "combined_venue_shots_last5", "combined_venue_shots_inside_box_last5", "venue_shots_gap_last5",
    "home_venue_conversion_rate_last5", "away_venue_conversion_rate_last5",
]


def _safe_div(a, b):
    return None if a is None or pd.isna(a) or b is None or pd.isna(b) or b == 0 else a / b


def add_shots_venue_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Shared by the historical loaders and the live scorer (predict_upcoming.py).
    Requires home_gf_last5/away_gf_last5 (still computed as raw fields
    even though no longer a model candidate) and the raw home/away_venue_shots_last5
    columns to already be present.
    """
    df["combined_venue_shots_last5"] = df["home_venue_shots_last5"] + df["away_venue_shots_last5"]
    df["combined_venue_shots_inside_box_last5"] = (
        df["home_venue_shots_inside_box_last5"] + df["away_venue_shots_inside_box_last5"]
    )
    df["venue_shots_gap_last5"] = (df["home_venue_shots_last5"] - df["away_venue_shots_last5"]).abs()
    df["home_venue_conversion_rate_last5"] = df.apply(
        lambda r: _safe_div(r["home_gf_last5"], r["home_venue_shots_last5"]), axis=1)
    df["away_venue_conversion_rate_last5"] = df.apply(
        lambda r: _safe_div(r["away_gf_last5"], r["away_venue_shots_last5"]), axis=1)
    return df


def _merge_venue_shots(df: pd.DataFrame) -> pd.DataFrame:
    venue_df = pd.read_csv(VENUE_SHOTS_PATH)[["fixture_id"] + VENUE_SHOTS_RAW_FEATURES]
    df = df.merge(venue_df, on="fixture_id", how="left")
    return add_shots_venue_derived_features(df)


def _merge_league_finish(df: pd.DataFrame) -> pd.DataFrame:
    from build_league_finish_features import add_league_finish_features, build_standings_cache

    standings_cache = build_standings_cache()
    return add_league_finish_features(df, standings_cache)


def load_with_player_form_and_shots_venue() -> pd.DataFrame:
    from analyze_player_form import load_with_player_form

    return _merge_league_finish(_merge_venue_shots(load_with_player_form()))


def load_with_xg_player_form_and_shots_venue() -> pd.DataFrame:
    from analyze_player_form import load_with_xg_and_player_form

    return _merge_league_finish(_merge_venue_shots(load_with_xg_and_player_form()))
