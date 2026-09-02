#!/usr/bin/env python3
"""Re-test of the core xG question (does real xG help?) using the SAME
candidate lists the live model actually uses now -- CORE_CANDIDATES vs
XG_CANDIDATES from predict_upcoming.py. The original rolling_validation_xg.py
tested ALL_CANDIDATES vs ALL_CANDIDATES+xG-features, which ADDS xG
alongside team-goals-form/blended-shots rather than swapping -- exactly
the collinearity-masking flaw the venue-split and player-form re-tests
caught earlier. CORE_CANDIDATES no longer contains those collinear
features (player-form and venue-shots replaced them), so this is
already a fair, clean comparison without needing a separate swap.

Usage:
    python3 rolling_validation_xg_v2.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES

warnings.filterwarnings("ignore")

N_FOLDS = 4
TOP_PCT = 0.05


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
    df = load_with_xg_player_form_and_shots_venue()
    all_features = sorted(set(CORE_CANDIDATES + XG_CANDIDATES))
    model_df = df[all_features + ["over_2_5", "date", "competition"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}")
    print(f"  by competition: {model_df['competition'].value_counts().to_dict()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"core-only (no xG)": CORE_CANDIDATES, "with xG": XG_CANDIDATES}
    results = {name: [] for name in variants}

    for fold in range(1, N_FOLDS):
        train_end = boundaries[fold]
        test_start, test_end = boundaries[fold], boundaries[fold + 1]
        train, test = model_df.iloc[:train_end], model_df.iloc[test_start:test_end]

        print(f"=== Fold {fold}: train={len(train)} test={len(test)} "
              f"({test['date'].min().date()} to {test['date'].max().date()}) "
              f"test comp: {test['competition'].value_counts().to_dict()} ===")
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
            print(f"  {name:<20s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
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
        print(f"  {name:<20s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
