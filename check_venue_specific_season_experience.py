import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from analyze_shots_venue import load_with_player_form_and_shots_venue, load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from build_xg_weighted_features import load_weighted_xg
from calibration import apply_calibration, load_calibrators
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

EARLY_CUTOFF = 3  # home team needs > this many HOME games this season, away team > this many AWAY games


def venue_games_into_season(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.sort_values("date").reset_index(drop=True)
    home_long = raw[["fixture_id", "date", "competition", "season", "home_team"]].rename(columns={"home_team": "team"})
    away_long = raw[["fixture_id", "date", "competition", "season", "away_team"]].rename(columns={"away_team": "team"})
    home_long["venue"] = "home"
    away_long["venue"] = "away"
    long = pd.concat([home_long, away_long]).sort_values("date")
    long["venue_games_played"] = long.groupby(["team", "competition", "season", "venue"]).cumcount()

    home_map = raw[["fixture_id", "home_team"]].merge(
        long[long["venue"] == "home"].rename(columns={"team": "home_team", "venue_games_played": "home_team_home_games_this_season"}),
        on=["fixture_id", "home_team"], how="left",
    )[["fixture_id", "home_team_home_games_this_season"]]
    away_map = raw[["fixture_id", "away_team"]].merge(
        long[long["venue"] == "away"].rename(columns={"team": "away_team", "venue_games_played": "away_team_away_games_this_season"}),
        on=["fixture_id", "away_team"], how="left",
    )[["fixture_id", "away_team_away_games_this_season"]]

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

    print("Computing venue-specific games-into-season (home games for home team, away games for away team)...")
    raw = pd.read_csv("data/matches_apifootball.csv")
    venue_info = venue_games_into_season(raw)
    merged = merged.merge(venue_info, on="fixture_id", how="left")
    merged["min_venue_games"] = merged[["home_team_home_games_this_season", "away_team_away_games_this_season"]].min(axis=1)

    y = merged["over_2_5"].astype(int).values
    p = merged["pred_p"].astype(float).values

    print(f"\nVenue-specific early season (home team <= {EARLY_CUTOFF} HOME games AND/effectively away team <= {EARLY_CUTOFF} AWAY games this season) vs rest:")
    early_mask = merged["min_venue_games"] <= EARLY_CUTOFF
    bucket_report(f"venue-thin (<= {EARLY_CUTOFF} same-venue games)", y[early_mask.values], p[early_mask.values])
    bucket_report(f"venue-seasoned (> {EARLY_CUTOFF})", y[~early_mask.values], p[~early_mask.values])

    print("\nFiner breakdown by min(home-team home games, away-team away games) this season:")
    for lo, hi in [(0, 1), (2, 3), (4, 5), (6, 999)]:
        m = (merged["min_venue_games"] >= lo) & (merged["min_venue_games"] <= hi)
        label = f"{lo}-{hi if hi < 999 else '+'} same-venue games"
        bucket_report(label, y[m.values], p[m.values])

    print("\nSame breakdown, restricted to model-confident fixtures (pred_p >= 60%) -- the regime that's actually bet:")
    confident = merged["pred_p"] >= 0.60
    bucket_report("venue-thin, confident", y[(early_mask & confident).values], p[(early_mask & confident).values])
    bucket_report("venue-seasoned, confident", y[(~early_mask & confident).values], p[(~early_mask & confident).values])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
