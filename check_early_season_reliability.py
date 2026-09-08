#!/usr/bin/env python3
"""Does the live model's accuracy degrade early in a season?

Season-scoped features (goals-this-season, season goal difference,
league-position gap) run on a genuinely thin sample in the first few
matchweeks of a new season -- 1-4 games per team in early September,
same order of magnitude as the 10-matchweek window
analyze_early_season_form.py already found carries no standalone signal
(docs/dataset-analysis.md). Most of the live model's real signal comes
from rolling windows that carry over from last season (xG history,
venue-specific shots, attacking form, H2H) rather than resetting at
kickoff, so the model isn't fully exposed to that -- but the
season-scoped features ARE live inputs right now, and this has never
been checked directly: does the model's actual calibrated output get
measurably worse specifically in the low-games-played fixtures, versus
mid/late season, using the identical out-of-fold prediction stream and
calibrators the live pick system runs on?

Method: reuse build_stream() (the same walk-forward out-of-fold CV the
live confidence bar is computed from -- backtest_season_rolling_percentile.py)
for both the core and xG models, apply the same saved calibrators
(calibration.py), then bucket every out-of-fold prediction by how many
games each team had played in that competition+season BEFORE this
fixture (computed directly from date order, not the all-time
home/away_competition_games column, which never resets across seasons).
Compare Brier score, AUC, and calibration gap between the early bucket
and everything else -- same games, same model, same calibration, the
only thing that differs is season-experience -- so any Brier/calibration
degradation isolates the early-season effect specifically.

Usage:
    python3 check_early_season_reliability.py
"""

import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from analyze_shots_venue import load_with_player_form_and_shots_venue, load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from build_xg_weighted_features import load_weighted_xg
from calibration import apply_calibration, load_calibrators
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

warnings.filterwarnings("ignore")

EARLY_CUTOFF = 4  # both teams have played <= this many prior competition+season games


def games_into_season(raw: pd.DataFrame) -> pd.DataFrame:
    """For every match, how many prior games (same competition+season) had
    the home/away team already played -- computed directly from date
    order, unlike home/away_competition_games which is cumulative across
    ALL seasons and never resets.
    """
    raw = raw.sort_values("date").reset_index(drop=True)
    long = pd.concat([
        raw[["fixture_id", "date", "competition", "season", "home_team"]].rename(columns={"home_team": "team"}),
        raw[["fixture_id", "date", "competition", "season", "away_team"]].rename(columns={"away_team": "team"}),
    ]).sort_values("date")
    long["games_played"] = long.groupby(["team", "competition", "season"]).cumcount()
    home_n = long[long["fixture_id"].isin(raw["fixture_id"])].copy()

    home_map = raw[["fixture_id", "home_team"]].merge(
        long.rename(columns={"team": "home_team", "games_played": "home_games_into_season"}),
        on=["fixture_id", "home_team"], how="left",
    )[["fixture_id", "home_games_into_season"]]
    away_map = raw[["fixture_id", "away_team"]].merge(
        long.rename(columns={"team": "away_team", "games_played": "away_games_into_season"}),
        on=["fixture_id", "away_team"], how="left",
    )[["fixture_id", "away_games_into_season"]]

    out = raw[["fixture_id"]].merge(home_map, on="fixture_id").merge(away_map, on="fixture_id")
    return out.drop_duplicates("fixture_id")


def bucket_report(label: str, y: np.ndarray, p: np.ndarray) -> None:
    if len(y) < 20:
        print(f"  {label}: n={len(y)} -- too few to report")
        return
    brier = brier_score_loss(y, p)
    auc = roc_auc_score(y, p) if len(set(y)) > 1 else float("nan")
    base_rate = y.mean()
    mean_pred = p.mean()
    print(f"  {label}: n={len(y):<5} base_rate={base_rate*100:5.1f}%  mean_pred={mean_pred*100:5.1f}%  "
          f"calib_gap={abs(base_rate-mean_pred)*100:+5.1f}pp  Brier={brier:.4f}  AUC={auc:.3f}")


def main() -> int:
    print("Building out-of-fold prediction streams (same as the live confidence bar)...")
    core_df = load_with_player_form_and_shots_venue()
    core_stream = build_stream(core_df, CORE_CANDIDATES, N_FOLDS_CORE, "core").rename(columns={"pred_p": "pred_p_core"})

    xg_df = load_weighted_xg(load_with_xg_player_form_and_shots_venue())
    xg_stream = build_stream(xg_df, XG_CANDIDATES, N_FOLDS_XG, "xG")[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})

    merged = core_stream.merge(xg_stream, on="fixture_id", how="left")
    merged["pred_p_raw"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})

    calibrators = load_calibrators()
    merged["pred_p"] = apply_calibration(merged["pred_p_raw"], merged["model_used"], calibrators)

    print("Computing games-into-season per fixture (date-order, resets each season)...")
    raw = pd.read_csv("data/matches_apifootball.csv")
    season_info = games_into_season(raw)
    merged = merged.merge(season_info, on="fixture_id", how="left")
    merged["min_games_into_season"] = merged[["home_games_into_season", "away_games_into_season"]].min(axis=1)

    y = merged["over_2_5"].astype(int).values
    p = merged["pred_p"].astype(float).values
    print(f"\nOverall (all {len(merged)} out-of-fold predictions, current live calibrators):")
    bucket_report("all", y, p)

    print(f"\nEarly season (both teams <= {EARLY_CUTOFF} games played this competition+season) vs. rest:")
    early_mask = merged["min_games_into_season"] <= EARLY_CUTOFF
    bucket_report(f"early (<= {EARLY_CUTOFF} games)", y[early_mask.values], p[early_mask.values])
    bucket_report(f"rest  (> {EARLY_CUTOFF} games)", y[~early_mask.values], p[~early_mask.values])

    print("\nFiner breakdown by min games-into-season bucket:")
    edges = [(0, 2), (3, 4), (5, 8), (9, 14), (15, 999)]
    for lo, hi in edges:
        m = (merged["min_games_into_season"] >= lo) & (merged["min_games_into_season"] <= hi)
        label = f"{lo}-{hi if hi < 999 else '+'} games"
        bucket_report(label, y[m.values], p[m.values])

    # Same comparison, restricted to fixtures the live pick bar would
    # actually flag (calibrated_p >= 0.60) -- the regime that matters,
    # since a Brier gap on low-confidence fixtures nobody would ever bet
    # is not actionable.
    print("\nSame breakdown, restricted to model-confident fixtures (pred_p >= 60%) -- the regime that's actually bet:")
    confident = merged["pred_p"] >= 0.60
    bucket_report(f"early, confident (n)", y[(early_mask & confident).values], p[(early_mask & confident).values])
    bucket_report(f"rest, confident (n)", y[(~early_mask & confident).values], p[(~early_mask & confident).values])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
