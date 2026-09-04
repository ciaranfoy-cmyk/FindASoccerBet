#!/usr/bin/env python3
"""Follow-up to diagnose_under_overconfidence.py: PL specifically showed
a -21.4pp overconfidence gap on its top-5% most-confident Under calls
(38.1% actual hit rate vs 59.5% predicted, n=42) while every other major
league (ELC, LaLiga, SerieA) was well-calibrated on the same selection.
This pulls those exact 42 PL fixtures with real scorelines and feature
context, looking for a common thread: specific seasons, specific teams,
a particular feature signature, or just small-sample noise.

Usage:
    python3 diagnose_pl_under_misses.py
"""

import warnings
from collections import deque

import pandas as pd

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from build_dataset_apifootball import fetch_all_fixtures
from build_xg_weighted_features import load_weighted_xg
from calibration import apply_calibration, load_calibrators
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

warnings.filterwarnings("ignore")

WINDOW = 500
PERCENTILE = 95.0
WARMUP = 200


def rolling_percentile_picks(stream: pd.DataFrame, window: int, percentile: float, warmup: int, value_col: str) -> pd.DataFrame:
    df = stream.dropna(subset=[value_col]).sort_values("date").reset_index(drop=True)
    trailing: deque = deque(maxlen=window)
    picked_idx = []
    for i, row in df.iterrows():
        if len(trailing) >= warmup:
            bar = pd.Series(trailing).quantile(percentile / 100.0)
            if row[value_col] >= bar:
                picked_idx.append(i)
        trailing.append(row[value_col])
    return df.loc[picked_idx]


def main() -> None:
    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)

    print("Building out-of-fold prediction streams...")
    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core").rename(columns={"pred_p": "pred_p_core"})
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")
    xg_small = xg_stream[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_small, on="fixture_id", how="left")
    merged["pred_p_raw"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    calibrators = load_calibrators()
    stream["pred_p"] = apply_calibration(stream["pred_p_raw"], stream["model_used"], calibrators)

    flipped = stream.copy()
    flipped["under_p"] = 1 - flipped["pred_p"]
    under_picks = rolling_percentile_picks(flipped, WINDOW, PERCENTILE, WARMUP, "under_p")
    under_picks = under_picks.copy()
    under_picks["actual_under"] = ~under_picks["over_2_5"].astype(bool)

    pl_picks = under_picks[under_picks["competition"] == "PL"].copy()
    print(f"\n{len(pl_picks)} PL fixtures in the top-5% most-confident-Under pool")

    all_finished = fetch_all_fixtures(None)
    goals_by_fixture = {m["fixture_id"]: (m["home_goals"], m["away_goals"]) for m in all_finished}

    pl_picks["year"] = pd.to_datetime(pl_picks["date"]).dt.year
    pl_picks = pl_picks.sort_values("date")

    print("\n=== All 42 PL fixtures in this pool, with actual scorelines ===")
    for _, r in pl_picks.iterrows():
        goals = goals_by_fixture.get(r["fixture_id"])
        score = f"{goals[0]}-{goals[1]}" if goals else "?"
        total = sum(goals) if goals else None
        outcome = "UNDER (hit)" if r["actual_under"] else "OVER (miss)"
        print(f"  {r['date'].date()}  {r['home_team']:<20s} vs {r['away_team']:<20s}  "
              f"score={score:<6s} total={total}  under_p={r['under_p']*100:.1f}%  [{r['model_used']}]  {outcome}")

    print("\n=== By season ===")
    for yr, grp in pl_picks.groupby("year"):
        hit_rate = grp["actual_under"].mean()
        print(f"  {yr}: n={len(grp)}  hit rate={hit_rate*100:.1f}%  mean under_p={grp['under_p'].mean()*100:.1f}%")

    print("\n=== By model used ===")
    for mu, grp in pl_picks.groupby("model_used"):
        hit_rate = grp["actual_under"].mean()
        print(f"  {mu}: n={len(grp)}  hit rate={hit_rate*100:.1f}%  mean under_p={grp['under_p'].mean()*100:.1f}%")

    # Team frequency -- is this dominated by a handful of repeat teams?
    print("\n=== Most frequent teams in this pool (home or away) ===")
    team_counts = pd.concat([pl_picks["home_team"], pl_picks["away_team"]]).value_counts()
    for team, count in team_counts.head(15).items():
        print(f"  {team:<20s} {count}")

    # Feature signature: pull key features for these fixtures directly from df.
    key_feats = ["h2h_avg_goals_shrunk", "attacking_form_total", "goal_diff_gap", "home_gf_season",
                 "away_gf_season", "clean_sheet_pct_combined_last5", "position_gap"]
    available = [f for f in key_feats if f in df.columns]
    feat_df = df.set_index("fixture_id")[available]
    joined = pl_picks.set_index("fixture_id").join(feat_df, how="left")

    print(f"\n=== Mean feature values: PL Under-pool hits vs misses ===")
    hits = joined[joined["actual_under"]]
    misses = joined[~joined["actual_under"]]
    print(f"{'feature':<32s}{'hits (n=' + str(len(hits)) + ')':<18s}{'misses (n=' + str(len(misses)) + ')'}")
    for f in available:
        print(f"{f:<32s}{hits[f].mean():<18.3f}{misses[f].mean():.3f}")


if __name__ == "__main__":
    main()
