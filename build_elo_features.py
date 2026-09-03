#!/usr/bin/env python3
"""Team-strength Elo ratings -- the model currently has no feature that
captures "how good is this team relative to this specific opponent" as
a single continuously-updated number. Goals-scored/conceded, league
position, and points are all real signals, but they're either
short-window (last 5/10 games) or reset every season -- none of them
carry a smooth, long-horizon sense of team quality the way Elo does,
updated after every single result including margin of victory.

Standard Elo, one shared rating pool across all leagues -- cross-league
comparability doesn't matter here since every fixture pits two teams
from the SAME league against each other, so home_elo - away_elo is
always a same-league, apples-to-apples comparison regardless of how
the pools drift relative to each other. Margin-of-victory scaling
(FiveThirtyEight's soccer/NFL formula) so a 4-0 win moves ratings more
than a 1-0 win, and light season-boundary regression toward the mean
(1/3 of the way back to 1500) so a decade of matches doesn't let
ratings drift to unrealistic extremes.

Single chronological pass, same no-lookahead discipline as everything
else: a match's home_elo/away_elo are the PRE-match ratings, then the
match result updates state afterward.

Usage:
    APIFOOTBALL_KEY=xxxx python3 build_elo_features.py
"""

import csv
import math
import os
import sys

import apifootball
from build_dataset_apifootball import fetch_all_fixtures

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "elo_features.csv")

START_ELO = 1500.0
K = 20.0
HOME_ADVANTAGE = 65.0
SEASON_REGRESSION = 1 / 3  # fraction of the way back toward 1500 at each new season


def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(elo_a - elo_b) / 400.0))


def mov_multiplier(goal_diff: int, winner_elo_diff: float) -> float:
    if goal_diff == 0:
        return 1.0
    return math.log(goal_diff + 1) * (2.2 / (winner_elo_diff * 0.001 + 2.2))


def main() -> int:
    try:
        matches = fetch_all_fixtures(None)
    except apifootball.ApiFootballError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processing {len(matches)} finished matches (pure computation, no API calls)")

    elo: dict[str, float] = {}
    last_season: dict[str, int] = {}

    rows = []
    for i, m in enumerate(matches, start=1):
        home, away = m["home"], m["away"]
        season = m["season"]

        for team in (home, away):
            if team not in elo:
                elo[team] = START_ELO
                last_season[team] = season
            elif last_season[team] != season:
                elo[team] = elo[team] + (START_ELO - elo[team]) * SEASON_REGRESSION
                last_season[team] = season

        home_elo, away_elo = elo[home], elo[away]
        rows.append({
            "fixture_id": m["fixture_id"], "date": m["date"][:10],
            "competition": m["competition"], "season": season,
            "home_team": home, "away_team": away,
            "home_elo": round(home_elo, 2), "away_elo": round(away_elo, 2),
            "elo_gap": round(home_elo - away_elo, 2),
            "elo_combined": round(home_elo + away_elo, 2),
        })

        home_goals, away_goals = m["home_goals"], m["away_goals"]
        goal_diff = abs(home_goals - away_goals)
        expected_home = expected_score(home_elo + HOME_ADVANTAGE, away_elo)
        if home_goals > away_goals:
            actual_home = 1.0
            winner_elo_diff = (home_elo + HOME_ADVANTAGE) - away_elo
        elif home_goals < away_goals:
            actual_home = 0.0
            winner_elo_diff = away_elo - (home_elo + HOME_ADVANTAGE)
        else:
            actual_home = 0.5
            winner_elo_diff = 0.0
        k_eff = K * mov_multiplier(goal_diff, winner_elo_diff)
        delta = k_eff * (actual_home - expected_home)
        elo[home] = home_elo + delta
        elo[away] = away_elo - delta

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
