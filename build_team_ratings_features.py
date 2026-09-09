#!/usr/bin/env python3
"""Team-quality ratings via a jointly-solved two-way (attack/defense)
Poisson regression -- a structurally different fix from Elo
(build_elo_features.py, tested and rejected) for the same underlying
problem: the model has no feature that measures a team's quality
ADJUSTED for the quality of opponents it has actually played, only raw
per-team scoring/conceding history. Elo was rejected because it pools
one shared rating scale across every division a team has ever played
in (see build_league_finish_features.py's fix for the same issue in a
different feature). This uses the same fix Elo needed but never got:
fit separately per competition, never pooled across divisions.

The mechanism is different from Elo too, not just the division split.
Elo updates one scalar per team sequentially, one match at a time, so a
team's rating today is entangled with the order results happened to
arrive in. This instead fits attack_rating[team] and defense_rating[team]
for every team in a competition SIMULTANEOULSY via one regression, the
standard Dixon-Coles-style setup: each match contributes two rows (home
team's goals scored, away team's goals scored), features are one-hot
"team" (scorer, for attack) and one-hot "opponent" (for defense), target
is goals, fit via Poisson GLM. A team's attack rating already accounts
for the fact that low goals against a stacked defense should count for
more than the same tally against a leaky one, and vice versa for
defense -- because attack and defense for every team are solved for at
once, not from each team's isolated average.

sklearn's PoissonRegressor (L2-regularized) is used instead of an
unregularized GLM: with a full one-hot per team on both sides plus an
intercept, the design is rank-deficient (adding a constant to every
attack rating and subtracting it from every defense rating leaves
predictions unchanged), so an unregularized fit needs an arbitrary
reference team pinned at 0. Ridge shrinkage instead pulls every team
toward 0 (the competition's average team), which is well-defined,
symmetric, and doesn't quietly make one arbitrary team's rating the
scale everyone else is measured against.

Point-in-time safety: ratings are refit periodically (every
REFIT_INTERVAL_DAYS), each time using only matches strictly before the
refit date, within a trailing LOOKBACK_DAYS window (so ratings track
the CURRENT squad, not a team's form from 3 seasons ago). A match's
home/away rating features always come from the most recent refit that
was itself computed from strictly earlier matches -- never from a fit
that has seen this match or any later one. A team absent from the
current window (newly promoted, or too early in the dataset to have
enough history) gets rating 0 -- "no evidence yet, assume average" --
the same cold-start convention every other rolling feature in this
project uses (None/NaN, here 0 as this is the model's own zero-point).

REFIT_INTERVAL_DAYS=21, LOOKBACK_DAYS=450, ALPHA=2.0 and
MIN_MATCHES_FOR_FIT=100 are starting points, not the result of a
parameter search -- validate before treating them as final.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_team_ratings_features.py
"""

import csv
import datetime
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import PoissonRegressor

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "team_ratings_features.csv")

REFIT_INTERVAL_DAYS = 21
LOOKBACK_DAYS = 450
MIN_MATCHES_FOR_FIT = 100
ALPHA = 2.0

RATINGS_RAW_FEATURES = [
    "home_attack_rating", "home_defense_rating",
    "away_attack_rating", "away_defense_rating",
]
RATINGS_DERIVED_FEATURES = [
    "home_expected_rating", "away_expected_rating",
    "combined_expected_rating", "rating_gap",
]


def add_ratings_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same derivations as the weighted-xG combo features, computed from
    the two-way regression ratings instead -- shared by the historical
    loader and the live scorer so a live prediction derives these
    identically to training.
    """
    df["home_expected_rating"] = df["home_attack_rating"] + df["away_defense_rating"]
    df["away_expected_rating"] = df["away_attack_rating"] + df["home_defense_rating"]
    df["combined_expected_rating"] = df["home_expected_rating"] + df["away_expected_rating"]
    df["rating_gap"] = df["home_expected_rating"] - df["away_expected_rating"]
    return df


def load_team_ratings(df: pd.DataFrame) -> pd.DataFrame:
    """Merge the precomputed ratings CSV onto df by fixture_id and add the
    derived combo features -- for historical training data only; a live
    prediction gets its raw ratings directly from the live refit (see
    predict_upcoming.py wiring), then just needs
    add_ratings_derived_features called on top.
    """
    ratings_df = pd.read_csv(OUTPUT_PATH)[["fixture_id"] + RATINGS_RAW_FEATURES]
    df = df.merge(ratings_df, on="fixture_id", how="left")
    return add_ratings_derived_features(df)


def fit_ratings(window: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """Jointly solve attack_rating[team]/defense_rating[team] for every
    team appearing in `window` (matches from ONE competition, already
    filtered to strictly-before-refit-date). Two rows per match: the
    home team's goals (is_home=1) and the away team's goals (is_home=0),
    each carrying a one-hot "scorer" (attack) and one-hot "opponent"
    (defense) column.
    """
    teams = sorted({m["home"] for m in window} | {m["away"] for m in window})
    team_idx = {t: i for i, t in enumerate(teams)}
    n_teams = len(teams)

    n_rows = len(window) * 2
    # Columns: [is_home, attack_0..attack_{n-1}, defense_0..defense_{n-1}]
    X = np.zeros((n_rows, 1 + 2 * n_teams))
    y = np.zeros(n_rows)

    for i, m in enumerate(window):
        home_i, away_i = team_idx[m["home"]], team_idx[m["away"]]

        r = 2 * i
        X[r, 0] = 1.0
        X[r, 1 + home_i] = 1.0
        X[r, 1 + n_teams + away_i] = 1.0
        y[r] = m["home_goals"]

        r = 2 * i + 1
        X[r, 0] = 0.0
        X[r, 1 + away_i] = 1.0
        X[r, 1 + n_teams + home_i] = 1.0
        y[r] = m["away_goals"]

    model = PoissonRegressor(alpha=ALPHA, max_iter=300)
    model.fit(X, y)

    attack = {t: model.coef_[1 + team_idx[t]] for t in teams}
    defense = {t: model.coef_[1 + n_teams + team_idx[t]] for t in teams}
    return attack, defense


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (pure computation, no API calls)")

    by_competition: dict[str, list[dict]] = {}
    for m in matches:
        by_competition.setdefault(m["competition"], []).append(m)

    rows = []
    for comp, comp_matches in by_competition.items():
        comp_matches.sort(key=lambda m: m["date"])
        print(f"  {comp}: {len(comp_matches)} matches")

        attack: dict[str, float] = {}
        defense: dict[str, float] = {}
        last_refit_date: datetime.datetime | None = None
        fitted = False  # distinguishes "no fit has happened yet" (NaN, same
        # cold-start convention as every other rolling feature) from "fit
        # exists but this specific team isn't in it" (0.0 = assume average,
        # documented above) -- otherwise every pre-first-fit row would
        # silently get a fake "average team" rating instead of being
        # excluded from training like a real cold start.

        for m in comp_matches:
            match_date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))

            if last_refit_date is None or (match_date - last_refit_date).days >= REFIT_INTERVAL_DAYS:
                lookback_start = match_date - datetime.timedelta(days=LOOKBACK_DAYS)
                window = [
                    w for w in comp_matches
                    if lookback_start <= datetime.datetime.fromisoformat(w["date"].replace("Z", "+00:00")) < match_date
                ]
                if len(window) >= MIN_MATCHES_FOR_FIT:
                    attack, defense = fit_ratings(window)
                    fitted = True
                last_refit_date = match_date

            rows.append({
                "fixture_id": m["fixture_id"], "date": m["date"][:10],
                "competition": comp, "season": m["season"],
                "home_team": m["home"], "away_team": m["away"],
                "home_attack_rating": attack.get(m["home"], 0.0) if fitted else None,
                "home_defense_rating": defense.get(m["home"], 0.0) if fitted else None,
                "away_attack_rating": attack.get(m["away"], 0.0) if fitted else None,
                "away_defense_rating": defense.get(m["away"], 0.0) if fitted else None,
            })

    rows.sort(key=lambda r: r["date"])

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
