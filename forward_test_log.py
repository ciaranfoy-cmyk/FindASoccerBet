#!/usr/bin/env python3
"""The only real way to know if this model is profitable: track real
predictions against real Kalshi prices, going forward, and see what
actually happens. There is no historical Kalshi price archive to
backtest against (confirmed this session -- real prices only exist for
a rolling window of upcoming/recent fixtures), so profitability can
only be established prospectively, one real gameweek at a time. This
script is that tracker.

Two modes:

  snapshot -- score upcoming fixtures across all 6 leagues, pull REAL
    live Kalshi "Over 2.5" prices, compute the calibrated model
    probability and the resulting edge, and APPEND new rows to
    data/forward_test_log.csv (one row per fixture, never edited
    retroactively -- already-logged fixture_ids are skipped so the
    same pick can't be logged twice with a different price later).

  settle -- for previously logged rows whose kickoff has passed, check
    the real result via API-Football and fill in the actual outcome
    and profit/loss AT THE PRICE THAT WAS ACTUALLY LOGGED (not a
    re-fetch -- that would defeat the point).

Selection rule (fixed BEFORE any outcome is known, not chosen after
the fact): a fixture is "selected" if its calibrated probability
clears the rolling p95 bar computed from the model's own trailing
historical out-of-fold predictions -- same methodology validated in
backtest_season_rolling_percentile.py / calibration_full_history.py,
just applied to live fixtures instead of stopping at historical data.

Usage:
    APIFOOTBALL_KEY=xxxx python3 forward_test_log.py snapshot
    APIFOOTBALL_KEY=xxxx python3 forward_test_log.py settle
"""

import argparse
import csv
import os
import re
import sys
import warnings
from collections import deque

import pandas as pd
from sklearn.linear_model import LogisticRegressionCV
from sklearn.preprocessing import StandardScaler

import apifootball
from analyze_dataset_apifootball import add_derived_features
from analyze_player_form import add_player_form_derived_features
from analyze_shots_venue import (
    add_shots_venue_derived_features,
    load_with_player_form_and_shots_venue,
    load_with_xg_player_form_and_shots_venue,
)
from build_xg_weighted_features import (
    GEO_FEATURES,
    WEIGHTED_XG_RAW_FEATURES,
    add_geo_mean_features,
    add_weighted_xg_derived_features,
    load_weighted_xg,
)
from build_team_ratings_features import (
    RATINGS_DERIVED_FEATURES,
    RATINGS_RAW_FEATURES,
    add_ratings_derived_features,
    load_team_ratings,
)
from backtest_season_rolling_percentile import N_FOLDS_CORE, N_FOLDS_XG, build_stream
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from build_league_finish_features import add_league_finish_features, build_standings_cache
from calibration import apply_calibration, load_calibrators
from live_kalshi_edge_test import kalshi_get, _normalize
from predict_upcoming import (
    CORE_CANDIDATES,
    XG_CANDIDATES,
    XG_FINISHING_FEATURES,
    build_feature_row,
    fetch_upcoming_fixtures,
    replay_to_current_state,
)

warnings.filterwarnings("ignore")

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "forward_test_log.csv")
FIELDS = [
    "logged_at", "fixture_id", "competition", "kickoff_date", "home_team", "away_team",
    "model_used", "raw_p", "calibrated_p", "is_early_season", "effective_bar", "selected",
    "kalshi_ticker", "kalshi_yes_ask", "kalshi_yes_bid", "kalshi_no_ask", "kalshi_no_bid",
    "kalshi_implied_p", "kalshi_fair_p", "pool_hit_rate", "fee_per_contract",
    "edge_vs_ask", "edge_vs_fair",
    "settled", "actual_home_goals", "actual_away_goals", "actual_over_2_5",
    "pnl_per_dollar_if_selected",
]


def kalshi_fee(price: float) -> float:
    """Kalshi's published standard trading fee (fee_type="quadratic",
    fee_multiplier=1 -- confirmed for all 9 tracked series via /series):
    fee = ceil_to_cent(0.07 * price * (1 - price)), charged per contract
    on entry regardless of outcome. Not verified against a live
    authenticated order preview (this session has no trading auth) --
    this is Kalshi's stated schedule, applied here as a modeling
    assumption. At the ~$0.60-0.75 price range these picks trade in,
    this is consistently about $0.02/contract -- roughly 2 percentage
    points of edge, enough on its own to flip a thin pick negative.
    """
    import math
    return math.ceil(0.07 * price * (1 - price) * 100) / 100

KALSHI_SERIES_BY_COMPETITION = {
    "PL": "KXEPLTOTAL",
    "ELC": "KXEFLCHAMPIONSHIPTOTAL",
    "LALIGA": "KXLALIGATOTAL",
    "BUNDESLIGA": "KXBUNDESLIGATOTAL",
    "SERIEA": "KXSERIEATOTAL",
    "LIGUE1": "KXLIGUE1TOTAL",
    "MLS": "KXMLSTOTAL",
    "EREDIVISIE": "KXEREDIVISIETOTAL",
    "SUPERLIG": "KXSUPERLIGTOTAL",
    "BRASILEIRAO": "KXBRASILEIROTOTAL",
    "LIGAPORTUGAL": "KXLIGAPORTUGALTOTAL",
    "JLEAGUE": "KXJLEAGUETOTAL",
}


# Above this width, the yes bid-ask spread is illiquid enough that its
# midpoint isn't a meaningful fair-value estimate -- found empirically:
# a market 6 days from kickoff had yes_bid=$0.02/yes_ask=$0.96 (a 94-cent
# spread, essentially an untraded placeholder quote), whose midpoint
# claimed "49% fair value" against a 96-cent ask -- nonsense. Every
# genuinely liquid market checked this session had a 1-3 cent spread.
MAX_LIQUID_SPREAD = 0.10


def fetch_kalshi_over25_for_series(series_ticker: str) -> list[dict]:
    events = kalshi_get("/events", {"series_ticker": series_ticker, "status": "open", "limit": 100}).get("events", [])
    out = []
    for e in events:
        title = e.get("title", "")
        m = re.match(r"(.+?) vs (.+?): Total Goals", title)
        if not m:
            continue
        home, away = m.group(1).strip(), m.group(2).strip()
        markets = kalshi_get("/markets", {"event_ticker": e["event_ticker"]}).get("markets", [])
        for mk in markets:
            if mk.get("floor_strike") != 2.5 or mk.get("strike_type") != "greater":
                continue
            yes_ask, yes_bid = mk.get("yes_ask_dollars"), mk.get("yes_bid_dollars")
            no_ask, no_bid = mk.get("no_ask_dollars"), mk.get("no_bid_dollars")
            if yes_ask is None or yes_bid is None:
                continue
            yes_ask, yes_bid = float(yes_ask), float(yes_bid)
            entry = {
                "home": home, "away": away, "ticker": mk["ticker"],
                "yes_ask": yes_ask, "yes_bid": yes_bid,
                "no_ask": None, "no_bid": None, "fair_p": None,
            }
            # De-vig: yes_ask + no_ask normally exceeds $1.00 by a small
            # amount (the market maker's real margin, confirmed empirically
            # at ~1-2pp on these contracts, far below a bookmaker's typical
            # 5-8% overround) -- but the yes/no MIDPOINTS sum to almost
            # exactly $1.00, meaning the vig lives in the bid-ask spread
            # itself rather than extra hidden shading. So the midpoint of
            # yes bid/ask is a good de-vigged fair-value estimate, used as
            # the "does the model genuinely disagree with the market"
            # check -- separate from edge_vs_ask, which is the "would
            # actually buying this be profitable" check (uses the real
            # price you'd pay, already conservative on its own).
            if no_ask is not None and no_bid is not None:
                no_ask, no_bid = float(no_ask), float(no_bid)
                entry["no_ask"], entry["no_bid"] = no_ask, no_bid
                if (yes_ask - yes_bid) <= MAX_LIQUID_SPREAD and (no_ask - no_bid) <= MAX_LIQUID_SPREAD:
                    yes_mid, no_mid = (yes_ask + yes_bid) / 2, (no_ask + no_bid) / 2
                    if yes_mid + no_mid > 0:
                        entry["fair_p"] = yes_mid / (yes_mid + no_mid)
            out.append(entry)
    return out


# Both a fixture's own games-into-season AND its opponent's matter --
# whichever team has played fewer games this competition+season is the
# binding constraint on how much current-season signal is really
# available, so EARLY_SEASON_CUTOFF is applied to min(home, away).
# Validated in check_early_season_reliability.py: fixtures at or below
# this cutoff are overconfident by +6.1pp in the model-confident (>=60%)
# regime (n=209) versus +1.1pp the rest of the season (n=2533) -- using
# the SAME out-of-fold stream and calibrators this bar is built from.
#
# FIX: compute_rolling_p95_bar(early_season_only=True) below -- a
# separate rolling threshold computed only from early-season out-of-fold
# predictions, applied to early-season live fixtures instead of the
# regular bar.
#
# The percentile used for the early-season Over bar is
# EARLY_SEASON_OVER_PERCENTILE=92.5, NOT 95 -- re-derived in
# check_early_season_bar_sweep.py after the original p95-for-both
# validation above went stale (it predates the geo-mean/team-ratings
# features, and check_early_season_reliability.py had a real bug --
# missing the ratings merge -- that made it silently unrunnable until
# fixed). The re-swept result inverts the intuition from the regular
# bar: going STRICTER (97.5, 99) makes the early-season Over bar worse,
# not better (calibration gap goes from -3.7pp at 95 to -21.0pp at 99),
# because the early-season population is small enough that a higher
# threshold just selects noisy outliers. 92.5 was the best of the swept
# candidates: n=52, +1.3pp calibration gap, Brier=0.2136 -- versus 95's
# n=34, -3.7pp, Brier=0.2290. Sample sizes here (11-95 per candidate)
# are small; treat this as the best available evidence, not a settled
# number. The Under-side early-season bar was swept too and found
# unreliable at every percentile tested (large negative calibration gaps
# throughout) -- left at 95 rather than "fixed" to something unvalidated,
# and the early-season Under bar generally should not be trusted.
#
# check_early_season_reliability.py's single-game breakdown (once fixed)
# also shows EARLY_SEASON_CUTOFF=4 draws the boundary in the wrong place:
# the real overconfidence is concentrated almost entirely at exactly 1
# game played (n=44, model said 65% confident, actual hit rate 34% --
# a 31pp gap), while games 2-4 are mostly fine or even underconfident
# (game 4 specifically: 0.0pp gap, better-calibrated than many
# "rest of season" fixtures). The cutoff itself has NOT been narrowed to
# reflect this yet -- kept at 4 pending a decision on whether to test 1
# or 2 instead.
EARLY_SEASON_CUTOFF = 4
EARLY_SEASON_OVER_PERCENTILE = 92.5


def _games_into_season_lookup() -> pd.DataFrame:
    """fixture_id -> min(home team's, away team's) games already played
    in that competition+season BEFORE this fixture, computed from date
    order -- NOT the home/away_competition_games column in
    matches_apifootball.csv, which is cumulative across ALL seasons and
    never resets. Same construction as
    check_early_season_reliability.py's games_into_season().
    """
    raw = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matches_apifootball.csv"))
    raw = raw.sort_values("date").reset_index(drop=True)
    long = pd.concat([
        raw[["fixture_id", "date", "competition", "season", "home_team"]].rename(columns={"home_team": "team"}),
        raw[["fixture_id", "date", "competition", "season", "away_team"]].rename(columns={"away_team": "team"}),
    ]).sort_values("date")
    long["games_played"] = long.groupby(["team", "competition", "season"]).cumcount()

    home_map = raw[["fixture_id", "home_team"]].merge(
        long.rename(columns={"team": "home_team", "games_played": "home_gis"}),
        on=["fixture_id", "home_team"], how="left")[["fixture_id", "home_gis"]]
    away_map = raw[["fixture_id", "away_team"]].merge(
        long.rename(columns={"team": "away_team", "games_played": "away_gis"}),
        on=["fixture_id", "away_team"], how="left")[["fixture_id", "away_gis"]]

    out = raw[["fixture_id"]].merge(home_map, on="fixture_id").merge(away_map, on="fixture_id").drop_duplicates("fixture_id")
    out["min_games_into_season"] = out[["home_gis", "away_gis"]].min(axis=1)
    return out[["fixture_id", "min_games_into_season"]]


def _calibrated_stream() -> pd.DataFrame:
    """Shared by compute_rolling_p95_bar() and compute_rolling_p95_under_bar()
    -- the out-of-fold prediction stream both are built from, calibrated
    the same way live predictions are.
    """
    df = load_with_xg_player_form_and_shots_venue()
    df = load_weighted_xg(df)
    df = load_team_ratings(df)
    core_stream = build_stream(df, CORE_CANDIDATES, N_FOLDS_CORE, "core").rename(columns={"pred_p": "pred_p_core"})
    xg_stream = build_stream(df, XG_CANDIDATES, N_FOLDS_XG, "xG")[["fixture_id", "pred_p"]].rename(columns={"pred_p": "pred_p_xg"})
    merged = core_stream.merge(xg_stream, on="fixture_id", how="left")
    merged["pred_p_raw"] = merged["pred_p_xg"].combine_first(merged["pred_p_core"])
    merged["model_used"] = merged["pred_p_xg"].notna().map({True: "xG", False: "core"})
    stream = merged.sort_values("date").reset_index(drop=True)

    calibrators = load_calibrators()
    stream["pred_p"] = apply_calibration(stream["pred_p_raw"], stream["model_used"], calibrators)
    return stream


def compute_rolling_p95_bar(early_season_only: bool = False, stream: pd.DataFrame | None = None) -> float:
    """The live selection bar: percentile of the trailing 500 out-of-fold
    predictions in the model's own validated history -- same construction
    as backtest_season_rolling_percentile.py. Despite the function name
    (kept for compatibility with existing callers), the percentile used
    is NOT always 95: the early-season case uses
    EARLY_SEASON_OVER_PERCENTILE=92.5 instead, re-derived in
    check_early_season_bar_sweep.py -- see EARLY_SEASON_CUTOFF's
    docstring for why 95 (and especially anything stricter) tests worse
    than 92.5 on this specific, smaller population.

    early_season_only=True restricts the trailing window to fixtures
    where min(home, away) games played this competition+season was
    <= EARLY_SEASON_CUTOFF -- a separate, stricter bar for the specific
    regime found to be overconfident (see EARLY_SEASON_CUTOFF docstring).

    stream: pass an already-built _calibrated_stream() to skip rebuilding
    it (retrains both models from scratch, the expensive part) -- a
    caller needing several of these bar/pool numbers in one run (e.g.
    explain_picks.py, cmd_snapshot) should build the stream ONCE and pass
    it to every call instead of the 7x-redundant rebuild that used to
    happen here. Omit to build fresh (used by standalone callers/scripts).
    """
    stream = stream if stream is not None else _calibrated_stream()
    percentile = 95.0
    if early_season_only:
        gis = _games_into_season_lookup()
        stream = stream.merge(gis, on="fixture_id", how="left")
        stream = stream[stream["min_games_into_season"] <= EARLY_SEASON_CUTOFF]
        percentile = EARLY_SEASON_OVER_PERCENTILE
    trailing = deque(stream["pred_p"].dropna().tail(500), maxlen=500)
    return float(pd.Series(trailing).quantile(percentile / 100.0))


def compute_rolling_p95_under_bar(early_season_only: bool = False, stream: pd.DataFrame | None = None) -> float:
    """Same construction as compute_rolling_p95_bar(), mirrored onto
    Under confidence (1 - pred_p) -- the live Under-side selection bar.
    Validated in diagnose_under_overconfidence.py: this exact rolling-p95
    selection rule, walked forward over the whole dataset, gave a 56.4%
    hit rate at a mean predicted confidence of 58.1% (n=598) -- a real,
    statistically significant edge over the ~46% baseline under-rate, but
    a smaller, less sharp edge than the Over side's bar produces, and
    mildly overconfident (-1.7pp) in its own stated probability. Worth
    surfacing, not worth treating as equally reliable as an Over PICK.

    early_season_only=True -- see compute_rolling_p95_bar(). stream --
    see compute_rolling_p95_bar()'s docstring; pass a shared, pre-built
    stream to avoid a redundant retrain.
    """
    stream = (stream if stream is not None else _calibrated_stream()).copy()
    stream["under_p"] = 1 - stream["pred_p"]
    if early_season_only:
        gis = _games_into_season_lookup()
        stream = stream.merge(gis, on="fixture_id", how="left")
        stream = stream[stream["min_games_into_season"] <= EARLY_SEASON_CUTOFF]
    trailing = deque(stream["under_p"].dropna().tail(500), maxlen=500)
    return float(pd.Series(trailing).quantile(0.95))


def compute_pool_hit_rate(
    under: bool = False, early_season_only: bool = False, stream: pd.DataFrame | None = None
) -> tuple[float, int]:
    """The number that should actually be used to price edge against a
    Kalshi ask -- NOT a fixture's own individually-stated calibrated
    probability.

    check_confidence_bar_sweep.py's bucket breakdown (651 historical
    standard-bar Over picks, split by stated confidence: 60-68%, 68-70%,
    70-72%, 72-75%, 75%+) found essentially no relationship between a
    pick's own precise number and how often it actually won -- the
    70-72% bucket outperformed the 72-75% bucket, and the correlation
    between stated confidence and outcome WITHIN the bar-clearing pool
    was 0.082, indistinguishable from zero. What IS validated is the
    pool's aggregate hit rate: of everything that clears the bar, ~70%
    of them win, regardless of which one you're looking at. So every
    pick priced off this rule should use the SAME number -- the pool's
    own historical hit rate -- not its individual stated probability,
    which claims a precision (71% vs 76%, say) the data doesn't support.

    Replays the same rolling-percentile selection rule compute_rolling_p95_bar
    / compute_rolling_p95_under_bar use (same percentile, same window,
    same warmup) forward through the whole out-of-fold history, and
    reports what fraction of the fixtures it actually selected went on
    to win. Returns (hit_rate, n).

    under=True mirrors this onto the Under side. early_season_only=True
    restricts to the early-season population and uses
    EARLY_SEASON_OVER_PERCENTILE -- but only when under=False: the
    early-season Under bar was swept in check_early_season_bar_sweep.py
    and found unreliable at every percentile tested (large negative
    calibration gaps throughout), so there is no trustworthy pool number
    for early-season Under, and callers should not offer Under picks in
    that regime at all rather than call this with under=True,
    early_season_only=True.

    stream -- see compute_rolling_p95_bar()'s docstring; pass a shared,
    pre-built stream to avoid a redundant retrain.
    """
    stream = (stream if stream is not None else _calibrated_stream()).copy()
    value_col, outcome_col = ("pred_p", "over_2_5")
    if under:
        stream["under_p"] = 1 - stream["pred_p"]
        stream["under_2_5"] = 1 - stream["over_2_5"]
        value_col, outcome_col = ("under_p", "under_2_5")

    percentile = 95.0
    if early_season_only:
        gis = _games_into_season_lookup()
        stream = stream.merge(gis, on="fixture_id", how="left")
        stream = stream[stream["min_games_into_season"] <= EARLY_SEASON_CUTOFF]
        if not under:
            percentile = EARLY_SEASON_OVER_PERCENTILE

    stream = stream.dropna(subset=[value_col]).sort_values("date").reset_index(drop=True)
    trailing: deque = deque(maxlen=500)
    picked_outcomes = []
    for _, row in stream.iterrows():
        if len(trailing) >= 200:
            bar = pd.Series(trailing).quantile(percentile / 100.0)
            if row[value_col] >= bar:
                picked_outcomes.append(row[outcome_col])
        trailing.append(row[value_col])

    if not picked_outcomes:
        return float("nan"), 0
    return float(pd.Series(picked_outcomes).mean()), len(picked_outcomes)


def team_games_into_season_live(team_name: str, competition: str, season: int, all_finished: list[dict]) -> int:
    """Live-time equivalent of _games_into_season_lookup(), for an
    upcoming fixture instead of a historical one: how many matches has
    this team already played in this competition+season, among the
    already-finished matches available right now. Used to decide whether
    a live fixture should be checked against the regular or early-season
    bar.
    """
    return sum(
        1 for m in all_finished
        if m["competition"] == competition and m["season"] == season
        and (m["home"] == team_name or m["away"] == team_name)
    )


def cmd_snapshot(days: int) -> int:
    print("Training the core model...")
    historical = load_with_player_form_and_shots_venue()
    model_df = historical[CORE_CANDIDATES + ["over_2_5"]].dropna()
    scaler = StandardScaler()
    X_train = scaler.fit_transform(model_df[CORE_CANDIDATES])
    model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    model.fit(X_train, model_df["over_2_5"])

    print("Training the xG-augmented model...")
    xg_historical = load_with_xg_player_form_and_shots_venue()
    xg_historical = load_weighted_xg(xg_historical)
    xg_historical = load_team_ratings(xg_historical)
    xg_model_df = xg_historical[XG_CANDIDATES + ["over_2_5"]].dropna()
    xg_scaler = StandardScaler()
    X_xg_train = xg_scaler.fit_transform(xg_model_df[XG_CANDIDATES])
    xg_model = LogisticRegressionCV(Cs=15, cv=5, penalty="l1", solver="liblinear", scoring="roc_auc", max_iter=2000, random_state=0)
    xg_model.fit(X_xg_train, xg_model_df["over_2_5"])

    print("Computing the live confidence bars and pool hit rates...")
    # Built ONCE, passed to all four calls below -- see compute_rolling_p95_bar()'s
    # stream= docstring for why (this used to retrain both models 4x here alone).
    shared_stream = _calibrated_stream()
    bar = compute_rolling_p95_bar(stream=shared_stream)
    early_bar = compute_rolling_p95_bar(early_season_only=True, stream=shared_stream)
    pool_over = compute_pool_hit_rate(under=False, early_season_only=False, stream=shared_stream)
    pool_over_early = compute_pool_hit_rate(under=False, early_season_only=True, stream=shared_stream)
    print(f"  bar = {bar*100:.1f}%  |  early-season bar = {early_bar*100:.1f}%")
    print(f"  pool hit rate: standard {pool_over[0]*100:.1f}% (n={pool_over[1]})  |  "
          f"early-season {pool_over_early[0]*100:.1f}% (n={pool_over_early[1]})")

    print(f"\nFetching upcoming fixtures across all leagues (next {days} days): {list(LEAGUES)}...")
    upcoming = fetch_upcoming_fixtures(days)
    all_finished = fetch_all_fixtures(None)
    state = replay_to_current_state(all_finished)

    rows = []
    for m in upcoming:
        row = build_feature_row(m, state)
        if row is not None:
            rows.append(row)
    if not rows:
        print("No fixtures had enough team history to score.")
        return 0

    live_df = pd.DataFrame(rows)
    live_df = add_derived_features(live_df)
    live_df = add_weighted_xg_derived_features(live_df)
    live_df = add_geo_mean_features(live_df)
    live_df = add_ratings_derived_features(live_df)
    live_df = add_player_form_derived_features(live_df)
    live_df = add_shots_venue_derived_features(live_df)
    standings_cache = build_standings_cache()
    live_df = add_league_finish_features(live_df, standings_cache)
    live_df = live_df.dropna(subset=CORE_CANDIDATES)
    if live_df.empty:
        print("All fixtures were missing a required feature.")
        return 0

    has_xg = live_df[
        XG_FINISHING_FEATURES + WEIGHTED_XG_RAW_FEATURES + GEO_FEATURES
        + RATINGS_RAW_FEATURES + RATINGS_DERIVED_FEATURES
    ].notna().all(axis=1)
    live_df["raw_p"] = pd.NA
    live_df["model_used"] = ""
    core_rows = live_df.loc[~has_xg]
    if not core_rows.empty:
        live_df.loc[~has_xg, "raw_p"] = model.predict_proba(scaler.transform(core_rows[CORE_CANDIDATES]))[:, 1]
        live_df.loc[~has_xg, "model_used"] = "core"
    xg_rows = live_df.loc[has_xg]
    if not xg_rows.empty:
        live_df.loc[has_xg, "raw_p"] = xg_model.predict_proba(xg_scaler.transform(xg_rows[XG_CANDIDATES]))[:, 1]
        live_df.loc[has_xg, "model_used"] = "xG"
    live_df["raw_p"] = live_df["raw_p"].astype(float)

    calibrators = load_calibrators()
    live_df["calibrated_p"] = apply_calibration(live_df["raw_p"], live_df["model_used"], calibrators)

    print("\nFetching real Kalshi prices per league...")
    kalshi_by_comp = {}
    for comp, series in KALSHI_SERIES_BY_COMPETITION.items():
        try:
            markets = fetch_kalshi_over25_for_series(series)
        except Exception as exc:
            print(f"  {comp} ({series}): could not reach Kalshi -- {exc}", file=sys.stderr)
            markets = []
        kalshi_by_comp[comp] = markets
        print(f"  {comp} ({series}): {len(markets)} open 'Over 2.5' markets")

    already_logged = set()
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            already_logged = {row["fixture_id"] for row in csv.DictReader(f)}

    new_rows = []
    for _, r in live_df.iterrows():
        fid = str(int(r["fixture_id"]))
        if fid in already_logged:
            continue
        comp = r["competition"]
        markets = kalshi_by_comp.get(comp, [])
        match = None
        for k in markets:
            if _normalize(k["home"]) == _normalize(r["home_team"]) and _normalize(k["away"]) == _normalize(r["away_team"]):
                match = k
                break

        home_gis = team_games_into_season_live(r["home_team"], comp, r["season"], all_finished)
        away_gis = team_games_into_season_live(r["away_team"], comp, r["season"], all_finished)
        is_early = min(home_gis, away_gis) <= EARLY_SEASON_CUTOFF
        effective_bar = early_bar if is_early else bar
        selected = bool(r["calibrated_p"] >= effective_bar)

        # Edge is priced off the POOL's historical hit rate, not this
        # fixture's own calibrated_p -- see compute_pool_hit_rate()'s
        # docstring (essentially zero correlation between a bar-clearing
        # pick's individual stated confidence and its actual outcome).
        # Only applies when the fixture actually clears its bar -- see
        # explain_picks.py's effective_over_prob for why blanket
        # substitution is wrong. Fee is subtracted from the effective
        # probability side, matching how it actually hits P&L (paid on
        # entry, win or lose) -- see kalshi_fee()'s docstring.
        effective_prob = float(r["calibrated_p"])
        pool_hit_rate = ""
        if selected:
            effective_prob = pool_over_early[0] if is_early else pool_over[0]
            pool_hit_rate = round(effective_prob, 4)
        fee = kalshi_fee(match["yes_ask"]) if match else None

        row = {
            "logged_at": pd.Timestamp.utcnow().isoformat(), "fixture_id": fid, "competition": comp,
            "kickoff_date": r["date"], "home_team": r["home_team"], "away_team": r["away_team"],
            "model_used": r["model_used"], "raw_p": round(float(r["raw_p"]), 4),
            "calibrated_p": round(float(r["calibrated_p"]), 4),
            "is_early_season": is_early, "effective_bar": round(effective_bar, 4), "selected": selected,
            "kalshi_ticker": match["ticker"] if match else "",
            "kalshi_yes_ask": match["yes_ask"] if match else "",
            "kalshi_yes_bid": match["yes_bid"] if match else "",
            "kalshi_no_ask": match["no_ask"] if match and match.get("no_ask") is not None else "",
            "kalshi_no_bid": match["no_bid"] if match and match.get("no_bid") is not None else "",
            "kalshi_implied_p": round(match["yes_ask"], 4) if match else "",
            "kalshi_fair_p": round(match["fair_p"], 4) if match and match.get("fair_p") is not None else "",
            "pool_hit_rate": pool_hit_rate,
            "fee_per_contract": fee if fee is not None else "",
            "edge_vs_ask": round(effective_prob - fee - match["yes_ask"], 4) if match else "",
            "edge_vs_fair": (
                round(effective_prob - fee - match["fair_p"], 4)
                if match and match.get("fair_p") is not None else ""
            ),
            "settled": False, "actual_home_goals": "", "actual_away_goals": "",
            "actual_over_2_5": "", "pnl_per_dollar_if_selected": "",
        }
        new_rows.append(row)

    if not new_rows:
        print("\nNothing new to log -- every scoreable fixture is already in the log.")
        return 0

    write_header = not os.path.exists(LOG_PATH)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    new_rows.sort(key=lambda r: r["calibrated_p"], reverse=True)
    print(f"\nLogged {len(new_rows)} new fixtures to {LOG_PATH}\n")
    print(f"{'Kickoff':<12}{'Comp':<12}{'Fixture':<38}{'Model P':<10}{'Kalshi':<10}{'EdgeAsk':<10}{'EdgeFair':<10}{'Selected'}")
    print("-" * 110)
    for r in new_rows:
        fixture = f"{r['home_team']} vs {r['away_team']}"
        kalshi_str = f"${r['kalshi_yes_ask']:.2f}" if r["kalshi_yes_ask"] != "" else "n/a"
        edge_ask_str = f"{r['edge_vs_ask']*100:+.1f}pp" if r["edge_vs_ask"] != "" else ""
        edge_fair_str = f"{r['edge_vs_fair']*100:+.1f}pp" if r["edge_vs_fair"] != "" else ""
        print(f"{r['kickoff_date']:<12}{r['competition']:<12}{fixture:<38}{r['calibrated_p']*100:5.1f}%   "
              f"{kalshi_str:<10}{edge_ask_str:<10}{edge_fair_str:<10}{'YES' if r['selected'] else ''}")

    n_selected = sum(1 for r in new_rows if r["selected"])
    n_priced = sum(1 for r in new_rows if r["kalshi_yes_ask"] != "")
    print(f"\n{n_selected}/{len(new_rows)} fixtures cleared their bar (standard {bar*100:.1f}% / "
          f"early-season {early_bar*100:.1f}%). {n_priced}/{len(new_rows)} had a real Kalshi price available.")
    return 0


def cmd_settle() -> int:
    if not os.path.exists(LOG_PATH):
        print("No log file yet -- run 'snapshot' first.")
        return 0
    with open(LOG_PATH) as f:
        rows = list(csv.DictReader(f))

    updated = 0
    for row in rows:
        if row["settled"] == "True":
            continue
        try:
            data = apifootball.get("/fixtures", {"id": row["fixture_id"]}, ttl_seconds=300)
        except apifootball.ApiFootballError:
            continue
        resp = data.get("response", [])
        if not resp or resp[0]["fixture"]["status"]["short"] != "FT":
            continue
        home_goals, away_goals = resp[0]["goals"]["home"], resp[0]["goals"]["away"]
        actual_over = (home_goals + away_goals) > 2.5
        row["actual_home_goals"], row["actual_away_goals"] = home_goals, away_goals
        # Stored as strings, not bools -- rows settled in a LATER run get
        # read back from CSV as strings ("True"/"False"), and the summary
        # check below (this same run included) compares against "True".
        # A bare Python True here would silently fail that comparison for
        # every row settled in this exact execution -- which is exactly
        # what caused the very first settle run to report "no data yet"
        # despite having just written 10 real settled+selected+priced rows.
        row["actual_over_2_5"] = "True" if actual_over else "False"
        row["settled"] = "True"
        if row["selected"] == "True" and row["kalshi_yes_ask"]:
            ask = float(row["kalshi_yes_ask"])
            # Fee is paid on entry regardless of outcome (kalshi_fee()'s
            # docstring) -- fall back to computing it fresh for rows
            # logged before fee_per_contract existed as a column.
            fee = float(row["fee_per_contract"]) if row.get("fee_per_contract") else kalshi_fee(ask)
            row["pnl_per_dollar_if_selected"] = round((1 - ask - fee) if actual_over else -(ask + fee), 4)
        updated += 1

    if updated:
        with open(LOG_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    print(f"Settled {updated} newly-finished fixture(s).")

    settled_selected = [r for r in rows if r["settled"] == "True" and r["selected"] == "True" and r["pnl_per_dollar_if_selected"]]
    if settled_selected:
        pnl = sum(float(r["pnl_per_dollar_if_selected"]) for r in settled_selected)
        wins = sum(1 for r in settled_selected if r["actual_over_2_5"] == "True")
        print(f"\nRunning forward-test record (selected picks with a real Kalshi price, settled so far):")
        print(f"  n={len(settled_selected)}  wins={wins}  hit rate={wins/len(settled_selected)*100:.1f}%  "
              f"P&L per $1 staked each = {pnl:+.2f}  (ROI={pnl/len(settled_selected)*100:+.1f}%)")
    else:
        print("No settled, selected, priced picks yet -- too early to say anything.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["snapshot", "settle"])
    parser.add_argument("--days", type=int, default=14, help="Snapshot mode: look this many days ahead, default 14")
    args = parser.parse_args()

    if args.mode == "snapshot":
        return cmd_snapshot(args.days)
    return cmd_settle()


if __name__ == "__main__":
    raise SystemExit(main())
