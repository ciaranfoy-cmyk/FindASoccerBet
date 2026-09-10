#!/usr/bin/env python3
"""The early-season confidence bar (forward_test_log.compute_rolling_p95_bar
with early_season_only=True) is what actually gates most real picks right
now -- most fixtures early in a season have <= EARLY_SEASON_CUTOFF games
played, so this bar binds far more often live than the plain rolling-p95
bar check_confidence_bar_sweep.py validated. That sweep never touched the
early-season variant at all; its own validation (EARLY_SEASON_CUTOFF's
docstring in forward_test_log.py) is a single fixed point (pct=95,
window=500) checked on n=36 -- never swept against alternatives the way
the regular bar was.

Same method as check_confidence_bar_sweep.py: build the out-of-fold
calibrated stream once, restrict it to early-season rows only (min games
played by either team <= EARLY_SEASON_CUTOFF, via
forward_test_log._games_into_season_lookup(), same lookup the live bar
itself uses), then sweep PERCENTILE and WINDOW with the same causal
rolling-percentile selection rule. Small base population (early-season
fixtures are a fraction of the full stream), so this is explicitly a
low-n exercise -- reported n's should be read with that in mind, same
caveat forward_test_log.py already carries for this bar.

Usage:
    python3 check_early_season_bar_sweep.py
"""

import warnings
from collections import deque

import pandas as pd
from sklearn.metrics import brier_score_loss

from forward_test_log import EARLY_SEASON_CUTOFF, _calibrated_stream, _games_into_season_lookup

warnings.filterwarnings("ignore")

DEFAULT_PERCENTILE = 95.0
DEFAULT_WINDOW = 500
DEFAULT_WARMUP = 200

PERCENTILE_CANDIDATES = [85.0, 90.0, 92.5, 95.0, 97.5, 99.0]
WINDOW_CANDIDATES = [100, 150, 250, 500]


def rolling_percentile_picks(df: pd.DataFrame, value_col: str, window: int, percentile: float, warmup: int) -> pd.DataFrame:
    """Same causal construction as check_confidence_bar_sweep.py / the
    live compute_rolling_p95_bar -- a fixture's bar comes only from
    predictions strictly before it, never itself or later ones."""
    df = df.dropna(subset=[value_col]).sort_values("date").reset_index(drop=True)
    trailing: deque = deque(maxlen=window)
    picked_idx = []
    for i, row in df.iterrows():
        if len(trailing) >= warmup:
            bar = pd.Series(trailing).quantile(percentile / 100.0)
            if row[value_col] >= bar:
                picked_idx.append(i)
        trailing.append(row[value_col])
    return df.loc[picked_idx]


def evaluate(df: pd.DataFrame, value_col: str, outcome_col: str, window: int, percentile: float, warmup: int, label: str) -> dict:
    picks = rolling_percentile_picks(df, value_col, window, percentile, warmup)
    if picks.empty:
        print(f"  [{label}] n=0 -- no picks at this setting")
        return {"label": label, "window": window, "percentile": percentile, "n": 0,
                "hit_rate": float("nan"), "mean_pred": float("nan"), "calib_gap": float("nan"), "brier": float("nan")}
    y = picks[outcome_col].astype(int).values
    p = picks[value_col].astype(float).values
    hit_rate = y.mean()
    mean_pred = p.mean()
    brier = brier_score_loss(y, p)
    print(f"  [{label}] n={len(y):<5} hit_rate={hit_rate*100:5.1f}%  mean_pred={mean_pred*100:5.1f}%  "
          f"calib_gap={(hit_rate-mean_pred)*100:+5.1f}pp  Brier={brier:.4f}")
    return {"label": label, "window": window, "percentile": percentile, "n": len(y),
            "hit_rate": hit_rate, "mean_pred": mean_pred, "calib_gap": hit_rate - mean_pred, "brier": brier}


def main() -> int:
    print("Building the out-of-fold calibrated stream once (this is the expensive part)...")
    stream = _calibrated_stream()
    print(f"  {len(stream)} out-of-fold predictions total")

    print(f"Restricting to early-season rows (min games played <= {EARLY_SEASON_CUTOFF}, same lookup the live bar uses)...")
    gis = _games_into_season_lookup()
    stream = stream.merge(gis, on="fixture_id", how="left")
    stream = stream[stream["min_games_into_season"] <= EARLY_SEASON_CUTOFF]
    stream["under_p"] = 1 - stream["pred_p"]
    stream["under_2_5"] = 1 - stream["over_2_5"]
    print(f"  {len(stream)} early-season out-of-fold predictions (this is the whole population being swept)")

    results_over = []
    results_under = []

    print(f"\n=== OVER side (early-season only): sweep PERCENTILE (WINDOW fixed at {DEFAULT_WINDOW}) ===")
    for pct in PERCENTILE_CANDIDATES:
        marker = " <-- current default" if pct == DEFAULT_PERCENTILE else ""
        results_over.append(evaluate(stream, "pred_p", "over_2_5", DEFAULT_WINDOW, pct, DEFAULT_WARMUP, f"pct={pct}{marker}"))

    print(f"\n=== OVER side (early-season only): sweep WINDOW (PERCENTILE fixed at {DEFAULT_PERCENTILE}) ===")
    for win in WINDOW_CANDIDATES:
        marker = " <-- current default" if win == DEFAULT_WINDOW else ""
        results_over.append(evaluate(stream, "pred_p", "over_2_5", win, DEFAULT_PERCENTILE, DEFAULT_WARMUP, f"win={win}{marker}"))

    print(f"\n=== UNDER side (early-season only): sweep PERCENTILE (WINDOW fixed at {DEFAULT_WINDOW}) ===")
    for pct in PERCENTILE_CANDIDATES:
        marker = " <-- current default" if pct == DEFAULT_PERCENTILE else ""
        results_under.append(evaluate(stream, "under_p", "under_2_5", DEFAULT_WINDOW, pct, DEFAULT_WARMUP, f"pct={pct}{marker}"))

    print(f"\n=== UNDER side (early-season only): sweep WINDOW (PERCENTILE fixed at {DEFAULT_PERCENTILE}) ===")
    for win in WINDOW_CANDIDATES:
        marker = " <-- current default" if win == DEFAULT_WINDOW else ""
        results_under.append(evaluate(stream, "under_p", "under_2_5", win, DEFAULT_PERCENTILE, DEFAULT_WARMUP, f"win={win}{marker}"))

    print("\n=== OVER (early-season) summary, sorted by calibration gap (closest to 0 = best-calibrated) ===")
    for r in sorted(results_over, key=lambda r: abs(r["calib_gap"]) if r["n"] > 0 else 999):
        print(f"  {r['label']:25s} n={r['n']:<5} hit_rate={r['hit_rate']*100:5.1f}%  gap={r['calib_gap']*100:+5.1f}pp  Brier={r['brier']:.4f}")

    print("\n=== UNDER (early-season) summary, sorted by calibration gap (closest to 0 = best-calibrated) ===")
    for r in sorted(results_under, key=lambda r: abs(r["calib_gap"]) if r["n"] > 0 else 999):
        print(f"  {r['label']:25s} n={r['n']:<5} hit_rate={r['hit_rate']*100:5.1f}%  gap={r['calib_gap']*100:+5.1f}pp  Brier={r['brier']:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
