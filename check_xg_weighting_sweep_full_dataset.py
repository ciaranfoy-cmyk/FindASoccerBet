#!/usr/bin/env python3
"""HALF_LIFE_DAYS=60 and CROSS_COMPETITION_DISCOUNT=0.4
(build_xg_weighted_features.py) were never tuned -- the module's own
docstring says so ("starting points, not the result of a parameter
search"). Since combined_expected_geo (built directly from these
recency-weighted raw features) is now the single largest coefficient in
the live model, an untested decay rate underneath it is a real gap.
This sweeps both parameters and checks the same two things every other
feature in this project was gated on: full-dataset L1 coefficient
magnitude for combined_expected_geo, and out-of-sample walk-forward
Brier/AUC (build_stream, same CV the live confidence bar uses).

Coordinate search, not a full grid (compute budget): sweep
HALF_LIFE_DAYS first with CROSS_COMPETITION_DISCOUNT held at the
current default (0.4), take whichever half-life wins on Brier, then
sweep CROSS_COMPETITION_DISCOUNT with that half-life held fixed.

The expensive part -- re-parsing cached /fixtures/statistics for xG --
is done ONCE up front (xg_map), not once per parameter combination;
each candidate then only recomputes the recency-weighted average itself
(pure in-memory arithmetic), not the underlying data pull.

Usage:
    python3 check_xg_weighting_sweep_full_dataset.py
"""

import datetime
import warnings
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from scipy.stats import poisson
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_shots_venue import load_with_xg_player_form_and_shots_venue
from backtest_season_rolling_percentile import N_FOLDS_XG, build_stream
from build_dataset_apifootball import fetch_all_fixtures
from build_team_ratings_features import load_team_ratings
from build_xg_features import xg_stats_for
from build_xg_weighted_features import HISTORY_MAXLEN, MIN_GAMES_FOR_ROLLING
from calibration import apply_calibration, load_calibrators
from predict_upcoming import XG_CANDIDATES
from build_xg_weighted_features import GEO_FEATURES

warnings.filterwarnings("ignore")

DEFAULT_HALF_LIFE = 60
DEFAULT_DISCOUNT = 0.4
HALF_LIFE_CANDIDATES = [30, 45, 60, 90, 120, 180]
DISCOUNT_CANDIDATES = [0.1, 0.2, 0.4, 0.6, 1.0]

RAW_COLS = ["home_xg_last5_weighted", "away_xg_last5_weighted",
            "home_xg_against_last5_weighted", "away_xg_against_last5_weighted"]


def build_xg_map(matches: list[dict]) -> dict[int, dict[int, float]]:
    """fixture_id -> {team_id: xg}. Done once -- this is the only part
    that touches apifootball.get() (cached, no live API calls)."""
    xg_map = {}
    for i, m in enumerate(matches, start=1):
        try:
            xg = xg_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            xg = {}
        xg_map[m["fixture_id"]] = {tid: v["xg"] for tid, v in xg.items()}
        if i % 5000 == 0:
            print(f"  ...xg_map {i}/{len(matches)}")
    return xg_map


def recompute_raw_features(matches: list[dict], xg_map: dict, half_life: float, discount: float) -> pd.DataFrame:
    xg_for_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))
    xg_against_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))

    def weighted_avg(history: deque, match_date: datetime.datetime, competition: str):
        if len(history) < MIN_GAMES_FOR_ROLLING:
            return None
        total_weight = 0.0
        total_value = 0.0
        for entry in history:
            days_before = (match_date - entry["date"]).days
            recency_weight = 0.5 ** (days_before / half_life)
            comp_weight = 1.0 if entry["competition"] == competition else discount
            w = recency_weight * comp_weight
            total_weight += w
            total_value += w * entry["value"]
        return total_value / total_weight if total_weight > 0 else None

    rows = []
    for m in matches:
        xg = xg_map.get(m["fixture_id"], {})
        home_id, away_id = m["home_id"], m["away_id"]
        match_date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        competition = m["competition"]

        rows.append({
            "fixture_id": m["fixture_id"],
            "home_xg_last5_weighted": weighted_avg(xg_for_history[home_id], match_date, competition),
            "away_xg_last5_weighted": weighted_avg(xg_for_history[away_id], match_date, competition),
            "home_xg_against_last5_weighted": weighted_avg(xg_against_history[home_id], match_date, competition),
            "away_xg_against_last5_weighted": weighted_avg(xg_against_history[away_id], match_date, competition),
        })

        if home_id in xg and away_id in xg:
            home_xg, away_xg = xg[home_id], xg[away_id]
            xg_for_history[home_id].append({"date": match_date, "competition": competition, "value": home_xg})
            xg_against_history[home_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_for_history[away_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_against_history[away_id].append({"date": match_date, "competition": competition, "value": home_xg})

    return pd.DataFrame(rows)


def add_geo_features(df: pd.DataFrame) -> pd.DataFrame:
    # Coerce to float64 first -- an all-NaN raw column (e.g. a slice with
    # no xG-covered matches at all) comes back as object dtype, which
    # numpy's sqrt ufunc can't operate on directly.
    for c in RAW_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["home_expected_geo"] = np.sqrt(df["home_xg_last5_weighted"] * df["away_xg_against_last5_weighted"])
    df["away_expected_geo"] = np.sqrt(df["away_xg_last5_weighted"] * df["home_xg_against_last5_weighted"])
    df["combined_expected_geo"] = df["home_expected_geo"] + df["away_expected_geo"]
    df["poisson_p_over_geo"] = 1 - poisson.cdf(2, df["combined_expected_geo"] / 2)
    return df


def evaluate(base_df: pd.DataFrame, matches: list[dict], xg_map: dict, half_life: float, discount: float, label: str) -> dict:
    raw_df = recompute_raw_features(matches, xg_map, half_life, discount)
    df = base_df.drop(columns=[c for c in RAW_COLS + GEO_FEATURES if c in base_df.columns], errors="ignore")
    df = df.merge(raw_df, on="fixture_id", how="left")
    df = add_geo_features(df)

    # XG_CANDIDATES already includes both RAW_COLS (kept as normal
    # candidates, refreshed via the merge above) and GEO_FEATURES
    # (post-swap) -- strip GEO_FEATURES once and re-add it so it isn't
    # duplicated, the same fix needed in check_team_ratings_full_dataset.py
    # and check_xg_geo_mean_full_dataset.py after each one's own feature
    # went live.
    features = [f for f in XG_CANDIDATES if f not in GEO_FEATURES] + GEO_FEATURES
    model_df = df[features + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X = scaler.fit_transform(model_df[features])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X, model_df["over_2_5"])
    coefs = dict(zip(features, model.coef_[0]))
    geo_coef = coefs.get("combined_expected_geo", 0.0)

    calibrators = load_calibrators()
    stream = build_stream(df, features, N_FOLDS_XG, label)
    stream["pred_p_cal"] = apply_calibration(stream["pred_p"], pd.Series(["xG"] * len(stream)), calibrators)
    y = stream["over_2_5"].astype(int).values
    p = stream["pred_p_cal"].astype(float).values
    brier = brier_score_loss(y, p)
    auc = roc_auc_score(y, p)

    print(f"  [{label}] n_full={len(model_df)}  combined_expected_geo coef={geo_coef:+.4f}  "
          f"n_oos={len(y)}  Brier={brier:.4f}  AUC={auc:.3f}")
    return {"label": label, "half_life": half_life, "discount": discount,
            "geo_coef": geo_coef, "n_full": len(model_df), "n_oos": len(y), "brier": brier, "auc": auc}


def main() -> int:
    print("Loading base dataset (everything except the swept xG raw features)...")
    base_df = load_with_xg_player_form_and_shots_venue()
    base_df = load_team_ratings(base_df)  # ratings only, not weighted xG -- that's what we're sweeping

    print("Fetching full fixture history (cached)...")
    matches = fetch_all_fixtures(None)

    print("Building xg_map once (re-parses cached /fixtures/statistics, no live API calls)...")
    xg_map = build_xg_map(matches)

    results = []

    print(f"\n=== Sweep 1: HALF_LIFE_DAYS (CROSS_COMPETITION_DISCOUNT fixed at {DEFAULT_DISCOUNT}) ===")
    for hl in HALF_LIFE_CANDIDATES:
        marker = " <-- current default" if hl == DEFAULT_HALF_LIFE else ""
        r = evaluate(base_df, matches, xg_map, hl, DEFAULT_DISCOUNT, f"half_life={hl}{marker}")
        results.append(r)

    best_hl_result = min(results, key=lambda r: r["brier"])
    best_hl = best_hl_result["half_life"]
    print(f"\nBest half-life by OOS Brier: {best_hl} (Brier={best_hl_result['brier']:.4f})")

    print(f"\n=== Sweep 2: CROSS_COMPETITION_DISCOUNT (HALF_LIFE_DAYS fixed at best={best_hl}) ===")
    for disc in DISCOUNT_CANDIDATES:
        marker = " <-- current default" if disc == DEFAULT_DISCOUNT else ""
        r = evaluate(base_df, matches, xg_map, best_hl, disc, f"discount={disc}{marker}")
        results.append(r)

    print("\n=== Full summary, sorted by OOS Brier (lower is better) ===")
    for r in sorted(results, key=lambda r: r["brier"]):
        print(f"  {r['label']:35s} half_life={r['half_life']:<5} discount={r['discount']:<5} "
              f"geo_coef={r['geo_coef']:+.4f}  Brier={r['brier']:.4f}  AUC={r['auc']:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
