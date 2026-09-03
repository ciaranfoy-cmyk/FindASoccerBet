#!/usr/bin/env python3
"""Full-dataset coefficient check for venue-specific actual-goals
features (build_goals_venue_features.py) -- the same verification that
overturned Elo and league-average-finish, and that weighted xG survived:
a LogisticRegressionCV fit on the COMPLETE dataset, not walk-forward
folds. rolling_validation_goals_venue.py's combined-vs-baseline edge
was razor-thin (410/574 vs 409/574 hit rate, Brier 0.2062 vs 0.2068) --
exactly the size of edge that both Elo and league-finish showed in
rolling folds before getting zeroed out here, so this checks whether
that pattern repeats.

Usage:
    python3 check_goals_venue_full_dataset.py
"""

import warnings

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from predict_upcoming import CORE_CANDIDATES
from rolling_validation_goals_venue import VENUE_GOALS_ALL, load_data

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
    combined = list(dict.fromkeys(CORE_CANDIDATES + VENUE_GOALS_ALL))

    print("=== Full-dataset fit: combined (CORE_CANDIDATES + venue-goals together) ===")
    model_combined = fit_full(df, combined)
    report("combined", combined, model_combined)

    coef_map = dict(zip(combined, model_combined.coef_[0]))
    venue_zeroed = [f for f in VENUE_GOALS_ALL if abs(coef_map[f]) <= 1e-6]
    print(f"\nVenue-goals features zeroed: {len(venue_zeroed)}/{len(VENUE_GOALS_ALL)} {venue_zeroed}")


if __name__ == "__main__":
    main()
