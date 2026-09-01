#!/usr/bin/env python3
"""v2: does player-form add anything on top of real xG -- re-tested
without unnecessarily bundling in the lineup-feature completeness
requirement. v1 (rolling_validation_xg_player_form.py) required lineup
features to be complete too (inherited from how the player-form-only
test was built), which cut the xG-covered subset from ~2,783 rows down
to 1,720 -- and with training folds as small as 430-860 rows against
~70 candidates, L1 collapsed to just 1 retained feature in 2 of 3 folds.
That's consistent with too little data to support more coefficients,
not proof the other signals are zero. This drops the lineup requirement
so the sample stays close to the full xG-covered size and the model has
room to actually keep more than one feature.

    A) xG + team-goals-form
    B) xG + player-form
    C) xG + neither

Usage:
    python3 rolling_validation_xg_player_form_v2.py
"""

import os
import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, univariate_correlations
from analyze_xg_features import XG_DERIVED_FEATURES, XG_RAW_FEATURES, load_with_xg

warnings.filterwarnings("ignore")

PLAYER_FORM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "player_form_features.csv")

N_FOLDS = 4
TOP_PCT = 0.05

TEAM_GOALS_FORM = [
    "home_gf_last5", "away_gf_last5", "home_gf_last10", "away_gf_last10",
    "home_gf_season", "away_gf_season", "combined_gf_last5", "naive_expected_total_last5",
]
PLAYER_FORM = ["home_attacking_form", "away_attacking_form", "attacking_form_total", "attacking_form_gap"]

XG_FEATURES = XG_RAW_FEATURES + XG_DERIVED_FEATURES
REST = [f for f in ALL_CANDIDATES if f not in TEAM_GOALS_FORM] + XG_FEATURES

TEAM_ONLY = REST + TEAM_GOALS_FORM
PLAYER_ONLY = REST + PLAYER_FORM
NEITHER = REST


def load_data() -> pd.DataFrame:
    df = load_with_xg()
    form_df = pd.read_csv(PLAYER_FORM_PATH)[["fixture_id", "home_attacking_form", "away_attacking_form"]]
    df = df.merge(form_df, on="fixture_id", how="left")
    df["attacking_form_total"] = df["home_attacking_form"] + df["away_attacking_form"]
    df["attacking_form_gap"] = (df["home_attacking_form"] - df["away_attacking_form"]).abs()
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
    all_features = sorted(set(TEAM_ONLY + PLAYER_ONLY))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset (xG-covered subset, no lineup requirement): "
          f"{len(model_df)} matches, {model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: xG expected-total vs. player-form vs. team-goals-form, head to head")
    univariate_correlations(
        df,
        ["naive_expected_total_xg_last5", "attacking_form_total", "combined_gf_last5"],
        "xG vs. player-form vs. team-goals-form (xG-covered rows only)",
    )

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"xg+team-goals": TEAM_ONLY, "xg+player-form": PLAYER_ONLY, "xg+neither": NEITHER}
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
            nonzero_names = [f for f, c in zip(features, model.coef_[0]) if abs(c) > 1e-6]
            results[name].append(top_results)
            print(f"  {name:<16s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
                  f"features retained={n_nonzero}/{len(features)}  -> {nonzero_names}")
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
        print(f"  {name:<16s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
