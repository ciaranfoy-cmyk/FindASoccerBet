#!/usr/bin/env python3
"""Why is the model's Brier score on its most-confident UNDER calls
(0.2495) worse than on its most-confident OVER calls (0.2045)? Earlier
this session, isolating Championship vs non-Championship subsets ruled
out "it's just low-scoring leagues" as the explanation (non-Championship
was actually MORE overconfident). This digs further: pulls the actual
top-5% most-confident Under picks (rolling p95 on the flipped stream,
same construction as the Over-side selection), prints real fixtures with
actual outcomes, breaks down by competition/season/scoreline, and
inspects the biggest misses (confident Under calls that actually went
Over) for a real, concrete pattern -- not just an aggregate number.

Usage:
    python3 diagnose_under_overconfidence.py
"""

import warnings
from collections import deque

import pandas as pd
from sklearn.metrics import brier_score_loss

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
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

    # Flip to "Under confidence" and take the top-5% most confident Under calls.
    flipped = stream.copy()
    flipped["under_p"] = 1 - flipped["pred_p"]
    under_picks = rolling_percentile_picks(flipped, WINDOW, PERCENTILE, WARMUP, "under_p")
    under_picks = under_picks.copy()
    under_picks["actual_under"] = ~under_picks["over_2_5"].astype(bool)
    under_picks["hit"] = under_picks["actual_under"]

    brier = brier_score_loss(under_picks["actual_under"], under_picks["under_p"])
    hit_rate = under_picks["hit"].mean()
    print(f"\nTop-5% most confident UNDER calls: n={len(under_picks)}  "
          f"hit rate={hit_rate*100:.1f}%  mean predicted under_p={under_picks['under_p'].mean()*100:.1f}%  "
          f"Brier={brier:.4f}")

    print("\n=== Breakdown by competition ===")
    for comp, grp in under_picks.groupby("competition"):
        b = brier_score_loss(grp["actual_under"], grp["under_p"]) if len(grp) > 1 else float("nan")
        print(f"  {comp:<12s} n={len(grp):<5d} hit rate={grp['hit'].mean()*100:5.1f}%  "
              f"mean under_p={grp['under_p'].mean()*100:5.1f}%  Brier={b:.4f}")

    print("\n=== Breakdown by season/year ===")
    under_picks["year"] = pd.to_datetime(under_picks["date"]).dt.year
    for yr, grp in under_picks.groupby("year"):
        print(f"  {yr}: n={len(grp):<5d} hit rate={grp['hit'].mean()*100:5.1f}%  mean under_p={grp['under_p'].mean()*100:5.1f}%")

    print("\n=== Breakdown by model used ===")
    for mu, grp in under_picks.groupby("model_used"):
        b = brier_score_loss(grp["actual_under"], grp["under_p"]) if len(grp) > 1 else float("nan")
        print(f"  {mu:<6s} n={len(grp):<5d} hit rate={grp['hit'].mean()*100:5.1f}%  Brier={b:.4f}")

    # The actual misses: most confident Under calls that were WRONG (actual was over).
    misses = under_picks[~under_picks["actual_under"]].sort_values("under_p", ascending=False)
    print(f"\n=== Biggest misses: most-confident-Under calls that actually went OVER (n={len(misses)}/{len(under_picks)}) ===")
    goals_lookup = df.set_index("fixture_id")[["home_gf_last5", "away_gf_last5"]] if "home_gf_last5" in df.columns else None
    for _, r in misses.head(25).iterrows():
        print(f"  {r['date'].date()}  [{r['competition']}] {r['home_team']:<20s} vs {r['away_team']:<20s}  "
              f"under_p={r['under_p']*100:.1f}%  model={r['model_used']}")

    print(f"\n=== Distribution of under_p among misses vs hits ===")
    print(f"  Misses (actual over): mean under_p = {misses['under_p'].mean()*100:.1f}%, n={len(misses)}")
    hits_df = under_picks[under_picks["actual_under"]]
    print(f"  Hits   (actual under): mean under_p = {hits_df['under_p'].mean()*100:.1f}%, n={len(hits_df)}")

    # Full calibration table on the Under-confidence axis, whole dataset (not just top 5%)
    print("\n=== Full calibration table: under_p vs actual under-rate, ALL out-of-fold predictions ===")
    full = flipped.dropna(subset=["under_p"]).copy()
    full["decile"] = pd.qcut(full["under_p"], 10, labels=False, duplicates="drop")
    print(f"{'Predicted under_p range':<26}{'n':<8}{'Predicted (mean)':<20}{'Actual under rate':<20}{'Gap'}")
    for d in sorted(full["decile"].unique()):
        sub = full[full["decile"] == d]
        pred_mean = sub["under_p"].mean()
        actual = (~sub["over_2_5"].astype(bool)).mean()
        gap = actual - pred_mean
        print(f"{sub['under_p'].min()*100:5.1f}%-{sub['under_p'].max()*100:5.1f}%       "
              f"{len(sub):<8}{pred_mean*100:6.1f}%             {actual*100:6.1f}%              {gap*100:+.1f}pp")


if __name__ == "__main__":
    main()
