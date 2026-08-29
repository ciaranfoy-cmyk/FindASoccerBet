#!/usr/bin/env python3
"""Rolling-origin (walk-forward) validation of the L1 over-2.5 model.

The single 80/20 split in analyze_dataset_apifootball.py answers "did
this work on one test window" — this answers the more important
question: does the top-5%-confidence edge hold up consistently across
several independent time periods, or was the original result a
favorable draw on one particular window?

Splits the complete-case dataset into 5 roughly equal chronological
chunks. For each chunk 2-5, trains on everything before it (expanding
window, still fully chronological — no lookahead) and evaluates on that
chunk alone, reporting the same top-5%-by-confidence hit rate each time.

Usage:
    python3 rolling_validation.py
"""

import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, load

warnings.filterwarnings("ignore")

N_FOLDS = 5
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
        "train_n": len(train),
        "test_n": len(test),
        "test_start": test["date"].min().date(),
        "test_end": test["date"].max().date(),
        "auc": auc,
        "overall_over_rate": test["over_2_5"].mean(),
        "top_n": len(top),
        "top_hit_rate": top["over_2_5"].mean(),
        "top_pred_range": (top["pred_p"].min(), top["pred_p"].max()),
        "top_results": top["over_2_5"].tolist(),
    }


def main() -> None:
    df = load()
    model_df = df[ALL_CANDIDATES + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    results = []
    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train = model_df.iloc[:train_end]
        test = model_df.iloc[test_start:test_end]

        r = fit_and_evaluate(train, test, ALL_CANDIDATES)
        results.append(r)
        print(f"=== Fold {fold}: train on {r['train_n']} matches, test on {r['test_n']} "
              f"({r['test_start']} to {r['test_end']}) ===")
        print(f"  AUC: {r['auc']:.3f}   Base rate this window: {r['overall_over_rate']*100:.1f}%")
        print(f"  Top {TOP_PCT*100:.0f}% by confidence: n={r['top_n']}, "
              f"predicted range {r['top_pred_range'][0]*100:.1f}-{r['top_pred_range'][1]*100:.1f}%, "
              f"hit rate={r['top_hit_rate']*100:.1f}%\n")

    print("=" * 70)
    print("Summary across all folds:")
    for i, r in enumerate(results, start=1):
        print(f"  Fold {i}: AUC={r['auc']:.3f}  top-5% hit rate={r['top_hit_rate']*100:.1f}% (n={r['top_n']})")

    all_top_results = [x for r in results for x in r["top_results"]]
    combined_n = len(all_top_results)
    combined_hits = sum(all_top_results)
    print(f"\nCombined across all {N_FOLDS - 1} folds: {combined_hits}/{combined_n} = "
          f"{combined_hits/combined_n*100:.1f}% hit rate on top-5%-confidence picks")

    baseline = model_df["over_2_5"].mean()
    from math import sqrt, erf
    expected = combined_n * baseline
    std = sqrt(combined_n * baseline * (1 - baseline))
    z = (combined_hits - expected) / std
    p = 0.5 * (1 - erf(z / sqrt(2)))
    print(f"vs. overall dataset base rate {baseline*100:.1f}%: z={z:.2f}, p={p:.4f}")


if __name__ == "__main__":
    main()
