#!/usr/bin/env python3
"""Standalone Over 1.5 edge checker -- kept alongside the live Over 2.5
pipeline for periodic re-checks, not wired into explain_picks.py/
forward_test_log.py (no calibrators.pkl, no model_cache entry -- fully
self-contained, retrains everything fresh on each run).

Why this exists: validated the same day as the live 2.5 pipeline's own
methodology (walk-forward folds, hybrid core/xG model, Platt calibration,
rolling-percentile confidence bar, pool-hit-rate pricing -- not a
fixture's own stated number, see forward_test_log.compute_pool_hit_rate()'s
docstring for why). Over 3.5 was checked the same way and rejected outright
(Brier gets WORSE, not better, as the bar tightens -- no real tail).
Over 1.5 passed the same historical test Over 2.5 did (Brier IMPROVES
with a tighter bar, calibration gap stays positive and non-degenerate),
but a full live price check across all 6 validated confidence tiers
(85th-99th percentile) found EVERY fixture priced with negative edge --
the closest was -0.4pp (Philadelphia Union vs Orlando City, Polymarket,
2026-09-20 slate). Heavy-favorite markets (Over 1.5's tail lives at
82-90%+ implied probability) are exactly where Kalshi/Polymarket price
tightest, so the real, honestly-calibrated ~3pp edge this model has
mostly doesn't survive the ask + fee.

Verdict: not part of the live pick pipeline. The near-misses (within
half a point on a couple of fixtures) mean it's worth re-running
periodically rather than closing the book permanently -- market prices
on these lines shift week to week the same way 2.5's does.

Confirmed ticker/slug naming (live, not assumed):
  Kalshi: same series (KXLIGUE1TOTAL etc.), floor_strike=1.5,
    strike_type "greater", ticker suffix "-2" (vs "-3" for the 2.5 line).
  Polymarket intl (Gamma): same "-more-markets" sibling event, market
    groupItemTitle == "O/U 1.5" (vs "O/U 2.5").

Usage:
    APIFOOTBALL_KEY=xxxx python3 check_over15_edge.py
"""

import re
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from build_xg_weighted_features import (
    GEO_FEATURES, WEIGHTED_XG_RAW_FEATURES,
    add_geo_mean_features, add_weighted_xg_derived_features, load_weighted_xg,
)
from build_team_ratings_features import (
    RATINGS_DERIVED_FEATURES, RATINGS_RAW_FEATURES,
    add_ratings_derived_features, load_team_ratings,
)
from build_league_finish_features import add_league_finish_features, build_standings_cache
from predict_upcoming import (
    CORE_CANDIDATES, XG_CANDIDATES, XG_FINISHING_FEATURES,
    build_feature_row, fetch_upcoming_fixtures, replay_to_current_state,
)
from build_dataset_apifootball import fetch_all_fixtures
from forward_test_log import KALSHI_SERIES_BY_COMPETITION, kalshi_get, kalshi_fee
from polymarket_prices import POLYMARKET_TAG_BY_COMPETITION, _get, _is_base_fixture_event, _poly_names_match, polymarket_fee
from live_kalshi_edge_test import _normalize

warnings.filterwarnings("ignore")

LABEL = "over_1_5"
LINE = 1.5
# (percentile, historical pool hit rate) -- re-derived from a full
# out-of-fold sweep the day this was built; re-validate if this file
# hasn't been run in a long time and the underlying model/data has moved.
TIERS = [
    (85.0, 0.853), (90.0, 0.857), (92.5, 0.861),
    (95.0, 0.867), (97.5, 0.881), (99.0, 0.859),
]
N_FOLDS_CORE, N_FOLDS_XG = 5, 4
WINDOW = 500


class PlattCalibrator:
    """Same choice calibration.py made for the 2.5 label after testing
    isotonic vs Platt -- see that module's docstring."""

    def __init__(self):
        self.model = LogisticRegression()

    def fit(self, raw_p, y):
        self.model.fit(np.asarray(raw_p).reshape(-1, 1), np.asarray(y))
        return self

    def predict(self, raw_p):
        return self.model.predict_proba(np.asarray(raw_p).reshape(-1, 1))[:, 1]


def fit_and_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[features])
    X_test = scaler.transform(test[features])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X_train, train[LABEL])
    out = test[["fixture_id", "date", LABEL, "home_team", "away_team", "competition"]].copy()
    out["pred_p"] = model.predict_proba(X_test)[:, 1]
    return out


def build_stream(df: pd.DataFrame, features: list[str], n_folds: int, label: str) -> pd.DataFrame:
    cols = ["fixture_id", "date", "home_team", "away_team", "competition", LABEL] + features
    model_df = df[cols].dropna().reset_index(drop=True)
    fold_size = len(model_df) // n_folds
    boundaries = [i * fold_size for i in range(n_folds + 1)]
    boundaries[-1] = len(model_df)
    preds = []
    for fold in range(1, n_folds):
        train, test = model_df.iloc[:boundaries[fold]], model_df.iloc[boundaries[fold]:boundaries[fold + 1]]
        preds.append(fit_and_predict(train, test, features))
        print(f"  [{label}] fold {fold}: {len(train)} train, {len(test)} test", file=sys.stderr)
    return pd.concat(preds, ignore_index=True)


def fetch_kalshi_line_for_series(series_ticker: str, floor_strike: float) -> list[dict]:
    events = kalshi_get("/events", {"series_ticker": series_ticker, "status": "open", "limit": 100}).get("events", [])
    out = []
    for e in events:
        m = re.match(r"(.+?) vs (.+?): Total Goals", e.get("title", ""))
        if not m:
            continue
        home, away = m.group(1).strip(), m.group(2).strip()
        markets = kalshi_get("/markets", {"event_ticker": e["event_ticker"]}).get("markets", [])
        for mk in markets:
            if mk.get("floor_strike") != floor_strike or mk.get("strike_type") != "greater":
                continue
            yes_ask, yes_bid = mk.get("yes_ask_dollars"), mk.get("yes_bid_dollars")
            if yes_ask is None or yes_bid is None:
                continue
            out.append({"home": home, "away": away, "ticker": mk["ticker"], "yes_ask": float(yes_ask), "yes_bid": float(yes_bid)})
    return out


def fetch_polymarket_line_for_league(competition: str, group_item_title: str) -> list[dict]:
    tag_slug = POLYMARKET_TAG_BY_COMPETITION.get(competition)
    if tag_slug is None:
        return []
    base_events, offset = [], 0
    while True:
        page = _get("/events", {"tag_slug": tag_slug, "closed": "false", "limit": 50, "offset": offset, "order": "startDate", "ascending": "true"})
        if not page:
            break
        base_events.extend(e for e in page if _is_base_fixture_event(e))
        if len(page) < 50:
            break
        offset += 50
    out = []
    for e in base_events:
        sibling = _get("/events", {"slug": f"{e['slug']}-more-markets"})
        if not sibling:
            continue
        mkt = next((m for m in sibling[0].get("markets", []) if m.get("groupItemTitle") == group_item_title), None)
        if mkt is None or mkt.get("bestAsk") is None or mkt.get("bestBid") is None:
            continue
        home, _, away = e["title"].partition(" vs. ")
        out.append({"home": home.strip(), "away": away.strip(), "yes_ask": float(mkt["bestAsk"]), "yes_bid": float(mkt["bestBid"])})
    return out


def main() -> int:
    print("=== Building the out-of-fold stream to fit calibration + confirm the live bar ===")
    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)
    df = load_team_ratings(df)
    df[LABEL] = (df["total_goals"] > LINE).astype(int)

    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core").rename(columns={"pred_p": "pred_p_core"})
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_stream, on="fixture_id", how="left")
    merged["pred_p_raw"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    calibrators = {}
    stream["pred_p"] = np.nan
    for model_type in ["core", "xG"]:
        mask = stream["model_used"] == model_type
        sub = stream[mask]
        cal = PlattCalibrator().fit(sub["pred_p_raw"], sub[LABEL])
        calibrators[model_type] = cal
        stream.loc[mask, "pred_p"] = cal.predict(sub["pred_p_raw"])

    trailing = stream["pred_p"].tail(WINDOW)
    tier_bars = []  # (percentile, bar_value, pool_rate), loosest first
    for pct, pool_rate in TIERS:
        bar_val = float(trailing.quantile(pct / 100.0))
        tier_bars.append((pct, bar_val, pool_rate))
        print(f"  pct={pct:<6} live bar={bar_val*100:.2f}%  pool_rate={pool_rate*100:.1f}%")
    loosest_bar = tier_bars[0][1]
    print()

    print("=== Training full-data models for scoring live upcoming fixtures ===")
    core_full = load_with_player_form_and_shots_venue()
    core_full[LABEL] = (core_full["total_goals"] > LINE).astype(int)
    core_model_df = core_full[CORE_CANDIDATES + [LABEL]].dropna()
    scaler = StandardScaler()
    X_core = scaler.fit_transform(core_model_df[CORE_CANDIDATES])
    core_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    core_model.fit(X_core, core_model_df[LABEL])

    xg_model_df = df[XG_CANDIDATES + [LABEL]].dropna()
    xg_scaler = StandardScaler()
    X_xg = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    xg_model.fit(X_xg, xg_model_df[LABEL])

    print("Fetching upcoming fixtures and rebuilding current team state...")
    upcoming = fetch_upcoming_fixtures(14)
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)
    rows = [r for m in upcoming if (r := build_feature_row(m, state)) is not None]
    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_weighted_xg_derived_features(live_df)
    live_df = add_geo_mean_features(live_df)
    live_df = add_ratings_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    live_df = add_league_finish_features(live_df, build_standings_cache())
    live_df = live_df.dropna(subset=CORE_CANDIDATES)

    has_xg = live_df[XG_FINISHING_FEATURES + WEIGHTED_XG_RAW_FEATURES + GEO_FEATURES + RATINGS_RAW_FEATURES + RATINGS_DERIVED_FEATURES].notna().all(axis=1)
    live_df["raw_pred_p"], live_df["model_used"] = np.nan, ""
    if (~has_xg).any():
        live_df.loc[~has_xg, "raw_pred_p"] = core_model.predict_proba(scaler.transform(live_df.loc[~has_xg, CORE_CANDIDATES]))[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    if has_xg.any():
        live_df.loc[has_xg, "raw_pred_p"] = xg_model.predict_proba(xg_scaler.transform(live_df.loc[has_xg, XG_CANDIDATES]))[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"

    live_df["calibrated_p"] = np.nan
    for model_type in ["core", "xG"]:
        mask = live_df["model_used"] == model_type
        if mask.any():
            live_df.loc[mask, "calibrated_p"] = calibrators[model_type].predict(live_df.loc[mask, "raw_pred_p"])

    live_df["clears_bar"] = live_df["calibrated_p"] >= loosest_bar
    pool = live_df[live_df["clears_bar"]]
    print(f"\n{len(live_df)} upcoming fixtures scored, {len(pool)} clear the loosest (85th-pct) Over 1.5 bar.\n")
    if pool.empty:
        return 0

    # Assign each fixture the TIGHTEST tier it clears -- the best-validated
    # pool rate applicable to it, same logic as the live 2.5 pipeline's
    # early-season/standard bar assignment, generalized to 6 tiers.
    def best_tier(p):
        for pct, bar_val, pool_rate in reversed(tier_bars):  # tightest first
            if p >= bar_val:
                return pct, pool_rate
        return None, None
    pool = pool.copy()
    pool[["tier_pct", "tier_pool_rate"]] = pool["calibrated_p"].apply(lambda p: pd.Series(best_tier(p)))

    print("Fetching real Kalshi + Polymarket 1.5-line prices...")
    kalshi_by_comp, poly_by_comp = {}, {}
    for comp, series in KALSHI_SERIES_BY_COMPETITION.items():
        try:
            kalshi_by_comp[comp] = fetch_kalshi_line_for_series(series, LINE)
        except Exception as exc:
            print(f"  Kalshi {comp}: {exc}")
            kalshi_by_comp[comp] = []
    for comp in POLYMARKET_TAG_BY_COMPETITION:
        try:
            poly_by_comp[comp] = fetch_polymarket_line_for_league(comp, "O/U 1.5")
        except Exception as exc:
            print(f"  Poly {comp}: {exc}")
            poly_by_comp[comp] = []

    print(f"\n{'Fixture':<34}{'Model p':<10}{'Tier':<8}{'Pool':<7}{'Book':<8}{'Price':<8}{'Fee':<9}{'Edge = pool - fee - price'}")
    found_edge = False
    for _, r in pool.iterrows():
        matches = []
        for k in kalshi_by_comp.get(r["competition"], []):
            if _normalize(k["home"]) == _normalize(r["home_team"]) and _normalize(k["away"]) == _normalize(r["away_team"]):
                matches.append(("kalshi", k["yes_ask"]))
        for k in poly_by_comp.get(r["competition"], []):
            if _poly_names_match(r["home_team"], k["home"]) and _poly_names_match(r["away_team"], k["away"]):
                matches.append(("poly", k["yes_ask"]))
        pool_rate = r["tier_pool_rate"]
        for book, price in matches:
            fee = kalshi_fee(price) if book == "kalshi" else polymarket_fee(price)
            edge = pool_rate - fee - price
            flag = "  <-- POSITIVE EDGE" if edge > 0 else ""
            if edge > 0:
                found_edge = True
            print(f"{r['home_team']+' vs '+r['away_team']:<34}{r['calibrated_p']*100:<9.1f}%pct={r['tier_pct']:<6}{pool_rate*100:<6.1f}%{book:<8}${price:<7.2f}${fee:<8.4f}"
                  f"{pool_rate*100:.1f}% - {fee*100:.2f}% - {price*100:.0f}% = {edge*100:+.1f}pp{flag}")
    if not found_edge:
        print("\nNo genuinely positive-edge fixtures found on the Over 1.5 line at ANY validated tier (85th-99th pct).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
