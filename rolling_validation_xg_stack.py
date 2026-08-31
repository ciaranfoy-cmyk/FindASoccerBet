#!/usr/bin/env python3
"""Test a STACKED core+xG model: instead of averaging the two models'
predictions equally every time (already tested, worse than xG-only), train
a small meta-model that learns WHEN to trust which one -- e.g. lean
toward whichever model has historically been more reliable when the two
disagree.

Proper stacking needs inner cross-validation: the meta-model can't train
on predictions from base models that already saw those exact rows during
their own fitting (that's a leak -- the base model's in-sample
prediction is artificially close to the truth). So for each outer
rolling-origin fold's training data, we generate genuinely out-of-fold
core/xG predictions via 5-fold inner CV, fit the meta-model on those,
then apply base models trained on the FULL training data to the real
test fold and combine via the meta-model.

Usage:
    python3 rolling_validation_xg_stack.py
"""

import warnings
from math import erf, sqrt

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import KFold
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES
from analyze_xg_features import XG_DERIVED_FEATURES, XG_RAW_FEATURES, load_with_xg

warnings.filterwarnings("ignore")

N_FOLDS = 4
INNER_FOLDS = 5
TOP_PCT = 0.05


def fit_base_model(train: pd.DataFrame, features: list[str]):
    scaler = StandardScaler()
    X = scaler.fit_transform(train[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, train["over_2_5"])
    return model, scaler


def predict_with(model, scaler, df: pd.DataFrame, features: list[str]) -> np.ndarray:
    return model.predict_proba(scaler.transform(df[features]))[:, 1]


def main() -> None:
    df = load_with_xg()
    xg_candidates = ALL_CANDIDATES + XG_RAW_FEATURES + XG_DERIVED_FEATURES
    model_df = df[xg_candidates + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    results = {"core": [], "xG": [], "stacked": []}

    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end].reset_index(drop=True), model_df.iloc[test_start:test_end]

        # --- Inner CV: generate out-of-fold core/xG predictions on TRAIN ---
        oof_core = np.zeros(len(train))
        oof_xg = np.zeros(len(train))
        kf = KFold(n_splits=INNER_FOLDS, shuffle=False)
        for inner_train_idx, inner_val_idx in kf.split(train):
            inner_train, inner_val = train.iloc[inner_train_idx], train.iloc[inner_val_idx]
            core_m, core_s = fit_base_model(inner_train, ALL_CANDIDATES)
            xg_m, xg_s = fit_base_model(inner_train, xg_candidates)
            oof_core[inner_val_idx] = predict_with(core_m, core_s, inner_val, ALL_CANDIDATES)
            oof_xg[inner_val_idx] = predict_with(xg_m, xg_s, inner_val, xg_candidates)

        # --- Meta-model: learns how to combine core_pred + xg_pred + their disagreement ---
        meta_X_train = np.column_stack([oof_core, oof_xg, np.abs(oof_core - oof_xg)])
        meta_model = LogisticRegression(max_iter=2000)
        meta_model.fit(meta_X_train, train["over_2_5"])

        # --- Base models trained on FULL training data, applied to the real test fold ---
        core_m, core_s = fit_base_model(train, ALL_CANDIDATES)
        xg_m, xg_s = fit_base_model(train, xg_candidates)
        pred_core = predict_with(core_m, core_s, test, ALL_CANDIDATES)
        pred_xg = predict_with(xg_m, xg_s, test, xg_candidates)
        meta_X_test = np.column_stack([pred_core, pred_xg, np.abs(pred_core - pred_xg)])
        pred_stacked = meta_model.predict_proba(meta_X_test)[:, 1]

        print(f"=== Fold {fold}: train={len(train)} test={len(test)} "
              f"({test['date'].min().date()} to {test['date'].max().date()}) ===")
        print(f"  meta-model coefficients: core={meta_model.coef_[0][0]:+.3f}  "
              f"xG={meta_model.coef_[0][1]:+.3f}  disagreement={meta_model.coef_[0][2]:+.3f}")
        for name, pred in [("core", pred_core), ("xG", pred_xg), ("stacked", pred_stacked)]:
            pred_s = pd.Series(pred, index=test.index)
            auc = roc_auc_score(test["over_2_5"], pred_s)
            n_top = max(1, int(len(test) * TOP_PCT))
            top_idx = pred_s.sort_values(ascending=False).index[:n_top]
            top_results = test["over_2_5"].loc[top_idx].tolist()
            hit_rate = sum(top_results) / len(top_results)
            results[name].append(top_results)
            print(f"  {name:<8s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})")
        print()

    baseline = model_df["over_2_5"].mean()
    print("=" * 70)
    print("Combined across all folds:")
    for name in results:
        all_top = [x for top in results[name] for x in top]
        n = len(all_top)
        hits = sum(all_top)
        expected = n * baseline
        std = sqrt(n * baseline * (1 - baseline))
        z = (hits - expected) / std if std > 0 else 0.0
        p = 0.5 * (1 - erf(z / sqrt(2)))
        print(f"  {name:<8s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
