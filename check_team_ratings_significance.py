#!/usr/bin/env python3
"""check_team_ratings_full_dataset.py's decisive test is full-dataset L1
coefficient survival, and that passed clearly. But its SECONDARY
out-of-sample Brier/AUC comparison (baseline 0.2412/0.591 vs combine
0.2406/0.595) was reported as a plain point-estimate difference with no
check on whether it's distinguishable from sampling noise on ~5337
fixtures -- exactly the kind of unverified "looks better" claim this
project has been burned by before. This bootstraps the SAME two
out-of-fold prediction streams (baseline XG_CANDIDATES vs combine
XG_CANDIDATES+ratings) to put a confidence interval on the Brier and
AUC deltas.

Usage:
    python3 check_team_ratings_significance.py
"""

import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from backtest_season_rolling_percentile import N_FOLDS_XG, build_stream
from calibration import apply_calibration, load_calibrators
from check_team_ratings_full_dataset import RATINGS_ALL, load_data
from predict_upcoming import XG_CANDIDATES

warnings.filterwarnings("ignore")

N_BOOTSTRAP = 3000
SEED = 0


def main() -> int:
    print("Loading data and rebuilding the two out-of-fold streams (baseline vs combine)...")
    df = load_data()
    calibrators = load_calibrators()

    # XG_CANDIDATES now already includes the ratings features (wired in
    # live after check_team_ratings_full_dataset.py's run approved them) --
    # so "baseline" here is XG_CANDIDATES MINUS ratings, and "combine" is
    # XG_CANDIDATES as-is, not XG_CANDIDATES + RATINGS_ALL (which would
    # double every ratings column).
    combine_features = XG_CANDIDATES
    baseline_features = [f for f in XG_CANDIDATES if f not in RATINGS_ALL]

    def score_variant(label: str, features: list[str]) -> pd.DataFrame:
        stream = build_stream(df, features, N_FOLDS_XG, label)
        stream["pred_p_cal"] = apply_calibration(stream["pred_p"], pd.Series(["xg"] * len(stream)), calibrators)
        return stream

    base_stream = score_variant("baseline", baseline_features)
    combo_stream = score_variant("combine", combine_features)

    merged = base_stream[["fixture_id", "over_2_5", "pred_p_cal"]].rename(columns={"pred_p_cal": "p_base"}).merge(
        combo_stream[["fixture_id", "pred_p_cal"]].rename(columns={"pred_p_cal": "p_combo"}),
        on="fixture_id", how="inner",
    )
    y = merged["over_2_5"].astype(int).values
    p_base = merged["p_base"].astype(float).values
    p_combo = merged["p_combo"].astype(float).values
    n = len(y)
    print(f"\n{n} fixtures scored by both variants (same out-of-fold rows)")

    brier_base = brier_score_loss(y, p_base)
    brier_combo = brier_score_loss(y, p_combo)
    auc_base = roc_auc_score(y, p_base)
    auc_combo = roc_auc_score(y, p_combo)
    print(f"\nPoint estimates:")
    print(f"  Brier: baseline={brier_base:.4f}  combine={brier_combo:.4f}  delta={brier_combo - brier_base:+.4f} (negative = combine better)")
    print(f"  AUC:   baseline={auc_base:.4f}  combine={auc_combo:.4f}  delta={auc_combo - auc_base:+.4f} (positive = combine better)")

    rng = np.random.default_rng(SEED)
    brier_deltas = np.empty(N_BOOTSTRAP)
    auc_deltas = np.empty(N_BOOTSTRAP)
    for i in range(N_BOOTSTRAP):
        idx = rng.integers(0, n, n)
        yb, pb, pc = y[idx], p_base[idx], p_combo[idx]
        brier_deltas[i] = brier_score_loss(yb, pc) - brier_score_loss(yb, pb)
        auc_deltas[i] = roc_auc_score(yb, pc) - roc_auc_score(yb, pb) if len(set(yb)) > 1 else np.nan

    auc_deltas = auc_deltas[~np.isnan(auc_deltas)]

    brier_lo, brier_hi = np.percentile(brier_deltas, [2.5, 97.5])
    auc_lo, auc_hi = np.percentile(auc_deltas, [2.5, 97.5])
    brier_p_combo_better = (brier_deltas < 0).mean()
    auc_p_combo_better = (auc_deltas > 0).mean()

    print(f"\n{N_BOOTSTRAP}-resample paired bootstrap (resampling fixtures, both variants scored on the same resample):")
    print(f"  Brier delta 95% CI: [{brier_lo:+.4f}, {brier_hi:+.4f}]  (combine better in {brier_p_combo_better*100:.1f}% of resamples)")
    print(f"  AUC   delta 95% CI: [{auc_lo:+.4f}, {auc_hi:+.4f}]  (combine better in {auc_p_combo_better*100:.1f}% of resamples)")

    if brier_lo < 0 < brier_hi and auc_lo < 0 < auc_hi:
        print("\nVERDICT: both CIs straddle zero -- the OOS Brier/AUC edge is NOT statistically distinguishable "
              "from noise at n~5337. The full-dataset L1 survival result stands on its own regardless (that's "
              "the decisive test), but the OOS numbers should not be cited as independent corroborating evidence.")
    else:
        print("\nVERDICT: at least one CI excludes zero -- the OOS edge is not just noise, corroborating the "
              "full-dataset L1 result.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
