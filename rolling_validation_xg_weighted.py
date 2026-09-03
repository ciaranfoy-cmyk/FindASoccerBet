#!/usr/bin/env python3
"""Does recency- and competition-weighted xG (build_xg_weighted_features.py)
beat the flat "last 5 games, any competition, equal weight" version
currently live? Built after investigating why real xG barely moved the
Man City vs Coventry prediction -- found the flat rolling window has
zero competition awareness (a promoted team's xG history is entirely
last season's Championship games, at full weight) and zero recency
weighting (a game from 11 months ago counts the same as last week).
Same clean-swap/combine discipline as every other feature test here.

    A) flat-only      (today's live XG_CANDIDATES xG features)
    B) weighted-only  (recency + competition discount)
    C) both

Usage:
    python3 rolling_validation_xg_weighted.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import univariate_correlations
from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from predict_upcoming import XG_CANDIDATES

warnings.filterwarnings("ignore")

WEIGHTED_PATH = "data/xg_weighted_features.csv"
N_FOLDS = 4
TOP_PCT = 0.05

FLAT_XG = ["home_xg_last5", "away_xg_last5", "home_xg_against_last5", "away_xg_against_last5"]
WEIGHTED_XG = [
    "home_xg_last5_weighted", "away_xg_last5_weighted",
    "home_xg_against_last5_weighted", "away_xg_against_last5_weighted",
]


def load_data() -> pd.DataFrame:
    from scipy.stats import poisson

    df = load_with_xg_player_form_and_shots_venue()
    weighted_df = pd.read_csv(WEIGHTED_PATH)[["fixture_id"] + WEIGHTED_XG]
    df = df.merge(weighted_df, on="fixture_id", how="left")
    # combined_xg_last5 / xg_gap_last5 / naive_expected_total_xg_last5 /
    # poisson_p_over_last5 are all derived FROM the flat xG features -- for
    # the weighted-only variant, recompute the same derivations from the
    # weighted raw features instead, so it's a fair like-for-like swap, not
    # flat-derived-features-vs-nothing.
    df["combined_xg_last5_weighted"] = df["home_xg_last5_weighted"] + df["away_xg_last5_weighted"]
    df["xg_gap_last5_weighted"] = (df["home_xg_last5_weighted"] - df["away_xg_last5_weighted"]).abs()
    df["naive_expected_total_xg_last5_weighted"] = (
        df["home_xg_last5_weighted"] + df["away_xg_against_last5_weighted"]
        + df["away_xg_last5_weighted"] + df["home_xg_against_last5_weighted"]
    )
    expected_total_weighted = df["naive_expected_total_xg_last5_weighted"] / 2
    df["poisson_p_over_last5_weighted"] = 1 - poisson.cdf(2, expected_total_weighted)
    return df


WEIGHTED_XG_ALL = WEIGHTED_XG + [
    "combined_xg_last5_weighted", "xg_gap_last5_weighted",
    "naive_expected_total_xg_last5_weighted", "poisson_p_over_last5_weighted",
]
FLAT_XG_DERIVED = ["combined_xg_last5", "xg_gap_last5", "naive_expected_total_xg_last5", "poisson_p_over_last5"]
FLAT_XG_ALL = FLAT_XG + FLAT_XG_DERIVED


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
    shared = [f for f in XG_CANDIDATES if f not in FLAT_XG_ALL]
    flat_only = shared + FLAT_XG_ALL
    weighted_only = shared + WEIGHTED_XG_ALL
    combined = list(dict.fromkeys(shared + FLAT_XG_ALL + WEIGHTED_XG_ALL))

    all_features = sorted(set(combined))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: flat vs. weighted xG features")
    univariate_correlations(df, FLAT_XG_ALL + WEIGHTED_XG_ALL, "Flat vs. weighted xG")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"flat-only": flat_only, "weighted-only": weighted_only, "combined": combined}
    results = {name: [] for name in variants}
    briers = {name: [] for name in variants}

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
            top_preds = pred_s.loc[top_idx].tolist()
            hit_rate = sum(top_results) / len(top_results)
            brier = brier_score_loss(top_results, top_preds)
            n_nonzero = sum(1 for c in model.coef_[0] if abs(c) > 1e-6)
            results[name].append(top_results)
            briers[name].append((top_results, top_preds))
            print(f"  {name:<14s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
                  f"Brier(top)={brier:.4f}  features retained={n_nonzero}/{len(features)}")
        print()

    baseline_rate = model_df["over_2_5"].mean()
    print("=" * 70)
    print("Combined across all folds:")
    for name in variants:
        all_top = [x for top in results[name] for x in top]
        n = len(all_top)
        hits = sum(all_top)
        expected = n * baseline_rate
        std = sqrt(n * baseline_rate * (1 - baseline_rate))
        z = (hits - expected) / std if std > 0 else 0.0
        p = 0.5 * (1 - erf(z / sqrt(2)))
        all_outcomes = [x for top, _ in briers[name] for x in top]
        all_preds = [x for _, preds in briers[name] for x in preds]
        combined_brier = brier_score_loss(all_outcomes, all_preds)
        print(f"  {name:<14s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline_rate*100:.1f}%  "
              f"z={z:.2f} p={p:.4f}  Brier(top, pooled)={combined_brier:.4f}")


if __name__ == "__main__":
    main()
