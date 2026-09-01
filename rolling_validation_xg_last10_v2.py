#!/usr/bin/env python3
"""Clean re-test of the 10-game xG window vs. the 5-game one -- the
first attempt (rolling_validation_xg_last10.py) added last10 features
ALONGSIDE last5, letting L1 arbitrarily choose between two highly
correlated windows (same flaw the venue-split and player-form re-tests
caught). This does a clean swap: last5-only vs. last10-only vs. both.

Usage:
    python3 rolling_validation_xg_last10_v2.py
"""

import os
import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, univariate_correlations
from analyze_xg_features import load_with_xg

warnings.filterwarnings("ignore")

LAST10_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_features_last10.csv")
N_FOLDS = 4
TOP_PCT = 0.05

SHARED = ALL_CANDIDATES + ["home_finishing_last5", "away_finishing_last5"]

LAST5_ONLY = SHARED + [
    "home_xg_last5", "away_xg_last5", "home_xg_against_last5", "away_xg_against_last5",
    "combined_xg_last5", "xg_gap_last5", "naive_expected_total_xg_last5", "poisson_p_over_last5",
]
LAST10_ONLY = SHARED + [
    "home_xg_last10", "away_xg_last10", "home_xg_against_last10", "away_xg_against_last10",
    "combined_xg_last10", "xg_gap_last10", "naive_expected_total_xg_last10",
]
BOTH = list(dict.fromkeys(LAST5_ONLY + LAST10_ONLY))


def load_data() -> pd.DataFrame:
    df = load_with_xg()
    l10 = pd.read_csv(LAST10_PATH)[[
        "fixture_id", "home_xg_last10", "away_xg_last10",
        "home_xg_against_last10", "away_xg_against_last10",
    ]]
    df = df.merge(l10, on="fixture_id", how="left")
    df["combined_xg_last10"] = df["home_xg_last10"] + df["away_xg_last10"]
    df["xg_gap_last10"] = (df["home_xg_last10"] - df["away_xg_last10"]).abs()
    df["naive_expected_total_xg_last10"] = (
        df["home_xg_last10"] + df["away_xg_against_last10"]
        + df["away_xg_last10"] + df["home_xg_against_last10"]
    )
    return df


def fit_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str]):
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X_train, train["over_2_5"])
    return model.predict_proba(X_test)[:, 1], model


def main() -> None:
    df = load_data()
    all_features = sorted(set(BOTH))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: last5 vs. last10 'expected total' feature, head to head")
    univariate_correlations(
        df,
        ["naive_expected_total_xg_last5", "naive_expected_total_xg_last10"],
        "5-game vs. 10-game expected-total xG",
    )

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"last5-only": LAST5_ONLY, "last10-only": LAST10_ONLY, "both": BOTH}
    results = {name: [] for name in variants}

    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]

        print(f"=== Fold {fold}: train={len(train)} test={len(test)} "
              f"({test['date'].min().date()} to {test['date'].max().date()}) ===")
        for name, features in variants.items():
            pred, model = fit_predict(train, test, features)
            pred_s = pd.Series(pred, index=test.index)
            auc = roc_auc_score(test["over_2_5"], pred_s)
            n_top = max(1, int(len(test) * TOP_PCT))
            top_idx = pred_s.sort_values(ascending=False).index[:n_top]
            top_results = test["over_2_5"].loc[top_idx].tolist()
            hit_rate = sum(top_results) / len(top_results)
            n_nonzero = sum(1 for c in model.coef_[0] if abs(c) > 1e-6)
            results[name].append(top_results)
            print(f"  {name:<14s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
                  f"features retained={n_nonzero}/{len(features)}")
        print()

    baseline = model_df["over_2_5"].mean()
    print("=" * 70)
    print("Combined across all folds:")
    for name in variants:
        all_top = [x for top in results[name] for x in top]
        n = len(all_top)
        hits = sum(all_top)
        expected = n * baseline
        std = sqrt(n * baseline * (1 - baseline))
        z = (hits - expected) / std if std > 0 else 0.0
        p = 0.5 * (1 - erf(z / sqrt(2)))
        print(f"  {name:<14s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
