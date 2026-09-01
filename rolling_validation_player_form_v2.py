#!/usr/bin/env python3
"""Clean re-test of player-form (recency-weighted individual attacking
scoring) vs. team-level recent-goals features -- the first test
(rolling_validation_player_form.py) added player-form ALONGSIDE the
existing team-goals features, letting L1 arbitrarily choose between
correlated signals (same flaw the venue-split re-test caught). This
does a clean swap: team-goals-only vs. player-form-only vs. both, on
the exact same rows.

Usage:
    python3 rolling_validation_player_form_v2.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, univariate_correlations
from analyze_player_form import load_with_player_form
from analyze_lineup_features import LINEUP_DERIVED_FEATURES, LINEUP_RAW_FEATURES

warnings.filterwarnings("ignore")

N_FOLDS = 4
TOP_PCT = 0.05

# The direct competitors: pure attacking-goals-form features that cover
# the same "how has this team been scoring lately" ground as player form.
TEAM_GOALS_FORM = [
    "home_gf_last5", "away_gf_last5", "home_gf_last10", "away_gf_last10",
    "home_gf_season", "away_gf_season", "combined_gf_last5", "naive_expected_total_last5",
]
PLAYER_FORM = ["home_attacking_form", "away_attacking_form", "attacking_form_total", "attacking_form_gap"]

REST = [f for f in ALL_CANDIDATES if f not in TEAM_GOALS_FORM] + LINEUP_RAW_FEATURES + LINEUP_DERIVED_FEATURES

TEAM_ONLY = REST + TEAM_GOALS_FORM
PLAYER_ONLY = REST + PLAYER_FORM
BOTH = REST + TEAM_GOALS_FORM + PLAYER_FORM


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
    df = load_with_player_form()
    all_features = sorted(set(BOTH))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: team goals-form vs. player-form, same job, head to head")
    univariate_correlations(
        df,
        ["combined_gf_last5", "attacking_form_total", "home_gf_last5", "home_attacking_form"],
        "Team-level vs. player-level attacking form",
    )

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"team-goals-only": TEAM_ONLY, "player-form-only": PLAYER_ONLY, "both": BOTH}
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
            print(f"  {name:<18s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
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
        print(f"  {name:<18s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
