#!/usr/bin/env python3
"""A Champions League Over/Under 2.5 feature set built on "respective
league form" -- i.e. reusing the same weighted-xG and team-quality-rating
machinery already validated for domestic matches (build_xg_weighted_features.py,
build_team_ratings_features.py), instead of inventing a new UCL-specific
model from scratch.

Team IDs are globally consistent across API-Football competitions
(verified: Real Madrid = team_id 541 in both LaLiga and UCL fixture data),
so a team's domestic match history and its UCL match history can be
merged into one chronological timeline per team. That's enough, on its
own, for the weighted-xG features: HALF_LIFE_DAYS recency-decay and
CROSS_COMPETITION_DISCOUNT already exist specifically to blend a team's
form across competitions (originally built for a promoted team's
Championship form feeding into a Premier League prediction), so feeding
that same mechanism a team's domestic-league games AND its UCL games
needs no new code, just a wider match universe.

Team ratings are a harder problem. build_team_ratings_features.py fits
attack/defense ratings SEPARATELY per competition, by deliberate design --
a rating of "+0.3" in the Eredivisie and "+0.3" in the Premier League
are not on the same scale, and pooling them naively is exactly the flaw
that got Elo rejected (build_league_finish_features.py's docstring).
The fix used here is different from naive pooling: fit ONE joint
two-way Poisson regression (reusing fit_ratings() from
build_team_ratings_features.py unchanged -- it already takes an
arbitrary match window, agnostic to competition) across a match
universe that includes BOTH each league's domestic matches AND UCL
matches. The UCL matches are real head-to-head evidence between teams
from different leagues -- Real Madrid actually playing Bayern Munich
tells you their relative strength directly, which is a fundamentally
different (and much stronger) source of cross-league information than
Elo's implicit assumption that "the average Championship team is X
points weaker than the average Premier League team." Every team in the
pool, tracked-league or not (Celtic, Shakhtar etc. included), gets its
own attack/defense columns in the same regression -- they just don't
get MIN_MATCHES_FOR_FIT if they don't play enough, and fall back to 0
(average) like any cold-start team.

Scope: checked empirically before building this (xg_stats_for coverage
across all 959 UCL matches 2011-2025 involving two teams from the 7
relevant leagues) -- API-Football has ZERO expected_goals coverage for
UCL fixtures before the 2022 season, then 41% in 2022 and ~98-100% from
2023 on. There is no point building a feature set around weighted xG /
the geo-mean combiner for seasons that don't have the underlying stat
at all, so the target evaluation set is restricted to UCL matches where
both teams belong to one of the 7 relevant tracked leagues (PL, LaLiga,
Bundesliga, Serie A, Ligue 1, Eredivisie, Sueper Lig -- deliberately
excluding ELC and MLS, which don't send teams to the UCL) AND season >=
TARGET_MIN_SEASON=2022. ~278-314 matches at last count
(check_champions_league_full_dataset.py has the up-to-date number and
the actual L1/walk-forward gate result) -- a real sample, but nowhere
near the tens of thousands the domestic full-dataset checks get, and
this has NOT been through a significance/bootstrap pass the way the
geo-mean or weighting-sweep features were.

The joint ratings universe (matches fed into the periodic pooled
refit) starts UNIVERSE_START=2021-01-01, over a year before the
earliest 2022 target match -- comfortably more than LOOKBACK_DAYS=450
of runway -- without dragging in years of pre-2021 history that would
only slow down every refit for zero benefit to a 2022+ evaluation
window.

REFIT_INTERVAL_DAYS/LOOKBACK_DAYS/MIN_MATCHES_FOR_FIT/ALPHA for the
joint ratings fit reuse the same defaults as the domestic-only ratings
build -- still just starting points there, doubly so here since this
joint version has never been swept at all. Documented, not hidden.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_champions_league_features.py
"""

import bisect
import csv
import datetime
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import poisson

import apifootball
from build_dataset_apifootball import LEAGUES, fetch_all_fixtures
from build_team_ratings_features import ALPHA, LOOKBACK_DAYS, MIN_MATCHES_FOR_FIT, REFIT_INTERVAL_DAYS, fit_ratings
from build_xg_features import xg_stats_for
from build_xg_weighted_features import CROSS_COMPETITION_DISCOUNT, HALF_LIFE_DAYS, HISTORY_MAXLEN, MIN_GAMES_FOR_ROLLING

UCL_LEAGUE_ID = 2
UNIVERSE_START = "2021-01-01"
TARGET_MIN_SEASON = 2022

RELEVANT_LEAGUES = {"PL", "LALIGA", "BUNDESLIGA", "SERIEA", "LIGUE1", "EREDIVISIE", "SUPERLIG"}

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "champions_league_features.csv")

RATINGS_RAW_FEATURES = [
    "home_attack_rating_joint", "home_defense_rating_joint",
    "away_attack_rating_joint", "away_defense_rating_joint",
]
RATINGS_DERIVED_FEATURES = [
    "home_expected_rating_joint", "away_expected_rating_joint",
    "combined_expected_rating_joint", "rating_gap_joint",
]
WEIGHTED_XG_RAW_FEATURES = [
    "home_xg_last5_weighted_ucl", "away_xg_last5_weighted_ucl",
    "home_xg_against_last5_weighted_ucl", "away_xg_against_last5_weighted_ucl",
]
GEO_FEATURES = [
    "home_expected_geo_ucl", "away_expected_geo_ucl",
    "combined_expected_geo_ucl", "poisson_p_over_geo_ucl",
]

ALL_FEATURES = RATINGS_RAW_FEATURES + RATINGS_DERIVED_FEATURES + WEIGHTED_XG_RAW_FEATURES + GEO_FEATURES


def fetch_ucl_fixtures(season_start: int, season_end: int) -> list[dict]:
    """All finished UCL fixtures, any teams -- non-tracked-league clubs
    (Celtic, Shakhtar etc.) are kept in the universe as bridge/connectivity
    nodes for the joint ratings fit even though they'll never appear in
    the target evaluation set."""
    matches = []
    for season in range(season_start, season_end + 1):
        data = apifootball.get("/fixtures", {"league": UCL_LEAGUE_ID, "season": season})
        for m in data.get("response", []):
            if m["fixture"]["status"]["short"] != "FT":
                continue
            matches.append({
                "fixture_id": m["fixture"]["id"],
                "date": m["fixture"]["date"],
                "competition": "UCL",
                "season": season,
                "home": m["teams"]["home"]["name"],
                "away": m["teams"]["away"]["name"],
                "home_id": m["teams"]["home"]["id"],
                "away_id": m["teams"]["away"]["id"],
                "home_goals": m["goals"]["home"],
                "away_goals": m["goals"]["away"],
            })
    return matches


def build_universe() -> tuple[list[dict], list[dict]]:
    """Returns (universe, target_matches). universe = every match (domestic
    7-league + UCL) from UNIVERSE_START onward, sorted by date -- this is
    what ratings/xG get fit on. target_matches = the subset of UCL matches
    where both teams belong to the 7 relevant leagues and season >=
    TARGET_MIN_SEASON -- this is the actual O/U 2.5 dataset."""
    print("Fetching domestic fixtures for the 7 relevant leagues (cached)...")
    all_domestic = fetch_all_fixtures(None)
    domestic = [m for m in all_domestic if m["competition"] in RELEVANT_LEAGUES and m["date"] >= UNIVERSE_START]
    print(f"  {len(domestic)} domestic matches from {UNIVERSE_START} onward")

    current_year = datetime.date.today().year if datetime.date.today().month >= 7 else datetime.date.today().year - 1
    print("Fetching UCL fixtures (live pull where not cached)...")
    ucl = fetch_ucl_fixtures(2021, current_year)
    ucl = [m for m in ucl if m["date"] >= UNIVERSE_START]
    print(f"  {len(ucl)} UCL matches from {UNIVERSE_START} onward")

    relevant_team_ids = set()
    for m in all_domestic:
        if m["competition"] in RELEVANT_LEAGUES:
            relevant_team_ids.add(m["home_id"])
            relevant_team_ids.add(m["away_id"])

    target_matches = [
        m for m in ucl
        if m["home_id"] in relevant_team_ids and m["away_id"] in relevant_team_ids and m["season"] >= TARGET_MIN_SEASON
    ]
    print(f"  {len(target_matches)} target UCL matches (both teams in the 7 relevant leagues, season >= {TARGET_MIN_SEASON})")

    universe = domestic + ucl
    universe.sort(key=lambda m: m["date"])
    return universe, target_matches


def compute_joint_ratings(universe: list[dict], target_fixture_ids: set[int]) -> dict[int, dict]:
    """Single chronological pass over the FULL universe (domestic + UCL,
    every league pooled into one regression), refitting fit_ratings()
    periodically same as the domestic-only version -- except here the
    window is never split by competition, so UCL bridge matches pull
    every league's ratings onto one shared scale. Only captures output
    for fixture_ids in target_fixture_ids (no reason to materialize
    ratings for the ~17k domestic/UCL matches that aren't part of the
    evaluation set)."""
    dates = [datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00")) for m in universe]

    attack: dict[str, float] = {}
    defense: dict[str, float] = {}
    last_refit_date: datetime.datetime | None = None
    fitted = False
    out: dict[int, dict] = {}

    for i, m in enumerate(universe):
        match_date = dates[i]

        if last_refit_date is None or (match_date - last_refit_date).days >= REFIT_INTERVAL_DAYS:
            lookback_start = match_date - datetime.timedelta(days=LOOKBACK_DAYS)
            left = bisect.bisect_left(dates, lookback_start, hi=i)
            window = universe[left:i]
            if len(window) >= MIN_MATCHES_FOR_FIT:
                attack, defense = fit_ratings(window)
                fitted = True
            last_refit_date = match_date

        if m["fixture_id"] in target_fixture_ids:
            out[m["fixture_id"]] = {
                "home_attack_rating_joint": attack.get(m["home"], 0.0) if fitted else None,
                "home_defense_rating_joint": defense.get(m["home"], 0.0) if fitted else None,
                "away_attack_rating_joint": attack.get(m["away"], 0.0) if fitted else None,
                "away_defense_rating_joint": defense.get(m["away"], 0.0) if fitted else None,
            }

    return out


def weighted_avg(history: list[dict], match_date: datetime.datetime, competition: str) -> float | None:
    if len(history) < MIN_GAMES_FOR_ROLLING:
        return None
    total_weight = 0.0
    total_value = 0.0
    for entry in history:
        days_before = (match_date - entry["date"]).days
        recency_weight = 0.5 ** (days_before / HALF_LIFE_DAYS)
        comp_weight = 1.0 if entry["competition"] == competition else CROSS_COMPETITION_DISCOUNT
        w = recency_weight * comp_weight
        total_weight += w
        total_value += w * entry["value"]
    return total_value / total_weight if total_weight > 0 else None


def compute_weighted_xg(universe: list[dict], target_fixture_ids: set[int]) -> dict[int, dict]:
    """Same recency/cross-competition-weighted rolling xG as
    build_xg_weighted_features.py, replayed over the combined
    domestic+UCL universe per team -- a team's UCL history counts at
    full weight for a future UCL match, its domestic history at
    CROSS_COMPETITION_DISCOUNT, exactly the existing mechanism, just
    fed a wider match list."""
    from collections import defaultdict, deque

    xg_for_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))
    xg_against_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))

    out: dict[int, dict] = {}
    for i, m in enumerate(universe):
        home_id, away_id = m["home_id"], m["away_id"]
        match_date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        competition = m["competition"]

        if m["fixture_id"] in target_fixture_ids:
            out[m["fixture_id"]] = {
                "home_xg_last5_weighted_ucl": weighted_avg(list(xg_for_history[home_id]), match_date, competition),
                "away_xg_last5_weighted_ucl": weighted_avg(list(xg_for_history[away_id]), match_date, competition),
                "home_xg_against_last5_weighted_ucl": weighted_avg(list(xg_against_history[home_id]), match_date, competition),
                "away_xg_against_last5_weighted_ucl": weighted_avg(list(xg_against_history[away_id]), match_date, competition),
            }

        try:
            xg = xg_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            xg = {}
        if home_id in xg and away_id in xg:
            home_xg, away_xg = xg[home_id]["xg"], xg[away_id]["xg"]
            xg_for_history[home_id].append({"date": match_date, "competition": competition, "value": home_xg})
            xg_against_history[home_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_for_history[away_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_against_history[away_id].append({"date": match_date, "competition": competition, "value": home_xg})

        if (i + 1) % 5000 == 0:
            print(f"  ...weighted xG replay {i + 1}/{len(universe)}")

    return out


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    df["home_expected_rating_joint"] = df["home_attack_rating_joint"] + df["away_defense_rating_joint"]
    df["away_expected_rating_joint"] = df["away_attack_rating_joint"] + df["home_defense_rating_joint"]
    df["combined_expected_rating_joint"] = df["home_expected_rating_joint"] + df["away_expected_rating_joint"]
    df["rating_gap_joint"] = df["home_expected_rating_joint"] - df["away_expected_rating_joint"]

    for c in WEIGHTED_XG_RAW_FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["home_expected_geo_ucl"] = np.sqrt(df["home_xg_last5_weighted_ucl"] * df["away_xg_against_last5_weighted_ucl"])
    df["away_expected_geo_ucl"] = np.sqrt(df["away_xg_last5_weighted_ucl"] * df["home_xg_against_last5_weighted_ucl"])
    df["combined_expected_geo_ucl"] = df["home_expected_geo_ucl"] + df["away_expected_geo_ucl"]
    df["poisson_p_over_geo_ucl"] = 1 - poisson.cdf(2, df["combined_expected_geo_ucl"] / 2)
    return df


def main() -> int:
    try:
        universe, target_matches = build_universe()
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    target_fixture_ids = {m["fixture_id"] for m in target_matches}
    print(f"\nUniverse: {len(universe)} matches ({universe[0]['date'][:10]} to {universe[-1]['date'][:10]})")

    print("\nFitting joint cross-league ratings (single pooled regression, periodic refit)...")
    ratings = compute_joint_ratings(universe, target_fixture_ids)

    print("\nReplaying recency-weighted xG across the combined domestic+UCL history...")
    weighted_xg = compute_weighted_xg(universe, target_fixture_ids)

    rows = []
    for m in target_matches:
        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10], "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_goals": m["home_goals"], "away_goals": m["away_goals"],
            "total_goals": m["home_goals"] + m["away_goals"],
            "over_2_5": 1 if (m["home_goals"] + m["away_goals"]) > 2.5 else 0,
        }
        row.update(ratings.get(m["fixture_id"], {}))
        row.update(weighted_xg.get(m["fixture_id"], {}))
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df = add_derived_features(df)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"\nWrote {len(df)} rows to {OUTPUT_PATH}")
    coverage = df[ALL_FEATURES].notna().all(axis=1).sum()
    print(f"{coverage}/{len(df)} rows have complete features (no cold-start gaps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
