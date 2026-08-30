#!/usr/bin/env python3
"""Rolling-origin validation of the real-xG features — same discipline
as rolling_validation.py / rolling_validation_player_form.py, scaled
down to fewer folds since the xG-complete-case sample (2,783 matches,
2023-02 to 2026-08) is much smaller than the main dataset's — this is
the real test of whether the single-split result (AUC 0.502 -> 0.542)
holds up, or is a favorable draw like player-form's was.

Usage:
    python3 rolling_validation_xg.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES
from analyze_xg_features import XG_DERIVED_FEATURES, XG_RAW_FEATURES, load_with_xg

warnings.filterwarnings("ignore")

N_FOLDS = 4  # fewer than the usual 5 -- smaller sample here
TOP_PCT = 0.05


def fit_and_evaluate(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> dict:
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])

    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X_train, train["over_2_5"])
    pred_prob = model.predict_proba(X_test)[:, 1]

    auc = roc_auc_score(test["over_2_5"], pred_prob)
    test = test.copy()
    test["pred_p"] = pred_prob
    n_top = max(1, int(len(test) * TOP_PCT))
    top = test.sort_values("pred_p", ascending=False).iloc[:n_top]

    return {
        "auc": auc, "top_n": len(top), "top_hit_rate": top["over_2_5"].mean(),
        "top_results": top["over_2_5"].tolist(),
    }


def run_rolling(model_df: pd.DataFrame, features: list[str], label: str) -> None:
    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    print(f"\n=== {label} ({len(features)} features) ===")
    all_top = []
    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]
        r = fit_and_evaluate(train, test, features)
        all_top += r["top_results"]
        print(f"  Fold {fold}: train={len(train)} test={len(test)} "
              f"({test['date'].min().date()} to {test['date'].max().date()})  AUC={r['auc']:.3f}  "
              f"top-{TOP_PCT*100:.0f}% hit rate={r['top_hit_rate']*100:.1f}% (n={r['top_n']})")

    baseline = model_df["over_2_5"].mean()
    n = len(all_top)
    hits = sum(all_top)
    expected = n * baseline
    std = sqrt(n * baseline * (1 - baseline))
    z = (hits - expected) / std if std > 0 else 0.0
    p = 0.5 * (1 - erf(z / sqrt(2)))
    print(f"  Combined: {hits}/{n} = {hits/n*100:.1f}% vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


def main() -> None:
    df = load_with_xg()
    new_candidates = XG_RAW_FEATURES + XG_DERIVED_FEATURES
    extended_candidates = ALL_CANDIDATES + new_candidates

    model_df = df[extended_candidates + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}")

    run_rolling(model_df, ALL_CANDIDATES, "WITHOUT xG features")
    run_rolling(model_df, extended_candidates, "WITH xG features")


if __name__ == "__main__":
    main()
