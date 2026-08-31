#!/usr/bin/env python3
"""Rolling-origin validation of a BLENDED core+xG model (average of both
models' predicted probabilities) against core-only and xG-only, on the
exact same folds/rows -- the real test of whether blending (found
promising on 4 specific games) actually improves results, or was just
a good story on a small anecdote.

Since xg_candidates is a superset of ALL_CANDIDATES, the xG-complete-case
row set already has complete core features too -- so all three variants
train/test on identical rows, making this a clean apples-to-apples
comparison.

Usage:
    python3 rolling_validation_xg_blend.py
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

N_FOLDS = 4
TOP_PCT = 0.05


def fit_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> tuple:
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X_train, train["over_2_5"])
    return model.predict_proba(X_test)[:, 1]


def top5_hit_rate(pred: pd.Series, over_2_5: pd.Series) -> tuple:
    n_top = max(1, int(len(pred) * TOP_PCT))
    order = pred.sort_values(ascending=False).index[:n_top]
    top_results = over_2_5.loc[order]
    return len(top_results), top_results.tolist()


def main() -> None:
    df = load_with_xg()
    xg_candidates = ALL_CANDIDATES + XG_RAW_FEATURES + XG_DERIVED_FEATURES

    model_df = df[xg_candidates + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    results = {"core": [], "xG": [], "blend": []}

    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]

        pred_core = fit_predict(train, test, ALL_CANDIDATES)
        pred_xg = fit_predict(train, test, xg_candidates)
        pred_blend = (pred_core + pred_xg) / 2

        print(f"=== Fold {fold}: train={len(train)} test={len(test)} "
              f"({test['date'].min().date()} to {test['date'].max().date()}) ===")
        for name, pred in [("core", pred_core), ("xG", pred_xg), ("blend", pred_blend)]:
            pred_s = pd.Series(pred, index=test.index)
            auc = roc_auc_score(test["over_2_5"], pred_s)
            n_top, top_results = top5_hit_rate(pred_s, test["over_2_5"])
            hit_rate = sum(top_results) / len(top_results)
            results[name].append((n_top, top_results))
            print(f"  {name:<6s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})")
        print()

    baseline = model_df["over_2_5"].mean()
    print("=" * 70)
    print("Combined across all folds:")
    for name in ["core", "xG", "blend"]:
        all_top = [x for _, top in results[name] for x in top]
        n = len(all_top)
        hits = sum(all_top)
        expected = n * baseline
        std = sqrt(n * baseline * (1 - baseline))
        z = (hits - expected) / std if std > 0 else 0.0
        p = 0.5 * (1 - erf(z / sqrt(2)))
        print(f"  {name:<6s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
