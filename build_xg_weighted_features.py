#!/usr/bin/env python3
"""Recency- and competition-weighted xG -- an alternative to the flat
"last 5 games" rolling average in build_xg_features.py, built in direct
response to two problems found while investigating why real xG barely
moved the Man City vs Coventry prediction:

  1. The flat rolling window is keyed purely by team, with no
     awareness of WHICH competition each game was in. A team promoted
     to a new division (Coventry, first PL football in 25 years) has
     its "last 5" made entirely of games from the OLD division, at
     full weight -- exactly the cross-league blind spot already found
     and fixed for team strength (build_league_finish_features.py) and
     Elo, just never fixed here.
  2. A flat average treats a game from 11 months ago identically to
     one from last week. "Form" isn't really that stable across a
     summer -- squads change, managers change, and recency should
     matter even within the same competition.

Both problems share one fix: weight each past game by
    recency_weight  = 0.5 ** (days_before_match / HALF_LIFE_DAYS)
    competition_weight = 1.0 if same competition as the upcoming match,
                          else CROSS_COMPETITION_DISCOUNT
    weight = recency_weight * competition_weight
and take a weighted average instead of a flat one. A cross-competition
game is discounted, not dropped entirely -- a promoted team's recent
Championship form is real evidence, just less reliable evidence than
actual top-flight games would be.

HALF_LIFE_DAYS=60 and CROSS_COMPETITION_DISCOUNT=0.4 are starting
points, not the result of a parameter search -- validate before
treating them as final.

Same no-lookahead discipline as everything else, same cached
/fixtures/statistics re-parse as build_xg_features.py (no new API
calls).

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_xg_weighted_features.py
"""

import csv
import datetime
import os
import sys
from collections import defaultdict, deque

import apifootball
from build_dataset_apifootball import fetch_all_fixtures
from build_xg_features import xg_stats_for

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xg_weighted_features.csv")
HALF_LIFE_DAYS = 60
CROSS_COMPETITION_DISCOUNT = 0.4
MIN_GAMES_FOR_ROLLING = 3
HISTORY_MAXLEN = 40  # generous cap -- decay makes anything older than a couple of half-lives negligible anyway


def weighted_avg(history: deque, match_date: datetime.datetime, competition: str) -> float | None:
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
    if total_weight <= 0:
        return None
    return total_value / total_weight


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (re-parsing cached /fixtures/statistics)")

    xg_for_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))
    xg_against_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))

    rows = []
    for i, m in enumerate(matches, start=1):
        try:
            xg = xg_stats_for(m["fixture_id"])
        except apifootball.ApiFootballError:
            xg = {}

        home_id, away_id = m["home_id"], m["away_id"]
        match_date = datetime.datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        competition = m["competition"]

        row = {
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": competition, "season": m["season"],
            "home_team": m["home"], "away_team": m["away"],
            "home_xg_last5_weighted": weighted_avg(xg_for_history[home_id], match_date, competition),
            "away_xg_last5_weighted": weighted_avg(xg_for_history[away_id], match_date, competition),
            "home_xg_against_last5_weighted": weighted_avg(xg_against_history[home_id], match_date, competition),
            "away_xg_against_last5_weighted": weighted_avg(xg_against_history[away_id], match_date, competition),
        }
        rows.append(row)

        # Update state AFTER computing this match's pre-match features.
        if home_id in xg and away_id in xg:
            home_xg, away_xg = xg[home_id]["xg"], xg[away_id]["xg"]
            xg_for_history[home_id].append({"date": match_date, "competition": competition, "value": home_xg})
            xg_against_history[home_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_for_history[away_id].append({"date": match_date, "competition": competition, "value": away_xg})
            xg_against_history[away_id].append({"date": match_date, "competition": competition, "value": home_xg})

        if i % 2000 == 0:
            print(f"  ...{i}/{len(matches)}")

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
