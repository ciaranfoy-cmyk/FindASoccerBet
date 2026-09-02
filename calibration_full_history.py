#!/usr/bin/env python3
"""How good is the model, really -- independent of any betting odds
assumption? Two questions:

1. Calibration: when the model says "60% chance of over 2.5", does that
   actually happen ~60% of the time, out of sample, across the model's
   entire validated history? (Not just one season -- pools every
   out-of-fold prediction the hybrid core/xG model has ever produced,
   same construction as backtest_season_rolling_percentile.py's stream,
   just not restricted to a single season's window.)

2. Rolling-percentile vs top-5/week: which selection RULE actually picks
   more accurate games, on the exact same underlying predictions? This
   is a pure hit-rate comparison, with no odds or P&L assumption at all
   -- answers "which is the better model/strategy" before any question
   of what it's worth in dollars.

Usage:
    python3 calibration_full_history.py
"""

import warnings
from collections import deque

import numpy as np
import pandas as pd

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

warnings.filterwarnings("ignore")

TOP_N_PER_WEEK = 5
PERCENTILE = 95.0
WINDOW = 500
WARMUP = 200


def calibration_table(stream: pd.DataFrame, label: str) -> None:
    df = stream.dropna(subset=["pred_p"]).copy()
    df["decile"] = pd.qcut(df["pred_p"], 10, labels=False, duplicates="drop")
    print(f"\n=== Calibration: {label} (n={len(df)}, out-of-sample) ===")
    print(f"{'Predicted P range':<22}{'n':<8}{'Predicted (mean)':<20}{'Actual over-2.5 rate':<22}{'Gap'}")
    for d in sorted(df["decile"].unique()):
        sub = df[df["decile"] == d]
        pred_mean = sub["pred_p"].mean()
        actual = sub["over_2_5"].mean()
        gap = actual - pred_mean
        print(f"{sub['pred_p'].min()*100:5.1f}%-{sub['pred_p'].max()*100:5.1f}%     "
              f"{len(sub):<8}{pred_mean*100:6.1f}%             {actual*100:6.1f}%                "
              f"{gap*100:+.1f}pp")


def top_n_per_week_picks(stream: pd.DataFrame, n: int) -> pd.DataFrame:
    df = stream.dropna(subset=["pred_p"]).copy()
    df["week"] = df["date"].dt.to_period("W")
    picks = df.sort_values("pred_p", ascending=False).groupby("week").head(n)
    return picks


def rolling_percentile_picks(stream: pd.DataFrame, window: int, percentile: float, warmup: int) -> pd.DataFrame:
    df = stream.dropna(subset=["pred_p"]).sort_values("date").reset_index(drop=True)
    trailing: deque = deque(maxlen=window)
    picked_idx = []
    for i, row in df.iterrows():
        if len(trailing) >= warmup:
            bar = pd.Series(trailing).quantile(percentile / 100.0)
            if row["pred_p"] >= bar:
                picked_idx.append(i)
        trailing.append(row["pred_p"])
    return df.loc[picked_idx]


def report_picks(name: str, picks: pd.DataFrame, baseline: float) -> None:
    n = len(picks)
    hits = picks["over_2_5"].sum()
    rate = hits / n if n else 0.0
    print(f"  {name:<28s} n={n:<6d} hit rate={rate*100:.1f}%  "
          f"(mean predicted P={picks['pred_p'].mean()*100:.1f}%, baseline={baseline*100:.1f}%)")


def main() -> None:
    df = load_with_xg_player_form_and_shots_venue()

    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core")
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")

    core_stream = core_stream.rename(columns={"pred_p": "pred_p_core"})
    xg_small = xg_stream[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_small, on="fixture_id", how="left")
    merged["pred_p"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    baseline = stream["over_2_5"].mean()
    print(f"Full out-of-fold hybrid stream: {len(stream)} games, "
          f"{stream['date'].min().date()} to {stream['date'].max().date()}")
    print(f"Baseline over-2.5 rate: {baseline*100:.1f}%")
    print(f"Predicted P range: {stream['pred_p'].min()*100:.1f}% to {stream['pred_p'].max()*100:.1f}% "
          f"(mean {stream['pred_p'].mean()*100:.1f}%)")

    calibration_table(stream, "full hybrid model, all out-of-fold predictions")
    calibration_table(stream[stream["model_used"] == "xG"], "xG-model predictions only")
    calibration_table(stream[stream["model_used"] == "core"], "core-model predictions only")

    print("\n" + "=" * 90)
    print("Rolling-percentile vs top-5/week: pure hit-rate comparison, same underlying predictions")
    print("=" * 90)
    top5_picks = top_n_per_week_picks(stream, TOP_N_PER_WEEK)
    rollpct_picks = rolling_percentile_picks(stream, WINDOW, PERCENTILE, WARMUP)
    report_picks(f"Top-{TOP_N_PER_WEEK}/week", top5_picks, baseline)
    report_picks(f"Rolling p{PERCENTILE:.0f} (window={WINDOW})", rollpct_picks, baseline)

    # Also show top-1/week and a few alternate rolling bars for context.
    top1_picks = top_n_per_week_picks(stream, 1)
    report_picks("Top-1/week", top1_picks, baseline)
    for pct in (90.0, 97.0, 99.0):
        picks = rolling_percentile_picks(stream, WINDOW, pct, WARMUP)
        report_picks(f"Rolling p{pct:.0f} (window={WINDOW})", picks, baseline)


if __name__ == "__main__":
    main()
