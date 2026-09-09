#!/usr/bin/env python3
"""Does a geometric-mean combiner beat the live arithmetic-mean one for
turning two teams' recency/competition-weighted xG rates into an
expected-total-goals estimate? Prompted by a real miss: Derby (leaky
recent home defense, xG against 1.85/1.98/3.33) vs West Brom (modest
recent away attack, xG for 0.69/1.66/1.87/1.85/1.40) -- the live model
called 59.1% Over, Kalshi priced 47.5%, and the game finished 0-1.

The live feature (build_xg_weighted_features.py) estimates away West
Brom's expected goals as the ARITHMETIC mean of West Brom's own xG-for
and Derby's xG-against: (1.40 + 3.33) / 2 = 2.365 -- Derby's leaky
defense drags the estimate up even though West Brom's own attack was
never sharp enough to actually exploit it (final match stats: West
Brom 4 shots, 1 on target, all game). An arithmetic mean lets either
side dominate regardless of how much the OTHER side disagrees; a
GEOMETRIC mean is pulled down hard when one side is weak, which is
exactly the "opponent's own attack has to be sharp enough to punish a
leaky defense" story: sqrt(1.40 * 3.33) = 2.16 instead of 2.365 -- pulls
the same case in the right direction, and more so whenever the gap
between the two signals is larger.

Concretely, replaces the arithmetic-mean derived features with a
geometric-mean version of the same two raw weighted-xG inputs (no new
data collection -- purely a different combination of numbers already
in xg_weighted_features.csv):
    home_expected_geo = sqrt(home_xg_last5_weighted * away_xg_against_last5_weighted)
    away_expected_geo = sqrt(away_xg_last5_weighted * home_xg_against_last5_weighted)
    combined_expected_geo = home_expected_geo + away_expected_geo
    poisson_p_over_geo = 1 - poisson.cdf(2, combined_expected_geo / 2)

Same discipline as every other feature in this project: full-dataset
LogisticRegressionCV L1 coefficient survival is the decisive test, with
a paired bootstrap on the out-of-sample Brier/AUC delta as a second
opinion (not decisive on its own -- see check_team_ratings_significance.py
for why a plain point estimate isn't enough).

Usage:
    python3 check_xg_geo_mean_full_dataset.py
"""

import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_XG, build_stream
from build_team_ratings_features import load_team_ratings
from build_xg_weighted_features import GEO_FEATURES, WEIGHTED_XG_DERIVED_FEATURES, load_weighted_xg
from calibration import apply_calibration, load_calibrators
from predict_upcoming import XG_CANDIDATES

warnings.filterwarnings("ignore")

N_BOOTSTRAP = 3000
SEED = 0


def load_data() -> pd.DataFrame:
    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)  # already adds GEO_FEATURES -- see build_xg_weighted_features.py
    return load_team_ratings(df)


def fit_full(df: pd.DataFrame, features: list[str]) -> LogisticRegressionCV:
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(
        Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc",
        max_iter=2000, random_state=0,
    )
    model.fit(X, model_df["over_2_5"])
    print(f"  Trained on {len(model_df)} complete-case rows (C={model.C_[0]:.4f})")
    return model


def report(name: str, features: list[str], model: LogisticRegressionCV, watch: list[str]) -> None:
    print(f"\n{name}:")
    for f, c in sorted(zip(features, model.coef_[0]), key=lambda x: -abs(x[1])):
        flag = "  <-- NEW" if f in watch else ""
        zeroed = " (ZEROED by L1)" if c == 0 else ""
        print(f"    {f:32s} {c:+.5f}{zeroed}{flag}")


def survival(features: list[str], model: LogisticRegressionCV, watch: list[str]) -> None:
    coefs = dict(zip(features, model.coef_[0]))
    zeroed = [f for f in watch if abs(coefs.get(f, 0.0)) <= 1e-6]
    print(f"  Watched features zeroed: {len(zeroed)}/{len(watch)} {zeroed}")


def bootstrap_compare(df: pd.DataFrame, baseline_features: list[str], variant_features: list[str], label: str) -> None:
    calibrators = load_calibrators()

    def score(feats: list[str], sub_label: str) -> pd.DataFrame:
        stream = build_stream(df, feats, N_FOLDS_XG, sub_label)
        stream["pred_p_cal"] = apply_calibration(stream["pred_p"], pd.Series(["xG"] * len(stream)), calibrators)
        return stream

    base_stream = score(baseline_features, f"{label}-baseline")
    var_stream = score(variant_features, f"{label}-variant")

    merged = base_stream[["fixture_id", "over_2_5", "pred_p_cal"]].rename(columns={"pred_p_cal": "p_base"}).merge(
        var_stream[["fixture_id", "pred_p_cal"]].rename(columns={"pred_p_cal": "p_var"}),
        on="fixture_id", how="inner",
    )
    y = merged["over_2_5"].astype(int).values
    p_base = merged["p_base"].astype(float).values
    p_var = merged["p_var"].astype(float).values
    n = len(y)

    brier_base, brier_var = brier_score_loss(y, p_base), brier_score_loss(y, p_var)
    auc_base, auc_var = roc_auc_score(y, p_base), roc_auc_score(y, p_var)
    print(f"\n  [{label}] n={n}  Brier: base={brier_base:.4f} var={brier_var:.4f} (delta {brier_var-brier_base:+.4f})"
          f"  AUC: base={auc_base:.3f} var={auc_var:.3f} (delta {auc_var-auc_base:+.3f})")

    rng = np.random.default_rng(SEED)
    brier_deltas = np.empty(N_BOOTSTRAP)
    auc_deltas = np.empty(N_BOOTSTRAP)
    for i in range(N_BOOTSTRAP):
        idx = rng.integers(0, n, n)
        yb, pb, pv = y[idx], p_base[idx], p_var[idx]
        brier_deltas[i] = brier_score_loss(yb, pv) - brier_score_loss(yb, pb)
        auc_deltas[i] = roc_auc_score(yb, pv) - roc_auc_score(yb, pb) if len(set(yb)) > 1 else np.nan
    auc_deltas = auc_deltas[~np.isnan(auc_deltas)]

    brier_lo, brier_hi = np.percentile(brier_deltas, [2.5, 97.5])
    auc_lo, auc_hi = np.percentile(auc_deltas, [2.5, 97.5])
    print(f"  Brier delta 95% CI: [{brier_lo:+.4f}, {brier_hi:+.4f}]  (variant better in {(brier_deltas < 0).mean()*100:.1f}% of resamples)")
    print(f"  AUC   delta 95% CI: [{auc_lo:+.4f}, {auc_hi:+.4f}]  (variant better in {(auc_deltas > 0).mean()*100:.1f}% of resamples)")


def main() -> int:
    print("Loading full dataset with geo-mean xG features added...")
    df = load_data()

    # XG_CANDIDATES already carries GEO_FEATURES instead of
    # WEIGHTED_XG_DERIVED_FEATURES as of this script's run (wired in live
    # on the strength of the result this script originally produced) --
    # so "swap" is XG_CANDIDATES as-is, and "baseline"/"combine" need the
    # old arithmetic-mean features reconstructed, same fix as
    # check_team_ratings_full_dataset.py needed after its own feature
    # went live.
    pre_geo_features = [f for f in XG_CANDIDATES if f not in GEO_FEATURES]
    baseline_features = pre_geo_features + WEIGHTED_XG_DERIVED_FEATURES
    combine_features = pre_geo_features + WEIGHTED_XG_DERIVED_FEATURES + GEO_FEATURES
    swap_features = pre_geo_features + GEO_FEATURES

    print("\n=== Full-dataset L1 coefficient survival ===")
    print("Baseline (live XG_CANDIDATES):")
    base_model = fit_full(df, baseline_features)
    report("Baseline", baseline_features, base_model, [])

    print("\nVariant A: COMBINE (geo-mean features added alongside everything live keeps):")
    a_model = fit_full(df, combine_features)
    report("Variant A (combine)", combine_features, a_model, GEO_FEATURES)
    survival(combine_features, a_model, GEO_FEATURES)

    print("\nVariant B: SWAP (geo-mean replaces the arithmetic-mean derived features):")
    b_model = fit_full(df, swap_features)
    report("Variant B (swap)", swap_features, b_model, GEO_FEATURES)
    survival(swap_features, b_model, GEO_FEATURES)
    print("  Arithmetic-mean derived features that would be given up:")
    survival(baseline_features, base_model, WEIGHTED_XG_DERIVED_FEATURES)

    print("\n=== Out-of-sample walk-forward bootstrap (XG-model CV, same as the live confidence bar) ===")
    bootstrap_compare(df, baseline_features, combine_features, "combine")
    bootstrap_compare(df, baseline_features, swap_features, "swap")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
