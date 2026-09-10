#!/usr/bin/env python3
"""Gate for the Champions League feature set (build_champions_league_features.py)
against the same decisive test every other feature in this project was
gated on: does full-dataset L1 (LogisticRegressionCV, Cs=15, cv=5,
penalty="l1", solver="liblinear") keep non-zero coefficients on the new
features, and does out-of-sample walk-forward Brier/AUC look reasonable.

Sample size here (494 matches at last count -- UCL fixtures 2019+ where
both teams are from the 7 relevant tracked leagues) is far smaller than
the domestic full-dataset checks (tens of thousands of rows), so this
result should be read as a first-pass sanity check, not the same
strength of evidence the domestic features got. Fewer walk-forward
folds (3, vs N_FOLDS_XG=4 domestically) to keep each training slice
large enough to mean anything.

Usage:
    python3 check_champions_league_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from backtest_season_rolling_percentile import build_stream
from build_champions_league_features import ALL_FEATURES, OUTPUT_PATH

warnings.filterwarnings("ignore")

N_FOLDS = 3


def main() -> int:
    df = pd.read_csv(OUTPUT_PATH)
    print(f"Loaded {len(df)} UCL target matches")

    model_df = df[ALL_FEATURES + ["over_2_5"]].dropna()
    print(f"{len(model_df)} rows with complete features (no cold-start gaps)")

    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[ALL_FEATURES])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X, model_df["over_2_5"])

    print("\n=== Full-dataset L1 coefficients ===")
    coefs = sorted(zip(ALL_FEATURES, model.coef_[0]), key=lambda x: -abs(x[1]))
    for name, coef in coefs:
        marker = "" if abs(coef) > 1e-6 else "  (zeroed by L1)"
        print(f"  {name:35s} {coef:+.4f}{marker}")
    n_survived = sum(1 for _, c in coefs if abs(c) > 1e-6)
    print(f"\n{n_survived}/{len(ALL_FEATURES)} features survive L1")

    print(f"\n=== Out-of-sample walk-forward ({N_FOLDS} folds) ===")
    stream = build_stream(df.assign(competition="UCL"), ALL_FEATURES, N_FOLDS, "UCL")
    if stream.empty:
        print("No out-of-sample predictions -- sample too small for this fold count.")
        return 0

    y = stream["over_2_5"].astype(int).values
    p = stream["pred_p"].astype(float).values
    brier = brier_score_loss(y, p)
    auc = roc_auc_score(y, p)
    hit_rate_over = y.mean()
    print(f"n_oos={len(y)}  Brier={brier:.4f}  AUC={auc:.3f}  base_rate_over={hit_rate_over*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
