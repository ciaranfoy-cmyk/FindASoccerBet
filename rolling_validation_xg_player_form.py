#!/usr/bin/env python3
"""Does player-form still add anything once real xG is already in the
model? The player-form clean-swap win (rolling_validation_player_form_v2.py)
was proven on the full 2015-2026 dataset with NO xG features present at
all. But the live model prefers the xG model whenever both teams have xG
coverage, and xG's own "naive_expected_total_xg_last5" already dominates
that model (~82% of its weight). attacking_form_total and that xG feature
could easily be measuring the same underlying thing -- so this does the
same clean-swap methodology, but restricted to the xG-covered subset and
with xG features present in every variant:

    A) xG + team-goals-form   (today's live xG model, in effect)
    B) xG + player-form
    C) xG + neither           (does recent-scoring-form matter at all
                                once real xG is already in the model?)

Usage:
    python3 rolling_validation_xg_player_form.py
"""

import os
import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import ALL_CANDIDATES, univariate_correlations
from analyze_lineup_features import LINEUP_DERIVED_FEATURES, LINEUP_RAW_FEATURES, load_with_lineups
from analyze_xg_features import XG_DERIVED_FEATURES, XG_PATH, XG_RAW_FEATURES, add_xg_derived_features

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
REST = (
    [f for f in ALL_CANDIDATES if f not in TEAM_GOALS_FORM]
    + LINEUP_RAW_FEATURES + LINEUP_DERIVED_FEATURES
    + XG_FEATURES
)

TEAM_ONLY = REST + TEAM_GOALS_FORM
PLAYER_ONLY = REST + PLAYER_FORM
NEITHER = REST


def load_data() -> pd.DataFrame:
    df = load_with_lineups()
    xg_df = pd.read_csv(XG_PATH)[["fixture_id"] + XG_RAW_FEATURES]
    df = df.merge(xg_df, on="fixture_id", how="left")
    df = add_xg_derived_features(df)
    form_df = pd.read_csv(PLAYER_FORM_PATH)[["fixture_id"] + ["home_attacking_form", "away_attacking_form"]]
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
    print(f"Fixed complete-case dataset (xG-covered subset): {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

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
            results[name].append(top_results)
            print(f"  {name:<16s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
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
        print(f"  {name:<16s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
