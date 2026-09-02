#!/usr/bin/env python3
"""Does xG's edge come back if it's expressed relative to each league's
own scoring environment, instead of as a raw absolute number? Prompted
directly by the user noticing the model pools all leagues into one set
of coefficients with only a single is_PL binary flag distinguishing
them -- La Liga runs meaningfully lower-scoring than the Premier League
(47.2% vs 53.8% over-2.5), so a raw xG value may not mean the same thing
in each league's context. rolling_validation_xg_v2.py already showed
xG's clear EPL/Championship-only edge (64.7% vs baseline, p=0.01)
disappearing once La Liga is pooled in (57.0% core vs 54.8% with xG,
neither significant) -- this tests whether that's a scale problem,
not a real loss of signal.

League-relative xG: for each match, subtract that league's own
expanding-window average team-xG-per-game (computed only from THAT
league's matches strictly before this one -- no lookahead, and
critically not blended across leagues) from the raw xG features. Note
xg_gap_last5 (|home - away|) is unchanged by this -- subtracting the
same baseline from both sides cancels out, so there's no separate
"relative gap" feature.

    A) core-only (no xG at all)
    B) absolute xG (today's live model)
    C) league-relative xG

Usage:
    python3 rolling_validation_xg_league_relative.py
"""

import warnings
from math import erf, sqrt

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from predict_upcoming import CORE_CANDIDATES, XG_CANDIDATES
from analyze_xg_features import XG_RAW_FEATURES, XG_DERIVED_FEATURES

warnings.filterwarnings("ignore")

N_FOLDS = 4
TOP_PCT = 0.05

XG_RELATIVE_RAW = [
    "home_xg_last5_relative", "away_xg_last5_relative",
    "home_xg_against_last5_relative", "away_xg_against_last5_relative",
]
XG_RELATIVE_DERIVED = ["combined_xg_last5_relative", "naive_expected_total_xg_last5_relative"]


def add_league_relative_xg(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("date").reset_index(drop=True)
    # Each match contributes two (competition, xg) observations -- the
    # home team's and away team's own recent xG -- stacked so the
    # per-league running average reflects "a team's typical recent xG
    # in this league" regardless of home/away split.
    stacked = pd.concat([
        df[["date", "competition", "home_xg_last5"]].rename(columns={"home_xg_last5": "xg"}),
        df[["date", "competition", "away_xg_last5"]].rename(columns={"away_xg_last5": "xg"}),
    ], ignore_index=True).dropna(subset=["xg"])

    # Collapse to one number per (competition, date) FIRST, so an
    # expanding mean computed over dates (not over individual matches)
    # can be shifted by one whole date -- otherwise two matches on the
    # same date could leak into each other's baseline depending on
    # arbitrary row order.
    daily = stacked.groupby(["competition", "date"], as_index=False)["xg"].mean()
    daily = daily.sort_values(["competition", "date"]).reset_index(drop=True)
    daily["league_baseline"] = daily.groupby("competition")["xg"].transform(lambda s: s.expanding().mean())
    daily["league_baseline"] = daily.groupby("competition")["league_baseline"].shift(1)
    daily = daily.dropna(subset=["league_baseline"])[["competition", "date", "league_baseline"]]

    overall_fallback = stacked["xg"].mean()  # only for the very first tracked date of a brand-new league
    df["_league_baseline"] = overall_fallback
    for comp, sub in df.groupby("competition"):
        comp_baselines = daily[daily["competition"] == comp][["date", "league_baseline"]]
        if comp_baselines.empty:
            continue
        merged = pd.merge_asof(
            sub[["date"]].reset_index().sort_values("date"),
            comp_baselines.sort_values("date"),
            on="date", direction="backward",
        ).set_index("index")
        df.loc[merged.index, "_league_baseline"] = merged["league_baseline"].fillna(overall_fallback)

    df["home_xg_last5_relative"] = df["home_xg_last5"] - df["_league_baseline"]
    df["away_xg_last5_relative"] = df["away_xg_last5"] - df["_league_baseline"]
    df["home_xg_against_last5_relative"] = df["home_xg_against_last5"] - df["_league_baseline"]
    df["away_xg_against_last5_relative"] = df["away_xg_against_last5"] - df["_league_baseline"]
    df["combined_xg_last5_relative"] = df["home_xg_last5_relative"] + df["away_xg_last5_relative"]
    df["naive_expected_total_xg_last5_relative"] = (
        df["home_xg_last5_relative"] + df["away_xg_against_last5_relative"]
        + df["away_xg_last5_relative"] + df["home_xg_against_last5_relative"]
    )
    return df.drop(columns=["_league_baseline"])


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
    df = add_league_relative_xg(df)

    RELATIVE_CANDIDATES = CORE_CANDIDATES + ["home_finishing_last5", "away_finishing_last5", "xg_gap_last5"] + XG_RELATIVE_RAW + XG_RELATIVE_DERIVED

    all_features = sorted(set(CORE_CANDIDATES + XG_CANDIDATES + RELATIVE_CANDIDATES))
    model_df = df[all_features + ["over_2_5", "date", "competition"]].dropna().reset_index(drop=True)
    print(f"Fixed complete-case dataset: {len(model_df)} matches, "
          f"{model_df['date'].min().date()} to {model_df['date'].max().date()}")
    print(f"  by competition: {model_df['competition'].value_counts().to_dict()}\n")

    fold_size = len(model_df) // N_FOLDS
    boundaries = [i * fold_size for i in range(N_FOLDS + 1)]
    boundaries[-1] = len(model_df)

    variants = {
        "core-only (no xG)": CORE_CANDIDATES,
        "absolute xG (live today)": XG_CANDIDATES,
        "league-relative xG": RELATIVE_CANDIDATES,
    }
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
            print(f"  {name:<26s} AUC={auc:.3f}  top-5% hit rate={hit_rate*100:.1f}% (n={n_top})  "
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
        print(f"  {name:<26s} {hits}/{n} = {hits/n*100:.1f}%  vs baseline {baseline*100:.1f}%  z={z:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
