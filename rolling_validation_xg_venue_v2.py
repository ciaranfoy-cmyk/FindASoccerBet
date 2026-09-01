#!/usr/bin/env python3
"""Cleaner re-test of the venue-split xG hypothesis. The first attempt
(rolling_validation_xg_venue.py) added venue-split features ALONGSIDE
the existing blended ones, letting them compete for the same job under
L1 -- which tends to arbitrarily keep one of two correlated features
and drop the other, potentially hiding a real effect (the same
collinearity issue that showed up comparing raw vs. Poisson-transformed
xG). This instead does a clean SWAP: blended-only vs. venue-only vs.
both, so venue-split features get a fair, isolated test rather than
competing with a near-duplicate for credit.

Usage:
    python3 rolling_validation_xg_venue_v2.py
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

VENUE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_venue_features.csv")
N_FOLDS = 4
TOP_PCT = 0.05

# Non-xG-specific candidates shared by every variant.
SHARED = ALL_CANDIDATES + ["home_finishing_last5", "away_finishing_last5"]

BLENDED_ONLY = SHARED + [
    "home_xg_last5", "away_xg_last5", "home_xg_against_last5", "away_xg_against_last5",
    "combined_xg_last5", "xg_gap_last5", "naive_expected_total_xg_last5",
]
VENUE_ONLY = SHARED + [
    "home_venue_xg_last5", "away_venue_xg_last5",
    "home_venue_xg_against_last5", "away_venue_xg_against_last5",
    "combined_venue_xg_last5", "venue_xg_gap_last5", "naive_expected_total_venue_xg_last5",
]
BOTH = list(dict.fromkeys(BLENDED_ONLY + VENUE_ONLY))


def load_data() -> pd.DataFrame:
    df = load_with_xg()
    venue_df = pd.read_csv(VENUE_PATH)[[
        "fixture_id", "home_venue_xg_last5", "away_venue_xg_last5",
        "home_venue_xg_against_last5", "away_venue_xg_against_last5",
        "home_xg_per_shot_last5", "away_xg_per_shot_last5",
    ]]
    df = df.merge(venue_df, on="fixture_id", how="left")
    df["combined_venue_xg_last5"] = df["home_venue_xg_last5"] + df["away_venue_xg_last5"]
    df["venue_xg_gap_last5"] = (df["home_venue_xg_last5"] - df["away_venue_xg_last5"]).abs()
    # Same construction as the original dominant feature, but from
    # venue-specific inputs instead of blended home+away ones.
    df["naive_expected_total_venue_xg_last5"] = (
        df["home_venue_xg_last5"] + df["away_venue_xg_against_last5"]
        + df["away_venue_xg_last5"] + df["home_venue_xg_against_last5"]
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

    print("Univariate check: which single feature correlates more strongly with total goals?")
    univariate_correlations(
        df,
        ["naive_expected_total_xg_last5", "naive_expected_total_venue_xg_last5"],
        "Blended vs. venue-specific 'expected total' feature",
    )

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"blended-only": BLENDED_ONLY, "venue-only": VENUE_ONLY, "both": BOTH}
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
