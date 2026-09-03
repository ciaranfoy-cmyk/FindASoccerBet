#!/usr/bin/env python3
"""Does venue-specific ACTUAL GOALS history help the model? Built after a
real, checked pattern: Man City's home games specifically have gone over
2.5 in 28/38 (74%) of their last two full PL seasons at the Etihad, well
above their any-venue rate over the same games (~55%). Every goals
feature that exists today (home_gf_last5, home_gf_season, etc.) blends a
team's home and away games together -- the model has a venue split for
SHOT volume (build_shots_venue_features.py, already validated) and for
goals-per-shot conversion, but nothing that directly says "how
high-scoring are this team's games specifically at this venue."
build_goals_venue_features.py fills that gap, keyed by (team, venue),
last 5 games at that specific venue, equal weight -- same windowing
style as the shots-venue precedent, kept deliberately unweighted so this
tests venue-specificity on its own, not tangled up with the separate
recency-weighting question already tested for xG.

    A) baseline  (today's live CORE_CANDIDATES, no venue-goals feature)
    B) venue-only (nothing to swap -- there's no existing equivalent)
    C) combined  (baseline + venue-goals features together)

Usage:
    python3 rolling_validation_goals_venue.py
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

VENUE_GOALS_PATH = "data/goals_venue_features.csv"
N_FOLDS = 4
TOP_PCT = 0.05

VENUE_GOALS_RAW = [
    "home_venue_gf_last5", "home_venue_ga_last5", "home_venue_total_last5", "home_venue_over_pct_last5",
    "away_venue_gf_last5", "away_venue_ga_last5", "away_venue_total_last5", "away_venue_over_pct_last5",
]


def add_venue_goals_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    df["combined_venue_total_last5"] = df["home_venue_total_last5"] + df["away_venue_total_last5"]
    df["venue_over_pct_combined"] = (df["home_venue_over_pct_last5"] + df["away_venue_over_pct_last5"]) / 2
    df["naive_expected_total_venue_last5"] = (
        df["home_venue_gf_last5"] + df["away_venue_ga_last5"]
        + df["away_venue_gf_last5"] + df["home_venue_ga_last5"]
    )
    return df


VENUE_GOALS_DERIVED = ["combined_venue_total_last5", "venue_over_pct_combined", "naive_expected_total_venue_last5"]
VENUE_GOALS_ALL = VENUE_GOALS_RAW + VENUE_GOALS_DERIVED


def load_data() -> pd.DataFrame:
    df = load_with_player_form_and_shots_venue()
    venue_df = pd.read_csv(VENUE_GOALS_PATH)[["fixture_id"] + VENUE_GOALS_RAW]
    df = df.merge(venue_df, on="fixture_id", how="left")
    return add_venue_goals_derived_features(df)


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
    combined_features = CORE_CANDIDATES + VENUE_GOALS_ALL
    all_features = sorted(set(combined_features))
    model_df = df[all_features + ["over_2_5", "date"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}\n")

    print("Univariate check: venue-specific goals features vs. over/under 2.5")
    univariate_correlations(df, VENUE_GOALS_ALL, "Venue-specific goals features")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {"baseline": baseline_features, "venue-only": VENUE_GOALS_ALL, "combined": combined_features}
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
        print(f"  {name:<12s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline_rate*100:.1f}%  "
              f"z={z:.2f} p={p:.4f}  Brier(top, pooled)={combined_brier:.4f}")


if __name__ == "__main__":
    main()
