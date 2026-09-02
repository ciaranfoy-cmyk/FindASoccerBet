#!/usr/bin/env python3
"""Comprehensive statistical analysis of data/matches_apifootball.csv.

Same discipline as analyze_dataset.py (Bonferroni-corrected significance,
chronological train/test split, out-of-sample AUC — statistical
significance in-sample doesn't mean a feature is useful for prediction)
but scaled to the full API-Football dataset: every plausible pre-match
feature, an L1-regularized (Lasso) logistic regression for principled
feature selection given how many correlated candidates there now are,
and a calibration check on the selected model.

Usage:
    python3 analyze_dataset_apifootball.py
"""

import os

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches_apifootball.csv")

# Pre-match-only candidates (no lookahead). Same-match shot stats
# (home_total_shots etc.) are deliberately excluded here — those are
# leakage, not predictors; see the separate sanity-check section below.
PREDICTIVE_FEATURES = [
    "home_games_played", "away_games_played",
    "home_competition_games", "away_competition_games",
    "home_gf_last5", "home_ga_last5", "away_gf_last5", "away_ga_last5",
    "home_gf_last10", "home_ga_last10", "away_gf_last10", "away_ga_last10",
    "home_gf_season", "home_ga_season", "away_gf_season", "away_ga_season",
    "home_clean_sheet_pct_last5", "away_clean_sheet_pct_last5",
    "home_clean_sheet_pct_last10", "away_clean_sheet_pct_last10",
    "home_league_position", "away_league_position",
    "home_points", "away_points", "home_goal_diff", "away_goal_diff",
    "home_rest_days", "away_rest_days",
    "h2h_games", "h2h_avg_goals",
    "home_shots_last5", "home_shots_on_goal_last5", "home_shots_inside_box_last5",
    "away_shots_last5", "away_shots_on_goal_last5", "away_shots_inside_box_last5",
    "home_conversion_rate_last5", "away_conversion_rate_last5",
    "home_missing_players", "away_missing_players",
]

LEAKAGE_SANITY_CHECK = [
    "home_total_shots", "home_shots_on_goal", "home_shots_inside_box", "home_shots_outside_box",
    "away_total_shots", "away_shots_on_goal", "away_shots_inside_box", "away_shots_outside_box",
]

# Dataset-wide mean total goals (data/matches_apifootball.csv, 23,777 matches) --
# the prior a head-to-head average gets shrunk toward when little h2h history exists.
GLOBAL_AVG_GOALS = 2.71

# Shrinkage strength in "games": a pairing with this many prior meetings gets its
# h2h average weighted equally with the prior; fewer meetings lean more on the
# prior, more meetings lean more on the pairing's own history. 47.7% of rows have
# fewer than 5 h2h games and 9.7% have zero -- a hard NaN-gate at that threshold
# would drop roughly half the dataset via CORE_CANDIDATES.dropna(), so this uses
# continuous shrinkage instead (never NaN, degrades gracefully to the prior).
H2H_SHRINKAGE_K = 5


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Shared by the historical loader and the live scorer, so a live
    prediction is computed with exactly the same derivation as training —
    no risk of the two drifting apart.
    """
    df["is_PL"] = (df["competition"] == "PL").astype(int)
    df["combined_gf_last5"] = df["home_gf_last5"] + df["away_gf_last5"]
    df["combined_ga_last5"] = df["home_ga_last5"] + df["away_ga_last5"]
    df["naive_expected_total_last5"] = (
        df["home_gf_last5"] + df["away_ga_last5"] + df["away_gf_last5"] + df["home_ga_last5"]
    )
    df["min_competition_experience"] = df[["home_competition_games", "away_competition_games"]].min(axis=1)
    df["position_gap"] = (df["home_league_position"] - df["away_league_position"]).abs()
    df["goal_diff_gap"] = (df["home_goal_diff"] - df["away_goal_diff"]).abs()
    df["combined_shots_last5"] = df["home_shots_last5"] + df["away_shots_last5"]
    df["combined_shots_inside_box_last5"] = df["home_shots_inside_box_last5"] + df["away_shots_inside_box_last5"]
    df["shots_gap_last5"] = (df["home_shots_last5"] - df["away_shots_last5"]).abs()
    df["missing_players_total"] = df["home_missing_players"] + df["away_missing_players"]
    df["missing_players_gap"] = (df["home_missing_players"] - df["away_missing_players"]).abs()
    df["clean_sheet_pct_combined_last5"] = (df["home_clean_sheet_pct_last5"] + df["away_clean_sheet_pct_last5"]) / 2
    df["season_year"] = df["season"]
    df["h2h_avg_goals_shrunk"] = (
        df["h2h_games"] * df["h2h_avg_goals"].fillna(0) + H2H_SHRINKAGE_K * GLOBAL_AVG_GOALS
    ) / (df["h2h_games"] + H2H_SHRINKAGE_K)
    return df


def load() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return add_derived_features(df)


DERIVED_FEATURES = [
    "is_PL", "combined_gf_last5", "combined_ga_last5", "naive_expected_total_last5",
    "min_competition_experience", "position_gap", "goal_diff_gap",
    "combined_shots_last5", "combined_shots_inside_box_last5", "shots_gap_last5",
    "missing_players_total", "missing_players_gap", "clean_sheet_pct_combined_last5",
    "season_year", "h2h_avg_goals_shrunk",
]

ALL_CANDIDATES = PREDICTIVE_FEATURES + DERIVED_FEATURES


def univariate_correlations(df: pd.DataFrame, features: list[str], label: str) -> list[tuple]:
    alpha = 0.05
    bonferroni_alpha = alpha / len(features)
    print(f"\n=== {label} ===")
    print(f"n = {len(df)} matches, testing {len(features)} features — Bonferroni threshold p < {bonferroni_alpha:.6f}\n")

    rows = []
    for feat in features:
        sub = df[[feat, "total_goals", "over_2_5"]].dropna()
        if len(sub) < 30:
            continue
        r1, p1 = stats.pearsonr(sub[feat], sub["total_goals"])
        r2, p2 = stats.pointbiserialr(sub["over_2_5"], sub[feat])
        rows.append((feat, len(sub), r1, p1, r2, p2))

    rows.sort(key=lambda x: x[3])
    print(f"{'Feature':<34}{'n':<8}{'r (total_goals)':<20}{'r (over_2.5)'}")
    for feat, n, r1, p1, r2, p2 in rows:
        sig1 = "**" if p1 < bonferroni_alpha else ("*" if p1 < alpha else "  ")
        sig2 = "**" if p2 < bonferroni_alpha else ("*" if p2 < alpha else "  ")
        print(f"{feat:<34}{n:<8}{r1:+.3f} (p={p1:.4f}){sig1}    {r2:+.3f} (p={p2:.4f}){sig2}")
    print("(* p<0.05, ** survives Bonferroni correction)")
    return rows


def lasso_feature_selection(df: pd.DataFrame, features: list[str]) -> None:
    model_df = df[features + ["over_2_5", "date"]].dropna()
    split_idx = int(len(model_df) * 0.8)
    train, test = model_df.iloc[:split_idx], model_df.iloc[split_idx:]

    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])

    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X_train, train["over_2_5"])

    coefs = sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1]))
    print(f"\n=== L1-regularized (Lasso) logistic regression ===")
    print(f"Train: {len(train)} matches ({train['date'].min().date()} to {train['date'].max().date()})")
    print(f"Test:  {len(test)} matches ({test['date'].min().date()} to {test['date'].max().date()})")
    print(f"Selected C: {model.C_[0]:.4f}\n")
    print("Non-zero coefficients (standardized), sorted by magnitude:")
    n_nonzero = 0
    for feat, coef in coefs:
        if abs(coef) > 1e-6:
            n_nonzero += 1
            print(f"  {feat:<34}{coef:+.4f}")
    print(f"\n{n_nonzero} of {len(features)} features retained (rest zeroed out by L1 penalty)")

    pred_prob = model.predict_proba(X_test)[:, 1]
    pred_class = model.predict(X_test)
    auc = roc_auc_score(test["over_2_5"], pred_prob)
    acc = accuracy_score(test["over_2_5"], pred_class)
    baseline = max(test["over_2_5"].mean(), 1 - test["over_2_5"].mean())
    print(f"\nOut-of-sample: AUC={auc:.3f} (0.5=chance)  accuracy={acc*100:.1f}%  "
          f"vs. always-guess-majority baseline={baseline*100:.1f}%")

    return test, pred_prob


def calibration_check(test: pd.DataFrame, pred_prob: np.ndarray) -> None:
    test = test.copy()
    test["pred_p"] = pred_prob
    test["decile"] = pd.qcut(test["pred_p"], 10, labels=False, duplicates="drop")
    print(f"\n=== Calibration (test set, n={len(test)}) ===")
    print(f"{'Predicted P range':<24}{'n':<8}{'Actual over-2.5 rate'}")
    for d in sorted(test["decile"].unique()):
        sub = test[test["decile"] == d]
        print(f"{sub['pred_p'].min()*100:5.1f}%-{sub['pred_p'].max()*100:5.1f}%       "
              f"{len(sub):<8}{sub['over_2_5'].mean()*100:.1f}%")


def main() -> None:
    df = load()
    print(f"Loaded {len(df)} matches, {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"Base rate over 2.5: {df['over_2_5'].mean()*100:.1f}%")

    univariate_correlations(df, ALL_CANDIDATES, "Pre-match predictive features")
    univariate_correlations(df, LEAKAGE_SANITY_CHECK, "SANITY CHECK ONLY — same-match shot stats (leakage, not usable for prediction)")

    test, pred_prob = lasso_feature_selection(df, ALL_CANDIDATES)
    calibration_check(test, pred_prob)


if __name__ == "__main__":
    main()
