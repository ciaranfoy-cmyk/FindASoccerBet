#!/usr/bin/env python3
"""Does adding Elo-based team-strength features (elo_gap, elo_combined
-- see build_elo_features.py) help the model? Same clean-swap/combine
discipline as every other feature test here.

Elo is a genuinely different KIND of signal than anything already in
CORE_CANDIDATES -- a smooth, long-horizon team-strength estimate
updated after every result (with margin of victory), vs. the existing
features which are all short rolling windows (last 5/10 games) or
reset-every-season table stats. Not obviously redundant, so testing
combine as the primary hypothesis (consistent with how player-form and
venue-shots ended up -- new information gets combined, near-duplicate
information gets swapped), but reporting all three for completeness:

    A) baseline    (today's live CORE_CANDIDATES, no Elo)
    B) elo-only    (Elo replacing nothing -- there's nothing to swap)
    C) combined    (baseline + Elo together)

Usage:
    python3 rolling_validation_elo.py
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

ELO_PATH = "data/elo_features.csv"
N_FOLDS = 4
TOP_PCT = 0.05

ELO_FEATURES = ["elo_gap", "elo_combined"]


def load_data() -> pd.DataFrame:
    df = load_with_player_form_and_shots_venue()
    elo_df = pd.read_csv(ELO_PATH)[["fixture_id"] + ELO_FEATURES]
    return df.merge(elo_df, on="fixture_id", how="left")


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
    baseline_features = CORE_CANDIDATES
    combined_features = CORE_CANDIDATES + ELO_FEATURES
    all_features = sorted(set(combined_features))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: Elo gap and combined level vs. over/under 2.5")
    univariate_correlations(df, ELO_FEATURES, "Elo features")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"baseline": baseline_features, "elo-only": ELO_FEATURES, "combined": combined_features}
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
            print(f"  {name:<10s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
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
        print(f"  {name:<10s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline_rate*100:.1f}%  "
              f"z={z:.2f} p={p:.4f}  Brier(top, pooled)={combined_brier:.4f}")


if __name__ == "__main__":
    main()
