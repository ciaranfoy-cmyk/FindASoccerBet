#!/usr/bin/env python3
"""Does shrinking h2h_avg_goals toward the dataset-wide mean (weighted by how
many prior meetings it's based on) beat the raw h2h average? Same clean-swap
discipline as rolling_validation_shots_venue.py.

Motivation: h2h_avg_goals is treated identically whether it's an average of
2 prior meetings or 10 -- a 2-game average is mostly noise, but nothing in
the pipeline currently discounts it. h2h_avg_goals_shrunk (added in
analyze_dataset_apifootball.py) pulls low-sample-size pairings toward
GLOBAL_AVG_GOALS using a continuous shrinkage weight (H2H_SHRINKAGE_K=5
"games" of prior strength) instead of a hard NaN-gate, since 47.7% of rows
have fewer than 5 h2h games and gating would drop them via CORE_CANDIDATES's
dropna().

    A) raw-only     (today's live model, in effect)
    B) shrunk-only
    C) both

Usage:
    python3 rolling_validation_h2h_shrinkage.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import univariate_correlations
from analyze_shots_venue import load_with_player_form_and_shots_venue
from predict_upcoming import CORE_CANDIDATES

warnings.filterwarnings("ignore")

N_FOLDS = 4
TOP_PCT = 0.05

RAW_H2H = ["h2h_avg_goals"]
SHRUNK_H2H = ["h2h_avg_goals_shrunk"]

SHARED = [f for f in CORE_CANDIDATES if f not in RAW_H2H and f not in SHRUNK_H2H]
RAW_ONLY = SHARED + RAW_H2H
SHRUNK_ONLY = SHARED + SHRUNK_H2H
BOTH = list(dict.fromkeys(RAW_ONLY + SHRUNK_ONLY))


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
    df = load_with_player_form_and_shots_venue()
    all_features = sorted(set(BOTH))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: raw vs. shrunk h2h average")
    univariate_correlations(
        df, ["h2h_games", "h2h_avg_goals", "h2h_avg_goals_shrunk"], "Raw vs. shrunk h2h average",
    )

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"raw-only": RAW_ONLY, "shrunk-only": SHRUNK_ONLY, "both": BOTH}
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
            print(f"  {name:<12s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
                  f"Brier(top)={brier:.4f}  features retained={n_nonzero}/{len(features)}")
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
        all_outcomes = [x for top, _ in briers[name] for x in top]
        all_preds = [x for _, preds in briers[name] for x in preds]
        combined_brier = brier_score_loss(all_outcomes, all_preds)
        print(f"  {name:<12s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  "
              f"z={z:.2f} p={p:.4f}  Brier(top, pooled)={combined_brier:.4f}")


if __name__ == "__main__":
    main()
