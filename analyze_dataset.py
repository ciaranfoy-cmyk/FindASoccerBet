#!/usr/bin/env python3
"""Statistical analysis of data/matches.csv (built by build_dataset.py).

Two parts:
1. Univariate correlations between every candidate feature and the
   outcome (total_goals, over_2_5), with a Bonferroni-corrected
   significance threshold since many features are tested at once.
2. A logistic regression for over_2_5, evaluated out-of-sample on a
   chronological train/test split (train on the earliest 80% of matches,
   test on the most recent 20% it never saw) — statistical significance
   in-sample doesn't mean a feature is actually useful for prediction;
   this checks that directly.

Usage:
    python3 analyze_dataset.py
"""

import os

import pandas as pd
import statsmodels.api as sm
from scipy import stats
from sklearn.metrics import accuracy_score, roc_auc_score

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches.csv")

CANDIDATE_FEATURES = [
    "home_games_played", "away_games_played",
    "home_competition_games", "away_competition_games",
    "home_gf_last5", "home_ga_last5", "away_gf_last5", "away_ga_last5",
    "home_gf_last10", "home_ga_last10", "away_gf_last10", "away_ga_last10",
    "home_gf_season", "home_ga_season", "away_gf_season", "away_ga_season",
    "home_rest_days", "away_rest_days",
    "h2h_games", "h2h_avg_goals",
    "combined_gf_last5", "combined_ga_last5",
    "naive_expected_total_last5", "min_competition_experience",
]


def load() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["is_PL"] = (df["competition"] == "PL").astype(int)
    df["combined_gf_last5"] = df["home_gf_last5"] + df["away_gf_last5"]
    df["combined_ga_last5"] = df["home_ga_last5"] + df["away_ga_last5"]
    df["naive_expected_total_last5"] = (
        df["home_gf_last5"] + df["away_ga_last5"] + df["away_gf_last5"] + df["home_ga_last5"]
    )
    df["min_competition_experience"] = df[["home_competition_games", "away_competition_games"]].min(axis=1)
    return df


def univariate_correlations(df: pd.DataFrame) -> None:
    alpha = 0.05
    bonferroni_alpha = alpha / len(CANDIDATE_FEATURES)
    print(f"n = {len(df)} matches")
    print(f"Testing {len(CANDIDATE_FEATURES)} candidate features — "
          f"Bonferroni-corrected significance threshold: p < {bonferroni_alpha:.5f}\n")

    print(f"{'Feature':<32}{'n':<8}{'corr w/ total_goals':<24}{'corr w/ over_2_5'}")
    rows = []
    for feat in CANDIDATE_FEATURES:
        sub = df[[feat, "total_goals", "over_2_5"]].dropna()
        if len(sub) < 30:
            continue
        r1, p1 = stats.pearsonr(sub[feat], sub["total_goals"])
        r2, p2 = stats.pointbiserialr(sub["over_2_5"], sub[feat])
        rows.append((feat, len(sub), r1, p1, r2, p2))

    rows.sort(key=lambda x: x[3])
    for feat, n, r1, p1, r2, p2 in rows:
        sig1 = "**" if p1 < bonferroni_alpha else ("*" if p1 < alpha else "  ")
        sig2 = "**" if p2 < bonferroni_alpha else ("*" if p2 < alpha else "  ")
        print(f"{feat:<32}{n:<8}{r1:+.3f} (p={p1:.4f}){sig1}      {r2:+.3f} (p={p2:.4f}){sig2}")
    print("\n(* = significant at p<0.05, ** = survives Bonferroni correction for 24 comparisons)")


def evaluate_model(df: pd.DataFrame, features: list[str], label: str) -> None:
    model_df = df[features + ["over_2_5", "date"]].dropna()
    split_idx = int(len(model_df) * 0.8)
    train, test = model_df.iloc[:split_idx], model_df.iloc[split_idx:]

    X_train = sm.add_constant(train[features])
    model = sm.Logit(train["over_2_5"], X_train).fit(disp=0)

    X_test = sm.add_constant(test[features], has_constant="add")
    pred = model.predict(X_test)
    auc = roc_auc_score(test["over_2_5"], pred)
    acc = accuracy_score(test["over_2_5"], (pred >= 0.5).astype(int))
    baseline_acc = max(test["over_2_5"].mean(), 1 - test["over_2_5"].mean())

    print(f"\n=== {label} ===")
    print(f"Train: {len(train)} matches ({train['date'].min().date()} to {train['date'].max().date()})")
    print(f"Test:  {len(test)} matches ({test['date'].min().date()} to {test['date'].max().date()})")
    print(model.summary())
    print(f"\nOut-of-sample: AUC={auc:.3f} (0.5=chance)  accuracy={acc*100:.1f}%  "
          f"vs. always-guess-majority baseline={baseline_acc*100:.1f}%")


def main() -> None:
    df = load()
    univariate_correlations(df)
    evaluate_model(df, ["naive_expected_total_last5", "min_competition_experience", "is_PL"], "Best 3-feature model")
    evaluate_model(df, ["is_PL"], "is_PL only")
    evaluate_model(
        df,
        [
            "home_gf_last5", "home_ga_last5", "away_gf_last5", "away_ga_last5",
            "home_gf_last10", "home_ga_last10", "away_gf_last10", "away_ga_last10",
            "home_competition_games", "away_competition_games",
            "home_rest_days", "away_rest_days", "is_PL",
        ],
        "Kitchen sink (all features)",
    )


if __name__ == "__main__":
    main()
