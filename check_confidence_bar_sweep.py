#!/usr/bin/env python3
"""The live selection bar (forward_test_log.compute_rolling_p95_bar) --
the actual gate deciding whether any fixture becomes a real pick -- has
never been swept. PERCENTILE=95.0, WINDOW=500, WARMUP=200 show up as
named constants in calibration_full_history.py and both diagnose_*.py
scripts, but nowhere is 95th-percentile-of-trailing-500 compared
against any alternative. This is the single highest-leverage untested
number in the whole system: unlike a model feature, it isn't "one
input among many" -- it's the literal threshold between "real pick"
and "reference only" for every fixture, every night.

Reuses forward_test_log._calibrated_stream() (the exact same
walk-forward out-of-fold prediction stream the live bar is computed
from) so this sweep is cheap: build the stream ONCE, then re-run the
causal rolling-percentile selection rule (same construction as
calibration.py's rolling_percentile_picks -- trailing deque, warmup,
percentile of the trailing window, select if this prediction clears
it, THEN append it to the trailing window) across a grid of
(percentile, window) pairs. No retraining involved.

Usage:
    python3 check_confidence_bar_sweep.py
"""

import warnings
from collections import deque

import pandas as pd
from sklearn.metrics import brier_score_loss

from forward_test_log import _calibrated_stream

warnings.filterwarnings("ignore")

DEFAULT_PERCENTILE = 95.0
DEFAULT_WINDOW = 500
DEFAULT_WARMUP = 200

PERCENTILE_CANDIDATES = [85.0, 90.0, 92.5, 95.0, 97.5, 99.0]
WINDOW_CANDIDATES = [250, 500, 750, 1000]


def rolling_percentile_picks(df: pd.DataFrame, value_col: str, window: int, percentile: float, warmup: int) -> pd.DataFrame:
    """Same causal construction as calibration.py's rolling_percentile_picks
    and the live compute_rolling_p95_bar -- a fixture's bar comes only
    from predictions strictly before it, never itself or later ones."""
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
    stream["under_p"] = 1 - stream["pred_p"]
    stream["under_2_5"] = 1 - stream["over_2_5"]
    print(f"  {len(stream)} out-of-fold predictions")

    results_over = []
    results_under = []

    print(f"\n=== OVER side: sweep PERCENTILE (WINDOW fixed at {DEFAULT_WINDOW}) ===")
    for pct in PERCENTILE_CANDIDATES:
        marker = " <-- current default" if pct == DEFAULT_PERCENTILE else ""
        results_over.append(evaluate(stream, "pred_p", "over_2_5", DEFAULT_WINDOW, pct, DEFAULT_WARMUP, f"pct={pct}{marker}"))

    print(f"\n=== OVER side: sweep WINDOW (PERCENTILE fixed at {DEFAULT_PERCENTILE}) ===")
    for win in WINDOW_CANDIDATES:
        marker = " <-- current default" if win == DEFAULT_WINDOW else ""
        results_over.append(evaluate(stream, "pred_p", "over_2_5", win, DEFAULT_PERCENTILE, DEFAULT_WARMUP, f"win={win}{marker}"))

    print(f"\n=== UNDER side: sweep PERCENTILE (WINDOW fixed at {DEFAULT_WINDOW}) ===")
    for pct in PERCENTILE_CANDIDATES:
        marker = " <-- current default" if pct == DEFAULT_PERCENTILE else ""
        results_under.append(evaluate(stream, "under_p", "under_2_5", DEFAULT_WINDOW, pct, DEFAULT_WARMUP, f"pct={pct}{marker}"))

    print(f"\n=== UNDER side: sweep WINDOW (PERCENTILE fixed at {DEFAULT_PERCENTILE}) ===")
    for win in WINDOW_CANDIDATES:
        marker = " <-- current default" if win == DEFAULT_WINDOW else ""
        results_under.append(evaluate(stream, "under_p", "under_2_5", win, DEFAULT_PERCENTILE, DEFAULT_WARMUP, f"win={win}{marker}"))

    print("\n=== OVER summary, sorted by calibration gap (closest to 0 = best-calibrated) ===")
    for r in sorted(results_over, key=lambda r: abs(r["calib_gap"]) if r["n"] > 0 else 999):
        print(f"  {r['label']:25s} n={r['n']:<5} hit_rate={r['hit_rate']*100:5.1f}%  gap={r['calib_gap']*100:+5.1f}pp  Brier={r['brier']:.4f}")

    print("\n=== UNDER summary, sorted by calibration gap (closest to 0 = best-calibrated) ===")
    for r in sorted(results_under, key=lambda r: abs(r["calib_gap"]) if r["n"] > 0 else 999):
        print(f"  {r['label']:25s} n={r['n']:<5} hit_rate={r['hit_rate']*100:5.1f}%  gap={r['calib_gap']*100:+5.1f}pp  Brier={r['brier']:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
