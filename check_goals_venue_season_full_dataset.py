#!/usr/bin/env python3
"""Full-dataset coefficient check for season-level venue goals/over-rate
features (build_goals_venue_season_features.py) -- the same verification
that overturned Elo and league-finish, confirmed the 5-game venue-goals
version added nothing, and confirmed weighted xG was real. This time
rolling_validation_goals_venue_season.py showed a much stronger,
consistent edge (combined beat baseline in all 3 folds on both hit rate
and Brier, not just a razor-thin pooled average) -- checking whether that
holds up on the complete dataset.

Usage:
    python3 check_goals_venue_season_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from predict_upcoming import CORE_CANDIDATES
from rolling_validation_goals_venue_season import VENUE_SEASON_ALL, load_data

warnings.filterwarnings("ignore")


def fit_full(df: pd.DataFrame, features: list[str]) -> LogisticRegressionCV:
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, model_df["over_2_5"])
    print(f"  Trained on {len(model_df)} complete-case rows")
    return model


def report(name: str, features: list[str], model: LogisticRegressionCV) -> None:
    print(f"\n{name} (C={model.C_[0]:.4f}):")
    for f, c in sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1])):
        flag = "" if abs(c) > 1e-6 else "  <-- ZEROED"
        print(f"  {f:<45s} {c:+.5f}{flag}")


def main() -> None:
    df = load_data()
    combined = list(dict.fromkeys(CORE_CANDIDATES + VENUE_SEASON_ALL))

    print("=== Full-dataset fit: combined (CORE_CANDIDATES + season-level venue-goals together) ===")
    model_combined = fit_full(df, combined)
    report("combined", combined, model_combined)

    coef_map = dict(zip(combined, model_combined.coef_[0]))
    venue_zeroed = [f for f in VENUE_SEASON_ALL if abs(coef_map[f]) <= 1e-6]
    print(f"\nSeason-level venue-goals features zeroed: {len(venue_zeroed)}/{len(VENUE_SEASON_ALL)} {venue_zeroed}")

    print("\n=== Full-dataset fit: season-only (no CORE_CANDIDATES) ===")
    model_season_only = fit_full(df, VENUE_SEASON_ALL)
    report("season-only", VENUE_SEASON_ALL, model_season_only)

    print("\n=== Full-dataset fit: baseline (CORE_CANDIDATES only, for reference) ===")
    model_baseline = fit_full(df, CORE_CANDIDATES)
    from sklearn.metrics import roc_auc_score
    base_df = df[CORE_CANDIDATES + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X_base = scaler.fit_transform(base_df[CORE_CANDIDATES])
    auc_base = roc_auc_score(base_df["over_2_5"], model_baseline.predict_proba(X_base)[:, 1])

    comb_df = df[combined + ["over_2_5"]].dropna()
    scaler2 = StandardScaler()
    X_comb = scaler2.fit_transform(comb_df[combined])
    auc_comb = roc_auc_score(comb_df["over_2_5"], model_combined.predict_proba(X_comb)[:, 1])
    print(f"\nIn-sample AUC: baseline={auc_base:.4f} (n={len(base_df)})  combined={auc_comb:.4f} (n={len(comb_df)})")


if __name__ == "__main__":
    main()
